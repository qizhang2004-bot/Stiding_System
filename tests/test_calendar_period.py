# 排班周期（25 日~次月 24 日）与个人排班日历 验收测试。
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.models import ScheduleRecord
from apps.scheduler.selectors import period_range
from apps.scheduler.services import create_schedule_record, new_schedule_batch_id

User = get_user_model()


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class PeriodBoundaryTests(TestCase):
    # 周期起止必须严格为 25 日 ~ 次月 24 日（跨月/跨年/2 月/闰年）
    def test_cross_month(self):
        self.assertEqual(
            period_range(datetime.date(2026, 9, 25)),
            (datetime.date(2026, 9, 25), datetime.date(2026, 10, 24)),
        )

    def test_cross_year(self):
        self.assertEqual(
            period_range(datetime.date(2026, 12, 25)),
            (datetime.date(2026, 12, 25), datetime.date(2027, 1, 24)),
        )

    def test_leap_february(self):
        self.assertEqual(
            period_range(datetime.date(2024, 1, 25)),
            (datetime.date(2024, 1, 25), datetime.date(2024, 2, 24)),
        )

    def test_non_leap_february(self):
        self.assertEqual(
            period_range(datetime.date(2023, 1, 25)),
            (datetime.date(2023, 1, 25), datetime.date(2023, 2, 24)),
        )

    def test_anchor_inside_period(self):
        for day in (25, 26, 30):
            self.assertEqual(
                period_range(datetime.date(2026, 9, day))[0], datetime.date(2026, 9, 25)
            )
        for day in (1, 15, 24):
            self.assertEqual(
                period_range(datetime.date(2026, 10, day)),
                (datetime.date(2026, 9, 25), datetime.date(2026, 10, 24)),
            )
        self.assertEqual(
            period_range(datetime.date(2026, 10, 25))[0], datetime.date(2026, 10, 25)
        )


class EmployeeScheduleCalendarTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("cal_super")
        cls.org = OrganizationUnit.objects.create(name="甲队", code="ORGA")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="ORGB")
        cls.admin_a = make_user("cal_admin_a", role="admin", organization=cls.org)
        cls.member = make_user("cal_member", role="member", organization=cls.org)
        cls.team = Team.objects.create(organization=cls.org, name="甲一班", code="TA")
        cls.config = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.emp = Employee.objects.create(
            employee_no="C001", name="日历甲", team=cls.team, shift_config=cls.config,
            position="电工", min_required_shifts=2,
        )
        cls.day = datetime.date(2026, 9, 26)
        cls.record = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=cls.org, team=cls.team, shift_config=cls.config,
            employee=cls.emp, work_date=cls.day,
            shift_type=ScheduleRecord.ShiftType.EARLY, position="电工",
            status=ScheduleRecord.Status.CONFIRMED,
        )
        cls.url = reverse("personnel:employee_calendar", args=[cls.emp.pk])

    def test_default_period_grid(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(self.url, {"period": "2026-09"})
        self.assertEqual(response.status_code, 200)
        days = response.context["days"]
        self.assertEqual(response.context["period_start"], datetime.date(2026, 9, 25))
        self.assertEqual(response.context["period_end"], datetime.date(2026, 10, 24))
        self.assertEqual(len(days), 30)
        self.assertEqual(len({d["date"] for d in days}), 30)  # 无重复
        self.assertEqual(
            [d["date"] for d in days],
            [datetime.date(2026, 9, 25) + datetime.timedelta(days=i) for i in range(30)],
        )
        content = response.content.decode()
        self.assertIn("早班", content)
        self.assertIn("电工", content)
        self.assertNotIn("实际状态", content)
        self.assertNotIn("请假", content)

    def test_record_shown_on_its_day_only(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(self.url, {"period": "2026-09"})
        with_record = [d for d in response.context["days"] if d["record"]]
        self.assertEqual(len(with_record), 1)
        self.assertEqual(with_record[0]["date"], self.day)
        self.assertEqual(with_record[0]["shift_type"], "early")

    def test_previous_and_next_period_switch(self):
        self.client.force_login(self.admin_a)
        prev = self.client.get(self.url, {"period": "2026-08"})
        self.assertEqual(prev.context["period_start"], datetime.date(2026, 8, 25))
        self.assertEqual(prev.context["period_end"], datetime.date(2026, 9, 24))
        nxt = self.client.get(self.url, {"period": "2026-10"})
        self.assertEqual(nxt.context["period_start"], datetime.date(2026, 10, 25))

    def test_february_leap_period(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(self.url, {"period": "2024-01"})
        self.assertEqual(
            (response.context["period_start"], response.context["period_end"]),
            (datetime.date(2024, 1, 25), datetime.date(2024, 2, 24)),
        )
        self.assertEqual(len(response.context["days"]), 31)

    def test_calendar_has_no_delete_entry(self):
        # 个人日历不再提供删除按钮；调整当天状态应使用“改班”
        self.client.force_login(self.admin_a)
        response = self.client.get(self.url, {"period": "2026-09"})
        html = response.content.decode()
        self.assertNotIn(
            reverse("scheduler:shift_board_remove", args=[self.record.pk]), html
        )
        self.assertNotIn("cal-remove", html)
        self.assertNotIn(">删除<", html)
        self.assertIn(
            reverse("scheduler:shift_board_change", args=[self.record.pk]), html
        )
        self.assertIn("改班", html)

    def test_admin_can_change_to_rest_from_calendar(self):
        # 通过改班把当天状态改为休息：记录保留、状态更新、页面刷新后仍正确
        self.client.force_login(self.admin_a)
        response = self.client.post(
            reverse("scheduler:shift_board_change", args=[self.record.pk]),
            {"target_shift": ScheduleRecord.ShiftType.REST, "next": self.url},
        )
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertTrue(self.record.is_active)
        self.assertEqual(self.record.shift_type, ScheduleRecord.ShiftType.REST)
        page = self.client.get(self.url, {"period": "2026-09"})
        self.assertEqual(page.status_code, 200)
        with_record = [d for d in page.context["days"] if d["record"]]
        self.assertEqual(len(with_record), 1)
        self.assertEqual(with_record[0]["shift_type"], "rest")

    def test_remove_endpoint_still_available_for_board(self):
        # 后端删除能力保留（班次看板仍使用），仅个人日历不再提供入口
        self.client.force_login(self.admin_a)
        response = self.client.post(
            reverse("scheduler:shift_board_remove", args=[self.record.pk]),
            {"next": self.url},
        )
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertFalse(self.record.is_active)  # 软删除，记录保留
        self.assertEqual(ScheduleRecord.objects.filter(pk=self.record.pk).count(), 1)

    def test_member_cannot_delete(self):
        self.client.force_login(self.member)
        response = self.client.post(reverse("scheduler:shift_board_remove", args=[self.record.pk]))
        self.assertEqual(response.status_code, 403)
        self.record.refresh_from_db()
        self.assertTrue(self.record.is_active)
