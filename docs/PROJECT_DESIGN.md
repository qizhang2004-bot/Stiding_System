# 项目总体设计

## 1. 项目概述
Stiding 排班系统面向队组/班组式组织：队组 -> 班组 -> 班次配置 -> 职工，
按批次生成排班记录，并对关键操作留痕。

## 2. 技术栈
- 后端框架：Django 5.2（Python 3.12+）
- 数据库：MySQL 8.0+（utf8mb4 / InnoDB），未配置 DB_* 时回退 SQLite
- 排班求解：OR-Tools CP-SAT（下一阶段基于 shift_config 规则实现）
- 部署：Gunicorn + WhiteNoise

## 3. 应用划分
| 应用 | 职责 |
| --- | --- |
| core | 公共基础模型（TimeStampedModel）、首页仪表盘、权限与中间件 |
| organization | 队组（OrganizationUnit）、班组（Team）、班次配置（ShiftConfig） |
| personnel | 职工（Employee） |
| scheduler | 排班记录（ScheduleRecord）、排班引擎（engine/，下一阶段改造） |
| auditlog | 关键操作记录（OperationLog） |

## 4. 核心数据流
创建队组 -> 创建班组（自动初始化早/中/晚班次配置）-> 配置班次规则 ->
录入职工（绑定班组+班次配置）-> 生成排班批次（schedule_batch_id）-> 排班记录留痕

## 5. 设计约定
- 分层：views 只做编排，业务逻辑在 services，校验在模型 clean() + 数据库约束。
- 历史数据保护：业务外键统一 PROTECT，停用用 is_active，不物理删除。
- 操作日志只增不改，后台只读。
- 当前阶段只完成数据库模型与完整性校验，不实现排班算法与业务页面。
