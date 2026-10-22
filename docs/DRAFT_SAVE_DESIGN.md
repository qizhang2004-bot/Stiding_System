# 排班草稿生成与确认保存 · 设计文档（第六阶段）

> 状态：**已确认并实施**（scheduler 0002_schedulebatch 迁移；实现见 apps/scheduler/drafting.py）。
> 实施中发现的班次口径冲突已按确认结论处理：`ScheduleRecord.clean()` 放宽为班组级
> （允许同班组内跨班次职工写入，跨班组仍拒绝），算法口径不变（N=班组在职职工）。
>
> **专项审查后加固（2026 审查轮）**：确认流程采用显式锁协议与班组级冲突检查：
> 固定锁顺序 批次行 → ShiftConfig 行 → 职工行 → 冲突检查锁定读（select_for_update）；
> 冲突范围为班组级（同职工同一天在班组任意班次配置下的 confirmed/published 记录均冲突）；
> 消除 select_related 隐式联表锁依赖；锁定读绕过 REPEATABLE READ 快照直读最新已提交数据。

## 0. 结论先行

- 现有 `ScheduleRecord` 已有 `status`（draft/confirmed/published）与 `position` 字段，
  但其唯一约束、统计与边界查询均以正式记录语义设计，**草稿不写入该表**。
- 新增唯一的一张**批次头表 `schedule_batch`**：保存批次元数据、完整排班明细 JSON、
  配置与职工快照、求解与校验结果；确认时在同一事务内展开明细写入 `ScheduleRecord`
  （status=confirmed，沿用草稿批次号）。
- 不新增排班明细表；不修改任何现有表；现有统计、考勤与跨周期边界查询零改动。

## 1. 批次表字段（schedule_batch）

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| batch_id | UUIDField(default=uuid4, unique, editable=False) | 否 | 批次标识，草稿与正式共用 |
| organization | FK(OrganizationUnit, PROTECT, related_name="schedule_batches") | 否 | 队组 |
| team | FK(Team, PROTECT) | 否 | 班组 |
| shift_config | FK(ShiftConfig, PROTECT) | 否 | 班次配置 |
| period_start / period_end | DateField | 否 | 排班周期起止（25日~次月24日） |
| mode | CharField(20) | 否 | 人多(surplus)/人少(tight)快照 |
| daily_worker_count | PositiveSmallIntegerField | 否 | 每日人数 R 快照 |
| position_requirements | JSONField(default=dict) | 否 | 岗位配置快照 {岗位: 人数} |
| employee_targets | JSONField(default=dict) | 否 | 目标班数快照 {职工ID: 班数} |
| employee_qualifications | JSONField(default=dict) | 否 | 在职职工+岗位资格快照 {职工ID: [岗位]} |
| payload | JSONField | 否 | 完整排班明细 [{employee_id, work_date, shift_type, position(可空)}...] |
| summary | JSONField(default=dict) | 否 | 求解摘要（总/最大偏差、休息质量、边界说明、校验结果） |
| created_by | FK(User, PROTECT, related_name="created_schedule_batches") | 否 | 创建人 |
| confirmed_by | FK(User, PROTECT, null=True) | 是 | 确认人 |
| status | CharField(20, choices=draft/confirmed, default=draft) | 否 | 批次状态 |
| confirmed_at | DateTimeField | 是 | 确认时间 |
| created_at / updated_at | TimeStampedModel | 否 | 时间戳 |

索引：`(organization, created_at)`、`(shift_config, status)`；唯一约束：仅 `batch_id`。

## 2. 状态流转

```
[求解 feasible 且独立校验通过] ──生成──▶ draft（单行插入，失败零残留；生成后可查看）
draft ──确认成功──▶ confirmed（一次性、不可逆；同一事务内写入正式记录）
draft ──确认失败──▶ 保持 draft（整体回滚，无部分正式记录；提示重新生成）
```
不增加 cancelled/superseded 等无实际业务需求的状态。

## 3. 事务与并发保护

确认流程在 `transaction.atomic` 中按以下顺序执行：

