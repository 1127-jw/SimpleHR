# SimpleJob 面试题库（基于真实代码实现）

> 说明：题目按「面试官提问频率 + 代码模块」组织。每题给出**考察点**和**答题要点**，
> 要点都对应仓库里的真实实现（括号内为代码位置）。带 ⭐ 的是高频题。
>
> 建议配合 `README.md`、`RESUME_PROJECT.md`、`docs/error_handling_design.md` 一起复习。

---

## 0. 高频 Top 20（时间紧只看这些）

1. ⭐ 这个项目做了什么？核心流程是什么？
2. ⭐ 为什么用 LangGraph 状态机，而不用 LLM Agent 自由决策？
3. ⭐ `interrupt/resume` 是怎么实现「等候选人回邮件」的？
4. ⭐ 状态图节点抛异常会发生什么？你的异常分层怎么设计？
5. ⭐ `AsyncPostgresSaver` 检查点为什么用进程级单例？`thread_id` 为什么用邮箱？
6. ⭐ 业务异常和基础设施异常为什么要分开处理？
7. ⭐ 为什么 `run_candidate_agent` 不能 re-raise 异常？
8. ⭐ 面试时间多轮协商怎么实现？怎么防止死循环？
9. ⭐ 人工重试（`Command(goto="email_invite")`）为什么保留旧检查点？
10. ⭐ JWT 双令牌 + Redis 里 refresh token 的原子消费（Lua）解决什么问题？
11. ⭐ 登录限流为什么用 ZSET 滑动窗口 + Lua，而不是固定窗口/计数器？
12. ⭐ 日志为什么用队列 + 独立监听进程？多进程为什么不能用 `uvicorn --workers N`？
13. ⭐ `request_id` 为什么用 `contextvars` 而不是 `threading.local`？
14. ⭐ 简历解析流水线为什么用「后台任务 + Redis 状态轮询」？
15. ⭐ 邮件机器人怎么保证幂等（UID 游标 + Message-ID 去重）？
16. ⭐ 状态字段为什么用 `StrEnum` + `values_callable`？PostgreSQL 枚举为什么要改 VARCHAR？
17. ⭐ `selectinload` 和 `lazy="joined"` 的区别？为什么改掉 joined？
18. ⭐ 时区问题：naive vs aware，`BEIJING_TZ`，DB 里存什么时间？
19. ⭐ 候选人列表的索引设计（复合索引 / 部分索引 / 唯一索引）？
20. ⭐ 你在这个项目里遇到的最大难点 / 踩过最深的坑是什么？

---

## 1. 项目与架构总览

### 1.1 ⭐ 介绍一下这个项目，核心流程是什么？

**考察点**：能否把业务讲清楚，而不是只讲技术名词。

**答题要点**
- 业务：AI 招聘后端，把「简历投递 → 硬性筛查 → AI 评分 → 邮件邀约 → 面试时间协商 → 日程确认」全流程自动化（`README.md`）。
- 技术：FastAPI + LangGraph 状态图 + PostgreSQL/Redis + 邮件机器人 + 钉钉日历。
- 主链路：`app/tasks/agent_tasks.py` 的 `run_candidate_agent` → `app/workflow/candidate_graph.py` 编译状态图 → 各节点（`app/workflow/candidate_nodes/`）。
- 一句话总结：**用显式状态机替代 LLM 黑盒决策，用 interrupt 实现人机协作，用检查点实现断点续跑**。

### 1.2 ⭐ 为什么用 LangGraph 状态机，而不是让 LLM 自己决定调什么工具？

**考察点**：对 Agent 与状态机两种范式的理解。

**答题要点**
- 旧模式（`app/agents/candidate.py`，`GRAPH_OPEN=False`）让 LLM 通过工具自己决策，流程不可控、难调试、难复现。
- 状态机把流程拆成 10 个显式节点：`screening`、`scoring`、`email_invite`、`get_candidate_email_reply_node`、`check_availability`、`confirm`、`rejected`、`refused`、`negotiation_expired`、`error`（`candidate_graph.py`）。
- 每个节点输入输出是结构化的 `CandidateRecruitmentState`（TypedDict），路由是显式的 `add_conditional_edges`，日志能精确到节点和路由。
- 附带好处：可以人工干预（`Command(goto=...)`）、可以断点恢复、可以精确控制循环次数。

### 1.3 整体分层架构是什么？

**答题要点**
- Router（`app/routers/`）→ Service（`app/service/`）→ Repository（`app/repository/`）→ Model（`app/models/`），Schema 用 Pydantic（`app/schemas/`）。
- 核心招聘流程不在三层里，而是抽到 `app/workflow/`（LangGraph 图 + 节点 + 状态 + 错误处理）。
- 基础设施在 `app/core/`：config、db、auth、exceptions、redis、checkpointer、logging、rate_limit、audit。
- 后台任务在 `app/tasks/`：OCR、邮件、Agent、超时巡检。

### 1.4 一个候选人的状态机有哪些状态？流转规则？

**答题要点**
- 12 个状态（`app/models/candidate.py` 的 `CandidateStatusEnum`）：已投递、AI筛选失败、AI筛选成功、人工通过/重试、待面试、拒绝面试、面试通过、面试未通过、已入职、已拒绝、协商超时、处理异常。
- 允许的转移是图状表 `ALLOWED_TRANSITIONS`（`app/service/candidate_status_ser.py`），支持失败后的回跳：`AI筛选失败 / 协商超时 / 处理异常 → 人工通过/重试 → 待面试`。
- 注意：该表只在人工 API（`PATCH /candidate/{id}/status`）里校验；工作流节点是直接改库，不走这张表（可主动提这个风险，见 §12）。

### 1.5 外部依赖分别承担什么职责？

| 依赖 | 作用 | 代码 |
|------|------|------|
| PostgreSQL | 业务数据 + LangGraph 检查点 | `app/core/db.py`、`app/core/checkpointer.py` |
| Redis | Token、任务状态、邀请码、限流、分布式锁 | `app/core/redis_client.py`、`rate_limit.py`、`auth.py` |
| LLM（DeepSeek/Qwen） | 评分、邮件时间意图解析、简历提取 | `app/agents/llms.py` |
| 邮件服务器（IMAP/SMTP） | 收发邮件 | `utils/emailbot/` |
| 钉钉开放平台 | OAuth 授权、日历空闲、创建日程 | `app/service/dingtalk_ser.py` |
| PaddleOCR | 简历图片/PDF 识别 | `utils/ocr_service.py` |

---

## 2. LangGraph / 工作流（核心，最高频）

### 2.1 ⭐ `StateGraph` + `TypedDict` + `Annotated` reducer 分别解决什么问题？

