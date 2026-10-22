# 组织表单：队组、班组、班次配置、队组+账号一体化创建向导。
import json

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError

from apps.personnel.services import normalize_position

from .models import OrganizationUnit, ShiftConfig, Team

User = get_user_model()
username_validator = UnicodeUsernameValidator()


class OrganizationUnitForm(forms.ModelForm):
    class Meta:
        model = OrganizationUnit
        fields = ["name", "code", "is_active"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "input", "maxlength": 100, "placeholder": "如：采煤一队"}
            ),
            "code": forms.TextInput(
                attrs={"class": "input", "maxlength": 50, "placeholder": "如：ORG-01"}
            ),
        }


class TeamForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = ["organization", "name", "code", "is_active"]
        widgets = {
            "organization": forms.Select(attrs={"class": "input"}),
            "name": forms.TextInput(
                attrs={"class": "input", "maxlength": 100, "placeholder": "如：一班"}
            ),
            "code": forms.TextInput(
                attrs={"class": "input", "maxlength": 50, "placeholder": "如：T01-01"}
            ),
        }

    def __init__(self, *args, user=None, **kwargs):
        # user 传入时，队组下拉仅展示该用户可见的队组（数据范围控制）
        super().__init__(*args, **kwargs)
        if user is not None:
            from apps.accounts.permissions import visible_organizations

            orgs = visible_organizations(user)
        else:
            orgs = OrganizationUnit.objects.all()
        self.fields["organization"].queryset = orgs.order_by("name")
        self.fields["organization"].label_from_instance = (
            lambda obj: f"{obj.name}（{obj.code}）" + ("" if obj.is_active else " · 已停用")
        )


class ShiftConfigForm(forms.ModelForm):
    exempt_employees = forms.ModelMultipleChoiceField(
        queryset=None,
        required=False,
        label="豁免职工",
        help_text="通过「选择豁免职工」弹窗多选本班组职工；提交使用职工主键。",
        widget=forms.CheckboxSelectMultiple(attrs={"class": "input"}),
    )

    class Meta:
        model = ShiftConfig
        # 所属班组与班次名称仅在配置页展示，不进入表单字段集合：
        # 若保留在 fields 中，未提交时 ModelForm._post_clean() 会把实例的
        # team/shift_name 置为 None/''，导致保存时模型校验报“必填/不能为 null”。
        # 旧字段 special_position_count 保留于数据库，但不再出现在配置表单中
        fields = [
            "daily_worker_count",
            "min_consecutive_rest_days",
            "max_consecutive_rest_days",
            "required_shifts",
            "is_active",
        ]
        widgets = {
            "daily_worker_count": forms.NumberInput(
                attrs={"class": "input", "min": 1, "max": 32767}
            ),
            "min_consecutive_rest_days": forms.NumberInput(
                attrs={"class": "input", "min": 0, "max": 32767}
            ),
            "max_consecutive_rest_days": forms.NumberInput(
                attrs={"class": "input", "min": 1, "max": 32767}
            ),
            "required_shifts": forms.NumberInput(
                attrs={"class": "input", "min": 1, "max": 32767}
            ),
        }
        help_texts = {
            "daily_worker_count": "该班次每天需要安排的上班人数。",
            "min_consecutive_rest_days": "连续休息的最少天数。",
            "max_consecutive_rest_days": "连续休息的最多天数，不能小于最少天数。",
            "required_shifts": "排班算法使用的每人目标班数（本周期）。与职工表的“矿上最低班数”无关。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        team = getattr(self.instance, "team", None)
        if team:
            from apps.personnel.services import shift_employees

            # 豁免只对本班次人员生效：下拉只列该班次成员，避免跨班次误设。
            # 尚未保存的班次配置没有任何成员，豁免可选集合为空。
            self.fields["exempt_employees"].queryset = (
                shift_employees(self.instance, active_only=False)
                if getattr(self.instance, "pk", None)
                else self.fields["exempt_employees"].queryset.none()
            )
            self.fields["exempt_employees"].initial = self.instance.exempt_employee_ids or []


class PositionRequirementsForm(forms.Form):
    # 岗位要求人数：动态岗位配置以 JSON 文本提交，后端严格校验。
    position_requirements = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={"id": "id_position_requirements"}),
    )

    def clean_position_requirements(self):
        raw = self.cleaned_data.get("position_requirements") or ""
        if not str(raw).strip():
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            raise forms.ValidationError("岗位要求数据格式不合法。")
        if not isinstance(data, dict):
            raise forms.ValidationError("岗位要求必须为对象（岗位名称 → 人数）。")
        cleaned = {}
        errors = []
        for name, count in data.items():
            normalized = normalize_position(name)
            if not normalized or len(normalized) > 100:
                errors.append(f"岗位名称「{name}」不合法（须为 1~100 字符）。")
                continue
            if isinstance(count, bool) or not isinstance(count, int):
                errors.append(f"岗位「{normalized}」的人数必须为整数。")
                continue
            if count < 0:
                errors.append(f"岗位「{normalized}」的人数不能为负数。")
                continue
            cleaned[normalized] = count
        if errors:
            raise forms.ValidationError(errors)
        return cleaned


