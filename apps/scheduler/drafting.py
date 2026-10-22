import logging
# 排班草稿生成、查询与确认保存服务（第六阶段）。
# 设计文档见 docs/DRAFT_SAVE_DESIGN.md。
# - 草稿只保存在 ScheduleBatch（payload 明细 + 配置/职工快照），不写入 ScheduleRecord；
# - 只有求解 feasible 且独立校验通过的结果才能生成草稿（unknown/infeasible/invalid 均拒绝）；
# - 确认时在单事务内：行锁 → 权限 → 状态 → 配置/职工/周期复核 → 正式排班冲突检查
#   → 重新独立校验 → 展开写入 ScheduleRecord（status=confirmed，沿用草稿批次号）
#   → 更新批次状态 → 操作日志；任一步异常整体回滚。
from datetime import date, timedelta
from typing import Dict, List

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.accounts.permissions import ROLE_ADMIN, has_role, visible_organizations
from apps.auditlog.services import log_operation
from apps.organization.models import ShiftConfig, Team
from apps.personnel.models import Employee
from apps.personnel.services import parse_positions

from .models import ScheduleBatch, ScheduleRecord
from .planning import STATUS_FEASIBLE, ShiftScheduleResult, solve_shift_schedule
from .selectors import period_range
from .services import create_schedule_record, new_schedule_batch_id


def _require_admin(user):
    # 服务端权限校验：仅队组管理员及以上可操作；管理员必须绑定有效队组
    if not user.is_authenticated or not has_role(user, ROLE_ADMIN):
        raise PermissionDenied("无权生成或确认排班。")
    from apps.accounts.permissions import require_organization

    require_organization(user)
    return user


def _require_visible_org(user, organization_id):
    if not visible_organizations(user).filter(pk=organization_id).exists():
        raise PermissionDenied("无权操作该队组的排班草稿。")


def _generation_errors(result: ShiftScheduleResult) -> List[str]:
    # 将非 feasible 状态统一转换为明确的拒绝原因
    if result.status == "unknown":
        return ["求解超时未确定可行解，不能生成排班草稿。请稍后重试或检查配置。"]
    if result.status == "invalid":
        return ["独立校验失败，不能生成排班草稿："] + list(result.validation_errors)
    if result.status == "infeasible":
        return ["求解不可行，不能生成排班草稿："] + list(result.diagnostics)
    return ["未知求解状态，不能生成排班草稿。"]


def _parse_date(value) -> date:
    return date.fromisoformat(value) if isinstance(value, str) else value


def _build_payload(result: ShiftScheduleResult, shift_config: ShiftConfig) -> List[dict]:
    # 展开为全量 职工×天 明细（含休息行），保证可完整还原每日人员/岗位/班数统计
    solution = result.solution
    shift_name = shift_config.shift_name
    payload = []
    for eid in sorted(solution.achieved.keys()):
        for day in range(result.total_days):
            work_date = result.period_start + timedelta(days=day)
            worked = (eid, day) in solution.assignments
            payload.append(
                {
                    "employee_id": eid,
                    "work_date": work_date.isoformat(),
                    "shift_type": shift_name if worked else ScheduleRecord.ShiftType.REST,
                    "position": solution.position_assignments.get((eid, day), ""),
                }
            )
    return payload


def _qualifications_snapshot(shift_config) -> Dict[str, List[str]]:
    from apps.personnel.services import shift_employees

    employees = shift_employees(shift_config)
    return {str(e.pk): parse_positions(e.position) for e in employees}


def _payload_work_matrix(batch: ScheduleBatch) -> Dict[int, List[int]]:
    # 从 payload 还原每人每天是否上班（0/1），供校验与班数统计复用
    total_days = (batch.period_end - batch.period_start).days + 1
    matrix = {int(eid): [0] * total_days for eid in batch.employee_targets}
    for item in batch.payload:
        day_index = (_parse_date(item["work_date"]) - batch.period_start).days
        if item["shift_type"] != ScheduleRecord.ShiftType.REST:
            matrix[item["employee_id"]][day_index] = 1
    return matrix


def validate_batch_payload(batch: ScheduleBatch) -> List[str]:
    # 独立校验草稿明细（不信任快照与求解结果）：每日人数、逐日岗位人数、一人一天一条、
    # 岗位资格、两条硬性休息规则（周期内部日期）、班数统计、无重复、周期与批次完整性。
    structure_errors = payload_structure_errors(batch)
    if structure_errors:
        return structure_errors
    errors = []
    total_days = (batch.period_end - batch.period_start).days + 1
    employees = [int(eid) for eid in batch.employee_targets]
    if not employees:
        return ["草稿明细缺失职工信息。"]
    if not isinstance(batch.payload, list) or not batch.payload:
        return ["草稿明细为空。"]

    # 覆盖性 + 重复检查：每名职工每天恰好一条
    seen = set()
    for item in batch.payload:
        eid = item.get("employee_id")
        raw_date = item.get("work_date")
        work_date = _parse_date(raw_date)
        if eid not in employees:
            errors.append(f"草稿含未知职工或跨班组人员：{eid}。")
            continue
        if not (batch.period_start <= work_date <= batch.period_end):
            errors.append(f"草稿含周期外日期：{raw_date}。")
            continue
        key = (eid, work_date)
        if key in seen:
            errors.append(f"草稿存在重复记录：职工 {eid} 于 {work_date}。")
        seen.add(key)
    if errors:
        return errors
    expected_keys = {
        (eid, batch.period_start + timedelta(days=d))
        for eid in employees for d in range(total_days)
    }
    missing = expected_keys - seen
    if missing:
        sample = sorted(missing, key=lambda k: (str(k[0]), k[1]))[0]
        return [f"草稿明细不完整：缺少 {len(missing)} 条（如职工 {sample[0]} 于 {sample[1]}）。"]

    matrix = _payload_work_matrix(batch)
    # 每日人数严格等于快照
    for day in range(total_days):
        current_date = batch.period_start + timedelta(days=day)
        count = sum(matrix[eid][day] for eid in employees)
        if count != batch.daily_worker_count:
            errors.append(
                f"{current_date} 实际上班 {count} 人，要求 {batch.daily_worker_count} 人。"
            )

    # 逐日岗位人数严格等于快照 + 资格检查 + 一人一天最多一个岗位
    quals = {str(k): v for k, v in batch.employee_qualifications.items()}
    for item in batch.payload:
        pos = (item.get("position") or "").strip()
        if not pos:
            continue
        if item["shift_type"] == ScheduleRecord.ShiftType.REST:
            errors.append(
                f"职工 {item['employee_id']} 于 {item['work_date']} 休息日却承担岗位「{pos}」。"
            )
        if pos not in quals.get(str(item["employee_id"]), []):
            errors.append(f"职工 {item['employee_id']} 不具备岗位「{pos}」资格。")
    for pos, required in batch.position_requirements.items():
        for day in range(total_days):
            current_date = batch.period_start + timedelta(days=day)
            count = sum(
                1 for item in batch.payload
                if _parse_date(item["work_date"]) == current_date
                and (item.get("position") or "") == pos
            )
            if count != required:
                errors.append(f"{current_date} 岗位「{pos}」实际 {count} 人，要求 {required} 人。")

    # 硬性休息规则（周期内部日期）：豁免人员不受这些规则约束（与求解模型一致）
    exempt_ids = {
        int(eid)
        for eid in (batch.summary.get("config_snapshot", {}) or {}).get(
            "exempt_employee_ids", []
        )
    }
    for eid in employees:
        if eid in exempt_ids:
            continue
        work = matrix[eid]
        for day in range(1, total_days - 1):
            if work[day] == 1 and work[day - 1] == 0 and work[day + 1] == 0:
                errors.append(f"职工 {eid} 第 {day + 1} 天违反「禁止孤立工作日」规则。")
            if work[day] == 0 and work[day - 1] == 1 and work[day + 1] == 1:
                errors.append(f"职工 {eid} 第 {day + 1} 天违反「禁止单日休息」规则。")

    # 班数统计与偏差一致
    actual = {eid: sum(matrix[eid]) for eid in employees}
    total_deviation = sum(
        abs(actual[eid] - batch.employee_targets[str(eid)]) for eid in employees
    )
    if total_deviation != batch.summary.get("total_shift_deviation"):
        errors.append(
            f"班数偏差统计不一致：摘要 {batch.summary.get('total_shift_deviation')}，实际 {total_deviation}。"
        )
    return errors


