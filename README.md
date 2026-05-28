# GPT-FULL-REGIST AND PAYMENT-FLOW

> 中文说明为主，English guide follows.

## 中文说明

`GPT-FULL-REGIST AND PAYMENT-FLOW` 是一个完整的全流程自动化项目，用于把以下阶段串成一条可运行、可观测、可并发的流水线：

```text
协议注册 / 登录
  -> 生成 OpenAI hosted checkout URL
  -> Stripe hosted checkout
  -> PayPal 支付 / 账户创建 / SMS OTP
  -> 成功账号归档
  -> 可选 session-json 导出
  -> 可选 Codex OAuth getrt / refresh_token 导出
```

本仓库包含完整运行所需的主流程代码、协议注册机、支付自动化、队列并发、资源池、代理池、Web 管理面板和调试工具。运行时账号、卡、短信接口、代理凭据、浏览器内核压缩包等私有数据不会提交到仓库，需要使用者按自己的环境配置。

### 核心设计

项目保持松耦合：

```text
protocol/gpt_trial_protocol  # 协议注册机，负责登录/注册并生成 checkoutUrl
ruyipage                    # ruyiPage Firefox 支付自动化，负责 Stripe/PayPal 支付
getrt                       # Codex OAuth getrt，可选导出 refresh_token
trial_payment_full_flow.py  # 全流程编排器
full_flow_web.py            # Web 管理面板
full_flow_queue_worker.py   # 队列/并发 worker
```

边界规则：

- 协议机不 import 支付机。
- 支付机不 import 协议机。
- getrt 通过 subprocess 调用，不强耦合进支付逻辑。
- 编排器只接受真正的支付链接：`/c/pay/cs_live...`。
- `/p/session/live...` 等非 hosted checkout 链接会被拒绝。
- 支付阶段默认 direct；只有明确配置代理时才使用代理。
- DataDome `t=bv` 被视为 blocked verdict，不当作滑块继续拖拽。

### 目录结构

```text
.
├── trial_payment_full_flow.py        # 单账号全流程编排器
├── run_trial_payment_full_flow.sh    # 单账号入口
├── full_flow_web.py                  # Web UI
├── run_full_flow_web.sh              # Web UI 入口
├── full_flow_queue_worker.py         # 并发队列 worker
├── run_full_flow_queue_worker.sh     # 并发入口
├── full_flow_pool.py                 # SQLite 资源池：email/card/phone
├── full_flow_proxy_pool.py           # 代理池
├── full_flow_concurrency_goal_runner.py
├── protocol/gpt_trial_protocol/      # 协议注册机
├── ruyipage/                         # ruyiPage + PayPal/Stripe 支付自动化
├── getrt/                            # Codex OAuth getrt
├── debug/headed_payment/             # 有头调试/VNC 工具
├── firefox-fingerprintBrowser/       # 指纹 Firefox 放置目录
├── full_flow.env.example             # 全流程配置模板
└── FULL_FLOW_INTEGRATION.md          # 集成细节说明
```

### 运行前准备

#### 系统依赖

建议环境：

- Linux 服务器
- Python 3.10+
- Node.js
- curl
- git
- tar/xz
- 可访问目标站点的网络环境
- 支持短信 OTP 查询的 SMS API
- 可用于注册阶段的邮箱/接码接口
- 可用于支付阶段的卡信息
- 可选：2Captcha API key

安装基础依赖示例：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nodejs npm curl git xz-utils
```

#### 指纹 Firefox

支付自动化使用 ruyiPage 配合指纹 Firefox。浏览器压缩包体积较大，不随仓库提交。

把兼容的 Linux Firefox 指纹浏览器压缩包放到：

```text
firefox-fingerprintBrowser/downloads/
```

文件名格式示例：

```text
firefox-xxx.linux-x86_64.tar.xz
```

`deploy_server.sh` 会自动解压到：

```text
firefox-fingerprintBrowser/browser/
```

### 安装

```bash
git clone https://github.com/huverse/GPT-FULL-REGIST-AND-PAYMENT-FLOW.git
cd GPT-FULL-REGIST-AND-PAYMENT-FLOW

