# -*- coding: utf-8 -*-
import calendar
import re
from datetime import date, timedelta

from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .models import Assignment, Group, Person, Role, Schedule, Team, UserProfile

# 排班算法模型（单一文件，见 scheduling.py 的模块说明）
from project.app.scheduler.scheduling import build_schedule, capacity_analysis, capacity_quick

# 工作窗口（固定：任意 10 天最多上 6 班）
DEFAULT_WORK_WINDOW = {"length": 10, "max_work": 6}

# 周期起算日：25 号开始算下一个月（某月 M 的周期 = 上月25号 ~ 本月24号）
PERIOD_START_DAY = 25
FIXED_SHIFTS = ["早班", "中班", "晚班"]


# ---------------------------------------------------------------------------
# 登录 / 团队权限
# ---------------------------------------------------------------------------
def login_view(request):
    if request.user.is_authenticated:
        return redirect("scheduler:index")
    error = ""
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""
        user = authenticate(request, username=username, password=password)
        if user is not None:
            auth_login(request, user)
            next_url = request.POST.get("next") or request.GET.get("next") or "/"
            return redirect(next_url)
        error = "账号或密码错误，请重试。"
    return render(request, "scheduler/login.html", {
        "error": error,
        "next": request.GET.get("next", ""),
    })


def logout_view(request):
    auth_logout(request)
    return redirect("scheduler:login")


def user_group(request):
    """当前登录用户所属队组（未绑定/超级管理员返回 None = 看所有队组）。"""
    if not request.user.is_authenticated:
        return None
    profile = getattr(request.user, "profile", None)
    return profile.group if profile else None


def user_role(request):
    """当前登录用户角色：super / team_admin / member。"""
    if not request.user.is_authenticated:
        return "member"
    profile = getattr(request.user, "profile", None)
    if request.user.is_superuser:
        return "super"
    return profile.role if profile else "member"


def can_edit_team(request, team) -> bool:
    """是否允许编辑该班组：超级管理员 或 该班组所属队组的队组管理员。"""
    role = user_role(request)
    if role == "super":
        return True
    ug = user_group(request)
    return role == "team_admin" and ug is not None and team is not None and team.group_id == ug.id


# ---------------------------------------------------------------------------
# 周期 / 日期工具
# ---------------------------------------------------------------------------
def period_range(year: int, month: int):
    """返回 (周期开始日期, 周期结束日期, 天数)。某月 M 的周期 = 上月25号 ~ 本月24号。"""
    if month == 1:
        start = date(year - 1, 12, PERIOD_START_DAY)
    else:
        start = date(year, month - 1, PERIOD_START_DAY)
    end = date(year, month, PERIOD_START_DAY - 1)
    days = (end - start).days + 1
    return start, end, days


def current_period(today: date = None):
    """今天属于哪个排班周期（按 25 号划分），返回 (year, month)。"""
    today = today or date.today()
    if today.day >= PERIOD_START_DAY:
        y, m = today.year, today.month + 1
        if m == 13:
            y, m = y + 1, 1
        return y, m
    return today.year, today.month


def month_shift(year: int, month: int, delta: int):
    """返回 (year, month) 偏移 delta 个月。"""
    m = month - 1 + delta
    y = year + m // 12
    return y, m % 12 + 1


def person_worked_auto(person: Person, schedule: Schedule = None) -> int:
    """已上班数（按日期自动计算）= 导入初始值 + 排班中「日期已过」的上班天数。"""
    count = person.worked_so_far
    if schedule and schedule.result_json and schedule.start_date:
        per_day = schedule.result_json.get("per_day", {})
        today = date.today()
        for d in range(schedule.days):
            if schedule.start_date + timedelta(days=d) > today:
                continue
            for s in schedule.shifts:
                if person.name in per_day.get(str(d), {}).get(s, []):
                    count += 1
                    break
    return count


# ---------------------------------------------------------------------------
# 首页
# ---------------------------------------------------------------------------
@login_required
def index(request):
    ug = user_group(request)
    qs = Schedule.objects.select_related("team")
    persons_qs = Person.objects.all()
    if ug:
        qs = qs.filter(team__group=ug)
        persons_qs = persons_qs.filter(team__group=ug)
    recent = qs[:5]
    return render(request, "scheduler/index.html", {
        "recent": recent,
        "person_count": persons_qs.count(),
        "team_count": (Team.objects.filter(group=ug).count() if ug else Team.objects.count()),
        "schedule_count": qs.count(),
        "user_group": ug,
    })


