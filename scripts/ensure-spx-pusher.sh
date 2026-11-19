#!/bin/bash
# Start the pusher supervisor if it is not already running.
set -u
LOGDIR=/workspace/logs
PIDF=$LOGDIR/spx-pusher.pid
LOG=$LOGDIR/spx-pusher-supervisor.log
mkdir -p "$LOGDIR"
if [ -f "$PIDF" ]; then
  pid=$(cat "$PIDF" 2>/dev/null || true)
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
    # Already running.
    exit 0
  fi
fi
nohup /workspace/bin/run-spx-pusher.sh >>"$LOG" 2>&1 &
echo $! >"$PIDF"
exit 0