**考察点**：LangGraph 状态合并机制。

**答题要点**
- `CandidateRecruitmentState` 是 `TypedDict`（`app/workflow/candidate_state.py`），定义全流程共享的状态。
- 每个字段用 `Annotated[T, reducer]` 指定「多节点写同一字段时怎么合并」：
  - `assign_state_property = lambda x, y: y if y is not None else x`：**新值非 None 就覆盖，None 表示不修改**，避免节点只返回部分字段时把其它字段清空。
  - `messages` 用 LangGraph 内置 `add_messages`（追加语义，用于对话历史）。
- 节点只需返回变化的字段（部分 dict），引擎按 reducer 合并。

### 2.2 ⭐ 10 个节点和边的类型？

**答题要点**（`app/workflow/candidate_graph.py`）
- 节点：`screening`（纯规则）、`scoring`（LLM）、`email_invite`（外部服务）、`get_candidate_email_reply_node`（interrupt）、`check_availability`（LLM+钉钉）、`confirm`（DB+钉钉+邮件）、`rejected`/`refused`/`negotiation_expired`（DB）、`error`（错误终结点）。
- 边：
  - 静态边：`START→screening`、各终态→`END`、`email_invite→get_candidate_email_reply_node`。
  - 条件边：`add_conditional_edges` + 路由函数，映射表里同时包含 `"error"` 分支。
- 路由函数都记录了结构化日志：`candidate_id` + 关键上下文 + 目标节点。

### 2.3 ⭐ `interrupt()` / `Command(resume=...)` 是怎么实现「等候选人回邮件」的？

**考察点**：人机协作（human-in-the-loop）的实现。

**答题要点**
- `get_candidate_email_reply_node` 里调用 `interrupt("candidate_email_reply")`，状态图在此暂停，快照写入 PostgreSQL 检查点（`check_availability_node.py`）。
- 邮件机器人收到回复后，`utils/emailbot/notify.py` 用 `Command(resume=<邮件内容>, update={...})` 恢复，`thread_id = 候选人邮箱`。
- 恢复时通过 `update` 刷新 `candidate/position/interviewer`，防止检查点里的旧数据。
- 超时兜底：`app/tasks/timeout_tasks.py` 用 `Command(resume=EMAIL_REPLY_TIMEOUT)`（`"TIMEOUT"`）恢复，路由到 `negotiation_expired`。

### 2.4 ⭐ `Command(resume=...)` 和 `Command(goto=...)` 的区别？

**答题要点**
- `resume`：恢复一个**中断点**，值会成为 `interrupt()` 的返回值，图从暂停处继续（`notify.py`、`timeout_tasks.py`）。
- `goto`：**跳转到指定节点**，相当于人工重试/改流程；配合 `update` 重置状态（`candidate_status_ser.py` 的 `trigger_manual_retry`）。
- 典型对比：
  - 新候选人流程：`ainvoke(初始 state)`。
  - 候选人回邮件：`ainvoke(Command(resume=...))`。
  - HR 人工重试：`ainvoke(Command(goto="email_invite", update={...}))`。

### 2.5 ⭐ 检查点为什么用进程级单例？`thread_id` 为什么用邮箱？

**答题要点**
- `AsyncPostgresSaver.from_conn_string` 是 asynccontextmanager，每次调用都会新建连接并执行 `setup()`（建表/迁移 DDL）。原实现每次 `ainvoke` 进出一次上下文，连接反复创建销毁，浪费且放大 DB 压力（`app/core/checkpointer.py` 注释）。
- 单例后：每进程恒定 1 条连接，`setup()` 只执行一次；用 `asyncio.Lock` + 双重检查保证幂等。
- `thread_id` 用候选人邮箱：业务上天然唯一，且人工重试/邮件恢复/超时巡检都能用同一 thread 找到同一条流程。
- 序列化：`JsonPlusSerializer(allowed_msgpack_modules=...)` 注册业务类型，避免反序列化警告/失败。

### 2.6 路由函数外面的 `_with_error_check` 守卫是干什么的？

**答题要点**
- 节点异常时 `node_error_wrapper` 会返回 `{"error_occurred": True, ...}`，图靠 `error_occurred` 决定走 `error` 节点。
- 但**条件路由本身**也可能读到脏状态，所以给每个路由函数包一层 `_with_error_check`：只要 `error_occurred` 为真就直接返回 `"error"`，不再执行原路由逻辑（`candidate_graph.py` L28-36）。
- 对应的 `path` 映射里都有 `"error": "error"`。

### 2.7 ⭐ 面试时间多轮协商怎么实现？怎么防止死循环？

**答题要点**
- `check_availability` 判断候选人意图（确认 / 拒绝 / 提议新时间），不满足时发「请重新选择时间」邮件并回到 `get_candidate_email_reply_node`，形成环。
- `negotiation_round` 每轮 +1，路由里判断 `negotiation_round >= max_negotiation_rounds`（默认 3）→ `negotiation_expired`。
- 阈值可配置：`MAX_NEGOTIATION_ROUNDS`，建图时注入到 state。
- 另有「邮件回复超时」兜底：48 小时（`EMAIL_REPLY_TIMEOUT_HOURS`）由巡检任务强制推进到协商超时。

### 2.8 ⭐ 人工重试为什么保留旧检查点，而新流程要 `adelete_thread`？

**答题要点**
- `run_candidate_agent` 启动**新流程**前 `await checkpointer.adelete_thread(candidate.email)`：清掉旧快照，避免重放历史错误状态。
- `trigger_manual_retry` **不删**旧检查点：人工重试的意义就是「从断点继续」，用 `Command(goto="email_invite")` 跳回邮件节点，并 `update` 清理 `error_*`、重置 `negotiation_round`、`candidate_email_reply`、`proposed_interview_time` 等。
- 一句话：**新流程要干净起点，人工重试要保留上下文**。

### 2.9 节点返回 dict 而不是整个 state，引擎怎么合并？

**答题要点**
- LangGraph 把节点返回值当作「部分更新」，按 state 里每个字段的 reducer 合并。
- 项目里 `assign_state_property` 的语义是「非 None 覆盖」，所以节点可以安全地只返回自己负责的字段。

### 2.10 中断期间服务重启会怎样？

**答题要点**
- 中断点快照已持久化在 PostgreSQL（`checkpoints` 表），重启后 `init_checkpointer()` 恢复同一 saver，用相同 `thread_id` 调 `Command(resume=...)` 即可从断点继续。
- 前置条件：重启后 `get_checkpointer()` 必须已初始化，否则 `run_candidate_agent`/恢复路径会抛 `RuntimeError`（`checkpointer.py` 有显式检查）。

### 2.11 `GRAPH_OPEN` 开关的设计意图？

