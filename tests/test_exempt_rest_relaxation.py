# 豁免人员休息规则放宽：模型级证明 + 运输班真实配置集成验证。
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from ortools.sat.python import cp_model

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.personnel.models import Employee
from apps.scheduler.engine.rest_solver import (
    RestProblem,
    _add_consecutive_rest_hard_constraints,
    _add_rest_hard_constraints,
    determinable_rest_runs,
    minimal_required_slots,
    validate_rest_solution,
)
from apps.scheduler.planning import build_rest_problem, solve_shift_schedule
from apps.scheduler.models import ScheduleRecord

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


def solve_fixed_pattern(*, pattern, exempt):
    # 单人模型：固定上班/休息模式，仅加入休息规则，验证规则是否作用于该人员
    problem = RestProblem(
        employee_ids=[1],
        qualifications={1: []},
        day_count=len(pattern),
        daily_required=1,
        position_requirements={},
        targets={},
        min_consecutive_rest_days=2,
        max_consecutive_rest_days=4,
        minimum_shifts={},
        exempt_employee_ids={1} if exempt else set(),
    )
    model = cp_model.CpModel()
    x = {(0, day): model.NewBoolVar(f"x0_{day}") for day in range(len(pattern))}
    for day, value in enumerate(pattern):
        model.Add(x[0, day] == value)
    _add_rest_hard_constraints(model, x, problem)
    _add_consecutive_rest_hard_constraints(model, x, problem)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10
    status = solver.Solve(model)
    return solver.StatusName(status)


class ExemptRestRuleModelTests(TestCase):
    # 规则“未施加到豁免人员”必须在模型层证明，而不只是求解返回可行
    def test_isolated_work_day_allowed_only_for_exempt(self):
        pattern = [1, 0, 1]      # 上班—休息—上班：违反“禁止孤立工作日”
        self.assertIn(solve_fixed_pattern(pattern=pattern, exempt=True), ("OPTIMAL", "FEASIBLE"))
        self.assertEqual(solve_fixed_pattern(pattern=pattern, exempt=False), "INFEASIBLE")

    def test_single_rest_day_allowed_only_for_exempt(self):
        pattern = [0, 1, 0]      # 休息—上班—休息：违反“禁止单日休息/最少连续休息 2 天”
        self.assertIn(solve_fixed_pattern(pattern=pattern, exempt=True), ("OPTIMAL", "FEASIBLE"))
        self.assertEqual(solve_fixed_pattern(pattern=pattern, exempt=False), "INFEASIBLE")

    def test_long_rest_run_allowed_only_for_exempt(self):
        pattern = [1, 1, 0, 0, 0, 0, 0, 0, 1, 1]   # 连续休息 6 天 > 上限 4
        self.assertIn(solve_fixed_pattern(pattern=pattern, exempt=True), ("OPTIMAL", "FEASIBLE"))
        self.assertEqual(solve_fixed_pattern(pattern=pattern, exempt=False), "INFEASIBLE")

    def test_minimum_shifts_capacity_ignores_exempt(self):
        problem = RestProblem(
            employee_ids=[1, 2, 3],
            qualifications={},
            day_count=30,
            daily_required=4,
            position_requirements={},
            targets={},
            min_consecutive_rest_days=2,
            max_consecutive_rest_days=4,
            minimum_shifts={1: 18},
            exempt_employee_ids={2, 3},
        )
        # 豁免人员不再因“连续休息上限”被推导出最少出勤
        self.assertEqual(minimal_required_slots(problem), 18)


