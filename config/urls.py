# 全局路由入口。
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("", include("apps.core.urls")),
    path("organization/", include("apps.organization.urls")),
    path("personnel/", include("apps.personnel.urls")),
    path("scheduler/", include("apps.scheduler.urls")),
    path("auditlog/", include("apps.auditlog.urls")),
]
