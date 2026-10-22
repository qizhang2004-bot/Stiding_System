# 停用账号的会话访问限制：已停用账号的既有会话在下一次请求时被登出。
from django.contrib.auth import logout


class ActiveUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and not request.user.is_active:
            logout(request)
        return self.get_response(request)
