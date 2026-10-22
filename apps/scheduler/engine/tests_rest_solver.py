# 休息规则（第四阶段）求解测试。
from unittest import skipIf

from django.test import SimpleTestCase

from .allocator import plan_shift_targets
from .position_solver import solve_position_assignment
from .rest_solver import (
    RestProblem,
    RestSolution,
    compute_rest_quality,
    solve_rest_assignment,
    validate_rest_solution,
)

try:
    import ortools  # noqa: F401

    HAS_ORTOOLS = True
except ImportError:
    HAS_ORTOOLS = False


def make_problem(
    *,
    qualifications,
    day_count=6,
    daily_required=3,
    position_requirements=None,
    exempt_ids=None,
    custom_targets=None,
    prev_work=None,
    next_work=None,
):
    employee_ids = list(qualifications.keys())
    exempt = set(exempt_ids or [])
    normal = [i for i in employee_ids if i not in exempt]
    allocation = plan_shift_targets(
        total_days=day_count,
        daily_required=daily_required,
        base_shifts=18,
        normal_ids=normal,
        exempt_ids=list(exempt),
    )
    targets = custom_targets if custom_targets is not None else None
    if targets is None:
        if allocation.feasible:
            targets = allocation.per_person_targets
        else:
            # 求解器机制测试使用固定夹具目标（名额紧张的分配不可行性由 allocator 测试覆盖）
            total = day_count * daily_required
            q, r = divmod(total, len(employee_ids))
            targets = {eid: q + (1 if i < r else 0) for i, eid in enumerate(employee_ids)}
    return RestProblem(
        employee_ids=employee_ids,
        qualifications=qualifications,
        day_count=day_count,
        daily_required=daily_required,
        position_requirements=position_requirements or {},
        targets=targets,
        prev_work=prev_work or {},
        next_work=next_work or {},
    ), allocation


def work_pattern(solution, eid, day_count):
    return [1 if (eid, day) in solution.assignments else 0 for day in range(day_count)]