# ---------------------------------------------------------------------------
# 班组管理（点击班组 → 显示存储约束 + 该班人员）
# ---------------------------------------------------------------------------
def _parse_import_text(text: str):
    """解析导入文本，返回 [(班组, 姓名, [岗位...], 已上班数, 应上班数, 默认班次), ...]。

    每行（推荐）：班组-姓名-岗位1,岗位2-已上班数-应上班数-默认班次
      - 默认班次可写 早班/中班/晚班，写在最后、可省略（省略则保持默认「早班」）
    """
    items = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if ":" in line or "：" in line:
            head = re.split(r"[:：]", line, maxsplit=1)
            role = head[0].strip()
            names = re.split(r"[,，、\s]+", head[1].strip()) if len(head) > 1 else []
            for nm in names:
                if nm:
                    items.append(("", nm, [role], 0, 0, ""))
            continue
        if "-" in line:
            parts = [p.strip() for p in line.split("-") if p.strip()]
        else:
            parts = [p.strip() for p in re.split(r"[,，、\s]+", line) if p.strip()]
        if not parts:
            continue
        # 从末尾抠默认班次
        default_shift = ""
        if parts[-1] in ("早班", "中班", "晚班"):
            default_shift = parts.pop()
        # 从末尾抠数字（已上班数、应上班数；允许 "0,18" 或 "0 18"）
        counts = []  # 最终为 [已上班数, 应上班数]
        while parts and len(counts) < 2:
            last_tokens = [t for t in re.split(r"[,，、\s]+", parts[-1]) if t.strip()]
            if len(last_tokens) >= 2 and all(re.fullmatch(r"\d+", t) for t in last_tokens[:2]):
                counts = [int(last_tokens[0]), int(last_tokens[1])]
                parts.pop()
                break
            if len(last_tokens) == 1 and re.fullmatch(r"\d+", last_tokens[0]):
                counts.insert(0, int(last_tokens[0]))
                parts.pop()
                continue
            break
        worked, required = (counts[0], counts[1]) if len(counts) == 2 else \
                           (0, counts[0]) if len(counts) == 1 else (0, 0)
        if "-" in line:
            team = parts.pop(0)
        else:
            team = ""
        if not parts:
            continue
        name = parts.pop(0)
        roles = []
        for p in parts:
            roles.extend([r.strip() for r in re.split(r"[,，、]+", p) if r.strip()])
        items.append((team, name, roles, worked, required, default_shift))
    return items


