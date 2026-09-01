from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Assignment, Group, Person, Role, Schedule, Team, UserProfile


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

    def test_shift_detail_button_label_by_role(self):
        # 构造一份排班明细，让某天某班次有人员
        sch = Schedule.objects.create(
            team=self.team_a, year=2025, month=5, start_date=date(2025, 4, 25), days=30,
            shifts=["早班", "中班", "晚班"],
        )
        Assignment.objects.create(schedule=sch, person=self.person_a, day=0, shift="早班")
        url = reverse("scheduler:shift_detail", args=[2025, 5, 0, "早班"])

        # 队组管理员：显示「改班」
        resp = self.client.get(url)
        self.assertIn("改班", resp.content.decode())

        # 队员（只读）：显示「查看」，不出现「改班」
        member = User.objects.create_user("member_b", password="pw")
        UserProfile.objects.create(user=member, group=self.group_a, role="member")
        self.client.force_login(member)
        resp2 = self.client.get(url)
        content2 = resp2.content.decode()
        self.assertIn("查看", content2)
        self.assertNotIn("改班", content2)

    def test_shift_add_adds_resting_person(self):
        # team_a 再加一人「丙」（当天休息），「甲」已上早班
        person_c = Person.objects.create(name="丙", team=self.team_a, required_shifts=18)
        sch = Schedule.objects.create(
            team=self.team_a, year=2025, month=5, start_date=date(2025, 4, 25), days=30,
            shifts=["早班", "中班", "晚班"],
        )
        Assignment.objects.create(schedule=sch, person=self.person_a, day=0, shift="早班")
        url = reverse("scheduler:shift_add", args=[2025, 5, 0, "早班"])

        # 管理员查看：显示休息的「丙」，不显示已上班的「甲」
        resp = self.client.get(url)
        content = resp.content.decode()
        self.assertIn("丙", content)
        self.assertNotIn("甲", content)

        # 点「丙」加人 → 创建 Assignment
        resp2 = self.client.post(url, {"person_id": str(person_c.id)})
        self.assertEqual(resp2.status_code, 302)
        self.assertTrue(Assignment.objects.filter(
            schedule=sch, person=person_c, day=0, shift="早班").exists())

    def test_shift_add_member_forbidden(self):
        member = User.objects.create_user("member_c", password="pw")
        UserProfile.objects.create(user=member, group=self.group_a, role="member")
        self.client.force_login(member)
        resp = self.client.get(reverse("scheduler:shift_add", args=[2025, 5, 0, "早班"]))
        self.assertEqual(resp.status_code, 302)

    def test_save_constraints_bad_team_id_no_500(self):
        resp = self.client.post(reverse("scheduler:team_manage"), {
            "action": "save_constraints", "team_id": "abc", "daily_headcount": "3",
        })
        self.assertEqual(resp.status_code, 200)

    def test_save_constraints_warns_when_daily_exceeds_people(self):
        # team_a 只有 1 名启用人员，每天 3 人不可行 → 应带 warn=daily 提示
        resp = self.client.post(reverse("scheduler:team_manage"), {
            "action": "save_constraints",
            "team_id": str(self.team_a.id),
            "daily_headcount": "3",
            "rest_min": "2", "rest_max": "4", "min_shift_target": "18",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("warn=daily", resp["Location"])

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

    def test_person_edit_roles_scoped_to_group(self):
        # 人员编辑页的岗位只显示本队组的岗位，其它队组的岗位不应出现
        role_own = Role.objects.create(name="本队岗位")
        role_other = Role.objects.create(name="他队岗位")
        self.person_a.roles.add(role_own)
        self.person_b.roles.add(role_other)
        resp = self.client.get(reverse("scheduler:person_edit", args=[self.person_a.id]))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("本队岗位", content)
        self.assertNotIn("他队岗位", content)

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
