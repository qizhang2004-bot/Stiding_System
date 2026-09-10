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
2. 阶段二：固定阶段一的达标人数，用**词典序多目标**优化：
   ① 每人班数贴近目标 → ② 非豁免班数均衡 → ③ 未达标者尽量接近目标且均分 →
   ④ 软性避免单休 → ⑤ 剩余班次在「豁免人员 + 只设下限的专人专项人员」之间均分。
   用阶段一的解作为 hint 起步。

   注意：CP-SAT **不支持**「连续多次 minimize 即词典序」——实测 OR-Tools 9.15 只有最后一次
   minimize 生效，前面写的目标会被静默丢弃。因此这里由 ``_solve_lexicographic`` 逐层求解，
   每层求出最优值后把「该目标 == 最优值」加成硬约束，再优化下一层。

班次是硬性约束：上传/导入的班次即该人员唯一可排的班次。

同配置必然产生同结果：求解器使用由配置内容派生的确定性随机种子（可用
``config["random_seed"]`` 覆盖），重复生成不再“每次都不一样”。

支持的能力（对应需求）
----------------------
1. 每天下井总人数                    -> ``daily_total``
2. 岗位人数条件（至少/至多/等于）    -> ``role_req``；
   一人一天只能干一个岗位（多岗位人员当天只计入一个岗位的配额）
3. 每人每天最多上一个班              -> 内置
4. 连休 2~4 天（不允许单休）         -> ``rest_block``（硬约束）
    「专人专项约束」逐人覆盖           -> ``worker_rules``（该人优先于班组默认）
5. 每人应上最少班数、可豁免、最大化达标人数 -> ``min_shift_target`` + ``exempt_workers``
6. 班次硬性规则（上传/默认班次即唯一可排班次）-> ``worker_default_shift``（硬约束）
7. 可行性预检 + 友好诊断             -> ``validate_config``
8. 容量预估（最多能满班几人/需豁免几人）-> ``capacity_analysis``（纯函数）

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
    }
    result = build_schedule(config)
    result.print_summary()

