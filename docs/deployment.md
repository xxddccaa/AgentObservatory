# Agent Observatory 部署与运维

本文档描述 Agent Observatory 在单机上的标准部署、升级、启停与排障流程。
应用是本地优先、只读的观测台：它只解析并索引各 agent 的原始会话文件，
唯一的持久化事实来源是本机 PostgreSQL。

当前默认监听 `127.0.0.1:8125`，由 `agent-observatory` 自带 supervisor 托管
API 与 Indexer Worker 两个子进程，不依赖 systemd / docker / Redis。

## 本仓库的实际部署方式（优先于通用开发步骤）

使用场景是**单人、多台机器共享源码目录**。共享目录只留源码，每台机器独立部署到
`/root/.agent-observatory/`，不共享数据库，也不直接从共享源码运行服务。

| 本机路径 | 用途 |
| --- | --- |
| `config.env` | 本机配置、随机数据库口令及签名密钥，权限 600 |
| `credentials.env` | 本机管理员凭据，权限 600；账户 `admin`，密码不写进仓库 |
| `releases/<版本>/`、`current` | 源码快照、Python venv、Node 依赖和前端 dist；current 原子切换 |
| `postgresql/data/`、`postgresql/run/`、`postgresql/logs/` | 独立 PostgreSQL 16，默认 127.0.0.1:55433 |
| `backups/` | 部署前的 PostgreSQL custom-format 备份；不自动删除旧备份 |
| `run/`、`logs/`、`cache/`、`exports/`、`review/` | 本机状态、运行和验证产物 |

前置依赖为 Python 3.12、uv、PATH 上的 Node 22 / npm、PostgreSQL 16 二进制、
`postgres` 用户、`runuser` 和 `setfacl`（Debian / Ubuntu 的 `acl` 包）。
只为 postgres 授予 `/root` 和应用目录的遍历 ACL；数据库目录由 postgres 拥有，
不要停止或搬动其他项目正在使用的 PostgreSQL。
需要 npm 下载代理时，在本机 `config.env` 配置 `AGENT_OBS_DEPLOY_PROXY`。
访问 GitHub 使用本机代理，访问公司 GitLab 直连，不修改共享 `.npmrc`。

```bash
./manage.sh deploy --prepare-only  # 首次先生成本机配置、数据库和待验证快照，不启动应用
./manage.sh deploy --release /root/.agent-observatory/releases/<prepared-release>
./manage.sh status
./manage.sh up                    # 机器重启后：启动本机 PostgreSQL 和已部署快照
./manage.sh down
./manage.sh logs api --lines 100
```

首次创建管理员时，密码通过本机 `credentials.env`、`AGENT_OBS_ADMIN_PASSWORD` 或
交互输入提供；不把真实值放在 shell 命令示例里。已存在的账号不会在每次部署时重置。
需要重置时用 `./manage.sh passwd admin`，该操作会撤销已有会话 / API token，并写审计日志。
本机默认关闭 Storage 删除；所有原始 agent 数据目录仅被读取。

已有旧实例（例如 5432 上的旧 `agent_observatory`）时，不要直接切到空数据库：
先准备快照，再用**旧数据库配置**执行 CLI `down`，将旧数据库备份到本机 `backups/`，
以 `pg_restore --no-owner --no-acl` 恢复到空的独立数据库，核对表行数及迁移版本后部署。
不要删除旧数据库、修改其他项目的数据、自动 downgrade 或强制 kill supervisor。

升级直接运行 `./manage.sh deploy`：复制当前工作区（包括未提交改动）→ 本机安装依赖及构建
→ 备份 → 经审计 CLI 停止旧应用 → 迁移并检查模型漂移 → 原子切换 current → CLI 启动并验证心跳。
构建失败不会停止旧应用；迁移 / 启动失败会尝试恢复旧运行快照，不会自动回滚数据库。

