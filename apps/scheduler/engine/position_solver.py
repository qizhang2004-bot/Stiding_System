# 排班求解（第三阶段）：特殊岗位人数约束（与每日人数约束联合求解）。
# 复用 OR-Tools CP-SAT。硬约束：
#   H1 每天实际安排人数严格等于 daily_required（不多不少）
#   H2 每人每天最多安排一个班次（x[i,d] 为 0/1 单变量）
#   H3 人员限定在当前班组有效职工范围（由适配层组装，求解前再次校验）
#   P1 每天每个配置岗位的实际安排人数严格等于配置人数（恰好，非最低）
#   P2 职工只能承担自己具备资格的岗位（资格由 parse_positions 解析）
#   P3 每人每天最多承担一个实际岗位（y[i,d,p] 求和 ≤ 1）
#   P4 岗位安排必须落在上班日期上（y ⇒ x），岗位人员计入当日总人数，不重复计数
# 目标：最小化每人实际班数与第一阶段分配目标的绝对偏差之和。
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

STATUS_INVALID = "invalid"  # 独立校验失败


@dataclass
class PositionProblem:
    # 岗位人数求解输入
    employee_ids: List[int]                       # 班组在职职工主键
    qualifications: Dict[int, List[str]]          # 职工ID -> 岗位资格列表（去重后）
    day_count: int                                # D：周期天数
    daily_required: int                           # R：每天要求上班人数
    position_requirements: Dict[str, int]         # 岗位 -> 每天要求人数（精确）
    targets: Dict[int, int]                       # 每人目标班数（第一阶段分配结果）


@dataclass
class PositionSolution:
    # 岗位人数求解结果
    status: str = STATUS_INVALID                  # optimal / feasible / infeasible / invalid
    assignments: Set[Tuple[int, int]] = field(default_factory=set)          # (职工ID, 天序号)
    position_assignments: Dict[Tuple[int, int], str] = field(default_factory=dict)  # (职工ID, 天序号) -> 岗位
    achieved: Dict[int, int] = field(default_factory=dict)                  # 每人实际班数
    daily_counts: List[int] = field(default_factory=list)                   # 每天实际人数
    daily_position_counts: Dict[str, List[int]] = field(default_factory=dict)  # 岗位 -> 每天人数
    max_deviation: int = 0
    total_deviation: int = 0
    objective: Optional[float] = None
    message: str = ""


def diagnose_position_infeasibility(problem: PositionProblem) -> List[str]:
    # 对岗位/每日人数不可行给出明确原因（不修改任何配置）
    reasons = []
    n = len(problem.employee_ids)
    r = problem.daily_required
    if n == 0:
        reasons.append("班组没有有效职工，无法排班。")
        return reasons
    if r > n:
        reasons.append(f"每天要求上班 {r} 人，但班组有效职工仅 {n} 人，无法满足每日人数要求。")
    total_req = 0
    for pos, req in problem.position_requirements.items():
        qualified = sum(
            1 for eid in problem.employee_ids if pos in problem.qualifications.get(eid, [])
        )
        total_req += req
        if req > qualified:
            reasons.append(
                f"岗位「{pos}」每天要求 {req} 人，但仅 {qualified} 人具备资格，无法满足岗位人数要求。"
            )
    if total_req > r:
        reasons.append(
            f"各岗位要求人数合计 {total_req} 人，超过每天上班人数 {r} 人，无法同时满足。"
        )
    if not reasons:
        reasons.append(
            "岗位之间存在人员竞争冲突：具备多岗位资格的人员数量不足以同时满足各岗位的精确人数要求。"
        )
    return reasons


def validate_position_solution(problem: PositionProblem, solution: PositionSolution) -> List[str]:
    # 独立校验：不依赖求解器状态，逐条检查硬约束与统计正确性。返回错误列表（空列表表示通过）。
    errors = []
    r = problem.daily_required
    d = problem.day_count

    # 1. 每天总人数严格等于配置人数
    if len(solution.daily_counts) != d:
        errors.append(f"每日人数统计长度异常：{len(solution.daily_counts)} ≠ {d}。")
    else:
        for day, count in enumerate(solution.daily_counts):
            if count != r:
                errors.append(f"第 {day + 1} 天实际 {count} 人，要求 {r} 人。")

    # 2. 每天各岗位实际人数严格等于配置人数
    for pos, req in problem.position_requirements.items():
        counts = solution.daily_position_counts.get(pos)
        if counts is None:
            errors.append(f"岗位「{pos}」缺少每日人数统计。")
            continue
        for day, count in enumerate(counts):
            if count != req:
                errors.append(f"第 {day + 1} 天岗位「{pos}」实际 {count} 人，要求 {req} 人。")

    # 3/4/5. 每人每天最多一个岗位、岗位须具备资格、人员范围与上班一致性
    seen = set()
    for (eid, day), pos in solution.position_assignments.items():
        if eid not in problem.employee_ids:
            errors.append(f"出现跨班组/未知人员：{eid}。")
        if (eid, day) not in solution.assignments:
            errors.append(f"职工 {eid} 在第 {day + 1} 天承担岗位「{pos}」，但当天未安排上班。")
        if pos not in problem.qualifications.get(eid, []):
            errors.append(f"职工 {eid} 不具备岗位「{pos}」的资格。")
        key = (eid, day)
        if key in seen:
            errors.append(f"职工 {eid} 在第 {day + 1} 天承担了多个岗位。")
        seen.add(key)

    # 6. 实际班数与偏差计算正确
    recomputed = {eid: 0 for eid in problem.employee_ids}
    for eid, _ in solution.assignments:
        recomputed[eid] = recomputed.get(eid, 0) + 1
    if recomputed != solution.achieved:
        errors.append("实际班数统计与排班明细不一致。")
    total_deviation = sum(
        abs(recomputed.get(eid, 0) - problem.targets.get(eid, 0))
        for eid in problem.employee_ids
    )
    if total_deviation != solution.total_deviation:
        errors.append(
            f"总偏差计算错误：报告 {solution.total_deviation}，实际 {total_deviation}。"
        )
    return errors


