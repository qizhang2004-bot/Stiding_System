# 班次配置页测试：动态岗位人数配置、豁免职工弹窗、当前班次成员与权限隔离。
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.personnel.services import parse_positions

User = get_user_model()


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class ShiftConfigPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("cfg_super")
        cls.org_a = OrganizationUnit.objects.create(name="甲队", code="ORG-A")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="ORG-B")
        cls.team_a = Team.objects.create(organization=cls.org_a, name="甲一班", code="TA-01")
        cls.team_b = Team.objects.create(organization=cls.org_b, name="乙一班", code="TB-01")
        cls.early_a = cls.team_a.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.night_a = cls.team_a.shift_configs.get(shift_name=ShiftConfig.ShiftName.NIGHT)
        cls.early_b = cls.team_b.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.emp1 = Employee.objects.create(employee_no="EA001", name="张三", team=cls.team_a, shift_config=cls.early_a, position="电工", min_required_shifts=18)
        cls.emp2 = Employee.objects.create(employee_no="EA002", name="李四", team=cls.team_a, shift_config=cls.early_a, position="电工", min_required_shifts=18)
        cls.emp3 = Employee.objects.create(employee_no="EA003", name="王五", team=cls.team_a, shift_config=cls.early_a, position="皮带", min_required_shifts=18)
        cls.emp4 = Employee.objects.create(employee_no="EA004", name="赵六", team=cls.team_a, shift_config=cls.early_a, position="皮带", min_required_shifts=18)
        cls.emp5 = Employee.objects.create(employee_no="EA005", name="孙七", team=cls.team_a, shift_config=cls.early_a, position="风水管", min_required_shifts=18)
        cls.emp6 = Employee.objects.create(employee_no="EA006", name="周八", team=cls.team_a, shift_config=cls.early_a, position="班长、电工", min_required_shifts=18)
        cls.emp_b = Employee.objects.create(employee_no="EB001", name="乙职工", team=cls.team_b, shift_config=cls.early_b, position="电工", min_required_shifts=18)
        cls.url_a = reverse("organization:shift_config_edit", args=[cls.early_a.pk])

    def setUp(self):
        self.client.login(username="cfg_super", password="pw")

    def _post(self, config, *, positions=None, exempt_ids=None):
        data = {
            "team": config.team_id,
            "shift_name": config.shift_name,
            "daily_worker_count": config.daily_worker_count,
            "min_consecutive_rest_days": config.min_consecutive_rest_days,
            "max_consecutive_rest_days": config.max_consecutive_rest_days,
            "required_shifts": config.required_shifts,
            "is_active": "on" if config.is_active else "",
            "exempt_employees": exempt_ids or [],
            "position_requirements": json.dumps(positions, ensure_ascii=False) if positions is not None else "{}",
        }
        url = reverse("organization:shift_config_edit", args=[config.pk])
        return self.client.post(url, data)

    # ---------- 动态岗位列表 ----------
    def test_position_list_generated_and_deduped(self):
        response = self.client.get(self.url_a)
        self.assertEqual(response.status_code, 200)
        context = response.context
        # 按成员真实岗位动态生成并去重：电工(张三/李四/周八)=3、皮带=2、风水管=1、班长=1
        self.assertEqual(
            context["position_counts"], {"电工": 3, "皮带": 2, "风水管": 1, "班长": 1}
        )
        self.assertEqual(len(context["position_rows"]), 4)
        self.assertContains(response, "风水管")
        self.assertContains(response, "班长")

    def test_member_list_no_duplicate_rows_for_multi_position(self):
        response = self.client.get(self.url_a)
        context = response.context
        members = list(context["member_page"])
        self.assertEqual(len(members), 6)
        self.assertEqual(len({m.pk for m in members}), 6)  # 多岗位职工只出现一行
        zhou = next(m for m in members if m.name == "周八")
        self.assertEqual(parse_positions(zhou.position), ["班长", "电工"])

    def test_member_list_scope_and_columns(self):
        response = self.client.get(self.url_a)
        self.assertEqual(response.context["member_total"], 6)
        self.assertContains(response, "张三")
        self.assertNotContains(response, "乙职工")  # 其他队组不出现
        # 夜班配置当前无成员 → 空状态
        url_night = reverse("organization:shift_config_edit", args=[self.night_a.pk])
        response = self.client.get(url_night)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["position_rows"], [])
        self.assertEqual(response.context["member_total"], 0)
        self.assertContains(response, "当前班次暂无成员")

    # ---------- 岗位要求人数保存与校验 ----------
    def test_save_and_echo(self):
        self._post(self.early_a, positions={"电工": 2, "皮带": 1, "风水管": 1, "班长": 1})
        self.early_a.refresh_from_db()
        self.assertEqual(
            self.early_a.position_requirements,
            {"电工": 2, "皮带": 1, "风水管": 1, "班长": 1},
        )
        response = self.client.get(self.url_a)
        self.assertEqual(response.context["saved_reqs"]["电工"], 2)
        self.assertContains(response, 'value="2"')

    def test_zero_allowed(self):
        self._post(self.early_a, positions={"电工": 0})
        self.early_a.refresh_from_db()
        self.assertEqual(self.early_a.position_requirements, {"电工": 0})

    def test_negative_rejected(self):
        response = self._post(self.early_a, positions={"电工": -1})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "不能为负数")
        self.early_a.refresh_from_db()
        self.assertEqual(self.early_a.position_requirements, {})

    def test_float_rejected(self):
        response = self._post(self.early_a, positions={"电工": 1.5})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "必须为整数")
        self.early_a.refresh_from_db()
        self.assertEqual(self.early_a.position_requirements, {})

    def test_non_numeric_rejected(self):
        response = self._post(self.early_a, positions={"电工": "abc"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "必须为整数")
        self.early_a.refresh_from_db()
        self.assertEqual(self.early_a.position_requirements, {})

    def test_over_subscription_warns_but_not_rewritten(self):
        self._post(self.early_a, positions={"电工": 5})
        self.early_a.refresh_from_db()
        self.assertEqual(self.early_a.position_requirements, {"电工": 5})  # 未自动改写
        response = self.client.get(self.url_a)
        self.assertContains(response, "要求 5 人，但当前班次仅 3 人具备资格")
        self.assertContains(response, "不会自动修改")

    def test_orphan_position_preserved_with_hint(self):
        self._post(self.early_a, positions={"电工": 2, "焊接": 1})
        self.early_a.refresh_from_db()
        self.assertEqual(self.early_a.position_requirements, {"电工": 2, "焊接": 1})
        response = self.client.get(self.url_a)
        self.assertContains(response, "暂时没有成员具备该岗位资格")
        self.assertContains(response, "焊接")
        self.assertContains(response, "删除该岗位配置")

    def test_isolated_between_configs_and_orgs(self):
        self._post(self.early_a, positions={"电工": 2})
        self.early_a.refresh_from_db()
        self.assertEqual(self.early_a.position_requirements, {"电工": 2})
        self.night_a.refresh_from_db()
        self.assertEqual(self.night_a.position_requirements, {})
        self.early_b.refresh_from_db()
        self.assertEqual(self.early_b.position_requirements, {})
        # 乙队页面只显示乙队数据
        url_b = reverse("organization:shift_config_edit", args=[self.early_b.pk])
        response = self.client.get(url_b)
        self.assertContains(response, "乙职工")
        self.assertNotContains(response, "张三")

    # ---------- 豁免职工弹窗 ----------
    def test_modal_candidates_scope(self):
        response = self.client.get(self.url_a)
        candidates = response.context["candidates_json"]  # 原生列表（供 json_script 单次序列化）
        self.assertEqual(
            {c["id"] for c in candidates},
            {self.emp1.pk, self.emp2.pk, self.emp3.pk, self.emp4.pk, self.emp5.pk, self.emp6.pk},
        )  # 仅当前队组、班组、班次的在职成员（不含乙职工、不含夜班成员）
        # 停用成员不在候选
        self.emp1.is_active = False
        self.emp1.save(update_fields=["is_active"])
        response = self.client.get(self.url_a)
        candidates = response.context["candidates_json"]
        self.assertNotIn(self.emp1.pk, {c["id"] for c in candidates})

    def test_exempt_save_echo_remove(self):
        self._post(self.early_a, exempt_ids=[self.emp1.pk, self.emp2.pk])
        self.early_a.refresh_from_db()
        self.assertEqual(set(self.early_a.exempt_employee_ids), {self.emp1.pk, self.emp2.pk})
        response = self.client.get(self.url_a)
        self.assertEqual(response.context["saved_exempt_ids"], {self.emp1.pk, self.emp2.pk})
        self.assertContains(response, "张三")
        self.assertContains(response, "李四")
        # 移除一名后保存
        self._post(self.early_a, exempt_ids=[self.emp1.pk])
        self.early_a.refresh_from_db()
        self.assertEqual(self.early_a.exempt_employee_ids, [self.emp1.pk])

    def test_exempt_foreign_employee_rejected(self):
        response = self._post(self.early_a, exempt_ids=[self.emp_b.pk])
        self.assertEqual(response.status_code, 200)
        self.early_a.refresh_from_db()
        self.assertEqual(self.early_a.exempt_employee_ids, [])  # 未保存

    # ---------- 权限 ----------
    def test_member_role_cannot_edit(self):
        member = make_user("cfg_member", role="member", organization=self.org_a)
        self.client.login(username="cfg_member", password="pw")
        self.assertEqual(self.client.get(self.url_a).status_code, 403)
        self.assertEqual(self._post(self.early_a, positions={"电工": 2}).status_code, 403)
        self.early_a.refresh_from_db()
        self.assertEqual(self.early_a.position_requirements, {})

    def test_admin_cross_org_404(self):
        admin_b = make_user("cfg_admin_b", role="admin", organization=self.org_b)
        self.client.login(username="cfg_admin_b", password="pw")
        self.assertEqual(self.client.get(self.url_a).status_code, 404)
        self.assertEqual(self._post(self.early_a, positions={"电工": 2}).status_code, 404)

    def test_legacy_field_not_in_form(self):
        response = self.client.get(self.url_a)
        self.assertNotContains(response, 'name="special_position_count"')


class ShiftConfigEditRegressionTests(TestCase):
    # 回归：展示字段（所属班组/班次名称）不随表单提交，
    # 保存成功且不回写；校验失败后显示值与用户输入均保留。
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("cfg_edit_super")
        cls.org = OrganizationUnit.objects.create(name="甲队", code="ORG-A")
        cls.team = Team.objects.create(organization=cls.org, name="甲一班", code="TA-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.url = reverse("organization:shift_config_edit", args=[cls.early.pk])

    def setUp(self):
        self.client.login(username="cfg_edit_super", password="pw")

    def _post(self, **overrides):
        data = {
            "daily_worker_count": "18",
            "min_consecutive_rest_days": "2",
            "max_consecutive_rest_days": "4",
            "required_shifts": "18",
            "is_active": "",
            "position_requirements": "{}",
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_save_succeeds_without_display_fields(self):
        response = self._post(required_shifts="20")
        self.assertEqual(response.status_code, 302)
        self.early.refresh_from_db()
        self.assertEqual(self.early.required_shifts, 20)
        # 展示字段不被回写（team/shift_name 保持原值）
        self.assertEqual(self.early.team_id, self.team.pk)
        self.assertEqual(self.early.shift_name, ShiftConfig.ShiftName.EARLY)

    def test_failed_save_keeps_display_values_and_user_input(self):
        response = self._post(daily_worker_count="0", required_shifts="20")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "甲队 / 甲一班")   # 班组默认显示保留
        self.assertContains(response, "早班")            # 班次默认显示保留
        self.assertContains(response, 'value="20"')      # 用户输入保留
        self.assertNotContains(response, "这个字段是必填项")  # 不再误报必填
        self.assertContains(response, "必须大于 0")      # 显示真实错误原因
        self.early.refresh_from_db()
        self.assertEqual(self.early.required_shifts, 1)  # 未写入
