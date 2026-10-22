# 人员模型测试。
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase, TransactionTestCase

from apps.organization.models import OrganizationUnit, ShiftConfig, Team

from .models import Employee


class ParsePositionsTests(TestCase):
    # 多岗位解析：集中封装于 services.parse_positions，供配置页/人员展示/后续算法复用
    def test_parse_positions(self):
        from .services import normalize_position, parse_positions

        self.assertEqual(parse_positions("班长、电工"), ["班长", "电工"])
        self.assertEqual(parse_positions("班长，电工"), ["班长", "电工"])
        self.assertEqual(parse_positions("班长,电工"), ["班长", "电工"])
        self.assertEqual(parse_positions("班长;电工"), ["班长", "电工"])
        self.assertEqual(parse_positions("班长/电工"), ["班长", "电工"])
        # 前后空格、空项、重复岗位
        self.assertEqual(parse_positions(" 电工 、 皮带 ，电工 "), ["电工", "皮带"])
        self.assertEqual(parse_positions("电工、、皮带"), ["电工", "皮带"])
        self.assertEqual(parse_positions("电工"), ["电工"])
        self.assertEqual(parse_positions(""), [])
        self.assertEqual(parse_positions(None), [])

    def test_normalize_position(self):
        from .services import normalize_position

        self.assertEqual(normalize_position(" 电工 "), "电工")
        self.assertEqual(normalize_position(""), "")


class EmployeeModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        cls.employee = Employee.objects.create(
            employee_no="E001", name="张三", team=cls.team,
            shift_config=cls.early, min_required_shifts=18,
        )

    def test_str(self):
        self.assertEqual(str(self.employee), "E001 张三")

    def test_employee_no_unique(self):
        with self.assertRaises(IntegrityError):
            Employee.objects.create(
                employee_no="E001", name="李四", team=self.team,
                shift_config=self.early, min_required_shifts=18,
            )

    def test_min_required_shifts_required(self):
        emp = Employee(employee_no="E002", name="王五", team=self.team, shift_config=self.early)
        with self.assertRaises(ValidationError):
            emp.full_clean()

    def test_shift_config_team_mismatch_rejected(self):
        other_team = Team.objects.create(organization=self.org, name="二班", code="T01-02")
        night = other_team.shift_configs.get(shift_name=ShiftConfig.ShiftName.NIGHT)
        emp = Employee(
            employee_no="E003", name="赵六", team=self.team,
            shift_config=night, min_required_shifts=18,
        )
        with self.assertRaises(ValidationError):
            emp.full_clean()

    def test_defaults(self):
        self.assertEqual(self.employee.worked_shifts, 0)
        self.assertTrue(self.employee.is_active)

    def test_min_required_independent_from_required_shifts(self):
        # 矿上最低班数与排班目标班数（shift_config.required_shifts）始终保持独立
        early = self.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        early.required_shifts = 20
        early.save(update_fields=["required_shifts"])
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.min_required_shifts, 18)  # 不受排班目标影响

        self.employee.min_required_shifts = 15
        self.employee.save(update_fields=["min_required_shifts"])
        early.refresh_from_db()
        self.assertEqual(early.required_shifts, 20)  # 不受矿上最低班数影响


