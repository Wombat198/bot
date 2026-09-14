#!/usr/bin/env bash
# 24/7 supervisor: restart long_run on exit. Default remains dry-run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
PIDFILE="logs/supervise.pid"
BOT_PIDFILE="logs/long-run.pid"
LOG="logs/long-run.log"
CONFIG="${CONFIG:-config/live-50.yaml}"
EXTRA_ARGS=("$@")

echo $$ > "$PIDFILE"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) SUPERVISOR start pid=$$ config=$CONFIG args=${EXTRA_ARGS[*]:-}" | tee -a "$LOG"

cleanup() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) SUPERVISOR stop" | tee -a "$LOG"
  if [[ -f "$BOT_PIDFILE" ]]; then
    botpid=$(cat "$BOT_PIDFILE" 2>/dev/null || true)
    if [[ -n "${botpid:-}" ]] && kill -0 "$botpid" 2>/dev/null; then
      kill "$botpid" 2>/dev/null || true
    fi
  fi
  rm -f "$PIDFILE"
}
trap cleanup EXIT INT TERM

# Activate venv if present
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

backoff=2
while true; do
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) SUPERVISOR launching long_run config=$CONFIG" | tee -a "$LOG"
  set +e
  python -u scripts/long_run.py --config "$CONFIG" "${EXTRA_ARGS[@]}" >>"$LOG" 2>&1
  code=$?
  set -e
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) SUPERVISOR child exited code=$code — restart in ${backoff}s" | tee -a "$LOG"
  sleep "$backoff"
  # soft cap backoff at 60s
  if (( backoff < 60 )); then
    backoff=$(( backoff * 2 ))
    if (( backoff > 60 )); then backoff=60; fi
  fi
  # reset backoff after a long healthy run is hard without timestamps;
  # keep simple: reset to 2 after each successful start loop iteration once child ran >5m
  # (approximate: if exit was after long time, still ok to slowly back off)
done
