#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# research-ctl.sh {start|stop|status|logs}: run the senseai-research server on this box.
# Installed by scripts/install_research_ec2.sh (which fills in @STACK@). The server listens on
# 127.0.0.1 only; people reach it through the viewer at /research, behind the viewer password.

set -euo pipefail

STACK="${SENSEAI_STACK_DIR:-@STACK@}"
[[ "$STACK" == "@STACK@" ]] && STACK="$HOME/senseai"
VENV="$STACK/.venv"

load_env() {
  set -a
  # shellcheck disable=SC1090
  if [[ -r "$STACK/viewer.env" ]]; then . "$STACK/viewer.env"; elif [[ -r /etc/senseai-viewer.env ]]; then . /etc/senseai-viewer.env; fi
  # shellcheck disable=SC1090
  [[ -r "$STACK/research.env" ]] && . "$STACK/research.env"
  set +a
  PORT="${RESEARCH_PORT:-8100}"
  DATA="${RESEARCH_DATA:-$STACK/research}"
  PIDFILE="$DATA/server.pid"
  LOG="$DATA/logs/server.log"
}

running() {
  [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

health() {
  curl -fsS --max-time 5 "http://127.0.0.1:$PORT/api/research/health" 2>/dev/null
}

cmd_start() {
  if running; then
    echo "already running (pid $(cat "$PIDFILE")) on 127.0.0.1:$PORT"
    return 0
  fi
  mkdir -p "$DATA/logs"
  nohup "$VENV/bin/senseai-research" serve --port "$PORT" >>"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  for _ in $(seq 1 60); do
    if health >/dev/null; then
      echo "research server running (pid $(cat "$PIDFILE")) on 127.0.0.1:$PORT"
      echo "open /research in the viewer"
      return 0
    fi
    running || break
    sleep 0.5
  done
  echo "the research server did not start; last lines of $LOG:" >&2
  tail -n 20 "$LOG" >&2 || true
  return 1
}

cmd_stop() {
  if ! running; then
    echo "not running"
    rm -f "$PIDFILE"
    return 0
  fi
  local pid
  pid="$(cat "$PIDFILE")"
  kill "$pid"
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
  rm -f "$PIDFILE"
  echo "stopped"
}

cmd_status() {
  if ! running; then
    echo "not running"
    return 3
  fi
  local h
  h="$(health || true)"
  if [[ -z "$h" ]]; then
    echo "running (pid $(cat "$PIDFILE")) but not answering on 127.0.0.1:$PORT"
    return 1
  fi
  "$VENV/bin/python" - "$h" <<'PY'
import json, sys
h = json.loads(sys.argv[1])
print(f"running, version {h['version']}, model {h['model']}")
print(f"  model reachable: {'yes' if h['model_reachable'] else 'no (' + h['model_detail'] + ')'}")
print(f"  ledger: {h['data']}")
print(f"  recordings: {h['data_root'] or 'SENSEAI_DATA_ROOT not set'}")
print(f"  profiles: {h['profiles_repo']} at {(h['profiles_sha'] or '')[:12]}")
print(f"  library PRs: {'GitHub' if h['github'] else 'saved for download (no GITHUB_TOKEN)'}")
PY
}

cmd_logs() {
  mkdir -p "$DATA/logs"
  touch "$LOG"
  tail -n 100 -f "$LOG"
}

load_env
case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop && cmd_start ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  *)
    echo "usage: research-ctl.sh {start|stop|restart|status|logs}" >&2
    exit 2
    ;;
esac
