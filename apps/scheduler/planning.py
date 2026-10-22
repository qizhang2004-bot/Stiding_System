# 排班规划适配层：从 Django 数据组装排班规划输入（供配置页面与后续正式排班流程调用）。
# 已包含：人多人少判断与班数分配目标（一）、每日人数硬约束（二）、特殊岗位人数约束（三）、
# 休息规则（四）、统一综合求解入口 solve_shift_schedule（五）。
# 后续阶段接入草稿生成与确认保存。
from dataclasses import dataclass, field
import logging
from datetime import date as date_type, timedelta

_logger = logging.getLogger(__name__)
from typing import Dict, List, Optional

from apps.organization.models import ShiftConfig
from apps.personnel.models import Employee
from apps.personnel.services import shift_employees

from .engine.allocator import MODE_SURPLUS, MODE_TIGHT, ShiftAllocation, plan_shift_targets
from .engine.daily_solver import DailyProblem, solve_daily_assignment
from .engine.position_solver import PositionProblem, solve_position_assignment
from .engine.rest_solver import (
    RestProblem,
    RestSolution,
    solve_rest_assignment,
    validate_rest_solution,
)
from .models import ScheduleRecord
from .selectors import period_range


def build_shift_allocation(shift_config: ShiftConfig, anchor: date_type = None) -> dict:
    # 根据班次配置组装第一阶段排班规划。
    # 数据来源：
    #   D = period_range（每月 25 日至次月 24 日，跨月/跨年）
    #   R = ShiftConfig.daily_worker_count
    #   M = ShiftConfig.required_shifts（每人目标班数；min_required_shifts 不参与算法）
    #   N = 班组在职职工；豁免 = ShiftConfig.exempt_employee_ids（须属于本班组）
    start, end = period_range(anchor)
    total_days = (end - start).days + 1
    team = shift_config.team
    employees = list(shift_employees(shift_config))
    exempt_ids = set(shift_config.exempt_employee_ids or [])
    normal_ids = [e.pk for e in employees if e.pk not in exempt_ids]
    exempt_employee_ids = [e.pk for e in employees if e.pk in exempt_ids]
    allocation = plan_shift_targets(
        total_days=total_days,
        daily_required=shift_config.daily_worker_count,
        base_shifts=shift_config.required_shifts,
        normal_ids=normal_ids,
        exempt_ids=exempt_employee_ids,
    )
    return {
        "start": start,
        "end": end,
        "total_days": total_days,
        "allocation": allocation,
    }


def build_daily_problem(shift_config: ShiftConfig, anchor: date_type = None) -> dict:
    # 组装第二阶段（每日人数硬约束）求解输入。
    # 第一阶段分配结果为“目标值”，仅作为偏差目标参与求解，不直接当作最终排班结果。
    info = build_shift_allocation(shift_config, anchor)
    allocation = info["allocation"]
    team = shift_config.team
    employees = list(shift_employees(shift_config))
    if not allocation.feasible:
        return {
            "start": info["start"],
            "end": info["end"],
            "total_days": info["total_days"],
            "problem": None,
            "infeasible_reasons": allocation.notes,
        }
    problem = DailyProblem(
        employee_ids=[e.pk for e in employees],
        day_count=info["total_days"],
        daily_required=shift_config.daily_worker_count,
        targets=allocation.per_person_targets,
    )
    return {
        "start": info["start"],
        "end": info["end"],
        "total_days": info["total_days"],
        "problem": problem,
        "allocation": allocation,
    }


def solve_shift_daily(shift_config: ShiftConfig, anchor: date_type = None) -> dict:
    # 执行第二阶段求解：每日人数硬约束 + 班数目标偏差最小化。
    info = build_daily_problem(shift_config, anchor)
    if info["problem"] is None:
        return {
            "start": info["start"],
            "end": info["end"],
            "total_days": info["total_days"],
            "solution": None,
            "infeasible_reasons": info["infeasible_reasons"],
        }
    solution = solve_daily_assignment(info["problem"])
    return {
        "start": info["start"],
        "end": info["end"],
        "total_days": info["total_days"],
        "problem": info["problem"],
        "solution": solution,
    }


