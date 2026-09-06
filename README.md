# 井下排班系统（Stiding_System）

一个基于 **Django + OR-Tools CP-SAT** 的煤矿井下排班系统：按队组、班组组织人员，
每个班组保存自己的排班约束，一键生成整个周期的排班表，并支持班次展示、个人日历、
手动改班、容量预估与达标统计。

组织结构：

```
队组（登录/权限单位）：综掘五队、综掘二队
  └── 班组：检修班、运输班、生产一班、生产二班、生产三班
        └── 人员（每人可挂多个岗位）
班次：早班、中班、晚班
```

---

## 功能特性

- **一键排班**：每个班组存储自己的约束（每天人数 / 岗位人数 / 连休 / 最少班数 / 豁免），一键生成周期排班表。
- **班次展示**：日历显示每天每个班次人数，按班组分层，点击下钻到个人并联动改班。
- **个人日历**：上班日打「下井·班次」标签，可在 早班/中班/晚班/休息 之间修改；已上班数按日期自动累计（周期从每月 25 号起算）。
- **容量预估**：求解前给出「最多能满几人 / 需豁免几人」，按 `floor(总班次/最少班数)` 快速估算，不虚报。
- **达标统计**：结果页给出求解状态、达标人数、人均班数、单休/连休超限次数，以及未达标名单与原因。
- **三层角色**：超级管理员（后台）、队组管理员（编辑本队组）、队员（只读）。

---

## 技术栈

| 项 | 说明 |
| --- | --- |
| 语言 | Python 3.10+ |
| Web 框架 | Django 5+（开发环境实测 6.0） |
| 求解器 | Google OR-Tools（CP-SAT） |
| 数据库 | MySQL（utf8mb4；连接参数用环境变量配置） |

依赖见 [`requirements.txt`](requirements.txt)：`Django>=5.0`、`ortools>=9.10`。

---

## 快速开始

```bash
git clone git@github.com:qizhang2004-bot/Stiding_System.git
cd Stiding_System

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python manage.py migrate
python manage.py seed_users        # 创建登录账号（见下）
python manage.py runserver         # http://127.0.0.1:8000/
```

### 登录账号（三种角色，按队组）

| 角色 | 账号 | 密码 | 权限 |
| --- | --- | --- | --- |
| **超级管理员** | `python manage.py createsuperuser` 创建 | 自定 | Django 后台 `/admin/`，管理所有队组/班组/人员/排班 |
| **队组管理员** | 队组电话 | `111111` | 管理本队组：编辑约束、导入人员、生成排班、改班 |
| **队员** | 队组缩写（如 `zjwd`） | `111111` | 只读查看本队组排班与个人日历 |

- 自定义账号：`python manage.py seed_users --user 账号 --password 密码 --group 综掘五队 --role team_admin|member`
- 队员为只读（前后端双重拦截）。

---

## 页面说明

| 页面 | 路径 | 说明 |
| --- | --- | --- |
| 首页 | `/` | 使用流程引导 + 休息规则说明 + 数据统计 + 最近排班入口 |
| 班组管理 | `/teams/` | 编辑班组约束（每天人数 / 岗位人数 / 连休范围 / 最少班数 / 豁免）、导入人员、生成排班 |
| 班次展示 | `/board/` | 日历显示每天每班次人数，按班组分层，点击下钻到班次详情 |
| 班次详情 | `/board/<y>/<m>/<d>/<班次>/` | 该班次各班组上班人员，点人名跳个人日历 |
| 个人日历 | `/persons/<id>/` | 每人上班日打标签，下拉改班（早/中/晚/休息），改完自动联动 |
| 排班记录 | `/schedule/list/` | 所有排班记录（每班组每月保留最新一份） |
| 排班结果 | `/schedule/<id>/` | 求解状态 / 达标人数 / 人均班数 / 违规数 / 每日名单 / 每人统计 |

---

## 排班算法

算法为单一文件 [`project/app/scheduler/scheduling.py`](project/app/scheduler/scheduling.py)，
**不依赖 Django，可独立运行**。把「某人某天是否上某班」建模为 0/1 变量交给 CP-SAT 求解。

