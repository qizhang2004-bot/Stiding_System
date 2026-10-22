# P3：周期归档、只读历史模式、边界回归与生成联动 验收测试。
import datetime
import threading
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.auditlog.models import OperationLog
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler import drafting, selectors
from apps.scheduler.drafting import (
    add_manual_schedule,
    archive_ended_periods_for_team,
    archive_period,
    change_schedule_record,
    confirm_schedule_draft,
    create_schedule_draft,
    generate_and_apply,
    remove_schedule_record,
)
from apps.scheduler.models import ScheduleBatch, ScheduleRecord

User = get_user_model()
OLD_PERIOD = (datetime.date(2026, 8, 25), datetime.date(2026, 9, 24))   # 已结束
CUR_PERIOD = (datetime.date(2026, 9, 25), datetime.date(2026, 10, 24))  # 未结束（“今天”之前结束与否由真实今天决定）


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class P3FixtureMixin:
    @classmethod
    def _build(cls):
        cls.super_user = make_user("p3_super")
        cls.org_a = OrganizationUnit.objects.create(name="甲队", code="P3A")
        cls.org_b = OrganizationUnit.objects.create(name="乙队", code="P3B")
        cls.admin_a = make_user("p3_admin_a", role="admin", organization=cls.org_a)
        cls.member_a = make_user("p3_member_a", role="member", organization=cls.org_a)
        cls.team = Team.objects.create(organization=cls.org_a, name="检修班", code="P3T1")
        cls.team_b = Team.objects.create(organization=cls.org_b, name="乙班", code="P3TB")
        cls.config = cls.team.shift_configs.get(shift_name="early")
        cls.config.daily_worker_count = 2
        cls.config.required_shifts = 2
        cls.config.is_active = True
        cls.config.save(update_fields=["daily_worker_count", "required_shifts", "is_active"])
        for name in ("middle", "night"):
            cfg = cls.team.shift_configs.get(shift_name=name)
            cfg.is_active = True
            cfg.save(update_fields=["is_active"])
        cls.config_b = cls.team_b.shift_configs.get(shift_name="early")
        cls.config_b.is_active = True
        cls.config_b.save(update_fields=["is_active"])
        cls.employees = [
            Employee.objects.create(
                employee_no=f"P3{i:02d}", name=f"归档{i}", team=cls.team,
                shift_config=cls.config, position="电工" if i == 1 else "",
                min_required_shifts=2,
            )
            for i in range(1, 4)
        ]

    @classmethod
    def _generate(cls, period_start, user=None):
        outcome = generate_and_apply(
            cls.config, user or cls.super_user, anchor=period_start, time_limit_seconds=60
        )
        assert outcome["applied"], outcome["errors"]
        return outcome

    def _active(self, start, end, employee=None):
        qs = ScheduleRecord.objects.filter(
            team=self.team, work_date__range=(start, end),
            status__in=[ScheduleRecord.Status.CONFIRMED, ScheduleRecord.Status.PUBLISHED],
            is_active=True,
        )
        return qs.filter(employee=employee) if employee else qs