验证只在本机快照内执行，`TEST_DATABASE_URL` / `E2E_DATABASE_URL` 必须指向独立的
`agent_observatory_ci`，不能是运行库。E2E 脚本在任何 downgrade / upgrade 前先验证库名。
共享仓库不要运行 `uv sync` / `npm ci`，也不要在里面创建 `.env`、运行时或测试报告。
API 只绑定 loopback；跨机器访问通过 SSH 隧道或单独配置的安全反向代理，保留鉴权。

以下为通用开发参考；所有安装、构建和运行步骤应在**本机源码副本**内进行。

## 1. 依赖

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.12 | 后端与 worker，推荐 `uv` 或仓库内 `backend/.venv` |
| Node | 22 | 前端构建，通过 PATH 使用，不假设机器路径 |
| PostgreSQL | 16 | 唯一持久化事实来源，必须可达 |
| tmux | 3.2+ | 仅人工排查时使用，应用不依赖 |

数据库约定示例（用户、口令和库名按环境替换）：

```text
dev  agent_observatory        role observatory  password <local-password>
ci   agent_observatory_ci     role observatory  password <local-password>   # 自动化测试专用
test agent_observatory_test   role observatory  password <local-password>
```

测试代码只允许连接 `agent_observatory_ci`，`backend/tests/conftest.py` 会拒绝其他库。

首次准备数据库：

```sql
CREATE ROLE observatory LOGIN PASSWORD '<local-password>';
CREATE DATABASE agent_observatory OWNER observatory;
CREATE DATABASE agent_observatory_ci OWNER observatory;
CREATE DATABASE agent_observatory_test OWNER observatory;
```

## 2. 安装

```bash
# 后端依赖（任选其一）
cd backend && uv sync
# 或：python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'

# 配置：首次由 init 自动生成随机密钥
cp .env.example backend/.env   # 手动复制时可自行填写密钥
```

部署通过 `AGENT_OBS_ENV_FILE` 读取本机 `config.env`；`backend/.env` 仅作为开发兼容入口。关键变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT_OBS_DATABASE_URL` | `postgresql+asyncpg://observatory:<password>@127.0.0.1:55433/agent_observatory` | 运行时数据库，按本机配置替换 |
| `AGENT_OBS_HOST` / `AGENT_OBS_PORT` | `127.0.0.1` / `8125` | API 监听地址与端口 |
| `AGENT_OBS_HOME` | `~/.agent-observatory` | 运行目录：`run/`（`supervisor.pid`、`supervisor-state.json`、lock/wakeup）、`logs/`（`*.jsonl`） |
| `AGENT_OBS_CODEX_HOME` | `~/.codex` | Codex 会话根，只读 |
| `AGENT_OBS_PI_HOME` | `~/.pi` | Pi 原生会话根，只读 |
| `AGENT_OBS_XDCODE_HOME` | `~/.xdcode` | XDCode 会话与归档根，只读 |
| `AGENT_OBS_STEPGO_HOME` | `~/.stepgo` | StepGo host 运行日志根，只读 |
| `AGENT_OBS_INDEXER_POLL_INTERVAL` | `2` | 增量扫描间隔（秒） |
| `AGENT_OBS_LOCAL_SOURCE_POLL_INTERVAL` | `5` | 本机数据源清单轮询间隔（秒） |
| `AGENT_OBS_WORKER_HEARTBEAT_INTERVAL` | `10` | worker 心跳（秒），健康接口据此判断 |
| `AGENT_OBS_SESSION_SECRET` / `AGENT_OBS_API_TOKEN_SECRET` | 由 `init` 生成 | 会话 Cookie 与 API Token 签名密钥 |
| `AGENT_OBS_ALLOWED_WORKSPACES` | 可空 | 工作区白名单，逗号分隔绝对路径，空=不限制 |
| `AGENT_OBS_EXTRA_ROOTS` | 可空 | `agent:path,...`，补充非标准数据根 |
| `AGENT_OBS_RETENTION_ENABLED` | `true` | Storage 清理总开关；`false` 时只保留预览 |
| `AGENT_OBS_RETENTION_MIN_HOURS` | `24` | 硬性保留期，比它新的数据永不可删 |
| `AGENT_OBS_RETENTION_PROTECTED_ROOTS` | 可空 | 逗号分隔的只读目录，例如 `~/.codex/sessions` |
| `AGENT_OBS_RETENTION_LARGE_RUN_BYTES` | `5368709120` | 超过该体积的清理需要额外确认 |
| `AGENT_OBS_RETENTION_PLAN_DRIFT_BYTES` | `67108864` | 计划与实际不一致时的拒绝阈值 |

