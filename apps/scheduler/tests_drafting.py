# 排班草稿生成与确认保存（第六阶段）测试。
import datetime
import threading
import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import close_old_connections, transaction
from django.test import TestCase, TransactionTestCase

from apps.accounts.models import UserProfile
from apps.auditlog.models import OperationLog
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.drafting import (
    confirm_schedule_draft,
    create_schedule_draft,
)
from apps.scheduler.models import ScheduleBatch, ScheduleRecord
from apps.scheduler.planning import ShiftScheduleResult
from apps.scheduler.selectors import batch_summaries

User = get_user_model()


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class DraftingFixtureMixin:
    @classmethod
    def _build_fixture(cls):
        cls.super_user = make_user("draft_super")
        cls.org_a = OrganizationUnit.objects.create(name="甲队", code="ORG-A")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="ORG-B")
        cls.admin_a = make_user("draft_admin_a", role="admin", organization=cls.org_a)
        cls.admin_b = make_user("draft_admin_b", role="admin", organization=cls.org_b)
        cls.member_a = make_user("draft_member_a", role="member", organization=cls.org_a)
        cls.team_a = Team.objects.create(organization=cls.org_a, name="甲一班", code="TA-01")
        cls.early = cls.team_a.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.early.daily_worker_count = 3
        cls.early.required_shifts = 18
        cls.early.position_requirements = {"电工": 1}
        cls.early.save(
            update_fields=["daily_worker_count", "required_shifts", "position_requirements"]
        )
        cls.emp_e1 = Employee.objects.create(employee_no="D001", name="电工甲", team=cls.team_a, shift_config=cls.early, position="电工", min_required_shifts=18)
        cls.emp_e2 = Employee.objects.create(employee_no="D002", name="电工乙", team=cls.team_a, shift_config=cls.early, position="电工", min_required_shifts=18)
        cls.workers = [
            Employee.objects.create(
                employee_no=f"D00{i + 2}", name=f"普工{i}", team=cls.team_a,
                shift_config=cls.early, position="", min_required_shifts=18,
            )
            for i in range(1, 4)
        ]
        # 名额紧张：豁免 2 名普工（K=floor(2D/18)=3），否则分配不可行
        cls.early.exempt_employee_ids = [w.pk for w in cls.workers[:2]]
        cls.early.save(update_fields=["exempt_employee_ids"])
        cls.all_employees = list(Employee.objects.filter(team=cls.team_a).order_by("employee_no"))


