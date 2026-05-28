# GPT Trial Protocol

这是从主注册机项目中分离出来的精简工程，只保留两件事：

1. **全协议 ChatGPT 账号注册/登录**
2. **Stripe Hosted Checkout 试用链接生成**

不包含：

- PayPal 支付
- DataDome / PayPal 验证
- 手机接码
- 指纹浏览器
- WebUI
- 设置密码流程

## 安装

```bash
cd gpt_trial_protocol
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

运行 Sentinel token 生成需要本机有：

```bash
node --version
curl --version
```

详细配置、`.env` 启动、打包交付见：

```text
CONFIGURATION.md
```

## 单账号运行

```bash
gpt-trial run \
  --email example@icloud.com \
  --email-type icloud \
  --proxy http://127.0.0.1:7890 \
  --out runtime/results.jsonl
```

如果邮箱已经注册过，只想登录并生成试用链接：

```bash
gpt-trial run \
  --email example@icloud.com \
  --email-type icloud \
  --login-existing \
  --proxy http://127.0.0.1:7890 \
  --out runtime/results.jsonl
```

成功时会输出：

```json
{
  "ok": true,
  "email": "example@icloud.com",
  "stage": "checkout_link",
  "checkoutUrl": "https://pay.openai.com/c/pay/...",
  "checkoutSessionId": "cs_live_...",
  "processorEntity": "openai_ie"
}
```

## 批量运行

邮箱文件一行一个，支持 `email` 或 `email|备注密码` 格式。

```bash
gpt-trial run \
  --emails-file emails.txt \
  --proxy http://127.0.0.1:7890 \
  --out runtime/results.jsonl
```

## 关键配置

| 参数 | 说明 |
| --- | --- |
| `--proxy` | ChatGPT/Auth/Sentinel 使用的 HTTP/SOCKS 代理，建议使用 JP 出口 |
| `--email-type` | 邮箱类型：`auto` / `icloud` / `frimail`，默认 `auto` |
| `--email-code-provider` | 接码接口类型：`auto` / `agiunx` / `frimail`，默认 `auto` |
| `--email-code-base-url` | 接码接口主域名；不填时 `agiunx` 用 `https://agiunx.com`，`frimail` 用 `https://api.cudaflowers.edu.kg` |
| `--login-existing` | 已注册账号登录模式 |
| `--checkout-country` | Checkout billing country，默认 `US` |
| `--checkout-currency` | Checkout currency，默认 `USD` |
| `--backend` | HTTP backend，默认 `curl_cffi` |
| `--trace-dir` | 保存协议请求 trace |
| `--trace-sensitive` | trace 中保留敏感 header/body，默认关闭 |

## 流程边界

本项目到生成试用链接为止：

```text
邮箱注册/登录 -> accessToken -> checkout link
```

生成链接之后的支付流程由其他项目处理。

## 接码接口兼容

当前兼容两种邮箱验证码接口。

### 1. agiunx

iCloud 等普通邮箱默认走这个接口：

```http
GET https://agiunx.com/api/v1/extract?email={email}&refresh=1&limit=20
```

读取字段：

```text
latestCode
latest.date
```

### 2. frimail / cudaflowers

`*@cudaflowers.edu.kg` 会在 `--email-code-provider auto` 下自动切到这个接口：

```http
GET https://api.cudaflowers.edu.kg/v1/openai-code?recipient={recipient}
```

读取字段：

```text
code
receivedAt
```

也可以强制指定：

```bash
gpt-trial run \
  --email cuda \
  --email-type frimail \
  --proxy http://127.0.0.1:7890
```

自动生成一个 frimail 邮箱前缀：

```bash
gpt-trial run \
  --generate-email \
  --email-type frimail \
  --proxy http://127.0.0.1:7890
```

默认生成规则：

```text
lu + HHMMSS + 4位小写字母数字
```

邮箱类型说明：

```text
--email-type auto     输入必须是完整邮箱；按域名自动选择接码接口
--email-type icloud   输入 name 会自动补成 name@icloud.com；接码默认 agiunx
--email-type frimail  输入 cuda 会自动补成 cuda@cudaflowers.edu.kg；接码默认 frimail
```
