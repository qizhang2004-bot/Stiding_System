# 自定义中间件：线程本地请求上下文。
import threading

_local = threading.local()


def get_current_request():
    # 获取当前线程的请求对象（仅在同一请求处理周期内有效）
    return getattr(_local, "request", None)


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _local.request = request
        try:
            return self.get_response(request)
        finally:
            _local.request = None