@require_http_methods(["GET", "POST"])
@login_required
def team_manage(request):
    ug = user_group(request)
    groups = Group.objects.order_by("name")

    # 选择队组：队组账号固定为自己的队组；超级管理员通过 ?group=<id> 选择
    sel_group = request.GET.get("group", "")
    if ug:
        selected_group = ug
    else:
        selected_group = groups.filter(id=sel_group).first() if sel_group.isdigit() else None

    teams = Team.objects.select_related("group").order_by("name")
    if selected_group:
        teams = teams.filter(group=selected_group)
    else:
        teams = Team.objects.none()

    # 通过 ?team=<id 或 名称> 选择班组
    sel = request.GET.get("team", "")
    default_team = None
    if sel:
        default_team = teams.filter(id=sel).first() if sel.isdigit() else teams.filter(name=sel).first()
    default_team = default_team or teams.first()

    gq = f"&group={selected_group.id}" if selected_group else ""  # 跳转时保留队组选择

    message = ""
    error = ""
    if request.GET.get("deleted"):
        message = f"已删除班组「{request.GET['deleted']}」。"
    if request.GET.get("saved") and default_team:
        message = f"已保存「{default_team.name}」的排班约束。"
    if request.GET.get("error") == "daily":
        error = "请先填写「该班应上人数（每天）」（大于 0）再生成排班。"

    if request.method == "POST":
        action = request.POST.get("action", "")
        if user_role(request) == "member":
            error = "队员账号为只读，只能查看，不能修改排班数据。"
        elif action == "save_constraints":
            team = teams.filter(id=request.POST.get("team_id")).first()
            if team:
                try:
                    team.daily_headcount = max(0, int(request.POST.get("daily_headcount") or 0))
                except ValueError:
                    pass
                role_names = request.POST.getlist("role_names")
                role_ops = request.POST.getlist("role_ops")
                role_counts = request.POST.getlist("role_counts")
                role_reqs = {}
                for rn, op, cnt in zip(role_names, role_ops, role_counts):
                    rn = (rn or "").strip()
                    if rn:
                        role_reqs[rn] = {"op": op or ">=", "count": int(cnt or 0)}
                team.role_reqs = role_reqs
                try:
                    rb_min = max(1, int(request.POST.get("rest_min") or 2))
                    rb_max = max(rb_min, int(request.POST.get("rest_max") or 4))
                except ValueError:
                    rb_min, rb_max = 2, 4
                team.rest_block = {"min": rb_min, "max": rb_max}
                try:
                    team.min_shift_target = max(0, int(request.POST.get("min_shift_target") or 0))
                except ValueError:
                    pass
                team.exempt_names = request.POST.getlist("exempt")
                team.save()
                from urllib.parse import quote
                return redirect(f"{request.path}?team={team.id}&saved=1{gq}")

        if action == "import":
            text = request.POST.get("import_text", "")
            uploaded = request.FILES.get("import_file")
            if uploaded:
                text = uploaded.read().decode("utf-8-sig", errors="ignore")
            parsed = _parse_import_text(text)
            if not parsed:
                error = "没有解析到任何人员，请检查导入格式。"
            else:
                created_p = 0
                for team, nm, roles, worked, required, default_shift in parsed:
                    person, is_new = Person.objects.get_or_create(name=nm)
                    if is_new:
                        created_p += 1
                    # 归属班组：队组账号只能导入到本队组下的班组
                    if ug:
                        tname = team or "检修班"
                        person.team, _ = Team.objects.get_or_create(group=ug, name=tname)
                    elif team:
                        person.team, _ = Team.objects.get_or_create(name=team)
                    person.worked_so_far = worked
                    person.required_shifts = required
                    # 默认班次：显式写了就用；没写保持默认「早班」（可在人员编辑页修改）
                    if default_shift:
                        person.default_shift = default_shift
                    person.save()
                    for rn in roles:
                        role, _ = Role.objects.get_or_create(name=rn)
                        person.roles.add(role)
                message = (f"导入完成：新增人员 {created_p} 人"
                           f"{'（已归入本队组「' + ug.name + '」的班组）' if ug else ''}"
                           f"，共处理 {len(parsed)} 条记录。")

        elif action == "delete":
            pid = request.POST.get("person_id")
            if pid:
                Person.objects.filter(id=pid).delete()
                message = "已删除该人员。"

        elif action == "add_team":
            if ug:
                error = "队组账号不能新增班组（班组由超级管理员管理）。"
            elif not selected_group:
                error = "请先选择一个队组，再新增班组。"
            else:
                tn = (request.POST.get("team_name") or "").strip()
                if tn:
                    Team.objects.get_or_create(group=selected_group, name=tn)
                    message = f"已添加班组「{tn}」。"
                    return redirect(f"{request.path}?group={selected_group.id}&team={tn}")

        elif action == "delete_team":
            if ug:
                error = "队组账号不能删除班组（班组由超级管理员管理）。"
            else:
                tid = request.POST.get("team_id")
                confirm = (request.POST.get("confirm_text") or "").strip()
                team = Team.objects.filter(id=tid).first() if tid else None
                if not team:
                    error = "班组不存在或已被删除。"
                elif confirm != "删除":
                    error = "确认失败：请输入「删除」两个字才能删除班组。"
                else:
                    tname = team.name
                    # 删除班组：其人员变为未分组，排班记录保留但失去班组归属
                    team.delete()
                    from urllib.parse import quote
                    return redirect(f"{request.path}?deleted={quote(tname)}{gq}")

        elif action == "generate":
            team = teams.filter(id=request.POST.get("team_id")).first()
            if team:
                return redirect(f"{request.path}?action=generate&team={team.id}{gq}")

    # 生成排班（使用该班组存储的约束）
    if request.GET.get("action") == "generate" and default_team:
        return _run_generate(request, default_team)

    # 该班人员（含按日期自动计算的已上班数）
    persons = Person.objects.filter(team=default_team).select_related("team").prefetch_related("roles").order_by("name") \
        if default_team else Person.objects.none()
    schedule_map = {s.team_id: s for s in Schedule.objects.filter(team=default_team).order_by("-created_at")} \
        if default_team else {}
    rows = [(p, person_worked_auto(p, schedule_map.get(p.team_id))) for p in persons]

    # 固定岗位下拉选项：该班所有人的岗位 去重后的唯一集合（元组），并合并已配置的岗位
    if default_team:
        person_roles = set()
        for p in persons:
            person_roles.update(p.roles.values_list("name", flat=True))
        person_roles.update((default_team.role_reqs or {}).keys())
        team_role_options = tuple(sorted(person_roles))
    else:
        team_role_options = ()

    # 连休最大值默认值 ≈ (周期实际天数 - 最少上班班数) / 3
    y0, m0 = current_period()
    _, _, period_days = period_range(y0, m0)
    min_tgt = default_team.min_shift_target if default_team else 18
    default_rest_max = max(2, (period_days - max(0, min_tgt)) // 3)

    # 容量预估（提示最多能有多少人排满，需要豁免几人；只统计启用人员）
    capacity = None
    if default_team:
        people_count = Person.objects.filter(team=default_team, is_active=True).count()
        exempt_count = len([n for n in (default_team.exempt_names or []) if
                            Person.objects.filter(team=default_team, name=n, is_active=True).exists()])
        capacity = capacity_analysis(
            people_count, default_team.daily_headcount or 0, period_days,
            (default_team.rest_block or {}).get("max", 4),
            default_team.min_shift_target or 0, exempt_count,
        )

    # 每个岗位的持有人数（用于实时"岗位条件可行性"检查；只统计启用人员）
    role_holder_counts = {}
    if default_team:
        for p in persons:
            if not p.is_active:
                continue
            for rn in p.roles.values_list("name", flat=True):
                role_holder_counts[rn] = role_holder_counts.get(rn, 0) + 1

    return render(request, "scheduler/team_manage.html", {
        "teams": teams,
        "team": default_team,
        "user_group": ug,
        "groups": groups,
        "selected_group": selected_group,
        "rows": rows,
        "team_persons": [p for p, _ in rows],
        "team_role_options": team_role_options,
        "role_holder_counts": role_holder_counts,
        "default_rest_max": default_rest_max,
        "capacity": capacity,
        "period_days": period_days,
        "is_team_user": ug is not None,
        "is_super": user_role(request) == "super",
        "can_edit": can_edit_team(request, default_team),
        "roles": Role.objects.order_by("name"),
        "message": message,
        "error": error,
    })


def _run_generate(request, team: Team):
    """用班组存储的约束调用引擎生成排班，返回重定向到结果页。"""
    if not team.daily_headcount or team.daily_headcount <= 0:
        from urllib.parse import quote
        return redirect(f"/teams/?group={team.group_id or ""}&team={team.id}&error=daily")
    y, m = current_period()
    start, end, days = period_range(y, m)
    persons = Person.objects.filter(team=team, is_active=True).prefetch_related("roles").order_by("name")
    if not persons:
        return redirect(f"/teams/?group={team.group_id or ""}&team={team.id}")
    worker_snapshot = [
        {"name": p.name, "roles": list(p.roles.values_list("name", flat=True)),
         "worked": p.worked_so_far, "required": p.required_shifts,
         "team": team.name, "default_shift": p.default_shift}
        for p in persons
    ]
    worker_req = {}
    default_shift_map = {}
    exempt_set = set(team.exempt_names or [])
    target_global = team.min_shift_target or 0
    for p in persons:
        default_shift_map[p.name] = p.default_shift

    # 容量预估：最多能有多少人排满目标（豁免人员不参与休息计算，不占最少班数）
    non_exempt_count = len([p for p in persons if p.name not in exempt_set])
    cap = capacity_analysis(
        len(persons), team.daily_headcount or 0, days,
        (team.rest_block or {}).get("max", 4), target_global,
        exempt_count=len(persons) - non_exempt_count,
    )
    hard_targets = non_exempt_count <= cap["max_fillable"]

    base_config = {
        "workers": worker_snapshot,
        "shifts": FIXED_SHIFTS,
        "days": days,
        "daily_total": team.daily_headcount or 0,
        "role_req": team.role_reqs or {},
        "min_shift_target": team.min_shift_target or None,
        "worker_default_shift": default_shift_map,
        "exempt_workers": team.exempt_names or [],
        "rest_block": dict(team.rest_block or {"min": 2, "max": 4}),
        "work_window": dict(DEFAULT_WORK_WINDOW),
    }

    def _build_req(hard: bool):
        req = {}
        for p in persons:
            if p.required_shifts > 0:
                tgt = p.required_shifts - p.worked_so_far
            else:
                tgt = target_global
            if tgt <= 0 or p.name in exempt_set:
                continue
            if hard:
                # 非豁免恰好达到目标（不超排），剩余班数交由豁免人员分割
                req[p.name] = {"target": tgt, "min": tgt, "max": tgt}
            else:
                req[p.name] = {"target": tgt}
        return req

    config = dict(base_config)
    config["worker_shift_req"] = _build_req(hard_targets)
    result = build_schedule(config, time_limit_seconds=30, phase2_seconds=10)
    if not result.feasible and hard_targets:
        # 岗位条件等导致硬达标不可行：回退到软性目标（最大化达标人数）
        config = dict(base_config)
        config["worker_shift_req"] = _build_req(hard=False)
        result = build_schedule(config, time_limit_seconds=30, phase2_seconds=10)
        result.diagnostics = list(result.diagnostics) + [
            "提示：岗位人数条件（如 电工 每天恰好 N 人）导致部分岗位人员无法全员达到应上班数，"
            "已按「最大化达标人数」处理，下方列出未达标人员。可把岗位条件从「等于」改为「至少」，或豁免这些人员。"
        ]

    if not result.feasible:
        # 整体无解：返回结果页显示诊断
        per_day = {str(d): {s: [] for s in FIXED_SHIFTS} for d in range(days)}
        result_json = {
            "per_day": per_day, "worker_counts": {}, "reached": {}, "target": target_global,
            "single_rest": 0, "rest_run_violations": 0, "days": days, "shortfall": [],
        }
        record = Schedule.objects.create(
            team=team, year=y, month=m, start_date=start, days=days,
            shifts=FIXED_SHIFTS, shift_demand={}, daily_total=team.daily_headcount,
            role_reqs=team.role_reqs or {}, min_shift_target=team.min_shift_target,
            exempt_names=team.exempt_names or [],
            rest_block=dict(team.rest_block or {"min": 2, "max": 4}),
            work_window=dict(DEFAULT_WORK_WINDOW), worker_snapshot=worker_snapshot,
            status=result.status, message=result.message, diagnostics=result.diagnostics,
            result_json=result_json,
        )
        return redirect("scheduler:schedule_result", pk=record.id)

    per_day = {str(d): {s: result.per_day[d][s] for s in FIXED_SHIFTS} for d in range(days)}
    # 排班不足（未达到应上班数）的人员名单，用于结果页警告并提醒设置豁免
    shortfall = []
    for p in persons:
        if p.name in exempt_set:
            continue
        if p.required_shifts > 0:
            tgt = p.required_shifts - p.worked_so_far
        else:
            tgt = team.min_shift_target or 0
        if tgt <= 0:
            continue
        cnt = result.worker_counts.get(p.name, 0)
        if cnt < tgt:
            shortfall.append({"name": p.name, "target": tgt, "count": cnt})
    shortfall.sort(key=lambda x: x["target"] - x["count"], reverse=True)
    result_json = {
        "per_day": per_day,
        "worker_counts": result.worker_counts,
        "reached": result.reached,
        "target": result.target,
        "single_rest": result.single_rest,
        "rest_run_violations": result.rest_run_violations,
        "days": days,
        "shortfall": shortfall,
    }
    record = Schedule.objects.create(
        team=team, year=y, month=m, start_date=start, days=days,
        shifts=FIXED_SHIFTS, shift_demand={}, daily_total=team.daily_headcount,
        role_reqs=team.role_reqs or {},
        min_shift_target=team.min_shift_target,
        exempt_names=team.exempt_names or [],
        rest_block=dict(team.rest_block or {"min": 2, "max": 4}),
        work_window=dict(DEFAULT_WORK_WINDOW),
        worker_snapshot=worker_snapshot,
        status=result.status,
        message=result.message,
        diagnostics=result.diagnostics,
        result_json=result_json,
    )
    # 每个班组每个月只保留一份排班：删除该班组同月份更旧的排班（含其明细），
    # 保证班次展示、个人日历、排班记录三者数据一致
    old_schedules = Schedule.objects.filter(team=team, year=y, month=m).exclude(id=record.id)
    if old_schedules.exists():
        Assignment.objects.filter(schedule__in=old_schedules).delete()
        old_schedules.delete()
    Assignment.objects.filter(schedule=record).delete()
    assign_rows = []
    for d in range(days):
        for s in FIXED_SHIFTS:
            for nm in per_day[str(d)][s]:
                p = Person.objects.filter(name=nm).first()
                if p:
                    assign_rows.append(Assignment(schedule=record, person=p, day=d, shift=s))
    Assignment.objects.bulk_create(assign_rows)
    return redirect("scheduler:schedule_result", pk=record.id)


# ---------------------------------------------------------------------------
# 人员编辑
# ---------------------------------------------------------------------------
@require_http_methods(["GET", "POST"])
@login_required
def person_edit(request, person_id):
    person = get_object_or_404(Person, id=person_id)
    ug = user_group(request)
    if ug and (person.team is None or person.team.group_id != ug.id):
        return redirect("scheduler:index")
    if not can_edit_team(request, person.team):
        return redirect("scheduler:person_detail", person_id=person.id)
    message = ""
    if request.method == "POST":
        selected = request.POST.getlist("roles")
        new_role_names = request.POST.get("new_roles", "")
        person.roles.set(Role.objects.filter(id__in=selected))
        for rn in re.split(r"[,，、\s]+", new_role_names.strip()):
            if rn:
                role, _ = Role.objects.get_or_create(name=rn)
                person.roles.add(role)
        team_id = request.POST.get("team")
        person.team = Team.objects.filter(id=team_id).first() if team_id else None
        person.default_shift = request.POST.get("default_shift") or "早班"
        person.is_active = request.POST.get("is_active") == "on"
        try:
            person.worked_so_far = max(0, int(request.POST.get("worked_so_far") or 0))
            person.required_shifts = max(0, int(request.POST.get("required_shifts") or 0))
        except ValueError:
            pass
        person.save()
        message = f"已保存「{person.name}」的信息。"
    return render(request, "scheduler/person_edit.html", {
        "person": person,
        "all_roles": Role.objects.order_by("name"),
        "teams": Team.objects.filter(group=user_group(request)) if user_group(request) else Team.objects.order_by("name"),
        "message": message,
    })


# ---------------------------------------------------------------------------
# 个人详情：日历 + 下井标签 + 改班（可按月份查看，默认 25 号起算的下一月）
# ---------------------------------------------------------------------------
def _schedule_for(person: Person, year: int, month: int):
    return Schedule.objects.filter(team=person.team, year=year, month=month).order_by("-created_at").first()


@login_required
def person_detail(request, person_id):
    person = get_object_or_404(Person, id=person_id)
    ug = user_group(request)
    if ug and (person.team is None or person.team.group_id != ug.id):
        return redirect("scheduler:index")
    message = ""
    error = ""

    # 月份参数；默认 = 25号起算的下一月周期
    try:
        y = int(request.GET.get("y", 0))
        m = int(request.GET.get("m", 0))
    except ValueError:
        y = m = 0
    if not (y and 1 <= m <= 12):
        y, m = current_period()

    schedule = _schedule_for(person, y, m)

    if request.method == "POST":
        if user_role(request) == "member":
            error = "队员账号为只读，不能修改排班。"
        else:
            try:
                day = int(request.POST.get("day", -1))
            except (TypeError, ValueError):
                day = -1
            new_shift = request.POST.get("shift", "")
            if not schedule:
                error = "该月还没有排班，无法改班。"
            elif not (0 <= day < schedule.days):
                error = "无效的改班请求。"
            else:
                ddate = schedule.start_date + timedelta(days=day) if schedule.start_date else None
                rj = schedule.result_json
                per_day = rj.get("per_day", {})
                cell = per_day.setdefault(str(day), {})
                was_working = any(person.name in cell.get(s, []) for s in FIXED_SHIFTS)

                if new_shift == "休息":
                    Assignment.objects.filter(schedule=schedule, person=person, day=day).delete()
                    for s in FIXED_SHIFTS:
                        if person.name in cell.get(s, []):
                            cell[s].remove(person.name)
                    message = f"已将{ddate:%m月%d日}改为休息。"
                elif new_shift in FIXED_SHIFTS:
                    assignment, _ = Assignment.objects.get_or_create(
                        schedule=schedule, person=person, day=day, defaults={"shift": new_shift})
                    old_shift = assignment.shift
                    assignment.shift = new_shift
                    assignment.save()
                    if old_shift and person.name in cell.get(old_shift, []):
                        cell[old_shift].remove(person.name)
                    cell.setdefault(new_shift, [])
                    if person.name not in cell[new_shift]:
                        cell[new_shift].append(person.name)
                    message = f"已将{ddate:%m月%d日}的班次改为「{new_shift}」。"
                else:
                    error = "无效的改班请求。"

                if not error:
                    rj["per_day"] = per_day
                    # 同步每人班数统计与达标标记（保证结果页/统计与日历一致）
                    is_working = any(person.name in cell.get(s, []) for s in FIXED_SHIFTS)
                    delta = (1 if is_working else 0) - (1 if was_working else 0)
                    wc = rj.setdefault("worker_counts", {})
                    wc[person.name] = max(0, wc.get(person.name, 0) + delta)
                    tgt = rj.get("target")
                    reached = rj.setdefault("reached", {})
                    if person.name in reached and tgt:
                        reached[person.name] = wc[person.name] >= tgt
                    schedule.result_json = rj
                    schedule.save()

    start, end, days = period_range(y, m)
    assignments = {}
    if schedule:
        assignments = {a.day: a.shift for a in Assignment.objects.filter(schedule=schedule, person=person)}
        if not assignments and schedule.result_json:
            per_day = schedule.result_json.get("per_day", {})
            for d in range(days):
                for s in schedule.shifts:
                    if person.name in per_day.get(str(d), {}).get(s, []):
                        assignments[d] = s

    worked_auto = person_worked_auto(person, schedule)
    remaining = max(0, person.required_shifts - worked_auto)

    lead = start.weekday()
    cells = [None] * lead
    for d in range(days):
        cells.append({"index": d, "date": start + timedelta(days=d), "shift": assignments.get(d, "")})
    while len(cells) % 7 != 0:
        cells.append(None)
    weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]
    py, pm = month_shift(y, m, -1)
    ny, nm = month_shift(y, m, 1)

    return render(request, "scheduler/person_detail.html", {
        "person": person,
        "schedule": schedule,
        "year": y, "month": m,
        "prev_y": py, "prev_m": pm, "next_y": ny, "next_m": nm,
        "start": start, "end": end, "days": days,
        "today": date.today(),
        "weeks": weeks,
        "shifts": FIXED_SHIFTS,
        "worked_auto": worked_auto,
        "required": person.required_shifts,
        "remaining": remaining,
        "can_edit": can_edit_team(request, person.team),
        "message": message,
        "error": error,
    })