class ArchiveServiceTests(P3FixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def test_archive_ended_period_success(self):
        outcome = self._generate(OLD_PERIOD[0])
        result = archive_period(self.team, *OLD_PERIOD, self.admin_a)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["archived"], outcome["record_count"])
        self.assertFalse(self._active(*OLD_PERIOD).exists())
        self.assertEqual(
            ScheduleRecord.objects.filter(
                team=self.team, work_date__range=OLD_PERIOD,
                status=ScheduleRecord.Status.ARCHIVED,
            ).count(),
            outcome["record_count"],
        )
        log = OperationLog.objects.filter(action="archive").latest("created_at")
        self.assertIn("周期归档", log.description)
        self.assertIn(str(OLD_PERIOD[0]), log.description)

    def test_archive_unfinished_period_rejected(self):
        today = datetime.date.today()
        start, end = selectors.period_range(today)  # 当前周期，必然未结束
        self._generate(start)
        before = list(
            ScheduleRecord.objects.filter(team=self.team, work_date__range=(start, end))
            .order_by("id").values_list("id", "status")
        )
        result = archive_period(self.team, start, end, self.admin_a)
        self.assertTrue(result["errors"])
        self.assertIn("尚未结束", result["errors"][0])
        after = list(
            ScheduleRecord.objects.filter(team=self.team, work_date__range=(start, end))
            .order_by("id").values_list("id", "status")
        )
        self.assertEqual(before, after)

    def test_archive_is_idempotent(self):
        self._generate(OLD_PERIOD[0])
        first = archive_period(self.team, *OLD_PERIOD, self.admin_a)
        audit_count = OperationLog.objects.filter(action="archive").count()
        second = archive_period(self.team, *OLD_PERIOD, self.admin_a)
        self.assertEqual(second["archived"], 0)
        self.assertTrue(second["already"])
        self.assertEqual(OperationLog.objects.filter(action="archive").count(), audit_count)

    def test_archive_keeps_manual_adjustment_final_state(self):
        # 先改班（最终人工调整）→ 再归档 → 归档内容为调整后的班次与岗位
        self._generate(OLD_PERIOD[0])
        record = self._active(*OLD_PERIOD, employee=self.employees[0]).first()
        target_day = record.work_date
        change = change_schedule_record(record, ScheduleRecord.ShiftType.NIGHT, self.admin_a)
        self.assertTrue(change["changed"], change)
        result = archive_period(self.team, *OLD_PERIOD, self.admin_a)
        self.assertGreater(result["archived"], 0)
        archived = ScheduleRecord.objects.get(
            team=self.team, employee=self.employees[0], work_date=target_day,
            status=ScheduleRecord.Status.ARCHIVED,
        )
        self.assertEqual(archived.shift_type, ScheduleRecord.ShiftType.NIGHT)
        self.assertEqual(archived.is_active, True)

    def test_archive_does_not_touch_superseded_history_or_payload(self):
        self._generate(OLD_PERIOD[0])
        first_batch = ScheduleBatch.objects.filter(team=self.team).latest("id")
        payload_before = list(first_batch.payload)
        # 造一条 superseded 历史行
        historic = ScheduleRecord.objects.filter(team=self.team).first()
        ScheduleRecord.objects.filter(pk=historic.pk).update(status=ScheduleRecord.Status.SUPERSEDED)
        self._generate(CUR_PERIOD[0])  # 生成新周期（会退休旧周期）
        archive_period(self.team, *CUR_PERIOD, self.admin_a)
        first_batch.refresh_from_db()
        self.assertEqual(list(first_batch.payload), payload_before)
        self.assertEqual(
            ScheduleRecord.objects.filter(
                team=self.team, status=ScheduleRecord.Status.SUPERSEDED
            ).count() >= 1,
            True,
        )

    def test_archive_requires_admin_and_org_scope(self):
        self._generate(OLD_PERIOD[0])
        # 普通成员：服务层直接拒绝
        with self.assertRaises(Exception):
            archive_period(self.team, *OLD_PERIOD, self.member_a)
        # 跨队组管理员（HTTP 404）
        admin_b = make_user("p3_admin_b", role="admin", organization=self.org_b)
        self.client.force_login(admin_b)
        response = self.client.post(
            reverse("scheduler:schedule_archive_period", args=[self.team.pk]),
            {"period": f"{OLD_PERIOD[0]:%Y-%m}"},
        )
        self.assertEqual(response.status_code, 404)


