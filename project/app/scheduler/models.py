# -*- coding: utf-8 -*-
from django.contrib.auth.models import User
from django.db import models

# 固定班次（只有这三种）
SHIFT_CHOICES = [("早班", "早班"), ("中班", "中班"), ("晚班", "晚班")]
DEFAULT_TEAMS = ["检修班", "运输班", "生产一班", "生产二班", "生产三班"]


class Group(models.Model):
    """队组：综掘五队 / 综掘二队（登录与权限的作用域，一个队组下有多个班组）。"""
    name = models.CharField("队组名称", max_length=50, unique=True)
    short_name = models.CharField("队员登录缩写", max_length=50, unique=True, blank=True,
        help_text="队员登录用的账号（如 综掘五队 -> zjwd）")

    class Meta:
        verbose_name = "队组"
        verbose_name_plural = "队组"

    def __str__(self):
        return self.name


class Team(models.Model):
    """班组：检修班 / 运输班 / 生产一班 / 生产二班 / 生产三班（归属某个队组）。

    每个班组保存自己的排班约束（1.1~1.4）。
    """
    group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="teams", verbose_name="所属队组"
    )
    name = models.CharField("班组名称", max_length=50)

    # 1.1 该班应上人数（每天下井总人数）
    daily_headcount = models.IntegerField("该班应上人数（每天）", default=0)

    # 1.1b 每个班次每天的人数（早班/中班/晚班各多少人，如 {"早班":3,"中班":5,"夜班":4}）
    #      填了则按每班精确人数排班，优先于 daily_headcount
    shift_demand = models.JSONField("每班每天人数", default=dict)

    # 1.2 固定岗位应上人数 {"电工":{"op":">=","count":2}}
    role_reqs = models.JSONField("固定岗位应上人数", default=dict)

    # 1.3 连休范围（休息必须连休 min~max 天）
    rest_block = models.JSONField("连休范围", default=dict)  # {"min":2,"max":4}

    # 1.4 每人应上最少班数 + 豁免名单
    min_shift_target = models.IntegerField("每人应上最少班数", default=18)
    exempt_names = models.JSONField("豁免人员", default=list)

    class Meta:
        verbose_name = "班组"
        verbose_name_plural = "班组"
        unique_together = [("group", "name")]

    def __str__(self):
        return self.name


class Role(models.Model):
    """岗位（工种），如 电工 / 班长 / 皮带。"""
    name = models.CharField("岗位名称", max_length=50, unique=True)

    class Meta:
        verbose_name = "岗位"
        verbose_name_plural = "岗位"

    def __str__(self):
        return self.name


class Person(models.Model):
    """人员。一个人可以胜任多个岗位（岗位互不重复，由 ManyToMany 保证唯一）。"""
    name = models.CharField("姓名", max_length=50, unique=True)
    team = models.ForeignKey(
        Team, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="persons", verbose_name="班组"
    )
    roles = models.ManyToManyField(
        Role, related_name="persons", blank=True, verbose_name="岗位"
    )
    default_shift = models.CharField(
        "默认班次", max_length=10, choices=SHIFT_CHOICES, default="早班"
    )
    worked_so_far = models.IntegerField("已上班数", default=0,
        help_text="导入时的已上班数（排班后按日期自动累计）")
    required_shifts = models.IntegerField("应上班数", default=0,
        help_text="本月应上班班数（每人班次要求，0 表示使用全局最少班数）")
    is_active = models.BooleanField("是否启用", default=True,
        help_text="禁用的人员不参与排班（但仍保留在人员列表里）")

    class Meta:
        verbose_name = "人员"
        verbose_name_plural = "人员"
        ordering = ["team__name", "name"]

    def __str__(self):
        return self.name


