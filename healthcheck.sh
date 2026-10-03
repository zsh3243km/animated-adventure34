#!/bin/sh
# Fails when the panel or the proxy service stops answering.
#
# Used by the container HEALTHCHECK and by the entrypoint watchdog. The panel
# is the part that matters: if it stops answering for long enough, the whole
# service is restarted by the entrypoint.
set -eu

PANEL=http://127.0.0.1:8000

# 401/403 still prove the panel is serving. Only a connection failure or a 5xx
# counts as unhealthy.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "$PANEL/api/system" || echo 000)
case "$code" in
  000|5*) echo "panel unhealthy (http $code)"; exit 1 ;;
esac

# The Xray node must be alive, otherwise every config is dead.
if ! pgrep -f '/opt/lumen-node/main' >/dev/null 2>&1; then
  echo "xray node process is not running"
  exit 1
fi

exit 0