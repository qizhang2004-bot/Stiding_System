#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 dumpdata 导出的 JSON 按依赖顺序导入当前数据库（用于 SQLite -> MySQL 迁移）。

用法（服务器上，先 DJANGO_SETTINGS_MODULE=project.settings.prod）：
    python tools/import_fixture.py /tmp/server_dump.json

说明：
    Django 自带 loaddata 在多对象场景偶发唯一键冲突报错，本脚本按
    User/Group/Role/UserProfile/Team/Person/Schedule/Assignment 的依赖顺序
    逐对象反序列化导入，稳靠可控，且失败会打印具体对象。
"""
import os
import sys

import django
from django.core import serializers
from django.db import connection

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings.prod")
django.setup()

ORDER = {
    "auth.user": 0,
    "scheduler.group": 1,
    "scheduler.role": 2,
    "scheduler.userprofile": 3,
    "scheduler.team": 4,
    "scheduler.person": 5,
    "scheduler.schedule": 6,
    "scheduler.assignment": 7,
}


def main(path: str) -> None:
    with connection.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0;")
    objs = list(serializers.deserialize(
        "json", open(path, encoding="utf-8"), ignorenonexistent=True))
    objs.sort(key=lambda o: ORDER.get(o.object._meta.label_lower, 50))
    ok = 0
    for o in objs:
        try:
            o.save()
            ok += 1
        except Exception as e:  # noqa: BLE001
            print("插入失败:", o.object._meta.label_lower,
                  getattr(o.object, "pk", "?"), "->", e)
    print(f"导入完成: {ok}/{len(objs)} 个对象")
    with connection.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=1;")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
