# -*- coding: utf-8 -*-
"""暴力排班测试（100 次，虚拟账户，仿照真实班组数据）。

本脚本不依赖 Django，直接调用算法引擎
    project/app/scheduler/scheduling.build_schedule
对 100 份随机生成的「虚拟班组」逐一求解，并围绕三个核心点做校验：

    [测试 1] 岗位要求的人是否能够达到每天的上班要求
             —— 每天每个岗位的实际上班人数是否满足 role_req 的 >= / <= / ==。
    [测试 2] 岗位要求满足后，剩余人员是否被填补以凑满每天排班人数
             —— 每天上班总人数是否精确等于 daily_total（且每人每天 ≤1 班）。
    [测试 3] 达到最少上班数的人数是否接近最大值；达不到的人是否均分；
             若「剩余可让出的班数」加起来足以再让一个未达标者达标，则报错记录。

目的：
    在真实数据规模与约束下，用暴力随机的方式暴露算法可能出现的三类问题——
    (a) 岗位约束无法满足（尤其「等于 =」导致的岗位容量不足）；
    (b) 总人数填补不上（每天应上人数 > 全员容量 → 无解）；
    (c) 达标人数未到容量上限、或剩余班数分配不均 / 本可再提升一人达标。

运行：
    cd /Users/qizhang2004/PythonPorjects/Stiding_System
    .venv/bin/python brute_force_test.py [次数] [随机种子]
"""

from __future__ import annotations

import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

# 允许从脚本所在目录 import 项目算法模块
sys.path.insert(0, str(Path(__file__).resolve().parent))
from project.app.scheduler.scheduling import (
    build_schedule,
    capacity_analysis,
    _max_work_in_month,
)

# ---------------------------------------------------------------------------
# 常量：贴合项目真实配置（views.py）
# ---------------------------------------------------------------------------
FIXED_SHIFTS = ["早班", "中班", "晚班"]
WORK_WINDOW = {"length": 10, "max_work": 6}   # 任意 10 天最多 6 班
ROLE_POOL = ["班长", "电工", "风水管", "盾构机", "皮带", "普通"]

SURNAMES = ["王", "李", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴",
            "徐", "孙", "马", "朱", "胡", "郭", "何", "高", "林", "罗"]
GIVEN = ["磊", "强", "伟", "军", "鹏", "杰", "超", "涛", "明", "辉",
         "峰", "亮", "洋", "宁", "波", "旭", "阳", "森", "宇", "鑫"]


@dataclass
class Scenario:
    idx: int
    config: Dict[str, Any]
    workers: List[dict]
    days: int
    daily_total: int
    target: int
    exempt: List[str]
    role_req: Dict[str, Any]
    rest_block: Dict[str, Any]
    result: Any = None
    problems: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 随机场景生成（仿照真实班组）
# ---------------------------------------------------------------------------
def _make_name(rng: random.Random, used: set) -> str:
    while True:
        nm = rng.choice(SURNAMES) + rng.choice(GIVEN) + rng.choice(GIVEN)
        if nm not in used:
            used.add(nm)
            return nm


