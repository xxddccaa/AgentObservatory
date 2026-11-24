# Agent Observatory 项目说明

本文件是 AI 代理进入本仓库后的第一份说明。

Agent Observatory 是一个**本地优先、只读、可审计、单人使用（无登录）**的多 agent 会话观测台。

它统一索引本机各 coding agent 的历史会话：

1. **多源会话历史可视化** —— 扫描 Codex、StepCode、Claude、DSH、OpenCode 等本地
   数据，把事件结构化成消息 / turn / 工具调用 / Token，并保留完整原始事件。
2. **自主发现与统一模型** —— `agent` 与 `source_kind` 解耦，通过适配器注册表发现、
   规划、增量索引和断点续扫。
3. **审计与访问控制** —— PostgreSQL 作为唯一持久化事实来源；所有用户与系统动作写审计
   日志（操作者记为 `local` / `system`）。没有账号、登录、角色或 API token：访问控制靠
   监听地址（默认 `127.0.0.1`）加 Host 白名单（防 DNS rebinding）和写请求同源检查（防 CSRF）。

本项目不提供终端交互、任务编排、watchdog 或自动重试。

## 部署约定：单人使用、共享源码、多台机器

本项目的实际使用者只有一人，有多台机器共享同一份仓库目录。
**共享目录只保存干净的源码；每台机器拥有独立的配置、运行副本和数据库，不共享运行时。**

- 本机部署根为 `/root/.agent-observatory/`；不得把数据库、依赖、构建产物、缓存、日志、
  导出、截图或备份放进共享仓库，即使这些文件已经被 `.gitignore` 忽略。
- **没有登录**：2026-11-24 起删除了账号 / 密码 / 角色 / API token（迁移 `0016_drop_accounts`
  删表），部署不再需要管理员密码，也不再有 `credentials.env` 和 `passwd`。不要重新引入登录。
- `config.env` 是本机配置（权限 `600`），含随机数据库密码和签名密钥
  `AGENT_OBS_SIGNING_SECRET`（旧机器上的 `AGENT_OBS_API_TOKEN_SECRET` 同样有效），
  **不要写入共享仓库或文档**；不要复制其他机器的 `config.env`、数据库、运行目录或 agent 原始数据。
- `releases/<版本>/` 是包含当前工作区改动的本机源码快照，依赖与前端构建也只在快照内产生；
  `current` 指向当前运行版本。服务不得通过 editable install 或 `PYTHONPATH` 导入共享源码。
- 独立 PostgreSQL 16 的数据位于 `/root/.agent-observatory/postgresql/data/`，默认仅监听
  `127.0.0.1:55433`。数据库为 `agent_observatory`，自动化测试只用 `agent_observatory_ci`，
  角色为 `observatory`，随机数据库密码只存本机配置。
  数据库密码和签名密钥每台机器独立。
- 不要搬动、停止或修改机器上服务其他项目的 PostgreSQL（通常在 `5432`）。首次迁移旧实例时，
  先通过原数据库配置调用 CLI `down`，再备份、恢复到独立库，核对数据量后切换；保留旧库和备份。
- PostgreSQL 必须由 `postgres` 用户运行；部署脚本只用 ACL 为它授予 `/root` 及应用目录的
  必要遍历权限，不要把 `/root` 改成所有用户可读，也不要用 root 启动 PostgreSQL。
- `./manage.sh deploy` 部署当前共享源码；`./manage.sh up` 启动已部署的本机快照（包括本机数据库）；
  `status / down / restart / logs / doctor` 操作本机实例。
- API 默认仅监听 `127.0.0.1:8125`，只接受 `127.0.0.1` / `localhost` / `::1` 的 Host，写请求必须同源。
  用户明确要求时可监听 `0.0.0.0`（首次部署 `AGENT_OBS_HOST=0.0.0.0 ./manage.sh deploy`，或改本机
  `config.env` 后 `./manage.sh restart`）；此时同一网络内任何人都能读取全部会话，不要擅自开启或暴露公网。
  经反向代理 / 自定义域名访问时用 `AGENT_OBS_ALLOWED_HOSTS` 追加 Host。