class Schedule(models.Model):
    """一次排班的配置快照 + 求解状态。

    排班明细（谁哪天哪个班）统一存在 Assignment 表里，本表不再保存结果 JSON，
    避免「结果 JSON」与「Assignment 明细」两份数据重复、改班时还要两边同步。
    这里只保留配置快照、求解状态、诊断信息，以及两个求解质量指标。
    """
    team = models.ForeignKey(
        Team, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="schedules", verbose_name="班组"
    )
    year = models.IntegerField("年份")
    month = models.IntegerField("月份")
    start_date = models.DateField("周期开始日期", null=True, blank=True)
    days = models.IntegerField("周期天数")

    # 班次与每班人数（固定为 早班/中班/晚班；若用 daily_total 则逐班人数可为空）
    shifts = models.JSONField("班次列表", default=list)
    shift_demand = models.JSONField("每班每天人数", default=dict)
    daily_total = models.IntegerField("每天下井总人数", null=True, blank=True)

    # 岗位人数条件 {"电工":{"op":">=","count":2}}  op: >= / <= / ==
    role_reqs = models.JSONField("岗位人数条件", default=dict)

    # 每人最少出勤班数（全员一致）与豁免名单
    min_shift_target = models.IntegerField("最少出勤班数", default=18)
    exempt_names = models.JSONField("豁免人员名单", default=list)

    # 休息规则与工作窗口
    rest_block = models.JSONField("连休规则", default=dict)   # {"min":2,"max":4}
    work_window = models.JSONField("工作窗口", default=dict)  # {"length":10,"max_work":6}

    # 求解时的人员快照（防止以后改人员导致旧结果对不上）
    worker_snapshot = models.JSONField("人员快照", default=list)

    # 求解结果
    status = models.CharField("求解状态", max_length=20, default="")
    message = models.CharField("结果说明", max_length=255, default="")
    diagnostics = models.JSONField("诊断信息", default=list)
    # 求解质量指标（排班明细在 Assignment 表，这两个指标单独存字段）
    single_rest = models.IntegerField("单休次数", default=0)
    rest_run_violations = models.IntegerField("连休超限次数", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "排班记录"
        verbose_name_plural = "排班记录"
        ordering = ["-created_at"]

    def __str__(self):
        team = f"{self.team.name} " if self.team else ""
        return f"{team}{self.year}-{self.month:02d} 排班"


class Assignment(models.Model):
    """某次排班中，某个人某天上的班次（用于个人日历的展示与改班）。"""
    schedule = models.ForeignKey(
        Schedule, on_delete=models.CASCADE, related_name="assignments", verbose_name="排班"
    )
    person = models.ForeignKey(
        Person, on_delete=models.CASCADE, related_name="assignments", verbose_name="人员"
    )
    day = models.IntegerField("周期内第几天（0 起）")
    shift = models.CharField("班次", max_length=10, blank=True, default="")

    class Meta:
        verbose_name = "排班明细"
        verbose_name_plural = "排班明细"
        unique_together = [("schedule", "person", "day")]

    def __str__(self):
        return f"{self.person} 第{self.day + 1}天 {self.shift}"


class UserProfile(models.Model):
    """登录账号与队组的绑定：一个账号对应一个队组（登录后只看本队组的排班）。

    角色：
      super      超级管理员（Django 后台，可看所有队组）
      team_admin 队组管理员（可编辑本队组的排班）
      member     队员（队组缩写登录，只读查看）
    """
    ROLE_CHOICES = [
        ("super", "超级管理员"),
        ("team_admin", "队组管理员"),
        ("member", "队员"),
    ]
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="profile", verbose_name="登录账号"
    )
    group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="profiles", verbose_name="所属队组"
    )
    role = models.CharField("角色", max_length=20, choices=ROLE_CHOICES, default="member")

    class Meta:
        verbose_name = "账号-队组绑定"
        verbose_name_plural = "账号-队组绑定"

    @property
    def role_label(self):
        return dict(self.ROLE_CHOICES).get(self.role, self.role)

    def __str__(self):
        return f"{self.user.username} -> {self.group.name if self.group else '未绑定'}"
