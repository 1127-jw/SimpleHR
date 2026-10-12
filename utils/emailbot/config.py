"""配置层：集中管理邮件机器人的所有参数。

要点：
- IMAP 负责"收"（默认 993 + SSL），SMTP 负责"发"（默认 587 + STARTTLS）。
- password 填的是邮箱的【授权码】，不是网页登录密码。
  QQ 邮箱获取路径：设置 → 账号 → POP3/IMAP/SMTP → 开启并生成授权码。
- 用 pydantic 的 BaseModel 而不是普通 dataclass：构造时就完成类型转换与
  必填校验，少填或填错立刻报错（fail fast），不会拖到连不上邮箱才暴露。

注意：这里继承的是 BaseModel（不是 BaseSettings），所以【不会自动读环境变量】，
需要调用方自己 os.getenv 之后再传进来，用法见 main.py。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class MailSettings(BaseModel):
    # ---- IMAP：收件 ----
    # 993 = IMAP over SSL 标准端口，连上即加密，不需要额外握手升级
    imap_host: str = Field(..., description="IMAP 服务器，如 imap.qq.com")
    imap_port: int = Field(993)
    imap_ssl: bool = Field(True)
    # 邮箱目录名。"INBOX" 是收件箱；"已发送"/"垃圾邮件"等在各服务商下是自定义中文名
    imap_mailbox: str = Field("INBOX")

    # ---- SMTP：发件 ----
    # 587 = 邮件"提交端口"：先明文建连，再用 STARTTLS 升级为加密通道，
    # 比 465(隐式 SSL) 更通用，被墙/被限的情况更少
    smtp_host: str = Field(..., description="SMTP 服务器，如 smtp.qq.com")
    smtp_port: int = Field(587)
    smtp_starttls: bool = Field(True)
    # 465/994 端口用这个：一连上就是 TLS（网易系官方推荐）。
    # 连 163 设 smtp_port=465 + smtp_implicit_ssl=True + smtp_starttls=False；
    # QQ 保持默认（587 + STARTTLS）即可。两个模式互斥，别同时开。
    smtp_implicit_ssl: bool = Field(False, description="465 隐式 SSL 模式开关")

    # ---- 账号 ----
    # Field(...) 里的 Ellipsis 表示"必填"：不传就抛 ValidationError
    email: str = Field(..., description="邮箱账号")
    password: str = Field(..., description="授权码 / 应用专用密码")

    # ---- 业务参数（这几个字段课程版没有，是我们自己加的）----
    poll_interval: int = Field(15, description="轮询间隔（秒）")
    fetch_limit: int = Field(20, description="首次兜底拉取条数")
    attach_dir: str = Field("./attachments", description="附件（简历等）落盘目录")
    state_file: str = Field("./mailbot_state.json", description="游标+去重状态文件")
    auto_reply: bool = Field(True, description="命中关键词规则时是否自动回复")
    reply_text: str = Field("您的来信已收到，我们会尽快处理。")
