# 改动记录

## 概述

本次改动解决三个核心问题：
1. **状态图节点硬编码阈值** — 评分通过阈值、协商轮次上限、面试时间窗口等硬编码在代码中，无法按职位动态调整
2. **异常处理粗糙** — 大量 `raise` 字符串、`except Exception` 吞错、缺少统一异常层级
3. **节点异常无处安放** — 节点中抛出的异常在 BackgroundTasks 中被静默吞掉，数据不一致且用户无感知

---

## 一、硬编码阈值 → Settings 配置化

### 1.1 新增配置项 `app/core/config.py`

```python
# ---------- 招聘流程阈值 ----------
SCORE_PASS_THRESHOLD: int = 7          # AI 评分通过阈值
MAX_NEGOTIATION_ROUNDS: int = 3        # 协商轮次上限
EMAIL_REPLY_TIMEOUT: str = "TIMEOUT"   # 超时恢复标记

# ---------- 面试时间窗口（24小时制） ----------
AM_WINDOW_START_HOUR: int = 9
AM_WINDOW_END_HOUR: int = 12
PM_WINDOW_START_HOUR: int = 14
PM_WINDOW_END_HOUR: int = 18

# 派生属性
am_window → (time(9, 0), time(12, 0))
pm_window → (time(14, 0), time(18, 0))
```

可通过 `.env` 或环境变量覆盖：
```env
SCORE_PASS_THRESHOLD=8
MAX_NEGOTIATION_ROUNDS=5
AM_WINDOW_START_HOUR=8
```

### 1.2 状态定义 `app/workflow/candidate_state.py`

- 移除模块级硬编码常量 `SCORE_PASS_THRESHOLD = 7`、`MAX_NEGOTIATION_ROUNDS = 3`
- 改为从 `Settings` 读取的 `DEFAULT_SCORE_PASS_THRESHOLD`、`DEFAULT_MAX_NEGOTIATION_ROUNDS`
- `CandidateRecruitmentState` 新增可配置字段：
  ```python
  score_pass_threshold: Annotated[Optional[int], assign_state_property]
  max_negotiation_rounds: Annotated[Optional[int], assign_state_property]
  ```
- 调用方可在初始状态注入自定义阈值，未注入时取 Settings 默认值

### 1.3 图路由逻辑 `app/workflow/candidate_graph.py`

- `build_candidate_graph()` 新增可选参数 `score_pass_threshold`、`max_negotiation_rounds`
- `_route_after_scoring()` 从 `state` 中读取阈值，不再引用硬编码常量
- `route_after_check_availability()` 从 `state` 中读取协商轮次上限

### 1.4 评分节点 `app/workflow/candidate_nodes/scoring_node.py`

- 阈值从 `state["score_pass_threshold"]` 读取，默认 7
- 评分通过/拒绝的判断逻辑使用配置化阈值

### 1.5 时间可用性节点 `app/workflow/candidate_nodes/check_availability_node.py`

- `DAILY_WINDOWS` 从 `Settings` 读取，不再硬编码 `(time(9,0), time(12,0))`
- 支持通过环境变量调整面试时间窗口

### 1.6 任务入口 `app/tasks/agent_tasks.py`

- `run_candidate_agent()` 新增 `score_pass_threshold`、`max_negotiation_rounds` 可选参数
- 图调用时将阈值注入初始状态
- 修复了原文件中 `from envs.myenv.Lib.asyncio import graph` 等错误的导入路径

### 改动前后对比

| 场景 | 改动前 | 改动后 |
|------|--------|--------|
| 调整评分阈值 | 改代码 → 重启 | 改 `.env` → 重启 |
| 调整协商轮次 | 改代码 → 重启 | 改 `.env` → 重启 |
| 调整面试时间窗口 | 改代码 → 重启 | 改 `.env` → 重启 |
| 按职位覆盖阈值 | 不支持 | 调用时传入参数即可 |
| 图路由逻辑 | 引用模块常量 | 从 state 读取 |

