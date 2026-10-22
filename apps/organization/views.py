# 组织管理页面：队组（含账号一体化创建）、班组、班次配置。
# 权限：队组管理相关页面要求队组管理员及以上；对象访问按用户队组范围过滤（跨队组按 ID 访问返回 404）。
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.permissions import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_SUPER,
    get_org_or_404,
    get_organization,
    get_role,
    get_shift_config_or_404,
    get_team_or_404,
    role_required,
    visible_organizations,
)
from apps.accounts import services as account_services
from apps.auditlog.services import log_operation
from apps.personnel.models import Employee
from apps.scheduler.selectors import period_range, period_scheduled_counts

from . import services
from .forms import (
    OrganizationUnitForm,
    OrganizationWithAccountsForm,
    PositionRequirementsForm,
    ShiftConfigForm,
    TeamForm,
)
from .models import OrganizationUnit, ShiftConfig, Team


def _paginate(request, queryset, per_page=15):
    paginator = Paginator(queryset, per_page)
    return paginator.get_page(request.GET.get("page"))


def _back_to(request, fallback_name):
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(fallback_name)


# ============================ 队组 ============================

@role_required(ROLE_ADMIN)
def unit_list(request):
    # 队组管理员只查看本队组（直接进入本队组详情）；超级管理员查看全部
    if get_role(request.user) != ROLE_SUPER:
        org = get_organization(request.user)
        if org is None:
            raise PermissionDenied("账号未绑定队组。")
        return redirect("organization:unit_detail", pk=org.pk)
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    units = OrganizationUnit.objects.annotate(team_count=Count("teams")).order_by("id")
    if q:
        units = units.filter(Q(name__icontains=q) | Q(code__icontains=q))
    if status == "active":
        units = units.filter(is_active=True)
    elif status == "inactive":
        units = units.filter(is_active=False)
    page = _paginate(request, units)
    return render(
        request,
        "organization/unit_list.html",
        {
            "page_obj": page,
            "q": q,
            "status": status,
            "active_count": OrganizationUnit.objects.filter(is_active=True).count(),
            "inactive_count": OrganizationUnit.objects.filter(is_active=False).count(),
        },
    )


@role_required(ROLE_SUPER)
def unit_create(request):
    # 第一步：队组信息 + 管理员账号 + 普通成员账号（提交后进入确认摘要页）
    form = OrganizationWithAccountsForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cleaned = form.cleaned_data
        request.session["org_wizard"] = {
            "name": cleaned["name"],
            "code": cleaned["code"],
            "is_active": bool(cleaned.get("is_active")),
            "admin_username": cleaned["admin_username"],
            "admin_password": cleaned.get("admin_password") or None,
            "member_username": cleaned["member_username"],
            "member_password": cleaned.get("member_password") or None,
            "admin_password_generated": not cleaned.get("admin_password"),
            "member_password_generated": not cleaned.get("member_password"),
        }
        return redirect("organization:unit_create_confirm")
    return render(request, "organization/unit_create.html", {"form": form})


@role_required(ROLE_SUPER)
def unit_create_confirm(request):
    data = request.session.get("org_wizard")
    if not data:
        messages.error(request, "会话已过期，请重新填写。")
        return redirect("organization:unit_create")
    if request.method == "POST":
        try:
            result = account_services.create_organization_with_accounts(
                name=data["name"],
                code=data["code"],
                is_active=data["is_active"],
                admin_username=data["admin_username"],
                admin_password=data["admin_password"],
                member_username=data["member_username"],
                member_password=data["member_password"],
                actor=request.user,
            )
        except ValidationError as exc:
            messages.error(request, f"创建失败：{'；'.join(exc.messages)}")
            return redirect("organization:unit_create")
        except Exception:  # noqa: BLE001 任何步骤失败都整体回滚
            import logging

            logging.getLogger(__name__).exception("创建队组失败（已回滚）")
            messages.error(request, "创建失败，操作已整体回滚，请重试。")
            return redirect("organization:unit_create")
        request.session.pop("org_wizard", None)
        request.session["org_created"] = {
            "org_id": result["organization"].pk,
            "org_name": result["organization"].name,
            "org_code": result["organization"].code,
            "admin_username": result["admin_user"].username,
            "member_username": result["member_user"].username,
            "admin_password": result["admin_password"] if data["admin_password_generated"] else None,
            "member_password": result["member_password"] if data["member_password_generated"] else None,
        }
        log_operation(
            user=request.user,
            action="create",
            module="organization",
            target_type="organization_unit",
            target_id=result["organization"].pk,
            description=f"创建队组 {data['name']}（{data['code']}），并创建管理员与普通成员账号",
            after_data={"name": data["name"], "code": data["code"]},
        )
        return redirect("organization:unit_create_success")
    return render(request, "organization/unit_create_confirm.html", {"data": data})


