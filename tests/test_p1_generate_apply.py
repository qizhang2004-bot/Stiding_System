# P1：生成即保存（求解在事务外、写入单事务、同队组同周期替换）验收测试。
import datetime
import threading
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.auditlog.models import OperationLog
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler import drafting
from apps.scheduler.drafting import generate_and_apply
from apps.scheduler.models import ScheduleBatch, ScheduleRecord
from apps.scheduler.selectors import active_schedule_records, build_shift_board
from apps.scheduler.services import create_schedule_record, new_schedule_batch_id

User = get_user_model()
DAY = datetime.date(2026, 9, 26)


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class P1FixtureMixin:
    @classmethod
    def _build(cls, employees=5):
        cls.super_user = make_user("p1_super")
        cls.org_a = OrganizationUnit.objects.create(name="甲队", code="P1A")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="P1B")
        cls.admin_a = make_user("p1_admin_a", role="admin", organization=cls.org_a)
        cls.team1 = Team.objects.create(organization=cls.org_a, name="检修班", code="P1T1")
        cls.team2 = Team.objects.create(organization=cls.org_a, name="生产一班", code="P1T2")
        cls.team_b = Team.objects.create(organization=cls.org_b, name="乙班", code="P1TB")
        cls.config1 = cls.team1.shift_configs.get(shift_name="early")
        cls.config1.daily_worker_count = 3
        cls.config1.required_shifts = 2
        cls.config1.save(update_fields=["daily_worker_count", "required_shifts"])
        cls.config2 = cls.team2.shift_configs.get(shift_name="early")
        cls.config2.daily_worker_count = 1
        cls.config2.required_shifts = 1
        cls.config2.save(update_fields=["daily_worker_count", "required_shifts"])
        cls.config_b = cls.team_b.shift_configs.get(shift_name="early")
        cls.config_b.save()
        cls.employees = [
            Employee.objects.create(
                employee_no=f"P1{i:03d}", name=f"生成{i}", team=cls.team1,
                shift_config=cls.config1, position="电工" if i == 1 else "",
                min_required_shifts=2,
            )
            for i in range(1, employees + 1)
        ]
        cls.emp_other_team = Employee.objects.create(
            employee_no="P1901", name="他班职工", team=cls.team2, shift_config=cls.config2,
            min_required_shifts=1,
        )
        cls.emp_other_org = Employee.objects.create(
            employee_no="P1902", name="外队职工", team=cls.team_b, shift_config=cls.config_b,
            min_required_shifts=1,
        )
        cls.period = (datetime.date(2026, 9, 25), datetime.date(2026, 10, 24))

    # 造一份“旧的有效排班”（period 版本 + 人工 adjustment 记录）
    def _seed_old_version(self):
        ScheduleBatch.objects.create(
            organization=self.org_a, team=self.team1, shift_config=self.config1,
            period_start=self.period[0], period_end=self.period[1], mode="tight",
            daily_worker_count=self.config1.daily_worker_count,
            position_requirements={}, employee_targets={}, employee_qualifications={},
            payload=[], summary={}, kind=ScheduleBatch.Kind.PERIOD,
            status=ScheduleBatch.Status.CONFIRMED,
            created_by=self.admin_a, confirmed_by=self.admin_a,
        )
        old_batch_id = ScheduleBatch.objects.latest("id").batch_id
        old_records = []
        for employee in self.employees:
            old_records.append(
                create_schedule_record(
                    schedule_batch_id=old_batch_id,
                    organization=self.org_a, team=self.team1, shift_config=self.config1,
                    employee=employee, work_date=DAY,
                    shift_type=ScheduleRecord.ShiftType.EARLY,
                    status=ScheduleRecord.Status.CONFIRMED,
                )
            )
        # 人工添加产生的一条 adjustment 有效记录（同周期、同一人另一天）
        manual = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_a, team=self.team1, shift_config=self.config1,
            employee=self.employees[0], work_date=DAY + datetime.timedelta(days=1),
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        return old_batch_id, old_records, manual

    def _state_snapshot(self):
        return sorted(
            ScheduleRecord.objects.values_list(
                "id", "employee_id", "work_date", "shift_type", "status", "is_active"
            )
        )