def build_position_problem(shift_config: ShiftConfig, anchor: date_type = None) -> dict:
    # 组装第三阶段（特殊岗位人数约束）求解输入。
    # 岗位资格：复用 parse_positions（分隔符解析，去空格/空项/去重）。
    # 岗位人数配置：ShiftConfig.position_requirements（{岗位: 要求人数}，精确人数）。
    from apps.personnel.services import parse_special_positions

    info = build_daily_problem(shift_config, anchor)
    team = shift_config.team
    employees = list(shift_employees(shift_config))
    qualifications = {e.pk: parse_special_positions(e.position) for e in employees}
    position_requirements = {
        name: count
        for name, count in (shift_config.position_requirements or {}).items()
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0
    }
    base = {
        "start": info["start"],
        "end": info["end"],
        "total_days": info["total_days"],
        "position_requirements": position_requirements,
        "qualifications": qualifications,
    }
    if info["problem"] is None:
        return {**base, "problem": None, "infeasible_reasons": info["infeasible_reasons"]}
    problem = PositionProblem(
        employee_ids=[e.pk for e in employees],
        qualifications=qualifications,
        day_count=info["total_days"],
        daily_required=shift_config.daily_worker_count,
        position_requirements=position_requirements,
        targets=info["allocation"].per_person_targets,
    )
    return {**base, "problem": problem, "allocation": info["allocation"]}


def solve_shift_positions(shift_config: ShiftConfig, anchor: date_type = None) -> dict:
    # 执行第三阶段求解：每日人数 + 特殊岗位人数联合求解。
    info = build_position_problem(shift_config, anchor)
    if info["problem"] is None:
        return {
            "start": info["start"],
            "end": info["end"],
            "total_days": info["total_days"],
            "solution": None,
            "infeasible_reasons": info["infeasible_reasons"],
        }
    solution = solve_position_assignment(info["problem"])
    return {
        "start": info["start"],
        "end": info["end"],
        "total_days": info["total_days"],
        "problem": info["problem"],
        "solution": solution,
    }


def build_rest_problem(shift_config: ShiftConfig, anchor: date_type = None) -> dict:
    # 组装第四阶段（休息规则）求解输入。
    # 边界数据：查询相邻周期真实排班记录（start-1 与 end+1），无数据时不虚构边界状态。
    from apps.personnel.services import parse_special_positions

    info = build_position_problem(shift_config, anchor)
    team = shift_config.team
    employees = list(shift_employees(shift_config))
    prev_date = info["start"] - timedelta(days=1)
    next_date = info["end"] + timedelta(days=1)
    prev_work: dict = {}
    next_work: dict = {}
    records = ScheduleRecord.objects.filter(
        employee__team=team,
        work_date__in=[prev_date, next_date],
        # 相邻周期边界输入：当前有效记录 + 已归档记录（归档是周期结束后的最终状态，
        # 仍必须作为下一周期的边界依据）；已人工取消（is_active=False）的记录不参与
        status__in=[
            ScheduleRecord.Status.CONFIRMED,
            ScheduleRecord.Status.PUBLISHED,
            ScheduleRecord.Status.ARCHIVED,
        ],
        is_active=True,
    ).select_related("employee")
    for record in records:
        worked = record.shift_type != ScheduleRecord.ShiftType.REST
        if record.work_date == prev_date:
            prev_work[record.employee_id] = worked
        else:
            next_work[record.employee_id] = worked
    base = {
        "start": info["start"],
        "end": info["end"],
        "total_days": info["total_days"],
        "position_requirements": info["position_requirements"],
        "qualifications": info["qualifications"],
        "prev_work": prev_work,
        "next_work": next_work,
    }
    if info["problem"] is None:
        return {**base, "problem": None, "infeasible_reasons": info["infeasible_reasons"]}
    problem = RestProblem(
        employee_ids=[e.pk for e in employees],
        qualifications=info["qualifications"],
        day_count=info["total_days"],
        daily_required=shift_config.daily_worker_count,
        position_requirements=info["position_requirements"],
        targets=info["allocation"].per_person_targets,
        prev_work=prev_work,
        next_work=next_work,
        min_consecutive_rest_days=shift_config.min_consecutive_rest_days,
        max_consecutive_rest_days=shift_config.max_consecutive_rest_days,
        # 普通人员最低班数（硬下限）：豁免人员不设最低班数
        minimum_shifts={
            e.pk: shift_config.required_shifts
            for e in employees
            if e.pk not in set(shift_config.exempt_employee_ids or [])
        },
        # 豁免人员：不受任何休息规则约束（仅受每日人数、岗位、班次归属等硬约束）
        exempt_employee_ids={
            e.pk for e in employees if e.pk in set(shift_config.exempt_employee_ids or [])
        },
    )
    return {**base, "problem": problem, "allocation": info["allocation"]}


