# GPT-FULL-REGIST AND PAYMENT-FLOW

Open-source snapshot of the current full-flow orchestration project.

This public snapshot intentionally excludes runtime data, account pools,
payment cards, SMS records, private proxy credentials, and local deployment
secrets. Copy `full_flow.env.example` to `full_flow.env` and fill your own
environment-specific values before running.

The fingerprint Firefox archive is not committed to the public repository.
Place your own compatible archive under `firefox-fingerprintBrowser/downloads/`
before running `deploy_server.sh`.

This repository contains the loose full-flow integration:

```text
protocol/gpt_trial_protocol  -> protocol registration + checkout URL
ruyipage                    -> ruyiPage Firefox payment automation
trial_payment_full_flow.py  -> CLI orchestrator
```

The two main projects stay independent. The orchestrator only connects them
through command-line contracts:

```text
gpt-trial emits checkoutUrl
ruyi_paypal_flow.py consumes --start-url
```

For batch/queue mode, use `run_full_flow_queue_worker.sh` on top of the same
SQLite-backed pool DB; the single-run manual mode still works unchanged.
For browser operation, use `run_full_flow_web.sh`.

## Server setup

```bash
git clone <private-repo-url>
cd <repo>
./deploy_server.sh
```

Then edit:

```text
full_flow.env
ruyipage/.env
```

On a plain Linux server, prefer explicit proxy values in `ruyipage/.env`:

```text
RUYI_PROXY_CHAIN_UPSTREAM=<proxy-url>
RUYI_PROXY_CHAIN_VIA=direct
APIKEY_2CAPTCHA=<2captcha-key>
```

Payment remains direct by default. To retry only DataDome `t=bv` failures
through a temporary US exit, set in `full_flow.env`:

```text
PAYMENT_TEMP_PROXY_ENABLED=1
PAYMENT_TEMP_PROXY=proxy-host:port:user:pass(socks)
```

To start payment through the configured proxy immediately:

```text
PAYMENT_PROXY_ENABLED=1
PAYMENT_PROXY=proxy-host:port:user:pass(http)
PAYMENT_PROXY_USE_BRIDGE=1
```

## Run

```bash
./run_trial_payment_full_flow.sh \
  --generate-email \
  --email-type frimail \
  --card-line 'CARD|MM|YYYY|CVV' \
  --sms-line '+1xxxxxxxxxx|https://sms-api'
```

Output is written under:

```text
runtime/full_flow/<run-id>/
```

See `FULL_FLOW_INTEGRATION.md` for more details.

## Headed payment debugging

For visual payment debugging, use the small package under
`debug/headed_payment/`:

```bash
SSHPASS='your-ssh-password' ./debug/headed_payment/connect_local.sh
```

Then open VNC at `127.0.0.1:5901` and run the headed server wrapper:

```bash
cd /opt/openaii
./debug/headed_payment/run_headed_payment.sh ...
```

## Full-flow web UI

```bash
./run_full_flow_web.sh \
  --pool-db accfile/pool/full_flow.sqlite3 \
  --host 0.0.0.0 \
  --port 8765
```

Open `http://SERVER_IP:8765/`. The UI is stdlib-only and keeps the same loose
boundary: it launches the orchestrator/queue worker by subprocess. It supports
single runs, pool workers, run log viewing, resource pool edits, failed-email
review/restore/delete, and proxy pool add/delete/tests.

For bounded concurrent smoke tests, set the queue form's `最大任务数`, or pass
`--max-runs N` to `full_flow_queue_worker.py`; child runs are shown under the
parent `webq_...` task with direct links to protocol/payment logs.

Queue workers keep protocol stages concurrent. Payment stages are limited by
`FULL_FLOW_PAYMENT_BROWSER_SLOTS` and the server currently uses two payment
slots; Firefox startup is still locked briefly to avoid ruyi/Firefox headless
connection failures.