- 新接入的 agent 原始目录始终只读；本机默认 `AGENT_OBS_RETENTION_ENABLED=false`。
- 部署失败保留原运行版本与备份；不要自动降级数据库或清空历史，也不要创建 commit 或强制推送。
- **macOS 工作站（无 root）**：`deploy.py` 自动切换到 user 档案，部署根为
  `~/.agent-observatory/`，独立 PostgreSQL 使用 Homebrew 的 16 或更新版本、以当前用户运行，
  仍只监听 `127.0.0.1:55433`；不需要 `runuser` / `setfacl`。其余约定（快照、备份、只读 agent 目录）不变。
- 详细操作和验证见 `docs/deployment.md`。

### 低资源部署（小内存机器同样适用）

**原则：降耗不能影响使用体验。** 凡是会让页面变慢、数据出现得更晚或降低故障隔离的手段都不采用，
包括把 indexer 并入 API 进程和放慢轮询（2026-11-17 试过并已撤回）。默认配置即可直接部署到小机器。

- **进程模型不变**：supervisor + api + worker 三个进程，worker 独立崩溃、独立重启，
  首次大批量导入时也不会拖慢页面。
- **轮询不变**：`AGENT_OBS_INDEXER_POLL_INTERVAL=2`、`AGENT_OBS_LOCAL_SOURCE_POLL_INTERVAL=5`（秒）。
- **增量扫描**（实现见 `workers/indexer/tree_cache.py`，细节见 `docs/architecture.md`）：每 2 秒仍 stat 全部已知
  会话文件，追加内容（包括恢复很久以前的会话）照常在一个轮询周期内入库；新会话靠目录修改时间发现，
  只检查有会话的目录、它们的上级目录和最近 10 分钟改动过的目录；所有目录至少每 10 秒校验一次，
  每 10 分钟完整重列一次；每 60 秒一次完整周期，重新核对所有 checkpoint 并执行维护。没有变化的
  来源不查数据库、不打开 SQLite、不重复 resolve 路径；空闲周期不写心跳（心跳任务仍每 10 秒写一次）。
  修改扫描逻辑时不得退化为每轮 `rglob` 全量遍历，也不得让追加内容的发现晚于一个轮询周期。
- **跳过未变化的本机 SQLite 源**：指纹只看主库、`-wal` 和库头 change counter，不看读者每次都会
  改写的 `-shm`。没有变化就不打开库，既省 CPU，也不再改写 agent 目录下的 shm 文件；
  任何写入都落在主库或 WAL 上，下一轮轮询照常发现；每小时还有一次兜底重读。
- **内存回收**：supervisor 给 api / worker 设 `MALLOC_ARENA_MAX=2`；worker 只在真正入库后执行
  `gc.collect()` + `malloc_trim(0)`，不在 API 进程里做。
- **PostgreSQL**：`deploy` / `up` 每次用 `ALTER SYSTEM` 设置 `shared_buffers=32MB`、
  `max_connections=100`（PostgreSQL 默认值；降到 40 只省约 3 MB，却会压缩连接余量，所以不降）、
  `jit=off`、`maintenance_work_mem=32MB`、`default_toast_compression=lz4`。用 `AGENT_OBS_PG_<NAME>`
  覆盖，例如 `AGENT_OBS_PG_SHARED_BUFFERS=128MB`；值只允许字母、数字、`_` 和 `.`。
  `shared_buffers` / `max_connections` 需要重启 PostgreSQL：`deploy` 在经 CLI 停掉旧应用、备份之后重启；
  `up` 只在应用未运行时重启；`--prepare-only` 只 reload，绝不在应用运行时重启。
  lz4 主要省 CPU，实测体积只比 pglz 小约 3%，且只作用于新写入的数据。
- **运行时依赖**：激活时执行 `uv sync --locked --no-dev`，去掉 pytest / mypy / ruff。
  已激活的快照不能再跑后端测试，需要测试时用 `./manage.sh deploy --prepare-only` 生成新快照。
- **部署峰值高于运行时**：`uv sync`、`npm ci`、`vite build` 都在本机快照内执行；1 GB 内存的机器
  部署时建议临时加 swap，或在部署窗口内关闭其他服务。
- **磁盘**：`releases/` 和 `backups/` 不会自动清理（每次部署都会 `pg_dump` 一份），
  小磁盘机器需人工删除旧快照和旧备份，保留 `current` 指向的版本和最近的备份。