### 硬性约束（必须全部满足）

1. **每天下井人数**：每天所有班次总人数 == 该班应上人数（也支持每班精确人数）。
2. **一人一天最多一个班**。
3. **岗位人数条件**：每天该岗位上班人数满足 至少 `>=` / 至多 `<=` / 等于 `=`。
   **一人一天只能干一个岗位**：多岗位人员当天只计入被指派的那个岗位的配额。
4. **连休规则**：连续休息天数 ∈ [最少, 最多]（默认最少 2 天，不允许单休、不允许连休超上限；
   最大值默认为 `(周期天数 − 至少上班天数) // 3` 向下取整，也可手动指定）。
5. **连续上班至少 2 天**：不允许「只上一天班就休息」（含月初/月末边界）。
6. **逐人班数上下限**（可选，硬性）。
7. **班次硬性规则**：上传/导入的班次即该人员唯一可排的班次（早班人员只排早班）。

### 软性目标（尽量满足）

- 最大化达标人数（班数 ≥ 目标）；目标 = `应上班数 − 已上班数`，或全局最少班数。
- 每人班数尽量接近目标（偏离有惩罚）。
- 豁免人员不参与达标与休息约束，只补剩余班次（有豁免时非豁免人员恰好排到目标，
  剩余班次 = 总班次 mod 目标，必然小于目标，全部由豁免人员补齐）。

### 两阶段求解

1. **阶段一**：最大化达标人数（模型更小、更快），并给每人「上 6 休 4」错峰提示加速收敛。
2. **阶段二**：固定达标人数 → 用**词典序多目标**（OR-Tools 多次 minimize 即词典序）依次优化：
   ① 班数贴近目标 → ② 非豁免班数均衡 → ③ 未达标者尽量接近目标且均分 → ④ 软性避免单休 →
   ⑤ 豁免人员尽量少且均衡；以阶段一解作为 hint 起步。

**确定性**：求解器使用由配置内容自动派生的随机种子（单线程搜索），同配置必然得到同一份排班；
可用 `config["random_seed"]` 覆盖。生产形状（硬性 min/max）求解时间约 0.3~2 秒。

### 容量预估（`capacity_analysis`）

「最多能满几人 / 需豁免几人」按 `floor(总班次 / 最少班数)` 纯算术快速估算（带 `lru_cache`
缓存，同参数只算一次），页面秒开。避免出现「预估 27 人、实际只有 12 人」的虚报。

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
| `Assignment` | **排班明细（唯一数据源）**：某排班 × 某人 × 第几天 × 班次 |
| `UserProfile` | 登录账号与队组绑定（角色：super / team_admin / member） |

> 说明：排班明细不再塞进 `Schedule` 的 JSON 大字段。改班 = 增删改 `Assignment`，
> 统计（达标人数、每人班数、班次名单）从 `Assignment` 实时聚合，天然一致。

---

## 项目结构

```
Stiding_System/
├── manage.py
├── requirements.txt
├── README.md
└── project/
    ├── settings/dev.py            # 开发配置（MySQL）
    ├── urls.py
    └── app/
        ├── order/ users/          # 预留应用
        └── scheduler/             # 排班系统主应用
            ├── scheduling.py      # ★ 算法模型（单一文件，不依赖 Django）
            ├── models.py          # 数据表定义
            ├── views.py           # 页面逻辑
            ├── urls.py
            ├── migrations/
            └── templates/scheduler/   # 页面模板
```

---

## 常见问题

- **提示无解？** 通常是每天应上人数太高或岗位条件过严，页面会给出诊断（如「总班次需求超出全员容量」、
  「豁免过多关键岗位人员导致岗位缺口无法补齐」）。
- **有人排不满？** 容量有限时必然有人排不满。结果页给出「最多能满几人 / 需豁免几人」和短名单，把这些人设为豁免后重新生成即可。
- **岗位条件「等于 =」的含义**：每天恰好该岗位上班的人数（如「电工每天等于 2 人」= 每天恰有 2 人干电工岗）。
  岗位条件只约束岗位人数，与个人要上满多少班无关：多岗位人员其余时间按普通岗（或其他岗位）补班，
  照样可以达标。只有当「岗位全周期所需岗位班数 > 该岗位持有人最多能提供的岗位班数」时才无解。
