# -*- coding: utf-8 -*-
import calendar
import re
from datetime import date, timedelta

from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
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
            if not url_has_allowed_host_and_scheme(
                next_url, allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                next_url = "/"
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


def _post_int(value, default=None):
    """把请求里的 id 参数安全转成 int，失败返回 default。

    直接拿用户输入的字符串喂给 ORM 的 filter(id=...) 会抛
    ``ValueError: Field 'id' expected a number`` 导致 500，这里统一兜底。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_year_month(request):
    """从 GET 参数解析 (year, month)，非法或越界时回退到当前周期。

    越界的 y（负数 / >9999）或 m 传给 datetime.date 会抛 ValueError → 500，
    因此在入口处钳制。
    """
    try:
        y = int(request.GET.get("y", 0) or 0)
        m = int(request.GET.get("m", 0) or 0)
    except (TypeError, ValueError):
        y = m = 0
    if not (1 <= y <= 9999 and 1 <= m <= 12):
        return current_period()
    return y, m


def _assignment_matrix(schedule: Schedule):
    """从 Assignment 表一次查询聚合出排班明细矩阵（唯一数据源）。

    返回 (per_day, worker_counts, by_person)：
        per_day[day][shift] = [names]     day 为 int、shift 为 str
        worker_counts[name] = 上班天数     （只含有班的人）
        by_person[name][day] = shift      个人日历快速查询
    """
    shifts = schedule.shifts or FIXED_SHIFTS
    per_day = {d: {s: [] for s in shifts} for d in range(schedule.days)}
    worker_counts = {}
    by_person = {}
    for a in Assignment.objects.filter(schedule=schedule).select_related("person"):
        name = a.person.name
        per_day.setdefault(a.day, {}).setdefault(a.shift, []).append(name)
        worker_counts[name] = worker_counts.get(name, 0) + 1
        by_person.setdefault(name, {})[a.day] = a.shift
    return per_day, worker_counts, by_person


def _worked_auto_map(persons, schedule: Schedule = None) -> dict:
    """批量计算「已上班数」= 导入初始值 + 排班中日期已过的上班天数。

    一次查询该排班的所有明细，避免对每个人各查一次（N+1 查询）。
    返回 {person_id: 已上班数}。
    """
    result = {p.id: p.worked_so_far for p in persons}
    if not schedule or not schedule.start_date:
        return result
    today = date.today()
    days_by_person = {}
    for pid, day in Assignment.objects.filter(
        schedule=schedule, person__in=list(persons)
    ).values_list("person_id", "day"):
        days_by_person.setdefault(pid, []).append(day)
    for pid, days in days_by_person.items():
        result[pid] += sum(1 for d in days if schedule.start_date + timedelta(days=d) <= today)
    return result


def person_worked_auto(person: Person, schedule: Schedule = None) -> int:
    """已上班数（按日期自动计算）= 导入初始值 + 排班中「日期已过」的上班天数。"""
    count = person.worked_so_far
    if schedule and schedule.start_date:
        today = date.today()
        days = Assignment.objects.filter(
            schedule=schedule, person=person).values_list("day", flat=True)
        count += sum(1 for d in days if schedule.start_date + timedelta(days=d) <= today)
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
def _parse_import_text(text: str, group_names=None):
    """解析导入文本，返回 [(队组, 班组, 姓名, [岗位...], 已上班数, 应上班数, 默认班次), ...]。

    每行两种格式：
      ① 班组-姓名-岗位1,岗位2-已上班数-应上班数-默认班次          （队组账号用）
      ② 队组-班组-姓名-岗位1,岗位2-已上班数-应上班数-默认班次      （超管用，最前方加队组）
      - 默认班次可写 早班/中班/晚班，写在最后、可省略（省略则保持默认「早班」）
    """
    group_names = group_names or set()
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
            first = parts.pop(0)
        else:
            first = ""
        # 第一个字段若是已知队组名，则它是「队组」，第二个字段才是「班组」
        group = ""
        if first in group_names:
            group = first
            team = parts.pop(0) if parts else ""
        else:
            team = first
        if not parts:
            continue
        name = parts.pop(0)
        roles = []
        for p in parts:
            roles.extend([r.strip() for r in re.split(r"[,，、]+", p) if r.strip()])
        items.append((group, team, name, roles, worked, required, default_shift))
    return items


def _rows_from_excel(raw_bytes):
    """从 Excel（.xlsx/.xls）二进制解析出人员文本行（每行单元格用 - 连接）。"""
    from io import BytesIO
    import openpyxl
    wb = openpyxl.load_workbook(BytesIO(raw_bytes), read_only=True, data_only=True)
    ws = wb.active
    lines = []
    for row in ws.iter_rows(values_only=True):
        cells = [str(c).strip() if c is not None else "" for c in row]
        cells = [c for c in cells if c]
        if cells:
            lines.append("-".join(cells))
    return lines


def _rows_from_csv(text):
    """从 CSV 文本解析出人员文本行（每行单元格用 - 连接）。"""
    import csv
    import io
    lines = []
    for row in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in row if c.strip()]
        if cells:
            lines.append("-".join(cells))
    return lines


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
    if request.GET.get("warn") == "daily":
        error = "⚠️ 每天应上人数大于该班组启用人数，无法排班（每人每天最多上 1 班）。请降低每天人数或增加启用人员。"

    if request.method == "POST":
        action = request.POST.get("action", "")
        if user_role(request) == "member":
            error = "队员账号为只读，只能查看，不能修改排班数据。"
        elif action == "save_constraints":
            tid = _post_int(request.POST.get("team_id"))
            team = teams.filter(id=tid).first() if tid else None
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
                        try:
                            count = int(cnt or 0)
                        except (TypeError, ValueError):
                            count = 0
                        role_reqs[rn] = {"op": op or ">=", "count": count}
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
                active_count = Person.objects.filter(team=team, is_active=True).count()
                warn = "&warn=daily" if team.daily_headcount > active_count else ""
                return redirect(f"{request.path}?team={team.id}&saved=1{warn}{gq}")

        elif action == "batch_shift":
            # 批量修改默认班次（用于中班/夜班倒班时统一切换）
            pids = [pid for pid in (_post_int(v) for v in request.POST.getlist("person_ids")) if pid is not None]
            new_shift = request.POST.get("batch_shift", "")
            if new_shift not in FIXED_SHIFTS:
                error = "无效的班次。"
            elif not pids:
                error = "请先勾选要修改的人员。"
            else:
                qs = Person.objects.filter(id__in=pids)
                if ug:
                    qs = qs.filter(team__group=ug)
                n = qs.update(default_shift=new_shift)
                message = f"已把 {n} 名人员的默认班次改为「{new_shift}」。"
                return redirect(request.get_full_path())

        elif action == "import":
            text = request.POST.get("import_text", "")
            uploaded = request.FILES.get("import_file")
            parse_error = ""
            if uploaded:
                fname = (uploaded.name or "").lower()
                raw = uploaded.read()
                try:
                    if fname.endswith((".xlsx", ".xls", ".xlsm")):
                        # Excel：按单元格读，每行拼成「班组-姓名-岗位-已上-应上-班次」
                        text = "\n".join(_rows_from_excel(raw))
                    elif fname.endswith(".csv"):
                        # CSV：按逗号分列，每行拼成同样格式
                        csv_text = raw.decode("utf-8-sig", errors="ignore")
                        text = "\n".join(_rows_from_csv(csv_text))
                    else:
                        # 纯文本（txt / 粘贴）
                        text = raw.decode("utf-8-sig", errors="ignore")
                except Exception as e:  # noqa: BLE001
                    parse_error = f"文件解析失败：{e}"
                    text = ""
            group_names = set(Group.objects.values_list("name", flat=True))
            parsed = _parse_import_text(text, group_names)
            if parse_error:
                error = parse_error
            elif not parsed:
                error = "没有解析到任何人员，请检查导入格式。"
            else:
                created_p = 0
                row_errors = []
                for grp, team, nm, roles, worked, required, default_shift in parsed:
                    try:
                        person, is_new = Person.objects.get_or_create(name=nm)
                        if is_new:
                            created_p += 1
                        # 归属队组：文本里写了队组 > 账号自己的队组 > URL 指定队组 > 空
                        if grp:
                            g = Group.objects.filter(name=grp).first()
                        else:
                            g = ug or selected_group
                        # 归属班组
                        tname = team or "检修班"
                        if g:
                            person.team, _ = Team.objects.get_or_create(group=g, name=tname)
                        else:
                            person.team = Team.objects.filter(name=tname).first()
                        person.worked_so_far = worked
                        person.required_shifts = required
                        # 默认班次：显式写了就用；没写保持默认「早班」
                        if default_shift:
                            person.default_shift = default_shift
                        person.save()
                        for rn in roles:
                            role, _ = Role.objects.get_or_create(name=rn)
                            person.roles.add(role)
                    except Exception as e:  # noqa: BLE001 —— 单行出错不中断整体导入
                        row_errors.append(f"{nm}: {e}")
                message = (f"导入完成：新增人员 {created_p} 人"
                           f"{'（已归入本队组「' + ug.name + '」的班组）' if ug else ''}"
                           f"，共处理 {len(parsed)} 条记录。")
                if row_errors:
                    error = "部分人员导入失败：" + "；".join(row_errors[:5])

        elif action == "delete":
            pid = _post_int(request.POST.get("person_id"))
            if pid:
                qs = Person.objects.filter(id=pid)
                if ug:
                    qs = qs.filter(team__group=ug)
                qs.delete()
                message = "已删除该人员。"

        elif action == "add_team":
            tn = (request.POST.get("team_name") or "").strip()
            # 队组管理员新增的班组自动归入自己的队组；超管需要先选队组
            target_group = ug or selected_group
            if not target_group:
                error = "请先选择一个队组，再新增班组。"
            elif not tn:
                error = "请输入班组名称。"
            else:
                Team.objects.get_or_create(group=target_group, name=tn)
                message = f"已添加班组「{tn}」。"
                return redirect(f"{request.path}?team={tn}{gq}")

        elif action == "delete_team":
            tid = _post_int(request.POST.get("team_id"))
            confirm = (request.POST.get("confirm_text") or "").strip()
            team = Team.objects.filter(id=tid).first() if tid else None
            if not team:
                error = "班组不存在或已被删除。"
            elif ug and team.group_id != ug.id:
                error = "只能删除本队组的班组。"
            elif confirm != "删除":
                error = "确认失败：请输入「删除」两个字才能删除班组。"
            else:
                tname = team.name
                # 删除班组：其人员变为未分组，排班记录保留但失去班组归属
                team.delete()
                from urllib.parse import quote
                return redirect(f"{request.path}?deleted={quote(tname)}{gq}")

        elif action == "generate":
            tid = _post_int(request.POST.get("team_id"))
            team = teams.filter(id=tid).first() if tid else None
            if team:
                return redirect(f"{request.path}?action=generate&team={team.id}{gq}")

    # 生成排班（使用该班组存储的约束）
    if request.GET.get("action") == "generate" and default_team:
        return _run_generate(request, default_team)

    # 该班人员（含按日期自动计算的已上班数）
    persons = Person.objects.filter(team=default_team).select_related("team").prefetch_related("roles").order_by("name") \
        if default_team else Person.objects.none()
    # 批量计算已上班数：取该班组最新的一份排班（order_by -created_at 后取第一条）
    latest_schedule = Schedule.objects.filter(team=default_team).order_by("-created_at").first() \
        if default_team else None
    persons_list = list(persons)
    worked_map = _worked_auto_map(persons_list, latest_schedule) if latest_schedule else {}
    rows = [(p, worked_map.get(p.id, p.worked_so_far)) for p in persons_list]

    # 固定岗位下拉选项：该班所有人的岗位 去重后的唯一集合（元组），并合并已配置的岗位
    if default_team:
        person_roles = set()
        for p in persons_list:
            person_roles.update(p.roles.values_list("name", flat=True))
        person_roles.update((default_team.role_reqs or {}).keys())
        team_role_options = tuple(sorted(person_roles))
    else:
        team_role_options = ()

    # 连休最大值默认值（规则4：最大连休 = 10 - 至少应上班数//3）
    y0, m0 = current_period()
    _, _, period_days = period_range(y0, m0)
    min_tgt = default_team.min_shift_target if default_team else 18
    default_rest_max = max(2, 10 - (min_tgt // 3))

    # 容量预估（提示最多能有多少人排满，需要豁免几人；只统计启用人员）
    capacity = None
    if default_team:
        people_count = Person.objects.filter(team=default_team, is_active=True).count()
        exempt_count = len([n for n in (default_team.exempt_names or []) if
                            Person.objects.filter(team=default_team, name=n, is_active=True).exists()])
        # 预估 target：全局「至少应上班数」优先，否则用每人「应上班数」的平均
        if (default_team.min_shift_target or 0) > 0:
            cap_target = default_team.min_shift_target
        else:
            reqs = [p.required_shifts for p in persons_list if p.required_shifts > 0]
            cap_target = (sum(reqs) // len(reqs)) if reqs else 18
        capacity = capacity_analysis(
            people_count, default_team.daily_headcount or 0, period_days,
            max(2, 10 - (cap_target // 3)), cap_target, exempt_count,
        )

    # 每个岗位的持有人数（用于实时"岗位条件可行性"检查；只统计启用人员）
    role_holder_counts = {}
    if default_team:
        for p in persons_list:
            if not p.is_active:
                continue
            for rn in p.roles.values_list("name", flat=True):
                role_holder_counts[rn] = role_holder_counts.get(rn, 0) + 1

    # 启用人数（参与排班的人数，前端「约束实时自检」用它算需豁免人数）
    active_person_count = Person.objects.filter(team=default_team, is_active=True).count() if default_team else 0

    return render(request, "scheduler/team_manage.html", {
        "teams": teams,
        "team": default_team,
        "user_group": ug,
        "groups": groups,
        "selected_group": selected_group,
        "shifts": FIXED_SHIFTS,
        "rows": rows,
        "team_persons": [p for p, _ in rows],
        "active_person_count": active_person_count,
        "team_role_options": team_role_options,
        "role_holder_counts": role_holder_counts,
        "default_rest_max": default_rest_max,
        "capacity": capacity,
        "period_days": period_days,
        "is_team_user": ug is not None,
        "is_super": user_role(request) == "super",
        "can_manage": user_role(request) in ("super", "team_admin"),
        "can_edit": can_edit_team(request, default_team),
        "roles": Role.objects.order_by("name"),
        "message": message,
        "error": error,
    })


def _run_generate(request, team: Team):
    """用班组存储的约束调用引擎生成排班，返回重定向到结果页。"""
    if not team.daily_headcount or team.daily_headcount <= 0:
        from urllib.parse import quote
        return redirect(f"/teams/?group={team.group_id or ''}&team={team.id}&error=daily")
    y, m = current_period()
    start, end, days = period_range(y, m)
    persons = Person.objects.filter(team=team, is_active=True).prefetch_related("roles").order_by("name")
    if not persons:
        return redirect(f"/teams/?group={team.group_id or ''}&team={team.id}")
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

    # 容量预估的 target：全局「至少应上班数」优先；否则用每人「应上班数」的平均
    if target_global > 0:
        cap_target = target_global
    else:
        reqs = [p.required_shifts for p in persons if p.required_shifts > 0]
        cap_target = (sum(reqs) // len(reqs)) if reqs else 18
    # 规则4：连休 2 ~ (10 - 至少应上班数//3) 天（已去掉「10天最多6班」工作窗口，
    # 休息天数由每天应上人数自然决定：人多则少休、人少则多休）
    rest_max = max(2, 10 - (cap_target // 3)) if cap_target > 0 else 4

    # 容量预估：最多能有多少人排满目标（豁免人员不参与休息计算，不占最少班数）
    non_exempt_count = len([p for p in persons if p.name not in exempt_set])
    cap = capacity_analysis(
        len(persons), team.daily_headcount or 0, days,
        rest_max, cap_target,
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
        "rest_block": {"min": 2, "max": rest_max},
    }

    def _build_req():
        req = {}
        has_exempt = bool(exempt_set)
        for p in persons:
            # 规则3：全局「至少应上班数」优先；未填时才用每人「应上班数」
            if target_global > 0:
                tgt = target_global - p.worked_so_far
            elif p.required_shifts > 0:
                tgt = p.required_shifts - p.worked_so_far
            else:
                tgt = 0
            if tgt <= 0 or p.name in exempt_set:
                continue
            if has_exempt:
                # 有豁免人员：非豁免恰好上满 tgt，剩余班次交给豁免人员平分
                req[p.name] = {"target": tgt, "min": tgt, "max": tgt}
            else:
                # 无豁免人员：非豁免最低 tgt（硬下限、不设上限），
                # 多出来的班次由求解器均衡分配给这些人
                req[p.name] = {"target": tgt, "min": tgt}
        return req

    config = dict(base_config)
    config["worker_shift_req"] = _build_req()
    result = build_schedule(config, time_limit_seconds=30, phase2_seconds=10)

    if not result.feasible:
        # 整体无解：创建记录保存诊断信息（无排班明细）
        record = Schedule.objects.create(
            team=team, year=y, month=m, start_date=start, days=days,
            shifts=FIXED_SHIFTS, shift_demand={}, daily_total=team.daily_headcount,
            role_reqs=team.role_reqs or {}, min_shift_target=team.min_shift_target,
            exempt_names=team.exempt_names or [],
            rest_block={"min": 2, "max": rest_max},
            work_window={}, worker_snapshot=worker_snapshot,
            status=result.status, message=result.message, diagnostics=result.diagnostics,
        )
        return redirect("scheduler:schedule_result", pk=record.id)

    record = Schedule.objects.create(
        team=team, year=y, month=m, start_date=start, days=days,
        shifts=FIXED_SHIFTS, shift_demand={}, daily_total=team.daily_headcount,
        role_reqs=team.role_reqs or {},
        min_shift_target=team.min_shift_target,
        exempt_names=team.exempt_names or [],
        rest_block={"min": 2, "max": rest_max},
        work_window={},
        worker_snapshot=worker_snapshot,
        status=result.status,
        message=result.message,
        diagnostics=result.diagnostics,
        single_rest=result.single_rest,
        rest_run_violations=result.rest_run_violations,
    )
    # 每个班组每个月只保留一份排班：删除该班组同月份更旧的排班（含其明细），
    # 保证班次展示、个人日历、排班记录三者数据一致
    old_schedules = Schedule.objects.filter(team=team, year=y, month=m).exclude(id=record.id)
    if old_schedules.exists():
        Assignment.objects.filter(schedule__in=old_schedules).delete()
        old_schedules.delete()
    # 明细统一写入 Assignment 表（唯一数据源）
    name_map = {p.name: p for p in persons}
    assign_rows = []
    for d in range(days):
        for s in FIXED_SHIFTS:
            for nm in result.per_day[d][s]:
                p = name_map.get(nm)
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
        selected = [rid for rid in (_post_int(v) for v in request.POST.getlist("roles")) if rid is not None]
        new_role_names = request.POST.get("new_roles", "")
        person.roles.set(Role.objects.filter(id__in=selected))
        for rn in re.split(r"[,，、\s]+", new_role_names.strip()):
            if rn:
                role, _ = Role.objects.get_or_create(name=rn)
                person.roles.add(role)
        team_id = _post_int(request.POST.get("team"))
        if team_id:
            tq = Team.objects.filter(id=team_id)
            if ug:
                tq = tq.filter(group=ug)
            new_team = tq.first()
            if new_team is not None:
                person.team = new_team
        else:
            person.team = None
        person.default_shift = request.POST.get("default_shift") or "早班"
        person.is_active = request.POST.get("is_active") == "on"
        try:
            person.worked_so_far = max(0, int(request.POST.get("worked_so_far") or 0))
            person.required_shifts = max(0, int(request.POST.get("required_shifts") or 0))
        except ValueError:
            pass
        person.save()
        message = f"已保存「{person.name}」的信息。"
    # 岗位只显示「该人员所属队组」里出现过的岗位（并保留本人已有岗位），
    # 避免把全系统其它队组/导入误产生的无关岗位（乱码、人名等）列出来
    scope_group = person.team.group if person.team else ug
    if scope_group:
        all_roles = Role.objects.filter(
            Q(persons__team__group=scope_group) | Q(persons=person)
        ).distinct().order_by("name")
    else:
        all_roles = person.roles.all().order_by("name")

    return render(request, "scheduler/person_edit.html", {
        "person": person,
        "all_roles": all_roles,
        "teams": Team.objects.filter(group=ug) if ug else Team.objects.order_by("name"),
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
    y, m = _parse_year_month(request)

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
                # 明细只存 Assignment 表：改班 = 增删改 Assignment，无需再同步结果 JSON
                if new_shift == "休息":
                    Assignment.objects.filter(schedule=schedule, person=person, day=day).delete()
                    message = f"已将{ddate:%m月%d日}改为休息。"
                elif new_shift in FIXED_SHIFTS:
                    assignment, _ = Assignment.objects.get_or_create(
                        schedule=schedule, person=person, day=day, defaults={"shift": new_shift})
                    assignment.shift = new_shift
                    assignment.save()
                    message = f"已将{ddate:%m月%d日}的班次改为「{new_shift}」。"
                else:
                    error = "无效的改班请求。"

    start, end, days = period_range(y, m)
    assignments = {}
    if schedule:
        assignments = {a.day: a.shift for a in Assignment.objects.filter(schedule=schedule, person=person)}

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

    y, m = _parse_year_month(request)
    start, end, days = period_range(y, m)

    schedules = _latest_schedules_for_month(y, m, selected_group) if selected_group else []
    # board[day][shift][team_name] = [names]
    board = {d: {s: {} for s in FIXED_SHIFTS} for d in range(days)}
    for sch in schedules:
        per_day, _, _ = _assignment_matrix(sch)
        team_name = sch.team.name if sch.team else "未分组"
        for d in range(min(days, sch.days)):
            for s in FIXED_SHIFTS:
                names = per_day.get(d, {}).get(s, [])
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
        "user_group": ug,
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
    if not (1 <= year <= 9999 and 1 <= month <= 12):
        return redirect("scheduler:shift_board")
    start, end, days = period_range(year, month)
    if not (0 <= day < days) or shift not in FIXED_SHIFTS:
        return redirect("scheduler:shift_board")
    ddate = start + timedelta(days=day)

    schedules = _latest_schedules_for_month(year, month, selected_group) if selected_group else []
    snap_by_team = {}
    names_today = []
    for sch in schedules:
        per_day, _, _ = _assignment_matrix(sch)
        names = per_day.get(day, {}).get(shift, [])
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
        "is_member": user_role(request) == "member",
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

    shifts = record.shifts or FIXED_SHIFTS
    # 明细统一从 Assignment 表聚合（唯一数据源）
    per_day, wc, _ = _assignment_matrix(record)
    snap = {s["name"]: s for s in (record.worker_snapshot or [])}
    # worker_counts 包含所有人（无班的人计 0）
    worker_counts = {nm: 0 for nm in snap}
    worker_counts.update(wc)

    # 实时重算达标 / 未达标（规则3：全局「至少应上班数」优先，否则每人「应上班数」）
    exempt = set(record.exempt_names or [])
    target_global = record.min_shift_target or 0
    reached = {}
    shortfall = []
    for nm, s in snap.items():
        if nm in exempt:
            continue
        if target_global > 0:
            tgt = target_global - s["worked"]
        elif s["required"] > 0:
            tgt = s["required"] - s["worked"]
        else:
            tgt = 0
        if tgt <= 0:
            continue
        cnt = worker_counts.get(nm, 0)
        reached[nm] = cnt >= tgt
        if cnt < tgt:
            shortfall.append({"name": nm, "target": tgt, "count": cnt})
    shortfall.sort(key=lambda x: x["target"] - x["count"], reverse=True)

    rows = []
    for d in range(1, record.days + 1):
        ddate = record.start_date + timedelta(days=d - 1) if record.start_date else None
        cells = per_day.get(d - 1, {})
        rows.append({"day": d, "date": ddate, "cells": [(s, cells.get(s, [])) for s in shifts]})

    def _status(nm):
        if nm in reached:
            return "ok" if reached[nm] else "no"
        return "exempt"

    count_rows = sorted(
        [(nm, cnt, _status(nm),
          snap.get(nm, {}).get("worked", 0),
          snap.get(nm, {}).get("required", 0))
         for nm, cnt in worker_counts.items()],
        key=lambda kv: (0 if kv[2] == "ok" else 1, -kv[1]),
    )

    capacity = None
    if record.team:
        people_count = len(worker_counts)
        exempt_count = len([n for n in (record.exempt_names or []) if n in worker_counts])
        capacity = capacity_quick(
            people_count, record.daily_total or 0, record.days,
            (record.rest_block or {}).get("max", 4),
            target_global if target_global > 0 else 18,
        )
        reached_count = sum(1 for v in reached.values() if v)
        capacity["max_fillable"] = reached_count
        capacity["needed_exempt"] = max(0, (people_count - exempt_count) - reached_count)
        capacity["people"] = people_count
        capacity["daily"] = record.daily_total or 0
        capacity["target"] = target_global
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
            "single_rest": record.single_rest,
            "rest_violations": record.rest_run_violations,
            "target": target_global or None,
        },
    })


@login_required
def schedule_list(request):
    # 队员（只读）不可访问排班记录
    if user_role(request) == "member":
        return redirect("scheduler:index")
    ug = user_group(request)
    records = Schedule.objects.select_related("team")
    if ug:
        records = records.filter(team__group=ug)
    return render(request, "scheduler/schedule_list.html", {"records": records})