class ImportRowValidationTests(TestCase):
    # 人员导入行数据校验（8 列格式见 docs/PERSONNEL_IMPORT_FORMAT.md）
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.org_b = OrganizationUnit.objects.create(name="采煤二队", code="ORG-02")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")
        cls.team_b = Team.objects.create(organization=cls.org_b, name="二队一班", code="T02-01")

    def _row(self, **overrides):
        row = dict(
            organization_code="ORG-01",
            team_code="T01-01",
            employee_no="E100",
            name="新职工",
            position="值班员",
            shift_name="early",
            worked_shifts=15,
            min_required_shifts=18,
        )
        row.update(overrides)
        return row

    def test_valid_row(self):
        from .validators import validate_import_row

        self.assertEqual(validate_import_row(**self._row()), [])

    def test_org_not_found(self):
        from .validators import validate_import_row

        errors = validate_import_row(**self._row(organization_code="NO-SUCH"))
        self.assertTrue(any("队组编号不存在" in e for e in errors))

    def test_org_code_empty(self):
        from .validators import validate_import_row

        errors = validate_import_row(**self._row(organization_code=""))
        self.assertTrue(any("队组编号不能为空" in e for e in errors))

    def test_team_not_found(self):
        from .validators import validate_import_row

        errors = validate_import_row(**self._row(team_code="NO-SUCH"))
        self.assertTrue(any("班组编号不存在" in e for e in errors))

    def test_team_code_empty(self):
        from .validators import validate_import_row

        errors = validate_import_row(**self._row(team_code=""))
        self.assertTrue(any("班组编号不能为空" in e for e in errors))

    def test_team_org_mismatch(self):
        # 班组存在但属于其他队组：必须提示“队组与班组不匹配”，而非“班组不存在”
        from .validators import validate_import_row

        errors = validate_import_row(
            **self._row(organization_code="ORG-01", team_code="T02-01")
        )
        self.assertTrue(any("队组与班组不匹配" in e for e in errors))
        self.assertFalse(any("班组编号不存在" in e for e in errors))

    def test_duplicate_employee_no(self):
        from .validators import validate_import_row

        Employee.objects.create(
            employee_no="E100", name="已存在", team=self.team,
            shift_config=self.team.shift_configs.get(shift_name="early"),
            min_required_shifts=18,
        )
        errors = validate_import_row(**self._row())
        self.assertTrue(any("职工编号已存在" in e for e in errors))

    def test_empty_employee_no(self):
        from .validators import validate_import_row

        errors = validate_import_row(**self._row(employee_no=""))
        self.assertTrue(any("职工编号不能为空" in e for e in errors))

    def test_invalid_shift_name(self):
        from .validators import validate_import_row

        errors = validate_import_row(**self._row(shift_name="day"))
        self.assertTrue(any("班次不合法" in e for e in errors))

    def test_negative_counts(self):
        from .validators import validate_import_row

        errors = validate_import_row(**self._row(worked_shifts=-1))
        self.assertTrue(any("不能为负数" in e for e in errors))
        errors = validate_import_row(**self._row(min_required_shifts=-2))
        self.assertTrue(any("不能为负数" in e for e in errors))

    def test_non_integer_counts(self):
        from .validators import validate_import_row

        errors = validate_import_row(**self._row(worked_shifts=None))
        self.assertTrue(any("已上班数必须为整数" in e for e in errors))

    def test_deactivated_team(self):
        from .validators import validate_import_row

        self.team.is_active = False
        self.team.save(update_fields=["is_active"])
        errors = validate_import_row(**self._row())
        self.assertTrue(any("班组已停用" in e for e in errors))

    def test_missing_shift_config(self):
        from .validators import validate_import_row

        from apps.organization.models import ShiftConfig

        ShiftConfig.objects.filter(team=self.team, shift_name="early").delete()
        errors = validate_import_row(**self._row())
        self.assertTrue(any("缺少班次配置" in e for e in errors))

    def test_allowed_org_scope(self):
        # 队组管理员只能校验本队组班组的数据行
        from .validators import validate_import_row

        errors = validate_import_row(
            **self._row(organization_code="ORG-02", team_code="T02-01"), allowed_org=self.org
        )
        self.assertTrue(any("无权导入该队组的数据" in e for e in errors))


