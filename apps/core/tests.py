# 核心功能测试。
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile


def make_super(username):
    user = get_user_model().objects.create_user(username, password="pw")
    user.is_superuser = True
    user.is_staff = True
    user.save()
    UserProfile.objects.update_or_create(user=user, defaults={"role": "super"})
    return user


class DashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_super("tester")

    def test_dashboard_requires_login(self):
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 302)

    def test_dashboard_loads_for_super_user(self):
        self.client.login(username="tester", password="pw")
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "工作台")
