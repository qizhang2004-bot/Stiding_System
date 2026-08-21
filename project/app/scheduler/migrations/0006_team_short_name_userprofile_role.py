# -*- coding: utf-8 -*-
"""给班组加队组中文缩写、给账号加角色字段（先回填缩写再设唯一约束）。"""
from django.db import migrations, models


def backfill_short_names(apps, schema_editor):
    Team = apps.get_model("scheduler", "Team")
    used = set()
    for team in Team.objects.all().order_by("id"):
        short = team.name[:-1] if team.name.endswith("队") else team.name
        base, i = short, 1
        while short in used:
            i += 1
            short = f"{base}{i}"
        used.add(short)
        team.short_name = short
        team.save(update_fields=["short_name"])


class Migration(migrations.Migration):

    dependencies = [
        ("scheduler", "0005_userprofile"),
    ]

    operations = [
        migrations.AddField(
            model_name="team",
            name="short_name",
            field=models.CharField(
                blank=True, max_length=50, verbose_name="队组中文缩写",
                help_text="队员登录用的账号（如 综掘五队 -> 综掘五）"),
        ),
        migrations.RunPython(backfill_short_names, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="team",
            name="short_name",
            field=models.CharField(
                blank=True, max_length=50, unique=True, verbose_name="队组中文缩写",
                help_text="队员登录用的账号（如 综掘五队 -> 综掘五）"),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="role",
            field=models.CharField(
                choices=[("super", "超级管理员"), ("team_admin", "队管理员"), ("member", "队员")],
                default="member", max_length=20, verbose_name="角色"),
        ),
    ]
