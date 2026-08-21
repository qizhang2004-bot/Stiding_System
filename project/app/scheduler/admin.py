# -*- coding: utf-8 -*-
from django.contrib import admin

from .models import Assignment, Person, Role, Schedule, Team, UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "group", "role")
    list_filter = ("group", "role")


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "person_count")

    @admin.display(description="人数")
    def person_count(self, obj):
        return obj.persons.count()


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name",)


class PersonAdmin(admin.ModelAdmin):
    list_display = ("name", "team", "default_shift", "worked_so_far", "required_shifts", "role_names")
    list_filter = ("team", "default_shift")
    filter_horizontal = ("roles",)

    @admin.display(description="岗位")
    def role_names(self, obj):
        return "、".join(obj.roles.values_list("name", flat=True))


admin.site.register(Person, PersonAdmin)


@admin.register(Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display = ("__str__", "team", "status", "daily_total", "created_at")
    list_filter = ("status", "team", "year", "month")


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ("schedule", "person", "day", "shift")
    list_filter = ("schedule", "shift")