**答题要点**
- `GRAPH_OPEN=True` 走 LangGraph 状态图；`False` 走旧 `CandidateProcessAgent`（`app/tasks/agent_tasks.py`）。
- 邮件恢复侧 `notify.py` 也按开关分流，保证两套模式都能跑。
- 注意：**README 写默认 False，代码里 `Settings.GRAPH_OPEN` 默认 True**（`app/core/config.py` L110）——文档与代码不一致，被问到时要说清以代码为准。

### 2.12 状态图节点里能抛异常吗？

**答题要点**
- 不能指望异常「传出去」：节点由 LangGraph 引擎调用，外层是 FastAPI `BackgroundTasks`，未捕获异常会被静默丢弃（`agent_tasks.py` 注释）。
- 所以节点不直接抛，而是用 `@node_error_wrapper` 把业务异常转成错误状态，让图自己走到 `error` 节点。

---

## 3. 异常处理（高频难点）

### 3.1 ⭐ 异常层级怎么设计的？为什么这么分？

**答题要点**（`app/core/exceptions.py`）
- `AppException`（基类，带 `message` + `code`）
  - `BusinessException`：面向用户的业务错误，HTTP 400（如 `CandidateNotFoundError`、`InterviewSchedulingError`、`NegotiationError`、`EmailSendError`）。
  - `ExternalServiceError`：面向运维的第三方/基础设施错误，HTTP 502/503（`LLMServiceError`、`OCRServiceError`、`DingTalkServiceError`）。
- 全局异常处理器按类型映射状态码：400 / 502 / 500（`app/core/exception_handlers.py`）。
- 分层的意义：**业务异常可以在节点内被消化成流程分支；基础设施异常必须上浮到统一兜底，标记 `PROCESSING_ERROR` 供人工重试**。

### 3.2 ⭐ `@node_error_wrapper` 的三层捕获逻辑？

**答题要点**（`app/workflow/error_handler.py`）
1. `BusinessException` → 记 warning，返回 `{"current_stage": "error", "error_occurred": True, "error_message": ..., "error_stage": ...}`，让图流转到 `error` 节点。
2. `ExternalServiceError` → 不捕获，继续向上传播，由 `run_candidate_agent` 统一处理。
3. 其它未知异常 → 记 error 日志，包装成 `AppException` 再抛（防御性，避免裸异常穿透）。
- 用 `@wraps(node_func)` 保留节点名/文档字符串，日志里仍显示 `scoring_node` 而不是 `wrapped`。

### 3.3 ⭐ 为什么业务异常要在节点内捕获，而不是直接抛？

**答题要点**
- 直接抛会穿过 LangGraph 引擎，最终被 `BackgroundTasks` 吞掉，候选人状态永远停在「已投递」，数据不一致。
- 转成 error state 后，图能确定性地走到 `error_node`，由它把状态更新为 `PROCESSING_ERROR` 并保存错误信息，流程有明确终态。

### 3.4 ⭐ 为什么基础设施异常反而要穿透？

**答题要点**
- 基础设施故障（LLM/邮件/钉钉）不是「这个候选人的业务问题」，而是系统级问题，需要统一标记 + 告警 + 人工重试。
- 外层 `run_candidate_agent` 捕获后：结构化日志 + `_mark_processing_error` + **静默返回**（不 re-raise）。

### 3.5 ⭐ 为什么 `run_candidate_agent` 不能 re-raise？

**答题要点**
- 该函数由 FastAPI `BackgroundTasks` 调用，抛出的异常会被框架静默丢弃，日志和状态都不会更新，问题无法追踪。
- 所以函数内必须「完全处理」：记日志 + 更新状态 + 返回 `None`。这是被坑过之后的设计（`agent_tasks.py` 注释明确写了这一点）。

### 3.6 `error_node` 为什么自己还要 try/catch？

**答题要点**
- 它是「最后一道防线」，如果连更新状态都失败，也必须保证记 `critical` 日志，不能二次抛异常导致图引擎崩掉或日志丢失（`error_handler.py` L101-118）。

### 3.7 全局异常处理器如何映射 HTTP 状态码？

**答题要点**
- `BusinessException → 400`，`ExternalServiceError → 502`，`AppException → 500`（`exception_handlers.py`）。
- 响应体统一 `{"detail": ..., "code": ...}`。

### 3.8 `PROCESSING_ERROR` 和 `AI筛选失败` / `协商超时` 有什么区别？

**答题要点**
- `AI筛选失败` / `协商超时` 是**正常业务结果**（规则/阈值判定），候选人在业务上已被淘汰，但允许 HR 人工重试。
- `PROCESSING_ERROR` 是**基础设施故障**（LLM/邮件/钉钉/DB 异常），不是候选人的错，管理后台可筛选出来统一重试。
- 三者都属于 `MANUAL_RETRY_SOURCES`（`candidate_status_ser.py` L83-87）。

### 3.9 如果 `error_node` 里数据库也挂了怎么办？

**答题要点**
- 记 `critical` 日志（带 `candidate_id`），异常被吞掉，流程安静结束。
- 改进方向：把失败事件写进独立可靠存储（如消息队列/本地文件/WAL），或引入重试与告警（可参考 `docs/ops_plan.md`）。

### 3.10 可能被 challenge 的点：`scoring_node` 把 DB 写入失败归类成 `LLMServiceError`，合理吗？

**答题要点（主动承认 + 解释）**
- 不合理的地方：DB 故障不是 LLM 故障，归类成 `LLMServiceError` 会误导排障和监控。
- 当时的考虑：外层只区分「业务异常 / 基础设施异常」，DB 写失败显然属于基础设施，但异常层级里没有 `DatabaseError`，就近用了 `LLMServiceError` 保证被外层捕获。
- 正确做法：加一个 `DatabaseError(ExternalServiceError)`，或统一 `InfrastructureError` 基类。

---

## 4. 认证与安全

### 4.1 ⭐ JWT 双令牌设计，为什么 access / refresh 分开？

**答题要点**（`app/core/auth.py`）
- access token：30 分钟，无状态，携带 `sub/jti/type/exp`，用于接口鉴权。
- refresh token：7 天，`jti` 存 Redis（`refresh:{jti}` → 邮箱），用于换取新 access token。
- 分开的好处：access 短命降低泄露风险；refresh 可集中吊销；`type` 字段防止把 access 当 refresh 用。

### 4.2 ⭐ refresh token 为什么用 Lua 脚本原子消费？

