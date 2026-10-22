# 排班日历（人员名单）+ 人工增删排班 + 权限隔离 + 与旧批次共存 验收测试。
import datetime

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.auditlog.models import OperationLog
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.drafting import confirm_schedule_draft
from apps.scheduler.models import ScheduleBatch, ScheduleRecord
from apps.scheduler.selectors import active_schedule_records, build_shift_board
from apps.scheduler.services import create_schedule_record, new_schedule_batch_id

User = get_user_model()
DAY = datetime.date(2026, 9, 26)
PERIOD = (datetime.date(2026, 9, 25), datetime.date(2026, 10, 24))


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class BoardFixtureMixin:
    @classmethod
    def _build(cls):
        cls.super_user = make_user("m_super")
        cls.org_a = OrganizationUnit.objects.create(name="甲队", code="ORGA")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="ORGB")
        cls.admin_a = make_user("m_admin_a", role="admin", organization=cls.org_a)
        cls.member_a = make_user("m_member_a", role="member", organization=cls.org_a)
        cls.team1 = Team.objects.create(organization=cls.org_a, name="检修班", code="T1")
        cls.team2 = Team.objects.create(organization=cls.org_a, name="生产一班", code="T2")
        cls.team_b = Team.objects.create(organization=cls.org_b, name="乙班", code="TB")
        cls.config1 = cls.team1.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        for config in (cls.config1, cls.team2.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)):
            config.is_active = True
            config.daily_worker_count = 3
            config.required_shifts = 2
            config.save(update_fields=["is_active", "daily_worker_count", "required_shifts"])
        cls.config_b = cls.team_b.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.config_b.is_active = True
        cls.config_b.save(update_fields=["is_active"])
        cls.emp1 = Employee.objects.create(
            employee_no="M001", name="张某某", team=cls.team1, shift_config=cls.config1,
            position="电工", min_required_shifts=2,
        )
        cls.emp2 = Employee.objects.create(
            employee_no="M002", name="李某某", team=cls.team1, shift_config=cls.config1,
            min_required_shifts=2,
        )
        cls.emp3 = Employee.objects.create(
            employee_no="M003", name="王某某", team=cls.team2,
            shift_config=cls.team2.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY),
            min_required_shifts=2,
        )
        cls.emp_b = Employee.objects.create(
            employee_no="M009", name="外队职工", team=cls.team_b, shift_config=cls.config_b,
            min_required_shifts=2,
        )
        cls.rec1 = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=cls.org_a, team=cls.team1, shift_config=cls.config1,
            employee=cls.emp1, work_date=DAY,
            shift_type=ScheduleRecord.ShiftType.EARLY, position="电工",
            status=ScheduleRecord.Status.CONFIRMED,
        )


