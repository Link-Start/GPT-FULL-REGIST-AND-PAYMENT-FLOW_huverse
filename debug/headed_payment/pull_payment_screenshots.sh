#!/usr/bin/env bash
set -euo pipefail

# Local helper: copy headed-payment failure screenshots from the server to the
# local workspace. Run it after headed tests so screenshots are available
# without browsing the server filesystem.

HOST="${DEBUG_HOST:-your-server.example}"
USER_NAME="${DEBUG_USER:-root}"
SSH_PORT="${DEBUG_SSH_PORT:-22}"
PROJECT_DIR="${DEBUG_PROJECT_DIR:-/opt/openaii}"
LOCAL_ROOT="${DEBUG_LOCAL_SCREENSHOT_DIR:-runtime/local_payment_screenshots}"
RUN_ID="${1:-latest}"

if [ -n "${DEBUG_SSH_PASSWORD:-}" ] && [ -z "${SSHPASS:-}" ]; then
  export SSHPASS="$DEBUG_SSH_PASSWORD"
fi

if [ -n "${SSHPASS:-}" ]; then
  SSH_CLIENT=(sshpass -e ssh)
  RSYNC_RSH="sshpass -e ssh"
else
  SSH_CLIENT=(ssh)
  RSYNC_RSH="ssh"
fi

SSH_OPTS=(
  -p "$SSH_PORT"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=10
)

for opt in "${SSH_OPTS[@]}"; do
  RSYNC_RSH+=" $(printf '%q' "$opt")"
done

REMOTE="${USER_NAME}@${HOST}"

quote() {
  printf "%q" "$1"
}

if [ "$RUN_ID" = "latest" ]; then
  RUN_ID="$("${SSH_CLIENT[@]}" "${SSH_OPTS[@]}" "$REMOTE" \
    "cd $(quote "$PROJECT_DIR") && find runtime/full_flow -mindepth 2 -maxdepth 2 -type f -name '*.png' -printf '%T@ %h\n' 2>/dev/null | sort -nr | awk 'NF{print \$2; exit}' | xargs -r basename")"
  if [ -z "$RUN_ID" ]; then
    echo "[screenshots] no remote png screenshots found" >&2
    exit 1
  fi
fi

REMOTE_RUN_DIR="${PROJECT_DIR%/}/runtime/full_flow/${RUN_ID}"
LOCAL_DIR="${LOCAL_ROOT%/}/${RUN_ID}"

files="$("${SSH_CLIENT[@]}" "${SSH_OPTS[@]}" "$REMOTE" \
  "find $(quote "$REMOTE_RUN_DIR") -maxdepth 1 -type f -name '*.png' -printf '%f\n' 2>/dev/null | sort")"

if [ -z "$files" ]; then
  echo "[screenshots] no screenshots for run: $RUN_ID" >&2
  exit 1
fi

mkdir -p "$LOCAL_DIR"
rsync -az -e "$RSYNC_RSH" \
  --include='*.png' \
  --exclude='*' \
  "${REMOTE}:${REMOTE_RUN_DIR}/" \
  "${LOCAL_DIR}/"

echo "[screenshots] run: $RUN_ID"
echo "[screenshots] local: $LOCAL_DIR"
find "$LOCAL_DIR" -maxdepth 1 -type f -name '*.png' -printf '  %f\n' | sort