./deploy_server.sh
```

脚本会创建：

```text
protocol/gpt_trial_protocol/.venv
ruyipage/.venv
full_flow.env
ruyipage/.env
```

首次安装后需要编辑配置：

```bash
nano full_flow.env
nano ruyipage/.env
```

### 配置

#### `full_flow.env`

复制自：

```bash
cp full_flow.env.example full_flow.env
```

常用配置：

```bash
# 协议注册代理。没有代理时可设 direct。
GPT_TRIAL_PROXY=direct

# 可选：资源池数据库
FULL_FLOW_POOL_DB=accfile/pool/full_flow.sqlite3

# Web UI
FULL_FLOW_WEB_HOST=0.0.0.0
FULL_FLOW_WEB_PORT=8765

# 支付浏览器并发槽位
FULL_FLOW_PAYMENT_BROWSER_SLOTS=2

# 可选：DataDome t=bv 后使用一次临时支付代理
PAYMENT_TEMP_PROXY_ENABLED=0
PAYMENT_TEMP_PROXY=proxy-host:port:user:pass(socks)

# 可选：支付阶段一开始就使用代理
PAYMENT_PROXY_ENABLED=0
PAYMENT_PROXY=proxy-host:port:user:pass(http)
PAYMENT_PROXY_USE_BRIDGE=1
```

#### `ruyipage/.env`

复制自：

```bash
cp ruyipage/.env.example ruyipage/.env
```

常用配置：

```bash
# 可选 2Captcha。默认流程不强制使用。
APIKEY_2CAPTCHA=

# 可选 ruyi 代理链
RUYI_PROXY_CHAIN_UPSTREAM=
RUYI_PROXY_CHAIN_VIA=direct

# 可选 Firefox 路径覆盖
RUYI_FIREFOX_PATH=
```

#### 协议机配置

协议机位于：

```text
protocol/gpt_trial_protocol/
```

可参考：

```text
protocol/gpt_trial_protocol/.env.example
protocol/gpt_trial_protocol/CONFIGURATION.md
```

全流程通常由 `trial_payment_full_flow.py` 传参调用协议机，不需要单独运行协议机。

### 单账号运行

#### 使用已有邮箱

```bash
./run_trial_payment_full_flow.sh \
  --email "user@example.com" \
  --email-type "icloud" \
  --email-code-provider "agiunx" \
  --card-line "4111 1111 1111 1111 02/30 123" \
  --sms-line "+1xxxxxxxxxx----https://sms-api.example/path"
```

#### 自动生成邮箱

```bash
./run_trial_payment_full_flow.sh \
  --generate-email \
  --email-type "frimail" \
  --email-code-provider "frimail" \
  --card-line "4111 1111 1111 1111 02/30 123" \
  --sms-line "+1xxxxxxxxxx----https://sms-api.example/path"
```

#### 导出 session-json

```bash
./run_trial_payment_full_flow.sh \
  --email "user@example.com" \
  --email-type "icloud" \
  --email-code-provider "agiunx" \
  --card-line "4111 1111 1111 1111 02/30 123" \
  --sms-line "+1xxxxxxxxxx----https://sms-api.example/path" \
  --enable-session-json \
  --session-json-format cpa
```

#### 导出 getrt / refresh_token

```bash
./run_trial_payment_full_flow.sh \
  --email "user@example.com" \
  --email-type "icloud" \
  --email-code-provider "agiunx" \
  --card-line "4111 1111 1111 1111 02/30 123" \
  --sms-line "+1xxxxxxxxxx----https://sms-api.example/path" \
  --enable-getrt \
  --getrt-output-format cpa
