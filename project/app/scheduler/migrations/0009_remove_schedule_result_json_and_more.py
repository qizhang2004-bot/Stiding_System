# -*- coding: utf-8 -*-
# 数据重构：Schedule.result_json 中的排班明细统一转移到 Assignment 表，
# 本迁移只把「单休/连休超限」两个质量指标搬到真正的字段，然后删除 result_json。
from django.db import migrations, models


def migrate_quality_metrics(apps, schema_editor):
    """把旧 result_json 里的 single_rest / rest_run_violations 搬到新字段。"""
    Schedule = apps.get_model("scheduler", "Schedule")
    for sch in Schedule.objects.all():
        rj = sch.result_json or {}
        sch.single_rest = rj.get("single_rest", 0) or 0
        sch.rest_run_violations = rj.get("rest_run_violations", 0) or 0
        sch.save(update_fields=["single_rest", "rest_run_violations"])


class Migration(migrations.Migration):

    dependencies = [
        ('scheduler', '0008_person_is_active'),
    ]

    operations = [
        # 1) 先加字段
        migrations.AddField(
            model_name='schedule',
            name='rest_run_violations',
            field=models.IntegerField(default=0, verbose_name='连休超限次数'),
        ),
        migrations.AddField(
            model_name='schedule',
            name='single_rest',
            field=models.IntegerField(default=0, verbose_name='单休次数'),
        ),
        # 2) 搬数据（在删除 result_json 之前）
        migrations.RunPython(migrate_quality_metrics, migrations.RunPython.noop),
        # 3) 再删字段
        migrations.RemoveField(
            model_name='schedule',
            name='result_json',
        ),
    ]
