# Headed payment debug package

This package is only for payment-stage visual debugging. Normal production runs
should keep the server default: direct + headless.

## 1. Open the local VNC tunnel

Run this on the local development machine:

```bash
SSHPASS='your-ssh-password' ./debug/headed_payment/connect_local.sh
```

Then open a VNC client at:

```text
127.0.0.1:5901
```

The VNC server is bound to `127.0.0.1` on the server and is only reachable
through the SSH tunnel.

Useful overrides:

```bash
DEBUG_HOST=your.server.ip
DEBUG_USER=root
DEBUG_LOCAL_VNC_PORT=5901
DEBUG_REMOTE_VNC_PORT=5901
DEBUG_DISPLAY_ID=99
DEBUG_PROJECT_DIR=/opt/openaii
```

## 2. Run the full flow with headed payment

Run this on the server:

```bash
cd /opt/openaii
./debug/headed_payment/run_headed_payment.sh \
  --email "xxx@icloud.com" \
  --email-type icloud \
  --email-code-provider agiunx \
  --card-line "CARD MM YYYY CVV" \
  --sms-line "+1xxxx----https://sms-api" \
  --enable-session-json \
  --session-json-format cpa
```

The wrapper starts the virtual desktop, then runs:

```bash
DISPLAY=:99 PAYMENT_HEADLESS=0 ./run_trial_payment_full_flow.sh --keep-browser-open ...
```

## Stop server desktop

```bash
cd /opt/openaii
./debug/headed_payment/server_desktop.sh stop
```