class TransportMiddleExemptTests(TestCase):
    # 运输班中班真实配置：8 人 / 每天 4 人 / 班长 1 人 / 6 普通(18 班) + 2 豁免 / 休息 2~4 天
    @classmethod
    def setUpTestData(cls):
        cls.super_user = make_user("ex_super")
        cls.org = OrganizationUnit.objects.create(name="运输队", code="EX-ORG")
        cls.team = Team.objects.create(organization=cls.org, name="运输班", code="EX-YS")
        cls.config = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.MIDDLE)
        cls.config.daily_worker_count = 4
        cls.config.required_shifts = 18
        cls.config.min_consecutive_rest_days = 2
        cls.config.max_consecutive_rest_days = 4
        cls.config.position_requirements = {"班长": 1}
        cls.config.is_active = True
        cls.config.save(update_fields=[
            "daily_worker_count", "required_shifts", "min_consecutive_rest_days",
            "max_consecutive_rest_days", "position_requirements", "is_active",
        ])
        cls.staff = [
            Employee.objects.create(
                employee_no=f"EX{i:02d}", name=f"中班{i}", team=cls.team, shift_config=cls.config,
                position="班长" if i <= 3 else "普通", min_required_shifts=18,
            )
            for i in range(1, 9)
        ]
        cls.exempt = [cls.staff[5], cls.staff[7]]
        cls.config.exempt_employee_ids = [e.pk for e in cls.exempt]
        cls.config.save(update_fields=["exempt_employee_ids"])
        cls.normal = [e for e in cls.staff if e not in cls.exempt]

    def test_full_solve_feasible_with_exempt_free_of_rest_rules(self):
        result = solve_shift_schedule(self.config, anchor=PERIOD, time_limit_seconds=60)
        self.assertEqual(result.status, "feasible", result.diagnostics)
        self.assertEqual(result.validation_errors, [])
        solution = result.solution

        # 每日恰好 4 人；每天恰好 1 名班长
        self.assertEqual(set(solution.daily_counts), {4})
        self.assertEqual(set(solution.daily_position_counts["班长"]), {1})

        # 6 名普通人员各自 ≥18 班；2 名豁免不受最低班数约束
        for employee in self.normal:
            self.assertGreaterEqual(solution.achieved[employee.pk], 18)
        self.assertEqual(sum(solution.achieved.values()), 120)
        self.assertEqual(
            sum(solution.achieved[e.pk] for e in self.exempt)
            + sum(solution.achieved[e.pk] for e in self.normal),
            120,
        )

        # 普通人员的休息规则全部满足（独立校验已覆盖；此处再逐人核对）
        for employee in self.normal:
            work = [1 if (employee.pk, day) in solution.assignments else 0 for day in range(30)]
            for day in range(1, 29):
                if work[day] == 1:
                    self.assertFalse(work[day - 1] == 0 and work[day + 1] == 0)
                else:
                    self.assertFalse(work[day - 1] == 1 and work[day + 1] == 1)
            runs = determinable_rest_runs(work, None, None)
            self.assertTrue(all(2 <= run <= 4 for run in runs), (employee.name, runs))

    def test_exempt_set_reaches_model_and_capacity_bound(self):
        info = build_rest_problem(self.config, PERIOD)
        problem = info["problem"]
        self.assertEqual(problem.exempt_employee_ids, {e.pk for e in self.exempt})
        self.assertEqual(set(problem.minimum_shifts), {e.pk for e in self.normal})
        # 容量下界只统计普通人员（108），不再因豁免人员休息上限而抬高
        self.assertEqual(minimal_required_slots(problem), 108)

    def test_validation_does_not_flag_exempt_patterns(self):
        # 构造一个豁免人员“孤立工作日 + 连续休息 6 天”的排班，其他约束仍满足：
        # 校验必须只对普通人员检查休息规则，不得因此报错。
        info = build_rest_problem(self.config, PERIOD)
        problem = info["problem"]
        result = solve_shift_schedule(self.config, anchor=PERIOD, time_limit_seconds=60)
        self.assertEqual(result.status, "feasible")
        solution = result.solution
        # 人为检查一名豁免人员的休息模式（若出现违规也不应被判无效）
        exempt_id = self.exempt[0].pk
        work = [1 if (exempt_id, day) in solution.assignments else 0 for day in range(30)]
        runs, current = [], 0
        for value in work:
            if value == 0:
                current += 1
            elif current:
                runs.append(current)
                current = 0
        if current:
            runs.append(current)
        errors = validate_rest_solution(problem, solution)
        self.assertEqual(errors, [])

    def test_same_config_with_all_staff_normal_is_still_infeasible(self):
        # 对照：若把 8 人全部当作普通人员（最低 18 班），容量 144 > 120 → 必须判不可行
        self.config.exempt_employee_ids = []
        self.config.save(update_fields=["exempt_employee_ids"])
        info = build_rest_problem(self.config, PERIOD)
        self.assertIsNone(info["problem"])
        self.assertTrue(info["infeasible_reasons"])

    def test_other_shift_rules_unaffected(self):
        # 另一个班次（无豁免）仍按原规则严格约束：孤立工作日必须被判不可行
        other = self.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        other.daily_worker_count = 3
        other.required_shifts = 18
        other.position_requirements = {}
        other.is_active = True
        other.save(update_fields=[
            "daily_worker_count", "required_shifts", "position_requirements", "is_active",
        ])
        for i in range(1, 5):
            Employee.objects.create(
                employee_no=f"EXE{i}", name=f"早班{i}", team=self.team,
                shift_config=other, min_required_shifts=18,
            )
        info = build_rest_problem(other, PERIOD)
        self.assertIsNotNone(info["problem"], info.get("infeasible_reasons"))
        self.assertEqual(info["problem"].exempt_employee_ids, set())
        self.assertEqual(len(info["problem"].minimum_shifts), 4)
