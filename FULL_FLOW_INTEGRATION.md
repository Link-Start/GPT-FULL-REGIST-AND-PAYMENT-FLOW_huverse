# Protocol registration + ruyi payment integration

This is a loose orchestration layer between two independent projects:

1. `protocol/gpt_trial_protocol`
   - Protocol registration/login.
   - Outputs `checkoutUrl`.

2. `ruyipage`
   - ruyiPage + fingerprint Firefox payment automation.
   - Consumes `checkoutUrl` as `--start-url`.

3. `getrt/codex_oauth_getrt.py`
   - Optional Codex OAuth refresh-token exporter.
   - Runs only after protocol registration and payment both succeed.

The orchestrator does **not** import implementation modules from either side.
It only calls their CLIs and stores logs/results under:

```text
/path/to/GPT-FULL-REGIST-AND-PAYMENT-FLOW/runtime/full_flow/<run-id>/
```

## Install protocol project

Local setup:

```bash
./deploy_server.sh
```

`deploy_server.sh` creates the protocol and ruyi virtualenvs and extracts the
Linux fingerprint Firefox from `firefox-fingerprintBrowser/downloads`.

## Dry run

```bash
cd /path/to/GPT-FULL-REGIST-AND-PAYMENT-FLOW
./run_trial_payment_full_flow.sh \
  --generate-email \
  --email-type frimail \
  --card-line '4111111111111111|03|2030|123' \
  --sms-line '+1xxxxxxxxxx|https://sms-provider.example/api' \
  --dry-run
```

## Full run

```bash
cd /path/to/GPT-FULL-REGIST-AND-PAYMENT-FLOW
./run_trial_payment_full_flow.sh \
  --generate-email \
  --email-type frimail \
  --card-line 'CARD|MM|YYYY|CVV' \
  --sms-line '+1xxxxxxxxxx|https://sms-provider.example/api'
```

## Existing iCloud mailbox

To use a specific iCloud mailbox instead of generating one:

```bash
./run_trial_payment_full_flow.sh \
  --email 'name@icloud.com' \
  --email-type icloud \
  --email-code-provider agiunx \
  --card-line 'CARD|MM|YYYY|CVV' \
  --sms-line '+1xxxxxxxxxx|https://sms-provider.example/api'
```

Do not add `--generate-email` when the account mailbox is supplied explicitly.

## Optional ChatGPT web session JSON export

The protocol stage can optionally persist the raw ChatGPT web session JSON
produced by registration/login. The full-flow orchestrator then converts that
session with the same logic as `token转换.html`.

Default output format: `cpa`

```bash
./run_trial_payment_full_flow.sh \
  --generate-email \
  --email-type frimail \
  --card-line 'CARD|MM|YYYY|CVV' \
  --sms-line '+1xxxxxxxxxx|https://sms-provider.example/api' \
  --enable-session-json
```

Optional formats:

```bash
--session-json-format cpa
--session-json-format cockpit
--session-json-format sub2api
--session-json-format raw
```

Per-run output:

```text
runtime/full_flow/<run-id>/web_session_result.json
runtime/full_flow/<run-id>/protocol_sessions/<email>.json
```

Archived final JSON (default):

```text
accfile/session_json/<email>.json
```

This step is optional and remains loosely coupled to the other stages.

## Full run + Codex OAuth refresh token

`getrt` is optional and remains loosely coupled. The orchestrator calls its CLI
with `subprocess` only after both protocol registration and payment succeed:

```bash
./run_trial_payment_full_flow.sh \
  --generate-email \
  --email-type frimail \
  --card-line 'CARD|MM|YYYY|CVV' \
  --sms-line '+1xxxxxxxxxx|https://sms-provider.example/api' \
  --enable-getrt
```

By default, the archived getrt JSON uses the CPA/CLIProxyAPI format. Other
formats can be selected without changing the loose integration boundary:

```bash
# Generic getrt JSON
--getrt-output-format codex

# Sub2Api / ChatGPT-to-API import JSON
--getrt-output-format sub2
--getrt-output-format sub2api
```

Format-specific metadata can be passed through the orchestrator:

```bash
--getrt-plan-type plus
--getrt-name-prefix '[testplus]'
--getrt-concurrency 10
--getrt-priority 1
--getrt-proxy-url 'http://proxy.example:8080'
--getrt-proxy-strict
--getrt-prefix 'route-a'
```