**答题要点**
- 刷新时必须「取出并删除」`refresh:{jti}`，如果拆成 `exists()` + `delete()` 两次往返，并发刷新会同时通过存在性检查，同一 token 被兑换两次，轮换失去意义。
- Lua 脚本在 Redis 单线程里原子执行：`GET` 到值才 `DEL` 并返回，否则返回 nil（`auth.py` L50-62）。
- 测试覆盖了原子消费（`tests/test_hardening.py`、`tests/test_auth_tokens.py`）。

### 4.3 登出怎么吊销？access token 有没有黑名单？

**答题要点（注意 README 措辞）**
- 登出：`revoke_refresh_token` 解出 `jti` 并 `DEL refresh:{jti}`，之后刷新会因「token 已被吊销」失败。
- access token **没有 Redis 黑名单**，靠 30 分钟短过期兜底；README 里「Redis 黑名单吊销」准确说是 refresh token 的存储 + 删除机制。
- 如果面试官追问「access 泄露怎么办」：可引入 `jti` 黑名单/版本号，或用 refresh 轮换 + 检测重用（刷新时发现 jti 已被消费可判定泄露并吊销整个会话）。

### 4.4 密码为什么用 Argon2（pwdlib）？

**答题要点**
- Argon2 是密码哈希竞赛获胜方案，抗 GPU/ASIC 暴力破解，支持内存硬度；比 bcrypt 更现代，比 MD5/SHA 系列安全得多。
- `pwdlib[argon2]` 负责哈希与校验，模型 setter 自动哈希（`tests/test_change_password.py` 验证每次哈希结果不同、不是明文存储）。

### 4.5 ⭐ 登录限流为什么用 ZSET 滑动窗口 + Lua？

**答题要点**（`app/core/rate_limit.py`）
- 固定窗口在窗口边界会有 2 倍突发；滑动窗口按 `now - window` 清理过期成员，天然平滑。
- ZSET 的 score 是请求时间戳，member 唯一（`time-uuid`），`ZREMRANGEBYSCORE` + `ZCARD` + `ZADD` + `PEXPIRE` 一段 Lua 原子完成，多 worker 并发安全。
- 双维度：单 IP（默认 20 次/60 秒）防同机枚举多账号；单账号（默认 5 次/60 秒）防定向爆破。任一超限返回 429。

### 4.6 账号枚举怎么防？

**答题要点**
- 登录失败统一返回「邮箱或密码错误」，不区分用户不存在/密码错误（`auth_ser.authenticate_user`），且用 400 而不是 401（注释说明：401 会触发前端 axios 自动刷新令牌流程）。
- 候选人简历下载：`get_for_user` 不可见时返回 `None`，接口统一 404「不存在或无权访问」，避免用状态差异枚举候选人 ID（`candidate_router.py` L139-143）。

### 4.7 权限模型与数据可见性？

**答题要点**
- 三种角色：超级管理员 / HR / 普通用户（`get_super_user`、`get_hr_or_superuser`）。
- 候选人可见性在 Repository 层统一实现（`resume_repo._visible_stmt`）：超管全部；HR 看所负责部门的候选人；普通人只看自己发布职位的候选人。
- 员工列表按部门过滤，避免越权。

### 4.8 路径穿越怎么防？

**答题要点**
- `_resolve_resume_path`：先 `resolve()` 基础目录和目标路径，再判断 `base not in path.parents` 直接 400；文件不存在 404（`candidate_router.py` L119-127）。
- 库里只存文件名（`uuid + 扩展名`），进一步降低风险。

### 4.9 邀请码流程？

**答题要点**（`auth_ser.get_invite_code`）
- 超管邀请：校验邮箱未注册、部门存在 → 生成 6 位数字邀请码 → Redis `invite:{email}` + `inviter_department_id:{email}`，过期时间 `INVITE_EXPIRE_MINUTES`（默认 30 分钟）。
- 注册时校验邀请码、判重、建用户并写入邀请人部门。
- 邀请邮件走 `BackgroundTasks`。

### 4.10 钉钉 OAuth2 流程？

**答题要点**（`user_router.py` + `dingtalk_ser.py`）
- 前端拿 `/user/dingtalk/authorize` 返回的授权 URL（`redirect_uri` 指回后端 callback，`state=current_user.id`）。
- 回调后：Redis 里没有 refresh token 就用 `code` 换 token；有就刷新；再拉用户信息写库，最后重定向到成功页。
- token 缓存：`dingtalk:user_actoken:{state}`（2 小时）、`dingtalk:user_retoken:{state}`（30 天）。
- `state` 既是用户标识也防 CSRF（项目里直接传 user id，更严格应做签名/随机 state）。

### 4.11 密钥校验为什么放在 Pydantic `model_validator`？

**答题要点**（`config.py` L169-187）
- 空密钥会让 PyJWT 抛 `InvalidKeyError`，如果等到登录才报错就是「跑起来才 500」。
- 启动即校验：非空；非 debug 环境要求长度 ≥ 32，防止弱密钥签名。
- 配置优先级：构造参数 > 环境变量 > `.env` > 默认值（Pydantic Settings）。

---

## 5. 数据库 / ORM

### 5.1 SQLAlchemy 2 异步用法，`AsyncSession` 生命周期？

**答题要点**（`app/core/db.py`）
- `create_async_engine` + `async_sessionmaker`。
- `get_db` 依赖：`async with AsyncSessionFactory() as session`，异常 rollback，finally close；**不自动 commit**，由 service 显式 `session.begin()`。
- 节点里用独立的 `AsyncSessionFactory()` 短会话，保证节点失败不污染外层事务。

### 5.2 连接池参数的作用？

**答题要点**
- `pool_size=10`：常驻连接数；`max_overflow=20`：超负荷时临时扩展；`pool_timeout=10`：拿连接超时；`pool_recycle=3600`：回收旧连接，避免数据库/网络设备单方面断连；`pool_pre_ping=True`：取连接时先探活，防止拿到坏连接。
- `expire_on_commit=False`：commit 后属性仍可访问，避免隐式 IO/DetachedInstanceError；`autoflush=False`：显式管理 flush 更稳。

### 5.3 ⭐ `selectinload` 和 `lazy="joined"` 的区别？

**答题要点**（`resume_repo.candidate_eager_options`）
- `lazy="joined"` 会让**每个查询**都强制 JOIN position/resume/creator 三张表，哪怕只要 name，造成行膨胀和多余 IO。
- 改为按场景显式 `selectinload`：主查询先拿候选人，再用一条 `IN` 查询批量拿关联，避免笛卡尔积式膨胀，层级按需展开。
- `notify.py` 里特别注释：关联对象要在 session 内转成 Pydantic，避免 `DetachedInstanceError`。

### 5.4 ⭐ 候选人表的索引设计？

