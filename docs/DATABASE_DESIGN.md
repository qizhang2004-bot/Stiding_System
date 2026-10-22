# 数据库设计

## 1. 全局约定

- 主键统一使用 Django `BigAutoField`（MySQL `BIGINT`，外键类型与被引用表主键一致）。
- MySQL 8.0+，所有业务表 InnoDB 引擎，字符集 `utf8mb4`。
- `USE_TZ = True`、`TIME_ZONE = "Asia/Shanghai"`；日期用 `DateField`，时间戳用 `DateTimeField`。
- `created_at`/`updated_at` 由公共抽象基类 `apps.core.models.TimeStampedModel` 提供。
- 历史数据相关外键使用 `on_delete=PROTECT`，防止误删破坏历史记录；
  停用数据（队组/班组/班次配置/职工）使用 `is_active=False`，不直接删除。
- 所有表使用显式 `db_table`，与实际数据库表名一一对应。

## 2. 表清单与关系

```text
organization_unit 1:N team 1:N shift_config 1:N employee

organization_unit / team / shift_config / employee 各自 1:N schedule_record

operation_log 通过 user 关联系统用户，通过 target_type/target_id 记录操作对象
```

| 表 | 模型 | 应用 | 说明 |
| --- | --- | --- | --- |
| organization_unit | OrganizationUnit | apps/organization | 队组信息 |
| team | Team | apps/organization | 班组信息 |
| shift_config | ShiftConfig | apps/organization | 班次配置（每班组早/中/晚三条） |
| employee | Employee | apps/personnel | 个人信息 |
| schedule_record | ScheduleRecord | apps/scheduler | 排班记录 |
| operation_log | OperationLog | apps/auditlog | 关键操作记录 |

## 3. organization_unit（队组）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| id | BigAutoField | 主键 |
| name | CharField(100) | 必填 |
| code | CharField(50) | 必填、全局唯一索引 |
| is_active | BooleanField | 默认 True |
| created_at / updated_at | DateTimeField | 自动维护 |

## 4. team（班组）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| id | BigAutoField | 主键 |
| organization | FK(organization_unit) | 必填、PROTECT |
| name | CharField(100) | 必填 |
| code | CharField(50) | 必填 |
| is_active | BooleanField | 默认 True |
| created_at / updated_at | DateTimeField | 自动维护 |

- 联合唯一约束 `(organization_id, code)`：同一队组内班组编号唯一。
- 新建班组时通过信号自动初始化早/中/晚三条班次配置（幂等）：
  - 实现：`apps/organization/signals.py` + `apps/organization/services.py` 的
    `ensure_default_shift_configs()`；重复保存/重复执行不会产生重复记录
    （get_or_create + 数据库联合唯一约束双重保证）。
  - 管理命令：`python manage.py ensure_shift_configs [--team-id 班组ID]`。
  - 自动创建的配置为 **is_active=False（待配置状态）**，占位参数
    （每日人数 1、目标班数 1、连休 2~4 天）仅用于占位，
    完成实际参数设置并启用前不会被正式排班使用。

## 5. shift_config（班次配置）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| id | BigAutoField | 主键 |
| team | FK(team) | 必填、PROTECT |
| shift_name | CharField(20) | 必填，枚举 early/middle/night |
| daily_worker_count | PositiveSmallIntegerField | 必填，> 0 |
| min_consecutive_rest_days | PositiveSmallIntegerField | 默认 2 |
| max_consecutive_rest_days | PositiveSmallIntegerField | 必填，>= min |
| required_shifts | PositiveSmallIntegerField | 必填，> 0 |
| special_position_count | PositiveSmallIntegerField | 默认 0 |
| exempt_employee_ids | JSONField | 默认 []，保存前校验人员属于本班组 |
| is_active | BooleanField | 默认 True |
| created_at / updated_at | DateTimeField | 自动维护 |

- 联合唯一约束 `(team_id, shift_name)`。
- 数据库 CHECK 约束：`daily_worker_count > 0`、`required_shifts > 0`、
  `max_consecutive_rest_days >= min_consecutive_rest_days`（模型 clean() 同步校验）。