def create_schedule_draft(shift_config, user, *, anchor=None, time_limit_seconds: int = 120) -> dict:
    # 生成排班草稿：求解 → 状态/校验检查 → 单行写入 ScheduleBatch（失败零残留）。
    _require_admin(user)
    from apps.accounts.permissions import get_shift_config_or_404

    shift_config = get_shift_config_or_404(user, shift_config.pk)

    result = solve_shift_schedule(shift_config, anchor=anchor, time_limit_seconds=time_limit_seconds)
    if result.status != STATUS_FEASIBLE or result.validation_errors:
        return {"draft": None, "result": result, "errors": _generation_errors(result)}

    payload = _build_payload(result, shift_config)
    summary = {
        "total_shift_deviation": result.total_shift_deviation,
        "max_shift_deviation": result.max_shift_deviation,
        "max_rest_run": getattr(result.solution, "max_rest_run", 0),
        "max_work_run": getattr(result.solution, "max_work_run", 0),
        "min_consecutive_rest_days": shift_config.min_consecutive_rest_days,
        "max_consecutive_rest_days": shift_config.max_consecutive_rest_days,
        "rest_quality": result.rest_quality,
        "boundary_note": result.boundary_note,
        "validation_errors": [],
        "config_snapshot": {
            "required_shifts": shift_config.required_shifts,
            "exempt_employee_ids": list(shift_config.exempt_employee_ids or []),
        },
    }
    batch = ScheduleBatch(
        organization=shift_config.team.organization,
        team=shift_config.team,
        shift_config=shift_config,
        period_start=result.period_start,
        period_end=result.period_end,
        mode=result.mode,
        daily_worker_count=result.daily_worker_count,
        position_requirements=dict(shift_config.position_requirements or {}),
        employee_targets={str(k): v for k, v in result.employee_targets.items()},
        employee_qualifications=_qualifications_snapshot(shift_config),
        payload=payload,
        summary=summary,
        created_by=user,
    )

    # 保存前独立校验（防御），失败不落库
    errors = validate_batch_payload(batch)
    if errors:
        return {"draft": None, "result": result, "errors": ["草稿明细校验失败："] + errors}

    with transaction.atomic():
        batch.save()
        log_operation(
            user=user,
            action="create",
            module="schedule",
            target_type="schedule_batch",
            target_id=str(batch.batch_id),
            description=(
                f"生成排班草稿 {batch.team.name} · {batch.shift_config.get_shift_name_display()} · "
                f"{batch.period_start}~{batch.period_end}（{len(payload)} 条明细）"
            ),
        )
    return {"draft": batch, "result": result, "errors": []}


def _config_snapshot(config, employees) -> dict:
    # 求解前快照：写入事务取锁后据此重新核对，发现变化则中止（不应用过期结果）
    return {
        "daily_worker_count": config.daily_worker_count,
        "required_shifts": config.required_shifts,
        "position_requirements": dict(config.position_requirements or {}),
        "exempt_employee_ids": list(config.exempt_employee_ids or []),
        "min_consecutive_rest_days": config.min_consecutive_rest_days,
        "max_consecutive_rest_days": config.max_consecutive_rest_days,
        "employees": {
            e.pk: {"is_active": e.is_active, "position": e.position} for e in employees
        },
    }


def _snapshot_mismatch(snapshot: dict, config, employees) -> List[str]:
    errors = []
    if config.daily_worker_count != snapshot["daily_worker_count"]:
        errors.append("每日要求人数在求解期间发生变化。")
    if config.required_shifts != snapshot["required_shifts"]:
        errors.append("每人目标班数在求解期间发生变化。")
    if dict(config.position_requirements or {}) != snapshot["position_requirements"]:
        errors.append("岗位人数配置在求解期间发生变化。")
    if list(config.exempt_employee_ids or []) != snapshot["exempt_employee_ids"]:
        errors.append("豁免人员配置在求解期间发生变化。")
    if (config.min_consecutive_rest_days, config.max_consecutive_rest_days) != (
        snapshot["min_consecutive_rest_days"], snapshot["max_consecutive_rest_days"]
    ):
        errors.append("连续休息规则在求解期间发生变化。")
    current = {e.pk: {"is_active": e.is_active, "position": e.position} for e in employees}
    if current != snapshot["employees"]:
        errors.append("班组在职人员或岗位资格在求解期间发生变化。")
    return errors


def _cross_shift_record_errors(team_id, employee_ids, start, end, config_id) -> List[str]:
    # 生成前的数据一致性校验：本班次人员在周期内若存在“其他班次”的有效排班（来源于人工跨班次改班），
    # 直接生成会造成同一职工同一天两条有效记录。此时明确拒绝并列出，交由人工处理，
    # 绝不静默删除人工调整结果，也不影响其他班次。
    rows = list(
        ScheduleRecord.objects.filter(
            team_id=team_id,
            employee_id__in=list(employee_ids),
            work_date__range=(start, end),
            status__in=ACTIVE_RECORD_STATUSES,
            is_active=True,
        )
        .exclude(shift_config_id=config_id)
        .values_list("employee_id", "work_date", "shift_config__shift_name")
    )
    return [
        f"职工 {employee_id} 于 {work_date} 存在 {shift_name} 班次的有效排班"
        for employee_id, work_date, shift_name in rows[:10]
    ]


