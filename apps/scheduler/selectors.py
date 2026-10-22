# 排班数据查询与统计（仅供页面展示，不涉及排班算法）。
import calendar
import logging
from datetime import date, timedelta

from .models import ScheduleBatch, ScheduleRecord

logger = logging.getLogger(__name__)

WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"]
SHIFT_LABELS = {"early": "早", "middle": "中", "night": "夜", "rest": "休"}
SHIFT_CLASSES = {
    "early": "cell-early",
    "middle": "cell-middle",
    "night": "cell-night",
    "rest": "cell-rest",
}


def period_range(anchor=None):
    # 排班周期：每月 25 日至次月 24 日。
    anchor = anchor or date.today()
    if anchor.day >= 25:
        start = date(anchor.year, anchor.month, 25)
    elif anchor.month == 1:
        start = date(anchor.year - 1, 12, 25)
    else:
        start = date(anchor.year, anchor.month - 1, 25)
    end = _add_months(start, 1) - timedelta(days=1)
    return start, end


def _add_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def parse_period_param(value):
    # 支持 ?period=2026-10（表示 2026-10-25 起始的排班周期）
    if not value:
        return None
    try:
        parts = value.split("-")
        year, month = int(parts[0]), int(parts[1])
        return period_range(date(year, month, 25))
    except (ValueError, TypeError, IndexError):
        return None


def period_anchor_bounds(anchor):
    # 给定锚点日期，返回该排班周期起止与相邻周期锚点（供日历切换）
    start, end = period_range(anchor)
    return start, end, _add_months(start, -1), _add_months(start, 1)


def iter_dates(start, end):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def batch_summaries(team, start, end):
    # 班组在周期内的排班批次概览（当前有效版本在前）。
    # 只用于“列出批次并标注版本性质”，不用于展示明细；明细由 build_schedule_grid 按查看模式取数。
    records = (
        ScheduleRecord.objects.filter(team=team, work_date__range=(start, end))
        .order_by("-created_at")
    )
    batches = {}
    period_archived = False
    for record in records:
        info = batches.setdefault(
            record.schedule_batch_id,
            {
                "batch_id": record.schedule_batch_id,
                "count": 0,
                "active_count": 0,
                "statuses": {},
                "is_active_record": False,
                "first_date": record.work_date,
                "last_date": record.work_date,
                "created_at": record.created_at,
            },
        )
        info["count"] += 1
        info["statuses"][record.status] = info["statuses"].get(record.status, 0) + 1
        if record.status == ScheduleRecord.Status.ARCHIVED:
            period_archived = True
        if record.status in (ScheduleRecord.Status.CONFIRMED, ScheduleRecord.Status.PUBLISHED) and record.is_active:
            info["active_count"] += 1
            info["is_active_record"] = True
        info["first_date"] = min(info["first_date"], record.work_date)
        info["last_date"] = max(info["last_date"], record.work_date)
    result = list(batches.values())
    version_heads = set(
        ScheduleBatch.objects.filter(
            team=team,
            kind=ScheduleBatch.Kind.PERIOD,
            status=ScheduleBatch.Status.CONFIRMED,
            period_start__lte=end,
            period_end__gte=start,
        ).values_list("batch_id", flat=True)
    )
    kinds = dict(
        ScheduleBatch.objects.filter(batch_id__in=list(batches.keys())).values_list(
            "batch_id", "kind"
        )
    )
    for info in result:
        # is_archived：该周期已归档（只读历史）
        info["is_archived"] = period_archived
        # is_current：整周期版本头 + 持有当前有效记录 + 周期未归档。
        # 以“是否持有当前有效记录”兜底，历史遗留的多头批次不会被误标为当前版本。
        info["is_current"] = (
            info["batch_id"] in version_heads
            and info["active_count"] > 0
            and not period_archived
        )
        # is_manual：人工记录批次（adjustment），仅在结果页标注，不作为版本头
        info["is_manual"] = kinds.get(info["batch_id"]) == ScheduleBatch.Kind.ADJUSTMENT
    result.sort(key=lambda b: (not b["is_current"], -(b["created_at"].timestamp() if b["created_at"] else 0)))
    return result