def _build_base_model(
    employee_ids: List[int],
    qualifications: Dict[int, List[str]],
    day_count: int,
    daily_required: int,
    position_requirements: Dict[str, int],
    targets: Dict[int, int],
):
    # 构建前三阶段共享的基础模型：x（上班）/y（实际岗位）变量、H1、P1-P4 硬约束
    # 以及每人班数偏差项。休息规则求解器（rest_solver）复用本函数，保证约束一致。
    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 ortools 依赖，请先执行：pip install ortools") from exc

    n = len(employee_ids)
    d = day_count
    r = daily_required
    # 仅要求人数 > 0 的岗位参与排班（0 表示当天不要求该岗位，不视为正式岗位）
    positions = [
        name
        for name, count in position_requirements.items()
        if isinstance(count, int) and not isinstance(count, bool) and count > 0
    ]

    model = cp_model.CpModel()
    x = {}
    for i in range(n):
        for day in range(d):
            x[i, day] = model.NewBoolVar(f"x_{i}_{day}")

    # 仅创建具备资格人员的岗位变量（P2 由变量创建范围保证）
    y = {}
    for i in range(n):
        eid = employee_ids[i]
        quals = set(qualifications.get(eid, []))
        for pos in positions:
            if pos not in quals:
                continue
            for day in range(d):
                y[i, day, pos] = model.NewBoolVar(f"y_{i}_{day}_{pos}")

    # H1：每天恰好 R 人
    for day in range(d):
        model.Add(sum(x[i, day] for i in range(n)) == r)

    # P1：每天每个岗位恰好配置人数
    for pos in positions:
        req = position_requirements.get(pos, 0)
        for day in range(d):
            model.Add(
                sum(y.get((i, day, pos), 0) for i in range(n)) == req
            )

    # P3/P4：每人每天最多一个岗位；岗位安排必须落在上班日期
    for i in range(n):
        for day in range(d):
            pos_vars = [y.get((i, day, pos)) for pos in positions if (i, day, pos) in y]
            if pos_vars:
                model.Add(sum(pos_vars) <= 1)
                model.Add(sum(pos_vars) <= x[i, day])

    # 班数偏差项（目标：偏差之和最小）
    dev_terms = []
    for i in range(n):
        eid = employee_ids[i]
        target = targets.get(eid, 0)
        total = sum(x[i, day] for day in range(d))
        diff = model.NewIntVar(-d, d, f"diff_{i}")
        model.Add(diff == total - target)
        abs_diff = model.NewIntVar(0, d, f"abs_diff_{i}")
        model.AddAbsEquality(abs_diff, diff)
        dev_terms.append(abs_diff)
    return model, x, y, dev_terms


def solve_position_assignment(
    problem: PositionProblem, *, time_limit_seconds: int = 60
) -> PositionSolution:
    # 每日人数 + 特殊岗位人数联合求解。
    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 ortools 依赖，请先执行：pip install ortools") from exc

    n = len(problem.employee_ids)
    d = problem.day_count
    r = problem.daily_required

    if n == 0 or r > n or d <= 0 or r < 0:
        return PositionSolution(
            status="infeasible",
            message="；".join(diagnose_position_infeasibility(problem)),
        )

    model, x, y, dev_terms = _build_base_model(
        employee_ids=problem.employee_ids,
        qualifications=problem.qualifications,
        day_count=problem.day_count,
        daily_required=problem.daily_required,
        position_requirements=problem.position_requirements,
        targets=problem.targets,
    )

    model.Minimize(sum(dev_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    status = solver.Solve(model)

    if status == cp_model.UNKNOWN:
        return PositionSolution(
            status="unknown",
            message="求解超时：未能在限定时间内证明可行或不可行。",
        )
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return PositionSolution(
            status="infeasible",
            message="；".join(diagnose_position_infeasibility(problem)),
        )

    positions = list(problem.position_requirements.keys())
    assignments = set()
    position_assignments = {}
    achieved = {}
    daily_counts = [0] * d
    daily_position_counts = {pos: [0] * d for pos in positions}
    for i in range(n):
        eid = problem.employee_ids[i]
        count = 0
        for day in range(d):
            if solver.Value(x[i, day]):
                assignments.add((eid, day))
                count += 1
                daily_counts[day] += 1
            for pos in positions:
                if (i, day, pos) in y and solver.Value(y[i, day, pos]):
                    position_assignments[(eid, day)] = pos
                    daily_position_counts[pos][day] += 1
        achieved[eid] = count

    deviations = [
        abs(achieved.get(eid, 0) - problem.targets.get(eid, 0))
        for eid in problem.employee_ids
    ]
    solution = PositionSolution(
        status="optimal" if status == cp_model.OPTIMAL else "feasible",
        assignments=assignments,
        position_assignments=position_assignments,
        achieved=achieved,
        daily_counts=daily_counts,
        daily_position_counts=daily_position_counts,
        max_deviation=max(deviations, default=0),
        total_deviation=sum(deviations),
        objective=solver.ObjectiveValue(),
        message=(
            f"每日人数与岗位人数约束满足；目标班数最大偏差 {max(deviations, default=0)}，"
            f"总偏差 {sum(deviations)}。"
        ),
    )

    # 独立校验：失败时不得返回成功结果
    errors = validate_position_solution(problem, solution)
    if errors:
        return PositionSolution(
            status=STATUS_INVALID,
            message="独立校验失败：" + "；".join(errors),
        )
    return solution
