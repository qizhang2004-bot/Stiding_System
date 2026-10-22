# 问题二/三/五 的针对性测试：
#   - 分配器理论口径（人多/人少/豁免/空班组）与预检页面展示（不再出现 None）
#   - 首页移除“尚未启用的班次配置（待配置）”，保留“尚无有效班次配置班组”
#   - 职工“本周期排班数”统计口径（周期首日～今天、班次、休息/作废/未来排除、聚合与去重）
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.engine.allocator import plan_shift_targets
from apps.scheduler.models import ScheduleBatch, ScheduleRecord
from apps.scheduler.selectors import period_range, period_scheduled_counts

User = get_user_model()


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class AllocationTheoryTests(TestCase):
    # 问题二：人多模式下三项理论值不得为 None，且与业务口径一致
    def test_surplus_mode_numbers(self):
        # 生产三班式场景：25 人、30 天、每天 18 人、18 班 → 人多模式
        alloc = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=list(range(1, 26)), exempt_ids=[],
        )
        self.assertEqual(alloc.mode, "surplus")
        self.assertTrue(alloc.feasible)
        self.assertEqual(alloc.full_count, 25)          # 全员都能达到 18 班
        self.assertEqual(alloc.theoretical_exempt_count, 0)
        self.assertEqual(alloc.exempt_remaining_target, 90)  # 540 − 25×18
        self.assertEqual(alloc.surplus, 90)
        self.assertEqual(sum(alloc.per_person_targets.values()), 540)

    def test_surplus_mode_with_exempt_people(self):
        # 25 人（3 名豁免）、每天 18 人：剩余 90 班优先由豁免承担
        alloc = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=list(range(1, 23)), exempt_ids=[23, 24, 25],
        )
        self.assertEqual(alloc.mode, "surplus")
        self.assertEqual(alloc.full_count, 25)
        self.assertEqual(alloc.theoretical_exempt_count, 0)
        self.assertEqual(alloc.exempt_remaining_target, 90)
        # 豁免 3 人各可再加 12 班（30−18），随后剩余 54 班由 22 名普通人员均摊
        self.assertEqual([alloc.per_person_targets[i] for i in (23, 24, 25)], [30, 30, 30])
        self.assertEqual(sum(alloc.per_person_targets.values()), 540)

    def test_tight_mode_numbers_unchanged(self):
        # 人少模式口径回归：22 人（1 人豁免）、每天 13 人
        alloc = plan_shift_targets(
            total_days=30, daily_required=13, base_shifts=18,
            normal_ids=list(range(1, 22)), exempt_ids=[22],
        )
        self.assertEqual(alloc.mode, "tight")
        self.assertEqual(alloc.full_count, 21)
        self.assertEqual(alloc.theoretical_exempt_count, 1)
        self.assertEqual(alloc.exempt_remaining_target, 12)

    def test_numbers_never_none_for_infeasible_and_empty(self):
        empty = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18, normal_ids=[], exempt_ids=[]
        )
        self.assertFalse(empty.feasible)
        self.assertEqual(
            (empty.full_count, empty.theoretical_exempt_count, empty.exempt_remaining_target),
            (0, 0, 540),
        )
        too_many = plan_shift_targets(
            total_days=30, daily_required=30, base_shifts=18,
            normal_ids=list(range(1, 6)), exempt_ids=[],
        )
        self.assertFalse(too_many.feasible)
        self.assertIsNotNone(too_many.full_count)
        self.assertIsNotNone(too_many.theoretical_exempt_count)
        self.assertIsNotNone(too_many.exempt_remaining_target)

    def test_target_over_period_days_is_infeasible(self):
        # M 超过周期天数：任何人都不可能达标 → 理论普通人员 0
        alloc = plan_shift_targets(
            total_days=30, daily_required=10, base_shifts=40,
            normal_ids=list(range(1, 4)), exempt_ids=[],
        )
        self.assertFalse(alloc.feasible)
        self.assertEqual(alloc.full_count, 0)
        self.assertEqual(alloc.theoretical_exempt_count, 3)