def gen_scenario(rng: random.Random, idx: int, stress: bool) -> Scenario:
    """生成一份随机但贴近真实的虚拟班组。

    正常用例保证「岗位持有人数足以覆盖每天岗位需求」「目标班数不超过每人最多班数」
    「每天人数略低于盈亏平衡」，从而让三个测试点能在可行解上跑起来；
    stress=True 时注入更苛刻的约束（高每天人数 / 岗位「等于」/ 需求超标）以暴露无解。
    """
    used: set = set()
    days = rng.choice([28, 29, 30, 31])

    # 每人最多班数（10 天 ≤6 班窗口）
    max_work = _max_work_in_month(days, WORK_WINDOW["length"], WORK_WINDOW["max_work"])
    # 目标班数：现实默认 18，且不超过每人最多班数
    target = rng.choice([t for t in (16, 17, 18) if t <= max_work] or [max_work])

    # 岗位持有人数：足够满足每天岗位需求（ceil(需求*days/max_work) 量级）
    role_counts = {
        "班长": rng.randint(2, 4),
        "电工": rng.randint(4, 6),
        "风水管": rng.randint(4, 6),
        "盾构机": rng.randint(2, 4),
        "皮带": rng.randint(4, 6),
        "普通": rng.randint(3, 6),
    }

    workers: List[dict] = []
    role_assign: Dict[str, List[str]] = defaultdict(list)
    for role in ROLE_POOL:
        for _ in range(role_counts[role]):
            nm = _make_name(rng, used)
            workers.append({"name": nm, "roles": [role]})
            role_assign[role].append(nm)

    # 约 20% 的人再挂一个第二岗位（多技能）
    multi = rng.sample(workers, k=min(len(workers), rng.randint(2, 6)))
    for w in multi:
        extra = rng.choice([r for r in ROLE_POOL if r not in w["roles"]])
        w["roles"].append(extra)
        role_assign[extra].append(w["name"])

    # 岗位每天需求：正常=可达成的量；压力=可能超标或改用「等于」
    role_req: Dict[str, Any] = {}
    for role, normal_cnt in [("班长", 1), ("电工", 2), ("盾构机", 1),
                             ("皮带", 2), ("风水管", 2)]:
        holders = len(role_assign.get(role, []))
        max_feasible = max(1, holders * max_work // days)  # 每天最多能持续满足的人数
        if stress and rng.random() < 0.4:
            op = rng.choice([">=", "=="])
            cnt = max(1, min(holders, max_feasible + rng.randint(0, 2)))
        else:
            op = ">="
            cnt = max(1, min(normal_cnt, max_feasible))
        role_req[role] = {"op": op, "count": cnt}

    # 连休范围（正常 2~4；压力用例可收紧）
    rmax = rng.choice([3, 4, 4, 5])
    rest_block = {"min": 2, "max": rmax}

    # 豁免人员（正常少量）
    n = len(workers)
    exempt = rng.sample([w["name"] for w in workers],
                        k=rng.randint(0, min(3, n // 6))) if n >= 6 else []

    # 每天人数：正常略低于盈亏平衡（留出余量，部分人达标、部分人不足）；
    #          压力用例偏高，逼近甚至超过容量 → 可能无解
    break_even = round(n * target / days)
    if stress and rng.random() < 0.5:
        daily_total = break_even + rng.randint(0, 3)
    else:
        daily_total = max(1, break_even - rng.randint(1, 3))

    config = {
        "workers": [{"name": w["name"], "roles": w["roles"]} for w in workers],
        "shifts": FIXED_SHIFTS,
        "days": days,
        "daily_total": daily_total,
        "role_req": role_req,
        "min_shift_target": target,
        "exempt_workers": exempt,
        "rest_block": rest_block,
        "work_window": dict(WORK_WINDOW),
        "worker_default_shift": {w["name"]: rng.choice(FIXED_SHIFTS) for w in workers},
    }
    return Scenario(idx, config, workers, days, daily_total, target,
                    exempt, role_req, rest_block)


# ---------------------------------------------------------------------------
# 校验：三个测试点
# ---------------------------------------------------------------------------
def _working_per_day(result, days: int):
    wd = defaultdict(set)
    for d in range(days):
        for nms in result.per_day.get(d, {}).values():
            wd[d].update(nms)
    return wd


def check_role_req(sc: Scenario) -> List[str]:
    """测试 1：岗位要求的人能否达到每天的上班要求。"""
    probs: List[str] = []
    if not sc.result.feasible:
        return probs
    role_of = {w["name"]: set(w["roles"]) for w in sc.workers}
    wd = _working_per_day(sc.result, sc.days)
    for role, spec in sc.role_req.items():
        op, cnt = spec["op"], spec["count"]
        holders = [n for n, rs in role_of.items() if role in rs]
        for d in range(sc.days):
            working = sum(1 for n in holders if n in wd[d])
            if op == ">=" and working < cnt:
                probs.append(f"[岗位] 第{d+1}天 {role} 需>={cnt}人，实际{working}人")
            elif op == "<=" and working > cnt:
                probs.append(f"[岗位] 第{d+1}天 {role} 需<={cnt}人，实际{working}人")
            elif op == "==" and working != cnt:
                probs.append(f"[岗位] 第{d+1}天 {role} 需=={cnt}人，实际{working}人")
    return probs


def check_daily_fill(sc: Scenario) -> List[str]:
    """测试 2：岗位满足后，剩余人员是否填补凑满每天排班人数。"""
    probs: List[str] = []
    if not sc.result.feasible:
        return probs
    wd = _working_per_day(sc.result, sc.days)
    names = [w["name"] for w in sc.workers]
    for d in range(sc.days):
        total = sum(1 for n in names if n in wd[d])
        if total != sc.daily_total:
            probs.append(f"[填补] 第{d+1}天上班{total}人，应上{sc.daily_total}人")
    for d in range(sc.days):
        for n in names:
            assigned = sum(1 for s in FIXED_SHIFTS
                           if sc.result.assignments.get((n, d, s)))
            if assigned > 1:
                probs.append(f"[填补] {n} 第{d+1}天被排了{assigned}个班")
    return probs


def check_reach_and_balance(sc: Scenario) -> List[str]:
    """测试 3：达标人数接近最大值 + 未达标者均分 + 冗余班数可再提升一人的报错。"""
    probs: List[str] = []
    if not sc.result.feasible:
        return probs
    names = [w["name"] for w in sc.workers]
    counts = {n: sc.result.worker_counts.get(n, 0) for n in names}
    exempt = set(sc.exempt)
    non_exempt = [n for n in names if n not in exempt]
    target = sc.target

    reached = [n for n in non_exempt if counts[n] >= target]
    shortfall = [n for n in non_exempt if counts[n] < target]

    cap = capacity_analysis(
        len(names), sc.daily_total, sc.days,
        sc.rest_block.get("max", 4), target, len(exempt),
    )
    max_fillable = cap["max_fillable"]

    # 3a. 报错：求解结果是否达到真实最优（达标人数 < 理论最优 = 还能再提升至少 1 人）
    #     max_fillable 现在由求解器精确计算，所以 reached < max_fillable 意味着
    #     求解器在时限内没有最大化达标人数（或岗位约束额外收紧了上限）。
    if len(reached) < max_fillable:
        probs.append(f"[报错] 实际达标{len(reached)}人，理论最多{max_fillable}人，"
                     f"还能再提升{max_fillable - len(reached)}人达标")

    # 3b. 均分：未达标者的班数是否「均分」
    if len(shortfall) >= 2:
        vals = sorted(counts[n] for n in shortfall)
        if vals[-1] - vals[0] > 2:
            probs.append(f"[均分] 未达标者班数不均：{vals}")
    return probs


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_once(idx: int, rng: random.Random, stress: bool,
             t1: float, t2: float) -> Scenario:
    sc = gen_scenario(rng, idx, stress)
    try:
        # 压力测试刻意缩短时限：既加速暴力遍历，又能暴露「超时只拿到可行解/
        # 未达最优」这类生产上也会遇到的问题。
        sc.result = build_schedule(sc.config,
                                   time_limit_seconds=t1, phase2_seconds=t2)
    except Exception as e:  # noqa: BLE001 —— 记录异常，不中断暴力测试
        sc.problems.append(f"[异常] {type(e).__name__}: {e}")
        return sc

    if not sc.result.feasible:
        sc.problems.append(
            f"[无解] {sc.result.status}：" + "；".join(sc.result.diagnostics or ["无诊断"])
        )
    else:
        sc.problems += check_role_req(sc)
        sc.problems += check_daily_fill(sc)
        sc.problems += check_reach_and_balance(sc)
    return sc


def _cat_of(p: str) -> str:
    return p.split("]")[0].lstrip("[")


def main() -> None:
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20240821
    t1 = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0   # 阶段一时限（秒）
    t2 = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0   # 阶段二时限（秒）
    rng = random.Random(seed)

    print("=" * 76)
    print(f"暴力排班测试：{total} 次 | 随机种子 {seed} | 班次 {FIXED_SHIFTS} | "
          f"工作窗口 {WORK_WINDOW} | 时限 {t1}s/{t2}s")
    print("=" * 76)

    scenarios = [run_once(i + 1, rng, (i % 5 == 4), t1, t2) for i in range(total)]

    feasible = [s for s in scenarios if s.result and s.result.feasible]
    infeasible = [s for s in scenarios if s.result and not s.result.feasible]
    has_problem = [s for s in feasible if s.problems]

    print(f"\n求解 {total} 次：可行 {len(feasible)}，无解 {len(infeasible)}，"
          f"可行但有问题的 {len(has_problem)}")

    cat = Counter(_cat_of(p) for s in scenarios for p in s.problems)
    print("\n问题分类统计：")
    for k, v in cat.most_common():
        print(f"  {k:6} × {v}")

    print("\n" + "=" * 76)
    print("典型问题样例（每类最多 3 条）：")
    print("=" * 76)
    seen_cat: Dict[str, int] = {}
    for s in scenarios:
        shown = []
        for p in s.problems:
            c = _cat_of(p)
            if seen_cat.get(c, 0) < 3:
                seen_cat[c] = seen_cat.get(c, 0) + 1
                shown.append(p)
        if not shown:
            continue
        print(f"\n【场景 {s.idx}】{len(s.workers)}人 · {s.days}天 · 每天{s.daily_total}人 · "
              f"目标{s.target}班 · 豁免{len(s.exempt)}人 · 连休{s.rest_block} · 岗位"
              f"{ {r: v['op'] + str(v['count']) for r, v in s.role_req.items()} }")
        for p in shown:
            print(f"    - {p}")

    print("\n" + "=" * 76)
    print("结论：")
    print("  1) 岗位「等于 ==」约束 + 目标班数过高，是最常见的排不满/无解来源；")
    print("  2) 每天应上人数超过『人数 × 每人最多班数』时整体无解，需加人或下调人数；")
    print("  3) 容量有限时达标人数达不到理论上限属正常，需设豁免；")
    print("     「达标/均分/报错」若在正常场景出现，说明求解未充分优化（疑似算法问题）。")
    print("=" * 76)


if __name__ == "__main__":
    main()
