# 组织业务逻辑。
from .models import OrganizationUnit, ShiftConfig, Team

# 自动初始化配置的占位值：仅用于占位。
# 这些配置创建时 is_active=False（待配置状态），在完成实际参数设置并启用前，
# 不会被正式排班使用（get_active_shift_configs 只返回已启用的配置）。
PLACEHOLDER_SHIFT_CONFIGS = [
    {
        "shift_name": ShiftConfig.ShiftName.EARLY,
        "daily_worker_count": 1,
        "min_consecutive_rest_days": 2,
        "max_consecutive_rest_days": 4,
        "required_shifts": 1,
        "special_position_count": 0,
    },
    {
        "shift_name": ShiftConfig.ShiftName.MIDDLE,
        "daily_worker_count": 1,
        "min_consecutive_rest_days": 2,
        "max_consecutive_rest_days": 4,
        "required_shifts": 1,
        "special_position_count": 0,
    },
    {
        "shift_name": ShiftConfig.ShiftName.NIGHT,
        "daily_worker_count": 1,
        "min_consecutive_rest_days": 2,
        "max_consecutive_rest_days": 4,
        "required_shifts": 1,
        "special_position_count": 0,
    },
]


def create_organization_unit(*, name, code, is_active=True):
    return OrganizationUnit.objects.create(name=name, code=code, is_active=is_active)


def deactivate_organization_unit(organization):
    # 停用队组：不直接删除存在关联数据的队组
    organization.is_active = False
    organization.save(update_fields=["is_active"])
    return organization


def create_team(*, organization, name, code, is_active=True):
    # 创建班组；早/中/晚三条班次配置由 signals 自动初始化（幂等）。
    return Team.objects.create(organization=organization, name=name, code=code, is_active=is_active)


def deactivate_team(team):
    # 停用班组：不直接删除存在关联数据的班组
    team.is_active = False
    team.save(update_fields=["is_active"])
    return team


def ensure_default_shift_configs(team):
    # 幂等补齐早/中/晚三条班次配置；重复执行不会产生重复记录。
    # 自动创建的配置为 is_active=False（待配置），需设置真实参数并启用后方可用于排班。
    created = 0
    configs = []
    for spec in PLACEHOLDER_SHIFT_CONFIGS:
        config, is_new = ShiftConfig.objects.get_or_create(
            team=team,
            shift_name=spec["shift_name"],
            defaults={**spec, "is_active": False},
        )
        created += int(is_new)
        configs.append(config)
    return created, configs


def get_active_shift_configs(team):
    # 返回已启用（完成参数设置）的班次配置；正式排班只能使用这里的结果
    return team.shift_configs.filter(is_active=True)


def update_shift_config(shift_config, **fields):
    # 修改班次配置并执行完整校验（数值范围、连休范围、豁免人员合法性）
    for field, value in fields.items():
        setattr(shift_config, field, value)
    shift_config.full_clean()
    shift_config.save()
    return shift_config


def set_shift_config_active(shift_config, active):
    # 启用/停用班次配置；启用前执行完整校验，非法参数无法被启用
    shift_config.is_active = active
    if active:
        shift_config.full_clean()
    shift_config.save(update_fields=["is_active"])
    return shift_config
