# 首页、仪表盘（工作台）：超级管理员全局视图；队组管理员仅本队组视图；普通成员跳转班次看板。
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.accounts.permissions import (
    ROLE_MEMBER,
    ROLE_SUPER,
    get_organization,
    get_role,
    visible_organizations,
)
from apps.auditlog.models import OperationLog
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.models import ScheduleRecord
from apps.scheduler.selectors import dashboard_trend_series, period_range


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "core/dashboard.html"

    def get(self, request, *args, **kwargs):
        # 普通成员无工作台入口：统一跳转班次看板
        if get_role(request.user) == ROLE_MEMBER:
            return redirect("scheduler:shift_board")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        role = get_role(user)
        orgs = visible_organizations(user)
        context.update(
            {
                "user_role": role,
                "is_super": role == ROLE_SUPER,
                "scope_org": get_organization(user),
                "organization_count": OrganizationUnit.objects.filter(pk__in=orgs).count(),
                "team_count": Team.objects.filter(organization__in=orgs).count(),
                "active_employee_count": Employee.objects.filter(
                    is_active=True, team__organization__in=orgs
                ).count(),
                "inactive_employee_count": Employee.objects.filter(
                    is_active=False, team__organization__in=orgs
                ).count(),
                "active_config_count": ShiftConfig.objects.filter(
                    is_active=True, team__organization__in=orgs
                ).count(),
                "record_status": {
                    row["status"]: row["count"]
                    for row in ScheduleRecord.objects.filter(team__organization__in=orgs)
                    .values("status")
                    .annotate(count=Count("id"))
                },
                "teams_without_active_config": (
                    Team.objects.filter(organization__in=orgs)
                    .annotate(
                        active_configs=Count(
                            "shift_configs", filter=Q(shift_configs__is_active=True)
                        )
                    )
                    .filter(active_configs=0)
                    .select_related("organization")[:8]
                ),
                "chart": dashboard_trend_series(
                    orgs,
                    start=period_range()[0],
                    end=period_range()[1],
                    is_super=role == ROLE_SUPER,
                ),
                "recent_logs": (
                    OperationLog.objects.select_related("user").order_by("-created_at")[:6]
                    if role == ROLE_SUPER
                    else []
                ),
            }
        )
        return context
