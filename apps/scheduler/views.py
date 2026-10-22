# 排班管理页面：排班计划、排班结果、班次看板（普通成员只读看板）、排班草稿生成与确认。
import json
from datetime import date, timedelta

from django.contrib import messages
from django.http import Http404
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.auditlog.services import log_operation
from apps.accounts.permissions import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_SUPER,
    get_role,
    get_shift_config_or_404,
    has_role,
    role_required,
    visible_organizations,
)
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee

from . import drafting as drafts_module
from . import selectors
from .drafting import (
    InvariantViolation,
    archive_period,
    add_manual_schedule,
    generate_and_apply,
    change_schedule_record,
    changeable_shift_choices,
    confirm_schedule_draft,
    create_schedule_draft,
    get_schedule_draft,
    remove_schedule_record,
)
from .models import ScheduleBatch, ScheduleRecord


def _resolve_team(request):
    # 解析筛选参数；队组选项限制在当前用户可见范围（数据隔离）。
    # 非超管：不依赖前端选择，自动使用本人所属队组与其第一个班组（不接受越权参数）。
    orgs = visible_organizations(request.user).order_by("name")
    org_id = request.GET.get("organization", "")
    team_id = request.GET.get("team", "")
    is_super = has_role(request.user, ROLE_SUPER)
    org = orgs.filter(pk=org_id).first() if org_id.isdigit() else None
    if org is None and not is_super:
        org = orgs.first()
    teams = org.teams.order_by("name") if org else Team.objects.none()
    team = teams.filter(pk=team_id).first() if (team_id.isdigit() and org) else None
    if team is None and not is_super and org:
        team = teams.first()
    return orgs, org, teams, team


@role_required(ROLE_ADMIN)
def schedule_plan(request):
    start, end = selectors.period_range()
    parsed = selectors.parse_period_param(request.GET.get("period", ""))
    if parsed:
        start, end = parsed
    orgs, org, teams, team = _resolve_team(request)

    context = {
        "start": start,
        "end": end,
        "prev_period": f"{selectors._add_months(start, -1):%Y-%m}",
        "next_period": f"{selectors._add_months(start, 1):%Y-%m}",
        "orgs": orgs,
        "org": org,
        "teams": teams,
        "team": team,
        "is_super": has_role(request.user, ROLE_SUPER),
    }
    if team:
        configs = list(team.shift_configs.order_by("shift_name"))
        active_configs = [c for c in configs if c.is_active]
        inactive_configs = [c for c in configs if not c.is_active]
        shift_names = {c.shift_name for c in configs}
        missing_config_names = [
            label
            for value, label in (("early", "早班"), ("middle", "中班"), ("night", "晚班"))
            if value not in shift_names
        ]
        batches = selectors.batch_summaries(team, start, end)
        status_distribution = {}
        for batch in batches:
            for status, count in batch["statuses"].items():
                status_distribution[status] = status_distribution.get(status, 0) + count
        context.update(
            {
                "configs": configs,
                "active_configs": active_configs,
                "inactive_configs": inactive_configs,
                "missing_config_names": missing_config_names,
                "employee_count": team.employees.filter(is_active=True).count(),
                "employee_total": team.employees.count(),
                "batches": batches,
                "status_distribution": status_distribution,
            }
        )
    return render(request, "scheduler/plan.html", context)


@role_required(ROLE_ADMIN)
@require_POST
def shift_board_remove(request, record_id):
    # 人工删除排班记录（软删除 + 审计），删除后回到来源页面并展示最新数据库数据
    from apps.accounts.permissions import get_org_or_404

    record = (
        ScheduleRecord.objects.select_related("team", "employee")
        .filter(pk=record_id)
        .first()
    )
    if record is None:
        raise Http404("排班记录不存在。")
    get_org_or_404(request.user, record.team.organization_id)
    result = remove_schedule_record(record, request.user)
    if result["removed"]:
        messages.success(
            request,
            f"已删除排班：{record.employee.name} · {record.work_date} · {record.get_shift_type_display()}。",
        )
    else:
        for error in result["errors"]:
            messages.error(request, error)
    next_url = request.POST.get("next", "")
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect("scheduler:shift_board")


