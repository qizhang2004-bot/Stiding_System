# 排班求解（第四阶段）：休息规则约束。
# 在前三阶段基础模型（position_solver._build_base_model）之上增量添加：
#   R1 硬约束：禁止孤立工作日（休—上—休）→ x[d] <= x[d-1] + x[d+1]
#   R2 硬约束：禁止单日休息（上—休—上）→ x[d-1] + x[d+1] <= 1 + x[d]
#   周期首尾边界：结合相邻周期真实排班数据（prev_work/next_work）校验；
#     缺少相邻周期数据时不虚构边界状态，并在结果中说明边界限制。
#   R3 软性优化：每个10天滑动窗口尽可能包含连续2至3天休息
#     （优先改善完全没有连续2天休息的窗口，其次争取连续3天休息）。
# 目标优先级（词典序优化，不使用任意权重）：
#   硬约束（每日人数/岗位/资格）→ 硬性休息规则 → 班数目标偏差最小 → 休息优化。
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .position_solver import (
    PositionProblem,
    _build_base_model,
    diagnose_position_infeasibility,
    validate_position_solution,
)

WINDOW_SIZE = 10

logger = logging.getLogger(__name__)


@dataclass
class RestProblem:
    # 休息规则求解输入（在岗位求解输入基础上增加相邻周期边界数据）
    employee_ids: List[int]
    qualifications: Dict[int, List[str]]
    day_count: int
    daily_required: int
    position_requirements: Dict[str, int]
    targets: Dict[int, int]
    prev_work: Dict[int, bool] = field(default_factory=dict)  # 周期前一天是否上班（真实数据）
    next_work: Dict[int, bool] = field(default_factory=dict)  # 周期后一天是否上班（真实数据）
    # 连续休息上下限（来自 ShiftConfig.min/max_consecutive_rest_days，硬约束）
    min_consecutive_rest_days: int = 2
    max_consecutive_rest_days: int = 4
    # 普通人员最低班数硬下限：{职工ID: 最低班数}；豁免人员不设下限
    minimum_shifts: Dict[int, int] = field(default_factory=dict)
    # 豁免人员：不设最低班数，且不受任何休息规则（孤立工作日/单日休息/连续休息上下限）约束
    exempt_employee_ids: Set[int] = field(default_factory=set)


def minimal_required_slots(problem: "RestProblem") -> int:
    # 容量下界：仅“普通人员最低班数合计”。
    # 豁免人员不受休息规则约束，因此不会被“连续休息上限”推导出最少出勤（其下限为 0）。
    # 仅作为可行性下界判断（真实可行域可能更小）。
    return sum(int(v) for v in problem.minimum_shifts.values())


def compute_rest_runs(work: List[int], prev=None, next=None) -> List[int]:
    # 计算连续休息区间长度列表（含相邻周期边界拼接）；prev/next：True 上班、False 休息、None 未知
    runs = []
    current = 1 if prev is False else 0
    for value in work:
        if value == 0:
            current += 1
        else:
            if current:
                runs.append(current)
            current = 0
    if current:
        if next is False:
            current += 1
        runs.append(current)
    return runs


def determinable_rest_runs(work: List[int], prev=None, next=None) -> List[int]:
    # 仅返回可以判定的连续休息区间长度：与未知相邻周期相接的首/尾休息段不参与判定
    # （无真实边界数据时不虚构边界状态）。已知 prev/next 为休息时，区间长度按拼接计算。
    d = len(work)
    runs = []
    day = 0
    while day < d:
        if work[day] == 1:
            day += 1
            continue
        start = day
        while day < d and work[day] == 0:
            day += 1
        end = day - 1
        length = end - start + 1
        if start == 0 and prev is None:
            continue
        if end == d - 1 and next is None:
            continue
        if start == 0 and prev is False:
            length += 1
        if end == d - 1 and next is False:
            length += 1
        runs.append(length)
    return runs