**答题要点**（`app/models/candidate.py` `__table_args__`）
- `ix_candidates_position_status_created (position_id, status, created_at)`：列表页主路径「职位 + 状态过滤 + 时间倒序」。
- `ix_candidates_position_created (position_id, created_at)`：只按职位过滤时用左缀。
- `ix_candidates_created_at`：无过滤/超管列表的倒序。
- `ix_candidates_email_waiting`：**部分索引**（`postgresql_where`），只覆盖「等待回复且已发邀请」的行，索引体积小，专供超时巡检。
- `ix_candidates_resume_id` 唯一索引：1:1 完整性 + 点查性能。
- 状态字段也加了索引（高频过滤）。

### 5.5 为什么用 `StrEnum` + `values_callable`？

**答题要点**
- `StrEnum` 让枚举成员本身就是字符串，序列化/比较直观。
- `SQLAlchemyEnum(..., values_callable=lambda obj: [e.value for e in obj])` 让数据库存的是**中文值**（如 `'已投递'`），而不是枚举名，避免 `InvalidTextRepresentation`（`docs/technical_highlights.md` 第三节的坑）。

### 5.6 PostgreSQL 枚举为什么改成 VARCHAR？

**答题要点**
- Python 枚举新增值（如 `PENDING`）后，数据库原生 ENUM 不会自动同步，写入报 `InvalidTextRepresentation`。
- 改成 `Enum(InterviewResultEnum, native_enum=False, length=20)`（实际是 VARCHAR + 检查），新增枚举值无需迁移，彻底解决同步问题。

### 5.7 事务边界怎么划？

**答题要点**
- 路由/服务里 `async with session.begin()` 显式开启事务；`get_db` 不 commit。
- 节点内部用独立 `AsyncSessionFactory()` + `session.begin()`，一个节点一个短事务，失败只回滚自己。
- `confirm_node` 里 DB 写入是一个事务，日程/邮件在其外（见 §12 的一致性问题）。

### 5.8 循环导入导致 Alembic 认不出表怎么办？

**答题要点**（`docs/technical_highlights.md` 第一节）
- `candidate.py` ↔ `position.py` 互相 import 形成循环，模型注册失败，autogenerate 迁移为空。
- 解决：`TYPE_CHECKING` + 字符串关系引用（`Mapped["PositionModel"]` + `relationship("PositionModel")`），运行时不需要真实类，类型检查仍保留提示。

### 5.9 时区：naive vs aware，DB 里存什么？

**答题要点**
- 系统里混用过 `datetime.now()`（naive）和带 `Asia/Shanghai` 的 aware 时间，直接比较会抛 `TypeError: can't compare offset-naive and offset-aware datetimes`。
- 统一：**内存里流转一律 aware 北京时间**（`now_beijing()` / `from_naive_beijing()`），只有跨两道边界时才变形态——写库剥成 naive 北京时间数值（`db_now()` / `to_naive_beijing()`，列是 `TIMESTAMP WITHOUT TIME ZONE`，所以零迁移），出网络格式化成 ISO-8601 字符串（`to_iso8601()` / `from_iso8601()`）。JWT 与审计日志仍走 UTC。
- 为什么用固定偏移 `timezone(timedelta(hours=8))` 而不是 `ZoneInfo("Asia/Shanghai")`：中国 1991 年后无夏令时，两者对现在与将来的时间戳等价，但后者依赖镜像里的 tzdata（项目没声明这个依赖，`python:3.13-slim` 上就是 `ZoneInfoNotFoundError`）。
- 守口：`tests/test_time_policy.py` 扫 `app/`、`utils/` 源码，出现 `timedelta(hours=8)`、`replace(tzinfo=None)` 或裸 `datetime.now()` 直接失败——防止「统一」再退化成十几处手写换算。
- 涉及文件：`app/core/time.py`（替代原 `utils/time_ISO8601_datatime.py`，取舍与实测见 `docs/change2026-11-17.md` §十九）、`utils/available_time.py`、`confirm_node.py`。

---

## 6. AI 集成

### 6.1 五维评分怎么实现？

**答题要点**（`scoring_node.py`）
- `create_agent(model=deepseek_llm, response_format=AgentCandidateScoreSchema)`，结构化输出五个维度 + 总分 + 优势/劣势 + 评语。
- 提示词在 `app/agents/prompts.py`，输入是候选人和职位的 JSON。
- 结果落库 `candidate_ai_scores`（唯一索引防重复评分），同时更新候选人状态为 `AI筛选成功/失败`。

### 6.2 双模型容错 `ModelFallbackMiddleware` 原理？

**答题要点**（`app/agents/llms.py`、`scoring_node.py`）
- 主模型 `deepseek-v4.1-flash`，备用 `qwen3.8-max`，都走 OpenAI 兼容协议。
- `ModelFallbackMiddleware(first_model=qwen_llm)`：主模型调用失败/超时时自动降级到备用模型，业务无感知。
- 评分节点捕获一切异常后统一抛 `LLMServiceError`（含降级后仍失败的情况）。

### 6.3 评分阈值怎么配置和注入？

**答题要点**
- `Settings.SCORE_PASS_THRESHOLD`（默认 7），`build_candidate_graph(score_pass_threshold=...)` 建图时可覆盖。
- `run_candidate_agent` 把阈值放进初始 state，路由 `_route_after_scoring` 用 `score_result.overall_score > threshold` 判断。
- 注意 `scoring_node` 内部用的是 `state.get("score_pass_threshold", 7)`，路由用的是 `state.get(...) or DEFAULT`，两处逻辑等价但写法不同（细节题）。

### 6.4 ⭐ 简历解析流水线为什么用「后台任务 + Redis 状态轮询」？

**答题要点**（`candidate_ser.parse_resume_service`、`ocr_tasks.py`）
- OCR + LLM 提取耗时且不可控，不能占用 HTTP 请求；用 FastAPI `BackgroundTasks` 异步执行。
- 任务状态写 Redis（`task:{task_id}`，初始 pending，完成 done + result，失败 failed + error），前端轮询 `GET /candidate/resume/parse/{task_id}`。
- Redis key 设过期（1 小时 / 30 分钟），避免状态残留。
- Word 文档先转 PDF（`utils/PDFser.py`，Windows 用 pywin32）再进 OCR。

### 6.5 邮件时间意图怎么解析？

**答题要点**（`check_availability_node.py`）
- `ProposedTime` schema：`confirm`（是否确认原时间）、`refused`（是否拒绝）、`proposed_time`（提议时间）、`exactly`（`hours`/`am`/`pm`）。
- LLM 带 `get_current_time` 工具，能处理「明天下午三点」这类相对日期。
- 解析失败 → `NegotiationError`（业务异常，走 error 节点）。
- 时间字符串解析支持 5 种格式，统一附加 `BEIJING_TZ`。

