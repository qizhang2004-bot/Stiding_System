# 归档门禁与 payload 数据校验加固（I1/I2/I3）：结构预检、日期预检、零写入与 HTTP 提示。
import datetime

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.drafting import (
    archive_period,
    confirm_schedule_draft,
    create_schedule_draft,
    generate_and_apply,
    validate_adjustment_payload,
    validate_batch_payload,
)
from apps.scheduler.models import ScheduleBatch, ScheduleRecord

User = get_user_model()
ARCHIVED_PERIOD = (datetime.date(2026, 6, 25), datetime.date(2026, 7, 24))  # 已结束→可归档
CUR_PERIOD = (datetime.date(2026, 9, 25), datetime.date(2026, 10, 24))


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class PayloadGateFixtureMixin:
    @classmethod
    def _build(cls):
        cls.super_user = make_user("pg_super")
        cls.org = OrganizationUnit.objects.create(name="甲队", code="PGA")
        cls.team = Team.objects.create(organization=cls.org, name="检修班", code="PGT1")
        cls.config = cls.team.shift_configs.get(shift_name="early")
        cls.config.daily_worker_count = 2
        cls.config.required_shifts = 2
        cls.config.is_active = True
        cls.config.save(update_fields=["daily_worker_count", "required_shifts", "is_active"])
        cls.employees = [
            Employee.objects.create(
                employee_no=f"PG{i:02d}", name=f"预检{i}", team=cls.team,
                shift_config=cls.config, min_required_shifts=2,
            )
            for i in range(1, 4)
        ]

    @classmethod
    def _generate(cls, period_start):
        outcome = generate_and_apply(
            cls.config, cls.super_user, anchor=period_start, time_limit_seconds=60
        )
        assert outcome["applied"], outcome["errors"]
        return outcome

    def _make_draft(self, payload, *, kind=ScheduleBatch.Kind.PERIOD,
                    period=ARCHIVED_PERIOD, status=ScheduleBatch.Status.DRAFT):
        return ScheduleBatch.objects.create(
            organization=self.org, team=self.team, shift_config=self.config,
            period_start=period[0], period_end=period[1], mode="tight",
            daily_worker_count=1, position_requirements={},
            employee_targets={str(self.employees[0].pk): 1}, employee_qualifications={},
            payload=payload, summary={}, kind=kind, status=status,
            created_by=self.super_user, confirmed_by=self.super_user,
        )

    @staticmethod
    def _records_snapshot():
        return list(
            ScheduleRecord.objects.order_by("id").values_list(
                "id", "employee_id", "work_date", "shift_type", "status", "is_active",
                "schedule_batch_id",
            )
        )

    @staticmethod
    def _batch_snapshot():
        return list(
            ScheduleBatch.objects.order_by("id").values_list("batch_id", "kind", "status")
        )

    def _assert_rejected_without_writes(self, draft, expect_text):
        records_before = self._records_snapshot()
        batches_before = self._batch_snapshot()
        result = confirm_schedule_draft(draft.batch_id, self.super_user)
        self.assertFalse(result["confirmed"])
        joined = " ".join(result["errors"])
        self.assertIn(expect_text, joined)
        self.assertEqual(self._records_snapshot(), records_before)   # 零写入
        self.assertEqual(self._batch_snapshot(), batches_before)     # 批次状态不变
        draft.refresh_from_db()
        self.assertEqual(draft.status, ScheduleBatch.Status.DRAFT)
        return result


