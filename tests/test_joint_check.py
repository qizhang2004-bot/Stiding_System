# 联合排查验收测试：连续休息硬约束、边界检测、正式记录冲突与作废替换、硬约束优先级。
import datetime
from unittest import skipIf

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.drafting import (
    build_conflict_report,
    confirm_schedule_draft,
    create_schedule_draft,
)
from apps.scheduler.engine.allocator import plan_shift_targets
from apps.scheduler.engine.rest_solver import (
    RestProblem,
    RestSolution,
    compute_rest_runs,
    determinable_rest_runs,
    solve_rest_assignment,
    validate_rest_solution,
)
from apps.scheduler.models import ScheduleBatch, ScheduleRecord
from apps.scheduler.services import create_schedule_record, new_schedule_batch_id

try:
    import ortools  # noqa: F401

    HAS_ORTOOLS = True
except ImportError:
    HAS_ORTOOLS = False

User = get_user_model()


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class RestRunHelperTests(TestCase):
    def test_compute_rest_runs_with_boundaries(self):
        # 内部 4 天连续休息
        work = [1, 1, 0, 0, 0, 0, 1, 1]
        self.assertEqual(compute_rest_runs(work), [4])
        # 周期开始处连续休息（前一天休息 → 拼接为 5）
        self.assertEqual(compute_rest_runs([0, 0, 0, 0, 1], prev=False), [5])
        # 周期结束处连续休息（后一天休息 → 拼接）
        self.assertEqual(compute_rest_runs([1, 0, 0, 0, 0], next=False), [5])