```

如果 OAuth 需要补手机号，可启用 add-phone 路线：

```bash
./run_trial_payment_full_flow.sh \
  --email "user@example.com" \
  --email-type "icloud" \
  --email-code-provider "agiunx" \
  --card-line "4111 1111 1111 1111 02/30 123" \
  --sms-line "+1xxxxxxxxxx----https://payment-sms-api.example/path" \
  --enable-getrt \
  --enable-getrt-add-phone \
  --getrt-phone-line "+1yyyyyyyyyy----https://oauth-sms-api.example/path"
```

### 输出文件

每次运行会生成独立目录：

```text
runtime/full_flow/<run_id>/
```

常见文件：

```text
summary.json
protocol.log
protocol_results.jsonl
payment.log
payment_result.json
webui.log                  # 仅 Web 父任务存在
getrt.log                  # 启用 getrt 时存在
getrt_result.json          # 启用 getrt 时存在
web_session_result.json    # 启用 session-json 时存在
```

成功账号写入：

```text
accfile/pwd/icsuccess_accounts.txt
```

内容只包含邮箱地址，一行一个：

```text
user@example.com
```

session-json 输出：

```text
accfile/session_json/<email>.json
```

getrt 输出：

```text
accfile/json/<email>.json
```

默认 `cpa` 格式包含：

```json
{
  "access_token": "...",
  "account_id": "...",
  "email": "user@example.com",
  "expired": "...",
  "last_refresh": "...",
  "refresh_token": "...",
  "type": "codex"
}
```

### Web UI

启动：

```bash
./run_full_flow_web.sh \
  --pool-db accfile/pool/full_flow.sqlite3 \
  --host 0.0.0.0 \
  --port 8765
```

访问：

```text
http://SERVER_IP:8765/
```

Web UI 支持：

- 单账号全流程启动
- 资源池管理
- 失败/预备邮箱管理
- 批量删除/批量转重试
- 代理池管理
- 注册代理测试
- 支付代理测试
- 任务列表
- 子任务列表
- 实时日志刷新
- 动态日志文件列表

### 资源池

资源池使用 SQLite：

```text
accfile/pool/full_flow.sqlite3
```

资源类型：

```text
email  # GPT 邮箱
card   # 支付卡
phone  # 支付短信手机号/API
```

邮箱池状态：

```text
main    # 主池
retry   # 预备/重试池
failed  # 失败池
```

拒绝卡类错误会进入 retry，下次重试会：

- 使用同一个 GPT 邮箱
- 换卡
- 重新生成 PayPal signup 邮箱
- 不强制换手机号

这类策略用于避免同一张卡或同一 PayPal funding path 反复失败。

### 并发队列

启动 2 并发、最多跑 2 个：

```bash
./run_full_flow_queue_worker.sh \
  --pool-db accfile/pool/full_flow.sqlite3 \
  --workers 2 \
  --max-runs 2
```

目标成功数模式：

```bash
./run_full_flow_queue_worker.sh \
  --pool-db accfile/pool/full_flow.sqlite3 \
  --workers 3 \
  --success-target 3 \
  --max-runs 6
```

说明：

- 协议阶段按 worker 并发。
- 支付阶段受 `FULL_FLOW_PAYMENT_BROWSER_SLOTS` 限制。
- Firefox 启动阶段有短锁，避免并发启动导致浏览器连接失败。
- 如果 `success-target` 未达成，worker 返回非 0。

### 有头调试

调试包位于：

```text
debug/headed_payment/
```

本地启动 VNC 隧道：

```bash
SSHPASS='your-ssh-password' ./debug/headed_payment/connect_local.sh
```

本地 VNC Viewer 连接：

```text
127.0.0.1:5901
```

服务器有头运行：

```bash
./debug/headed_payment/run_headed_payment.sh \
  --email "user@example.com" \
  --email-type icloud \
  --email-code-provider agiunx \
  --card-line "4111 1111 1111 1111 02/30 123" \
  --sms-line "+1xxxxxxxxxx----https://sms-api.example/path"
