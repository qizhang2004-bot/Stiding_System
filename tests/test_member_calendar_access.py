# 队员从「个人信息看板」点击人员进入只读排班日历：权限、数据隔离、只读性与空状态。
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.models import ScheduleBatch, ScheduleRecord
from apps.scheduler.services import new_schedule_batch_id

User = get_user_model()
PERIOD = datetime.date(2026, 9, 25)


def make_user(username, *, role="member", organization=None, org_code=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class MemberCalendarAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("mc_super", role="super")
        cls.org_a = OrganizationUnit.objects.create(name="甲队", code="MC-A")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="MC-B")
        cls.admin_a = make_user("mc_admin_a", role="admin", organization=cls.org_a)
        cls.member_a = make_user("mc_member_a", role="member", organization=cls.org_a)
        cls.member_b = make_user("mc_member_b", role="member", organization=cls.org_b)

        cls.team_a = Team.objects.create(organization=cls.org_a, name="甲班", code="MC-TA")
        cls.team_b = Team.objects.create(organization=cls.org_b, name="乙班", code="MC-TB")
        cls.config_a = cls.team_a.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.config_b = cls.team_b.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        for config in (cls.config_a, cls.config_b):
            config.daily_worker_count = 2
            config.is_active = True
            config.save(update_fields=["daily_worker_count", "is_active"])

        cls.emp_a1 = Employee.objects.create(
            employee_no="MC-A1", name="甲一", team=cls.team_a, shift_config=cls.config_a,
            position="电工", min_required_shifts=1,
        )
        cls.emp_a2 = Employee.objects.create(
            employee_no="MC-A2", name="甲二", team=cls.team_a, shift_config=cls.config_a,
            min_required_shifts=1,
        )
        cls.emp_a3 = Employee.objects.create(
            employee_no="MC-A3", name="甲三", team=cls.team_a, shift_config=cls.config_a,
            min_required_shifts=1,
        )
        cls.emp_b1 = Employee.objects.create(
            employee_no="MC-B1", name="乙一", team=cls.team_b, shift_config=cls.config_b,
            min_required_shifts=1,
        )
        cls.batch = ScheduleBatch.objects.create(
            organization=cls.org_a, team=cls.team_a, shift_config=cls.config_a,
            period_start=PERIOD, period_end=PERIOD + datetime.timedelta(days=29),
            mode="tight", daily_worker_count=2, position_requirements={},
            employee_targets={}, employee_qualifications={}, payload=[], summary={},
            kind=ScheduleBatch.Kind.PERIOD, status=ScheduleBatch.Status.CONFIRMED,
            created_by=cls.super_user, confirmed_by=cls.super_user,
        )
        cls.record_a1 = ScheduleRecord.objects.create(
            schedule_batch_id=cls.batch.batch_id, organization=cls.org_a, team=cls.team_a,
            shift_config=cls.config_a, employee=cls.emp_a1,
            work_date=datetime.date(2026, 9, 26), shift_type=ScheduleRecord.ShiftType.EARLY,
            position="电工", status=ScheduleRecord.Status.CONFIRMED, is_active=True,
        )
        cls.record_a2 = ScheduleRecord.objects.create(
            schedule_batch_id=cls.batch.batch_id, organization=cls.org_a, team=cls.team_a,
            shift_config=cls.config_a, employee=cls.emp_a2,
            work_date=datetime.date(2026, 9, 27), shift_type=ScheduleRecord.ShiftType.MIDDLE,
            status=ScheduleRecord.Status.CONFIRMED, is_active=True,
        )
        cls.record_b1 = ScheduleRecord.objects.create(
            schedule_batch_id=new_schedule_batch_id(), organization=cls.org_b,
            team=cls.team_b, shift_config=cls.config_b, employee=cls.emp_b1,
            work_date=datetime.date(2026, 9, 26), shift_type=ScheduleRecord.ShiftType.NIGHT,
            status=ScheduleRecord.Status.CONFIRMED, is_active=True,
        )

    def setUp(self):
        self.client.login(username="mc_member_a", password="pw")

    def _calendar(self, employee, period="2026-09"):
        return self.client.get(
            reverse("personnel:employee_calendar", args=[employee.pk]), {"period": period}
        )

    # 1. 队员可从看板点击人员进入日历
    def test_board_links_to_calendar(self):
        response = self.client.get(reverse("personnel:person_board"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for employee in (self.emp_a1, self.emp_a2):
            url = reverse("personnel:employee_calendar", args=[employee.pk])
            self.assertIn(url, html)
            self.assertIn(f'title="查看 {employee.name} 的排班日历"', html)
        self.assertIn("person-link", html)
        self.assertNotIn(self.emp_b1.name, html)   # 其他队组人员不出现

    # 2. 不同人员显示各自真实记录
    def test_different_people_show_their_own_records(self):
        page_a1 = self._calendar(self.emp_a1)
        self.assertEqual(page_a1.status_code, 200)
        self.assertEqual(page_a1.context["employee"].pk, self.emp_a1.pk)
        self.assertEqual(page_a1.context["scheduled"], 1)
        recorded = [day for day in page_a1.context["days"] if day["record"]]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["date"], datetime.date(2026, 9, 26))
        self.assertEqual(recorded[0]["shift_type"], "early")
        self.assertContains(page_a1, "甲一")

        page_a2 = self._calendar(self.emp_a2)
        self.assertEqual(page_a2.context["employee"].pk, self.emp_a2.pk)
        self.assertEqual(page_a2.context["scheduled"], 1)
        recorded = [day for day in page_a2.context["days"] if day["record"]]
        self.assertEqual(recorded[0]["date"], datetime.date(2026, 9, 27))
        self.assertEqual(recorded[0]["shift_type"], "middle")
        self.assertContains(page_a2, "甲二")

    # 3. 无排班记录 → 空状态
    def test_employee_without_records_shows_empty_state(self):
        response = self._calendar(self.emp_a3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["scheduled"], 0)
        self.assertFalse(any(day["record"] for day in response.context["days"]))
        self.assertContains(response, "未排班")
        self.assertContains(response, "甲三")

    # 4. 越权：其他队组人员 404
    def test_member_cannot_view_other_org_employee(self):
        response = self._calendar(self.emp_b1)
        self.assertEqual(response.status_code, 404)

    def test_other_org_member_cannot_view_our_employee(self):
        self.client.login(username="mc_member_b", password="pw")
        self.assertEqual(self._calendar(self.emp_a1).status_code, 404)

    # 5. 只读：无编辑入口，且写接口 403
    def test_member_calendar_is_read_only(self):
        response = self._calendar(self.emp_a1)
        html = response.content.decode()
        self.assertFalse(response.context["can_edit"])
        self.assertNotIn("改班", html)
        self.assertNotIn("cal-remove", html)
        self.assertNotIn(
            reverse("scheduler:shift_board_change", args=[self.record_a1.pk]), html
        )
        self.assertNotIn(
            reverse("scheduler:shift_board_remove", args=[self.record_a1.pk]), html
        )
        # 直接调用写接口仍被拒绝（403），记录保持不变
        change = self.client.post(
            reverse("scheduler:shift_board_change", args=[self.record_a1.pk]),
            {"target_shift": "night"},
        )
        remove = self.client.post(
            reverse("scheduler:shift_board_remove", args=[self.record_a1.pk])
        )
        self.assertEqual(change.status_code, 403)
        self.assertEqual(remove.status_code, 403)
        self.record_a1.refresh_from_db()
        self.assertEqual(self.record_a1.shift_type, ScheduleRecord.ShiftType.EARLY)
        self.assertEqual(self.record_a1.status, ScheduleRecord.Status.CONFIRMED)
        self.assertTrue(self.record_a1.is_active)

    # 6. 管理员原有功能不受影响（可编辑）
    def test_admin_calendar_keeps_edit_controls(self):
        self.client.login(username="mc_admin_a", password="pw")
        response = self._calendar(self.emp_a1)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_edit"])
        self.assertContains(response, "改班")
        change = self.client.post(
            reverse("scheduler:shift_board_change", args=[self.record_a1.pk]),
            {"target_shift": "night"},
        )
        self.assertEqual(change.status_code, 302)
        self.record_a1.refresh_from_db()
        self.assertEqual(self.record_a1.shift_type, ScheduleRecord.ShiftType.NIGHT)

    # 7. 成员看板与日历的历史周期导航沿用现有实现
    def test_member_can_browse_other_periods(self):
        response = self._calendar(self.emp_a1, period="2026-08")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["period_start"], datetime.date(2026, 8, 25))
        self.assertEqual(response.context["period_end"], datetime.date(2026, 9, 24))
        self.assertEqual(response.context["scheduled"], 0)
        self.assertContains(response, "未排班")

    # 8. 超级管理员看板行为保持不变（重定向到职工管理），日历仍可访问
    def test_super_admin_board_behavior_unchanged(self):
        self.client.login(username="mc_super", password="pw")
        response = self.client.get(reverse("personnel:person_board"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("personnel:employee_list"))
        calendar = self._calendar(self.emp_b1)
        self.assertEqual(calendar.status_code, 200)
        self.assertTrue(calendar.context["can_edit"])
