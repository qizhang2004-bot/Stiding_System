from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Group, Person, Team, UserProfile


class ViewSafetyTests(TestCase):
    """针对「非法输入导致 500 / 越权」的回归测试。"""

    @classmethod
    def setUpTestData(cls):
        cls.group_a = Group.objects.create(name="A队", short_name="a")
        cls.group_b = Group.objects.create(name="B队", short_name="b")
        cls.team_a = Team.objects.create(group=cls.group_a, name="A班", daily_headcount=1)
        cls.team_b = Team.objects.create(group=cls.group_b, name="B班", daily_headcount=1)
        cls.person_a = Person.objects.create(name="甲", team=cls.team_a, required_shifts=18)
        cls.person_b = Person.objects.create(name="乙", team=cls.team_b, required_shifts=18)

    def setUp(self):
        self.admin_a = User.objects.create_user("admin_a", password="pw")
        UserProfile.objects.create(user=self.admin_a, group=self.group_a, role="team_admin")
        self.client.force_login(self.admin_a)

    def test_shift_board_bad_year_no_500(self):
        resp = self.client.get(reverse("scheduler:shift_board") + "?y=-1&m=5")
        self.assertEqual(resp.status_code, 200)

    def test_person_detail_bad_year_no_500(self):
        url = reverse("scheduler:person_detail", args=[self.person_a.id]) + "?y=10000&m=5"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_shift_detail_bad_month_redirects(self):
        resp = self.client.get(reverse("scheduler:shift_detail", args=[2025, 13, 0, "早班"]))
        self.assertEqual(resp.status_code, 302)

    def test_save_constraints_bad_team_id_no_500(self):
        resp = self.client.post(reverse("scheduler:team_manage"), {
            "action": "save_constraints", "team_id": "abc", "daily_headcount": "3",
        })
        self.assertEqual(resp.status_code, 200)

    def test_cannot_delete_cross_group_person(self):
        resp = self.client.post(reverse("scheduler:team_manage"), {
            "action": "delete", "person_id": str(self.person_b.id),
        })
        self.assertTrue(Person.objects.filter(id=self.person_b.id).exists())

    def test_member_cannot_delete_person(self):
        # 队员账号为只读，POST 写操作应被整体拦截（回归：此前只有 save_constraints 被拦）
        member = User.objects.create_user("member_a", password="pw")
        UserProfile.objects.create(user=member, group=self.group_a, role="member")
        self.client.force_login(member)
        resp = self.client.post(reverse("scheduler:team_manage"), {
            "action": "delete", "person_id": str(self.person_a.id),
        })
        self.assertTrue(Person.objects.filter(id=self.person_a.id).exists())

    def test_cannot_move_person_to_other_group_team(self):
        resp = self.client.post(
            reverse("scheduler:person_edit", args=[self.person_a.id]),
            {
                "team": str(self.team_b.id),
                "default_shift": "早班",
                "worked_so_far": "0",
                "required_shifts": "18",
                "is_active": "on",
            },
        )
        self.person_a.refresh_from_db()
        self.assertEqual(self.person_a.team_id, self.team_a.id)

    def test_login_rejects_external_next(self):
        self.client.logout()
        User.objects.create_user("alice", password="pw123456")
        resp = self.client.post(reverse("scheduler:login"), {
            "username": "alice", "password": "pw123456", "next": "https://evil.example.com/",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/")
