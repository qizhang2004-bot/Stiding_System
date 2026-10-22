# 操作日志测试。
from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import OperationLog
from .services import log_operation


class OperationLogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("tester", password="pw")

    def test_log_operation(self):
        entry = log_operation(
            user=self.user,
            action="create",
            module="team",
            target_type="team",
            target_id=1,
            description="新建班组",
            before_data={},
            after_data={"name": "一班"},
            ip_address="127.0.0.1",
        )
        self.assertTrue(OperationLog.objects.filter(pk=entry.pk).exists())
        self.assertEqual(entry.username, "tester")
        self.assertIn("team.create", str(entry))
        self.assertEqual(entry.before_data, {})
        self.assertEqual(entry.after_data, {"name": "一班"})

    def test_log_operation_without_user(self):
        entry = log_operation(action="update", module="shift_config", description="修改班次配置")
        self.assertIsNone(entry.user)
        self.assertEqual(entry.username, "")