class ConsecutiveRestConstraintTests(TestCase):
    def _problem(self, day_count=10, daily_required=2, **kwargs):
        employees = [1, 2, 3, 4]
        targets = kwargs.pop("targets", None) or {
            eid: day_count * daily_required // len(employees) for eid in employees
        }
        return RestProblem(
            employee_ids=employees,
            qualifications={eid: [] for eid in employees},
            day_count=day_count,
            daily_required=daily_required,
            position_requirements={},
            targets=targets,
            **kwargs,
        )

    def _single_problem(self, day_count, target, **kwargs):
        return RestProblem(
            employee_ids=[1],
            qualifications={1: []},
            day_count=day_count,
            daily_required=1,
            position_requirements={},
            targets={1: target},
            **kwargs,
        )

    def test_max_rest_four_allowed_five_rejected_by_validator(self):
        # 4 天连续休息：允许（无违规）；5 天：违反硬约束
        problem = self._single_problem(10, 5, max_consecutive_rest_days=4)
        ok_solution = RestSolution(
            status="optimal",
            assignments={(1, d) for d in range(5, 10)},
            achieved={1: 5},
            daily_counts=[1] * 10,
            total_deviation=0,
            windows_with_2_block=1,
            windows_with_3_block=1,
            total_windows=1,
        )
        errors = validate_rest_solution(problem, ok_solution)
        self.assertFalse(any("超过配置上限" in e for e in errors), errors)

        # 周期中部的 5 天连续休息（可判定，必报错）
        bad_solution = RestSolution(
            status="optimal",
            assignments={(1, d) for d in (0, 1, 7, 8, 9)},
            achieved={1: 5},
            daily_counts=[1] * 10,
            total_deviation=0,
            windows_with_2_block=1,
            windows_with_3_block=1,
            total_windows=1,
        )
        errors = validate_rest_solution(problem, bad_solution)
        self.assertTrue(any("连续休息 5 天，超过配置上限 4 天" in e for e in errors), errors)

    @skipIf(not HAS_ORTOOLS, "未安装 ortools")
    def test_solver_respects_max_rest_in_output(self):
        # 目标班数偏低导致大量休息时，求解器仍必须满足连续休息 ≤4
        problem = self._problem(
            day_count=20,
            daily_required=2,
            targets={1: 6, 2: 6, 3: 6, 4: 6},
            max_consecutive_rest_days=4,
        )
        solution = solve_rest_assignment(problem, time_limit_seconds=20)
        self.assertIn(solution.status, ("optimal", "feasible"))
        self.assertLessEqual(solution.max_rest_run, 4)
        self.assertEqual(validate_rest_solution(problem, solution), [])

    def test_normal_shift_below_minimum_is_invalid(self):
        # 普通人员最低班数 18 为硬下限：实际 17 班必须判定违规（不得显示 17/17 就算合格）
        problem = RestProblem(
            employee_ids=[1],
            qualifications={1: []},
            day_count=20,
            daily_required=1,
            position_requirements={},
            targets={1: 18},
            minimum_shifts={1: 18},
            max_consecutive_rest_days=4,
            min_consecutive_rest_days=2,
        )
        solution = RestSolution(
            status="optimal",
            assignments={(1, d) for d in range(0, 17)},
            achieved={1: 17},
            daily_counts=[1] * 17 + [0] * 3,
            total_deviation=1,
            windows_with_2_block=2,
            windows_with_3_block=2,
            total_windows=11,
        )
        errors = validate_rest_solution(problem, solution)
        self.assertTrue(
            any("低于最低班数硬下限 18 班" in e for e in errors), errors
        )

    def test_boundary_rest_run_detected_at_start_and_end(self):
        # 周期开始：前一日休息 + 周期前 4 天休息 = 5 天（超上限）→ 必须报错
        problem_start = self._single_problem(
            8, 4, max_consecutive_rest_days=4, min_consecutive_rest_days=2,
            prev_work={1: False},
        )
        start_solution = RestSolution(
            status="optimal",
            assignments={(1, d) for d in range(4, 8)},
            achieved={1: 4},
            daily_counts=[1] * 8,
            total_deviation=0,
            total_windows=0,
        )
        errors = validate_rest_solution(problem_start, start_solution)
        self.assertTrue(any("连续休息 5 天" in e for e in errors), errors)

        # 周期结束：后一日休息 + 周期末 4 天休息 = 5 天（超上限）→ 必须报错
        problem_tail = self._single_problem(
            8, 4, max_consecutive_rest_days=4, min_consecutive_rest_days=2,
            next_work={1: False},
        )
        tail_solution = RestSolution(
            status="optimal",
            assignments={(1, d) for d in range(0, 4)},
            achieved={1: 4},
            daily_counts=[1] * 8,
            total_deviation=0,
            total_windows=0,
        )
        errors_tail = validate_rest_solution(problem_tail, tail_solution)
        self.assertTrue(any("连续休息 5 天" in e for e in errors_tail), errors_tail)

        # 边界未知时，与未知相邻周期相接的首/尾休息段不参与判定
        unknown = self._single_problem(8, 4, max_consecutive_rest_days=4)
        boundary_solution = RestSolution(
            status="optimal",
            assignments={(1, d) for d in range(4, 8)},
            achieved={1: 4},
            daily_counts=[1] * 8,
            total_deviation=0,
            total_windows=0,
        )
        self.assertEqual(validate_rest_solution(unknown, boundary_solution), [])
        self.assertEqual(determinable_rest_runs([0, 0, 0, 0, 1, 1, 1, 1]), [])

    @skipIf(not HAS_ORTOOLS, "未安装 ortools")
    def test_position_and_rest_infeasible_reports_instead_of_relaxing(self):
        # 岗位要求迫使某人每天到岗（无休息），但每日人数过低导致其他人必须超出上限休息
        problem = RestProblem(
            employee_ids=[1, 2],
            qualifications={1: ["电工"], 2: []},
            day_count=10,
            daily_required=1,
            position_requirements={"电工": 1},
            targets={1: 10, 2: 0},
            max_consecutive_rest_days=4,
            min_consecutive_rest_days=2,
        )
        solution = solve_rest_assignment(problem, time_limit_seconds=20)
        # 职工2 必须休息 10 天（>4）→ 无解；必须如实返回不可行，不得放宽约束
        self.assertEqual(solution.status, "infeasible")
        self.assertTrue(solution.message)


class ConflictAndSupersedeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("conf_super")
        cls.org = OrganizationUnit.objects.create(name="甲队", code="ORG-A")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="ORG-B")
        cls.team = Team.objects.create(organization=cls.org, name="甲一班", code="TA-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.early.daily_worker_count = 3
        cls.early.required_shifts = 18
        cls.early.max_consecutive_rest_days = 4
        cls.early.save(
            update_fields=["daily_worker_count", "required_shifts", "max_consecutive_rest_days"]
        )
        cls.employees = [
            Employee.objects.create(
                employee_no=f"K{i:03d}", name=f"冲突{i}", team=cls.team,
                shift_config=cls.early, min_required_shifts=18,
            )
            for i in range(1, 6)
        ]
        # 名额紧张：豁免 2 人，保证分配可行
        cls.early.exempt_employee_ids = [cls.employees[3].pk, cls.employees[4].pk]
        cls.early.save(update_fields=["exempt_employee_ids"])

    def _make_draft(self):
        result = create_schedule_draft(self.early, self.super_user)
        self.assertEqual(result["errors"], [])
        return result["draft"]

    def _confirm_active_batch(self, draft):
        return confirm_schedule_draft(draft.batch_id, self.super_user)

    def test_same_day_duplicate_record_blocks_confirm(self):
        from apps.scheduler.drafting import ACTIVE_RECORD_STATUSES

        draft = self._make_draft()
        # 预先写入一条同职工同日期的正式记录（模拟真实重复）
        first_date = draft.period_start
        create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org, team=self.team, shift_config=self.early,
            employee=self.employees[0], work_date=first_date,
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        result = confirm_schedule_draft(draft.batch_id, self.super_user)
        self.assertFalse(result["confirmed"])
        self.assertIn("已有正式排班记录冲突", result["errors"][0])
        report = result["conflict_report"]
        self.assertEqual(report["total"], 1)
        sample = report["samples"][0]
        for key in ("employee_id", "employee_no", "name", "work_date", "team", "record_id", "batch_id"):
            self.assertIn(key, sample)
        # 未写入任何正式记录（除预置的一条外）
        self.assertEqual(
            ScheduleRecord.objects.filter(schedule_batch_id=draft.batch_id).count(), 0
        )

    def test_records_outside_period_or_team_not_counted(self):
        draft = self._make_draft()
        other_team = Team.objects.create(organization=self.org_b, name="乙一班", code="TB-01")
        other_config = other_team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        other_emp = Employee.objects.create(
            employee_no="K900", name="外部", team=other_team,
            shift_config=other_config, min_required_shifts=18,
        )
        # 其他班组、其他周期的正式记录不得被计为冲突
        create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_b, team=other_team, shift_config=other_config,
            employee=other_emp, work_date=draft.period_start,
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org, team=self.team, shift_config=self.early,
            employee=self.employees[0],
            work_date=draft.period_start - datetime.timedelta(days=5),
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        report = build_conflict_report(draft)
        self.assertEqual(report["total"], 0)
        self.assertTrue(confirm_schedule_draft(draft.batch_id, self.super_user)["confirmed"])

    def test_supersede_flow_replaces_without_deleting(self):
        first = self._make_draft()
        self.assertTrue(confirm_schedule_draft(first.batch_id, self.super_user)["confirmed"])
        old_count = ScheduleRecord.objects.filter(schedule_batch_id=first.batch_id).count()
        self.assertGreater(old_count, 0)

        second = self._make_draft()
        # 新规则：确认时自动识别并作废同队组、同周期的旧有效版本（无需显式参数）
        replaced = confirm_schedule_draft(second.batch_id, self.super_user)
        self.assertTrue(replaced["confirmed"], replaced["errors"])
        first.refresh_from_db()
        self.assertEqual(first.status, ScheduleBatch.Status.SUPERSEDED)
        # 旧记录仍在（未删除），但已标记作废；不再计入冲突
        old_records = ScheduleRecord.objects.filter(schedule_batch_id=first.batch_id)
        self.assertEqual(old_records.count(), old_count)
        self.assertFalse(
            old_records.exclude(status=ScheduleRecord.Status.SUPERSEDED).exists()
        )
        self.assertEqual(build_conflict_report(second)["total"], 0)

    def test_confirm_failure_rolls_back_everything(self):
        # 写入过程中失败必须整体回滚：不产生半截正式记录，批次仍为草稿
        from unittest import mock

        from apps.scheduler import drafting

        draft = self._make_draft()
        original = drafting.create_schedule_record
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("模拟写入中途失败")
            return original(*args, **kwargs)

        with mock.patch.object(drafting, "create_schedule_record", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                confirm_schedule_draft(draft.batch_id, self.super_user)

        self.assertEqual(
            ScheduleRecord.objects.filter(schedule_batch_id=draft.batch_id).count(), 0
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, ScheduleBatch.Status.DRAFT)

    def test_duplicate_confirm_does_not_write_twice(self):
        draft = self._make_draft()
        first = confirm_schedule_draft(draft.batch_id, self.super_user)
        self.assertTrue(first["confirmed"])
        count = ScheduleRecord.objects.filter(schedule_batch_id=draft.batch_id).count()
        second = confirm_schedule_draft(draft.batch_id, self.super_user)
        self.assertFalse(second["confirmed"])
        self.assertTrue(any("已确认" in e for e in second["errors"]))
        self.assertEqual(
            ScheduleRecord.objects.filter(schedule_batch_id=draft.batch_id).count(), count
        )

    def test_allocation_still_enforces_minimum_and_exempt_rules(self):
        # 修复过程中不得破坏：普通人员 18 班下限、豁免分配与总量守恒
        result = create_schedule_draft(self.early, self.super_user)
        allocation = result["result"].allocation
        targets = allocation.per_person_targets
        exempt = set(self.early.exempt_employee_ids)
        for emp in self.employees:
            if emp.pk in exempt:
                continue
            self.assertEqual(targets[emp.pk], 18)
        self.assertEqual(sum(targets.values()), allocation.total_slots)
