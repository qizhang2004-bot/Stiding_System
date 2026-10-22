# 班次人员隔离最终验收：运输班早班/中班生成、替换、人工调整保留、失败不影响、重复生成、跨班次守卫。
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.drafting import (
    change_schedule_record,
    generate_and_apply,
)
from apps.scheduler.models import ScheduleBatch, ScheduleRecord

User = get_user_model()
PERIOD = datetime.date(2026, 9, 25)


def make_user(username, *, role="super", organization=None):
    user = User.objects.create_user(username, password="pw")
    user.is_superuser = user.is_staff = (role == "super")
    user.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "organization": organization}
    )
    return user


class ShiftIsolationAcceptanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("acc_super")
        cls.org = OrganizationUnit.objects.create(name="验收队", code="ACC-ORG")
        cls.team = Team.objects.create(organization=cls.org, name="运输班", code="ACC-YS")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.middle = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.MIDDLE)
        for config, daily, target in ((cls.early, 3, 18), (cls.middle, 4, 14)):
            config.daily_worker_count = daily
            config.required_shifts = target
            config.is_active = True
            config.save(update_fields=[
                "daily_worker_count", "required_shifts", "is_active",
            ])
        cls.early_staff = [
            Employee.objects.create(
                employee_no=f"AE{i}", name=f"早班{i}", team=cls.team, shift_config=cls.early,
                min_required_shifts=18,
            )
            for i in range(1, 6)
        ]
        cls.middle_staff = [
            Employee.objects.create(
                employee_no=f"AM{i}", name=f"中班{i}", team=cls.team, shift_config=cls.middle,
                min_required_shifts=14,
            )
            for i in range(1, 9)
        ]

    def _generate(self, config):
        return generate_and_apply(config, self.super_user, anchor=PERIOD, time_limit_seconds=60)

    def _records(self, config, *, status=ScheduleRecord.Status.CONFIRMED, active=True):
        return ScheduleRecord.objects.filter(
            team=self.team, shift_config=config, status=status, is_active=active
        )

    # 场景 1：早班/中班分别生成后的人员、班次、日期、数量
    def test_separate_generation_records(self):
        early_out = self._generate(self.early)
        middle_out = self._generate(self.middle)
        self.assertTrue(early_out["applied"], early_out["errors"])
        self.assertTrue(middle_out["applied"], middle_out["errors"])

        early_rows = self._records(self.early)
        middle_rows = self._records(self.middle)
        self.assertEqual(early_rows.count(), 5 * 30)
        self.assertEqual(middle_rows.count(), 8 * 30)
        self.assertEqual(set(early_rows.values_list("employee_id", flat=True)),
                         {e.pk for e in self.early_staff})
        self.assertEqual(set(middle_rows.values_list("employee_id", flat=True)),
                         {e.pk for e in self.middle_staff})
        self.assertEqual(set(early_rows.values_list("shift_type", flat=True)),
                         {"rest", "early"})
        self.assertEqual(set(middle_rows.values_list("shift_type", flat=True)),
                         {"rest", "middle"})
        start = PERIOD
        end = PERIOD + datetime.timedelta(days=29)
        self.assertEqual(
            set(early_rows.values_list("work_date", flat=True)),
            {start + datetime.timedelta(days=i) for i in range(30)},
        )
        self.assertEqual(
            set(middle_rows.values_list("work_date", flat=True)),
            {start + datetime.timedelta(days=i) for i in range(30)},
        )

    # 场景 2：生成中班不作废/覆盖/修改早班记录
    def test_generating_middle_does_not_touch_early(self):
        self._generate(self.early)
        early_ids = set(self._records(self.early).values_list("id", flat=True))
        self._generate(self.middle)
        self.assertEqual(set(self._records(self.early).values_list("id", flat=True)), early_ids)
        self.assertEqual(
            ScheduleRecord.objects.filter(pk__in=early_ids, status="superseded").count(), 0
        )

    # 场景 3：早班人工调整后生成中班，调整完整保留
    def test_manual_adjustment_survives_middle_generation(self):
        self._generate(self.early)
        record = self._records(self.early).filter(
            shift_type=ScheduleRecord.ShiftType.EARLY, employee=self.early_staff[0]
        ).first()
        day = record.work_date
        result = change_schedule_record(record, ScheduleRecord.ShiftType.REST, self.super_user)
        self.assertTrue(result["changed"], result)
        self._generate(self.middle)
        kept = self._records(self.early).get(employee=self.early_staff[0], work_date=day)
        self.assertEqual(kept.shift_type, ScheduleRecord.ShiftType.REST)
        self.assertEqual(kept.status, ScheduleRecord.Status.CONFIRMED)

    # 场景 4：中班不可行导致生成失败，早班与人工调整不受影响
    def test_middle_failure_leaves_early_intact(self):
        self._generate(self.early)
        early_rows = list(self._records(self.early).values_list("id", "shift_type", "updated_at"))
        self.middle.required_shifts = 18
        self.middle.save(update_fields=["required_shifts"])
        before_batches = ScheduleBatch.objects.count()
        outcome = self._generate(self.middle)
        self.assertFalse(outcome["applied"])
        self.assertTrue(outcome["errors"])
        self.assertEqual(ScheduleBatch.objects.count(), before_batches)
        self.assertEqual(
            list(self._records(self.early).values_list("id", "shift_type", "updated_at")),
            early_rows,
        )

    # 场景 5：同一班次重复生成，只替换该班次有效记录
    def test_regenerate_only_replaces_own_shift(self):
        self._generate(self.early)
        self._generate(self.middle)
        middle_ids = set(self._records(self.middle).values_list("id", flat=True))
        early_before = set(self._records(self.early).values_list("id", flat=True))

        self._generate(self.early)

        self.assertEqual(
            set(self._records(self.early).values_list("id", flat=True)).isdisjoint(early_before),
            True,
        )
        self.assertEqual(
            ScheduleRecord.objects.filter(pk__in=early_before, status="superseded").count(),
            len(early_before),
        )
        self.assertEqual(set(self._records(self.middle).values_list("id", flat=True)), middle_ids)
        self.assertEqual(
            ScheduleRecord.objects.filter(pk__in=middle_ids, status="superseded").count(), 0
        )

    # 场景 6：跨班次人工记录会导致“一人一天两条有效”，生成前必须拒绝且零写入
    def test_cross_shift_manual_record_blocks_generation(self):
        self._generate(self.early)
        record = self._records(self.early).filter(
            shift_type=ScheduleRecord.ShiftType.EARLY, employee=self.early_staff[0]
        ).first()
        day = record.work_date
        # 人工改班到中班（业务规则允许）→ 产生跨班次记录
        changed = change_schedule_record(record, ScheduleRecord.ShiftType.MIDDLE, self.super_user)
        self.assertTrue(changed["changed"], changed)
        self.assertEqual(
            ScheduleRecord.objects.get(pk=record.pk).shift_config_id, self.middle.pk
        )
        snapshot = list(
            ScheduleRecord.objects.filter(team=self.team).values_list(
                "id", "shift_type", "status", "is_active", "shift_config_id"
            )
        )
        batches = ScheduleBatch.objects.count()
        outcome = self._generate(self.early)
        self.assertFalse(outcome["applied"])
        self.assertTrue(any("其他班次" in e for e in outcome["errors"]), outcome["errors"])
        self.assertEqual(ScheduleBatch.objects.count(), batches)
        self.assertEqual(
            list(
                ScheduleRecord.objects.filter(team=self.team).values_list(
                    "id", "shift_type", "status", "is_active", "shift_config_id"
                )
            ),
            snapshot,
        )

    # 场景 7：多班次批次头互不作废（生成早班不能把中班版本头标为 superseded）
    def test_generating_one_shift_keeps_other_shift_batch_head(self):
        early_out = self._generate(self.early)
        middle_out = self._generate(self.middle)
        self.assertTrue(early_out["applied"], early_out["errors"])
        self.assertTrue(middle_out["applied"], middle_out["errors"])
        self._generate(self.early)   # 再生成早班
        middle_batch = ScheduleBatch.objects.get(batch_id=middle_out["batch"].batch_id)
        self.assertEqual(middle_batch.status, ScheduleBatch.Status.CONFIRMED)
        self.assertTrue(
            ScheduleRecord.objects.filter(
                schedule_batch_id=middle_batch.batch_id,
                status=ScheduleRecord.Status.CONFIRMED, is_active=True,
            ).exists()
        )
        heads = ScheduleBatch.objects.filter(
            team=self.team, kind=ScheduleBatch.Kind.PERIOD,
            status=ScheduleBatch.Status.CONFIRMED,
            period_start=PERIOD,
        )
        self.assertEqual(
            set(heads.values_list("shift_config__shift_name", flat=True)),
            {"early", "middle"},
        )
