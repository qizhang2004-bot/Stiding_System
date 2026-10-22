# 排班记录模型。
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TimeStampedModel
from apps.organization.models import OrganizationUnit, ShiftConfig, Team


class ScheduleRecord(TimeStampedModel):
    # 排班记录表：保存每个排班批次中具体职工每天的排班结果
    class ShiftType(models.TextChoices):
        EARLY = "early", "早班"
        MIDDLE = "middle", "中班"
        NIGHT = "night", "晚班"
        REST = "rest", "休息"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        CONFIRMED = "confirmed", "已确认"
        PUBLISHED = "published", "已发布"
        SUPERSEDED = "superseded", "已作废（被新版本替换，仅保留历史）"
        ARCHIVED = "archived", "已归档（周期结束定稿，只读历史）"

    class ActualType(models.TextChoices):
        # 实际出勤状态（与计划班次分离存储；计划仍保存在 shift_type）
        EARLY = "early", "早班"
        MIDDLE = "middle", "中班"
        NIGHT = "night", "晚班"
        REST = "rest", "休息"
        LEAVE_PERSONAL = "leave_personal", "事假"
        LEAVE_SICK = "leave_sick", "病假"
        LEAVE_MARRIAGE = "leave_marriage", "婚假"
        OTHER = "other", "其他"

    WORK_ACTUAL_TYPES = {ActualType.EARLY, ActualType.MIDDLE, ActualType.NIGHT}
    NON_WORK_ACTUAL_TYPES = {
        ActualType.REST,
        ActualType.LEAVE_PERSONAL,
        ActualType.LEAVE_SICK,
        ActualType.LEAVE_MARRIAGE,
        ActualType.OTHER,
    }

    is_active = models.BooleanField(
        "当前有效", default=True, db_index=True,
        help_text="人工删除排班时置为 False（软删除，仅保留历史，不物理删除记录）",
    )
    schedule_batch_id = models.UUIDField("排班批次号", help_text="同一版排班结果共享同一批次号")
    organization = models.ForeignKey(
        OrganizationUnit,
        verbose_name="所属队组",
        related_name="schedule_records",
        on_delete=models.PROTECT,
    )
    team = models.ForeignKey(
        Team,
        verbose_name="所属班组",
        related_name="schedule_records",
        on_delete=models.PROTECT,
    )
    shift_config = models.ForeignKey(
        ShiftConfig,
        verbose_name="所属班次配置",
        related_name="schedule_records",
        on_delete=models.PROTECT,
    )
    employee = models.ForeignKey(
        "personnel.Employee",
        verbose_name="职工",
        related_name="schedule_records",
        on_delete=models.PROTECT,
    )
    work_date = models.DateField("排班日期")
    shift_type = models.CharField("班次类型", max_length=20, choices=ShiftType.choices)
    position = models.CharField("岗位", max_length=100, blank=True)
    actual_type = models.CharField(
        "实际状态",
        max_length=20,
        blank=True,
        default="",
        help_text="实际出勤状态；为空表示与计划一致（计划班次保留在 shift_type，不被覆盖）",
    )
    actual_remark = models.CharField("实际状态备注", max_length=200, blank=True, default="")
    status = models.CharField("状态", max_length=20, choices=Status.choices, default=Status.DRAFT)
    remark = models.TextField("备注", blank=True)

    class Meta:
        db_table = "schedule_record"
        verbose_name = "排班记录"
        verbose_name_plural = "排班记录"
        ordering = ["work_date", "id"]
        constraints = [
            # 同一排班批次中，同一职工同一天只能有一条记录（休息日同样保存记录）
            models.UniqueConstraint(
                fields=["schedule_batch_id", "employee", "work_date"],
                name="uniq_schedule_batch_employee_date",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "work_date"], name="idx_schedule_org_date"),
            models.Index(fields=["team", "work_date"], name="idx_schedule_team_date"),
            models.Index(fields=["shift_config", "work_date"], name="idx_schedule_shiftconfig_date"),
            models.Index(fields=["employee", "work_date"], name="idx_schedule_employee_date"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.team_id and self.organization_id and self.team.organization_id != self.organization_id:
            errors["organization"] = "排班记录的队组必须与班组的所属队组一致。"
        if self.team_id and self.shift_config_id and self.shift_config.team_id != self.team_id:
            errors["shift_config"] = "班次配置必须属于该班组。"
        if self.employee_id and self.team_id and self.employee.team_id != self.team_id:
            errors["employee"] = "职工必须属于该班组。"
        # 说明：不再要求“职工所属班次配置与本记录班次配置一致”——
        # 排班人员池按班组在职职工计算（N=班组有效职工总人数），允许跨班次轮班写入，
        # 跨班组写入仍由上方班组归属校验阻止（见 docs/DRAFT_SAVE_DESIGN.md）。
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.work_date} {self.get_shift_type_display()} {self.employee}"


