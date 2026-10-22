# 第一阶段（结果页口径 / 版本自动替换 / 加人失败清理）与第二阶段（直接改班）验收测试。
import datetime

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.auditlog.models import OperationLog
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler import selectors
from apps.scheduler.drafting import (
    add_manual_schedule,
    change_schedule_record,
    confirm_schedule_draft,
    create_schedule_draft,
)
from apps.scheduler.models import ScheduleBatch, ScheduleRecord
from apps.scheduler.services import create_schedule_record, new_schedule_batch_id

User = get_user_model()
DAY = datetime.date(2026, 9, 26)


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class PhaseOneFixtureMixin:
    @classmethod
    def _build(cls):
        cls.super_user = make_user("p_super")
        cls.org_a = OrganizationUnit.objects.create(name="甲队", code="P1A")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="P1B")
        cls.admin_a = make_user("p_admin_a", role="admin", organization=cls.org_a)
        cls.member_a = make_user("p_member_a", role="member", organization=cls.org_a)
        cls.team1 = Team.objects.create(organization=cls.org_a, name="检修班", code="P1T1")
        cls.team_b = Team.objects.create(organization=cls.org_b, name="乙班", code="P1TB")
        for team in (cls.team1, cls.team_b):
            for name in ("early", "middle", "night"):
                cfg = team.shift_configs.get(shift_name=name)
                cfg.is_active = True
                cfg.daily_worker_count = 2
                cfg.required_shifts = 2
                cfg.save(update_fields=["is_active", "daily_worker_count", "required_shifts"])
        cls.config1 = cls.team1.shift_configs.get(shift_name="early")
        cls.config_b = cls.team_b.shift_configs.get(shift_name="early")
        cls.emp1 = Employee.objects.create(
            employee_no="P1001", name="张某某", team=cls.team1, shift_config=cls.config1,
            position="电工", min_required_shifts=2,
        )
        cls.emp2 = Employee.objects.create(
            employee_no="P1002", name="李某某", team=cls.team1, shift_config=cls.config1,
            min_required_shifts=2,
        )
        cls.emp_b = Employee.objects.create(
            employee_no="P1009", name="外队职工", team=cls.team_b, shift_config=cls.config_b,
            min_required_shifts=2,
        )
        cls.rec1 = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=cls.org_a, team=cls.team1, shift_config=cls.config1,
            employee=cls.emp1, work_date=DAY,
            shift_type=ScheduleRecord.ShiftType.EARLY, position="电工",
            status=ScheduleRecord.Status.CONFIRMED,
        )


