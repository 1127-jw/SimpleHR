# my_mailbot —— 邮件收发机器人

一个从零搭的异步邮件机器人，覆盖四项能力：**收件+解析、自动回复/主动发信、附件（简历）抓取、关键词监听+提醒**。

## 文件结构（分层）

| 层 | 文件 | 职责 |
|---|---|---|
| 配置 | `config.py` | `MailSettings`，pydantic 校验，必填 fail-fast |
| 数据 | `models.py` | `ParsedEmail` / `EmailAddress` / `Attachment` |
| 协议 | `imap_client.py` | 收件：`imaplib` + `to_thread` + `asyncio.Lock` + 断线自愈 |
| 协议 | `smtp_client.py` | 发件：`smtplib` + STARTTLS + 断连重试 |
| 解析 | `parser.py` | MIME → 结构化：表头解码、正文提取、**附件提取** |
| 状态 | `state.py` | 游标 + Message-ID 去重，落盘 JSON（原子写） |
| 规则 | `rules.py` | 关键词/附件条件 → 动作，`DEFAULT_RULES` 是招聘示例 |
| 门面 | `bot.py` | `MailBot`：`poll_once` / `send_email` / `reply` |
| 入口 | `main.py` | 轮询循环 + 提醒回调 |

## 跑起来

```powershell
$env:MAIL_USERNAME = "you@qq.com"
$env:MAIL_PASSWORD = "邮箱授权码"   # 不是登录密码
cd my_mailbot
pip install pydantic loguru
python main.py
```

QQ 邮箱需先在「设置 → 账号」里开启 IMAP/SMTP 服务并生成授权码。

## 关键设计决策

1. **为什么加锁**：`imaplib` 不是线程安全的，而 `to_thread` 每次可能换线程。课程版把 `mark_seen` 丢进 `create_task` 后与下一轮轮询并发访问同一连接，有踩乱协议流的风险。这里两个 client 都用 `asyncio.Lock` 串行化。
2. **游标为什么要落盘**：内存字典重启即丢，停机期间的邮件会被永久跳过。`state.py` 持久化 `last_uid`。
3. **为什么要 Message-ID 去重**：UID 游标只解决"哪些是新信"。一轮内回复失败、游标未推进时下轮会重来，导致同一候选人收到两封模板信。`seen_ids` 补上幂等。
4. **附件为什么不能跳**：简历几乎都是 PDF/DOCX 附件，课程版 `continue` 掉了。`parser._is_attachment` 额外覆盖 `inline+文件名` 和裸 `application/*` 两种常见形态。
5. **文件名为什么要清洗**：附件名来自外部不可信输入，`..\..\evil.pdf` 会写出目标目录。`safe_filename` 剥离路径与非法字符。

## 可扩展方向

- 附件字节 → 交给 PDF/DOCX 解析器抽文本，再喂 LLM 做候选人匹配打分
- `notify` 回调换成企业微信/钉钉 webhook，或写库后推前端
- 游标与去重从 JSON 换成 SQLite/Redis，支持多实例
- 用 IMAP `IDLE` 替代固定间隔轮询，降低延迟与请求数

## 常见报错

| 现象 | 原因 |
|---|---|
| `LOGIN failed` | 用了登录密码而非授权码；或 IMAP/SMTP 服务未开启 |
| 连接建立后立刻断 | 长期空闲被服务端踢掉，client 会自动重连；若反复失败请检查端口 993/587 是否被代理拦截 |
| 收到 `unknown-8bit` 乱码 | 已在 `decode_maybe` 里兜底处理 |