前端：

```bash
cd frontend
PATH=/opt/node22/bin:$PATH npm ci
PATH=/opt/node22/bin:$PATH npm run build   # 产物 frontend/dist，由 API 同源托管
```

## 3. 初始化

```bash
cd backend
AGENT_OBS_ADMIN_PASSWORD='change-this-password' \
  .venv/bin/agent-observatory init      # 校验依赖、建 .env、应用迁移、构建前端
```

`init` 会：生成 `backend/.env`（若不存在，密钥随机化，权限 600）→ 执行
`alembic upgrade head` → 前端 `dist` 缺失或过期时执行 `npm ci` / `npm run build`。

创建管理员（幂等，会写审计行）：

```bash
.venv/bin/agent-observatory user-add <username> --role admin --password '<password>'
```

密码最短 10 位；省略 `--password` 时读 `AGENT_OBS_USER_PASSWORD` 或交互输入，
避免进 shell history。角色：`admin` / `operator` / `viewer`，可重复 `--role` 叠加。

## 4. 启动与常用命令

```bash
cd backend
.venv/bin/agent-observatory up          # 迁移 + 前端构建 + 启动 supervisor(api + worker)
.venv/bin/agent-observatory status      # 组件状态，--json 输出机器可读结果
.venv/bin/agent-observatory status --json | python3 -m json.tool | head -50
.venv/bin/agent-observatory doctor      # 依赖、迁移、端口、数据库检查
.venv/bin/agent-observatory logs worker --follow
.venv/bin/agent-observatory logs api --lines 200
.venv/bin/agent-observatory indexer --once   # 前台跑一轮增量扫描，便于排查
.venv/bin/agent-observatory restart api      # 只重启 API
.venv/bin/agent-observatory restart worker   # 只重启 Worker
.venv/bin/agent-observatory down             # 停止 supervisor 及全部子进程
```

`up` 会先执行 `alembic upgrade head`，并在 `frontend/dist/index.html` 缺失或
早于 `src/` 变更时自动重建前端。

进程模型：

- supervisor 常驻，`run/supervisor.pid` 记录 PID 与启动令牌（防 PID 复用误杀）。
- 子进程 api：`python -m uvicorn agent_observatory.main:app --host <HOST> --port <PORT>`。
- 子进程 worker：`agent_observatory.cli._run_worker`，内部为 IndexerWorker。
- 子进程异常退出后按退避自动重启；`CRASH_WINDOW=120s` 内失败 `CRASH_LIMIT=5` 次会
  进入 `crash-loop` 状态并停止重启，需人工介入（看日志 → 修复 → `restart`）。
- 日志按组件落在 `$AGENT_OBS_HOME/logs/{api,worker,supervisor}.jsonl`（JSON Lines，每行含 `ts` / `component` / `stream` / `message`）。

### 运维红线

所有启停、重启都属于"系统控制动作"，必须走 `agent-observatory` 命令：命令先写入
PostgreSQL `system_commands`，由 supervisor 领取执行，并落 `audit_logs` 审计行。
**不要用 `kill -9` / `pkill` 直接杀进程**，否则审计与状态会不一致。

## 5. 健康检查

```bash
curl -s 127.0.0.1:8125/api/health | python3 -m json.tool
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:8125/api/health/live
curl -s 127.0.0.1:8125/api/health/ready | python3 -m json.tool
curl -s 127.0.0.1:8125/api/system/status | python3 -m json.tool
```

`/api/health` 期望返回：

关键字段（完整响应还含 `environment`、`components`、`recovery_actions`）：

```json
{"status":"ok","version":"0.1.0","postgresql":true,"workers":{"indexer":true}}
```

