# 人员管理后台注册。
from django.contrib import admin

from .models import Employee


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        "employee_no",
        "name",
        "team",
        "shift_config",
        "worked_shifts",
        "min_required_shifts",
        "position",
        "is_active",
    )
    list_filter = ("is_active", "team", "shift_config")
    search_fields = ("employee_no", "name", "position")
    list_select_related = ("team", "shift_config")
    list_editable = ("is_active",)
