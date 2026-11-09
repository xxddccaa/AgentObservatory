# AgentObservatory 实施计划书

> 仓库根目录：当前 checkout（部署与测试命令均在仓库内执行）
> 状态：已实现（P0–P4 代码、测试与运行验证完成；持续维护中）
> 最后更新：2026-09-02

## 文档目的

把"统一、只读、多 agent 的会话观测台"从想法落到可执行工程。读者对象是接手本项目的
工程师或 AI 代理：读完应能理解目标、架构、关键取舍、数据源细节、分阶段任务与验收标准，
并可据此继续开发。

## 目录

1. 背景与目标
2. 范围（做 / 不做）
3. 架构总览
4. 关键设计决策
5. 数据源与适配器矩阵
6. 数据模型与迁移
7. 分阶段实施计划（P0–P4 + 收尾）
8. 测试与验证策略
9. 部署与运维
10. 风险登记册
11. 当前状态与下一步
12. 附录 A：各 agent 数据格式速查
13. 附录 B：SourceAdapter 接口草案

---

## 1. 背景与目标

### 1.1 背景

本机已有两个功能高度重叠的观测项目：

- **CodexObservatory**（端口 8123）：Codex 工作台 = tmux Web 终端 + watchdog + Codex
  Session Viewer + Local Insights。已实现两种只读数据源适配器（`codex_jsonl`、
  `stepcode_sqlite`）与完整 Insights。
- **ClaudeObservatory**（端口 8124）：只读的 Claude Code 会话观测台，扫描
  `~/.claude/projects/**/*.jsonl`。它本质是 CodexObservatory 去掉终端后的分身。

两者共享同一套内核（FastAPI + Worker / Vue3+Vite / PostgreSQL 事实源 /
RBAC + 审计 / Session Viewer + Insights），差异只在"数据源"和"是否带终端控制"。
按 agent 各建一个目录会导致内核重复维护、版本漂移、运维面翻倍。

### 1.2 目标

建设单一的 **AgentObservatory**：一个**本地优先、只读、可审计**的**多 agent 会话
观测台**，统一观测本机各 coding agent 的历史会话，支持**数据源自主发现**，从而
不再"一个 agent 一个目录"。

观测对象（agent 维度）：`codex`、`stepcode`、`claude`、`opencode`、`dsh`。

### 1.3 落地策略（已决策：new_unified）

以 CodexObservatory（功能超集）为基底泛化成独立新项目 AgentObservatory：复制内核 →
砍掉终端 → 引入适配器注册表 + agent 维度 + 自主发现 → 逐个接入 agent → 达到 parity
后冻结 / 退役两个旧项目。已排除的另两方案：原地改造 CodexObservatory、或在两个旧项目
上做聚合层。

## 2. 范围（做 / 不做）

### 2.1 做（In scope）

- 只读发现、解析、增量索引本机各 agent 的会话数据，投影成统一的
  会话 / 事件 / 轮次 / 消息 / 工具调用 / Token 关系模型。
- **Session Viewer**：按 agent、workspace（cwd）分组浏览对话、思考/推理、工具调用与
  结果、Token、原始事件时间线；会话内搜索；整会话 Text/Markdown/JSON 导出。
- **Insights**：活动与 Token 时序、Turn P50/P95、工具 P95 与失败率、质量信号、Skill
  可见性、父子会话图、数据源覆盖/健康度；支持按 agent 过滤与跨 agent 对比。
- **自主发现**：内置各 agent 已知 home 布局，启动即探测存在的数据源并纳管；支持配置
  额外扫描根；对未知目录用 generic 兜底（只存 raw_json，不丢数据）。
- RBAC（admin/operator/viewer）、审计日志、健康检查、CLI 运维（init/up/down/dev/doctor）。

### 2.2 不做（Out of scope）

- **不做终端交互 / 任务编排 / watchdog / 自动重试**（这些留在 CodexObservatory 或
  resilient-codex-tasks）。AgentObservatory 只观测，不驱动 agent。
- **不修改、重命名、删除任何 agent 的原始会话文件**；对 `~/.codex`、`~/.claude`、
  `~/.stepcode`、`~/.dsh` 等一律只读。
- 不做跨机聚合、不做实时协作、不做告警系统（可作为后续演进）。

### 2.3 红线（继承自旧项目 AGENTS.md）

1. PostgreSQL 是唯一持久化事实来源，保存历史、审计、系统命令和索引断点。
2. 解析器必须保留未知事件的 `raw_json`，未知事件类型不丢弃。
3. 大型 JSONL / SQLite 必须增量、有界读取，不可一次性载入内存。
4. 所有长消息支持复制完整原文；展示层做 secret redaction，原始事件 hash 保留。
5. 自动化测试只准连 `agent_observatory_ci`。
6. 不提交 token、密码、数据库 dump、运行日志、截图。

---

## 3. 架构总览

### 3.1 复用的内核

沿用两个旧项目验证过的分层：

