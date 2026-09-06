# -*- coding: utf-8 -*-
"""django-jazzmin 后台主题配置：层级菜单导航（队组->班组->人员->排班->明细）。"""

JAZZMIN_SETTINGS = {
    "site_title": "井下排班系统 · 后台管理",
    "site_header": "井下排班系统",
    "site_brand": "⛏ 排班后台",
    "welcome_sign": "欢迎使用井下排班系统管理后台",
    "copyright": "Stiding System",
    "language_chooser": False,
    "show_sidebar": True,
    "navigation_expanded": True,
    "custom_links": {
        "scheduler": [
            {
                "name": "层级导航",
                "url": "#",
                "icon": "fas fa-sitemap",
                "children": [
                    {"name": "① 队组（内联班组）", "url": "admin:scheduler_group_changelist",
                     "icon": "fas fa-building"},
                    {"name": "② 班组（内联人员）", "url": "admin:scheduler_team_changelist",
                     "icon": "fas fa-user-friends"},
                    {"name": "③ 人员", "url": "admin:scheduler_person_changelist",
                     "icon": "fas fa-user"},
                    {"name": "④ 排班记录", "url": "admin:scheduler_schedule_changelist",
                     "icon": "fas fa-calendar-alt"},
                    {"name": "⑤ 排班明细", "url": "admin:scheduler_assignment_changelist",
                     "icon": "fas fa-list"},
                    {"name": "⑥ 岗位", "url": "admin:scheduler_role_changelist",
                     "icon": "fas fa-hard-hat"},
                ],
            },
            {
                "name": "返回前台首页",
                "url": "/",
                "icon": "fas fa-home",
            },
        ],
        "auth": [
            {
                "name": "返回前台首页",
                "url": "/",
                "icon": "fas fa-home",
            },
        ],
    },
    "icons": {
        "auth": "fas fa-users-cog",
        "auth.user": "fas fa-user",
        "auth.group": "fas fa-users",
        "scheduler": "fas fa-clock",
        "scheduler.group": "fas fa-building",
        "scheduler.team": "fas fa-user-friends",
        "scheduler.person": "fas fa-user",
        "scheduler.role": "fas fa-hard-hat",
        "scheduler.schedule": "fas fa-calendar-alt",
        "scheduler.assignment": "fas fa-list",
        "scheduler.userprofile": "fas fa-id-card",
    },
    "topmenu_links": [
        {"name": "前台首页", "url": "/", "new_window": False},
    ],
    "usermenu_links": [
        {"name": "前台首页", "url": "/", "new_window": False},
    ],
}

JAZZMIN_UI_TWEAKS = {
    "navbar_small_text": False,
    "footer_small_text": False,
    "body_small_text": False,
    "brand_small_text": False,
    "brand_colour": "navbar-primary",
    "accent": "accent-primary",
    "navbar": "navbar-dark",
    "no_navbar_border": False,
    "navbar_fixed": True,
    "layout_boxed": False,
    "footer_fixed": False,
    "sidebar_fixed": True,
    "sidebar": "sidebar-dark-primary",
    "sidebar_nav_small_text": False,
    "sidebar_disable_expand": False,
    "sidebar_nav_child_indent": True,
    "sidebar_nav_compact_style": False,
    "sidebar_nav_legacy_style": False,
    "sidebar_nav_flat_style": False,
    "theme": "default",
    "dark_mode_theme": None,
    "button_classes": {
        "primary": "btn-primary",
        "secondary": "btn-secondary",
        "info": "btn-info",
        "warning": "btn-warning",
        "danger": "btn-danger",
        "success": "btn-success",
    },
}
