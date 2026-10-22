# 信号：创建班组时自动初始化早/中/晚三条班次配置（幂等）。
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Team
from .services import ensure_default_shift_configs


@receiver(post_save, sender=Team)
def auto_create_default_shift_configs(sender, instance, created, **kwargs):
    # 仅在新建班组时初始化；重复保存/重复初始化不会产生重复记录
    # （ensure_default_shift_configs 内部 get_or_create + 数据库联合唯一约束双重保证）。
    if not created:
        return
    ensure_default_shift_configs(instance)