def build_schedule_grid(team, batch_id, start, end, *, active_only=None):
    # 组装排班结果网格数据（供排班结果页展示）。
    # active_only=True：只看当前有效排班（confirmed/published 且 is_active=True）；
    # active_only=False：查看历史批次原貌（该批次当期的全部记录，含已作废状态）；
    # active_only=None：自动判断（存在有效记录则按有效口径，否则按历史原貌）。
    if active_only is None:
        auto = ScheduleRecord.objects.filter(
            team=team, schedule_batch_id=batch_id, work_date__range=(start, end)
        ) if batch_id else ScheduleRecord.objects.none()
        active_only = auto.filter(status__in=ACTIVE_STATUSES, is_active=True).exists()
    if active_only:
        # 当前有效视图：整个队组、整个周期的最终有效排班（含遗留人工记录），与看板/日历同口径
        qs = ScheduleRecord.objects.filter(
            team=team, work_date__range=(start, end), status__in=ACTIVE_STATUSES, is_active=True
        )
    else:
        # 历史原貌视图：严格按指定批次（含其已作废记录）
        qs = ScheduleRecord.objects.filter(
            team=team, schedule_batch_id=batch_id, work_date__range=(start, end)
        )
    records = list(qs.select_related("employee"))
    employees = list(team.employees.order_by("employee_no"))
    known_ids = {e.pk for e in employees}
    for record in records:
        if record.employee_id not in known_ids:
            employees.append(record.employee)
            known_ids.add(record.employee_id)
    employees.sort(key=lambda e: e.employee_no)

    raw_dates = list(iter_dates(start, end))
    today = date.today()
    dates = [
        {
            "date": d,
            "day": d.day,
            "month_label": d.strftime("%m-%d"),
            "weekday": d.weekday(),
            "weekday_label": "周" + WEEKDAY_LABELS[d.weekday()],
            "is_weekend": d.weekday() >= 5,
            "is_today": d == today,
            "is_month_boundary": d.day == 25,
        }
        for d in raw_dates
    ]

    cell_map = {(r.employee_id, r.work_date): r for r in records}
    stats = {}
    shift_counts = {"early": 0, "middle": 0, "night": 0, "rest": 0}
    daily_counts = {}
    position_counts = {}
    for record in records:
        item = stats.setdefault(
            record.employee_id,
            {"early": 0, "middle": 0, "night": 0, "rest": 0, "work": 0},
        )
        if record.shift_type in item:
            item[record.shift_type] += 1
        if record.shift_type != "rest":
            item["work"] += 1
            daily_counts[record.work_date] = daily_counts.get(record.work_date, 0) + 1
        if record.position:
            position_counts[record.position] = position_counts.get(record.position, 0) + 1
        if record.shift_type in shift_counts:
            shift_counts[record.shift_type] += 1

    rows = []
    for employee in employees:
        item = stats.setdefault(
            employee.pk,
            {"early": 0, "middle": 0, "night": 0, "rest": 0, "work": 0},
        )
        sequence = [
            "work"
            if (rec := cell_map.get((employee.pk, day))) and rec.shift_type != "rest"
            else "rest"
            for day in raw_dates
        ]
        item["max_work_streak"], item["max_rest_streak"] = _streaks(sequence)
        cells = []
        for day in raw_dates:
            record = cell_map.get((employee.pk, day))
            cells.append(
                {
                    "record": record,
                    "shift": record.shift_type if record else None,
                    "label": SHIFT_LABELS.get(record.shift_type, "") if record else "",
                    "css_class": SHIFT_CLASSES.get(record.shift_type, "") if record else "",
                    "position": record.position if record else "",
                }
            )
        rows.append({"employee": employee, "cells": cells, "stat": item})

    for item in dates:
        item["daily_count"] = daily_counts.get(item["date"], 0)

    return {
        "dates": dates,
        "rows": rows,
        "record_count": len(records),
        "shift_counts": shift_counts,
        "position_counts": position_counts,
    }


