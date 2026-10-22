# 排班记录模型测试。
import datetime
import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase

from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee

from .models import ScheduleRecord
from .selectors import period_range


class PeriodRangeTests(SimpleTestCase):
    # 排班周期：每月 25 日至次月 24 日（跨月、跨年、不同月份天数）
    def test_period_within_same_year(self):
        start, end = period_range(datetime.date(2026, 10, 25))
        self.assertEqual((start, end), (datetime.date(2026, 10, 25), datetime.date(2026, 11, 24)))
        self.assertEqual((end - start).days + 1, 31)

    def test_period_cross_year(self):
        start, end = period_range(datetime.date(2026, 12, 25))
        self.assertEqual((start, end), (datetime.date(2026, 12, 25), datetime.date(2027, 1, 24)))
        self.assertEqual((end - start).days + 1, 31)

    def test_anchor_in_second_half_of_month(self):
        start, end = period_range(datetime.date(2026, 10, 12))
        self.assertEqual((start, end), (datetime.date(2026, 9, 25), datetime.date(2026, 10, 24)))

    def test_anchor_cross_year(self):
        start, end = period_range(datetime.date(2027, 1, 5))
        self.assertEqual((start, end), (datetime.date(2026, 12, 25), datetime.date(2027, 1, 24)))

    def test_february_leap_year(self):
        start, end = period_range(datetime.date(2024, 1, 25))
        self.assertEqual((start, end), (datetime.date(2024, 1, 25), datetime.date(2024, 2, 24)))
        self.assertEqual((end - start).days + 1, 31)  # 1月25-31(7天) + 2月1-24(24天)

    def test_february_non_leap_year(self):
        start, end = period_range(datetime.date(2023, 1, 25))
        self.assertEqual((start, end), (datetime.date(2023, 1, 25), datetime.date(2023, 2, 24)))


class BuildShiftAllocationTests(TestCase):
    # 适配层：从 Django 数据组装人多人少判断与班数分配目标
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.early.daily_worker_count = 18
        cls.early.required_shifts = 18
        cls.early.save(update_fields=["daily_worker_count", "required_shifts"])
        cls.employees = [
            Employee.objects.create(
                employee_no=f"E00{i}", name=f"职工{i}", team=cls.team,
                shift_config=cls.early, min_required_shifts=18,
            )
            for i in range(1, 6)
        ]
        cls.inactive = Employee.objects.create(
            employee_no="E099", name="停用职工", team=cls.team,
            shift_config=cls.early, min_required_shifts=18, is_active=False,
        )
        cls.early.exempt_employee_ids = [cls.employees[0].pk]
        cls.early.save(update_fields=["exempt_employee_ids"])

    def test_allocation_built_from_config(self):
        from .planning import build_shift_allocation

        result = build_shift_allocation(self.early)
        allocation = result["allocation"]
        self.assertEqual(
            allocation.total_days, (result["end"] - result["start"]).days + 1
        )
        self.assertEqual(allocation.daily_required, 18)   # R 来自 daily_worker_count
        self.assertEqual(allocation.base_shifts, 18)      # M 来自 required_shifts
        self.assertEqual(allocation.employee_count, 5)    # N 仅统计在职职工（不含停用）
        # 每日人数 18 > 有效职工 5：理论不可行，需如实报告（此时不生成分配目标）
        self.assertFalse(allocation.feasible)
        self.assertEqual(allocation.per_person_targets, {})
        self.assertTrue(any("无法满足每日人数" in note for note in allocation.notes))

    def test_allocation_respects_exempt_split(self):
        from .planning import build_shift_allocation

        self.early.daily_worker_count = 3
        self.early.required_shifts = 18
        self.early.save(update_fields=["daily_worker_count", "required_shifts"])
        # 名额紧张：豁免 2 人（K=floor(2D/18)=3），否则分配不可行
        self.early.exempt_employee_ids = [self.employees[0].pk, self.employees[1].pk]
        self.early.save(update_fields=["exempt_employee_ids"])
        result = build_shift_allocation(self.early)
        allocation = result["allocation"]
        self.assertTrue(allocation.feasible)
        self.assertEqual(allocation.total_slots, allocation.total_days * 3)
        # 总额守恒
        self.assertEqual(sum(allocation.per_person_targets.values()), allocation.total_slots)


