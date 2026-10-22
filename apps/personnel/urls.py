# 人员管理路由。
from django.urls import path

from . import views

app_name = "personnel"

urlpatterns = [
    path("employees/", views.employee_list, name="employee_list"),
    path("employees/create/", views.employee_create, name="employee_create"),
    path("employees/<int:pk>/", views.employee_detail, name="employee_detail"),
    path("employees/<int:pk>/calendar/", views.employee_calendar, name="employee_calendar"),
    path("employees/<int:pk>/edit/", views.employee_edit, name="employee_edit"),
    path("employees/<int:pk>/toggle/", views.employee_toggle, name="employee_toggle"),
    path("person-board/", views.person_board, name="person_board"),
    path("import/", views.employee_import, name="employee_import"),
    path("import/template.csv", views.import_template, name="import_template"),
]
