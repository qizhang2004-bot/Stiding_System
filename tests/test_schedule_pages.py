# 排班生成入口与草稿页面测试（前端入口 + 后端调用链）。
import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.models import ScheduleBatch, ScheduleRecord

User = get_user_model()


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class ScheduleGenerateEntryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("gen_super")
        cls.org_a = OrganizationUnit.objects.create(name="甲队", code="ORG-A")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="ORG-B")
        cls.admin_b = make_user("gen_admin_b", role="admin", organization=cls.org_b)
        cls.member_a = make_user("gen_member_a", role="member", organization=cls.org_a)
        cls.team = Team.objects.create(organization=cls.org_a, name="甲一班", code="TA-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        # 每日 3 人：普通 3 人×18 班占 54 个名额，剩余 36 个名额由 2 名豁免人员承担；
        # 豁免人员不受休息规则约束，因此该配置可行（R=2 时才因名额不足不可行）
        cls.early.daily_worker_count = 3
        cls.early.required_shifts = 18
        cls.early.save(update_fields=["daily_worker_count", "required_shifts"])
        cls.employees = [
            Employee.objects.create(
                employee_no=f"G{i:03d}", name=f"生成{i}", team=cls.team,
                shift_config=cls.early, position="电工" if i <= 2 else "",
                min_required_shifts=18,
            )
            for i in range(1, 6)
        ]
        # 名额紧张：豁免 2 人（K=floor(2D/18)=3），否则分配不可行
        cls.early.exempt_employee_ids = [cls.employees[3].pk, cls.employees[4].pk]
        cls.early.save(update_fields=["exempt_employee_ids"])
        cls.edit_url = reverse("organization:shift_config_edit", args=[cls.early.pk])

    def setUp(self):
        self.client.login(username="gen_super", password="pw")

    def test_config_page_has_generate_entry(self):
        response = self.client.get(self.edit_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "生成并保存排班")
        self.assertContains(
            response, reverse("scheduler:schedule_generate", args=[self.early.pk])
        )

    def test_generate_saves_directly_and_redirects(self):
        # P1：生成即保存 —— 直接写入正式记录；成功后跳转到本次生成批次的结果页
        # （按批次号定位，不再回到来源页面，也不会指向旧批次/草稿）
        response = self.client.post(
            reverse("scheduler:schedule_generate", args=[self.early.pk]),
            {"next": self.edit_url},
        )
        self.assertEqual(response.status_code, 302)
        batch = ScheduleBatch.objects.get()
        location = response.headers["Location"]
        self.assertIn(reverse("scheduler:schedule_results"), location)
        self.assertIn(f"batch={batch.batch_id}", location)
        self.assertIn(f"team={batch.team_id}", location)
        self.assertIn(f"period={batch.period_start:%Y-%m}", location)
        self.assertEqual(batch.status, ScheduleBatch.Status.CONFIRMED)
        self.assertEqual(ScheduleBatch.objects.filter(status=ScheduleBatch.Status.DRAFT).count(), 0)
        total_days = (batch.period_end - batch.period_start).days + 1
        self.assertEqual(
            ScheduleRecord.objects.filter(
                schedule_batch_id=batch.batch_id, status=ScheduleRecord.Status.CONFIRMED, is_active=True
            ).count(),
            5 * total_days,
        )
        # 草稿详情页仍可查看该版本（历史/兼容路径），但不再要求用户确认
        detail = self.client.get(
            reverse("scheduler:schedule_draft_detail", args=[batch.batch_id])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "生成1")

    def test_generate_with_incomplete_config_shows_reason(self):
        # 每日人数 18 > 在职职工 5 → 明确报错、不生成草稿、回跳原页面
        self.early.daily_worker_count = 18
        self.early.save(update_fields=["daily_worker_count"])
        response = self.client.post(
            reverse("scheduler:schedule_generate", args=[self.early.pk]),
            {"next": self.edit_url},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], self.edit_url)
        self.assertEqual(ScheduleBatch.objects.count(), 0)
        page = self.client.get(self.edit_url)
        self.assertContains(page, "无法满足每日人数")

    def test_generate_via_plan_page(self):
        plan_url = (
            reverse("scheduler:schedule_plan")
            + f"?organization={self.org_a.pk}&team={self.team.pk}"
        )
        response = self.client.get(plan_url)
        self.assertEqual(response.status_code, 200)
        # 三个班次配置各有一个生成按钮（按按钮文本精确计数）
        self.assertEqual(
            response.content.decode().count(">生成并保存排班</button>"), 3
        )
        post = self.client.post(
            reverse("scheduler:schedule_generate", args=[self.early.pk]),
            {"next": plan_url},
        )
        self.assertEqual(post.status_code, 302)
        batch = ScheduleBatch.objects.get()          # 生成即保存：直接产生已确认版本
        self.assertEqual(batch.status, ScheduleBatch.Status.CONFIRMED)
        self.assertFalse(ScheduleBatch.objects.filter(status=ScheduleBatch.Status.DRAFT).exists())

    def test_generate_applies_immediately_without_draft(self):
        # P1：生成即保存 —— 无草稿确认步骤，正式记录立即写入
        self.client.post(reverse("scheduler:schedule_generate", args=[self.early.pk]))
        batch = ScheduleBatch.objects.get()
        self.assertEqual(batch.status, ScheduleBatch.Status.CONFIRMED)
        self.assertFalse(ScheduleBatch.objects.filter(status=ScheduleBatch.Status.DRAFT).exists())
        total_days = (batch.period_end - batch.period_start).days + 1
        self.assertEqual(
            ScheduleRecord.objects.filter(
                schedule_batch_id=batch.batch_id, status=ScheduleRecord.Status.CONFIRMED
            ).count(),
            5 * total_days,
        )
        # 看板立即显示新排班
        board = self.client.get(
            reverse("scheduler:shift_board"),
            {"organization": self.org_a.pk, "team": self.team.pk, "period": f"{batch.period_start:%Y-%m}"},
        )
        self.assertTrue(
            any(
                shift["total"] for day in board.context["board"]["days"] for shift in day["shifts"]
            )
        )

    def test_draft_detail_permissions(self):
        # 草稿流程保留（历史批次查看/兼容路径）：直接用服务层构造草稿后校验权限
        from apps.scheduler.drafting import create_schedule_draft

        admin = User.objects.get(username="gen_super")
        batch = create_schedule_draft(self.early, admin)["draft"]
        self.assertIsNotNone(batch)
        detail_url = reverse("scheduler:schedule_draft_detail", args=[batch.batch_id])
        # 普通成员无权查看
        self.client.login(username="gen_member_a", password="pw")
        self.assertEqual(self.client.get(detail_url).status_code, 403)
        # 其他队组管理员无权查看
        self.client.login(username="gen_admin_b", password="pw")
        self.assertEqual(self.client.get(detail_url).status_code, 403)

    def test_exempt_modal_assets_present(self):
        # 豁免职工弹窗所需元素/数据/脚本齐全，且候选人限定本班组在职职工
        response = self.client.get(self.edit_url)
        content = response.content.decode()
        for marker in (
            'id="exempt-open"',
            'id="exempt-modal"',
            'id="exempt-list"',
            'id="exempt-checkboxes"',
            'exempt-candidates-data',
            'shift_config.js?v=2',
        ):
            self.assertIn(marker, content, f"缺少 {marker}")
        match = re.search(r'exempt-candidates-data[^>]*>(.*?)</script>', content)
        self.assertIsNotNone(match)
        import json

        candidates = json.loads(match.group(1))
        self.assertEqual({c["id"] for c in candidates}, {e.pk for e in self.employees})
