# 认证与账号管理视图：登录/登出/修改密码/角色首页/超级管理员账号管理/队组成员管理。
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeDoneView, PasswordChangeView
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST

from apps.auditlog.services import log_operation
from apps.organization.models import OrganizationUnit

from . import services
from .forms import AccountCreateForm, AccountEditForm, MemberAccountForm, PasswordResetForm
from .models import UserProfile
from .permissions import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_SUPER,
    get_organization,
    get_profile,
    get_role,
    role_required,
)

User = get_user_model()


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


# ============================ 认证 ============================

class AppLoginView(LoginView):
    # 统一登录入口：登录成功/失败均记录审计日志（不记录密码）
    template_name = "registration/login.html"

    def form_valid(self, form):
        log_operation(
            user=form.get_user(),
            action="login",
            module="auth",
            description=f"账号 {form.get_user().username} 登录成功",
            ip_address=_client_ip(self.request),
        )
        return super().form_valid(form)

    def form_invalid(self, form):
        username = self.request.POST.get("username", "")
        log_operation(
            user=None,
            action="login_failed",
            module="auth",
            description=f"账号登录失败：{username}",
            ip_address=_client_ip(self.request),
        )
        return super().form_invalid(form)


class AppLogoutView(LogoutView):
    next_page = "accounts:login"

    def post(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            log_operation(
                user=request.user,
                action="logout",
                module="auth",
                description=f"账号 {request.user.username} 登出",
                ip_address=_client_ip(request),
            )
        return super().post(request, *args, **kwargs)


class AppPasswordChangeView(PasswordChangeView):
    template_name = "registration/password_change_form.html"
    success_url = reverse_lazy("accounts:password_change_done")

    def form_valid(self, form):
        log_operation(
            user=self.request.user,
            action="change_password",
            module="auth",
            description="修改登录密码",
            ip_address=_client_ip(self.request),
        )
        return super().form_valid(form)


app_login = AppLoginView.as_view()
app_logout = AppLogoutView.as_view()
app_password_change = AppPasswordChangeView.as_view()
app_password_change_done = PasswordChangeDoneView.as_view(
    template_name="registration/password_change_done.html"
)


@login_required
def home(request):
    # 登录后的角色首页：超管/队组管理员 → 工作台；普通成员 → 班次看板
    role = get_role(request.user)
    if role == ROLE_MEMBER:
        return redirect("scheduler:shift_board")
    return redirect("core:dashboard")


# ============================ 超级管理员：账号管理 ============================

@role_required(ROLE_SUPER)
def user_list(request):
    q = request.GET.get("q", "").strip()
    role = request.GET.get("role", "")
    org_id = request.GET.get("organization", "")
    status = request.GET.get("status", "")
    profiles = UserProfile.objects.select_related("user", "organization").order_by("user__username")
    if q:
        profiles = profiles.filter(user__username__icontains=q)
    if role:
        profiles = profiles.filter(role=role)
    if org_id:
        profiles = profiles.filter(organization_id=org_id)
    if status == "active":
        profiles = profiles.filter(user__is_active=True)
    elif status == "inactive":
        profiles = profiles.filter(user__is_active=False)
    paginator = Paginator(profiles, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "accounts/user_list.html",
        {
            "page_obj": page_obj,
            "q": q,
            "role": role,
            "org_id": org_id,
            "status": status,
            "orgs": OrganizationUnit.objects.order_by("name"),
            "roles": UserProfile.Role.choices,
        },
    )


@role_required(ROLE_SUPER)
def user_create(request):
    form = AccountCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cleaned = form.cleaned_data
        try:
            user, password = services.create_user_account(
                username=cleaned["username"],
                password=cleaned.get("password") or None,
                role=cleaned["role"],
                organization=cleaned.get("organization"),
                actor=request.user,
            )
        except ValidationError as exc:
            for field, errs in exc.message_dict.items():
                for err in errs:
                    messages.error(request, f"{field}：{err}")
        else:
            if not cleaned.get("password"):
                messages.success(
                    request,
                    f"账号「{user.username}」已创建，系统生成的初始密码为：{password}（仅显示这一次，请妥善保存）",
                )
            else:
                messages.success(request, f"账号「{user.username}」已创建。")
            return redirect("accounts:user_detail", pk=get_profile(user).pk)
    return render(request, "accounts/user_form.html", {"form": form, "editing": False})


@role_required(ROLE_SUPER)
def user_detail(request, pk):
    profile = get_object_or_404(
        UserProfile.objects.select_related("user", "organization"), pk=pk
    )
    return render(request, "accounts/user_detail.html", {"profile": profile})


@role_required(ROLE_SUPER)
def user_edit(request, pk):
    profile = get_object_or_404(UserProfile.objects.select_related("user"), pk=pk)
    form = AccountEditForm(
        request.POST or None,
        initial={"role": profile.role, "organization": profile.organization},
    )
    if request.method == "POST" and form.is_valid():
        cleaned = form.cleaned_data
        try:
            if cleaned["role"] != profile.role:
                services.set_role(profile, cleaned["role"], actor=request.user)
            if profile.role != ROLE_SUPER and cleaned.get("organization") and cleaned["organization"].pk != profile.organization_id:
                services.set_organization(profile, cleaned["organization"], actor=request.user)
            messages.success(request, f"账号「{profile.user.username}」已更新。")
            return redirect("accounts:user_detail", pk=profile.pk)
        except ValidationError as exc:
            messages.error(request, "；".join(exc.messages))
    return render(
        request,
        "accounts/user_edit.html",
        {"form": form, "profile": profile},
    )


@role_required(ROLE_SUPER)
@require_POST
def user_toggle(request, pk):
    profile = get_object_or_404(UserProfile.objects.select_related("user"), pk=pk)
    try:
        services.set_account_active(profile.user, not profile.user.is_active, actor=request.user)
        state = "停用" if not profile.user.is_active else "恢复"
        messages.success(request, f"账号「{profile.user.username}」已{state}。")
    except ValidationError as exc:
        messages.error(request, "；".join(exc.messages))
    next_url = request.POST.get("next")
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect("accounts:user_list")


@role_required(ROLE_SUPER)
def user_reset_password(request, pk):
    profile = get_object_or_404(UserProfile.objects.select_related("user"), pk=pk)
    form = PasswordResetForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.reset_password(profile.user, form.cleaned_data["new_password"], actor=request.user)
            messages.success(request, f"账号「{profile.user.username}」的密码已重置。")
            return redirect("accounts:user_detail", pk=profile.pk)
        except ValidationError as exc:
            messages.error(request, "；".join(exc.messages))
    return render(request, "accounts/password_reset.html", {"form": form, "profile": profile})


# ============================ 队组管理员：本队组成员管理 ============================

@role_required(ROLE_ADMIN)
def member_list(request):
    # 超级管理员无“本队组”概念，统一转去全局账号管理
    if get_role(request.user) == ROLE_SUPER:
        return redirect("accounts:user_list")
    org = get_organization(request.user)
    if org is None:
        raise PermissionDenied("账号未绑定队组。")
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    members = UserProfile.objects.filter(
        role=ROLE_MEMBER, organization=org
    ).select_related("user").order_by("user__username")
    if q:
        members = members.filter(user__username__icontains=q)
    if status == "active":
        members = members.filter(user__is_active=True)
    elif status == "inactive":
        members = members.filter(user__is_active=False)
    paginator = Paginator(members, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "accounts/member_list.html",
        {"page_obj": page_obj, "q": q, "status": status, "org": org},
    )


@role_required(ROLE_ADMIN)
def member_create(request):
    if get_role(request.user) == ROLE_SUPER:
        return redirect("accounts:user_create")
    org = get_organization(request.user)
    if org is None:
        raise PermissionDenied("账号未绑定队组。")
    form = MemberAccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cleaned = form.cleaned_data
        try:
            user, password = services.create_user_account(
                username=cleaned["username"],
                password=cleaned.get("password") or None,
                role=ROLE_MEMBER,
                organization=org,
                actor=request.user,
            )
        except ValidationError as exc:
            for errs in exc.message_dict.values():
                for err in errs:
                    messages.error(request, err)
        else:
            if not cleaned.get("password"):
                messages.success(
                    request,
                    f"普通成员账号「{user.username}」已创建，系统生成的初始密码为：{password}（仅显示这一次，请妥善保存）",
                )
            else:
                messages.success(request, f"普通成员账号「{user.username}」已创建。")
            return redirect("accounts:member_list")
    return render(request, "accounts/member_form.html", {"form": form, "org": org})


@role_required(ROLE_ADMIN)
@require_POST
def member_toggle(request, pk):
    org = get_organization(request.user)
    profile = get_object_or_404(UserProfile, pk=pk, role=ROLE_MEMBER, organization=org)
    try:
        services.set_account_active(profile.user, not profile.user.is_active, actor=request.user)
        state = "停用" if not profile.user.is_active else "恢复"
        messages.success(request, f"成员账号「{profile.user.username}」已{state}。")
    except ValidationError as exc:
        messages.error(request, "；".join(exc.messages))
    return redirect("accounts:member_list")


@role_required(ROLE_ADMIN)
def member_reset_password(request, pk):
    org = get_organization(request.user)
    profile = get_object_or_404(
        UserProfile.objects.select_related("user"), pk=pk, role=ROLE_MEMBER, organization=org
    )
    form = PasswordResetForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.reset_password(profile.user, form.cleaned_data["new_password"], actor=request.user)
            messages.success(request, f"成员账号「{profile.user.username}」的密码已重置。")
            return redirect("accounts:member_list")
        except ValidationError as exc:
            messages.error(request, "；".join(exc.messages))
    return render(request, "accounts/password_reset.html", {"form": form, "profile": profile})