def generate_and_apply(shift_config, user, *, anchor=None, time_limit_seconds: int = 120) -> dict:
    # 生成即保存：求解与独立校验在事务外完成（失败零写入），随后单事务内
    # “锁定 → 复核快照 → 退休同队组同周期旧有效排班 → 写入新版本 → 审计”。
    _require_admin(user)
    from apps.accounts.permissions import get_shift_config_or_404

    shift_config = get_shift_config_or_404(user, shift_config.pk)
    period_start, period_end = period_range(anchor or date.today())

    import time as _time

    _started = _time.monotonic()
    _timings = {}

    # 1) 求解（事务外，避免长事务占用锁）
    result = solve_shift_schedule(
        shift_config, anchor=period_start, time_limit_seconds=time_limit_seconds
    )
    _timings["solve"] = round(_time.monotonic() - _started, 3)
    if result.status != STATUS_FEASIBLE or result.validation_errors:
        errors = _generation_errors(result)
        _log_generate_failure(user, shift_config, period_start, period_end, errors)
        return {"applied": False, "result": result, "errors": errors}

    # 2) 独立明细校验（事务外；复用既有校验器，取消草稿流程也不删除该校验）
    _validate_started = _time.monotonic()
    payload = _build_payload(result, shift_config)
    batch = ScheduleBatch(
        organization=shift_config.team.organization,
        team=shift_config.team,
        shift_config=shift_config,
        period_start=result.period_start,
        period_end=result.period_end,
        mode=result.mode,
        daily_worker_count=result.daily_worker_count,
        position_requirements=dict(shift_config.position_requirements or {}),
        employee_targets={str(k): v for k, v in result.employee_targets.items()},
        employee_qualifications=_qualifications_snapshot(shift_config),
        payload=payload,
        summary={
            "total_shift_deviation": result.total_shift_deviation,
            "max_shift_deviation": result.max_shift_deviation,
            "max_rest_run": getattr(result.solution, "max_rest_run", 0),
            "max_work_run": getattr(result.solution, "max_work_run", 0),
            "min_consecutive_rest_days": shift_config.min_consecutive_rest_days,
            "max_consecutive_rest_days": shift_config.max_consecutive_rest_days,
            "rest_quality": result.rest_quality,
            "boundary_note": result.boundary_note,
            "validation_errors": [],
            "config_snapshot": {
                "required_shifts": shift_config.required_shifts,
                "exempt_employee_ids": list(shift_config.exempt_employee_ids or []),
            },
            "generated_at": timezone.now().isoformat(),
        },
        kind=ScheduleBatch.Kind.PERIOD,
        created_by=user,
    )
    errors = validate_batch_payload(batch)
    _timings["validate"] = round(_time.monotonic() - _validate_started, 3)
    if errors:
        errors = ["排班明细校验失败："] + errors
        _log_generate_failure(user, shift_config, period_start, period_end, errors)
        return {"applied": False, "result": result, "errors": errors}

    # 3) 求解前快照（写入事务内复核）
    from apps.personnel.services import shift_employees

    # 快照/复核只覆盖当前班次的在职职工，与求解器输入保持同一人员口径
    employees = list(shift_employees(shift_config).order_by("pk"))
    snapshot = _config_snapshot(shift_config, employees)

    # 4) 写入事务：团队级串行化 → 复核快照 → 退休旧有效排班 → 写入新版本
    _write_started = _time.monotonic()
    try:
        with transaction.atomic():
            Team.objects.select_for_update().filter(pk=shift_config.team_id).first()
            config = ShiftConfig.objects.select_for_update().get(pk=shift_config.pk)
            locked_employees = {
                e.pk: e
                for e in Employee.objects.select_for_update()
                .filter(team=config.team, shift_config=config, is_active=True)
                .order_by("pk")
            }
            if (config.team_id, config.team.organization_id) != (
                batch.team_id, batch.organization_id
            ):
                raise PermissionDenied("配置与批次队组/班组不一致，拒绝写入。")
            mismatch = _snapshot_mismatch(snapshot, config, locked_employees.values())
            if mismatch:
                log_operation(
                    user=user, action="generate_failed", module="schedule",
                    target_type="shift_config", target_id=str(config.pk),
                    description=(
                        f"生成中止（快照变化）：{config.team.name} · "
                        f"{config.get_shift_name_display()} · {period_start}~{period_end}；"
                        + "；".join(mismatch)
                    ),
                )
                return {
                    "applied": False, "result": result,
                    "errors": ["配置或人员已变化，未应用本次求解结果："] + mismatch,
                }

            # 周期规则：仅允许“当前有效”或“空周期”；历史/归档周期拒绝
            state = period_version_state(
                config.team_id, batch.period_start, batch.period_end, lock=True
            )
            if state in ("archived", "historical"):
                reason = (
                    f"{batch.period_start}~{batch.period_end} 周期已归档或已被替换，拒绝覆盖。"
                    if state == "archived"
                    else f"{batch.period_start}~{batch.period_end} 周期已不是当前有效排班（已被替换），拒绝覆盖。"
                )
                log_operation(
                    user=user, action="generate_failed", module="schedule",
                    target_type="shift_config", target_id=str(config.pk),
                    description=(
                        f"生成被拒绝：{config.team.name} · {config.get_shift_name_display()} · "
                        f"{batch.period_start}~{batch.period_end}；{reason}"
                    ),
                )
                return {"applied": False, "result": result, "errors": [reason]}

            # 退休同队组、同周期、当前有效的全部记录（含人工添加的 adjustment 记录）
            # 数据一致性：本班次人员在周期内存在其他班次的有效记录（人工改班）→ 拒绝，
            # 避免生成后同一职工同一天出现两条有效记录（不静默删除人工调整）。
            shift_employee_ids = list(locked_employees)
            cross_shift_errors = _cross_shift_record_errors(
                config.team_id,
                shift_employee_ids,
                batch.period_start,
                batch.period_end,
                config.pk,
            )
            if cross_shift_errors:
                errors = [
                    "该班次人员在周期内存在其他班次的有效排班，为免重复请先处理后再生成："
                ] + cross_shift_errors
                _log_generate_failure(
                    user, shift_config, period_start, period_end, errors
                )
                return {"applied": False, "result": result, "errors": errors}

            # 只退休“当前班次 + 当前周期”的旧有效记录：
            # 同一班组的不同班次（如运输班早班/中班）人员与排班互相独立，不能被彼此替换。
            retired = ScheduleRecord.objects.select_for_update().filter(
                team_id=config.team_id,
                shift_config=config,
                work_date__range=(batch.period_start, batch.period_end),
                status__in=ACTIVE_RECORD_STATUSES,
                is_active=True,
            ).update(status=ScheduleRecord.Status.SUPERSEDED, updated_at=timezone.now())

            # 写入新版本
            batch.status = ScheduleBatch.Status.CONFIRMED
            batch.confirmed_at = timezone.now()
            batch.confirmed_by = user
            batch.save()
            # 旧版本批次头同步作废（其每日记录已在上一步退休为 superseded），
            # 保证同一班组同一周期只有一个“当前版本”（避免结果页把旧批次也标成当前版本）
            # 仅作废“同一班次”的旧版本批次头：同班组其他班次（如运输班早/中班）互相独立，
            # 不能被彼此的生成操作标记为 superseded。
            ScheduleBatch.objects.select_for_update().filter(
                team_id=config.team_id,
                kind=ScheduleBatch.Kind.PERIOD,
                status=ScheduleBatch.Status.CONFIRMED,
                period_start=batch.period_start,
                period_end=batch.period_end,
                shift_config=config,
            ).exclude(pk=batch.pk).update(
                status=ScheduleBatch.Status.SUPERSEDED, updated_at=timezone.now()
            )
            for item in payload:
                employee = locked_employees.get(item["employee_id"])
                if employee is None:
                    raise PermissionDenied("求解结果含本班组之外的职工，拒绝写入。")
                create_schedule_record(
                    schedule_batch_id=batch.batch_id,
                    organization=batch.organization,
                    team=batch.team,
                    shift_config=config,
                    employee=employee,
                    work_date=_parse_date(item["work_date"]),
                    shift_type=item["shift_type"],
                    position=item.get("position", ""),
                    status=ScheduleRecord.Status.CONFIRMED,
                )

            # 写入后一致性断言（同一事务内，失败即整体回滚）
            active_rows = list(
                ScheduleRecord.objects.filter(
                    team_id=config.team_id,
                    shift_config=config,
                    work_date__range=(batch.period_start, batch.period_end),
                    status__in=ACTIVE_RECORD_STATUSES,
                    is_active=True,
                ).values_list("employee_id", "work_date")
            )
            if len(active_rows) != len(payload):
                raise RuntimeError(
                    f"写入后有效记录数 {len(active_rows)} 与求解明细数 {len(payload)} 不一致。"
                )
            if len(set(active_rows)) != len(active_rows):
                raise RuntimeError("写入后出现同一职工同一天多条有效记录。")

            log_operation(
                user=user, action="generate", module="schedule",
                target_type="schedule_batch", target_id=str(batch.batch_id),
                after_data={
                    "batch_id": str(batch.batch_id),
                    "team": batch.team.name,
                    "shift_type": config.shift_name,
                    "shift_label": config.get_shift_name_display(),
                    "period_start": batch.period_start.isoformat(),
                    "period_end": batch.period_end.isoformat(),
                    "record_count": len(payload),
                    "retired_records": retired,
                    "scope": "period",
                },
                description=(
                    f"生成并保存排班：{batch.team.name} · {config.get_shift_name_display()} · "
                    f"{batch.period_start}~{batch.period_end}，写入 {len(payload)} 条；"
                    f"退休旧有效排班 {retired} 条（保留为已作废）。"
                ),
            )
            # 自动归档：本队组所有已结束、尚未归档且早于新周期起始日的旧周期（与生成同事务）
            archive_result = archive_ended_periods_for_team(
                config.team, batch.period_start, user
            )
            if retired:
                log_operation(
                    user=user, action="replace", module="schedule",
                    target_type="schedule_batch", target_id=str(batch.batch_id),
                    after_data={
                        "batch_id": str(batch.batch_id),
                        "team": batch.team.name,
                        "shift_type": config.shift_name,
                        "period_start": batch.period_start.isoformat(),
                        "period_end": batch.period_end.isoformat(),
                        "retired_records": retired,
                        "scope": "replace",
                    },
                    description=(
                        f"整周期替换：{batch.team.name} · {batch.period_start}~{batch.period_end} "
                        f"退休 {retired} 条旧有效记录（含人工添加记录），记录保留为已作废状态。"
                    ),
                )
        _timings["write"] = round(_time.monotonic() - _write_started, 3)
        _timings["total"] = round(_time.monotonic() - _started, 3)
        logger.info(
            "排班生成完成：班组=%s 班次=%s 周期=%s~%s 记录=%s 耗时 求解=%.2fs 校验=%.2fs 写入=%.2fs 总计=%.2fs",
            shift_config.team.name,
            shift_config.shift_name,
            batch.period_start,
            batch.period_end,
            len(payload),
            _timings["solve"],
            _timings["validate"],
            _timings["write"],
            _timings["total"],
        )
        return {
            "applied": True,
            "batch": batch,
            "record_count": len(payload),
            "retired": retired,
            "archived": archive_result["archived"],
            "result": result,
            "timings": _timings,
            "errors": [],
        }
    except Exception as exc:  # 事务已回滚：旧排班完整保留
        _timings["write"] = round(_time.monotonic() - _write_started, 3)
        _timings["total"] = round(_time.monotonic() - _started, 3)
        logger.warning(
            "排班生成失败（已回滚）：班组=%s 班次=%s 周期=%s~%s 耗时 求解=%.2fs 校验=%.2fs 写入=%.2fs 原因=%s",
            shift_config.team.name, shift_config.shift_name, period_start, period_end,
            _timings.get("solve", 0.0), _timings.get("validate", 0.0),
            _timings["write"], exc,
        )
        message = f"写入失败，已回滚，旧排班保持不变：{exc}"
        _log_generate_failure(user, shift_config, period_start, period_end, [message])
        return {"applied": False, "result": result, "errors": [message]}