def _streaks(sequence):
    max_work = max_rest = 0
    work = rest = 0
    for kind in sequence:
        if kind == "work":
            work += 1
            rest = 0
            max_work = max(max_work, work)
        else:
            rest += 1
            work = 0
            max_rest = max(max_rest, rest)
    return max_work, max_rest


ACTIVE_STATUSES = [
    ScheduleRecord.Status.CONFIRMED,
    ScheduleRecord.Status.PUBLISHED,
]


SHIFT_BOARD_ORDER = [
    ("early", "早班"),
    ("middle", "中班"),
    ("night", "晚班"),
]


def active_schedule_records(*, org=None, team=None, start=None, end=None, work_date=None, shift_key=None):
    # 当前有效排班记录：状态为已确认/已发布、未被人工删除（is_active=True），共享同一条查询口径
    qs = (
        ScheduleRecord.objects.filter(
            status__in=[ScheduleRecord.Status.CONFIRMED, ScheduleRecord.Status.PUBLISHED],
            is_active=True,
        )
        .select_related("employee", "team", "shift_config")
        .order_by("-created_at", "-id")
    )
    if team is not None:
        qs = qs.filter(team=team)
    elif org is not None:
        qs = qs.filter(team__organization=org)
    if work_date is not None:
        qs = qs.filter(work_date=work_date)
    if start is not None and end is not None:
        qs = qs.filter(work_date__range=(start, end))
    if shift_key:
        qs = qs.filter(shift_config__shift_name=shift_key)
    return qs


def period_scheduled_counts(start, end, *, org=None, team=None, employees=None, until=None):
    # 统计每名职工“当前周期开始 → 今天（含）”的实际排班班数：
    #   - 仅统计早/中/晚班的有效记录（status ∈ confirmed/published 且 is_active=True），休息不计；
    #   - 不统计未来日期（截止到今天，周期结束日之后不再延长）；
    #   - 同一职工同一天的多条异常重复记录只计 1 班，并写诊断日志，不静默掩盖数据问题；
    #   - 单次数据库聚合（按职工分组、distinct 排班日期），避免逐人查询造成 N+1。
    from datetime import date as _date

    from django.db.models import Count

    cutoff = min(end, until or _date.today())
    qs = active_schedule_records(org=org, team=team, start=start, end=cutoff).exclude(
        shift_type=ScheduleRecord.ShiftType.REST
    )
    rows = list(
        qs.values("employee_id")
        .annotate(days=Count("work_date", distinct=True), row_count=Count("id"))
        .order_by()
    )
    counts = {row["employee_id"]: row["days"] for row in rows}
    duplicated = [(row["employee_id"], row["row_count"], row["days"]) for row in rows
                  if row["row_count"] > row["days"]]
    if duplicated:
        logger.warning(
            "周期排班数发现同一职工同一天多条有效工作记录（按天去重统计）：%s",
            duplicated,
        )
    if employees is not None:
        ids = [e.pk for e in employees]
        return {eid: counts.get(eid, 0) for eid in ids}
    return counts


def build_shift_board(start, end, org=None, team=None, *, archived=False):
    # 排班日历数据源：周期内每天 × 早/中/晚 × 班组，给出该班次当前已安排人员名单。
    # archived=True 时为只读历史模式（读取归档记录），否则读取当前有效记录。
    seen = set()
    buckets = {}
    source = (
        archived_records(org=org, team=team, start=start, end=end)
        if archived
        else active_schedule_records(org=org, team=team, start=start, end=end)
    )
    for record in source:
        if record.shift_type == ScheduleRecord.ShiftType.REST:
            continue
        shift_key = record.shift_config.shift_name
        key = (record.employee_id, record.work_date, shift_key)
        if key in seen:
            continue
        seen.add(key)
        shift = buckets.setdefault(record.work_date, {}).setdefault(shift_key, {})
        entry = shift.setdefault(
            record.team_id,
            {"id": record.team_id, "name": record.team.name, "code": record.team.code, "people": []},
        )
        entry["people"].append(
            {
                "employee_id": record.employee_id,
                "employee_no": record.employee.employee_no,
                "name": record.employee.name,
                "position": record.position,
                "record_id": record.pk,
            }
        )

    days = []
    current = start
    while current <= end:
        day_buckets = buckets.get(current, {})
        shifts = []
        for key, label in SHIFT_BOARD_ORDER:
            teams = day_buckets.get(key, {})
            team_list = []
            for entry in sorted(teams.values(), key=lambda t: t["name"]):
                entry = dict(entry)
                entry["people"].sort(key=lambda p: p["employee_no"])
                team_list.append(entry)
            shifts.append({"key": key, "label": label, "teams": team_list, "total": sum(len(t["people"]) for t in team_list)})
        days.append(
            {
                "date": current,
                "weekday": "周" + WEEKDAY_LABELS[current.weekday()],
                "shifts": shifts,
            }
        )
        current += timedelta(days=1)
    return {"days": days, "leading_blanks": range(start.weekday())}