- `required_shifts` 是排班算法的默认目标班数；`employee.min_required_shifts`
  仅用于展示/统计/预警，不参与排班算法。
- 配置状态流程：自动初始化的占位配置为 `is_active=False`（待配置）；
  设置真实参数后通过 `set_shift_config_active()` 启用（启用前执行完整校验）。
  正式排班只能使用 `get_active_shift_configs()` 返回的已启用配置。

## 6. employee（职工）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| id | BigAutoField | 主键 |
| employee_no | CharField(50) | 必填、全局唯一索引 |
| name | CharField(50) | 必填 |
| team | FK(team) | 必填、PROTECT |
| shift_config | FK(shift_config) | 必填、PROTECT |
| worked_shifts | PositiveSmallIntegerField | 默认 0，仅统计当前周期（每月25日至次月24日），不跨周期累计 |
| min_required_shifts | PositiveSmallIntegerField | 必填（矿上最低出勤班数） |
| position | CharField(100) | 可空 |
| is_active | BooleanField | 默认 True |
| remark | TextField | 可空 |
| created_at / updated_at | DateTimeField | 自动维护 |

- 联合普通索引 `(team_id, shift_config_id)`。
- 模型校验：`shift_config.team == team`（禁止班次配置与班组不一致）。

## 7. schedule_record（排班记录）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| id | BigAutoField | 主键 |
| schedule_batch_id | UUIDField | 必填（每版排班结果一个新批次号） |
| organization | FK(organization_unit) | 必填、PROTECT |
| team | FK(team) | 必填、PROTECT |
| shift_config | FK(shift_config) | 必填、PROTECT |
| employee | FK(employee) | 必填、PROTECT |
| work_date | DateField | 必填 |
| shift_type | CharField(20) | 枚举 early/middle/night/rest |
| position | CharField(100) | 可空 |
| status | CharField(20) | 枚举 draft/confirmed/published，默认 draft |
| remark | TextField | 可空 |
| created_at / updated_at | DateTimeField | 自动维护 |

- 联合唯一约束 `(schedule_batch_id, employee_id, work_date)`：
  同一批次同一职工同一天仅一条记录；休息日同样保存记录（shift_type=rest）。
- 普通索引：`(organization_id, work_date)`、`(team_id, work_date)`、
  `(shift_config_id, work_date)`、`(employee_id, work_date)`。
- 模型校验：organization 与 team 所属队组一致；shift_config 属于 team；
  employee 属于 team 且其 shift_config 与本记录一致。

## 8. operation_log（关键操作记录）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| id | BigAutoField | 主键 |
| user | FK(settings.AUTH_USER_MODEL) | 可空、SET_NULL |
| username | CharField(150) | 可空（用户名快照） |
| action | CharField(50) | 必填 |
| module | CharField(50) | 必填 |
| target_type | CharField(100) | 可空 |
| target_id | CharField(100) | 可空 |
| description | TextField | 必填 |
| before_data | JSONField | 可空 |
| after_data | JSONField | 可空 |
| ip_address | GenericIPAddressField | 可空 |
| created_at | DateTimeField | 自动生成，普通索引 |

- 日志只增不改：后台管理只读（禁改/禁删），写入统一走 `auditlog.services.log_operation`。
- 允许记录队组、班组、人员、班次配置及排班等模块的关键操作。

## 9. 索引汇总

| 索引 | 类型 |
| --- | --- |
| organization_unit.code | 唯一 |
| team(organization_id, code) | 联合唯一 |
| shift_config(team_id, shift_name) | 联合唯一 |
| employee.employee_no | 唯一 |
| employee(team_id, shift_config_id) | 普通 |
| schedule_record(schedule_batch_id, employee_id, work_date) | 联合唯一 |
| schedule_record(organization_id, work_date) | 普通 |
| schedule_record(team_id, work_date) | 普通 |
| schedule_record(shift_config_id, work_date) | 普通 |
| schedule_record(employee_id, work_date) | 普通 |
| operation_log(created_at) | 普通 |

## 10. 迁移管理

- 全部表结构由 Django Model + Migration 管理，禁止手工建表。
- 本版为 6 表初始迁移（0001_initial，仅 CreateModel），对既有数据无破坏性操作。