def solve_shift_rest(shift_config: ShiftConfig, anchor: date_type = None) -> dict:
    # 执行第四阶段求解：每日人数 + 岗位人数 + 硬性休息规则 + 词典序优化。
    info = build_rest_problem(shift_config, anchor)
    if info["problem"] is None:
        return {
            "start": info["start"],
            "end": info["end"],
            "total_days": info["total_days"],
            "solution": None,
            "infeasible_reasons": info["infeasible_reasons"],
        }
    solution = solve_rest_assignment(info["problem"])
    return {
        "start": info["start"],
        "end": info["end"],
        "total_days": info["total_days"],
        "problem": info["problem"],
        "solution": solution,
    }


# ==================== 第五阶段：统一综合求解入口 ====================
# 所有硬性约束（每日人数、岗位人数、资格、一人一天一班一岗、休息规则）
# 在同一 CP-SAT 模型中共同生效（rest_solver 复用 _build_base_model 一次建模），
# 词典序优化后经独立校验，汇总偏差/休息质量/边界数据/诊断信息。
STATUS_FEASIBLE = "feasible"
STATUS_INFEASIBLE = "infeasible"
STATUS_UNKNOWN = "unknown"
STATUS_INVALID = "invalid"

_SOLUTION_STATUS_MAP = {
    "optimal": STATUS_FEASIBLE,
    "feasible": STATUS_FEASIBLE,
    "infeasible": STATUS_INFEASIBLE,
    "unknown": STATUS_UNKNOWN,
    "invalid": STATUS_INVALID,
}


@dataclass
class ShiftScheduleResult:
    # 统一求解结果
    status: str = STATUS_INVALID               # feasible / infeasible / unknown / invalid
    mode: str = ""                             # 人多模式 / 人少模式（MODE_SURPLUS / MODE_TIGHT）
    shift_config_id: int = 0
    period_start: date_type = None
    period_end: date_type = None
    total_days: int = 0
    daily_worker_count: int = 0
    employee_targets: Dict[int, int] = field(default_factory=dict)
    employee_actual_shifts: Dict[int, int] = field(default_factory=dict)
    total_shift_deviation: int = 0
    max_shift_deviation: int = 0
    position_summary: Dict[str, dict] = field(default_factory=dict)
    rest_quality: Dict[str, int] = field(default_factory=dict)
    boundary_note: str = ""
    validation_errors: List[str] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
    allocation: Optional[ShiftAllocation] = None
    solution: Optional[RestSolution] = None


