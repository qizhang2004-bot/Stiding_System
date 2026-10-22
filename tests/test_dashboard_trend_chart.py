# 问题四：首页排班人数趋势折线图的数据口径与权限隔离测试。
import datetime
import json
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.models import ScheduleBatch, ScheduleRecord
from apps.scheduler.selectors import period_range

User = get_user_model()


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class TrendFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("tr_super")
        cls.org_a = OrganizationUnit.objects.create(name="甲队", code="TR-A")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="TR-B")
        cls.admin_a = make_user("tr_admin_a", role="admin", organization=cls.org_a)
        cls.team_a = Team.objects.create(organization=cls.org_a, name="甲班", code="TR-TA")
        cls.team_b = Team.objects.create(organization=cls.org_b, name="乙班", code="TR-TB")
        cls.config_a = cls.team_a.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.config_b = cls.team_b.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.employees_a = [
            Employee.objects.create(
                employee_no=f"TRA{i}", name=f"甲{i}", team=cls.team_a,
                shift_config=cls.config_a, min_required_shifts=1,
            )
            for i in range(1, 5)
        ]
        cls.employees_b = [
            Employee.objects.create(
                employee_no=f"TRB{i}", name=f"乙{i}", team=cls.team_b,
                shift_config=cls.config_b, min_required_shifts=1,
            )
            for i in range(1, 3)
        ]
        cls.start, cls.end = period_range()
        cls.batch_a = cls._batch(cls.org_a, cls.team_a, cls.config_a)
        cls.batch_b = cls._batch(cls.org_b, cls.team_b, cls.config_b)

    @classmethod
    def _batch(cls, org, team, config):
        return ScheduleBatch.objects.create(
            organization=org, team=team, shift_config=config,
            period_start=cls.start, period_end=cls.end, mode="tight",
            daily_worker_count=2, position_requirements={}, employee_targets={},
            employee_qualifications={}, payload=[], summary={},
            kind=ScheduleBatch.Kind.PERIOD, status=ScheduleBatch.Status.CONFIRMED,
            created_by=cls.super_user, confirmed_by=cls.super_user,
        )

    def _record(self, batch, employee, day, shift_type, *, status=None, active=True):
        return ScheduleRecord.objects.create(
            schedule_batch_id=batch.batch_id, organization=batch.organization,
            team=employee.team, shift_config=employee.shift_config, employee=employee,
            work_date=day, shift_type=shift_type,
            status=status or ScheduleRecord.Status.CONFIRMED, is_active=active,
        )

    def _chart_payload(self, username):
        self.client.login(username=username, password="pw")
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        match = re.search(
            r'<script id="trend-data" type="application/json">(.*?)</script>', html, re.S
        )
        self.assertIsNotNone(match, "首页缺少趋势图数据")
        return json.loads(match.group(1))

