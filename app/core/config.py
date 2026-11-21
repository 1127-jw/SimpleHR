"""应用配置（Pydantic Settings）。

所有配置项集中在 ``Settings`` 类中，按以下优先级取值：
1. 实例化时传入的参数：Settings(debug=True) 优先级最高
2. 进程环境变量（适合容器 / CI / 生产部署）
3. 项目根目录的 ``.env`` 文件（适合本地开发，模板见 ``.env.example``）
4. 类字段默认值（开箱即用）

用法::

    from app.core.config import settings

    print(settings.db_url, settings.default_chunk_size)

设计说明
--------
* 字段名小写（如 ``db_url``），对应环境变量名自动为大写（``DB_URL``），
  且大小写不敏感，行为与旧的 ``os.getenv("DB_URL")`` 完全一致。
* 派生值用 ``@property`` 暴露（``cors_origin_list`` / ``saq_redis_url`` ），避免同一份配置维护两遍。
* ``TORTOISE_ORM`` 仍以「模块级常量」导出：Aerich 与 SAQ worker 需要一个能用
  字符串引用的配置对象（``app.core.config.TORTOISE_ORM``）。
"""
import os
from pathlib import Path
from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------- 项目路径 ----------
# config.py -> core -> app -> 项目根目录（后端工程根，前端是其同级目录）
BASE_DIR = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    """全部应用配置。

        环境变量名 = 字段名大写，例如 ``db_url`` <-> ``DB_URL``（大小写不敏感）。
    """

    # ---------- 应用元信息 ----------
    app_name: str = Field(default="MyApp", description="应用名称")
    version: str = Field(default="0.1.0", description="应用版本")
    environment: str = Field(default="development", description="运行环境")
    debug: bool = Field(default=False, description="是否开启调试模式")

    api_prefix: str = Field(default="/api", description="API 路由前缀")


    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",  # 本地开发：自动读取项目根目录的 .env
        env_file_encoding="utf-8",
        case_sensitive=False,  # DB_URL / db_url 均可
        extra="ignore",  # 忽略 .env 中无关变量，不报错
    )

    # ---------- 数据库 ----------
    # 开发环境使用 SQLite（基于文件，无需服务器）：sqlite://db.sqlite3
    # 生产环境示例：
    #   PostgreSQL -> postgres://user:password@localhost:5432/dbname
    #   MySQL      -> mysql://root:password@127.0.0.1:3306/oaapi
    DB_USERNAME: str = Field(default="postgres", description="数据库用户名")
    DB_PASSWORD: str = Field(default="root", description="数据库密码")
    DB_HOST: str = Field(default="127.0.0.1", description="数据库地址")
    DB_PORT: int = Field(default=5432, description="数据库端口")
    DB_NAME: str = Field(default="hr_system", description="数据库名")

    # ---------- JWT ----------
    ACCESS_SECRET: str = Field(default="", description="access_token 加盐")
    REFRESH_SECRET: str = Field(default="", description="refresh_token 加盐")
    ALGORITHM: str = Field(default="HS256", description="加密算法")
    ACCESS_EXPIRE_MINUTES: int = Field(default=30, description="access_token过期时间（分钟）")
    REFRESH_EXPIRE_DAYS: int = Field(default=7, description="refresh_token过期时间（天）")

    # ---------- Redis ----------
    REDIS_HOST: str = Field(default="localhost", description="redis地址")
    REDIS_PORT: int = Field(default=6379, description="redis端口")
    REDIS_DB: int = Field(default=0, description="redis数据库号")

    # ---------- Email(网易163) ----------
    MAIL_USERNAME: str = Field(default="", description="邮箱用户名")
    MAIL_PASSWORD: str = Field(default="", description="邮箱密码")
    MAIL_FROM: str = Field(default="", description="发件人")
    MAIL_PORT: int = Field(default=465, description="端口")
    MAIL_SERVER: str = Field(default="smtp.163.com", description="邮箱服务器")
    MAIL_FROM_NAME: str = Field(default="SimpleHR", description="发件人显示名称")
    MAIL_STARTTLS: bool = Field(default=False, description="先明文建连，再升级加密")
    MAIL_SSL_TLS: bool = Field(default=True, description="连接一开始就是加密的")
    # 是否启用邮件机器人（IMAP 长连接 + 定时轮询）。
    # CI / 本地测试环境没有邮箱服务时，设为 false 可跳过 MailManager 的
    # 初始化与关闭，避免连不上邮箱导致启动慢或日志报错。
    MAIL_BOT_ENABLED: bool = Field(default=True, description="是否启用邮件机器人")

    # ---------- 邀请码 ----------
    INVITE_EXPIRE_MINUTES: int = Field(default=30, description="邀请码过期时间（分钟）")

    # ---------- 钉钉 ----------
    DINGTALK_CLIENT_ID: str = Field(default="", description="钉钉应用 Client ID")
    DINGTALK_CLIENT_SECRET: str = Field(default="", description="钉钉应用 Client Secret")
    #内网穿刺
    BACKEND_BASE_URL: str = Field(default="", description="后端基础 URL")
    # 前端基础 URL：钉钉授权成功页跳转用（为空则跳后端根路径）
    FRONTEND_BASE_URL: str = Field(default="", description="前端基础 URL")

    # ---------- 文件上传 ----------
    RESUME_DIR: str = Field(default=os.path.join(BASE_DIR, "upload"), description="简历上传目录")

    # ---------- API_KEY ----------
    #OCR
    PADDLE_OCR_ACCESS_TOKEN: str = Field(default="", description="PaddleOCR 访问令牌")
    #LLM
    DASHSCOPE_API_KEY: str = Field(default="", description="DashScope API Key")
    BASE_URL:str =  Field(default="", description="DashScope API URL")
    PRIMARY_MODEL:str =  Field(default="", description="主模型")
    FALLBACK_MODEL:str =  Field(default="", description="副模型")

    # ---------- agent/graph ----------
    GRAPH_OPEN: bool = Field(default=True, description="是否开启图模式")

    # ---------- 招聘流程阈值 ----------
    SCORE_PASS_THRESHOLD: int = Field(default=7, description="AI 评分通过阈值（overall_score > 该值视为通过）")
    MAX_NEGOTIATION_ROUNDS: int = Field(default=3, description="面试时间协商轮次上限")
    EMAIL_REPLY_TIMEOUT: str = Field(default="TIMEOUT", description="interrupt 超时恢复标记")

    # 等待候选人回复邮件的超时时间（小时）。
    # 超过该时长仍停留在「等待回复」中断点的候选人，由超时巡检任务
    # 以 EMAIL_REPLY_TIMEOUT 恢复状态图，流转到「协商超时」终态。
    EMAIL_REPLY_TIMEOUT_HOURS: int = Field(
        default=48, ge=1, description="等待候选人回复邮件的超时时间（小时）"
    )

    # ---------- 意图识别 ----------
    # rule：纯规则（兜底/降级）；llm：纯大模型；hybrid：LLM 优先、失败回落规则
    INTENT_ENGINE: str = Field(default="hybrid", description="意图识别引擎：rule / llm / hybrid")
    INTENT_CONFIDENCE_THRESHOLD: float = Field(
        default=0.70, ge=0.0, le=1.0, description="低于该置信度转人工复核"
    )
    # 允许自动回复的意图白名单（高风险动作一律不自动回）
    INTENT_AUTO_REPLY_INTENTS: list[str] = Field(
        default=["RESUME_SUBMISSION", "PROGRESS_INQUIRY"],
        description="允许自动回复的意图白名单",
    )
    INTENT_HUMAN_REVIEW_ENABLED: bool = Field(default=True, description="低置信是否转人工复核")
    INTENT_MAX_MAIL_CHARS: int = Field(default=4000, ge=500, description="分类正文截断长度")
    INTENT_CACHE_SIZE: int = Field(default=20, description="进程内分类结果缓存上限")
    INTENT_LLM_TIMEOUT_SECONDS: float = Field(
        default=30.0, ge=1.0, description="单次意图分类 LLM 调用超时"
    )

    # ---------- 邮件投递待确认 ----------
    MAIL_SUBMISSION_EXPIRE_DAYS: int = Field(
        default=7, ge=1, description="待确认投递的超期天数"
    )
    MAIL_SUBMISSION_DEFAULT_POSITION_ID: str = Field(
        default="", description="匹配不到职位时的默认职位，留空则转人工"
    )

    # ---------- 意图复核 ----------
    # 复核「执行」动作可能触发状态图恢复（LLM + 钉钉 + 发信），必须有硬超时：
    # 否则一次模型挂起就会让记录长时间处于「已认领」状态，别人无法接手。
    MAIL_REVIEW_EXECUTE_TIMEOUT_SECONDS: float = Field(
        default=180.0, ge=1.0, description="复核动作的硬超时（秒）"
    )
    # 认领租约：必须大于动作硬超时，否则动作还没跑完就会被重新认领 → 重复执行。
    # 它同时是「进程被硬杀后，记录最晚多久能被别人接手」的上界。
    MAIL_REVIEW_CLAIM_LEASE_SECONDS: int = Field(
        default=300, ge=1, description="复核记录认领租约（秒），须大于动作硬超时"
    )

    # ---------- 图内 LLM 调用 ----------
    # 评分 / 时间解析走的是同一类结构化调用，之前没有超时兜底：
    # 模型侧挂起会让节点一直卡住，直到连接被对端断开。
    LLM_CALL_TIMEOUT_SECONDS: float = Field(
        default=60.0, ge=1.0, description="图内节点 LLM 调用超时（评分 / 时间解析）"
    )

    # ---------- LangGraph 检查点连接池 ----------
    # 注意：AsyncPostgresSaver 内部有 asyncio.Lock，checkpoint 读写是串行的，
    # 因此池的并发能力用不上；这里限制大小只为节省数据库连接资源，
    # 同时保留 1 条冗余连接用于坏连接替换 / 健康检查。
    CHECKPOINT_DB_MIN_SIZE: int = Field(
        default=1, ge=1, description="检查点连接池最小连接数"
    )
    CHECKPOINT_DB_MAX_SIZE: int = Field(
        default=2, ge=1, description="检查点连接池最大连接数（串行场景下 2 足够）"
    )

    # ---------- LLM 调用限流 ----------
    LLM_MAX_CONCURRENCY: int = Field(
        default=20, ge=1,
        description="同时在飞的 LLM 调用上限（进程内信号量）；本地 DB 连接池 30 为参考上限",
    )
    LLM_RPM_LIMIT: int = Field(
        default=600, ge=1,
        description="每分钟 LLM 调用上限（Redis 滑动窗口，0 表示不限）；突发缓冲",
    )

    # ---------- 任务级并发限流 ----------
    # 限制"同时运行的完整业务任务数"，与 LLM 调用级限流叠加：
    # 任务级主要保护下游副作用（钉钉日程 / 邮件 / DB 写），调用级保护模型服务压力。
    RECRUIT_TASK_MAX_CONCURRENCY: int = Field(
        default=12, ge=1, description="同时进行的招聘流程（图执行/恢复）数上限"
    )
    RESUME_PARSE_MAX_CONCURRENCY: int = Field(
        default=12, ge=1, description="同时进行的简历解析（OCR + LLM）数上限"
    )
    MAIL_ENRICH_MAX_CONCURRENCY: int = Field(
        default=20, ge=1, description="同时进行的邮件简历后台富化数上限（低优先）"
    )

    # ---------- 周期任务调度 ----------
    # 调度模式不再做成三选一的手动开关，改由 DEBUG 推导，
    # 见文件末尾的 computed_field ``PERIODIC_TASK_MODE``。
    PERIODIC_TASK_DAILY_TIMES: str = Field(
        default="08:00,12:00,17:00,24:00",
        description="每日执行时刻（逗号分隔，24:00 表示次日 0 点）；非 debug 环境生效",
    )
    PERIODIC_TASK_DEV_INTERVAL_SECONDS: int = Field(
        default=15, ge=1, description="debug 模式下的固定执行间隔（秒）"
    )
    # 连续「执行失败」（每次已用尽重试）多少次后告警一次；
    # 默认 3 = 容忍 2 次执行失败，第 3 次才告警（恢复前只告警一次）。
    PERIODIC_TASK_FAILURE_ALERT_THRESHOLD: int = Field(
        default=3, ge=1,
        description="连续执行失败（重试耗尽）多少次后告警一次；默认 3 即容忍 2 次"
    )

    # 后台任务失败重试：重试属于「一次执行」内部，重试耗尽才记为一次执行失败。
    BACKGROUND_TASK_RETRY_TIMES: int = Field(
        default=3, ge=0,
        description="后台任务每次执行失败后的重试次数（不含首次）；0 表示不重试"
    )
    BACKGROUND_TASK_RETRY_BASE_DELAY_SECONDS: float = Field(
        default=1.0, gt=0,
        description="重试退避基数（秒）：第 n 次重试前等待 base * 2^(n-1)"
    )
    BACKGROUND_TASK_RETRY_MAX_DELAY_SECONDS: float = Field(
        default=30.0, gt=0, description="单次重试等待上限（秒）"
    )

    # 后台任务（周期巡检 + 邮件轮询）启动后到「首次执行」的等待秒数。
    # 默认 2 分钟：既避免应用启动瞬间压一波数据库查询，
    # 也避免 daily_times 模式下首次任务要等上数小时（最长 8 小时）。
    BACKGROUND_TASK_STARTUP_DELAY_SECONDS: int = Field(
        default=120, ge=0,
        description="后台任务启动后到首次执行的等待秒数（周期巡检与邮件轮询共用）"
    )

    # 邮件机器人轮询：独立开关，不跟随 DEBUG / 全局调度模式，默认 15 秒一次。
    MAIL_POLL_INTERVAL_SECONDS: int = Field(
        default=15, ge=1, description="邮件机器人轮询间隔（秒）"
    )


    # ---------- 登录限流（滑动窗口，单位：次/60 秒） ----------
    LOGIN_RATE_LIMIT_PER_IP: int = Field(
        default=20, ge=1, description="单 IP 登录尝试上限（60 秒窗口）"
    )
    LOGIN_RATE_LIMIT_PER_EMAIL: int = Field(
        default=5, ge=1, description="单账号登录尝试上限（60 秒窗口）"
    )

    # ---------- 面试时间窗口（24小时制） ----------
    AM_WINDOW_START_HOUR: int = Field(default=9, description="上午面试窗口开始")
    AM_WINDOW_END_HOUR: int = Field(default=12, description="上午面试窗口结束")
    PM_WINDOW_START_HOUR: int = Field(default=14, description="下午面试窗口开始")
    PM_WINDOW_END_HOUR: int = Field(default=18, description="下午面试窗口结束")

    # ---------- 日志系统 ----------
    NODE_LOGGER_NAME:str = Field(default= "workflow.nodes",description="工作流节点日志文件")

    @computed_field
    @property
    def am_window(self) -> tuple:
        """上午面试窗口（time 元组）。"""
        from datetime import time
        return (time(self.AM_WINDOW_START_HOUR, 0), time(self.AM_WINDOW_END_HOUR, 0))

    @computed_field
    @property
    def pm_window(self) -> tuple:
        """下午面试窗口（time 元组）。"""
        from datetime import time
        return (time(self.PM_WINDOW_START_HOUR, 0), time(self.PM_WINDOW_END_HOUR, 0))

    @computed_field
    @property
    def database_url(self) -> str:
        return f"postgresql+psycopg://{self.DB_USERNAME}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    @computed_field
    @property
    def REDIS_URL(self) -> str:
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @computed_field
    @property
    def PERIODIC_TASK_MODE(self) -> str:
        """周期巡检的调度模式（取值同 ``app/tasks/scheduler.py`` 的模式常量）。

        简化为两档，不再让运维在三个模式里选：

        * ``debug=True``  → ``dev_interval``：每 ``PERIODIC_TASK_DEV_INTERVAL_SECONDS``
          （默认 15 秒）跑一轮；
        * ``debug=False`` → ``daily_times``：按 ``PERIODIC_TASK_DAILY_TIMES``
          的每日时刻执行。

        用 ``DEBUG`` 而不是 ``ENVIRONMENT`` 推导，是因为「多久跑一次巡检」
        本质上是「我想不想现在就看到它跑」——本地开发要快，生产要按时刻。
        """
        return "dev_interval" if self.debug else "daily_times"

    @computed_field
    @property
    def MAIL_POLL_MODE(self) -> str:
        """邮件轮询的调度模式：固定 ``interval``。

        刻意**不跟随** ``DEBUG`` / ``PERIODIC_TASK_MODE``：邮件轮询是独立开关，
        间隔由 ``MAIL_POLL_INTERVAL_SECONDS`` 决定（默认 15 秒）。
        保留这个 computed field 是为了让模式在 ``model_dump()`` 里可见，
        与 ``PERIODIC_TASK_MODE`` 保持同一套读法。
        """
        return "interval"

    @model_validator(mode="after")
    def _validate_secrets(self) -> "Settings":
        """启动即校验密钥，避免「跑起来才在登录处 500」。

        空密钥会让 PyJWT 抛 InvalidKeyError；生产环境（非 debug）还要求
        长度 >= 32，防止用弱密钥给 JWT 签名。
        """
        for name in ("ACCESS_SECRET", "REFRESH_SECRET"):
            value: str = getattr(self, name)
            if not value:
                raise ValueError(
                    f"{name} 未配置：请在 .env 或环境变量中设置"
                    "（生成方式：openssl rand -hex 32）"
                )
            if not self.debug and len(value) < 32:
                raise ValueError(
                    f"{name} 太短（{len(value)} 字符），非 debug 环境要求至少 32 字符"
                )
        return self

    @model_validator(mode="after")
    def _validate_review_lease(self) -> "Settings":
        """复核记录的认领租约必须大于动作硬超时。

        租约 <= 动作超时会有两种坏结果：
        * 动作还没跑完，租约已过期 → 别人可以重新认领 → **重复执行**
          （重复恢复状态图、重复建投递记录）；
        * 动作一旦超时被取消，记录很快就能被重试，看似更快，但代价是
          一个仍在取消过程中的动作可能和新的认领重叠。

        因此在启动期就拦住，而不是等到线上偶发重放。
        """
        if self.MAIL_REVIEW_CLAIM_LEASE_SECONDS <= self.MAIL_REVIEW_EXECUTE_TIMEOUT_SECONDS:
            raise ValueError(
                "MAIL_REVIEW_CLAIM_LEASE_SECONDS 必须大于 "
                "MAIL_REVIEW_EXECUTE_TIMEOUT_SECONDS"
                f"（当前 {self.MAIL_REVIEW_CLAIM_LEASE_SECONDS} <= "
                f"{self.MAIL_REVIEW_EXECUTE_TIMEOUT_SECONDS}）"
            )
        return self



from functools import lru_cache

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
