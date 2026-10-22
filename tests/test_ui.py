# UI 功能测试：页面路由、表单提交、筛选分页、联动接口、导入预览与确认写入、排班结果展示。
import csv
import datetime
import io
import re
import uuid

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.auditlog.models import OperationLog
from apps.organization.models import OrganizationUnit, ShiftConfig, Team
from apps.accounts.models import UserProfile
from apps.personnel.models import Employee
from apps.scheduler.models import ScheduleRecord
from apps.scheduler.selectors import period_range

PAGES = [
    "core:dashboard",
    "organization:unit_list",
    "organization:unit_create",
    "organization:team_list",
    "organization:team_create",
    "organization:shift_config_list",
    "personnel:employee_list",
    "personnel:employee_create",
    "personnel:employee_import",
    "scheduler:schedule_plan",
    "scheduler:schedule_results",
    "auditlog:log_list",
]


class UIPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("ui_tester", password="pw")
        cls.user.is_superuser = True
        cls.user.is_staff = True
        cls.user.save()
        UserProfile.objects.update_or_create(user=cls.user, defaults={"role": "super"})
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.org2 = OrganizationUnit.objects.create(name="采煤二队", code="ORG-02")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")
        cls.team2 = Team.objects.create(organization=cls.org, name="二班", code="T01-02")
        cls.early = cls.team.shift_configs.get(shift_name=ShiftConfig.ShiftName.EARLY)
        for i in range(1, 26):
            Employee.objects.create(
                employee_no=f"E{i:03d}",
                name=f"职工{i}",
                team=cls.team,
                shift_config=cls.early,
                min_required_shifts=18,
                worked_shifts=i % 20,
            )
        cls.batch = uuid.uuid4()
        emp = Employee.objects.get(employee_no="E001")
        period_start, _ = period_range()
        for offset in (0, 1, 2):
            ScheduleRecord.objects.create(
                schedule_batch_id=cls.batch,
                organization=cls.org,
                team=cls.team,
                shift_config=cls.early,
                employee=emp,
                work_date=period_start + datetime.timedelta(days=offset),
                shift_type="early" if offset != 1 else "rest",
                status="confirmed",  # 当前有效排班口径：已确认且未删除
            )

    def setUp(self):
        self.client.login(username="ui_tester", password="pw")

    # ---------- 回归：职工数统计不被班次配置 JOIN 放大 ----------
    def test_team_employee_count_not_multiplied_by_shift_configs(self):
        # 一班有 25 名职工、3 个班次配置；错误统计会显示 25×3=75
        response = self.client.get(reverse("organization:team_list"))
        team_row = next(t for t in response.context["page_obj"] if t.pk == self.team.pk)
        self.assertEqual(team_row.employee_count, 25)
        detail = self.client.get(reverse("organization:unit_detail", args=[self.org.pk]))
        self.assertEqual(detail.context["employee_count"], 25)
        detail_row = next(t for t in detail.context["teams"] if t.pk == self.team.pk)
        self.assertEqual(detail_row.employee_count, 25)
        team_detail = self.client.get(reverse("organization:team_detail", args=[self.team.pk]))
        self.assertEqual(team_detail.context["employee_count"], 25)

    # ---------- 导航与页面加载 ----------
    def test_all_nav_pages_load(self):
        for name in PAGES:
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)

    def test_nav_links_point_to_existing_pages(self):
        # 页面内的导航链接都指向存在的路由
        for name in PAGES:
            with self.subTest(name=name):
                self.client.get(reverse(name))  # 渲染即验证模板中所有 {% url %} 可解析

    # ---------- 队组 CRUD ----------
    def test_unit_create_wizard_and_edit(self):
        # 第一步：填写队组信息与两类账号
        response = self.client.post(
            reverse("organization:unit_create"),
            {
                "name": "采煤三队",
                "code": "ORG-03",
                "is_active": "on",
                "admin_username": "org03_admin",
                "admin_password": "Admin#12345",
                "admin_password2": "Admin#12345",
                "member_username": "org03_member",
                "member_password": "Member#12345",
                "member_password2": "Member#12345",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("confirm", response.url)
        # 确认页展示摘要
        response = self.client.get(reverse("organization:unit_create_confirm"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "采煤三队")
        # 确认提交
        response = self.client.post(reverse("organization:unit_create_confirm"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("success", response.url)
        unit = OrganizationUnit.objects.get(code="ORG-03")
        self.assertEqual(unit.name, "采煤三队")
        # 账号绑定正确
        admin_user = get_user_model().objects.get(username="org03_admin")
        member_user = get_user_model().objects.get(username="org03_member")
        self.assertEqual(admin_user.profile.role, "admin")
        self.assertEqual(admin_user.profile.organization, unit)
        self.assertEqual(member_user.profile.role, "member")
        self.assertEqual(member_user.profile.organization, unit)
        # 编辑队组
        response = self.client.post(
            reverse("organization:unit_edit", args=[unit.pk]),
            {"name": "采煤三队改", "code": "ORG-03", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 302)
        unit.refresh_from_db()
        self.assertEqual(unit.name, "采煤三队改")

    def test_unit_duplicate_code_shows_error(self):
        response = self.client.post(
            reverse("organization:unit_create"),
            {
                "name": "重复",
                "code": "ORG-01",
                "is_active": "on",
                "admin_username": "dup_admin",
                "admin_password": "Admin#12345",
                "admin_password2": "Admin#12345",
                "member_username": "dup_member",
                "member_password": "Member#12345",
                "member_password2": "Member#12345",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "该队组编号已存在")

    def test_unit_toggle_requires_post(self):
        response = self.client.get(reverse("organization:unit_toggle", args=[self.org.pk]))
        self.assertEqual(response.status_code, 405)
        response = self.client.post(reverse("organization:unit_toggle", args=[self.org.pk]), {"next": reverse("organization:unit_list")})
        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertFalse(self.org.is_active)

    # ---------- 班组 ----------
    def test_team_create_with_unique_constraint(self):
        response = self.client.post(
            reverse("organization:team_create"),
            {"organization": self.org.pk, "name": "三班", "code": "T01-03", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 302)
        team = Team.objects.get(code="T01-03")
        self.assertEqual(team.shift_configs.count(), 3)

        # 同队组重复编号 → 表单错误
        response = self.client.post(
            reverse("organization:team_create"),
            {"organization": self.org.pk, "name": "四班", "code": "T01-03", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "已经存在")

    # ---------- 班次配置 ----------
    def test_shift_config_page_and_validation_issue(self):
        response = self.client.get(
            reverse("organization:shift_config_list"),
            {"organization": self.org.pk, "team": self.team.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "待配置")

        cfg = self.early
        cfg.daily_worker_count = 5
        cfg.required_shifts = 18
        cfg.max_consecutive_rest_days = 4
        cfg.save()
        response = self.client.post(
            reverse("organization:shift_config_edit", args=[cfg.pk]),
            {
                "team": self.team.pk,
                "shift_name": "early",
                "daily_worker_count": "0",
                "min_consecutive_rest_days": "2",
                "max_consecutive_rest_days": "4",
                "required_shifts": "18",
                "special_position_count": "0",
                "is_active": "on",
                "exempt_employees": [],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "必须大于 0")

    def test_shift_config_toggle_enable_validation(self):
        cfg = self.early
        cfg.daily_worker_count = 5
        cfg.required_shifts = 18
        cfg.max_consecutive_rest_days = 4
        cfg.save()
        response = self.client.post(
            reverse("organization:shift_config_toggle", args=[cfg.pk]),
            {"next": reverse("organization:shift_config_list")},
        )
        self.assertEqual(response.status_code, 302)
        cfg.refresh_from_db()
        self.assertTrue(cfg.is_active)

    # ---------- 职工 ----------
    def test_employee_create_with_cascade_validation(self):
        night = self.team2.shift_configs.get(shift_name=ShiftConfig.ShiftName.NIGHT)
        response = self.client.post(
            reverse("personnel:employee_create"),
            {
                "employee_no": "E900",
                "name": "测试职工",
                "team": self.team.pk,
                "shift_config": night.pk,  # 二班的配置
                "worked_shifts": "0",
                "min_required_shifts": "18",
                "position": "值班员",
                "is_active": "on",
                "remark": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "班次配置必须属于所选班组")

        response = self.client.post(
            reverse("personnel:employee_create"),
            {
                "employee_no": "E900",
                "name": "测试职工",
                "team": self.team.pk,
                "shift_config": self.early.pk,
                "worked_shifts": "0",
                "min_required_shifts": "18",
                "position": "值班员",
                "is_active": "on",
                "remark": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Employee.objects.filter(employee_no="E900").exists())

    def test_employee_list_filter_and_pagination(self):
        response = self.client.get(reverse("personnel:employee_list"), {"q": "职工1"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "职工1")
        self.assertNotContains(response, "职工20")

        response = self.client.get(
            reverse("personnel:employee_list"), {"team": self.team2.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "暂无职工")

        # 25 人，每页 20 → 第 2 页
        response = self.client.get(reverse("personnel:employee_list"), {"page": 2})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "第 2 / 2 页")

    def test_employee_toggle_keeps_history(self):
        emp = Employee.objects.get(employee_no="E001")
        response = self.client.post(
            reverse("personnel:employee_toggle", args=[emp.pk]),
            {"next": reverse("personnel:employee_list")},
        )
        self.assertEqual(response.status_code, 302)
        emp.refresh_from_db()
        self.assertFalse(emp.is_active)
        self.assertEqual(emp.schedule_records.count(), 3)

    # ---------- 联动 JSON 接口 ----------
    def test_cascade_json_endpoints(self):
        response = self.client.get(
            reverse("organization:teams_json"), {"organization": self.org.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["teams"]), 2)

        response = self.client.get(
            reverse("organization:shift_configs_json"), {"team": self.team.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["shift_configs"]), 3)

    # ---------- 职工导入预览 ----------
    def test_import_preview_validates_without_writing(self):
        content = (
            "队组编号,班组编号,职工编号,姓名,岗位,班次,已上班数,矿上应上最少班数\n"
            "ORG-01,T01-01,E901,新职工,值班员,early,15,18\n"
            "ORG-01,T01-01,E901,重复编号,值班员,early,0,18\n"
            "ORG-02,T01-02,E902,错组职工,值班员,early,0,18\n"
            "ORG-99,T01-01,E903,坏队职工,值班员,early,0,18\n"
        )
        upload = SimpleUploadedFile(
            "employees.csv", content.encode("utf-8"), content_type="text/csv"
        )
        response = self.client.post(reverse("personnel:employee_import"), {"file": upload})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1</span> 行校验通过")
        self.assertContains(response, "3</span> 行校验失败")
        self.assertContains(response, "队组编号不存在")
        self.assertContains(response, "队组与班组不匹配")
        self.assertContains(response, "职工编号在文件内重复")
        self.assertContains(response, "仅校验，未写入数据库")
        self.assertContains(response, "不是职工工号")
        self.assertFalse(Employee.objects.filter(employee_no="E901").exists())

    def test_import_xlsx_preview(self):
        # XLSX 实际解析与校验（非仅页面文字）
        from io import BytesIO

        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["队组编号", "班组编号", "职工编号", "姓名", "岗位", "班次", "已上班数", "矿上应上最少班数"])
        ws.append(["ORG-01", "T01-01", "E905", "表格职工", "值班员", "early", 15, 18])
        ws.append(["ORG-99", "T01-01", "E906", "坏队", "值班员", "early", 0, 18])
        buffer = BytesIO()
        wb.save(buffer)
        upload = SimpleUploadedFile(
            "employees.xlsx",
            buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response = self.client.post(reverse("personnel:employee_import"), {"file": upload})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1</span> 行校验通过")
        self.assertContains(response, "1</span> 行校验失败")
        self.assertContains(response, "队组编号不存在")
        self.assertFalse(Employee.objects.filter(employee_no="E905").exists())

    def test_import_template_download(self):
        response = self.client.get(reverse("personnel:import_template"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        content = response.content.decode("utf-8")
        self.assertIn(
            "队组编号,班组编号,职工编号,姓名,岗位,班次,已上班数,矿上应上最少班数", content
        )

    # ---------- 排班计划 / 结果 ----------
    def test_schedule_plan_shows_team_readiness(self):
        response = self.client.get(
            reverse("scheduler:schedule_plan"),
            {"organization": self.org.pk, "team": self.team.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "排班周期")
        self.assertContains(response, "25")
        # 生成排班入口已开放：三个班次配置各有一个生成按钮
        self.assertEqual(response.content.decode().count(">生成并保存排班</button>"), 3)

    def test_schedule_results_grid_and_empty_state(self):
        response = self.client.get(
            reverse("scheduler:schedule_results"),
            {"organization": self.org.pk, "team": self.team.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "职工1")
        self.assertContains(response, "早")
        self.assertContains(response, "休")

        response = self.client.get(
            reverse("scheduler:schedule_results"),
            {"organization": self.org.pk, "team": self.team2.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "当前周期暂无排班记录")

    # ---------- 操作日志 ----------
    def test_operation_log_pages(self):
        # 经登录视图登录一次以产生审计日志（client.login 不经过视图）
        self.client.post(
            reverse("accounts:login"), {"username": "ui_tester", "password": "pw"}
        )
        response = self.client.get(reverse("auditlog:log_list"))
        self.assertEqual(response.status_code, 200)
        # 登录动作本身会被记录
        self.assertContains(response, "登录成功")

        from apps.auditlog.services import log_operation

        entry = log_operation(
            user=self.user, action="create", module="unit", description="创建队组",
            before_data={"a": 1}, after_data={"a": 2},
        )
        response = self.client.get(reverse("auditlog:log_list"))
        self.assertContains(response, "创建队组")
        response = self.client.get(reverse("auditlog:log_detail", args=[entry.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "操作后数据")


VALID_IMPORT_CONTENT = (
    "队组编号,班组编号,职工编号,姓名,岗位,班次,已上班数,矿上应上最少班数\n"
    "ORG-01,T01-01,E910,导入职工,值班员,early,15,18\n"
    "ORG-01,T01-01,E911,空岗位职工,,middle,0,32767\n"
)


class ImportConfirmFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("import_super", password="pw")
        cls.user.is_superuser = True
        cls.user.is_staff = True
        cls.user.save()
        UserProfile.objects.update_or_create(user=cls.user, defaults={"role": "super"})
        cls.org = OrganizationUnit.objects.create(name="采煤一队", code="ORG-01")
        cls.team = Team.objects.create(organization=cls.org, name="一班", code="T01-01")

    def setUp(self):
        self.client.login(username="import_super", password="pw")

    def _preview(self, content):
        upload = SimpleUploadedFile(
            "employees.csv", content.encode("utf-8"), content_type="text/csv"
        )
        return self.client.post(reverse("personnel:employee_import"), {"file": upload})

    def _token_from(self, response):
        match = re.search(
            r'name="import_token" value="([^"]+)"', response.content.decode()
        )
        self.assertIsNotNone(match, "预览页应包含确认令牌")
        return match.group(1)

    def test_full_import_confirm_flow_with_correct_mapping(self):
        response = self._preview(VALID_IMPORT_CONTENT)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "确认导入")
        token = self._token_from(response)

        confirm = self.client.post(
            reverse("personnel:employee_import"),
            {"action": "confirm", "import_token": token},
        )
        self.assertRedirects(confirm, reverse("personnel:employee_list"))

        e1 = Employee.objects.get(employee_no="E910")
        self.assertEqual(e1.name, "导入职工")
        self.assertEqual(e1.position, "值班员")
        self.assertEqual(e1.team, self.team)
        self.assertEqual(e1.shift_config, self.team.shift_configs.get(shift_name="early"))
        self.assertEqual(e1.worked_shifts, 15)        # 第7列 → worked_shifts
        self.assertEqual(e1.min_required_shifts, 18)  # 第8列 → min_required_shifts（不参与算法）

        e2 = Employee.objects.get(employee_no="E911")
        self.assertEqual(e2.position, "")             # 空岗位按现有规则保留为空
        self.assertEqual(e2.shift_config.shift_name, "middle")
        self.assertEqual(e2.worked_shifts, 0)
        self.assertEqual(e2.min_required_shifts, 32767)

        self.assertTrue(OperationLog.objects.filter(module="employee", action="import").exists())

    def test_stale_or_invalid_token_writes_nothing(self):
        self._preview(VALID_IMPORT_CONTENT)
        confirm = self.client.post(
            reverse("personnel:employee_import"),
            {"action": "confirm", "import_token": "bogus-token"},
        )
        self.assertRedirects(confirm, reverse("personnel:employee_import"))
        self.assertFalse(Employee.objects.filter(employee_no__in=["E910", "E911"]).exists())

    def test_confirm_revalidates_against_current_state(self):
        response = self._preview(VALID_IMPORT_CONTENT)
        token = self._token_from(response)
        # 预览后班组被停用 → 确认时重新校验必须拦截，整批不写入
        self.team.is_active = False
        self.team.save(update_fields=["is_active"])
        confirm = self.client.post(
            reverse("personnel:employee_import"),
            {"action": "confirm", "import_token": token},
        )
        self.assertRedirects(confirm, reverse("personnel:employee_import"))
        self.assertFalse(Employee.objects.filter(employee_no__in=["E910", "E911"]).exists())

    def test_errors_hide_confirm_button_and_write_nothing(self):
        content = (
            "队组编号,班组编号,职工编号,姓名,岗位,班次,已上班数,矿上应上最少班数\n"
            "ORG-01,T01-01,E910,导入职工,值班员,early,15,18\n"
            "ORG-99,T01-01,E920,坏队职工,值班员,early,0,18\n"
        )
        response = self._preview(content)
        self.assertContains(response, "队组编号不存在")
        # 存在错误时不渲染确认表单（页面说明文案本身含“确认导入”字样，故断言表单元素）
        self.assertNotContains(response, 'name="action" value="confirm"')
        self.assertFalse(Employee.objects.filter(employee_no__in=["E910", "E920"]).exists())
        # 未暂存会话时直接确认同样不写入
        confirm = self.client.post(
            reverse("personnel:employee_import"),
            {"action": "confirm", "import_token": "whatever"},
        )
        self.assertFalse(Employee.objects.filter(employee_no__in=["E910", "E920"]).exists())

    def test_member_cannot_upload_or_confirm(self):
        member = get_user_model().objects.create_user("import_member", password="pw")
        member.save()
        UserProfile.objects.update_or_create(
            user=member, defaults={"role": "member", "organization": self.org}
        )
        self.client.login(username="import_member", password="pw")
        upload = SimpleUploadedFile(
            "employees.csv", VALID_IMPORT_CONTENT.encode("utf-8"), content_type="text/csv"
        )
        self.assertEqual(
            self.client.post(reverse("personnel:employee_import"), {"file": upload}).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("personnel:employee_import"),
                {"action": "confirm", "import_token": "x"},
            ).status_code,
            403,
        )
        self.assertFalse(Employee.objects.filter(employee_no__in=["E910", "E911"]).exists())

    def test_template_header_exact_order(self):
        response = self.client.get(reverse("personnel:import_template"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn(
            "队组编号,班组编号,职工编号,姓名,岗位,班次,已上班数,矿上应上最少班数", content
        )
