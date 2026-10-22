# 统一权限检查与队组数据范围控制。
# 所有业务视图的角色校验与数据隔离都通过本模块实现，避免在各视图散落互不一致的判断。
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee

from .models import UserProfile

ROLE_SUPER = UserProfile.Role.SUPER
ROLE_ADMIN = UserProfile.Role.ADMIN
ROLE_MEMBER = UserProfile.Role.MEMBER
ROLE_RANK = {ROLE_MEMBER: 1, ROLE_ADMIN: 2, ROLE_SUPER: 3}


def get_profile(user):
    # 获取用户角色档案；始终读取数据库最新行（避免 user.profile 反向缓存过期）。
    # 缺失时按 is_superuser 推断并补建（幂等）。
    if user is None or not user.is_authenticated:
        return None
    profile = (
        UserProfile.objects.filter(user=user).select_related("organization").first()
    )
    if profile is None:
        profile = UserProfile.objects.create(
            user=user,
            role=ROLE_SUPER if user.is_superuser else ROLE_MEMBER,
        )
    return profile


def get_role(user):
    profile = get_profile(user)
    return profile.role if profile else None


def get_organization(user):
    profile = get_profile(user)
    return profile.organization if profile else None


def has_role(user, minimum):
    profile = get_profile(user)
    if profile is None:
        return False
    return ROLE_RANK.get(profile.role, 0) >= ROLE_RANK[minimum]


def require_organization(user):
    # 队组管理员/普通成员必须绑定有效队组；未绑定或队组已停用时明确拒绝，
    # 绝不默认使用其他队组。超级管理员返回 None（不受队组限制）。
    profile = get_profile(user)
    if profile is None:
        raise PermissionDenied("当前账号缺少权限档案，请联系超级管理员配置。")
    if profile.role == ROLE_SUPER:
        return None
    org = profile.organization
    if org is None or not org.is_active:
        raise PermissionDenied(
            "您的账号未绑定有效队组，请联系超级管理员配置所属队组后再操作。"
        )
    return org


def role_required(minimum):
    # 统一角色装饰器：未登录跳登录页；权限不足返回 403。
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def wrapper(request, *args, **kwargs):
            if not has_role(request.user, minimum):
                raise PermissionDenied("您没有访问该页面的权限。")
            if minimum in (ROLE_ADMIN, ROLE_SUPER):
                # 队组管理员必须绑定有效队组（服务端强制，不依赖前端隐藏选项）
                require_organization(request.user)
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


def visible_organizations(user):
    # 当前用户可见的队组范围：超级管理员全部；管理员/普通成员仅本队组。
    profile = get_profile(user)
    if profile is None:
        return OrganizationUnit.objects.none()
    if profile.role == ROLE_SUPER:
        return OrganizationUnit.objects.all()
    if profile.organization_id:
        return OrganizationUnit.objects.filter(pk=profile.organization_id)
    return OrganizationUnit.objects.none()


# ---- 对象级访问控制：跨队组按 ID 访问一律 404（不泄露存在性） ----

def get_org_or_404(user, pk):
    return get_object_or_404(visible_organizations(user), pk=pk)


def get_team_or_404(user, pk):
    return get_object_or_404(
        Team.objects.filter(organization__in=visible_organizations(user)), pk=pk
    )


def get_shift_config_or_404(user, pk):
    return get_object_or_404(
        ShiftConfig.objects.filter(team__organization__in=visible_organizations(user)), pk=pk
    )


def get_employee_or_404(user, pk):
    return get_object_or_404(
        Employee.objects.filter(team__organization__in=visible_organizations(user)), pk=pk
    )