- **参考实测**（2026-11-17，约 310 个会话，数据库 2.9 GB，PSS 口径）：调整前约 515 MB
  （supervisor 52 + api 94 + worker 179 + PostgreSQL 190，worker 已连续运行 7 天）；调整后正常使用
  约 306 MB（supervisor 50 + api 97 + worker 65 + PostgreSQL 99，worker 刚启动，长期值待观察）。
  增量扫描上线后，worker CPU 从约 5.7% 降到约 0.65%（空闲快周期约 6 ms CPU，原来每轮约 110 ms），
  PostgreSQL 约 0.07%；会话追加内容 0.3–1.8 秒入库，和原来一样在 2 秒轮询内。
- **已知的内存峰值**：打开超大会话的 Timeline（1.2 万条事件、41.7 MB 响应）时，
  `/api/codex/sessions/{id}/events` 一次性序列化全部事件，耗时约 2.8 秒，api 峰值约 300 MB；
  这是现有接口行为，与上述配置无关。

网络：访问 GitHub 使用本机 `127.0.0.1:10826` 代理，访问公司 GitLab 必须直连。
需要代理下载 npm 依赖时，在本机 `config.env` 设置 `AGENT_OBS_DEPLOY_PROXY`，
不要修改共享 `.npmrc` 或把代理凭据写入仓库。

## 必须遵守的红线

1. **不要修改** `codex-trace`、`wmux`、`resilient-codex-tasks-dev`。它们只是参考，
   只读。绝对不允许往这些仓库回写任何改动。
2. **源码只修改当前仓库**，不要修改相邻或参考仓库。用户要求部署时，允许在上述 `/root` 本机部署根
   写配置、运行副本、数据库、依赖和产物；不得回写 agent 原始数据目录。
3. **不要创建额外 commit**，除非用户明确要求。
4. **不要提交** token、密码、数据库 dump、任务输出、运行日志、终端截图、浏览器截图。
5. **不要修改或删除**各 agent 原始数据目录下的文件，包括 `~/.codex`、
   `~/.stepcode`、`~/.step-cli`、`~/.claude`、`~/.dsh`、
   `~/.local/share/opencode`、`~/.config/opencode`、`~/.pi`、`~/.xdcode`、
   `~/.stepgo`、`~/.gemini/antigravity-cli`（agy）和 `~/.gemini`（gemini）。应用只做读取、解析、索引、监听、展示、导出副本；
   Storage 的显式清理流程除外。
6. **PostgreSQL 是唯一持久化事实来源**，保存历史、审计、系统命令和索引断点。
7. **所有用户和系统控制动作都必须写 `audit_logs`**。
8. **所有系统控制动作先写 command 表，再由 Supervisor 应用**。
9. **session parser 必须保留未知事件的 `raw_json`**。
10. **大型 JSONL / SQLite 必须增量、有界读取**，不能一次性全部加载到内存。
11. **所有长消息必须支持复制完整原文**。
12. **每个阶段必须先读现有实现和测试，再编辑**。
13. **每个阶段完成后必须运行针对性测试**；测试失败先修复当前阶段，不要继续扩展。
14. **真实运行行为要做 API / 进程级验证**，不能只依据静态推断。
15. **最终回复必须列出**：修改文件、测试命令、测试结果、未完成事项。

## 工程结构

```text
.
├── backend/           # FastAPI + Worker（Python 3.12）
│   ├── agent_observatory/       # Python 包
│   │   ├── main.py              # FastAPI app
│   │   ├── config.py
│   │   ├── api/                 # 路由
│   │   ├── db/                  # SQLAlchemy 模型与数据库会话
│   │   ├── services/            # auth/rbac/...
│   │   └── workers/             # indexer
│   ├── alembic/                 # 数据库迁移
│   ├── tests/                   # pytest
│   └── pyproject.toml
├── frontend/          # Vue 3 + TS + Vite
│   ├── src/           # 应用源码；单元测试在 src/__tests__/
│   └── tests/         # Playwright e2e
├── docs/              # 架构与设计说明
└── manage.sh          # 本机状态 / 启停 / 日志入口
```

## Markdown 渲染约定

会话详情中的用户消息、助手回复和 reasoning summary 使用以下安全渲染链：