class BoardCalendarTests(BoardFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def setUp(self):
        self.client.force_login(self.admin_a)

    def test_grid_covers_period_with_three_shifts(self):
        response = self.client.get(reverse("scheduler:shift_board"), {"period": "2026-09"})
        self.assertEqual(response.status_code, 200)
        board = response.context["board"]
        self.assertEqual(len(board["days"]), 30)
        self.assertEqual(board["days"][0]["date"], PERIOD[0])
        self.assertEqual(board["days"][-1]["date"], PERIOD[1])
        for day in board["days"]:
            self.assertEqual([s["key"] for s in day["shifts"]], ["early", "middle", "night"])
        content = response.content.decode()
        self.assertIn("排班日历", content)
        self.assertIn("检修班", content)
        self.assertIn("张某某", content)
        self.assertNotIn("实际出勤", content)
        self.assertNotIn("请假", content)

    def test_board_shows_only_current_active_records(self):
        self.rec1.is_active = False  # 模拟人工删除
        self.rec1.save(update_fields=["is_active"])
        board = build_shift_board(*PERIOD, org=self.org_a)
        people = [p["name"] for day in board["days"] for s in day["shifts"] for t in s["teams"] for p in t["people"]]
        self.assertEqual(people, [])

    def test_detail_lists_people_by_team(self):
        response = self.client.get(
            reverse("scheduler:shift_board_detail", args=[DAY.isoformat(), "early"]),
            {"team": self.team1.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(response.context["groups"][0]["name"], "检修班")
        self.assertContains(response, "张某某")


class ManualAddTests(BoardFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def setUp(self):
        self.client.force_login(self.admin_a)
        self.add_url = reverse("scheduler:shift_board_add", args=[DAY.isoformat(), "early"])

    def test_candidates_are_people_not_scheduled_that_day(self):
        response = self.client.get(self.add_url)
        groups = {g["name"]: [p["name"] for p in g["people"]] for g in response.context["candidates"]}
        self.assertEqual(groups.get("检修班"), ["李某某"])
        self.assertEqual(groups.get("生产一班"), ["王某某"])
        self.assertNotIn("外队职工", str(groups))

    def test_add_persists_and_refreshes(self):
        response = self.client.post(self.add_url, {"employee_id": self.emp2.pk})
        self.assertEqual(response.status_code, 302)
        record = active_schedule_records(org=self.org_a, work_date=DAY, shift_key="early").filter(
            employee=self.emp2
        )
        self.assertEqual(record.count(), 1)
        self.assertEqual(record.first().status, ScheduleRecord.Status.CONFIRMED)
        # P2：人工添加不再创建 adjustment 批次（无新批次），但仍写审计
        self.assertFalse(
            ScheduleBatch.objects.filter(
                batch_id=record.first().schedule_batch_id,
                kind=ScheduleBatch.Kind.ADJUSTMENT,
            ).exists()
        )
        self.assertTrue(OperationLog.objects.filter(action="add").exists())
        # 刷新后日历含新人员（数据库驱动）
        page = self.client.get(reverse("scheduler:shift_board"), {"period": "2026-09"})
        self.assertContains(page, "李某某")

    def test_duplicate_add_rejected(self):
        before = ScheduleBatch.objects.count()
        response = self.client.post(self.add_url, {"employee_id": self.emp1.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ScheduleBatch.objects.count(), before)
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("已有排班" in m and "改班" in m for m in messages), messages)

    def test_add_other_team_same_shift_allowed(self):
        # 班次看板是队组级：同队组其他班组、但同属早班的人员可以添加
        response = self.client.post(self.add_url, {"employee_id": self.emp3.pk})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            active_schedule_records(org=self.org_a, work_date=DAY, shift_key="early")
            .filter(employee=self.emp3).exists()
        )

    def test_add_other_shift_employee_rejected(self):
        # 人员班次隔离：中班人员不能被添加到早班
        middle = self.team2.shift_configs.get(shift_name=ShiftConfig.ShiftName.MIDDLE)
        middle.is_active = True
        middle.daily_worker_count = 2
        middle.required_shifts = 2
        middle.save(update_fields=["is_active", "daily_worker_count", "required_shifts"])
        other_shift_emp = Employee.objects.create(
            employee_no="M010", name="中班职工", team=self.team2, shift_config=middle,
            min_required_shifts=2,
        )
        response = self.client.post(self.add_url, {"employee_id": other_shift_emp.pk})
        self.assertEqual(response.status_code, 302)
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("不属于「早班」班次人员" in m for m in messages), messages)
        self.assertFalse(
            active_schedule_records(org=self.org_a, work_date=DAY, shift_key="early")
            .filter(employee=other_shift_emp).exists()
        )

    def test_add_cross_org_employee_denied(self):
        response = self.client.post(self.add_url, {"employee_id": self.emp_b.pk})
        self.assertEqual(response.status_code, 404)

    def test_member_cannot_add(self):
        self.client.force_login(self.member_a)
        self.assertEqual(self.client.get(self.add_url).status_code, 403)
        self.assertEqual(
            self.client.post(self.add_url, {"employee_id": self.emp2.pk}).status_code, 403
        )

    def test_empty_candidate_state(self):
        for emp in (self.emp2, self.emp3):
            create_schedule_record(
                schedule_batch_id=new_schedule_batch_id(),
                organization=emp.team.organization, team=emp.team,
                shift_config=emp.shift_config, employee=emp, work_date=DAY,
                shift_type=ScheduleRecord.ShiftType.EARLY,
                status=ScheduleRecord.Status.CONFIRMED,
            )
        response = self.client.get(self.add_url)
        self.assertEqual(response.context["candidate_count"], 0)
        self.assertContains(response, "没有可添加的人员")


    def test_rest_employee_not_listed_as_add_candidate(self):
        # 当天有休息记录的人员不应出现在“可添加人员”候选里（避免列出却无法添加）
        rest_record = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_a, team=self.team1, shift_config=self.config1,
            employee=self.emp2, work_date=DAY,
            shift_type=ScheduleRecord.ShiftType.REST,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        response = self.client.get(self.add_url)
        groups = {g["name"]: [p["name"] for p in g["people"]] for g in response.context["candidates"]}
        names = [name for people in groups.values() for name in people]
        self.assertNotIn("李某某", names)
        # 直接提交仍会被拒绝（提示使用改班），保持既有业务规则
        reject = self.client.post(self.add_url, {"employee_id": self.emp2.pk})
        messages = [str(m) for m in get_messages(reject.wsgi_request)]
        self.assertTrue(any("改班" in m for m in messages), messages)
        rest_record.refresh_from_db()
        self.assertEqual(rest_record.shift_type, ScheduleRecord.ShiftType.REST)


    def test_add_page_shows_context_and_search(self):
        response = self.client.get(self.add_url, {"focus": self.team2.pk})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("当前选择", html)
        self.assertIn("2026-09-26", html)
        self.assertIn("早班", html)
        self.assertIn('name="q"', html)
        self.assertEqual(response.context["focus_team"].pk, self.team2.pk)
        # 分组标题包含“当前班组”标记
        self.assertIn("当前班组", html)

    def test_search_filters_candidates(self):
        response = self.client.get(self.add_url, {"q": "王某某"})
        groups = {g["name"]: [p["name"] for p in g["people"]] for g in response.context["candidates"]}
        names = [name for people in groups.values() for name in people]
        self.assertEqual(names, ["王某某"])
        empty = self.client.get(self.add_url, {"q": "不存在的名字"})
        self.assertEqual(empty.context["candidate_count"], 0)

    def test_board_add_links_use_focus_not_team_scope(self):
        response = self.client.get(reverse("scheduler:shift_board"), {"team": self.team1.pk})
        html = response.content.decode()
        add_url = reverse("scheduler:shift_board_add", args=[DAY.isoformat(), "early"])
        self.assertIn(f"{add_url}?focus=", html)

    def test_add_affects_only_selected_date(self):
        other_day = DAY + datetime.timedelta(days=1)
        before_other = ScheduleRecord.objects.filter(
            employee=self.emp3, work_date=other_day, is_active=True
        ).count()
        response = self.client.post(self.add_url, {"employee_id": self.emp3.pk})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            ScheduleRecord.objects.filter(
                employee=self.emp3, work_date=DAY, status="confirmed", is_active=True,
                shift_type__in=["early", "middle", "night"],
            ).exists()
        )
        self.assertEqual(
            ScheduleRecord.objects.filter(
                employee=self.emp3, work_date=other_day, is_active=True
            ).count(),
            before_other,
        )

    def test_added_person_disappears_from_candidates(self):
        self.client.post(self.add_url, {"employee_id": self.emp3.pk})
        response = self.client.get(self.add_url)
        names = [p["name"] for g in response.context["candidates"] for p in g["people"]]
        self.assertNotIn("王某某", names)


class ManualRemoveTests(BoardFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def setUp(self):
        self.client.force_login(self.admin_a)
        self.remove_url = reverse("scheduler:shift_board_remove", args=[self.rec1.pk])

    def test_remove_soft_deletes_only_that_record(self):
        other = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_a, team=self.team1, shift_config=self.config1,
            employee=self.emp1, work_date=DAY + datetime.timedelta(days=1),
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        response = self.client.post(self.remove_url, {"next": "/scheduler/shift-board/"})
        self.assertEqual(response.status_code, 302)
        self.rec1.refresh_from_db()
        self.assertFalse(self.rec1.is_active)                       # 软删除
        self.assertEqual(ScheduleRecord.objects.filter(pk=self.rec1.pk).count(), 1)  # 未物理删除
        self.assertTrue(
            active_schedule_records(org=self.org_a, work_date=DAY).filter(employee=self.emp1).count() == 0
        )
        other.refresh_from_db()
        self.assertTrue(other.is_active)                            # 其他日期不受影响
        log = OperationLog.objects.filter(action="remove").last()
        self.assertIn("人工删除排班", log.description)

    def test_remove_then_refresh_hides_person(self):
        self.client.post(self.remove_url, {"next": "/scheduler/shift-board/"})
        page = self.client.get(reverse("scheduler:shift_board"), {"period": "2026-09"})
        names = [
            p["name"]
            for day in page.context["board"]["days"]
            for shift in day["shifts"]
            for team in shift["teams"]
            for p in team["people"]
        ]
        self.assertNotIn("张某某", names)  # 页面名单来自数据库当前有效记录

    def test_remove_twice_is_reported(self):
        self.client.post(self.remove_url)
        response = self.client.post(self.remove_url)
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("已被删除" in m for m in messages), messages)

    def test_member_and_cross_org_cannot_remove(self):
        self.client.force_login(self.member_a)
        self.assertEqual(self.client.post(self.remove_url).status_code, 403)
        self.rec1.refresh_from_db()
        self.assertTrue(self.rec1.is_active)
        other_admin = make_user("m_admin_b", role="admin", organization=self.org_b)
        self.client.force_login(other_admin)
        self.assertEqual(self.client.post(self.remove_url).status_code, 404)
        self.rec1.refresh_from_db()
        self.assertTrue(self.rec1.is_active)


class OldBatchCoexistenceTests(BoardFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def setUp(self):
        self.client.force_login(self.admin_a)

    def test_manual_records_survive_and_new_version_replaces_them(self):
        # 1) 人工添加一条排班（补充批次）
        self.client.post(
            reverse("scheduler:shift_board_add", args=[DAY.isoformat(), "early"]),
            {"employee_id": self.emp2.pk},
        )
        manual = active_schedule_records(org=self.org_a, work_date=DAY, shift_key="early").filter(
            employee=self.emp2
        ).first()
        self.assertIsNotNone(manual)
        manual_batch_id = manual.schedule_batch_id

        # 2) 生成整周期草稿并确认（模拟重新排班，走作废替换）
        from apps.scheduler.drafting import create_schedule_draft

        self.config1.daily_worker_count = 2  # 检修班 2 人 → 每日 2 人可行
        self.config1.save(update_fields=["daily_worker_count"])
        result = create_schedule_draft(self.config1, self.super_user, time_limit_seconds=60)
        self.assertEqual(result["errors"], [], result["errors"])
        draft = result["draft"]
        confirm = confirm_schedule_draft(draft.batch_id, self.super_user, supersede_confirmed=True)
        self.assertTrue(confirm["confirmed"], confirm["errors"])

        # 3) 旧人工记录不被覆盖也不重复：整周期替换后，人工补充记录不再计入当前有效排班
        manual.refresh_from_db()
        self.assertEqual(ScheduleRecord.objects.filter(pk=manual.pk).count(), 1)   # 保留
        self.assertNotEqual(manual.status, ScheduleRecord.Status.CONFIRMED)        # 但已随整周期替换退休
        active_pairs = set(
            active_schedule_records(org=self.org_a).values_list("employee_id", "work_date")
        )
        self.assertEqual(len(active_pairs), len(set(active_pairs)))               # 无重复
        self.assertEqual(
            ScheduleRecord.objects.filter(schedule_batch_id=manual_batch_id, is_active=True, status="confirmed").count(),
            0,
        )

    def test_manual_edit_does_not_touch_old_batch(self):
        old_batch_id = self.rec1.schedule_batch_id
        self.client.post(reverse("scheduler:shift_board_remove", args=[self.rec1.pk]))
        batch = ScheduleBatch.objects.filter(batch_id=old_batch_id).first()
        self.assertIsNone(batch)  # 原记录来自独立批次号（非批次表），不被创建或修改
        self.assertEqual(ScheduleRecord.objects.filter(schedule_batch_id=old_batch_id).count(), 1)