@skipIf(not HAS_ORTOOLS, "未安装 ortools，跳过休息规则求解测试")
class RestSolverTests(SimpleTestCase):
    def test_no_isolated_work_internal_days(self):
        # 内部日期不得出现 休—上—休
        quals = {i: [] for i in range(1, 5)}
        problem, _ = make_problem(qualifications=quals, day_count=8, daily_required=2)
        solution = solve_rest_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        for eid in problem.employee_ids:
            work = work_pattern(solution, eid, 8)
            for d in range(1, 7):
                self.assertFalse(
                    work[d] == 1 and work[d - 1] == 0 and work[d + 1] == 0,
                    f"职工 {eid} 第 {d + 1} 天违反禁止孤立工作日",
                )
        self.assertEqual(validate_rest_solution(problem, solution), [])

    def test_no_single_day_rest_internal_days(self):
        # 内部日期不得出现 上—休—上
        quals = {i: [] for i in range(1, 5)}
        problem, _ = make_problem(qualifications=quals, day_count=8, daily_required=2)
        solution = solve_rest_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        for eid in problem.employee_ids:
            work = work_pattern(solution, eid, 8)
            for d in range(1, 7):
                self.assertFalse(
                    work[d] == 0 and work[d - 1] == 1 and work[d + 1] == 1,
                    f"职工 {eid} 第 {d + 1} 天违反禁止单日休息",
                )

    def test_boundary_without_neighbor_data_reports_limitation(self):
        quals = {i: [] for i in range(1, 4)}
        problem, _ = make_problem(qualifications=quals, day_count=6, daily_required=2)
        solution = solve_rest_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertIn("周期首尾边界限制", solution.boundary_note)

    def test_boundary_with_neighbor_data_applies_rules(self):
        # 提供相邻周期数据后，首尾日同样遵守休息规则且校验通过
        quals = {1: [], 2: [], 3: []}
        problem, _ = make_problem(
            qualifications=quals, day_count=6, daily_required=2,
            prev_work={1: True, 2: False, 3: True},
            next_work={1: False, 2: True, 3: True},
        )
        solution = solve_rest_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(validate_rest_solution(problem, solution), [])
        self.assertIn("已使用相邻周期真实排班数据", solution.boundary_note)

    def test_boundary_data_can_make_problem_infeasible(self):
        # D=1、N=1、R=1：无边界数据时可行（唯一一天上班）；
        # 提供 prev=休、next=休 后，该天上班违反「休—上—休」→ 硬性无解
        quals = {1: []}
        problem_no_boundary, _ = make_problem(
            qualifications=quals, day_count=1, daily_required=1, custom_targets={1: 1}
        )
        solution = solve_rest_assignment(problem_no_boundary)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(validate_rest_solution(problem_no_boundary, solution), [])

        problem_boundary, _ = make_problem(
            qualifications=quals, day_count=1, daily_required=1, custom_targets={1: 1},
            prev_work={1: False}, next_work={1: False},
        )
        solution = solve_rest_assignment(problem_boundary)
        self.assertEqual(solution.status, "infeasible")
        self.assertIn("休息规则", solution.message)

    def test_rest_rules_make_base_feasible_problem_infeasible(self):
        # D=1、N=1、R=1 + 边界休休：基础约束（每日人数）单独可满足，
        # 加入硬性休息规则后无解；诊断须区分两类原因并说明能力边界
        quals = {1: []}
        problem, _ = make_problem(
            qualifications=quals, day_count=1, daily_required=1, custom_targets={1: 1},
            prev_work={1: False}, next_work={1: False},
        )
        from .position_solver import PositionProblem, solve_position_assignment

        base = PositionProblem(
            employee_ids=problem.employee_ids,
            qualifications=problem.qualifications,
            day_count=1,
            daily_required=1,
            position_requirements={},
            targets=problem.targets,
        )
        self.assertIn(solve_position_assignment(base).status, ("optimal", "feasible"))
        solution = solve_rest_assignment(problem)
        self.assertEqual(solution.status, "infeasible")
        self.assertIn("加入硬性休息规则", solution.message)
        self.assertIn("诊断能力边界", solution.message)

    def test_window_with_2_and_3_day_rest(self):
        # D=10、N=2、R=1：每人都应有一段 ≥3 天连续休息 → 2天与3天窗口均满足
        quals = {1: [], 2: []}
        problem, _ = make_problem(qualifications=quals, day_count=10, daily_required=1)
        solution = solve_rest_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.total_windows, 2 * (10 - 9))
        self.assertEqual(solution.windows_with_2_block, solution.total_windows)
        self.assertEqual(solution.windows_with_3_block, solution.total_windows)
        self.assertEqual(validate_rest_solution(problem, solution), [])

    def test_window_without_2_day_rest_still_valid(self):
        # N=1、R=1、D=10：每天都上班 → 无连续2天休息；
        # 软目标未达成不得判定无解，需报告优化未完全满足
        quals = {1: []}
        problem, _ = make_problem(qualifications=quals, day_count=10, daily_required=1)
        solution = solve_rest_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.windows_with_2_block, 0)
        self.assertIn("休息优化未完全满足", solution.message)
        # 质量指标未满足不影响排班有效性
        self.assertEqual(validate_rest_solution(problem, solution), [])

    def test_joint_daily_position_rest(self):
        # 每日人数 + 岗位人数 + 休息规则联合求解
        quals = {1: ["电工"], 2: ["电工"], 3: [], 4: [], 5: []}
        problem, _ = make_problem(
            qualifications=quals, day_count=10, daily_required=3,
            position_requirements={"电工": 1},
        )
        solution = solve_rest_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.daily_counts, [3] * 10)
        self.assertEqual(solution.daily_position_counts["电工"], [1] * 10)
        for (eid, day), pos in solution.position_assignments.items():
            self.assertEqual(pos, "电工")
            self.assertIn(eid, {1, 2})
        self.assertEqual(validate_rest_solution(problem, solution), [])

    def test_deviation_positive_with_rest_rules(self):
        # 目标总班数 6 > 总名额 4 → 偏差最小为 2，休息规则仍满足
        quals = {1: [], 2: []}
        problem, _ = make_problem(
            qualifications=quals, day_count=4, daily_required=1, custom_targets={1: 3, 2: 3}
        )
        solution = solve_rest_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.total_deviation, 2)
        self.assertEqual(solution.daily_counts, [1] * 4)
        self.assertEqual(validate_rest_solution(problem, solution), [])

    def test_deviation_forced_by_positions_with_rest(self):
        # 岗位约束迫使电工每天到岗（目标 2 实际 6），偏差如实报告、休息规则成立
        quals = {1: ["电工"], 2: ["电工"]}
        problem, _ = make_problem(
            qualifications=quals, day_count=6, daily_required=2,
            position_requirements={"电工": 2}, custom_targets={1: 2, 2: 2},
        )
        solution = solve_rest_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.achieved, {1: 6, 2: 6})
        self.assertEqual(solution.total_deviation, 8)
        self.assertEqual(validate_rest_solution(problem, solution), [])


