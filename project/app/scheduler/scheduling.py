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
2. 阶段二：固定阶段一的达标人数，用**词典序多目标**优化（OR-Tools ≥9.9 多次 minimize 即词典序，
   先写者最优先）：① 每人班数贴近目标 → ② 非豁免班数均衡 → ③ 未达标者尽量接近目标且均分 →
   ④ 软性避免单休 → ⑤ 豁免人员尽量少且均衡。用阶段一的解作为 hint 起步。

班次是硬性约束：上传/导入的班次即该人员唯一可排的班次。

同配置必然产生同结果：求解器使用由配置内容派生的确定性随机种子（可用
``config["random_seed"]`` 覆盖），重复生成不再“每次都不一样”。

支持的能力（对应需求）
----------------------
1. 每天下井总人数 / 每班每天人数     -> ``daily_total`` / ``shift_demand``
2. 岗位人数条件（至少/至多/等于）    -> ``role_req``（兼容旧字段 ``role_min``）；
   一人一天只能干一个岗位（多岗位人员当天只计入一个岗位的配额）
3. 每人每天最多上一个班              -> 内置
4. 连休 2~4 天（不允许单休）         -> ``rest_block``（硬约束）
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
# 二、默认权重（软性目标评分）
# 说明：阶段二/单次求解已改用词典序多目标（优先级固定，见模块 docstring），
# 以下权重仅作为向后兼容保留，并参与随机种子派生（保证历史行为一致）。
# ===========================================================================
DEFAULT_WEIGHTS = {
    "single_rest": 100,    # 每个单休日的惩罚权重（soft 模式用）
    "shift_target": 2,     # 每人偏离目标班数的惩罚权重
    "reach_target": 1000,  # 每人达到最少班数的奖励权重（最大化达标人数）
    "shift_mismatch": 1,   # 每班次偏离默认班次的惩罚权重
}


def _derive_seed(snapshot: Dict[str, Any]) -> int:
    """从配置快照派生确定性随机种子：同配置 -> 同种子 -> 同排班结果。"""
    digest = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


