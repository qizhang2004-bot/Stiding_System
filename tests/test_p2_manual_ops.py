# P2：人工操作直接修改每日排班记录（不创建 adjustment 批次，一人一天一条有效记录）验收测试。
import datetime
import threading

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.auditlog.models import OperationLog
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler import drafting, selectors
from apps.scheduler.drafting import (
    add_manual_schedule,
    change_schedule_record,
    generate_and_apply,
    remove_schedule_record,
)
from apps.scheduler.models import ScheduleBatch, ScheduleRecord
from apps.scheduler.services import create_schedule_record, new_schedule_batch_id

User = get_user_model()
DAY = datetime.date(2026, 9, 26)
PERIOD = (datetime.date(2026, 9, 25), datetime.date(2026, 10, 24))


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class P2FixtureMixin:
    @classmethod
    def _build(cls, employees=3):
        cls.super_user = make_user("p2_super")
        cls.org_a = OrganizationUnit.objects.create(name="甲队", code="P2A")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="P2B")
        cls.admin_a = make_user("p2_admin_a", role="admin", organization=cls.org_a)
        cls.member_a = make_user("p2_member_a", role="member", organization=cls.org_a)
        cls.team = Team.objects.create(organization=cls.org_a, name="检修班", code="P2T1")
        cls.team_b = Team.objects.create(organization=cls.org_b, name="乙班", code="P2TB")
        cls.config = cls.team.shift_configs.get(shift_name="early")
        cls.config.daily_worker_count = 3
        cls.config.required_shifts = 2
        cls.config.is_active = True
        cls.config.save(update_fields=["daily_worker_count", "required_shifts", "is_active"])
        for name in ("middle", "night"):
            cfg = cls.team.shift_configs.get(shift_name=name)
            cfg.is_active = True
            cfg.save(update_fields=["is_active"])
        cls.config_b = cls.team_b.shift_configs.get(shift_name="early")
        cls.employees = [
            Employee.objects.create(
                employee_no=f"P2{i:03d}", name=f"人工{i}", team=cls.team,
                shift_config=cls.config, position="电工" if i == 1 else "",
                min_required_shifts=2,
            )
            for i in range(1, employees + 1)
        ]
        cls.emp_other_org = Employee.objects.create(
            employee_no="P2901", name="外队职工", team=cls.team_b, shift_config=cls.config_b,
            min_required_shifts=1,
        )
        # 当前版本（P1 生成即保存），使周期处于 current 状态且版本批次可追溯
        outcome = generate_and_apply(cls.config, cls.super_user, anchor=PERIOD[0],
                                     time_limit_seconds=60)
        assert outcome["applied"], outcome["errors"]
        cls.version_batch_id = outcome["batch"].batch_id

    def active_of(self, employee, day=DAY):
        return list(
            ScheduleRecord.objects.filter(
                team=self.team, employee=employee, work_date=day,
                status__in=[ScheduleRecord.Status.CONFIRMED, ScheduleRecord.Status.PUBLISHED],
                is_active=True,
            )
        )


