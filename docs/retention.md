# Retention: cleaning up local agent data

The Storage page exists to reclaim disk space from old agent sessions without
giving up the audit trail. It is the only place in Agent Observatory that
deletes anything.

## Data flow

```
browser (/storage)
  └─ POST /api/retention/preview      # read-only: inventory + sealed plan token
       └─ POST /api/retention/runs    # admin only: audit_logs + system_commands row
            └─ Supervisor             # claims the command, applies the plan
                 ├─ deletes files older than the retention floor
                 ├─ purges matching agent_sessions / local_data_sources rows
                 └─ retention_runs row + audit_logs entry
```

Nothing is deleted inline in a request handler. Request handlers only plan and
enqueue; the Supervisor (the local process owner) performs the deletion, which
keeps red lines #7 (audit every control action) and #8 (command table first)
intact.

## Page behaviour (self-refreshing numbers)

The Storage page never asks the operator to reload it by hand:

* While a run is queued or executing the page polls the run every 2 seconds and
  re-scans the sizes every ~16 seconds, so the numbers visibly shrink during a
  long purge.
* The moment the run reaches a terminal state the page re-scans the sizes and
  the run list and reports the freed bytes.
* Switching away and back re-reads the overview (background tabs throttle
  timers, so the poll is re-aligned on focus).
* The size scan is cached server-side for `AGENT_OBS_RETENTION_OVERVIEW_TTL`,
  but the cache is keyed on the `retention_runs` generation: as soon as a run is
  queued or finishes, the next request re-scans. A plain browser reload right
  after a purge therefore shows the post-purge numbers.
* Every `/api/*` response carries `Cache-Control: no-store`, so a browser can
  never replay a stale size payload.
* The page header shows the scan time (`数据时间`) and marks cached payloads,
  which makes "is this number current?" answerable at a glance.

## Safety invariants

* The browser sends a *selection* only (agents, categories, entry ids, keep
  days). Every path is re-derived server-side from the catalog in
  `backend/agent_observatory/services/retention.py`, so a request can never
  point at an arbitrary file.
* `AGENT_OBS_RETENTION_MIN_HOURS` (default 24) is a hard floor. Whatever
  `keep_days` the operator picks, nothing modified inside that window is
  planned or deleted.
* A path must resolve inside its catalog root; symlinks are skipped and never
  followed.
* Roots listed in `AGENT_OBS_RETENTION_PROTECTED_ROOTS` are skipped and reported
  as `protected` in the preview.
* Directories with a single file inside the retention window are skipped
  whole (`in_use`), so an active session directory is never partially removed.
* Execution re-plans from scratch and refuses when the live plan is larger
  than what was previewed (`AGENT_OBS_RETENTION_PLAN_DRIFT_BYTES`, default
  64 MiB of slack).
* `AGENT_OBS_RETENTION_ENABLED=false` keeps the overview and preview working
  while refusing every run.

## Catalog

| Entry | Root | Default | Risk |
| --- | --- | --- | --- |
| `codex.sessions` | `$AGENT_OBS_CODEX_HOME/sessions` | selected | history |
| `codex.logs` | `$AGENT_OBS_CODEX_HOME` (`logs_2.sqlite*`) | selected | regenerable |
| `codex.state` | `$AGENT_OBS_CODEX_HOME` (thread history, state, goals, `history.jsonl`) | off | runtime |
| `stepcode.sessions` | `~/.stepcode/sessions` | selected | history |
| `stepcode.codex_logs` | `~/.stepcode/codex` (`logs_2.sqlite*`) | selected | regenerable |
| `stepcode.logs` | `~/.stepcode/logs` | selected | regenerable |
| `stepcode.snapshots` | `~/.stepcode/ux-plan/snapshots` | off | runtime |
| `stepcode.versions` | `~/.stepcode/versions` | off | runtime |
| `stepcli.sessions` | `$AGENT_OBS_STEPCLI_HOME/sessions` | selected | history |
| `stepcli.logs`, `stepcli.telemetry` | `~/.step-cli/...` | selected | regenerable |
| `claude.sessions` | `$AGENT_OBS_CLAUDE_PROJECTS_ROOT` | selected | history |
| `claude.file_history`, `claude.paste_cache` | `~/.claude/...` | selected | regenerable |
| `dsh.sessions` | `~/.dsh/sessions` | selected | history |
| `opencode.data` | `~/.local/share/opencode` | selected | history |
| `observatory.logs` | `$AGENT_OBS_HOME/logs` | selected | regenerable |

`runtime` entries stay unselected in the UI: deleting them makes the owning CLI
forget state or lose rollback versions.

## Index handling

Deleting files leaves `agent_sessions`, `agent_events`, `messages`,
`tool_calls`, `index_checkpoints` and `local_data_sources` rows behind. The run
matches those rows by path (with the `claude:` / `stepcli:` prefixes stripped)
and deletes them in batches; child rows follow through `ON DELETE CASCADE`.
Rows newer than the retention floor are never purged, and `scope=index` purges
rows without touching files - useful when only the PostgreSQL footprint matters.
Because the indexer is a watcher, a later index-only purge of a file that still
exists will simply re-index it.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENT_OBS_RETENTION_ENABLED` | `true` | Master switch for runs. |
| `AGENT_OBS_RETENTION_MIN_HOURS` | `24` | Hard floor; nothing newer can be deleted. |
| `AGENT_OBS_RETENTION_PROTECTED_ROOTS` | empty | CSV of absolute roots that stay read-only. |
| `AGENT_OBS_RETENTION_MAX_ENTRIES` | `250000` | Bounds the planning walk. |
| `AGENT_OBS_RETENTION_LARGE_RUN_BYTES` | `5 GiB` | Above this size the UI requires an extra acknowledgement. |
| `AGENT_OBS_RETENTION_PLAN_DRIFT_BYTES` | `64 MiB` | Slack before execution refuses a grown plan. |
| `AGENT_OBS_RETENTION_PLAN_TTL` | `1800` | Lifetime of the signed plan token. |
| `AGENT_OBS_RETENTION_OVERVIEW_TTL` | `60` | Cache for the size overview; dropped whenever a run changes state. |

## Relationship to the read-only rule

`AGENTS.md` describes the `.codex` tree as read-only for the application. The
Storage feature is the one deliberate, explicitly approved exception: an
administrator selects old data on a web page and the local Supervisor deletes
it. Everything else stays read-only. To restore a strict read-only posture for
a specific tree, list it in `AGENT_OBS_RETENTION_PROTECTED_ROOTS` (for example
`~/.codex/sessions`), or set `AGENT_OBS_RETENTION_ENABLED=false`.

## Verifying a change

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://observatory:<local-password>@127.0.0.1:5432/agent_observatory_ci \
  .venv/bin/python -m pytest tests/test_retention_service.py tests/test_retention_api.py \
  tests/test_retention_supervisor.py -q
```

The service tests build the whole tree under `tmp_path`; they never plan
against the real agent roots.
