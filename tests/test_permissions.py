# 权限测试：未登录访问受保护页面应跳转登录。
from django.test import TestCase
from django.urls import reverse


class PermissionTests(TestCase):
    def test_dashboard_redirects_to_login(self):
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
