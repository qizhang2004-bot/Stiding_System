# 组织架构后台注册。
from django.contrib import admin

from .models import OrganizationUnit, ShiftConfig, Team


class ShiftConfigInline(admin.TabularInline):
    model = ShiftConfig
    extra = 0
    fields = (
        "shift_name",
        "daily_worker_count",
        "min_consecutive_rest_days",
        "max_consecutive_rest_days",
        "required_shifts",
        "special_position_count",
        "exempt_employee_ids",
        "is_active",
    )


@admin.register(OrganizationUnit)
class OrganizationUnitAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "created_at", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "code")
    list_editable = ("is_active",)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "organization", "is_active", "created_at")
    list_filter = ("organization", "is_active")
    search_fields = ("name", "code", "organization__name")
    list_select_related = ("organization",)
    list_editable = ("is_active",)
    inlines = [ShiftConfigInline]


@admin.register(ShiftConfig)
class ShiftConfigAdmin(admin.ModelAdmin):
    list_display = (
        "team",
        "shift_name",
        "daily_worker_count",
        "min_consecutive_rest_days",
        "max_consecutive_rest_days",
        "required_shifts",
        "special_position_count",
        "is_active",
    )
    list_filter = ("shift_name", "is_active", "team__organization")
    search_fields = ("team__name", "team__code")
    list_select_related = ("team",)
    list_editable = ("is_active",)