def _log_generate_failure(user, shift_config, period_start, period_end, errors) -> None:
    # 生成失败审计（在写入事务之外记录，确保回滚不吞掉失败痕迹）
    log_operation(
        user=user, action="generate_failed", module="schedule",
        target_type="shift_config", target_id=str(shift_config.pk),
        description=(
            f"生成失败：{shift_config.team.name} · {shift_config.get_shift_name_display()} · "
            f"{period_start}~{period_end}；" + "；".join(str(e) for e in errors[:3])
        ),
    )


def get_schedule_draft(batch_id, user) -> ScheduleBatch:
    # 查询草稿（供后续页面查看使用）；跨队组/只读用户抛 PermissionDenied
    _require_admin(user)
    batch = ScheduleBatch.objects.filter(batch_id=batch_id).first()
    if batch is None:
        raise PermissionDenied("排班草稿不存在或无权访问。")
    _require_visible_org(user, batch.organization_id)
    return batch


def _freshness_errors(batch: ScheduleBatch, config: ShiftConfig, locked_employees) -> List[str]:
    # 确认前复核：与已锁定（最新已提交）的配置与职工状态比对，不信任快照。
    # config/locked_employees 由调用方用 select_for_update 获取：锁定读绕过
    # REPEATABLE READ 快照，直接读取最新已提交数据，消除复核与写入之间的竞态。
    errors = []
    if config.daily_worker_count != batch.daily_worker_count:
        errors.append(
            f"每日要求人数已由 {batch.daily_worker_count} 变更为 {config.daily_worker_count}，草稿已过期。"
        )
    if dict(config.position_requirements or {}) != dict(batch.position_requirements or {}):
        errors.append("岗位人数配置已变化，草稿已过期。")
    snapshot = batch.summary.get("config_snapshot", {})
    if config.required_shifts != snapshot.get("required_shifts"):
        errors.append("每人目标班数配置已变化，草稿已过期。")
    if list(config.exempt_employee_ids or []) != list(snapshot.get("exempt_employee_ids", [])):
        errors.append("豁免人员配置已变化，草稿已过期。")
    current_quals = {
        str(e.pk): parse_positions(e.position) for e in locked_employees.values()
    }
    if current_quals != dict(batch.employee_qualifications or {}):
        errors.append("在职职工或岗位资格已变化，草稿已过期。")
    start, end = period_range()
    if (start, end) != (batch.period_start, batch.period_end):
        errors.append(
            f"当前排班周期为 {start}~{end}，草稿周期 {batch.period_start}~{batch.period_end} 已过期。"
        )
    return errors


logger = logging.getLogger(__name__)

ACTIVE_RECORD_STATUSES = [
    ScheduleRecord.Status.CONFIRMED,
    ScheduleRecord.Status.PUBLISHED,
]


def build_conflict_report(batch: ScheduleBatch, sample_limit: int = 20) -> dict:
    # 冲突明细报告：列出与草稿 (职工, 日期) 重叠的既有正式记录（同班组、同周期）。
    # 返回 {total, samples[], source_batches[]}；仅统计 confirmed/published（已作废记录不计）。
    keys = {(item["employee_id"], _parse_date(item["work_date"])) for item in batch.payload}
    draft_lines = {int(item["employee_id"]): item for item in batch.payload}
    rows = (
        ScheduleRecord.objects.filter(
            team_id=batch.team_id,
            status__in=ACTIVE_RECORD_STATUSES,
            work_date__range=(batch.period_start, batch.period_end),
        )
        .exclude(schedule_batch_id=batch.batch_id)  # 本批次自身记录不算冲突
        .select_related("employee")
        .order_by("employee_id", "work_date")
    )
    conflicts = []
    for record in rows:
        key = (record.employee_id, record.work_date)
        if key in keys:
            conflicts.append(record)
    samples = []
    for record in conflicts[:sample_limit]:
        item = draft_lines.get(record.employee_id, {})
        samples.append(
            {
                "employee_id": record.employee_id,
                "employee_no": record.employee.employee_no,
                "name": record.employee.name,
                "work_date": record.work_date,
                "team": record.team.name,
                "record_id": record.pk,
                "batch_id": str(record.schedule_batch_id),
                "shift_type": record.get_shift_type_display(),
                "draft_shift_type": item.get("shift_type", ""),
                "draft_position": item.get("position", ""),
            }
        )
    source_batches = {}
    for record in conflicts:
        source_batches.setdefault(
            str(record.schedule_batch_id),
            {"batch_id": str(record.schedule_batch_id), "count": 0},
        )["count"] += 1
    return {
        "total": len(conflicts),
        "samples": samples,
        "source_batches": sorted(source_batches.values(), key=lambda x: -x["count"]),
        "batch_id": str(batch.batch_id),
        "period_start": batch.period_start,
        "period_end": batch.period_end,
    }


def supersedable_sources(batch: ScheduleBatch, report: dict) -> tuple[bool, str]:
    # 判断冲突来源是否全部为“同队组、同周期的已知正式批次”（可安全自动作废）。
    # 不满足时返回原因，确认流程将保持阻止（避免误伤其他队组/周期或来源不明的历史记录）。
    batch_ids = [entry["batch_id"] for entry in report["source_batches"]]
    if not batch_ids:
        return False, "没有可作废的来源批次。"
    if str(batch.batch_id) in batch_ids:
        return False, "冲突来自本批次自身，拒绝自动作废。"
    existing = list(ScheduleBatch.objects.filter(batch_id__in=batch_ids))
    known = {str(other.batch_id) for other in existing}
    unknown = [bid for bid in batch_ids if bid not in known]
    if unknown:
        return False, (
            f"存在 {len(unknown)} 个来源不明的历史批次号（无批次记录），"
            "无法确认版本关系，已阻止自动作废。"
        )
    for other in existing:
        if other.status != ScheduleBatch.Status.CONFIRMED:
            return False, f"来源批次 {str(other.batch_id)[:8]} 当前状态为 {other.get_status_display()}，不构成可作废的旧版本。"
        if other.team_id != batch.team_id or other.period_start != batch.period_start or other.period_end != batch.period_end:
            return False, "冲突来源批次不属于同队组、同周期，拒绝自动作废。"
    return True, ""


def _supersede_batches(batch: ScheduleBatch, report: dict, user) -> List[str]:
    # 将冲突来源批次整体作废（保留记录，状态改为 superseded）；不删除任何数据。
    # 兼容“记录引用的批次号没有批次行”的历史数据：按批次号退休记录，仅对存在的批次行更新状态。
    batch_ids = [entry["batch_id"] for entry in report["source_batches"]]
    if not batch_ids:
        return []
    if str(batch.batch_id) in batch_ids:
        return []  # 与自身批次冲突：属重复写入，拒绝
    existing = list(ScheduleBatch.objects.select_for_update().filter(batch_id__in=batch_ids))
    if any(other.status == ScheduleBatch.Status.DRAFT for other in existing):
        return []  # 未确认草稿不构成正式冲突来源，出现即视为异常，拒绝
    retired = 0
    for other in existing:
        retired += ScheduleRecord.objects.filter(
            schedule_batch_id=other.batch_id, status__in=ACTIVE_RECORD_STATUSES
        ).update(status=ScheduleRecord.Status.SUPERSEDED, updated_at=timezone.now())
        other.status = ScheduleBatch.Status.SUPERSEDED
        other.save(update_fields=["status", "updated_at"])
        log_operation(
            user=user,
            action="supersede",
            module="schedule",
            target_type="schedule_batch",
            target_id=str(other.batch_id),
            description=(
                f"作废批次 {other.batch_id}（{other.team.name} · "
                f"{other.shift_config.get_shift_name_display()} · {other.period_start}~{other.period_end}），"
                f"由新批次 {batch.batch_id} 替换；原记录保留为已作废状态。"
            ),
        )
    known_ids = {other.batch_id for other in existing}
    unknown_ids = [bid for bid in batch_ids if bid not in known_ids]
    if unknown_ids:
        # 历史/外部写入的记录（无批次行）：同样纳入整周期替换范围，仅退休记录
        retired += ScheduleRecord.objects.filter(
            schedule_batch_id__in=unknown_ids, status__in=ACTIVE_RECORD_STATUSES
        ).update(status=ScheduleRecord.Status.SUPERSEDED, updated_at=timezone.now())
        log_operation(
            user=user,
            action="supersede",
            module="schedule",
            target_type="schedule_batch",
            target_id=str(batch.batch_id),
            description=(
                f"整周期替换：退休 {len(unknown_ids)} 个历史批次号的 {retired} 条排班记录"
                f"（{batch.team.name} · {batch.period_start}~{batch.period_end}），记录保留为已作废状态。"
            ),
        )
    # 同队组、同周期的“人工补充排班”记录一并退休（避免旧的人工修改与新版本重复显示）
    adjustment_batch_ids = ScheduleBatch.objects.filter(
        kind=ScheduleBatch.Kind.ADJUSTMENT
    ).values("batch_id")
    ScheduleRecord.objects.filter(
        team_id=batch.team_id,
        work_date__range=(batch.period_start, batch.period_end),
        is_active=True,
        schedule_batch_id__in=adjustment_batch_ids,
    ).exclude(schedule_batch_id=batch.batch_id).update(
        status=ScheduleRecord.Status.SUPERSEDED, updated_at=timezone.now()
    )
    return batch_ids


