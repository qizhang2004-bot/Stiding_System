# 操作日志页面：只读列表与详情（仅超级管理员可访问）。
import json
from datetime import datetime

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, render

from apps.accounts.permissions import ROLE_SUPER, role_required

from .models import OperationLog


@role_required(ROLE_SUPER)
def log_list(request):
    q = request.GET.get("q", "").strip()
    module = request.GET.get("module", "")
    action = request.GET.get("action", "")
    start_date = request.GET.get("start_date", "")
    end_date = request.GET.get("end_date", "")

    logs = OperationLog.objects.select_related("user").order_by("-created_at")
    if q:
        logs = logs.filter(Q(username__icontains=q) | Q(description__icontains=q))
    if module:
        logs = logs.filter(module=module)
    if action:
        logs = logs.filter(action=action)
    try:
        if start_date:
            logs = logs.filter(created_at__date__gte=datetime.strptime(start_date, "%Y-%m-%d").date())
    except ValueError:
        pass
    try:
        if end_date:
            logs = logs.filter(created_at__date__lte=datetime.strptime(end_date, "%Y-%m-%d").date())
    except ValueError:
        pass

    paginator = Paginator(logs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "auditlog/log_list.html",
        {
            "page_obj": page_obj,
            "q": q,
            "module": module,
            "action": action,
            "start_date": start_date,
            "end_date": end_date,
            "modules": OperationLog.objects.values_list("module", flat=True).distinct().order_by("module"),
            "actions": OperationLog.objects.values_list("action", flat=True).distinct().order_by("action"),
        },
    )


@role_required(ROLE_SUPER)
def log_detail(request, pk):
    log = get_object_or_404(OperationLog.objects.select_related("user"), pk=pk)
    before_pretty = (
        json.dumps(log.before_data, ensure_ascii=False, indent=2)
        if log.before_data is not None
        else None
    )
    after_pretty = (
        json.dumps(log.after_data, ensure_ascii=False, indent=2)
        if log.after_data is not None
        else None
    )
    return render(
        request,
        "auditlog/log_detail.html",
        {"log": log, "before_pretty": before_pretty, "after_pretty": after_pretty},
    )