def shift_board_day_detail(work_date, shift_key, org=None, team=None, *, archived=False):
    # 某日某班次的人员名单（按班组分组）：当前有效排班，或只读历史模式下的归档记录
    seen = set()
    rows = []
    source = (
        archived_records(org=org, team=team, work_date=work_date, shift_key=shift_key)
        if archived
        else active_schedule_records(
            org=org, team=team, work_date=work_date, shift_key=shift_key
        )
    )
    for record in source.exclude(shift_type=ScheduleRecord.ShiftType.REST):
        if record.employee_id in seen:
            continue
        seen.add(record.employee_id)
        rows.append(
            {
                "record_id": record.pk,
                "employee_id": record.employee_id,
                "employee_no": record.employee.employee_no,
                "name": record.employee.name,
                "team": record.team.name,
                "team_id": record.team_id,
                "position": record.position,
                "shift_type": record.shift_type,
                "shift_label": record.get_shift_type_display(),
            }
        )
    rows.sort(key=lambda r: (r["team"], r["employee_no"]))
    groups = {}
    for row in rows:
        group = groups.setdefault(
            row["team_id"], {"id": row["team_id"], "name": row["team"], "people": []}
        )
        group["people"].append(row)
    return rows, sorted(groups.values(), key=lambda g: g["name"])


def board_candidates(org, work_date, team=None, shift_name=None):
    # 可添加人员：当前队组在册职工中，当天尚无任何有效排班记录（即尚未被安排到该日班次）的人员，按班组分组
    from apps.personnel.models import Employee

    employees = (
        Employee.objects.filter(team__organization=org, is_active=True)
        .select_related("team")
        .order_by("team__name", "employee_no")
    )
    if team is not None:
        employees = employees.filter(team=team)
    if shift_name:
        # 只允许把“归属该班次”的人员添加到该班次（不同班次人员范围互相隔离）
        employees = employees.filter(
            shift_config__shift_name=shift_name, shift_config__is_active=True
        )
    # 当天已有任何有效记录（含休息）的人员都不作为“可添加”候选：
    # 休息→上班必须走「改班」，否则会出现“页面列出了却提示不能添加”的无效选项。
    busy = set(
        active_schedule_records(org=org, work_date=work_date)
        .values_list("employee_id", flat=True)
    )
    groups = {}
    for employee in employees:
        if employee.pk in busy:
            continue
        group = groups.setdefault(
            employee.team_id, {"id": employee.team_id, "name": employee.team.name, "people": []}
        )
        group["people"].append(
            {
                "employee_id": employee.pk,
                "employee_no": employee.employee_no,
                "name": employee.name,
                "position": employee.position,
            }
        )
    return sorted(groups.values(), key=lambda g: g["name"])


def period_is_archived(start, end, *, org=None, team=None) -> bool:
    # 该周期在给定范围内是否已归档（用于看板/个人日历切换只读历史模式）
    qs = ScheduleRecord.objects.filter(
        work_date__range=(start, end), status=ScheduleRecord.Status.ARCHIVED
    )
    if team is not None:
        qs = qs.filter(team=team)
    elif org is not None:
        qs = qs.filter(team__organization=org)
    return qs.exists()


