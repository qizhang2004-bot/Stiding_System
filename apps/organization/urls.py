# 组织管理路由。
from django.urls import path

from . import views

app_name = "organization"

urlpatterns = [
    # 队组
    path("units/", views.unit_list, name="unit_list"),
    path("units/create/", views.unit_create, name="unit_create"),
    path("units/create/confirm/", views.unit_create_confirm, name="unit_create_confirm"),
    path("units/create/success/", views.unit_create_success, name="unit_create_success"),
    path("units/<int:pk>/", views.unit_detail, name="unit_detail"),
    path("units/<int:pk>/edit/", views.unit_edit, name="unit_edit"),
    path("units/<int:pk>/toggle/", views.unit_toggle, name="unit_toggle"),
    # 班组
    path("teams/", views.team_list, name="team_list"),
    path("teams/create/", views.team_create, name="team_create"),
    path("teams/<int:pk>/", views.team_detail, name="team_detail"),
    path("teams/<int:pk>/edit/", views.team_edit, name="team_edit"),
    path("teams/<int:pk>/toggle/", views.team_toggle, name="team_toggle"),
    # 班次配置
    path("shift-configs/", views.shift_config_list, name="shift_config_list"),
    path("shift-configs/<int:pk>/edit/", views.shift_config_edit, name="shift_config_edit"),
    path("shift-configs/<int:pk>/toggle/", views.shift_config_toggle, name="shift_config_toggle"),
    # 联动 JSON
    path("teams.json", views.teams_json, name="teams_json"),
    path("shift-configs.json", views.shift_configs_json, name="shift_configs_json"),
]
