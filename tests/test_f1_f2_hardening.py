# F1：旧草稿/adjustment 确认的归档门禁与 Team 锁；F2：排班记录 Admin 只读 验收测试。
import datetime
import threading
from unittest import mock

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler import drafting
from apps.scheduler.drafting import (
    archive_period,
    confirm_schedule_draft,
    create_schedule_draft,
    generate_and_apply,
)
from apps.scheduler.models import ScheduleBatch, ScheduleRecord

User = get_user_model()
OLD_PERIOD = (datetime.date(2026, 8, 25), datetime.date(2026, 9, 24))  # 已结束


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class F1FixtureMixin:
    @classmethod
    def _build(cls):
        cls.super_user = make_user("f1_super")
        cls.org = OrganizationUnit.objects.create(name="甲队", code="F1A")
        cls.admin_a = make_user("f1_admin", role="admin", organization=cls.org)
        cls.team = Team.objects.create(organization=cls.org, name="检修班", code="F1T1")
        cls.config = cls.team.shift_configs.get(shift_name="early")
        cls.config.daily_worker_count = 2
        cls.config.required_shifts = 2
        cls.config.is_active = True
        cls.config.save(update_fields=["daily_worker_count", "required_shifts", "is_active"])
        cls.employees = [
            Employee.objects.create(
                employee_no=f"F1{i:02d}", name=f"门禁{i}", team=cls.team,
                shift_config=cls.config, min_required_shifts=2,
            )
            for i in range(1, 4)
        ]

    @classmethod
    def _generate(cls, period_start, user=None):
        outcome = generate_and_apply(
            cls.config, user or cls.super_user, anchor=period_start, time_limit_seconds=60
        )
        assert outcome["applied"], outcome["errors"]
        return outcome

    @staticmethod
    def _snapshot(start, end):
        return list(
            ScheduleRecord.objects.filter(work_date__range=(start, end))
            .order_by("id")
            .values_list("id", "employee_id", "work_date", "shift_type", "status", "is_active", "schedule_batch_id")
        )

    @staticmethod
    def _batch_snapshot():
        return list(
            ScheduleBatch.objects.order_by("id").values_list("batch_id", "kind", "status")
        )


