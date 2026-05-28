#!/usr/bin/env bash
set -euo pipefail

# Local helper: start server-side virtual desktop and bind it to local VNC.
# Keep this process running while viewing 127.0.0.1:${DEBUG_LOCAL_VNC_PORT:-5901}.

HOST="${DEBUG_HOST:-your.server.ip}"
USER_NAME="${DEBUG_USER:-root}"
SSH_PORT="${DEBUG_SSH_PORT:-22}"
PROJECT_DIR="${DEBUG_PROJECT_DIR:-/opt/openaii}"
DISPLAY_ID="${DEBUG_DISPLAY_ID:-99}"
REMOTE_VNC_PORT="${DEBUG_REMOTE_VNC_PORT:-5901}"
LOCAL_VNC_PORT="${DEBUG_LOCAL_VNC_PORT:-5901}"

if [ -n "${DEBUG_SSH_PASSWORD:-}" ] && [ -z "${SSHPASS:-}" ]; then
  export SSHPASS="$DEBUG_SSH_PASSWORD"
fi

if [ -n "${SSHPASS:-}" ]; then
  SSH_CLIENT=(sshpass -e ssh)
else
  SSH_CLIENT=(ssh)
fi

SSH_OPTS=(
  -p "$SSH_PORT"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=10
)
REMOTE="${USER_NAME}@${HOST}"

quote() {
  printf "%q" "$1"
}

echo "[debug] starting remote desktop on ${REMOTE}:${PROJECT_DIR}"
"${SSH_CLIENT[@]}" "${SSH_OPTS[@]}" "$REMOTE" \
  "cd $(quote "$PROJECT_DIR") && DEBUG_DISPLAY_ID=$(quote "$DISPLAY_ID") DEBUG_REMOTE_VNC_PORT=$(quote "$REMOTE_VNC_PORT") ./debug/headed_payment/server_desktop.sh start"

echo "[debug] VNC tunnel ready:"
echo "        local 127.0.0.1:${LOCAL_VNC_PORT} -> server 127.0.0.1:${REMOTE_VNC_PORT}"
echo "        open VNC Viewer at 127.0.0.1:${LOCAL_VNC_PORT}"
echo "        Ctrl-C closes only the tunnel; server desktop can be stopped with server_desktop.sh stop"

exec "${SSH_CLIENT[@]}" "${SSH_OPTS[@]}" \
  -N \
  -L "${LOCAL_VNC_PORT}:127.0.0.1:${REMOTE_VNC_PORT}" \
  "$REMOTE"
