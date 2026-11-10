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
from pydantic import Field, computed_field
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

    # ---------- 邀请码 ----------
    INVITE_EXPIRE_MINUTES: int = Field(default=30, description="邀请码过期时间（分钟）")

    # ---------- 钉钉 ----------
    DINGTALK_CLIENT_ID: str = Field(default="", description="钉钉应用 Client ID")
    DINGTALK_CLIENT_SECRET: str = Field(default="", description="钉钉应用 Client Secret")
    #内网穿刺
    BACKEND_BASE_URL: str = Field(default="", description="后端基础 URL")

    # ---------- 文件上传 ----------
    RESUME_DIR: str = Field(default=os.path.join(BASE_DIR, "upload"), description="简历上传目录")

    PADDLE_OCR_ACCESS_TOKEN: str = Field(default="", description="PaddleOCR 访问令牌")
    DASHSCOPE_API_KEY: str = Field(default="", description="DashScope API Key")

    # ---------- agent/graph ----------
    GRAPH_OPEN: bool = Field(default=False, description="是否开启图模式")

    # ---------- 招聘流程阈值 ----------
    SCORE_PASS_THRESHOLD: int = Field(default=7, description="AI 评分通过阈值（overall_score > 该值视为通过）")
    MAX_NEGOTIATION_ROUNDS: int = Field(default=3, description="面试时间协商轮次上限")
    EMAIL_REPLY_TIMEOUT: str = Field(default="TIMEOUT", description="interrupt 超时恢复标记")

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



from functools import lru_cache

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