def _latest_schedules_for_month(year: int, month: int, group: Group = None):
    """某年月的班组排班（每个班组只取最新一份），可按队组过滤（保证班次展示与个人日历一致）。"""
    qs = Schedule.objects.filter(year=year, month=month)
    if group:
        qs = qs.filter(team__group=group)
    qs = qs.select_related("team").order_by("team_id", "-created_at")
    latest = {}
    for sch in qs:
        if sch.team_id not in latest:
            latest[sch.team_id] = sch
    return list(latest.values())


# ---------------------------------------------------------------------------
# 班次展示：日历显示每天每个班次人数（按班组分层）
# ---------------------------------------------------------------------------
@login_required
def shift_board(request):
    ug = user_group(request)
    groups = Group.objects.order_by("name")
    sel_group = request.GET.get("group", "")
    if ug:
        selected_group = ug
    else:
        selected_group = groups.filter(id=sel_group).first() if sel_group.isdigit() else None

    try:
        y = int(request.GET.get("y", 0))
        m = int(request.GET.get("m", 0))
    except ValueError:
        y = m = 0
    if not (y and 1 <= m <= 12):
        y, m = current_period()
    start, end, days = period_range(y, m)

    schedules = _latest_schedules_for_month(y, m, selected_group) if selected_group else []
    # board[day][shift][team_name] = [names]
    board = {d: {s: {} for s in FIXED_SHIFTS} for d in range(days)}
    for sch in schedules:
        per_day = (sch.result_json or {}).get("per_day", {})
        team_name = sch.team.name if sch.team else "未分组"
        for d in range(min(days, sch.days)):
            for s in FIXED_SHIFTS:
                names = per_day.get(str(d), {}).get(s, [])
                if names:
                    board[d][s][team_name] = names

    lead = start.weekday()
    cells = [None] * lead
    for d in range(days):
        day_total = 0
        shift_infos = []
        for s in FIXED_SHIFTS:
            teams_info = board[d][s]
            if teams_info:
                shift_infos.append({
                    "name": s,
                    "total": sum(len(n) for n in teams_info.values()),
                    "teams": [(tname, len(names)) for tname, names in teams_info.items()],
                })
                day_total += shift_infos[-1]["total"]
        cells.append({"index": d, "date": start + timedelta(days=d),
                      "shifts": shift_infos, "total": day_total})
    while len(cells) % 7 != 0:
        cells.append(None)
    weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]
    py, pm = month_shift(y, m, -1)
    ny, nm = month_shift(y, m, 1)

    return render(request, "scheduler/shift_board.html", {
        "year": y, "month": m,
        "prev_y": py, "prev_m": pm, "next_y": ny, "next_m": nm,
        "start": start, "end": end, "days": days,
        "today": date.today(),
        "weeks": weeks,
        "shifts": FIXED_SHIFTS,
        "groups": groups,
        "selected_group": selected_group,
        "schedule_count": len(schedules),
    })


