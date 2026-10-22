# 排班算法测试。
import datetime
from unittest import skipIf

from django.test import SimpleTestCase

from .data import EmployeeInfo, ScheduleProblem, ShiftRequirement
from .solver import solve

try:
    import ortools  # noqa: F401

    HAS_ORTOOLS = True
except ImportError:
    HAS_ORTOOLS = False


@skipIf(not HAS_ORTOOLS, "未安装 ortools，跳过引擎求解测试")
class SolverTests(SimpleTestCase):
    def test_minimal_problem(self):
        problem = ScheduleProblem(
            employees=[EmployeeInfo(id=1, name="张三"), EmployeeInfo(id=2, name="李四")],
            dates=[datetime.date(2025, 1, 1)],
            shifts=["白班", "夜班"],
            requirements={
                (datetime.date(2025, 1, 1), "白班"): [
                    ShiftRequirement(shift="白班", min_count=1, max_count=2)
                ],
                (datetime.date(2025, 1, 1), "夜班"): [
                    ShiftRequirement(shift="夜班", min_count=1, max_count=2)
                ],
            },
            unavailability=set(),
            max_night_shifts={},
        )
        solution = solve(problem)
        self.assertIn(solution.status, ("optimal", "feasible"))
        self.assertEqual(len(solution.assignments), 2)