@role_required(ROLE_ADMIN)
def schedule_results(request):
    start, end = selectors.period_range()
    parsed = selectors.parse_period_param(request.GET.get("period", ""))
    if parsed:
        start, end = parsed
    orgs, org, teams, team = _resolve_team(request)

    grid = None
    grid_is_current_only = False
    batches = []
    selected_batch = None
    batch_id = request.GET.get("batch", "")
    if team:
        batches = selectors.batch_summaries(team, start, end)
        if batch_id:
            # 显式指定批次：允许查看历史批次原貌（active_only=False）
            selected_batch = next(
                (b for b in batches if str(b["batch_id"]) == batch_id), None
            )
        else:
            # 默认只展示当前有效版本；没有有效版本时保持空白，避免把历史数据当作当前排班
            selected_batch = next((b for b in batches if b["is_current"]), None)
        if selected_batch:
            grid = selectors.build_schedule_grid(
                team,
                selected_batch["batch_id"],
                start,
                end,
                active_only=selected_batch["is_current"],
            )
        elif not batch_id and selectors.active_schedule_records(
            team=team, start=start, end=end
        ).exists():
            # 兼容：该周期存在当前有效记录但缺少版本头（历史/外部写入）→ 仍按当前有效口径展示
            grid = selectors.build_schedule_grid(
                team, None, start, end, active_only=True
            )
            grid_is_current_only = True

    # 原生列表直接交给 json_script（避免双重编码导致浏览器端解析成字符串）
    dates_json = [
        {"day": d.day, "weekday": "周" + selectors.WEEKDAY_LABELS[d.weekday()]}
        for d in selectors.iter_dates(start, end)
    ]
    return render(
        request,
        "scheduler/results.html",
        {
            "start": start,
            "end": end,
            "prev_period": f"{selectors._add_months(start, -1):%Y-%m}",
            "next_period": f"{selectors._add_months(start, 1):%Y-%m}",
            "orgs": orgs,
            "org": org,
            "teams": teams,
            "team": team,
            "batches": batches,
            "selected_batch": selected_batch,
            "grid": grid,
            "grid_is_current_only": grid_is_current_only,
            "dates_json": dates_json,
            "status_labels": dict(ScheduleRecord.Status.choices),
            "today": date.today(),
            "is_super": has_role(request.user, ROLE_SUPER),
        },
    )



def _safe_next(request, fallback_name, **kwargs):
    # 仅允许站内相对路径回跳（防开放重定向），否则回到指定页面
    target = request.POST.get("next", "") or request.GET.get("next", "")
    if target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect(fallback_name, **kwargs)

@role_required(ROLE_MEMBER)
def shift_board(request):
    # 班次看板（日历网格）：非超管自动使用本人所属队组（不接受队组参数切换），超管可切换。
    start, end = selectors.period_range()
    parsed = selectors.parse_period_param(request.GET.get("period", ""))
    if parsed:
        start, end = parsed
    is_super = has_role(request.user, ROLE_SUPER)
    orgs, org, teams, team = _resolve_team(request)
    team_id = request.GET.get("team", "")
    if not is_super:
        org = visible_organizations(request.user).first()
        team = org.teams.filter(pk=team_id).first() if (org and team_id.isdigit()) else None
    else:
        team = Team.objects.filter(pk=team_id).first() if team_id.isdigit() else None
        if team is not None:
            org = team.organization
        elif org is None:
            org = orgs.first()
    teams = org.teams.order_by("name") if org else Team.objects.none()

    is_archive_view = selectors.period_is_archived(start, end, org=org, team=team)
    board = selectors.build_shift_board(start, end, org=org, team=team, archived=is_archive_view)
    return render(
        request,
        "scheduler/shift_board.html",
        {
            "start": start,
            "end": end,
            "prev_period": f"{selectors._add_months(start, -1):%Y-%m}",
            "next_period": f"{selectors._add_months(start, 1):%Y-%m}",
            "orgs": orgs,
            "org": org,
            "teams": teams,
            "team": team,
            "board": board,
            "has_any": any(
                shift["total"] for day in board["days"] for shift in day["shifts"]
            ),
            "is_super": is_super,
            "today": date.today(),
            "is_archive_view": is_archive_view,
            "archive_allowed": (
                has_role(request.user, ROLE_ADMIN) and org is not None and end < date.today()
                and not is_archive_view
            ),
            "can_edit": has_role(request.user, ROLE_ADMIN) and not is_archive_view,
        },
    )