class _AccountFieldsMixin:
    # 队组创建向导中的两类初始账号字段（密码留空则系统生成随机密码）
    username_field = None
    password_field = None
    password2_field = None

    def _style(self, name):
        widget = self.fields[name].widget
        if widget.input_type in ("text", "password"):
            widget.attrs.setdefault("class", "input")

    def _check_password_pair(self, cleaned, password_field, confirm_field, username):
        password = cleaned.get(password_field)
        password2 = cleaned.get(confirm_field)
        if password or password2:
            if password != password2:
                self.add_error(confirm_field, "两次输入的密码不一致。")
            if password:
                try:
                    validate_password(password, User(username=username))
                except ValidationError as exc:
                    for msg in exc.messages:
                        self.add_error(password_field, msg)


class OrganizationWithAccountsForm(_AccountFieldsMixin, forms.Form):
    # 第一步：队组信息
    name = forms.CharField(
        label="队组名称", max_length=100,
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "如：综掘五队"}),
    )
    code = forms.CharField(
        label="队组编号", max_length=50,
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "如：ZJWD"}),
    )
    is_active = forms.BooleanField(label="启用状态", required=False, initial=True)

    # 第二步：队组管理员账号
    admin_username = forms.CharField(
        label="管理员登录账号", max_length=150, validators=[username_validator],
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    admin_password = forms.CharField(
        label="管理员初始密码", required=False, widget=forms.PasswordInput(attrs={"class": "input"}),
        help_text="留空则系统生成随机密码。",
    )
    admin_password2 = forms.CharField(
        label="确认管理员密码", required=False, widget=forms.PasswordInput(attrs={"class": "input"})
    )

    # 第三步：普通成员通用账号
    member_username = forms.CharField(
        label="普通成员登录账号", max_length=150, validators=[username_validator],
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    member_password = forms.CharField(
        label="普通成员初始密码", required=False, widget=forms.PasswordInput(attrs={"class": "input"}),
        help_text="留空则系统生成随机密码。",
    )
    member_password2 = forms.CharField(
        label="确认普通成员密码", required=False, widget=forms.PasswordInput(attrs={"class": "input"})
    )

    def clean_code(self):
        code = self.cleaned_data["code"]
        if OrganizationUnit.objects.filter(code=code).exists():
            raise forms.ValidationError("该队组编号已存在。")
        return code

    def clean(self):
        cleaned = super().clean()
        admin_username = cleaned.get("admin_username")
        member_username = cleaned.get("member_username")
        for field, username in (("admin_username", admin_username), ("member_username", member_username)):
            if username and User.objects.filter(username=username).exists():
                self.add_error(field, "该登录账号已存在。")
        if admin_username and member_username and admin_username == member_username:
            self.add_error("member_username", "普通成员账号不能与管理员账号相同。")
        self._check_password_pair(cleaned, "admin_password", "admin_password2", admin_username or "")
        self._check_password_pair(cleaned, "member_password", "member_password2", member_username or "")
        return cleaned
