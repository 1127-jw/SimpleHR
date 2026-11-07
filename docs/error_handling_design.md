# 异常处理与可观测性设计详解

> 本文档详细说明项目中 7 个核心工程亮点：分层异常架构、LangGraph 节点安全网、BackgroundTask 安全、PROCESSING_ERROR 状态、@wraps 元信息保留、结构化路由日志、error_node 防御性设计。

---

## 目录

1. [全景图](#一全景图)
2. [分层异常架构](#二分层异常架构)
3. [LangGraph 节点安全网](#三langgraph-节点安全网)
4. [BackgroundTask 安全](#四backgroundtask-安全)
5. [候选人 PROCESSING_ERROR 状态](#五候选人-processing_error-状态)
6. [@wraps 保留元信息](#六wraps-保留元信息)
7. [结构化路由日志](#七结构化路由日志)
8. [error_node 防御性](#八error_node-防御性)
9. [端到端异常流追踪](#九端到端异常流追踪)

---

## 一、全景图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         FastAPI 路由层                                   │
│  @app.exception_handler(BusinessException)    → HTTP 400                │
│  @app.exception_handler(ExternalServiceError) → HTTP 502                │
│  @app.exception_handler(AppException)         → HTTP 500                │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    BackgroundTasks 调用层                                │
│  background_tasks.add_task(run_candidate_agent, ...)                    │
│  ⚠️ 未捕获异常会被静默丢弃（仅打印 stderr）                              │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                  run_candidate_agent（外层兜底）                          │
│  try:                                                                   │
│      graph.ainvoke(...)                                                 │
│  except (ExternalServiceError, AppException) as e:                      │
│      结构化日志 + _mark_processing_error() + return None                │
│      ❌ 不 re-raise（会被 BackgroundTasks 吞掉）                          │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    LangGraph 状态图                                      │
│  ┌──────────────────────────────────────────────────────────────┐       │
│  │           @node_error_wrapper（节点安全网）                    │       │
│  │  BusinessException → 返回 error state → 路由到 error 节点     │       │
│  │  ExternalServiceError → 不捕获，向上传播                      │       │
│  │  未知 Exception → 包装为 AppException 向上传播                │       │
│  └──────────────────────────────────────────────────────────────┘       │
│                                    │                                     │
│                                    ▼                                     │
│  ┌──────────────────────────────────────────────────────────────┐       │
│  │                    error_node（错误终结点）                    │       │
│  │  1. 记录错误日志（含 candidate_id + stage + error）           │       │
│  │  2. try: 更新候选人状态 → PROCESSING_ERROR                    │       │
│  │     except: logger.critical(...) ← 防御性兜底                 │       │
│  └──────────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 二、分层异常架构

### 2.1 设计目标

项目中存在两类性质完全不同的错误，需要区别对待：

| 错误类型 | 例子 | 处理方式 | 面向对象 |
|----------|------|----------|----------|
| **业务异常** | 面试官没有空闲时间、时间格式解析失败 | 转为错误状态，让状态图优雅终止 | 用户 / HR |
| **基础设施异常** | LLM 服务宕机、SMTP 连接超时、钉钉 API 500 | 向上传播，标记 `PROCESSING_ERROR` 供人工重试 | 运维 / 开发 |

### 2.2 异常层级

```
AppException (基类)
├── message: str          # 错误信息
├── code: str             # 错误码（默认取类名）
│
├── BusinessException          # 业务逻辑错误（HTTP 400）
│   ├── ResumeProcessingError      # 简历处理
│   ├── CandidateNotFoundError     # 候选人不存在
│   ├── InterviewSchedulingError   # 面试排期
│   ├── TokenExpiredError          # 令牌过期
│   ├── TokenInvalidError          # 令牌无效
│   ├── DingTalkAPIError           # 钉钉 API 业务错误
│   ├── EmailSendError             # 邮件发送
│   ├── StatusFlowError            # 状态流转非法
│   └── NegotiationError           # 协商异常
│
└── ExternalServiceError       # 第三方服务错误（HTTP 502/503）
    ├── OCRServiceError             # OCR 服务
    ├── LLMServiceError             # 大模型服务
    └── DingTalkServiceError        # 钉钉服务
```

### 2.3 基类实现

```python
# app/core/exceptions.py
class AppException(Exception):
    """应用异常基类。"""

    def __init__(self, message: str, code: Optional[str] = None):
        self.message = message
        self.code = code or self.__class__.__name__
        super().__init__(self.message)
```

**设计要点：**

- `message` 和 `code` 分离：`message` 给人看，`code` 给程序/前端用
- `code` 默认取类名，便于快速定位错误类型
- 继承自 `Exception`，兼容 Python 原生异常处理

### 2.4 全局异常处理器

```python
# app/main.py
@app.exception_handler(BusinessException)
async def business_exception_handler(request: Request, exc: BusinessException):
    """业务异常 → HTTP 400。"""
    return JSONResponse(
        status_code=400,
        content={"detail": exc.message, "code": exc.code},
    )

@app.exception_handler(ExternalServiceError)
async def external_service_exception_handler(request: Request, exc: ExternalServiceError):
    """第三方服务异常 → HTTP 502。"""
    return JSONResponse(
        status_code=502,
        content={"detail": exc.message, "code": exc.code},
    )

@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    """兜底应用异常 → HTTP 500。"""
    return JSONResponse(
        status_code=500,
        content={"detail": exc.message, "code": exc.code},
    )
```

### 2.5 改动前后对比

| 改动前 | 改动后 |
|--------|--------|
| `raise ValueError("给候选人发送邮件失败")` | `raise EmailSendError("给候选人发送邮件失败")` |
| `raise f"获取面试官可用时间失败：{e}"` | `raise DingTalkServiceError(f"获取面试官可用时间失败: {e}")` |
| `except Exception: return f"失败: {e}"` | `except Exception as e: raise LLMServiceError(...) from e` |
| 异常信息是字符串，无结构 | 异常携带 `message` + `code`，可被全局处理器统一转换 |
| 无法区分业务错误和服务故障 | 清晰分层，处理策略不同 |

---

## 三、LangGraph 节点安全网

### 3.1 问题背景

LangGraph 节点函数**不能抛出异常**——一旦抛出，异常会穿过 LangGraph 引擎，导致：

1. 状态图执行中断
2. 检查点状态损坏
3. 异常最终被 BackgroundTasks 静默吞掉

因此需要一种机制，在节点内部捕获异常，转化为状态图可处理的**错误状态**。

### 3.2 解决方案：@node_error_wrapper 装饰器

```python
# app/workflow/error_handler.py
def node_error_wrapper(
    node_func: Callable[[CandidateRecruitmentState], Awaitable[dict]],
) -> Callable[[CandidateRecruitmentState], Awaitable[dict]]:

    @wraps(node_func)
    async def wrapped(state: CandidateRecruitmentState) -> dict:
        try:
            return await node_func(state)
        except BusinessException as e:
            # 业务异常 → 返回错误状态
            logger.warning(...)
            return {
                "current_stage": "error",
                "error_occurred": True,
                "error_message": e.message,
                "error_stage": state.get("current_stage"),
            }
        except ExternalServiceError:
            raise  # 基础设施异常 → 不捕获
        except Exception as e:
            # 未知异常 → 防御性包装
            raise AppException(...) from e

    return wrapped
```

### 3.3 三层捕获逻辑

| 异常类型 | 处理方式 | 设计理由 |
|----------|----------|----------|
| `BusinessException` | 捕获 → 返回 error state | 业务异常是「预期内的」，状态图应优雅终止 |
| `ExternalServiceError` | 不捕获，向上传播 | 基础设施故障需要运维感知，不能静默 |
| 其他 `Exception` | 包装为 `AppException` 后传播 | 防御性编程，避免裸露内置异常穿透 |

### 3.4 使用方式

```python
# screening_node.py
from app.workflow.error_handler import node_error_wrapper

@node_error_wrapper
async def screening_node(state: CandidateRecruitmentState) -> dict:
    ...
```

**为什么用装饰器而不是在每个节点里手写 try/catch？**

- 9 个节点的异常处理逻辑几乎相同，手写是重复劳动
- 新增节点只需加 `@node_error_wrapper`，不会遗漏
- 修改异常处理逻辑只需改一处

### 3.5 装饰器 vs 手动包装对比

| 方式 | 优点 | 缺点 |
|------|------|------|
| 手动 `graph.add_node("x", wrapper(node))` | 集中注册 | 容易遗漏，新增节点时忘记包装 |
| `@node_error_wrapper` 装饰器 | 声明式，不会遗漏 | 装饰器在模块加载时生效，调试时多一层 |

项目最终选择了装饰器方案，因为**不会遗漏**这一点在生产环境中更重要。

---

## 四、BackgroundTask 安全

### 4.1 问题发现

初始设计中 `run_candidate_agent` 的 except 块会 `raise` 重新抛出异常：

```python
except (ExternalServiceError, AppException) as e:
    logger.error(...)
    await _mark_processing_error(candidate.id, e.message)
    raise   # ← 问题所在
```

但实际调用链是：

```
Router → background_tasks.add_task(run_candidate_agent, ...)
                              │
                              ▼
                    run_candidate_agent 内部 raise
                              │
                              ▼
                    BackgroundTasks 捕获 → 打印 traceback → 静默丢弃 ❌
```

**FastAPI/Starlette 的 `BackgroundTasks` 会捕获所有后台任务中的异常并静默丢弃**，`raise` 毫无意义。

### 4.2 修复方案

```python
# app/tasks/agent_tasks.py
except (ExternalServiceError, AppException) as e:
    # 注意：不 re-raise！BackgroundTasks 会吞掉异常，raise 毫无意义。
    logger.opt(exception=True).error(
        f"[agent_task] candidate_id={candidate.id} | infra_error={e.message} | code={e.code}"
    )
    await _mark_processing_error(candidate.id, e.message)
    return None   # ← 静默返回，不抛出
```

### 4.3 关键设计决策

| 决策 | 原因 |
|------|------|
| **不 re-raise** | BackgroundTasks 会吞掉异常，raise 无法传递 |
| **结构化日志** | 日志包含 `candidate_id` + `infra_error` + `code` + 完整 traceback |
| **更新 DB 状态** | 即使异常被吞，候选人状态已标记为 `PROCESSING_ERROR`，管理后台可查 |
| **return None** | 明确告知调用方流程异常结束，而非返回错误的状态图结果 |

### 4.4 改动前后对比

| 场景 | 改动前 | 改动后 |
|------|--------|--------|
| LLM 服务宕机 | `raise` 被 BackgroundTasks 吞掉，stderr 有无结构 traceback | 结构化日志 + 候选人状态更新 |
| 运维感知 | 依赖扫 stderr，无法按候选人筛选 | 日志含 candidate_id，可精确追踪 |
| 数据一致性 | DB 状态可能卡在中间态 | 统一标记 `PROCESSING_ERROR` |

---

## 五、候选人 PROCESSING_ERROR 状态

### 5.1 新增状态

```python
# app/models/candidate.py
class CandidateStatusEnum(StrEnum):
    APPLICATION = "已投递"
    AI_FILTER_FAILED = "AI筛选失败"
    AI_FILTER_PASSED = "AI筛选成功"
    WAITING_FOR_INTERVIEW = "待面试"
    REFUSED_INTERVIEW = "拒绝面试"
    INTERVIEW_PASSED = "面试通过"
    INTERVIEW_REJECTED = "面试未通过"
    HIRED = "已入职"
    REJECTED = "已拒绝"
    NEGOTIATION_EXPIRED = "协商超时"
    PROCESSING_ERROR = "处理异常"   # ← 新增
```

### 5.2 状态含义

`PROCESSING_ERROR` 表示**基础设施故障导致的流程中断**，区别于正常业务流转：

| 状态 | 含义 | 是否可恢复 |
|------|------|-----------|
| `AI_FILTER_FAILED` | AI 评分不达标 | 终态，不可恢复 |
| `NEGOTIATION_EXPIRED` | 候选人超时未回复 | 终态，不可恢复 |
| `PROCESSING_ERROR` | LLM/邮件/钉钉服务故障 | **可重试**，等待服务恢复后重新触发 |

### 5.3 状态流转图

```
已投递 ──▶ AI筛选成功 ──▶ 待面试 ──▶ 面试通过 ──▶ 已入职
   │           │            │
   │           │            ├──▶ 面试未通过（终态）
   │           │            │
   │           │            └──▶ 处理异常 ◀── 可重试
   │           │
   │           └──▶ AI筛选失败（终态）
   │
   └──▶ 处理异常 ◀── 可重试
```

### 5.4 触发路径

```
路径 1：节点内 BusinessException
  node_error_wrapper 捕获
    → 返回 error state
    → 路由到 error_node
    → error_node 更新状态为 PROCESSING_ERROR

路径 2：节点内 ExternalServiceError
  node_error_wrapper 不捕获
    → 传播到 run_candidate_agent
    → _mark_processing_error() 更新状态为 PROCESSING_ERROR

路径 3：未知异常
  node_error_wrapper 包装为 AppException
    → 传播到 run_candidate_agent
    → _mark_processing_error() 更新状态为 PROCESSING_ERROR
```

### 5.5 运维价值

```
管理后台 SQL:
SELECT * FROM candidates WHERE status = '处理异常';

→ 得到所有因基础设施故障中断的候选人列表
→ 服务恢复后，手动/批量重新触发招聘流程
```

---

## 六、@wraps 保留元信息

### 6.1 问题背景

装饰器会创建一个新的函数 `wrapped`，如果没有 `@wraps`：

```python
def node_error_wrapper(node_func):
    async def wrapped(state):
        ...
    return wrapped
```

此时 `wrapped.__name__` 是 `"wrapped"`，而不是原始的 `"scoring_node"`。

### 6.2 影响

在 LangGraph 中，节点名用于：

1. **日志输出**：`[error_wrap] %s BusinessException` 中的 `%s` 会显示 `wrapped`
2. **图可视化**：Graphviz 渲染的节点名会显示 `wrapped`
3. **调试追踪**：出错时不知道是哪个节点

### 6.3 解决方案

```python
from functools import wraps

def node_error_wrapper(node_func):
    @wraps(node_func)   # ← 关键
    async def wrapped(state):
        ...
    return wrapped
```

`@wraps` 会复制原函数的元信息：

| 属性 | 没有 @wraps | 有 @wraps |
|------|-------------|-----------|
| `__name__` | `"wrapped"` | `"scoring_node"` |
| `__doc__` | `None` | 原函数的 docstring |
| `__module__` | `"app.workflow.error_handler"` | `"app.workflow.candidate_nodes.scoring_node"` |
| `__qualname__` | `"node_error_wrapper.<locals>.wrapped"` | `"scoring_node"` |

### 6.4 实际效果

```python
# 有 @wraps 时，日志输出：
[error_wrap] scoring_node BusinessException: 面试官 7 天内无空闲时间 (code=InterviewSchedulingError)

# 没有 @wraps 时，日志输出：
[error_wrap] wrapped BusinessException: 面试官 7 天内无空闲时间 (code=InterviewSchedulingError)
```

---

## 七、结构化路由日志

### 7.1 设计目标

招聘流程有多个分支路由，出问题时需要快速定位：
- 哪个候选人？
- 在哪个阶段？
- 路由到了哪个节点？
- 为什么这样路由？

### 7.2 路由日志实现

每个路由函数在执行时记录结构化日志：

```python
# app/workflow/candidate_graph.py
def route_after_check_availability(state: CandidateRecruitmentState) -> str:
    """check_availability 分支路由（含协商轮次上限）。"""
    cid = state["candidate"].id
    reply = state.get("candidate_email_reply", "")
    if reply == "CANDIDATE_REFUSED":
        dest = "refused"
    elif reply == "SYSTEM_TIME_AVAILABLE":
        dest = "confirmed"
    else:
        max_rounds = state.get("max_negotiation_rounds") or DEFAULT_MAX_NEGOTIATION_ROUNDS
        if state.get("negotiation_round", 0) >= max_rounds:
            dest = "negotiation_expired"
        else:
            dest = "get_candidate_email_reply_node"
    logger.info(
        "[route_check_avail] candidate_id=%s | reply=%.30s | round=%d | dest=%s",
        cid, reply, state.get("negotiation_round", 0), dest,
    )
    return dest
```

### 7.3 日志格式规范

```
[路由函数名] candidate_id=<候选人ID> | <关键上下文> | dest=<目标节点>
```

| 路由函数 | 日志内容 |
|----------|----------|
| `route_after_email_reply` | `candidate_id` + `reply`(前50字符) + `dest` |
| `route_after_check_availability` | `candidate_id` + `reply`(前30字符) + `round` + `dest` |
| `_route_after_scoring` | `candidate_id` + `score` + `threshold` + `dest` |

### 7.4 节点日志

每个节点内部也有结构化日志：

```python
# scoring_node.py
logger.info("[scoring] start | candidate_id=%s | threshold=%d", cid, threshold)
logger.info("[scoring] LLM scored | candidate_id=%s | overall_score=%d", cid, ...)
logger.info("[scoring] done | candidate_id=%s | score=%d | next_stage=%s", ...)
```

### 7.5 日志输出示例

```
2024-01-15 10:23:45 | INFO  | candidate_graph:route_after_scoring| [route_scoring] candidate_id=abc123 | score=8 | threshold=7 | dest=email_invite
2024-01-15 10:23:46 | INFO  | scoring_node:scoring_node        | [scoring] done | candidate_id=abc123 | score=8 | next_stage=email_invite
2024-01-15 10:23:47 | INFO  | email_invite_node:email_invite_node | [email_invite] start | candidate_id=abc123
2024-01-15 10:23:48 | INFO  | check_availability_node:get_candidate_email_reply_node | [email_reply] waiting for candidate reply | candidate_id=abc123
2024-01-15 10:25:12 | INFO  | check_availability_node:get_candidate_email_reply_node | [email_reply] resumed | candidate_id=abc123 | reply=我明天下午3点方便
2024-01-15 10:25:13 | INFO  | candidate_graph:route_after_check_availability | [route_check_avail] candidate_id=abc123 | reply=我明天下午3点方便 | round=1 | dest=get_candidate_email_reply_node
```

### 7.6 日志文件配置

```python
# app/core/logging_config.py
# 全量日志：logs/workflow.log（按天轮转，保留 30 天）
# 错误日志：logs/error.log（按大小轮转 50MB，保留 90 天）
# 节点日志：logs/nodes.log（按天轮转，保留 60 天）
```

---

## 八、error_node 防御性

### 8.1 设计目标

`error_node` 是错误处理的**最后一环**，它自身也不能失败——否则异常会再次穿透 LangGraph。

### 8.2 实现

```python
# app/workflow/error_handler.py
async def error_node(state: CandidateRecruitmentState) -> dict:
    """错误终结点节点。"""
    candidate = state["candidate"]
    cid = candidate.id
    error_msg = state.get("error_message", "未知错误")
    error_stage = state.get("error_stage", "unknown")

    logger.error(
        "[error_node] candidate_id=%s(%s) | stage=%s | error=%s",
        cid, candidate.name, error_stage, error_msg,
    )

    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                repo = CandidateRepo(session)
                await repo.update_candidate_status(
                    candidate_id=cid,
                    status=CandidateStatusEnum.PROCESSING_ERROR,
                )
    except Exception as e:
        # 如果连状态更新都失败了，至少记录日志
        logger.critical(
            "[error_node] failed to update status | candidate_id=%s",
            cid, exc_info=True,
        )

    return {"current_stage": "error"}
```

### 8.3 防御层次

| 层次 | 作用 |
|------|------|
| **第一层：try/except** | 捕获状态更新可能失败的情况（如数据库连接断开） |
| **第二层：logger.critical** | 即使更新失败，也用最高级别日志记录，确保触发告警 |
| **第三层：return 正常值** | `error_node` 始终返回 `{"current_stage": "error"}`，不重新抛出异常 |

### 8.4 为什么不重新抛出？

```
error_node 抛出异常
    │
    ▼
LangGraph 引擎捕获 → 但 error_node 已是最后一个节点，无处可去
    │
    ▼
异常传播到 run_candidate_agent
    │
    ▼
外层 except 捕获 → 但 _mark_processing_error() 也会失败（同样的 DB 问题）
    │
    ▼
最终：异常被 BackgroundTasks 吞掉，且状态未更新
```

**结论**：`error_node` 内必须「自我兜底」，即使失败也只记录日志，不重新抛出。

### 8.5 防御性设计总结

| 场景 | 行为 |
|------|------|
| 正常情况 | 更新候选人状态为 `PROCESSING_ERROR`，返回 error state |
| DB 连接断开 | `logger.critical` 记录完整 traceback，返回 error state |
| 状态更新成功但提交失败 | 事务回滚，`logger.critical` 记录，返回 error state |

---

## 九、端到端异常流追踪

### 9.1 业务异常路径

```
1. scoring_node 调用 LLM 评分
   └─ raise InterviewSchedulingError("面试官 7 天内无空闲时间")

2. @node_error_wrapper 捕获
   ├─ 匹配 except BusinessException:
   ├─ logger.warning("[error_wrap] scoring_node BusinessException: ...")
   └─ return {"current_stage": "error", "error_occurred": True, ...}

3. LangGraph 引擎
   ├─ 合并状态到全局 state
   ├─ 路由函数检测到 error_occurred=True
   └─ 流转到 "error" 节点

4. error_node 执行
   ├─ logger.error("[error_node] candidate_id=abc123 | stage=scoring | error=...")
   ├─ 更新候选人状态 → PROCESSING_ERROR
   └─ return {"current_stage": "error"} → 流转到 END

5. run_candidate_agent 检查最终状态
   └─ final_stage == "error" → logger.warning(...)
```

### 9.2 基础设施异常路径

```
1. scoring_node 调用 LLM 评分
   └─ raise LLMServiceError("AI 评分调用失败: timeout")

2. @node_error_wrapper 不捕获
   └─ raise（向上传播）

3. LangGraph 引擎
   └─ ainvoke() 抛出 LLMServiceError

4. run_candidate_agent 外层 except
   ├─ logger.error("[agent_task] candidate_id=abc123 | infra_error=... | code=...")
   ├─ await _mark_processing_error(abc123, "AI 评分调用失败: timeout")
   │   └─ 更新候选人状态 → PROCESSING_ERROR
   └─ return None（不 re-raise）
```

### 9.3 未知异常路径

```
1. confirm_node 内部 bug（如 NoneType 属性访问）
   └─ raise AttributeError("'NoneType' has no attribute 'id'")

2. @node_error_wrapper 捕获
   ├─ 匹配 except Exception:
   ├─ logger.error("[error_wrap] confirm_node unexpected exception | ...")
   └─ raise AppException("节点 confirm_node 未知异常: ...") from e

3. LangGraph 引擎
   └─ ainvoke() 抛出 AppException

4. run_candidate_agent 外层 except
   ├─ 匹配 except (ExternalServiceError, AppException):
   ├─ logger.error(...)
   ├─ await _mark_processing_error(...)
   └─ return None
```

---

## 十、相关文件索引

| 文件 | 职责 |
|------|------|
| `app/core/exceptions.py` | 异常层级定义（12 个异常类） |
| `app/workflow/error_handler.py` | `@node_error_wrapper` + `error_node` |
| `app/tasks/agent_tasks.py` | `run_candidate_agent` 外层兜底 + `_mark_processing_error` |
| `app/models/candidate.py` | `PROCESSING_ERROR` 状态定义 |
| `app/workflow/candidate_graph.py` | 路由函数 + 结构化日志 |
| `app/workflow/candidate_nodes/*.py` | 各节点使用 `@node_error_wrapper` |
| `app/main.py` | 全局异常处理器 |
| `app/core/logging_config.py` | 日志配置（轮转 + 分级） |

---

## 十一、设计原则总结

| 原则 | 体现 |
|------|------|
| **异常分级** | 业务异常 vs 基础设施异常，处理策略不同 |
| **状态机友好** | 节点不抛异常，返回错误状态让状态图处理 |
| **外层兜底** | `run_candidate_agent` 完全处理异常，不依赖 BackgroundTasks |
| **可观测** | 全链路结构化日志，`candidate_id` 贯穿始终 |
| **防御性** | `error_node` 自身也能失败，但至少记录日志 |
| **可重试** | `PROCESSING_ERROR` 状态明确标记，供管理后台筛选重试 |
| **元信息保留** | `@wraps` 确保日志中的节点名正确 |
