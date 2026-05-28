# Protocol Registrar Configuration

## 公开版接码接口说明

本项目公开版不内置任何私有邮箱验证码接口，也不默认请求任何作者自用服务。你需要提供自己的邮箱验证码服务，并通过以下任一方式配置：

```bash
--email-code-base-url "https://your-email-code.example"
```

或：

```bash
GPT_TRIAL_EMAIL_CODE_BASE_URL=https://your-email-code.example
```

## `.env` 示例

```bash
GPT_TRIAL_PROXY=direct
GPT_TRIAL_EMAIL_TYPE=auto
GPT_TRIAL_EMAIL_CODE_PROVIDER=auto
GPT_TRIAL_EMAIL_CODE_BASE_URL=https://your-email-code.example
GPT_TRIAL_CUSTOM_EMAIL_DOMAIN=example-mail.invalid
GPT_TRIAL_CHECKOUT_COUNTRY=US
GPT_TRIAL_CHECKOUT_CURRENCY=USD
GPT_TRIAL_TIMEOUT=30
GPT_TRIAL_CODE_TIMEOUT=90
GPT_TRIAL_BACKEND=curl_cffi
GPT_TRIAL_OUT=runtime/results.jsonl
GPT_TRIAL_TRACE_DIR=runtime/traces
```

## 邮箱类型

| 类型 | 行为 |
| --- | --- |
| `auto` | 必须输入完整邮箱 |
| `icloud` | 输入 `name` 时补成 `name@icloud.com` |
| `custom` | 输入 `name` 时补成 `name@${GPT_TRIAL_CUSTOM_EMAIL_DOMAIN}` |

## 验证码 Provider

| Provider | 接口形态 |
| --- | --- |
| `auto` | 根据邮箱类型选择通用形态 |
| `extract_json` | `GET <base>/api/v1/extract?email=...&refresh=1&limit=20` |
| `openai_code_json` | `GET <base>/v1/openai-code?recipient=...` |

### `extract_json` 响应

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

### `openai_code_json` 响应

```json
{
  "recipient": "user@example.com",
  "code": "123456",
  "receivedAt": "2026-01-01T00:00:00Z"
}
```

## 运行示例

```bash
./run_gpt_trial.sh \
  --email "user@example.com" \
  --email-type auto \
  --email-code-provider extract_json \
  --email-code-base-url "https://your-email-code.example"
```

生成 custom 邮箱 local-part：

```bash
GPT_TRIAL_CUSTOM_EMAIL_DOMAIN=example-mail.invalid \
./run_gpt_trial.sh \
  --generate-email \
  --email-type custom \
  --email-code-provider openai_code_json \
  --email-code-base-url "https://your-email-code.example"
```
