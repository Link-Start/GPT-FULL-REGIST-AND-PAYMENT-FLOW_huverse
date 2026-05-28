#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

DEBUG_DISPLAY_ID="${DEBUG_DISPLAY_ID:-99}"
DEBUG_REMOTE_VNC_PORT="${DEBUG_REMOTE_VNC_PORT:-5901}"
export DEBUG_DISPLAY_ID DEBUG_REMOTE_VNC_PORT

"$ROOT/debug/headed_payment/server_desktop.sh" start

export DISPLAY=":${DEBUG_DISPLAY_ID#:}"
export PAYMENT_HEADLESS=0

exec "$ROOT/run_trial_payment_full_flow.sh" --keep-browser-open "$@"
