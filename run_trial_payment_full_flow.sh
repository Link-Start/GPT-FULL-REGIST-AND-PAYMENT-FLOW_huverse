#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ -f full_flow.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./full_flow.env
  set +a
fi

exec python3 ./trial_payment_full_flow.py "$@"
