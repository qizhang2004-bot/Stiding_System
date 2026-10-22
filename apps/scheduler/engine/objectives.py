# 公平性等优化目标（排班引擎）。
# 当前基础目标：总排班数量最小化（在满足硬约束前提下尽量精简）。
# 公平性目标：夜班均衡度（供 solver 叠加次级优化项使用）。


def night_shift_counts(assignments, night_shift="夜班"):
    # 统计每位员工的夜班数量
    counts = {}
    for item in assignments:
        if item.shift == night_shift:
            counts[item.employee_id] = counts.get(item.employee_id, 0) + 1
    return counts


def night_balance_score(assignments, night_shift="夜班"):
    # 夜班均衡度得分：夜班数量的极差，越小越均衡
    counts = night_shift_counts(assignments, night_shift)
    if not counts:
        return 0
    return max(counts.values()) - min(counts.values())
