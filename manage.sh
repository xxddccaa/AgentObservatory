#!/usr/bin/env bash
# AgentObservatory 交互式管理菜单：状态 / 启停 / 日志 / 健康检查（单人本机使用，无登录）
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Linux 服务器用 root 部署（/root）；macOS 无 root 时部署到当前用户 home。
DEFAULT_HOME=/root/.agent-observatory
if [ "$(uname -s)" = Darwin ] && [ "$(id -u)" != 0 ]; then
  DEFAULT_HOME="$HOME/.agent-observatory"
fi
HOME_DIR="${AGENT_OBS_HOME:-$DEFAULT_HOME}"
export AGENT_OBS_HOME="$HOME_DIR"
export AGENT_OBS_ENV_FILE="${AGENT_OBS_ENV_FILE:-$HOME_DIR/config.env}"
CLI="$HOME_DIR/current/backend/.venv/bin/agent-observatory"

usage() {
  cat <<'EOF'
用法:
  ./manage.sh              进入交互菜单
  ./manage.sh <命令>       直通执行

命令:
  deploy | status | up | down | restart | logs | doctor

示例:
  ./manage.sh status
  ./manage.sh logs api --lines 200 --follow
  AGENT_OBS_HOST=0.0.0.0 ./manage.sh deploy   # 首次部署时改为监听所有网卡（无登录，慎用）
EOF
}

run_cli() {
  if [ -x "$CLI" ]; then
    "$CLI" "$@"
  else
    echo "尚未部署本机副本，请先执行 ./manage.sh deploy（依赖和数据不会写入共享仓库）。" >&2
    return 127
  fi
}

deploy() {
  python3 "$ROOT/scripts/deploy.py" "$@"
}

start() {
  if [ -x "$CLI" ]; then
    python3 "$HOME_DIR/current/scripts/deploy.py" --start-only
  else
    deploy
  fi
}

status_summary() {
  local json
  if ! json="$(run_cli status --json 2>/dev/null)"; then
    echo "  supervisor 未运行（选 2 启动）"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$json" <<'PY'
import json
import sys

data = json.loads(sys.argv[1])
for name in ("api", "worker"):
    info = (data.get("components") or {}).get(name) or {}
    command = info.get("command") or []
    port = ""
    if "--port" in command:
        port = " port=" + command[command.index("--port") + 1]
    print(f"  {name}: {info.get('status', 'unknown')} pid={info.get('pid')}{port}")
ready = data.get("readiness")
if isinstance(ready, dict) and ready.get("status"):
    print(f"  readiness: {ready['status']}")
PY
  else
    run_cli status
  fi
}

view_logs() {
  local component lines follow
  read -rp "组件 [all/api/worker] (默认 all): " component
  component="${component:-all}"
  read -rp "行数 (默认 100): " lines
  lines="${lines:-100}"
  read -rp "实时跟随? 结束时按 Ctrl-C 回菜单 [y/N]: " follow
  echo
  if [[ "${follow:-}" =~ ^[Yy]$ ]]; then
    trap 'echo; echo "(已停止跟随)"' INT
    run_cli logs "$component" --lines "$lines" --follow
    trap - INT
  else
    run_cli logs "$component" --lines "$lines"
  fi
}

menu() {
  if [ ! -t 0 ]; then
    echo "交互菜单需要终端（TTY）；可用 ./manage.sh <命令> 直通执行。" >&2
    usage >&2
    return 1
  fi
  local choice
  while true; do
    clear 2>/dev/null || true
    echo "=================================================="
    echo " AgentObservatory 管理菜单"
    echo " $ROOT"
    echo "=================================================="
    status_summary
    echo "--------------------------------------------------"
    echo " 1) 服务状态"
    echo " 2) 启动 / 重新部署 (up)"
    echo " 3) 停止服务 (down)"
    echo " 4) 重启服务 (restart)"
    echo " 5) 查看日志"
    echo " 6) 健康检查 (doctor)"
    echo " 0) 退出"
    echo "--------------------------------------------------"
    read -rp "请选择 [0-6]: " choice || choice="0"
    case "$choice" in
      1) run_cli status ;;
      2) deploy ;;
      3) run_cli down ;;
      4) run_cli restart ;;
      5) view_logs ;;
      6) run_cli doctor ;;
      0) echo "已退出"; return 0 ;;
      *) echo "无效选项: $choice" ;;
    esac
    echo
    read -rp "按回车返回菜单..." _
  done
}

case "${1:-}" in
  ""|menu) menu ;;
  deploy) shift; deploy "$@" ;;
  up) start ;;
  status|down|restart|doctor) run_cli "$@" ;;
  logs)
    shift
    run_cli logs "$@"
    ;;
  -h|--help|help) usage ;;
  *)
    echo "未知命令: $1" >&2
    usage >&2
    exit 2
    ;;
esac
exit $?