If OAuth is redirected to `auth.openai.com/add-phone`, getrt can complete that
branch by protocol when explicitly enabled:

```bash
--enable-getrt
--enable-getrt-add-phone
--getrt-phone-line '+1xxxxxxxxxx----https://sms-provider.example/api'
```

When `--enable-getrt-add-phone` is set and `--getrt-phone-line` is omitted, the
orchestrator passes the payment `--sms-line` to getrt. Without this opt-in,
add-phone keeps the old behavior: getrt writes a no-refresh-token JSON marked
`requires_phone`.

Per-run getrt output:

```text
runtime/full_flow/<run-id>/getrt_result.json
runtime/full_flow/<run-id>/getrt.log
```

If getrt succeeds, the same JSON is copied to:

```text
accfile/json/<email>.json
```

`summary.json` records only metadata:

```json
{
  "getrt": {
    "status": "success",
    "outputFormat": "cpa",
    "outputPath": ".../getrt_result.json",
    "hasAccessToken": true,
    "hasRefreshToken": true
  }
}
```

If OpenAI OAuth requires phone binding, getrt exits successfully and writes a
format-compatible JSON with an empty `refresh_token`. The orchestrator treats
that as a completed getrt stage and archives the JSON:

```json
{
  "getrt": {
    "status": "requires_phone",
    "hasRefreshToken": false,
    "refreshTokenMissingReason": "add_phone_required",
    "requiresPhone": true
  }
}
```

The refresh token is not printed to terminal output.

## Protocol proxy

Default protocol proxy:

```text
http://127.0.0.1:7897
```

Override it when needed:

```bash
./run_trial_payment_full_flow.sh \
  --protocol-proxy 'http://127.0.0.1:7890' \
  --generate-email \
  --email-type frimail \
  --card-line 'CARD|MM|YYYY|CVV' \
  --sms-line '+1xxxxxxxxxx|https://sms-provider.example/api'
```

Run protocol registration without proxy:

```bash
./run_trial_payment_full_flow.sh \
  --no-protocol-proxy \
  --generate-email \
  --email-type frimail \
  --card-line 'CARD|MM|YYYY|CVV' \
  --sms-line '+1xxxxxxxxxx|https://sms-provider.example/api'
```

Equivalent env values for direct mode:

```text
GPT_TRIAL_PROXY=direct
GPT_TRIAL_PROXY=none
GPT_TRIAL_PROXY=off
```

### Protocol/getrt chained proxy

Protocol registration and getrt can also use a local chained proxy bridge:

```text
protocol/getrt CLI -> localhost bridge -> local parent proxy -> upstream proxy
```

Configure in `full_flow.env`:

```bash
PROTOCOL_PROXY_CHAIN_UPSTREAM='host:port:user:pass(socks)'
PROTOCOL_PROXY_CHAIN_VIA='http://127.0.0.1:7897'

GETRT_PROXY_CHAIN_UPSTREAM='host:port:user:pass(socks)'
GETRT_PROXY_CHAIN_VIA='http://127.0.0.1:7897'
```

On the server deployment under `/opt/openaii`, the local `127.0.0.1:7897`
bridge hop is skipped automatically and the upstream proxy is used directly.
That bridge remains a local-dev only path.

Equivalent CLI flags:

```bash
--protocol-proxy-chain-upstream 'host:port:user:pass(socks)'
--protocol-proxy-chain-via 'http://127.0.0.1:7897'
--getrt-proxy-chain-upstream 'host:port:user:pass(socks)'
--getrt-proxy-chain-via 'http://127.0.0.1:7897'
```

## Protocol only

```bash
./run_trial_payment_full_flow.sh \
  --generate-email \
  --email-type frimail \
  --skip-payment
```

## Payment only from existing checkout URL

```bash
./run_trial_payment_full_flow.sh \
  --checkout-url 'https://pay.openai.com/c/pay/...' \
  --card-line 'CARD|MM|YYYY|CVV' \
  --sms-line '+1xxxxxxxxxx|https://sms-provider.example/api'
```

## Payment retries

The payment stage retries transient browser/page failures by default:

```text
--payment-retries 2
```

Current retryable cases include:

