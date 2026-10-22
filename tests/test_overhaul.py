# 功能完善与算法修复验收测试：出勤统计、个人周期日历、配置预检、检修班分配模型。
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.engine.allocator import plan_shift_targets
from apps.scheduler.engine.daily_solver import DailyProblem, solve_daily_assignment
from apps.scheduler.models import ScheduleRecord
from apps.scheduler.services import create_schedule_record, new_schedule_batch_id

User = get_user_model()

ANCHOR = datetime.date(2026, 10, 15)  # 周期 2026-09-25 ~ 2026-10-24


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class JiaXiuAllocationIntegrationTests(TestCase):
    # 检修班 22 人简化模型：目标分配 + 与每日约束共同成立
    def test_allocation_targets_and_daily_solver(self):
        # 22 人、豁免 1 人：普通 21×18=378、豁免 12、合计 390
        exempt = [22]
        allocation = plan_shift_targets(
            total_days=30, daily_required=13, base_shifts=18,
            normal_ids=list(range(1, 22)), exempt_ids=exempt,
        )
        self.assertTrue(allocation.feasible)
        self.assertEqual(allocation.total_slots, 390)
        self.assertEqual(allocation.full_count, 21)
        self.assertEqual(allocation.theoretical_exempt_count, 1)
        self.assertEqual(allocation.exempt_remaining_target, 12)
        self.assertTrue(
            all(allocation.per_person_targets[i] == 18 for i in range(1, 22))
        )
        self.assertEqual(allocation.per_person_targets[22], 12)
        self.assertEqual(sum(allocation.per_person_targets.values()), 390)

        # 与每日人数约束共同成立：偏差 0，实际班数=目标
        problem = DailyProblem(
            employee_ids=list(range(1, 23)),
            day_count=30,
            daily_required=13,
            targets=allocation.per_person_targets,
        )
        solution = solve_daily_assignment(problem)
        self.assertEqual(solution.status, "optimal")
        self.assertEqual(solution.daily_counts, [13] * 30)
        self.assertEqual(solution.total_deviation, 0)
        self.assertEqual(solution.achieved, allocation.per_person_targets)
        self.assertEqual(sum(solution.achieved.values()), 390)

    def test_normal_minimum_is_hard_lower_bound(self):
        # 无豁免：21 人才能保证 18 班，普通 22 人 → 明确不可行，绝不生成 17 班目标
        allocation = plan_shift_targets(
            total_days=30, daily_required=13, base_shifts=18,
            normal_ids=list(range(1, 23)), exempt_ids=[],
        )
        self.assertFalse(allocation.feasible)
        self.assertTrue(any("需要至少豁免 1 人" in n for n in allocation.notes))


class ShiftConfigPrecheckTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("pc_super")
        cls.org = OrganizationUnit.objects.create(name="甲队", code="ORG-A")
        cls.team = Team.objects.create(organization=cls.org, name="甲一班", code="TA-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.early.daily_worker_count = 2
        cls.early.required_shifts = 18
        cls.early.save(update_fields=["daily_worker_count", "required_shifts"])
        cls.employees = [
            Employee.objects.create(
                employee_no=f"P{i:03d}", name=f"预检{i}", team=cls.team,
                shift_config=cls.early, position="电工" if i <= 2 else "",
                min_required_shifts=18,
            )
            for i in range(1, 6)
        ]
        cls.early.exempt_employee_ids = [cls.employees[3].pk, cls.employees[4].pk]
        cls.early.save(update_fields=["exempt_employee_ids"])
        cls.url = reverse("organization:shift_config_edit", args=[cls.early.pk])

    def setUp(self):
        self.client.login(username="pc_super", password="pw")

    def test_precheck_renders_theoretical_numbers(self):
        response = self.client.get(self.url)
        self.assertContains(response, "排班可行性预检")
        self.assertContains(response, "理论普通人员")
        self.assertContains(response, "理论豁免人员")
        self.assertContains(response, "豁免人员剩余目标")
        self.assertContains(response, "运行完整求解验证")
        self.assertContains(response, "尚未完成完整求解验证")

    def test_precheck_position_insufficient_reports_bad(self):
        self.early.position_requirements = {"电工": 3}
        self.early.save(update_fields=["position_requirements"])
        response = self.client.get(self.url)
        self.assertContains(response, "明确不可行")
        self.assertContains(response, "仅 2 名有效职工具备该岗位资格")

    def test_full_verify_action(self):
        response = self.client.post(self.url, {"action": "verify"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "上次完整求解验证")
        self.assertTrue("可行" in response.content.decode() or "不可行" in response.content.decode())
