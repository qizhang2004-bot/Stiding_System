# 排班算法引擎。
from .allocator import MODE_SURPLUS, MODE_TIGHT, ShiftAllocation, plan_shift_targets
from .daily_solver import DailyProblem, DailySolution, solve_daily_assignment
from .data import Assignment, EmployeeInfo, ScheduleProblem, ScheduleSolution, ShiftRequirement
from .diagnostics import diagnose
from .position_solver import (
    PositionProblem,
    PositionSolution,
    diagnose_position_infeasibility,
    solve_position_assignment,
    validate_position_solution,
)
from .rest_solver import (
    RestProblem,
    RestSolution,
    compute_rest_quality,
    diagnose_rest_infeasibility,
    solve_rest_assignment,
    validate_rest_solution,
)
from .solver import solve

__all__ = [
    "Assignment",
    "DailyProblem",
    "DailySolution",
    "EmployeeInfo",
    "MODE_SURPLUS",
    "MODE_TIGHT",
    "PositionProblem",
    "PositionSolution",
    "RestProblem",
    "RestSolution",
    "ScheduleProblem",
    "ScheduleSolution",
    "ShiftAllocation",
    "ShiftRequirement",
    "compute_rest_quality",
    "diagnose",
    "diagnose_position_infeasibility",
    "diagnose_rest_infeasibility",
    "plan_shift_targets",
    "solve",
    "solve_daily_assignment",
    "solve_position_assignment",
    "solve_rest_assignment",
    "validate_position_solution",
    "validate_rest_solution",
]
