# -*- coding: utf-8 -*-
"""井下排班系统 —— 排班算法模型（单一文件）。

本文件是整个系统的「算法模型」部分，独立、可复用、不依赖 Django：
把「排班问题」建模成 0/1 整数变量，交给 Google OR-Tools 的 CP-SAT 求解器
（from ortools.sat.python import cp_model），找出满足所有硬性约束、
并尽量满足软性目标的方案。

求解策略
--------
当设置了「最少出勤班数 + 豁免名单」时，采用**两阶段求解**：
1. 阶段一：最大化“达到最少班数的人数”（用户的核心目标，模型不带偏离变量，搜索更快）；
2. 阶段二：固定阶段一的达标人数，再最小化每人班数与目标的偏离、尽量贴近默认班次，
   得到更均衡的班表（用阶段一的解作为 hint 起步，保证能快速找到可行解）。

支持的能力（对应需求）
----------------------
1. 每天下井总人数 / 每班每天人数     -> ``daily_total`` / ``shift_demand``
2. 岗位人数条件（至少/至多/等于）    -> ``role_req``（兼容旧字段 ``role_min``）
3. 每人每天最多上一个班              -> 内置
4. 连休 2~4 天（不允许单休）         -> ``rest_block``（硬约束）
5. 每人应上最少班数、可豁免、最大化达标人数 -> ``min_shift_target`` + ``exempt_workers``
6. 任意 N 天最多上 M 班（工作窗口）  -> ``work_window``
7. 默认班次偏好（早/中/晚）          -> ``worker_default_shift``
8. 可行性预检 + 友好诊断             -> ``validate_config``
9. 容量预估（最多能满班几人/需豁免几人）-> ``capacity_analysis``（纯函数）

用法（Django 之外的独立调用）
----------------------------
    from project.app.scheduler.scheduling import build_schedule

    config = {
        "workers": [{"name": "张三", "roles": ["电工"]}, ...],
        "shifts": ["早班", "中班", "晚班"],
        "days": 30,
        "daily_total": 13,
        "role_req": {"电工": {"op": ">=", "count": 2}},
        "min_shift_target": 18,
        "exempt_workers": ["张三"],
        "rest_block": {"min": 2, "max": 4},
        "work_window": {"length": 10, "max_work": 6},
    }
    result = build_schedule(config)
    result.print_summary()

也可以直接运行本文件看示例：python project/app/scheduler/scheduling.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from ortools.sat.python import cp_model


# ===========================================================================
# 一、结果对象
# ===========================================================================
@dataclass
class ScheduleResult:
    """排班求解结果。"""

    status: str = ""                 # OPTIMAL / FEASIBLE / INFEASIBLE / UNKNOWN
    feasible: bool = False
    message: str = ""                # 给用户看的一句话结果
    diagnostics: List[str] = field(default_factory=list)  # 可行性检查/诊断信息
    assignments: Dict[Tuple[str, int, str], bool] = field(default_factory=dict)
    #   assignments[(worker, day, shift)] = True/False，day 从 0 开始
    worker_counts: Dict[str, int] = field(default_factory=dict)  # 每人当月班数
    single_rest: int = 0             # 总共出现的单休天数（连休<2 的天数）
    rest_run_violations: int = 0     # 连休天数超出 [min,max] 的违规次数
    reached: Dict[str, bool] = field(default_factory=dict)  # 每人是否达到最少班数
    target: Optional[int] = None     # 最少出勤班数目标
    per_day: Dict[int, Dict[str, List[str]]] = field(default_factory=dict)
    #   per_day[day][shift] = [当天该班上班的人名列表]

    def print_summary(self) -> None:
        print("=" * 60)
        print(f"求解状态: {self.status}")
        print(f"结果说明: {self.message}")
        for line in self.diagnostics:
            print("  *", line)
        if not self.feasible:
            return
        print("-" * 60)
        for day in sorted(self.per_day):
            parts = []
            for shift in self.per_day[day]:
                names = "、".join(self.per_day[day][shift]) or "—"
                parts.append(f"{shift}: {names}")
            print(f"第 {day + 1:>2} 天  " + "  |  ".join(parts))
        print("-" * 60)
        print("每人当月班数:")
        if self.target is not None:
            print(f"  最少出勤班数目标: {self.target}，达标人数: "
                  f"{sum(1 for v in self.reached.values() if v)}/{len(self.reached)}")
        for name, cnt in self.worker_counts.items():
            mark = ""
            if name in self.reached:
                mark = "  ✓达标" if self.reached[name] else "  ✗未达标"
            print(f"  {name}: {cnt} 班{mark}")
        print(f"\n单休(连休不足2天)次数: {self.single_rest}"
              f"，连休超上限次数: {self.rest_run_violations}")


# ===========================================================================
# 二、默认权重（软性目标评分）
# ===========================================================================
DEFAULT_WEIGHTS = {
    "single_rest": 100,    # 每个单休日的惩罚权重（soft 模式用）
    "shift_target": 2,     # 每人偏离目标班数的惩罚权重
    "reach_target": 1000,  # 每人达到最少班数的奖励权重（最大化达标人数）
    "shift_mismatch": 1,   # 每班次偏离默认班次的惩罚权重
}


# ===========================================================================
# 三、容量预估（纯数学，不依赖 Django）
# ===========================================================================
def _max_work_in_month(days: int, wlen: int, wmax: int) -> int:
    """在“任意 wlen 天内最多 wmax 班”的窗口约束下，days 天内每人最多能上几班。

    分段覆盖：每段长度为 wlen、最多 wmax 班，整月上限约 days * wmax / wlen；
    对尾部不整除的部分最多还能再塞 min(wmax, 余数) 班。
    """
    if wlen <= 0:
        return days
    full = days // wlen
    rem = days % wlen
    return min(days, full * wmax + min(wmax, rem))


def _min_work_days(days: int, rmax: int) -> int:
    """连休最多 rmax 天（且至少 2 天）时，一个周期里每人最少要上几天班。

    上班日作为休息段的「分隔」：w 个上班日最多隔出 w+1 段休息；
    休息总天数 rest 需能被分成若干段、每段 ∈ [2, rmax]。
    """
    for w in range(days + 1):
        rest = days - w
        min_parts = (rest + rmax - 1) // rmax   # 尽量用 rmax 大段
        max_parts = w + 1                        # w 个上班日最多 w+1 段休息
        if min_parts > max_parts:
            continue
        # rest 能否分成 k 段（k ∈ [min_parts, max_parts]），每段 ∈ [2, rmax]
        if any(2 * k <= rest <= rmax * k for k in range(min_parts, max_parts + 1)):
            return w
    return days



def capacity_quick(people: int, daily: int, days: int, rest_max: int = 4,
                   target: int = 18) -> dict:
    """快速容量参数（纯计算、不求解）：total / min_work / max_work。

    已去掉「10 天最多 6 班」工作窗口，每人最多能上 days 天（无密度上限），
    休息天数由「每天应上人数」自然决定：人多则少休、人少则多休。
    """
    return {
        "total": daily * days,
        "min_work": _min_work_days(days, rest_max),
        "max_work": days,
    }


@lru_cache(maxsize=512)
def capacity_analysis(people: int, daily: int, days: int,
                      rest_max: int = 4, target: int = 18, exempt_count: int = 0) -> dict:
    """容量预估：按人数、每天应上人数、周期天数等计算最多能满班几人、需要豁免几人。

    参数:
        people       总人数（含豁免）
        daily        每天应上人数（该班应上人数）
        days         周期实际天数
        rest_max     连休最大天数（默认 4，仅约束非豁免人员）
        target       每人应上最少班数（默认 18）
        exempt_count 已豁免人数（豁免人员不参与休息计算，可上 0 班）

    返回:
        total        周期总班次 = daily * days
        min_work     连休规则限定的每人最少班数（仅非豁免人员）
        max_work     每人最多班数（10 天 ≤6 班窗口）
        max_fillable 最多能有多少人排满 target
        needed_exempt 还需要豁免多少人（否则会有人排不满）

    这里用「纯算术」快速估算（不再跑 CP-SAT，页面秒开）：
        最多能满 = floor(总班次 / 目标班数)
        需豁免   = 总人数 - 最多能满
    例：每天 13 人 × 31 天 = 403 班，目标 18 班 → 最多能满 22 人，25 人需豁免 3 人。
    """
    base = capacity_quick(people, daily, days, rest_max, target)
    # 快速公式（纯计算，秒开）
    if target > base["max_work"]:
        # 目标班数超过工作窗口上限，没人能满
        max_fillable = 0
    else:
        max_fillable = (base["total"] // target) if target > 0 else people
        max_fillable = max(0, min(people, max_fillable))
    return {
        "total": base["total"], "min_work": base["min_work"], "max_work": base["max_work"],
        "max_fillable": max_fillable, "needed_exempt": max(0, people - max_fillable),
        "people": people, "daily": daily, "target": target,
    }


# ===========================================================================
# 四、配置校验 + 可行性预检
# ===========================================================================
def validate_config(config: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """校验配置并做可行性预检，返回 (是否基本合理, 诊断信息列表)。

    预检是“必要条件”检查，能提前发现明显无解的情况（比如每天要上的人数超过了
    所有人的容量上限），避免直接抛出“无解”却不知道原因。
    """
    diag: List[str] = []
    workers = config.get("workers") or []
    days = config.get("days") or 0
    shifts = config.get("shifts") or []
    demand = config.get("shift_demand") or {}
    role_req = config.get("role_req") or {}
    window = config.get("work_window") or {}
    rest_block = config.get("rest_block") or {}

    # 1) 基本结构
    if not workers:
        diag.append("缺少人员列表 workers")
    if not shifts:
        diag.append("缺少班次列表 shifts")
    if days <= 0:
        diag.append("days 必须为正整数")
    daily_total = config.get("daily_total")
    if daily_total:
        if int(daily_total) <= 0:
            diag.append("daily_total（每天下井人数）必须为正整数")
    else:
        if not demand:
            diag.append("缺少每班人数需求 shift_demand（或设置每天下井总人数 daily_total）")
        else:
            missing = [s for s in shifts if s not in demand]
            if missing:
                diag.append(f"班次 {missing} 在 shift_demand 中没有指定每天人数")

    # 2) 总容量预检
    if daily_total:
        total_daily = int(daily_total)
    else:
        total_daily = sum(int(demand.get(s) or 0) for s in shifts)
    if window:
        wlen = int(window.get("length", 10))
        wmax = int(window.get("max_work", 6))
        max_work_in_month = _max_work_in_month(days, wlen, wmax)
    else:
        max_work_in_month = days

    if total_daily > 0 and max_work_in_month > 0:
        capacity = len(workers) * max_work_in_month
        need = total_daily * days
        if need > capacity:
            diag.append(
                f"总班次需求 {need} 超出全员容量 {capacity} "
                f"(每天 {total_daily} 人 × {days} 天，每人最多 {max_work_in_month} 班)。"
                f"请降低每天人数、增加人员，或放宽工作窗口。"
            )

    # 3) 岗位人数条件容量
    for role, spec in role_req.items():
        if isinstance(spec, (int, float)):
            spec = {"op": ">=", "count": int(spec)}
        op = str(spec.get("op", ">="))
        cnt = int(spec.get("count", 0))
        holders = [w for w in workers if role in w.get("roles", [])]
        cap = len(holders) * max_work_in_month
        need = cnt * days
        if op in (">=", "==") and need > cap:
            diag.append(
                f"岗位「{role}」每天 {op} {cnt} 人，{days} 天共需 {need} 人天，"
                f"但该岗位只有 {len(holders)} 人，最多 {cap} 人天。不足。"
            )
        if not holders:
            diag.append(f"岗位「{role}」没有匹配的人员，无法满足条件。")

    # 4) 休息规则检查
    if rest_block:
        rmin = int(rest_block.get("min", 2))
        rmax = int(rest_block.get("max", 4))
        if rmin < 1 or rmax < rmin:
            diag.append(f"休息规则 rest_block 不合法: min={rmin}, max={rmax}")

    fatal = any("不足" in d or "超出" in d or "没有匹配" in d for d in diag)
    structurally_bad = not workers or not shifts or days <= 0 or (not demand and not daily_total)
    ok = not fatal and not structurally_bad
    return ok, diag


# ===========================================================================
# 五、求解主函数
# ===========================================================================
def build_schedule(
    config: Dict[str, Any],
    weights: Optional[Dict[str, float]] = None,
    time_limit_seconds: Optional[float] = 20.0,
    phase2_seconds: Optional[float] = 10.0,
) -> ScheduleResult:
    """构建并求解排班模型。

    config 支持字段:
        workers:          [{"name": str, "roles": [str, ...]}, ...]
        shifts:           [str, ...] 班次名称，如 ["早班","中班","晚班"]
        days:             int 周期天数
        daily_total:      int 每天下井总人数（替代逐班人数，常用）
        shift_demand:     {shift: int} 每个班每天需要的人数（精确匹配）
        role_req:         {role: {"op": ">="|"<="|"==", "count": int}}
                          某些岗位每天的人数条件（至少/至多/等于）；
                          兼容旧字段 role_min: {role: int}（等价于 >=）
        min_shift_target: int 每人最少出勤班数（全员一致的目标值，如 18）。
                          非豁免人员尽量接近，并最大化“达标人数”。
        exempt_workers:   [name,...] 豁免人员（不需要去接近最少班数目标）
        worker_shift_req: {name: {"target":int}|{"min":int,"max":int}} 逐人覆盖
        worker_default_shift: {name: shift} 默认班次偏好（软）
        work_window:      {"length": int, "max_work": int} 任意 length 天内最多上
                          max_work 班
        rest_block:       {"min": int, "max": int} 连休天数硬约束（默认 2~4），
                          即休息必须多天连休且不超过上限（含月初/月末边界）。
        no_single_rest:   bool 或 {"hard": bool, "weight": float} 软性避免单休
                          （仅当未设置 rest_block 时作为软目标使用）

    weights 可选字段: single_rest / shift_target / reach_target / shift_mismatch。
    """
    weights = weights or DEFAULT_WEIGHTS
    result = ScheduleResult()

    # ================================================================
    # 求解整体结构（按以下顺序组织，方便阅读）：
    #   ① 模型对象：x[人,天,班次]=是否上该班；y[人,天]=当天是否上班；r=1-y 是否休息
    #   ② 约束：
    #      2.1 上班人数约束：每天下井总人数 == daily_total
    #      2.2 岗位约束：每天每岗位人数满足 >= / <= / ==
    #          （岗位优先排入，剩余名额由「当天不上班的人」补满）
    #      2.3 要求班数约束：目标 = 全局「至少应上班数」优先，否则每人「应上班数」；
    #          非豁免尽量达标，豁免人员不参与达标、只补剩余班
    #      2.4 休息日约束：连休 [min,max] 天 + 工作窗口（任意10天最多 max_work 班）
    #   ③ 求解：两阶段——先最大化达标人数，再最小化偏离 + 默认班次偏好
    # ================================================================

    # ---------- 解析配置 ----------
    workers: List[dict] = config.get("workers") or []
    shifts: List[str] = config.get("shifts") or []
    days: int = int(config.get("days") or 0)
    demand: Dict[str, int] = config.get("shift_demand") or {}
    role_min: Dict[str, int] = config.get("role_min") or {}
    role_req: Dict[str, Any] = config.get("role_req") or {}
    worker_req: Dict[str, dict] = config.get("worker_shift_req") or {}
    min_shift_target = config.get("min_shift_target")
    exempt: set = set(config.get("exempt_workers") or [])
    window = config.get("work_window") or {}
    rest_block = config.get("rest_block") or {}
    nsr = config.get("no_single_rest", True)
    daily_total = config.get("daily_total")   # 每天下井总人数（替代逐班人数）
    default_shift = config.get("worker_default_shift") or {}  # {name: 默认班次}

    # 合并 role_min 与 role_req（role_req 优先）
    for role, mn in role_min.items():
        role_req.setdefault(role, {"op": ">=", "count": int(mn)})
    for role, spec in list(role_req.items()):
        if isinstance(spec, (int, float)):
            role_req[role] = {"op": ">=", "count": int(spec)}

    ok, diag = validate_config(config)
    result.diagnostics = diag
    if not ok:
        result.feasible = False
        result.status = "INFEASIBLE"
        result.message = "配置校验未通过（可能无解），请先看诊断信息。"
        return result

    names = [w["name"] for w in workers]
    name_idx = {n: i for i, n in enumerate(names)}
    role_of = {w["name"]: set(w.get("roles", [])) for w in workers}
    if daily_total:
        total_need = int(daily_total) * days
    else:
        total_need = sum(int(demand.get(s) or 0) for s in shifts) * days
    fair_target = total_need // len(names) if names else 0
    target = int(min_shift_target) if min_shift_target else fair_target

    # ---------- 模型构建（可重复调用：阶段一/阶段二各建一次） ----------
    def build_model(with_deviation: bool = True):
        """返回 (model, track)。

        with_deviation=False 用于阶段一（只最大化达标人数，不建偏离变量，
        模型更小、搜索更快）；True 用于阶段二（最小化偏离）。
        """
        m = cp_model.CpModel()
        x: Dict[Tuple[int, int, str], Any] = {}
        for w in range(len(names)):
            for d in range(days):
                for s in shifts:
                    x[w, d, s] = m.new_bool_var(f"x_{names[w]}_{d}_{s}")
        y: Dict[Tuple[int, int], Any] = {}
        r: Dict[Tuple[int, int], Any] = {}
        for w in range(len(names)):
            for d in range(days):
                y[w, d] = m.new_bool_var(f"y_{names[w]}_{d}")
                r[w, d] = m.new_bool_var(f"r_{names[w]}_{d}")
                m.add(r[w, d] == y[w, d].Not())

        # 约束 1：每天下井人数 / 每班每天需要的人数
        if daily_total:
            for d in range(days):
                m.add(sum(x[w, d, s] for w in range(len(names)) for s in shifts)
                      == int(daily_total))
        else:
            for d in range(days):
                for s in shifts:
                    m.add(sum(x[w, d, s] for w in range(len(names)))
                          == int(demand.get(s, 0)))

        # 约束 2：一人一天最多一个班；y 与 x 的关系
        for w in range(len(names)):
            for d in range(days):
                m.add(sum(x[w, d, s] for s in shifts) <= 1)
                m.add(y[w, d] == sum(x[w, d, s] for s in shifts))

        # 约束 3：岗位人数条件（至少/至多/等于）
        for role, spec in role_req.items():
            op = str(spec.get("op", ">="))
            cnt = int(spec.get("count", 0))
            holders = [name_idx[n] for n in names if role in role_of[n]]
            for d in range(days):
                s = sum(y[w, d] for w in holders)
                if op == "<=":
                    m.add(s <= cnt)
                elif op == "==":
                    m.add(s == cnt)
                else:
                    m.add(s >= cnt)

        # 约束 4：连续工作窗口（任意 length 天内最多 max_work 班）
        # 豁免人员不参与休息/窗口计算，可自由上任意班（含 0 班）
        if window:
            wlen = int(window.get("length", 10))
            wmax = int(window.get("max_work", 6))
            for w in range(len(names)):
                if names[w] in exempt:
                    continue
                for start in range(days - wlen + 1):
                    m.add(sum(y[w, d] for d in range(start, start + wlen)) <= wmax)

        # 约束 5：休息规则（连休天数 ∈ [min, max]，硬约束）
        # 豁免人员不参与连休规则，可自由休息（连休 0 天、1 天或任意天）
        if rest_block:
            rmin = int(rest_block.get("min", 2))
            rmax = int(rest_block.get("max", 4))
            for w in range(len(names)):
                if names[w] in exempt:
                    continue
                for length in range(1, rmin):
                    # 中间：上班 + length 个休息 + 上班（禁止）
                    for d in range(1, days - length):
                        m.add(
                            y[w, d - 1]
                            + sum(r[w, dd] for dd in range(d, d + length))
                            + y[w, d + length]
                            <= length + 1
                        )
                    # 月初：length 个休息 + 上班（禁止）
                    if length <= days - 1:
                        m.add(
                            sum(r[w, dd] for dd in range(0, length)) + y[w, length] <= length
                        )
                    # 月末：上班 + length 个休息（禁止）
                    if length <= days - 1:
                        m.add(
                            y[w, days - length - 1]
                            + sum(r[w, dd] for dd in range(days - length, days))
                            <= length
                        )
                # 不允许连休超过 rmax 天
                for start in range(days - rmax):
                    m.add(sum(r[w, d] for d in range(start, start + rmax + 1)) <= rmax)

        # 约束 5b：连续上班至少 2 天（禁止「休-上-休」，含月初/月末边界）
        # 豁免人员不受此约束
        for w in range(len(names)):
            if names[w] in exempt:
                continue
            for d in range(1, days - 1):
                m.add(r[w, d - 1] + y[w, d] + r[w, d + 1] <= 2)
            if days >= 2:
                # 月初：禁止「第1天上班、第2天就休息」（连续上班只有1天）
                m.add(y[w, 0] + r[w, 1] <= 1)
                # 月末：禁止「最后1天才上班」（连续上班只有1天）
                m.add(r[w, days - 2] + y[w, days - 1] <= 1)

        # 每人班次要求
        count_v: Dict[str, Any] = {}
        over_dev: Dict[str, Any] = {}
        under_dev: Dict[str, Any] = {}
        reached_v: Dict[str, Any] = {}
        exempt_count_vars: List[Any] = []
        for w in range(len(names)):
            nm = names[w]
            c = m.new_int_var(0, days, f"cnt_{nm}")
            m.add(c == sum(y[w, d] for d in range(days)))
            count_v[nm] = c

            req = worker_req.get(nm, {})
            # 该人的目标班数：逐人优先，否则用全局目标
            tgt = int(req.get("target", target)) if req else target

            if nm in exempt:
                # 豁免人员：不施加达标/接近目标压力、不参与休息计算；
                # 只收集其班数用于“尽量少且均衡”的软目标
                exempt_count_vars.append(c)
                if "min" in req or "max" in req:
                    m.add(c >= int(req.get("min", 0)))
                    m.add(c <= int(req.get("max", days)))
                continue

            # 显式给了 min/max 时作为硬性上下限（不给时目标只是软性引导）
            if "min" in req or "max" in req:
                m.add(c >= int(req.get("min", 0)))
                m.add(c <= int(req.get("max", days)))

            # 达标标记：班数 >= 该人目标 则 reached=1（参与“最大化达标人数”）
            if tgt >= 1:
                reached = m.new_bool_var(f"reached_{nm}")
                m.add(c >= tgt).only_enforce_if(reached)
                m.add(c <= tgt - 1).only_enforce_if(reached.Not())
                reached_v[nm] = reached
                # 尽量接近目标（仅阶段二需要偏离变量）
                if with_deviation:
                    ov = m.new_int_var(0, days, f"over_{nm}")
                    un = m.new_int_var(0, days, f"under_{nm}")
                    m.add(c == tgt + ov - un)
                    over_dev[nm] = ov
                    under_dev[nm] = un

        # 软性不单休（仅当未启用 rest_block 硬约束）
        single_rest_vars: List[Any] = []
        if nsr and not rest_block:
            for w in range(len(names)):
                for d in range(1, days - 1):
                    s = m.new_bool_var(f"sr_{names[w]}_{d}")
                    m.add(s <= y[w, d - 1])
                    m.add(s <= r[w, d])
                    m.add(s <= y[w, d + 1])
                    m.add(s >= y[w, d - 1] + r[w, d] + y[w, d + 1] - 2)
                    single_rest_vars.append(s)

        # 默认班次偏好（软）：尽量给每人排默认班次，偏离的班次计入惩罚
        shift_mismatch_vars: List[Any] = []
        if default_shift and with_deviation:
            for w in range(len(names)):
                pref = default_shift.get(names[w])
                if not pref:
                    continue
                for d in range(days):
                    for s in shifts:
                        if s != pref:
                            shift_mismatch_vars.append(x[w, d, s])

        # 解提示：按“上6休4”错峰模式给每个工人一个初始作息，帮助搜索更快找到好解
        # （仅当启用了连休硬约束时使用；豁免人员不参与，给 0 提示即尽量少排）
        if rest_block and not with_deviation:
            for w in range(len(names)):
                if names[w] in exempt:
                    for d in range(days):
                        m.add_hint(y[w, d], 0)
                    continue
                phase = (w * 4) % 10
                for d in range(days):
                    m.add_hint(y[w, d], 1 if (d + phase) % 10 < 6 else 0)

        track = {
            "x": x, "y": y, "r": r,
            "count_v": count_v, "reached_v": reached_v,
            "over_dev": over_dev, "under_dev": under_dev,
            "single_rest_vars": single_rest_vars,
            "shift_mismatch_vars": shift_mismatch_vars,
            "exempt_count_vars": exempt_count_vars,
        }
        return m, track

    # ---------- 求解 ----------
    def _solve(m, limit: float):
        solver = cp_model.CpSolver()
        if limit:
            solver.parameters.max_time_in_seconds = float(limit)
        solver.parameters.num_search_workers = 8
        st = solver.solve(m)
        return solver, st

    def _extract(solver, track, status) -> None:
        """把求解结果写入 result。"""
        x, y = track["x"], track["y"]
        count_v, reached_v = track["count_v"], track["reached_v"]
        result.feasible = True
        result.status = "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE"
        for nm in names:
            result.worker_counts[nm] = solver.value(count_v[nm])
            if nm in reached_v:
                result.reached[nm] = bool(solver.value(reached_v[nm]))
        for d in range(days):
            result.per_day[d] = {}
            for s in shifts:
                result.per_day[d][s] = [
                    names[w] for w in range(len(names)) if solver.value(x[w, d, s])
                ]
        # 重算单休与连休超限（豁免人员不参与休息计算，跳过统计）
        _sr, _rv = 0, 0
        rb = rest_block
        for w in range(len(names)):
            if names[w] in exempt:
                continue
            for d in range(1, days - 1):
                if (solver.value(y[w, d - 1]) and not solver.value(y[w, d])
                        and solver.value(y[w, d + 1])):
                    _sr += 1
            run = 0
            for d in range(days):
                if solver.value(y[w, d]) == 0:
                    run += 1
                else:
                    if run and run < (rb.get("min", 2) if rb else 1):
                        _sr += 1
                    if rb and run > int(rb.get("max", 4)):
                        _rv += 1
                    run = 0
            if run:
                if run < (rb.get("min", 2) if rb else 1):
                    _sr += 1
                if rb and run > int(rb.get("max", 4)):
                    _rv += 1
        result.single_rest = _sr
        result.rest_run_violations = _rv
        for w in range(len(names)):
            for d in range(days):
                for s in shifts:
                    result.assignments[(names[w], d, s)] = bool(solver.value(x[w, d, s]))

    # 需要达标目标 -> 两阶段；否则单次求解最小化偏离
    w_sr = weights.get("single_rest", 100)
    w_tgt = weights.get("shift_target", 2)
    w_reach = weights.get("reach_target", 1000)
    if isinstance(nsr, dict):
        w_sr = float(nsr.get("weight", w_sr))

    m1, t1 = build_model(with_deviation=False)
    if t1["reached_v"]:
        # ---- 阶段一：最大化达标人数（不带偏离变量，模型更小更快） ----
        m1.maximize(sum(t1["reached_v"].values()))
        sol1, st1 = _solve(m1, time_limit_seconds)
        if st1 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            result.feasible = False
            result.status = "INFEASIBLE" if st1 == cp_model.INFEASIBLE else "UNKNOWN"
            result.message = ("无解。请结合诊断信息调整参数：降低每天人数、放宽岗位人数条件、"
                              "增加人员或放宽工作窗口/休息规则。")
            return result
        best = int(sol1.objective_value)

        # ---- 阶段二：固定达标人数，最小化偏离（以阶段一的解作为起点 hint） ----
        m2, t2 = build_model(with_deviation=True)
        m2.add(sum(t2["reached_v"].values()) >= best)
        # 用阶段一的解给阶段二一个可行起点，避免重新搜索
        for w in range(len(names)):
            for d in range(days):
                m2.add_hint(t2["y"][w, d], int(sol1.value(t1["y"][w, d])))
                for s in shifts:
                    m2.add_hint(t2["x"][w, d, s], int(sol1.value(t1["x"][w, d, s])))
        obj = 0
        # 均衡：最小化非豁免人员的「最大班数 - 最小班数」，
        # 让「每天人数×天数」多出来的班次均衡分配给非豁免人员（而非集中在个别人/豁免）
        non_exempt_counts = [t2["count_v"][nm] for nm in names if nm not in exempt]
        if len(non_exempt_counts) >= 2:
            max_c = m2.new_int_var(0, days, "max_c")
            min_c = m2.new_int_var(0, days, "min_c")
            m2.add_max_equality(max_c, non_exempt_counts)
            m2.add_min_equality(min_c, non_exempt_counts)
            obj += (max_c - min_c) * w_tgt
        obj += sum(t2["single_rest_vars"]) * w_sr
        obj += sum(t2["shift_mismatch_vars"]) * weights.get("shift_mismatch", 1)
        # 豁免人员尽量少且均衡：最小化其最大班数（剩余班数均匀分割）
        if t2["exempt_count_vars"]:
            exempt_max = m2.new_int_var(0, days, "exempt_max")
            m2.add_max_equality(exempt_max, t2["exempt_count_vars"])
            obj += exempt_max * weights.get("exempt_balance", 5)
        m2.minimize(obj)
        sol2, st2 = _solve(m2, phase2_seconds)
        if st2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            _extract(sol2, t2, st2)
        else:
            # 阶段二失败则退回阶段一的解（同样是合法解）
            _extract(sol1, t1, st1)
            result.diagnostics = list(result.diagnostics) + [
                "阶段二优化未完成，已采用阶段一的排班结果。"
            ]
        result.target = target
        result.message = (
            f"求解成功：{sum(1 for v in result.reached.values() if v)}/"
            f"{len(result.reached)} 人达到最少 {target} 班。"
        )
    else:
        # 无达标目标：单次求解最小化偏离（兼容旧行为）
        obj = sum(t1["over_dev"][nm] + t1["under_dev"][nm] for nm in t1["over_dev"]) * w_tgt
        obj += sum(t1["single_rest_vars"]) * w_sr
        obj += sum(t1["shift_mismatch_vars"]) * weights.get("shift_mismatch", 1)
        m1.minimize(obj)
        sol1, st1 = _solve(m1, time_limit_seconds)
        if st1 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            _extract(sol1, t1, st1)
            result.message = "求解成功，已得到一份可行排班表。"
        else:
            result.feasible = False
            result.status = "INFEASIBLE" if st1 == cp_model.INFEASIBLE else "UNKNOWN"
            result.message = ("无解。请结合诊断信息调整参数：降低每天人数、放宽岗位人数条件、"
                              "增加人员或放宽工作窗口/休息规则。")

    return result


# ===========================================================================
# 六、便捷工具
# ===========================================================================
def workers_from_roles(role_map: Dict[str, List[str]]) -> List[dict]:
    """从 {岗位: [人名,...]} 构造 workers 列表。

    >>> workers_from_roles({"电工": ["张三"], "班长": ["李四"]})
    [{'name': '张三', 'roles': ['电工']}, {'name': '李四', 'roles': ['班长']}]
    """
    workers: List[dict] = []
    for role, names in role_map.items():
        for nm in names:
            entry = next((w for w in workers if w["name"] == nm), None)
            if entry is None:
                workers.append({"name": nm, "roles": [role]})
            else:
                entry["roles"].append(role)
    return workers


# ===========================================================================
# 七、可运行示例（python project/app/scheduler/scheduling.py）
# ===========================================================================
def demo() -> None:
    """用 demo 人员数据跑一份排班示例。"""
    workers = [
        {'name': '王磊磊', 'roles': ['班长']}, {'name': '祁向前', 'roles': ['班长']},
        {'name': '秦湖平', 'roles': ['班长', '皮带']}, {'name': '杨志磊', 'roles': ['电工']},
        {'name': '韩二波', 'roles': ['电工']}, {'name': '刘广宏', 'roles': ['电工']},
        {'name': '张建伟', 'roles': ['电工']}, {'name': '程朝阳', 'roles': ['风水管']},
        {'name': '刘瑶', 'roles': ['风水管']}, {'name': '王鹏', 'roles': ['盾构机']},
        {'name': '李志中', 'roles': ['盾构机']}, {'name': '牛国辉', 'roles': ['皮带']},
        {'name': '赵国辉', 'roles': ['皮带']}, {'name': '苏忠', 'roles': ['皮带']},
        {'name': '李坤', 'roles': ['普通']}, {'name': '王钰杰', 'roles': ['风水管']},
        {'name': '王逸凡', 'roles': ['风水管']}, {'name': '魏志强', 'roles': ['普通']},
        {'name': '李怀亮', 'roles': ['皮带', '盾构机']}, {'name': '赵磊', 'roles': ['普通']},
        {'name': '赵兴宇', 'roles': ['皮带', '风水管']}, {'name': '洪泽文', 'roles': ['普通']},
    ]
    config = {
        "workers": workers,
        "shifts": ["早班", "中班", "晚班"],
        "days": 30,
        "daily_total": 12,
        "role_req": {"电工": {"op": ">=", "count": 2}, "班长": {"op": ">=", "count": 1}},
        "min_shift_target": 18,
        "exempt_workers": ["洪泽文"],
        "rest_block": {"min": 2, "max": 4},
        "work_window": {"length": 10, "max_work": 6},
    }
    result = build_schedule(config)
    result.print_summary()


if __name__ == "__main__":
    demo()
