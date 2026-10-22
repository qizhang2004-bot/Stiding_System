# 路由设计

| URL | 视图 | 名称 | 角色 | 说明 |
| --- | --- | --- | --- | --- |
| /accounts/login/ | accounts.AppLoginView | accounts:login | 公开 | 统一登录（成功/失败审计） |
| /accounts/logout/ | accounts.AppLogoutView | accounts:logout | 登录 | 登出（POST，审计） |
| /accounts/home/ | accounts.home | accounts:home | 登录 | 按角色跳转首页 |
| /accounts/password-change/ | accounts 密码修改 | accounts:password_change | 登录 | 修改本人密码 |
| /accounts/users/ | accounts.user_list | accounts:user_list | super | 账号管理列表 |
| /accounts/users/create/ | accounts.user_create | accounts:user_create | super | 创建账号（角色+队组） |
| /accounts/users/<pk>/ | accounts.user_detail | accounts:user_detail | super | 账号详情 |
| /accounts/users/<pk>/edit/ | accounts.user_edit | accounts:user_edit | super | 修改角色/所属队组 |
| /accounts/users/<pk>/toggle/ | accounts.user_toggle | accounts:user_toggle | super | 停用/恢复账号（POST） |
| /accounts/users/<pk>/reset-password/ | accounts.user_reset_password | accounts:user_reset_password | super | 重置密码 |
| /accounts/organization-members/ | accounts.member_list | accounts:member_list | admin | 本队组成员账号列表 |
| /accounts/organization-members/create/ | accounts.member_create | accounts:member_create | admin | 创建本队组普通成员 |
| /accounts/organization-members/<pk>/toggle/ | accounts.member_toggle | accounts:member_toggle | admin | 停用/恢复成员（POST） |
| /accounts/organization-members/<pk>/reset-password/ | accounts.member_reset_password | accounts:member_reset_password | admin | 重置成员密码 |
| /organization/units/create/confirm/ | organization.unit_create_confirm | organization:unit_create_confirm | super | 队组创建确认摘要 |
| /organization/units/create/success/ | organization.unit_create_success | organization:unit_create_success | super | 队组创建成功（含初始密码） |
| /scheduler/shift-board/ | scheduler.shift_board | scheduler:shift_board | member+ | 班次看板（只读） |
| /personnel/person-board/ | personnel.person_board | personnel:person_board | member+ | 个人信息看板（只读） |

## 角色与权限约定

- 未登录 → 跳登录页；权限不足 → 403 无权限页；跨队组按 ID 访问 → 404。
- 队组数据范围：`apps/accounts/permissions.visible_organizations()`；所有对象查询经
  `get_org_or_404 / get_team_or_404 / get_shift_config_or_404 / get_employee_or_404` 过滤。
- 队组管理相关页面要求 admin 及以上；队组创建/编辑/启停与账号管理、操作日志要求 super；
  普通成员仅可访问班次看板、个人信息看板、修改密码。
- 其余业务路由（队组/班组/班次配置/职工/排班）见各应用 urls.py，均按角色 + 队组范围控制。


## 个人周期日历与状态下拉（2026 追加）

- `GET /personnel/employees/<pk>/calendar/?period=YYYY-MM`：按排班周期（该月 25 日 ~ 次月 24 日）
  展示个人日历，缺省当前周期；每格显示计划班次、实际状态（下拉框）、岗位。
- `POST /personnel/employees/<pk>/calendar/actual/`：实际状态即时保存（JSON：work_date、actual_type、period）；
  仅队组管理员及以上、且限本队组；幂等（重复提交不重复写库），计划班次永不被覆盖；写入审计日志。

## 队组权限（2026 追加）

- 队组管理员的所有页面/操作强制限定所属队组（`role_required` 内含 `require_organization`）。
- 未绑定有效队组的队组管理员：相关页面返回 403，并提示「您的账号未绑定有效队组，请联系超级管理员配置」。
- 超级管理员不受队组限制，可通过 `?organization=`/`?team=` 切换。


## 排班维护（人工增删，2026 简化版）

- 看板即排班日历：`GET /scheduler/shift-board/?period=YYYY-MM[&organization=&team=]`，
  按排班周期（25 日~次月 24 日）展示每天早/中/晚当前已安排人员（按班组分组），不再展示计划/实际/请假统计。
- 添加人员：`GET|POST /scheduler/shift-board/<date>/<shift>/add/`，候选为当天未安排上班的在册职工（按班组分组）。
- 删除人员：`POST /scheduler/shift-board/records/<record_id>/remove/`。
- 数据规则（数据库为唯一事实来源）：
  1. 当前有效排班 = `status ∈ {confirmed, published}` 且 `is_active=True`；
  2. 人工添加 → 新建 `kind=adjustment` 批次并在同一事务内确认，写入新的正式记录（不改动原记录）；
  3. 人工删除 → 仅将该记录 `is_active=False`（软删除，记录与审计保留），不影响该人员其他日期；
  4. 整周期“作废替换”时，同队组同周期的既有批次记录与人工补充记录一并置为 `superseded`，
     避免旧数据覆盖或重复显示人工修改（记录均保留可追溯）；
  5. 看板/详情/个人日历/统计均通过同一查询口径 `selectors.active_schedule_records` 取数。
- 排班生成仍执行原有资格、岗位覆盖、每日人数上限等约束；人工维护阶段只校验权限、队组/班组范围、
  日期与班次有效性与“一人一天最多一条上班记录”，不再重跑整周期算法。
