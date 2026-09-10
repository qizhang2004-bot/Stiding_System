#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把旧 SQLite 数据库(db.sqlite3)数据一键迁移到当前 MySQL。

用法（服务器上）:
    cd /home/ubuntu/Stiding_System
    source .venv/bin/activate
    export DJANGO_SETTINGS_MODULE=project.settings.prod
    python tools/migrate_sqlite_to_mysql.py

流程: 清空 MySQL 中本项目各表 -> 按依赖顺序从 SQLite 逐表拷贝(保持主键 id)。
依赖: 标准库 sqlite3 + 已安装的 pymysql(当前 settings 已配置)。
"""
import os
import sqlite3
import sys

import django
import pymysql

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings.prod")
django.setup()

SQLITE_PATH = os.path.join(BASE_DIR, "project", "db.sqlite3")

# 表与列（按依赖顺序；列与 Django 模型字段一一对应）
TABLES = [
    # (目标表名, [列...], 来源表名或 None=同名)
    ("auth_user", ["id", "password", "last_login", "is_superuser", "username",
                   "first_name", "last_name", "email", "is_staff", "is_active",
                   "date_joined"], None),
    ("scheduler_group", ["id", "name"], None),
    ("scheduler_role", ["id", "name"], None),
    ("scheduler_userprofile", ["id", "user_id", "group_id", "role"], None),
    ("scheduler_team", ["id", "group_id", "name", "daily_headcount",
                        "role_reqs", "rest_block", "min_shift_target", "exempt_names",
                        "person_overrides"], None),
    ("scheduler_person", ["id", "name", "team_id", "default_shift", "worked_so_far",
                          "required_shifts", "is_active"], None),
    ("scheduler_person_roles", ["id", "person_id", "role_id"], None),
    ("scheduler_schedule", ["id", "team_id", "year", "month", "start_date", "days",
                            "shifts", "daily_total", "role_reqs",
                            "min_shift_target", "exempt_names", "rest_block",
                            "person_overrides", "worker_snapshot", "status", "message",
                            "diagnostics", "single_rest", "rest_run_violations",
                            "created_at"], None),
    ("scheduler_assignment", ["id", "schedule_id", "person_id", "day", "shift", "role"], None),
]


def main() -> None:
    from django.conf import settings
    db = settings.DATABASES["default"]
    conn = pymysql.connect(
        host=db.get("HOST", "localhost"), port=int(db.get("PORT", 3306)),
        user=db.get("USER", ""), password=db.get("PASSWORD", ""),
        database=db.get("NAME", ""), charset="utf8mb4",
    )
    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row

    with conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0;")
        for table, cols, _ in TABLES:
            cur.execute(f"DELETE FROM `{table}`;")
        conn.commit()

        for table, cols, _ in TABLES:
            rows = src.execute(f"SELECT {', '.join(cols)} FROM `{table}`").fetchall()
            if not rows:
                print(f"{table}: 0 行")
                continue
            placeholders = ", ".join(["%s"] * len(cols))
            sql = f"INSERT INTO `{table}` ({', '.join(cols)}) VALUES ({placeholders})"
            data = [tuple(r[c] for c in cols) for r in rows]
            cur.executemany(sql, data)
            conn.commit()
            print(f"{table}: {len(rows)} 行")
        cur.execute("SET FOREIGN_KEY_CHECKS=1;")
    src.close()
    conn.close()
    print("迁移完成 ✓")


if __name__ == "__main__":
    if not os.path.exists(SQLITE_PATH):
        print(f"未找到旧 SQLite 文件: {SQLITE_PATH}")
        sys.exit(1)
    main()
