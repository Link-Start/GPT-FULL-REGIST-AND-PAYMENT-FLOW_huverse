# getrt

独立的 Codex OAuth refresh-token 获取模块。

当前目标：

```text
OpenAI 邮箱登录协议
-> Codex OAuth PKCE authorize
-> /oauth/token
-> refresh_token
```

源码已纳入主仓库；`getrt/runtime/` 继续被 `.gitignore` 排除，避免 token 和 trace 运行产物进入 git。

## 最小运行

```bash
cd /path/to/GPT-FULL-REGIST-AND-PAYMENT-FLOW

python3 getrt/codex_oauth_getrt.py \
  --email 'name@cudaflowers.edu.kg' \
  --email-type frimail \
  --proxy 'http://127.0.0.1:7897' \
  --out getrt/runtime/name_codex_oauth.json
```

默认输出 CPA/CLIProxyAPI JSON。可选输出格式：

```bash
# 默认，等价于 --format cpa
--format cpa

# Sub2Api / ChatGPT-to-API 导入格式
--format sub2
# 或
--format sub2api

# getrt 通用 JSON，包含 ok/stage/access_token/refresh_token/id_token 等字段
--format codex
```

如果只验证 OAuth authorize 是否能全协议拿到 callback code，不交换 token：

```bash
python3 getrt/codex_oauth_getrt.py \
  --email 'name@cudaflowers.edu.kg' \
  --email-type frimail \
  --probe-only
```

## 设计边界

- 不 import 支付机。
- 不写入全流程账号文件。
- 只复用协议注册机的登录能力。
- 后续跑通后由全流程编排器以 CLI 方式调用，保持松耦合。

## 输出

默认 CPA 成功输出形态：

```json
{
  "access_token": "...",
  "account_id": "...",
  "email": "...",
  "expired": "...",
  "last_refresh": "...",
  "refresh_token": "...",
  "type": "codex"
}
```

CPA/Sub2 的字段提取逻辑仿照 `token转换.html`：

- `accessToken` / `access_token` / `token.accessToken` / `credentials.access_token`
- `refreshToken` / `refresh_token` / `token.refreshToken`
- `idToken` / `id_token` / `token.idToken`
- `sessionToken` / `session_token` / `token.sessionToken`
- `user.email` / `email` / `credentials.email` / JWT claims
- `account.id` / `account_id` / JWT auth claims

缺少真实 `id_token` 时会构造 CPA 占位 id_token；实际调用仍依赖 `access_token`。

`--format codex` 成功时 JSON 包含：

```json
{
  "ok": true,
  "stage": "token",
  "email": "...",
  "access_token": "...",
  "refresh_token": "...",
  "id_token": "...",
  "expires_at": "..."
}
```

失败时会写入 authorize probe 信息，必要时同时保存：

```text
getrt/runtime/traces/<email>/authorize_blocker.html
```

特殊情况：

```text
auth.openai.com/add-phone
```

表示邮箱登录/邮箱验证码已完成，但 OpenAI Auth 要求先绑定手机号才继续 OAuth callback。

如要继续纯协议推进 add-phone，提供手机号和短信验证码来源：

```bash
python3 getrt/codex_oauth_getrt.py \
  --email 'name@example.com' \
  --phone-line '+1xxxxxxxxxx----https://sms-api.example/record?token=...' \
  --out getrt/runtime/name_codex_oauth.json
```

也可以拆开传：

```bash
--phone-number '+1xxxxxxxxxx'
--phone-otp-api 'https://sms-api.example/record?token=...'
```

未提供手机号/OTP 来源时，脚本不按失败处理，退出码为 0，并按当前 `--format` 写出无 `refresh_token` 的 JSON：

```json
{
  "type": "codex",
  "email": "...",
  "refresh_token": "",
  "refresh_token_missing_reason": "add_phone_required",
  "requires_phone": true
}
```
