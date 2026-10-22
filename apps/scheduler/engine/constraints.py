# 硬约束与软约束（排班引擎）。
from dataclasses import dataclass


@dataclass
class Constraint:
    # 约束描述（用于诊断与文档化）
    code: str   # 约束编号，如 H1
    name: str   # 约束名称
    kind: str   # hard / soft
    description: str


HARD_CONSTRAINTS = [
    Constraint("H1", "每人每天至多一个班次", "hard", "任一员工同一天最多被安排一个班次。"),
    Constraint("H2", "岗位最低需求", "hard", "每个日期/班次安排人数不低于岗位需求 min_count。"),
    Constraint("H3", "个人禁排", "hard", "必须休息/不可排班的人员不得安排班次。"),
    Constraint("H4", "夜班上限", "hard", "个人夜班数量不超过规则上限。"),
]

SOFT_CONSTRAINTS = [
    Constraint("S1", "夜班均衡", "soft", "夜班在各员工之间尽量均衡分配。"),
    Constraint("S2", "连班限制", "soft", "连续工作天数不超过默认上限。"),
]
