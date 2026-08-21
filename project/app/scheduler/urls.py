# -*- coding: utf-8 -*-
from django.urls import path

from . import views

app_name = "scheduler"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("", views.index, name="index"),
    # 班组管理（点击班组显示存储约束 + 该班人员）
    path("teams/", views.team_manage, name="team_manage"),
    # 人员编辑 / 个人日历
    path("persons/<int:person_id>/edit/", views.person_edit, name="person_edit"),
    path("persons/<int:person_id>/", views.person_detail, name="person_detail"),
    # 班次展示（日历：每天每班次人数，按班组分层）
    path("board/", views.shift_board, name="shift_board"),
    path("board/<int:year>/<int:month>/<int:day>/<str:shift>/", views.shift_detail, name="shift_detail"),
    # 排班结果 / 记录
    path("schedule/<int:pk>/", views.schedule_result, name="schedule_result"),
    path("schedule/list/", views.schedule_list, name="schedule_list"),
]
