# 周期性任务管理器重构 · 实施稿

## 一、背景

项目中存在两个周期性巡检任务：

| Manager | 业务 | 触发周期 |
|---------|------|---------|
| `TimeoutSweepManager` | 邮件回复超时 → 推进到「协商超时」 | `EMAIL_REPLY_SWEEP_INTERVAL_MINUTES`（默认 15 分钟） |
| `MailSubmissionSweepManager` | 待确认投递超期 → 置为 EXPIRED | `MAIL_SUBMISSION_SWEEP_INTERVAL_MINUTES`（默认 60 分钟） |

两者的**生命周期管理代码几乎完全重复**（`_task` / `init()` / `close()` / `_sweep_loop()`），只有「执行什么」和「间隔多久」不同。

---

## 二、重构方案

### 2.1 抽取基类 `PeriodicTaskManager`

```
app/tasks/periodic_task.py
```

基类统一负责：
- 后台 `asyncio.Task` 的创建 / 取消
- 「先等一个周期再开跑」的启动策略
- 定时循环 + 异常兜底（失败不退出，下周期重试）
- 从 `Settings` 读取间隔

子类只需：
1. 声明 `_task_name`
2. 声明 `_interval_setting`
3. 实现 `_run_once()`

### 2.2 重构前后对比

| 文件 | 重构前 | 重构后 |
|------|--------|--------|
| `timeout_tasks.py` | 185 行（Manager 模板 ~60 行 + 业务 ~125 行） | 120 行（业务 ~110 行 + Manager ~12 行） |
| `mail_submission_tasks.py` | 58 行（Manager 模板 ~50 行 + 业务 ~8 行） | 30 行（Manager ~10 行 + 业务 ~5 行） |
| 新增 `periodic_task.py` | — | ~90 行（基类） |

---

## 三、核心代码

### 3.1 基类 `app/tasks/periodic_task.py`

```python
import asyncio
import logging
from contextlib import suppress
from typing import Optional

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class PeriodicTaskManager:
    """所有周期性巡检任务的基类。"""

    _task: Optional[asyncio.Task] = None
    _task_name: str = "周期性任务"
    _interval_setting: str = ""

    @classmethod
    async def init(cls) -> None:
        """启动后台巡检任务（全局只启动一次）。"""
        if cls._task is not None:
            logger.warning("%s已初始化，跳过重复 init", cls._task_name)
            return
        cls._task = asyncio.create_task(cls._loop(), name=cls._task_name)
        logger.info("%s已启动，间隔 %s 分钟", cls._task_name, cls._get_interval_minutes())

    @classmethod
    async def close(cls) -> None:
        """取消并等待巡检任务结束。"""
        if cls._task is None:
            return
        cls._task.cancel()
        with suppress(asyncio.CancelledError):
            await cls._task
        cls._task = None
        logger.info("%s已停止", cls._task_name)

    @classmethod
    def _get_interval_minutes(cls) -> int:
        if not cls._interval_setting:
            raise NotImplementedError(f"{cls.__name__} 未设置 _interval_setting")
        return int(getattr(settings, cls._interval_setting))

    @classmethod
    async def _loop(cls) -> None:
        interval_seconds = cls._get_interval_minutes() * 60
        await asyncio.sleep(interval_seconds)
        while True:
            try:
                await cls._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s失败，将在下个周期重试", cls._task_name)
            await asyncio.sleep(interval_seconds)

    @classmethod
    async def _run_once(cls) -> None:
        raise NotImplementedError
```

### 3.2 子类：`TimeoutSweepManager`

```python
# app/tasks/timeout_tasks.py
from app.tasks.periodic_task import PeriodicTaskManager


class TimeoutSweepManager(PeriodicTaskManager):
    """邮件回复超时巡检的生命周期管理器。"""

    _task_name = "邮件回复超时巡检"
    _interval_setting = "EMAIL_REPLY_SWEEP_INTERVAL_MINUTES"

    @classmethod
    async def _run_once(cls) -> None:
        swept = await sweep_stale_email_replies()
        if swept:
            logger.info("邮件回复超时巡检：已推进 %s 个候选人", swept)
```

### 3.3 子类：`MailSubmissionSweepManager`

```python
# app/tasks/mail_submission_tasks.py
from app.tasks.periodic_task import PeriodicTaskManager


class MailSubmissionSweepManager(PeriodicTaskManager):
    """待确认投递超期巡检的生命周期管理器。"""

    _task_name = "待确认投递巡检"
    _interval_setting = "MAIL_SUBMISSION_SWEEP_INTERVAL_MINUTES"

    @classmethod
    async def _run_once(cls) -> None:
        expired = await expire_stale_submissions()
        if expired:
            logger.info("待确认投递巡检：置为 EXPIRED %s 条", expired)
```

---

## 四、调用方无需改动

`app/main.py` 的 lifespan 保持原样：

```python
await TimeoutSweepManager.init()
await MailSubmissionSweepManager.init()

yield

await TimeoutSweepManager.close()
await MailSubmissionSweepManager.close()
```

子类的 `init()` / `close()` 方法名与签名完全不变，**调用方零感知**。

---

## 五、设计要点

| 要点 | 说明 |
|------|------|
| **先等一个周期再开跑** | 避免应用启动瞬间压一波数据库查询 |
| **异常不退出** | 单轮失败只记日志，下周期重试，保证巡检长期存活 |
| **`CancelledError` 单独抛出** | 保证 `close()` 能正常取消任务，不被通用 `except` 吞掉 |
| **间隔可配** | 子类通过 `_interval_setting` 声明读取哪个配置项 |
| **跨 worker 互斥** | 业务侧自行处理（如 `TimeoutSweepManager` 用 Redis `SET NX`） |

---

## 六、新增巡检的方法

未来要新增一个周期性任务，只需 3 步：

1. 在 `Settings` 加配置项（如 `XXX_SWEEP_INTERVAL_MINUTES`）
2. 写业务函数（如 `sweep_xxx()`）
3. 继承基类：

```python
class XxxSweepManager(PeriodicTaskManager):
    _task_name = "XXX 巡检"
    _interval_setting = "XXX_SWEEP_INTERVAL_MINUTES"

    @classmethod
    async def _run_once(cls) -> None:
        await sweep_xxx()
```

---

## 七、验证

```bash
.venv/Scripts/python.exe -c "
from app.tasks.timeout_tasks import TimeoutSweepManager
from app.tasks.mail_submission_tasks import MailSubmissionSweepManager
print(TimeoutSweepManager._get_interval_minutes())        # 15
print(MailSubmissionSweepManager._get_interval_minutes()) # 60
"
# 输出：15 / 60

.venv/Scripts/python.exe -m pytest tests/ -q   # 全部通过
```

---

## 八、收益

| 维度 | 改进 |
|------|------|
| **消除重复** | 两个 Manager 的 init/close/loop 模板统一 |
| **易于扩展** | 新增巡检只需继承 + 3 行声明 |
| **行为一致** | 所有巡检的启动策略、异常处理、日志格式统一 |
| **可测试性** | 单轮逻辑（`_run_once`）与调度解耦，便于单独测试 |