class PeriodDraftArchiveGateTests(F1FixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def test_period_draft_confirm_rejected_on_archived_period(self):
        # 构造覆盖已归档周期的 period 草稿（服务层直接写草稿，不经过视图）
        cls = type(self)
        cls._generate(OLD_PERIOD[0], cls.super_user)
        archive_period(cls.team, *OLD_PERIOD, cls.super_user)
        draft = create_schedule_draft(cls.config, cls.super_user, anchor=OLD_PERIOD[0],
                                      time_limit_seconds=60)["draft"]
        self.assertIsNotNone(draft)
        records_before = cls._snapshot(*OLD_PERIOD)
        batches_before = cls._batch_snapshot()
        result = confirm_schedule_draft(draft.batch_id, cls.super_user)
        self.assertFalse(result["confirmed"])
        self.assertTrue(any("已归档" in e for e in result["errors"]), result["errors"])
        self.assertEqual(cls._snapshot(*OLD_PERIOD), records_before)   # 记录逐条不变
        self.assertEqual(cls._batch_snapshot(), batches_before)        # 批次（含本草稿）状态不变
        draft.refresh_from_db()
        self.assertEqual(draft.status, ScheduleBatch.Status.DRAFT)
        self.assertFalse(
            ScheduleRecord.objects.filter(
                work_date__range=OLD_PERIOD, status=ScheduleRecord.Status.CONFIRMED, is_active=True
            ).exists()
        )

    def test_draft_history_view_still_available(self):
        cls = type(self)
        cls._generate(OLD_PERIOD[0], cls.super_user)
        archive_period(cls.team, *OLD_PERIOD, cls.super_user)
        draft = create_schedule_draft(cls.config, cls.super_user, anchor=OLD_PERIOD[0],
                                      time_limit_seconds=60)["draft"]
        self.client.force_login(cls.super_user)
        page = self.client.get(
            reverse("scheduler:schedule_draft_detail", args=[draft.batch_id])
        )
        self.assertEqual(page.status_code, 200)          # 草稿历史查看能力保留
        self.assertContains(page, self.employees[0].name)


class AdjustmentConfirmGateTests(F1FixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def _make_adjustment_draft(self, work_date, employee):
        # 直接构造遗留 adjustment 草稿（P2 起不再创建此类批次）
        return ScheduleBatch.objects.create(
            organization=self.org, team=self.team, shift_config=self.config,
            period_start=work_date, period_end=work_date, mode="adjustment",
            daily_worker_count=1, position_requirements={},
            employee_targets={str(employee.pk): 1}, employee_qualifications={},
            payload=[{
                "employee_id": employee.pk,
                "work_date": work_date.isoformat(),
                "shift_type": ScheduleRecord.ShiftType.EARLY,
                "position": "",
            }],
            summary={"kind": ScheduleBatch.Kind.ADJUSTMENT},
            kind=ScheduleBatch.Kind.ADJUSTMENT, status=ScheduleBatch.Status.DRAFT,
            created_by=self.super_user, confirmed_by=self.super_user,
        )

    def test_adjustment_confirm_rejected_on_archived_period(self):
        self._generate(OLD_PERIOD[0])
        archive_period(self.team, *OLD_PERIOD, self.super_user)
        day = OLD_PERIOD[0] + datetime.timedelta(days=1)
        draft = self._make_adjustment_draft(day, self.employees[2])
        records_before = self._snapshot(*OLD_PERIOD)
        batches_before = self._batch_snapshot()
        result = confirm_schedule_draft(draft.batch_id, self.super_user)
        self.assertFalse(result["confirmed"])
        self.assertTrue(any("已归档" in e for e in result["errors"]), result["errors"])
        self.assertEqual(self._snapshot(*OLD_PERIOD), records_before)
        self.assertEqual(self._batch_snapshot(), batches_before)
        draft.refresh_from_db()
        self.assertEqual(draft.status, ScheduleBatch.Status.DRAFT)

    def test_adjustment_confirm_rejected_on_replaced_period(self):
        # historical：周期已被新版本替换（无活跃行、仅剩 superseded）→ 拒绝
        self._generate(OLD_PERIOD[0])
        ScheduleRecord.objects.filter(work_date__range=OLD_PERIOD).update(
            status=ScheduleRecord.Status.SUPERSEDED
        )
        ScheduleBatch.objects.filter(
            team=self.team, kind=ScheduleBatch.Kind.PERIOD
        ).update(status=ScheduleBatch.Status.SUPERSEDED)  # 版本头同步作废 → historical
        day = OLD_PERIOD[0] + datetime.timedelta(days=2)
        draft = self._make_adjustment_draft(day, self.employees[2])
        records_before = self._snapshot(*OLD_PERIOD)
        result = confirm_schedule_draft(draft.batch_id, self.super_user)
        self.assertFalse(result["confirmed"])
        self.assertTrue(any("已被新版本替换" in e for e in result["errors"]), result["errors"])
        self.assertEqual(self._snapshot(*OLD_PERIOD), records_before)

    def test_adjustment_confirm_allowed_on_ended_but_unarchived_period(self):
        # 规则：已结束但尚未归档的周期仍按既定规则允许人工维护
        self._generate(OLD_PERIOD[0])
        before_count = ScheduleRecord.objects.filter(
            work_date__range=OLD_PERIOD, status=ScheduleRecord.Status.CONFIRMED, is_active=True
        ).count()
        day = OLD_PERIOD[1]  # 周期最后一天，仍属未归档周期
        # 该日该人先取消，避免与既有记录冲突
        existing = ScheduleRecord.objects.get(
            team=self.team, employee=self.employees[2], work_date=day
        )
        existing.is_active = False
        existing.save(update_fields=["is_active"])
        draft = self._make_adjustment_draft(day, self.employees[2])
        result = confirm_schedule_draft(draft.batch_id, self.super_user)
        self.assertTrue(result["confirmed"], result.get("errors"))
        draft.refresh_from_db()
        self.assertEqual(draft.status, ScheduleBatch.Status.CONFIRMED)
        self.assertTrue(
            ScheduleRecord.objects.filter(
                team=self.team, employee=self.employees[2], work_date=day,
                status=ScheduleRecord.Status.CONFIRMED, is_active=True,
            ).exists()
        )
        self.assertEqual(
            ScheduleRecord.objects.filter(
                work_date__range=OLD_PERIOD, status=ScheduleRecord.Status.CONFIRMED, is_active=True
            ).count(),
            before_count,
        )


class ConfirmTeamLockTests(F1FixtureMixin, TransactionTestCase):
    # 真实数据库锁验证：确认流程必须获取 Team 行锁（持锁方未释放时确认应等待并最终报锁等待超时）
    def setUp(self):
        self._build()

    def test_confirm_acquires_team_row_lock(self):
        self._generate(OLD_PERIOD[0], self.super_user)
        draft = create_schedule_draft(self.config, self.super_user, anchor=OLD_PERIOD[0],
                                      time_limit_seconds=60)["draft"]
        team_id = self.team.pk
        holder_ready = threading.Event()
        release_holder = threading.Event()

        def hold_team_lock():
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET SESSION innodb_lock_wait_timeout=30")
                    cursor.execute("START TRANSACTION")
                    cursor.execute("SELECT id FROM team WHERE id=%s FOR UPDATE", [team_id])
                    holder_ready.set()
                    release_holder.wait(timeout=30)
                    cursor.execute("ROLLBACK")
            finally:
                connections.close_all()

        holder = threading.Thread(target=hold_team_lock)
        holder.start()
        try:
            self.assertTrue(holder_ready.wait(timeout=10), "持锁线程未就绪")
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout=1")
            with self.assertRaises(Exception) as ctx:
                confirm_schedule_draft(draft.batch_id, self.super_user)
            # 若是锁等待超时，说明确实在等待 Team 行锁（未被绕过）
            self.assertIn("lock wait timeout", str(ctx.exception).lower())
        finally:
            release_holder.set()
            holder.join(timeout=30)
        draft.refresh_from_db()
        self.assertEqual(draft.status, ScheduleBatch.Status.DRAFT)  # 失败未改动批次


class ScheduleRecordAdminReadOnlyTests(F1FixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def setUp(self):
        self.client.force_login(self.super_user)

    def _request(self):
        from django.test import RequestFactory

        request = RequestFactory().get("/admin/scheduler/schedulerecord/")
        request.user = self.super_user
        return request

    def test_permissions_deny_all_writes(self):
        model_admin = django_admin.site._registry[ScheduleRecord]
        request = self._request()
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))
        self.assertEqual(model_admin.get_actions(request), {})

    def test_admin_http_endpoints_readonly(self):
        self._generate(OLD_PERIOD[0], self.super_user)
        record = ScheduleRecord.objects.filter(work_date__range=OLD_PERIOD).first()
        original_shift = record.shift_type
        changelist = reverse("admin:scheduler_schedulerecord_changelist")
        self.assertEqual(self.client.get(changelist).status_code, 200)      # 列表可查看
        add_page = reverse("admin:scheduler_schedulerecord_add")
        self.assertEqual(self.client.get(add_page).status_code, 403)        # 禁止新增
        change_url = reverse("admin:scheduler_schedulerecord_change", args=[record.pk])
        self.assertIn(self.client.get(change_url).status_code, (200, 403))  # 详情可查看（只读）
        self.assertEqual(self.client.post(change_url, {"shift_type": "night"}).status_code, 403)
        delete_url = reverse("admin:scheduler_schedulerecord_delete", args=[record.pk])
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)
        record.refresh_from_db()
        self.assertEqual(record.shift_type, original_shift)

    def test_other_model_admin_permissions_unchanged(self):
        request = self._request()
        employee_admin = django_admin.site._registry[Employee]
        self.assertTrue(employee_admin.has_add_permission(request))     # 其他模型权限未受影响
        self.assertTrue(employee_admin.has_change_permission(request))