def archived_records(*, org=None, team=None, start=None, end=None, work_date=None, shift_key=None):
    # 归档记录查询（只读历史模式使用；绝不参与当前有效排班口径）
    qs = (
        ScheduleRecord.objects.filter(
            status=ScheduleRecord.Status.ARCHIVED, is_active=True
        )
        .select_related("employee", "team", "shift_config")
        .order_by("-created_at", "-id")
    )
    if team is not None:
        qs = qs.filter(team=team)
    elif org is not None:
        qs = qs.filter(team__organization=org)
    if work_date is not None:
        qs = qs.filter(work_date=work_date)
    if start is not None and end is not None:
        qs = qs.filter(work_date__range=(start, end))
    if shift_key:
        qs = qs.filter(shift_config__shift_name=shift_key)
    return qs

def dashboard_trend_series(orgs, *, start=None, end=None, is_super=False):
    # 首页排班人数趋势：当前周期每天 × 班次的实际排班人数（按当前有效记录聚合）。
    #   - 只统计早/中/晚班；休息、草稿、被替代、已取消、归档记录一律不计入；
    #   - 同一职工同一天多条异常重复记录只计 1 人，并写诊断日志，不静默掩盖数据问题；
    #   - 单次数据库聚合（按 队组/日期/班次 分组，distinct 职工），不在前端逐条计算；
    #   - 普通管理员：本队组 3 条班次折线 + 1 条当日总人数折线；
    #     超级管理员：每个队组各 3 条班次折线（队组+班次），不把不同队组混成一条线。
    from django.db.models import Count

    start, end = (start, end) if start and end else period_range()
    days = list(iter_dates(start, end))
    labels = [f"{day:%m-%d}" for day in days]
    org_list = list(orgs.order_by("name")) if hasattr(orgs, "order_by") else list(orgs)
    org_names = {org.pk: org.name for org in org_list}
    shift_order = [
        (ScheduleRecord.ShiftType.EARLY, "早班"),
        (ScheduleRecord.ShiftType.MIDDLE, "中班"),
        (ScheduleRecord.ShiftType.NIGHT, "夜班"),
    ]
    rows = (
        ScheduleRecord.objects.filter(
            team__organization__in=org_list,
            work_date__range=(start, end),
            status__in=ACTIVE_STATUSES,
            is_active=True,
        )
        .exclude(shift_type=ScheduleRecord.ShiftType.REST)
        .values("team__organization_id", "work_date", "shift_type")
        .annotate(people=Count("employee_id", distinct=True), row_count=Count("id"))
        .order_by()
    )
    duplicates = []
    daily = {org.pk: {day: {shift: 0 for shift, _ in shift_order} for day in days} for org in org_list}
    for row in rows:
        org_id = row["team__organization_id"]
        shift = row["shift_type"]
        if org_id not in daily or shift not in daily[org_id].get(row["work_date"], {}):
            continue
        daily[org_id][row["work_date"]][shift] = row["people"]
        if row["row_count"] > row["people"]:
            duplicates.append((org_id, row["work_date"], shift, row["row_count"], row["people"]))
    if duplicates:
        logger.warning("排班人数趋势发现同一职工同一天多条有效记录（按人去重统计）：%s", duplicates)

    series = []
    for org in org_list:
        org_has_data = any(
            daily[org.pk][day][shift] > 0 for day in days for shift, _ in shift_order
        )
        if is_super and not org_has_data:
            continue  # 超级管理员视图：没有数据的队组不画全 0 折线，避免图例拥挤
        for shift, shift_label in shift_order:
            series.append(
                {
                    "name": f"{org.name}{shift_label}" if is_super else shift_label,
                    "key": f"org{org.pk}-{shift}",
                    "org": org.name,
                    "shift": shift,
                    "data": [daily[org.pk][day][shift] for day in days],
                }
            )
        if not is_super:
            series.append(
                {
                    "name": "当日总人数",
                    "key": f"org{org.pk}-total",
                    "org": org.name,
                    "shift": "total",
                    "data": [
                        sum(daily[org.pk][day][shift] for shift, _ in shift_order)
                        for day in days
                    ],
                }
            )
    return {
        "labels": labels,
        "dates": [day.isoformat() for day in days],
        "series": series,
        "is_super": is_super,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "empty": not any(any(series_item["data"]) for series_item in series),
    }