def compute_work_runs(work: List[int], prev=None, next=None) -> List[int]:
    # 计算连续工作区间长度列表（含相邻周期边界拼接），用于结果核对与报告
    runs = []
    current = 1 if prev is True else 0
    for value in work:
        if value == 1:
            current += 1
        else:
            if current:
                runs.append(current)
            current = 0
    if current:
        if next is True:
            current += 1
        runs.append(current)
    return runs


@dataclass
class RestSolution:
    # 休息规则求解结果
    status: str = "invalid"
    assignments: Set[Tuple[int, int]] = field(default_factory=set)
    position_assignments: Dict[Tuple[int, int], str] = field(default_factory=dict)
    achieved: Dict[int, int] = field(default_factory=dict)
    daily_counts: List[int] = field(default_factory=list)
    daily_position_counts: Dict[str, List[int]] = field(default_factory=dict)
    max_deviation: int = 0
    total_deviation: int = 0
    max_rest_run: int = 0           # 全员最长连续休息天数（含相邻周期边界）
    max_work_run: int = 0           # 全员最长连续工作天数（含相邻周期边界）
    windows_with_2_block: int = 0   # 含连续2天休息的(职工,窗口)数
    windows_with_3_block: int = 0   # 含连续3天休息的(职工,窗口)数
    total_windows: int = 0          # (职工,窗口)总数（周期不足10天时为0）
    objective: Optional[float] = None
    message: str = ""
    boundary_note: str = ""


def _to_position_problem(problem: RestProblem) -> PositionProblem:
    return PositionProblem(
        employee_ids=problem.employee_ids,
        qualifications=problem.qualifications,
        day_count=problem.day_count,
        daily_required=problem.daily_required,
        position_requirements=problem.position_requirements,
        targets=problem.targets,
    )


def compute_rest_quality(employee_ids: List[int], day_count: int, assignments: Set[Tuple[int, int]]) -> Tuple[int, int, int]:
    # 纯函数：统计含连续2/3天休息的10天窗口数（供求解结果统计与独立校验共用）。
    if day_count < WINDOW_SIZE:
        return 0, 0, 0
    count2 = count3 = 0
    total = 0
    for eid in employee_ids:
        work = [1 if (eid, day) in assignments else 0 for day in range(day_count)]
        for start in range(day_count - WINDOW_SIZE + 1):
            total += 1
            window = work[start:start + WINDOW_SIZE]
            has2 = any(window[j] == 0 and window[j + 1] == 0 for j in range(WINDOW_SIZE - 1))
            has3 = any(
                window[j] == 0 and window[j + 1] == 0 and window[j + 2] == 0
                for j in range(WINDOW_SIZE - 2)
            )
            if has2:
                count2 += 1
            if has3:
                count3 += 1
    return count2, count3, total


def _add_rest_hard_constraints(model, x, problem: RestProblem):
    # R1/R2：内部日期完整应用；首尾日仅当提供相邻周期真实数据时应用
    n = len(problem.employee_ids)
    d = problem.day_count
    for i in range(n):
        eid = problem.employee_ids[i]
        if eid in problem.exempt_employee_ids:
            continue  # 豁免人员不受“禁止孤立工作日/禁止单日休息”约束
        prev = problem.prev_work.get(eid)
        nxt = problem.next_work.get(eid)
        for day in range(d):
            left = x[i, day - 1] if day > 0 else (1 if prev else 0 if prev is not None else None)
            right = x[i, day + 1] if day < d - 1 else (1 if nxt else 0 if nxt is not None else None)
            if left is None or right is None:
                continue  # 缺少相邻周期数据：不虚构边界状态，跳过该日边界规则
            # R1 禁止孤立工作日：x[d] <= left + right
            model.Add(x[i, day] <= left + right)
            # R2 禁止单日休息：left + right <= 1 + x[d]
            model.Add(left + right <= 1 + x[i, day])


