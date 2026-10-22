# 请求操作记录中间件。
import logging

logger = logging.getLogger(__name__)

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
SKIP_PREFIXES = ("/static/", "/media/")


class AuditLogMiddleware:
    # 记录写操作（POST/PUT/PATCH/DELETE）的请求日志
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.method in WRITE_METHODS:
            self._log(request)
        return response

    def _log(self, request):
        path = request.path
        if path.startswith(SKIP_PREFIXES):
            return
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return
        try:
            from .services import log_operation

            match = getattr(request, "resolver_match", None)
            log_operation(
                user=user,
                action=request.method,
                module="http",
                target_type=(match.view_name if match else "")[:100],
                target_id=(str(match.kwargs.get("pk")) if match and match.kwargs else "")[:100],
                description=f"{request.method} {path}",
                ip_address=self._client_ip(request),
            )
        except Exception:  # noqa: BLE001
            logger.exception("操作日志记录失败")

    @staticmethod
    def _client_ip(request):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "")
