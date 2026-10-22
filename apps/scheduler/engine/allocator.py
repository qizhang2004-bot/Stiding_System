# 排班规划（第一阶段/第二阶段）：人多人少判断与班数分配目标。
# 纯计算模块：不依赖 Django ORM，供引擎、配置页面与后续正式排班流程复用。
#
# 变量定义：
#   D = 排班周期总天数（每月 25 日至次月 24 日，跨月/跨年，见 selectors.period_range）
#   R = 每天要求上班人数（ShiftConfig.daily_worker_count）
#   M = 每人至少应上班数 / 排班目标班数（ShiftConfig.required_shifts，默认 18）
#   N = 班组有效职工总人数（在职职工）
#
# 判断规则（严格按业务定义）：
#   可排开最大人数 = D × R ÷ M
#   可排开最大人数 > N  → 人多模式（名额有剩余），使用算法一
#   可排开最大人数 ≤ N  → 人少模式（名额紧张），使用算法二
#
# 注意：判断规则以“可排开最大人数与 N 的大小关系”为准；
# 算法二示例中的数值（D=30,R=18,N=25）按上述规则属于人多模式，示例仅演示
# floor(D×R÷N) 的计算方式，特此注明。
from dataclasses import dataclass, field
from typing import Dict, List, Optional

MODE_SURPLUS = "surplus"  # 人多模式（名额有剩余）→ 算法一
MODE_TIGHT = "tight"      # 人少模式（名额紧张）→ 算法二


@dataclass
class ShiftAllocation:
    # 班数分配目标结果
    mode: str                           # MODE_SURPLUS / MODE_TIGHT
    feasible: bool                      # 当前数据下是否存在可行的班数分配目标
    total_days: int                     # D：排班周期总天数
    daily_required: int                 # R：每天要求上班人数
    base_shifts: int                    # M：每人至少应上班数（目标班数）
    employee_count: int                 # N：班组有效职工总人数
    total_slots: int                    # D × R：周期内总名额
    capacity: float                     # 可排开最大人数 = D × R ÷ M
    per_person_targets: Dict[int, int]  # 职工ID -> 目标班数（组内班数差 ≤ 1）
    baseline: Optional[int] = None      # 人少模式：理论最低班数 floor(D×R÷N)（仅参考）
    surplus: Optional[int] = None       # 人多模式：普通人员达到目标后的剩余名额
    full_count: Optional[int] = None             # 理论可保证最低班数的人数 K = min(N, floor(D÷M))
    theoretical_exempt_count: Optional[int] = None  # 理论豁免人数 E = N − K
    exempt_remaining_target: Optional[int] = None   # 豁免人员剩余目标 R' = D − K×M
    notes: List[str] = field(default_factory=list)  # 说明、警告或不可行原因