def _add_consecutive_rest_hard_constraints(model, x, problem: RestProblem):
    # R4 硬约束：连续休息区间不得超过 max_consecutive_rest_days；
    # R5 硬约束：连续休息区间不得短于 min_consecutive_rest_days（min=2 时等价于 R2）。
    # 周期首尾结合相邻周期真实数据；无边界数据时按周期内可判定的范围约束（不虚构边界）。
    n = len(problem.employee_ids)
    d = problem.day_count
    max_rest = problem.max_consecutive_rest_days or 0
    min_rest = max(problem.min_consecutive_rest_days or 1, 1)
    for i in range(n):
        eid = problem.employee_ids[i]
        if eid in problem.exempt_employee_ids:
            continue  # 豁免人员不受连续休息上下限约束
        prev = problem.prev_work.get(eid)
        nxt = problem.next_work.get(eid)

        # R4：任意 max_rest+1 个连续日期至少 1 天上班（≤ max_rest 天连续休息）
        if 0 < max_rest < d:
            for start in range(0, d - max_rest):
                model.Add(
                    sum(x[i, day] for day in range(start, start + max_rest + 1)) >= 1
                )
            # 边界：周期前一日休息时，周期开头 max_rest 天不能全部休息
            if prev is False:
                model.Add(sum(x[i, day] for day in range(0, min(max_rest, d))) >= 1)
            # 边界：周期后一日休息时，周期末尾 max_rest 天不能全部休息
            if nxt is False:
                model.Add(
                    sum(x[i, day] for day in range(d - min(max_rest, d), d)) >= 1
                )

        # R5：禁止长度 < min_rest 的休息段（两侧均为上班，含边界已知数据）
        for length in range(2, min_rest):
            for start in range(0, d - length + 1):
                left = x[i, start - 1] if start > 0 else (
                    1 if prev else 0 if prev is not None else None
                )
                right = x[i, start + length] if start + length < d else (
                    1 if nxt else 0 if nxt is not None else None
                )
                if left is None or right is None:
                    continue
                rest_in_run = sum(1 - x[i, day] for day in range(start, start + length))
                model.Add(rest_in_run - (1 - left) - (1 - right) <= length - 1)


def _add_rest_quality_vars(model, x, problem: RestProblem):
    # R3 软目标变量：has2[i,s]/has3[i,s] 表示职工 i 的第 s 个10天窗口含连续2/3天休息
    n = len(problem.employee_ids)
    d = problem.day_count
    has2_vars = []
    has3_vars = []
    if d < WINDOW_SIZE:
        return has2_vars, has3_vars
    for i in range(n):
        if problem.employee_ids[i] in problem.exempt_employee_ids:
            continue  # 豁免人员不参与休息质量优化
        r2 = {}
        for j in range(d - 1):
            r2[j] = model.NewBoolVar(f"r2_{i}_{j}")
            model.Add(r2[j] == 1).OnlyEnforceIf([x[i, j].Not(), x[i, j + 1].Not()])
            model.Add(r2[j] == 0).OnlyEnforceIf([x[i, j]])
            model.Add(r2[j] == 0).OnlyEnforceIf([x[i, j + 1]])
        r3 = {}
        for j in range(d - 2):
            r3[j] = model.NewBoolVar(f"r3_{i}_{j}")
            model.Add(r3[j] == 1).OnlyEnforceIf(
                [x[i, j].Not(), x[i, j + 1].Not(), x[i, j + 2].Not()]
            )
            model.Add(r3[j] == 0).OnlyEnforceIf([x[i, j]])
            model.Add(r3[j] == 0).OnlyEnforceIf([x[i, j + 1]])
            model.Add(r3[j] == 0).OnlyEnforceIf([x[i, j + 2]])
        for start in range(d - WINDOW_SIZE + 1):
            h2 = model.NewBoolVar(f"h2_{i}_{start}")
            model.Add(h2 <= sum(r2[j] for j in range(start, start + WINDOW_SIZE - 1)))
            for j in range(start, start + WINDOW_SIZE - 1):
                model.Add(h2 >= r2[j])
            h3 = model.NewBoolVar(f"h3_{i}_{start}")
            model.Add(h3 <= sum(r3[j] for j in range(start, start + WINDOW_SIZE - 2)))
            for j in range(start, start + WINDOW_SIZE - 2):
                model.Add(h3 >= r3[j])
            has2_vars.append(h2)
            has3_vars.append(h3)
    return has2_vars, has3_vars