def _conflict_errors(batch: ScheduleBatch) -> List[str]:
    # 正式排班冲突（班组级）：同一职工同一天在该班组**任意班次配置**下已存在
    # confirmed/published 正式记录 → 拒绝。使用锁定读（select_for_update）：
    # 锁定读直读最新已提交数据（绕过一致性快照），同时其范围锁（idx_schedule_team_date）
    # 串行化同班组全部班次配置的确认，杜绝跨批次/跨班次的并发重复写入。
    keys = {
        (item["employee_id"], _parse_date(item["work_date"])) for item in batch.payload
    }
    rows = (
        ScheduleRecord.objects.select_for_update()
        .filter(
            team_id=batch.team_id,
            status__in=ACTIVE_RECORD_STATUSES,
            work_date__range=(batch.period_start, batch.period_end),
        )
        .exclude(schedule_batch_id=batch.batch_id)
        .only("employee_id", "work_date")
    )
    existing = {(row.employee_id, row.work_date) for row in rows}
    conflicts = keys & existing
    if conflicts:
        sample = sorted(conflicts, key=lambda k: (k[0], k[1]))[0]
        return [
            f"已有正式排班记录冲突：共 {len(conflicts)} 条（如职工 {sample[0]} 于 {sample[1]}），请重新生成草稿。"
        ]
    return []


class InvariantViolation(Exception):
    # 数据不变量被破坏（写入后断言失败）：事务回滚，向上返回可读错误而非 500
    pass


def record_write_block_reason(record: ScheduleRecord) -> str:
    # 人工修改/删除的前置条件：只有“当前有效”的已确认记录可改。
    # 历史版本（superseded）与未确认/已删除记录一律拒绝，防止通过普通操作改动历史。
    if record.status == ScheduleRecord.Status.SUPERSEDED:
        return "该记录属于已被替换的历史版本，不能修改或删除。"
    if record.status != ScheduleRecord.Status.CONFIRMED:
        return f"该记录当前状态为「{record.get_status_display()}」，只有当前有效的已确认排班可以修改。"
    if not record.is_active:
        return "该记录已被删除（取消），不能重复修改。"
    return ""


def period_version_state(team_id, period_start, period_end, *, lock: bool = False) -> str:
    # 周期状态（版本级判定，区分四种语义）：
    #   "archived"   该周期已归档（只读历史，任何写入入口都必须拒绝）
    #   "current"    该周期存在当前有效排班（活跃记录，或已确认的整周期版本头）
    #   "historical" 该周期已被替换（无可写版本，仅剩 superseded 等历史记录）
    #   "empty"      该周期尚无任何排班数据（允许首次生成/添加）
    # lock=True 时对当前有效记录做锁定读，与整周期替换的批量退休互斥。
    qs = ScheduleRecord.objects.filter(team_id=team_id, work_date__range=(period_start, period_end))
    if lock:
        qs = qs.select_for_update()
    # 归档优先：即使原 ScheduleBatch 仍为 confirmed，已归档周期也不得判为 current
    if qs.filter(status=ScheduleRecord.Status.ARCHIVED).exists():
        return "archived"
    active = qs.filter(status__in=ACTIVE_RECORD_STATUSES, is_active=True)
    has_active = active.exists()
    has_version_head = ScheduleBatch.objects.filter(
        team_id=team_id,
        kind=ScheduleBatch.Kind.PERIOD,
        status=ScheduleBatch.Status.CONFIRMED,
        period_start=period_start,
        period_end=period_end,
    ).exists()
    if has_active or has_version_head:
        return "current"
    # 历史记录：任何既非当前有效、也非草稿的既有记录（superseded 现在；archived 在 P3 落地后自动纳入）
    history_statuses = (
        ScheduleRecord.objects.filter(
            team_id=team_id, work_date__range=(period_start, period_end)
        )
        .exclude(status__in=ACTIVE_RECORD_STATUSES)
        .exclude(status=ScheduleRecord.Status.DRAFT)
        .exists()
    )
    return "historical" if history_statuses else "empty"


def period_write_block_reason(team_id, work_date, *, lock: bool = False) -> str:
    # 人工写入（添加）的周期级门禁：只有“当前有效”或“空周期”可写；
    # 已被替换的历史周期一律拒绝（归档周期在 P3 落地后同样落入 historical）。
    start, end = period_range(work_date)
    state = period_version_state(team_id, start, end, lock=lock)
    if state == "archived":
        return f"{start}~{end} 周期已归档（只读历史），不能新增或修改排班。"
    if state == "historical":
        return f"{start}~{end} 周期已不是当前有效排班（已被替换），不能新增或修改排班。"
    return ""


def remove_schedule_record(record: ScheduleRecord, user) -> dict:
    # 人工删除排班（软删除）：仅置 is_active=False 并写审计，不物理删除、不影响其他日期
    _require_admin(user)
    from apps.accounts.permissions import get_org_or_404

    get_org_or_404(user, record.team.organization_id)
    with transaction.atomic():
        Team.objects.select_for_update().filter(pk=record.team_id).first()
        locked = (
            ScheduleRecord.objects.select_for_update()
            .select_related("employee", "team", "shift_config")
            .get(pk=record.pk)
        )
        reason = record_write_block_reason(locked)
        if reason:
            return {"removed": False, "errors": [reason]}
        before_snapshot = _record_audit_snapshot(locked)
        locked.is_active = False
        locked.save(update_fields=["is_active", "updated_at"])
        after_snapshot = _record_audit_snapshot(locked)
        log_operation(
            user=user,
            action="remove",
            module="schedule",
            target_type="schedule_record",
            target_id=str(locked.pk),
            before_data=before_snapshot,
            after_data=after_snapshot,
            description=(
                f"人工删除排班：{locked.team.name} · {locked.employee.name} · {locked.work_date} · "
                f"{locked.get_shift_type_display()}（原批次 {str(locked.schedule_batch_id)[:8]}）"
            ),
        )
    return {"removed": True, "record_id": locked.pk}


CHANGEABLE_SHIFTS = [
    (ScheduleRecord.ShiftType.EARLY, "早班"),
    (ScheduleRecord.ShiftType.MIDDLE, "中班"),
    (ScheduleRecord.ShiftType.NIGHT, "晚班"),
    (ScheduleRecord.ShiftType.REST, "休息"),
]


def changeable_shift_choices(team) -> list:
    # 可改班目标：该班组已存在配置的早/中/晚班 + 休息（以现有班次配置为准）
    existing = set(team.shift_configs.values_list("shift_name", flat=True))
    return [
        (value, label)
        for value, label in CHANGEABLE_SHIFTS
        if value == ScheduleRecord.ShiftType.REST or value in existing
    ]


def _record_audit_snapshot(record: ScheduleRecord) -> dict:
    # 排班记录审计快照：只记录当时真实存在的字段，供职工详情展示“操作前后状态”。
    return {
        "record_id": record.pk,
        "employee_id": record.employee_id,
        "employee_no": record.employee.employee_no,
        "employee_name": record.employee.name,
        "team": record.team.name,
        "work_date": record.work_date.isoformat(),
        "shift_type": record.shift_type,
        "shift_label": record.get_shift_type_display(),
        "position": record.position or "",
        "status": record.status,
        "is_active": record.is_active,
        "batch_id": str(record.schedule_batch_id),
    }


