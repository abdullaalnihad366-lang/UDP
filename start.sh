#!/usr/bin/env sh
# Keeps KILLU X HOST running. If the server ever stops, it starts again.
# Termux:  sh start.sh      (keeps the phone awake too)
cd "$(dirname "$0")" || exit 1
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock
while true; do
  python server.py
  echo "Server stopped. Restarting in 3 seconds... (Ctrl+C to quit)"
  sleep 3
done
