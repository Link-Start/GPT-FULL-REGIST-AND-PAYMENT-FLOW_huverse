# GPT Trial Protocol Registrar

协议注册机负责通过协议方式完成 GPT 账号注册/登录，并输出 OpenAI hosted checkout URL。

公开版不内置任何私有邮箱验证码接口，也不会默认请求作者自用服务。开发者需要配置自己的验证码接口：

```bash
--email-code-base-url "https://your-email-code.example"
```

或在 `.env` 中配置：

```bash
GPT_TRIAL_EMAIL_CODE_BASE_URL=https://your-email-code.example
GPT_TRIAL_EMAIL_CODE_PROVIDER=auto
GPT_TRIAL_CUSTOM_EMAIL_DOMAIN=example-mail.invalid
```

## 安装

通常由仓库根目录的 `deploy_server.sh` 自动安装。单独安装：

```bash
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e '.[dev]'
```

## 运行

```bash
gpt-trial run \
  --email "user@example.com" \
  --email-type auto \
  --email-code-provider extract_json \
  --email-code-base-url "https://your-email-code.example" \
  --proxy "http://127.0.0.1:7897" \
  --out runtime/results.jsonl
```

也可以通过包装脚本运行：

```bash
cp .env.example .env
./run_gpt_trial.sh --email "user@example.com"
```

## 邮箱类型

| 类型 | 说明 |
| --- | --- |
| `auto` | 输入完整邮箱地址 |
| `icloud` | 输入 local-part 时自动补 `@icloud.com` |
| `custom` | 输入 local-part 时自动补 `GPT_TRIAL_CUSTOM_EMAIL_DOMAIN` |

## 验证码接口适配

公开版只保留通用 JSON 适配形态。

### `extract_json`

请求：

```text
GET <base>/api/v1/extract?email={email}&refresh=1&limit=20
```

响应：

```json
{
  "ok": true,
  "email": "user@example.com",
  "latestCode": "123456",
  "latest": {
    "date": "2026-01-01T00:00:00Z"
  }
}
```

### `openai_code_json`

请求：

```text
GET <base>/v1/openai-code?recipient={email}
```

响应：

```json
{
  "recipient": "user@example.com",
  "code": "123456",
  "receivedAt": "2026-01-01T00:00:00Z"
}
```

## 关键参数

| 参数 | 说明 |
| --- | --- |
| `--email` | 邮箱；可多次传入 |
| `--generate-email` | 生成一个 local-part |
| `--generated-email-prefix` | 生成邮箱前缀 |
| `--email-type` | `auto` / `icloud` / `custom` |
| `--email-code-provider` | `auto` / `extract_json` / `openai_code_json` |
| `--email-code-base-url` | 你的邮箱验证码服务根地址 |
| `--proxy` | 协议注册代理 |
| `--checkout-country` | checkout 国家，默认 `US` |
| `--checkout-currency` | checkout 币种，默认 `USD` |
| `--session-output-dir` | 可选输出 ChatGPT web session |
| `--out` | JSONL 输出文件 |

输出中会包含协议事件和 `checkoutUrl`。全流程编排器只接受 `/c/pay/cs_live...` 形式的 hosted checkout URL。