def _solve_lexicographic(
    model,
    objectives,
    time_limit_seconds: int,
    *,
    hint_vars=None,
    objective_caps=None,
    soft_stage_seconds: Optional[float] = None,
    label="排班求解",
):
    # 词典序优化：逐级求解并固定上一级最优值，避免任意权重让低优先级目标压过高优先级。
    # objectives: [(表达式, minimize: bool), ...]
    #
    # 性能要点（不改变任何约束与目标优先级）：
    #   1) 跨阶段热启动：把上一级的最优解作为下一级的提示（hint），避免每级都从零搜索；
    #      实测把“3 连休窗口”这一级的 30s 搜索降到 0.1s 且结果相同。
    #   2) 已知上/下界提前判定：目标值的理论上限/下限已知时（如窗口总数），达到即视为最优，
    #      不再为“证明”而耗尽时限。
    #   3) 全局时间预算：time_limit_seconds 是整条词典序链的总预算，避免逐级各自计时
    #      导致总耗时成倍放大；后续软目标预算耗尽时按 soft_incomplete 处理（已有可行解）。
    #   3b) 软目标限时：soft_stage_seconds 只限制“首级之后”的软性优化目标（如 3 连休窗口数）。
    #      硬约束、班数目标（首级）不受影响，仍按完整预算求解；软目标限时只影响“是否证明最优”，
    #      不影响可行性，并在结果中如实注明，绝不放宽任何硬约束、也不误报无解。
    #   4) 分阶段耗时日志：区分各级求解耗时与状态，便于排查慢点。
    #
    # 返回 (status, solver, objective_value, soft_incomplete)：
    #   soft_incomplete=True 表示首级目标已达最优、但后续软目标在时限内未证明最优
    #   （此时已有可行解，不判不可行，仅在结果中注明）。
    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 ortools 依赖，请先执行：pip install ortools") from exc
    solver = cp_model.CpSolver()
    caps = list(objective_caps or [])
    hint_vars = list(hint_vars or [])
    hints = []
    remaining = float(time_limit_seconds)
    started = time.monotonic()
    last_status = None
    last_value = None
    soft_not_proven = []
    for index, (obj_expr, minimize) in enumerate(objectives):
        if index > 0 and soft_stage_seconds:
            budget = max(0.5, min(remaining, float(soft_stage_seconds)))
        else:
            budget = max(0.5, remaining)
        solver.parameters.max_time_in_seconds = budget
        if hints:
            model.ClearHints()
            for var, value in hints:
                model.AddHint(var, value)
        if minimize:
            model.Minimize(obj_expr)
        else:
            model.Maximize(obj_expr)
        stage_started = time.monotonic()
        status = solver.Solve(model)
        stage_elapsed = time.monotonic() - stage_started
        remaining = max(0.0, remaining - stage_elapsed)
        cap = caps[index] if index < len(caps) else None
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            value = solver.ObjectiveValue()
            if cap is not None:
                reached = value <= cap if minimize else value >= cap
                if reached and status != cp_model.OPTIMAL:
                    # 已达理论最优界：无需继续搜索，直接按最优处理
                    status = cp_model.OPTIMAL
            logger.info(
                "%s 阶段 %d/%d 用时=%.2fs 累计=%.2fs 预算=%.2fs 状态=%s 目标值=%.0f%s",
                label, index + 1, len(objectives), stage_elapsed,
                time.monotonic() - started, budget, solver.StatusName(status), value,
                "（达到理论界）" if cap is not None and (value <= cap if minimize else value >= cap) else "",
            )
            if hint_vars:
                hints = [(var, solver.Value(var)) for var in hint_vars]
        else:
            logger.warning(
                "%s 阶段 %d/%d 用时=%.2fs 状态=%s（预算 %.2fs）",
                label, index + 1, len(objectives), stage_elapsed,
                solver.StatusName(status), budget,
            )
        last_status = status
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if index > 0 and status == cp_model.UNKNOWN:
                return status, solver, last_value, True
            if index > 0 and status == cp_model.FEASIBLE:
                soft_not_proven.append(index)
            return status, solver, None, False
        last_value = solver.ObjectiveValue()
        if index > 0 and status == cp_model.FEASIBLE:
            soft_not_proven.append(index)
        bound = int(round(last_value))
        if minimize:
            model.Add(obj_expr <= bound)
        else:
            model.Add(obj_expr >= bound)
    if soft_not_proven:
        logger.info(
            "%s 软目标阶段 %s 在 %.1fs 限时内达到可行值但未证明最优（不影响可行性与硬约束）。",
            label, [i + 1 for i in soft_not_proven], float(soft_stage_seconds or 0),
        )
    return last_status, solver, last_value, False