class PrecheckPageTests(TestCase):
    # 问题二：预检页面人多/人少两种模式都不得出现 None
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("pr_super")
        cls.org = OrganizationUnit.objects.create(name="预检队", code="PR-ORG")
        cls.team = Team.objects.create(organization=cls.org, name="预检班", code="PR-T1")
        cls.config = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.config.daily_worker_count = 4
        cls.config.required_shifts = 18
        cls.config.is_active = True
        cls.config.save(update_fields=["daily_worker_count", "required_shifts", "is_active"])
        cls.employees = [
            Employee.objects.create(
                employee_no=f"PR{i:02d}", name=f"预检{i}", team=cls.team,
                shift_config=cls.config, min_required_shifts=18,
            )
            for i in range(1, 7)
        ]
        cls.url = reverse("organization:shift_config_edit", args=[cls.config.pk])

    def setUp(self):
        self.client.login(username="pr_super", password="pw")

    def _precheck_html(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("人多人少检查", html)
        return html

    def test_surplus_page_shows_numbers_not_none(self):
        html = self._precheck_html()
        # 6 人、30 天、每天 4 人、18 班 → 人多模式（120/18 > 6）
        self.assertIn("人多模式", html)
        self.assertNotIn(">None<", html)
        self.assertIn("理论普通人员 <strong>6</strong> 人", html)
        self.assertIn("理论豁免人员 <strong>0</strong> 人", html)
        self.assertIn("豁免人员剩余目标 <strong>12</strong> 班", html)  # 120 − 6×18

    def test_tight_page_shows_numbers_not_none(self):
        self.config.daily_worker_count = 3  # 90/18 = 5 ≤ 6 → 人少模式
        self.config.exempt_employee_ids = [self.employees[-1].pk]  # 普通 5 人可保证
        self.config.save(update_fields=["daily_worker_count", "exempt_employee_ids"])
        html = self._precheck_html()
        self.assertIn("人少模式", html)
        self.assertNotIn(">None<", html)

    def test_infeasible_page_shows_reason_not_numbers(self):
        self.config.daily_worker_count = 9  # 9 > 6 人 → 不可行
        self.config.save(update_fields=["daily_worker_count"])
        html = self._precheck_html()
        self.assertIn("人多人少检查未通过", html)
        self.assertNotIn(">None<", html)


class DashboardConfigSectionTests(TestCase):
    # 问题三：首页移除“尚未启用的班次配置（待配置）”，保留“尚无有效班次配置班组”
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user("db_admin", role="admin")
        cls.org = OrganizationUnit.objects.create(name="首页队", code="DB-ORG")
        UserProfile.objects.filter(user=cls.admin).update(organization=cls.org)
        cls.configured = Team.objects.create(organization=cls.org, name="已配置班", code="DB-T1")
        cls.pending = Team.objects.create(organization=cls.org, name="待配置班", code="DB-T2")
        cfg = cls.configured.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cfg.daily_worker_count = 2
        cfg.is_active = True
        cfg.save(update_fields=["daily_worker_count", "is_active"])

    def setUp(self):
        self.client.login(username="db_admin", password="pw")

    def test_pending_config_section_removed(self):
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn("尚未启用的班次配置", html)
        self.assertNotIn("待配置班次", html)
        self.assertNotIn("pending_config", html)

    def test_teams_without_active_config_still_listed(self):
        response = self.client.get(reverse("core:dashboard"))
        html = response.content.decode()
        self.assertIn("尚无有效班次配置的班组", html)
        self.assertIn("待配置班", html)          # 真正没有启用配置的班组仍提示
        self.assertNotIn("已配置班", html.split("尚无有效班次配置的班组")[1].split("最近操作日志")[0])

    def test_shift_config_feature_preserved(self):
        # 删除的是首页展示，不是班次配置本身
        self.assertEqual(self.pending.shift_configs.count(), 3)
        self.assertEqual(ShiftConfig.objects.filter(team__organization=self.org).count(), 6)


class PeriodScheduledCountTests(TestCase):
    # 问题五：本周期排班数 = 周期第一天～今天、早/中/晚有效记录，休息/作废/未来不计
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("ps_super")
        cls.org = OrganizationUnit.objects.create(name="统计队", code="PS-ORG")
        cls.team = Team.objects.create(organization=cls.org, name="统计班", code="PS-T1")
        cls.team2 = Team.objects.create(organization=cls.org, name="统计二班", code="PS-T2")
        cls.config = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.config.daily_worker_count = 2
        cls.config.is_active = True
        cls.config.save(update_fields=["daily_worker_count", "is_active"])
        cls.config2 = cls.team2.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.employees = [
            Employee.objects.create(
                employee_no=f"PS{i:02d}", name=f"统计{i}", team=cls.team,
                shift_config=cls.config, min_required_shifts=18,
            )
            for i in range(1, 4)
        ]
        cls.other = Employee.objects.create(
            employee_no="PS-OTHER", name="他班职工", team=cls.team2,
            shift_config=cls.config2, min_required_shifts=18,
        )
        cls.batch = ScheduleBatch.objects.create(
            organization=cls.org, team=cls.team, shift_config=cls.config,
            period_start=datetime.date(2026, 9, 25), period_end=datetime.date(2026, 10, 24),
            mode="tight", daily_worker_count=2, position_requirements={},
            employee_targets={}, employee_qualifications={}, payload=[], summary={},
            kind=ScheduleBatch.Kind.PERIOD, status=ScheduleBatch.Status.CONFIRMED,
            created_by=cls.super_user, confirmed_by=cls.super_user,
        )

    def _record(self, employee, day, shift_type, *, status=None, active=True):
        return ScheduleRecord.objects.create(
            schedule_batch_id=self.batch.batch_id, organization=self.org, team=employee.team,
            shift_config=employee.shift_config, employee=employee, work_date=day,
            shift_type=shift_type, status=status or ScheduleRecord.Status.CONFIRMED,
            is_active=active,
        )

    def test_counts_first_day_middle_and_today_boundary(self):
        today = datetime.date.today()
        start, end = period_range()
        # 周期首日 + 中间 + 今天，各 1 班；未来日期 1 班（不计）
        self._record(self.employees[0], start, ScheduleRecord.ShiftType.EARLY)
        middle = start + (today - start) // 2
        self._record(self.employees[0], middle, ScheduleRecord.ShiftType.MIDDLE)
        self._record(self.employees[0], today, ScheduleRecord.ShiftType.NIGHT)
        future = min(today + datetime.timedelta(days=1), end)
        if future > today:
            self._record(self.employees[0], future, ScheduleRecord.ShiftType.EARLY)
        counts = period_scheduled_counts(start, end, team=self.team, employees=[self.employees[0]])
        self.assertEqual(counts[self.employees[0].pk], 3)

    def test_rest_superseded_inactive_and_other_team_excluded(self):
        today = datetime.date.today()
        start, end = period_range()
        # 四条记录分散到不同日期，避免同批次唯一约束干扰统计断言
        self._record(self.employees[1], start, ScheduleRecord.ShiftType.REST)              # 休息不计
        self._record(self.employees[1], start + datetime.timedelta(days=1),
                     ScheduleRecord.ShiftType.EARLY,
                     status=ScheduleRecord.Status.SUPERSEDED)                              # 作废不计
        self._record(self.employees[1], start + datetime.timedelta(days=2),
                     ScheduleRecord.ShiftType.MIDDLE, active=False)                        # 已取消不计
        self._record(self.other, start + datetime.timedelta(days=3),
                     ScheduleRecord.ShiftType.EARLY)                                       # 其他班组不计
        counts = period_scheduled_counts(
            start, end, team=self.team, employees=[self.employees[1]]
        )
        self.assertEqual(counts[self.employees[1].pk], 0)

    def test_duplicate_same_day_counted_once(self):
        today = datetime.date.today()
        start, end = period_range()
        day = start if start <= today else today
        self._record(self.employees[2], day, ScheduleRecord.ShiftType.EARLY)
        # 人为制造同日重复有效工作记录（不违反唯一约束：走不同批次号）
        other_batch = ScheduleBatch.objects.create(
            organization=self.org, team=self.team, shift_config=self.config,
            period_start=start, period_end=end, mode="adjustment",
            daily_worker_count=1, position_requirements={}, employee_targets={},
            employee_qualifications={}, payload=[], summary={},
            kind=ScheduleBatch.Kind.ADJUSTMENT, status=ScheduleBatch.Status.CONFIRMED,
            created_by=self.super_user, confirmed_by=self.super_user,
        )
        ScheduleRecord.objects.create(
            schedule_batch_id=other_batch.batch_id, organization=self.org, team=self.team,
            shift_config=self.config, employee=self.employees[2], work_date=day,
            shift_type=ScheduleRecord.ShiftType.MIDDLE,
            status=ScheduleRecord.Status.CONFIRMED, is_active=True,
        )
        counts = period_scheduled_counts(start, end, team=self.team, employees=[self.employees[2]])
        self.assertEqual(counts[self.employees[2].pk], 1)   # 同日只计 1 班

    def test_zero_for_employee_without_schedule(self):
        start, end = period_range()
        counts = period_scheduled_counts(start, end, team=self.team, employees=[self.employees[0]])
        self.assertEqual(counts[self.employees[0].pk], 0)

    def test_employee_list_page_shows_counts(self):
        today = datetime.date.today()
        start, end = period_range()
        day = start if start <= today else today
        self._record(self.employees[0], day, ScheduleRecord.ShiftType.EARLY)
        self.client.login(username="ps_super", password="pw")
        response = self.client.get(reverse("personnel:employee_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "本周期排班数")
        self.assertContains(response, "周期开始至今天")
        counts = response.context["scheduled_counts"]
        self.assertEqual(counts[self.employees[0].pk], 1)     # 当天 1 班
        self.assertEqual(counts[self.employees[1].pk], 0)     # 无排班显示 0
