#!/bin/sh
set -eu

uid="$(id -u)"
runtime="/run/user/$uid"
bus="$runtime/bus"

if [ ! -S "$bus" ]; then
  echo "Projects Hub DevCoveer user bus is unavailable" >&2
  exit 70
fi

export XDG_RUNTIME_DIR="$runtime"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$bus"

unit="projects-hub-devcoveer-$PPID-$$"
exec /usr/bin/systemd-run --user --pipe --wait --collect --quiet \
  --unit="$unit" \
  --property=BindsTo=projects-hub.service \
  --property=After=projects-hub.service \
  --property=NoNewPrivileges=true \
  --property=UMask=0077 \
  --property=TimeoutStopSec=30 \
  /home/dev/.local/bin/codex-mcp-server