- **已上班数怎么算？** 导入初始值 + 该班组最近排班中「日期 ≤ 今天」的上班天数，按日期自动累计；周期从每月 25 号起算（可在 `views.py` 的 `PERIOD_START_DAY` 修改）。
- **休息规则能改吗？** 连休最少天数固定为 2 天；最大值每班组可单独设置，不填时默认
  `(周期天数 − 至少上班天数) // 3` 向下取整（在 `views.py` 的 `_run_generate` / `team_manage` 计算）。
- **同样的配置为什么两次结果一样？** 求解器按配置内容派生确定性随机种子（同配置同结果）；
  重新生成会覆盖该班组同月份旧排班（含手动改班），生成前请确认。

## 安全与运维

- **登录防爆破**：同 IP+账号 5 次失败锁定 5 分钟（进程内存级）。
- **操作审计**：改班 / 加人 / 导入 / 编辑 / 生成排班等写操作输出到 `scheduler.audit` 日志
  （dev 输出到控制台；prod 由 gunicorn 收集到 `journalctl -u stiding -f`）。
- **并发保护**：同一班组同一时刻只允许一个生成任务（其他请求提示"正在生成"）；
  数据库为 MySQL，支持多 worker 并发读写。

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
| MySQL 数据库 | 服务器 MySQL 实例，库名 `stiding`（utf8mb4），连接参数见 `/etc/stiding.env` |
| SSL 证书 / 私钥 | `/home/ubuntu/ubuntu.pem` / `/home/ubuntu/ubuntu.key`（通配符 `*.qizhang2004.cn`） |
| 生产配置 | `project/settings/prod.py` |

### 缓存

- **静态文件 `/static/`**：Nginx 加了 `expires 30d` + `Cache-Control: max-age=2592000`（30 天强缓存）。
- **动态页面**：不缓存（排班数据、登录态会变化，不应在 Nginx 层缓存）。
- Bootstrap 已本地化到 `scheduler/static/scheduler/`，不依赖 jsdelivr CDN，国内访问稳定。

### 自动部署

push 到 `main` 分支会触发 GitHub Actions（`.github/workflows/Deploy.yml`）：
SSH 到服务器 → `git pull` → `pip install` → `migrate` → `collectstatic` → `systemctl restart stiding`。

> 依赖 GitHub 仓库的三个 Secrets：`SERVER_HOST`、`SERVER_USER`、`SERVER_KEY`。

### MySQL 数据库（服务器）

服务器 MySQL 沿用 threeminutes 项目的配置风格：应用连接账号 `django`（密码默认 `Zq//02089754`，
可用 `DB_PASSWORD` 环境变量覆盖），库名 `stiding`。部署脚本会自动幂等建库与授权。

`/etc/stiding.env` 需配置（systemd 与部署脚本都会加载）：

```bash
SECRET_KEY=<随机密钥>
DJANGO_SECRET_KEY=<同上>
DB_ROOT_PASSWORD=<mysql root 密码，用于部署时自动建库授权>
# 以下可选，默认值与 threeminutes 生产一致：
# DB_NAME=stiding
# DB_USER=django
# DB_PASSWORD=Zq//02089754
# DB_HOST=localhost
# DB_PORT=3306
```

### 旧 SQLite 数据迁移到 MySQL（一次性）

在第一次拉取新版代码**之前**（旧代码还在用 SQLite 时），在服务器上执行：

```bash
cd /home/ubuntu/Stiding_System
source .venv/bin/activate
export DJANGO_SETTINGS_MODULE=project.settings.prod
python manage.py dumpdata --exclude auth.permission --exclude contenttypes --exclude sessions --exclude admin > /tmp/server_dump.json
```

然后正常部署（自动建库 + migrate），部署完成后导入旧数据：

```bash
cd /home/ubuntu/Stiding_System && source .venv/bin/activate
export DJANGO_SETTINGS_MODULE=project.settings.prod
python tools/import_fixture.py /tmp/server_dump.json
sudo systemctl restart stiding
```

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