class DailyScheduleUniquenessTests(P2FixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def test_add_rejected_when_rest_record_exists(self):
        # 目标：同日已有“休息”有效记录时，添加必须被拒绝且不产生第二条有效记录
        record = self.active_of(self.employees[1])[0]
        change_schedule_record(record, ScheduleRecord.ShiftType.REST, self.admin_a)
        self.assertEqual(len(self.active_of(self.employees[1])), 1)
        result = add_manual_schedule(self.config, DAY, self.employees[1], self.admin_a)
        self.assertTrue(result["errors"])
        self.assertIn("已有排班", result["errors"][0])
        active = self.active_of(self.employees[1])
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].shift_type, ScheduleRecord.ShiftType.REST)

    def test_add_creates_single_active_record(self):
        # 先删除当天记录 → 未安排；再添加 → 恰好一条上班记录
        record = self.active_of(self.employees[1])[0]
        remove_schedule_record(record, self.admin_a)
        self.assertEqual(self.active_of(self.employees[1]), [])
        result = add_manual_schedule(self.config, DAY, self.employees[1], self.admin_a)
        self.assertEqual(result["errors"], [])
        active = self.active_of(self.employees[1])
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].shift_type, ScheduleRecord.ShiftType.EARLY)

    def test_duplicate_add_rejected(self):
        result = add_manual_schedule(self.config, DAY, self.employees[0], self.admin_a)
        self.assertTrue(result["errors"])  # 已有有效记录
        self.assertEqual(len(self.active_of(self.employees[0])), 1)

    def test_change_keeps_single_active_record(self):
        record = self.active_of(self.employees[0])[0]
        before_count = ScheduleRecord.objects.filter(team=self.team).count()
        result = change_schedule_record(record, ScheduleRecord.ShiftType.MIDDLE, self.admin_a)
        self.assertTrue(result["changed"], result)
        active = self.active_of(self.employees[0])
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].shift_type, ScheduleRecord.ShiftType.MIDDLE)
        record.refresh_from_db()
        self.assertTrue(record.is_active)                       # 原地更新：记录仍有效
        self.assertEqual(record.shift_type, ScheduleRecord.ShiftType.MIDDLE)
        self.assertEqual(
            ScheduleRecord.objects.filter(team=self.team).count(), before_count
        )  # 不新增记录

    def test_change_to_rest_keeps_one_rest_record(self):
        record = self.active_of(self.employees[0])[0]
        self.assertTrue(change_schedule_record(record, ScheduleRecord.ShiftType.REST, self.admin_a)["changed"])
        active = self.active_of(self.employees[0])
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].shift_type, ScheduleRecord.ShiftType.REST)
        # 个人日历显示“休息”而非“未排班”
        self.client.force_login(self.admin_a)
        cal = self.client.get(
            reverse("personnel:employee_calendar", args=[self.employees[0].pk]), {"period": "2026-09"}
        )
        cell = next(d for d in cal.context["days"] if d["date"] == DAY)
        self.assertIsNotNone(cell["record"])
        self.assertEqual(cell["shift_type"], ScheduleRecord.ShiftType.REST)

    def test_remove_creates_no_rest_record(self):
        record = self.active_of(self.employees[0])[0]
        self.assertTrue(remove_schedule_record(record, self.admin_a)["removed"])
        self.assertEqual(self.active_of(self.employees[0]), [])  # 未安排，而非休息
        self.client.force_login(self.admin_a)
        cal = self.client.get(
            reverse("personnel:employee_calendar", args=[self.employees[0].pk]), {"period": "2026-09"}
        )
        cell = next(d for d in cal.context["days"] if d["date"] == DAY)
        self.assertIsNone(cell["record"])  # 日历显示“未排班”

    def test_legacy_rest_plus_work_conflict_is_normalized_by_change(self):
        # 重现真实库已知冲突形态：同日同时存在“休息 + 上班”两条有效记录（历史遗留）
        employee = self.employees[2]
        existing = self.active_of(employee)[0]
        existing.shift_type = ScheduleRecord.ShiftType.REST
        existing.save(update_fields=["shift_type"])
        create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_a, team=self.team, shift_config=self.config,
            employee=employee, work_date=DAY,
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        self.assertEqual(len(self.active_of(employee)), 2)  # 冲突形态（临时数据，非真实库）
        # 新逻辑：任一次改班/添加都会收敛为唯一有效记录
        result = add_manual_schedule(self.config, DAY, employee, self.admin_a)
        self.assertTrue(result["errors"])  # 添加被拒绝（已有有效记录）
        current = self.active_of(employee)[0]
        self.assertTrue(change_schedule_record(current, ScheduleRecord.ShiftType.NIGHT, self.admin_a)["changed"])
        active = self.active_of(employee)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].shift_type, ScheduleRecord.ShiftType.NIGHT)

    def test_no_adjustment_batch_created_by_manual_ops(self):
        before = ScheduleBatch.objects.count()
        record = self.active_of(self.employees[1])[0]
        remove_schedule_record(record, self.admin_a)
        add_manual_schedule(self.config, DAY, self.employees[1], self.admin_a)
        current = self.active_of(self.employees[1])[0]
        change_schedule_record(current, ScheduleRecord.ShiftType.MIDDLE, self.admin_a)
        self.assertEqual(ScheduleBatch.objects.count(), before)  # 人工操作不再建批次
        self.assertFalse(ScheduleBatch.objects.filter(kind=ScheduleBatch.Kind.ADJUSTMENT).exists())
        # 版本批次关联保留可追溯
        final = self.active_of(self.employees[1])[0]
        self.assertEqual(final.schedule_batch_id, self.version_batch_id)

    def test_audit_contains_operator_and_before_after(self):
        record = self.active_of(self.employees[0])[0]
        change_schedule_record(record, ScheduleRecord.ShiftType.NIGHT, self.admin_a)
        log = OperationLog.objects.filter(action="change").latest("created_at")
        self.assertEqual(log.user, self.admin_a)
        self.assertIn("早班 → 晚班", log.description)
        self.assertIn(str(record.schedule_batch_id)[:8], log.description)  # 原批次号可追溯
        self.assertIn(self.team.name, log.description)
        self.assertIn(str(DAY), log.description)

    def test_change_and_remove_still_block_history_and_cross_org(self):
        self.assertEqual(len(self.active_of(self.employees[0])), 1)
        # 历史保护：superseded 记录不可改
        historic = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_a, team=self.team, shift_config=self.config,
            employee=self.employees[0], work_date=DAY + datetime.timedelta(days=10),
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.SUPERSEDED,
        )
        self.assertFalse(change_schedule_record(historic, ScheduleRecord.ShiftType.MIDDLE, self.admin_a)["changed"])
        self.assertFalse(remove_schedule_record(historic, self.admin_a)["removed"])
        # 跨队组：HTTP 层 404
        admin_b = make_user("p2_admin_b", role="admin", organization=self.org_b)
        self.client.force_login(admin_b)
        self.assertEqual(
            self.client.post(
                reverse("scheduler:shift_board_change", args=[self.active_of(self.employees[0])[0].pk]),
                {"target_shift": "middle"},
            ).status_code,
            404,
        )
        # 成员：403
        self.client.force_login(self.member_a)
        self.assertEqual(
            self.client.post(
                reverse("scheduler:shift_board_change", args=[self.active_of(self.employees[0])[0].pk]),
                {"target_shift": "middle"},
            ).status_code,
            403,
        )

    def test_board_calendar_detail_consistent_after_manual_ops(self):
        record = self.active_of(self.employees[1])[0]
        change_schedule_record(record, ScheduleRecord.ShiftType.NIGHT, self.admin_a)
        board = selectors.build_shift_board(*PERIOD, org=self.org_a)
        day = next(d for d in board["days"] if d["date"] == DAY)
        night_names = [
            p["name"] for s in day["shifts"] if s["key"] == "night" for t in s["teams"] for p in t["people"]
        ]
        early_names = [
            p["name"] for s in day["shifts"] if s["key"] == "early" for t in s["teams"] for p in t["people"]
        ]
        self.assertIn(self.employees[1].name, night_names)
        self.assertNotIn(self.employees[1].name, early_names)

        self.client.force_login(self.admin_a)
        cal = self.client.get(
            reverse("personnel:employee_calendar", args=[self.employees[1].pk]), {"period": "2026-09"}
        )
        cell = next(d for d in cal.context["days"] if d["date"] == DAY)
        self.assertEqual(cell["shift_type"], "night")
        detail = self.client.get(
            reverse("scheduler:shift_board_detail", args=[DAY.isoformat(), "night"])
        )
        self.assertIn(
            self.employees[1].name, [r["name"] for r in detail.context["rows"]]
        )

    def test_http_add_rejected_for_rest_day_with_guidance(self):
        record = self.active_of(self.employees[1])[0]
        change_schedule_record(record, ScheduleRecord.ShiftType.REST, self.admin_a)
        self.client.force_login(self.admin_a)
        response = self.client.post(
            reverse("scheduler:shift_board_add", args=[DAY.isoformat(), "early"]),
            {"employee_id": self.employees[1].pk},
        )
        self.assertIn(response.status_code, (200, 302))  # 错误提示后同页渲染或回跳
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("改班" in m for m in messages), messages)
        self.assertEqual(len(self.active_of(self.employees[1])), 1)


