# 账号体系与权限隔离测试：角色权限矩阵、跨队组隔离、账号保护、密码安全、审计日志。
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.accounts.permissions import ROLE_ADMIN, ROLE_MEMBER, ROLE_SUPER, get_profile
from apps.accounts.services import (
    create_organization_with_accounts,
    create_user_account,
    reset_password,
    set_account_active,
    set_role,
)
from apps.auditlog.models import OperationLog
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.models import ScheduleRecord
from apps.scheduler.selectors import period_range

User = get_user_model()

SUPER_URLS = [
    "core:dashboard",
    "organization:unit_list",
    "organization:unit_create",
    "organization:team_list",
    "organization:team_create",
    "organization:shift_config_list",
    "personnel:employee_list",
    "personnel:employee_create",
    "personnel:employee_import",
    "scheduler:schedule_plan",
    "scheduler:schedule_results",
    "auditlog:log_list",
    "accounts:user_list",
    "accounts:user_create",
]

MEMBER_FORBIDDEN_URLS = [
    "organization:unit_list",
    "organization:team_list",
    "organization:shift_config_list",
    "personnel:employee_list",
    "personnel:employee_import",
    "scheduler:schedule_plan",
    "scheduler:schedule_results",
    "auditlog:log_list",
    "accounts:user_list",
    "accounts:member_list",
]


def make_user(username, *, role=None, organization=None, is_superuser=False, is_active=True):
    user = User.objects.create_user(username, password="pw-12345")
    user.is_superuser = is_superuser
    user.is_staff = is_superuser
    user.is_active = is_active
    user.save()
    UserProfile.objects.update_or_create(
        user=user,
        defaults={
            "role": role or (ROLE_SUPER if is_superuser else ROLE_MEMBER),
            "organization": organization,
        },
    )
    return user


class AclFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.org_a = OrganizationUnit.objects.create(name="甲队", code="ORG-A")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="ORG-B")
        cls.super_user = make_user("super_u", role=ROLE_SUPER, is_superuser=True)
        cls.admin_a = make_user("admin_a", role=ROLE_ADMIN, organization=cls.org_a)
        cls.admin_b = make_user("admin_b", role=ROLE_ADMIN, organization=cls.org_b)
        cls.member_a = make_user("member_a", role=ROLE_MEMBER, organization=cls.org_a)
        cls.team_a = Team.objects.create(organization=cls.org_a, name="甲一班", code="TA-01")
        cls.team_a2 = Team.objects.create(organization=cls.org_a, name="甲二班", code="TA-02")
        cls.team_b = Team.objects.create(organization=cls.org_b, name="乙一班", code="TB-01")
        cls.early_a = cls.team_a.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.early_b = cls.team_b.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.emp_a = Employee.objects.create(
            employee_no="EA001", name="甲职工", team=cls.team_a,
            shift_config=cls.early_a, min_required_shifts=18, position="值班员",
            remark="内部备注内容",
        )
        cls.emp_b = Employee.objects.create(
            employee_no="EB001", name="乙职工", team=cls.team_b,
            shift_config=cls.early_b, min_required_shifts=18,
        )
        period_start, _ = period_range()
        ScheduleRecord.objects.create(
            schedule_batch_id=uuid.uuid4(),
            organization=cls.org_a,
            team=cls.team_a,
            shift_config=cls.early_a,
            employee=cls.emp_a,
            work_date=period_start,
            shift_type="early",
        )

    def login(self, username):
        self.client.login(username=username, password="pw-12345")


class ProfileAndPasswordTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="测试队", code="TST")

    def test_profile_auto_created_for_new_user(self):
        user = User.objects.create_user("plainuser", password="pw-12345")
        self.assertEqual(get_profile(user).role, ROLE_MEMBER)
        self.assertFalse(user.is_superuser)

    def test_passwords_are_hashed_not_plaintext(self):
        user, password = create_user_account(
            username="h1", password="Secret#12345", role=ROLE_ADMIN, organization=self.org
        )
        self.assertTrue(password == "Secret#12345")
        self.assertTrue(user.password.startswith("pbkdf2_"))
        self.assertNotEqual(user.password, password)
        self.assertNotIn(password, user.password)
        self.assertTrue(user.check_password("Secret#12345"))
        # 日志中不包含明文密码
        self.assertFalse(
            OperationLog.objects.filter(description__icontains="Secret#12345").exists()
        )

    def test_account_requires_org_for_non_super(self):
        with self.assertRaises(ValidationError):
            create_user_account(username="h2", password="Secret#12345", role=ROLE_ADMIN)

    def test_super_has_no_org(self):
        user, _ = create_user_account(username="h3", password="Secret#12345", role=ROLE_SUPER)
        self.assertTrue(user.is_superuser)
        self.assertIsNone(user.profile.organization)


class OrganizationCreationFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("flow_super", role=ROLE_SUPER, is_superuser=True)
        cls.existing = OrganizationUnit.objects.create(name="已存在队", code="EXIST")

    def setUp(self):
        self.client.login(username="flow_super", password="pw-12345")

    def _wizard_data(self, **overrides):
        data = {
            "name": "综掘五队",
            "code": "ZJWD",
            "is_active": "on",
            "admin_username": "zjwd_admin",
            "admin_password": "Admin#12345",
            "admin_password2": "Admin#12345",
            "member_username": "zjwd_member",
            "member_password": "Member#12345",
            "member_password2": "Member#12345",
        }
        data.update(overrides)
        return data

    def test_wizard_creates_org_and_bound_accounts(self):
        response = self.client.post(reverse("organization:unit_create"), self._wizard_data())
        self.assertEqual(response.status_code, 302)
        self.assertIn("confirm", response.url)
        response = self.client.get(reverse("organization:unit_create_confirm"))
        self.assertContains(response, "综掘五队")
        response = self.client.post(reverse("organization:unit_create_confirm"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("success", response.url)

        org = OrganizationUnit.objects.get(code="ZJWD")
        admin_user = User.objects.get(username="zjwd_admin")
        member_user = User.objects.get(username="zjwd_member")
        self.assertEqual(admin_user.profile.role, ROLE_ADMIN)
        self.assertEqual(admin_user.profile.organization, org)
        self.assertEqual(member_user.profile.role, ROLE_MEMBER)
        self.assertEqual(member_user.profile.organization, org)
        self.assertFalse(admin_user.is_superuser)
        self.assertFalse(member_user.is_superuser)
        # 密码只保存哈希
        self.assertTrue(admin_user.password.startswith("pbkdf2_"))
        self.assertTrue(admin_user.check_password("Admin#12345"))
        # 成功页展示两个登录名
        response = self.client.get(reverse("organization:unit_create_success"))
        self.assertContains(response, "zjwd_admin")
        self.assertContains(response, "zjwd_member")

    def test_wizard_generates_passwords_when_blank(self):
        data = self._wizard_data(
            admin_password="", admin_password2="", member_password="", member_password2=""
        )
        self.client.post(reverse("organization:unit_create"), data)
        self.client.post(reverse("organization:unit_create_confirm"))
        admin_user = User.objects.get(username="zjwd_admin")
        member_user = User.objects.get(username="zjwd_member")
        self.assertTrue(admin_user.password.startswith("pbkdf2_"))
        # 成功页展示生成的初始密码（仅一次）
        response = self.client.get(reverse("organization:unit_create_success"))
        self.assertContains(response, "初始密码")

    def test_wizard_duplicate_code_rejected(self):
        response = self.client.post(
            reverse("organization:unit_create"), self._wizard_data(code="EXIST")
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "该队组编号已存在")
        self.assertFalse(OrganizationUnit.objects.filter(name="综掘五队").exists())

    def test_wizard_duplicate_username_rejected(self):
        User.objects.create_user("zjwd_admin", password="pw-12345")
        response = self.client.post(reverse("organization:unit_create"), self._wizard_data())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "该登录账号已存在")

    def test_wizard_short_password_rejected(self):
        response = self.client.post(
            reverse("organization:unit_create"),
            self._wizard_data(admin_password="123", admin_password2="123"),
        )
        self.assertEqual(response.status_code, 200)

    def test_rollback_when_account_creation_fails(self):
        from apps.accounts import services as accounts_services

        self.client.post(reverse("organization:unit_create"), self._wizard_data())
        real_create = accounts_services.create_user_account
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated failure")
            return real_create(*args, **kwargs)

        with mock.patch(
            "apps.accounts.services.create_user_account", side_effect=flaky
        ):
            with self.assertRaises(RuntimeError):
                create_organization_with_accounts(
                    name="回滚队", code="RB-01", is_active=True,
                    admin_username="rb_admin", admin_password="Admin#12345",
                    member_username="rb_member", member_password="Member#12345",
                )
        # 事务回滚：队组与账号均未创建
        self.assertFalse(OrganizationUnit.objects.filter(code="RB-01").exists())
        self.assertFalse(User.objects.filter(username="rb_admin").exists())
        self.assertFalse(User.objects.filter(username="rb_member").exists())


class PermissionMatrixTests(AclFixtureMixin, TestCase):
    def test_super_accesses_all_pages(self):
        self.login("super_u")
        for name in SUPER_URLS:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_admin_accesses_own_org_pages(self):
        self.login("admin_a")
        self.assertEqual(self.client.get(reverse("organization:unit_detail", args=[self.org_a.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("organization:team_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("organization:shift_config_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("personnel:employee_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("scheduler:schedule_plan")).status_code, 200)
        self.assertEqual(self.client.get(reverse("scheduler:schedule_results")).status_code, 200)
        self.assertEqual(self.client.get(reverse("scheduler:shift_board")).status_code, 200)
        self.assertEqual(self.client.get(reverse("personnel:person_board")).status_code, 200)
        self.assertEqual(self.client.get(reverse("accounts:member_list")).status_code, 200)
        # 队组管理员看板只展示本队组数据
        self.assertEqual(
            self.client.get(reverse("personnel:employee_detail", args=[self.emp_a.pk])).status_code, 200
        )

    def test_admin_cross_org_access_returns_404(self):
        self.login("admin_a")
        self.assertEqual(
            self.client.get(reverse("organization:unit_detail", args=[self.org_b.pk])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse("organization:team_detail", args=[self.team_b.pk])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse("personnel:employee_detail", args=[self.emp_b.pk])).status_code, 404
        )
        self.assertEqual(
            self.client.get(
                reverse("organization:shift_config_edit", args=[self.early_b.pk])
            ).status_code,
            404,
        )

    def test_admin_cannot_access_super_pages(self):
        self.login("admin_a")
        for name in ["auditlog:log_list", "accounts:user_list", "accounts:user_create", "organization:unit_create"]:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("organization:unit_edit", args=[self.org_a.pk])).status_code, 403
        )

    def test_admin_post_super_actions_blocked(self):
        self.login("admin_a")
        self.assertEqual(self.client.post(reverse("organization:unit_create"), {}).status_code, 403)
        self.assertEqual(self.client.post(reverse("accounts:user_create"), {}).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("organization:unit_toggle", args=[self.org_a.pk]), {}).status_code,
            403,
        )

    def test_admin_team_create_forced_to_own_org(self):
        self.login("admin_a")
        # 正常创建：绑定本队组
        response = self.client.post(
            reverse("organization:team_create"),
            {"organization": self.org_a.pk, "name": "甲三班", "code": "TA-03", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 302)
        team = Team.objects.get(code="TA-03")
        self.assertEqual(team.organization, self.org_a)
        # 提交其他队组 ID：不在可见队组选项中，被表单拒绝
        response = self.client.post(
            reverse("organization:team_create"),
            {"organization": self.org_b.pk, "name": "越权班组", "code": "TA-04", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Team.objects.filter(code="TA-04").exists())
        self.assertFalse(self.org_b.teams.filter(code="TA-04").exists())

    def test_admin_cannot_create_admin_or_super_accounts(self):
        self.login("admin_a")
        # 成员账号页：角色由服务端固定，不能提权
        response = self.client.post(
            reverse("accounts:member_create"),
            {"username": "new_member", "password": "Member#12345", "password2": "Member#12345"},
        )
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="new_member")
        self.assertEqual(user.profile.role, ROLE_MEMBER)
        self.assertEqual(user.profile.organization, self.org_a)
        self.assertFalse(user.is_superuser)

    def test_admin_cannot_edit_own_role_or_org(self):
        self.login("admin_a")
        profile = self.admin_a.profile
        response = self.client.post(reverse("accounts:user_edit", args=[profile.pk]), {})
        self.assertEqual(response.status_code, 403)

    def test_member_only_boards_and_password_change(self):
        self.login("member_a")
        self.assertEqual(self.client.get(reverse("scheduler:shift_board")).status_code, 200)
        self.assertEqual(self.client.get(reverse("personnel:person_board")).status_code, 200)
        self.assertEqual(self.client.get(reverse("accounts:password_change")).status_code, 200)
        # 工作台对普通成员跳转班次看板
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("shift-board", response.url)

    def test_member_forbidden_pages_403(self):
        self.login("member_a")
        for name in MEMBER_FORBIDDEN_URLS:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_member_post_actions_blocked(self):
        self.login("member_a")
        for name, kwargs in [
            ("organization:unit_create", {}),
            ("organization:team_create", {}),
            ("personnel:employee_create", {}),
            ("personnel:employee_toggle", {"pk": self.emp_a.pk}),
            ("organization:shift_config_toggle", {"pk": self.early_a.pk}),
            ("accounts:member_create", {}),
        ]:
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(name, kwargs=kwargs), {}).status_code, 403)

    def test_member_cross_org_url_tampering_returns_own_data(self):
        self.login("member_a")
        response = self.client.get(
            reverse("scheduler:shift_board"),
            {"organization": self.org_b.pk, "team": self.team_b.pk},
        )
        self.assertEqual(response.status_code, 200)
        # 篡改参数后仍限定本队组（看板按班组聚合展示，不再逐人列出姓名）
        self.assertEqual(response.context["org"], self.org_a)
        self.assertContains(response, "甲队")
        self.assertNotContains(response, "乙队")

    def test_member_board_hides_internal_fields(self):
        self.login("member_a")
        response = self.client.get(reverse("personnel:person_board"))
        self.assertContains(response, "甲职工")
        self.assertNotContains(response, "EA001")  # 职工编号不展示
        self.assertNotContains(response, "内部备注内容")  # 备注不展示

    def test_shift_board_empty_state_without_records(self):
        self.login("member_a")
        response = self.client.get(
            reverse("scheduler:shift_board"), {"team": self.team_a2.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "暂无班次安排")

    def test_login_redirect_by_role(self):
        for username, target in [
            ("super_u", "core:dashboard"),
            ("admin_a", "core:dashboard"),
            ("member_a", "scheduler:shift_board"),
        ]:
            with self.subTest(username=username):
                self.login(username)
                response = self.client.get(reverse("accounts:home"))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, reverse(target))

    def test_deactivated_user_cannot_login(self):
        user = User.objects.create_user("temp_member", password="pw-12345")
        UserProfile.objects.update_or_create(
            user=user, defaults={"role": ROLE_MEMBER, "organization": self.org_a}
        )
        self.assertTrue(self.client.login(username="temp_member", password="pw-12345"))
        self.client.logout()
        set_account_active(user, False, actor=self.super_user)
        self.assertFalse(self.client.login(username="temp_member", password="pw-12345"))

    def test_deactivated_user_session_logged_out(self):
        user = User.objects.create_user("temp_member2", password="pw-12345")
        UserProfile.objects.update_or_create(
            user=user, defaults={"role": ROLE_MEMBER, "organization": self.org_a}
        )
        self.client.login(username="temp_member2", password="pw-12345")
        self.assertEqual(self.client.get(reverse("scheduler:shift_board")).status_code, 200)
        set_account_active(user, False, actor=self.super_user)
        response = self.client.get(reverse("scheduler:shift_board"))
        self.assertEqual(response.status_code, 302)  # 会话被登出 → 跳登录

    def test_login_failure_logged_without_password(self):
        response = self.client.post(
            reverse("accounts:login"), {"username": "admin_a", "password": "Wrong#12345"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "用户名或密码错误")
        log = OperationLog.objects.filter(module="auth", action="login_failed").first()
        self.assertIsNotNone(log)
        self.assertIn("admin_a", log.description)
        self.assertNotIn("Wrong#12345", log.description)

    def test_login_success_logged(self):
        # 直接经登录视图登录（测试客户端 client.login 不经过视图，不产生日志）
        self.client.post(
            reverse("accounts:login"), {"username": "admin_a", "password": "pw-12345"}
        )
        self.assertTrue(
            OperationLog.objects.filter(
                module="auth", action="login", description__icontains="admin_a"
            ).exists()
        )


class AccountProtectionTests(AclFixtureMixin, TestCase):
    def test_cannot_deactivate_last_active_super(self):
        # 当前只有 super_u 一个有效超级管理员
        with self.assertRaises(ValidationError):
            set_account_active(self.super_user, False, actor=self.super_user)
        self.super_user.refresh_from_db()
        self.assertTrue(self.super_user.is_active)

    def test_cannot_demote_last_active_super(self):
        with self.assertRaises(ValidationError):
            set_role(self.super_user.profile, ROLE_ADMIN, actor=self.super_user)
        self.super_user.profile.refresh_from_db()
        self.assertEqual(self.super_user.profile.role, ROLE_SUPER)

    def test_can_manage_when_second_super_exists(self):
        second = make_user("super_2", role=ROLE_SUPER, is_superuser=True)
        set_account_active(second, False, actor=self.super_user)
        second.refresh_from_db()
        self.assertFalse(second.is_active)
        set_account_active(second, True, actor=self.super_user)
        self.assertTrue(second.is_active)

    def test_super_can_change_role_and_org(self):
        profile = UserProfile.objects.get(user=self.member_a)
        set_role(profile, ROLE_ADMIN, actor=self.super_user)
        profile.refresh_from_db()
        self.assertEqual(profile.role, ROLE_ADMIN)
        self.assertEqual(profile.organization, self.org_a)
        # 移到乙队
        from apps.accounts.services import set_organization

        set_organization(profile, self.org_b, actor=self.super_user)
        profile.refresh_from_db()
        self.assertEqual(profile.organization, self.org_b)

    def test_reset_password_hashed_and_logged(self):
        reset_password(self.member_a, "New#Secret999", actor=self.super_user)
        self.member_a.refresh_from_db()
        self.assertTrue(self.member_a.password.startswith("pbkdf2_"))
        self.assertTrue(self.member_a.check_password("New#Secret999"))
        self.assertTrue(
            OperationLog.objects.filter(
                module="account", action="reset_password", target_id=str(self.member_a.pk)
            ).exists()
        )
        # 日志不含明文密码
        self.assertFalse(OperationLog.objects.filter(description__icontains="New#Secret999").exists())
