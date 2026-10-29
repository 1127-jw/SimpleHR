# 招聘工作流修复与超时机制说明

本文档记录 `app/workflow/` 的一次修复，包含：

1. 修复 3 个会导致崩溃的 Bug
2. 统一评分阈值为 7
3. 删除死代码、清理死导入、评分 Agent 提为模块级单例
4. 补齐 `negotiation_round` 初始化、协商轮次上限、interrupt 超时

> 约定：**修改** = 本次已在代码中改掉；**插入** = 需要外部补齐的代码（已给出位置与示例）。

---

## 一、改动总览

| 文件 | 类型 | 说明 |
|---|---|---|
| `app/workflow/candidate_nodes/scoring_node.py` | 修改 | 导入路径 Bug、Agent 单例、阈值、冗余 try/except |
| `app/workflow/candidate_nodes/confirm_node.py` | 修改 | `UnboundLocalError`、节点返回字符串 |
| `app/workflow/candidate_state.py` | 修改 | 新增流程常量；`negotiation_round` reducer 统一；`current_stage` 增加超时态 |
| `app/workflow/candidate_graph.py` | 修改 | 阈值、轮次上限路由、超时路由、函数定义顺序 |
| `app/workflow/candidate_nodes/email_invite_node.py` | 修改 | 清理死导入、初始化 `negotiation_round` |
| `app/workflow/candidate_nodes/check_availability_node.py` | 修改 | 防御式读取 `negotiation_round` |
| `app/models/candidate.py` | 修改 | 新增 `NEGOTIATION_EXPIRED` 状态 |
| `app/workflow/candidate_nodes/negotiation_expired_node.py` | 新增 | 协商超时终止节点 |
| `app/workflow/candidate_nodes/negotiation_router.py` | 删除 | 死代码（未被 import，且逻辑与图不一致） |
| `app/tasks/workflow_timeout_tasks.py` | **插入** | 外部超时调度任务（见第五节） |
| `app/main.py` | **插入** | lifespan 中启动 APScheduler（见第五节） |
| `notify.py`（邮件通知） | **插入** | resume 图执行 + 超时注入（见第五节） |
| `email_invite_node.py` | **插入** | 保存 `thread_id` / `deadline` 到 Redis（见第五节） |

---

## 二、崩溃 Bug 修复

### 2.1 `scoring_node.py` 导入路径错误

- **问题**：`from agents.prompts import ...` 指向不存在的顶层 `agents` 包，运行到该节点会 `ModuleNotFoundError`。
- **修改**：改为 `from app.agents.prompts import SCORE_FOR_CANDIDATE_SYSTEM_PROMPT, SCORE_FOR_CANDIDATE_USER_PROMPT`（文件顶部导入区）。

### 2.2 `confirm_node.py` 未定义变量 `interview_datetime_without_tz`

- **问题**：无时区 `datetime` 时 `if interview_datetime.tzinfo is not None` 不成立，变量未赋值，后面写库处使用会 `UnboundLocalError`。
- **修改**（`confirm_node.py:26-31`）：补充 `else` 分支：

```python
    if interview_datetime.tzinfo is not None:
        interview_datetime_without_tz = interview_datetime.astimezone(None).replace(tzinfo=None)
    else:
        interview_datetime_without_tz = interview_datetime
```

### 2.3 `confirm_node.py` 节点返回字符串

- **问题**：`get_access_token` 失败返回字符串，节点却 `return result`，违反「节点必须返回 dict 状态更新」的约定。
- **修改**（`confirm_node.py:52-56`）：改为抛异常：

```python
    if isinstance(result, str):
        raise ValueError(result)
```

---

## 三、统一评分阈值为 7

- **修改** `app/workflow/candidate_state.py:12-15` 新增常量：

```python
SCORE_PASS_THRESHOLD = 7          # 评分通过阈值（> 该值视为通过，与旧 score_for_candidate 保持一致）
MAX_NEGOTIATION_ROUNDS = 3        # 面试时间协商轮次上限
EMAIL_REPLY_TIMEOUT = "TIMEOUT"   # interrupt 超时恢复标记（外部调度器超时后 resume 用该值）
```

- **修改** `scoring_node.py`：状态写入与 `next_stage` 两处 `> 8` → `> SCORE_PASS_THRESHOLD`。
- **修改** `candidate_graph.py:86`：条件边 `> 8` → `> SCORE_PASS_THRESHOLD`。

> 注：业务语义为「大于 7 才通过」，与旧 `score_for_candidate`（`> 7`）保持一致。

---

## 四、清理 / 重构

### 4.1 删除 `negotiation_router.py`