`components.indexer.last_cycle` 里能看到每轮扫描的 `plan` / `ingest` / `discover` / `maintain` 耗时（毫秒）与 `changed` / `indexed` / `failures` / `duplicates_skipped` 计数，是判断扫描效率最直接的指标。

`/api/health/live` 只表示进程存活；`/api/health/ready` 额外要求 PostgreSQL 可达、
worker 心跳新鲜。排查顺序：`health/live` → `health/ready` → `doctor` → `logs`。

## 6. 升级发布流程

```bash
cd <repo>
git pull --ff-only

cd backend
uv sync                      # 或 .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic check          # 必须干净：模型与迁移无漂移

cd ../frontend
PATH=/opt/node22/bin:$PATH npm ci
PATH=/opt/node22/bin:$PATH npm run build

cd ../backend
.venv/bin/agent-observatory restart api
.venv/bin/agent-observatory restart worker
.venv/bin/agent-observatory status
curl -s 127.0.0.1:8125/api/health
```

只改前端时也要 `npm run build` 后再 `restart api`，因为静态资源由 API 同源托管。
只改 worker 逻辑时 `restart worker` 即可，避免无谓中断 API。

回滚：`git checkout <上一版本>` → `alembic downgrade -1`（若本次含迁移）→
重新 `npm run build` → `restart api worker`。

## 7. 变更前后自检

```bash
cd backend
TEST_DATABASE_URL='postgresql+asyncpg://observatory:<local-password>@127.0.0.1:5432/agent_observatory_ci' \
  .venv/bin/python -m pytest tests/ -q
.venv/bin/python -m alembic check
.venv/bin/python -m py_compile $(find agent_observatory -name '*.py')

cd ../frontend
PATH=/opt/node22/bin:$PATH npm run check     # tsc + oxlint + oxfmt + vitest
```

## 8. 排障

| 现象 | 排查 |
| --- | --- |
| `up` 报端口被占用 | `doctor` 会检查端口；先确认 8125 上是不是旧的 API 进程，走 `down`/`restart` |
| API 起来了但前端 404 | `frontend/dist` 缺失或过期，执行 `npm run build` 后 `restart api` |
| worker 心跳过期（`health/ready` 不 ok） | 看 `logs worker`；确认扫描间隔与数据根可达 |
| 组件 `crash-loop` | 120s 内崩溃 5 次触发；修因后 `restart <component>`，或 `down` 再 `up` |
| `alembic check` 有漂移 | 不要手改库结构；补一条迁移使其与模型一致 |
| 数据库连接失败 | 校验 `AGENT_OBS_DATABASE_URL`、PostgreSQL 是否运行、数据库角色权限 |
| 索引数据陈旧 | `indexer --once` 观察单轮结果，再确认 worker 在跑 |

## 9. 绝对不能提交或外泄

`.env`、token、密码、数据库 dump、`runtime/` 产物、`$AGENT_OBS_HOME` 日志、
终端或浏览器截图、`~/.codex` 下的原始会话内容。仓库 `.gitignore` 已覆盖
`backend/.venv/`、`node_modules/`、`dist/`、`runtime/`、`*.sqlite*`、`*.log`。

发布前用仓库外提供的敏感词清单做一次全仓检查（词表本身不要提交）：

```bash
SENSITIVE_TERMS_FILE=/secure/path/release-terms.txt \
  python3 scripts/check_sensitive.py
```

检查只报告文件、行号和词条序号，不回显敏感词内容；未配置词表时直接失败，
避免 CI 因漏配而静默放行。

## 10. 对外暴露（可选）

默认只绑定 `127.0.0.1`。若需局域网访问，优先保持本机绑定并加反向代理，
而不是把 `AGENT_OBS_HOST` 改成 `0.0.0.0`：

```nginx
location / {
    proxy_pass http://127.0.0.1:8125;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 300s;   # 导出/大页查询较慢
}
```

应用自带 session cookie 与 API token 鉴权（admin/operator/viewer），
对外暴露时务必使用 HTTPS 并轮换 `.env` 中的两个 secret。
