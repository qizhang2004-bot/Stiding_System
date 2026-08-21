# -*- coding: utf-8 -*-
"""概念重构：队组(Group) -> 班组(Team) -> 人员；账号绑定队组。

- 新增 Group（队组：综掘五队/综掘二队），Team 增加 group 外键
- 账号绑定从 Team 改为 Group
- 数据迁移：旧 Team 中的 综掘五队/综掘二队 转成队组，班组归入队组
"""
import django.db.models.deletion
from django.db import migrations, models


def migrate_group_data(apps, schema_editor):
    Group = apps.get_model("scheduler", "Group")
    Team = apps.get_model("scheduler", "Team")
    Person = apps.get_model("scheduler", "Person")
    Schedule = apps.get_model("scheduler", "Schedule")
    UserProfile = apps.get_model("scheduler", "UserProfile")

    g5, _ = Group.objects.get_or_create(name="综掘五队", defaults={"short_name": "zjwd"})
    g2, _ = Group.objects.get_or_create(name="综掘二队", defaults={"short_name": "zjed"})
    groups = {"综掘五队": g5, "综掘二队": g2}

    # 1) 账号绑定迁移（此时 userprofile.team 字段还在）
    for prof in UserProfile.objects.all():
        t = prof.team
        if not t:
            continue
        if t.name in groups:
            prof.group = groups[t.name]
        else:
            prof.group = g5  # 班组账号 -> 归综掘五队（后续可调整）
        prof.save(update_fields=["group"])

    # 2) 现有班组（检修班/运输班/生产一二三班）归入综掘五队
    for t in Team.objects.filter(group__isnull=True):
        if t.name in groups:
            continue
        t.group = g5
        t.save(update_fields=["group"])

    # 3) 旧"队组"Team 行（综掘五队/综掘二队）转成队组：人员并入该队组下的检修班，再删除
    for old in Team.objects.filter(name__in=groups):
        grp = groups[old.name]
        target, _ = Team.objects.get_or_create(group=grp, name="检修班", defaults={
            "short_name": f"{grp.short_name}-检修班",  # 迁移后该字段会被删除，仅保证唯一
            "daily_headcount": old.daily_headcount,
            "role_reqs": old.role_reqs,
            "rest_block": old.rest_block,
            "min_shift_target": old.min_shift_target,
            "exempt_names": old.exempt_names,
        })
        Person.objects.filter(team=old).update(team=target)
        Schedule.objects.filter(team=old).update(team=None)
        old.delete()

    # 4) 综掘二队也建好 5 个班组（空人员，等导入）
    for tname in ["检修班", "运输班", "生产一班", "生产二班", "生产三班"]:
        Team.objects.get_or_create(group=g2, name=tname,
                                   defaults={"short_name": f"zjed-{tname}"})

    # 5) 班组上的账号 -> 归属其班组所在的队组
    for prof in UserProfile.objects.all():
        if prof.group is None:
            team = Team.objects.filter(profiles=prof).first()
            if team and team.group_id:
                prof.group = team.group
                prof.save(update_fields=["group"])


class Migration(migrations.Migration):

    dependencies = [
        ('scheduler', '0006_team_short_name_userprofile_role'),
    ]

    operations = [
        migrations.CreateModel(
            name='Group',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=50, unique=True, verbose_name='队组名称')),
                ('short_name', models.CharField(blank=True, help_text='队员登录用的账号（如 综掘五队 -> zjwd）', max_length=50, unique=True, verbose_name='队员登录缩写')),
            ],
            options={
                'verbose_name': '队组',
                'verbose_name_plural': '队组',
            },
        ),
        migrations.AlterModelOptions(
            name='userprofile',
            options={'verbose_name': '账号-队组绑定', 'verbose_name_plural': '账号-队组绑定'},
        ),
        migrations.AlterField(
            model_name='team',
            name='name',
            field=models.CharField(max_length=50, verbose_name='班组名称'),
        ),
        migrations.AlterField(
            model_name='userprofile',
            name='role',
            field=models.CharField(choices=[('super', '超级管理员'), ('team_admin', '队组管理员'), ('member', '队员')], default='member', max_length=20, verbose_name='角色'),
        ),
        migrations.AddField(
            model_name='team',
            name='group',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='teams', to='scheduler.group', verbose_name='所属队组'),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='group',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='profiles', to='scheduler.group', verbose_name='所属队组'),
        ),
        migrations.RunPython(migrate_group_data, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(
            name='team',
            unique_together={('group', 'name')},
        ),
        migrations.RemoveField(
            model_name='userprofile',
            name='team',
        ),
        migrations.RemoveField(
            model_name='team',
            name='short_name',
        ),
    ]