```text
Vue 3 + Vite (SPA，同源托管 frontend/dist)
    │ HTTP（相对 /api，含 cookie）
FastAPI（api/ 路由：health, auth, admin, audit, history, insights, stats, system）
    │
    ├── PostgreSQL：users/roles、sessions、api_tokens、audit_logs、
    │               agent_sessions/events/turns、messages、tool_calls、
    │               local_data_sources、insights(evidence/activity/skills/goals)

agent-observatory supervisor
    ├── FastAPI (api)
    └── worker（IndexerWorker）
          └── IndexerOrchestrator
                └── 遍历 SourceAdapter 注册表：
                      discover → plan(checkpoint) → parse/ingest → link → checkpoint
```

相较 CodexObservatory：删除 `workers/terminal/`、`managed_sessions`、`codex_failures`、
`outbox_publisher` 及相关 4 张表；worker 只剩 IndexerWorker。

### 3.2 索引数据流（每个源）

```text
discover()  发现具体数据源（文件/SQLite thread），产出 SourceDescriptor
   │            （kind, identity=sha256(realpath[,thread]), path, format, agent）
plan()      对比 index_checkpoints 指纹/游标 → UNCHANGED / APPEND / REBUILD
   │
parse/ingest 按 agent 专属解析器把原始事件规整为 NormalizedEvent，
   │            投影到 agent_sessions/events/turns + messages + tool_calls
link()      归并 workspace(cwd)、回填标题、父子会话/thread 关系
   │
checkpoint  写回 index_checkpoints（游标+指纹+parser_version），保证幂等与断点续扫
```

### 3.3 关键模块（后端）

| 模块 | 职责 | 现状 |
| --- | --- | --- |
| `workers/indexer/scanner.py` | Codex rollout JSONL 有界扫描/解析 | 复用 |
| `workers/indexer/stepcode.py` | StepCode SQLite（thread_history）导入原语 | 复用 |
| `workers/indexer/local_sources.py` | Codex 本地 SQLite→Insights（logs/state/goals/skills） | 复用 |
| `workers/indexer/parser.py` | Codex 事件规整 NormalizedEvent | 复用，将抽象化 |
| `workers/indexer/adapters.py` | SourceDescriptor/SourcePlan + 各源 discover/plan | 扩展为注册表 |
| `workers/indexer/ingestion.py` | 事件→关系投影 | 扩展为按 agent 分派 |
| `workers/indexer/orchestrator.py` | 轮询编排、心跳 | 改为遍历注册表 |
| `workers/indexer/linking.py` | workspace / 标题 / 关系归并 | 已去 managed |
| `services/history.py` `api/history.py` | Session Viewer 查询/导出 | 复用，加 agent 过滤 |
| `services/insights.py` `api/insights.py` | Insights 派生与查询 | 复用，加 agent 维度 |

---

## 4. 关键设计决策

### 4.1 `agent`（产品）与 `source_kind`（存储格式）解耦 —— 核心决策

`agent` = 会话由谁产生（`codex`/`stepcode`/`claude`/`opencode`/`dsh`）；
`source_kind` = 数据以什么格式落盘（`codex_jsonl`/`codex_sqlite`/`claude_jsonl`/
`dsh_jsonl`/`opencode_json`/`generic_jsonl`）。二者正交：**stepcode 同时产出 Codex 家族
和 Claude 家族数据**（见 §5），因此同一个解析器会被不同 agent 复用，同一个 agent 也可
能有多种存储格式。故必须新增独立的 `agent` 列，而非用 `source_kind` 兼作产品维度。

### 4.2 通用中性表命名

现有基底沿用 Codex 命名（`codex_sessions`/`codex_events`/`codex_turns`/`codex_thread_edges`）。
统一版改为中性名，容纳所有 agent：

- `codex_sessions` → `agent_sessions`（新增 `agent` 列，已存在 `source_kind`）
- `codex_events` → `agent_events`
- `codex_turns` → `agent_turns`
- `codex_thread_edges` → `thread_edges`
- 子表 FK `codex_session_id` → `agent_session_id`
- `messages`、`tool_calls`、`local_data_sources`、`index_checkpoints`、`activity_spans`、
  `evidence_records`、`skill_definitions`、`session_skill_evidence`、`thread_goals` 保留表名。

`agent` 以 `agent_sessions` 为权威来源，并**冗余下沉**到聚合/查询热点表
（`agent_turns`、`tool_calls`、`activity_spans`、`local_data_sources`），便于 Insights
按 agent 高效 group-by，避免每次 join 会话表。

### 4.3 自主发现（Autonomous discovery）

- **已知布局表** `AGENT_LAYOUTS`：每个 agent 声明其 home 布局与对应 adapter/agent 标注
  （见 §5）。worker 启动即探测哪些路径存在并纳管，无需为每个 agent 单独配置。
- **额外根** `AGENT_OBS_EXTRA_ROOTS`：允许配置非标准路径（如自定义 CODEX_HOME）。
- **generic 兜底**：对配置的未知根用 `generic_jsonl`，逐行存 `raw_json` + 最小会话骨架，
  agent 标为 `generic`，保证"先不丢数据，后补专属解析器"。
- 采用"探测即注册"而非"智能格式嗅探"：范围可控、可测试、可解释。

### 4.4 共享 rollout 的去重与归属（重要坑）

`~/.stepcode/codex/sessions` 与 `~/.stepcode/codex/history.jsonl` 是指向 `~/.codex` 的
**软链**，即 stepcode-codex 与 codex **共用同一批 rollout 文件**。处理约定：

