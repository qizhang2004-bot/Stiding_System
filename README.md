# 井下排班系统（Stiding_System）

一个基于 **Django + OR-Tools CP-SAT** 的煤矿井下排班系统：按队组、班组组织人员，每个班组保存自己的排班约束，一键生成整个周期的排班表，支持首页班次人数折线图、出勤统计、班次展示、个人日历改班、容量预估、Excel 导出与达标统计。

组织结构：

```
队组（登录/权限单位）：综掘五队、综掘二队
  └── 班组：检修班、运输班、生产一班、生产二班、生产三班
        └── 人员（每人可挂多个岗位）
班次：早班、中班、晚班
```

---

## 功能特性

**首页（分角色）**

- 队组管理员：当前周期**早/中/晚班人数折线图**（鼠标悬停显示人数，右上角图例）；统计卡片：队组总人数、可达最低出勤总人数、班组数、本周期排班数；**未达最低出勤名单**（除去豁免人员）；最近排班入口。
- 超级管理员：**队组卡片点击切换**展示（队组多时可左右滑动），切换后显示该队组的折线图与统计。
- 队员：只读精简首页，不显示折线图与出勤统计。

**排班管理**

- **一键排班**：每个班组存储自己的约束（每天人数 / 岗位条件 / 连休范围 / 最少班数 / 豁免），一键生成周期排班；同一班组并发生成自动互斥。
- **岗位专人专岗**：岗位条件（≥/≤/=）优先满足；**一人一天只能干一个岗位**，多岗位人员当天只计入被指派的岗位配额，其余上班人员按「普通」岗补位。
- **班次硬性规则**：上传/导入的班次即该人员唯一可排的班次（早班人员只排早班）。
- **班次展示**：日历显示每天每个班次人数，按班组分层；班次详情**按当天实际岗位展示**（补位显示「普通」），点击人名联动改班。
- **个人日历**：上班日打「下井·班次」标签，可改班；已上班数按日期自动累计（周期从每月 25 号起算）。
- **容量预估**：求解前给出「最多能满几人 / 需豁免几人」，并给出岗位缺口、非豁免班数超总量等**中文无解诊断**。
- **确定性求解**：同配置必然得到同一份排班（配置派生随机种子），词典序多目标优化。
- **Excel 导出**：班次展示（日期表头 + 早/中/晚班行，单元格按班组分类、班组间空行、自动换行）；个人日历（日期 + 当天是否上班）。

**管理功能**

- 超级管理员：**队组增删改查**（新增队组可同时创建队组管理员/队员账号；编辑可改名、改账号、重置密码；删除需输入「删除」二次确认，自动复用/清理孤儿账号）、**班组重命名**。
- 队组管理员：管理本队组约束与人员、生成排班、改班。
- **人员导入**：支持文本/txt/csv/Excel；格式 `班组-姓名-岗位1,岗位2-已上班数-应上班数-默认班次`；**不写班组默认导入当前班组**，写了班组名则同名放入、不同名自动创建；**没有专门岗位请填「普通」**（未填自动兜底）；跨队组保护。
- 三层角色：超级管理员（后台+前台）、队组管理员（编辑本队组）、队员（只读）。

**后台 `/admin/`**

- grappelli 主题；**层级导航**：队组（内联班组）→ 班组（内联人员）→ 人员 → 排班记录 → 排班明细 → 岗位。

---

## 技术栈

| 项 | 说明 |
| --- | --- |
| 语言 | Python 3.10+ |
| Web 框架 | Django 5.2 LTS（`>=5.0,<6.0`，兼容服务器 MySQL 8.0） |
| 求解器 | Google OR-Tools（CP-SAT） |
| 数据库 | MySQL（utf8mb4，pymysql 驱动，连接参数用环境变量配置） |
| 后台主题 | django-grappelli |
| 图表 | ECharts（已本地化，不依赖外网 CDN） |

依赖见 [`requirements.txt`](requirements.txt)。

---

## 快速开始

```bash
git clone git@github.com:qizhang2004-bot/Stiding_System.git
cd Stiding_System

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 本地需有 MySQL（默认连 127.0.0.1:3306，可用 DB_* 环境变量覆盖），并创建数据库：
#   CREATE DATABASE stiding CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

python manage.py migrate
python manage.py seed_users        # 创建登录账号（见下）
python manage.py runserver         # http://127.0.0.1:8000/
```