@role_required(ROLE_SUPER)
def unit_create_success(request):
    info = request.session.pop("org_created", None)
    if not info:
        return redirect("organization:unit_list")
    return render(request, "organization/unit_create_success.html", {"info": info})


@role_required(ROLE_SUPER)
def unit_edit(request, pk):
    unit = get_object_or_404(OrganizationUnit, pk=pk)
    form = OrganizationUnitForm(request.POST or None, instance=unit)
    if request.method == "POST" and form.is_valid():
        form.save()
        log_operation(
            user=request.user,
            action="edit",
            module="organization",
            target_type="organization_unit",
            target_id=unit.pk,
            description=f"编辑队组 {unit.name}（{unit.code}）",
        )
        messages.success(request, f"队组「{unit.name}」已更新。")
        return redirect("organization:unit_detail", pk=unit.pk)
    return render(request, "organization/unit_form.html", {"form": form, "editing": True, "unit": unit})


@role_required(ROLE_ADMIN)
def unit_detail(request, pk):
    unit = get_org_or_404(request.user, pk)
    teams = (
        unit.teams.annotate(
            # distinct=True：避免与 shift_configs 多表 JOIN 造成职工重复计数（同 team_list）
            employee_count=Count("employees", distinct=True),
            active_configs=Count("shift_configs", filter=Q(shift_configs__is_active=True)),
        )
        .order_by("id")
    )
    return render(
        request,
        "organization/unit_detail.html",
        {
            "unit": unit,
            "teams": teams,
            "team_count": teams.count(),
            "employee_count": sum(t.employee_count for t in teams),
        },
    )


@role_required(ROLE_SUPER)
@require_POST
def unit_toggle(request, pk):
    unit = get_object_or_404(OrganizationUnit, pk=pk)
    if unit.is_active:
        services.deactivate_organization_unit(unit)
        messages.success(request, f"队组「{unit.name}」已停用。")
    else:
        unit.is_active = True
        unit.save(update_fields=["is_active"])
        messages.success(request, f"队组「{unit.name}」已启用。")
    return _back_to(request, "organization:unit_list")


# ============================ 班组 ============================

@role_required(ROLE_ADMIN)
def team_list(request):
    q = request.GET.get("q", "").strip()
    org_id = request.GET.get("organization", "")
    status = request.GET.get("status", "")
    orgs = visible_organizations(request.user)
    teams = (
        Team.objects.filter(organization__in=orgs)
        .select_related("organization")
        .annotate(
            # distinct=True：职工数与班次配置数同时注解会产生多表 JOIN，
            # 每名职工会按每个班次配置重复计数（如 22 人 × 3 配置 = 66），必须去重
            employee_count=Count("employees", distinct=True),
            active_configs=Count("shift_configs", filter=Q(shift_configs__is_active=True)),
        )
        .order_by("organization__name", "id")
    )
    if q:
        teams = teams.filter(Q(name__icontains=q) | Q(code__icontains=q))
    if org_id and orgs.filter(pk=org_id).exists():
        teams = teams.filter(organization_id=org_id)
    if status == "active":
        teams = teams.filter(is_active=True)
    elif status == "inactive":
        teams = teams.filter(is_active=False)
    page = _paginate(request, teams)
    return render(
        request,
        "organization/team_list.html",
        {
            "page_obj": page,
            "q": q,
            "org_id": org_id,
            "status": status,
            "orgs": orgs.order_by("name"),
        },
    )


@role_required(ROLE_ADMIN)
def team_create(request):
    form = TeamForm(request.POST or None, initial=request.GET.dict(), user=request.user)
    if request.method == "POST" and form.is_valid():
        team = form.save(commit=False)
        if get_role(request.user) != ROLE_SUPER:
            # 队组管理员只能在本队组下创建班组（服务端强制，忽略提交的 organization）
            team.organization = get_organization(request.user)
        team.save()
        messages.success(
            request,
            f"班组「{team.name}」已创建，并自动生成早/中/晚三条班次配置（待配置状态）。",
        )
        return redirect("organization:team_detail", pk=team.pk)
    return render(request, "organization/team_form.html", {"form": form, "editing": False})


@role_required(ROLE_ADMIN)
def team_edit(request, pk):
    team = get_team_or_404(request.user, pk)
    form = TeamForm(request.POST or None, instance=team, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"班组「{team.name}」已更新。")
        return redirect("organization:team_detail", pk=team.pk)
    return render(request, "organization/team_form.html", {"form": form, "editing": True, "team": team})


