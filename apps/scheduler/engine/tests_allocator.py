# 班数分配目标（第一阶段/第二阶段）单元测试：纯计算，无数据库依赖。
from django.test import SimpleTestCase

from .allocator import MODE_SURPLUS, MODE_TIGHT, ShiftAllocation, plan_shift_targets


def ids(count, start=1):
    return list(range(start, start + count))


class ModeJudgmentTests(SimpleTestCase):
    def test_surplus_mode_when_capacity_exceeds_team(self):
        # D=30, R=18, M=18 → 可排开最大人数 = 540/18 = 30 > N=25 → 人多模式
        result = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=ids(25), exempt_ids=[],
        )
        self.assertEqual(result.mode, MODE_SURPLUS)
        self.assertGreater(result.capacity, result.employee_count)
        self.assertTrue(result.feasible)

    def test_tight_mode_when_capacity_within_team(self):
        # 可排开最大人数 30 ≤ N=31 → 人少模式
        result = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=ids(31), exempt_ids=[],
        )
        self.assertEqual(result.mode, MODE_TIGHT)

    def test_boundary_capacity_equals_team_is_tight(self):
        # 可排开最大人数 30 == N=30 → ≤ 取人少模式
        result = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=ids(30), exempt_ids=[],
        )
        self.assertEqual(result.mode, MODE_TIGHT)
        self.assertAlmostEqual(result.capacity, 30.0)

    def test_invalid_params_raise_value_error(self):
        with self.assertRaises(ValueError):
            plan_shift_targets(total_days=0, daily_required=18, base_shifts=18,
                               normal_ids=ids(5), exempt_ids=[])
        with self.assertRaises(ValueError):
            plan_shift_targets(total_days=30, daily_required=-1, base_shifts=18,
                               normal_ids=ids(5), exempt_ids=[])
        with self.assertRaises(ValueError):
            plan_shift_targets(total_days=30, daily_required=18, base_shifts=0,
                               normal_ids=ids(5), exempt_ids=[])

    def test_empty_team_infeasible(self):
        result = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=[], exempt_ids=[],
        )
        self.assertFalse(result.feasible)
        self.assertIn("没有有效职工", result.notes[0])

    def test_daily_required_exceeds_team_infeasible(self):
        # R=18 > N=5：每日人数硬约束理论上不可满足
        result = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=ids(5), exempt_ids=[],
        )
        self.assertFalse(result.feasible)
        self.assertTrue(any("无法满足每日人数" in note for note in result.notes))

    def test_base_shifts_exceed_days_tight_mode_note(self):
        # M=22 > D=20：任何人都不可能达到最低班数 → 名额紧张模式下必然不可行，
        # 明确报告（K=floor(200/22)=9 < 普通25人），绝不生成 20 班以下“目标”。
        result = plan_shift_targets(
            total_days=20, daily_required=10, base_shifts=22,
            normal_ids=ids(25), exempt_ids=[],
        )
        self.assertEqual(result.mode, MODE_TIGHT)
        self.assertFalse(result.feasible)
        self.assertTrue(any("需要至少豁免" in note for note in result.notes))


class SurplusModeAllocationTests(SimpleTestCase):
    def test_all_normal_even_distribution(self):
        # D=30,R=18,M=18,N=25 → 人多模式；剩余名额 90 由 25 人平均分担
        result = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=ids(25), exempt_ids=[],
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.surplus, 90)
        values = list(result.per_person_targets.values())
        self.assertEqual(sum(values), 540)  # 与总名额一致
        self.assertTrue(all(18 <= v <= 30 for v in values))
        self.assertLessEqual(max(values) - min(values), 1)  # 班数差尽可能小

    def test_exempt_priority_takes_surplus_first(self):
        # 豁免 3 人、普通 22 人：剩余名额 90 优先由豁免承担
        exempt = ids(3, start=1001)
        normal = ids(22)
        result = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=normal, exempt_ids=exempt,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.surplus, 90)
        exempt_values = [result.per_person_targets[e] for e in exempt]
        normal_values = [result.per_person_targets[e] for e in normal]
        self.assertEqual(sum(result.per_person_targets.values()), 540)
        # 豁免优先承担：豁免人员班数高于普通人员
        self.assertGreater(min(exempt_values), max(normal_values))
        # 豁免之间、普通之间班数差 ≤ 1
        self.assertLessEqual(max(exempt_values) - min(exempt_values), 1)
        self.assertLessEqual(max(normal_values) - min(normal_values), 1)
        # 豁免封顶为周期天数 30
        self.assertTrue(all(v <= 30 for v in exempt_values))

    def test_exempt_insufficient_overflow_to_normals(self):
        # 豁免 1 人最多多承担 12 班，剩余部分由普通人员平均分担
        exempt = [2001]
        normal = ids(24)
        result = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=normal, exempt_ids=exempt,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.per_person_targets[2001], 30)  # 豁免达到上限
        self.assertEqual(sum(result.per_person_targets.values()), 540)
        normal_values = [result.per_person_targets[e] for e in normal]
        self.assertLessEqual(max(normal_values) - min(normal_values), 1)

    def test_exempt_only_team_daily_requirement_infeasible(self):
        # 仅豁免人员且 R=18 > N=1：每日人数不可满足，由前置检查拦截并说明原因
        # （R≤N 时“豁免容量不足”在数学上不可达：D×R−N×M ≤ (D−M)×N ⇔ R ≤ N）
        exempt = ids(1, start=3001)
        result = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=[], exempt_ids=exempt,
        )
        self.assertFalse(result.feasible)
        self.assertTrue(any("无法满足每日人数" in note for note in result.notes))