- `source_identity = sha256(realpath)`：软链解析到同一真实路径 → **只索引一次**，天然去重。
- **归属规则**：`~/.codex/sessions/**` 的 rollout 归 `codex`；stepcode 的**独有**数据是其
  `~/.stepcode/codex/*.sqlite`（thread_history 等，`stepcode_sqlite`，归 `stepcode`）与
  `~/.stepcode/claude/projects/**`（`claude_jsonl`，归 `stepcode`）。即：rollout 层不重复
  归属，SQLite / claude 投影层区分产品。

### 4.5 增量、幂等、断点续扫

沿用现有 `index_checkpoints`（游标 `cursor_json` + 指纹 `fingerprint_json` +
`parser_version` + `status`）与 `SourcePlan{UNCHANGED/APPEND/REBUILD}`：
- 明文 JSONL 用 byte offset 续读；zstd 流不可随机 seek，用行数续读或整段 REBUILD。
- `parser_version` 变更强制 REBUILD；文件变小/被替换触发 REBUILD。

### 4.6 安全与合规

只读挂载各 agent home；展示层 secret redaction，原始事件保留 hash；`.env`、认证配置、
`history.jsonl` 正文、shell snapshot 正文不入索引（沿用 Codex 的排除清单）。

---

## 5. 数据源与适配器矩阵

本机实地探查（2026-08-27）确认的落点与格式：

| agent | 本机路径 | source_kind | 解析器 | 状态 |
| --- | --- | --- | --- | --- |
| codex | `~/.codex/sessions/**/rollout-*.jsonl(.zst)` | `codex_jsonl` | scanner.py/parser.py | 复用（已 parity） |
| codex | `~/.codex/{thread_history_1,logs_2,state_5,goals_1}.sqlite` + `skills/` | `codex_sqlite` | stepcode.py/local_sources.py | 复用 |
| stepcode | `~/.stepcode/codex/*.sqlite`（sessions/history 软链到 ~/.codex） | `codex_sqlite` | 复用，agent=stepcode | P1 |
| stepcli | `~/.step-cli/sessions/*/{session.json,events.jsonl}` | `stepcli_json` | stepcli.py 原生会话适配器 | 已实现（2026-09-03） |
| stepcode | `~/.stepcode/claude/projects/**/*.jsonl` | `claude_jsonl` | claude 解析器，agent=stepcode | P2 |
| claude | `~/.claude/projects/**/*.jsonl` | `claude_jsonl` | 移植自 ClaudeObservatory | P2 |
| dsh | `~/.dsh/sessions/<编码cwd>/session-<uuid>/session.jsonl.zstd` | `dsh_jsonl` | 新写（zstd 事件流） | P3 |
| opencode | 本机暂无（一般 `~/.local/share/opencode/**` JSON store） | `opencode_json` | 新写（待真实样本） | P4 |
| （任意） | 配置的未知根 | `generic_jsonl` | 兜底：逐行 raw_json | P4 |

> 注意：`dsh` 扩展名是 `.jsonl.zstd`（非 Codex 的 `.jsonl.zst`），发现与解压需分别处理。

各 adapter 统一实现 §附录 B 的 `SourceAdapter` 协议；接入新 agent = 新增一个 adapter +
一个解析器 + 在 `AGENT_LAYOUTS` 注册已知布局，内核不改。

---

## 6. 数据模型与迁移

### 6.1 现有表清单（P0 后，共 22 张）

`users`、`roles`、`user_roles`、`sessions`、`api_tokens`、`audit_logs`、`workspaces`、
`system_commands`、`worker_heartbeats`、`codex_sessions`、`index_checkpoints`、
`codex_events`、`codex_turns`、`messages`、`tool_calls`、`local_data_sources`、
`evidence_records`、`activity_spans`、`skill_definitions`、`session_skill_evidence`、
`codex_thread_edges`、`thread_goals`。

（已在 P0 删除 4 张终端表：`managed_sessions`、`session_commands`、`outbox`、
`workspace_locks`。）

### 6.2 会话核心表 `agent_sessions`（由 `codex_sessions` 重命名 + 加列）

已具备的源无关字段（无需改）：`source_kind`、`source_identity`、`source_path`、
`source_format`、`source_compression`、`thread_id`、`cwd`、`workspace_id`、`model`、
`git_branch`、Token 计数、`turn_count`、`is_ongoing`、`is_subagent`、
`parent_session_id`、`index_status`、增量游标字段等。

新增：
```sql
ALTER TABLE agent_sessions ADD COLUMN agent varchar(32) NOT NULL DEFAULT 'codex';
CREATE INDEX ix_agent_sessions_agent ON agent_sessions (agent);
CREATE INDEX ix_agent_sessions_agent_updated ON agent_sessions (agent, updated_at);
```
`agent` 取值域：`codex|stepcode|claude|opencode|dsh|generic`。

### 6.3 `agent` 冗余下沉的表

`agent_turns`、`tool_calls`、`activity_spans`、`local_data_sources` 各加 `agent`
列 + `(agent, <时间/会话>)` 复合索引，供 Insights 按 agent 聚合。写入时由 ingestion
从会话继承。

### 6.4 迁移策略

项目处于孵化期（旧项目已声明"基线压平、无旧库升级路径"）。采用**重生成单一基线**：

