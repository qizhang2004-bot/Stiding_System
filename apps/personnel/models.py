# 职工个人信息模型。
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TimeStampedModel
from apps.organization.models import ShiftConfig, Team


class Employee(TimeStampedModel):
    # 个人信息表
    employee_no = models.CharField("职工编号", max_length=50, unique=True)
    name = models.CharField("姓名", max_length=50)
    team = models.ForeignKey(
        Team,
        verbose_name="所属班组",
        related_name="employees",
        on_delete=models.PROTECT,
    )
    shift_config = models.ForeignKey(
        ShiftConfig,
        verbose_name="所属班次配置",
        related_name="employees",
        on_delete=models.PROTECT,
    )
    worked_shifts = models.PositiveSmallIntegerField(
        "本周期已上班数",
        default=0,
        help_text="仅统计当前排班周期（每月25日至次月24日）已完成的出勤班数，不跨周期累计",
    )
    min_required_shifts = models.PositiveSmallIntegerField(
        "矿上最低出勤班数",
        help_text="仅用于展示、统计和预警，不参与排班算法计算",
    )
    position = models.CharField("岗位", max_length=100, blank=True)
    is_active = models.BooleanField("是否在岗", default=True)
    remark = models.TextField("备注", blank=True)

    class Meta:
        db_table = "employee"
        verbose_name = "职工"
        verbose_name_plural = "职工"
        ordering = ["employee_no"]
        indexes = [
            models.Index(fields=["team", "shift_config"], name="idx_employee_team_shiftconfig"),
        ]

    def clean(self):
        super().clean()
        if self.team_id and self.shift_config_id and self.shift_config.team_id != self.team_id:
            raise ValidationError({"shift_config": "班次配置所属班组与职工所属班组不一致。"})

    def __str__(self):
        return f"{self.employee_no} {self.name}"
