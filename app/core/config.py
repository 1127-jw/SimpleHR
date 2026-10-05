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
    DB_USERNAME: str = "postgres"
    DB_PASSWORD: str = "root"
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 5432
    DB_NAME: str = "hr_system"

    # ---------- JWT ----------
    ACCESS_SECRET: str = ""
    REFRESH_SECRET: str = ""
    ALGORITHM: str = "HS256"
    ACCESS_EXPIRE_MINUTES: int = 30
    REFRESH_EXPIRE_DAYS: int = 7

    # ---------- Redis ----------
    REDIS_HOST = "localhost"
    REDIS_PORT = 6379
    REDIS_DB =  0


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