def change_schedule_record(record: ScheduleRecord, target_shift: str, user) -> dict:
    # 人工改班（P2）：原地更新该职工当天唯一的当前有效记录（保留版本批次号与审计），
    # 不新建记录、不创建批次；历史上同一人同一天因旧逻辑产生的多条有效记录会在本次操作中收敛为一条。
    _require_admin(user)
    from apps.accounts.permissions import get_org_or_404

    get_org_or_404(user, record.team.organization_id)
    labels = dict(CHANGEABLE_SHIFTS)
    if target_shift not in labels:
        return {"changed": False, "errors": ["目标班次不合法。"]}

    with transaction.atomic():
        Team.objects.select_for_update().filter(pk=record.team_id).first()
        current = (
            ScheduleRecord.objects.select_for_update()
            .select_related("employee", "team", "shift_config")
            .get(pk=record.pk)
        )
        reason = record_write_block_reason(current)
        if reason:
            return {"changed": False, "errors": [reason]}
        employee = Employee.objects.select_for_update().get(pk=current.employee_id)
        team = current.team
        configs = {c.shift_name: c for c in team.shift_configs.all()}
        if target_shift != ScheduleRecord.ShiftType.REST and target_shift not in configs:
            return {
                "changed": False,
                "errors": [f"该班组没有{labels.get(target_shift, target_shift)}配置，无法改班。"],
            }

        day_rows = list(
            ScheduleRecord.objects.select_for_update()
            .filter(
                team_id=team.pk,
                employee_id=employee.pk,
                work_date=current.work_date,
                status__in=ACTIVE_RECORD_STATUSES,
                is_active=True,
            )
            .order_by("-created_at", "-id")
        )
        work_rows = [r for r in day_rows if r.shift_type != ScheduleRecord.ShiftType.REST]
        effective = work_rows[0] if work_rows else (day_rows[0] if day_rows else current)
        effective_shift = effective.shift_type
        if effective_shift == target_shift and len(day_rows) == 1:
            return {
                "changed": False,
                "errors": [f"{employee.name} 在 {current.work_date} 已经是{labels[target_shift]}，无需修改。"],
                "noop": True,
            }

        before_snapshot = _record_audit_snapshot(effective)
        original_position = effective.position
        if target_shift == ScheduleRecord.ShiftType.REST:
            new_config = effective.shift_config or team.shift_configs.first()
            new_position = ""
        else:
            new_config = configs[target_shift]
            new_position = _position_for_addition(employee, new_config)
            required = {
                pos
                for pos, count in (new_config.position_requirements or {}).items()
                if isinstance(count, int) and not isinstance(count, bool) and count > 0
            }
            if original_position and effective_shift != ScheduleRecord.ShiftType.REST and original_position in required:
                new_position = original_position  # 同日内改班尽量保留原岗位

        # 收敛：仅保留 effective 一条，其余同日有效记录退休（软删除，保留历史）
        retired = 0
        for row in day_rows:
            if row.pk == effective.pk:
                continue
            row.is_active = False
            row.save(update_fields=["is_active", "updated_at"])
            retired += 1

        # 原地更新（schedule_batch_id 与 created_at 不变，版本关联与追溯保持）
        effective.shift_type = target_shift
        effective.shift_config = new_config
        effective.position = new_position
        effective.status = ScheduleRecord.Status.CONFIRMED
        effective.save(update_fields=["shift_type", "shift_config", "position", "status", "updated_at"])

        active = list(active_records_for_day(team.pk, employee.pk, current.work_date))
        if len(active) != 1:
            logger.error(
                "改班后不变量失败：team=%s employee=%s date=%s 有效记录=%s",
                team.pk, employee.pk, current.work_date, len(active),
            )
            raise InvariantViolation(
                f"改班后 {employee.name} 在 {current.work_date} 存在 {len(active)} 条当前有效记录，已回滚。"
            )
        after_snapshot = _record_audit_snapshot(effective)
        after_snapshot["retired_records"] = retired
        log_operation(
            user=user, action="change", module="schedule",
            target_type="schedule_record", target_id=str(effective.pk),
            before_data=before_snapshot,
            after_data=after_snapshot,
            description=(
                f"人工改班：{team.organization.name}/{team.name} · {employee.name} · {current.work_date} · "
                f"{labels.get(effective_shift, effective_shift)} → {labels[target_shift]}"
                f"（岗位：{original_position or '普工'} → {new_position or '普工'}；"
                f"原批次 {str(current.schedule_batch_id)[:8]}；收敛多余有效记录 {retired} 条）"
            ),
        )
    return {
        "changed": True,
        "record_id": effective.pk,
        "batch_id": str(effective.schedule_batch_id),
        "from_shift": effective_shift,
        "to_shift": target_shift,
    }


def is_period_ended(period_end, today=None) -> bool:
    # 周期是否已结束：结束日早于今天即视为已结束（今天所在的周期永远未结束）
    return period_end < (today or date.today())


def archive_period(team, period_start, period_end, user) -> dict:
    # 周期归档（唯一实现，自动归档与显式归档共用）：
    # 把该队组该周期内当前有效的每日排班记录置为 archived（原地状态变更，不复制、不删除）。
    # 幂等：无可归档记录时返回 archived=0，不写审计、不改变任何数据。
    _require_admin(user)
    from apps.accounts.permissions import get_org_or_404

    get_org_or_404(user, team.organization_id)
    if not is_period_ended(period_end):
        return {
            "archived": 0,
            "already": False,
            "errors": [f"{period_start}~{period_end} 周期尚未结束，不能归档。"],
        }
    with transaction.atomic():
        Team.objects.select_for_update().filter(pk=team.pk).first()
        rows = ScheduleRecord.objects.select_for_update().filter(
            team_id=team.pk,
            work_date__range=(period_start, period_end),
            status__in=ACTIVE_RECORD_STATUSES,
            is_active=True,
        )
        affected = rows.update(status=ScheduleRecord.Status.ARCHIVED, updated_at=timezone.now())
        if affected == 0:
            return {"archived": 0, "already": True, "errors": []}
        log_operation(
            user=user, action="archive", module="schedule",
            target_type="team", target_id=str(team.pk),
            description=(
                f"周期归档：{team.organization.name}/{team.name} · {period_start}~{period_end} · "
                f"归档 {affected} 条最终排班记录（状态置为已归档，记录保留）"
            ),
        )
    return {"archived": affected, "already": False, "errors": []}


def archive_ended_periods_for_team(team, before_date, user) -> dict:
    # 自动归档：把该队组所有“已结束且早于 before_date、尚未归档”的周期一并归档（不止紧邻上一个周期）
    _require_admin(user)
    archived_total = 0
    periods = []
    rows = (
        ScheduleRecord.objects.filter(
            team_id=team.pk,
            status__in=ACTIVE_RECORD_STATUSES,
            is_active=True,
            work_date__lt=before_date,
        )
        .values_list("work_date", flat=True)
        .distinct()
    )
    seen = set()
    for work_date in rows:
        start, end = period_range(work_date)
        if (start, end) in seen:
            continue
        seen.add((start, end))
        periods.append((start, end))
    for start, end in sorted(periods):
        result = archive_period(team, start, end, user)
        if result["errors"]:
            raise RuntimeError(f"自动归档失败（{start}~{end}）：{result['errors'][0]}")
        archived_total += result["archived"]
    return {"archived": archived_total, "periods": len(periods)}


def current_version_batch_id(team_id, work_date):
    # 人工记录沿用的“版本批次号”：该队组覆盖该日期的已确认整周期版本（取最新）；
    # 找不到时返回 None（由调用方决定回退策略，绝不随意清空既有批次号）。
    start, end = period_range(work_date)
    batch = (
        ScheduleBatch.objects.filter(
            team_id=team_id,
            kind=ScheduleBatch.Kind.PERIOD,
            status=ScheduleBatch.Status.CONFIRMED,
            period_start__lte=work_date,
            period_end__gte=work_date,
        )
        .order_by("-confirmed_at", "-id")
        .first()
    )
    return batch.batch_id if batch else None


def active_records_for_day(team_id, employee_id, work_date, *, lock: bool = False):
    # 某职工某天的全部当前有效记录（正常情况下恰好一条；历史遗留可能出现休息+上班两条）
    qs = ScheduleRecord.objects.filter(
        team_id=team_id,
        employee_id=employee_id,
        work_date=work_date,
        status__in=ACTIVE_RECORD_STATUSES,
        is_active=True,
    )
    return qs.select_for_update() if lock else qs