class DashboardTrendTests(TrendFixtureMixin, TestCase):
    def test_labels_cover_whole_period_with_zero_days(self):
        # 普通管理员：即使整周期没有记录，也必须给出完整日期与 0 值折线
        payload = self._chart_payload("tr_admin_a")
        self.assertEqual(len(payload["labels"]), (self.end - self.start).days + 1)
        self.assertEqual(payload["labels"][0], f"{self.start:%m-%d}")
        self.assertEqual(payload["labels"][-1], f"{self.end:%m-%d}")
        self.assertEqual(len(payload["series"]), 4)
        self.assertTrue(all(value == 0 for value in payload["series"][0]["data"]))
        self.assertTrue(payload["empty"])
        # 超级管理员：全部队组都没有数据时不画全 0 折线，但日期轴必须完整
        super_payload = self._chart_payload("tr_super")
        self.assertEqual(len(super_payload["labels"]), len(payload["labels"]))
        self.assertEqual(super_payload["series"], [])
        self.assertTrue(super_payload["empty"])

    def test_shift_counts_and_daily_total(self):
        day1 = self.start
        day2 = self.start + datetime.timedelta(days=1)
        for employee in self.employees_a[:2]:
            self._record(self.batch_a, employee, day1, ScheduleRecord.ShiftType.EARLY)
        self._record(self.batch_a, self.employees_a[2], day1, ScheduleRecord.ShiftType.MIDDLE)
        self._record(self.batch_a, self.employees_a[3], day1, ScheduleRecord.ShiftType.NIGHT)
        self._record(self.batch_a, self.employees_a[0], day2, ScheduleRecord.ShiftType.NIGHT)
        payload = self._chart_payload("tr_admin_a")
        series = {item["name"]: item["data"] for item in payload["series"]}
        self.assertEqual(series["早班"][0], 2)
        self.assertEqual(series["中班"][0], 1)
        self.assertEqual(series["夜班"][0], 1)
        self.assertEqual(series["当日总人数"][0], 4)       # 2+1+1
        self.assertEqual(series["夜班"][1], 1)
        self.assertEqual(series["当日总人数"][1], 1)
        self.assertEqual(series["当日总人数"][2], 0)       # 无记录日期为 0

    def test_rest_superseded_inactive_and_draft_excluded(self):
        day = self.start
        self._record(self.batch_a, self.employees_a[0], day, ScheduleRecord.ShiftType.REST)
        self._record(self.batch_a, self.employees_a[1], day, ScheduleRecord.ShiftType.EARLY,
                     status=ScheduleRecord.Status.SUPERSEDED)
        self._record(self.batch_a, self.employees_a[2], day, ScheduleRecord.ShiftType.EARLY,
                     active=False)
        self._record(self.batch_a, self.employees_a[3], day, ScheduleRecord.ShiftType.EARLY,
                     status=ScheduleRecord.Status.DRAFT)
        payload = self._chart_payload("tr_admin_a")
        self.assertTrue(payload["empty"])
        for item in payload["series"]:
            self.assertTrue(all(value == 0 for value in item["data"]), item["name"])

    def test_duplicate_person_day_not_double_counted(self):
        day = self.start
        self._record(self.batch_a, self.employees_a[0], day, ScheduleRecord.ShiftType.EARLY)
        # 同一人同一天另一条有效记录（不同批次）——不得重复计数
        self._record(
            self._batch(self.org_a, self.team_a, self.config_a),
            self.employees_a[0], day, ScheduleRecord.ShiftType.EARLY,
        )
        payload = self._chart_payload("tr_admin_a")
        series = {item["name"]: item["data"] for item in payload["series"]}
        self.assertEqual(series["早班"][0], 1)
        self.assertEqual(series["当日总人数"][0], 1)

    def test_admin_sees_only_own_org_with_four_series(self):
        self._record(self.batch_a, self.employees_a[0], self.start, ScheduleRecord.ShiftType.EARLY)
        self._record(self.batch_b, self.employees_b[0], self.start, ScheduleRecord.ShiftType.EARLY)
        payload = self._chart_payload("tr_admin_a")
        names = [item["name"] for item in payload["series"]]
        self.assertEqual(names, ["早班", "中班", "夜班", "当日总人数"])
        self.assertFalse(payload["is_super"])
        orgs = {item["org"] for item in payload["series"]}
        self.assertEqual(orgs, {"甲队"})                    # 不包含乙队数据
        self.assertEqual(payload["series"][0]["data"][0], 1)

    def test_super_sees_org_plus_shift_series(self):
        self._record(self.batch_a, self.employees_a[0], self.start, ScheduleRecord.ShiftType.EARLY)
        self._record(self.batch_b, self.employees_b[0], self.start, ScheduleRecord.ShiftType.EARLY)
        payload = self._chart_payload("tr_super")
        names = [item["name"] for item in payload["series"]]
        self.assertTrue(payload["is_super"])
        self.assertIn("甲队早班", names)
        self.assertIn("甲队中班", names)
        self.assertIn("乙队早班", names)
        self.assertNotIn("早班", names)                     # 不允许把不同队组混成一条线
        by_name = {item["name"]: item for item in payload["series"]}
        self.assertEqual(by_name["甲队早班"]["data"][0], 1)
        self.assertEqual(by_name["乙队早班"]["data"][0], 1)
        self.assertEqual(by_name["甲队中班"]["data"][0], 0)

    def test_chart_card_and_script_rendered(self):
        self.client.login(username="tr_admin_a", password="pw")
        response = self.client.get(reverse("core:dashboard"))
        html = response.content.decode()
        self.assertIn("排班人数趋势", html)
        self.assertIn('id="trend-chart"', html)
        self.assertIn("trend_chart.js", html)
        self.assertIn("当前周期", html)


class ChartTooltipTests(TrendFixtureMixin, TestCase):
    # 图例不可点击隐藏折线；悬停提示所需数据齐备（浏览器交互无法自动化，见报告说明）
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.js_source = (Path(settings.BASE_DIR) / "static/js/trend_chart.js").read_text(
            encoding="utf-8"
        )

    def test_legend_has_no_toggle_behaviour(self):
        self.assertNotIn("is-hidden", self.js_source)
        self.assertNotIn('.addEventListener("click"', self.js_source)
        self.assertNotIn("button.addEventListener", self.js_source)
        # 图例条目使用非交互元素
        self.assertIn('document.createElement("span")', self.js_source)

    def test_tooltip_container_and_hover_handlers(self):
        self.client.login(username="tr_admin_a", password="pw")
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn('id="trend-tooltip"', html)
        self.assertIn("trend-tooltip", self.js_source)
        self.assertIn("pointermove", self.js_source)
        self.assertIn("pointerleave", self.js_source)
        # 提示内容包含日期与人数
        self.assertIn("日期 ", self.js_source)
        self.assertIn(" 人", self.js_source)

    def test_tooltip_payload_has_date_org_shift_and_value(self):
        self._record(self.batch_a, self.employees_a[0], self.start, ScheduleRecord.ShiftType.EARLY)
        payload = self._chart_payload("tr_super")
        self.assertGreaterEqual(len(payload["labels"]), 30)
        for item in payload["series"]:
            self.assertIn("name", item)
            self.assertIn("org", item)
            self.assertIn("shift", item)
            self.assertEqual(len(item["data"]), len(payload["labels"]))
        self.assertTrue(all(item["org"] == "甲队" for item in payload["series"]))

    def test_admin_payload_covers_early_middle_night_and_total(self):
        payload = self._chart_payload("tr_admin_a")
        shifts = {item["shift"] for item in payload["series"]}
        self.assertEqual(shifts, {"early", "middle", "night", "total"})

    def test_zero_and_empty_values_present_in_payload(self):
        # 无数据时所有点均为 0（悬停仍能显示 0 人，而不是跳过日期）
        payload = self._chart_payload("tr_admin_a")
        self.assertEqual(len(payload["series"]), 4)
        for item in payload["series"]:
            self.assertEqual(item["data"], [0] * len(payload["labels"]))
