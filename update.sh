#!/usr/bin/env bash
# Roll the box forward to origin/main and restart.
# The reset may replace "Appalachia Inventory Dashboard.html" with the repo's
# copy, so the build stamp is cleared — the restart re-checks every S3 source
# and rebuilds the HTML only if anything actually moved.
set -euo pipefail
cd "$(dirname "$0")"
git fetch origin
git reset --hard origin/main
rm -f _app_build.json
if systemctl is-enabled novi-dash >/dev/null 2>&1; then
  sudo systemctl restart novi-dash
  echo "restarted novi-dash — journalctl -u novi-dash -f"
else
  pkill -f "serve.py 8080" || true
  sleep 2
  nohup python3 serve.py 8080 > build.log 2>&1 &
  echo "restarted — tail -f build.log"
fi