def add_manual_schedule(shift_config, work_date, employee, user) -> dict:
    # 人工添加排班（P2）：直接写入一条当前有效的每日排班记录，不再创建 adjustment 批次。
    # 不变量：同一职工同一天只允许一条当前有效记录（休息也算已安排，须用改班切换）。
    _require_admin(user)
    from apps.accounts.permissions import get_employee_or_404, get_shift_config_or_404

    shift_config = get_shift_config_or_404(user, shift_config.pk)
    employee = get_employee_or_404(user, employee.pk)
    if employee.team_id != shift_config.team_id:
        return {"errors": ["该职工不属于所选班组，不能添加到本班次。"]}
    if employee.shift_config_id != shift_config.pk:
        return {
            "added": False,
            "errors": [
                f"{employee.name} 不属于「{shift_config.get_shift_name_display()}」班次人员，"
                "不能安排到该班次；请先在职工资料中调整其所属班次。"
            ],
        }
    if not employee.is_active:
        return {"errors": ["该职工已停用，不能添加到班次。"]}
    if not isinstance(work_date, date):
        return {"errors": ["日期格式不正确。"]}
    shift_name = shift_config.shift_name
    labels = dict(CHANGEABLE_SHIFTS)

    with transaction.atomic():
        Team.objects.select_for_update().filter(pk=shift_config.team_id).first()
        config = ShiftConfig.objects.select_for_update().get(pk=shift_config.pk)
        locked_employee = Employee.objects.select_for_update().get(pk=employee.pk)

        block = period_write_block_reason(config.team_id, work_date, lock=True)
        if block:
            return {"errors": [block]}

        existing = list(
            active_records_for_day(config.team_id, employee.pk, work_date, lock=True)
        )
        if existing:
            current = existing[0].shift_type
            return {
                "errors": [
                    f"{employee.name} 在 {work_date} 已有排班（{labels.get(current, current)}），"
                    "不能重复添加；如需调整请使用「改班」。"
                ]
            }

        version_batch_id = current_version_batch_id(config.team_id, work_date)
        position = _position_for_addition(locked_employee, config)
        reusable = (
            ScheduleRecord.objects.select_for_update()
            .filter(
                team_id=config.team_id,
                employee_id=employee.pk,
                work_date=work_date,
                schedule_batch_id=version_batch_id,
                status=ScheduleRecord.Status.CONFIRMED,  # 禁止复活 superseded/archived/草稿记录
            )
            .order_by("-id")
            .first()
            if version_batch_id
            else None
        )
        if reusable is not None:
            # 该版本批次内已有同人同日记录（此前被删除，且仍为 confirmed）：原地恢复并更新
            if reusable.schedule_batch_id != version_batch_id:
                return {"errors": ["该记录不属于当前有效版本，不能恢复。"]}
            reusable.shift_type = shift_name
            reusable.shift_config = config
            reusable.position = position
            reusable.status = ScheduleRecord.Status.CONFIRMED
            reusable.is_active = True
            reusable.save(
                update_fields=[
                    "shift_type", "shift_config", "position", "status", "is_active", "updated_at"
                ]
            )
            record = reusable
        else:
            record = ScheduleRecord(
                schedule_batch_id=version_batch_id or new_schedule_batch_id(),
                organization=config.team.organization,
                team=config.team,
                shift_config=config,
                employee=locked_employee,
                work_date=work_date,
                shift_type=shift_name,
                position=position,
                status=ScheduleRecord.Status.CONFIRMED,
            )
            record.full_clean()
            record.save()

        # 写入后不变量校验（同一事务，失败整体回滚）
        active = list(active_records_for_day(config.team_id, employee.pk, work_date))
        if len(active) != 1:
            logger.error(
                "人工添加后不变量失败：team=%s employee=%s date=%s 有效记录=%s",
                config.team_id, employee.pk, work_date, len(active),
            )
            raise InvariantViolation(
                f"写入后 {employee.name} 在 {work_date} 存在 {len(active)} 条当前有效记录，已回滚。"
            )
        log_operation(
            user=user, action="add", module="schedule",
            target_type="schedule_record", target_id=str(record.pk),
            after_data=_record_audit_snapshot(record),
            description=(
                f"人工添加排班：{config.team.organization.name}/{config.team.name} · "
                f"{employee.name} · {work_date} · {labels.get(shift_name, shift_name)}"
                f"（岗位：{record.position or '普工'}；版本批次 {str(record.schedule_batch_id)[:8]}）"
            ),
        )
    return {"record": record, "errors": []}


def validate_adjustment_payload(batch: ScheduleBatch) -> List[str]:
    # 补充排班（加人）明细校验：只允许“个别职工个别日期”的补充，不要求整周期覆盖
    structure_errors = payload_structure_errors(batch)
    if structure_errors:
        return structure_errors
    errors = []
    from apps.personnel.services import shift_employees

    employee_ids = {e.pk for e in shift_employees(batch.shift_config, active_only=False)}
    for item in batch.payload:
        eid = item.get("employee_id")
        work_date = _parse_date(item.get("work_date"))
        if eid not in employee_ids:
            errors.append(f"补充人员 {eid} 不属于本班组。")
            continue
        if not (batch.period_start <= work_date <= batch.period_end):
            errors.append(f"补充日期 {work_date} 不在周期内。")
        if item.get("shift_type") == ScheduleRecord.ShiftType.REST:
            errors.append("补充排班不允许写入休息记录。")
    return errors


def _position_for_addition(employee, config) -> str:
    # 人工添加时的岗位：取本人具备且该班次配置要求的第一个特殊岗位，否则记为普工（空岗位）
    from apps.personnel.services import parse_special_positions

    required = {
        pos
        for pos, count in (config.position_requirements or {}).items()
        if isinstance(count, int) and not isinstance(count, bool) and count > 0
    }
    for position in parse_special_positions(employee.position):
        if position in required:
            return position
    return ""


def _adjustment_conflict_errors(batch: ScheduleBatch) -> List[str]:
    # 补充排班冲突复核：一人一天最多一条活跃记录；每日计划人数不得超过上限
    errors = []
    for item in batch.payload:
        eid = item["employee_id"]
        work_date = _parse_date(item["work_date"])
        if (
            ScheduleRecord.objects.select_for_update()
            .filter(
                employee_id=eid,
                work_date=work_date,
                status__in=ACTIVE_RECORD_STATUSES,
                is_active=True,
            )
            .exclude(shift_type=ScheduleRecord.ShiftType.REST)
            .exclude(schedule_batch_id=batch.batch_id)
            .exists()
        ):
            errors.append(f"职工 {eid} 于 {work_date} 已有排班（上班）记录。")
    return errors


def _confirm_adjustment(batch: ScheduleBatch, config, locked_employees, user) -> dict:
    # 补充排班写入：同一事务内写入正式记录并标记批次已确认（保留可追溯批次）
    written = 0
    for item in batch.payload:
        employee = locked_employees.get(item["employee_id"])
        if employee is None:
            raise PermissionDenied("补充人员不属于本班组，拒绝写入。")
        create_schedule_record(
            schedule_batch_id=batch.batch_id,
            organization=batch.organization,
            team=batch.team,
            shift_config=config,
            employee=employee,
            work_date=_parse_date(item["work_date"]),
            shift_type=item["shift_type"],
            position=item.get("position", ""),
            status=ScheduleRecord.Status.CONFIRMED,
        )
        written += 1
    batch.status = ScheduleBatch.Status.CONFIRMED
    batch.confirmed_at = timezone.now()
    batch.confirmed_by = user
    batch.save(update_fields=["status", "confirmed_at", "confirmed_by", "updated_at"])
    log_operation(
        user=user,
        action="confirm",
        module="schedule",
        target_type="schedule_batch",
        target_id=str(batch.batch_id),
        description=(
            f"确认补充排班（加人）：{batch.team.name} · "
            f"{batch.shift_config.get_shift_name_display()} · {written} 人 "
            f"（{', '.join(i['work_date'] for i in batch.payload)}）"
        ),
    )
    return {
        "confirmed": True,
        "batch_id": str(batch.batch_id),
        "record_count": written,
        "kind": batch.kind,
    }


def payload_structure_errors(batch: ScheduleBatch) -> List[str]:
    # 草稿明细的基础结构 + 日期预检（period 与 adjustment 两分支共用，必须先于周期门禁与业务校验执行）：
    # 保证任何进入 period_range() 或写入逻辑的日期都是“存在且可解析的合法日期”，
    # 从而杜绝缺失日期回退到“今天所在周期”的误判，也避免畸形数据抛异常变成 HTTP 500。
    payload = batch.payload
    if not isinstance(payload, list) or not payload:
        return ["草稿明细为空或格式错误：应为非空列表。"]
    if not isinstance(batch.period_start, date) or not isinstance(batch.period_end, date):
        return ["草稿周期无效：缺少周期起止日期。"]
    errors = []
    for index, item in enumerate(payload):
        label = f"第 {index + 1} 条明细"
        if not isinstance(item, dict):
            errors.append(f"{label}不是有效的数据项（应为键值结构）。")
            continue
        if "work_date" not in item or item["work_date"] in (None, ""):
            errors.append(f"{label}缺少排班日期。")
            continue
        raw_date = item["work_date"]
        try:
            work_date = _parse_date(raw_date)
        except (ValueError, TypeError):
            errors.append(f"{label}的排班日期无法解析：{raw_date!r}。")
            continue
        if not isinstance(work_date, date):
            errors.append(f"{label}的排班日期无效：{raw_date!r}。")
            continue
        if not (batch.period_start <= work_date <= batch.period_end):
            errors.append(
                f"{label}的排班日期 {work_date} 超出草稿周期 "
                f"{batch.period_start}~{batch.period_end}。"
            )
    return errors


