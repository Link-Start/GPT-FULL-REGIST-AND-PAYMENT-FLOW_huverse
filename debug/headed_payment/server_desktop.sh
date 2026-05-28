#!/usr/bin/env bash
set -euo pipefail

# Server-side helper for headed payment debugging.
# It keeps VNC bound to 127.0.0.1; use connect_local.sh for SSH tunneling.

DISPLAY_ID="${DEBUG_DISPLAY_ID:-99}"
DISPLAY_NAME=":${DISPLAY_ID#:}"
SCREEN_SPEC="${DEBUG_SCREEN_SPEC:-1280x900x24}"
VNC_PORT="${DEBUG_REMOTE_VNC_PORT:-5901}"
INSTALL_MISSING="${DEBUG_INSTALL_MISSING:-1}"
XVFB_LOG="/tmp/openaii_xvfb_${DISPLAY_ID#:}.log"
X11VNC_LOG="/tmp/openaii_x11vnc_${DISPLAY_ID#:}.log"

install_missing() {
  if command -v Xvfb >/dev/null 2>&1 && command -v x11vnc >/dev/null 2>&1; then
    return
  fi
  if [ "$INSTALL_MISSING" != "1" ]; then
    echo "[desktop] missing Xvfb/x11vnc; set DEBUG_INSTALL_MISSING=1 or install manually" >&2
    exit 1
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y xvfb x11vnc
}

start_desktop() {
  install_missing
  if ! pgrep -f "Xvfb ${DISPLAY_NAME}" >/dev/null 2>&1; then
    nohup Xvfb "$DISPLAY_NAME" -screen 0 "$SCREEN_SPEC" -nolisten tcp >"$XVFB_LOG" 2>&1 &
    sleep 0.5
  fi
  if ! pgrep -f "x11vnc .*${DISPLAY_NAME}.*rfbport ${VNC_PORT}" >/dev/null 2>&1; then
    nohup x11vnc \
      -display "$DISPLAY_NAME" \
      -localhost \
      -nopw \
      -forever \
      -shared \
      -rfbport "$VNC_PORT" \
      >"$X11VNC_LOG" 2>&1 &
    sleep 0.5
  fi
  echo "[desktop] display=${DISPLAY_NAME} vnc=127.0.0.1:${VNC_PORT}"
  echo "[desktop] logs=${XVFB_LOG} ${X11VNC_LOG}"
}

stop_desktop() {
  pkill -f "x11vnc .*${DISPLAY_NAME}.*rfbport ${VNC_PORT}" 2>/dev/null || true
  pkill -f "Xvfb ${DISPLAY_NAME}" 2>/dev/null || true
  echo "[desktop] stopped display=${DISPLAY_NAME} vnc=127.0.0.1:${VNC_PORT}"
}

status_desktop() {
  if pgrep -f "Xvfb ${DISPLAY_NAME}" >/dev/null 2>&1; then
    echo "[desktop] Xvfb running on ${DISPLAY_NAME}"
  else
    echo "[desktop] Xvfb not running on ${DISPLAY_NAME}"
  fi
  if pgrep -f "x11vnc .*${DISPLAY_NAME}.*rfbport ${VNC_PORT}" >/dev/null 2>&1; then
    echo "[desktop] x11vnc running on 127.0.0.1:${VNC_PORT}"
  else
    echo "[desktop] x11vnc not running on 127.0.0.1:${VNC_PORT}"
  fi
}

case "${1:-start}" in
  start) start_desktop ;;
  stop) stop_desktop ;;
  status) status_desktop ;;
  restart)
    stop_desktop
    start_desktop
    ;;
  *)
    echo "usage: $0 [start|stop|status|restart]" >&2
    exit 2
    ;;
esac