def plan_shift_targets(
    *,
    total_days: int,
    daily_required: int,
    base_shifts: int,
    normal_ids: List[int],
    exempt_ids: List[int],
) -> ShiftAllocation:
    # 判断人多人少并计算每人班数分配目标。
    #
    # 算法一（人多模式）：
    #   1. 全体（含豁免）先以 M 为基线；剩余名额 = D×R − N×M。
    #   2. 剩余名额优先由豁免人员承担，豁免人员之间班数差尽可能小（≤1）。
    #   3. 豁免人员无法承担的部分由普通人员平均分担，普通人员之间班数差 ≤1。
    #
    # 算法二（人少模式，修复版）：
    #   K = min(N, floor(D×R÷M))：理论上最多可保证 M 班的普通人员数；
    #   普通人员目标 = M（硬性下限，绝不悄悄降低）；
    #   剩余需求 D×R − 普通人员数×M 由豁免人员公平分配（组内班数差 ≤1）；
    #   普通人员数超过 K 且豁免不足时 → 明确报告不可行。
    if total_days <= 0:
        raise ValueError("排班周期总天数必须为正。")
    if daily_required < 0:
        raise ValueError("每天要求上班人数不能为负。")
    if base_shifts <= 0:
        raise ValueError("每人至少应上班数必须为正。")

    all_ids = list(normal_ids) + list(exempt_ids)
    n_total = len(all_ids)
    n_normal = len(normal_ids)
    n_exempt = len(exempt_ids)
    total_slots = total_days * daily_required

    # 理论口径（人多/人少两种模式统一计算，避免人多模式下三项目标值缺失为 None）：
    #   K = min(N, ⌊D×R÷M⌋)  理论可保证最低班数 M 的人数
    #   E = N − K             理论豁免人数
    #   R' = D×R − K×M        需要由豁免人员承担的剩余班数（人多模式下即“剩余名额”）
    # 目标班数 M 超过周期天数 D 时，任何人都不可能上满 M 天 → K = 0（无人可达标）。
    if base_shifts <= total_days:
        theory_full = min(n_total, total_slots // base_shifts)
    else:
        theory_full = 0
    theory_exempt = max(n_total - theory_full, 0)
    theory_remaining = total_slots - theory_full * base_shifts

    def make(
        *,
        mode: str = MODE_TIGHT,
        feasible: bool = True,
        per_person_targets: Optional[Dict[int, int]] = None,
        baseline: Optional[int] = None,
        surplus: Optional[int] = None,
        full_count: Optional[int] = None,
        theoretical_exempt_count: Optional[int] = None,
        exempt_remaining_target: Optional[int] = None,
        notes: Optional[List[str]] = None,
    ) -> ShiftAllocation:
        return ShiftAllocation(
            mode=mode,
            feasible=feasible,
            total_days=total_days,
            daily_required=daily_required,
            base_shifts=base_shifts,
            employee_count=n_total,
            total_slots=total_slots,
            capacity=total_slots / base_shifts,
            per_person_targets=per_person_targets or {},
            baseline=baseline,
            surplus=surplus,
            full_count=theory_full if full_count is None else full_count,
            theoretical_exempt_count=(
                theory_exempt
                if theoretical_exempt_count is None
                else theoretical_exempt_count
            ),
            exempt_remaining_target=(
                theory_remaining
                if exempt_remaining_target is None
                else exempt_remaining_target
            ),
            notes=notes or [],
        )

    if n_total == 0:
        return make(feasible=False, notes=["班组没有有效职工，无法排班。"])

    # 理论上限检查：每天上班人数不能超过有效职工总数（后续每日人数硬约束的前置判断）
    if daily_required > n_total:
        return make(
            feasible=False,
            notes=[
                f"每天要求上班 {daily_required} 人，但班组有效职工仅 {n_total} 人，无法满足每日人数要求。"
            ],
        )

    capacity = total_slots / base_shifts

    if capacity > n_total:
        # ---- 人多模式（算法一）：名额有剩余 ----
        if base_shifts > total_days:
            return make(
                mode=MODE_SURPLUS,
                feasible=False,
                notes=[
                    f"目标班数 {base_shifts} 超过周期天数 {total_days}，普通人员无法达到目标班数。"
                ],
            )
        surplus = total_slots - n_total * base_shifts
        targets = {eid: base_shifts for eid in all_ids}

        if surplus > 0:
            # 剩余名额优先由豁免人员承担（组内平均，班数差 ≤ 1）
            exempt_capacity = (total_days - base_shifts) * n_exempt
            exempt_take = min(surplus, exempt_capacity)
            if n_exempt > 0 and exempt_take > 0:
                extra, remainder = divmod(exempt_take, n_exempt)
                for index, eid in enumerate(exempt_ids):
                    targets[eid] = base_shifts + extra + (1 if index < remainder else 0)

            # 豁免无法承担的部分由普通人员平均分担
            remaining = surplus - exempt_take
            if remaining > 0:
                if n_normal == 0:
                    return make(
                        mode=MODE_SURPLUS,
                        feasible=False,
                        notes=[
                            f"剩余名额 {surplus} 班无法承担：没有普通人员可分担，且豁免人员已达到周期上限。"
                        ],
                    )
                extra, remainder = divmod(remaining, n_normal)
                for index, eid in enumerate(normal_ids):
                    targets[eid] = base_shifts + extra + (1 if index < remainder else 0)

        return make(
            mode=MODE_SURPLUS,
            per_person_targets=targets,
            surplus=surplus,
        )

    # ---- 人少模式（算法二·修复）：名额紧张，普通人员必须达到最低班数 M ----
    # 数学规则（与业务定义一致）：
    #   K = min(N, floor(D ÷ M))：理论上最多可保证 M 班的人数（普通人员）
    #   E = N − K：理论豁免人数
    #   R' = D − K × M：豁免人员剩余目标（总班数）
    # 实际分配：普通人员全部定为 M 班（硬性下限）；剩余需求由配置的豁免人员
    # 公平分配（组内班数差 ≤ 1）。普通人员数量超过 K 且豁免人数不足时，
    # 明确报告不可行，绝不悄悄降低普通人员的最低班数。
    max_full = theory_full
    theoretical_exempt = theory_exempt
    remaining_theory = theory_remaining

    if n_normal > max_full:
        need_exempt = n_normal - max_full
        return make(
            mode=MODE_TIGHT,
            feasible=False,
            baseline=total_slots // n_total,
            full_count=max_full,
            theoretical_exempt_count=theoretical_exempt,
            exempt_remaining_target=remaining_theory,
            notes=[
                f"普通人员 {n_normal} 人，但周期总需求 {total_slots} 人次最多只能保证 "
                f"{max_full} 人达到最低班数 {base_shifts} 班；需要至少豁免 {need_exempt} 人"
                f"（当前豁免 {n_exempt} 人）。请增加豁免人员或调整每日人数配置。"
            ],
        )

    targets = {eid: base_shifts for eid in normal_ids}
    remaining = total_slots - n_normal * base_shifts  # ≥ 0
    if n_exempt > 0:
        if remaining > 0:
            extra, remainder = divmod(remaining, n_exempt)
            for index, eid in enumerate(exempt_ids):
                targets[eid] = extra + (1 if index < remainder else 0)
        else:
            for eid in exempt_ids:
                targets[eid] = 0

    notes = [
        f"人少模式：理论可保证最低班数 {base_shifts} 班的普通人员 {max_full} 人，"
        f"理论豁免 {theoretical_exempt} 人，豁免剩余目标 {remaining_theory} 班；"
        f"当前配置豁免 {n_exempt} 人，剩余需求 {remaining} 班已由豁免人员公平分配。"
    ]
    return make(
        mode=MODE_TIGHT,
        per_person_targets=targets,
        baseline=total_slots // n_total,
        full_count=max_full,
        theoretical_exempt_count=theoretical_exempt,
        exempt_remaining_target=remaining_theory,
        notes=notes,
    )