```text
Stripe PayPal payment method was not found or could not be selected
Stripe amount not found
PayPal signup form did not become ready
ruyi/Firefox PageDisconnectedError
temporary proxy/page timeouts
```

DataDome `t=bv` is not treated as a normal retry. By default it fails fast.
If a temporary payment proxy is enabled, the orchestrator retries the current
payment stage once through that proxy. The temporary-proxy attempt does not
enter the normal retry loop. The browser uses a local HTTP bridge to reach the
configured upstream proxy, so authenticated SOCKS endpoints are handled outside
Firefox:

```text
PAYMENT_TEMP_PROXY_ENABLED=1
PAYMENT_TEMP_PROXY=proxy-host:port:user:pass(socks)
```

To start payment through a proxy immediately instead of waiting for direct
DataDome `t=bv`, enable the primary payment proxy:

```text
PAYMENT_PROXY_ENABLED=1
PAYMENT_PROXY=proxy-host:port:user:pass(http)
PAYMENT_PROXY_USE_BRIDGE=1
```

CLI equivalent:

```bash
./run_trial_payment_full_flow.sh ... --enable-payment-proxy
```

If `--payment-proxy` / `PAYMENT_PROXY` is empty, `--enable-payment-proxy`
reuses `PAYMENT_TEMP_PROXY` as the starting payment exit. When payment starts
through a proxy, `t=bv` is not retried again through the same temporary proxy.
Use `--no-payment-proxy-bridge` only when the ruyi/Firefox layer can consume
the proxy directly.

Disable it for a single run:

```bash
./run_trial_payment_full_flow.sh ... --disable-payment-temp-proxy
```

The following are treated as hard failures and do not retry:

```text
Stripe amount check failed
paypal_signup_not_advanced_after_sms
DataDome t=bv when PAYMENT_TEMP_PROXY is disabled or already used
```

This keeps the run fast: amount mismatches fail immediately; selector misses
and transient page/load issues stay retryable.

Disable retries:

```bash
./run_trial_payment_full_flow.sh ... --payment-retries 0
```

## Optional SQLite resource pool

Manual arguments still work as before. If you want the orchestrator to lease
resources from a persistent SQLite pool instead of typing them every run, pass
`--pool-db` and seed files as needed:

```bash
./run_trial_payment_full_flow.sh \
  --pool-db accfile/pool/full_flow.sqlite3 \
  --pool-seed-emails-file accfile/pool/emails.txt \
  --pool-seed-cards-file accfile/pool/cards.txt \
  --pool-seed-phones-file accfile/pool/phones.txt
```

Pool rules:

- email: lease from `main`, retryable failures move to `retry`, and `retry`
  gets promoted automatically when `main` is empty
- non-retryable or retry-exhausted emails move to `failed` instead of being
  deleted; operators can inspect the failed list and manually move them back to
  `retry` or `main`
- retry/failed email rows keep the last detailed failure reason and run id, so
  the web UI can jump directly to the relevant summary/protocol/payment logs
- card: one use, then consume/delete
- phone: round-robin lease/release; concurrent workers wait briefly for a
  reusable phone instead of failing immediately when the phone pool is
  temporarily leased
- non-zero Stripe amount failures are hard failures and do not retry
- other payment/protocol failures can re-enter the email retry pool

### Full-flow web UI

For quick operation from a browser:

```bash
./run_full_flow_web.sh \
  --pool-db accfile/pool/full_flow.sqlite3 \
  --host 0.0.0.0 \
  --port 8765
```

The page is served by `full_flow_web.py` with Python stdlib only. It stays
detachable: it starts `trial_payment_full_flow.py` or
`full_flow_queue_worker.py` by subprocess and does not import protocol/payment
logic. It can:

- start one full-flow run with manual inputs
- start pool workers for batch mode
- show child runs under a parent `webq_...` queue task, including worker, run
  id, email, status, duration, payment reason, and protocol/payment log links
- show recent `summary.json` results and log tails
- show email/card/phone availability and active leases
- filter/list pool rows
- seed values into email/card/phone pools
- promote retry emails back to main
- inspect failed emails, jump to the related run logs, move them back to the
  retry pool, or delete them from the failed pool
- delete stale rows
- add/delete proxy records
- test proxies for registration and payment reachability

API endpoints:

