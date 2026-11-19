# 项目技术难点与解决方案

## 一、循环导入导致 Alembic 无法识别模型

### 问题描述

`alembic revision --autogenerate` 无法检测到任何数据库表，迁移文件为空。

### 根因分析

```
app/models/candidate.py  →  from .position import PositionModel
app/models/position.py   →  from .candidate import CandidateModel
```

两个模型互相引用，形成循环依赖：
- 加载 `candidate.py` 时触发 `position.py` 加载
- `position.py` 又尝试导入 `candidate.py`（尚未完成）
- 导致 `ImportError`，模型未能注册到 SQLAlchemy Metadata

### 解决方案

使用 `TYPE_CHECKING` + 字符串关系引用，打破循环：

```python
# candidate.py
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .position import PositionModel

class CandidateModel(BaseModel):
    # 运行时用字符串，类型检查时用实际类型
    position: Mapped["PositionModel"] = relationship("PositionModel", back_populates="candidates")
```

### 成果

- Alembic 成功检测到 8 张表，生成完整迁移文件
- 类型检查（mypy/IDE）仍保留完整提示
- 后续新增模型无需重复处理

---

## 二、时区不一致导致时间比较失败

### 问题描述

`find_available_slot` 函数报错：
```
TypeError: can't compare offset-naive and offset-aware datetimes
```

### 根因分析

系统中混用两种时间：
- `datetime.now()` → **naive**（无时区）
- ISO 8601 解析出来的时间 → **aware**（带 Asia/Shanghai）

Python 不允许直接比较，导致面试官空闲时间计算失败。

### 解决方案

统一到 `app/core/time.py`（见 `docs/change2026-11-17.md` §十九）：内存里一律 aware 北京时间，
只有写库时才剥成 naive。时区常量只有一个：

```python
# utils/available_time.py
from app.core.time import BEIJING_TZ

# 修改前
window_start = datetime.combine(search_date.date(), win_start_time)  # naive

# 修改后
window_start = datetime.combine(search_date.date(), win_start_time, tzinfo=BEIJING_TZ)  # aware
```

时间字符串解析后补时区，也不许再手写 `replace(tzinfo=...)`：

```python
# 修改前（check_availability_node.py）
proposed_dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)

# 修改后
proposed_dt = from_naive_beijing(datetime.strptime(text, "%Y-%m-%d %H:%M:%S"))
```

### 成果

- 时间比较逻辑正常工作
- 面试官空闲时间计算准确
- 避免了时区转换错误
- 取时间/换时区只剩一个入口，`tests/test_time_policy.py` 扫源码禁止手写 `timedelta(hours=8)`

---

## 三、枚举未同步导致数据库写入失败

### 问题描述

创建面试记录时报错：
```
psycopg.errors.InvalidTextRepresentation: 对于枚举 interviewresultenum 的输入值无效: "PENDING"
```

### 根因分析

Python 枚举新增了值，但数据库 ENUM 类型未同步：

```python
# Python 代码
class InterviewResultEnum(StrEnum):
    PENDING = "PENDING"    # 新增
    PASSED = "PASSED"
    FAILED = "FAILED"

# 数据库 ENUM（旧）
CREATE TYPE interviewresultenum AS ENUM ('PASSED', 'FAILED');
```

### 解决方案

将 PostgreSQL ENUM 改为 VARCHAR，避免枚举同步问题：

```python
# 模型修改
result: Mapped[Optional[InterviewResultEnum]] = mapped_column(
    Enum(InterviewResultEnum, native_enum=False, length=20)
)
```

```python
# 迁移脚本
op.alter_column(
    'interviews', 'result',
    existing_type=postgresql.ENUM('PASSED', 'FAILED', 'PENDING', name='interviewresultenum'),
    type_=sa.String(length=20),
    existing_nullable=True,
    postgresql_using='result::text',
)
```

### 成果

- 新增枚举值无需数据库迁移
- 彻底解决 ENUM 同步问题
- 提高了系统可维护性

---

## 四、前端字段不匹配导致数据显示异常

### 问题描述

前端页面数据显示为空：
- 员工管理：用户名、姓名为空
- HR 管理：姓名为空
- 候选人管理：创建时间不显示

### 根因分析

前后端字段名/结构不一致：

| 后端返回 | 前端期望 |
|---------|---------|
| `UserSH.realname` | `User.realname`（缺失） |
| `UserSH.status` | `User.is_active`（错误） |
| `UserListResponse.items` | `UserListResponse.users`（错误） |
| `CandidateSchema.created_at` | `Candidate.created_at`（缺失） |

### 解决方案

**后端：补全 Schema 字段**

```python
# app/schemas/user_schema.py
class UserSH(BaseModel):
    realname: str = Field(..., description="Real name")  # 新增

# app/schemas/candidate_schema.py
class CandidateSchema(BaseModel):
    created_at: datetime = Field(..., description="创建时间")  # 新增
```

**前端：对齐接口定义**

```typescript
// 修改前
export interface User {
    is_active: boolean      // 错误
}

// 修改后
export interface User {
    status: 'ACTIVE' | 'BLOCKED' | 'RESIGNED'  // 正确
}

// 修改前
export interface UserListResponse {
    users: User[] | null    // 错误
}

// 修改后
export interface UserListResponse {
    items: User[]           // 正确
}
```

### 成果

- 员工管理页面正确显示用户名和姓名
- HR 管理页面正确显示姓名
- 候选人管理页面正确显示创建时间
- 前后端接口定义完全一致

---

## 技术栈

- **后端**：FastAPI + SQLAlchemy 2.0 (async) + Alembic + Pydantic v2
- **前端**：Vue 3 + TypeScript + Element Plus + Vite
- **数据库**：PostgreSQL + Redis
- **消息队列**：Redis（规划中）
- **测试**：pytest + pytest-asyncio

---

## 核心能力提升

| 能力 | 具体实践 |
|------|---------|
| **问题定位** | 通过日志分析、堆栈追踪快速定位循环导入、时区、枚举等问题 |
| **架构设计** | 使用延迟导入、字符串引用解决循环依赖；统一时区处理 |
| **数据库设计** | 理解 ENUM 类型局限，选择更灵活的 VARCHAR 方案 |
| **前后端协作** | 对齐接口定义，确保数据一致性 |
| **代码质量** | 编写单元测试覆盖核心功能，提高代码可维护性 |
