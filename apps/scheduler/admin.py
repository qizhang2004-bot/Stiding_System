# 排班记录后台注册。
from django.contrib import admin

from .models import ScheduleRecord


@admin.register(ScheduleRecord)
class ScheduleRecordAdmin(admin.ModelAdmin):
    list_display = (
        "work_date",
        "employee",
        "shift_type",
        "status",
        "team",
        "organization",
        "schedule_batch_id",
    )
    list_filter = ("status", "shift_type", "team", "organization")
    search_fields = ("employee__name", "employee__employee_no", "schedule_batch_id")
    date_hierarchy = "work_date"
    list_select_related = ("employee", "team", "organization", "shift_config")

    # F2：排班记录在 Admin 中只读 —— 日常维护必须走排班日历/接口，
    # 以保证 Team 行锁、归档门禁、不变量校验与操作审计不被绕过。
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        # 移除批量删除等可能改动数据的 action
        return {}

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]
