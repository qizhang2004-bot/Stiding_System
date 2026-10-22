# 账号管理服务：账号创建、角色/队组变更、启停、密码重置、队组+账号一体化创建。
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.crypto import get_random_string

from apps.auditlog.services import log_operation

from .models import UserProfile
from .permissions import ROLE_ADMIN, ROLE_MEMBER, ROLE_SUPER

User = get_user_model()


def generate_password():
    # 12 位随机密码（避开易混淆字符），满足 Django 默认密码校验
    return get_random_string(
        length=12,
        allowed_chars="abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789",
    )


def _sync_superuser_flags(user, role):
    is_super = role == ROLE_SUPER
    if user.is_superuser != is_super or user.is_staff != is_super:
        user.is_superuser = is_super
        user.is_staff = is_super
        user.save(update_fields=["is_superuser", "is_staff"])


def create_user_account(*, username, password=None, role, organization=None, actor=None):
    # 创建账号与角色档案；返回 (user, password)。password 为空时系统生成随机密码。
    from apps.organization.models import OrganizationUnit

    if role not in (ROLE_SUPER, ROLE_ADMIN, ROLE_MEMBER):
        raise ValidationError({"role": "无效的角色。"})
    if User.objects.filter(username=username).exists():
        raise ValidationError({"username": "该登录账号已存在。"})
    if role == ROLE_SUPER:
        organization = None
    elif organization is None:
        raise ValidationError({"organization": "队组管理员与普通成员必须绑定所属队组。"})
    elif not isinstance(organization, OrganizationUnit):
        organization = OrganizationUnit.objects.filter(pk=organization).first()
        if organization is None:
            raise ValidationError({"organization": "所选队组不存在。"})

    password = password or generate_password()
    user = User(username=username, is_superuser=(role == ROLE_SUPER), is_staff=(role == ROLE_SUPER))
    try:
        validate_password(password, user)
    except ValidationError as exc:
        raise ValidationError({"password": exc.messages})
    user.set_password(password)  # 数据库中只保存 Django 安全哈希
    user.save()
    # User 信号可能已创建默认档案，这里统一覆盖为指定角色与队组（幂等）
    UserProfile.objects.update_or_create(
        user=user,
        defaults={"role": role, "organization": organization},
    )
    log_operation(
        user=actor,
        action="create",
        module="account",
        target_type="user",
        target_id=user.pk,
        description=f"创建账号 {username}（{UserProfile.Role(role).label}）",
    )
    return user, password


def is_last_active_super(user):
    return (
        user.is_superuser
        and user.is_active
        and not User.objects.filter(is_superuser=True, is_active=True)
        .exclude(pk=user.pk)
        .exists()
    )


def set_role(profile, new_role, actor=None):
    # 修改角色（仅超级管理员调用）；保护最后一个有效超级管理员。
    if new_role not in (ROLE_SUPER, ROLE_ADMIN, ROLE_MEMBER):
        raise ValidationError({"role": "无效的角色。"})
    if profile.role == ROLE_SUPER and new_role != ROLE_SUPER and profile.user.is_active:
        if is_last_active_super(profile.user):
            raise ValidationError({"role": "不能移除系统中最后一个有效的超级管理员。"})
    before = {"role": profile.role, "organization_id": profile.organization_id}
    profile.role = new_role
    if new_role == ROLE_SUPER:
        profile.organization = None
    profile.full_clean()
    profile.save()
    _sync_superuser_flags(profile.user, new_role)
    log_operation(
        user=actor,
        action="change_role",
        module="account",
        target_type="user",
        target_id=profile.user_id,
        description=f"修改账号 {profile.user.username} 角色：{before['role']} → {new_role}",
        before_data=before,
        after_data={"role": new_role},
    )
    return profile


def set_organization(profile, organization, actor=None):
    # 修改所属队组（仅超级管理员调用）。
    if profile.role == ROLE_SUPER:
        raise ValidationError({"organization": "超级管理员不绑定队组。"})
    before = {"organization_id": profile.organization_id}
    profile.organization = organization
    profile.full_clean()
    profile.save()
    log_operation(
        user=actor,
        action="change_org",
        module="account",
        target_type="user",
        target_id=profile.user_id,
        description=f"修改账号 {profile.user.username} 所属队组",
        before_data=before,
        after_data={"organization_id": organization.pk},
    )
    return profile


def set_account_active(user, active, actor=None):
    # 停用/恢复账号；保护最后一个有效超级管理员。
    if not active and user.is_active and is_last_active_super(user):
        raise ValidationError("不能停用系统中最后一个有效的超级管理员。")
    before = user.is_active
    user.is_active = active
    user.save(update_fields=["is_active"])
    log_operation(
        user=actor,
        action="deactivate" if not active else "activate",
        module="account",
        target_type="user",
        target_id=user.pk,
        description=f"{'停用' if not active else '恢复'}账号 {user.username}",
        before_data={"is_active": before},
        after_data={"is_active": active},
    )
    return user


def reset_password(user, new_password, actor=None):
    # 重置密码：校验强度、只保存哈希、日志不记录明文。
    try:
        validate_password(new_password, user)
    except ValidationError as exc:
        raise ValidationError({"new_password": exc.messages})
    user.set_password(new_password)
    user.save(update_fields=["password"])
    log_operation(
        user=actor,
        action="reset_password",
        module="account",
        target_type="user",
        target_id=user.pk,
        description=f"重置账号 {user.username} 的密码",
    )
    return user


def create_organization_with_accounts(
    *,
    name,
    code,
    is_active,
    admin_username,
    admin_password=None,
    member_username,
    member_password=None,
    actor=None,
):
    # 创建队组 + 队组管理员 + 普通成员通用账号（同一事务：全部成功或全部回滚）。
    # 队组的班次配置仍沿用现有机制：在创建班组时自动初始化早/中/晚三条配置。
    from apps.organization.models import OrganizationUnit

    with transaction.atomic():
        organization = OrganizationUnit.objects.create(name=name, code=code, is_active=is_active)
        admin_user, admin_pw = create_user_account(
            username=admin_username,
            password=admin_password,
            role=ROLE_ADMIN,
            organization=organization,
            actor=actor,
        )
        member_user, member_pw = create_user_account(
            username=member_username,
            password=member_password,
            role=ROLE_MEMBER,
            organization=organization,
            actor=actor,
        )
    return {
        "organization": organization,
        "admin_user": admin_user,
        "admin_password": admin_pw,
        "member_user": member_user,
        "member_password": member_pw,
    }