class DraftGenerationTests(DraftingFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build_fixture()

    def test_normal_draft_generation(self):
        result = create_schedule_draft(self.early, self.super_user)
        self.assertEqual(result["errors"], [])
        draft = result["draft"]
        self.assertIsNotNone(draft)
        self.assertEqual(draft.status, ScheduleBatch.Status.DRAFT)
        total_days = (draft.period_end - draft.period_start).days + 1
        # 明细 = 全量 职工×天（含休息行）
        self.assertEqual(len(draft.payload), 5 * total_days)
        work_rows = [i for i in draft.payload if i["shift_type"] != ScheduleRecord.ShiftType.REST]
        self.assertEqual(len(work_rows), total_days * 3)
        # 岗位对应关系正确
        electrician_rows = [i for i in work_rows if i["position"] == "电工"]
        self.assertEqual(len(electrician_rows), total_days)  # 每天恰 1 名电工
        self.assertTrue(
            {i["employee_id"] for i in electrician_rows} <= {self.emp_e1.pk, self.emp_e2.pk}
        )
        # 草稿不进入正式排班查询
        self.assertEqual(
            ScheduleRecord.objects.filter(schedule_batch_id=draft.batch_id).count(), 0
        )
        summaries = batch_summaries(self.team_a, draft.period_start, draft.period_end)
        self.assertNotIn(draft.batch_id, {s["batch_id"] for s in summaries})
        # 生成日志
        log = OperationLog.objects.filter(module="schedule", action="create", target_id=str(draft.batch_id))
        self.assertEqual(log.count(), 1)

    def test_unknown_status_cannot_generate(self):
        fake = ShiftScheduleResult(status="unknown", diagnostics=["求解超时"])
        with mock.patch("apps.scheduler.drafting.solve_shift_schedule", return_value=fake):
            result = create_schedule_draft(self.early, self.super_user)
        self.assertIsNone(result["draft"])
        self.assertTrue(any("求解超时" in e for e in result["errors"]))
        self.assertEqual(ScheduleBatch.objects.count(), 0)

    def test_infeasible_cannot_generate(self):
        self.early.daily_worker_count = 18
        self.early.save(update_fields=["daily_worker_count"])
        result = create_schedule_draft(self.early, self.super_user)
        self.assertIsNone(result["draft"])
        self.assertTrue(any("无法满足每日人数" in e for e in result["errors"]))
        self.assertEqual(ScheduleBatch.objects.count(), 0)

    def test_invalid_cannot_generate(self):
        fake = ShiftScheduleResult(status="invalid", validation_errors=["某天人数不符"])
        with mock.patch("apps.scheduler.drafting.solve_shift_schedule", return_value=fake):
            result = create_schedule_draft(self.early, self.super_user)
        self.assertIsNone(result["draft"])
        self.assertTrue(any("独立校验失败" in e for e in result["errors"]))

    def test_draft_failure_leaves_no_partial_data(self):
        with mock.patch(
            "apps.scheduler.drafting.ScheduleBatch.save", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                create_schedule_draft(self.early, self.super_user)
        self.assertEqual(ScheduleBatch.objects.count(), 0)
        self.assertEqual(
            OperationLog.objects.filter(module="schedule", action="create").count(), 0
        )

    def test_member_cannot_generate(self):
        from django.core.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied):
            create_schedule_draft(self.early, self.member_a)

    def test_admin_cross_org_cannot_generate(self):
        from django.http import Http404

        with self.assertRaises(Http404):
            create_schedule_draft(self.early, self.admin_b)


class DraftConfirmTests(DraftingFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build_fixture()

    def _make_draft(self):
        result = create_schedule_draft(self.early, self.super_user)
        self.assertEqual(result["errors"], [])
        return result["draft"]

    def test_normal_confirm_writes_complete_records(self):
        draft = self._make_draft()
        result = confirm_schedule_draft(draft.batch_id, self.admin_a)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["record_count"], len(draft.payload))
        draft.refresh_from_db()
        self.assertEqual(draft.status, ScheduleBatch.Status.CONFIRMED)
        self.assertIsNotNone(draft.confirmed_at)
        self.assertEqual(draft.confirmed_by_id, self.admin_a.pk)
        # 正式记录完整且全部 confirmed
        records = ScheduleRecord.objects.filter(schedule_batch_id=draft.batch_id)
        self.assertEqual(records.count(), len(draft.payload))
        self.assertFalse(records.exclude(status=ScheduleRecord.Status.CONFIRMED).exists())
        # 每日人数与岗位人数与配置精确匹配
        total_days = (draft.period_end - draft.period_start).days + 1
        for day in range(total_days):
            current = draft.period_start + datetime.timedelta(days=day)
            self.assertEqual(
                records.filter(work_date=current)
                .exclude(shift_type=ScheduleRecord.ShiftType.REST)
                .count(),
                3,
            )
            self.assertEqual(
                records.filter(work_date=current, position="电工").count(), 1
            )
        # 确认日志
        self.assertEqual(
            OperationLog.objects.filter(
                module="schedule", action="confirm", target_id=str(draft.batch_id)
            ).count(),
            1,
        )

    def test_duplicate_confirm_rejected(self):
        draft = self._make_draft()
        first = confirm_schedule_draft(draft.batch_id, self.admin_a)
        self.assertTrue(first["confirmed"])
        second = confirm_schedule_draft(draft.batch_id, self.admin_a)
        # 同一批次重复确认仍被状态检查拒绝（不写第二次、不作废自己）
        self.assertFalse(second["confirmed"])
        self.assertTrue(any("已确认" in e for e in second["errors"]))
        records = ScheduleRecord.objects.filter(schedule_batch_id=draft.batch_id)
        self.assertEqual(records.count(), len(draft.payload))

    def test_config_change_blocks_confirm(self):
        draft = self._make_draft()
        self.early.daily_worker_count = 4
        self.early.save(update_fields=["daily_worker_count"])
        result = confirm_schedule_draft(draft.batch_id, self.admin_a)
        self.assertFalse(result["confirmed"])
        self.assertTrue(any("已过期" in e for e in result["errors"]))
        self.assertEqual(
            ScheduleRecord.objects.filter(schedule_batch_id=draft.batch_id).count(), 0
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, ScheduleBatch.Status.DRAFT)

    def test_employee_deactivated_blocks_confirm(self):
        draft = self._make_draft()
        self.emp_e1.is_active = False
        self.emp_e1.save(update_fields=["is_active"])
        result = confirm_schedule_draft(draft.batch_id, self.admin_a)
        self.assertFalse(result["confirmed"])
        self.assertTrue(any("已变化" in e for e in result["errors"]))

    def test_position_change_blocks_confirm(self):
        draft = self._make_draft()
        self.emp_e1.position = "皮带"
        self.emp_e1.save(update_fields=["position"])
        result = confirm_schedule_draft(draft.batch_id, self.admin_a)
        self.assertFalse(result["confirmed"])
        self.assertTrue(any("已变化" in e for e in result["errors"]))

    def test_conflict_with_existing_formal_records(self):
        from apps.scheduler.services import create_schedule_record, new_schedule_batch_id

        draft = self._make_draft()
        first_date = draft.period_start
        # 预置另一批次的正式记录，与草稿某职工同一天冲突
        create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_a, team=self.team_a, shift_config=self.early,
            employee=self.emp_e1, work_date=first_date,
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        result = confirm_schedule_draft(draft.batch_id, self.admin_a)
        self.assertFalse(result["confirmed"])
        self.assertTrue(any("正式排班记录冲突" in e for e in result["errors"]))

    def test_mid_save_exception_rolls_back(self):
        draft = self._make_draft()

        def broken(*args, **kwargs):
            raise RuntimeError("中途失败")

        with mock.patch("apps.scheduler.drafting.create_schedule_record", side_effect=broken):
            with self.assertRaises(RuntimeError):
                confirm_schedule_draft(draft.batch_id, self.admin_a)
        # 正式记录零残留、批次保持草稿
        self.assertEqual(
            ScheduleRecord.objects.filter(schedule_batch_id=draft.batch_id).count(), 0
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, ScheduleBatch.Status.DRAFT)

    def test_cross_org_confirm_forbidden(self):
        from django.core.exceptions import PermissionDenied

        draft = self._make_draft()
        with self.assertRaises(PermissionDenied):
            confirm_schedule_draft(draft.batch_id, self.admin_b)
        with self.assertRaises(PermissionDenied):
            confirm_schedule_draft(draft.batch_id, self.member_a)

    def test_member_cannot_confirm(self):
        from django.core.exceptions import PermissionDenied

        draft = self._make_draft()
        with self.assertRaises(PermissionDenied):
            confirm_schedule_draft(draft.batch_id, self.member_a)


class ConcurrentConfirmTests(TransactionTestCase):
    # 并发确认：使用真实 MySQL 事务与行锁，需要独立连接，故用 TransactionTestCase
    def test_concurrent_confirm_saves_once(self):
        # 手动构建夹具（TransactionTestCase 数据跨连接可见）
        org = OrganizationUnit.objects.create(name="甲队", code="ORG-A")
        team = Team.objects.create(organization=org, name="甲一班", code="TA-01")
        early = team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        early.daily_worker_count = 3
        early.required_shifts = 18
        early.save(update_fields=["daily_worker_count", "required_shifts"])
        admin = make_user("draft_conc_admin", role="admin", organization=org)
        conc_employees = [
            Employee.objects.create(
                employee_no=f"C00{i}", name=f"并发{i}", team=team,
                shift_config=early, min_required_shifts=18,
            )
            for i in range(1, 6)
        ]
        early.exempt_employee_ids = [conc_employees[0].pk, conc_employees[1].pk]
        early.save(update_fields=["exempt_employee_ids"])
        result = create_schedule_draft(early, make_user("draft_conc_super"))
        self.assertEqual(result["errors"], [])
        batch_id = result["draft"].batch_id

        outcomes = []
        barrier = threading.Barrier(2)

        def worker():
            close_old_connections()
            user = User.objects.get(pk=admin.pk)
            barrier.wait()
            try:
                outcome = confirm_schedule_draft(batch_id, user)
                outcomes.append(("ok", outcome))
            except Exception as exc:  # pragma: no cover
                outcomes.append(("error", repr(exc)))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(len(outcomes), 2)
        successes = [o for o in outcomes if o[0] == "ok" and o[1]["confirmed"]]
        rejections = [o for o in outcomes if o[0] == "ok" and not o[1]["confirmed"]]
        # 恰好一个成功，另一个被状态检查拒绝（重复确认）
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(rejections), 1)
        batch = ScheduleBatch.objects.get(batch_id=batch_id)
        self.assertEqual(batch.status, ScheduleBatch.Status.CONFIRMED)
        records = ScheduleRecord.objects.filter(schedule_batch_id=batch_id)
        self.assertEqual(records.count(), 5 * ((batch.period_end - batch.period_start).days + 1))


