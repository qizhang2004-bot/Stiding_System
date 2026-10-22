# 操作日志后台注册：只读，禁止修改/删除历史日志。
from django.contrib import admin

from .models import OperationLog


@admin.register(OperationLog)
class OperationLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "username",
        "action",
        "module",
        "target_type",
        "target_id",
        "ip_address",
    )
    list_filter = ("action", "module", "target_type")
    search_fields = ("username", "description", "target_type", "target_id")
    readonly_fields = [field.name for field in OperationLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