# ===========================================================================
# 三、容量预估（纯数学，不依赖 Django）
# ===========================================================================
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

    没有工作窗口限制，每人最多能上 days 天（无密度上限），
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
        max_work     每人最多班数（= 周期天数，无额外窗口限制）
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

    # 3b) 逐人硬性下限与豁免的联动预检（有硬性 min 时才能精确判断）
    worker_req = config.get("worker_shift_req") or {}
    exempt_names = set(config.get("exempt_workers") or [])
    non_exempt_mins = 0
    for w in workers:
        req = worker_req.get(w.get("name"), {})
        if w.get("name") not in exempt_names and "min" in req:
            non_exempt_mins += int(req.get("min", 0))
    total_need = total_daily * days
    if non_exempt_mins > total_need:
        diag.append(
            f"非豁免人员的硬性最低班数合计 {non_exempt_mins} 班，"
            f"超过周期总班次 {total_need} 班（豁免人数不够或目标太高），整体无解。"
        )
    else:
        leftover = total_need - non_exempt_mins  # 豁免人员可补的剩余班次
        for role, spec in role_req.items():
            if isinstance(spec, (int, float)):
                spec = {"op": ">=", "count": int(spec)}
            op = str(spec.get("op", ">="))
            if op not in (">=", "=="):
                continue
            cnt = int(spec.get("count", 0))
            need = cnt * days
            guaranteed = sum(
                int(worker_req.get(w.get("name"), {}).get("min", 0))
                for w in workers
                if role in w.get("roles", []) and w.get("name") not in exempt_names
            )
            if guaranteed < need and (need - guaranteed) > leftover:
                diag.append(
                    f"岗位「{role}」每天 {op} {cnt} 人，{days} 天共需 {need} 人天；"
                    f"非豁免该岗位人员硬性只保证 {guaranteed} 人天，缺口 {need - guaranteed} "
                    f"人天大于豁免人员全部可补的 {leftover} 人天。"
                    f"请豁免其它岗位的人员，或降低该岗位人数条件。"
                )

    # 4) 休息规则检查
    if rest_block:
        rmin = int(rest_block.get("min", 2))
        rmax = int(rest_block.get("max", 4))
        if rmin < 1 or rmax < rmin:
            diag.append(f"休息规则 rest_block 不合法: min={rmin}, max={rmax}")

    fatal = any("不足" in d or "超出" in d or "没有匹配" in d or "大于豁免人员" in d
                or "整体无解" in d for d in diag)
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
        worker_default_shift: {name: shift} 硬性班次：该人员只能排这个班次（上传什么班次就排什么班次）
        rest_block:       {"min": int, "max": int} 连休天数硬约束（默认 2~4），
                          即休息必须多天连休且不超过上限（含月初/月末边界）。
        work_block:       {"min": int, "max": int} 连续上班天数硬约束（可选），
                          默认 min=2（连续上班至少 2 天，无上限）；
                          规则建议 min=(周期天数//3)−最长休息、max=(周期天数//3)−最短休息。
        no_single_rest:   bool 或 {"hard": bool, "weight": float} 软性避免单休
                          （仅当未设置 rest_block 时作为软目标使用）
        random_seed:      int 求解随机种子（不传则按配置内容自动派生，同配置同结果）

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
    #      2.4 休息日约束：连休 [min,max] 天（含月初/月末边界）
    #          豁免人员不参与休息规则，可自由排班
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
    rest_block = config.get("rest_block") or {}
    work_block = config.get("work_block") or {}
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

    # ---------- 确定性随机种子（同配置 -> 同结果） ----------
    seed = _derive_seed({
        "workers": sorted(
            (w.get("name"), sorted(w.get("roles") or []),
             w.get("default_shift", ""), w.get("worked", 0), w.get("required", 0))
            for w in workers
        ),
        "shifts": list(shifts), "days": days,
        "daily_total": daily_total, "shift_demand": sorted(demand.items()),
        "role_req": sorted(
            (r, v.get("op", ">="), int(v.get("count", 0)))
            for r, v in role_req.items()
        ),
        "min_shift_target": min_shift_target,
        "exempt_workers": sorted(exempt),
        "worker_shift_req": sorted(
            (n, sorted(v.items())) for n, v in worker_req.items()
        ),
        "worker_default_shift": sorted(default_shift.items()),
        "rest_block": sorted(rest_block.items()),
        "work_block": sorted(work_block.items()),
        "no_single_rest": bool(nsr),
        "weights": sorted(weights.items()),
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

        # 约束 4：休息规则（连休天数 ∈ [min, max]，硬约束）
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

        # 约束 4b：连续上班天数 ∈ [work_run_min, work_run_max]（硬约束，含月初/月末边界）
        # 用户规则：min = (周期天数//3) − 最长休息；max = (周期天数//3) − 最短休息。
        # 未提供 work_block 时退化为「连续上班至少 2 天」（旧行为）。
        # 豁免人员不受此约束。
        wr_min = int(work_block.get("min", 2)) if work_block else 2
        wr_max = int(work_block.get("max", 0)) if work_block else 0
        for w in range(len(names)):
            if names[w] in exempt:
                continue
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
                if length <= days - 1:
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
        # 岗位指派（一人一天只干一个岗位）
        for (w, d, role), zv in track.get("z", {}).items():
            if solver.value(zv):
                result.role_assignments[(names[w], d)] = role

    # 需要达标目标 -> 两阶段；否则单次求解（词典序多目标）
    def _add_lexicographic_objectives(m, track) -> None:
        """按优先级挂载词典序目标（OR-Tools ≥9.9：多次 minimize 即词典序，先写者最优先）：
        ① 班数贴近目标（偏离最小）→ ② 非豁免均衡 → ③ 未达标者：最差者尽量接近目标、
        且未达标者之间尽量均分（同一层内用 (days+2)*max−min 保证先 max 后 min）→
        ④ 软性避免单休 → ⑤ 豁免尽量少且均衡（同样合并为一层）。

        注：③/⑤ 内部两级合并进一个加权目标，减少一层求解证明（单线程下更省时间）。
        班次是硬性约束（上传班次=唯一可排班次），不属于软目标。
        """
        if track["over_dev"]:
            m.minimize(sum(track["over_dev"][nm] + track["under_dev"][nm]
                           for nm in track["over_dev"]))
        non_exempt_counts = [track["count_v"][nm] for nm in names if nm not in exempt]
        if len(non_exempt_counts) >= 2:
            max_c = m.new_int_var(0, days, "max_c")
            min_c = m.new_int_var(0, days, "min_c")
            m.add_max_equality(max_c, non_exempt_counts)
            m.add_min_equality(min_c, non_exempt_counts)
            m.minimize(max_c - min_c)
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
                    # 权重 days+2 > days ≥ max_s，保证层内先最小化 max_s、再最小化 (max_s-min_s)
                    m.minimize((days + 2) * max_s - min_s)
                else:
                    m.minimize(max_s)
        if track["single_rest_vars"]:
            m.minimize(sum(track["single_rest_vars"]))
        if track["exempt_count_vars"]:
            exempt_max = m.new_int_var(0, days, "exempt_max")
            exempt_min = m.new_int_var(0, days, "exempt_min")
            m.add_max_equality(exempt_max, track["exempt_count_vars"])
            m.add_min_equality(exempt_min, track["exempt_count_vars"])
            m.minimize((days + 2) * exempt_max - exempt_min)

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
        _add_lexicographic_objectives(m2, t2)
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
        # 无达标目标：单次求解，按词典序最小化偏离/均衡/单休/班次偏好
        m1, t1 = build_model(with_deviation=True)
        _add_lexicographic_objectives(m1, t1)
        sol1, st1 = _solve(m1, time_limit_seconds)
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
    }
    result = build_schedule(config)
    result.print_summary()


if __name__ == "__main__":
    demo()
