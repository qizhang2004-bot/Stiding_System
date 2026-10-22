# 队组、班组、班次配置模型。
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from apps.core.models import TimeStampedModel


class OrganizationUnit(TimeStampedModel):
    # 队组信息表
    name = models.CharField("队组名称", max_length=100)
    code = models.CharField("队组编号", max_length=50, unique=True)
    is_active = models.BooleanField("是否启用", default=True)

    class Meta:
        db_table = "organization_unit"
        verbose_name = "队组"
        verbose_name_plural = "队组"
        ordering = ["id"]

    def __str__(self):
        return self.name


class Team(TimeStampedModel):
    # 班组信息表（隶属于某个队组）
    organization = models.ForeignKey(
        OrganizationUnit,
        verbose_name="所属队组",
        related_name="teams",
        on_delete=models.PROTECT,
    )
    name = models.CharField("班组名称", max_length=100)
    code = models.CharField("班组编号", max_length=50)
    is_active = models.BooleanField("是否启用", default=True)

    class Meta:
        db_table = "team"
        verbose_name = "班组"
        verbose_name_plural = "班组"
        ordering = ["id"]
        constraints = [
            # 同一队组内班组编号唯一
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_team_org_code"),
        ]

    def __str__(self):
        return f"{self.organization.name} / {self.name}"


class ShiftConfig(TimeStampedModel):
    # 班次配置表：每个班组独立的早/中/晚班排班配置
    class ShiftName(models.TextChoices):
        EARLY = "early", "早班"
        MIDDLE = "middle", "中班"
        NIGHT = "night", "晚班"

    team = models.ForeignKey(
        Team,
        verbose_name="所属班组",
        related_name="shift_configs",
        on_delete=models.PROTECT,
    )
    shift_name = models.CharField("班次名称", max_length=20, choices=ShiftName.choices)
    daily_worker_count = models.PositiveSmallIntegerField("每天上班人数")
    min_consecutive_rest_days = models.PositiveSmallIntegerField("最少连续休息天数", default=2)
    max_consecutive_rest_days = models.PositiveSmallIntegerField("最多连续休息天数")
    required_shifts = models.PositiveSmallIntegerField("每人目标班数")
    special_position_count = models.PositiveSmallIntegerField("特殊岗位人数", default=0)
    position_requirements = models.JSONField(
        "岗位要求人数",
        default=dict,
        blank=True,
        help_text="各岗位当天实际安排的人数（必须恰好等于配置值）：{岗位名称: 要求人数}",
    )
    exempt_employee_ids = models.JSONField(
        "豁免人员ID列表",
        default=list,
        blank=True,
        help_text="该班次的豁免人员职工ID列表，保存前校验人员必须属于本班组",
    )
    is_active = models.BooleanField("是否启用", default=True)

    class Meta:
        db_table = "shift_config"
        verbose_name = "班次配置"
        verbose_name_plural = "班次配置"
        ordering = ["team_id", "shift_name"]
        constraints = [
            # 同一班组不能重复创建相同班次
            models.UniqueConstraint(fields=["team", "shift_name"], name="uniq_shiftconfig_team_shift"),
            # 每天上班人数必须大于 0
            models.CheckConstraint(
                check=Q(daily_worker_count__gt=0),
                name="chk_shiftconfig_daily_worker_positive",
            ),
            # 每人目标班数必须大于 0
            models.CheckConstraint(
                check=Q(required_shifts__gt=0),
                name="chk_shiftconfig_required_shifts_positive",
            ),
            # 最多连续休息天数 >= 最少连续休息天数
            models.CheckConstraint(
                check=Q(max_consecutive_rest_days__gte=F("min_consecutive_rest_days")),
                name="chk_shiftconfig_rest_range",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.daily_worker_count is not None and self.daily_worker_count <= 0:
            errors["daily_worker_count"] = "每天上班人数必须大于 0。"
        if self.required_shifts is not None and self.required_shifts <= 0:
            errors["required_shifts"] = "每人目标班数必须大于 0。"
        if (
            self.min_consecutive_rest_days is not None
            and self.max_consecutive_rest_days is not None
            and self.max_consecutive_rest_days < self.min_consecutive_rest_days
        ):
            errors["max_consecutive_rest_days"] = "最多连续休息天数不能小于最少连续休息天数。"
        if errors:
            raise ValidationError(errors)
        self._validate_exempt_employee_ids()
        self._validate_position_requirements()

    def _validate_position_requirements(self):
        reqs = self.position_requirements or {}
        if not isinstance(reqs, dict):
            raise ValidationError({"position_requirements": "岗位要求人数必须为对象（岗位名称 → 人数）。"})
        errors = []
        for name, count in reqs.items():
            normalized = str(name).strip()
            if not isinstance(name, str) or not normalized:
                errors.append("岗位名称必须为非空字符串。")
                continue
            if isinstance(count, bool) or not isinstance(count, int):
                errors.append(f"岗位「{normalized}」的人数必须为整数。")
            elif count < 0:
                errors.append(f"岗位「{normalized}」的人数不能为负数。")
        if errors:
            raise ValidationError({"position_requirements": errors})

    def _validate_exempt_employee_ids(self):
        ids = self.exempt_employee_ids or []
        if not isinstance(ids, list):
            raise ValidationError({"exempt_employee_ids": "豁免人员 ID 必须为列表。"})
        for item in ids:
            if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                raise ValidationError({"exempt_employee_ids": "豁免人员 ID 必须为正整数。"})
        if not ids:
            return
        # 局部导入避免循环依赖
        from apps.personnel.models import Employee

        if not self.team_id:
            return
        valid_ids = set(
            Employee.objects.filter(pk__in=ids, team_id=self.team_id).values_list("pk", flat=True)
        )
        missing = sorted(set(ids) - valid_ids)
        if missing:
            raise ValidationError(
                {"exempt_employee_ids": f"豁免人员不存在或不属于本班组：{missing}。"}
            )

    def __str__(self):
        return f"{self.team} {self.get_shift_name_display()}"