def _board_scope(request):
    # 看板相关页面的队组/班组范围：非超管强制本人队组
    is_super = has_role(request.user, ROLE_SUPER)
    team_id = request.GET.get("team", "")
    team = None
    if is_super:
        orgs, org, _teams, _team = _resolve_team(request)
        if team_id.isdigit():
            team = Team.objects.filter(pk=team_id).first()
        if team is not None:
            org = team.organization
        elif org is None:
            org = orgs.first()
    else:
        org = visible_organizations(request.user).first()
        if org is not None and team_id.isdigit():
            team = org.teams.filter(pk=team_id).first()
    teams = org.teams.order_by("name") if org else Team.objects.none()
    return is_super, org, teams, team


@role_required(ROLE_MEMBER)
def shift_board_detail(request, work_date, shift_name):
    # 某日某班次人员详情：明确区分计划人员名单与实际出勤名单；管理员可进入加人页。
    from datetime import date as date_cls

    try:
        target_date = date_cls.fromisoformat(work_date)
    except ValueError:
        raise Http404("日期格式不正确。")
    labels = dict(selectors.SHIFT_BOARD_ORDER)
    if shift_name not in labels:
        raise Http404("班次不存在。")
    is_super, org, teams, team = _board_scope(request)
    period_start, period_end = selectors.period_range(target_date)
    is_archive_view = selectors.period_is_archived(
        period_start, period_end, org=org, team=team
    )
    rows, groups = selectors.shift_board_day_detail(
        target_date, shift_name, org=org, team=team, archived=is_archive_view
    )
    can_edit = has_role(request.user, ROLE_ADMIN)
    return render(
        request,
        "scheduler/shift_board_detail.html",
        {
            "work_date": target_date,
            "shift_name": shift_name,
            "shift_label": labels[shift_name],
            "rows": rows,
            "groups": groups,
            "org": org,
            "team": team,
            "teams": teams,
            "is_super": is_super,
            "can_edit": can_edit,
            "total": len(rows),
            "shift_choices": (
                changeable_shift_choices(team or org.teams.first())
                if (team or (org and org.teams.exists()))
                else []
            ),
        },
    )


@role_required(ROLE_ADMIN)
@require_POST
def shift_board_change(request, record_id):
    # 人工改班（POST）：仅管理员、仅本队组；改班后回到来源页面并展示最新数据库数据
    from apps.accounts.permissions import get_org_or_404

    record = (
        ScheduleRecord.objects.select_related("team", "employee")
        .filter(pk=record_id)
        .first()
    )
    if record is None:
        raise Http404("排班记录不存在。")
    get_org_or_404(request.user, record.team.organization_id)
    target_shift = request.POST.get("target_shift", "")
    try:
        result = change_schedule_record(record, target_shift, request.user)
    except InvariantViolation as exc:
        log_operation(
            user=request.user, action="invariant_blocked", module="schedule",
            target_type="schedule_record", target_id=str(record.pk),
            description=f"改班被回滚：{exc}",
        )
        messages.error(request, f"操作未生效（已回滚）：{exc}")
        return redirect("scheduler:shift_board")
    if result.get("changed"):
        labels = dict(drafting_labels())
        messages.success(
            request,
            f"已改班：{record.employee.name} · {record.work_date} · "
            f"{labels.get(result['from_shift'], result['from_shift'])} → "
            f"{labels.get(result['to_shift'], result['to_shift'])}。",
        )
    else:
        level = messages.info if result.get("noop") else messages.error
        for error in result["errors"]:
            level(request, error)
    next_url = request.POST.get("next", "")
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect("scheduler:shift_board")


def drafting_labels():
    from .drafting import CHANGEABLE_SHIFTS

    return CHANGEABLE_SHIFTS


@role_required(ROLE_ADMIN)
@require_POST
def schedule_archive_period(request, team_id):
    # 显式归档：归档本人队组中已结束的指定周期（与自动归档共用 draftings.archive_period）
    from datetime import date as date_cls

    from apps.accounts.permissions import get_org_or_404

    team = Team.objects.filter(pk=team_id).first()
    if team is None:
        raise Http404("班组不存在。")
    get_org_or_404(request.user, team.organization_id)
    parsed = selectors.parse_period_param(request.POST.get("period", ""))
    # 说明：team_id 为“班组 ID”（URL 由看板的当前班组传入），不是队组 ID
    if not parsed:
        messages.error(request, "请选择要归档的排班周期。")
        return _safe_next(request, "scheduler:shift_board")
    start, end = parsed
    result = archive_period(team, start, end, request.user)
    if result["errors"]:
        for error in result["errors"]:
            messages.error(request, error)
    elif result["already"]:
        messages.info(request, f"{start}~{end} 周期已归档，无需重复操作。")
    else:
        messages.success(
            request,
            f"已归档 {team.name} {start}~{end} 周期共 {result['archived']} 条最终排班记录。",
        )
    return _safe_next(request, "scheduler:shift_board")