### 登录账号（三种角色，按队组）

| 角色 | 账号 | 密码 | 权限 |
| --- | --- | --- | --- |
| **超级管理员** | `python manage.py createsuperuser` 创建 | 自定 | Django 后台 `/admin/` + 前台全部功能 |
| **队组管理员** | 队组电话 | `111111` | 管理本队组：编辑约束、导入人员、生成排班、改班 |
| **队员** | 队组缩写（如 `zjwd`） | `111111` | 只读查看本队组排班与个人日历 |

- 自定义账号：`python manage.py seed_users --user 账号 --password 密码 --group 综掘五队 --role team_admin|member`
- 队员为只读（前后端双重拦截）。

---

## 页面说明

| 页面 | 路径 | 说明 |
| --- | --- | --- |
| 首页 | `/` | 分角色：折线图 + 出勤统计 / 队组切换 / 队员精简 |
| 班组管理 | `/teams/` | 队组增删改查（超管）、班组增删改名、约束编辑、人员导入、生成排班 |
| 班次展示 | `/board/` | 日历显示每天每班次人数，按班组分层，可导出 Excel |
| 班次详情 | `/board/<y>/<m>/<d>/<班次>/` | 该班次各班组上班人员（按当天实际岗位），点人名跳个人日历 |
| 个人日历 | `/persons/<id>/` | 每人上班日打标签，下拉改班，可导出 Excel |
| 排班记录 | `/schedule/list/` | 所有排班记录 |
| 排班结果 | `/schedule/<id>/` | 求解状态 / 达标人数 / 人均班数 / 违规数 / 每日名单 / 每人统计 |

---

## 排班算法

算法为单一文件 [`project/app/scheduler/scheduling.py`](project/app/scheduler/scheduling.py)，**不依赖 Django，可独立运行**。把「某人某天是否上某班」建模为 0/1 变量交给 CP-SAT 求解。

### 硬性约束（必须全部满足）

1. **每天下井人数**：每天所有班次总人数 == 该班应上人数。
2. **一人一天最多一个班**。
3. **班次硬性规则**：上传/导入的班次即该人员唯一可排的班次。
4. **岗位人数条件**：每天该岗位上班人数满足 至少 `>=` / 至多 `<=` / 等于 `=`；**一人一天只能干一个岗位**（多岗位人员当天只计入被指派的那个岗位配额，其余按普通岗补位）。
5. **连休规则**：连续休息天数 ∈ [2, 最大]，不允许单休、不允许连休超上限（含月初/月末）；最大值默认 `(周期天数 − 至少上班天数) // 3` 向下取整。
6. **连续上班**：至少 2 天（禁止只上一天班就休息）；上限 = `(周期天数 // 3) − 最短连续休息天`。
7. **逐人班数上下限**（可选，硬性）。

### 软性目标（词典序多目标，依次优化）

1. 每人班数尽量贴近目标（偏离最小）
2. 非豁免班数均衡
3. 未达标者尽量接近目标且互相均分
4. 软性避免单休
5. 豁免人员尽量少且均衡

### 两阶段求解

1. **阶段一**：最大化达标人数（模型更小、更快），并给每人「上 6 休 4」错峰提示加速收敛。
2. **阶段二**：固定达标人数，用**词典序多目标**优化（以阶段一解作为 hint 起步）。

**确定性**：求解器使用由配置内容自动派生的随机种子（单线程搜索），同配置必然得到同一份排班；可用 `config["random_seed"]` 覆盖。

### 容量预估与诊断

- `capacity_analysis`：`最多能满几人 = floor(总班次 / 最少班数)`、`需豁免几人 = 人数 − 最多能满`，纯算术秒开。
- `validate_config`：无解时给出中文诊断（总班次超容量、岗位持有人不足、非豁免硬性班数超总量、岗位缺口大于豁免可补班次等）。

### 独立调用示例