---

## 二、异常处理重构

### 2.1 新增异常层级 `app/core/exceptions.py`

```
AppException (基类)
├── BusinessException (HTTP 400 范畴)
│   ├── ResumeProcessingError       # 简历处理
│   ├── CandidateNotFoundError      # 候选人不存在
│   ├── InterviewSchedulingError    # 面试排期
│   ├── TokenExpiredError           # 令牌过期
│   ├── TokenInvalidError           # 令牌无效
│   ├── DingTalkAPIError            # 钉钉 API 业务错误
│   ├── EmailSendError              # 邮件发送
│   ├── StatusFlowError             # 状态流转非法
│   └── NegotiationError            # 协商异常
└── ExternalServiceError (HTTP 502/503 范畴)
    ├── OCRServiceError             # OCR 服务
    ├── LLMServiceError             # 大模型服务
    └── DingTalkServiceError        # 钉钉服务
```

所有异常携带 `message` + `code` 字段，便于日志追踪和前端展示。

### 2.2 全局异常处理器 `app/main.py`

新增三个 `@app.exception_handler`：
- `BusinessException` → HTTP 400
- `ExternalServiceError` → HTTP 502
- `AppException`（兜底） → HTTP 500

### 2.3 改动涉及的文件

#### `app/agents/candidate.py`

| 改动点 | 改动前 | 改动后 |
|--------|--------|--------|
| `get_access_token()` | 返回 `str` 表示错误，调用方 `isinstance` 判断 | 统一 `raise DingTalkTokenError`，返回值类型为 `tuple[str, dict]` |
| `score_for_candidate` | `except Exception` 吞错 + 返回字符串 | `raise LLMServiceError`，保留原始异常链 |
| `get_interviewer_available_slot` | `raise f"..."` 字符串 | `raise DingTalkServiceError / InterviewSchedulingError` |
| `send_interview_email` | `return f"失败: {e}"` | `raise EmailSendError` |
| `confirm_interview_time` | `raise f"..."` + `isinstance(result, str)` | `raise InterviewSchedulingError / EmailSendError / DingTalkServiceError` |
| `refuse_interview` | `except` 吞错后返回通用字符串 | `raise InterviewSchedulingError` |

#### `app/service/dingtalk_ser.py`

| 改动点 | 改动前 | 改动后 |
|--------|--------|--------|
| `get_union_id_and_access_token_by_user_id()` | `raise f"..."` + 返回 `str` | `raise DingTalkServiceError`，返回类型标注 `tuple[str, dict]` |
| `get_user_available_slots()` | `raise f"..."` | `raise DingTalkServiceError` |

#### `app/workflow/candidate_nodes/check_availability_node.py`

| 改动点 | 改动前 | 改动后 |
|--------|--------|--------|
| `_extract_time_from_reply` | `raise ValueError` | `raise NegotiationError` |
| `can_not_find_available_time` | `raise ValueError` | `raise EmailSendError` |
| `_parse_proposed_time` | `raise ValueError` | `raise NegotiationError` |
| `check_availability_node` | `raise ValueError` + 无具体类型 | `raise DingTalkServiceError / NegotiationError` |

#### `app/workflow/candidate_nodes/confirm_node.py`

| 改动点 | 改动前 | 改动后 |
|--------|--------|--------|
| 时间格式化 | `return f"失败"` | `raise InterviewSchedulingError` |
| 发邮件 | `return f"失败"` | `raise EmailSendError` |
| 创建钉钉日程 | `raise f"..."` | `raise DingTalkServiceError` |
| 创建面试记录 | `raise f"..."` | `raise InterviewSchedulingError` |

#### `app/workflow/candidate_nodes/email_invite_node.py`

