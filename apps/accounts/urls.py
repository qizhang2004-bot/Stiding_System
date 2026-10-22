# 账号与认证路由（名称与 django.contrib.auth 保持一致：login/logout/password_change）。
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.app_login, name="login"),
    path("logout/", views.app_logout, name="logout"),
    path("password-change/", views.app_password_change, name="password_change"),
    path("password-change/done/", views.app_password_change_done, name="password_change_done"),
    path("home/", views.home, name="home"),
    # 超级管理员：全局账号管理
    path("users/", views.user_list, name="user_list"),
    path("users/create/", views.user_create, name="user_create"),
    path("users/<int:pk>/", views.user_detail, name="user_detail"),
    path("users/<int:pk>/edit/", views.user_edit, name="user_edit"),
    path("users/<int:pk>/toggle/", views.user_toggle, name="user_toggle"),
    path("users/<int:pk>/reset-password/", views.user_reset_password, name="user_reset_password"),
    # 队组管理员：本队组普通成员账号管理
    path("organization-members/", views.member_list, name="member_list"),
    path("organization-members/create/", views.member_create, name="member_create"),
    path("organization-members/<int:pk>/toggle/", views.member_toggle, name="member_toggle"),
    path(
        "organization-members/<int:pk>/reset-password/",
        views.member_reset_password,
        name="member_reset_password",
    ),
]