@role_required(ROLE_ADMIN)
def shift_board_add(request, work_date, shift_name):
    # 班次看板「添加人员」：候选为当天未上班职工（按班组分组）；
    # 提交后经“补充排班草稿 → 确认”流程写入正式记录（事务 + 审计 + 硬规则校验）。
    from datetime import date as date_cls

    try:
        target_date = date_cls.fromisoformat(work_date)
    except ValueError:
        raise Http404("日期格式不正确。")
    labels = dict(selectors.SHIFT_BOARD_ORDER)
    if shift_name not in labels:
        raise Http404("班次不存在。")
    is_super, org, teams, team = _board_scope(request)
    if org is None:
        raise PermissionDenied("您的账号未绑定有效队组，请联系超级管理员配置。")
    # 加人页默认展示“整个队组当天未排班人员，按班组分组”；
    # team 参数仅保留兼容（显式指定时收窄），点击某个班组的 ＋ 只传 focus 用于高亮。
    focus_team_id = request.GET.get("focus", "")
    if focus_team_id.isdigit():
        focus_team = teams.filter(pk=focus_team_id).first()
    else:
        focus_team = team
    keyword = request.GET.get("q", "").strip()

    # 同一班次在队组内每个班组各有一条配置；添加时以“职工自己的班次配置”为目标，
    # 不同班次人员互相隔离（早班人员不会被加进中班）。
    configs = list(
        ShiftConfig.objects.filter(
            team__organization=org, shift_name=shift_name, is_active=True
        )
        .select_related("team")
        .order_by("team__name")
    )
    if team is not None:
        configs = [item for item in configs if item.team_id == team.pk]
    if not configs:
        messages.error(request, f"该队组没有启用中的{labels[shift_name]}配置，无法添加人员。")
        return redirect("scheduler:shift_board")
    config = configs[0]

    if request.method == "POST":
        employee_id = request.POST.get("employee_id", "")
        employee = Employee.objects.filter(pk=employee_id).first() if employee_id.isdigit() else None
        if employee is None:
            messages.error(request, "请选择要添加的职工。")
        else:
            target_config = employee.shift_config
            if target_config is None or target_config.shift_name != shift_name:
                messages.error(
                    request,
                    f"{employee.name} 不属于「{labels[shift_name]}」班次人员，不能添加到该班次；"
                    "请先在职工资料中调整其所属班次。",
                )
                return redirect(
                    f"{reverse('scheduler:shift_board_detail', args=[work_date, shift_name])}"
                    f"?team={employee.team_id}"
                )
            if not target_config.is_active:
                messages.error(
                    request,
                    f"{employee.name} 所属的「{labels[shift_name]}」配置未启用，无法添加。",
                )
                return redirect("scheduler:shift_board")
            try:
                result = add_manual_schedule(target_config, target_date, employee, request.user)
            except InvariantViolation as exc:
                log_operation(
                    user=request.user, action="invariant_blocked", module="schedule",
                    target_type="team", target_id=str(config.team_id),
                    description=f"人工添加被回滚：{exc}",
                )
                messages.error(request, f"操作未生效（已回滚）：{exc}")
                return redirect("scheduler:shift_board")
            if result["errors"]:
                for error in result["errors"]:
                    messages.error(request, error)
            else:
                messages.success(
                    request,
                    f"已在 {target_date} {labels[shift_name]} 添加 {employee.name}（"
                    f"{employee.team.name}）。",
                )
                return redirect(
                    f"{reverse('scheduler:shift_board_detail', args=[work_date, shift_name])}"
                    f"?team={employee.team_id}"
                )

    candidates = selectors.board_candidates(org, target_date, team=team, shift_name=shift_name)
    if keyword:
        lowered = keyword.lower()
        filtered = []
        for group in candidates:
            people = [
                person for person in group["people"]
                if lowered in person["name"].lower()
                or lowered in person["employee_no"].lower()
                or lowered in (person["position"] or "").lower()
            ]
            if people:
                filtered.append({**group, "people": people})
        candidates = filtered
    candidate_count = sum(len(group["people"]) for group in candidates)
    return render(
        request,
        "scheduler/shift_board_add.html",
        {
            "work_date": target_date,
            "shift_name": shift_name,
            "shift_label": labels[shift_name],
            "config": config,
            "org": org,
            "team": team,
            "teams": teams,
            "candidates": candidates,
            "candidate_count": candidate_count,
            "focus_team": focus_team,
            "keyword": keyword,
        },
    )


