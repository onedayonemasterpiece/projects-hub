#!/usr/bin/env bash
set -euo pipefail
# Fixed trusted administrative binding; no cookies, tokens or provider keys.
release_root=$(readlink -f /home/dev/.local/share/projects-hub/current)
export PROJECTS_HUB_DATA_DIR=/home/dev/.local/state/projects-hub/data
export PROJECTS_HUB_SESSION_SECRET_FILE=/home/dev/.local/state/projects-hub/session-secret
export PROJECTS_HUB_PUBLIC_ORIGIN=https://projects-hub.kenigevents.ru
export PROJECTS_HUB_DEPLOY_SHA=${release_root##*/}
exec "$release_root/venv/bin/python" "$release_root/source/scripts/mcp_test_request.py" "$@"
