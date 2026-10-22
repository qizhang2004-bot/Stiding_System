# 账号管理表单。
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError

from apps.organization.models import OrganizationUnit

from .models import UserProfile

User = get_user_model()
username_validator = UnicodeUsernameValidator()


class _BaseAccountForm(forms.Form):
    def _style_fields(self, names):
        for name in names:
            widget = self.fields[name].widget
            if widget.input_type in ("text", "password", "select", "number"):
                widget.attrs.setdefault("class", "input")

    def _check_password_pair(self, cleaned, password_field, confirm_field):
        password = cleaned.get(password_field)
        password2 = cleaned.get(confirm_field)
        if password or password2:
            if password != password2:
                self.add_error(confirm_field, "两次输入的密码不一致。")
            if password:
                try:
                    validate_password(password, User(username=cleaned.get("username", "")))
                except ValidationError as exc:
                    for msg in exc.messages:
                        self.add_error(password_field, msg)


class AccountCreateForm(_BaseAccountForm):
    # 超级管理员创建账号（可指定角色与所属队组）
    username = forms.CharField(label="登录账号", max_length=150, validators=[username_validator])
    password = forms.CharField(
        label="初始密码", required=False, widget=forms.PasswordInput,
        help_text="留空则系统生成随机密码。",
    )
    password2 = forms.CharField(label="确认密码", required=False, widget=forms.PasswordInput)
    role = forms.ChoiceField(label="账号角色", choices=UserProfile.Role.choices)
    organization = forms.ModelChoiceField(
        label="所属队组",
        queryset=OrganizationUnit.objects.order_by("name"),
        required=False,
        help_text="队组管理员与普通成员必选；超级管理员无需选择。",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields(["username", "password", "password2", "role", "organization"])
        self.fields["organization"].label_from_instance = (
            lambda obj: f"{obj.name}（{obj.code}）" + ("" if obj.is_active else " · 已停用")
        )

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("该登录账号已存在。")
        return username

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("role")
        organization = cleaned.get("organization")
        if role and role != UserProfile.Role.SUPER and not organization:
            self.add_error("organization", "队组管理员与普通成员必须选择所属队组。")
        self._check_password_pair(cleaned, "password", "password2")
        return cleaned


class AccountEditForm(_BaseAccountForm):
    # 超级管理员修改账号角色与所属队组
    role = forms.ChoiceField(label="账号角色", choices=UserProfile.Role.choices)
    organization = forms.ModelChoiceField(
        label="所属队组",
        queryset=OrganizationUnit.objects.order_by("name"),
        required=False,
        help_text="队组管理员与普通成员必选；超级管理员无需选择。",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields(["role", "organization"])
        self.fields["organization"].label_from_instance = (
            lambda obj: f"{obj.name}（{obj.code}）" + ("" if obj.is_active else " · 已停用")
        )

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("role")
        organization = cleaned.get("organization")
        if role and role != UserProfile.Role.SUPER and not organization:
            self.add_error("organization", "队组管理员与普通成员必须选择所属队组。")
        return cleaned


class PasswordResetForm(_BaseAccountForm):
    new_password = forms.CharField(label="新密码", widget=forms.PasswordInput)
    new_password2 = forms.CharField(label="确认新密码", widget=forms.PasswordInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields(["new_password", "new_password2"])

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("new_password")
        if password:
            try:
                validate_password(password, User(username="placeholder"))
            except ValidationError as exc:
                for msg in exc.messages:
                    self.add_error("new_password", msg)
        if cleaned.get("new_password") != cleaned.get("new_password2"):
            self.add_error("new_password2", "两次输入的密码不一致。")
        return cleaned


class MemberAccountForm(_BaseAccountForm):
    # 队组管理员创建本队组普通成员账号（角色与队组由服务端固定，不提供选择）
    username = forms.CharField(label="登录账号", max_length=150, validators=[username_validator])
    password = forms.CharField(
        label="初始密码", required=False, widget=forms.PasswordInput,
        help_text="留空则系统生成随机密码。",
    )
    password2 = forms.CharField(label="确认密码", required=False, widget=forms.PasswordInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields(["username", "password", "password2"])

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("该登录账号已存在。")
        return username

    def clean(self):
        cleaned = super().clean()
        self._check_password_pair(cleaned, "password", "password2")
        return cleaned