def validate_rest_solution(problem: RestProblem, solution: RestSolution) -> List[str]:
    # 独立校验：前三阶段全部检查 + 休息规则检查 + 休息质量指标统计检查。
    # 软目标（10天窗口）未全部满足不判定排班无效，仅统计是否正确。
    base_problem = _to_position_problem(problem)
    errors = validate_position_solution(base_problem, solution)

    n = len(problem.employee_ids)
    d = problem.day_count
    for i, eid in enumerate(problem.employee_ids):
        work = [1 if (eid, day) in solution.assignments else 0 for day in range(d)]
        if eid in problem.exempt_employee_ids:
            # 豁免人员不受休息规则约束（也不设最低班数），仅参与每日人数/岗位等基础校验
            continue
        prev = problem.prev_work.get(eid)
        nxt = problem.next_work.get(eid)
        for day in range(d):
            left = work[day - 1] if day > 0 else (1 if prev else 0 if prev is not None else None)
            right = work[day + 1] if day < d - 1 else (1 if nxt else 0 if nxt is not None else None)
            if left is None or right is None:
                continue
            if work[day] == 1 and left == 0 and right == 0:
                errors.append(f"职工 {eid} 在第 {day + 1} 天违反「禁止孤立工作日」规则（休—上—休）。")
            if work[day] == 0 and left == 1 and right == 1:
                errors.append(f"职工 {eid} 在第 {day + 1} 天违反「禁止单日休息」规则（上—休—上）。")

        # 普通人员最低班数硬下限复核
        minimum = int(problem.minimum_shifts.get(eid, 0) or 0)
        if minimum > 0 and sum(work) < minimum:
            errors.append(
                f"职工 {eid} 实际 {sum(work)} 班，低于最低班数硬下限 {minimum} 班。"
            )

        # 连续休息上下限硬约束校验（仅判定可确定的区间；边界未知时不虚构状态）
        rest_runs = determinable_rest_runs(work, prev, nxt)
        if rest_runs:
            longest = max(rest_runs)
            if problem.max_consecutive_rest_days and longest > problem.max_consecutive_rest_days:
                errors.append(
                    f"职工 {eid} 连续休息 {longest} 天，超过配置上限 {problem.max_consecutive_rest_days} 天。"
                )
            shortest = min(rest_runs)
            if problem.min_consecutive_rest_days and shortest < problem.min_consecutive_rest_days:
                errors.append(
                    f"职工 {eid} 存在 {shortest} 天连续休息，低于配置下限 {problem.min_consecutive_rest_days} 天。"
                )

    quality_employee_ids = [
        eid for eid in problem.employee_ids if eid not in problem.exempt_employee_ids
    ]
    count2, count3, total = compute_rest_quality(quality_employee_ids, d, solution.assignments)
    if count2 != solution.windows_with_2_block:
        errors.append(
            f"休息质量指标统计错误：含连续2天休息窗口 {solution.windows_with_2_block}，实际 {count2}。"
        )
    if count3 != solution.windows_with_3_block:
        errors.append(
            f"休息质量指标统计错误：含连续3天休息窗口 {solution.windows_with_3_block}，实际 {count3}。"
        )
    if total != solution.total_windows:
        errors.append(f"休息窗口总数统计错误：报告 {solution.total_windows}，实际 {total}。")
    return errors