class DailySolverAdapterTests(TestCase):
    # 第二阶段适配层：从配置组装每日人数求解并验证硬约束
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        # 每日 3 人：满足“普通人员最低班数 + 豁免受连续休息上限约束”的容量要求
        cls.early.daily_worker_count = 3
        cls.early.required_shifts = 18
        cls.early.save(update_fields=["daily_worker_count", "required_shifts"])
        cls.employees = [
            Employee.objects.create(
                employee_no=f"E10{i}", name=f"职工{i}", team=cls.team,
                shift_config=cls.early, min_required_shifts=18,
            )
            for i in range(1, 6)
        ]
        # 名额紧张场景：豁免 2 人，普通 3 人（K=floor(2D/18)=3）方可达成最低班数
        cls.early.exempt_employee_ids = [cls.employees[0].pk, cls.employees[1].pk]
        cls.early.save(update_fields=["exempt_employee_ids"])

    def test_solve_shift_daily_feasible(self):
        from .planning import solve_shift_daily

        result = solve_shift_daily(self.early)
        solution = result["solution"]
        self.assertIsNotNone(solution)
        self.assertEqual(solution.status, "optimal")
        days = result["total_days"]
        self.assertEqual(solution.daily_counts, [3] * days)  # 每天恰好 3 人
        self.assertEqual(len(solution.assignments), days * 3)
        self.assertEqual(solution.total_deviation, 0)
        # 不跨班组：所有被安排职工都属于本班组
        self.assertTrue(
            {emp_id for emp_id, _ in solution.assignments} <= {e.pk for e in self.employees}
        )

    def test_solve_shift_daily_infeasible_reports_reason(self):
        from .planning import solve_shift_daily

        self.early.daily_worker_count = 18
        self.early.save(update_fields=["daily_worker_count"])
        result = solve_shift_daily(self.early)
        self.assertIsNone(result["solution"])
        self.assertTrue(any("无法满足每日人数" in r for r in result["infeasible_reasons"]))
        # 不修改每日要求人数：配置值保持 18
        self.early.refresh_from_db()
        self.assertEqual(self.early.daily_worker_count, 18)


class PositionSolverAdapterTests(TestCase):
    # 第三阶段适配层：岗位人数与每日人数联合求解
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        # 每日 3 人：满足“普通人员最低班数 + 豁免受连续休息上限约束”的容量要求
        cls.early.daily_worker_count = 3
        cls.early.required_shifts = 18
        cls.early.save(update_fields=["daily_worker_count", "required_shifts"])
        cls.emp_e1 = Employee.objects.create(employee_no="E201", name="电工甲", team=cls.team, shift_config=cls.early, position="电工", min_required_shifts=18)
        cls.emp_e2 = Employee.objects.create(employee_no="E202", name="电工乙", team=cls.team, shift_config=cls.early, position="电工", min_required_shifts=18)
        cls.workers = [
            Employee.objects.create(
                employee_no=f"E20{i + 2}", name=f"普工{i}", team=cls.team,
                shift_config=cls.early, position="", min_required_shifts=18,
            )
            for i in range(1, 4)
        ]
        cls.early.exempt_employee_ids = [w.pk for w in cls.workers[:2]]
        cls.early.save(update_fields=["exempt_employee_ids"])

    def test_solve_shift_positions_feasible(self):
        from .planning import solve_shift_positions

        self.early.position_requirements = {"电工": 1}
        self.early.save(update_fields=["position_requirements"])
        result = solve_shift_positions(self.early)
        solution = result["solution"]
        self.assertIsNotNone(solution)
        self.assertEqual(solution.status, "optimal")
        days = result["total_days"]
        # 每天总人数 2、电工恰 1，且不跨班组
        self.assertEqual(solution.daily_counts, [3] * days)
        self.assertEqual(solution.daily_position_counts["电工"], [1] * days)
        self.assertTrue(
            {eid for eid, _ in solution.assignments}
            <= {e.pk for e in Employee.objects.filter(team=self.team)}
        )
        for (eid, _), pos in solution.position_assignments.items():
            self.assertEqual(pos, "电工")
            self.assertIn(eid, {self.emp_e1.pk, self.emp_e2.pk})

    def test_solve_shift_positions_infeasible_reports_reason(self):
        from .planning import solve_shift_positions

        self.early.position_requirements = {"电工": 3}
        self.early.save(update_fields=["position_requirements"])
        result = solve_shift_positions(self.early)
        solution = result["solution"]
        self.assertIsNotNone(solution)
        self.assertEqual(solution.status, "infeasible")
        self.assertIn("仅 2 人具备资格", solution.message)
        # 配置未被修改
        self.early.refresh_from_db()
        self.assertEqual(self.early.position_requirements, {"电工": 3})