```python
from project.app.scheduler.scheduling import build_schedule

result = build_schedule({
    "workers": [{"name": "张三", "roles": ["电工"]}],
    "shifts": ["早班", "中班", "晚班"],
    "days": 30,
    "daily_total": 13,
    "role_req": {"电工": {"op": ">=", "count": 2}},
    "min_shift_target": 18,
    "exempt_workers": [],
    "rest_block": {"min": 2, "max": 4},
})
result.print_summary()
```

直接运行示例：`python project/app/scheduler/scheduling.py`

---

## 数据模型

排班明细**只存一张表**（`Assignment`），所有页面统一查询它，避免多表冗余与同步错误。

| 表 | 说明 |
| --- | --- |
| `Group` | 队组（登录与权限作用域） |
| `Team` | 班组（归属队组，存储排班约束） |
| `Role` | 岗位（电工 / 班长 / 皮带…） |
| `Person` | 人员（可挂多岗位、默认班次、已上/应上班数） |
| `Schedule` | 排班记录（配置快照 + 求解状态 + 单休/连休超限指标） |
| `Assignment` | **排班明细（唯一数据源）**：某排班 × 某人 × 第几天 × 班次 × 当天实际岗位 |
| `UserProfile` | 登录账号与队组绑定（角色：super / team_admin / member） |

---

## 项目结构

```
Stiding_System/
├── manage.py
├── requirements.txt
├── README.md
├── tools/
│   ├── migrate_sqlite_to_mysql.py   # 旧 SQLite 数据一键迁移 MySQL（服务器用）
│   └── import_fixture.py            # dumpdata JSON 按依赖顺序导入
└── project/
    ├── settings/
    │   ├── dev.py                   # 开发配置（本地 MySQL）
    │   └── prod.py                  # 生产配置（服务器 MySQL，环境变量可覆盖）
    ├── urls.py
    └── app/
        ├── order/ users/            # 预留应用
        └── scheduler/               # 排班系统主应用
            ├── scheduling.py        # ★ 算法模型（单一文件，不依赖 Django）
            ├── models.py            # 数据表定义
            ├── views.py             # 页面逻辑（含导入/导出/队组管理/首页图表）
            ├── admin.py             # grappelli 后台层级配置
            ├── urls.py
            ├── static/scheduler/    # Bootstrap / ECharts（全部本地化）
            ├── migrations/
            └── templates/scheduler/ # 页面模板
```

---

## 常见问题

- **提示无解？** 通常是每天应上人数太高或岗位条件过严，页面会给出中文诊断（如「总班次需求超出全员容量」「岗位缺口大于豁免可补班次」）。
- **有人排不满？** 容量有限时必然有人排不满。首页/结果页给出「可达最低出勤总人数 / 未达标名单」，把这些人设为豁免后重新生成即可。
- **岗位条件「等于 =」的含义**：每天恰好该岗位上班的人数；岗位条件只约束岗位人数，与个人要上满多少班无关——多岗位人员其余时间按普通岗补班照样达标。
- **连续上班天数**：至少 2 天（禁止只上一天班就休息），上限 = (周期天数÷3) − 最短连续休息天数（31 天周期为 8 天，随周期自动变化）。
- **已上班数怎么算？** 导入初始值 + 该班组最近排班中「日期 ≤ 今天」的上班天数，按日期自动累计；周期从每月 25 号起算（可在 `views.py` 的 `PERIOD_START_DAY` 修改）。
- **同样的配置为什么两次结果一样？** 求解器按配置内容派生确定性随机种子（同配置同结果）；重新生成会覆盖该班组同月份旧排班（含手动改班），生成前请确认。
- **导入人员**：不写班组默认导入当前班组；写了班组名同名放入、不同名自动创建新班组；没有专门岗位请填「普通」。

## 安全与运维

- **登录防爆破**：同 IP+账号 5 次失败锁定 5 分钟（进程内存级）。
- **操作审计**：改班 / 加人 / 导入 / 编辑 / 生成排班等写操作输出到 `scheduler.audit` 日志（dev 输出控制台；prod 由 gunicorn 收集到 `journalctl -u stiding -f`）。
- **并发保护**：同一班组同一时刻只允许一个生成任务（其他请求提示"正在生成"）；数据库为 MySQL，支持多 worker 并发读写。
- **静态资源本地化**：Bootstrap / ECharts 均存放于项目内，不依赖外网 CDN。

---

