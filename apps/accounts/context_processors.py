# 模板上下文：当前用户角色与所属队组（用于按角色渲染导航）。
from .permissions import get_organization, get_role


def account(request):
    if not request.user.is_authenticated:
        return {"user_role": None, "user_org": None}
    return {"user_role": get_role(request.user), "user_org": get_organization(request.user)}