class RestSolverAdapterTests(TestCase):
    # 第四阶段适配层：休息规则求解（含相邻周期边界数据读取）
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        # 每日 3 人：满足“普通人员最低班数 + 豁免受连续休息上限约束”的容量要求
        cls.early.daily_worker_count = 3
        cls.early.required_shifts = 18
        cls.early.save(update_fields=["daily_worker_count", "required_shifts"])
        cls.emp_e1 = Employee.objects.create(employee_no="E301", name="电工甲", team=cls.team, shift_config=cls.early, position="电工", min_required_shifts=18)
        cls.emp_e2 = Employee.objects.create(employee_no="E302", name="电工乙", team=cls.team, shift_config=cls.early, position="电工", min_required_shifts=18)
        cls.workers = [
            Employee.objects.create(
                employee_no=f"E30{i + 2}", name=f"普工{i}", team=cls.team,
                shift_config=cls.early, position="", min_required_shifts=18,
            )
            for i in range(1, 4)
        ]
        cls.early.exempt_employee_ids = [w.pk for w in cls.workers[:2]]
        cls.early.save(update_fields=["exempt_employee_ids"])

    def test_solve_shift_rest_feasible_with_boundary_lookup(self):
        import uuid

        from .models import ScheduleRecord
        from .planning import build_rest_problem, solve_shift_rest

        info = build_rest_problem(self.early)
        # 无相邻周期记录 → 边界数据为空，结果说明边界限制
        self.assertEqual(info["prev_work"], {})
        result = solve_shift_rest(self.early)
        solution = result["solution"]
        self.assertIsNotNone(solution)
        self.assertEqual(solution.status, "optimal")
        days = result["total_days"]
        self.assertEqual(solution.daily_counts, [3] * days)
        self.assertIn("周期首尾边界限制", solution.boundary_note)
        self.assertEqual(
            len(solution.assignments),
            days * 3,
        )

        # 写入上一周期末班记录后，边界数据被读取使用
        prev_date = result["start"] - datetime.timedelta(days=1)
        from .services import create_schedule_record, new_schedule_batch_id

        batch = new_schedule_batch_id()
        create_schedule_record(
            schedule_batch_id=batch, organization=self.org, team=self.team,
            shift_config=self.early, employee=self.emp_e1, work_date=prev_date,
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        create_schedule_record(
            schedule_batch_id=batch, organization=self.org, team=self.team,
            shift_config=self.early, employee=self.emp_e2, work_date=prev_date,
            shift_type=ScheduleRecord.ShiftType.REST,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        info = build_rest_problem(self.early)
        self.assertEqual(info["prev_work"].get(self.emp_e1.pk), True)
        self.assertEqual(info["prev_work"].get(self.emp_e2.pk), False)
        result = solve_shift_rest(self.early)
        solution = result["solution"]
        self.assertIsNotNone(solution)
        self.assertEqual(solution.status, "optimal")
        self.assertIn("边界限制", solution.boundary_note)  # 部分职工仍无边界数据


class ScheduleRecordModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.employee = Employee.objects.create(
            employee_no="E001", name="张三", team=cls.team,
            shift_config=cls.early, min_required_shifts=18,
        )
        cls.batch = uuid.uuid4()

    def _record(self, **overrides):
        fields = dict(
            schedule_batch_id=self.batch,
            organization=self.org,
            team=self.team,
            shift_config=self.early,
            employee=self.employee,
            work_date=datetime.date(2025, 1, 1),
            shift_type=ScheduleRecord.ShiftType.EARLY,
        )
        fields.update(overrides)
        return ScheduleRecord(**fields)

    def test_create_and_str(self):
        record = self._record()
        record.full_clean()
        record.save()
        self.assertIn("张三", str(record))

    def test_default_status_is_draft(self):
        record = self._record()
        record.full_clean()
        record.save()
        self.assertEqual(record.status, ScheduleRecord.Status.DRAFT)

    def test_unique_batch_employee_date(self):
        self._record().save()
        with self.assertRaises(IntegrityError):
            self._record(shift_type=ScheduleRecord.ShiftType.NIGHT).save()

    def test_rest_record_allowed(self):
        record = self._record(shift_type=ScheduleRecord.ShiftType.REST)
        record.full_clean()
        record.save()
        self.assertEqual(record.shift_type, ScheduleRecord.ShiftType.REST)

    def test_organization_team_mismatch(self):
        other_org = OrganizationUnit.objects.create(name="采煤二队", code="ORG-02")
        record = self._record(organization=other_org)
        with self.assertRaises(ValidationError):
            record.full_clean()

    def test_shift_config_team_mismatch(self):
        other_team = Team.objects.create(organization=self.org, name="二班", code="T01-02")
        night = other_team.shift_configs.get(shift_name=ShiftConfig.ShiftName.NIGHT)
        record = self._record(shift_config=night)
        with self.assertRaises(ValidationError):
            record.full_clean()

    def test_employee_team_mismatch(self):
        other_team = Team.objects.create(organization=self.org, name="二班", code="T01-02")
        night = other_team.shift_configs.get(shift_name=ShiftConfig.ShiftName.NIGHT)
        other_emp = Employee.objects.create(
            employee_no="E002", name="李四", team=other_team,
            shift_config=night, min_required_shifts=18,
        )
        record = self._record(employee=other_emp)
        with self.assertRaises(ValidationError):
            record.full_clean()

    def test_cross_shift_within_team_allowed(self):
        # 第六阶段调整：允许同班组内跨班次职工写入（人员池=班组在职职工），
        # 原“职工所属班次配置必须与记录一致”的校验已按业务确认移除
        night = self.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.NIGHT)
        emp2 = Employee.objects.create(
            employee_no="E003", name="王五", team=self.team,
            shift_config=night, min_required_shifts=18,
        )
        record = self._record(employee=emp2)
        record.full_clean()  # 不抛异常
        record.save()
        self.assertTrue(
            ScheduleRecord.objects.filter(employee=emp2).exists()
        )

    def test_deactivated_employee_history_still_queryable(self):
        # 停用人员后仍能查询其历史排班记录
        self._record().save()
        self.employee.is_active = False
        self.employee.save(update_fields=["is_active"])
        self.assertFalse(Employee.objects.filter(pk=self.employee.pk, is_active=True).exists())
        self.assertEqual(self.employee.schedule_records.count(), 1)
        self.assertEqual(self.team.schedule_records.count(), 1)

    def test_delete_employee_with_history_blocked(self):
        from django.db.models import ProtectedError

        self._record().save()
        with self.assertRaises(ProtectedError):
            self.employee.delete()

    def test_delete_team_with_history_blocked(self):
        from django.db.models import ProtectedError

        self._record().save()
        with self.assertRaises(ProtectedError):
            self.team.delete()

    def test_config_update_does_not_affect_history(self):
        # 修改班次配置不会意外修改已有历史排班记录
        record = self._record()
        record.full_clean()
        record.save()

        self.early.daily_worker_count = 9
        self.early.required_shifts = 20
        self.early.save(update_fields=["daily_worker_count", "required_shifts"])

        record.refresh_from_db()
        self.assertEqual(record.work_date, datetime.date(2025, 1, 1))
        self.assertEqual(record.shift_type, ScheduleRecord.ShiftType.EARLY)
        self.assertEqual(record.status, ScheduleRecord.Status.DRAFT)
        self.assertEqual(record.position, "")
        self.assertEqual(record.shift_config_id, self.early.pk)  # 历史记录仍指向同一配置


class ScheduleRecordCrossShiftTests(TestCase):
    # 第六阶段调整：排班记录允许同班组内跨班次职工（人员池=班组在职职工），
    # 跨班组写入仍被拒绝。
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.other_org = OrganizationUnit.objects.create(name="采煤二队", code="ORG-02")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")
        cls.other_team = Team.objects.create(organization=cls.other_org, name="外组", code="T99-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.night = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.NIGHT)
        cls.emp_night = Employee.objects.create(
            employee_no="E501", name="夜班职工", team=cls.team,
            shift_config=cls.night, min_required_shifts=18,
        )
        cls.emp_other = Employee.objects.create(
            employee_no="E502", name="外组职工", team=cls.other_team,
            shift_config=cls.other_team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY),
            min_required_shifts=18,
        )

    def _record(self, employee, shift_config):
        return ScheduleRecord(
            schedule_batch_id=uuid.uuid4(),
            organization=shift_config.team.organization,
            team=shift_config.team,
            shift_config=shift_config,
            employee=employee,
            work_date=datetime.date(2026, 10, 25),
            shift_type=ScheduleRecord.ShiftType.EARLY,
        )

    def test_cross_shift_within_team_allowed(self):
        # 早班排班记录可以写给本班组内所属班次为夜班的职工
        record = self._record(self.emp_night, self.early)
        record.full_clean()  # 不抛异常

    def test_cross_team_still_rejected(self):
        record = self._record(self.emp_other, self.early)
        with self.assertRaises(ValidationError):
            record.full_clean()
