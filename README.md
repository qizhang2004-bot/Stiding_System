# Stiding 排班系统

面向煤矿队组/班组场景的排班管理系统：**Django + MySQL + OR-Tools CP-SAT**。
按「队组 → 班组 → 班次配置 → 职工」组织数据，按排班周期（每月 25 日至次月 24 日）生成、维护与归档排班，
并提供班次看板、个人日历、排班结果、统计看板与操作审计。

## 功能概览

- **三级账号与数据隔离**：超级管理员（全部队组）、队组管理员（本队组）、普通成员（本队组只读）。
- **基础数据**：队组、班组、班次配置（早/中/晚独立配置）、职工、岗位与资格、豁免人员。
- **8 列 CSV 批量导入**：校验预览 → 全部通过后事务写入；失败整批回滚。
- **排班生成（生成即保存）**：CP-SAT 统一求解（每日人数、岗位人数、最低班数、连续休息、连休质量），
  单事务写入新版本并退休旧有效记录；失败零写入、旧排班完整保留。
- **人工调整**：班次看板/个人日历内改班、添加、取消（软删除），单条记录级操作并写审计。
- **历史与归档**：生成新周期时自动归档该队组更早的已结束周期；管理员也可显式归档；
  归档周期只读，保留全部记录与批次关联。
- **看板与统计**：班次看板、排班结果（当前/历史批次）、首页排班人数折线图、个人信息看板。
- **人员日历**：队员可从个人信息看板点击人员查看其只读排班日历（限本队组）。

## 技术栈

| 组件 | 选型 |
| --- | --- |
| 语言 | Python 3.12+（本地以 3.14 验证，CI 使用 3.13） |
| Web 框架 | Django 5.2 |
| 数据库 | MySQL 8.0+（本地以 MySQL 9.x 验证）；未配置 `DB_NAME` 时回退 SQLite |
| 求解器 | Google OR-Tools CP-SAT |
| 前端 | Django 模板 + 原生 CSS/JS（零 CDN 依赖，图表为内联 SVG） |
| 部署 | Gunicorn + WhiteNoise（见 `.github/workflows/Deploy.yml`） |

## 目录结构

```
apps/           业务应用（core / accounts / organization / personnel / scheduler / auditlog）
  scheduler/    排班模型、求解引擎（engine/）、服务（drafting）、查询（selectors）
config/         Django 设置与路由
templates/      页面模板
static/         CSS / JS / 图标
tests/          跨应用验收测试（应用内测试位于各 apps/*/tests*.py）
docs/           设计文档（数据模型、路由、排班规则、导入格式等）
.github/workflows/  CI（测试）与 Deploy（部署）
```

## 关键业务规则

- **排班周期**：每月 25 日至次月 24 日（如 2026-09-25 ~ 2026-10-24）。
- **有效记录**：`status ∈ {confirmed, published}` 且 `is_active=True`；休息记录同样属于有效排班但不算上班班数。
- **一人一天一条有效记录**：应用层 `Team` 行锁 + 事务内写入后断言保证。
- **班次隔离**：职工通过 `Employee.shift_config` 归属班次；可行性检查、求解器输入、生成快照、替换范围
  全部按「班组 + 班次」隔离，生成早班不会影响中班/夜班。
- **豁免人员**：不设最低班数，且不受连续休息上下限、孤立工作日、单日休息等休息规则约束；
  仍受每日人数、岗位人数、班次归属等硬约束。
- **归档**：仅已结束周期可归档；归档后只读；生成新周期时自动归档该队组所有更早的已结束未归档周期。
- **草稿**：历史遗留批次可查看/确认；当前主流程为「生成并保存」，不产生草稿。

## 本地运行

```bash
# 1) 依赖
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2) 环境变量（复制示例后填写，切勿提交真实密钥）
cp .env.example .env
#   DJANGO_SECRET_KEY / DJANGO_DEBUG / DJANGO_ALLOWED_HOSTS
#   DB_NAME / DB_USER / DB_PASSWORD / DB_HOST / DB_PORT

# 3) 数据库迁移并启动
python manage.py migrate
python manage.py runserver
# 访问 http://127.0.0.1:8000/
```

> 说明：`DB_NAME` 非空时使用 MySQL；为空时回退 SQLite（仅便于快速本地体验）。
> `.env` 已在 `.gitignore` 中忽略。

## 测试

测试使用 Django 自动创建的**独立测试库**（`test_<DB_NAME>`），不会写入业务库。

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test --noinput          # 全部测试
python manage.py test tests --noinput    # 跨应用验收测试
```

测试覆盖：组织/权限隔离、CSV 导入、排班生成与替换、人工操作、归档与只读、班次人员隔离、
豁免规则、历史查询、个人信息看板与人员日历、首页图表与统计口径、并发与回滚等。

## 持续集成

- `.github/workflows/ci.yml`：推送 `main`、Pull Request、手动触发；使用 MySQL 8 服务容器作为隔离测试库，
  执行 `check`、迁移变更检查与完整测试。
- `.github/workflows/Deploy.yml`：既有部署流程（推送 `main` 后 SSH 部署），敏感信息全部来自 GitHub Secrets。
