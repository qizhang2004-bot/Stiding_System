from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Assignment, Group, Person, Role, Schedule, Team, UserProfile


class ViewSafetyTests(TestCase):
    """针对「非法输入导致 500 / 越权」的回归测试。"""

    @classmethod
    def setUpTestData(cls):
        cls.group_a = Group.objects.create(name="A队")
        cls.group_b = Group.objects.create(name="B队")
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


class PersonOverrideTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.group = Group.objects.create(name="测试队组A")
        cls.team = Team.objects.create(
            group=cls.group, name="检修班", daily_headcount=2,
            min_shift_target=8, rest_block={"min": 2, "max": 5},
        )
        cls.people = [
            Person.objects.create(name=f"张{i}", team=cls.team, required_shifts=8,
                                  default_shift="早班")
            for i in range(6)
        ]

    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw")
        UserProfile.objects.create(user=self.admin, group=self.group, role="team_admin")
        self.client.force_login(self.admin)

    def _save(self, **extra):
        data = {
            "action": "save_constraints", "team_id": str(self.team.id),
            "daily_headcount": "2", "rest_min": "2", "rest_max": "5",
            "min_shift_target": "10",
        }
        data.update(extra)
        return self.client.post(reverse("scheduler:team_manage"), data)

    def test_save_and_render_person_overrides(self):
        resp = self._save(**{
            "ov_person": ["张0", "张1"],
            "ov_rest_min": ["3", "2"],
            "ov_rest_max": ["4", "3"],
            "ov_work_days": ["18", "0"],
        })
        self.assertEqual(resp.status_code, 302)
        self.team.refresh_from_db()
        self.assertEqual(self.team.person_overrides, {
            "张0": {"rest_min": 3, "rest_max": 4, "work_days": 18},
            "张1": {"rest_min": 2, "rest_max": 3, "work_days": 0},
        })
        # 页面能渲染出已保存的专项约束
        page = self.client.get(reverse("scheduler:team_manage") + f"?team={self.team.id}")
        content = page.content.decode()
        self.assertIn("专人专项约束", content)
        self.assertIn("张0", content)

    def test_override_rejects_foreign_and_blank_rows(self):
        other = Group.objects.create(name="测试队组B")
        other_team = Team.objects.create(group=other, name="运输班")
        outsider = Person.objects.create(name="外人", team=other_team)
        self._save(**{
            "ov_person": ["", outsider.name, "张2"],
            "ov_rest_min": ["3", "3", "3"],
            "ov_rest_max": ["4", "4", "4"],
            "ov_work_days": ["9", "9", "9"],
        })
        self.team.refresh_from_db()
        self.assertEqual(list(self.team.person_overrides), ["张2"])

    def test_override_rest_min_clamped_to_two(self):
        self._save(**{
            "ov_person": ["张3"], "ov_rest_min": ["0"],
            "ov_rest_max": ["1"], "ov_work_days": ["0"],
        })
        self.team.refresh_from_db()
        self.assertEqual(self.team.person_overrides["张3"]["rest_min"], 2)
        self.assertEqual(self.team.person_overrides["张3"]["rest_max"], 2)

    def test_deleted_person_override_not_rendered(self):
        """人员被删除后，其专项约束不再出现在页面上（也不会渲染成可选行）。"""
        self._save(**{
            "ov_person": ["张4"], "ov_rest_min": ["3"],
            "ov_rest_max": ["4"], "ov_work_days": ["0"],
        })
        self.team.refresh_from_db()
        self.assertIn("张4", self.team.person_overrides)
        Person.objects.filter(name="张4").delete()
        page = self.client.get(reverse("scheduler:team_manage") + f"?team={self.team.id}")
        content = page.content.decode()
        self.assertNotIn('value="张4"', content)          # 已保存行的下拉不再含他
        self.assertNotIn("张4：连休", content)             # 只读视图也不展示

    def test_generate_applies_override_rest_block(self):
        """带专项约束生成排班：结果快照里应记录该人的专项要求，且求解成功。"""
        resp = self.client.post(reverse("scheduler:team_manage"), {
            "action": "generate", "team_id": str(self.team.id),
            "daily_headcount": "2", "rest_min": "2", "rest_max": "5",
            "min_shift_target": "8",
            "ov_person": ["张0"], "ov_rest_min": ["3"],
            "ov_rest_max": ["4"], "ov_work_days": ["12"],
        })
        self.assertEqual(resp.status_code, 302)
        rec = Schedule.objects.filter(team=self.team).order_by("-created_at").first()
        self.assertIsNotNone(rec)
        self.assertEqual(rec.person_overrides["张0"]["work_days"], 12)
        self.assertEqual(rec.rest_block, {"min": 2, "max": 5})
        self.assertIn(rec.status, ("OPTIMAL", "FEASIBLE"),
                      f"生成应可行，实际 {rec.status}：{rec.diagnostics}")
        if True:
            # 张0 必须满足自己的连休 3~4 天与至少 12 班
            days = sorted(Assignment.objects.filter(
                schedule=rec, person__name="张0").values_list("day", flat=True))
            self.assertGreaterEqual(len(days), 12)
            work = set(days)
            runs, cur = [], 0
            for d in range(rec.days):
                if d not in work:
                    cur += 1
                else:
                    if cur:
                        runs.append(cur)
                    cur = 0
            if cur:
                runs.append(cur)
            for r in runs:
                self.assertTrue(3 <= r <= 4, f"张0 出现连休 {r} 天，违反专项要求 3~4 天")


