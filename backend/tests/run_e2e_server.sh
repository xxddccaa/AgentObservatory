#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
ALEMBIC_BIN="${ALEMBIC_BIN:-.venv/bin/alembic}"

: "${E2E_DATABASE_URL:?set E2E_DATABASE_URL to a disposable agent_observatory_ci PostgreSQL URL}"
export AGENT_OBS_DATABASE_URL="$E2E_DATABASE_URL"
export TEST_DATABASE_URL="$AGENT_OBS_DATABASE_URL"
"$PYTHON_BIN" - <<'PY'
import os
from urllib.parse import urlparse

if urlparse(os.environ["TEST_DATABASE_URL"]).path.lstrip("/") != "agent_observatory_ci":
    raise SystemExit("refusing E2E migrations against a non-CI database")
PY
export AGENT_OBS_HOME='/tmp/agent-observatory-e2e/home'
export AGENT_OBS_CODEX_HOME='/tmp/agent-observatory-e2e/codex'
export AGENT_OBS_STEPCODE_HOME='/tmp/agent-observatory-e2e/stepcode'
export AGENT_OBS_CLAUDE_PROJECTS_ROOT='/tmp/agent-observatory-e2e/claude-projects'
export AGENT_OBS_PI_HOME='/tmp/agent-observatory-e2e/pi'
export AGENT_OBS_XDCODE_HOME='/tmp/agent-observatory-e2e/xdcode'
export AGENT_OBS_STEPGO_HOME='/tmp/agent-observatory-e2e/stepgo'
export AGENT_OBS_EXTRA_ROOTS=''
export AGENT_OBS_ALLOWED_WORKSPACES='/tmp/agent-observatory-e2e'
export AGENT_OBS_DISCOVERY_ENABLED='false'
export AGENT_OBS_INSIGHTS_ENABLED='false'
export AGENT_OBS_MEMORIES_ENABLED='true'
export AGENT_OBS_PORT='18124'
export PYTHONDONTWRITEBYTECODE=1

mkdir -p "$AGENT_OBS_STEPCODE_HOME"
"$ALEMBIC_BIN" downgrade base
"$ALEMBIC_BIN" upgrade head
PYTHONPATH=. "$PYTHON_BIN" tests/e2e_seed.py

PYTHONPATH=. "$PYTHON_BIN" -c \
  'from agent_observatory.cli import main; raise SystemExit(main(["worker"]))' &
worker_pid=$!
PYTHONPATH=. "$PYTHON_BIN" -m uvicorn agent_observatory.main:app \
  --host 127.0.0.1 --port "$AGENT_OBS_PORT" &
api_pid=$!

cleanup() {
  kill "$api_pid" "$worker_pid" 2>/dev/null || true
  wait "$api_pid" "$worker_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait "$api_pid"