也可以直接运行本文件看示例：python project/app/scheduler/scheduling.py
"""

from __future__ import annotations

import hashlib
import json
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
    phase1_status: str = ""          # 阶段一（最大化达标人数）的求解状态，供测试判断是否证到最优
    feasible: bool = False
    message: str = ""                # 给用户看的一句话结果
    diagnostics: List[str] = field(default_factory=list)  # 可行性检查/诊断信息
    assignments: Dict[Tuple[str, int, str], bool] = field(default_factory=dict)
    #   assignments[(worker, day, shift)] = True/False，day 从 0 开始
    role_assignments: Dict[Tuple[str, int], str] = field(default_factory=dict)
    #   role_assignments[(worker, day)] = 当天被指派的岗位（一人一天只干一个岗位）
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
# 二、随机种子
# ===========================================================================
def _derive_seed(snapshot: Dict[str, Any]) -> int:
    """从配置快照派生确定性随机种子：同配置 -> 同种子 -> 同排班结果。"""
    digest = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


# ===========================================================================
# 三、容量预估（纯数学，不依赖 Django）
# ===========================================================================
def _can_split(total: int, parts: int, lo: int, hi: int) -> bool:
    """total 天能否分成 parts 段、每段长度 ∈ [lo, hi]。"""
    return parts >= 0 and lo * parts <= total <= hi * parts


def _max_work_days(days: int, rmax: int, rmin: int = 2) -> int:
    """同一连休规则下，一个周期里每人最多能上几天班（只考虑休息约束，不设上班段上限）。"""
    rmin = max(2, int(rmin))
    rmax = max(rmin, int(rmax))
    for w in range(days, -1, -1):
        rest = days - w
        min_parts = 0 if rest <= 0 else (rest + rmax - 1) // rmax
        max_parts = w + 1
        if min_parts <= max_parts and any(
                _can_split(rest, k, rmin, rmax)
                for k in range(min_parts, max_parts + 1)):
            return w
    return 0


def _min_work_days(days: int, rmax: int, rmin: int = 2) -> int:
    """连休天数 ∈ [rmin, rmax] 时，一个周期里每人最少要上几天班。

    上班日作为休息段的「分隔」：w 个上班日最多隔出 w+1 段休息；
    休息总天数 rest 需能被分成若干段、每段 ∈ [rmin, rmax]。

    rmin 即「最短连续休息天数」（默认 2 = 禁止单休，也是系统强制下限）。
    """
    rmin = max(2, int(rmin))
    rmax = max(rmin, int(rmax))
    for w in range(days + 1):
        rest = days - w
        min_parts = 0 if rest <= 0 else (rest + rmax - 1) // rmax
        max_parts = w + 1
        if min_parts <= max_parts and any(
                _can_split(rest, k, rmin, rmax)
                for k in range(min_parts, max_parts + 1)):
            return w
    return days


def reachable_work_days_bounded(days: int, rmin: int, rmax: int,
                                wmin: int, wmax: int) -> List[int]:
    """列出排班规则下所有可达的上班天数（升序）。

    直接对「上班/休息」0-1 序列做逐日 DP，状态为
    ``(已排天数, 当前是上班还是休息, 当前的连续长度)``，
    只在「当天与前一天不同」或「序列结束」时校验上一段的长度（必须落在对应区间内），
    因此与模型里施加的边界规则完全一致（含月初/月末）。

    这比只看休息约束严格得多：上班段被 wmax 截断后，可达天数会变得稀疏。
    例：30 天、连休 3~4 天、连续上班 2~7 天 → 可达 10~21 天，22~30 全部排不出
    （22 天要切成 4 段上班，4 段之间至少夹 3 段休息共需 ≥9 天休息，
      而 30−22=8 天休息不够）。
    """
    rmin = max(1, int(rmin))
    rmax = max(rmin, int(rmax))
    wmin = max(1, int(wmin))
    wmax = max(wmin, int(wmax) if int(wmax) > 0 else days)
    # state: (day, 值, 当前段已排长度) -> 累计上班天数集合
    cur: Dict[Tuple[int, int], set] = {}
    for v in (0, 1):
        cur[(v, 1)] = {v}
    for day in range(1, days):
        nxt: Dict[Tuple[int, int], set] = {}
        for (prev, ln), totals in cur.items():
            for v in (0, 1):
                if v == prev:
                    if ln + 1 <= (rmax if v == 0 else wmax):
                        s = nxt.setdefault((v, ln + 1), set())
                        s.update(t + v for t in totals)
                else:
                    lo = rmin if prev == 0 else wmin
                    if ln >= lo:
                        s = nxt.setdefault((v, 1), set())
                        s.update(t + v for t in totals)
        cur = nxt
    out: set = set()
    for (v, ln), totals in cur.items():
        lo, hi = (rmin, rmax) if v == 0 else (wmin, wmax)
        if lo <= ln <= hi:
            out.update(totals)
    return sorted(out)


def can_work_days_bounded(days: int, want: int, rmin: int, rmax: int,
                          wmin: int, wmax: int) -> bool:
    """完整规则下「恰好上 want 天」是否可行（详见 reachable_work_days_bounded）。"""
    if want < 0 or want > days:
        return False
    return int(want) in set(reachable_work_days_bounded(days, rmin, rmax, wmin, wmax))


def _min_rest_days(days: int, rmax: int, rmin: int = 2) -> int:
    """同一连休规则下，一个周期里每人最少要休几天（= 最长连休上限决定的休息量）。"""
    return days - _max_work_days(days, rmax, rmin)



def capacity_quick(people: int, daily: int, days: int, rest_max: int = 4,
                   target: int = 18, rest_min: int = 2) -> dict:
    """快速容量参数（纯计算、不求解）：total / min_work / max_work。

    没有工作窗口限制，每人最多能上 days 天（无密度上限），
    休息天数由「每天应上人数」自然决定：人多则少休、人少则多休。
    """
    return {
        "total": daily * days,
        "min_work": _min_work_days(days, rest_max, rest_min),
        "max_work": days,
    }


@lru_cache(maxsize=512)
def capacity_analysis(people: int, daily: int, days: int,
                      rest_max: int = 4, target: int = 18, exempt_count: int = 0,
                      rest_min: int = 2) -> dict:
    """容量预估：按人数、每天应上人数、周期天数等计算最多能满班几人、需要豁免几人。

    参数:
        people       总人数（含豁免）
        daily        每天应上人数（该班应上人数）
        days         周期实际天数
        rest_max     连休最大天数（默认 4，仅约束非豁免人员）
        target       每人应上最少班数（默认 18）
        exempt_count 已豁免人数（豁免人员不参与休息计算，可上 0 班）
        rest_min     连休最小天数（默认 2，即禁止单休）

    返回:
        total        周期总班次 = daily * days
        min_work     连休规则限定的每人最少班数（仅非豁免人员）
        max_work     每人最多班数（= 周期天数，无额外窗口限制）
        max_fillable 最多能有多少人排满 target
        needed_exempt 还需要豁免多少人（否则会有人排不满）

    这里用「纯算术」快速估算（不再跑 CP-SAT，页面秒开）：
        最多能满 = floor(总班次 / 目标班数)
        需豁免   = 总人数 - 最多能满
    例：每天 13 人 × 31 天 = 403 班，目标 18 班 → 最多能满 22 人，25 人需豁免 3 人。
    """
    base = capacity_quick(people, daily, days, rest_max, target, rest_min)
    # 快速公式（纯计算，秒开）
    if target > base["max_work"]:
        # 目标班数超过周期天数，没人能满
        max_fillable = 0
    else:
        max_fillable = (base["total"] // target) if target > 0 else people
        max_fillable = max(0, min(people, max_fillable))
    return {
        "total": base["total"], "min_work": base["min_work"], "max_work": base["max_work"],
        "max_fillable": max_fillable, "needed_exempt": max(0, people - max_fillable),
        "people": people, "daily": daily, "target": target,
    }


def _work_run_bounds(days: int, rest_min: int) -> Dict[str, int]:
    """连续上班天数范围：min=2（禁止只上一天就休），max=(周期天数//3)−最短休息。"""
    rmin = max(2, int(rest_min or 2))
    wmin = 2
    wmax = max(wmin, days // 3 - rmin) if days else wmin
    return {"min": min(wmin, days) if days else wmin,
            "max": min(wmax, days) if days else wmax}


def effective_work_block(days: int, rest_min: int, rest_max: int) -> Dict[str, int]:
    """由连休范围推导「连续上班天数」范围（专人专项覆盖后逐人重算）。

    规则同班组默认口径（上限只由「最短连续休息」决定，与 rest_max 无关）：
        最短连续上班 = 2（禁止只上一天班就休息）
        最长连续上班 = (周期天数 // 3) − 最短连续休息天数
    """
    return _work_run_bounds(days, rest_min)


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
    role_req = config.get("role_req") or {}
    rest_block = config.get("rest_block") or {}
    worker_req_all = config.get("worker_shift_req") or {}
    worker_rules_all = config.get("worker_rules") or {}
    exempt_names = set(config.get("exempt_workers") or [])
    work_block = config.get("work_block") or {}

    # 1) 基本结构
    if not workers:
        diag.append("缺少人员列表 workers")
    if not shifts:
        diag.append("缺少班次列表 shifts")
    if days <= 0:
        diag.append("days 必须为正整数")
    daily_total = config.get("daily_total")
    if not daily_total:
        diag.append("缺少每天下井人数 daily_total")
    elif int(daily_total) <= 0:
        diag.append("daily_total（每天下井人数）必须为正整数")

    # 2) 总容量预检
    total_daily = int(daily_total or 0)
    max_work_in_month = days

    if total_daily > 0 and max_work_in_month > 0:
        capacity = len(workers) * max_work_in_month
        need = total_daily * days
        if need > capacity:
            diag.append(
                f"总班次需求 {need} 超出全员容量 {capacity} "
                f"(每天 {total_daily} 人 × {days} 天，每人最多 {max_work_in_month} 班)。"
                f"请降低每天人数或增加人员。"
            )

    # 3) 岗位人数条件容量
    for role, spec in role_req.items():
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

    # 3b) 逐人硬性下限与豁免的联动预检（有硬性 min 时才能精确判断）
    # 逐人「硬性最少班数」解析（专人专项 work_days 覆盖 worker_shift_req.min）：
    # 两处都可能在，取专人专项优先，避免重复计数。
    def _effective_min(name: str) -> int:
        if name in exempt_names:
            return 0
        wd = (worker_rules_all.get(name) or {}).get("work_days")
        if wd is not None:
            return max(0, int(wd))
        req_min = (worker_req_all.get(name) or {}).get("min")
        return max(0, int(req_min)) if req_min is not None else 0

    total_need = total_daily * days
    non_exempt_mins = sum(_effective_min(w.get("name")) for w in workers)
    if non_exempt_mins > total_need:
        diag.append(
            f"非豁免人员的硬性最低班数合计 {non_exempt_mins} 班"
            f"（含专人专项要求），超过周期总班次 {total_need} 班"
            f"（豁免人数不够或目标太高），整体无解。"
        )
    else:
        leftover = total_need - non_exempt_mins  # 豁免人员可补的剩余班次
        for role, spec in role_req.items():
            op = str(spec.get("op", ">="))
            if op not in (">=", "=="):
                continue
            cnt = int(spec.get("count", 0))
            need = cnt * days
            guaranteed = sum(
                _effective_min(w.get("name"))
                for w in workers
                if role in w.get("roles", [])
            )
            if guaranteed < need and (need - guaranteed) > leftover:
                diag.append(
                    f"岗位「{role}」每天 {op} {cnt} 人，{days} 天共需 {need} 人天；"
                    f"非豁免该岗位人员硬性只保证 {guaranteed} 人天，缺口 {need - guaranteed} "
                    f"人天大于豁免人员全部可补的 {leftover} 人天。"
                    f"请豁免其它岗位的人员，或降低该岗位人数条件。"
                )

    # 4) 休息规则检查（班组默认）
    if rest_block:
        rmin = int(rest_block.get("min", 2))
        rmax = int(rest_block.get("max", 4))
        if rmin < 1 or rmax < rmin:
            diag.append(f"休息规则 rest_block 不合法: min={rmin}, max={rmax}")

    # 5) 专人专项约束检查（逐人覆盖班组默认的休息日 / 最少上班天数）
    for name, rule in worker_rules_all.items():
        rb = rule.get("rest_block") or {}
        wd = rule.get("work_days")
        if not rb:
            continue
        rmin = int(rb.get("min", 2))
        rmax = int(rb.get("max", 4))
        if rmin < 1 or rmax < rmin:
            diag.append(f"「{name}」专人专项休息规则不合法: 最少 {rmin} 天、最多 {rmax} 天。")
            continue
        if rmin >= days:
            diag.append(
                f"「{name}」专人专项连休下限 {rmin} 天 ≥ 周期 {days} 天，本周期无法排班。"
            )
            continue
        if wd is not None and int(wd) > 0:
            # 该人的连续上班范围：自带休息要求时按自己的休息范围重算
            wb = _work_run_bounds(days, rmin)
            if not can_work_days_bounded(days, int(wd), rmin, rmax,
                                         wb["min"], wb["max"]):
                reach = reachable_work_days_bounded(days, rmin, rmax,
                                                    wb["min"], wb["max"])
                near = [w for w in reach if w >= int(wd)][:3]
                hint = ("；不小于该数的可达天数为 "
                        + "、".join(str(w) for w in near)) if near else ""
                diag.append(
                    f"「{name}」要求至少上 {int(wd)} 班，但在连休 {rmin}~{rmax} 天 + "
                    f"连续上班 {wb['min']}~{wb['max']} 天的规则下，"
                    f"本周期实际排不出这个天数（最多能上 {max(reach) if reach else 0} 班）"
                    f"{hint}。条件冲突。"
                )

    fatal = any("不足" in d or "超出" in d or "没有匹配" in d or "大于豁免人员" in d
                or "整体无解" in d or "条件冲突" in d or "无法排班" in d for d in diag)
    structurally_bad = not workers or not shifts or days <= 0 or not daily_total
    ok = not fatal and not structurally_bad
    return ok, diag


# ===========================================================================
# 五、求解主函数
# ===========================================================================
def build_schedule(
    config: Dict[str, Any],
    time_limit_seconds: Optional[float] = 20.0,
    phase2_seconds: Optional[float] = 10.0,
) -> ScheduleResult:
    """构建并求解排班模型。

    config 支持字段:
        workers:          [{"name": str, "roles": [str, ...]}, ...]
        shifts:           [str, ...] 班次名称，如 ["早班","中班","晚班"]
        days:             int 周期天数
        daily_total:      int 每天下井总人数
        role_req:         {role: {"op": ">="|"<="|"==", "count": int}}
                          某些岗位每天的人数条件（至少/至多/等于）
        min_shift_target: int 每人最少出勤班数（全员一致的目标值，如 18）。
                          非豁免人员尽量接近，并最大化“达标人数”。
        exempt_workers:   [name,...] 豁免人员（不需要去接近最少班数目标）
        worker_shift_req: {name: {"target":int}|{"min":int,"max":int}} 逐人覆盖
        worker_default_shift: {name: shift} 硬性班次：该人员只能排这个班次（上传什么班次就排什么班次）
        rest_block:       {"min": int, "max": int} 连休天数硬约束（默认 2~4），
                          即休息必须多天连休且不超过上限（含月初/月末边界）。
        work_block:       {"min": int, "max": int} 连续上班天数硬约束（可选），
                          默认 min=2（连续上班至少 2 天，无上限）；
                          规则建议 min=(周期天数//3)−最长休息、max=(周期天数//3)−最短休息。
        worker_rules:     {name: {"rest_block": {"min":int,"max":int},
                                  "work_days": int}}
                          「专人专项约束」：逐人覆盖班组默认的休息日要求与最少上班天数，
                          该人的要求**完全取代**班组默认值（不是取交集）：
                            rest_block 省略 = 沿用班组默认连休范围；
                            work_days  省略 = 该人不受硬性最少上班天数约束（仅软性贴近目标）。
                          连续上班天数上限会按该人自己的连休范围重算
                          （见 effective_work_block），保证不会自相矛盾。
                          豁免人员不参与休息规则，其 worker_rules 不生效。
        no_single_rest:   bool 仅当未设置 rest_block 时作为软性避免单休的开关
        random_seed:      int 求解随机种子（不传则按配置内容自动派生，同配置同结果）
    """
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
    #      2.4 休息日约束：连休 [min,max] 天（含月初/月末边界）
    #          豁免人员不参与休息规则，可自由排班
    #   ③ 求解：两阶段——先最大化达标人数，再最小化偏离 + 默认班次偏好
    # ================================================================

    # ---------- 解析配置 ----------
    workers: List[dict] = config.get("workers") or []
    shifts: List[str] = config.get("shifts") or []
    days: int = int(config.get("days") or 0)
    role_req: Dict[str, Any] = config.get("role_req") or {}
    worker_req: Dict[str, dict] = config.get("worker_shift_req") or {}
    min_shift_target = config.get("min_shift_target")
    exempt: set = set(config.get("exempt_workers") or [])
    rest_block = config.get("rest_block") or {}
    work_block = config.get("work_block") or {}
    nsr = config.get("no_single_rest", True)
    daily_total = config.get("daily_total")   # 每天下井总人数（替代逐班人数）
    default_shift = config.get("worker_default_shift") or {}  # {name: 默认班次}
    # 「专人专项约束」：逐人覆盖班组默认的休息日 / 最少上班天数
    worker_rules: Dict[str, dict] = config.get("worker_rules") or {}

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
    total_need = int(daily_total or 0) * days
    fair_target = total_need // len(names) if names else 0
    target = int(min_shift_target) if min_shift_target else fair_target

    # ---------- 确定性随机种子（同配置 -> 同结果） ----------
    # 说明：worker_shift_req / worker_rules 的值含混合类型（int 与嵌套 dict），
    # 直接 sorted(items()) 会因类型不可比较而报错，故先归一化成可排序的 JSON 文本。
    seed = _derive_seed({
        "workers": sorted(
            (w.get("name"), sorted(w.get("roles") or []),
             w.get("default_shift", ""), w.get("worked", 0), w.get("required", 0))
            for w in workers
        ),
        "shifts": list(shifts), "days": days,
        "daily_total": daily_total,
        "role_req": sorted(
            (r, v.get("op", ">="), int(v.get("count", 0)))
            for r, v in role_req.items()
        ),
        "min_shift_target": min_shift_target,
        "exempt_workers": sorted(exempt),
        "worker_shift_req": sorted(
            (n, json.dumps(v, ensure_ascii=False, sort_keys=True))
            for n, v in worker_req.items()
        ),
        "worker_rules": sorted(
            (n, json.dumps(v, ensure_ascii=False, sort_keys=True))
            for n, v in worker_rules.items()
        ),
        "worker_default_shift": sorted(default_shift.items()),
        "rest_block": sorted(rest_block.items()),
        "work_block": sorted(work_block.items()),
        "no_single_rest": bool(nsr),
    })
    if config.get("random_seed") is not None:
        seed = int(config["random_seed"])

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

        # 约束 1：每天下井总人数
        for d in range(days):
            m.add(sum(x[w, d, s] for w in range(len(names)) for s in shifts)
                  == int(daily_total))

        # 约束 2：一人一天最多一个班；y 与 x 的关系
        for w in range(len(names)):
            for d in range(days):
                m.add(sum(x[w, d, s] for s in shifts) <= 1)
                m.add(y[w, d] == sum(x[w, d, s] for s in shifts))

        # 约束 3：岗位人数条件（至少/至多/等于）。
        # 硬规则：一人一天最多占一个岗位名额 —— 多岗位人员当天只能计入其中一个岗位的配额；
        # 未被指派岗位的上班人员按普通岗补位，不占任何岗位名额（否则「等于」会因名额不足无解）。
        # 用 z[w,d,role] 表示「某人某天被指派到某岗位」，岗位条件统计 z 而不是上班天数 y。
        constrained_roles_of = {
            w: [r for r in role_of[names[w]] if r in role_req]
            for w in range(len(names))
        }
        z: Dict[Tuple[int, int, str], Any] = {}
        for w in range(len(names)):
            for d in range(days):
                for role in constrained_roles_of[w]:
                    z[w, d, role] = m.new_bool_var(f"z_{names[w]}_{d}_{role}")
        # 只有上班的人能占岗位名额；一人一天最多占一个岗位名额（可以不占 = 按普通岗补位）
        for w in range(len(names)):
            if not constrained_roles_of[w]:
                continue
            for d in range(days):
                m.add(sum(z[w, d, role] for role in constrained_roles_of[w]) <= y[w, d])

        for role, spec in role_req.items():
            op = str(spec.get("op", ">="))
            cnt = int(spec.get("count", 0))
            holders = [name_idx[n] for n in names if role in role_of[n]]
            for d in range(days):
                s = sum(z[w, d, role] for w in holders if (w, d, role) in z)
                if op == "<=":
                    m.add(s <= cnt)
                elif op == "==":
                    m.add(s == cnt)
                else:
                    m.add(s >= cnt)

        # 逐人「休息规则 / 连续上班规则」解析：专人专项约束优先于班组默认值。
        #   rest_block：该人自带 → 完全取代默认；否则用班组默认。
        #   work_block：该人自带休息范围 → 按其休息范围重算连续上班上限；
        #               否则用班组默认 work_block。
        person_blocks: List[Tuple[dict, dict]] = []
        for nm in names:
            rule = worker_rules.get(nm) or {}
            rb_w = dict(rule.get("rest_block") or rest_block or {})
            if rule.get("rest_block"):
                wb_w = effective_work_block(
                    days, rb_w.get("min", 2), rb_w.get("max", 4))
            else:
                wb_w = dict(work_block or {})
            person_blocks.append((rb_w, wb_w))

        # 约束 4：休息规则（连休天数 ∈ [min, max]，硬约束）
        # 豁免人员不参与连休规则，可自由休息（连休 0 天、1 天或任意天数）。
        # 「专人专项约束」：某人自带连休范围时，用他自己的范围**完全取代**班组默认范围
        # （不是取交集）。
        for w in range(len(names)):
            if names[w] in exempt:
                continue
            rb_w, _wb_w = person_blocks[w]
            if not rb_w:
                continue
            rmin = int(rb_w.get("min", 2))
            rmax = int(rb_w.get("max", 4))
            for length in range(1, rmin):
                # 中间：上班 + length 个休息 + 上班（禁止）
                for d in range(1, days - length):
                    m.add(
                        y[w, d - 1]
                        + sum(r[w, dd] for dd in range(d, d + length))
                        + y[w, d + length]
                        <= length + 1
                    )
                if length <= days - 1:
                    # 月初：length 个休息 + 上班（禁止）
                    m.add(
                        sum(r[w, dd] for dd in range(0, length)) + y[w, length] <= length
                    )
                    # 月末：上班 + length 个休息（禁止）
                    m.add(
                        y[w, days - length - 1]
                        + sum(r[w, dd] for dd in range(days - length, days))
                        <= length
                    )
            # 不允许连休超过 rmax 天（窗口 [start, start+rmax] 半开，右端不越界）
            for start in range(days - rmax):
                m.add(sum(r[w, d] for d in range(start, start + rmax + 1)) <= rmax)

        # 约束 4b：连续上班天数 ∈ [wr_min, wr_max]（硬约束，含月初/月末边界）
        # 默认口径 min = 2、max = (周期天数 // 3) − 最短休息天数；
        # 「专人专项约束」的人员按他自己的连休范围重算（避免与其休息要求自相矛盾）。
        # 豁免人员不受此约束。
        for w in range(len(names)):
            if names[w] in exempt:
                continue
            _rb_w, wb_w = person_blocks[w]
            wr_min = int(wb_w.get("min", 2))
            wr_max = int(wb_w.get("max", 0))
            # 上班段 ≤ wr_max：任意 wr_max+1 天窗口内上班天数 ≤ wr_max（禁止连续超过上限）
            if wr_max and wr_max >= wr_min:
                for start in range(days - wr_max):
                    m.add(sum(y[w, d] for d in range(start, start + wr_max + 1)) <= wr_max)
            # 上班段 ≥ wr_min：禁止「休 + len 天班 + 休」模式（len = 1..wr_min-1），含边界
            for length in range(1, wr_min):
                for d in range(1, days - length):
                    m.add(
                        r[w, d - 1]
                        + sum(y[w, dd] for dd in range(d, d + length))
                        + r[w, d + length]
                        <= length + 1
                    )
                if length <= days - 1:
                    # 月初：len 天上班 + 休（禁止）
                    m.add(sum(y[w, dd] for dd in range(0, length)) + r[w, length] <= length)
                    # 月末：休 + len 天上班（禁止）
                    m.add(
                        r[w, days - length - 1]
                        + sum(y[w, dd] for dd in range(days - length, days))
                        <= length
                    )

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
            # 专人专项的「至少上班天数」：作为硬性下限，且作为该人的软性目标
            wr_work = (worker_rules.get(nm) or {}).get("work_days")
            # 该人的目标班数：逐人 target 优先，其次专人专项 work_days，否则用全局目标
            if req and "target" in req:
                tgt = int(req["target"])
            elif wr_work is not None:
                tgt = int(wr_work)
            else:
                tgt = target

            if nm in exempt:
                # 豁免人员：不施加达标/接近目标压力、不参与休息计算；
                # 只收集其班数用于“尽量少且均衡”的软目标
                exempt_count_vars.append(c)
                if "min" in req or "max" in req:
                    m.add(c >= int(req.get("min", 0)))
                    m.add(c <= int(req.get("max", days)))
                continue

            # 专人专项「至少上班天数」= 该人的硬性下限（可超出全局目标）
            if wr_work is not None:
                m.add(c >= max(0, min(int(wr_work), days)))

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

        # 全局均衡变量（仅阶段二）：min(其余人的班数) / max(其余人的班数)。
        #
        # 「其余人」= 所有「可以自由多吃班次」的人，即
        #   ① 豁免人员（不参与达标、只补剩余班次）；
        #   ② 有专人专项要求的人员（只设了下限，没有上限，因此会吸收剩余班次）。
        # 为什么必须把它们放在同一个均衡组里：如果只对豁免组单独均衡、
        # 而把「有下限无上限」的人排除在外，求解器会把全部剩余班次塞给后者
        # （他们多吃班次不付出任何代价），豁免人员就被挤成 0 班。
        # 放进同一组后，目标变成「让这一组里最少的人尽量多」，于是剩余班次
        # 会在整组之间平分，而不是被少数人独吞。
        balance_min: Optional[Any] = None
        balance_max: Optional[Any] = None
        balance_group = [nm for nm in names
                         if nm in exempt or (with_deviation and "max" not in worker_req.get(nm, {}))]
        if with_deviation and len(balance_group) >= 2:
            group_counts = [count_v[nm] for nm in balance_group]
            balance_max = m.new_int_var(0, days, "share_max")
            balance_min = m.new_int_var(0, days, "share_min")
            m.add_max_equality(balance_max, group_counts)
            m.add_min_equality(balance_min, group_counts)

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

        # 班次硬性约束：导入/上传的默认班次 = 该人员唯一可排的班次。
        # 上传早班就只能排早班，其它班次直接禁止（不是“偏好”，是硬规则）。
        # 未指定默认班次的人员不受限制（保持兼容）。
        for w in range(len(names)):
            pref = default_shift.get(names[w])
            if not pref or pref not in shifts:
                continue
            for d in range(days):
                for s in shifts:
                    if s != pref:
                        m.add(x[w, d, s] == 0)

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
            "x": x, "y": y, "r": r, "z": z,
            "count_v": count_v, "reached_v": reached_v,
            "over_dev": over_dev, "under_dev": under_dev,
            "single_rest_vars": single_rest_vars,
            "exempt_count_vars": exempt_count_vars,
            "balance_min": balance_min, "balance_max": balance_max,
            "balance_group": balance_group,
        }
        return m, track

    # ---------- 求解 ----------
    def _solve(m, limit: float):
        solver = cp_model.CpSolver()
        if limit:
            solver.parameters.max_time_in_seconds = float(limit)
        # 单线程搜索 + 固定种子 => 同配置同结果（多线程并行求解无法保证确定性）
        solver.parameters.num_search_workers = int(config.get("num_search_workers", 1))
        solver.parameters.random_seed = seed
        st = solver.solve(m)
        return solver, st

    def _solve_lexicographic(m, objectives, limit):
        """**真正的**词典序多目标求解。

        重要：CP-SAT 不支持「连续多次 minimize 即词典序」——实测 OR-Tools 9.15
        只有**最后一次** minimize 生效，前面写的目标会被静默丢弃（这一点与本文件
        早期注释里的说法相反，已修正）。所以这里逐个目标求解：每层求出最优值后，
        把「该目标 == 本层最优值」加成硬约束，再优化下一层，从而保证高优先级真正被固定。

        objectives: [(名称, 线性表达式), ...]，按优先级从高到低。
        返回 (solver, status)，solver 中保存的是满足全部已达成优先级的最优解。
        """
        solver, st = _solve(m, limit)
        if not objectives or st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return solver, st
        for _name, expr in objectives:
            m.minimize(expr)
            solver, st = _solve(m, limit)
            if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                return solver, st
            m.add(expr == int(round(solver.objective_value)))   # 固定本层，再进下一层
        # 收起全部目标后重新求解一次，得到同时满足各层固定值的解
        return _solve(m, limit)

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
        # 重算单休与连休超限（豁免人员不参与休息计算，跳过统计）。
        # 「专人专项约束」人员按其自己的连休范围判定，不用班组默认值。
        _sr, _rv = 0, 0
        for w in range(len(names)):
            if names[w] in exempt:
                continue
            rb = (worker_rules.get(names[w]) or {}).get("rest_block") or rest_block
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
        # 岗位指派（一人一天只干一个岗位）
        for (w, d, role), zv in track.get("z", {}).items():
            if solver.value(zv):
                result.role_assignments[(names[w], d)] = role

    # 需要达标目标 -> 两阶段；否则单次求解（词典序多目标）
    def _build_objectives(m, track) -> List[Tuple[str, Any]]:
        """按优先级从高到低构造目标列表 [(名称, 线性表达式), ...]。

        注意：不能直接连续调用 m.minimize()——CP-SAT 只认最后一个，前面的会被静默丢弃
        （详见 _solve_lexicographic 的说明）。这里只负责造表达式，由求解器逐层固定。

        优先级：
          ① 班数贴近目标（偏离最小）→ ② 非豁免之间均衡 → ③ 未达标者：最差者尽量接近目标
          且未达标者之间尽量均分 → ④ 软性避免单休 →
          ⑤ 全局均衡：让「可自由多吃班次的人」（豁免 + 只设下限的专人专项人员）
             里班数最少的人尽量多，再把它们之间的差距压小。
             这一层保证剩余班次在整组之间平分，而不是被少数没有上限的人独吞。
        层内两级用加权 (days+2)*max − min 合并（权重 days+2 > days ≥ max，
        保证先最小化 max、再最小化 max−min），少一层求解更快。
        班次是硬性约束（上传班次=唯一可排班次），不属于软目标。
        """
        objs: List[Tuple[str, Any]] = []
        if track["over_dev"]:
            objs.append(("班数贴近目标",
                         sum(track["over_dev"][nm] + track["under_dev"][nm]
                             for nm in track["over_dev"])))
        non_exempt_counts = [track["count_v"][nm] for nm in names if nm not in exempt]
        if len(non_exempt_counts) >= 2:
            max_c = m.new_int_var(0, days, "max_c")
            min_c = m.new_int_var(0, days, "min_c")
            m.add_max_equality(max_c, non_exempt_counts)
            m.add_min_equality(min_c, non_exempt_counts)
            objs.append(("非豁免均衡", max_c - min_c))
        # 未达标者（reached=0）：u_w = 未达标者班数（达标者记 0）；
        # min 侧用 c + days*(1-b) 把达标者排挤出最小值。
        reached_map = track["reached_v"]
        if reached_map:
            short_u, short_min_exprs = {}, []
            for nm, rch in reached_map.items():
                b = rch.Not()
                u = m.new_int_var(0, days, f"short_u_{nm}")
                m.add(u == track["count_v"][nm]).only_enforce_if(b)
                m.add(u == 0).only_enforce_if(b.Not())
                short_u[nm] = u
                short_min_exprs.append(track["count_v"][nm] + days * (1 - b))
            if short_u:
                max_s = m.new_int_var(0, days, "short_max")
                m.add_max_equality(max_s, list(short_u.values()))
                if len(short_u) >= 2:
                    # 域上限 2*days：表达式 c + days*(1-b) 在全员达标时为 c+days ≤ 2*days
                    min_s = m.new_int_var(0, 2 * days, "short_min")
                    m.add_min_equality(min_s, short_min_exprs)
                    objs.append(("未达标者接近目标且均分",
                                 (days + 2) * max_s - min_s))
                else:
                    objs.append(("未达标者接近目标", max_s))
        if track["single_rest_vars"]:
            objs.append(("软性避免单休", sum(track["single_rest_vars"])))
        # 全局均衡：先抬高「这一组里班数最少的人」，再把差距压小。
        if track.get("balance_min") is not None and track.get("balance_max") is not None:
            objs.append(("剩余班次均分（豁免+专人专项）",
                         (days + 2) * track["balance_max"] - track["balance_min"]))
        return objs

    m1, t1 = build_model(with_deviation=False)
    if t1["reached_v"]:
        # ---- 阶段一：最大化达标人数（不带偏离变量，模型更小更快） ----
        m1.maximize(sum(t1["reached_v"].values()))
        sol1, st1 = _solve(m1, time_limit_seconds)
        if st1 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            result.feasible = False
            result.status = "INFEASIBLE" if st1 == cp_model.INFEASIBLE else "UNKNOWN"
            result.message = ("无解。请结合诊断信息调整参数：降低每天人数、放宽岗位人数条件、"
                              "增加人员或放宽休息规则。")
            return result
        best = int(sol1.objective_value)
        result.phase1_status = "OPTIMAL" if st1 == cp_model.OPTIMAL else "FEASIBLE"

        # ---- 阶段二：固定达标人数，按词典序多目标优化（以阶段一解为 hint 起步） ----
        m2, t2 = build_model(with_deviation=True)
        m2.add(sum(t2["reached_v"].values()) >= best)
        for w in range(len(names)):
            for d in range(days):
                m2.add_hint(t2["y"][w, d], int(sol1.value(t1["y"][w, d])))
                for s in shifts:
                    m2.add_hint(t2["x"][w, d, s], int(sol1.value(t1["x"][w, d, s])))
        objs2 = _build_objectives(m2, t2)
        sol2, st2 = _solve_lexicographic(m2, objs2, phase2_seconds)
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
        # 无达标目标：单次求解，按词典序最小化偏离/均衡/单休/剩余班次均分
        m1, t1 = build_model(with_deviation=True)
        objs1 = _build_objectives(m1, t1)
        sol1, st1 = _solve_lexicographic(m1, objs1, time_limit_seconds)
        if st1 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            _extract(sol1, t1, st1)
            result.message = "求解成功，已得到一份可行排班表。"
        else:
            result.feasible = False
            result.status = "INFEASIBLE" if st1 == cp_model.INFEASIBLE else "UNKNOWN"
            result.message = ("无解。请结合诊断信息调整参数：降低每天人数、放宽岗位人数条件、"
                              "增加人员或放宽休息规则。")

    return result


# ===========================================================================
# 六、可运行示例（python project/app/scheduler/scheduling.py）
# ===========================================================================
def demo() -> None:
    """用示例人员数据跑一份排班，并演示「专人专项约束」覆盖班组默认值。"""
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
        # 班组默认：连休 2~5 天，连续上班 2~8 天
        "rest_block": {"min": 2, "max": 5},
        "work_block": {"min": 2, "max": 8},
        # 专人专项约束：覆盖上面两项（该人只受自己的要求约束）
        "worker_rules": {
            "王磊磊": {"rest_block": {"min": 3, "max": 4}, "work_days": 20},
            "杨志磊": {"rest_block": {"min": 2, "max": 3}},
        },
    }
    result = build_schedule(config)
    result.print_summary()


if __name__ == "__main__":
    demo()
