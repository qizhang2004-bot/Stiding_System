# -*- coding: utf-8 -*-
from django.contrib import admin

from .models import Assignment, Group, Person, Role, Schedule, Team, UserProfile


class GlassAdminMixin:
    """给所有后台页面注入「渐变色背景 + 毛玻璃」主题 CSS。"""

    class Media:
        css = {"all": ("admin_theme_v2.css",)}


class GlassInlineMixin:
    """内联表格同样注入主题 CSS。"""

    class Media:
        css = {"all": ("admin_theme_v2.css",)}


# ---------------------------------------------------------------------------
# 层级式后台：队组(Group) -> 班组(Team) -> 人员(Person) -> 排班(Schedule/Assignment)
# 每个上层对象都内联展示下一层，形成多层级增删改查。
# ---------------------------------------------------------------------------
class TeamInline(GlassInlineMixin, admin.TabularInline):
    """队组 -> 班组（内联）。"""
    model = Team
    extra = 0
    fields = ("name", "daily_headcount", "min_shift_target", "rest_block", "exempt_names")
    show_change_link = True


@admin.register(Group)
class GroupAdmin(GlassAdminMixin, admin.ModelAdmin):
    list_display = ("name", "short_name", "team_count", "person_count")
    search_fields = ("name", "short_name")
    inlines = [TeamInline]

    @admin.display(description="班组数")
    def team_count(self, obj):
        return obj.teams.count()

    @admin.display(description="人员数")
    def person_count(self, obj):
        return obj.person_count


class PersonInline(GlassInlineMixin, admin.TabularInline):
    """班组 -> 人员（内联）。"""
    model = Person
    extra = 0
    fields = ("name", "default_shift", "worked_so_far", "required_shifts", "is_active")
    show_change_link = True


@admin.register(Team)
class TeamAdmin(GlassAdminMixin, admin.ModelAdmin):
    list_display = ("name", "group", "daily_headcount", "min_shift_target", "person_count")
    list_filter = ("group",)
    search_fields = ("name", "group__name")
    list_select_related = ("group",)
    inlines = [PersonInline]

    @admin.display(description="人数")
    def person_count(self, obj):
        return obj.persons.count()


@admin.register(Role)
class RoleAdmin(GlassAdminMixin, admin.ModelAdmin):
    list_display = ("name", "holder_count")
    search_fields = ("name",)

    @admin.display(description="持有人数")
    def holder_count(self, obj):
        return obj.persons.count()


@admin.register(Person)
class PersonAdmin(GlassAdminMixin, admin.ModelAdmin):
    list_display = ("name", "team", "group_name", "default_shift",
                    "worked_so_far", "required_shifts", "is_active", "role_names")
    list_filter = ("team__group", "team", "default_shift", "is_active")
    search_fields = ("name", "team__name")
    list_select_related = ("team", "team__group")
    filter_horizontal = ("roles",)

    @admin.display(description="队组")
    def group_name(self, obj):
        return obj.team.group.name if obj.team and obj.team.group else "—"

    @admin.display(description="岗位")
    def role_names(self, obj):
        return "、".join(obj.roles.values_list("name", flat=True))


@admin.register(Schedule)
class ScheduleAdmin(GlassAdminMixin, admin.ModelAdmin):
    list_display = ("__str__", "team", "group_name", "year", "month", "status",
                    "daily_total", "created_at")
    list_filter = ("status", "team__group", "team", "year", "month")
    search_fields = ("team__name",)
    list_select_related = ("team", "team__group")
    ordering = ("-created_at",)

    @admin.display(description="队组")
    def group_name(self, obj):
        return obj.team.group.name if obj.team and obj.team.group else "—"


@admin.register(Assignment)
class AssignmentAdmin(GlassAdminMixin, admin.ModelAdmin):
    list_display = ("schedule", "person", "day", "shift", "role")
    list_filter = ("shift", "role", "schedule__team__group", "schedule__team")
    search_fields = ("person__name",)
    list_select_related = ("schedule__team", "person")
    ordering = ("schedule", "day", "shift")


@admin.register(UserProfile)
class UserProfileAdmin(GlassAdminMixin, admin.ModelAdmin):
    list_display = ("user", "group", "role")
    list_filter = ("group", "role")
    search_fields = ("user__username", "group__name")