```text
Markdown 原文
  -> markdown-it / remark
  -> KaTeX 公式
  -> Shiki 代码高亮
  -> DOMPurify 净化
  -> 局部主题 CSS
```

- 当前实现位于 `frontend/src/markdown/render.ts`、`frontend/src/markdown/session.ts`
  和 `frontend/src/markdown/history.module.css`；页面接入见
  `frontend/src/views/HistoryDetail.vue`。
- 保留 `html: false`、KaTeX `trust: false` 和最终 DOMPurify 净化；不得为了颜色
  放宽标签或属性权限、执行内联 HTML 或引入 CDN 运行时。
- Shiki 异步初始化，并按语言和源码缓存；未知语言及超大代码块降级为完整转义文本。
  切换会话或离页时必须清理缓存，并忽略迟到的异步结果。
- 主题必须通过 CSS Modules 或局部容器隔离；不得全局导入 GitHub Markdown 样式，
  也不得让会话主题作用于 Memories 等无关页面。
- 新增类似的 Markdown 阅读场景时沿用该管线；系统/开发者消息、工具输出、Diff 和
  原始 JSON 默认保持纯文本。

## 开发环境

- Python 3.12（用 `uv` 管理 venv，`uv sync` / `uv run`）
- Node 22（通过 `PATH` 使用，不假设机器安装路径）
- PostgreSQL 16（本项目独立实例与其他项目的数据库隔离）
- tmux 3.2
- codex CLI 0.149

本机 PostgreSQL / 测试数据库约定：

```text
run PostgreSQL：agent_observatory      role：observatory    密码：读取本机 config.env
ci  PostgreSQL：agent_observatory_ci   role：observatory    密码：读取本机 config.env
```

测试自动化**只准**连 `agent_observatory_ci`。`backend/tests/conftest.py`
会拒绝其他库，避免误伤测试/体验环境。

## 健康检查

```bash
./manage.sh up
curl 127.0.0.1:8125/api/health
```

应返回：

```json
{"status":"ok","postgresql":true,"workers":{"indexer":true},"version":"0.1.0"}
```

## 测试约定

先用 `./manage.sh deploy --prepare-only` 生成待验证的本机副本；在该副本内运行测试，
不要在共享源码树里创建 `.venv`、`node_modules`、`dist` 或测试缓存。测试连接串从本机配置
派生，将库名替换为 `agent_observatory_ci`，不要把连接串或密码写入源码。
同一台机器的后端、E2E 和进程验证必须串行执行，不能让多个测试进程同时清空同一 CI 库。
History 验证必须真实点击分组标题、目录路径和会话进入详情，检查消息、工具输出、原始事件、
完整复制及返回列表后的展开状态；只检查页面标题、直接访问详情 URL 或 API 成功不算通过。
部署后还须在 `http://localhost:8125/history` 重走上述用户路径，验证实际运行版本。

后端（在本机副本的 `backend/`）：

```bash
uv run pytest tests/ -q
uv run ruff check agent_observatory tests
uv run alembic check
uv run python -m py_compile $(find agent_observatory -name '*.py')
```

前端（本机副本的 `frontend/`，Node 22）：

```bash
LC_ALL=C.UTF-8 TZ=UTC npm run check      # vue-tsc + vitest，固定测试区域与时区
E2E_DATABASE_URL="$TEST_DATABASE_URL" npx playwright test
```

Playwright 版本与本机缓存的浏览器不一致时，用 `PW_CHROMIUM_PATH` 指向
`~/.cache/ms-playwright/` 下已有的 `chrome-headless-shell`，不要在共享仓库里安装浏览器。
测试只需导出 `TEST_DATABASE_URL`；不要把整份 `config.env` 导入环境，否则
`AGENT_OBS_PORT` 等变量会覆盖测试自带的配置文件。

Git 校验在共享源码仓库根目录单独执行 `git diff --check`；本机快照不包含 `.git`。

## 每个阶段的固定交付模板

每个 Phase 完成后，回复必须包含：

- **修改文件列表**
- **实现摘要**
- **测试命令**
- **测试结果**
- **剩余风险 / 未完成事项**

每个阶段开始前，先读本仓库已有的实现与测试，再编辑。
