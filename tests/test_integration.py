# 应用集成测试：登录用户可访问工作台与管理页面，管理员可访问后台。
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile


class IntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("tester", password="pw")
        cls.user.is_superuser = True
        cls.user.is_staff = True
        cls.user.save()
        UserProfile.objects.update_or_create(user=cls.user, defaults={"role": "super"})
        cls.staff = get_user_model().objects.create_user("staff", password="pw", is_staff=True)

    def test_dashboard_loads_for_user(self):
        self.client.login(username="tester", password="pw")
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_main_pages_load_for_user(self):
        self.client.login(username="tester", password="pw")
        for name in [
            "organization:unit_list",
            "organization:team_list",
            "organization:shift_config_list",
            "personnel:employee_list",
            "personnel:employee_import",
            "scheduler:schedule_plan",
            "scheduler:schedule_results",
            "auditlog:log_list",
        ]:
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)

    def test_admin_loads_for_staff(self):
        self.client.login(username="staff", password="pw")
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