class TeamRestConfigTests(TestCase):
    """班组级「休息日要求」（1.3）必须真正落库并参与生成，而不是界面摆设。"""

    @classmethod
    def setUpTestData(cls):
        cls.group = Group.objects.create(name="测试队组C")
        cls.team = Team.objects.create(
            group=cls.group, name="生产班", daily_headcount=2, min_shift_target=8)
        for i in range(6):
            Person.objects.create(name=f"李{i}", team=cls.team, required_shifts=8,
                                  default_shift="早班")

    def setUp(self):
        self.admin = User.objects.create_user("boss_c", password="pw")
        UserProfile.objects.create(user=self.admin, group=self.group, role="team_admin")
        self.client.force_login(self.admin)

    def _post(self, **extra):
        data = {"action": "save_constraints", "team_id": str(self.team.id),
                "daily_headcount": "2", "min_shift_target": "8"}
        data.update(extra)
        return self.client.post(reverse("scheduler:team_manage"), data)

    def test_team_rest_config_persisted(self):
        self._post(rest_min="3", rest_max="6")
        self.team.refresh_from_db()
        self.assertEqual(self.team.rest_block, {"min": 3, "max": 6})
        # 页面回显保存的值（而非公式默认值）
        page = self.client.get(reverse("scheduler:team_manage") + f"?team={self.team.id}")
        self.assertIn("value=\"3\"", page.content.decode())

    def test_generate_uses_saved_team_rest_config(self):
        """班组级连休设为 3~5 后生成：排班结果里每个人的连休都必须落在 3~5 天。"""
        self._post(rest_min="3", rest_max="5")
        resp = self.client.post(reverse("scheduler:team_manage"), {
            "action": "generate", "team_id": str(self.team.id),
            "daily_headcount": "2", "rest_min": "3", "rest_max": "5",
            "min_shift_target": "8",
        })
        self.assertEqual(resp.status_code, 302)
        rec = Schedule.objects.filter(team=self.team).order_by("-created_at").first()
        self.assertEqual(rec.rest_block, {"min": 3, "max": 5})
        self.assertIn(rec.status, ("OPTIMAL", "FEASIBLE"), rec.diagnostics)
        for p in Person.objects.filter(team=self.team):
            days = set(Assignment.objects.filter(
                schedule=rec, person=p).values_list("day", flat=True))
            runs, cur = [], 0
            for d in range(rec.days):
                if d not in days:
                    cur += 1
                else:
                    if cur:
                        runs.append(cur)
                    cur = 0
            if cur:
                runs.append(cur)
            for r in runs:
                self.assertTrue(3 <= r <= 5,
                                f"{p.name} 出现连休 {r} 天，违反班组级要求 3~5 天")