### 6.6 ⭐ 模糊时间（上午/下午）怎么校验？策略模式用在哪？

**答题要点**
- `CHECK_STRATEGIES = {"hours": check_hours, "am": check_am_or_pm, "pm": check_am_or_pm, "day": check_day}`。
- `check_am_or_pm`：把「上午/下午」映射到配置的时间窗口（`settings.am_window/pm_window`），要求面试官的空闲段完整落在窗口内且长度 ≥ 1 小时。
- `check_hours`：精确时间，要求 `start <= proposed` 且 `proposed + 1h <= end`。
- 未知策略 → `NegotiationError`。

### 6.7 LLM 调用失败怎么处理？

**答题要点**
- 节点内捕获并抛 `LLMServiceError`（基础设施异常），穿透到 `run_candidate_agent` → 标记 `PROCESSING_ERROR` + error 日志。
- 不会被 `@node_error_wrapper` 转成 error state（因为它只捕 `BusinessException`）。

---

## 7. 邮件机器人

### 7.1 ⭐ 整体架构？

**答题要点**（`utils/emailbot/`）
- 分层：`config`（设置）→ `imap_client`/`smtp_client`（协议）→ `parser`（MIME 解析）→ `attachments`（附件落盘）→ `rules`（规则）→ `state`（游标/去重）→ `bot`（门面）→ `mail_manager`（单例 + 调度）。
- 对外三个动作：`poll_once()`（拉增量 → 存附件 → 跑规则 → 回复/提醒 → 推游标）、`send_email()`、`reply()`。

### 7.2 长连接、断线自愈、并发串行化？

**答题要点**
- `MailManager` 的轮询由 `app/tasks/mail_poll_loop.py` 的 `MailPollTimer` 驱动，与周期巡检共用 `app/tasks/scheduler.py` 的调度策略，但邮件是**独立开关**：恒为固定间隔 `MAIL_POLL_INTERVAL_SECONDS`（默认 15 秒），既不看 `DEBUG` 也不跟随全局。单轮失败不退出循环，下一轮自动重试。
- `MailBot._ensure_connected()`：发送前检查 `is_connected`，断了自动重连（节点里也常显式判断并 `connect()`）。
- `bot.py` 里用 `asyncio.Lock` 串行化处理（imap/smtp 客户端内部），`_spawn` 保留后台任务引用防 GC。

### 7.3 ⭐ 幂等怎么保证？

**答题要点**
- **UID 游标**：`StateStore.last_uid` 持久化在 `mailbot_state.json`，每次只拉 `UID > last_uid` 的新邮件。
- **Message-ID 去重**：`state.is_seen(message_id)`，防游标回退导致同一封邮件二次回复/二次提醒。
- **冷启动**：`last_uid is None` 时把游标定到当前最大 UID，只处理「从现在起」的新邮件，不补历史。
- **游标推进策略**：无论处理成功失败，`finally` 里都推进游标——失败已记日志，不能卡死整条链。

### 7.4 规则引擎和回调？

**答题要点**（`rules.py`、`bot.py`、`notify.py`）
- `MailRule` 按主题/发件人/正文关键词/附件名匹配，命中后可自动回复（`auto_reply` + `reply_text`）。
- `on_match` 回调默认是 `notify`：驱动招聘流程 `Command(resume=...)`。
- 兜底逻辑 `_handle_candidate_reply`：即使规则没命中，只要候选人处于 `AI筛选成功/人工通过` 状态，也恢复流程——解决候选人回「确认」「好的」不命中关键词的问题。

### 7.5 回复邮件的会话头？

**答题要点**
- `reply()` 优先用 `Reply-To`（转发邮件才不会回错人）。
- 设置 `In-Reply-To` 和 `References`（拼接原 `References` + 原 `Message-ID`），让对方客户端把回信挂到同一会话。
- 主动发信自己生成 `Message-ID`（`make_msgid()`），格式可控，后续去重/会话线程都靠它。

### 7.6 邮件机器人怎么触发状态图恢复？

**答题要点**
- `notify(email, rule)`：按邮箱找候选人 → 图模式用 `Command(resume=<邮件内容>, update={candidate/position/interviewer})` + `thread_id=邮箱` 恢复；旧模式用 `CandidateProcessAgent`。
- 若找不到候选人/状态不对，只记日志不恢复，避免误触发。

### 7.7 为什么默认单 worker？

**答题要点**
- 邮件机器人是**长连接 + 进程内单例**，多 worker 会有多个轮询/多条连接，状态和去重都会乱。
- 所以 `gunicorn.conf.py` 默认 `workers=1`，注释明确「受邮件机器人约束」；拆分邮件机器人为独立服务后才能调大。

---

## 8. 时间协商算法

### 8.1 `find_available_slot` 的算法？

**答题要点**（`utils/available_time.py`）
- 输入忙碌时段 + 时长（默认 1 小时）+ 起始日期 + 搜索天数。
- 预处理：修正 `end < start` 的错误数据。
- 按天遍历上午（9-12）/下午（14-18）两个窗口，把忙碌时段裁剪到窗口内，排序后扫描空隙，把每个空隙切成若干个 `hour_interval` 段。
- 输出 `(start, end)` 列表，供「提议时间」和「冲突校验」复用。

### 8.2 时区在这个算法里怎么处理？

**答题要点**
- 窗口边界用 `datetime.combine(..., tzinfo=BEIJING_TZ)` 构造 aware 时间，与忙碌时段（钉钉返回转北京时间）保持一致，避免 naive/aware 比较报错。

### 8.3 部分索引在超时巡检里的作用？

**答题要点**
- `ix_candidates_email_waiting` 只索引 `status IN ('AI筛选成功','人工通过/重试') AND email_invite_sent_at IS NOT NULL` 的行。
- 巡检 SQL（`list_stale_email_waiters`）条件完全匹配，走小索引，避免全表扫描。

### 8.4 ⭐ 超时巡检怎么保证多进程安全、不误恢复？

**答题要点**（`app/tasks/timeout_tasks.py`）
- Redis `SET NX EX` 抢锁（`sweep:email_reply_timeout`，TTL 30 分钟），同一时刻只有一个 worker 执行。
- 单轮批量上限 200，避免一次扫全场。
- 恢复前 `aget_state(config)` 检查 `snapshot.interrupts` 是否非空，**确实停在中断点**才 `Command(resume=EMAIL_REPLY_TIMEOUT)`，避免误伤已完成/未启动的 thread。
- 异常逐条捕获，单条失败不影响整轮；`finally` 释放锁。

### 8.5 可能被 challenge 的点：`utils/available_time.py` 里时间窗口硬编码 9-12/14-18，而 `check_availability_node` 用 `settings.am_window/pm_window`，不一致怎么办？

