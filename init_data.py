"""初始化种子数据：8 个部门 + 9 个测试账号。

**幂等**：部门按名称、用户按邮箱/用户名判重，已存在则跳过。
重复执行不会产生重复数据，可以放心重跑。
"""
import asyncio
import sys

from app.core.db import AsyncSessionFactory
from app.repository.user_repo import DepartmentRepo, UserRepo

# Fix for Windows psycopg issue with ProactorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


DEPARTMENTS: list[dict] = [
    {"name": "人事部", "description": "人事部门"},
    {"name": "技术部", "description": "负责产品和技术部"},
    {"name": "运营部", "description": "负责产品和用户运营"},
    {"name": "市场部", "description": "负责市场推广和品牌建设"},
    {"name": "财务部", "description": "负责财务管理和审计"},
    {"name": "法务部", "description": "负责法律事务和合规管理"},
    {"name": "行政部", "description": "负责行政管理和后勤保障"},
    {"name": "客服部", "description": "负责客户服务和投诉处理"},
]


# 部门用名称引用，避免硬编码 id；id 在 init_users 里按名称查出来
USERS: list[dict] = [
    {"username": "Boss", "password": "111111", "email": "boss@qq.com",
     "realname": "黄老板", "is_superuser": True, "department": "人事部"},
    {"username": "hr", "password": "111111", "email": "hr@qq.com",
     "realname": "周HR", "is_superuser": False, "department": "人事部"},
    {"username": "tech", "password": "111111", "email": "tech@qq.com",
     "realname": "张技术", "is_superuser": False, "department": "技术部"},
    {"username": "operator", "password": "111111", "email": "operator@qq.com",
     "realname": "孙运营", "is_superuser": False, "department": "运营部"},
    {"username": "market", "password": "111111", "email": "market@qq.com",
     "realname": "李市场", "is_superuser": False, "department": "市场部"},
    {"username": "finance", "password": "111111", "email": "finance@qq.com",
     "realname": "王财务", "is_superuser": False, "department": "财务部"},
    {"username": "legal", "password": "111111", "email": "legal@qq.com",
     "realname": "赵法务", "is_superuser": False, "department": "法务部"},
    {"username": "admin", "password": "111111", "email": "admin@qq.com",
     "realname": "王行政", "is_superuser": False, "department": "行政部"},
    {"username": "service", "password": "111111", "email": "service@qq.com",
     "realname": "张客服", "is_superuser": False, "department": "客服部"},
]


async def init_departments() -> tuple[int, int]:
    """创建缺失的部门，返回 (新建数, 跳过数)。"""
    created = skipped = 0
    async with AsyncSessionFactory() as session:
        async with session.begin():
            department_repo = DepartmentRepo(session)
            for department in DEPARTMENTS:
                if await department_repo.get_by_name(department["name"]):
                    skipped += 1
                else:
                    await department_repo.create_department(department)
                    created += 1
    return created, skipped


async def init_users() -> tuple[int, int]:
    """创建缺失的用户，返回 (新建数, 跳过数)。"""
    created = skipped = 0
    async with AsyncSessionFactory() as session:
        async with session.begin():
            user_repo = UserRepo(session)
            department_repo = DepartmentRepo(session)

            # 先把用到的部门 id 查出来（部门已由 init_departments 建好）
            department_ids: dict[str, str] = {}
            for name in {u["department"] for u in USERS}:
                department = await department_repo.get_by_name(name)
                if department is None:
                    raise RuntimeError(
                        f"部门 {name!r} 不存在，请先确保部门初始化成功"
                    )
                department_ids[name] = department.id

            for user in USERS:
                existing = await user_repo.get_by_email(user["email"]) or \
                    await user_repo.get_by_username(user["username"])
                if existing:
                    skipped += 1
                    continue
                await user_repo.create_user({
                    "username": user["username"],
                    "password": user["password"],
                    "email": user["email"],
                    "realname": user["realname"],
                    "is_superuser": user["is_superuser"],
                    "department_id": department_ids[user["department"]],
                })
                created += 1
    return created, skipped


async def main() -> None:
    department_created, department_skipped = await init_departments()
    print(f"部门初始化完成：新建 {department_created} 个，跳过 {department_skipped} 个（已存在）")

    user_created, user_skipped = await init_users()
    print(f"用户初始化完成：新建 {user_created} 个，跳过 {user_skipped} 个（已存在）")

    print("初始账号：Boss / 111111（超管）、hr / 111111 等，上线后请立即修改密码")


if __name__ == "__main__":
    asyncio.run(main())