def draft_confirm_period_errors(batch: ScheduleBatch) -> List[str]:
    # 草稿确认的周期门禁（显式判定，不依赖 _freshness_errors 的“当前周期”间接屏障）：
    #   archived   → 该周期已归档，拒绝确认（不得改动批次、记录或历史数据）
    #   historical → 该周期已被新版本替换，拒绝把记录写回历史周期
    #   current / empty → 允许（业务规则：已结束但未归档的周期仍可人工维护）
    errors = []
    seen = set()
    for item in batch.payload:
        # 结构预检已保证 work_date 存在且可解析；此处仍防护式处理，绝不把 None 交给 period_range()
        try:
            work_date = _parse_date(item["work_date"] if isinstance(item, dict) else None)
        except (ValueError, TypeError):
            errors.append("草稿明细包含无法解析的排班日期，已阻止确认。")
            continue
        if not isinstance(work_date, date):
            errors.append("草稿明细包含缺失或无效的排班日期，已阻止确认。")
            continue
        period = period_range(work_date)
        if period in seen:
            continue
        seen.add(period)
        state = period_version_state(batch.team_id, period[0], period[1], lock=True)
        if state == "archived":
            errors.append(f"{period[0]}~{period[1]} 周期已归档（只读历史），不能确认排班。")
        elif state == "historical":
            errors.append(
                f"{period[0]}~{period[1]} 周期已被新版本替换，不能确认排班。"
            )
    return errors


def confirm_schedule_draft(batch_id, user, *, supersede_confirmed: bool = False) -> dict:
    # 确认草稿：单事务内完成全部校验与写入，任一步失败整体回滚。
    # 显式锁协议（固定顺序，勿调整）：
    #   0) Team 行（select_for_update：与生成/人工写入/归档统一串行化，避免与
    #      作废替换中“对其他批次加锁”形成环）
    #   1) 批次行（select_for_update，不带 select_related，避免隐式联表锁）
    #   2) ShiftConfig 行（select_for_update：串行化同配置确认，关闭配置编辑竞态）
    #   3) 班组在职职工行（select_for_update：关闭职工状态/资格编辑竞态）
    #   4) 正式排班冲突检查（锁定读 + 班组级范围，见 _conflict_errors）
    # 锁定顺序固定为 Team → 批次 → 配置 → 职工 → 冲突范围，与生成/人工/归档路径一致，无死锁。
    _require_admin(user)
    # 事务外仅做无锁定位（读取队组），事务内按统一锁顺序加锁：Team → 批次 → 配置 → 职工 → 记录
    located = (
        ScheduleBatch.objects.filter(batch_id=batch_id).only("id", "team_id").first()
    )
    if located is None:
        raise PermissionDenied("排班草稿不存在或无权访问。")
    with transaction.atomic():
        Team.objects.select_for_update().filter(pk=located.team_id).first()
        batch = (
            ScheduleBatch.objects.select_for_update()
            .filter(batch_id=batch_id)
            .first()
        )
        if batch is None:
            raise PermissionDenied("排班草稿不存在或无权访问。")
        _require_visible_org(user, batch.organization_id)

        if batch.status != ScheduleBatch.Status.DRAFT:
            return {
                "confirmed": False,
                "batch_id": str(batch.batch_id),
                "errors": ["该批次已确认，不能重复确认。"],
            }

        config = ShiftConfig.objects.select_for_update().get(pk=batch.shift_config_id)
        locked_employees = {
            e.pk: e
            for e in Employee.objects.select_for_update().filter(
                team=batch.team, shift_config_id=batch.shift_config_id, is_active=True
            ).order_by("pk")
        }

        # 基础结构与日期预检：先于周期门禁执行，畸形 payload 一律以业务错误拒绝
        structure_errors = payload_structure_errors(batch)
        if structure_errors:
            return {
                "confirmed": False,
                "batch_id": str(batch.batch_id),
                "errors": ["确认前校验失败："] + structure_errors,
            }

        # 周期门禁：归档/已被替换的周期一律拒绝确认（period 与 adjustment 两条路径共用）
        period_errors = draft_confirm_period_errors(batch)
        if period_errors:
            return {
                "confirmed": False,
                "batch_id": str(batch.batch_id),
                "errors": ["确认前校验失败："] + period_errors,
            }

        if batch.kind == ScheduleBatch.Kind.ADJUSTMENT:
            # 补充排班（加人）：不比对整周期快照，只复核人员/日期/上限与硬规则
            adjustment_errors = validate_adjustment_payload(batch) + _adjustment_conflict_errors(
                batch
            )
            if adjustment_errors:
                return {
                    "confirmed": False,
                    "batch_id": str(batch.batch_id),
                    "errors": ["补充排班确认前校验失败："] + adjustment_errors,
                }
            return _confirm_adjustment(batch, config, locked_employees, user)

        errors = _freshness_errors(batch, config, locked_employees) + validate_batch_payload(
            batch
        )
        if errors:
            return {
                "confirmed": False,
                "batch_id": str(batch.batch_id),
                "errors": ["确认前校验失败："] + errors,
            }

        # 正式排班冲突：默认阻止；仅在用户显式选择「作废旧批次并确认」时，
        # 将同班组同周期的既有已确认批次整体标记为已作废（保留全部记录，不删除），
        # 再写入新版本。任何与自身批次重复、草稿来源或跨范围异常仍一律阻止。
        conflict_report = build_conflict_report(batch)
        if conflict_report["total"] > 0:
            # 默认自动作废旧有效版本（同队组、同周期）；仅当来源全部为已知正式批次时才允许，
            # 否则保持阻止并说明原因，避免误伤其他队组/周期或来源不明的历史记录。
            can_supersede, reason = supersedable_sources(batch, conflict_report)
            if not (supersede_confirmed or can_supersede):
                return {
                    "confirmed": False,
                    "batch_id": str(batch.batch_id),
                    "errors": [
                        f"已有正式排班记录冲突：共 {conflict_report['total']} 条"
                        f"（如职工 {conflict_report['samples'][0]['employee_id']} "
                        f"于 {conflict_report['samples'][0]['work_date']}）。"
                        + (reason or "该冲突无法通过版本替换自动处理。")
                    ],
                    "conflict_report": conflict_report,
                }
            superseded = _supersede_batches(batch, conflict_report, user)
            if not superseded:
                return {
                    "confirmed": False,
                    "batch_id": str(batch.batch_id),
                    "errors": ["存在无法通过作废替换处理的冲突（如与本批次自身重复），已阻止确认。"],
                    "conflict_report": conflict_report,
                }
            # 作废后重新验证：本批次范围内不应再有活跃正式记录
            remaining = _conflict_errors(batch)
            if remaining:
                return {
                    "confirmed": False,
                    "batch_id": str(batch.batch_id),
                    "errors": ["作废替换后仍存在冲突："] + remaining,
                    "conflict_report": build_conflict_report(batch),
                }

        # 展开明细写入正式记录（每条 full_clean；唯一约束兜底防重复）
        for item in batch.payload:
            employee = locked_employees.get(item["employee_id"])
            if employee is None:
                raise PermissionDenied("草稿含本班组之外的职工，拒绝确认。")
            create_schedule_record(
                schedule_batch_id=batch.batch_id,
                organization=batch.organization,
                team=batch.team,
                shift_config=config,
                employee=employee,
                work_date=_parse_date(item["work_date"]),
                shift_type=item["shift_type"],
                position=item.get("position", ""),
                status=ScheduleRecord.Status.CONFIRMED,
            )
        batch.status = ScheduleBatch.Status.CONFIRMED
        batch.confirmed_at = timezone.now()
        batch.confirmed_by = user
        batch.save(update_fields=["status", "confirmed_at", "confirmed_by", "updated_at"])
        log_operation(
            user=user,
            action="confirm",
            module="schedule",
            target_type="schedule_batch",
            target_id=str(batch.batch_id),
            description=(
                f"确认排班草稿 {batch.team.name} · {config.get_shift_name_display()} · "
                f"{batch.period_start}~{batch.period_end}，写入 {len(batch.payload)} 条正式记录"
            ),
        )
    return {
        "confirmed": True,
        "batch_id": str(batch.batch_id),
        "record_count": len(batch.payload),
        "errors": [],
    }
