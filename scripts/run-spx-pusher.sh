#!/bin/bash
# Restart-loop for the Futu SPX snapshot pusher. Does not print secrets.
set -u
ROOT=/workspace/options-dashboard-render
LOGDIR=/workspace/logs
LOG=$LOGDIR/spx-pusher.log
mkdir -p "$LOGDIR"
export PYTHONUNBUFFERED=1
export PYTHONPATH="$ROOT/qqq:${PYTHONPATH:-}"
export LBSAFE="${LBSAFE:-$ROOT/lbsafe}"
export RENDER_DASH_URL="${RENDER_DASH_URL:-https://options-dashboard-pp77.onrender.com}"
cd "$ROOT"
echo "$(date '+%Y-%m-%d %H:%M:%S') supervisor start" >>"$LOG"
while true; do
  python3 -u "$ROOT/qqq/push_spx_snapshot.py" >>"$LOG" 2>&1
  rc=$?
  echo "$(date '+%Y-%m-%d %H:%M:%S') pusher exited rc=$rc, restart in 15s" >>"$LOG"
  sleep 15
done
