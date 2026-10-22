# 组织模型测试：队组、班组、班次配置及自动初始化。
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from .models import OrganizationUnit, ShiftConfig, Team


class OrganizationModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")

    def test_org_str(self):
        self.assertEqual(str(self.org), "采煤一队")

    def test_team_str(self):
        self.assertEqual(str(self.team), "采煤一队 / 一班")

    def test_org_code_unique(self):
        with self.assertRaises(IntegrityError):
            OrganizationUnit.objects.create(name="重复队", code="ORG-01")

    def test_team_code_unique_within_org(self):
        with self.assertRaises(IntegrityError):
            Team.objects.create(organization=self.org, name="另一班", code="T01-01")

    def test_team_code_can_repeat_across_orgs(self):
        other = OrganizationUnit.objects.create(name="采煤二队", code="ORG-02")
        Team.objects.create(organization=other, name="一班", code="T01-01")  # 不抛异常

    def test_team_auto_creates_three_shift_configs(self):
        configs = list(self.team.shift_configs.all())
        self.assertEqual(len(configs), 3)
        self.assertEqual(sorted(c.shift_name for c in configs), ["early", "middle", "night"])

    def test_shift_config_unique_within_team(self):
        with self.assertRaises(IntegrityError):
            ShiftConfig.objects.create(
                team=self.team,
                shift_name=ShiftConfig.ShiftName.EARLY,
                daily_worker_count=5,
                max_consecutive_rest_days=4,
                required_shifts=18,
            )


class ShiftConfigValidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)

    def test_daily_worker_count_must_be_positive(self):
        self.early.daily_worker_count = 0
        with self.assertRaises(ValidationError):
            self.early.full_clean()

    def test_required_shifts_must_be_positive(self):
        self.early.daily_worker_count = 5
        self.early.required_shifts = 0
        with self.assertRaises(ValidationError):
            self.early.full_clean()

    def test_max_rest_not_less_than_min_rest(self):
        self.early.daily_worker_count = 5
        self.early.required_shifts = 18
        self.early.min_consecutive_rest_days = 5
        self.early.max_consecutive_rest_days = 3
        with self.assertRaises(ValidationError):
            self.early.full_clean()

    def test_exempt_ids_must_exist_and_belong_to_team(self):
        from apps.personnel.models import Employee

        self.early.exempt_employee_ids = [99999]
        with self.assertRaises(ValidationError):
            self.early.full_clean()

        employee = Employee.objects.create(
            employee_no="E001", name="张三", team=self.team,
            shift_config=self.early, min_required_shifts=18,
        )
        self.early.exempt_employee_ids = [employee.pk]
        self.early.full_clean()  # 同班组职工：通过

        other_team = Team.objects.create(organization=self.org, name="二班", code="T01-02")
        other_night = other_team.shift_configs.get(shift_name=ShiftConfig.ShiftName.NIGHT)
        outsider = Employee.objects.create(
            employee_no="E002", name="李四", team=other_team,
            shift_config=other_night, min_required_shifts=18,
        )
        self.early.exempt_employee_ids = [outsider.pk]
        with self.assertRaises(ValidationError):
            self.early.full_clean()

    def test_exempt_ids_must_be_positive_ints(self):
        self.early.exempt_employee_ids = ["1"]
        with self.assertRaises(ValidationError):
            self.early.full_clean()


class PositionRequirementModelTests(TestCase):
    # shift_config.position_requirements 模型校验与旧字段共存
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)

    def test_default_is_empty_dict(self):
        self.assertEqual(self.early.position_requirements, {})

    def test_legacy_special_position_count_unchanged(self):
        # 旧字段保留且不受新字段影响
        self.early.special_position_count = 5
        self.early.save(update_fields=["special_position_count"])
        self.early.refresh_from_db()
        self.assertEqual(self.early.special_position_count, 5)
        self.assertEqual(self.early.position_requirements, {})

    def test_valid_requirements_pass_clean(self):
        self.early.position_requirements = {"电工": 2, "皮带": 0}
        self.early.full_clean()  # 不抛异常

    def test_negative_count_rejected(self):
        self.early.position_requirements = {"电工": -1}
        with self.assertRaises(ValidationError):
            self.early.full_clean()

    def test_float_count_rejected(self):
        self.early.position_requirements = {"电工": 1.5}
        with self.assertRaises(ValidationError):
            self.early.full_clean()

    def test_string_count_rejected(self):
        self.early.position_requirements = {"电工": "2"}
        with self.assertRaises(ValidationError):
            self.early.full_clean()

    def test_boolean_count_rejected(self):
        self.early.position_requirements = {"电工": True}
        with self.assertRaises(ValidationError):
            self.early.full_clean()

    def test_non_dict_rejected(self):
        self.early.position_requirements = ["电工"]
        with self.assertRaises(ValidationError):
            self.early.full_clean()

    def test_empty_position_name_rejected(self):
        self.early.position_requirements = {"": 2}
        with self.assertRaises(ValidationError):
            self.early.full_clean()


class TeamInitializationTests(TestCase):
    # 班组与班次配置初始化流程：幂等、待配置状态、启用校验
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")

    def test_auto_created_configs_are_inactive_until_configured(self):
        # 占位配置不得被误认为真实业务配置：未启用（待配置）
        self.assertEqual(self.team.shift_configs.filter(is_active=False).count(), 3)
        from .services import get_active_shift_configs

        self.assertEqual(get_active_shift_configs(self.team).count(), 0)

    def test_repeated_init_does_not_duplicate(self):
        from .services import ensure_default_shift_configs

        created, _ = ensure_default_shift_configs(self.team)
        self.assertEqual(created, 0)
        self.assertEqual(self.team.shift_configs.count(), 3)

    def test_ensure_command_idempotent(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("ensure_shift_configs", stdout=out)
        self.assertEqual(self.team.shift_configs.count(), 3)
        self.assertIn("新增 0 条", out.getvalue())

    def test_enable_config_with_invalid_values_rejected(self):
        from .services import set_shift_config_active

        cfg = self.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cfg.daily_worker_count = 0  # 非法参数
        with self.assertRaises(ValidationError):
            set_shift_config_active(cfg, True)
        cfg.refresh_from_db()
        self.assertFalse(cfg.is_active)  # 未能启用

    def test_enable_config_after_configuring(self):
        from .services import get_active_shift_configs, set_shift_config_active, update_shift_config

        cfg = self.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        update_shift_config(
            cfg,
            daily_worker_count=5,
            required_shifts=18,
            max_consecutive_rest_days=4,
        )
        set_shift_config_active(cfg, True)
        active = get_active_shift_configs(self.team)
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.first().shift_name, ShiftConfig.ShiftName.EARLY)

    def test_deactivate_team(self):
        from .services import deactivate_team

        deactivate_team(self.team)
        self.team.refresh_from_db()
        self.assertFalse(self.team.is_active)