## 服务器部署

线上地址：https://stiding.qizhang2004.cn/

部署架构：**Cloudflare（DNS + 代理）→ Nginx（443 SSL 反代）→ Gunicorn（127.0.0.1:8001）→ Django**

### 配置文件位置

| 用途 | 路径 |
| --- | --- |
| 项目目录 | `/home/ubuntu/Stiding_System` |
| Python 虚拟环境 | `/home/ubuntu/Stiding_System/.venv` |
| systemd 服务 | `/etc/systemd/system/stiding.service` |
| 环境变量（SECRET_KEY / DB） | `/etc/stiding.env` |
| Nginx 站点配置 | `/etc/nginx/sites-available/stiding`（软链到 `sites-enabled/`） |
| Gunicorn 配置 | 写在 systemd 服务的 `ExecStart` 里（绑 `127.0.0.1:8001`） |
| 静态文件目录 | `/home/ubuntu/Stiding_System/project/staticfiles` |
| MySQL 数据库 | 服务器 MySQL 实例，库名 `stiding`（utf8mb4） |
| SSL 证书 / 私钥 | `/home/ubuntu/ubuntu.pem` / `/home/ubuntu/ubuntu.key`（通配符 `*.qizhang2004.cn`） |
| 生产配置 | `project/settings/prod.py` |

### 缓存

- **静态文件 `/static/`**：Nginx 加了 `expires 30d` + `Cache-Control: max-age=2592000`（30 天强缓存）。
- **动态页面**：不缓存（排班数据、登录态会变化，不应在 Nginx 层缓存）。
- 注意：Cloudflare WAF 若拦截 `/static/vendor/` 之类路径，需在 CF 规则放行（本项目静态均在 `/static/scheduler/`、`/static/grappelli/`，无 vendor 前缀）。

### 自动部署

push 到 `main` 分支会触发 GitHub Actions（`.github/workflows/Deploy.yml`）：
SSH 到服务器 → `git pull` → 加载 `/etc/stiding.env` → `pip install` →
**`sudo mysql` 幂等建库授权（stiding 库给 django 用户）** → `migrate` → `collectstatic` → `systemctl restart stiding`。

> 依赖 GitHub 仓库的三个 Secrets：`SERVER_HOST`、`SERVER_USER`、`SERVER_KEY`。

### MySQL 数据库（服务器）

服务器 MySQL 沿用 threeminutes 项目的配置风格：应用连接账号 `django`（密码默认 `Zq//02089754`，可用 `DB_PASSWORD` 环境变量覆盖），库名 `stiding`。部署脚本会自动幂等建库与授权。

`/etc/stiding.env`（systemd 与部署脚本都会加载）：

```bash
SECRET_KEY=<随机密钥>
DJANGO_SECRET_KEY=<同上>
# 以下可选，默认值见 settings/prod.py：
# DB_NAME=stiding
# DB_USER=django
# DB_PASSWORD=Zq//02089754
# DB_HOST=localhost
# DB_PORT=3306
```

### 旧 SQLite 数据迁移到 MySQL（一次性）

部署完成后，在服务器上执行一键迁移脚本，把旧 `project/db.sqlite3` 里的全部数据（人员/班组/排班/账号）迁入 MySQL：

```bash
cd /home/ubuntu/Stiding_System
source .venv/bin/activate
export DJANGO_SETTINGS_MODULE=project.settings.prod
python tools/migrate_sqlite_to_mysql.py
sudo systemctl restart stiding
```

> 该脚本清空 MySQL 中本项目各表后，按依赖顺序从旧 SQLite 文件逐表拷贝（保持主键与外键关系），可重复执行（幂等）。

### 常用运维命令

```bash
# 查看服务状态 / 实时日志
systemctl status stiding
journalctl -u stiding -f

# 重启服务
sudo systemctl restart stiding

# 重载 nginx
sudo nginx -t && sudo systemctl reload nginx

# 手动部署（不用 GitHub Actions）
cd /home/ubuntu/Stiding_System
git pull origin main
source .venv/bin/activate
export DJANGO_SETTINGS_MODULE=project.settings.prod
pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py collectstatic --noinput
sudo systemctl restart stiding
```

---

## License

内部项目，未指定开源许可证。