@role_required(ROLE_ADMIN)
def team_detail(request, pk):
    team = get_team_or_404(request.user, pk)
    configs = team.shift_configs.order_by("shift_name")
    employees = team.employees.select_related("shift_config").order_by("employee_no")
    return render(
        request,
        "organization/team_detail.html",
        {
            "team": team,
            "configs": configs,
            "employees": employees[:10],
            "employee_count": employees.count(),
            "active_config_count": configs.filter(is_active=True).count(),
            "config_count": configs.count(),
        },
    )


@role_required(ROLE_ADMIN)
@require_POST
def team_toggle(request, pk):
    team = get_team_or_404(request.user, pk)
    if team.is_active:
        services.deactivate_team(team)
        messages.success(request, f"班组「{team.name}」已停用。")
    else:
        team.is_active = True
        team.save(update_fields=["is_active"])
        messages.success(request, f"班组「{team.name}」已启用。")
    return _back_to(request, "organization:team_list")


# ============================ 班次配置 ============================

@role_required(ROLE_ADMIN)
def shift_config_list(request):
    orgs = visible_organizations(request.user).order_by("name")
    org_id = request.GET.get("organization", "")
    team_id = request.GET.get("team", "")
    org = orgs.filter(pk=org_id).first() if org_id else None
    if org is None:
        org = orgs.first()
    teams = org.teams.order_by("name") if org else Team.objects.none()
    team = teams.filter(pk=team_id).first() if team_id and org else None
    if team is None:
        team = teams.first()
    configs = []
    config_issues = {}
    exempt_names = {}
    pos_summaries = {}
    if team:
        configs = list(team.shift_configs.order_by("shift_name"))
        employee_map = {e.pk: f"{e.employee_no} {e.name}" for e in team.employees.all()}
        for cfg in configs:
            try:
                cfg.full_clean()
                config_issues[cfg.pk] = []
            except ValidationError as exc:
                flat = []
                for errs in exc.message_dict.values():
                    flat.extend(errs)
                config_issues[cfg.pk] = flat
            exempt_names[cfg.pk] = [
                employee_map.get(pk, f"职工#{pk}") for pk in (cfg.exempt_employee_ids or [])
            ]
            reqs = cfg.position_requirements or {}
            pos_summaries[cfg.pk] = {
                "count": len(reqs),
                "total": sum(v for v in reqs.values() if isinstance(v, int)),
            }
    return render(
        request,
        "organization/shift_config_list.html",
        {
            "orgs": orgs,
            "teams": teams,
            "org": org,
            "team": team,
            "configs": configs,
            "config_issues": config_issues,
            "exempt_names": exempt_names,
            "pos_summaries": pos_summaries,
            "active_count": sum(1 for c in configs if c.is_active),
        },
    )


def _build_precheck(config):
    # 排班可行性预检（配置页实时计算）：人多人少理论值 + 岗位资格分级检查。
    # 注意：仅为数量级预检，不等于完整求解证明；完整验证由「运行完整求解验证」执行。
    from apps.personnel.services import parse_special_positions
    from apps.scheduler.engine.allocator import plan_shift_targets
    from apps.scheduler.selectors import period_range

    team = config.team
    # 只统计“归属当前班次”的在职职工：同一班组的不同班次人员范围互相独立
    from apps.personnel.services import shift_employees

    employees = list(shift_employees(config))
    n = len(employees)
    start, end = period_range()
    days = (end - start).days + 1
    r = config.daily_worker_count
    m = config.required_shifts
    total = days * r
    exempt_ids = set(config.exempt_employee_ids or [])
    normal_ids = [e.pk for e in employees if e.pk not in exempt_ids]
    alloc = plan_shift_targets(
        total_days=days,
        daily_required=r,
        base_shifts=m,
        normal_ids=normal_ids,
        exempt_ids=list(exempt_ids),
    )
    quals = {str(e.pk): parse_special_positions(e.position) for e in employees}
    positions = []
    for pos, required in (config.position_requirements or {}).items():
        if not isinstance(required, int) or isinstance(required, bool) or required <= 0:
            continue  # 0 表示不要求该岗位，不作为正式岗位参与预检
        qualified = sum(1 for q in quals.values() if pos in q)
        if qualified < required:
            level = "bad"      # 明显不可行
            note = f"仅 {qualified} 名有效职工具备该岗位资格，无法满足每天 {required} 人。"
        elif qualified == required:
            level = "warn"     # 表面可行但无冗余，存在兼岗冲突风险
            note = f"资格人数恰好等于要求（{qualified} 人），无冗余，存在兼岗冲突风险。"
        else:
            level = "ok"
            note = f"具备资格 {qualified} 人 ≥ 要求 {required} 人。"
        positions.append({
            "name": pos, "required": required, "qualified": qualified,
            "level": level, "note": note,
        })
    total_req = sum(
        int(v) for v in (config.position_requirements or {}).values()
        if isinstance(v, int) and not isinstance(v, bool) and v > 0
    )
    overall = "bad" if any(p["level"] == "bad" for p in positions) or total_req > r else (
        "warn" if any(p["level"] == "warn" for p in positions) else "ok"
    )
    return {
        "employee_count": n,
        "days": days,
        "daily": r,
        "total": total,
        "base_shifts": m,
        "alloc": alloc,
        "positions": positions,
        "positions_total": total_req,
        "overall": overall,
        "period_start": start,
        "period_end": end,
    }


