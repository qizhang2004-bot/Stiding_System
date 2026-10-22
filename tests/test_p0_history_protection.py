# P0：历史写入保护 + 边界查询一致性 验收测试。
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.drafting import change_schedule_record, remove_schedule_record
from apps.scheduler.models import ScheduleBatch, ScheduleRecord
from apps.scheduler.planning import build_rest_problem
from apps.scheduler.selectors import period_range
from apps.scheduler.services import create_schedule_record, new_schedule_batch_id

User = get_user_model()
DAY = datetime.date(2026, 9, 26)


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class HistoryProtectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("h_super")
        cls.org = OrganizationUnit.objects.create(name="甲队", code="HIST-A")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="HIST-B")
        cls.admin_a = make_user("h_admin_a", role="admin", organization=cls.org)
        cls.member_a = make_user("h_member_a", role="member", organization=cls.org)
        cls.team = Team.objects.create(organization=cls.org, name="检修班", code="HT1")
        cls.team_b = Team.objects.create(organization=cls.org_b, name="乙班", code="HTB")
        for team in (cls.team, cls.team_b):
            for name in ("early", "middle", "night"):
                cfg = team.shift_configs.get(shift_name=name)
                cfg.is_active = True
                cfg.daily_worker_count = 2
                cfg.required_shifts = 2
                cfg.save(update_fields=["is_active", "daily_worker_count", "required_shifts"])
        cls.config = cls.team.shift_configs.get(shift_name="early")
        cls.emp = Employee.objects.create(
            employee_no="H001", name="历史甲", team=cls.team, shift_config=cls.config,
            min_required_shifts=2,
        )
        cls.emp2 = Employee.objects.create(
            employee_no="H002", name="历史乙", team=cls.team, shift_config=cls.config,
            min_required_shifts=2,
        )
        # 历史版本记录（superseded）
        cls.historic = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=cls.org, team=cls.team, shift_config=cls.config,
            employee=cls.emp, work_date=DAY,
            shift_type=ScheduleRecord.ShiftType.EARLY, position="电工",
            status=ScheduleRecord.Status.SUPERSEDED,
        )
        # 当前有效记录（同周期，另一位职工）——使该周期“仍有当前有效排班”
        cls.current = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=cls.org, team=cls.team, shift_config=cls.config,
            employee=cls.emp2, work_date=DAY,
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )

    # ---- 改班：历史版本不可改 ----
    def test_cannot_change_superseded_record(self):
        before = ScheduleRecord.objects.filter(team=self.team).count()
        result = change_schedule_record(self.historic, "middle", self.admin_a)
        self.assertFalse(result["changed"])
        self.assertIn("历史版本", result["errors"][0])
        self.historic.refresh_from_db()
        self.assertEqual(self.historic.shift_type, ScheduleRecord.ShiftType.EARLY)
        self.assertEqual(self.historic.status, ScheduleRecord.Status.SUPERSEDED)
        self.assertTrue(self.historic.is_active)
        self.assertEqual(ScheduleRecord.objects.filter(team=self.team).count(), before)  # 未新建记录
        self.assertFalse(
            ScheduleRecord.objects.filter(
                employee=self.emp, status=ScheduleRecord.Status.CONFIRMED, is_active=True
            ).exists()
        )  # 未产生新的当前有效记录

    def test_cannot_change_inactive_record(self):
        record = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org, team=self.team, shift_config=self.config,
            employee=self.emp, work_date=DAY + datetime.timedelta(days=1),
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        remove_schedule_record(record, self.admin_a)
        result = change_schedule_record(record, "night", self.admin_a)
        self.assertFalse(result["changed"])
        self.assertIn("已被删除", result["errors"][0])

    # ---- 删除：历史版本不可删 ----
    def test_cannot_remove_superseded_record(self):
        result = remove_schedule_record(self.historic, self.admin_a)
        self.assertFalse(result["removed"])
        self.assertIn("历史版本", result["errors"][0])
        self.historic.refresh_from_db()
        self.assertTrue(self.historic.is_active)
        self.assertEqual(self.historic.status, ScheduleRecord.Status.SUPERSEDED)

    def test_cannot_remove_draft_record(self):
        draft_row = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org, team=self.team, shift_config=self.config,
            employee=self.emp, work_date=DAY + datetime.timedelta(days=2),
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.DRAFT,
        )
        result = remove_schedule_record(draft_row, self.admin_a)
        self.assertFalse(result["removed"])
        draft_row.refresh_from_db()
        self.assertTrue(draft_row.is_active)

    # ---- 添加：已被替换的历史周期不可写入 ----
    def test_cannot_add_into_replaced_period(self):
        from apps.scheduler.drafting import add_manual_schedule

        # 把该周期最后一条当前有效记录也变为历史 → 该周期成为“已被替换的历史周期”
        self.current.status = ScheduleRecord.Status.SUPERSEDED
        self.current.save(update_fields=["status"])
        before_records = ScheduleRecord.objects.filter(team=self.team).count()
        before_batches = ScheduleBatch.objects.count()
        result = add_manual_schedule(self.config, DAY, self.emp, self.admin_a)
        self.assertTrue(result["errors"])
        self.assertIn("已不是当前有效排班", result["errors"][0])
        self.assertEqual(ScheduleRecord.objects.filter(team=self.team).count(), before_records)
        self.assertEqual(ScheduleBatch.objects.count(), before_batches)

    def test_add_into_empty_period_still_allowed(self):
        from apps.scheduler.drafting import add_manual_schedule

        empty_day = datetime.date(2026, 11, 5)  # 该周期无任何记录
        result = add_manual_schedule(self.config, empty_day, self.emp, self.admin_a)
        self.assertEqual(result["errors"], [])
        self.assertTrue(
            ScheduleRecord.objects.filter(
                employee=self.emp, work_date=empty_day, status=ScheduleRecord.Status.CONFIRMED,
                is_active=True,
            ).exists()
        )

    # ---- HTTP 层：历史版本不可通过接口改动 ----
    def test_http_change_and_remove_on_superseded_rejected(self):
        self.client.force_login(self.admin_a)
        change = self.client.post(
            reverse("scheduler:shift_board_change", args=[self.historic.pk]),
            {"target_shift": "middle"},
        )
        self.assertEqual(change.status_code, 302)
        messages = [str(m) for m in change.wsgi_request._messages]
        self.assertTrue(any("历史版本" in m for m in messages), messages)
        remove = self.client.post(reverse("scheduler:shift_board_remove", args=[self.historic.pk]))
        self.assertEqual(remove.status_code, 302)
        messages = [str(m) for m in remove.wsgi_request._messages]
        self.assertTrue(any("历史版本" in m for m in messages), messages)
        self.historic.refresh_from_db()
        self.assertTrue(self.historic.is_active)
        self.assertEqual(self.historic.shift_type, ScheduleRecord.ShiftType.EARLY)

    # ---- 权限：跨队组仍 404 ----
    def test_cross_org_change_and_remove_404(self):
        admin_b = make_user("h_admin_b", role="admin", organization=self.org_b)
        self.client.force_login(admin_b)
        self.assertEqual(
            self.client.post(
                reverse("scheduler:shift_board_change", args=[self.historic.pk]),
                {"target_shift": "middle"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("scheduler:shift_board_remove", args=[self.historic.pk])
            ).status_code,
            404,
        )

    def test_member_cannot_change_or_remove(self):
        self.client.force_login(self.member_a)
        self.assertEqual(
            self.client.post(
                reverse("scheduler:shift_board_change", args=[self.current.pk]),
                {"target_shift": "middle"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("scheduler:shift_board_remove", args=[self.current.pk])
            ).status_code,
            403,
        )
        self.current.refresh_from_db()
        self.assertEqual(self.current.shift_type, ScheduleRecord.ShiftType.EARLY)


class BoundaryQueryConsistencyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="甲队", code="BND-A")
        cls.team = Team.objects.create(organization=cls.org, name="边界班", code="BT1")
        cls.config = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.config.is_active = True
        cls.config.daily_worker_count = 1
        cls.config.required_shifts = 1
        cls.config.save(update_fields=["is_active", "daily_worker_count", "required_shifts"])
        cls.emp = Employee.objects.create(
            employee_no="B001", name="边界甲", team=cls.team, shift_config=cls.config,
            min_required_shifts=1,
        )
        cls.emp2 = Employee.objects.create(
            employee_no="B002", name="边界乙", team=cls.team, shift_config=cls.config,
            min_required_shifts=1,
        )

    def _boundary_record(self, employee, day, shift_type, status, is_active=True):
        return create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org, team=self.team, shift_config=self.config,
            employee=employee, work_date=day, shift_type=shift_type,
            status=status,
        ) if True else None

    def test_soft_deleted_boundary_record_ignored(self):
        start, end = period_range(datetime.date(2026, 9, 26))
        prev_day = start - datetime.timedelta(days=1)
        record = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org, team=self.team, shift_config=self.config,
            employee=self.emp, work_date=prev_day,
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        info = build_rest_problem(self.config, anchor=datetime.date(2026, 9, 26))
        self.assertEqual(info["prev_work"].get(self.emp.pk), True)  # 未删除时参与边界
        record.is_active = False
        record.save(update_fields=["is_active"])
        info = build_rest_problem(self.config, anchor=datetime.date(2026, 9, 26))
        self.assertIsNone(info["prev_work"].get(self.emp.pk))  # 软删除后不再参与边界判定

    def test_superseded_boundary_record_still_ignored(self):
        start, end = period_range(datetime.date(2026, 9, 26))
        next_day = end + datetime.timedelta(days=1)
        create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org, team=self.team, shift_config=self.config,
            employee=self.emp2, work_date=next_day,
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.SUPERSEDED,
        )
        info = build_rest_problem(self.config, anchor=datetime.date(2026, 9, 26))
        self.assertIsNone(info["next_work"].get(self.emp2.pk))