该文件从未被 import，且内部路由用「确认/拒绝」字符串匹配，与图实际使用的 `SYSTEM_TIME_AVAILABLE` / `CANDIDATE_REFUSED` / LLM 提取方案不一致，还路由到不存在的 `wait_for_reply` 节点。已删除。

### 4.2 `scoring_node.py` 清理与单例

- 删除函数内重复导入（`PromptTemplate` 未使用）。
- `create_agent` / `ChatPromptTemplate` 提到模块级单例 `_score_agent` / `_score_user_prompt_template`，避免每次调用重建（与 `check_availability_node.py` 的 `_proposed_time_agent` 一致）。
- 删除无意义的 `try: ... except Exception as e: raise e`。

### 4.3 `email_invite_node.py` 死导入清理

删除未使用导入：`timedelta`、`AsyncSessionFactory`、`get_redis`、`DingdingUserModel`、`UserRepo`、`UserSH`、`get_calendar_list`、`get_union_id_and_access_token_by_user_id`、`cast, Any`、`find_available_slot`、`iso8601_to_datetime_beijing`、`datetime_to_iso8601_beijing`；并移除主分支未使用的 `email_bot = get_mail_bot_()`。

### 4.4 `candidate_state.py` 一致性

- `negotiation_round` 的 reducer 由内联 lambda 统一为 `assign_state_property`（行为一致）。
- `current_stage` 的 `Literal` 增加 `"negotiation_expired"`。

---

## 五、`negotiation_round` 初始化 + 轮次上限 + interrupt 超时

### 5.1 `negotiation_round` 初始化（修改）

- `email_invite_node.py` 两个 `return` 均增加 `"negotiation_round": 0`。
- `check_availability_node.py`（`can_not_find_available_time`）由 `state["negotiation_round"] + 1` 改为 `state.get("negotiation_round", 0) + 1`，避免未初始化 KeyError。

### 5.2 协商轮次上限（修改）

- 常量 `MAX_NEGOTIATION_ROUNDS = 3`（`candidate_state.py:14`）。
- `candidate_graph.py:33-49` 的 `route_after_check_availability`：时间不可用且 `negotiation_round >= MAX_NEGOTIATION_ROUNDS` 时路由到 `negotiation_expired`。
- 新增终止节点 `app/workflow/candidate_nodes/negotiation_expired_node.py`，把候选人状态更新为 `NEGOTIATION_EXPIRED`（`app/models/candidate.py` 新增 `协商超时`）。
- `candidate_graph.py` 注册 `negotiation_expired` 节点并连 `END`。

### 5.3 interrupt 超时（修改 + 插入）

`interrupt()` 本身**不支持墙钟超时**（`interrupt` 没有 timeout 参数；节点级 `timeout` 只约束节点执行时间且要求 async 节点，不覆盖等待人工回复的时间）。因此采用「图内处理超时标记 + 图外调度器触发」的分离方案：

**图内（已修改）**：`candidate_graph.py:22-30` 新增 `route_after_email_reply`，当恢复值是 `EMAIL_REPLY_TIMEOUT`（`"TIMEOUT"`）时路由到 `negotiation_expired`；并把 `get_candidate_email_reply_node → check_availability` 的静态边改为条件边。

**图外（需插入）**：外部调度器在候选人超时未回复时，以 `Command(resume=EMAIL_REPLY_TIMEOUT)` 恢复该线程。具体插入位置如下。

#### 插入 A：`email_invite_node.py` 发送邀请后保存 thread_id 与 deadline

位置：`email_invite_node.py` 的 `email_invite_node` 函数内，发送邮件成功之后、`return` 之前。

```python
    # === 插入：记录线程与超时截止时间，供外部调度器使用 ===
    from datetime import timedelta
    from app.workflow.candidate_state import NEGOTIATION_TIMEOUT_DAYS  # 需在 candidate_state.py 增补该常量
    from app.service.workflow_thread_store import save_thread_deadline
    thread_id = f"candidate_{candidate.id}"
    await save_thread_deadline(thread_id, datetime.now() + timedelta(days=NEGOTIATION_TIMEOUT_DAYS))
```

> 说明：`NEGOTIATION_TIMEOUT_DAYS` 与 `workflow_thread_store`（Redis 封装 `save_thread_id`/`save_thread_deadline`）属于本步配套新增，尚未创建。

#### 插入 B：新增 `app/tasks/workflow_timeout_tasks.py`