class ResultsScopeTests(PhaseOneFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def setUp(self):
        self.client.force_login(self.admin_a)
        self.url = reverse("scheduler:schedule_results")

    def _grid_names(self, response):
        grid = response.context["grid"]
        if grid is None:
            return []
        return [
            row["employee"].name
            for row in grid["rows"]
            for cell in row["cells"]
            if cell.get("shift") and cell["shift"] != ScheduleRecord.ShiftType.REST
        ]

    def test_current_view_excludes_superseded_records(self):
        response = self.client.get(self.url, {"period": "2026-09"})
        self.assertIn("张某某", self._grid_names(response))
        # 作废该批次（模拟旧版本）→ 当前有效视图不再展示
        self.rec1.status = ScheduleRecord.Status.SUPERSEDED
        self.rec1.save(update_fields=["status"])
        response = self.client.get(self.url, {"period": "2026-09"})
        self.assertIsNone(response.context["grid"])  # 无当前有效版本 → 不展示历史数据
        self.assertNotIn("张某某", self._grid_names(response))

    def test_current_view_excludes_inactive_records(self):
        self.rec1.is_active = False
        self.rec1.save(update_fields=["is_active"])
        response = self.client.get(self.url, {"period": "2026-09"})
        self.assertIsNone(response.context["grid"])
        self.assertNotIn("张某某", self._grid_names(response))

    def test_history_batch_still_viewable(self):
        # 历史批次（已作废）在其批次详情中仍按原貌展示
        self.rec1.status = ScheduleRecord.Status.SUPERSEDED
        self.rec1.save(update_fields=["status"])
        response = self.client.get(
            self.url, {"period": "2026-09", "batch": str(self.rec1.schedule_batch_id)}
        )
        self.assertIn("张某某", self._grid_names(response))
        self.assertFalse(response.context["selected_batch"]["is_current"])

    def test_batch_summaries_marks_current_version(self):
        start, end = selectors.parse_period_param("2026-09")
        # 建立“整周期版本头”（P3 起：只有版本头才算当前版本）
        head = ScheduleBatch.objects.create(
            organization=self.org_a, team=self.team1, shift_config=self.config1,
            period_start=start, period_end=end, mode="tight",
            daily_worker_count=self.config1.daily_worker_count,
            position_requirements={}, employee_targets={}, employee_qualifications={},
            payload=[], summary={}, kind=ScheduleBatch.Kind.PERIOD,
            status=ScheduleBatch.Status.CONFIRMED,
            created_by=self.super_user, confirmed_by=self.super_user,
        )
        self.rec1.schedule_batch_id = head.batch_id
        self.rec1.save(update_fields=["schedule_batch_id"])
        summaries = selectors.batch_summaries(self.team1, start, end)
        self.assertEqual(len(summaries), 1)
        self.assertTrue(summaries[0]["is_current"])
        self.assertFalse(summaries[0]["is_manual"])
        # 版本头作废 + 记录作废后，不再标为当前版本
        head.status = ScheduleBatch.Status.SUPERSEDED
        head.save(update_fields=["status"])
        self.rec1.status = ScheduleRecord.Status.SUPERSEDED
        self.rec1.save(update_fields=["status"])
        summaries = selectors.batch_summaries(self.team1, start, end)
        self.assertFalse(summaries[0]["is_current"])


class AutoSupersedeTests(PhaseOneFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()
        # 版本替换测试只关注“有批次记录的旧版本”，先移除夹具中来源不明的记录
        ScheduleRecord.objects.filter(team=cls.team1).delete()

    def test_confirm_auto_supersedes_previous_version(self):
        first = create_schedule_draft(self.config1, self.super_user, time_limit_seconds=30)["draft"]
        self.assertTrue(confirm_schedule_draft(first.batch_id, self.super_user)["confirmed"])
        second = create_schedule_draft(self.config1, self.super_user, time_limit_seconds=30)["draft"]
        result = confirm_schedule_draft(second.batch_id, self.super_user)  # 未显式传参
        self.assertTrue(result["confirmed"], result.get("errors"))
        first.refresh_from_db()
        self.assertEqual(first.status, ScheduleBatch.Status.SUPERSEDED)
        self.assertEqual(
            ScheduleRecord.objects.filter(schedule_batch_id=first.batch_id, status="superseded").count(),
            len(first.payload),
        )
        active = ScheduleRecord.objects.filter(status="confirmed", is_active=True)
        self.assertEqual({r.schedule_batch_id for r in active}, {second.batch_id})

    def test_auto_supersede_does_not_touch_other_org_or_period(self):
        other_rec = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_b, team=self.team_b, shift_config=self.config_b,
            employee=self.emp_b, work_date=DAY,
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        old_period_rec = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_a, team=self.team1, shift_config=self.config1,
            employee=self.emp1, work_date=datetime.date(2026, 8, 26),
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        first = create_schedule_draft(self.config1, self.super_user, time_limit_seconds=30)["draft"]
        self.assertTrue(confirm_schedule_draft(first.batch_id, self.super_user)["confirmed"])
        # 生成并确认新版本（同队组同周期）→ 自动作废上一版本
        second = create_schedule_draft(self.config1, self.super_user, time_limit_seconds=30)["draft"]
        self.assertTrue(confirm_schedule_draft(second.batch_id, self.super_user)["confirmed"])
        other_rec.refresh_from_db()
        old_period_rec.refresh_from_db()
        self.assertEqual(other_rec.status, ScheduleRecord.Status.CONFIRMED)   # 其他队组不受影响
        self.assertEqual(old_period_rec.status, ScheduleRecord.Status.CONFIRMED)  # 其他周期不受影响

    def test_confirm_failure_keeps_old_version_valid(self):
        first = create_schedule_draft(self.config1, self.super_user, time_limit_seconds=30)["draft"]
        self.assertTrue(confirm_schedule_draft(first.batch_id, self.super_user)["confirmed"])
        second = create_schedule_draft(self.config1, self.super_user, time_limit_seconds=30)["draft"]
        # 让新草稿失效（配置变更）→ 确认失败
        self.config1.required_shifts = 5
        self.config1.save(update_fields=["required_shifts"])
        result = confirm_schedule_draft(second.batch_id, self.super_user)
        self.assertFalse(result["confirmed"])
        first.refresh_from_db()
        self.assertEqual(first.status, ScheduleBatch.Status.CONFIRMED)
        self.assertEqual(
            ScheduleRecord.objects.filter(schedule_batch_id=first.batch_id, status="confirmed").count(),
            len(first.payload),
        )


class AddFailureCleanupTests(PhaseOneFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def test_failed_add_leaves_no_new_draft_batch(self):
        before = set(ScheduleBatch.objects.values_list("id", flat=True))
        # 目标班组：检修班；emp2 当天无记录 → 添加成功路径先验证一次正常
        ok = add_manual_schedule(self.config1, DAY, self.emp2, self.admin_a)
        self.assertEqual(ok["errors"], [])
        # 再对同一人同一天添加 → 失败，且不留下新的草稿批次
        before2 = set(ScheduleBatch.objects.values_list("id", flat=True))
        fail = add_manual_schedule(self.config1, DAY, self.emp2, self.admin_a)
        self.assertTrue(fail["errors"])
        self.assertEqual(set(ScheduleBatch.objects.values_list("id", flat=True)), before2)
        self.assertTrue(before <= before2)
        # 历史草稿未被触碰
        self.assertEqual(ScheduleBatch.objects.filter(status=ScheduleBatch.Status.DRAFT).count(), 0)

    def test_add_then_remove_keeps_board_and_calendar_consistent(self):
        self.client.force_login(self.admin_a)
        add_manual_schedule(self.config1, DAY, self.emp2, self.admin_a)
        board = selectors.build_shift_board(DAY, DAY, org=self.org_a)
        early = next(s for s in board["days"][0]["shifts"] if s["key"] == "early")
        names = [p["name"] for t in early["teams"] for p in t["people"]]
        self.assertIn("李某某", names)
        cal = self.client.get(
            reverse("personnel:employee_calendar", args=[self.emp2.pk]), {"period": "2026-09"}
        )
        day_cell = next(d for d in cal.context["days"] if d["date"] == DAY)
        self.assertIsNotNone(day_cell["record"])
        self.assertEqual(day_cell["shift_type"], "early")
        # 删除后两边同步消失 / 变未排班
        record = day_cell["record"]
        self.client.post(reverse("scheduler:shift_board_remove", args=[record.pk]), {})
        board = selectors.build_shift_board(DAY, DAY, org=self.org_a)
        early = next(s for s in board["days"][0]["shifts"] if s["key"] == "early")
        names = [p["name"] for t in early["teams"] for p in t["people"]]
        self.assertNotIn("李某某", names)
        cal = self.client.get(
            reverse("personnel:employee_calendar", args=[self.emp2.pk]), {"period": "2026-09"}
        )
        day_cell = next(d for d in cal.context["days"] if d["date"] == DAY)
        self.assertIsNone(day_cell["record"])


class ShiftChangeTests(PhaseOneFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def setUp(self):
        self.client.force_login(self.admin_a)

    def _change(self, record, target):
        return change_schedule_record(record, target, self.admin_a)

    def _current(self, employee=None):
        qs = ScheduleRecord.objects.filter(
            employee=employee or self.emp1, work_date=DAY, status="confirmed", is_active=True
        ).exclude(shift_type=ScheduleRecord.ShiftType.REST)
        return qs.first()

    def test_early_to_middle(self):
        result = change_schedule_record(self.rec1, "middle", self.admin_a)
        self.assertTrue(result["changed"], result)
        current = self._current()
        self.assertEqual(current.shift_type, "middle")
        self.assertEqual(current.shift_config.shift_name, "middle")
        self.rec1.refresh_from_db()
        self.assertTrue(self.rec1.is_active)  # P2：原地更新同一条记录
        self.assertEqual(self.rec1.shift_type, "middle")
        self.assertEqual(
            ScheduleRecord.objects.filter(
                employee=self.emp1, work_date=DAY, status="confirmed", is_active=True
            ).count(),
            1,
        )
        log = OperationLog.objects.filter(action="change").last()
        self.assertIn("早班 → 中班", log.description)

    def test_middle_to_night(self):
        change_schedule_record(self.rec1, "middle", self.admin_a)
        current = self._current()
        result = change_schedule_record(current, "night", self.admin_a)
        self.assertTrue(result["changed"], result)
        self.assertEqual(self._current().shift_type, "night")

    def test_work_to_rest_shows_rest_in_calendar(self):
        self.assertTrue(change_schedule_record(self.rec1, "rest", self.admin_a)["changed"])
        self.assertIsNone(self._current())  # 无有效上班记录
        cal = self.client.get(
            reverse("personnel:employee_calendar", args=[self.emp1.pk]), {"period": "2026-09"}
        )
        cell = next(d for d in cal.context["days"] if d["date"] == DAY)
        self.assertIsNotNone(cell["record"])
        self.assertEqual(cell["shift_type"], ScheduleRecord.ShiftType.REST)  # 明确显示休息
        board = selectors.build_shift_board(DAY, DAY, org=self.org_a)
        names = [
            p["name"] for s in board["days"][0]["shifts"] for t in s["teams"] for p in t["people"]
        ]
        self.assertNotIn("张某某", names)  # 看板不再显示为上班

    def test_rest_to_work(self):
        rest_rec = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_a, team=self.team1, shift_config=self.config1,
            employee=self.emp2, work_date=DAY,
            shift_type=ScheduleRecord.ShiftType.REST,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        result = change_schedule_record(rest_rec, "night", self.admin_a)
        self.assertTrue(result["changed"], result)
        current = self._current(self.emp2)
        self.assertEqual(current.shift_type, "night")
        board = selectors.build_shift_board(DAY, DAY, org=self.org_a)
        night = next(s for s in board["days"][0]["shifts"] if s["key"] == "night")
        self.assertIn("李某某", [p["name"] for t in night["teams"] for p in t["people"]])

    def test_calendar_and_board_sync_after_change(self):
        change_schedule_record(self.rec1, "night", self.admin_a)
        cal = self.client.get(
            reverse("personnel:employee_calendar", args=[self.emp1.pk]), {"period": "2026-09"}
        )
        cell = next(d for d in cal.context["days"] if d["date"] == DAY)
        self.assertEqual(cell["shift_type"], "night")
        board = selectors.build_shift_board(DAY, DAY, org=self.org_a)
        night_names = [
            p["name"]
            for s in board["days"][0]["shifts"] if s["key"] == "night"
            for t in s["teams"] for p in t["people"]
        ]
        early_names = [
            p["name"]
            for s in board["days"][0]["shifts"] if s["key"] == "early"
            for t in s["teams"] for p in t["people"]
        ]
        self.assertIn("张某某", night_names)
        self.assertNotIn("张某某", early_names)

    def test_change_via_http_and_persists_after_refresh(self):
        response = self.client.post(
            reverse("scheduler:shift_board_change", args=[self.rec1.pk]),
            {"target_shift": "middle", "next": "/scheduler/shift-board/"},
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get(reverse("scheduler:shift_board"), {"period": "2026-09"})
        middle = next(
            s for d in page.context["board"]["days"] if d["date"] == DAY
            for s in d["shifts"] if s["key"] == "middle"
        )
        self.assertIn("张某某", [p["name"] for t in middle["teams"] for p in t["people"]])

    def test_no_duplicate_or_conflicting_active_records(self):
        change_schedule_record(self.rec1, "middle", self.admin_a)
        current = self._current()
        change_schedule_record(current, "night", self.admin_a)
        active = ScheduleRecord.objects.filter(
            employee=self.emp1, work_date=DAY, status="confirmed", is_active=True
        )
        work = active.exclude(shift_type=ScheduleRecord.ShiftType.REST)
        self.assertEqual(work.count(), 1)
        self.assertEqual(work.first().shift_type, "night")

    def test_old_batch_record_does_not_reappear(self):
        # 让 emp2 有一条旧批次早班记录，改班后旧记录不得重新出现在看板
        old = create_schedule_record(
            schedule_batch_id=new_schedule_batch_id(),
            organization=self.org_a, team=self.team1, shift_config=self.config1,
            employee=self.emp2, work_date=DAY, shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        change_schedule_record(old, "night", self.admin_a)
        board = selectors.build_shift_board(DAY, DAY, org=self.org_a)
        early_names = [
            p["name"]
            for s in board["days"][0]["shifts"] if s["key"] == "early"
            for t in s["teams"] for p in t["people"]
        ]
        self.assertNotIn("李某某", early_names)

    def test_member_and_cross_org_cannot_change(self):
        self.client.force_login(self.member_a)
        response = self.client.post(
            reverse("scheduler:shift_board_change", args=[self.rec1.pk]), {"target_shift": "middle"}
        )
        self.assertEqual(response.status_code, 403)
        self.rec1.refresh_from_db()
        self.assertEqual(self.rec1.shift_type, "early")
        self.assertTrue(self.rec1.is_active)

        admin_b = make_user("p_admin_b", role="admin", organization=self.org_b)
        self.client.force_login(admin_b)
        self.assertEqual(
            self.client.post(
                reverse("scheduler:shift_board_change", args=[self.rec1.pk]), {"target_shift": "middle"}
            ).status_code,
            404,
        )

    def test_invalid_target_rolls_back(self):
        result = change_schedule_record(self.rec1, "bogus", self.admin_a)
        self.assertFalse(result["changed"])
        self.rec1.refresh_from_db()
        self.assertEqual(self.rec1.shift_type, "early")
        self.assertTrue(self.rec1.is_active)

    def test_noop_change_is_reported(self):
        result = change_schedule_record(self.rec1, "early", self.admin_a)
        self.assertFalse(result["changed"])
        self.assertTrue(result.get("noop"))
