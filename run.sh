#!/usr/bin/env bash
# Minimal run script. Stage 8: it manages the Security Judge, the Data Masker and Phoenix,
# started in that order (SPEC section 6). Postgres and the Toolbox are still started by hand.
#
#   ./run.sh start    start the Judge, the Masker and Phoenix (skips any that is already running)
#   ./run.sh stop     stop all three
#   ./run.sh status   say which are up
#   ./run.sh stop judge | start judge      one service only (used for the failing-turn test)
#   ./run.sh stop masker | start masker    same, for the Masker
#
# Each service runs as its OWN process (J-1, K-1, O-1). The CLI exiting never stops them.

set -euo pipefail
cd "$(dirname "$0")"

PHOENIX_PORT="${PHOENIX_PORT:-6006}"
JUDGE_PORT="${JUDGE_PORT:-10002}"
MASKER_PORT="${MASKER_PORT:-10003}"
export PHOENIX_PORT JUDGE_PORT MASKER_PORT
export PHOENIX_WORKING_DIR="${PHOENIX_WORKING_DIR:-$(pwd)/.phoenix}"   # trace database lives here
RUN_DIR=".run"

# For each service: the command that starts it, and the URL that proves it is healthy.
cmd_of() {
  case "$1" in
    judge)   echo "python -m guards.judge.server" ;;
    masker)  echo "python -m guards.masker.server" ;;
    phoenix) echo "python -m phoenix.server.main serve" ;;
  esac
}
url_of() {
  case "$1" in
    judge)   echo "http://127.0.0.1:${JUDGE_PORT}/.well-known/agent-card.json" ;;
    masker)  echo "http://127.0.0.1:${MASKER_PORT}/.well-known/agent-card.json" ;;
    phoenix) echo "http://localhost:${PHOENIX_PORT}/healthz" ;;
  esac
}

is_up() { curl -fsS --max-time 2 "$(url_of "$1")" >/dev/null 2>&1; }

start_one() {
  local name="$1"
  if is_up "$name"; then
    echo "$name is already running."
    return 0
  fi
  mkdir -p "$RUN_DIR" "$PHOENIX_WORKING_DIR"
  echo "Starting $name..."
  # shellcheck disable=SC2046
  nohup $(cmd_of "$name") >"$RUN_DIR/$name.log" 2>&1 &
  echo $! >"$RUN_DIR/$name.pid"
  # Health-check before declaring success: wait up to 60 seconds.
  for _ in $(seq 1 60); do
    if is_up "$name"; then
      echo "$name is up: $(url_of "$name")"
      return 0
    fi
    sleep 1
  done
  echo "$name did not become healthy in 60 seconds. Last log lines:" >&2
  tail -n 20 "$RUN_DIR/$name.log" >&2 || true
  return 1
}

stop_one() {
  local name="$1" pid_file="$RUN_DIR/$1.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    kill "$(cat "$pid_file")"
    rm -f "$pid_file"
    echo "$name stopped."
  else
    echo "No $name started by this script is running."
  fi
}

SERVICES="judge masker phoenix"
if [[ -n "${2:-}" ]]; then
  case "$2" in
    judge|masker|phoenix) SERVICES="$2" ;;
    *) echo "unknown service: $2 (use judge, masker or phoenix)" >&2; exit 2 ;;
  esac
fi

case "${1:-}" in
  start)  for s in $SERVICES; do start_one "$s"; done ;;
  stop)   for s in $SERVICES; do stop_one "$s"; done ;;
  status)
    rc=0
    for s in $SERVICES; do
      if is_up "$s"; then echo "$s: up ($(url_of "$s"))"; else echo "$s: DOWN"; rc=1; fi
    done
    exit $rc ;;
  *) echo "usage: ./run.sh start|stop|status [judge|masker|phoenix]" >&2; exit 2 ;;
esac