```text
GET  /api/health
GET  /api/overview
GET  /api/runs
GET  /api/queue-children?runId=<webq_id>
GET  /api/log?runId=<id>&file=payment.log
GET  /api/resource-items?kind=email&state=available&bucket=main&limit=200
GET  /api/proxies
POST /api/runs/start
POST /api/queue/start
POST /api/resource-seed
POST /api/resource-release
POST /api/resource-delete
POST /api/promote-retry-emails
POST /api/restore-failed-emails
POST /api/delete-failed-emails
POST /api/resource-restore-email
POST /api/proxies/add
POST /api/proxies/delete
POST /api/proxies/test
```

Proxy test rules are intentionally fast:

- registration test: egress geo probe plus `chatgpt.com` and
  `auth.openai.com` non-5xx reachability through the proxy
- payment test: egress geo probe, Stripe JS must return 200, and PayPal signin
  must be reachable with a non-5xx HTTP response

These tests catch broken/TLS/blocked exits quickly without spending a full
checkout run.

## Queue / concurrent workers

The queue wrapper keeps the single-run flow unchanged, but can drain the same
SQLite pool with multiple workers:

```bash
./run_full_flow_queue_worker.sh \
  --pool-db accfile/pool/full_flow.sqlite3 \
  --workers 3 \
  --max-runs 20 \
  --payment-headless
```

Each worker process still calls the same `trial_payment_full_flow.py`, so the
manual-argument mode remains available. The DB lease prevents two workers from
taking the same email/card/phone at the same time.

`--max-runs` is optional. When set, the queue starts at most that many child
runs across all workers, which is useful for smoke tests without draining the
whole pool.

This is true multi-task concurrency for protocol and orchestration:
`full_flow_queue_worker.py` creates one thread per worker, each thread runs an
independent orchestrator subprocess, and SQLite `BEGIN IMMEDIATE` leases
serialize only the short resource-acquire section.

Payment browser sessions are guarded by a small cross-process slot lock because
unbounded ruyi/Firefox headless concurrency can trigger BrowserConnectError on
the same server. The server config currently allows two payment browser slots,
while Firefox startup itself is still serialized by an internal startup lock:

```bash
FULL_FLOW_PAYMENT_BROWSER_SLOTS=2
FULL_FLOW_PAYMENT_SLOT_WAIT_INTERVAL=1
```

With this setup, protocol workers run concurrently and up to two workers can be
inside the payment stage at the same time. The short Firefox startup critical
section remains serialized, then both browsers may continue through Stripe /
PayPal concurrently. Set `FULL_FLOW_PAYMENT_BROWSER_SLOTS=0` only for explicit
stress testing, or lower it to `1` if the server starts producing Firefox
connection failures again.

## Payment headless mode

On the server deployment under `/opt/openaii`, payment defaults to headless so
Firefox startup is stable and fast. Local development still keeps the old
headed default unless you opt in.

To force the ruyi payment browser headless:

```bash
./run_trial_payment_full_flow.sh ... --payment-headless
```

Equivalent environment switch:

```bash
PAYMENT_HEADLESS=1
```

For visual debugging only, use the headed payment package:

```bash
# local machine: start remote Xvfb/x11vnc and tunnel VNC to localhost
SSHPASS='your-ssh-password' ./debug/headed_payment/connect_local.sh

# server: run full flow with DISPLAY=:99 and PAYMENT_HEADLESS=0
cd /opt/openaii
./debug/headed_payment/run_headed_payment.sh ...
```

Open VNC locally at `127.0.0.1:5901`. The VNC listener stays bound to
`127.0.0.1` on the server and is not exposed publicly.

## Output files

Each run writes:

```text
summary.json
protocol.log
protocol_results.jsonl
protocol_traces/
protocol_sessions/         # only when --enable-session-json is used
payment.log
payment_result.json
card.json                 # only when --card-line is used
web_session_result.json    # only when --enable-session-json is used
getrt_result.json         # only when --enable-getrt and payment succeeds
```

`summary.json` is the main machine-readable result.

Successful account txt output contains only:

```text
UTC_TIME<TAB>EMAIL
```

Default txt destinations are split by mailbox type:

```text
accfile/pwd/success_accounts.txt    # frimail/default
accfile/pwd/icsuccess_accounts.txt  # icloud
```
