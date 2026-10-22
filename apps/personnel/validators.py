# 人员导入行数据校验（人员导入功能的校验核心，供导入预览复用）。
# 确定的人员导入格式见 docs/PERSONNEL_IMPORT_FORMAT.md（8 列，按固定顺序解析）。
from apps.organization.models import OrganizationUnit, Team
from apps.personnel.models import Employee

VALID_SHIFT_NAMES = {"early", "middle", "night"}


def validate_import_row(
    *,
    organization_code,
    team_code,
    employee_no,
    name,
    position,
    shift_name,
    worked_shifts,
    min_required_shifts,
    allowed_org=None,
):
    # 校验一行人员导入数据，返回错误列表（空列表表示通过）。
    #
    # 校验顺序（与导入格式的字段顺序一致）：
    #   1. 队组编号 → 查询队组（不存在则明确提示）
    #   2. 班组编号 → 查询班组（不存在则明确提示）
    #   3. 班组必须属于第一列指定的队组（否则提示“队组与班组不匹配”）
    #   4. 职工编号：非空、长度、与现有职工编号不重复
    #   5. 姓名、岗位、班次等字段
    #   6. 班次仅允许 early / middle / night
    #   7. 已上班数与矿上应上最少班数遵循现有字段规则（非负整数，0~32767）
    #   8. 复用既有规则：班组状态、数据范围（allowed_org）、班次配置存在性
    #
    # 业务约定：
    #   - worked_shifts（已上班数）属于当前排班周期（每月25日至次月24日）；
    #   - min_required_shifts（矿上应上最少班数）仅用于出勤达标统计；
    #   - 排班目标班数读取班次配置 shift_config.required_shifts，与导入数据无关。
    errors = []

    # 1. 队组编号
    org = None
    org_code = str(organization_code or "").strip()
    if not org_code:
        errors.append("队组编号不能为空。")
    else:
        org = OrganizationUnit.objects.filter(code=org_code).first()
        if org is None:
            errors.append(f"队组编号不存在：{org_code}。")

    # 2/3. 班组编号与队组归属
    team = None
    team_code_text = str(team_code or "").strip()
    if not team_code_text:
        errors.append("班组编号不能为空。")
    elif org is not None:
        candidates = Team.objects.filter(code=team_code_text)
        if not candidates.exists():
            errors.append(f"班组编号不存在：{team_code_text}。")
        else:
            team = candidates.filter(organization=org).first()
            if team is None:
                owner = candidates.first().organization
                errors.append(
                    f"队组与班组不匹配：班组「{team_code_text}」属于「{owner.name}」，"
                    f"不属于「{org.name}」。"
                )

    # 4. 职工编号
    employee_no_text = str(employee_no or "").strip()
    if not employee_no_text:
        errors.append("职工编号不能为空。")
    elif len(employee_no_text) > 50:
        errors.append(f"职工编号长度不能超过 50：{employee_no_text}。")
    elif Employee.objects.filter(employee_no=employee_no_text).exists():
        errors.append(f"职工编号已存在：{employee_no_text}。")

    # 5. 姓名 / 岗位
    name_text = str(name or "").strip()
    if not name_text:
        errors.append("姓名不能为空。")
    elif len(name_text) > 50:
        errors.append(f"姓名长度不能超过 50：{name_text}。")
    if len(str(position or "").strip()) > 100:
        errors.append("岗位长度不能超过 100。")

    # 6. 班次
    if shift_name not in VALID_SHIFT_NAMES:
        errors.append(f"班次不合法（应为 early/middle/night）：{shift_name}。")

    # 7. 已上班数 / 矿上应上最少班数
    for label, value in (
        ("已上班数", worked_shifts),
        ("矿上应上最少班数", min_required_shifts),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{label}必须为整数：{value}。")
        elif value < 0:
            errors.append(f"{label}不能为负数：{value}。")
        elif value > 32767:
            errors.append(f"{label}超出范围（0-32767）：{value}。")

    # 8. 既有规则（依赖班组查询成功）
    if team is not None:
        if not team.is_active:
            errors.append(f"班组已停用：{team_code_text}。")
        if allowed_org is not None and team.organization_id != allowed_org.pk:
            errors.append(f"无权导入该队组的数据：{team_code_text}。")
        if shift_name in VALID_SHIFT_NAMES and not team.shift_configs.filter(
            shift_name=shift_name
        ).exists():
            errors.append(f"班组 {team_code_text} 缺少班次配置：{shift_name}。")
    return errors