```

停止虚拟桌面：

```bash
./debug/headed_payment/server_desktop.sh stop
```

### 成功判定

支付成功只认明确成功信号：

```text
pm-redirects.stripe.com/return/...status=success
pay.openai.com/...redirect_status=succeeded
pay.openai.com/...returned_from_redirect=true
chatgpt.com/payments/success
```

协议阶段如果返回：

```text
User is already paid
```

会被视为幂等成功：

```text
status=success
reason=already_paid
```

这表示 OpenAI 后端已经确认账号处于 paid 状态。

### DataDome / CAPTCHA 策略

默认策略：

```text
CSS bypass
  -> 本地 ruyi/DDC slider
  -> 只有显式启用时才使用 2Captcha
```

如果 DataDome iframe URL 出现：

```text
t=bv
```

表示 blocked verdict。当前 IP/session 已被阻断，不会继续盲拖滑块。

如果配置了：

```bash
PAYMENT_TEMP_PROXY_ENABLED=1
PAYMENT_TEMP_PROXY=...
```

支付阶段 direct 遇到 `t=bv` 后会通过临时支付代理重试一次。

### 常见问题

#### `webui.log not found`

`webui.log` 只存在于 Web 启动器父任务。CLI 任务或队列子任务通常只有：

```text
summary.json
protocol.log
payment.log
payment_result.json
```

Web UI 会动态显示实际存在的日志。

#### `Stripe amount check failed`

表示 Stripe 页面金额不是 0，例如 `$20.00`。这是硬失败，不会重试同一支付阶段。

#### `BrowserConnectError`

通常是 Firefox/ruyi backend 启动连接失败。队列模式下建议设置：

```bash
FULL_FLOW_PAYMENT_BROWSER_SLOTS=1
```

或逐步提高到 2/3，并观察日志。

#### `DataDome t=bv`

这是 IP/session blocked verdict，不是滑块识别问题。需要更换支付出口或启用临时支付代理。

### 开发者约定

推荐改动原则：

- 先看日志和证据，再改代码。
- 只改真实失败层。
- 不增加无意义长等待。
- 不用宽松成功文本判断代替明确支付回跳。
- 协议、支付、getrt 保持 subprocess 边界。

本地检查：

```bash
python3 -m py_compile \
  trial_payment_full_flow.py \
  full_flow_web.py \
  full_flow_pool.py \
  full_flow_proxy_pool.py \
  full_flow_queue_worker.py \
  getrt/codex_oauth_getrt.py \
  ruyipage/ruyi_paypal_flow.py
```

---

## English Guide

`GPT-FULL-REGIST AND PAYMENT-FLOW` is a complete full-flow automation project that connects:

```text
Protocol registration / login
  -> OpenAI hosted checkout URL generation
  -> Stripe hosted checkout
  -> PayPal payment / signup / SMS OTP
  -> success account archival
  -> optional session-json export
  -> optional Codex OAuth getrt / refresh_token export
```

The repository contains the runnable orchestrator, protocol registrar, payment automation, queue worker, resource pool, proxy pool, Web UI, and headed debugging tools. Runtime data, account pools, cards, SMS records, proxy credentials, and local secrets are intentionally not committed. You must provide your own environment-specific data.

### Architecture

The project is intentionally loosely coupled:

```text
protocol/gpt_trial_protocol  # protocol registrar, emits checkoutUrl
ruyipage                    # ruyiPage Firefox payment automation
getrt                       # optional Codex OAuth refresh_token exporter
trial_payment_full_flow.py  # orchestrator
full_flow_web.py            # Web console
full_flow_queue_worker.py   # queue/concurrency worker
```

Rules:

- The protocol registrar does not import the payment module.
- The payment module does not import the protocol registrar.
- getrt is invoked through subprocess.
- The orchestrator accepts only real hosted checkout URLs: `/c/pay/cs_live...`.
- Payment is direct by default; payment proxies are opt-in.
- DataDome `t=bv` is treated as a blocked verdict, not a slider challenge.

### Setup

Install basic system dependencies:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nodejs npm curl git xz-utils
```