1. 待 `agent_observatory_ci` 建好后，对空库跑 `alembic revision --autogenerate` 生成
   反映"中性表名 + agent 列 + 去终端"后的**新基线**，人工核对（`compare_type`/
   `compare_server_default` 已开）。
2. 删除继承自 Codex 的旧迁移（`0001_incubator_baseline`、`0002_local_insights`），
   以新基线替换；`alembic check` 必须零 diff。
3. 不提供从旧库的在线升级；开发/测试库重建后重跑 `init`。

### 6.5 命名重构的落点（代码侧）

重命名 `CodexSession/CodexEvent/CodexTurn/CodexThreadEdge` 及表名、FK
`codex_session_id`，波及：`db/models.py`、`workers/indexer/{scanner,ingestion,linking,
local_sources,orchestrator}.py`、`services/{history,insights,stats}.py`、
`api/{history,insights,stats}.py`、`tests/*`。将以脚本化改名 + 逐文件核对 + ruff/导入
冒烟 + （建库后）pytest 验证。保留 `source_kind='codex_jsonl'` 等取值不变。

---

## 7. 分阶段实施计划

每个阶段给出：目标 / 任务 / 关键文件 / 交付物 / 验收标准 / 测试命令 / 风险。
通用验证命令（后端）在建库前用现成 venv 做静态校验，建库后做 DB 级校验：

```bash
# 静态（无需库）：
../CodexObservatory/backend/.venv/bin/ruff check agent_observatory tests
python3 -m py_compile $(find agent_observatory -name '*.py')
# DB 级（需 agent_observatory_ci）：
TEST_DATABASE_URL='postgresql+asyncpg://observatory:<local-password>@127.0.0.1:5432/agent_observatory_ci' \
  uv run pytest tests/ -q
TEST_DATABASE_URL='postgresql+psycopg://observatory:<local-password>@127.0.0.1:5432/agent_observatory_ci' \
  uv run alembic check
# 前端：
PATH=/opt/node22/bin:$PATH npm run build && npm run check
```

### P0 — 项目骨架 + 适配器注册表 + agent 维度 + 自主发现

**目标**：可运行的统一骨架：独立身份、去终端、内核按注册表遍历、schema 带 agent 维度、
启动自主发现已知数据源。

**任务与状态**：

- [x] 从 CodexObservatory 复制并改名（包 `agent_observatory`、CLI `agent-observatory`、
  env `AGENT_OBS_`、端口 8125、库 `agent_observatory(_ci)`、home
  `~/.agent-observatory`）。
- [x] 删除终端：`workers/terminal/`、`api|services/managed_sessions`、`codex_failures`、
  `codex_state`、`outbox_publisher` 及 4 张终端表；worker 仅 IndexerWorker；trim
  main/cli/supervisor/health/linking/orchestrator/conftest；`pyproject` 去 `ptyprocess`。
- [x] 前端去终端：删 TerminalView/TerminalSocket/SessionWorkspace/NewSessionDialog/
  api sessions 及终端测试；`package.json` 去 xterm；router/AppShell/health 收敛。
- [x] **适配器注册表**：抽象 `SourceAdapter` 协议（§附录 B），把
  `orchestrator.scan_once` 与 `ingestion` 从写死两源改为遍历 `REGISTRY`。
- [x] **agent 维度**：按 §6 重命名核心表、加 `agent` 列与索引、下沉 `agent`；ingestion
  写入 agent。
- [x] **自主发现**：实现 `AGENT_LAYOUTS` 探测 + `AGENT_OBS_EXTRA_ROOTS`；worker 启动
  据此实例化 adapter。
- [x] **新 alembic 基线**（需 CI 库）：autogenerate → 人工核对 → `alembic check` 零 diff。

**关键文件**：`workers/indexer/{adapters,orchestrator,ingestion,registry}.py`、
`db/models.py`、`config.py`、`alembic/versions/*`。

**交付物**：worker 启动即发现 `~/.codex` 数据并按注册表索引；`/api/health/ready` 绿；
`agent_sessions.agent` 正确落值。

**验收标准**：ruff + py_compile 通过；导入冒烟通过；建库后 pytest 全绿、`alembic check`
零 diff；`agent-observatory indexer --once` 能索引现有 Codex 会话且 `agent='codex'`。

**风险**：重命名波及面大 → 用脚本化改名 + 逐文件核对 + 冒烟；注册表重构改变 ingestion
分派 → 保持 codex/stepcode 两源行为不变，用 parity 测试兜底。

### P1 — codex + stepcode 两源达到 Codex 版 parity

**目标**：新项目在 `codex_jsonl` + `codex_sqlite` 两源上，Session Viewer 与 Insights
达到 CodexObservatory 同等能力，且正确标注 agent（`~/.codex`→codex，
`~/.stepcode/codex`→stepcode）。

**任务**：
- 通过注册表实例化 `CodexJsonlSource`（root=`~/.codex/sessions`）与
  `StepCodeSqliteSource`（root=`~/.stepcode/codex`）；agent 标注按 §4.4。
- 校验 realpath 去重：stepcode 软链的 sessions 不重复索引 rollout。
- 迁移并跑通 Codex 的既有测试（indexer、history projection、insights、local_sources、
  stepcode importer）改名后版本。

