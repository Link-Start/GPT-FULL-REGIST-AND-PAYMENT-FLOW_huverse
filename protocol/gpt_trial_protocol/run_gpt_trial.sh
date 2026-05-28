#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

proxy="${GPT_TRIAL_PROXY:-}"
email_type="${GPT_TRIAL_EMAIL_TYPE:-auto}"
email_code_provider="${GPT_TRIAL_EMAIL_CODE_PROVIDER:-auto}"
email_code_base_url="${GPT_TRIAL_EMAIL_CODE_BASE_URL:-}"
checkout_country="${GPT_TRIAL_CHECKOUT_COUNTRY:-US}"
checkout_currency="${GPT_TRIAL_CHECKOUT_CURRENCY:-USD}"
timeout="${GPT_TRIAL_TIMEOUT:-30}"
code_timeout="${GPT_TRIAL_CODE_TIMEOUT:-90}"
backend="${GPT_TRIAL_BACKEND:-curl_cffi}"
out="${GPT_TRIAL_OUT:-runtime/results.jsonl}"
trace_dir="${GPT_TRIAL_TRACE_DIR:-runtime/traces}"
generated_prefix="${GPT_TRIAL_GENERATED_EMAIL_PREFIX:-lu}"

args=(
  run
  --email-type "$email_type"
  --email-code-provider "$email_code_provider"
  --checkout-country "$checkout_country"
  --checkout-currency "$checkout_currency"
  --timeout "$timeout"
  --code-timeout "$code_timeout"
  --backend "$backend"
  --out "$out"
  --trace-dir "$trace_dir"
)

if [ -n "$proxy" ]; then
  args+=(--proxy "$proxy")
fi
if [ -n "$email_code_base_url" ]; then
  args+=(--email-code-base-url "$email_code_base_url")
fi

if [ "${GPT_TRIAL_GENERATE_EMAIL:-0}" = "1" ]; then
  args+=(--generate-email --generated-email-prefix "$generated_prefix")
fi
if [ "${GPT_TRIAL_LOGIN_EXISTING:-0}" = "1" ]; then
  args+=(--login-existing)
fi

exec gpt-trial "${args[@]}" "$@"
