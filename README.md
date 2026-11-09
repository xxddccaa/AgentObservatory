# Agent Observatory

Agent Observatory 是一个本地优先、只读、可审计的多 agent 会话观测台。它统一索引本机
coding agent 的历史会话，提供 Session Viewer、搜索/导出、Token 与工具指标、数据源
健康度和 RBAC 审计，不负责终端控制或任务编排。

当前实施状态与后续阶段见 [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)。

部署、升级、启停与排障见 [`docs/deployment.md`](docs/deployment.md)。

## 当前能力

- Codex rollout `*.jsonl` / `*.jsonl.zst` 的流式、增量、只读索引。
- StepCode `thread_history` SQLite 会话导入。
- `agent_sessions` / `agent_events` / `agent_turns` 中性关系模型。
- `agent` 与 `source_kind` 分离，并下沉到 Turn、Tool、Activity、Local Source 聚合表。
- Pi / XDCode 原生 JSONL 会话与 StepGo host 运行记录；StepGo 托管目录按真实
  agent 归类，原生 transcript 是对话与 Token 的唯一事实来源。
- 自主发现 Codex、StepCode、Claude、DSH、OpenCode、Pi、XDCode、StepGo 已知数据根，并支持
  `AGENT_OBS_EXTRA_ROOTS`；未知根通过 generic JSONL 兜底。
- Session Viewer：分页、搜索、完整复制、Text/Markdown/JSON 导出、原始事件时间线。
- Memories：只读索引 Codex/StepCode memory SQLite 和 Claude project memory，
  支持脱敏浏览、完整复制、Markdown/ZIP 迁移导出。
- Insights：Token、Turn/工具延时、质量信号、Skill 证据、父子会话和来源健康度。
- Storage：按保留窗口预览并清理各 agent 的旧会话数据（保留期下限 24 小时），
  删除请求经 `system_commands` 交由本机 Supervisor 执行，结果写入 `retention_runs` 与审计日志。
- session cookie / API token、admin/operator/viewer RBAC、PostgreSQL 审计日志。
- Claude、StepCode-Claude、DSH、OpenCode 和 generic fallback 适配器均已注册；
  没有数据的布局会被优雅跳过。

## 架构

```text
Vue 3 + Vite
    │ HTTP /api
FastAPI
    └── PostgreSQL：唯一持久化事实来源

agent-observatory supervisor
    ├── FastAPI（同源托管 frontend/dist）
    └── IndexerWorker
        ├── source discovery / adapter registry
        ├── incremental scanner / parser / ingestion
        └── projection / linking / checkpoints
```

应用只读取各 agent 的原始数据，不修改、重命名或删除源文件。

## 本地启动

实际使用场景为单人、多台机器共享源码。要求 Python 3.12、uv、PATH 上的 Node 22、
PostgreSQL 16、postgres 用户和 acl。共享仓库只留源码，所有数据和构建都在本机
`/root/.agent-observatory/`；不共享数据库，不直接从共享工作区运行服务。

```bash
./manage.sh deploy       # 首次从本机 credentials.env / 环境或交互输入取得管理员密码
./manage.sh status
./manage.sh up           # 启动已部署快照与本机独立 PostgreSQL
```

默认入口：`http://127.0.0.1:8125`。

常用命令：

```bash
./manage.sh status
./manage.sh doctor
./manage.sh logs worker --follow
./manage.sh down

# 忘记密码时在本机重置（输入不回显，需输两次；会吊销该用户全部会话与令牌）
./manage.sh passwd admin

# 交互式管理菜单：状态 / 启停 / 重启 / 日志 / 重置密码 / 健康检查
./manage.sh
./manage.sh status          # 也可直通执行：deploy|status|up|down|restart|logs|passwd|doctor
```

健康接口：

```text
GET /api/health/live
GET /api/health/ready
GET /api/system/status
GET /api/system/logs
```

## 测试

自动化测试只能连接 `agent_observatory_ci`，在本机待验证快照中执行，不要在共享仓库安装依赖：

```bash
export AGENT_OBS_ENV_FILE=/root/.agent-observatory/config.env
set -a
source "$AGENT_OBS_ENV_FILE"
set +a
export TEST_DATABASE_URL="${AGENT_OBS_DATABASE_URL%/agent_observatory}/agent_observatory_ci"
cd /root/.agent-observatory/current/backend
uv run pytest tests/ -q

uv run ruff check agent_observatory tests
uv run python -m py_compile $(find agent_observatory tests -name '*.py')

uv run alembic check

cd ../frontend
npm run build
npm run check
E2E_DATABASE_URL="$TEST_DATABASE_URL" npx playwright test
```

## 数据安全

- PostgreSQL 是唯一持久化事实来源，保存历史、审计、系统命令和索引断点。
- 未知事件保留完整 `raw_json`，并保存原始 hash。
- 大型 JSONL / SQLite 必须增量、有界读取。
- 展示与导出执行 secret redaction；不要提交 `.env`、token、密码、dump、日志或截图。
- 本机账号与密码配置使用 `config.env` / `credentials.env`，数据库与日志留在各机器的
  本机部署目录；这些文件和运行数据均不得写入共享源码目录。
- Memories 列表和普通详情默认隐藏敏感值；完整揭示、复制和迁移导出仅限
  operator/admin，并写入不含正文的审计记录。
- Storage 的清理计划由服务端根据内置目录清单推导，浏览器只能提交选择；
  详见 `docs/retention.md`（包含保护目录与停用开关）。
- 详细开发约束见 `AGENTS.md`。

## License

[MIT](LICENSE)