**关键文件**：`workers/indexer/{registry,adapters,scanner,stepcode,local_sources,
ingestion}.py`、`services/{history,insights}.py`、`tests/test_indexer*.py`、
`tests/test_local_sources.py`、`tests/test_stepcode_importer.py`。

**交付物**：Session Viewer 列出 codex + stepcode 会话；Insights 图表可用；数据源健康度
覆盖两源。

**验收标准**：pytest 全绿；对比 CodexObservatory 同库同数据，会话数/turn 数/Token 汇总
一致（parity 抽样核对）；`alembic check` 零 diff。

**风险**：stepcode 与 codex 共享 rollout 的归属错乱 → 明确"rollout 归 codex、SQLite 归
stepcode"，加去重与归属单测。

### P2 — 接入 claude 适配器（含 stepcode-claude）

**目标**：观测 Claude Code 会话，支持 `~/.claude/projects` 与
`~/.stepcode/claude/projects` 两 root，并正确标注 agent（前者 claude、后者 stepcode）。

**任务**：
- 从 ClaudeObservatory 移植 `claude` 的 scanner/parser/adapter（其
  `adapters.py`/`scanner.py`/`parser.py`），适配统一 `SourceAdapter` 协议与中性表。
- Claude 数据模型要点（移植时保真）：记录顶层 `type` ∈ user/assistant/summary/
  attachment；**轮次隐式**（真人 user 提问到下一次提问为一轮，排除 tool_result 回填）；
  内容块 text/thinking/tool_use；工具按 `tool_use_id` 配对下一条 user 的 tool_result；
  Token 取每条 assistant 的 `message.usage`。
- 在 `AGENT_LAYOUTS` 注册 claude 两 root；同一解析器按 root 决定 agent 标注。

**关键文件**：`workers/indexer/claude_scanner.py`、`claude_parser.py`、
`adapters.py`（新增 `ClaudeJsonlSource`）、`registry.py`、`tests/test_claude_*.py`
（移植）。

**交付物**：三个 agent（codex/stepcode/claude）会话同台可见、可搜索、可导出。

**验收标准**：移植的 claude 测试全绿；抽样对比 ClaudeObservatory 的会话/轮次/Token 一致；
stepcode-claude 会话 agent 标注为 stepcode。

**风险**：Claude 与 Codex 的轮次/工具配对语义差异 → 用 ClaudeObservatory 既有测试作为
黄金样本；解析器完全独立，不与 codex 解析器耦合。

### P3 — 接入 dsh（deepseek-harness）适配器

**目标**：解析 `~/.dsh/sessions/<编码cwd>/session-<uuid>/session.jsonl.zstd` 的
zstd 压缩事件流，纳入统一模型，agent=dsh。

**任务**：
- 发现：递归 `~/.dsh/sessions`，匹配 `session.jsonl.zstd`；`source_identity` 用
  session 目录/uuid + realpath；`compression=zstd`，注意扩展名 `.jsonl.zstd`。
- 解压/增量：复用现有 zstd 流式读取（`zstandard` 依赖已在）；不可 seek，按行续读或
  REBUILD。
- 解析：先摸清 dsh 的 `SessionEventMap` 事件模型（`deepseek-harness` 仓库
  `docs/` 与 `.agents/notes` 有 `SESSION_FORMAT_VERSION`/session-log 机制说明），
  用少量真实会话做**黄金样本**，映射事件→会话/轮次/消息/工具/Token；未知事件保留
  `raw_json`。
- 注册 `AGENT_LAYOUTS['dsh'] = ['~/.dsh/sessions']`。

**关键文件**：`workers/indexer/dsh_scanner.py`、`dsh_parser.py`、`adapters.py`
（新增 `DshJsonlSource`）、`registry.py`、`tests/test_dsh_*.py`（新建，带样本 fixture）。

**交付物**：dsh 会话在 Session Viewer 可见；关键字段（cwd、时间、消息、Token）正确。

**验收标准**：dsh 解析单测（黄金样本）全绿；`agent-observatory indexer --once` 能索引
本机现有 dsh 会话且不报错、raw 事件不丢。

**风险**：dsh 事件 schema 未完全掌握、且声明"无兼容承诺、格式可能变" → 解析器容错：
未知事件仅存 raw_json；`parser_version` 便于整体 REBUILD；样本覆盖多种事件类型。

### P4 — 前端 agent 过滤/对比 + opencode + generic 兜底

**目标**：UI 层把"多 agent"暴露给用户；补齐 opencode 适配器与未知格式兜底。

**任务**：
- 前端：Session Viewer 列表/详情加 `agent` 过滤器与标签；Insights 支持按 agent 过滤与
  **跨 agent 对比**（Token/Turn/工具指标分组）；导航体现 agent 维度。
- opencode 适配器：待本机出现 opencode 数据后，依据真实 JSON store
  （一般 `~/.local/share/opencode/**` 的 session/message/part）编写 `opencode_json`
  解析器；先注册已知布局，缺数据时优雅跳过。
- generic 兜底：`generic_jsonl` adapter，对 `AGENT_OBS_EXTRA_ROOTS` 中未识别的 *.jsonl
  逐行存 `raw_json` + 最小会话骨架，agent=generic。
- 顺带：统一品牌名（当前前端 AppShell/router 仍残留 "Codex Observatory" 文案，见
  §11）。

