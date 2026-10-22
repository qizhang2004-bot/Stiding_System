# 账号层级：用户业务角色与队组绑定。
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.core.models import TimeStampedModel


class UserProfile(TimeStampedModel):
    # 用户业务角色档案。
    # 业务角色与 is_superuser/is_staff 分离：后者仅控制 Django Admin 入口，
    # 由服务层与 super 角色保持同步。
    class Role(models.TextChoices):
        SUPER = "super", "超级管理员"
        ADMIN = "admin", "队组管理员"
        MEMBER = "member", "队组普通成员"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="用户",
        related_name="profile",
        on_delete=models.CASCADE,
    )
    role = models.CharField("角色", max_length=10, choices=Role.choices, default=Role.MEMBER)
    organization = models.ForeignKey(
        "organization.OrganizationUnit",
        verbose_name="所属队组",
        related_name="user_profiles",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        help_text="超级管理员不绑定队组；队组管理员与普通成员必须绑定队组。",
    )

    class Meta:
        db_table = "user_profile"
        verbose_name = "用户角色"
        verbose_name_plural = "用户角色"
        indexes = [
            models.Index(fields=["role", "organization"], name="idx_userprofile_role_org"),
        ]

    def clean(self):
        super().clean()
        if self.role == self.Role.SUPER and self.organization_id:
            raise ValidationError({"organization": "超级管理员不绑定队组。"})
        if self.role != self.Role.SUPER and not self.organization_id:
            raise ValidationError({"organization": "队组管理员与普通成员必须绑定所属队组。"})

    def __str__(self):
        return f"{self.user.username}（{self.get_role_display()}）"


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_user_profile(sender, instance, created, **kwargs):
    # 用户创建后自动补建角色档案（幂等）
    UserProfile.objects.get_or_create(
        user=instance,
        defaults={
            "role": UserProfile.Role.SUPER if instance.is_superuser else UserProfile.Role.MEMBER,
        },
    )
