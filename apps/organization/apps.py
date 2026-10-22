from django.apps import AppConfig


class OrganizationConfig(AppConfig):
    name = "apps.organization"
    verbose_name = "组织架构"

    def ready(self):
        from . import signals  # noqa: F401