class TightModeAllocationTests(SimpleTestCase):
    def test_normals_get_full_minimum_and_exempt_take_remainder(self):
        # D=30,R=18,M=18,N=31,豁免1人 → K=min(31,30)=30；豁免剩余 R'=540-540=0
        # 普通人员 30 人全部 18 班，豁免 0 班；总班数 540
        exempt = ids(1, start=3001)
        result = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=ids(30), exempt_ids=exempt,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.mode, MODE_TIGHT)
        self.assertEqual(result.full_count, 30)
        self.assertEqual(result.theoretical_exempt_count, 1)
        self.assertEqual(result.exempt_remaining_target, 0)
        values = list(result.per_person_targets.values())
        self.assertEqual(sum(values), 540)
        self.assertTrue(all(result.per_person_targets[i] == 18 for i in ids(30)))
        self.assertEqual(result.per_person_targets[exempt[0]], 0)

    def test_normals_exceed_capacity_without_exempt_infeasible(self):
        # D=30,R=18,M=18,N=31,无豁免 → 普通人员 31 > K=30 → 明确不可行（绝不降低为17）
        result = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=ids(31), exempt_ids=[],
        )
        self.assertFalse(result.feasible)
        self.assertTrue(any("需要至少豁免 1 人" in n for n in result.notes))

    def test_insufficient_exempt_reports_required_count(self):
        # D=30,R=17,M=18,N=31,豁免1人 → K=28，普通 30 > 28 → 需要至少豁免 2 人
        result = plan_shift_targets(
            total_days=30, daily_required=17, base_shifts=18,
            normal_ids=ids(30), exempt_ids=ids(1, start=4001),
        )
        self.assertFalse(result.feasible)
        self.assertTrue(any("需要至少豁免 2 人" in n for n in result.notes))
        self.assertFalse(any("17" in n for n in result.notes) or any(
            "17" in n for n in result.notes
        ))  # 不会把 17 当作合法目标提示

    def test_daily_required_exceeds_team_still_infeasible(self):
        result = plan_shift_targets(
            total_days=10, daily_required=8, base_shifts=18,
            normal_ids=ids(5), exempt_ids=[],
        )
        self.assertFalse(result.feasible)


class OverhaulSpecTests(SimpleTestCase):
    # 修复后的核心场景：检修班 22 人简化模型与豁免分配规则
    def _jia_xiu(self, exempt_ids):
        return plan_shift_targets(
            total_days=30, daily_required=13, base_shifts=18,
            normal_ids=[i for i in ids(22) if i not in set(exempt_ids)],
            exempt_ids=exempt_ids,
        )

    def test_jia_xiu_simplified_model(self):
        # D=30,R=13,M=18,N=22,豁免1人：
        # 总需求 390；K=min(22, floor(390/18)=21)=21；E=1；豁免目标 390-378=12
        exempt = [22]
        result = self._jia_xiu(exempt)
        self.assertTrue(result.feasible)
        self.assertEqual(result.mode, MODE_TIGHT)
        self.assertEqual(result.total_slots, 390)
        self.assertEqual(result.full_count, 21)
        self.assertEqual(result.theoretical_exempt_count, 1)
        self.assertEqual(result.exempt_remaining_target, 12)
        # 21 名普通人员全部 18 班；1 名豁免 12 班；总和 390
        normals = [i for i in ids(22) if i not in set(exempt)]
        self.assertTrue(all(result.per_person_targets[i] == 18 for i in normals))
        self.assertEqual(result.per_person_targets[exempt[0]], 12)
        self.assertEqual(sum(result.per_person_targets.values()), 390)

    def test_multiple_exempt_share_remainder_fairly(self):
        # 豁免 3 人 → 普通 19×18=342；剩余 48 → 每人 16 班（组内差 ≤1）
        exempt = [20, 21, 22]
        result = self._jia_xiu(exempt)
        self.assertTrue(result.feasible)
        values = [result.per_person_targets[e] for e in exempt]
        self.assertEqual(sum(values), 390 - 19 * 18)
        self.assertLessEqual(max(values) - min(values), 1)
        normals = [i for i in ids(22) if i not in set(exempt)]
        self.assertTrue(all(result.per_person_targets[i] == 18 for i in normals))

    def test_exempt_more_than_theoretical_still_fair(self):
        # 豁免 5 人（> 理论 1 人）→ 普通 17×18=306；剩余 84 → 17,17,17,17,16
        exempt = [18, 19, 20, 21, 22]
        result = self._jia_xiu(exempt)
        self.assertTrue(result.feasible)
        values = [result.per_person_targets[e] for e in exempt]
        self.assertEqual(sum(values), 84)
        self.assertLessEqual(max(values) - min(values), 1)

    def test_no_exempt_with_tight_demand_infeasible(self):
        # 22 人全部普通 → K=21 < 22 → 需要至少豁免 1 人，绝不生成 17 班目标
        result = self._jia_xiu([])
        self.assertFalse(result.feasible)
        self.assertTrue(any("需要至少豁免 1 人" in n for n in result.notes))
        self.assertEqual(result.per_person_targets, {})


class SpecExampleNumbersTests(SimpleTestCase):
    def test_spec_example_numbers_fall_into_surplus_mode(self):
        # 文档示例：D=30、R=18、N=25 → floor(540/25)=21 用于演示公式；
        # 按判断规则（可排开最大人数 30 > 25）该组数据属于人多模式。
        result = plan_shift_targets(
            total_days=30, daily_required=18, base_shifts=18,
            normal_ids=ids(25), exempt_ids=[],
        )
        self.assertEqual(result.mode, MODE_SURPLUS)
        # 演示用的理论最低班数公式在此组数据下为 21（仅参考，不影响人多模式分配）
        self.assertEqual(540 // 25, 21)
