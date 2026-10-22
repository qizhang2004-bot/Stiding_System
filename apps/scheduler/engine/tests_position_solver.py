# 特殊岗位人数约束（第三阶段）求解测试。
from unittest import skipIf

from django.test import SimpleTestCase

from .allocator import plan_shift_targets
from .position_solver import (
    PositionProblem,
    diagnose_position_infeasibility,
    solve_position_assignment,
    validate_position_solution,
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
    base_shifts=18,
    exempt_ids=None,
    custom_targets=None,
):
    # qualifications: {职工ID: [岗位, ...]}
    employee_ids = list(qualifications.keys())
    exempt = set(exempt_ids or [])
    normal = [i for i in employee_ids if i not in exempt]
    allocation = plan_shift_targets(
        total_days=day_count,
        daily_required=daily_required,
        base_shifts=base_shifts,
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
    return PositionProblem(
        employee_ids=employee_ids,
        qualifications=qualifications,
        day_count=day_count,
        daily_required=daily_required,
        position_requirements=position_requirements or {},
        targets=targets,
    ), allocation


@skipIf(not HAS_ORTOOLS, "未安装 ortools，跳过岗位约束求解测试")
class PositionSolverTests(SimpleTestCase):
    def test_single_position_exact_daily_and_targets(self):
        # 3 名电工资格，每天要求 2 名电工；与每日人数约束同时成立，目标偏差 0
        quals = {1: ["电工"], 2: ["电工"], 3: ["电工"], 4: [], 5: []}
        problem, allocation = make_problem(
            qualifications=quals, daily_required=3, position_requirements={"电工": 2}
        )
        solution = solve_position_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        # 每天总人数恰好 3
        self.assertEqual(solution.daily_counts, [3] * 6)
        # 每天电工恰好 2
        self.assertEqual(solution.daily_position_counts["电工"], [2] * 6)
        # 岗位人员具备资格且计入当日总人数
        for (eid, day), pos in solution.position_assignments.items():
            self.assertEqual(pos, "电工")
            self.assertIn(eid, quals)
            self.assertIn((eid, day), solution.assignments)
        # 目标班数偏差 0（无其他约束时目标与硬约束共同成立）
        self.assertEqual(solution.total_deviation, 0)
        self.assertEqual(solution.achieved, problem.targets)
        # 独立校验通过
        self.assertEqual(validate_position_solution(problem, solution), [])

    def test_multi_position_qualification_parsing(self):
        # 多岗位资格（分隔符解析后的列表）允许承担任一具备资格的岗位
        quals = {
            1: ["电工"],
            2: ["班长", "电工"],  # 多岗位
            3: ["皮带"],
            4: ["皮带"],
        }
        problem, _ = make_problem(
            qualifications=quals,
            daily_required=3,
            position_requirements={"电工": 1, "皮带": 2},
        )
        solution = solve_position_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.daily_position_counts["电工"], [1] * 6)
        self.assertEqual(solution.daily_position_counts["皮带"], [2] * 6)
        for (eid, day), pos in solution.position_assignments.items():
            self.assertIn(pos, quals[eid])  # 只承担具备资格的岗位
        self.assertEqual(validate_position_solution(problem, solution), [])

    def test_requirement_zero_means_no_assignment(self):
        quals = {1: ["电工"], 2: ["电工"], 3: [], 4: [], 5: []}
        problem, _ = make_problem(
            qualifications=quals, daily_required=3, position_requirements={"电工": 0}
        )
        solution = solve_position_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.daily_position_counts["电工"], [0] * 6)
        self.assertEqual(solution.total_deviation, 0)

    def test_requirement_equals_qualified_count(self):
        # 2 名电工、要求 2 名 → 每天两人都承担电工
        quals = {1: ["电工"], 2: ["电工"]}
        problem, _ = make_problem(
            qualifications=quals, day_count=4, daily_required=2,
            position_requirements={"电工": 2},
        )
        solution = solve_position_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.daily_position_counts["电工"], [2] * 4)
        for day in range(4):
            self.assertTrue(all(((eid, day) in solution.assignments) for eid in (1, 2)))

    def test_requirement_exceeds_qualified_infeasible(self):
        quals = {1: ["电工"], 2: [], 3: []}
        problem, _ = make_problem(
            qualifications=quals, daily_required=3, position_requirements={"电工": 2}
        )
        solution = solve_position_assignment(problem)
        self.assertEqual(solution.status, "infeasible")
        self.assertIn("仅 1 人具备资格", solution.message)
        reasons = diagnose_position_infeasibility(problem)
        self.assertTrue(any("仅 1 人具备资格" in reason for reason in reasons))

    def test_position_total_exceeds_daily_infeasible(self):
        quals = {1: ["电工"], 2: ["电工"], 3: ["皮带"], 4: ["皮带"]}
        problem, _ = make_problem(
            qualifications=quals, daily_required=3,
            position_requirements={"电工": 2, "皮带": 2},
        )
        solution = solve_position_assignment(problem)
        self.assertEqual(solution.status, "infeasible")
        self.assertIn("超过每天上班人数", solution.message)

    def test_multi_position_competition_feasible(self):
        # 重叠资格人员可在两个岗位之间分配：每天电工 2、皮带 2、总人数 4
        quals = {1: ["电工"], 2: ["电工"], 3: ["皮带"], 4: ["皮带"], 5: ["班长", "电工"]}
        problem, _ = make_problem(
            qualifications=quals, daily_required=4,
            position_requirements={"电工": 2, "皮带": 2},
        )
        solution = solve_position_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.daily_counts, [4] * 6)
        self.assertEqual(solution.daily_position_counts["电工"], [2] * 6)
        self.assertEqual(solution.daily_position_counts["皮带"], [2] * 6)
        self.assertEqual(validate_position_solution(problem, solution), [])

    def test_multi_position_joint_conflict_infeasible(self):
        # 各岗位单独看资格均足够、总数不超每日人数，但联合分配存在 Hall 违例：
        # 电工×3 与 皮带×1 的岗位名额只能由 {1,2,3} 三人承担（4 个名额 vs 3 人），
        # 而职工 3 同时是唯一的皮工资格者 → 无解（求解器级不可行，报告人员竞争冲突）
        quals = {1: ["电工"], 2: ["电工"], 3: ["电工", "皮带"], 4: ["班长"], 5: ["班长"]}
        problem, _ = make_problem(
            qualifications=quals, day_count=3, daily_required=5,
            position_requirements={"电工": 3, "皮带": 1, "班长": 1},
        )
        # 前置检查不触发具体原因（各岗位资格够、合计不超每日人数），返回兜底原因
        reasons = diagnose_position_infeasibility(problem)
        self.assertTrue(any("人员竞争冲突" in reason for reason in reasons))
        solution = solve_position_assignment(problem)
        self.assertEqual(solution.status, "infeasible")
        self.assertIn("人员竞争冲突", solution.message)

    def test_deviation_greater_than_zero_with_constraints_held(self):
        # 岗位约束迫使电工资格人员每天上班，超出其目标班数 → 如实报告偏差，硬约束不放松
        quals = {1: ["电工"], 2: ["电工"]}
        problem, _ = make_problem(
            qualifications=quals, day_count=6, daily_required=2,
            position_requirements={"电工": 2},
            custom_targets={1: 2, 2: 2},  # 目标低于岗位需求造成的实际班数
        )
        solution = solve_position_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        # 硬约束仍严格成立
        self.assertEqual(solution.daily_counts, [2] * 6)
        self.assertEqual(solution.daily_position_counts["电工"], [2] * 6)
        # 实际班数均为 6，目标 2 → 偏差如实报告
        self.assertEqual(solution.achieved, {1: 6, 2: 6})
        self.assertEqual(solution.total_deviation, 8)
        self.assertEqual(solution.max_deviation, 4)
        self.assertIn("总偏差 8", solution.message)

    def test_validate_detects_corruption(self):
        quals = {1: ["电工"], 2: ["电工"], 3: [], 4: [], 5: []}
        problem, _ = make_problem(
            qualifications=quals, daily_required=3, position_requirements={"电工": 2}
        )
        solution = solve_position_assignment(problem)
        self.assertEqual(validate_position_solution(problem, solution), [])

        # 篡改：某天人数不满足
        corrupted = solve_position_assignment(problem)
        corrupted.daily_counts[0] += 1
        errors = validate_position_solution(problem, corrupted)
        self.assertTrue(any("第 1 天实际" in e for e in errors))

        # 篡改：无资格人员承担岗位
        corrupted2 = solve_position_assignment(problem)
        corrupted2.position_assignments[(4, 0)] = "电工"
        errors = validate_position_solution(problem, corrupted2)
        self.assertTrue(any("不具备岗位" in e for e in errors))

        # 篡改：跨班组/未知人员
        corrupted3 = solve_position_assignment(problem)
        corrupted3.position_assignments[(999, 0)] = "电工"
        corrupted3.assignments.add((999, 0))
        errors = validate_position_solution(problem, corrupted3)
        self.assertTrue(any("未知人员" in e for e in errors))

        # 篡改：偏差统计错误
        corrupted4 = solve_position_assignment(problem)
        corrupted4.total_deviation += 1
        errors = validate_position_solution(problem, corrupted4)
        self.assertTrue(any("总偏差计算错误" in e for e in errors))

    def test_no_positions_configured_equivalent_to_phase2(self):
        # 未配置岗位时行为与第二阶段一致：目标偏差 0
        quals = {i: [] for i in range(1, 6)}
        problem, allocation = make_problem(qualifications=quals, daily_required=3)
        solution = solve_position_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.total_deviation, 0)
        self.assertEqual(solution.achieved, problem.targets)