| 改动点 | 改动前 | 改动后 |
|--------|--------|--------|
| 发送邮件 | `raise ValueError` | `raise EmailSendError` |
| 获取可用时间 | 无异常处理 | `raise DingTalkServiceError` |

#### `app/workflow/candidate_nodes/scoring_node.py`

| 改动点 | 改动前 | 改动后 |
|--------|--------|--------|
| LLM 调用 | `except Exception` 吞错 | `raise LLMServiceError` |
| 数据库写入 | `except Exception` 吞错 | `raise LLMServiceError` |

#### `app/service/candidate_ser.py`

| 改动点 | 改动前 | 改动后 |
|--------|--------|--------|
| 文件写入 | `except Exception` | `except OSError` → `raise ResumeProcessingError` |
| Word 转 PDF | `except Exception` | `raise ResumeProcessingError` |

---

## 三、改动影响评估

### 兼容性

- ✅ 所有阈值默认值与改动前完全一致（`SCORE_PASS_THRESHOLD=7`、`MAX_NEGOTIATION_ROUNDS=3`）
- ✅ 状态图路由逻辑接口不变，仅内部读取来源变化
- ✅ 异常均为新增类，不影响已有 `HTTPException` 的处理路径

### 行为变化

| 变化点 | 影响 |
|--------|------|
| `get_access_token()` 不再返回字符串 | 调用方已同步更新，不再需要 `isinstance(result, str)` 判断 |
| 异常类型更具体 | 日志更清晰，便于排查问题；前端可展示更精准的错误信息 |
| 全局异常处理器 | 未捕获的 `BusinessException`/`ExternalServiceError` 自动转为标准 HTTP 响应 |

---

## 三、节点异常处理架构（新增）

### 3.1 问题分析

LangGraph 节点中的异常与 HTTP 路由中的异常行为完全不同：

```
HTTP 异常路径:
  Router → raise BusinessException → FastAPI 全局异常处理器 → HTTP 400 JSON ✅

节点异常路径（改动前）:
  Node → raise EmailSendError → LangGraph 中断 → BackgroundTasks 静默吞掉
  → 数据不一致、用户无感知、日志仅有 traceback ❌
```

### 3.2 解决方案：分层异常处理

```
┌──────────────────────────────────────────────────────────────────┐
│  LangGraph 状态图节点                                             │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │ node_error_wrapper (装饰器)                              │     │
│  │                                                         │     │
│  │  BusinessException ──→ 返回 error state ──→ error 节点  │     │
│  │  ExternalServiceError ──→ 向上抛出（不捕获）            │     │
│  │  未知 Exception ──→ 包装为 AppException 向上抛出        │     │
│  └─────────────────────────────────────────────────────────┘     │
│                            │                                     │
│                            ↓                                     │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │ error_node (错误终结点)                                  │     │
│  │  • 记录错误日志                                          │     │
│  │  • 更新候选人状态 → PROCESSING_ERROR                     │     │
│  └─────────────────────────────────────────────────────────┘     │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│  run_candidate_agent (外层包装)                                   │
│  • 捕获 ExternalServiceError / AppException                      │
│  • 调用 _mark_processing_error() 更新候选人状态                   │
│  • 重新抛出（供调用方感知）                                       │
└──────────────────────────────────────────────────────────────────┘
```

### 3.3 新增文件

| 文件 | 职责 |
|------|------|
| `app/workflow/error_handler.py` | `node_error_wrapper` 装饰器 + `error_node` 错误终结点 |

### 3.4 异常分流矩阵