```python
"""工作流超时调度任务：扫描超时未回复的线程并以 TIMEOUT 恢复执行。"""
from datetime import datetime
from loguru import logger
from langgraph.types import Command

from app.workflow.candidate_state import EMAIL_REPLY_TIMEOUT
from app.service.workflow_thread_store import iter_expired_threads, remove_thread


async def expire_stale_negotiations(get_checkpointer) -> None:
    """遍历已超时的 thread_id，恢复图执行并注入超时标记。"""
    from app.workflow.candidate_graph import build_candidate_graph

    async for thread_id in iter_expired_threads(datetime.now()):
        try:
            app = build_candidate_graph(await get_checkpointer())
            await app.ainvoke(
                Command(resume=EMAIL_REPLY_TIMEOUT),
                config={"configurable": {"thread_id": thread_id}},
            )
            await remove_thread(thread_id)
            logger.info("协商超时，已终止线程 {}", thread_id)
        except Exception as e:  # 单个线程失败不影响其他线程
            logger.error("处理超时线程 {} 失败：{}", thread_id, e)
```

#### 插入 C：`app/main.py` 的 lifespan 启动调度器

位置：`app/main.py:27-41` 的 `lifespan` 内，`yield` 之前启动，`yield` 之后关闭。

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# lifespan 中 yield 之前：
    scheduler = AsyncIOScheduler()
    scheduler.add_job(expire_stale_negotiations, "interval", minutes=30, args=[get_checkpointer])
    scheduler.start()

# yield 之后：
    scheduler.shutdown(wait=False)
```

> `get_checkpointer` 需要与 `CandidateProcessAgent` 一致的 `AsyncPostgresSaver`（见 `app/agents/candidate.py:413-422`）。

#### 插入 D：`notify.py` 收到邮件后 resume 图执行

位置：邮件规则命中「询问面试时间或进度」分支，替换旧的 `CandidateProcessAgent` 调用。

```python
    from langgraph.types import Command
    from app.workflow.candidate_graph import build_candidate_graph
    from app.service.workflow_thread_store import get_thread_id

    thread_id = await get_thread_id(email.from_.address)
    if not thread_id:
        logger.warning("未找到候选人的工作流线程")
        return
    app = build_candidate_graph(get_checkpointer())
    await app.ainvoke(
        Command(resume=email.text),
        config={"configurable": {"thread_id": thread_id}},
    )
```

---

## 六、修复后的流程图

```
START
  │
screening ──失败──▶ rejected ──▶ END
  │通过
scoring ──≤7──▶ rejected ──▶ END
  │>7
email_invite ──▶ get_candidate_email_reply_node (interrupt)
                      │
        ┌─────────────┴──────────────┐
   candidate_email_reply        == TIMEOUT
   （正常回复）                   （外部调度器注入）
        │                            │
   check_availability          negotiation_expired ──▶ END
        │
   ┌────┼───────────────┬──────────────────┐
SYSTEM_TIME_AVAILABLE  CANDIDATE_REFUSED  其他（不可用）
   │                     │                  │
confirm ─▶ END        refused ─▶ END    negotiation_round >= 上限 ?
                                          │是           │否
                                    negotiation_expired  回到 get_candidate_email_reply_node
                                          ─▶ END         （继续协商）
```

---

## 七、验证

已执行并通过：

```bash
# 1) 图可编译，节点齐全
python -c "from app.workflow.candidate_graph import build_candidate_graph; g=build_candidate_graph(None); print(sorted(g.nodes))"

# 2) 路由单测
python -c "
from app.workflow.candidate_graph import route_after_check_availability as r1, route_after_email_reply as r2
print(r1({'candidate_email_reply':'SYSTEM_TIME_AVAILABLE'}))        # confirmed
print(r1({'candidate_email_reply':'CANDIDATE_REFUSED'}))            # refused
print(r1({'candidate_email_reply':'SYSTEM_TIME_UNAVAILABLE','negotiation_round':1}))  # 继续
print(r1({'candidate_email_reply':'SYSTEM_TIME_UNAVAILABLE','negotiation_round':3}))  # 超时
print(r2({'candidate_email_reply':'TIMEOUT'}))                      # 超时
"
```

节点列表输出应为：`check_availability, confirm, email_invite, get_candidate_email_reply_node, negotiation_expired, refused, rejected, scoring, screening`。

---

## 八、遗留事项（本次未改）

1. **工作流尚未接线**：`build_candidate_graph()` 目前没有生产调用方，仍走旧的 `CandidateProcessAgent`。
2. **重复写库**：`scoring_node` 与 `rejected_node` 都会写 `AI_FILTER_FAILED`，评分失败路径重复写入；`screening` 淘汰也复用 `AI_FILTER_FAILED`，语义待统一。
3. **`email_sent_at` 类型**：state 声明为 `Optional[str]`，实际写入 `datetime.now()`，建议统一。
4. **部分节点副作用不可重入**：发邮件/建日程/写库都在节点内，图重试会重复执行，后续可拆出幂等层。
