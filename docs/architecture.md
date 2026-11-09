# Agent Observatory Architecture

Agent Observatory is a local-first, read-only session index. PostgreSQL is the
durable source of truth. Original agent files are never modified.

```text
Browser (Vue 3 + Vite)
        │ same-origin HTTP
FastAPI (auth/RBAC, history, insights, audit, system)
        │
        └── PostgreSQL
              users, roles, audit_logs, agent_sessions
              agent_events, agent_turns, messages, tool_calls
              local_data_sources, evidence/activity/skills/thread tables
              memory_entries

IndexerWorker
  └── IndexerOrchestrator
        └── SourceAdapter registry
              discover → plan(checkpoint) → ingest → link → checkpoint

Read-only local roots
  ~/.codex/sessions                 codex_jsonl (agent=codex)
  ~/.stepcode/codex/*.sqlite       stepcode_sqlite (agent=stepcode)
  ~/.step-cli/sessions/*/          stepcli_json (agent=stepcli)
    session.json + events.jsonl
  ~/.claude/projects/**/*.jsonl    claude_jsonl (agent=claude)
  ~/.stepcode/claude/projects      claude_jsonl (agent=stepcode)
  ~/.dsh/sessions/**/session.jsonl.zstd
                                      dsh_jsonl (agent=dsh)
  ~/.local/share/opencode/**       opencode_json (agent=opencode)
  configured extra roots           generic_jsonl (agent=generic)

MemoryIndexer
  ~/.codex/memories_1.sqlite        codex_memories_sqlite
  ~/.stepcode/codex/memories_1.sqlite
                                      stepcode_memories_sqlite
  ~/.claude/projects/*/memory/*     claude_memory_files
```

## Indexing Contract

Each adapter emits a `SourceDescriptor` with an agent-independent identity,
format, compression and optional thread id. `SourcePlan` selects
`UNCHANGED`, `APPEND` or `REBUILD` from the durable checkpoint. Ingestion keeps
every raw event (including unknown or malformed records), then projects known
messages, turns, tools and activity spans. Checkpoints are committed in the
same transaction as the projection, making retries idempotent.

Plain JSONL resumes by byte offset. SQLite readers use bounded `fetchmany`
batches. Zstandard streams are replayed from the beginning and skip indexed
lines because compressed streams are not randomly seekable. Same-size rewrites,
truncation and parser-version changes trigger a rebuild.

Memory sources are indexed independently from session adapters. SQLite
`stage1_outputs` rows are streamed with bounded `fetchmany` reads. Claude
memory directories accept regular `.md` and `.txt` files only and never follow
symbolic links. Missing source entries remain in PostgreSQL with
`is_present=false` so disappearance is auditable.

## Polling And Projection Efficiency

A polling cycle is split into a cheap planning pass and an ingest pass.
`_collect_sources` discovers every source once and drops duplicate roots (a
symlinked root such as `~/.stepcode/claude/projects` -> `~/.claude/projects`
registers the same adapter class twice, so the pair is deduplicated by adapter
class plus resolved root; rows already indexed under the dropped root are left
untouched). `_plan_sources` then loads all checkpoints with one `IN` query per
400 identities and calls the adapter's stat-only `plan` for each source. A
source whose fingerprint is unchanged costs one dictionary lookup: the cycle no
longer issues a checkpoint query, a session query and a commit per source per
poll.

Ingest only runs for changed sources. The rollout pass and the local-source pass
share one write lock: they update the same `agent_sessions` / `agent_turns` /
evidence rows, and running them fully in parallel made PostgreSQL deadlock on
those rows (one aborted cycle every ~30s on the dev instance) because each loop
locked them in its own order. The lock keeps their cadences independent without
interleaving the writes. Independent loop tasks keep the cadences honest: the rollout scan polls at `AGENT_OBS_INDEXER_POLL_INTERVAL` (default
2s), local insights/memories poll at `AGENT_OBS_LOCAL_SOURCE_POLL_INTERVAL`
(default 5s), and both publish through one heartbeat task at
`AGENT_OBS_WORKER_HEARTBEAT_INTERVAL` (default 10s). Each loop schedules its
next run from the end of the previous one; an unexpected failure in any of the
three tasks is caught, logged, and recorded (the heartbeat flips to `degraded`)
instead of cancelling its siblings, and a local-source failure is reported
through `last_local_error` as a JSON-safe `{kind, message}` summary rather than
the exception object.