class TeamManagePageMarkupTests(TestCase):
    """班组管理页「专人专项约束」增删行的 DOM 结构回归。

    背景：新增行原型 `#newOverrideRow` 必须真的位于容器 `#overrideRows` 之内，
    JS 用 `overrideRows.insertBefore(row, proto)` 插到它前面；原型若落在容器外，
    insertBefore 会抛 NotFoundError，表现为点「+ 添加专人专项约束」毫无反应。
    因此这里用 HTML 解析器判断真实的父子关系，而不是靠字符串位置猜。
    """

    @classmethod
    def setUpTestData(cls):
        cls.group = Group.objects.create(name="测试队组E")
        cls.team = Team.objects.create(group=cls.group, name="结构班",
                                       daily_headcount=3, min_shift_target=18)
        Person.objects.create(name="结构甲", team=cls.team, required_shifts=18)

    def setUp(self):
        self.admin = User.objects.create_user("boss_e", password="pw")
        UserProfile.objects.create(user=self.admin, group=self.group, role="team_admin")
        self.client.force_login(self.admin)

    def _doc(self):
        from html.parser import HTMLParser

        class Finder(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.parent_of = {}      # id -> 最近的带 id 祖先
                self.ids = set()

            def handle_starttag(self, tag, attrs):
                if tag in ("br", "img", "input", "meta", "link", "hr"):
                    return
                el_id = dict(attrs).get("id")
                if el_id:
                    self.ids.add(el_id)
                    self.parent_of[el_id] = next(
                        (i for i in reversed(self.stack) if i), None)
                self.stack.append(el_id)

            def handle_endtag(self, tag):
                if tag in ("br", "img", "input", "meta", "link", "hr"):
                    return
                if self.stack:
                    self.stack.pop()

        resp = self.client.get(reverse("scheduler:team_manage") + f"?team={self.team.id}")
        self.assertEqual(resp.status_code, 200)
        f = Finder()
        f.feed(resp.content.decode())
        return f

    def test_add_override_prototype_is_child_of_container(self):
        """原型必须是 #overrideRows 的直接子元素（否则 insertBefore 抛异常、按钮无效）。"""
        f = self._doc()
        self.assertIn("overrideRows", f.ids)
        self.assertIn("newOverrideRow", f.ids)
        self.assertEqual(
            f.parent_of.get("newOverrideRow"), "overrideRows",
            "#newOverrideRow 必须是 #overrideRows 的直接子元素，"
            "否则点击「+ 添加专人专项约束」会因 insertBefore 抛 NotFoundError 而毫无反应")

    def test_add_override_button_exists(self):
        f = self._doc()
        self.assertIn("addOverride", f.ids)
        self.assertIn("activePersonsData", f.ids)   # JS 依赖的人员 JSON

    def test_team_admin_page_has_no_superuser_only_widgets(self):
        """队组管理员看不到「删除队组」弹窗，脚本对这些元素必须判空，否则整段 JS 中断。"""
        f = self._doc()
        self.assertNotIn("delGroupModal", f.ids)
        self.assertIn("addOverride", f.ids)


class LexicographicObjectiveTests(TestCase):
    """回归：CP-SAT 不支持「多次 minimize 即词典序」，必须逐层求解固定最优值。

    背景 bug：`_add_lexicographic_objectives` 连续调用 7 次 m.minimize()，
    实测 OR-Tools 9.15 只有最后一次（豁免均衡）生效，前面「班数贴近目标」
    「非豁免均衡」等全被静默丢弃，导致：20 人锁死 18 班后，剩余班次被
    「只设下限、没有上限」的专人专项人员全部吃掉，豁免人员被挤成 0 班。
    """

    def _solve(self, days, daily, n_lock, n_flex, n_exempt, lock_days, flex_days):
        from .scheduling import build_schedule
        names = [f"L{i}" for i in range(n_lock)] + [f"F{i}" for i in range(n_flex)] \
                + [f"E{i}" for i in range(n_exempt)]
        flex = names[n_lock:n_lock + n_flex]
        exempt = names[n_lock + n_flex:]
        cfg = {
            "workers": [{"name": n, "roles": []} for n in names],
            "shifts": ["早班", "中班", "晚班"], "days": days, "daily_total": daily,
            "min_shift_target": lock_days, "exempt_workers": exempt,
            "rest_block": {"min": 2, "max": 4},
            "work_block": {"min": 2, "max": days // 3 - 2},
            "worker_rules": {n: {"rest_block": {"min": 2, "max": 3},
                                 "work_days": flex_days} for n in flex},
            "worker_shift_req": {
                **{n: {"target": lock_days, "min": lock_days, "max": lock_days}
                   for n in names[:n_lock]},
                **{n: {"target": flex_days, "min": flex_days} for n in flex},
            },
        }
        r = build_schedule(cfg, time_limit_seconds=20, phase2_seconds=10)
        self.assertIn(r.status, ("OPTIMAL", "FEASIBLE"), r.diagnostics)
        counts = {}
        for nm in names:
            counts[nm] = sum(1 for d in range(days) for s in cfg["shifts"]
                             if r.assignments.get((nm, d, s)))
        # 每天人数与总班次自检
        for d in range(days):
            self.assertEqual(
                sum(1 for nm in names for s in cfg["shifts"]
                    if r.assignments.get((nm, d, s))), daily)
        return counts, names[:n_lock], flex, exempt

    def test_exempt_people_get_a_fair_share_not_zero(self):
        """剩余班次必须分给豁免人员，不能被「无上限」的人独吞。"""
        counts, lock, flex, exempt = self._solve(
            days=31, daily=14, n_lock=20, n_flex=3, n_exempt=2,
            lock_days=18, flex_days=19)
        # 锁死的人必须正好达标
        for n in lock:
            self.assertEqual(counts[n], 18, f"{n} 应恰好 18 班")
        # 专项人员不得超过自己的最低要求太多（否则就是吃掉了剩余班次）
        for n in flex:
            self.assertEqual(counts[n], 19, f"{n} 应停在最低要求 19 班，不应多吃")
        # 豁免人员必须都排到班，且大致平分
        shares = sorted(counts[n] for n in exempt)
        self.assertTrue(all(s > 0 for s in shares),
                        f"豁免人员被挤成 0 班：{shares}")
        self.assertLessEqual(shares[-1] - shares[0], 1,
                             f"豁免人员之间应尽量平分：{shares}")

    def test_total_shifts_and_targets_still_respected(self):
        """修复词典序后，总班次与各人下限仍然全部满足。"""
        counts, lock, flex, exempt = self._solve(
            days=31, daily=13, n_lock=18, n_flex=3, n_exempt=2,
            lock_days=18, flex_days=19)
        self.assertEqual(sum(counts.values()), 31 * 13)
        for n in lock:
            self.assertGreaterEqual(counts[n], 18)
        for n in flex:
            self.assertGreaterEqual(counts[n], 19)


class EffectiveScheduleSelectionTests(TestCase):
    """回归：失败的生成记录（INFEASIBLE、无明细）不得「藏掉」之前有效的排班。

    背景 bug：`_schedule_for` / `_latest_schedules_for_month` 只按 created_at 取最新一条，
    而失败的重新生成也会落库（为了在结果页展示诊断）。于是「先成功、后失败」时，
    个人日历与班次展示会选中那份 0 明细的失败记录，原有排班凭空消失。
    """

    @classmethod
    def setUpTestData(cls):
        cls.group = Group.objects.create(name="测试队组F")
        cls.team = Team.objects.create(group=cls.group, name="生效班", daily_headcount=2)
        cls.person = Person.objects.create(name="生效甲", team=cls.team, required_shifts=5)

    def setUp(self):
        self.admin = User.objects.create_user("boss_f", password="pw")
        UserProfile.objects.create(user=self.admin, group=self.group, role="team_admin")
        self.client.force_login(self.admin)

    def _mk(self, status, year=2026, month=8, with_assignments=False):
        sch = Schedule.objects.create(
            team=self.team, year=year, month=month, start_date=date(year, month - 1, 25),
            days=30, shifts=["早班", "中班", "晚班"], daily_total=2, status=status)
        if with_assignments:
            for d in range(30):
                Assignment.objects.create(schedule=sch, person=self.person, day=d, shift="早班")
        return sch

    def test_person_page_uses_successful_schedule_over_later_failure(self):
        good = self._mk("OPTIMAL", with_assignments=True)
        bad = self._mk("INFEASIBLE")                      # 后创建 → created_at 更新
        self.assertGreater(bad.created_at, good.created_at)

        from .views import _schedule_for, _latest_schedules_for_month
        self.assertEqual(_schedule_for(self.person, 2026, 8).id, good.id)
        picked = [s for s in _latest_schedules_for_month(2026, 8, self.group)
                  if s.team_id == self.team.id]
        self.assertEqual(picked[0].id, good.id)

    def test_person_calendar_shows_shifts_not_wiped_by_failure(self):
        self._mk("OPTIMAL", with_assignments=True)
        self._mk("INFEASIBLE")
        resp = self.client.get(reverse("scheduler:person_detail", args=[self.person.id])
                               + "?y=2026&m=8")
        self.assertEqual(resp.status_code, 200)
        # 有效排班有 30 天早班，页面必须显示出来
        self.assertGreaterEqual(resp.content.decode().count("早班"), 20)
        self.assertIn("已上班", resp.content.decode())

    def test_failed_generation_keeps_only_latest_failed_record(self):
        """重复失败不应无限堆积记录（同班组同月只留最新一条失败记录）。"""
        from .views import current_period
        cy, cm = current_period()          # 生成走的是"当前周期"，测试数据必须同月才有意义
        self._mk("INFEASIBLE", year=cy, month=cm)
        self._mk("INFEASIBLE", year=cy, month=cm)
        self.assertEqual(Schedule.objects.filter(
            team=self.team, year=cy, month=cm).count(), 2)

        # 再走一次真实生成（必然失败：每天 2 人但只有 1 个人）
        tok = self.client.cookies.get("csrftoken")
        self.client.post(reverse("scheduler:team_manage"), {
            "csrfmiddlewaretoken": tok.value if tok else "",
            "action": "generate", "team_id": str(self.team.id),
            "daily_headcount": "2", "rest_min": "2", "rest_max": "4",
            "min_shift_target": "5",
            "ov_person": [""], "ov_rest_min": ["2"], "ov_rest_max": ["4"],
            "ov_work_days": ["0"],
        })
        left = Schedule.objects.filter(team=self.team)
        self.assertEqual(left.count(), 1, "旧的失败记录应被清理，只留最新一条")
        self.assertNotIn(left.first().status, ("OPTIMAL", "FEASIBLE"))

    def test_successful_schedule_still_wins_when_created_later(self):
        self._mk("INFEASIBLE")
        good = self._mk("OPTIMAL", with_assignments=True)
        from .views import _schedule_for
        self.assertEqual(_schedule_for(self.person, 2026, 8).id, good.id)


class ExemptEstimateTests(TestCase):
    """回归：容量估算必须先扣掉「专人专项」占用的班次，否则豁免人数会算少。

    原实现用 floor(总班次 ÷ 目标) 直接当「能排满的人数」，完全忽略专人专项。
    例：25 人、434 班、3 人各 19 班、其余每人 18 班
        旧算法 floor(434/18)=24 → 说「差 1 人」
        正确   (434−57)÷18=20.94 → 20 人 → 25−3−20 = 2 人需豁免
    """

    def test_special_days_are_deducted_first(self):
        from .scheduling import estimate_exempt_count
        r = estimate_exempt_count(25, 434, 18, [19, 19, 19])
        self.assertEqual(r["special_total"], 57)
        self.assertEqual(r["normal_available"], 377)
        self.assertEqual(r["fillable_normal"], 20)      # floor(377/18)
        self.assertEqual(r["needed_exempt"], 2)         # 25 − 3 − 20

    def test_matches_user_scenario_min21_special22(self):
        from .scheduling import estimate_exempt_count
        r = estimate_exempt_count(25, 434, 21, [22, 22, 22])
        self.assertEqual(r["special_total"], 66)
        self.assertEqual(r["normal_available"], 368)
        self.assertEqual(r["fillable_normal"], 17)      # floor(368/21)
        self.assertEqual(r["needed_exempt"], 5)         # 25 − 3 − 17

    def test_no_exempt_needed_when_capacity_is_enough(self):
        from .scheduling import estimate_exempt_count
        r = estimate_exempt_count(25, 465, 18, [19, 19, 19])   # 每天 15 人
        self.assertEqual(r["fillable_normal"], 22)
        self.assertEqual(r["needed_exempt"], 0)         # 不会出现负数

    def test_without_special_still_works(self):
        from .scheduling import estimate_exempt_count
        r = estimate_exempt_count(25, 434, 18, [])
        self.assertEqual(r["special_total"], 0)
        self.assertEqual(r["fillable_normal"], 24)      # floor(434/18)
        self.assertEqual(r["needed_exempt"], 1)

    def test_all_special_falls_back_to_smallest_requirement(self):
        """所有人都有专项要求时，除数退化为专项里最小的要求，不应除零。"""
        from .scheduling import estimate_exempt_count
        r = estimate_exempt_count(3, 90, 0, [20, 20, 20])
        self.assertEqual(r["normal_count"], 0)
        self.assertEqual(r["divisor"], 20)
        self.assertEqual(r["needed_exempt"], 0)

    def test_capacity_analysis_accepts_special_days_list(self):
        """capacity_analysis 接受 list（内部转 tuple 供缓存），且口径一致。"""
        from .scheduling import capacity_analysis
        r = capacity_analysis(25, 14, 31, 5, 21, 5, special_days=[22, 22, 22])
        self.assertEqual(r["total"], 434)
        self.assertEqual(r["special_total"], 66)
        self.assertEqual(r["fillable_normal"], 17)
        self.assertEqual(r["needed_exempt"], 5)
        # 再调一次走缓存，结果必须一致
        r2 = capacity_analysis(25, 14, 31, 5, 21, 5, special_days=[22, 22, 22])
        self.assertEqual(r2["needed_exempt"], 5)
