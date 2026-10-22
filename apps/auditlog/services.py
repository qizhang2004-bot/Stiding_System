# 日志记录服务（只增不改）。
from .models import OperationLog


def log_operation(
    *,
    user=None,
    action,
    module,
    target_type="",
    target_id="",
    description,
    before_data=None,
    after_data=None,
    ip_address="",
):
    # 记录一条关键操作日志
    return OperationLog.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        username=getattr(user, "username", "") if user is not None else "",
        action=action[:50],
        module=module[:50],
        target_type=target_type[:100],
        target_id=str(target_id)[:100],
        description=description,
        before_data=before_data,
        after_data=after_data,
        ip_address=ip_address[:39],
    )