@role_required(ROLE_ADMIN)
def shift_config_edit(request, pk):
    config = get_shift_config_or_404(request.user, pk)
    team = config.team
    form = ShiftConfigForm(request.POST or None, instance=config)
    position_form = PositionRequirementsForm(
        request.POST or None,
        initial={
            "position_requirements": json.dumps(
                config.position_requirements or {}, ensure_ascii=False
            )
        },
    )

    # ---- 岗位人数配置数据（按当前班次成员的真实岗位动态生成） ----
    from apps.personnel.services import parse_positions, parse_special_positions

    shift_members = (
        Employee.objects.filter(team=team, shift_config=config)
        .select_related("shift_config")
        .order_by("employee_no")
    )
    position_counts = {}
    for member in shift_members:
        # 动态岗位行仅统计“特殊岗位”；“普通/普工”不作为正式岗位参与配置与排班
        for pos in parse_special_positions(member.position):
            position_counts[pos] = position_counts.get(pos, 0) + 1
    saved_reqs = dict(config.position_requirements or {})
    over_warnings = [
        f"岗位「{name}」要求 {count} 人，但当前班次仅 {position_counts.get(name, 0)} 人具备资格。"
        for name, count in saved_reqs.items()
        if position_counts.get(name, 0) < count
    ]
    orphan_positions = [
        (name, saved_reqs[name]) for name in saved_reqs if name not in position_counts
    ]
    position_rows = [
        {"name": name, "eligible": position_counts[name], "required": saved_reqs.get(name, 0)}
        for name in position_counts
    ]
    siblings = list(team.shift_configs.order_by("shift_name"))

    # ---- 当前班次成员列表（搜索 + 分页） ----
    q = request.GET.get("q", "").strip()
    members_qs = shift_members
    if q:
        from django.db.models import Q

        members_qs = members_qs.filter(
            Q(name__icontains=q) | Q(employee_no__icontains=q) | Q(position__icontains=q)
        )
    paginator = Paginator(members_qs, 15)
    member_page = paginator.get_page(request.GET.get("page"))
    saved_exempt_ids = set(config.exempt_employee_ids or [])
    for member in member_page:
        member.position_list = parse_positions(member.position)

    # ---- 豁免职工弹窗数据 ----
    team_employees = (
        Employee.objects.filter(team=team).select_related("shift_config").order_by("employee_no")
    )
    candidates = [e for e in team_employees if e.is_active and e.shift_config_id == config.pk]
    selected_employees = [e for e in team_employees if e.pk in saved_exempt_ids]
    # 直接传递原生列表给 json_script（过滤器内部会自行 JSON 序列化一次）；
    # 若预先 json.dumps 成字符串会导致双重编码，浏览器端解析出字符串而非数组，
    # 豁免弹窗 JS 随即抛异常（allMembers.forEach 不存在）而整个区块静默失效。
    candidates_json = [
        {"id": e.pk, "employee_no": e.employee_no, "name": e.name,
         "positions": parse_positions(e.position)}
        for e in candidates
    ]
    all_members_json = [
        {"id": e.pk, "employee_no": e.employee_no, "name": e.name} for e in team_employees
    ]
    saved_reqs_json = json.dumps(saved_reqs, ensure_ascii=False)

    # ---- 排班可行性预检（实时计算，保存时也会重新计算） ----
    precheck = _build_precheck(config)
    verify_result = None

    if request.method == "POST" and request.POST.get("action") == "verify":
        # 完整求解验证（不保存任何数据；结果仅作展示）
        from apps.scheduler.planning import solve_shift_schedule

        solved = solve_shift_schedule(config, time_limit_seconds=30)
        if solved.status == "feasible":
            detail = f"完整求解验证通过：全部硬性约束满足、独立校验通过，总班数偏差 {solved.total_shift_deviation}。"
        else:
            detail = "；".join(solved.diagnostics) or solved.status
        verify_result = {"status": solved.status, "detail": detail}
        precheck = _build_precheck(config)
        # 重建表单数据避免与提交冲突（继续走渲染分支）
        form = ShiftConfigForm(instance=config)
        position_form = PositionRequirementsForm(
            initial={"position_requirements": json.dumps(config.position_requirements or {}, ensure_ascii=False)}
        )
    elif request.method == "POST":
        if form.is_valid() and position_form.is_valid():
            cleaned = form.cleaned_data
            exempt_ids = [e.pk for e in cleaned.get("exempt_employees") or []]
            # team/shift_name 为展示字段（未提交时 cleaned 中为 None/''），不得回写模型
            fields = {
                k: v
                for k, v in cleaned.items()
                if k not in ("exempt_employees", "team", "shift_name")
            }
            try:
                services.update_shift_config(
                    config,
                    **fields,
                    exempt_employee_ids=exempt_ids,
                    position_requirements=position_form.cleaned_data["position_requirements"],
                )
                log_operation(
                    user=request.user,
                    action="edit",
                    module="shift_config",
                    target_type="shift_config",
                    target_id=config.pk,
                    description=f"修改班次配置 {config.team.name} · {config.get_shift_name_display()}（含岗位要求与豁免职工）",
                )
                messages.success(
                    request,
                    f"班次配置「{config.team.name} · {config.get_shift_name_display()}」已保存。",
                )
                return redirect("organization:shift_config_edit", pk=config.pk)
            except ValidationError as exc:
                messages.error(request, f"保存失败：{'；'.join(exc.messages)}")
        else:
            messages.error(request, "保存失败，请检查表单中的错误提示。")

    return render(
        request,
        "organization/shift_config_edit.html",
        {
            "form": form,
            "position_form": position_form,
            "config": config,
            "team": team,
            "position_counts": position_counts,
            "position_rows": position_rows,
            "siblings": siblings,
            "saved_reqs": saved_reqs,
            "over_warnings": over_warnings,
            "orphan_positions": orphan_positions,
            "member_page": member_page,
            "member_scheduled_counts": period_scheduled_counts(
                *period_range(), team=team, employees=list(member_page)
            ),
            "precheck": precheck,
            "verify_result": verify_result,
            "q": q,
            "saved_exempt_ids": saved_exempt_ids,
            "selected_employees": selected_employees,
            "candidates_json": candidates_json,
            "all_members_json": all_members_json,
            "saved_reqs_json": saved_reqs_json,
            "member_total": shift_members.count(),
        },
    )


