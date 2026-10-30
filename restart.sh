#!/bin/bash
# Safe restart for the FamPay gateway.
# Kill patterns live in THIS file so they never appear in the caller's own
# command line — `pkill -f` matches substrings, and a pattern typed directly
# in an exec command would match your own shell and kill it (SIGTERM).
D=/home/hatch/workspace/fampay-gateway
pkill -f "^${D}/venv/bin/python"
pkill -f "flock -n /tmp/fampay"
sleep 3
# Aggressive second pass: SIGTERM can be missed; escalate to SIGKILL.
for _ in 1 2 3; do
  if pgrep -f "^${D}/venv/bin/python" > /dev/null 2>&1; then
    pkill -9 -f "^${D}/venv/bin/python"
    sleep 2
  else
    break
  fi
done
pkill -f "flock -n /tmp/fampay"
sleep 1
exec "$D/ensure_running.sh"