class RestQualityMetricTests(SimpleTestCase):
    def test_compute_rest_quality(self):
        # 休息日 2,3 → 仅连续2天块；休息日 2,3,4 → 含连续3天块；全上班 → 0
        self.assertEqual(
            compute_rest_quality([1], 10, {(1, 0), (1, 1), (1, 4), (1, 5), (1, 6), (1, 7), (1, 8), (1, 9)}),
            (1, 0, 1),
        )
        self.assertEqual(
            compute_rest_quality([1], 10, {(1, 0), (1, 1), (1, 5), (1, 6), (1, 7), (1, 8), (1, 9)}),
            (1, 1, 1),
        )
        self.assertEqual(
            compute_rest_quality([1], 10, {(1, d) for d in range(10)}),
            (0, 0, 1),
        )
        self.assertEqual(compute_rest_quality([1], 5, set()), (0, 0, 0))  # 不足10天无窗口


@skipIf(not HAS_ORTOOLS, "未安装 ortools，跳过休息校验测试")
class RestValidatorTests(SimpleTestCase):
    def _blank_solution(self, problem):
        return RestSolution(
            assignments=set(),
            achieved={eid: 0 for eid in problem.employee_ids},
            daily_counts=[0] * problem.day_count,
            daily_position_counts={p: [0] * problem.day_count for p in problem.position_requirements},
            total_deviation=sum(problem.targets.values()),
        )

    def test_validator_detects_isolated_work(self):
        quals = {1: []}
        problem, _ = make_problem(qualifications=quals, day_count=3, daily_required=1,
                                  custom_targets={1: 1})
        solution = self._blank_solution(problem)
        solution.assignments = {(1, 1)}  # 休—上—休（内部）
        errors = validate_rest_solution(problem, solution)
        self.assertTrue(any("禁止孤立工作日" in e for e in errors))

    def test_validator_detects_single_day_rest(self):
        quals = {1: []}
        problem, _ = make_problem(qualifications=quals, day_count=3, daily_required=1,
                                  custom_targets={1: 1})
        solution = self._blank_solution(problem)
        solution.assignments = {(1, 0), (1, 2)}  # 上—休—上（内部）
        errors = validate_rest_solution(problem, solution)
        self.assertTrue(any("禁止单日休息" in e for e in errors))

    def test_validator_detects_boundary_violation_with_neighbor_data(self):
        quals = {1: []}
        problem, _ = make_problem(qualifications=quals, day_count=3, daily_required=1,
                                  custom_targets={1: 1}, prev_work={1: False})
        solution = self._blank_solution(problem)
        solution.assignments = {(1, 0)}  # 首日上班，前一天休息、次日休息 → 边界孤立工作日
        errors = validate_rest_solution(problem, solution)
        self.assertTrue(any("禁止孤立工作日" in e for e in errors))

    def test_validator_detects_quality_metric_mismatch(self):
        quals = {1: [], 2: []}
        problem, _ = make_problem(qualifications=quals, day_count=10, daily_required=1)
        solution = solve_rest_assignment(problem)
        solution.windows_with_2_block += 100
        errors = validate_rest_solution(problem, solution)
        self.assertTrue(any("休息质量指标统计错误" in e for e in errors))