class PayloadDateHardeningTests(PayloadGateFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls._build()
        cls._generate(ARCHIVED_PERIOD[0])
        archive_period(cls.team, *ARCHIVED_PERIOD, cls.super_user)   # 形成已归档周期

    # 1) 多周期 payload：一个已归档 + 一个正常周期 → 整批拒绝
    def test_multi_period_payload_rejected_as_whole(self):
        archived_day = ARCHIVED_PERIOD[0] + datetime.timedelta(days=1)
        normal_day = CUR_PERIOD[1]
        draft = self._make_draft(
            [
                {"employee_id": self.employees[0].pk, "work_date": archived_day.isoformat(),
                 "shift_type": "early", "position": ""},
                {"employee_id": self.employees[1].pk, "work_date": normal_day.isoformat(),
                 "shift_type": "early", "position": ""},
            ],
            period=(ARCHIVED_PERIOD[0], CUR_PERIOD[1]),  # 横跨已归档周期与正常周期
        )
        # 结构预检通过后，周期门禁必须逐周期检查并拦住其中的已归档周期
        result = self._assert_rejected_without_writes(draft, "已归档")
        self.assertTrue(any("已归档" in e for e in result["errors"]))
        # 正常周期那一天也不得被写入
        self.assertFalse(
            ScheduleRecord.objects.filter(
                work_date=normal_day, status=ScheduleRecord.Status.CONFIRMED, is_active=True
            ).exists()
        )

    # 2) 缺失日期 / None
    def test_missing_work_date_rejected(self):
        draft = self._make_draft([
            {"employee_id": self.employees[0].pk, "shift_type": "early", "position": ""}
        ])
        self._assert_rejected_without_writes(draft, "缺少排班日期")

    def test_none_work_date_rejected(self):
        draft = self._make_draft([
            {"employee_id": self.employees[0].pk, "work_date": None,
             "shift_type": "early", "position": ""}
        ])
        self._assert_rejected_without_writes(draft, "缺少排班日期")

    # 3) 非法日期字符串与无法解析的值
    def test_invalid_date_string_rejected(self):
        for bad in ("abc", "2026-13-45", "2026/09/26"):
            with self.subTest(bad=bad):
                draft = self._make_draft([
                    {"employee_id": self.employees[0].pk, "work_date": bad,
                     "shift_type": "early", "position": ""}
                ])
                self._assert_rejected_without_writes(draft, "无法解析")

    def test_unparseable_date_value_rejected(self):
        for bad in ({"y": 2026}, [2026, 9, 26], 20260926):
            with self.subTest(bad=bad):
                draft = self._make_draft([
                    {"employee_id": self.employees[0].pk, "work_date": bad,
                     "shift_type": "early", "position": ""}
                ])
                self._assert_rejected_without_writes(draft, "无效")

    # 4) 错误结构：非列表 / 元素非字典 / 空列表
    def test_non_list_payload_rejected(self):
        for bad in ({"work_date": "2026-07-01"}, "2026-07-01", 42):
            with self.subTest(bad=type(bad).__name__):
                draft = self._make_draft(bad)
                self._assert_rejected_without_writes(draft, "格式错误")

    def test_non_dict_item_rejected(self):
        draft = self._make_draft(["2026-07-01", 2026])
        self._assert_rejected_without_writes(draft, "不是有效的数据项")

    def test_empty_payload_rejected(self):
        draft = self._make_draft([])
        self._assert_rejected_without_writes(draft, "格式错误")

    # 5) 周期外日期（period 与 adjustment 两个校验器 / 两条确认分支）
    def test_out_of_period_date_rejected_period_branch(self):
        out_day = ARCHIVED_PERIOD[1] + datetime.timedelta(days=40)
        draft = self._make_draft([
            {"employee_id": self.employees[0].pk, "work_date": out_day.isoformat(),
             "shift_type": "early", "position": ""}
        ])
        self._assert_rejected_without_writes(draft, "超出草稿周期")

    def test_out_of_period_date_rejected_adjustment_branch(self):
        out_day = ARCHIVED_PERIOD[1] + datetime.timedelta(days=40)
        draft = self._make_draft(
            [{"employee_id": self.employees[0].pk, "work_date": out_day.isoformat(),
              "shift_type": "early", "position": ""}],
            kind=ScheduleBatch.Kind.ADJUSTMENT,
        )
        self._assert_rejected_without_writes(draft, "超出草稿周期")

    def test_validators_are_robust_to_malformed_payload(self):
        # 直接调用两个校验器也不得抛异常（None 日期曾导致 date <= None 的 TypeError）
        for kind in (ScheduleBatch.Kind.PERIOD, ScheduleBatch.Kind.ADJUSTMENT):
            with self.subTest(kind=kind):
                draft = self._make_draft(
                    [{"employee_id": self.employees[0].pk, "work_date": None,
                      "shift_type": "early"}],
                    kind=kind,
                )
                if kind == ScheduleBatch.Kind.PERIOD:
                    errors = validate_batch_payload(draft)
                else:
                    errors = validate_adjustment_payload(draft)
                self.assertTrue(errors)
                self.assertIn("缺少排班日期", " ".join(errors))

    def test_adjustment_missing_date_rejected_without_writes(self):
        draft = self._make_draft(
            [{"employee_id": self.employees[0].pk, "shift_type": "early"}],
            kind=ScheduleBatch.Kind.ADJUSTMENT,
        )
        self._assert_rejected_without_writes(draft, "缺少排班日期")

    # 7) HTTP：预期输入错误不得返回 500
    def test_http_confirm_reports_business_error_not_500(self):
        self.client.force_login(self.super_user)
        bad_payloads = [
            ([{"employee_id": self.employees[0].pk, "shift_type": "early"}], "缺少排班日期"),
            ([{"employee_id": self.employees[0].pk, "work_date": "abc", "shift_type": "early"}], "无法解析"),
            ({"work_date": "2026-07-01"}, "格式错误"),
            ([], "格式错误"),
            (["2026-07-01"], "不是有效的数据项"),
        ]
        for payload, expect in bad_payloads:
            with self.subTest(expect=expect):
                draft = self._make_draft(payload)
                response = self.client.post(
                    reverse("scheduler:schedule_draft_detail", args=[draft.batch_id]),
                    {"action": "confirm"},
                )
                self.assertEqual(response.status_code, 302)   # 业务错误 → 回跳提示，而非 500
                messages = [str(m) for m in get_messages(response.wsgi_request)]
                self.assertTrue(any(expect in m for m in messages), messages)
                draft.refresh_from_db()
                self.assertEqual(draft.status, ScheduleBatch.Status.DRAFT)

    # 8) 正常流程回归：合法 period / adjustment 仍按规则运行
    def test_valid_period_draft_still_confirms(self):
        start, end = CUR_PERIOD
        self._generate(start)  # 建立当前周期版本（合法确认的前提之一）
        draft = create_schedule_draft(self.config, self.super_user, anchor=start,
                                      time_limit_seconds=60)["draft"]
        result = confirm_schedule_draft(draft.batch_id, self.super_user)
        self.assertTrue(result["confirmed"], result.get("errors"))

    def test_valid_adjustment_on_ended_unarchived_period_still_confirms(self):
        # 已结束但未归档：合法补充排班仍可确认（规则 2）
        ended_unarchived = (datetime.date(2026, 8, 25), datetime.date(2026, 9, 24))
        self._generate(ended_unarchived[0])  # 该周期已结束且未被归档
        day = ended_unarchived[1]
        existing = ScheduleRecord.objects.filter(
            team=self.team, employee=self.employees[2], work_date=day
        ).first()
        if existing:
            existing.is_active = False
            existing.save(update_fields=["is_active"])
        draft = self._make_draft(
            [{"employee_id": self.employees[2].pk, "work_date": day.isoformat(),
              "shift_type": "early", "position": ""}],
            kind=ScheduleBatch.Kind.ADJUSTMENT, period=(day, day),
        )
        result = confirm_schedule_draft(draft.batch_id, self.super_user)
        self.assertTrue(result["confirmed"], result.get("errors"))
        self.assertTrue(
            ScheduleRecord.objects.filter(
                team=self.team, employee=self.employees[2], work_date=day,
                status=ScheduleRecord.Status.CONFIRMED, is_active=True,
            ).exists()
        )