def diagnose_rest_infeasibility(problem: RestProblem) -> List[str]:
    # 区分：基础约束不可行（每日/岗位）与 基础可行但休息规则不可行。
    from .position_solver import solve_position_assignment

    reasons = []
    base_reasons = diagnose_position_infeasibility(_to_position_problem(problem))
    if base_reasons and base_reasons != [
        "岗位之间存在人员竞争冲突：具备多岗位资格的人员数量不足以同时满足各岗位的精确人数要求。"
    ]:
        # 基础层面有明确原因（每日人数/岗位资格/岗位合计超每日人数）
        reasons.extend(base_reasons)
        return reasons
    base_solution = solve_position_assignment(_to_position_problem(problem))
    if base_solution.status in ("optimal", "feasible"):
        reasons.append(
            "每日人数与岗位人数约束单独可满足，但加入硬性休息规则（禁止孤立工作日、禁止单日休息）后无解。"
        )
    elif base_solution.status == "unknown":
        reasons.append("基础约束求解超时，无法判断是否为休息规则导致的无解。")
        return reasons
    elif base_reasons:
        reasons.extend(base_reasons)
    else:
        reasons.append("基础约束本身不可行。")
    reasons.append(
        "诊断能力边界：无法自动精确定位具体冲突的职工或日期，请人工核对休息规则与岗位、班数配置。"
    )
    return reasons