**答题要点**
- 确实存在不一致：算法侧硬编码，校验侧走配置。
- 改进：把窗口作为参数传给 `find_available_slot`，或统一从 Settings 构造，避免配置改了只生效一半。

---

## 9. 日志系统

### 9.1 ⭐ 为什么用队列异步日志？

**答题要点**（`app/core/logging_config.py`）
- 业务线程（含 asyncio 事件循环）只做 `put_nowait` 入队，磁盘 IO 不阻塞请求。
- `QueueHandler` → 队列 → 监听线程/进程 → 真正的 `FileHandler`。
- 格式化（字符串拼接）在 worker 侧完成，IO 在监听侧。

### 9.2 ⭐ 多进程为什么不能用 `uvicorn --workers N`？

**答题要点**
- uvicorn 多 worker 是 spawn 启动，worker 拿不到主进程的共享队列，会退化成每个 worker 各写各的日志文件、互相覆盖。
- gunicorn 是 fork，worker 继承主进程的 `multiprocessing.Queue` 引用；主进程在 `when_ready`（fork 之前）调 `bootstrap_logging()` 创建队列并启动**独立日志监听进程**，全局唯一写盘者。
- 单进程（`uvicorn --workers 1`）退化为进程内 `QueueListener` 线程，同样只有一个写入者。

### 9.3 日志怎么分流？

**答题要点**
- `workflow.log`：全量，按天轮转，留 30 天。
- `error.log`：ERROR+，按大小（50MB）轮转，留 90 天。
- `nodes.log`：只收 `NODE_LOGGER_NAME`（默认 `workflow.nodes`）的记录，按天轮转。
- `audit.log`：只收 `audit` logger，JSON Lines，按天轮转，对接 SIEM。
- 用 `_LoggerNameFilter(prefix, include)` 做白/黑名单，logger 名与过滤器同源，改名自动跟随。

### 9.4 ⭐ `request_id` 为什么用 `contextvars` 而不是 `threading.local`？

**答题要点**（`app/core/request_context.py`）
- Web 层是异步的，同一个线程会承载多个并发请求，`threading.local` 会串味。
- `ContextVar` 跟随 async 上下文传播；中间件在请求进入时 `set_request_id()`（有 `X-Request-ID` 就透传），并写回响应头。
- 日志通过自定义 `LogRecordFactory` 给每条记录挂 `request_id`；`_RequestIdFilter` 兜底防 KeyError。

### 9.5 `LogRecordFactory` 为什么必须在 worker 侧安装？

**答题要点**
- 多进程模式下日志记录会被序列化后送给监听进程，监听进程的 contextvar 与 worker 无关，所以只能在**产生记录的进程**里取值。

### 9.6 监听进程为什么要显式判断 `record.levelno >= handler.level`？

**答题要点**
- `Handler.handle()` 本身不做 level 检查（那是 `Logger.callHandlers` 的职责），直接 `handle` 会把 INFO 也写进 `error.log`。等价于 `QueueListener(respect_handler_level=True)`。

---

## 10. 工程化 / 部署 / 测试

### 10.1 Pydantic Settings 的优先级和派生字段？

**答题要点**（`app/core/config.py`）
- 优先级：构造参数 > 环境变量 > `.env` > 字段默认值；`case_sensitive=False`，`extra="ignore"`。
- `@computed_field` + `@property` 暴露派生值（`am_window`、`pm_window`、`database_url`、`REDIS_URL`），避免同一份配置维护两遍。
- `@lru_cache` 的 `get_settings()` 保证全局单例。

### 10.2 配置项有哪些？

**答题要点**
- 招聘阈值：评分通过（7）、协商轮次（3）、上午/下午窗口、邮件回复超时（48h）、巡检间隔（15min）。
- 安全：access/refresh 有效期（30min/7d）、邀请码（30min）、登录限流（20/5 次每分钟）。
- 开关：`GRAPH_OPEN`、`MAIL_BOT_ENABLED`、`NODE_LOGGER_NAME`。

### 10.3 Windows 兼容性处理？

**答题要点**
- `app/main.py` / `app/core/db.py` 在 win32 下设置 `WindowsSelectorEventLoopPolicy`，解决 psycopg 在 ProactorEventLoop 下的兼容问题，并强制 uvicorn 用 SelectorEventLoop。
- Word→PDF 用 pywin32（仅 Windows）。
- gunicorn 依赖 `os.fork`，Windows 不可用，只能 `uvicorn --workers 1`。

### 10.4 测试策略？

**答题要点**
- `tests/` 共 60+ 个测试函数（`pytest` + `pytest-asyncio`，`asyncio_mode=auto`）。
- 不依赖真实 DB/Redis/网络：用 fake/in-memory Redis、纯函数覆盖路由、异常包装、Token 生命周期、日志分流、密码哈希。
- 覆盖重点：`test_graph_routes.py`（所有路由 + 错误守卫）、`test_exception_wrapper.py`（三层异常）、`test_auth_tokens.py`（签发/轮换/吊销/篡改）、`test_logging_routing.py`（分流/request_id）、`test_hardening.py`（refresh 原子消费、checkpointer 未初始化报错）。

### 10.5 CI/CD？

**答题要点**
- `.github/workflows/ci.yml` 存在（可按需展开：安装、跑 pytest）。
- 部署文档 `docs/deployment.md` 覆盖裸机/Docker、Nginx/Caddy、systemd、Compose、备份升级、gunicorn 多进程。

### 10.6 监控与告警？

**答题要点**
- `docs/ops_plan.md` 有方案：日志聚合、关键指标（流程失败率、协商超时率、LLM/邮件/钉钉错误率）、告警规则。
- 现有可观测性：结构化日志 + `candidate_id` + `request_id` + 审计日志 + `PROCESSING_ERROR` 可筛选。

---

## 11. 场景 / 开放题（区分度最高）

### 11.1 如果 LLM 服务整体挂了，系统会怎样？怎么改进？

**答题要点**
- 现状：评分节点抛 `LLMServiceError` → 外层标记 `PROCESSING_ERROR`，人工可在后台重试；备用模型 `ModelFallbackMiddleware` 先降级。
- 改进：指数退避重试、任务队列 + 死信、熔断（避免打爆 LLM）、把「重试」做成带次数上限的自动策略、监控告警。

### 11.2 `confirm` 节点先写库、再创建钉钉日程、再发邮件，中间失败会怎样？

