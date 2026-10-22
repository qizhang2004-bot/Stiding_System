# 统一综合求解入口（第五阶段）测试。
import datetime
from unittest import mock

from django.test import TestCase

from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee

from .engine.rest_solver import RestSolution
from .models import ScheduleRecord
from .planning import (
    STATUS_FEASIBLE,
    STATUS_INFEASIBLE,
    STATUS_INVALID,
    STATUS_UNKNOWN,
    solve_shift_schedule,
)

ANCHOR = datetime.date(2026, 10, 25)  # 固定周期 2026-10-25 ~ 2026-11-24，共 31 天


class UnifiedSolveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.early.daily_worker_count = 18
        cls.early.required_shifts = 18
        cls.early.save(update_fields=["daily_worker_count", "required_shifts"])

    def _make_employees(self, count, positions=None, start_no=1):
        positions = positions or {}
        employees = []
        for i in range(count):
            employees.append(
                Employee.objects.create(
                    employee_no=f"U{start_no + i:03d}",
                    name=f"统一{i + start_no}",
                    team=self.team,
                    shift_config=self.early,
                    position=positions.get(i + start_no, ""),
                    min_required_shifts=18,
                )
            )
        return employees

    def test_surplus_mode_normal_solve(self):
        # D=31、R=18、M=18、N=25 → 可排开最大人数 31 > 25 → 人多模式
        self._make_employees(25)
        result = solve_shift_schedule(self.early, anchor=ANCHOR, time_limit_seconds=20)
        self.assertEqual(result.status, STATUS_FEASIBLE)
        self.assertEqual(result.mode, "surplus")
        self.assertEqual(result.total_days, 31)
        self.assertEqual(result.daily_worker_count, 18)
        self.assertEqual(result.validation_errors, [])
        self.assertEqual(result.solution.daily_counts, [18] * 31)
        # 无其他约束时目标班数全部达成
        self.assertEqual(result.total_shift_deviation, 0)
        self.assertEqual(result.employee_actual_shifts, result.employee_targets)

    def test_exempt_employee_no_longer_forced_by_rest_limits(self):
        # D=31、R=18、N=32、豁免 1 人：普通 31 人×18 班 = 558 班恰好占满名额。
        # 豁免人员不再受“连续休息上限”约束，因此其最少出勤为 0，配置可行。
        employees = self._make_employees(32)
        self.early.exempt_employee_ids = [employees[-1].pk]
        self.early.save(update_fields=["exempt_employee_ids"])
        result = solve_shift_schedule(self.early, anchor=ANCHOR, time_limit_seconds=20)
        self.assertEqual(result.status, STATUS_FEASIBLE, result.diagnostics)
        for employee in employees[:-1]:
            self.assertGreaterEqual(result.employee_actual_shifts[employee.pk], 18)

    def test_tight_mode_minimum_conflict_reported(self):
        # D=31、R=18、N=32、全部为普通人员：32×18 = 576 > 558 个名额 → 容量冲突，
        # 必须如实报告不可行，绝不把普通人员静默降为 17 班。
        self._make_employees(32)
        result = solve_shift_schedule(self.early, anchor=ANCHOR, time_limit_seconds=20)
        self.assertEqual(result.status, STATUS_INFEASIBLE)
        self.assertTrue(
            any("最低班数" in d for d in result.diagnostics),
            result.diagnostics,
        )

    def test_daily_position_rest_jointly_hold(self):
        employees = self._make_employees(25, positions={1: "电工", 2: "电工", 3: "电工"})
        self.early.position_requirements = {"电工": 2}
        self.early.save(update_fields=["position_requirements"])
        result = solve_shift_schedule(self.early, anchor=ANCHOR, time_limit_seconds=20)
        self.assertEqual(result.status, STATUS_FEASIBLE)
        self.assertEqual(result.solution.daily_counts, [18] * 31)
        self.assertEqual(result.solution.daily_position_counts["电工"], [2] * 31)
        summary = result.position_summary["电工"]
        self.assertEqual(summary["required"], 2)
        self.assertEqual(summary["qualified"], 3)
        self.assertTrue(summary["all_days_match"])
        self.assertEqual(result.validation_errors, [])

    def test_deviation_positive_but_feasible(self):
        # 岗位要求迫使 5 名电工资格者每天到岗 → 实际班数远超目标，仍可行并如实报告
        self._make_employees(25, positions={i: "电工" for i in range(1, 6)})
        self.early.position_requirements = {"电工": 5}
        self.early.save(update_fields=["position_requirements"])
        result = solve_shift_schedule(self.early, anchor=ANCHOR, time_limit_seconds=20)
        self.assertEqual(result.status, STATUS_FEASIBLE)
        self.assertGreater(result.total_shift_deviation, 0)
        self.assertTrue(any("班数目标未完全达成" in d for d in result.diagnostics))
        self.assertEqual(result.solution.daily_position_counts["电工"], [5] * 31)

    def test_rest_soft_unmet_but_feasible(self):
        # R == N：全员每天上班 → 无连续2天休息窗口，软目标未达成但排班有效
        self.early.daily_worker_count = 18
        self.early.save(update_fields=["daily_worker_count"])
        self._make_employees(18)
        result = solve_shift_schedule(self.early, anchor=ANCHOR)
        self.assertEqual(result.status, STATUS_FEASIBLE)
        self.assertEqual(result.rest_quality["windows_with_2_block"], 0)
        self.assertGreater(result.rest_quality["total_windows"], 0)
        self.assertTrue(any("休息软目标未完全满足" in d for d in result.diagnostics))

    def test_daily_count_infeasible(self):
        # R=18 > N=5
        self._make_employees(5)
        result = solve_shift_schedule(self.early, anchor=ANCHOR)
        self.assertEqual(result.status, STATUS_INFEASIBLE)
        self.assertTrue(any("无法满足每日人数" in d for d in result.diagnostics))
        self.assertEqual(result.solution, None)

    def test_position_infeasible(self):
        self._make_employees(25, positions={1: "电工", 2: "电工"})
        self.early.position_requirements = {"电工": 3}
        self.early.save(update_fields=["position_requirements"])
        result = solve_shift_schedule(self.early, anchor=ANCHOR)
        self.assertEqual(result.status, STATUS_INFEASIBLE)
        self.assertTrue(any("仅 2 人具备资格" in d for d in result.diagnostics))

    def test_rest_infeasible_propagated(self):
        # 休息无解场景由底层求解器产生（引擎层已覆盖），此处验证统一入口正确传递。
        # R=18 ≤ N=20：确保进入求解阶段（而非分配层拦截），mock 才能生效
        self._make_employees(20)
        infeasible = RestSolution(
            status="infeasible",
            message="每日人数与岗位人数约束单独可满足，但加入硬性休息规则后无解。；诊断能力边界：无法自动精确定位。",
        )
        with mock.patch(
            "apps.scheduler.planning.solve_rest_assignment", return_value=infeasible
        ):
            result = solve_shift_schedule(self.early, anchor=ANCHOR)
        self.assertEqual(result.status, STATUS_INFEASIBLE)
        self.assertTrue(any("加入硬性休息规则" in d for d in result.diagnostics))

    def test_unknown_status_reported_honestly(self):
        self._make_employees(20)
        unknown = RestSolution(status="unknown", message="求解超时：未能在限定时间内证明可行或不可行。")
        with mock.patch(
            "apps.scheduler.planning.solve_rest_assignment", return_value=unknown
        ):
            result = solve_shift_schedule(self.early, anchor=ANCHOR)
        self.assertEqual(result.status, STATUS_UNKNOWN)  # 既不是不可行也不是成功
        self.assertTrue(any("求解超时" in d for d in result.diagnostics))

    def test_validation_failure_never_succeeds(self):
        self._make_employees(20)
        corrupted = RestSolution(
            status="optimal",
            assignments=set(),
            achieved={},
            daily_counts=[0] * 31,
            daily_position_counts={},
            total_deviation=0,
            windows_with_2_block=0,
            windows_with_3_block=0,
            total_windows=0,
        )
        with mock.patch(
            "apps.scheduler.planning.solve_rest_assignment", return_value=corrupted
        ):
            result = solve_shift_schedule(self.early, anchor=ANCHOR)
        self.assertEqual(result.status, STATUS_INVALID)
        self.assertTrue(result.validation_errors)
        self.assertTrue(any("独立校验失败" in d for d in result.diagnostics))

    def test_boundary_data_passed_through(self):
        from .services import create_schedule_record, new_schedule_batch_id

        self.early.daily_worker_count = 3
        self.early.save(update_fields=["daily_worker_count"])
        employees = self._make_employees(5)
        # 名额紧张：豁免 2 人（K=floor(2×31/18)=3），否则分配不可行
        self.early.exempt_employee_ids = [employees[0].pk, employees[1].pk]
        self.early.save(update_fields=["exempt_employee_ids"])
        prev_date = ANCHOR - datetime.timedelta(days=1)
        batch = new_schedule_batch_id()
        for emp in employees:
            create_schedule_record(
                schedule_batch_id=batch, organization=self.org, team=self.team,
                shift_config=self.early, employee=emp, work_date=prev_date,
                shift_type=ScheduleRecord.ShiftType.EARLY,
                status=ScheduleRecord.Status.CONFIRMED,
            )
        result = solve_shift_schedule(self.early, anchor=ANCHOR)
        self.assertEqual(result.status, STATUS_FEASIBLE)
        # 上一周期末班数据完整传递（5/5）；下一周期数据不存在（0/5）→ 如实报告边界限制
        self.assertIn("周期首尾边界限制", result.boundary_note)
        self.assertIn("5/5 名职工有上一周期末班数据", result.boundary_note)
        self.assertIn("0/5 名职工有下一周期首班数据", result.boundary_note)
