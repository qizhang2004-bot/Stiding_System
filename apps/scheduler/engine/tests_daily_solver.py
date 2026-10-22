# 每日上班人数硬约束（第二阶段）求解测试。
from unittest import skipIf

from django.test import SimpleTestCase

from .allocator import plan_shift_targets
from .daily_solver import DailyProblem, solve_daily_assignment

try:
    import ortools  # noqa: F401

    HAS_ORTOOLS = True
except ImportError:
    HAS_ORTOOLS = False


def ids(count, start=1):
    return list(range(start, start + count))


@skipIf(not HAS_ORTOOLS, "未安装 ortools，跳过每日人数求解测试")
class DailySolverTests(SimpleTestCase):
    def _solve(self, total_days, daily_required, base_shifts, n_total, exempt_ids=None):
        exempt = exempt_ids or []
        normal = [i for i in ids(n_total) if i not in set(exempt)]
        allocation = plan_shift_targets(
            total_days=total_days, daily_required=daily_required,
            base_shifts=base_shifts, normal_ids=normal, exempt_ids=exempt,
        )
        problem = DailyProblem(
            employee_ids=ids(n_total),
            day_count=total_days,
            daily_required=daily_required,
            targets=allocation.per_person_targets,
        )
        return allocation, solve_daily_assignment(problem)

    def test_normal_surplus_mode_exact_daily_counts(self):
        # D=30, R=18, N=25 → 人多模式；目标与每日约束应同时成立（偏差 0）
        allocation, solution = self._solve(30, 18, 18, 25)
        self.assertTrue(allocation.feasible)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(len(solution.assignments), 30 * 18)
        # 每天恰好 18 人，不多不少
        self.assertEqual(solution.daily_counts, [18] * 30)
        # 每人每天至多一班：按人统计次数 ≤ 30，且总和守恒
        self.assertEqual(sum(solution.achieved.values()), 30 * 18)
        self.assertTrue(all(v <= 30 for v in solution.achieved.values()))
        # 目标全部达成
        self.assertEqual(solution.total_deviation, 0)
        self.assertEqual(solution.max_deviation, 0)
        self.assertEqual(solution.achieved, allocation.per_person_targets)

    def test_tight_mode_exact_daily_counts(self):
        # D=30, R=18, N=31、豁免1人 → 人少模式（普通30人目标18、豁免目标0）；偏差 0
        allocation, solution = self._solve(30, 18, 18, 31, exempt_ids=[31])
        self.assertEqual(allocation.mode, "tight")
        self.assertTrue(allocation.feasible)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.daily_counts, [18] * 30)
        self.assertEqual(solution.total_deviation, 0)
        self.assertEqual(solution.achieved, allocation.per_person_targets)

    def test_boundary_required_equals_team(self):
        # R == N：每人每天都必须上班，目标=周期天数
        allocation, solution = self._solve(30, 5, 18, 5)
        self.assertTrue(allocation.feasible)
        self.assertEqual(solution.daily_counts, [5] * 30)
        self.assertEqual(solution.total_deviation, 0)
        self.assertEqual(set(solution.achieved.values()), {30})

    def test_small_team_boundary(self):
        # R=1, N=5（全部豁免）：每天恰好 1 人，剩余需求 10 班公平分配给豁免人员
        allocation, solution = self._solve(10, 1, 18, 5, exempt_ids=ids(5))
        self.assertTrue(allocation.feasible)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.daily_counts, [1] * 10)
        self.assertEqual(len(solution.assignments), 10)
        self.assertEqual(solution.total_deviation, 0)

    def test_exempt_surplus_balanced(self):
        # 豁免 3 人、普通 22 人：豁免优先承担剩余名额，班数差目标达成
        exempt = ids(3, start=1001)
        normal = [i for i in ids(25) if i not in set(exempt)]
        allocation = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=normal, exempt_ids=exempt,
        )
        problem = DailyProblem(
            employee_ids=normal + exempt,
            day_count=30,
            daily_required=18,
            targets=allocation.per_person_targets,
        )
        solution = solve_daily_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.daily_counts, [18] * 30)
        self.assertEqual(solution.total_deviation, 0)
        self.assertEqual(solution.achieved, allocation.per_person_targets)

    def test_insufficient_personnel_infeasible(self):
        # R=18 > N=5：不可行，必须明确报告原因，不得降低 R
        _, solution = self._solve(30, 18, 18, 5)
        self.assertEqual(solution.status, "infeasible")
        self.assertIn("无法满足每日人数", solution.message)

    def test_empty_team_infeasible(self):
        problem = DailyProblem(employee_ids=[], day_count=30, daily_required=18, targets={})
        solution = solve_daily_assignment(problem)
        self.assertEqual(solution.status, "infeasible")
        self.assertIn("没有有效职工", solution.message)

    def test_daily_required_never_auto_adjusted(self):
        # 不可行时不修改 R：返回结果中的要求仍是原值（在 message 中体现）
        _, solution = self._solve(30, 18, 18, 5)
        self.assertEqual(solution.status, "infeasible")
        self.assertIn("18 人", solution.message)

    def test_each_person_at_most_one_shift_per_day(self):
        # 每个 (职工, 天) 组合最多出现一次（单变量天然保证，仍显式验证）
        _, solution = self._solve(30, 18, 18, 25)
        per_day_people = {}
        for emp_id, day in solution.assignments:
            per_day_people.setdefault(day, set()).add(emp_id)
        self.assertTrue(all(len(people) == 18 for people in per_day_people.values()))
        self.assertEqual(len(solution.assignments), len({a for a in solution.assignments}))
