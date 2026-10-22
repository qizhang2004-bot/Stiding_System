# 人员表单：职工。
from django import forms

from apps.organization.models import ShiftConfig, Team

from .models import Employee


class EmployeeForm(forms.ModelForm):
    class Meta:
        model = Employee
        fields = [
            "employee_no",
            "name",
            "team",
            "shift_config",
            "worked_shifts",
            "min_required_shifts",
            "position",
            "is_active",
            "remark",
        ]
        widgets = {
            "employee_no": forms.TextInput(
                attrs={"class": "input", "maxlength": 50, "placeholder": "如：E001"}
            ),
            "name": forms.TextInput(attrs={"class": "input", "maxlength": 50}),
            "team": forms.Select(attrs={"class": "input", "data-cascade-cfg": "#id_shift_config"}),
            "shift_config": forms.Select(attrs={"class": "input"}),
            "worked_shifts": forms.NumberInput(attrs={"class": "input", "min": 0, "max": 32767}),
            "min_required_shifts": forms.NumberInput(
                attrs={"class": "input", "min": 0, "max": 32767}
            ),
            "position": forms.TextInput(
                attrs={"class": "input", "maxlength": 100, "placeholder": "如：值班员"}
            ),
            "remark": forms.Textarea(attrs={"class": "input", "rows": 3}),
        }
        help_texts = {
            "worked_shifts": "当前排班周期（每月25日至次月24日）已完成的出勤班数，不跨周期累计。",
            "min_required_shifts": "矿上规定的最低出勤班数，仅用于展示、统计和预警，不参与排班算法。",
        }

    def __init__(self, *args, user=None, **kwargs):
        # user 传入时，班组与班次配置下拉仅展示该用户可见队组的数据
        super().__init__(*args, **kwargs)
        if user is not None:
            from apps.accounts.permissions import visible_organizations

            visible = visible_organizations(user)
        else:
            visible = None
        team_qs = Team.objects.select_related("organization")
        if visible is not None:
            team_qs = team_qs.filter(organization__in=visible)
        self.fields["team"].queryset = team_qs.order_by("organization__name", "name")
        self.fields["team"].label_from_instance = (
            lambda obj: f"{obj.organization.name} / {obj.name}" + ("" if obj.is_active else "（已停用）")
        )
        team = self.instance.team if self.instance.pk else None
        self._set_shift_config_queryset(team, visible)

    def _set_shift_config_queryset(self, team, visible=None):
        qs = ShiftConfig.objects.select_related("team").order_by("team__name", "shift_name")
        if self.is_bound:
            # POST：选项放开为可见范围配置，由 clean() 给出“必须属于所选班组”的友好错误
            if visible is not None:
                qs = qs.filter(team__organization__in=visible)
        elif team:
            # 编辑：仅当前班组的配置（与后端一致性校验一致）
            qs = qs.filter(team=team)
        elif visible is not None:
            qs = qs.filter(team__organization__in=visible)
        self.fields["shift_config"].queryset = qs
        if self.is_bound or team is None:
            self.fields["shift_config"].label_from_instance = (
                lambda obj: f"{obj.team.name} · {obj.get_shift_name_display()}"
                + ("" if obj.is_active else "（待配置）")
            )
        else:
            self.fields["shift_config"].label_from_instance = (
                lambda obj: f"{obj.get_shift_name_display()}" + ("" if obj.is_active else "（待配置）")
            )

    def clean(self):
        cleaned = super().clean()
        team = cleaned.get("team")
        shift_config = cleaned.get("shift_config")
        if team and shift_config and shift_config.team_id != team.pk:
            self.add_error("shift_config", "班次配置必须属于所选班组，请重新选择。")
        return cleaned


class EmployeeImportFileForm(forms.Form):
    file = forms.FileField(
        label="选择文件",
        help_text="支持 .csv（UTF-8）或 .xlsx，单次最多校验前 200 行数据。",
        widget=forms.ClearableFileInput(
            attrs={"class": "input", "accept": ".csv,.xlsx", "data-file-label": "import-file-info"}
        ),
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        name = (upload.name or "").lower()
        if not (name.endswith(".csv") or name.endswith(".xlsx")):
            raise forms.ValidationError("仅支持 .csv 或 .xlsx 文件。")
        if upload.size > 5 * 1024 * 1024:
            raise forms.ValidationError("文件大小不能超过 5 MB。")
        return upload