1. `ScheduleBatch.objects.select_for_update().get(batch_id=...)`（行级锁，并发确认串行化）
2. 权限校验（见 §7）
3. 状态校验：非 draft → 拒绝（重复确认）
4. 确认前复核（见 §4）
5. 正式排班冲突检查（见 §4）
6. 重新独立校验 payload（见 §5）
7. 逐条 `create_schedule_record(...)` 写入（status=confirmed、草稿批次号）
8. 更新批次 status/confirmed_at/confirmed_by
9. `log_operation`（confirm）
10. 提交；任一步异常整体回滚

并发保护依据（真实库核查结果）：

- 同批次一人一天重复：数据库唯一约束
  `uniq_schedule_batch_employee_date (schedule_batch_id, employee_id, work_date)` **已存在**，兜底；
- 同批次重复确认：步骤 1 行锁 + 步骤 3 状态检查，后到者被拒；
- **跨批次同一天重复**：现有约束不覆盖（批次是版本化语义，跨批次允许并存）；
  确认时由应用层检查"同 (职工, work_date) 已存在于其他 confirmed 批次"（步骤 5），
  冲突即拒绝并提示——**不修改现有表**，最小化方案即应用层检查 + 现有同批次唯一约束。

## 4. 确认前复核（不信任快照）

确认时重读当前数据并逐一比对草稿快照，任一不一致 → 拒绝并返回"草稿已过期，请重新生成"：

- `shift_config.daily_worker_count`、`position_requirements`、`required_shifts`、`exempt_employee_ids`；
- 当前在职职工集合（team 内 is_active=True）与每人 `parse_positions(position)`；
- 排班周期（period_range）；
- 现有正式排班冲突：其他 confirmed 批次中同 (职工, work_date) 已存在 → 拒绝。

## 5. 数据完整性

- payload 可完整还原：每日人员（work_date+employee_id）、实际岗位（position）、
  班数统计（按 employee_id 聚合）、班次类型（shift_type=该配置班次）；
- 正式保存前用 `validate_batch_payload` 重算并核对：每日人数 == R、逐日岗位人数 == 配置、
  一人一天一班一岗、岗位资格、两条硬性休息规则、班数统计、无重复记录、周期完整；
- 保存失败整体回滚（单事务），不存在部分正式记录。

## 6. 迁移影响分析

- 仅新增 `schedule_batch` 表；不改现有表结构/索引/约束，不新增明细表；
- `ScheduleRecord` 语义不变（仅新增 confirmed 行）；存量数据无影响；
- 草稿绝不进入：`core` 工作台统计、`selectors.batch_summaries`/`build_schedule_grid`、
  `planning.build_rest_problem` 的跨周期边界查询（这些查询均只读 schedule_record，
  草稿不在其中，无需改动）。
- 回滚成本 = 删除该表。

## 7. 权限与审计

- 服务层函数（生成/查看/确认）逐次校验：`role ≥ ROLE_ADMIN`（member 抛 PermissionDenied）
  且 `批次.organization ∈ visible_organizations(user)`（跨队组抛 PermissionDenied）；
- 队组管理员仅限本队组；后端强制，不依赖前端；
- `created_by`（生成时）、`confirmed_by`（确认时）写入批次表；
- 生成与确认各写一条 OperationLog（复用 `auditlog.log_operation`）：
  module=schedule，action=create/confirm，target_type=schedule_batch，
  target_id=批次号，描述含队组/班组/班次/周期/记录数。

## 附录：核查证据

1. `SHOW CREATE TABLE schedule_record`（真实库）：
   `UNIQUE KEY uniq_schedule_batch_employee_date (schedule_batch_id, employee_id, work_date)`；
   organization/team/shift_config/employee 四个外键均 NOT NULL。
2. 现有表清单（真实库）：无 schedule_batch；有 operation_log（字段：user/username/action/
   module/target_type/target_id/description/created_at）。
3. ScheduleRecord 消费点代码核查：
   - `apps/core/views.py:55` 工作台按 status 分组统计（只读 schedule_record）；
   - `apps/scheduler/selectors.py` `batch_summaries`/`build_schedule_grid`（只读）；
   - `apps/scheduler/planning.py:181` 跨周期边界查询（只读）；
   以上均只查询 schedule_record，草稿存于 schedule_batch 即天然隔离。
4. 权限体系：`apps/accounts/permissions.py`（role_required、visible_organizations、
   get_shift_config_or_404）；审计：`apps/auditlog/services.py` `log_operation`。