def solve_rest_assignment(problem: RestProblem, *, time_limit_seconds: int = 120) -> RestSolution:
    # 每日人数 + 岗位人数 + 硬性休息规则 + 词典序优化（班数偏差 → 休息质量）联合求解。
    n = len(problem.employee_ids)
    d = problem.day_count
    r = problem.daily_required

    if n == 0 or r > n or d <= 0 or r < 0:
        return RestSolution(
            status="infeasible",
            message="；".join(diagnose_rest_infeasibility(problem)),
        )

    capacity_lower_bound = minimal_required_slots(problem)
    if capacity_lower_bound > problem.day_count * problem.daily_required:
        normal_total = sum(int(v) for v in problem.minimum_shifts.values())
        return RestSolution(
            status="infeasible",
            message=(
                f"硬约束冲突：普通人员最低班数合计 {normal_total} 班"
                f"（{len(problem.minimum_shifts)} 人）已超过周期总需求 "
                f"{problem.day_count * problem.daily_required} 班"
                f"（豁免人员不受休息规则约束、不设最低班数，其下限为 0）；"
                "请调整每日人数、最低班数或豁免人员。不得静默放宽任何硬约束。"
            ),
        )

    model, x, y, dev_terms = _build_base_model(
        employee_ids=problem.employee_ids,
        qualifications=problem.qualifications,
        day_count=d,
        daily_required=r,
        position_requirements=problem.position_requirements,
        targets=problem.targets,
    )
    _add_rest_hard_constraints(model, x, problem)
    _add_consecutive_rest_hard_constraints(model, x, problem)
    # 普通人员最低班数：硬性下限（不得因休息规则或岗位需求被降低）
    for i, eid in enumerate(problem.employee_ids):
        minimum = int(problem.minimum_shifts.get(eid, 0) or 0)
        if minimum > 0:
            model.Add(sum(x[i, day] for day in range(d)) >= minimum)
    has2_vars, has3_vars = _add_rest_quality_vars(model, x, problem)

    objectives = [(sum(dev_terms), True)]
    if has2_vars:
        objectives.append((sum(has2_vars), False))
    if has3_vars:
        objectives.append((sum(has3_vars), False))

    hint_vars = [x[i, day] for i in range(n) for day in range(d)]
    hint_vars += list(y.values())
    caps = [0]  # 首级目标（总偏差）理论下界为 0
    if has2_vars:
        caps.append(len(has2_vars))  # 2 连休窗口数不超过窗口总数
    if has3_vars:
        caps.append(len(has3_vars))  # 3 连休窗口数不超过窗口总数
    from django.conf import settings as _settings

    soft_stage_seconds = float(
        getattr(_settings, "SCHEDULER_SOFT_STAGE_SECONDS", 5.0)
    )
    status, solver, _, soft_incomplete = _solve_lexicographic(
        model,
        objectives,
        time_limit_seconds,
        hint_vars=hint_vars,
        objective_caps=caps,
        soft_stage_seconds=soft_stage_seconds,
        label="排班求解",
    )
    from ortools.sat.python import cp_model

    if status is None:
        return RestSolution(
            status="unknown",
            message="求解失败：未能在限定时间内证明可行或不可行。",
        )
    if status == cp_model.UNKNOWN:
        # 首级目标（班数偏差）都未求解成功 → 状态未确定，不能标记可行或不可行
        return RestSolution(
            status="unknown",
            message="求解超时：未能在限定时间内证明可行或不可行。",
        )
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return RestSolution(
            status="infeasible",
            message="；".join(diagnose_rest_infeasibility(problem)),
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
    quality_employee_ids = [
        eid for eid in problem.employee_ids if eid not in problem.exempt_employee_ids
    ]
    count2, count3, total_windows = compute_rest_quality(
        quality_employee_ids, d, assignments
    )
    max_rest_run = 0
    max_work_run = 0
    for eid in quality_employee_ids:
        work = [1 if (eid, day) in assignments else 0 for day in range(d)]
        rest_runs = determinable_rest_runs(
            work, problem.prev_work.get(eid), problem.next_work.get(eid)
        )
        work_runs = compute_work_runs(
            work, problem.prev_work.get(eid), problem.next_work.get(eid)
        )
        if rest_runs:
            max_rest_run = max(max_rest_run, max(rest_runs))
        if work_runs:
            max_work_run = max(max_work_run, max(work_runs))
    prev_count = len(problem.prev_work)
    next_count = len(problem.next_work)
    if prev_count < n or next_count < n:
        boundary_note = (
            f"周期首尾边界限制：仅 {prev_count}/{n} 名职工有上一周期末班数据、"
            f"{next_count}/{n} 名职工有下一周期首班数据；缺少数据的职工在周期首/尾日未应用完整休息规则。"
        )
    else:
        boundary_note = "周期首尾边界：已使用相邻周期真实排班数据校验首尾日休息规则。"

    quality_note = ""
    if total_windows > 0:
        unmet2 = total_windows - count2
        soft_limited = bool(getattr(_settings, "SCHEDULER_SOFT_STAGE_SECONDS", 5.0))
        suffix = (
            f"（3 连休软目标在 {float(getattr(_settings, 'SCHEDULER_SOFT_STAGE_SECONDS', 5.0)):.0f}s 限时内求解，"
            "未证明全局最优；硬约束与班数目标不受影响）"
            if soft_limited
            else ""
        )
        if unmet2 > 0:
            quality_note = f"休息优化未完全满足：{unmet2}/{total_windows} 个10天窗口没有连续2天休息。"
        else:
            quality_note = f"所有 {total_windows} 个10天窗口均含连续2天休息。" + suffix
    else:
        quality_note = f"周期 {d} 天不足 10 天，无滑动窗口优化。"

    solution = RestSolution(
        status="optimal",
        assignments=assignments,
        position_assignments=position_assignments,
        achieved=achieved,
        daily_counts=daily_counts,
        daily_position_counts=daily_position_counts,
        max_deviation=max(deviations, default=0),
        total_deviation=sum(deviations),
        max_rest_run=max_rest_run,
        max_work_run=max_work_run,
        windows_with_2_block=count2,
        windows_with_3_block=count3,
        total_windows=total_windows,
        objective=solver.ObjectiveValue(),
        message=(
            f"每日人数、岗位人数与硬性休息规则满足；目标班数最大偏差 {max(deviations, default=0)}，"
            f"总偏差 {sum(deviations)}。{quality_note}"
            + ("（提示：班数目标已达最优，但休息优化在时限内未证明最优，返回当前可行解。）"
               if soft_incomplete else "")
        ),
        boundary_note=boundary_note,
    )

    # 独立校验：失败时不得返回成功结果（软目标未满足不算失败）
    errors = validate_rest_solution(problem, solution)
    if errors:
        return RestSolution(
            status="invalid",
            message="独立校验失败：" + "；".join(errors),
            boundary_note=boundary_note,
        )
    return solution