**关键文件**：`frontend/src/{views,components,api,stores,router}`、
`workers/indexer/{opencode_*,generic_*}.py`、`api/history.py`（agent 过滤参数）。

**交付物**：用户可按 agent 浏览与对比；opencode 有数据即可观测；未知源不丢数据。

**验收标准**：前端 `npm run build && npm run check` 通过；后端新增过滤参数有测试；
opencode/generic 各有解析单测（有样本时）。

**风险**：opencode 格式未在本机验证 → 明确"有真实样本再定契约"，先注册布局占位。

### 收尾 — 验收与退役旧项目

- 各阶段完成后跑：pytest / ruff / alembic check / 前端 build+check（命令见 §7 开头）。
- 与 CodexObservatory、ClaudeObservatory 做 parity 抽样核对（会话/轮次/Token/工具）。
- parity 通过后：冻结两个旧项目（停服、文档标注 deprecated、保留数据），流量切到
  AgentObservatory（8125）。退役顺序、备份与回滚在运维手册补充。

---

## 8. 测试与验证策略

### 8.1 层次

- **静态**：`ruff check`、`py_compile`、导入冒烟（`import agent_observatory.main` 等）。
  无需数据库，建库前的主要门禁。
- **单元**：每个解析器用**黄金样本** fixture（少量真实/构造事件）断言事件→模型映射；
  scanner 增量/续读/REBUILD；plan 指纹判定；redaction。
- **集成**：indexer worker 端到端（临时 sessions 根 → 索引 → 校验会话/turn/token）；
  history projection；insights 派生；API 鉴权与查询。
- **parity**：对同一批真实数据，核对新旧项目的会话数、turn 数、Token 汇总、工具计数一致。
- **前端**：`vue-tsc` 类型检查、`vitest`、`npm run build`。

### 8.2 CI 数据库与护栏

- 自动化测试**只准**连 `agent_observatory_ci`；`tests/conftest.py` 的
  `_is_disposable` 硬校验库名。其他库一律拒绝，保护 dev/体验环境。
- 测试间用 `TRUNCATE ... RESTART IDENTITY CASCADE` 清表（已剔除终端表）。

### 8.3 每阶段门禁

进入下一阶段前，本阶段必须：ruff + py_compile + 导入冒烟通过；相关 pytest 全绿；
`alembic check` 零 diff（涉及 schema 时）；前端改动跑 build+check。任何失败先修复当前
阶段，不扩大范围。

---

## 9. 部署与运维

### 9.1 隔离约定（与两个旧实例并存互不干扰）

| 维度 | Codex | Claude | **Agent（本项目）** |
| --- | --- | --- | --- |
| 包 / CLI | codex_observatory | claude_observatory | `agent_observatory` / `agent-observatory` |
| 端口 | 8123 | 8124 | **8125** |
| env 前缀 | CODEX_OBS_ | CLAUDE_OBS_ | `AGENT_OBS_` |
| PG 库 | codex_observatory | claude_observatory | `agent_observatory`(+`_ci`) |
| 运行时 home | ~/.codex/... | ~/.claude/... | `~/.agent-observatory`（中性） |
| PG 角色/口令 | 按环境配置，不写入仓库 |

### 9.2 CLI 运维（沿用 supervisor 模式）

```bash
cd backend
uv sync
AGENT_OBS_ADMIN_PASSWORD='change-me' uv run agent-observatory init   # 配置/迁移/前端构建/管理员
uv run agent-observatory up            # 后台起 API + Indexer Worker
uv run agent-observatory status | doctor | logs worker --follow
uv run agent-observatory indexer --once   # 手动全量扫描一次
uv run agent-observatory dev            # API + Worker + Vite HMR
uv run agent-observatory down
```

稳定入口 `http://127.0.0.1:8125`（FastAPI 同源托管 `frontend/dist`）。健康：
`/api/health/live`、`/api/health/ready`（PostgreSQL/Indexer）、`/api/system/status`。

### 9.3 数据库初始化（需一次性特权操作）

应用角色通常无 createdb 权限，需由具备权限的角色创建隔离库：

```bash
createdb -O observatory agent_observatory       # dev
createdb -O observatory agent_observatory_ci    # 测试（pytest 唯一允许目标）
```

### 9.4 与旧项目的关系

并存期三套实例独立库/端口/home，互不写扰。AgentObservatory 达 parity 后，冻结
CodexObservatory 的"观测"职能（其终端能力若仍需可另行保留）与 ClaudeObservatory。

---

## 10. 风险登记册