@login_required
def shift_detail(request, year, month, day, shift):
    """某天某班次的详情：各班组上班人员（姓名/岗位/已上/应上），点击人名跳个人日历。"""
    ug = user_group(request)
    sel_group = request.GET.get("group", "")
    if ug:
        selected_group = ug
    else:
        selected_group = Group.objects.filter(id=sel_group).first() if sel_group.isdigit() else None
    start, end, days = period_range(year, month)
    if not (0 <= day < days) or shift not in FIXED_SHIFTS:
        return redirect("scheduler:shift_board")
    ddate = start + timedelta(days=day)

    schedules = _latest_schedules_for_month(year, month, selected_group) if selected_group else []
    snap_by_team = {}
    names_today = []
    for sch in schedules:
        per_day = (sch.result_json or {}).get("per_day", {})
        names = per_day.get(str(day), {}).get(shift, [])
        if names:
            snap_by_team[sch.team.name if sch.team else "未分组"] = names
            names_today.extend(names)

    persons = {p.name: p for p in Person.objects.prefetch_related("roles").filter(name__in=names_today)}
    schedule_map = {}
    for sch in schedules:
        if sch.team_id not in schedule_map:
            schedule_map[sch.team_id] = sch

    groups = []
    for team_name in sorted(snap_by_team):
        entries = []
        for nm in snap_by_team[team_name]:
            p = persons.get(nm)
            sch = None
            if p and p.team_id in schedule_map:
                sch = schedule_map[p.team_id]
            entries.append({
                "person": p,
                "roles": list(p.roles.values_list("name", flat=True)) if p else [],
                "worked": person_worked_auto(p, sch) if p else 0,
                "required": p.required_shifts if p else 0,
            })
        groups.append({"team": team_name, "entries": entries})

    return render(request, "scheduler/shift_detail.html", {
        "year": year, "month": month, "day": day, "shift": shift,
        "date": ddate,
        "groups": groups,
        "selected_group": selected_group,
    })


