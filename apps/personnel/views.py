# 人员管理页面：职工、职工导入、个人信息看板（普通成员只读看板）、个人周期日历。
import secrets
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.auditlog.models import OperationLog
from apps.accounts.permissions import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_SUPER,
    get_employee_or_404,
    get_organization,
    get_role,
    has_role,
    role_required,
    visible_organizations,
)
from apps.auditlog.services import log_operation
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.scheduler.drafting import changeable_shift_choices
from apps.scheduler.selectors import (
    active_schedule_records,
    archived_records,
    period_is_archived,
    period_anchor_bounds,
    period_scheduled_counts,
)
from apps.scheduler.models import ScheduleRecord
from apps.scheduler.selectors import parse_period_param, period_range
from apps.scheduler.selectors import period_range

from . import services
from .forms import EmployeeForm, EmployeeImportFileForm
from .importers import ImportFormatError, parse_import_file, preview_import_rows
from .models import Employee
from .services import ImportConflictError, import_employees

ALLOWED_SORTS = {
    "employee_no",
    "-employee_no",
    "name",
    "-name",
    "worked_shifts",
    "-worked_shifts",
    "min_required_shifts",
    "-min_required_shifts",
}


def _back_to(request, fallback_name):
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(fallback_name)


@role_required(ROLE_ADMIN)
def employee_list(request):
    q = request.GET.get("q", "").strip()
    org_id = request.GET.get("organization", "")
    team_id = request.GET.get("team", "")
    shift_id = request.GET.get("shift_config", "")
    status = request.GET.get("status", "")
    sort = request.GET.get("sort", "employee_no")
    if sort not in ALLOWED_SORTS:
        sort = "employee_no"

    orgs = visible_organizations(request.user)
    employees = Employee.objects.filter(team__organization__in=orgs).select_related(
        "team__organization", "shift_config"
    ).order_by(sort)
    if q:
        employees = employees.filter(
            Q(name__icontains=q) | Q(employee_no__icontains=q) | Q(position__icontains=q)
        )
    if org_id and orgs.filter(pk=org_id).exists():
        employees = employees.filter(team__organization_id=org_id)
    if team_id:
        employees = employees.filter(team_id=team_id)
    if shift_id:
        employees = employees.filter(shift_config_id=shift_id)
    if status == "active":
        employees = employees.filter(is_active=True)
    elif status == "inactive":
        employees = employees.filter(is_active=False)

    paginator = Paginator(employees, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    scheduled_counts = _page_scheduled_counts(list(page_obj))

    teams = Team.objects.filter(organization__in=orgs).order_by("name")
    if org_id and orgs.filter(pk=org_id).exists():
        teams = teams.filter(organization_id=org_id)
    shift_configs = ShiftConfig.objects.filter(team__organization__in=orgs).order_by("shift_name")
    if team_id:
        shift_configs = shift_configs.filter(team_id=team_id)

    return render(
        request,
        "personnel/employee_list.html",
        {
            "page_obj": page_obj,
            "q": q,
            "org_id": org_id,
            "team_id": team_id,
            "shift_id": shift_id,
            "status": status,
            "sort": sort,
            "orgs": orgs.order_by("name"),
            "teams": teams,
            "shift_configs": shift_configs,
            "scheduled_counts": scheduled_counts,
        },
    )


@role_required(ROLE_ADMIN)
def employee_create(request):
    form = EmployeeForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        employee = services.create_employee(**form.cleaned_data)
        log_operation(
            user=request.user,
            action="create",
            module="employee",
            target_type="employee",
            target_id=employee.pk,
            description=f"新增职工 {employee.name}（{employee.employee_no}）",
        )
        messages.success(request, f"职工「{employee.name}（{employee.employee_no}）」已创建。")
        return redirect("personnel:employee_detail", pk=employee.pk)
    return render(request, "personnel/employee_form.html", {"form": form, "editing": False})


@role_required(ROLE_ADMIN)
def employee_edit(request, pk):
    employee = get_employee_or_404(request.user, pk)
    form = EmployeeForm(request.POST or None, instance=employee, user=request.user)
    if request.method == "POST" and form.is_valid():
        services.update_employee(employee, **form.cleaned_data)
        log_operation(
            user=request.user,
            action="edit",
            module="employee",
            target_type="employee",
            target_id=employee.pk,
            description=f"编辑职工 {employee.name}（{employee.employee_no}）",
        )
        messages.success(request, f"职工「{employee.name}」已更新。")
        return redirect("personnel:employee_detail", pk=employee.pk)
    return render(
        request,
        "personnel/employee_form.html",
        {"form": form, "editing": True, "employee": employee},
    )


@role_required(ROLE_ADMIN)
def employee_detail(request, pk):
    employee = get_employee_or_404(request.user, pk)
    start_period, end_period = period_range()
    scheduled_counts = period_scheduled_counts(
        start_period, end_period, team=employee.team, employees=[employee]
    )
    records = employee.schedule_records.select_related("shift_config").order_by("-work_date")[:10]
    operations = _employee_operations(employee)
    return render(
        request,
        "personnel/employee_detail.html",
        {
            "employee": employee,
            "scheduled_counts": scheduled_counts,
            "records": records,
            "operations": operations,
            "record_count": employee.schedule_records.count(),
            "met_requirement": employee.min_required_shifts > 0
            and employee.worked_shifts >= employee.min_required_shifts,
        },
    )


@role_required(ROLE_ADMIN)
@require_POST
def employee_toggle(request, pk):
    employee = get_employee_or_404(request.user, pk)
    if employee.is_active:
        services.deactivate_employee(employee)
        messages.success(request, f"职工「{employee.name}」已停用，其历史排班记录仍可查询。")
    else:
        employee.is_active = True
        employee.save(update_fields=["is_active"])
        messages.success(request, f"职工「{employee.name}」已重新启用。")
    return _back_to(request, "personnel:employee_list")


# ============================ 个人信息看板（普通成员只读） ============================



ACTION_LABELS = {
    "generate": "生成并保存",
    "replace": "整周期替换",
    "change": "改班",
    "add": "添加",
    "remove": "取消",
    "supersede": "作废旧版本",
    "archive": "归档",
}


def _format_audit_state(data):
    # 把审计快照格式化为可读文本；缺失时明确标注“未记录”，不伪造历史
    if not data:
        return "—（历史日志未记录）"
    shift = data.get("shift_label") or data.get("shift_type") or ""
    position = data.get("position") or "普工"
    status = data.get("status") or ""
    if data.get("scope") in ("period", "replace"):
        parts = [f"整批 {data.get('record_count', 0)} 条", shift]
        if data.get("retired_records"):
            parts.append(f"退休旧记录 {data['retired_records']} 条")
        return " · ".join(part for part in parts if part)
    parts = [shift, position, status]
    if data.get("is_active") is False:
        parts.append("已取消")
    return " · ".join(part for part in parts if part) or "—"


def _employee_operations(employee, limit: int = 30):
    # 该职工的真实排班操作记录：复用 OperationLog（schedule 模块），
    # 通过“记录 ID / 批次 ID / 结构化 employee_id”关联，绝不从当前排班结果反推。
    from django.db.models import Q

    record_ids = [str(pk) for pk in employee.schedule_records.values_list("id", flat=True)]
    batch_ids = [
        str(bid) for bid in employee.schedule_records.values_list("schedule_batch_id", flat=True).distinct()
    ]
    if not record_ids and not batch_ids:
        return []
    query = Q()
    if record_ids:
        query |= Q(target_type="schedule_record", target_id__in=record_ids)
    if batch_ids:
        query |= Q(target_type="schedule_batch", target_id__in=batch_ids)
    query |= Q(after_data__employee_id=employee.pk)
    query |= Q(before_data__employee_id=employee.pk)
    logs = (
        OperationLog.objects.filter(module="schedule")
        .filter(query)
        .select_related("user")
        .order_by("-created_at")[:limit]
    )
    operations = []
    for log in logs:
        before = log.before_data or {}
        after = log.after_data or {}
        structured = bool(before or after)
        work_date = after.get("work_date") or before.get("work_date") or ""
        batch_id = after.get("batch_id") or before.get("batch_id") or ""
        if not batch_id and log.target_type == "schedule_batch":
            batch_id = log.target_id
        operations.append(
            {
                "created_at": log.created_at,
                "action": ACTION_LABELS.get(log.action, log.action),
                "work_date": work_date,
                "before": _format_audit_state(before),
                "after": _format_audit_state(after),
                "operator": log.username or (log.user.username if log.user else "-"),
                "batch": (batch_id or log.target_id or "")[:8],
                "description": log.description,
                "structured": structured,
                "record_id": log.target_id if log.target_type == "schedule_record" else "",
            }
        )
    return operations


@role_required(ROLE_MEMBER)
def person_board(request):
    # 超级管理员使用职工管理页，此看板面向队组管理员与普通成员
    if get_role(request.user) == ROLE_SUPER:
        return redirect("personnel:employee_list")
    orgs = visible_organizations(request.user)
    q = request.GET.get("q", "").strip()
    employees = Employee.objects.filter(team__organization__in=orgs).select_related(
        "team", "shift_config"
    ).order_by("employee_no")
    if q:
        employees = employees.filter(Q(name__icontains=q) | Q(position__icontains=q))
    paginator = Paginator(employees, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    scheduled_counts = _page_scheduled_counts(list(page_obj))
    return render(
        request,
        "personnel/person_board.html",
        {"page_obj": page_obj, "q": q, "org": get_organization(request.user), "scheduled_counts": scheduled_counts},
    )


# ============================ 职工导入 ============================

@role_required(ROLE_ADMIN)
def employee_import(request):
    # 上传解析与预览；全部校验通过后暂存到会话，等待用户二次确认再整批写入。
    results = None
    file_info = None
    truncated = False
    import_token = None
    form = EmployeeImportFileForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and request.POST.get("action") == "confirm":
        return _confirm_import(request)
    if request.method == "POST" and form.is_valid():
        uploaded = request.FILES["file"]
        file_info = {"name": uploaded.name, "size": uploaded.size}
        try:
            rows, truncated = parse_import_file(uploaded)
            # 队组管理员只能校验本队组班组的行数据
            allowed_org = (
                None if get_role(request.user) == ROLE_SUPER else get_organization(request.user)
            )
            results = preview_import_rows(rows, allowed_org=allowed_org)
            if results and not results["failed"] and results["ok"]:
                import_token = secrets.token_urlsafe(16)
                request.session["employee_import_rows"] = {
                    "token": import_token,
                    "rows": rows,
                }
        except ImportFormatError as exc:
            messages.error(request, f"文件解析失败：{exc}")
    return render(
        request,
        "personnel/import.html",
        {
            "form": form,
            "results": results,
            "file_info": file_info,
            "truncated": truncated,
            "import_token": import_token,
        },
    )


def _confirm_import(request):
    # 确认导入：取出会话中的已校验行（防重放），重新整批校验后事务写入。
    token = request.POST.get("import_token", "")
    stash = request.session.pop("employee_import_rows", None)
    if not token or not stash or stash.get("token") != token:
        messages.error(request, "导入确认数据已失效，请重新上传并预览。")
        return redirect("personnel:employee_import")
    rows = stash["rows"]
    allowed_org = (
        None if get_role(request.user) == ROLE_SUPER else get_organization(request.user)
    )
    results = preview_import_rows(rows, allowed_org=allowed_org)
    if results["failed"]:
        messages.error(
            request,
            f"确认前重新校验未通过（{len(results['failed'])} 行有问题），未写入任何职工数据。请重新上传。",
        )
        return redirect("personnel:employee_import")
    try:
        created = import_employees(rows, allowed_org=allowed_org)
    except ImportConflictError as exc:
        messages.error(request, f"导入失败：{exc}")
        return redirect("personnel:employee_import")
    except ValidationError as exc:
        messages.error(request, f"导入失败（数据校验）：{'；'.join(exc.messages)}")
        return redirect("personnel:employee_import")
    log_operation(
        user=request.user,
        action="import",
        module="employee",
        target_type="employee",
        target_id="batch",
        description=f"批量导入 {created} 名职工（整批校验通过后确认写入）",
    )
    messages.success(request, f"已导入 {created} 名职工。")
    return redirect("personnel:employee_list")


@role_required(ROLE_ADMIN)
def import_template(request):
    content = (
        "队组编号,班组编号,职工编号,姓名,岗位,班次,已上班数,矿上应上最少班数\n"
        "ORG-01,T01-01,E001,张三,值班员,early,15,18\n"
    )
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="employee_import_template.csv"'
    return response


# ============================ 个人周期日历（严格按排班周期：25 日 ~ 次月 24 日） ============================

@role_required(ROLE_MEMBER)
def employee_calendar(request, pk):
    # 个人排班日历：按排班周期（每月 25 日至次月 24 日）展示该职工的排班记录，管理员可删除单条记录。
    employee = get_employee_or_404(request.user, pk)
    today = date.today()
    focus_date = None
    focus_raw = request.GET.get("date", "")
    if focus_raw:
        try:
            focus_date = date.fromisoformat(focus_raw)
        except ValueError:
            focus_date = None
    period_param = parse_period_param(request.GET.get("period", ""))
    anchor = period_param[0] if period_param else (focus_date or today)
    start, end, prev_anchor, next_anchor = period_anchor_bounds(anchor)

    is_archive_view = period_is_archived(start, end, team=employee.team)
    records = {}
    source = (
        archived_records(team=employee.team, start=start, end=end)
        if is_archive_view
        else active_schedule_records(team=employee.team, start=start, end=end)
    )
    for record in source:
        if record.employee_id != employee.pk:
            continue
        records.setdefault(record.work_date, record)

    from apps.scheduler.selectors import WEEKDAY_LABELS

    days = []
    current = start
    while current <= end:
        record = records.get(current)
        days.append(
            {
                "date": current,
                "weekday": "周" + WEEKDAY_LABELS[current.weekday()],
                "record": record,
                "shift_label": record.get_shift_type_display() if record else "",
                "shift_type": record.shift_type if record else "",
                "position": record.position if record else "",
            }
        )
        current += timedelta(days=1)

    scheduled = sum(1 for day in days if day["record"] and day["shift_type"] != ScheduleRecord.ShiftType.REST)
    return render(
        request,
        "personnel/employee_calendar.html",
        {
            "employee": employee,
            "today": today,
            "period_start": start,
            "period_end": end,
            "days": days,
            "leading_blanks": range(start.weekday()),
            "can_edit": has_role(request.user, ROLE_ADMIN) and not is_archive_view,
            "is_archive_view": is_archive_view,
            "prev_period": prev_anchor.strftime("%Y-%m"),
            "next_period": next_anchor.strftime("%Y-%m"),
            "is_current_period": start <= today <= end,
            "scheduled": scheduled,
            "focus_date": focus_date,
            "shift_choices": changeable_shift_choices(employee.team),
        },
    )


def _page_scheduled_counts(employees):
    # 批量统计本周期排班班数（按当前有效排班记录，休息不计）
    start, end = period_range()
    groups = {}
    for employee in employees:
        groups.setdefault(employee.team_id, []).append(employee)
    counts = {}
    for employees_in_team in groups.values():
        counts.update(
            period_scheduled_counts(
                start, end, team=employees_in_team[0].team, employees=employees_in_team
            )
        )
    return counts



