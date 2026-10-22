# 问题一：不同班次人员必须独立配置、独立计算、独立排班（运输班 5 人早班 / 8 人中班回归）。
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.personnel.services import shift_employees
from apps.scheduler.drafting import (
    add_manual_schedule,
    generate_and_apply,
    validate_adjustment_payload,
)
from apps.scheduler.models import ScheduleBatch, ScheduleRecord
from apps.scheduler.planning import (
    build_rest_problem,
    build_shift_allocation,
    solve_shift_schedule,
)
from apps.scheduler.selectors import board_candidates, parse_period_param

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


class TransportShiftIsolationTests(TestCase):
    # 运输班：早班 5 人 / 每天 3 人；中班 8 人 / 每天 4 人；最低 18 班；无豁免
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("ys_super")
        cls.org = OrganizationUnit.objects.create(name="运输队", code="YS-ORG")
        cls.team = Team.objects.create(organization=cls.org, name="运输班", code="YS")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.middle = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.MIDDLE)
        cls.night = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.NIGHT)
        for config, daily in ((cls.early, 3), (cls.middle, 4)):
            config.daily_worker_count = daily
            config.required_shifts = 18
            config.position_requirements = {}
            config.is_active = True
            config.save(update_fields=[
                "daily_worker_count", "required_shifts", "position_requirements", "is_active",
            ])
        cls.early_staff = [
            Employee.objects.create(
                employee_no=f"YSE{i}", name=f"早班{i}", team=cls.team, shift_config=cls.early,
                min_required_shifts=18,
            )
            for i in range(1, 6)
        ]
        cls.middle_staff = [
            Employee.objects.create(
                employee_no=f"YSM{i}", name=f"中班{i}", team=cls.team, shift_config=cls.middle,
                min_required_shifts=18,
            )
            for i in range(1, 9)
        ]

    # ---------- 1/2/3：人员范围与独立可行性 ----------
    def test_early_config_uses_only_early_members(self):
        self.assertEqual(shift_employees(self.early).count(), 5)
        info = build_shift_allocation(self.early, PERIOD)
        self.assertEqual(info["allocation"].employee_count, 5)
        precheck = build_rest_problem(self.early, PERIOD)
        self.assertEqual(set(precheck["problem"].employee_ids),
                         {e.pk for e in self.early_staff})
        self.assertEqual(len(precheck["problem"].targets), 5)

    def test_middle_config_uses_only_middle_members(self):
        self.assertEqual(shift_employees(self.middle).count(), 8)
        info = build_shift_allocation(self.middle, PERIOD)
        self.assertEqual(info["allocation"].employee_count, 8)
        # 8 人 × 18 班 > 30 天 × 4 人 → 中班在当前配置下不可行，problem 不会被构建
        precheck = build_rest_problem(self.middle, PERIOD)
        self.assertIsNone(precheck["problem"])
        self.assertTrue(precheck["infeasible_reasons"])
        # 加 2 名豁免后可行，求解器输入仍只含这 8 名中班人员
        self.middle.exempt_employee_ids = [
            self.middle_staff[6].pk, self.middle_staff[7].pk
        ]
        self.middle.save(update_fields=["exempt_employee_ids"])
        precheck = build_rest_problem(self.middle, PERIOD)
        self.assertIsNotNone(precheck["problem"])
        self.assertEqual(set(precheck["problem"].employee_ids),
                         {e.pk for e in self.middle_staff})
        self.assertEqual(len(precheck["problem"].targets), 8)

    def test_feasibility_is_judged_per_shift(self):
        early_alloc = build_shift_allocation(self.early, PERIOD)["allocation"]
        middle_alloc = build_shift_allocation(self.middle, PERIOD)["allocation"]
        # 早班：5 人 × 18 = 90 = 30×3，恰好可行
        self.assertTrue(early_alloc.feasible)
        self.assertEqual(early_alloc.full_count, 5)
        self.assertEqual(early_alloc.total_slots, 90)
        # 中班：8 人 × 18 = 144 > 30×4 = 120，真实不可行（需要 2 名豁免），
        # 且原因必须基于 8 人 / 120 人次，不能把早班人员混进来变成 13 人
        self.assertFalse(middle_alloc.feasible)
        self.assertEqual(middle_alloc.employee_count, 8)
        self.assertEqual(middle_alloc.total_slots, 120)
        self.assertEqual(middle_alloc.full_count, 6)
        self.assertEqual(middle_alloc.theoretical_exempt_count, 2)
        self.assertTrue(any("8 人" in note for note in middle_alloc.notes), middle_alloc.notes)

    def test_precheck_page_counts_per_shift(self):
        self.client.login(username="ys_super", password="pw")
        for config, expected in ((self.early, 5), (self.middle, 8)):
            response = self.client.get(
                reverse("organization:shift_config_edit", args=[config.pk])
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["precheck"]["employee_count"], expected)
            self.assertNotContains(response, "普通人员 13 人")

    # ---------- 4/5：生成结果只包含本班次人员 ----------
    def test_generation_contains_only_shift_members(self):
        early_outcome = generate_and_apply(
            self.early, self.super_user, anchor=PERIOD, time_limit_seconds=60
        )
        self.assertTrue(early_outcome["applied"], early_outcome["errors"])
        early_ids = set(
            ScheduleRecord.objects.filter(schedule_batch_id=early_outcome["batch"].batch_id)
            .values_list("employee_id", flat=True)
        )
        self.assertEqual(early_ids, {e.pk for e in self.early_staff})
        self.assertTrue(early_ids.isdisjoint({e.pk for e in self.middle_staff}))

        # 中班：8 人、每天 4 人、目标 14 班 → 120 名额 ÷ 8 人 = 15 ≥ 14，可行；
        # 生成结果只含中班人员（真实数据 18 班目标下的不可行性由 test_feasibility_is_judged_per_shift 覆盖）
        self.middle.required_shifts = 14
        self.middle.save(update_fields=["required_shifts"])
        middle_outcome = generate_and_apply(
            self.middle, self.super_user, anchor=PERIOD, time_limit_seconds=60
        )
        self.assertTrue(middle_outcome["applied"], middle_outcome["errors"])
        middle_ids = set(
            ScheduleRecord.objects.filter(
                schedule_batch_id=middle_outcome["batch"].batch_id
            ).values_list("employee_id", flat=True)
        )
        self.assertEqual(middle_ids, {e.pk for e in self.middle_staff})
        self.assertTrue(middle_ids.isdisjoint({e.pk for e in self.early_staff}))

        # 两个班次的当前有效记录互不重叠
        active = ScheduleRecord.objects.filter(status="confirmed", is_active=True)
        self.assertEqual(active.filter(employee__in=self.early_staff).count(), 5 * 30)
        self.assertEqual(active.filter(employee__in=self.middle_staff).count(), 8 * 30)

    # ---------- 6/7：豁免设置不跨班次影响 ----------
    def test_exempt_settings_do_not_leak_across_shifts(self):
        self.early.exempt_employee_ids = [self.early_staff[4].pk]
        self.early.save(update_fields=["exempt_employee_ids"])
        early_alloc = build_shift_allocation(self.early, PERIOD)["allocation"]
        middle_alloc = build_shift_allocation(self.middle, PERIOD)["allocation"]
        self.assertEqual(len(early_alloc.per_person_targets), 5)
        # 中班仍按自己 8 人计算（当前数据不可行 → 无目标），早班豁免不会改变中班结论
        self.assertEqual(middle_alloc.employee_count, 8)
        self.assertEqual(middle_alloc.per_person_targets, {})
        self.assertNotIn(self.early_staff[4].pk, self.middle.exempt_employee_ids)

    # ---------- 跨班次写入被拒绝 ----------
    def test_cross_shift_manual_add_rejected(self):
        day = PERIOD + datetime.timedelta(days=1)
        result = add_manual_schedule(self.early, day, self.middle_staff[0], self.super_user)
        self.assertFalse(result["added"])
        self.assertIn("不属于「早班」班次人员", result["errors"][0])
        self.assertFalse(
            ScheduleRecord.objects.filter(employee=self.middle_staff[0], work_date=day).exists()
        )

    def test_cross_shift_adjustment_rejected(self):
        batch = ScheduleBatch.objects.create(
            organization=self.org, team=self.team, shift_config=self.early,
            period_start=PERIOD, period_end=PERIOD + datetime.timedelta(days=29),
            mode="adjustment", daily_worker_count=1, position_requirements={},
            employee_targets={}, employee_qualifications={},
            payload=[{
                "employee_id": self.middle_staff[0].pk,
                "work_date": (PERIOD + datetime.timedelta(days=1)).isoformat(),
                "shift_type": "early",
                "position": "",
            }],
            summary={}, kind=ScheduleBatch.Kind.ADJUSTMENT,
            status=ScheduleBatch.Status.DRAFT,
            created_by=self.super_user, confirmed_by=self.super_user,
        )
        errors = validate_adjustment_payload(batch)
        self.assertTrue(errors)
        self.assertTrue(any("不属于本班组" in e or "不属于" in e for e in errors), errors)

    # ---------- 看板候选与豁免下拉同样按班次隔离 ----------
    def test_board_candidates_scoped_to_shift(self):
        day = PERIOD
        early_candidates = board_candidates(
            self.org, day, team=self.team, shift_name=self.early.shift_name
        )
        early_ids = {
            person["employee_id"] for group in early_candidates for person in group["people"]
        }
        self.assertEqual(early_ids, {e.pk for e in self.early_staff})
        middle_candidates = board_candidates(
            self.org, day, team=self.team, shift_name=self.middle.shift_name
        )
        middle_ids = {
            person["employee_id"] for group in middle_candidates for person in group["people"]
        }
        self.assertEqual(middle_ids, {e.pk for e in self.middle_staff})

    def test_exempt_form_only_offers_shift_members(self):
        from apps.organization.forms import ShiftConfigForm

        form = ShiftConfigForm(instance=self.early)
        self.assertEqual(
            set(form.fields["exempt_employees"].queryset.values_list("pk", flat=True)),
            {e.pk for e in self.early_staff},
        )

    # ---------- 单班次班组不受影响 ----------
    def test_single_shift_team_unaffected(self):
        team = Team.objects.create(organization=self.org, name="单班", code="YS-S1")
        config = team.shift_configs.get(shift_name=ShiftConfig.ShiftName.NIGHT)
        config.daily_worker_count = 3
        config.required_shifts = 18
        config.is_active = True
        config.save(update_fields=["daily_worker_count", "required_shifts", "is_active"])
        staff = [
            Employee.objects.create(
                employee_no=f"YSX{i}", name=f"单班{i}", team=team, shift_config=config,
                min_required_shifts=18,
            )
            for i in range(1, 5)
        ]
        self.assertEqual(shift_employees(config).count(), 4)
        alloc = build_shift_allocation(config, PERIOD)["allocation"]
        self.assertEqual(alloc.employee_count, 4)
        self.assertTrue(alloc.feasible)   # 4×18=72 ≤ 30×3=90
