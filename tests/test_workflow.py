# 完整业务数据流测试：队组 -> 班组（自动班次配置）-> 职工 -> 排班记录 -> 操作日志。
import datetime
import uuid

from django.test import TestCase

from apps.auditlog.services import log_operation
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.models import ScheduleRecord


class DataFlowTests(TestCase):
    def test_full_data_flow(self):
        # 1. 队组
        org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        # 2. 班组：自动初始化早/中/晚三条班次配置
        team = Team.objects.create(organization=org, name="一班", code="T01-01")
        self.assertEqual(team.shift_configs.count(), 3)
        early = team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        # 3. 职工：关联班组与班次配置
        employee = Employee.objects.create(
            employee_no="E001", name="张三", team=team,
            shift_config=early, min_required_shifts=18,
        )
        # 4. 排班记录：同一批次、休息日同样保存
        batch = uuid.uuid4()
        for day, shift_type in [
            (datetime.date(2025, 1, 1), ScheduleRecord.ShiftType.EARLY),
            (datetime.date(2025, 1, 2), ScheduleRecord.ShiftType.REST),
        ]:
            record = ScheduleRecord(
                schedule_batch_id=batch, organization=org, team=team,
                shift_config=early, employee=employee,
                work_date=day, shift_type=shift_type,
            )
            record.full_clean()
            record.save()
        # 5. 操作日志
        log_operation(user=None, action="create", module="schedule", description="生成排班批次")

        self.assertEqual(org.teams.count(), 1)
        self.assertEqual(team.shift_configs.count(), 3)
        self.assertEqual(employee.schedule_records.count(), 2)
        self.assertEqual(team.schedule_records.count(), 2)
