# 队组管理员/超级管理员队组权限隔离验收测试。
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.drafting import confirm_schedule_draft, create_schedule_draft
from apps.scheduler.models import ScheduleBatch

User = get_user_model()


def make_user(username, *, role="admin", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class TeamScopeFixtureMixin:
    @classmethod
    def _build(cls):
        cls.super_user = make_user("scope_super", role="super")
        cls.org_a = OrganizationUnit.objects.create(name="甲队", code="ORG-A")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="ORG-B")
        cls.admin_a = make_user("scope_admin_a", organization=cls.org_a)
        cls.admin_b = make_user("scope_admin_b", organization=cls.org_b)
        cls.admin_no_org = make_user("scope_admin_none", organization=None)
        cls.member_a = make_user("scope_member_a", role="member", organization=cls.org_a)
        cls.team_a = Team.objects.create(organization=cls.org_a, name="甲一班", code="TA-01")
        cls.team_b = Team.objects.create(organization=cls.org_b, name="乙一班", code="TB-01")
        cls.config_a = cls.team_a.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.config_b = cls.team_b.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.emp_a = Employee.objects.create(
            employee_no="SA001", name="甲职工", team=cls.team_a,
            shift_config=cls.config_a, min_required_shifts=18,
        )
        cls.emp_b = Employee.objects.create(
            employee_no="SB001", name="乙职工", team=cls.team_b,
            shift_config=cls.config_b, min_required_shifts=18,
        )


class TeamAdminScopeTests(TeamScopeFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def setUp(self):
        self.client.login(username="scope_admin_a", password="pw")

    def test_list_pages_only_own_org(self):
        teams = self.client.get(reverse("organization:team_list"))
        self.assertContains(teams, "TA-01")
        self.assertNotContains(teams, "TB-01")
        # 队组管理员访问队组列表会被重定向到本队组详情（既有行为），内容仍限定本队组
        units = self.client.get(reverse("organization:unit_list"), follow=True)
        self.assertContains(units, "甲队")
        self.assertNotContains(units, "乙队")
        employees = self.client.get(reverse("personnel:employee_list"))
        self.assertContains(employees, "SA001")
        self.assertNotContains(employees, "SB001")

    def test_json_endpoints_scoped(self):
        teams_json = self.client.get(reverse("organization:teams_json"), {"organization": self.org_b.pk})
        self.assertNotIn("TB-01", teams_json.content.decode())
        configs_json = self.client.get(
            reverse("organization:shift_configs_json"), {"team": self.team_b.pk}
        )
        self.assertNotIn(self.config_b.pk, [
            item["id"] for item in configs_json.json().get("shift_configs", [])
        ] if configs_json.json().get("shift_configs") else [])

    def test_object_url_tampering_returns_404(self):
        for url in (
            reverse("organization:team_detail", args=[self.team_b.pk]),
            reverse("organization:team_edit", args=[self.team_b.pk]),
            reverse("organization:shift_config_edit", args=[self.config_b.pk]),
            reverse("personnel:employee_detail", args=[self.emp_b.pk]),
            reverse("personnel:employee_edit", args=[self.emp_b.pk]),
            reverse("personnel:employee_calendar", args=[self.emp_b.pk]),
        ):
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_post_tampering_cannot_create_in_other_org(self):
        # 建班组时伪造其他队组 → 表单拒绝，且不写入
        response = self.client.post(
            reverse("organization:team_create"),
            {"organization": self.org_b.pk, "name": "伪造班", "code": "FAKE-1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Team.objects.filter(code="FAKE-1").exists())
        # 建职工时伪造其他班组 → 拒绝
        self.client.post(
            reverse("personnel:employee_create"),
            {
                "employee_no": "FAKE-2", "name": "伪造职工", "team": self.team_b.pk,
                "shift_config": self.config_b.pk, "min_required_shifts": 18,
            },
        )
        self.assertFalse(Employee.objects.filter(employee_no="FAKE-2").exists())

    def test_generate_and_confirm_other_org_blocked(self):
        # 生成其他队组草稿 → 404
        self.assertEqual(
            self.client.post(reverse("scheduler:schedule_generate", args=[self.config_b.pk])).status_code,
            404,
        )
        # 服务层同样受限（模拟直接调用接口）
        with self.assertRaises(Exception):
            create_schedule_draft(self.config_b, self.admin_a)
        # 其他队组既有草稿：确认/作废一律拒绝
        draft = create_schedule_draft(self.config_b, self.super_user)["draft"]
        if draft is not None:
            with self.assertRaises(PermissionDenied):
                confirm_schedule_draft(draft.batch_id, self.admin_a)

    def test_admin_without_org_is_denied(self):
        self.client.login(username="scope_admin_none", password="pw")
        response = self.client.get(reverse("organization:team_list"))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "未绑定有效队组", status_code=403)
        self.assertEqual(
            self.client.get(reverse("personnel:employee_list")).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                reverse("scheduler:schedule_generate", args=[self.config_a.pk])
            ).status_code,
            403,
        )
        with self.assertRaises(PermissionDenied):
            create_schedule_draft(self.config_a, self.admin_no_org)

    def test_member_is_scoped(self):
        self.client.login(username="scope_member_a", password="pw")
        board = self.client.get(reverse("personnel:person_board"))
        self.assertContains(board, "甲职工")
        self.assertNotContains(board, "乙职工")
        self.assertEqual(
            self.client.get(reverse("personnel:employee_calendar", args=[self.emp_b.pk])).status_code,
            404,
        )


class SuperAdminScopeTests(TeamScopeFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def setUp(self):
        self.client.login(username="scope_super", password="pw")

    def test_super_can_switch_orgs(self):
        page_b = self.client.get(reverse("personnel:employee_list"), {"organization": self.org_b.pk})
        self.assertContains(page_b, "SB001")
        self.assertNotContains(page_b, "SA001")
        teams = self.client.get(reverse("organization:team_list"))
        self.assertContains(teams, "TA-01")
        self.assertContains(teams, "TB-01")

    def test_super_can_manage_other_org(self):
        detail = self.client.get(reverse("organization:team_detail", args=[self.team_b.pk]))
        self.assertEqual(detail.status_code, 200)
        draft = create_schedule_draft(self.config_b, self.super_user)["draft"]
        self.assertIsNotNone(draft)
        self.assertIsInstance(draft, ScheduleBatch)
