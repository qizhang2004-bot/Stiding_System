# -*- coding: utf-8 -*-
"""初始化登录账号（三种角色，绑定队组）。

用法：python manage.py seed_users

默认账号：
    超级管理员  admin            （Django 后台 /admin/，看所有队组；密码请超级管理员自行修改）
    队组管理员  3626103 / 111111  -> 综掘五队
    队组管理员  3626102 / 111111  -> 综掘二队
    队员        zjwd    / 111111  -> 综掘五队（只读）
    队员        zjed    / 111111  -> 综掘二队（只读）

自定义：python manage.py seed_users --user 账号 --password 密码 --group 队组名 --role team_admin|member
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from project.app.scheduler.models import Group, UserProfile


class Command(BaseCommand):
    help = "创建登录账号（超级管理员/队组管理员/队员）并绑定队组"

    def add_arguments(self, parser):
        parser.add_argument("--user", default=None, help="账号")
        parser.add_argument("--password", default=None, help="密码")
        parser.add_argument("--group", default=None, help="绑定的队组名")
        parser.add_argument("--role", default="team_admin",
                            choices=["super", "team_admin", "member"], help="角色")

    def handle(self, *args, **options):
        accounts = [
            {"username": "admin", "password": "admin123", "group": None, "role": "super"},
            {"username": "3626103", "password": "111111", "group": "综掘五队", "role": "team_admin"},
            {"username": "3626102", "password": "111111", "group": "综掘二队", "role": "team_admin"},
            {"username": "zjwd", "password": "111111", "group": "综掘五队", "role": "member"},
            {"username": "zjed", "password": "111111", "group": "综掘二队", "role": "member"},
        ]
        if options.get("user"):
            accounts = [{
                "username": options["user"],
                "password": options["password"] or "111111",
                "group": options.get("group"),
                "role": options.get("role", "team_admin"),
            }]

        # 清理旧的自动账号（旧版按班组生成的账号）
        stale = User.objects.exclude(username__in=[a["username"] for a in accounts]) \
            .filter(profile__isnull=False)
        for u in stale:
            self.stdout.write(f"清理旧账号 {u.username}")
            u.delete()

        for acc in accounts:
            user, _ = User.objects.get_or_create(
                username=acc["username"],
                defaults={"is_staff": acc.get("role") == "super"},
            )
            user.set_password(acc["password"])
            user.is_staff = acc.get("role") == "super"
            user.is_superuser = acc.get("role") == "super"
            user.save()

            group = None
            if acc.get("group"):
                group, _ = Group.objects.get_or_create(name=acc["group"])
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.group = group
            profile.role = acc.get("role", "member")
            profile.save()
            label = dict(UserProfile.ROLE_CHOICES).get(profile.role, profile.role)
            self.stdout.write(self.style.SUCCESS(
                f"{label} {acc['username']} -> {group.name if group else '全部'}"
            ))