class GenerateApplyTests(P1FixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def setUp(self):
        self.client.force_login(self.admin_a)

    # 1) 生成成功直接生效，不产生空 draft
    def test_generate_applies_directly_without_draft(self):
        outcome = generate_and_apply(self.config1, self.admin_a, anchor=self.period[0],
                                     time_limit_seconds=60)
        self.assertTrue(outcome["applied"], outcome["errors"])
        self.assertEqual(ScheduleBatch.objects.filter(status=ScheduleBatch.Status.DRAFT).count(), 0)
        batch = outcome["batch"]
        self.assertEqual(batch.status, ScheduleBatch.Status.CONFIRMED)
        days = (self.period[1] - self.period[0]).days + 1
        self.assertEqual(
            ScheduleRecord.objects.filter(
                schedule_batch_id=batch.batch_id, status="confirmed", is_active=True
            ).count(),
            5 * days,
        )
        # 看板 / 个人日历立即生效
        board = build_shift_board(*self.period, org=self.org_a)
        early = next(s for s in board["days"][0]["shifts"] if s["key"] == "early")
        self.assertTrue(early["total"] > 0)
        cal = self.client.get(
            reverse("personnel:employee_calendar", args=[self.employees[0].pk]),
            {"period": "2026-09"},
        )
        self.assertTrue(any(day["record"] for day in cal.context["days"]))
        # 审计区分“生成”与“替换”
        self.assertTrue(OperationLog.objects.filter(action="generate").exists())

    # 2) 求解失败 → 旧排班完全不变
    def test_solve_failure_keeps_old_data(self):
        self._seed_old_version()
        before = self._state_snapshot()
        self.config1.daily_worker_count = 99  # 需求人数远超在职人数 → 不可行
        self.config1.save(update_fields=["daily_worker_count"])
        outcome = generate_and_apply(self.config1, self.admin_a, anchor=self.period[0],
                                     time_limit_seconds=30)
        self.assertFalse(outcome["applied"])
        self.assertEqual(self._state_snapshot(), before)
        self.assertTrue(OperationLog.objects.filter(action="generate_failed").exists())

    # 3) 明细校验失败 → 旧排班完全不变
    def test_payload_validation_failure_keeps_old_data(self):
        self._seed_old_version()
        before = self._state_snapshot()
        with mock.patch.object(
            drafting, "validate_batch_payload", return_value=["模拟明细校验失败"]
        ):
            outcome = generate_and_apply(self.config1, self.admin_a, anchor=self.period[0],
                                         time_limit_seconds=60)
        self.assertFalse(outcome["applied"])
        self.assertTrue(any("模拟明细校验失败" in e for e in outcome["errors"]))
        self.assertEqual(self._state_snapshot(), before)

    # 4) 写入中途异常 → 整体回滚，旧排班完整
    def test_write_failure_rolls_back(self):
        _, old_records, manual = self._seed_old_version()
        before = self._state_snapshot()
        real = drafting.create_schedule_record
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("模拟写入中途失败")
            return real(*args, **kwargs)

        with mock.patch.object(drafting, "create_schedule_record", side_effect=flaky):
            outcome = generate_and_apply(self.config1, self.admin_a, anchor=self.period[0],
                                         time_limit_seconds=60)
        self.assertFalse(outcome["applied"])
        self.assertEqual(self._state_snapshot(), before)
        for record in old_records + [manual]:
            record.refresh_from_db()
            self.assertEqual(record.status, ScheduleRecord.Status.CONFIRMED)
            self.assertTrue(record.is_active)

    # 5) 同周期的 period 与 adjustment 有效记录都被替换
    def test_replaces_period_and_adjustment_records(self):
        old_batch_id, old_records, manual = self._seed_old_version()
        outcome = generate_and_apply(self.config1, self.admin_a, anchor=self.period[0],
                                     time_limit_seconds=60)
        self.assertTrue(outcome["applied"], outcome["errors"])
        self.assertGreater(outcome["retired"], 0)
        for record in old_records + [manual]:
            record.refresh_from_db()
            self.assertEqual(record.status, ScheduleRecord.Status.SUPERSEDED)
            self.assertTrue(record.is_active)  # 仅状态变更，记录保留
        active = active_schedule_records(org=self.org_a, start=self.period[0], end=self.period[1])
        self.assertEqual({r.schedule_batch_id for r in active}, {outcome["batch"].batch_id})

    # 6) 其他队组、其他周期不变
    def test_other_team_and_period_untouched(self):
        other = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_a, team=self.team2, shift_config=self.config2,
            employee=self.emp_other_team, work_date=DAY,
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        other_org = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_b, team=self.team_b, shift_config=self.config_b,
            employee=self.emp_other_org, work_date=DAY,
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        other_period = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_a, team=self.team1, shift_config=self.config1,
            employee=self.employees[0], work_date=datetime.date(2026, 8, 26),
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        outcome = generate_and_apply(self.config1, self.admin_a, anchor=self.period[0],
                                     time_limit_seconds=60)
        self.assertTrue(outcome["applied"], outcome["errors"])
        # 其他队组：完全不参与本次生成与归档
        other_org.refresh_from_db()
        self.assertEqual(other_org.status, ScheduleRecord.Status.CONFIRMED)
        self.assertTrue(other_org.is_active)
        # 其他班组（同队组）：不受整周期替换影响
        other.refresh_from_db()
        self.assertEqual(other.status, ScheduleRecord.Status.CONFIRMED)
        self.assertTrue(other.is_active)
        # 其他周期（同班组、已结束）：P3 起会被“自动归档”覆盖为 archived，
        # 但内容不得被替换或删除（区别于 superseded）
        other_period.refresh_from_db()
        self.assertIn(
            other_period.status,
            (ScheduleRecord.Status.CONFIRMED, ScheduleRecord.Status.ARCHIVED),
        )
        self.assertNotEqual(other_period.status, ScheduleRecord.Status.SUPERSEDED)
        self.assertEqual(other_period.shift_type, ScheduleRecord.ShiftType.EARLY)
        self.assertTrue(other_period.is_active)

    # 7) superseded 历史记录不被修改
    def test_superseded_history_untouched(self):
        # 周期内先有“当前有效版本”（可被替换）+ 一条更早的 superseded 历史记录
        self._seed_old_version()
        historic = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_a, team=self.team1, shift_config=self.config1,
            employee=self.employees[1], work_date=DAY + datetime.timedelta(days=3),
            shift_type=ScheduleRecord.ShiftType.NIGHT,
            status=ScheduleRecord.Status.SUPERSEDED,
        )
        outcome = generate_and_apply(self.config1, self.admin_a, anchor=self.period[0],
                                     time_limit_seconds=60)
        self.assertTrue(outcome["applied"], outcome["errors"])
        historic.refresh_from_db()
        self.assertEqual(historic.status, ScheduleRecord.Status.SUPERSEDED)
        self.assertEqual(historic.shift_type, ScheduleRecord.ShiftType.NIGHT)
        self.assertTrue(historic.is_active)

    # 11) 快照变化 → 中止写入，旧排班不变
    def test_snapshot_change_aborts_application(self):
        self._seed_old_version()
        before = self._state_snapshot()
        real_solve = drafting.solve_shift_schedule

        def solve_then_change_config(*args, **kwargs):
            result = real_solve(*args, **kwargs)
            ShiftConfig.objects.filter(pk=self.config1.pk).update(required_shifts=17)
            return result

        with mock.patch.object(drafting, "solve_shift_schedule", side_effect=solve_then_change_config):
            outcome = generate_and_apply(self.config1, self.admin_a, anchor=self.period[0],
                                         time_limit_seconds=60)
        self.assertFalse(outcome["applied"])
        self.assertTrue(any("变化" in e for e in outcome["errors"]), outcome["errors"])
        self.assertEqual(self._state_snapshot(), before)

    # 12) 历史（已被替换）周期拒绝生成；缺口 A：有版本头但记录被删光的当前周期允许添加
    def test_historical_period_generate_refused(self):
        create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_a, team=self.team1, shift_config=self.config1,
            employee=self.employees[0], work_date=DAY,
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.SUPERSEDED,
        )
        before = self._state_snapshot()
        outcome = generate_and_apply(self.config1, self.admin_a, anchor=self.period[0],
                                     time_limit_seconds=60)
        self.assertFalse(outcome["applied"])
        self.assertTrue(any("拒绝覆盖" in e for e in outcome["errors"]), outcome["errors"])
        self.assertEqual(self._state_snapshot(), before)

    def test_add_allowed_when_version_head_exists_but_rows_deleted(self):
        # 缺口 A 修复验证：存在已确认版本头、但该版本记录被全部软删除 → 仍属当前周期，允许添加
        _, old_records, manual = self._seed_old_version()
        for record in old_records + [manual]:
            record.is_active = False
            record.save(update_fields=["is_active"])
        outcome = drafting.add_manual_schedule(
            self.config1, DAY + datetime.timedelta(days=2), self.employees[1], self.admin_a
        )
        self.assertEqual(outcome["errors"], [])
        self.assertTrue(
            active_schedule_records(
                org=self.org_a, work_date=DAY + datetime.timedelta(days=2)
            ).filter(employee=self.employees[1]).exists()
        )

    # 10) 看板 / 日历 / 日详情一致
    def test_board_calendar_detail_consistent(self):
        outcome = generate_and_apply(self.config1, self.admin_a, anchor=self.period[0],
                                     time_limit_seconds=60)
        self.assertTrue(outcome["applied"], outcome["errors"])
        board = build_shift_board(*self.period, org=self.org_a)
        day = next(d for d in board["days"] if d["date"] == DAY)
        board_names = sorted(p["name"] for s in day["shifts"] for t in s["teams"] for p in t["people"])
        calendar_names = []
        for employee in self.employees:
            cal = self.client.get(
                reverse("personnel:employee_calendar", args=[employee.pk]), {"period": "2026-09"}
            )
            cell = next(d for d in cal.context["days"] if d["date"] == DAY)
            if cell["record"] and cell["shift_type"] != ScheduleRecord.ShiftType.REST:
                calendar_names.append(employee.name)
        self.assertEqual(board_names, sorted(calendar_names))
        detail = self.client.get(
            reverse("scheduler:shift_board_detail", args=[DAY.isoformat(), "early"])
        )
        self.assertEqual(sorted(r["name"] for r in detail.context["rows"]), board_names)


