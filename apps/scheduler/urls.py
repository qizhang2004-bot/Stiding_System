# 排班管理路由。
from django.urls import path

from . import views

app_name = "scheduler"

urlpatterns = [
    path("plan/", views.schedule_plan, name="schedule_plan"),
    path("plan/generate/<int:pk>/", views.schedule_generate, name="schedule_generate"),
    path("drafts/<uuid:batch_id>/", views.schedule_draft_detail, name="schedule_draft_detail"),
    path("results/", views.schedule_results, name="schedule_results"),
    path("shift-board/", views.shift_board, name="shift_board"),
    path("shift-board/<str:work_date>/<str:shift_name>/", views.shift_board_detail, name="shift_board_detail"),
    path("shift-board/<str:work_date>/<str:shift_name>/add/", views.shift_board_add, name="shift_board_add"),
    path("shift-board/records/<int:record_id>/remove/", views.shift_board_remove, name="shift_board_remove"),
    path("shift-board/records/<int:record_id>/change/", views.shift_board_change, name="shift_board_change"),
    path("teams/<int:team_id>/archive-period/", views.schedule_archive_period, name="schedule_archive_period"),
]