# ---------------------------------------------------------------------------
# 排班结果 / 记录
# ---------------------------------------------------------------------------
@login_required
def schedule_result(request, pk):
    record = get_object_or_404(Schedule, id=pk)
    ug = user_group(request)
    if ug and (record.team is None or record.team.group_id != ug.id):
        return redirect("scheduler:index")
    rj = record.result_json or {}
    per_day = rj.get("per_day", {})
    worker_counts = rj.get("worker_counts", {})
    reached = rj.get("reached", {})
    shifts = record.shifts or FIXED_SHIFTS
    rows = []
    for d in range(1, record.days + 1):
        ddate = record.start_date + timedelta(days=d - 1) if record.start_date else None
        cells = per_day.get(str(d - 1), {})
        rows.append({"day": d, "date": ddate, "cells": [(s, cells.get(s, [])) for s in shifts]})

    def _status(nm):
        if nm in reached:
            return "ok" if reached[nm] else "no"
        return "exempt"

    snap = {s["name"]: s for s in (record.worker_snapshot or [])}
    count_rows = sorted(
        [(nm, cnt, _status(nm),
          snap.get(nm, {}).get("worked", 0),
          snap.get(nm, {}).get("required", 0))
         for nm, cnt in worker_counts.items()],
        key=lambda kv: (0 if kv[2] == "ok" else 1, -kv[1]),
    )
    shortfall = rj.get("shortfall", [])
    capacity = None
    if record.team:
        people_count = len(worker_counts)
        exempt_count = len([n for n in (record.exempt_names or []) if n in worker_counts])
        # 用快速参数 + 排班时已求得的真实达标人数，避免再次跑求解器（省 5~10 秒）
        capacity = capacity_quick(
            people_count, record.daily_total or 0, record.days,
            (record.rest_block or {}).get("max", 4),
        )
        reached_count = sum(1 for v in reached.values() if v)
        capacity["max_fillable"] = reached_count
        capacity["needed_exempt"] = max(0, (people_count - exempt_count) - reached_count)
        capacity["people"] = people_count
        capacity["daily"] = record.daily_total or 0
        capacity["target"] = record.min_shift_target or 0
    return render(request, "scheduler/schedule_result.html", {
        "record": record,
        "shifts": shifts,
        "rows": rows,
        "count_rows": count_rows,
        "shortfall": shortfall,
        "capacity": capacity,
        "stats": {
            "total": sum(worker_counts.values()),
            "avg": round(sum(worker_counts.values()) / len(worker_counts), 1)
            if worker_counts else 0,
            "reached_count": sum(1 for v in reached.values() if v),
            "reached_total": len(reached),
            "single_rest": rj.get("single_rest", 0),
            "rest_violations": rj.get("rest_run_violations", 0),
            "target": rj.get("target"),
        },
    })


@login_required
def schedule_list(request):
    ug = user_group(request)
    records = Schedule.objects.select_related("team")
    if ug:
        records = records.filter(team__group=ug)
    return render(request, "scheduler/schedule_list.html", {"records": records})
