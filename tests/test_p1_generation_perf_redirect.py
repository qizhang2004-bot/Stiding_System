# 问题一：生产三班（25 人 / 每天 17 人 / 18 班 / 1 个班长岗）求解性能与生成成功跳转验收测试。
import datetime
import time

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.drafting import generate_and_apply
from apps.scheduler.models import ScheduleBatch, ScheduleRecord
from apps.scheduler.planning import solve_shift_schedule
from apps.scheduler.selectors import parse_period_param

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


def build_sc3_like_team(org, *, code="SC3", employees=25, daily=17, target=18, leaders=4):
    # 复刻生产三班真实配置：25 人、每天 17 人、18 班目标、班长岗每天 1 人
    team = Team.objects.create(organization=org, name="生产三班", code=code)
    config = team.shift_configs.get(shift_name=ShiftConfig.ShiftName.NIGHT)
    config.daily_worker_count = daily
    config.required_shifts = target
    config.position_requirements = {"班长": 1}
    config.is_active = True
    config.save(update_fields=[
        "daily_worker_count", "required_shifts", "position_requirements", "is_active",
    ])
    staff = [
        Employee.objects.create(
            employee_no=f"{code}{i:03d}", name=f"三班{i}", team=team, shift_config=config,
            position="班长" if i <= leaders else "", min_required_shifts=target,
        )
        for i in range(1, employees + 1)
    ]
    return team, config, staff


class GenerationPerformanceTests(TestCase):
    # 生产三班式场景：必须快速返回可行解，且硬约束/班数目标不被削弱
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("pf_super")
        cls.org = OrganizationUnit.objects.create(name="性能队", code="PF-ORG")
        cls.team, cls.config, cls.staff = build_sc3_like_team(cls.org)

    @override_settings(SCHEDULER_SOFT_STAGE_SECONDS=2)
    def test_sc3_like_scenario_solves_quickly_and_feasibly(self):
        started = time.monotonic()
        result = solve_shift_schedule(self.config, anchor=PERIOD, time_limit_seconds=120)
        elapsed = time.monotonic() - started
        self.assertEqual(result.status, "feasible", result.diagnostics)
        self.assertEqual(result.validation_errors, [])
        # 硬约束仍被完整满足：每日人数 17、班长岗每天 1 人、班数偏差 0
        self.assertEqual(sum(result.solution.daily_counts), 30 * 17)
        self.assertTrue(all(count == 17 for count in result.solution.daily_counts))
        self.assertTrue(all(c == 1 for c in result.solution.daily_position_counts["班长"]))
        self.assertEqual(result.total_shift_deviation, 0)
        # 软目标（3 连休）限时；2 连休窗口仍应全部满足
        self.assertEqual(result.rest_quality["total_windows"], 525)
        self.assertEqual(result.rest_quality["windows_with_2_block"], 525)
        # 性能护栏：优化前该场景需 60~120s（软目标耗尽时限），优化后应远低于 15s
        self.assertLess(elapsed, 15, f"求解耗时 {elapsed:.1f}s 超过性能护栏")

    def test_timing_logging_in_result_message(self):
        result = solve_shift_schedule(self.config, anchor=PERIOD, time_limit_seconds=60)
        self.assertIn("限时", result.solution.message)          # 软目标限时如实说明
        self.assertNotIn("不可行", result.solution.message)


class GenerationRedirectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("pr_redirect_super")
        cls.admin = make_user("pr_redirect_admin", role="admin")
        cls.org = OrganizationUnit.objects.create(name="跳转队", code="RJ-ORG")
        UserProfile.objects.filter(user=cls.admin).update(organization=cls.org)
        cls.team, cls.config, cls.staff = build_sc3_like_team(
            cls.org, code="SC3", employees=6, daily=4, target=18, leaders=1
        )
        cls.team2 = Team.objects.create(organization=cls.org, name="生产二班", code="RJ-T2")
        cls.config2 = cls.team2.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.config2.daily_worker_count = 2
        cls.config2.required_shifts = 2
        cls.config2.is_active = True
        cls.config2.save(update_fields=["daily_worker_count", "required_shifts", "is_active"])
        for i in range(1, 4):
            Employee.objects.create(
                employee_no=f"RJ2{i:02d}", name=f"二班{i}", team=cls.team2,
                shift_config=cls.config2, min_required_shifts=2,
            )

    def setUp(self):
        self.client.login(username="pr_redirect_admin", password="pw")

    def _generate(self, config, period=PERIOD):
        return self.client.post(
            reverse("scheduler:schedule_generate", args=[config.pk]),
            {"period": f"{period:%Y-%m}"},
        )

    @override_settings(SCHEDULER_SOFT_STAGE_SECONDS=1)
    def test_success_redirects_to_this_batch_result_page(self):
        response = self._generate(self.config)
        self.assertEqual(response.status_code, 302)
        batch = ScheduleBatch.objects.filter(team=self.team).latest("created_at")
        location = response["Location"]
        self.assertIn(reverse("scheduler:schedule_results"), location)
        self.assertIn(f"batch={batch.batch_id}", location)
        self.assertIn(f"team={self.team.pk}", location)
        self.assertIn(f"period={PERIOD:%Y-%m}", location)

        # 目标页面确实展示本次生成的批次结果（不是旧批次/草稿）
        page = self.client.get(location)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["selected_batch"]["batch_id"], batch.batch_id)
        self.assertTrue(page.context["selected_batch"]["is_current"])
        # 明细包含休息行：6 名职工 × 30 天 = 180 条
        self.assertEqual(page.context["grid"]["record_count"], 30 * 6)
        names = [row["employee"].name for row in page.context["grid"]["rows"]]
        self.assertIn("三班1", names)

    @override_settings(SCHEDULER_SOFT_STAGE_SECONDS=1)
    def test_redirect_is_correct_for_other_team_and_period(self):
        other_period = datetime.date(2026, 8, 25)
        response = self._generate(self.config2, other_period)
        self.assertEqual(response.status_code, 302)
        batch = ScheduleBatch.objects.filter(team=self.team2).latest("created_at")
        location = response["Location"]
        self.assertIn(f"team={self.team2.pk}", location)
        self.assertIn(f"batch={batch.batch_id}", location)
        self.assertIn(f"period={other_period:%Y-%m}", location)
        self.assertNotIn(f"team={self.team.pk}&", location)

    @override_settings(SCHEDULER_SOFT_STAGE_SECONDS=1)
    def test_old_batch_is_not_selected_after_new_generation(self):
        self._generate(self.config)
        old_batch = ScheduleBatch.objects.filter(team=self.team).latest("created_at")
        # 人为把旧批次时间调早，确保“按最新创建时间猜测”会选错，验证我们用的是批次号
        ScheduleBatch.objects.filter(pk=old_batch.pk).update(
            created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        )
        response = self._generate(self.config)
        new_batch = ScheduleBatch.objects.filter(team=self.team).exclude(pk=old_batch.pk).latest("created_at")
        self.assertIn(f"batch={new_batch.batch_id}", response["Location"])
        self.assertNotIn(f"batch={old_batch.batch_id}", response["Location"])

    def test_failure_does_not_redirect_to_results(self):
        # 每天人数 > 在职人数 → 不可行：不得跳转结果页，也不得生成批次
        self.config2.daily_worker_count = 99
        self.config2.save(update_fields=["daily_worker_count"])
        before = ScheduleBatch.objects.count()
        response = self._generate(self.config2)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(reverse("scheduler:schedule_results"), response["Location"])
        self.assertEqual(ScheduleBatch.objects.count(), before)
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(messages, "失败时必须给出明确提示")

    def test_generation_returns_timings(self):
        outcome = generate_and_apply(
            self.config2, self.admin, anchor=PERIOD, time_limit_seconds=30
        )
        self.assertTrue(outcome["applied"], outcome["errors"])
        for key in ("solve", "validate", "write", "total"):
            self.assertIn(key, outcome["timings"])
        self.assertGreaterEqual(outcome["timings"]["total"], outcome["timings"]["write"])

class SingleVersionHeadTests(TestCase):
    # 连续生成两次：同一班组同一周期只能有一个“当前版本”（旧批次头作废、结果页不误标）
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("vh_super")
        cls.org = OrganizationUnit.objects.create(name="版本队", code="VH-ORG")
        cls.team, cls.config, cls.staff = build_sc3_like_team(
            cls.org, code="VH1", employees=5, daily=3, target=2, leaders=1
        )

    def test_second_generation_supersedes_previous_batch_head(self):
        first = generate_and_apply(self.config, self.super_user, anchor=PERIOD, time_limit_seconds=30)
        self.assertTrue(first["applied"], first["errors"])
        first_batch = first["batch"]
        second = generate_and_apply(self.config, self.super_user, anchor=PERIOD, time_limit_seconds=30)
        self.assertTrue(second["applied"], second["errors"])
        second_batch = second["batch"]
        first_batch.refresh_from_db()
        second_batch.refresh_from_db()
        self.assertEqual(first_batch.status, ScheduleBatch.Status.SUPERSEDED)
        self.assertEqual(second_batch.status, ScheduleBatch.Status.CONFIRMED)
        heads = ScheduleBatch.objects.filter(
            team=self.team, kind=ScheduleBatch.Kind.PERIOD,
            status=ScheduleBatch.Status.CONFIRMED,
        )
        self.assertEqual(heads.count(), 1)

        # 结果页：仅本次批次被标为当前版本
        from apps.scheduler.selectors import batch_summaries, parse_period_param
        summaries = batch_summaries(self.team, *parse_period_param(f"{PERIOD:%Y-%m}"))
        current = [item["batch_id"] for item in summaries if item["is_current"]]
        self.assertEqual(current, [second_batch.batch_id])
