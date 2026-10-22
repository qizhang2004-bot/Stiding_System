# 关键操作记录模型。
from django.conf import settings
from django.db import models


class OperationLog(models.Model):
    # 关键操作记录表：记录队组、班组、人员、班次配置及排班等模块的关键操作。
    # 日志创建后，普通业务操作不得修改或删除历史日志。
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="操作用户",
        related_name="operation_logs",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    username = models.CharField("用户名快照", max_length=150, blank=True)
    action = models.CharField("操作类型", max_length=50)
    module = models.CharField("所属模块", max_length=50)
    target_type = models.CharField("对象类型", max_length=100, blank=True)
    target_id = models.CharField("对象ID", max_length=100, blank=True)
    description = models.TextField("操作描述")
    before_data = models.JSONField("操作前数据", null=True, blank=True)
    after_data = models.JSONField("操作后数据", null=True, blank=True)
    ip_address = models.GenericIPAddressField("来源IP", null=True, blank=True)
    created_at = models.DateTimeField("操作时间", auto_now_add=True, db_index=True)

    class Meta:
        db_table = "operation_log"
        verbose_name = "操作日志"
        verbose_name_plural = "操作日志"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.module}.{self.action}"