class GenerateConcurrencyTests(P1FixtureMixin, TransactionTestCase):
    # 真实 MySQL 锁下的并发验证（不使用应用层串行模拟）
    def setUp(self):
        self._build()
        self.period = (datetime.date(2026, 9, 25), datetime.date(2026, 10, 24))

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

    def test_concurrent_generate_single_active_version(self):
        errors = self._run_threads([
            lambda: generate_and_apply(
                ShiftConfig.objects.get(pk=self.config1.pk), self.super_user,
                anchor=self.period[0], time_limit_seconds=60,
            ),
            lambda: generate_and_apply(
                ShiftConfig.objects.get(pk=self.config1.pk), self.super_user,
                anchor=self.period[0], time_limit_seconds=60,
            ),
        ])
        self.assertEqual(errors, [])
        active = ScheduleRecord.objects.filter(
            team=self.team1, status="confirmed", is_active=True
        )
        pairs = list(active.values_list("employee_id", "work_date"))
        self.assertEqual(len(pairs), len(set(pairs)), "同一职工同一天出现多条有效记录")
        self.assertEqual(
            len({r.schedule_batch_id for r in active}), 1, "出现多个有效版本"
        )

    def test_concurrent_add_and_generate_keeps_invariants(self):
        self._seed_old_version()
        target_day = DAY + datetime.timedelta(days=4)
        errors = self._run_threads([
            lambda: drafting.add_manual_schedule(
                ShiftConfig.objects.get(pk=self.config1.pk), target_day,
                Employee.objects.get(pk=self.employees[2].pk), self.super_user,
            ),
            lambda: generate_and_apply(
                ShiftConfig.objects.get(pk=self.config1.pk), self.super_user,
                anchor=self.period[0], time_limit_seconds=60,
            ),
        ])
        self.assertEqual(errors, [])
        active = ScheduleRecord.objects.filter(team=self.team1, status="confirmed", is_active=True)
        pairs = list(active.values_list("employee_id", "work_date"))
        self.assertEqual(len(pairs), len(set(pairs)), "并发后出现同一职工同一天多条有效记录")
        # 若添加写入成功，它必须与整周期版本同属一个有效版本（或添加被拒绝）
        versions = {r.schedule_batch_id for r in active}
        self.assertEqual(len(versions), 1, f"出现多个有效版本：{versions}")
