# 排班算法输入数据结构。
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class EmployeeInfo:
    # 参与排班的员工
    id: int
    name: str
    position_id: Optional[int] = None
    group_id: Optional[int] = None


@dataclass
class ShiftRequirement:
    # 某日期某班次的岗位需求
    shift: str
    min_count: int
    max_count: int
    position_id: Optional[int] = None
    group_id: Optional[int] = None


@dataclass
class Assignment:
    # 一条排班结果
    employee_id: int
    date: date
    shift: str
    position_id: Optional[int] = None
    group_id: Optional[int] = None


@dataclass
class ScheduleProblem:
    # 排班问题定义
    employees: List[EmployeeInfo]
    dates: List[date]
    shifts: List[str]
    requirements: Dict[Tuple[date, str], List[ShiftRequirement]]
    unavailability: Set[Tuple[int, date]] = field(default_factory=set)
    max_night_shifts: Dict[int, int] = field(default_factory=dict)


@dataclass
class ScheduleSolution:
    # 排班求解结果
    status: str  # optimal / feasible / infeasible
    assignments: List[Assignment] = field(default_factory=list)
    objective: Optional[float] = None