def solve_shift_schedule(
    shift_config: ShiftConfig,
    anchor: date_type = None,
    *,
    time_limit_seconds: int = 120,
) -> ShiftScheduleResult:
    # 统一综合求解入口：队组/班组/班次/周期 → 有效职工与岗位资格 → 人多人少判断与
    # 班数目标 → 统一问题（每日人数+岗位+休息全部硬约束在同一个 CP-SAT 模型）
    # → 词典序优化 → 独立校验 → 汇总结果。
    import time as _time

    _prep_started = _time.monotonic()
    info = build_rest_problem(shift_config, anchor)
    _prep_elapsed = _time.monotonic() - _prep_started

    result = ShiftScheduleResult(
        shift_config_id=shift_config.pk,
        period_start=info["start"],
        period_end=info["end"],
        total_days=info["total_days"],
        daily_worker_count=shift_config.daily_worker_count,
    )

    if info["problem"] is None:
        # 第一阶段/基础数据不可行（如 R>N、空班组）
        result.status = STATUS_INFEASIBLE
        result.diagnostics = list(info["infeasible_reasons"])
        return result

    problem: RestProblem = info["problem"]
    allocation: ShiftAllocation = info["allocation"]
    result.mode = allocation.mode
    result.allocation = allocation
    result.employee_targets = dict(allocation.per_person_targets)

    _solve_started = _time.monotonic()
    solution = solve_rest_assignment(problem, time_limit_seconds=time_limit_seconds)
    _solve_elapsed = _time.monotonic() - _solve_started
    result.solution = solution
    result.boundary_note = solution.boundary_note
    result.employee_actual_shifts = dict(solution.achieved)
    result.total_shift_deviation = solution.total_deviation
    result.max_shift_deviation = solution.max_deviation
    result.rest_quality = {
        "windows_with_2_block": solution.windows_with_2_block,
        "windows_with_3_block": solution.windows_with_3_block,
        "total_windows": solution.total_windows,
    }
    # 岗位汇总：要求人数 / 具备资格人数 / 逐日实际人数 / 是否逐日精确匹配
    qualifications = info["qualifications"]
    for pos, required in info["position_requirements"].items():
        counts = solution.daily_position_counts.get(pos, [])
        result.position_summary[pos] = {
            "required": required,
            "qualified": sum(
                1 for eid in problem.employee_ids if pos in qualifications.get(eid, [])
            ),
            "counts": counts,
            "all_days_match": bool(counts) and all(c == required for c in counts),
        }

    result.status = _SOLUTION_STATUS_MAP.get(solution.status, STATUS_INVALID)

    if result.status == STATUS_FEASIBLE:
        # 统一入口对外再执行一次独立校验，任何硬性校验失败不得返回成功
        _verify_started = _time.monotonic()
        errors = validate_rest_solution(problem, solution)
        _verify_elapsed = _time.monotonic() - _verify_started
        result.validation_errors = errors
        if errors:
            result.status = STATUS_INVALID
            result.diagnostics = ["独立校验失败：" + "；".join(errors)]
            return result
        # 软目标信息性诊断（不影响可行状态）
        if solution.total_deviation > 0:
            result.diagnostics.append(
                f"班数目标未完全达成：总偏差 {solution.total_deviation}，最大偏差 {solution.max_deviation}。"
            )
        if solution.total_windows > 0 and solution.windows_with_2_block < solution.total_windows:
            unmet = solution.total_windows - solution.windows_with_2_block
            result.diagnostics.append(
                f"休息软目标未完全满足：{unmet}/{solution.total_windows} 个10天窗口没有连续2天休息。"
            )
    elif result.status == STATUS_INFEASIBLE:
        result.diagnostics = [solution.message]
    elif result.status == STATUS_UNKNOWN:
        result.diagnostics = [solution.message]
    try:
        _logger.info(
            "排班求解耗时：数据准备=%.2fs 建模+求解=%.2fs 独立校验=%.2fs 总计=%.2fs"
            "（班组=%s 班次=%s 周期=%s~%s 人数=%s 每日=%s）",
            _prep_elapsed,
            _solve_elapsed,
            locals().get("_verify_elapsed", 0.0),
            _time.monotonic() - _prep_started,
            shift_config.team.name,
            shift_config.shift_name,
            result.period_start,
            result.period_end,
            len(problem.employee_ids),
            shift_config.daily_worker_count,
        )
    except Exception:  # 日志失败绝不影响排班主流程
        _logger.debug("排班耗时日志写入失败", exc_info=True)
    return result