class ScheduleBatch(TimeStampedModel):
    # 排班批次头表（草稿与正式共用同一批次号）：
    # 草稿只保存在本表（payload 明细 + 配置/职工快照），不写入 ScheduleRecord；
    # 确认成功后才在同一事务内展开明细写入 ScheduleRecord（status=confirmed）。
    # 设计文档见 docs/DRAFT_SAVE_DESIGN.md。
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        CONFIRMED = "confirmed", "已确认"
        SUPERSEDED = "superseded", "已作废（被新版本替换）"

    class Kind(models.TextChoices):
        PERIOD = "period", "整周期排班"
        ADJUSTMENT = "adjustment", "补充排班（加人）"

    kind = models.CharField(
        "批次类型", max_length=16, choices=Kind.choices, default=Kind.PERIOD,
        help_text="整周期排班草稿或针对个别日期的补充排班（加人），均经确认流程写入正式记录",
    )
    batch_id = models.UUIDField(
        "排班批次号", default=uuid.uuid4, unique=True, editable=False,
        help_text="草稿与正式记录共用同一批次号",
    )
    organization = models.ForeignKey(
        OrganizationUnit, verbose_name="所属队组", related_name="schedule_batches",
        on_delete=models.PROTECT,
    )
    team = models.ForeignKey(
        Team, verbose_name="所属班组", related_name="schedule_batches",
        on_delete=models.PROTECT,
    )
    shift_config = models.ForeignKey(
        ShiftConfig, verbose_name="所属班次配置", related_name="schedule_batches",
        on_delete=models.PROTECT,
    )
    period_start = models.DateField("周期开始")
    period_end = models.DateField("周期结束")
    mode = models.CharField("人多人少模式", max_length=20)
    daily_worker_count = models.PositiveSmallIntegerField("每日要求人数快照")
    position_requirements = models.JSONField("岗位人数配置快照", default=dict)
    employee_targets = models.JSONField("目标班数快照", default=dict)
    employee_qualifications = models.JSONField("职工与岗位资格快照", default=dict)
    payload = models.JSONField(
        "排班明细", help_text="[{employee_id, work_date, shift_type, position}, ...]（含休息行）"
    )
    summary = models.JSONField(
        "求解摘要", default=dict,
        help_text="总/最大班数偏差、休息质量、边界说明、配置快照指纹、校验结果",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="创建人",
        related_name="created_schedule_batches", on_delete=models.PROTECT,
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="确认人",
        related_name="confirmed_schedule_batches", null=True, blank=True,
        on_delete=models.PROTECT,
    )
    status = models.CharField(
        "状态", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    confirmed_at = models.DateTimeField("确认时间", null=True, blank=True)

    class Meta:
        db_table = "schedule_batch"
        verbose_name = "排班批次"
        verbose_name_plural = "排班批次"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "created_at"], name="idx_batch_org_created"),
            models.Index(fields=["shift_config", "status"], name="idx_batch_shiftconfig_status"),
        ]

    def __str__(self):
        return f"{self.team} {self.period_start}~{self.period_end}（{self.get_status_display()}）"