Clone and install:

```bash
git clone https://github.com/huverse/GPT-FULL-REGIST-AND-PAYMENT-FLOW.git
cd GPT-FULL-REGIST-AND-PAYMENT-FLOW
./deploy_server.sh
```

Before running payment automation, place a compatible Linux fingerprint Firefox archive under:

```text
firefox-fingerprintBrowser/downloads/
```

Then edit:

```text
full_flow.env
ruyipage/.env
```

### Single Run

```bash
./run_trial_payment_full_flow.sh \
  --email "user@example.com" \
  --email-type "icloud" \
  --email-code-provider "agiunx" \
  --card-line "4111 1111 1111 1111 02/30 123" \
  --sms-line "+1xxxxxxxxxx----https://sms-api.example/path"
```

Enable session-json:

```bash
--enable-session-json --session-json-format cpa
```

Enable getrt:

```bash
--enable-getrt --getrt-output-format cpa
```

Enable OAuth add-phone for getrt:

```bash
--enable-getrt-add-phone \
--getrt-phone-line "+1yyyyyyyyyy----https://oauth-sms-api.example/path"
```

### Outputs

Per-run files:

```text
runtime/full_flow/<run_id>/
```

Important files:

```text
summary.json
protocol.log
protocol_results.jsonl
payment.log
payment_result.json
getrt.log
web_session_result.json
```

Successful GPT account emails are appended to:

```text
accfile/pwd/icsuccess_accounts.txt
```

One email per line only:

```text
user@example.com
```

session-json:

```text
accfile/session_json/<email>.json
```

getrt output:

```text
accfile/json/<email>.json
```

### Web UI

```bash
./run_full_flow_web.sh \
  --pool-db accfile/pool/full_flow.sqlite3 \
  --host 0.0.0.0 \
  --port 8765
```

Open:

```text
http://SERVER_IP:8765/
```

The Web UI supports:

- single full-flow runs
- queue/concurrent runs
- resource pool management
- failed/retry email management
- proxy pool tests
- live logs
- dynamic log file list per run

### Queue Mode

Two workers, two runs:

```bash
./run_full_flow_queue_worker.sh \
  --pool-db accfile/pool/full_flow.sqlite3 \
  --workers 2 \
  --max-runs 2
```

Three successes target:

```bash
./run_full_flow_queue_worker.sh \
  --pool-db accfile/pool/full_flow.sqlite3 \
  --workers 3 \
  --success-target 3 \
  --max-runs 6
```

Protocol stages are concurrent. Payment browsers are limited by `FULL_FLOW_PAYMENT_BROWSER_SLOTS`, and Firefox startup is briefly locked to avoid headless backend connection failures.

### Success Signals

Payment success is decided by explicit redirect signals only:

```text
pm-redirects.stripe.com/return/...status=success
pay.openai.com/...redirect_status=succeeded
pay.openai.com/...returned_from_redirect=true
chatgpt.com/payments/success
```

If the protocol stage returns `User is already paid`, it is treated as idempotent success:

```text
status=success
reason=already_paid
```

### Development Notes

Recommended checks:

```bash
python3 -m py_compile \
  trial_payment_full_flow.py \
  full_flow_web.py \
  full_flow_pool.py \
  full_flow_proxy_pool.py \
  full_flow_queue_worker.py \
  getrt/codex_oauth_getrt.py \
  ruyipage/ruyi_paypal_flow.py
```

Operational principles:

- inspect logs before changing code
- fix the actual failing layer only
- avoid broad fallback logic
- avoid long waits that slow the whole flow
- keep protocol/payment/getrt subprocess boundaries intact
