# 排班求解（第二阶段）：每日上班人数硬约束。
# 复用 OR-Tools CP-SAT；本阶段仅包含：
#   H1 每天实际安排人数必须严格等于 daily_required（不多不少）
#   H2 每人每天最多安排一个班次（每个 x[i,d] 为 0/1 单变量，天然满足）
#   H3 不跨班组调用人员（人员列表由适配层按班组组装）
# 目标：最小化每人实际班数与第一阶段分配目标的绝对偏差之和
# （第一阶段目标是目标值/基线，不与每日人数约束冲突时应达成偏差 0）。
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class DailyProblem:
    # 每日人数求解输入
    employee_ids: List[int]       # 参与排班的职工主键（必须全部来自同一班组）
    day_count: int                # D：周期天数
    daily_required: int           # R：每天要求上班人数（不得自动修改）
    targets: Dict[int, int]       # 每人目标班数（第一阶段分配结果）


@dataclass
class DailySolution:
    # 每日人数求解结果
    status: str                            # optimal / feasible / infeasible
    assignments: Set[Tuple[int, int]] = field(default_factory=set)  # (职工ID, 天序号)
    achieved: Dict[int, int] = field(default_factory=dict)          # 每人实际班数
    daily_counts: List[int] = field(default_factory=list)           # 每天实际人数
    max_deviation: int = 0
    total_deviation: int = 0
    objective: Optional[float] = None
    message: str = ""


def solve_daily_assignment(problem: DailyProblem, *, time_limit_seconds: int = 60) -> DailySolution:
    # 求解每日上班人数安排。
    # 不可行时明确报告原因，绝不通过调整 R 制造可行解。
    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 ortools 依赖，请先执行：pip install ortools") from exc

    n = len(problem.employee_ids)
    d = problem.day_count
    r = problem.daily_required

    if n == 0:
        return DailySolution(status="infeasible", message="班组没有有效职工，无法排班。")
    if d <= 0:
        raise ValueError("排班周期总天数必须为正。")
    if r < 0:
        raise ValueError("每天要求上班人数不能为负。")
    if r > n:
        return DailySolution(
            status="infeasible",
            message=f"每天要求上班 {r} 人，但班组有效职工仅 {n} 人，无法满足每日人数要求。",
        )

    model = cp_model.CpModel()
    x = {}
    for i in range(n):
        for day in range(d):
            x[i, day] = model.NewBoolVar(f"x_{i}_{day}")

    # H1：每天恰好 R 人上班（硬约束，不多不少）
    for day in range(d):
        model.Add(sum(x[i, day] for i in range(n)) == r)

    # 目标：每人实际班数与分配目标的绝对偏差之和最小
    dev_terms = []
    for i in range(n):
        emp_id = problem.employee_ids[i]
        target = problem.targets.get(emp_id, 0)
        total = sum(x[i, day] for day in range(d))
        diff = model.NewIntVar(-d, d, f"diff_{i}")
        model.Add(diff == total - target)
        abs_diff = model.NewIntVar(0, d, f"abs_diff_{i}")
        model.AddAbsEquality(abs_diff, diff)
        dev_terms.append(abs_diff)
    model.Minimize(sum(dev_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return DailySolution(
            status="infeasible",
            message=(
                f"每日人数约束无可行解：每天要求 {r} 人，班组有效职工 {n} 人，周期 {d} 天。"
                "请检查每日人数配置与班组人员数量。"
            ),
        )

    assignments = set()
    achieved = {}
    daily_counts = [0] * d
    for i in range(n):
        emp_id = problem.employee_ids[i]
        count = 0
        for day in range(d):
            if solver.Value(x[i, day]):
                assignments.add((emp_id, day))
                count += 1
                daily_counts[day] += 1
        achieved[emp_id] = count

    # 防御性复核：求解结果必须逐日严格等于 R（不应依赖求解器状态码）
    for day, count in enumerate(daily_counts):
        if count != r:
            return DailySolution(
                status="infeasible",
                message=f"内部校验失败：第 {day + 1} 天实际安排 {count} 人，与要求 {r} 人不符。",
            )

    deviations = [
        abs(achieved.get(emp_id, 0) - problem.targets.get(emp_id, 0))
        for emp_id in problem.employee_ids
    ]
    return DailySolution(
        status="optimal" if status == cp_model.OPTIMAL else "feasible",
        assignments=assignments,
        achieved=achieved,
        daily_counts=daily_counts,
        max_deviation=max(deviations, default=0),
        total_deviation=sum(deviations),
        objective=solver.ObjectiveValue(),
        message=(
            f"每日人数约束满足：{d} 天每天 {r} 人；"
            f"目标班数最大偏差 {max(deviations, default=0)}，总偏差 {sum(deviations)}。"
        ),
    )