| # | 风险 | 影响 | 缓解 |
| --- | --- | --- | --- |
| R1 | 核心表改名波及面广 | 编译/运行破坏 | 脚本化改名 + 逐文件核对 + ruff/导入冒烟 + pytest |
| R2 | 注册表重构改变 ingestion 分派 | codex/stepcode 行为回归 | 保持两源行为不变 + parity 测试兜底 |
| R3 | stepcode 与 codex 共享 rollout | 重复索引/归属错乱 | realpath 去重 + 明确归属规则 + 去重单测 |
| R4 | dsh 事件 schema 未完全掌握且声明可变 | 解析漏字段/破裂 | 黄金样本 + 未知事件仅存 raw_json + parser_version 整体 REBUILD |
| R5 | opencode 本机无数据 | 无法验证契约 | 先注册布局占位，有真实样本再实现，缺数据优雅跳过 |
| R6 | 大 SQLite（logs_2 ~1.3GB）/大 JSONL | 内存/性能 | 有界增量读取（沿用现有），只回填最近 N 天 |
| R7 | 格式漂移（各 agent 升级） | 解析失效 | parser_version + 未知事件不丢 + 覆盖多版本样本 |
| R8 | 建 CI 库需特权 | 阻塞 DB 级验证 | 由特权角色一次性 createdb；期间用静态门禁推进 |
| R9 | 迁移基线重生成出错 | schema 漂移 | 对空库 autogenerate + 人工核对 + `alembic check` 零 diff |

---

## 11. 当前状态与下一步

### 11.1 已完成（P0–P4）

- 新项目 `AgentObservatory/` 建立，标识/端口/库/env/home 全隔离。
- 后端去终端：删终端模块与 4 张表，worker 仅 IndexerWorker；`ruff` 全绿、`py_compile`
  通过、导入冒烟通过（app=agent-observatory、22 表、managed_sessions 已消失）。
- 前端去终端：由子代理完成并**通过 `npm run build`**（views/components 收敛，
  `package.json` 去 xterm，src 无 terminal/xterm/managed 残留）。
- `pyproject` 去 `ptyprocess`；`conftest` 保留 CI PostgreSQL 硬护栏。

### 11.2 P0 收口记录

- [x] **适配器注册表**：`orchestrator` 改为遍历 `self.sources` 注册表（§附录 B）。
- [x] **agent 维度**：`agent_sessions.agent` 列 + 索引已加，codex/stepcode 两源经 ingestion
  正确落 `agent`（实测 codex 652 源、stepcode 43 线程标注正确）。
- [x] **自主发现**：`workers/indexer/discovery.py`（`AGENT_LAYOUTS` + `probe/detected/
  summary`）+ `AGENT_OBS_EXTRA_ROOTS` 配置，接入 worker 心跳；实测检测到
  codex/stepcode/claude/dsh。
- [x] 中性表重命名：`agent_sessions` / `agent_events` / `agent_turns` /
  `thread_edges`，所有子表 FK 使用 `agent_session_id` / `agent_event_id`。
- [x] `agent` 冗余下沉到 `agent_turns` / `tool_calls` / `activity_spans` /
  `local_data_sources`，并增加 agent 查询索引。
- [x] 新 Alembic 单一基线：CI 库重建后 `upgrade head` 与 `alembic check` 零 diff。
- [x] DB 级门禁：后端 `96 passed`；ruff / py_compile 通过；前端 Vitest `38 passed`、
  Playwright `12 passed`。
- [x] 品牌与只读定位清理：README、AGENTS、env 示例和前端品牌改为 Agent
  Observatory，删除遗留 terminal/watchdog 配置。

### 11.3 当前无阻塞项

- `agent_observatory` 与 `agent_observatory_ci` 已存在，CI 库已用新单一基线重建并验证。
- P1 parity：Codex rollout 与 StepCode SQLite 均通过统一注册表索引，软链
  realpath 去重，Session Viewer/Insights 支持同库聚合。
- P2 Claude：`~/.claude/projects` 与 `~/.stepcode/claude/projects` 共用 Claude
  解析器，隐式轮次、thinking、tool_use/tool_result 和 usage 均投影。
- P3 DSH：`.jsonl.zstd` 流式读取、未知事件 raw_json 保留、turn/tool/token/activity
  投影与 rebuild 语义已实现。
- P4 UI/适配器：History/Insights agent 过滤与跨 agent 对比已完成；OpenCode
  session/message/part 常见布局和 generic JSONL 兜底已注册，无数据时跳过。

### 11.4 下一步

- 持续用真实 agent 新版本样本扩充 parser fixtures；格式漂移通过
  `parser_version` 触发 rebuild。
- 在部署环境运行一次 `agent-observatory indexer --once`，抽样核对各 agent 的会话、
  turn、token、工具和来源健康度指标。

### 11.5 项目统计收尾评估（2026-08-31）

项目级 Token 统计首版已落地到 `/projects`，本次收尾对交付后遗留项逐一评估：

| 项目 | 评估结论 | 本次处理 |
| --- | --- | --- |
| Projects 列表浏览器 Back/Forward 同步 | 实际技术缺口，会导致 URL 与筛选控件/数据不一致 | 已增加 route query watcher，并补充 Vitest 覆盖 |
| Project 详情趋势筛选历史同步 | 同类可用性缺口，趋势范围应可分享、可回退 | 已将趋势范围写入 `trend` query，并补充 Back/Forward 测试 |
| 项目重命名/别名 | 与“精确规范化 cwd 即项目身份”的首版决策冲突；会改变审计和索引语义 | 本轮不实现；如需支持，应新增独立展示元数据并保留 cwd 不变 |
| 多目录合并/手工映射 | 与父子目录隔离、Workspace 复用和只读观测边界冲突 | 本轮不实现；后续应先定义映射生命周期、冲突处理和管理员审计协议 |
| 费用估算 | 不同 agent/model 的价格、缓存计费和版本时效性不稳定，当前 Token 事实模型未承诺价格来源 | 本轮不实现；后续需引入带生效时间的价格表、币种和估算免责声明 |
| PostgreSQL 趋势 rollup/cache | 当前开发库约 10,074 turns、项目聚合查询可接受；引入 rollup 会增加增量修正和失效复杂度 | 本轮保留在线聚合；当单表达到约 1,000 万 turns 或 P95 超过 500ms 时再评估 PostgreSQL 内部 rollup |