**答题要点**
- 现状：DB 里已有面试记录且候选人状态已改为「待面试」，但日程没创建/邮件没发出；异常穿透后状态被改成 `PROCESSING_ERROR`。
- 人工重试时 `confirm_node` 会 `get_by_candidate_id` 找到已有记录并更新（幂等设计），所以可以恢复。
- 更严谨的做法：把「创建日程」和「发确认邮件」做成可重试的步骤，或引入 outbox / Saga，保证最终一致。

### 11.3 同一个候选人重复创建 / 重复触发流程会怎样？

**答题要点**
- 新流程会 `adelete_thread(candidate.email)` 清旧检查点，所以同邮箱重复创建会重新开始（可能重复发邀请邮件）。
- 邮件机器人有 UID + Message-ID 去重，不会重复回复；但同一封邮件若内容不同 Message-ID 不同，仍可能多次 resume。
- 改进：业务层对「同邮箱 + 同职位」做幂等约束；resume 前检查 `aget_state` 是否仍在 interrupt（超时巡检已经这么做了）。

### 11.4 邮件机器人收到重复回复 / 乱序回复怎么办？

**答题要点**
- 去重：Message-ID + 游标；回复顺序按 UID 升序处理。
- 若候选人在系统发「请重新选择时间」邮件之前又回了一封，可能出现「恢复—再恢复」竞争；`_ensure_connected` 和 asyncio 单线程轮询降低了概率，但跨进程多实例需要分布式锁（当前默认单 worker）。

### 11.5 如果要支持「多个面试官 / 多轮面试」，状态图怎么扩展？

**答题要点**
- 状态里已有 `interviewer`，可抽象为 `interviewers` 列表 + `round` 维度。
- 节点可拆成 `schedule_round_n`，用条件边串联；检查点已经按 `thread_id` 隔离，可再加 `round` 维度或子图（subgraph）。
- 注意 thread_id 目前用邮箱，多轮需要更细的 thread 或显式轮次字段。

### 11.6 如何观测这个系统？

**答题要点**
- 日志：`workflow.log` / `nodes.log` / `error.log` / `audit.log`，全链路 `candidate_id` + `request_id`。
- 业务：`PROCESSING_ERROR`、`error_message`、协商轮次、`email_invite_sent_at` 都可查。
- 缺口：没有指标埋点（成功率、耗时、LLM token），可按 `docs/ops_plan.md` 补 Prometheus/结构化指标。

### 11.7 你在这个项目里遇到的最大难点是什么？

**参考话术（`RESUME_PROJECT.md` 已有，可再个性化）**
- 异常处理的三层设计 + BackgroundTasks 吞异常的坑。
- 检查点单例与断点恢复的连锁问题（新流程删 thread、人工重试留 thread、超时巡检校验 interrupt）。
- 时间协商里 naive/aware 时区、模糊时间策略、多轮循环上限。

---

## 12. 面试官可能「找茬」的代码细节（提前准备）

> 这些不一定会被问到，但如果你主动指出，会显得对代码理解很深。

1. **`GRAPH_OPEN` 默认值文档与代码不一致**：README 写 False，`config.py` L110 默认 True。
2. **README 的「Redis 黑名单吊销」措辞**：实际只有 refresh token 存 Redis 并可删除，access token 无黑名单。
3. **`scoring_node` 把 DB 写失败归类为 `LLMServiceError`**（异常语义不准）。
4. **`utils/available_time.py` 硬编码 9-12/14-18**，与 `Settings` 的可配置窗口不一致。
5. **`CandidateModel.email` 可为空**，但 `thread_id=candidate.email` 用邮箱，空邮箱会导致 thread_id 异常（`candidate.py` L79）。
6. **`CandidateModel.resume_id` 非空**，但下载接口仍判断 `candidate.resume is None`，两边假设不完全一致。
7. **状态流转表只在 API 层校验**，工作流节点直接 `update_candidate_status`，存在绕过校验的路径（`error_node`、`confirm_node`）。
8. **`get_candidate_email_reply_node` 没有 `@node_error_wrapper`** —— **这是对的，不要加**。它曾把 `interrupt()` 用 `except Exception` 包住，吞掉 `GraphInterrupt`，导致图从不暂停、回复永远收不到（已于 2026-11-17 修复）。`node_error_wrapper` 的「未知异常」分支同样会把 `GraphInterrupt` 包装成 `AppException`，破坏中断，所以该节点必须保持裸调用。
9. **`route_after_check_availability` 返回 `"confirmed"`，节点名是 `"confirm"`**，靠 `path` 映射 `{"confirmed": "confirm"}` 连接——能跑，但命名容易看错。
10. **`check_availability_node` 里 `can_not_find_available_time` 返回的 `"SYSTEM_TIME_UNAVAILABLE"` 与路由常量 `REPLY_TIME_AVAILABLE` 不是同一套语义**，路由只认 `REPLY_REFUSED` / `REPLY_TIME_AVAILABLE`，其余走轮次判断。
11. **`email_invite` 无空闲分支返回 `email_sent_at: None`**，但 DB 里 `update_email_invite_sent_at` 写了时间，state 与库不一致（超时巡检按库里的值扫，能捞到，不影响功能，但读 state 的代码别依赖这个字段）。
12. **README 的测试数量长期滞后**（曾写 46，实际先涨到 169、再涨到 219）。数字写死在文档里必然漂移，改成跑一次 `pytest --collect-only -q` 校准。
13. **`get_user_access_token` 里 `str(await r.get(...))` 在 key 不存在时会得到 `"None"`**，再用它去刷新（`dingtalk_ser.py` L122）。
14. **`login` 失败返回 400 而不是 401**（有意为之，前端拦截器会因 401 自动刷新 token）。
15. **`refresh_access_token` 抛 `ValueError`**，路由没有显式转换，可能变成 500 而不是 401。

---

## 附：一分钟自我介绍模板（技术版）

> 这是一个基于 **FastAPI + LangGraph** 的 AI 招聘后端。核心是把「简历筛选 → AI 评分 → 邮件邀约 → 面试时间协商 → 日程确认」做成一个 **10 节点的显式状态机**，而不是让 LLM 自由决策。
>
> 三个我认为最值得讲的点：
> 1. **人机协作**：用 `interrupt()` 让状态图停在「等待候选人回邮件」，邮件机器人收到回复后用 `Command(resume=...)` 恢复；配合 `AsyncPostgresSaver` 检查点，服务重启也能续跑。
> 2. **异常分层**：业务异常在节点内转成 error state，基础设施异常穿透到外层统一标记 `PROCESSING_ERROR`，并且因为外层是 `BackgroundTasks`，所有异常必须在函数内完全处理、不能 re-raise。
> 3. **工程化**：日志走队列 + 独立监听进程保证多进程安全；限流/Token 用 Redis Lua 保证原子性；阈值、轮次、时间窗口全部配置化。
