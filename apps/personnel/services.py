# 人员业务逻辑。
import re

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from apps.organization.models import OrganizationUnit, Team

from .models import Employee

# 岗位分隔符：中文顿号、中文逗号、英文逗号、分号、斜杠（统一由 parse_positions 处理）
_POSITION_SEPARATOR_RE = re.compile(r"[、，,;；/]+")


class ImportConflictError(Exception):
    # 导入写入冲突（职工编号重复/并发冲突/目标失效），整批未写入
    pass


def parse_positions(value):
    # 解析岗位文本为去重后的岗位列表（集中封装，供配置页面、人员展示与后续排班算法复用）。
    # 支持中文顿号、中文逗号、英文逗号等分隔符；去除前后空格、过滤空项、按出现顺序去重。
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    seen = set()
    result = []
    for part in _POSITION_SEPARATOR_RE.split(text):
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


# “普通/普工”表示无特殊岗位资格，不作为正式岗位参与排班与岗位统计
PLAIN_POSITION_NAMES = {"普通", "普工", "普工岗位"}


def parse_special_positions(value):
    # 解析“特殊岗位”资格：过滤掉表示普工的岗位名称（岗位为空/普工都按普工处理）
    return [p for p in parse_positions(value) if p not in PLAIN_POSITION_NAMES]


def normalize_position(name):
    # 统一岗位名称（去除前后空格），保存岗位要求时使用，避免“电工”与“ 电工 ”被识别为两个岗位
    return str(name or "").strip()


def shift_employees(shift_config, *, active_only: bool = True):
    # 人员与班次的归属关系（唯一口径）：Employee.shift_config 指向该职工的默认班次配置，
    # 而 ShiftConfig 属于唯一班组，因此 (team, shift_config) 共同确定“本班次可选人员”。
    # 可行性检查、求解器输入、生成快照必须全部使用本函数，禁止再按整个班组取人，
    # 否则会把同班组其他班次的人员混入当前班次（例如运输班早班 5 人 / 中班 8 人）。
    from apps.personnel.models import Employee

    qs = Employee.objects.filter(
        team_id=shift_config.team_id, shift_config=shift_config
    )
    if active_only:
        qs = qs.filter(is_active=True)
    return qs.order_by("employee_no")


def create_employee(*, employee_no, name, team, shift_config, min_required_shifts, **extra):
    # 创建职工并执行模型校验（班组与班次配置一致性等）
    employee = Employee(
        employee_no=employee_no,
        name=name,
        team=team,
        shift_config=shift_config,
        min_required_shifts=min_required_shifts,
        **extra,
    )
    employee.full_clean()
    employee.save()
    return employee


def update_employee(employee, **fields):
    # 修改职工信息并执行完整校验（班组与班次配置一致性、编号唯一等）
    for field, value in fields.items():
        setattr(employee, field, value)
    employee.full_clean()
    employee.save()
    return employee


def deactivate_employee(employee):
    # 停用职工：不直接删除存在历史排班记录的人员
    employee.is_active = False
    employee.save(update_fields=["is_active"])
    return employee


def import_employees(rows, *, allowed_org=None):
    # 整批写入导入职工（rows 为解析后的 8 列 dict，调用前需已通过行级校验）。
    # 全部写入在单个事务中完成：任一失败整体回滚，不留下部分数据。
    # 职工编号唯一由数据库唯一约束兜底：并发导入时后到者触发 IntegrityError，
    # 统一转为 ImportConflictError 并整批回滚（不依赖导入前的应用层查询）。
    with transaction.atomic():
        created = 0
        try:
            for data in rows:
                org_code = str(data["队组编号"] or "").strip()
                team_code = str(data["班组编号"] or "").strip()
                shift_name = data["班次"]
                org = OrganizationUnit.objects.filter(code=org_code).first()
                team = (
                    Team.objects.filter(code=team_code, organization=org).first()
                    if org else None
                )
                shift_config = (
                    team.shift_configs.filter(shift_name=shift_name).first()
                    if team else None
                )
                if org is None or team is None or shift_config is None:
                    raise ImportConflictError(
                        f"导入目标不存在或已失效（{org_code}/{team_code}/{shift_name}），整批未写入。"
                    )
                if allowed_org is not None and org.pk != allowed_org.pk:
                    raise PermissionDenied(f"无权导入队组 {org_code} 的职工数据。")
                create_employee(
                    employee_no=str(data["职工编号"] or "").strip(),
                    name=str(data["姓名"] or "").strip(),
                    team=team,
                    shift_config=shift_config,
                    min_required_shifts=int(data["矿上应上最少班数"]),
                    position=str(data["岗位"] or "").strip(),
                    worked_shifts=int(data["已上班数"]),
                )
                created += 1
        except IntegrityError as exc:
            raise ImportConflictError(
                "职工编号与已有数据或并发导入冲突，整批未写入。"
            ) from exc
    return created