@role_required(ROLE_ADMIN)
@require_POST
def schedule_generate(request, pk):
    # 生成即保存：求解 + 独立校验通过后直接写入并立即生效；失败时旧排班完整保留
    shift_config = get_shift_config_or_404(request.user, pk)
    parsed = selectors.parse_period_param(request.POST.get("period", ""))
    anchor = parsed[0] if parsed else None
    outcome = generate_and_apply(shift_config, request.user, anchor=anchor)
    if not outcome["applied"]:
        for error in outcome["errors"]:
            messages.error(request, error)
        return _safe_next(request, "scheduler:schedule_plan")
    batch = outcome["batch"]
    messages.success(
        request,
        f"排班已生成并保存：{shift_config.team.name} · {shift_config.get_shift_name_display()} · "
        f"{batch.period_start}~{batch.period_end}，写入 {outcome['record_count']} 条"
        + (f"，替换旧有效排班 {outcome['retired']} 条。" if outcome["retired"] else "。"),
    )
    # 成功跳转：按本次生成返回的批次号定位结果页（团队 + 周期 + 批次），
    # 不依赖“最新创建时间”猜测，也不会指向旧批次/草稿/已被替代的批次。
    from urllib.parse import urlencode

    query = urlencode(
        {
            "organization": batch.organization_id,
            "team": batch.team_id,
            "period": f"{batch.period_start:%Y-%m}",
            "batch": str(batch.batch_id),
        }
    )
    return redirect(f"{reverse('scheduler:schedule_results')}?{query}")


@role_required(ROLE_ADMIN)
def schedule_draft_detail(request, batch_id):
    # 排班草稿详情：展示每日人员/岗位/班数/休息质量，供校验后确认保存
    draft = get_schedule_draft(batch_id, request.user)
    conflict_report = None
    if request.method == "POST" and request.POST.get("action") in (
        "confirm",
        "confirm_supersede",
    ):
        supersede = request.POST.get("action") == "confirm_supersede"
        result = confirm_schedule_draft(
            batch_id, request.user, supersede_confirmed=supersede
        )
        if result["confirmed"]:
            messages.success(
                request,
                f"排班已确认保存：批次 {result['batch_id']}，写入 {result['record_count']} 条正式记录。",
            )
            return redirect("scheduler:schedule_results")
        for error in result["errors"]:
            messages.error(request, error)
        if result.get("conflict_report"):
            # 存在冲突：停留在本页展示冲突明细与「作废替换」入口
            conflict_report = result["conflict_report"]
        else:
            return redirect("scheduler:schedule_draft_detail", batch_id=batch_id)

    dates = [
        draft.period_start + timedelta(days=i)
        for i in range((draft.period_end - draft.period_start).days + 1)
    ]
    employees_by_id = {e.pk: e for e in Employee.objects.filter(team=draft.team)}
    payload_by_emp = {}
    for item in draft.payload:
        payload_by_emp.setdefault(item["employee_id"], {})[item["work_date"]] = item
    rows = []
    for eid_str, target in draft.employee_targets.items():
        eid = int(eid_str)
        cells = []
        for current in dates:
            item = payload_by_emp.get(eid, {}).get(current.isoformat())
            cells.append(item or {"shift_type": ScheduleRecord.ShiftType.REST, "position": ""})
        actual = sum(1 for cell in cells if cell["shift_type"] != ScheduleRecord.ShiftType.REST)
        rows.append(
            {
                "employee": employees_by_id.get(eid),
                "employee_no": employees_by_id.get(eid).employee_no if employees_by_id.get(eid) else eid,
                "name": employees_by_id.get(eid).name if employees_by_id.get(eid) else f"职工#{eid}",
                "cells": cells,
                "target": target,
                "actual": actual,
            }
        )
    return render(
        request,
        "scheduler/schedule_draft_detail.html",
        {
            "draft": draft,
            "dates": dates,
            "rows": rows,
            "summary": draft.summary,
            "conflict_report": conflict_report,
            "position_requirements": draft.position_requirements,
            "shift_labels": dict(ScheduleRecord.ShiftType.choices),
        },
    )
