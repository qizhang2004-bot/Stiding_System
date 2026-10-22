from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    verbose_name = "账号权限"

    def ready(self):
        from . import models  # noqa: F401  注册 User 信号
