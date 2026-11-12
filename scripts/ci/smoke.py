#!/usr/bin/env python3
"""冒烟测试：对运行中的应用请求若干核心接口，验证主要链路可用。

前置条件（由 CI 的 smoke job 准备）：
  1. PostgreSQL + Redis 已就绪；
  2. 已执行 `alembic upgrade head` 与 `init_data.py`；
  3. 应用已在 SMOKE_BASE_URL（默认 http://127.0.0.1:8000）上运行。

只验证「能不能用」，不校验业务细节；任一接口失败即以非 0 退出。
"""
from __future__ import annotations

import os
import sys

import httpx

BASE_URL = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8000")

# init_data.py 创建的种子超管账号
SEED_EMAIL = os.environ.get("SMOKE_SEED_EMAIL", "boss@qq.com")
SEED_PASSWORD = os.environ.get("SMOKE_SEED_PASSWORD", "111111")


def check(name: str, resp: httpx.Response, expect: int = 200) -> None:
    """断言状态码，失败时打印响应内容并退出。"""
    if resp.status_code != expect:
        raise SystemExit(
            f"❌ {name} 期望 {expect}，实际 {resp.status_code}\n响应: {resp.text[:300]}"
        )
    print(f"✅ {name} → {resp.status_code}")


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        # 1) 根路径：应用活着
        r = client.get("/")
        check("GET /", r)
        if r.json().get("message") != "Hello World":
            raise SystemExit(f"❌ GET / 返回内容不对: {r.text[:200]}")

        # 2) 登录：拿到 token（同时验证密码哈希、JWT 签发链路）
        r = client.post("/user/token", json={"email": SEED_EMAIL, "password": SEED_PASSWORD})
        check("POST /user/token（登录）", r)
        token = r.json().get("access_token")
        if not token:
            raise SystemExit(f"❌ 登录未返回 access_token: {r.text[:200]}")
        headers = {"Authorization": f"Bearer {token}"}

        # 3) 受保护接口：验证鉴权中间件 + 数据库查询
        check("GET /user/me", client.get("/user/me", headers=headers))
        check(
            "GET /position/list",
            client.get("/position/list", headers=headers, params={"page": 1, "size": 10}),
        )
        check(
            "GET /candidate/list",
            client.get("/candidate/list", headers=headers, params={"page": 1, "size": 10}),
        )

        # 4) 近 7 天统计：验证聚合查询（顺带覆盖统计接口的边界修复）
        r = client.get("/candidate/candidate/7d", headers=headers)
        check("GET /candidate/candidate/7d", r)
        days = r.json()
        if not isinstance(days, list) or len(days) != 7:
            raise SystemExit(f"❌ 近7天统计应返回 7 条，实际: {days}")

    print("🎉 冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