class DraftRaceHardeningTests(TransactionTestCase):
    # 专项审查后的并发/复核加固测试：真实触发竞争条件（真实 MySQL 锁），非串行模拟。
    def _fixture(self):
        org = OrganizationUnit.objects.create(name="甲队", code="ORG-A")
        team = Team.objects.create(organization=org, name="甲一班", code="TA-01")
        early = team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        early.daily_worker_count = 3
        early.required_shifts = 18
        early.save(update_fields=["daily_worker_count", "required_shifts"])
        admin = make_user("draft_race_super")
        race_employees = [
            Employee.objects.create(
                employee_no=f"X00{i}", name=f"竞争{i}", team=team,
                shift_config=early, min_required_shifts=18,
            )
            for i in range(1, 6)
        ]
        early.exempt_employee_ids = [race_employees[0].pk, race_employees[1].pk]
        early.save(update_fields=["exempt_employee_ids"])
        return org, team, early, admin

    def test_concurrent_cross_batch_same_config_one_wins(self):
        _, team, early, admin = self._fixture()
        draft_a = create_schedule_draft(early, admin)["draft"]
        draft_b = create_schedule_draft(early, admin)["draft"]

        block_a = threading.Event()
        release_a = threading.Event()
        real_create = __import__(
            "apps.scheduler.drafting", fromlist=["create_schedule_record"]
        ).create_schedule_record
        gated_batches = {str(draft_a.batch_id), str(draft_b.batch_id)}

        def gated_create(*args, **kwargs):
            # 无论哪个批次先进入写入循环（锁竞争赢家），都挂起直到放行
            if str(kwargs.get("schedule_batch_id")) in gated_batches:
                block_a.set()
                release_a.wait(timeout=30)
            return real_create(*args, **kwargs)

        outcomes = {}
        thread_errors = []
        barrier = threading.Barrier(2)

        def worker(batch, key):
            try:
                close_old_connections()
                user = User.objects.get(pk=admin.pk)
                barrier.wait()
                outcomes[key] = confirm_schedule_draft(batch.batch_id, user)
            except Exception as exc:  # pragma: no cover
                thread_errors.append((key, repr(exc)))

        with mock.patch(
            "apps.scheduler.drafting.create_schedule_record", side_effect=gated_create
        ):
            ta = threading.Thread(target=worker, args=(draft_a, "a"))
            tb = threading.Thread(target=worker, args=(draft_b, "b"))
            ta.start()
            tb.start()
            self.assertTrue(block_a.wait(timeout=30), "赢家未到达写入挂起点")
            # 赢家已通过全部校验并持有 配置行锁 + 职工行锁 + 冲突范围锁；
            # 输家此刻必须阻塞在这些锁上（若两线程都不再存活说明锁协议失效）
            time.sleep(1.5)
            self.assertTrue(ta.is_alive(), "线程 A 未被锁阻塞——锁协议失效")
            self.assertTrue(tb.is_alive(), "线程 B 未被锁阻塞——锁协议失效")
            release_a.set()
            ta.join(timeout=30)
            tb.join(timeout=30)

        self.assertEqual(thread_errors, [])
        successes = [v for v in outcomes.values() if v["confirmed"]]
        # 新规则：确认即“替换旧版本”，并发下允许多次成功，但最终必须只有一个有效版本
        self.assertGreaterEqual(len(successes), 1, f"确认结果：{outcomes}")
        active_batches = {
            record.schedule_batch_id
            for record in ScheduleRecord.objects.filter(
                status=ScheduleRecord.Status.CONFIRMED, is_active=True
            )
        }
        self.assertEqual(len(active_batches), 1, f"有效版本应唯一：{active_batches}")
        # 无跨批次重复：同一职工同一天仅一条正式记录
        total_days = (draft_a.period_end - draft_a.period_start).days + 1
        seen = set()
        for record in ScheduleRecord.objects.filter(
            team=team, status=ScheduleRecord.Status.CONFIRMED,
            work_date__range=(draft_a.period_start, draft_a.period_end),
        ):
            key = (record.employee_id, record.work_date)
            self.assertNotIn(key, seen, f"跨批次重复排班：{key}")
            seen.add(key)
        self.assertEqual(len(seen), 5 * total_days)

    def test_shifts_are_independent_when_confirming(self):
        # 班次隔离：早班确认后，晚班（独立人员）生成/确认不得影响早班记录；
        # 没有成员的班次不能生成草稿（人员范围为空 → 明确报告，而不是借用早班人员）。
        org = OrganizationUnit.objects.create(name="甲队", code="ORG-A")
        team = Team.objects.create(organization=org, name="甲一班", code="TA-01")
        early = team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        night = team.shift_configs.get(shift_name=ShiftConfig.ShiftName.NIGHT)
        admin = make_user("draft_race_super2")
        for config in (early, night):
            config.daily_worker_count = 2
            config.required_shifts = 2
            config.is_active = True
            config.save(update_fields=["daily_worker_count", "required_shifts", "is_active"])
        early_staff = [
            Employee.objects.create(
                employee_no=f"E1{i:02d}", name=f"早班{i}", team=team,
                shift_config=early, min_required_shifts=2,
            )
            for i in range(1, 4)
        ]
        # 晚班暂无成员 → 不能生成草稿
        empty_draft = create_schedule_draft(night, admin)
        self.assertIsNone(empty_draft["draft"])
        self.assertTrue(empty_draft.get("errors"), empty_draft)

        draft_early = create_schedule_draft(early, admin)["draft"]
        self.assertTrue(confirm_schedule_draft(draft_early.batch_id, admin)["confirmed"])
        early_before = list(
            ScheduleRecord.objects.filter(
                team=team, shift_config=early, status="confirmed", is_active=True
            ).values_list("id", "employee_id", "work_date")
        )
        self.assertTrue(early_before)

        # 给晚班补人后确认晚班草稿：早班记录保持原样（不被退休/替换）
        night_staff = [
            Employee.objects.create(
                employee_no=f"N1{i:02d}", name=f"晚班{i}", team=team,
                shift_config=night, min_required_shifts=2,
            )
            for i in range(1, 4)
        ]
        draft_night = create_schedule_draft(night, admin)["draft"]
        night_result = confirm_schedule_draft(draft_night.batch_id, admin)
        self.assertTrue(night_result["confirmed"], night_result.get("errors"))
        early_after = list(
            ScheduleRecord.objects.filter(
                team=team, shift_config=early, status="confirmed", is_active=True
            ).values_list("id", "employee_id", "work_date")
        )
        self.assertEqual(early_before, early_after)
        night_ids = set(
            ScheduleRecord.objects.filter(
                shift_config=night, status="confirmed", is_active=True
            ).values_list("employee_id", flat=True)
        )
        self.assertEqual(night_ids, {employee.pk for employee in night_staff})
        self.assertTrue(night_ids.isdisjoint({employee.pk for employee in early_staff}))

    def test_config_change_while_confirm_blocked_rejected(self):
        # 复核期间配置变化（真实锁竞争）：主线程持有配置行锁，确认线程阻塞；
        # 主线程修改配置后提交 → 确认线程的锁定读看到新值 → 拒绝
        _, team, early, admin = self._fixture()
        draft = create_schedule_draft(early, admin)["draft"]
        outcomes = {}
        barrier = threading.Barrier(2)

        def worker():
            close_old_connections()
            user = User.objects.get(pk=admin.pk)
            barrier.wait()
            outcomes["result"] = confirm_schedule_draft(draft.batch_id, user)

        with transaction.atomic():
            config = ShiftConfig.objects.select_for_update().get(pk=early.pk)
            t = threading.Thread(target=worker)
            t.start()
            barrier.wait()
            time.sleep(1.5)
            self.assertTrue(t.is_alive(), "确认线程未被配置行锁阻塞——锁协议失效")
            config.daily_worker_count = 4
            config.save(update_fields=["daily_worker_count"])
        t.join(timeout=30)
        outcome = outcomes["result"]
        self.assertFalse(outcome["confirmed"])
        self.assertTrue(any("已过期" in e for e in outcome["errors"]))
        self.assertEqual(
            ScheduleRecord.objects.filter(schedule_batch_id=draft.batch_id).count(), 0
        )

    def test_employee_deactivated_while_confirm_blocked_rejected(self):
        # 复核期间职工状态变化（真实锁竞争）：主线程持有职工行锁，确认线程阻塞；
        # 主线程停用一名职工后提交 → 确认线程锁定读看到新状态 → 拒绝
        _, team, early, admin = self._fixture()
        draft = create_schedule_draft(early, admin)["draft"]
        outcomes = {}
        barrier = threading.Barrier(2)

        def worker():
            close_old_connections()
            user = User.objects.get(pk=admin.pk)
            barrier.wait()
            outcomes["result"] = confirm_schedule_draft(draft.batch_id, user)

        with transaction.atomic():
            locked = list(
                Employee.objects.select_for_update()
                .filter(team=team, is_active=True)
                .order_by("pk")
            )
            t = threading.Thread(target=worker)
            t.start()
            barrier.wait()
            time.sleep(1.5)
            self.assertTrue(t.is_alive(), "确认线程未被职工行锁阻塞——锁协议失效")
            locked[0].is_active = False
            locked[0].save(update_fields=["is_active"])
        t.join(timeout=30)
        outcome = outcomes["result"]
        self.assertFalse(outcome["confirmed"])
        self.assertTrue(any("已变化" in e for e in outcome["errors"]))
        self.assertEqual(
            ScheduleRecord.objects.filter(schedule_batch_id=draft.batch_id).count(), 0
        )