# ---------- 批量导入写入（第六阶段：确认写入/回滚/并发） ----------
class ImportWriteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.org_b = OrganizationUnit.objects.create(name="采煤二队", code="ORG-02")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")
        cls.team_b = Team.objects.create(organization=cls.org_b, name="二队一班", code="T02-01")

    def _rows(self, count=3):
        return [
            {
                "队组编号": "ORG-01",
                "班组编号": "T01-01",
                "职工编号": f"IW{i:03d}",
                "姓名": f"导入{i}",
                "岗位": "" if i == 2 else "值班员",
                "班次": "early",
                "已上班数": "0",
                "矿上应上最少班数": "18",
            }
            for i in range(1, count + 1)
        ]

    def test_write_happy_path_field_mapping(self):
        from .services import import_employees

        created = import_employees(self._rows())
        self.assertEqual(created, 3)
        e1 = Employee.objects.get(employee_no="IW001")
        self.assertEqual(e1.position, "值班员")
        self.assertEqual(e1.team, self.team)
        self.assertEqual(e1.shift_config, self.team.shift_configs.get(shift_name="early"))
        self.assertEqual(e1.worked_shifts, 0)         # 第7列
        self.assertEqual(e1.min_required_shifts, 18)  # 第8列（统计用，不参与算法）
        e2 = Employee.objects.get(employee_no="IW002")
        self.assertEqual(e2.position, "")             # 空岗位保留为空

    def test_duplicate_employee_no_rolls_back_whole_batch(self):
        from .services import import_employees

        Employee.objects.create(
            employee_no="IW001", name="已存在", team=self.team,
            shift_config=self.team.shift_configs.get(shift_name="early"),
            min_required_shifts=18,
        )
        # 预先存在的重复由模型 full_clean 拦截（ValidationError），整批回滚；
        # 并发场景下的重复由数据库唯一约束拦截（IntegrityError → ImportConflictError，
        # 见 ConcurrentImportTests）。
        with self.assertRaises(ValidationError):
            import_employees(self._rows(3))
        self.assertEqual(
            Employee.objects.filter(employee_no__in=["IW001", "IW002", "IW003"]).count(), 1
        )

    def test_mid_batch_failure_rolls_back_everything(self):
        from unittest import mock

        from .services import import_employees

        calls = {"n": 0}
        original = __import__("apps.personnel.services", fromlist=["create_employee"]).create_employee

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ValueError("模拟中途失败")
            return original(*args, **kwargs)

        with mock.patch("apps.personnel.services.create_employee", side_effect=flaky):
            with self.assertRaises(ValueError):
                import_employees(self._rows(3))
        self.assertFalse(Employee.objects.filter(employee_no__startswith="IW").exists())

    def test_target_vanished_guard(self):
        from .services import import_employees, ImportConflictError

        rows = self._rows(1)
        rows[0]["队组编号"] = "NO-SUCH"
        with self.assertRaises(ImportConflictError):
            import_employees(rows)
        self.assertFalse(Employee.objects.filter(employee_no="IW001").exists())

    def test_allowed_org_scope_enforced(self):
        from django.core.exceptions import PermissionDenied

        from .services import import_employees

        rows = [
            {
                "队组编号": "ORG-02",
                "班组编号": "T02-01",
                "职工编号": "IW099",
                "姓名": "跨队",
                "岗位": "",
                "班次": "early",
                "已上班数": "0",
                "矿上应上最少班数": "18",
            }
        ]
        with self.assertRaises(PermissionDenied):
            import_employees(rows, allowed_org=self.org)
        self.assertFalse(Employee.objects.filter(employee_no="IW099").exists())


class ConcurrentImportTests(TransactionTestCase):
    # 并发导入同一批职工编号：数据库唯一约束兜底，恰好一个批次成功
    def test_concurrent_same_employee_nos_only_one_wins(self):
        import threading

        from django.db import close_old_connections

        from .services import ImportConflictError, import_employees

        org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        team = Team.objects.create(organization=org, name="一班", code="T01-01")
        rows = [
            {
                "队组编号": "ORG-01",
                "班组编号": "T01-01",
                "职工编号": f"CC{i:03d}",
                "姓名": f"并发{i}",
                "岗位": "值班员",
                "班次": "early",
                "已上班数": "0",
                "矿上应上最少班数": "18",
            }
            for i in range(1, 4)
        ]
        outcomes = []
        barrier = threading.Barrier(2)

        def worker():
            close_old_connections()
            barrier.wait()
            try:
                outcomes.append(("ok", import_employees(rows)))
            except ImportConflictError:
                outcomes.append(("conflict", None))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(len(outcomes), 2)
        self.assertEqual([o[0] for o in outcomes].count("ok"), 1)
        self.assertEqual([o[0] for o in outcomes].count("conflict"), 1)
        self.assertEqual(
            Employee.objects.filter(employee_no__in=["CC001", "CC002", "CC003"]).count(), 3
        )