Derived state is repaired conditionally instead of being recomputed every
cycle: the maintenance pass only touches sessions with a missing workspace link
or title, plus workspaces whose kind was never written, capped at 200 rows per
cycle. Every predicate is terminal, so unrepairable rows leave the batch: title
candidates must have a non-blank user message *by the same trim rule the
backfill consumes*, a blank cwd is normalized to NULL (and counted, so the
change commits), and `unknown` counts as a final workspace classification (it
is what `classify_workspace` returns for paths it cannot verify, so a workspace
classified `unknown` is only revisited when a session re-index calls
`link_workspace` for it). Workspace classification is memoized per normalized
path for 300s (bounded to 4096 entries) because it probes the filesystem.

Projection has a fast path: `agent_sessions.projected_event_index` records how
far a session's events were folded into messages / turns / tools, and
`project_events` returns immediately when it already covers the highest event
index (it still refreshes `is_ongoing` / `end_time`, which also depend on the
file mtime and on wall-clock time). A session that *gains* events still rebuilds its projection from
scratch, which is the remaining tail cost: a 104k-event session took ~62s to
re-project, so a very large and actively appended session can stall the poll
loop for that interval (turn-level incremental replay is the follow-up).
Anything that rewrites or deletes a session's events resets the cursor to `-1`
in the same transaction, so the cursor can never name a projection that no
longer matches its events; today that reset lives in the Codex scanner and the
StepCode rebuild path, and no other adapter calls `project_events`.

Evidence rows are inserted in batches of 250 via a Core insert with autoflush
suppressed; the remaining per-event work is bounded and periodic flushes (every
1000 events) keep foreign keys ordered. The projection cursor is read from the
`(agent_session_id, event_index)` unique index, so the check is an index-only
scan.

Deleting a parent row runs its foreign-key action once per deleted row: an
index lookup when the referencing column is indexed, a sequential scan of the
whole child table otherwise. Migration `0010` indexes the two
`evidence_records` referencing columns, and `0012` indexes the remaining
`SET NULL` / `CASCADE` referencing columns (`messages.turn_id`,
`tool_calls.turn_id`, `evidence_records.agent_event_id`,
`session_skill_evidence.skill_id`, `thread_edges.source_id`,
`thread_goals.source_id`, `user_roles.role_id`). Before them, deleting one
session's evidence rows took 87s on the dev database because every row
triggered a sequential scan of `activity_spans`. `0011` drops
`ix_agent_events_session_event`, which duplicated the
`agent_events (agent_session_id, event_index)` unique constraint. A test
asserts that every delete-action foreign key leads with an indexed column, and
all of these indexes are declared in the model metadata so `alembic check`
stays clean.

Diagnosis lives in the heartbeat metadata: `last_cycle` (discovered / unchanged
/ changed / indexed / failures, a `finished` flag so a mid-cycle heartbeat is
recognisable, and `discover` / `plan` / `ingest` / `maintain` / `cycle`
milliseconds), `scanner` (projections, skipped, projection_ms), and
`local_scan` (changed sources, duration, error). `/api/health` and
`/api/health/ready` surface `last_cycle` and `local_scan` on the indexer
component.

## Retention

`/storage` reclaims disk space from old agent data. The API never deletes:
`POST /api/retention/preview` inventories files older than the configured
retention floor and seals the selection into an HMAC plan token;
`POST /api/retention/runs` (admin) writes an `audit_logs` row and a
`retention.purge` `system_commands` row; the Supervisor claims that command and
applies it, deleting files and the matching `agent_sessions` /
`local_data_sources` rows before recording the freed bytes in `retention_runs`.
The size overview is cached per process but keyed on the `retention_runs`
generation, so the page (and a plain reload) shows post-purge numbers, and every
`/api/*` response is sent with `Cache-Control: no-store`.
See `docs/retention.md` for the catalog, guards and configuration.

## Security And Audit

All user and system control actions write `audit_logs`; system lifecycle
commands are persisted in `system_commands` before the Supervisor applies
them. API responses and exports redact secrets while retaining raw-event hashes
for auditability. Authentication supports session cookies and bearer API
tokens with admin/operator/viewer RBAC.
Memory list and ordinary detail responses redact detected secrets. Revealing,
copying, and exporting complete memory content requires operator or admin and
records hashes and entry ids in `audit_logs`, never the memory body.