@role_required(ROLE_ADMIN)
@require_POST
def shift_config_toggle(request, pk):
    config = get_shift_config_or_404(request.user, pk)
    try:
        services.set_shift_config_active(config, not config.is_active)
        state = "启用" if config.is_active else "停用"
        log_operation(
            user=request.user,
            action="toggle",
            module="shift_config",
            target_type="shift_config",
            target_id=config.pk,
            description=f"{state}班次配置 {config.team.name} · {config.get_shift_name_display()}",
        )
        messages.success(
            request,
            f"班次配置「{config.team.name} · {config.get_shift_name_display()}」已{state}。",
        )
    except ValidationError as exc:
        messages.error(request, f"无法启用：{'；'.join(exc.messages)}。请先修正配置参数。")
    return _back_to(request, "organization:shift_config_list")


# ============================ 联动 JSON（数据范围受限） ============================

@role_required(ROLE_ADMIN)
def teams_json(request):
    org_id = request.GET.get("organization", "")
    teams = Team.objects.filter(organization__in=visible_organizations(request.user)).order_by("name")
    if org_id:
        teams = teams.filter(organization_id=org_id)
    data = {
        "teams": [
            {"id": t.pk, "name": t.name, "code": t.code, "is_active": t.is_active}
            for t in teams
        ]
    }
    return JsonResponse(data)


@role_required(ROLE_ADMIN)
def shift_configs_json(request):
    team_id = request.GET.get("team", "")
    configs = ShiftConfig.objects.filter(
        team__organization__in=visible_organizations(request.user)
    ).order_by("shift_name")
    if team_id:
        configs = configs.filter(team_id=team_id)
    data = {
        "shift_configs": [
            {
                "id": c.pk,
                "shift_name": c.shift_name,
                "display": c.get_shift_name_display(),
                "is_active": c.is_active,
            }
            for c in configs
        ]
    }
    return JsonResponse(data)
