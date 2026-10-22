# 职工详情“排班操作记录”：复用审计日志，记录真实操作前后状态，不反推历史。
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.auditlog.models import OperationLog
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.drafting import change_schedule_record, generate_and_apply
from apps.scheduler.models import ScheduleRecord

User = get_user_model()
PERIOD = datetime.date(2026, 9, 25)


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class EmployeeOperationLogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("op_super")
        cls.org = OrganizationUnit.objects.create(name="操作队", code="OP-A")
        cls.org_b = OrganizationUnit.objects.create(name="他队", code="OP-B")
        cls.admin = make_user("op_admin", role="admin", organization=cls.org)
        cls.member = make_user("op_member", role="member", organization=cls.org)
        cls.admin_b = make_user("op_admin_b", role="admin", organization=cls.org_b)
        cls.team = Team.objects.create(organization=cls.org, name="操作班", code="OP-T")
        cls.config = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.config.daily_worker_count = 2
        cls.config.required_shifts = 2
        cls.config.is_active = True
        cls.config.save(update_fields=["daily_worker_count", "required_shifts", "is_active"])
        cls.staff = [
            Employee.objects.create(
                employee_no=f"OP{i:02d}", name=f"操作{i}", team=cls.team,
                shift_config=cls.config, min_required_shifts=2,
            )
            for i in range(1, 4)
        ]
        outcome = generate_and_apply(cls.config, cls.super_user, anchor=PERIOD, time_limit_seconds=60)
        assert outcome["applied"], outcome["errors"]
        cls.batch = outcome["batch"]
        cls.employee = cls.staff[0]
        cls.url = reverse("personnel:employee_detail", args=[cls.employee.pk])

    def setUp(self):
        self.client.login(username="op_admin", password="pw")

    def _operations(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.context["operations"]

    def test_generation_is_recorded_with_batch(self):
        operations = self._operations()
        self.assertTrue(operations)
        generate_ops = [op for op in operations if op["action"] == "生成并保存"]
        self.assertTrue(generate_ops)
        op = generate_ops[0]
        self.assertEqual(op["batch"], str(self.batch.batch_id)[:8])
        self.assertIn("整批", op["after"])
        self.assertIn(op["operator"], ("op_super", "qizhang2004", "op_super"))

    def test_change_records_before_and_after(self):
        record = ScheduleRecord.objects.filter(
            schedule_batch_id=self.batch.batch_id, employee=self.employee,
            shift_type=ScheduleRecord.ShiftType.EARLY,
        ).first()
        result = change_schedule_record(record, ScheduleRecord.ShiftType.REST, self.admin)
        self.assertTrue(result["changed"], result)
        operations = self._operations()
        change_ops = [op for op in operations if op["action"] == "改班"]
        self.assertTrue(change_ops)
        op = change_ops[0]
        self.assertEqual(op["work_date"], record.work_date.isoformat())
        self.assertIn("早班", op["before"])
        self.assertIn("休息", op["after"])
        self.assertEqual(op["operator"], "op_admin")
        self.assertTrue(op["structured"])
        self.assertEqual(op["batch"], str(record.schedule_batch_id)[:8])

        # 操作数据来自真实审计记录，before/after 与数据库一致
        log = OperationLog.objects.filter(action="change").latest("created_at")
        self.assertEqual(log.before_data["shift_type"], "early")
        self.assertEqual(log.after_data["shift_type"], "rest")
        self.assertEqual(log.after_data["employee_id"], self.employee.pk)

    def test_operations_sorted_desc_and_multiple_changes(self):
        records = list(
            ScheduleRecord.objects.filter(
                schedule_batch_id=self.batch.batch_id, employee=self.employee,
                shift_type=ScheduleRecord.ShiftType.EARLY,
            )[:2]
        )
        self.assertEqual(len(records), 2)
        change_schedule_record(records[0], ScheduleRecord.ShiftType.REST, self.admin)
        change_schedule_record(records[1], ScheduleRecord.ShiftType.REST, self.admin)
        operations = self._operations()
        change_ops = [op for op in operations if op["action"] == "改班"]
        self.assertEqual(len(change_ops), 2)
        self.assertGreaterEqual(change_ops[0]["created_at"], change_ops[1]["created_at"])

    def test_failed_change_is_not_logged(self):
        record = ScheduleRecord.objects.filter(
            schedule_batch_id=self.batch.batch_id, employee=self.employee,
        ).first()
        before_count = OperationLog.objects.filter(action="change").count()
        result = change_schedule_record(record, "not-a-shift", self.admin)
        self.assertFalse(result["changed"])
        self.assertEqual(OperationLog.objects.filter(action="change").count(), before_count)

    def test_legacy_log_without_structured_data_is_not_fabricated(self):
        record = ScheduleRecord.objects.filter(
            schedule_batch_id=self.batch.batch_id, employee=self.employee,
        ).first()
        OperationLog.objects.create(
            user=self.admin, username="op_admin", action="change", module="schedule",
            target_type="schedule_record", target_id=str(record.pk),
            description="历史改班：缺少结构化前后数据",
        )
        operations = self._operations()
        legacy = [op for op in operations if op["description"].startswith("历史改班")]
        self.assertTrue(legacy)
        op = legacy[0]
        self.assertFalse(op["structured"])
        self.assertIn("历史日志未记录", op["before"])
        self.assertIn("历史日志未记录", op["after"])

    def test_other_employee_and_other_org_are_isolated(self):
        # 另一名职工的记录不应出现在当前职工详情中
        other = self.staff[1]
        other_record = ScheduleRecord.objects.filter(
            schedule_batch_id=self.batch.batch_id, employee=other,
            shift_type=ScheduleRecord.ShiftType.EARLY,
        ).first()
        change_schedule_record(other_record, ScheduleRecord.ShiftType.REST, self.admin)
        operations = self._operations()
        self.assertFalse(
            any(str(other.pk) == str(op.get("record_id")) for op in operations)
        )

        # 跨队组管理员访问 404，成员访问 403
        self.client.logout()
        self.client.login(username="op_admin_b", password="pw")
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.logout()
        self.client.login(username="op_member", password="pw")
        self.assertEqual(self.client.get(self.url).status_code, 403)
