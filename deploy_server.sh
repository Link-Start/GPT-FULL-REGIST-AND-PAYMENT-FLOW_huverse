#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "[setup] root=$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[setup] python3 is required" >&2
  exit 1
fi

if ! command -v node >/dev/null 2>&1; then
  echo "[setup] node is required for gpt_trial_protocol sentinel token generation" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "[setup] curl is required" >&2
  exit 1
fi

echo "[setup] protocol venv"
python3 -m venv protocol/gpt_trial_protocol/.venv
protocol/gpt_trial_protocol/.venv/bin/pip install -U pip
protocol/gpt_trial_protocol/.venv/bin/pip install -e './protocol/gpt_trial_protocol[dev]'
protocol/gpt_trial_protocol/.venv/bin/pip install -q 'h2>=4,<5'

echo "[setup] ruyipage venv"
python3 -m venv ruyipage/.venv
ruyipage/.venv/bin/pip install -U pip
ruyipage/.venv/bin/pip install -e ./ruyipage
if [ -f ruyipage/requirements.txt ]; then
  ruyipage/.venv/bin/pip install -r ruyipage/requirements.txt
fi

if [ ! -x firefox-fingerprintBrowser/browser/firefox/firefox ]; then
  archive="$(find firefox-fingerprintBrowser/downloads -maxdepth 1 -name 'firefox-*.linux-x86_64.tar.xz' | sort | tail -n 1)"
  if [ -z "$archive" ]; then
    echo "[setup] firefox fingerprint browser archive not found under firefox-fingerprintBrowser/downloads" >&2
    exit 1
  fi
  echo "[setup] extract fingerprint browser: $archive"
  rm -rf firefox-fingerprintBrowser/browser
  mkdir -p firefox-fingerprintBrowser/browser
  tar -xJf "$archive" -C firefox-fingerprintBrowser/browser
fi
chmod +x firefox-fingerprintBrowser/browser/firefox/firefox

if [ ! -f full_flow.env ]; then
  cp full_flow.env.example full_flow.env
  echo "[setup] created full_flow.env; review before production run"
fi

if [ ! -f ruyipage/.env ]; then
  cp ruyipage/.env.example ruyipage/.env
  echo "[setup] created ruyipage/.env; fill API/proxy values before production run"
fi

echo "[setup] done"
echo "Run example:"
echo "./run_trial_payment_full_flow.sh --generate-email --email-type frimail --card-line 'CARD|MM|YYYY|CVV' --sms-line '+1xxx|https://sms-api'"
