# ai-daily-brief

每天早上 **北京时间 08:00** 自动推送一份「AI 圈重大新闻摘要」到**钉钉群 / 邮件 / 飞书 / 微信**。

- 数据源：官方一手（OpenAI、DeepMind、微软研究院）+ 一线媒体（TechCrunch、The Verge、量子位、36氪）
- 筛选：DeepSeek 按 rubric 打分，只留「重大事件」，每天 ≤5 条、同类 ≤2 条
- 形态：`[类别] 标题` + `一句话要点` + `来源（T1/T2）+ 原始链接`
- 成本：GitHub Actions 免费额度内 ¥0，DeepSeek 约 ¥0.5–1.5/月
- 依赖：零第三方 Python 包，只用标准库

---

## 一、部署（约 20 分钟）

### 1. 创建钉钉群机器人（想用邮件可跳过）

1. 钉钉里建一个群（可以只有自己/小号），点群设置 → **机器人** → **添加机器人** → **自定义**。
2. 安全设置勾选 **加签**，复制 `SEC` 开头的密钥；**不要选 IP 白名单**（GitHub 的出口 IP 不固定）。
   也可以选「自定义关键词」，关键词填 `AI 日报`（本程序每条消息都带这个词）。
3. 复制 Webhook，形如 `https://oapi.dingtalk.com/robot/send?access_token=xxxx`。

### 2. 配置 GitHub Secrets

仓库 → Settings → Secrets and variables → Actions → **New repository secret**：

| 名称 | 必填 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek 开放平台 API Key |
| `DINGTALK_WEBHOOK` | 钉钉必填 | 上一步复制的 Webhook |
| `DINGTALK_SECRET` | 钉钉加签必填 | `SEC` 开头的加签密钥；用关键词模式则不用配 |
| `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASS` `MAIL_TO` | 邮件必填 | 例：`smtp.qq.com` / `465` / 邮箱账号 / 授权码 / 收件地址 |

### 3. 选渠道

仓库 → Settings → Variables → Actions → **New repository variable**：

- 名称 `CHANNEL`，值 `dingtalk`（钉钉）、`email`（邮件）、`feishu`、`serverchan`
- 不配则默认 `email`

### 4. 验证

Actions → **ai-daily-brief** → **Run workflow**（手动触发会跳过等待、立即推送）。
收到消息即说明链路通；之后每天北京时间 07:50 触发、08:00 整发送。

---

## 二、时间机制（为什么不会晚）

- cron `50 23 * * *`（UTC）= 北京时间 **07:50**。提前 10 分钟，避开 GitHub 整点调度高峰。
- 脚本 `--at 08:00` 会 sleep 到 08:00 再发送，吸收几分钟排队延迟。
- 若已经过了 08:00 才启动（排队晚到），**立即发送，绝不顺延到第二天**。
- 中国无夏令时，全年不用改。

## 三、日常维护（每月 10 分钟）

```bash
python3 ai_daily_brief.py --check-feeds        # 巡检各源是否还活着
python3 ai_daily_brief.py --dry-run --fake-llm # 本地自检：抓源+渲染，不调模型、不推送
python3 ai_daily_brief.py --no-wait            # 本地立即生成并推送（需先配环境变量）
```

- 某天条数太多/太少：调 `MIN_SCORE`（默认 4）；太杂调到 4.5，太少降到 3.5。
- 某个源长期失败：把 `FEEDS` 里对应项的 `"enabled"` 改成 `False`。
- 换渠道：只改 `CHANNEL` 变量，代码不用动。

## 四、常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| 钉钉返回 `310000` | 关键词没命中 / 加签没配 | 检查 `DINGTALK_SECRET`，或消息里带关键词 |
| 钉钉返回 `130101` | 机器人被移出群 / Webhook 失效 | 重新添加机器人、更新 Secret |
| 邮件登录失败 | 用了登录密码而非 SMTP 授权码 | 去邮箱设置生成授权码 |
| 一条都没收到 | `DEEPSEEK_API_KEY` 没配或余额不足 | Actions 日志会打印具体错误 |
| 08:00 没收到但 Actions 有记录 | 发送渠道被拦截 | 看运行日志里的报错原文 |

## 五、文件说明

| 文件 | 作用 |
|---|---|
| `ai_daily_brief.py` | 主程序：抓取 → 去重 → 打分 → 限额 → 渲染 → 推送 |
| `.github/workflows/daily.yml` | 定时配置（UTC 23:50 触发） |