class ManualOpsConcurrencyTests(P2FixtureMixin, TransactionTestCase):
    def setUp(self):
        self._build()

    def _run_threads(self, targets):
        errors = []

        def runner(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=runner, args=(fn,)) for fn in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        return errors

    def test_concurrent_changes_leave_single_active_record(self):
        record = self.active_of(self.employees[0])[0]
        record_id = record.pk
        errors = self._run_threads([
            lambda: change_schedule_record(
                ScheduleRecord.objects.get(pk=record_id), ScheduleRecord.ShiftType.MIDDLE,
                self.super_user,
            ),
            lambda: change_schedule_record(
                ScheduleRecord.objects.get(pk=record_id), ScheduleRecord.ShiftType.NIGHT,
                self.super_user,
            ),
        ])
        self.assertEqual(errors, [])
        active = self.active_of(self.employees[0])
        self.assertEqual(len(active), 1, "并发改班后出现多条当前有效记录")

    def test_concurrent_add_same_person_day(self):
        record = self.active_of(self.employees[1])[0]
        remove_schedule_record(record, self.super_user)
        errors = self._run_threads([
            lambda: add_manual_schedule(
                ShiftConfig.objects.get(pk=self.config.pk), DAY,
                Employee.objects.get(pk=self.employees[1].pk), self.super_user,
            ),
            lambda: add_manual_schedule(
                ShiftConfig.objects.get(pk=self.config.pk), DAY,
                Employee.objects.get(pk=self.employees[1].pk), self.super_user,
            ),
        ])
        self.assertEqual(errors, [])
        active = self.active_of(self.employees[1])
        self.assertEqual(len(active), 1, "并发添加后出现多条当前有效记录")

    def test_concurrent_manual_ops_and_generate_keep_invariants(self):
        errors = self._run_threads([
            lambda: change_schedule_record(
                ScheduleRecord.objects.get(pk=self.active_of(self.employees[0])[0].pk),
                ScheduleRecord.ShiftType.REST, self.super_user,
            ),
            lambda: generate_and_apply(
                ShiftConfig.objects.get(pk=self.config.pk), self.super_user,
                anchor=PERIOD[0], time_limit_seconds=60,
            ),
        ])
        self.assertEqual(errors, [])
        active = ScheduleRecord.objects.filter(
            team=self.team, status="confirmed", is_active=True
        )
        pairs = list(active.values_list("employee_id", "work_date"))
        self.assertEqual(len(pairs), len(set(pairs)), "生成与人工操作并发后出现重复有效记录")