| 异常类型 | 来源示例 | 处理方式 | 最终结果 |
|----------|----------|----------|----------|
| `InterviewSchedulingError` | 面试官无空闲时间 | wrapper 捕获 → error state | 候选人 → `PROCESSING_ERROR` |
| `NegotiationError` | 时间格式解析失败 | wrapper 捕获 → error state | 候选人 → `PROCESSING_ERROR` |
| `EmailSendError` | SMTP 连接超时 | 向上抛出 → 外层 catch | 候选人 → `PROCESSING_ERROR` |
| `DingTalkServiceError` | 钉钉 API 500 | 向上抛出 → 外层 catch | 候选人 → `PROCESSING_ERROR` |
| `LLMServiceError` | DeepSeek 服务不可用 | 向上抛出 → 外层 catch | 候选人 → `PROCESSING_ERROR` |
| `OCRServiceError` | PaddleOCR 失败 | 向上抛出 → 外层 catch | 候选人 → `PROCESSING_ERROR` |

### 3.5 状态图新增节点和边

```
screening ──error──→ error ──→ END
scoring   ──error──→ error ──→ END
email_invite ─error→ error ──→ END
check_availability ─error→ error ──→ END
confirm   ──error──→ error ──→ END
rejected  ──error──→ error ──→ END
refused   ──error──→ error ──→ END
negotiation_expired ─error→ error ──→ END
```

### 3.6 新增候选人状态

```python
# app/models/candidate.py
class CandidateStatusEnum(StrEnum):
    ...
    PROCESSING_ERROR = "处理异常"  # 新增：基础设施故障，可重试
```

### 3.7 状态定义扩展

```python
# app/workflow/candidate_state.py
class CandidateRecruitmentState(TypedDict):
    ...
    # 错误信息
    error_occurred: Annotated[Optional[bool], assign_state_property]
    error_message: Annotated[Optional[str], assign_state_property]
    error_stage: Annotated[Optional[str], assign_state_property]
    # current_stage Literal 新增 "error"
```

### 3.8 改动前后对比

| 场景 | 改动前 | 改动后 |
|------|--------|--------|
| LLM 评分服务宕机 | 异常被 BackgroundTasks 吞掉，候选人卡在 `AI_FILTER_PASSED` | 外层 catch → 标记 `PROCESSING_ERROR` → 可重试 |
| 发送邮件失败 | 异常中断流程，面试记录已创建但候选人未收到确认 | 外层 catch → 标记 `PROCESSING_ERROR` → 日志报警 |
| 面试官无空闲时间 | `raise ValueError` 导致流程中断 | wrapper 捕获 → error 节点 → 标记 `PROCESSING_ERROR` |
| 候选人状态一致性 | 部分操作成功、部分失败 → 数据不一致 | 异常时统一标记 `PROCESSING_ERROR` → 明确可重试 |
| 问题排查 | 只有 traceback，不知道哪个候选人受影响 | 日志含候选人 ID + 阶段 + 错误原因 |

### 3.9 关键设计决策

**为什么不全在节点内 catch 返回 error state？**

基础设施异常（如 SMTP 超时）如果也转为 error state 静默处理，运维无法感知服务故障。抛出后由外层统一告警（日志 + 状态标记），便于监控和重试。

**为什么不用 LangGraph 的 retry 机制？**

LangGraph 的 `retry_policy` 适用于可立即重试的瞬时故障（如网络抖动）。当前场景的异常（如 LLM 服务宕机、SMTP 认证失败）需要人工介入或等待服务恢复，不适合自动重试。标记为 `PROCESSING_ERROR` 后，管理后台可以手动触发重试。

---

## 四、后续可优化项

1. **密码哈希**：`init_data.py` 中明文密码应使用 Argon2 哈希后存入（项目已依赖 `pwdlib[argon2]`）
2. **Agent 单例**：`candidate.py` 中 `score_for_candidate` 每次调用都重建 Agent，建议提为模块级单例
3. **状态图节点单元测试**：补充对 `screening_node`、`scoring_node`、`check_availability_node` 的单元测试
4. **类型标注**：逐步消除 `Any`、`cast`、`# type: ignore`，补全 TypedDict 类型
5. **CI 流水线**：接入 ruff + pyright + pytest，自动化检查
6. **重试 API**：为 `PROCESSING_ERROR` 状态的候选人提供手动重试接口
