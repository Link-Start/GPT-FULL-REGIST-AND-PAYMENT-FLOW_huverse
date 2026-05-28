# GPT Trial Protocol 使用和配置文档

## 1. 项目定位

本项目只做：

```text
ChatGPT 全协议注册/登录
-> 邮箱验证码
-> Stripe Hosted Checkout 试用链接生成
```

不做：

```text
PayPal 支付
手机接码
DataDome
浏览器自动化
设置密码
代理订阅管理
```

## 2. 环境要求

必须：

```bash
python3 --version
node --version
curl --version
```

建议：

```text
Python >= 3.10
Node.js >= 18
curl 可正常访问 HTTPS
一个稳定的本地 HTTP/SOCKS 代理端口
```

## 3. 安装

```bash
cd gpt_trial_protocol
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

验证：

```bash
gpt-trial --help
pytest -q
```

## 4. 最小启动参数

### 4.1 使用完整邮箱

```bash
gpt-trial run \
  --email example@icloud.com \
  --email-type icloud \
  --proxy http://127.0.0.1:7897
```

### 4.2 使用 frimail/edu 邮箱前缀

```bash
gpt-trial run \
  --email cuda \
  --email-type frimail \
  --proxy http://127.0.0.1:7897
```

实际邮箱会变成：

```text
cuda@cudaflowers.edu.kg
```

### 4.3 自动生成 frimail 邮箱

```bash
gpt-trial run \
  --generate-email \
  --email-type frimail \
  --proxy http://127.0.0.1:7897
```

默认生成规则：

```text
lu + HHMMSS + 4位小写字母数字
```

示例：

```text
lu15580473fi@cudaflowers.edu.kg
```

## 5. 参数说明

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--email` | 无 | 单个邮箱或邮箱前缀，可多次传 |
| `--emails` | 无 | 换行或分号分隔的邮箱列表 |
| `--emails-file` | 无 | 邮箱文件，一行一个 |
| `--generate-email` | false | 自动生成一个邮箱前缀 |
| `--generated-email-prefix` | `lu` | 自动邮箱前缀 |
| `--email-type` | `auto` | `auto` / `icloud` / `frimail` |
| `--email-code-provider` | `auto` | `auto` / `agiunx` / `frimail` |
| `--email-code-base-url` | 自动 | 自定义接码接口主域名 |
| `--proxy` | 无 | ChatGPT/Auth/Sentinel/Checkout 使用的代理 |
| `--login-existing` | false | 已注册账号登录模式 |
| `--checkout-country` | `US` | Checkout country |
| `--checkout-currency` | `USD` | Checkout currency |
| `--timeout` | `30` | HTTP 请求超时 |
| `--code-timeout` | `90` | 邮箱验证码等待超时 |
| `--backend` | `curl_cffi` | `curl_cffi` / `httpx` |
| `--trace-dir` | `runtime/traces` | 协议请求 trace |
| `--no-trace` | false | 关闭 trace |
| `--trace-sensitive` | false | trace 保留敏感内容，默认不要开 |
| `--out` | `runtime/results.jsonl` | 结果输出 |

## 6. 邮箱类型

### auto

必须输入完整邮箱：

```bash
gpt-trial run --email a@icloud.com --email-type auto --proxy http://127.0.0.1:7897
```

按域名自动选择接码接口。

### icloud

输入前缀会补齐：

```text
name -> name@icloud.com
```

默认接码：

```text
agiunx
```

### frimail

输入前缀会补齐：

```text
cuda -> cuda@cudaflowers.edu.kg
```

默认接码：

```text
frimail
```

## 7. 接码接口

### 7.1 agiunx

请求：

```http
GET https://agiunx.com/api/v1/extract?email={email}&refresh=1&limit=20
```

读取：

```text
latestCode
latest.date
```

### 7.2 frimail

请求：

```http
GET https://api.cudaflowers.edu.kg/v1/openai-code?recipient={recipient}
```

读取：

```text
code
receivedAt
```

404 会被视为“暂时没码”，继续轮询；429 会按普通临时错误处理，直到总超时。

## 8. 代理逻辑

本项目不管理代理订阅，也不启动 Mihomo/Clash。

你需要先准备一个本地代理端口，然后传入：

```bash
--proxy http://127.0.0.1:7897
```

这个代理会用于：

```text
chatgpt.com
auth.openai.com
sentinel.openai.com
backend-api/payments/checkout
```

接码接口默认不走代理：

```text
agiunx / frimail 直连
```

原因：接码属于外部辅助接口，代码使用 `trust_env=False`，避免被系统 `HTTP_PROXY/HTTPS_PROXY` 污染。

## 9. .env 启动方式

复制配置：

```bash
cp .env.example .env
```

编辑：

```bash
GPT_TRIAL_PROXY=http://127.0.0.1:7897
GPT_TRIAL_EMAIL_TYPE=frimail
```

自动生成邮箱并运行：

```bash
GPT_TRIAL_GENERATE_EMAIL=1 ./run_gpt_trial.sh
```

传入指定邮箱：

```bash
./run_gpt_trial.sh --email cuda --email-type frimail
```

## 10. 输出说明

结果文件：

```text
runtime/results.jsonl
```

成功生成 checkout：

```json
{
  "ok": true,
  "email": "xxx@cudaflowers.edu.kg",
  "stage": "checkout_link",
  "checkoutUrl": "https://pay.openai.com/c/pay/...",
  "checkoutSessionId": "cs_live_...",
  "processorEntity": "openai_llc"
}
```

## 11. 打包

```bash
bash scripts/package_release.sh
```

输出：

```text
dist/gpt-trial-protocol-YYYYMMDD-HHMMSS.tar.gz
```

打包会排除：

```text
.venv/
runtime/
dist/
.pytest_cache/
__pycache__/
*.pyc
```

## 12. 常见问题

### fresh email code not found

邮箱验证码没有收到或接口没有查到。检查：

```text
邮箱类型是否正确
接码接口是否可用
是否被限流
```

### Sentinel 失败

检查：

```bash
node --version
curl --version
```

也可以尝试换代理或改 backend：

```bash
--backend httpx
```

### TLS / curl 连接错误

通常是代理出口不稳。换代理节点后重试。