以上三类产品能力（别名/映射/费用）仍属于明确的首版 out-of-scope，并非遗漏实现；
如果产品范围发生变化，应先更新 §2 范围和数据模型，再单独立项，避免破坏当前项目身份和审计语义。

---

## 12. 附录 A：各 agent 数据格式速查

- **codex（rollout JSONL）**：`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl(.zst)`；
  首行 `session_meta`（thread_id/cwd/model/git 等），随后事件行；`turn_context` 带具体
  model；已有 `RolloutScanner`/`parse_json_line` 处理，含 zstd 与 unknown 事件 raw_json。
- **codex（本地 SQLite）**：`~/.codex/{thread_history_1,logs_2,state_5,goals_1}.sqlite`
  + `skills/`；`thread_history` 的 `thread_items`/`thread_turns` 承载会话投影，
  `logs_2`(~1.3GB) 需有界读取；`local_sources.py` 已把它们投影到 evidence/activity/
  skills/goals（Insights）。
- **stepcode**：`~/.stepcode/codex`（codex-home 克隆，自带上述 SQLite，其 `sessions` 与
  `history.jsonl` 软链到 `~/.codex`）；`~/.stepcode/claude/projects/**/*.jsonl`（Claude
  格式）。归属见 §4.4。
- **claude（Claude Code JSONL）**：`~/.claude/projects/<编码cwd>/<session-id>.jsonl`；
  每行顶层 `type`(user/assistant/summary/attachment...)；轮次隐式；内容块
  text/thinking/tool_use；工具按 `tool_use_id` 配对 tool_result；Token 在
  `message.usage`。移植 ClaudeObservatory 的解析器。
- **dsh（deepseek-harness）**：`~/.dsh/sessions/<编码cwd>/session-<uuid>/session.jsonl.zstd`；
  zstd 压缩事件流，`SESSION_FORMAT_VERSION`/`SessionEventMap`；扩展名 `.jsonl.zstd`。
  事件模型以真实样本 + 仓库 docs/.agents 说明为准。
- **opencode**：本机暂无；一般 `~/.local/share/opencode/**` 的 JSON store
  （session/message/part）。待真实样本确定契约。

## 13. 附录 B：SourceAdapter 接口草案

在现有 `SourceDescriptor` / `SourcePlan` / `PlanAction` 之上抽象统一协议，并加入 `agent`：

```python
# workers/indexer/adapters.py（草案）
from typing import Protocol, Iterable
from pathlib import Path

@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    kind: str            # codex_jsonl | codex_sqlite | claude_jsonl | dsh_jsonl | ...
    agent: str           # codex | stepcode | claude | opencode | dsh | generic
    identity: str        # sha256(realpath[,thread_id]) —— 跨明文/压缩/软链稳定且去重
    path: Path
    format: str          # jsonl | sqlite | json
    compression: str | None = None
    thread_id: str | None = None

class SourceAdapter(Protocol):
    kind: str
    agent: str
    def discover(self) -> list[SourceDescriptor]: ...
    def plan(self, source: SourceDescriptor,
             checkpoint: "IndexCheckpoint | None") -> SourcePlan: ...
    async def ingest(self, db, plan: SourcePlan) -> "AgentSession": ...
```

```python
# workers/indexer/registry.py（草案）
from agent_observatory.config import settings

# 已知布局：agent -> [(adapter 工厂, 该 root 的 agent 标注)]
AGENT_LAYOUTS = {
    "codex":    ["~/.codex/sessions", "~/.codex"],              # jsonl + sqlite
    "stepcode": ["~/.stepcode/codex", "~/.stepcode/claude/projects"],
    "claude":   ["~/.claude/projects"],
    "dsh":      ["~/.dsh/sessions"],
    # "opencode": ["~/.local/share/opencode"],  # 有数据再启用
}

def build_registry() -> list[SourceAdapter]:
    """探测存在的已知布局 + AGENT_OBS_EXTRA_ROOTS，实例化 adapter；
    不存在的路径跳过；未知额外根落到 generic_jsonl。"""
    ...
```

orchestrator 由"写死 codex_source/stepcode_source"改为：

```python
for adapter in build_registry():
    for source in safe_discover(adapter):
        plan = adapter.plan(source, checkpoint_of(source))
        if plan.action is PlanAction.UNCHANGED:
            continue
        session = await adapter.ingest(db, plan)   # 内部按 agent/kind 分派解析器
        await linker.link_workspace(db, session)
        await checkpoints.ensure(db, session=session, source_kind=source.kind, ...)
```

接入新 agent 的最小改动 = 新增一个实现 `SourceAdapter` 的类 + 一个解析器 + 在
`AGENT_LAYOUTS` 注册 root；`orchestrator`/`checkpoints`/`linking`/schema 均不改。

---

*本计划书随实施推进持续更新；每阶段完成后回填 §11 状态与验收结果。*