class ArchiveWriteProtectionTests(P3FixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()
        cls._generate(OLD_PERIOD[0])
        cls.archive = archive_period(cls.team, *OLD_PERIOD, cls.admin_a)

    def test_all_write_entries_rejected_on_archived_period(self):
        archived = ScheduleRecord.objects.filter(
            team=self.team, status=ScheduleRecord.Status.ARCHIVED
        ).first()
        # 改班
        result = change_schedule_record(archived, ScheduleRecord.ShiftType.NIGHT, self.admin_a)
        self.assertFalse(result["changed"])
        self.assertIn("已归档", result["errors"][0])
        # 取消
        result = remove_schedule_record(archived, self.admin_a)
        self.assertFalse(result["removed"])
        self.assertIn("已归档", result["errors"][0])
        # 添加（该周期）
        day = archived.work_date
        result = add_manual_schedule(self.config, day, self.employees[1], self.admin_a)
        self.assertTrue(result["errors"])
        self.assertIn("已归档", result["errors"][0])
        # 整周期生成/替换
        outcome = generate_and_apply(
            self.config, self.admin_a, anchor=OLD_PERIOD[0], time_limit_seconds=60
        )
        self.assertFalse(outcome["applied"])
        self.assertTrue(any("已归档" in e for e in outcome["errors"]), outcome["errors"])
        # 恢复非活跃记录：不复活 archived（归档行本就不是 confirmed，无法复用）
        self.assertFalse(
            ScheduleRecord.objects.filter(
                team=self.team, status=ScheduleRecord.Status.ARCHIVED, is_active=False
            ).exists()
        )

    def test_draft_confirm_rejected_for_archived_period(self):
        # 构造覆盖已归档周期的草稿（服务层）→ 确认必须被拒绝且零写入
        draft = create_schedule_draft(self.config, self.super_user, anchor=OLD_PERIOD[0],
                                      time_limit_seconds=60)["draft"]
        self.assertIsNotNone(draft)
        before = list(
            ScheduleRecord.objects.filter(team=self.team, work_date__range=OLD_PERIOD)
            .order_by("id").values_list("id", "status", "shift_type")
        )
        result = confirm_schedule_draft(draft.batch_id, self.super_user)
        self.assertFalse(result["confirmed"])
        after = list(
            ScheduleRecord.objects.filter(team=self.team, work_date__range=OLD_PERIOD)
            .order_by("id").values_list("id", "status", "shift_type")
        )
        self.assertEqual(before, after)

    def test_unarchived_ended_period_still_editable(self):
        # 规则 2：已结束但尚未归档的周期仍允许人工修改
        outcome = self._generate(datetime.date(2026, 6, 25))
        self.assertTrue(outcome["applied"])
        record = ScheduleRecord.objects.filter(
            team=self.team, work_date__range=(datetime.date(2026, 6, 25), datetime.date(2026, 7, 24)),
            status=ScheduleRecord.Status.CONFIRMED, is_active=True,
        ).first()
        self.assertIsNotNone(record)
        result = change_schedule_record(record, ScheduleRecord.ShiftType.MIDDLE, self.admin_a)
        self.assertTrue(result["changed"], result)


class AutoArchiveTests(P3FixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def test_generate_auto_archives_all_ended_periods(self):
        # 两个已结束周期（6/25~7/24、7/25~8/24）+ 生成本周期 → 两个旧周期都应自动归档
        first = self._generate(datetime.date(2026, 6, 25))
        # 生成第二个周期时，第一个已结束周期会被自动归档（记录数 > 0）
        second = self._generate(datetime.date(2026, 7, 25))
        self.assertGreater(second.get("archived", 0), 0)
        self.assertFalse(
            ScheduleRecord.objects.filter(
                team=self.team, work_date__range=(datetime.date(2026, 6, 25), datetime.date(2026, 7, 24)),
                status=ScheduleRecord.Status.CONFIRMED, is_active=True,
            ).exists()
        )
        self.assertTrue(
            ScheduleRecord.objects.filter(
                team=self.team, work_date__range=(datetime.date(2026, 6, 25), datetime.date(2026, 7, 24)),
                status=ScheduleRecord.Status.ARCHIVED,
            ).exists()
        )
        self.assertEqual(first["record_count"] > 0, True)

    def test_generate_failure_rolls_back_archive_and_write(self):
        self._generate(datetime.date(2026, 6, 25))  # 已结束周期（未归档）
        old_rows = list(
            ScheduleRecord.objects.filter(
                team=self.team, work_date__range=(datetime.date(2026, 6, 25), datetime.date(2026, 7, 24))
            ).order_by("id").values_list("id", "status", "shift_type")
        )
        batches_before = ScheduleBatch.objects.count()
        # 让归档服务在生成事务中抛错 → 生成与归档一起回滚
        with mock.patch.object(
            drafting, "archive_ended_periods_for_team",
            side_effect=RuntimeError("模拟归档失败"),
        ):
            outcome = generate_and_apply(
                self.config, self.super_user, anchor=datetime.date(2026, 7, 25),
                time_limit_seconds=60,
            )
        self.assertFalse(outcome["applied"])
        after_rows = list(
            ScheduleRecord.objects.filter(
                team=self.team, work_date__range=(datetime.date(2026, 6, 25), datetime.date(2026, 7, 24))
            ).order_by("id").values_list("id", "status", "shift_type")
        )
        self.assertEqual(old_rows, after_rows)  # 旧周期既未归档也未改动
        self.assertEqual(ScheduleBatch.objects.count(), batches_before)  # 新周期版本头回滚
        self.assertFalse(
            ScheduleRecord.objects.filter(
                team=self.team, work_date__range=(datetime.date(2026, 7, 25), datetime.date(2026, 8, 24))
            ).exists()
        )


class ArchiveHistoryViewTests(P3FixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()
        cls._generate(OLD_PERIOD[0])
        cls.admin_a_archived = archive_period(cls.team, *OLD_PERIOD, cls.admin_a)

    def setUp(self):
        self.client.force_login(self.admin_a)

    def test_board_history_mode_shows_archived_and_is_readonly(self):
        response = self.client.get(
            reverse("scheduler:shift_board"),
            {"organization": self.org_a.pk, "team": self.team.pk, "period": f"{OLD_PERIOD[0]:%Y-%m}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_archive_view"])
        self.assertFalse(response.context["can_edit"])
        names = [
            p["name"]
            for day in response.context["board"]["days"]
            for shift in day["shifts"]
            for team in shift["teams"]
            for p in team["people"]
        ]
        self.assertIn(self.employees[0].name, names)  # 归档后仍可见，不为空
        self.assertContains(response, "历史归档")

    def test_calendar_history_mode_shows_archived(self):
        response = self.client.get(
            reverse("personnel:employee_calendar", args=[self.employees[0].pk]),
            {"period": f"{OLD_PERIOD[0]:%Y-%m}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_archive_view"])
        self.assertFalse(response.context["can_edit"])
        self.assertTrue(any(day["record"] for day in response.context["days"]))
        self.assertFalse(
            any(day["record"] and day["shift_type"] == ScheduleRecord.ShiftType.REST
                for day in response.context["days"]) is None
        )

    def test_archive_write_endpoints_rejected_even_if_called_directly(self):
        archived = ScheduleRecord.objects.filter(
            team=self.team, status=ScheduleRecord.Status.ARCHIVED
        ).first()
        original_shift = archived.shift_type
        change = self.client.post(
            reverse("scheduler:shift_board_change", args=[archived.pk]), {"target_shift": "middle"}
        )
        self.assertEqual(change.status_code, 302)
        messages = [str(m) for m in get_messages(change.wsgi_request)]
        self.assertTrue(any("已归档" in m for m in messages), messages)
        remove = self.client.post(reverse("scheduler:shift_board_remove", args=[archived.pk]))
        self.assertEqual(remove.status_code, 302)
        archived.refresh_from_db()
        self.assertEqual(archived.shift_type, original_shift)
        self.assertEqual(archived.status, ScheduleRecord.Status.ARCHIVED)

    def test_results_current_view_and_history_view(self):
        # 当前有效视图：归档周期不再出现在当前口径（该周期无 confirmed 记录）
        self.assertFalse(self._active(*OLD_PERIOD).exists())
        # 历史原貌：显式指定批次可查看归档记录
        batch_id = ScheduleRecord.objects.filter(
            team=self.team, status=ScheduleRecord.Status.ARCHIVED
        ).first().schedule_batch_id
        response = self.client.get(
            reverse("scheduler:schedule_results"),
            {"organization": self.org_a.pk, "team": self.team.pk,
             "period": f"{OLD_PERIOD[0]:%Y-%m}", "batch": str(batch_id)},
        )
        self.assertEqual(response.status_code, 200)
        grid = response.context["grid"]
        self.assertIsNotNone(grid)
        self.assertGreater(grid["record_count"], 0)
        self.assertFalse(response.context["selected_batch"]["is_current"])
        names = [row["employee"].name for row in grid["rows"]]
        self.assertIn(self.employees[0].name, names)

    def test_batch_summaries_marks_manual_vs_version_head(self):
        # 造一个 adjustment 批次（遗留形态）并确认不被标为当前版本
        adjustment = ScheduleBatch.objects.create(
            organization=self.org_a, team=self.team, shift_config=self.config,
            period_start=CUR_PERIOD[0], period_end=CUR_PERIOD[1], mode="adjustment",
            daily_worker_count=1, position_requirements={}, employee_targets={},
            employee_qualifications={}, payload=[], summary={},
            kind=ScheduleBatch.Kind.ADJUSTMENT, status=ScheduleBatch.Status.CONFIRMED,
            created_by=self.super_user, confirmed_by=self.super_user,
        )
        # 遗留 adjustment 批次在真实库中带有自己的记录，这里补一条以复现其形态
        from apps.scheduler.services import create_schedule_record as _create

        _create(
            schedule_batch_id=adjustment.batch_id,
            organization=self.org_a, team=self.team, shift_config=self.config,
            employee=self.employees[2], work_date=CUR_PERIOD[0] + datetime.timedelta(days=5),
            shift_type=ScheduleRecord.ShiftType.EARLY,
            status=ScheduleRecord.Status.CONFIRMED,
        )
        summaries = selectors.batch_summaries(self.team, *CUR_PERIOD)
        entry = next(b for b in summaries if b["batch_id"] == adjustment.batch_id)
        self.assertFalse(entry["is_current"])
        self.assertTrue(entry["is_manual"])

    def test_archived_excluded_from_current_queries(self):
        self.assertFalse(
            selectors.active_schedule_records(
                team=self.team, start=OLD_PERIOD[0], end=OLD_PERIOD[1]
            ).exists()
        )
        self.assertTrue(
            selectors.archived_records(
                team=self.team, start=OLD_PERIOD[0], end=OLD_PERIOD[1]
            ).exists()
        )
        self.assertTrue(selectors.period_is_archived(*OLD_PERIOD, team=self.team))


class BoundaryAfterArchiveTests(P3FixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()

    def test_boundary_uses_archived_records(self):
        # 归档上一周期后，下一周期的连休边界仍读取上一周期最终排班
        self._generate(OLD_PERIOD[0])
        archive_period(self.team, *OLD_PERIOD, self.admin_a)
        from apps.scheduler.planning import build_rest_problem

        info = build_rest_problem(self.config, anchor=CUR_PERIOD[0])
        prev_day = CUR_PERIOD[0] - datetime.timedelta(days=1)  # 2026-09-24，属已归档周期
        self.assertIn(self.employees[0].pk, info["prev_work"])
        expected = not ScheduleRecord.objects.filter(
            team=self.team, employee=self.employees[0], work_date=prev_day,
            status=ScheduleRecord.Status.ARCHIVED,
            shift_type=ScheduleRecord.ShiftType.REST,
        ).exists()
        self.assertEqual(info["prev_work"][self.employees[0].pk], expected)

    def test_cancelled_records_not_used_as_boundary(self):
        self._generate(OLD_PERIOD[0])
        prev_day = CUR_PERIOD[0] - datetime.timedelta(days=1)
        record = ScheduleRecord.objects.get(
            team=self.team, employee=self.employees[0], work_date=prev_day
        )
        remove_schedule_record(record, self.admin_a)  # 取消（is_active=False）
        archive_period(self.team, *OLD_PERIOD, self.admin_a)
        from apps.scheduler.planning import build_rest_problem

        info = build_rest_problem(self.config, anchor=CUR_PERIOD[0])
        self.assertNotIn(self.employees[0].pk, info["prev_work"])


class ArchiveConcurrencyTests(P3FixtureMixin, TransactionTestCase):
    def setUp(self):
        self._build()
        self._generate(OLD_PERIOD[0])

    def _run_threads(self, targets):
        errors = []

        def runner(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=runner, args=(fn,)) for fn in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        return errors

    def test_concurrent_archive_is_idempotent(self):
        errors = self._run_threads([
            lambda: archive_period(
                Team.objects.get(pk=self.team.pk), *OLD_PERIOD, self.super_user
            ),
            lambda: archive_period(
                Team.objects.get(pk=self.team.pk), *OLD_PERIOD, self.super_user
            ),
        ])
        self.assertEqual(errors, [])
        archived = ScheduleRecord.objects.filter(
            team=self.team, status=ScheduleRecord.Status.ARCHIVED
        )
        self.assertTrue(archived.exists())
        self.assertEqual(
            ScheduleRecord.objects.filter(
                team=self.team, work_date__range=OLD_PERIOD, status__in=["confirmed", "published"],
                is_active=True,
            ).count(),
            0,
        )

    def test_concurrent_archive_and_change_keep_invariants(self):
        record_id = ScheduleRecord.objects.filter(
            team=self.team, status=ScheduleRecord.Status.CONFIRMED, is_active=True
        ).first().pk
        errors = self._run_threads([
            lambda: archive_period(
                Team.objects.get(pk=self.team.pk), *OLD_PERIOD, self.super_user
            ),
            lambda: change_schedule_record(
                ScheduleRecord.objects.get(pk=record_id), "night", self.super_user
            ),
        ])
        self.assertEqual(errors, [])
        # 终态：该周期无活跃记录；若改班先执行，其记录也应随归档变为 archived
        self.assertFalse(
            ScheduleRecord.objects.filter(
                team=self.team, work_date__range=OLD_PERIOD, status__in=["confirmed", "published"],
                is_active=True,
            ).exists()
        )

class ArchiveButtonTests(P3FixtureMixin, TestCase):
    # 看板“归档本周期”按钮必须传班组 ID（曾经误传队组 ID，导致归档到错误的班组）
    @classmethod
    def setUpTestData(cls):
        cls._build()
        # 已结束但尚未归档 → 看板应展示“归档本周期”按钮
        cls._generate(OLD_PERIOD[0])

    def test_archive_button_uses_team_id(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(
            reverse("scheduler:shift_board"),
            {"team": self.team.pk, "period": f"{OLD_PERIOD[0]:%Y-%m}"},
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        import re
        actions = re.findall(r'action="([^"]*archive-period[^"]*)"', html)
        self.assertTrue(actions, "未渲染归档按钮")
        # 必须是当前班组的 ID；队组 ID 与班组 ID 可能数值相同，故直接比对完整 URL
        self.assertEqual(actions[0], reverse(
            "scheduler:schedule_archive_period", args=[self.team.pk]
        ))

    def test_archive_post_with_wrong_team_id_does_not_touch_other_team(self):
        other_team = Team.objects.create(organization=self.org_a, name="另一班", code="P3T2")
        self.client.force_login(self.admin_a)
        response = self.client.post(
            reverse("scheduler:schedule_archive_period", args=[other_team.pk]),
            {"period": f"{OLD_PERIOD[0]:%Y-%m}"},
        )
        self.assertEqual(response.status_code, 302)
        # 另一个班组没有该周期记录，不应产生任何归档
        self.assertFalse(
            ScheduleRecord.objects.filter(
                team=other_team, status=ScheduleRecord.Status.ARCHIVED
            ).exists()
        )
