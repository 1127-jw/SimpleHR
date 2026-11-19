# 事故汇总（Incident Summary）

> 把项目从联调至今的线上故障、数据脏化、链路卡死等事件汇总成一份复盘视图。
> 每一条都按「现象 → 根因 → 影响 → 修复 → 教训」展开，并附**可复用的检查清单**。
>
> 详细变更过程见 `change2026-11-17.md` / `change2026-11-18.md` / `change2026-11-19.md`。

---

## 0. 事故概览

| # | 日期 | 级别 | 标题 | 状态 |
|---|------|------|------|------|
| 1 | 联调期 | 🟠 高 | `/user/me` 返回 ORM 对象，泄露密码哈希 | 已修复 |
| 2 | 联调期 | 🔴 严重 | `confirm_node` 重复创建面试记录 → 唯一约束冲突 | 已修复 |
| 3 | 联调期 | 🟠 高 | 检查点 `candidate_id` 过期 → 外键约束违反 | 已修复 |
| 4 | 联调期 | 🟠 高 | 候选人回复邮件不命中规则 → 流程永不恢复、状态卡死 | 已修复 |
| 5 | 联调期 | 🟠 高 | PG 枚举缺「人工通过/重试」→ 写库 `DataError` | 已修复 |
| 6 | 联调期 | 🟠 高 | `candidates.email_invite_sent_at` 列缺失 → 列表查询报错 | 已修复 |
| 7 | 11-17 | 🟡 中 | 固定心跳阈值在 `daily_times` 下**永久误报** | 已修复 |
| 8 | 11-18 | 🔴 严重 | `get_redis` 漏 `await` → 超时巡检连续失败 **30 轮** | 已修复 |
| 9 | 11-18 | 🟡 中 | 浏览器启发式缓存 → 接口请求**根本没到服务器** | 已修复 |
| 10 | 11-18 | 🟠 高 | 「上午/下午/全天」提议被排到**凌晨 00:00** | 已修复 |
| 11 | 11-18 | 🟠 高 | 「7 天内无空档」后回「确认」→ `KeyError` | 已修复 |
| 12 | 11-18 | 🟠 高 | `GET /candidate/ai-score/{id}` 越权可读他人评分 | 已修复 |
| 13 | 11-18 | 🟡 中 | 协商全程静默：被拒/超时都不通知候选人 | 已修复 |
| 14 | 11-18 | 🟡 中 | 巡检锁误删他人锁 → 并发重复 resume | 已修复 |
| 15 | 11-18 | 🟡 中 | `confirm_node` 先落库后调外部系统 → 脏状态 | 已修复 |
| 16 | 11-19 | 🔴 严重 | 评分 9 分却显示「AI筛选失败」+ 邀请后紧跟「协商暂停」 | 已修复 |
| 17 | 11-19 | 🟠 高 | 回复邀请后收到**两封邮件**（重复发邀请） | 已修复 |
| 18 | 11-19 | 🟠 高 | `settle_node` 参数不匹配 → 恢复图失败 | 已修复 |
| 19 | 11-19 | 🟠 高 | 人工重试时 AI 评分唯一键冲突 → 候选人被打成「处理异常」 | 已修复 |
| 20 | 11-19 | 🟡 中 | 人工重试不写状态变更链 | 已修复 |

---

## 1. 联调期事故

### 1.1 `/user/me` 返回 ORM 对象，泄露密码哈希

- **现象**：`GET /user/me` 响应里出现 `_password` 字段（Argon2 哈希）。
- **根因**：接口直接 `return user`（`UserModel` ORM 对象），FastAPI 序列化时把私有列也带出去。
- **影响**：所有登录用户的密码哈希可被任意读取，属于**敏感信息泄露**。
- **修复**：所有用户相关接口统一返回 `UserSH` schema（白名单字段），并在 `UserSH` 中把 `department` 改为 `Optional`。
- **教训**：**永远不要直接返回 ORM 对象**。Schema 是契约，也是安全边界。

### 1.2 `confirm_node` 重复创建面试记录

- **现象**：`UniqueViolation: 重复键违反唯一约束 "uq_interviews_candidate_id"`。
- **根因**：`interviews.candidate_id` 有唯一约束；人工重试时该候选人已有面试记录，`confirm_node` 仍走 `INSERT`。
- **影响**：面试确认失败，候选人被打成「处理异常」。
- **修复**：`confirm_node` 改为 **先查后写**：已有记录则 `UPDATE`，无则 `INSERT`。
- **教训**：有唯一约束的表，写入前必须考虑「已存在」分支。

### 1.3 检查点 `candidate_id` 过期 → 外键约束违反

- **现象**：`ForeignKeyViolation: 键值 (candidate_id)=(xxx) 没有在表 "candidates" 中出现`。
- **根因**：人工重试保留了旧检查点，检查点里的 `candidate` 对象是旧数据（ID 已不在库中）；`confirm_node` 用 `state["candidate"].id` 建面试记录。
- **影响**：确认面试时报错，流程中断。
- **修复**：在 `trigger_manual_retry` 与 `notify._resume_candidate_graph` 的 `Command(update=...)` 中**重新注入最新的 candidate/position/interviewer**。
- **教训**：**检查点是快照，不是真相**。长时间存活的检查点必须在使用前刷新关键外键。

### 1.4 候选人回复邮件不命中规则 → 流程永不恢复

- **现象**：候选人回复「确认」「好的」「明天下午三点方便」，状态一直停在「AI筛选成功」，不进入协商。
- **根因**：邮件机器人用关键词规则匹配，短回复不含「面试时间」等关键词，落到保底规则「其他邮件」，`notify` 的 `else` 分支是 `pass`。
- **影响**：**面试协商链路完全失效**，候选人回复被静默丢弃。
- **修复**：
  1. 扩大关键词（确认/好的/可以/方便/上午/下午…）；
  2. 新增 `_handle_candidate_reply()` 兜底：即使规则没命中，只要候选人处于 `AI筛选成功`/`人工通过/重试` 就恢复图。
- **教训**：**关键词匹配天然脆弱**，必须有「状态驱动」的兜底路径。

### 1.5 PG 枚举缺「人工通过/重试」→ `DataError`

- **现象**：`invalid input value for enum candidatestatusenum: "人工通过/重试"`。
- **根因**：Python 枚举新增了值，但 PostgreSQL 的 ENUM 类型没同步（Alembic 迁移未执行）。
- **影响**：人工通过/重试写库失败。
- **修复**：新增迁移 `ALTER TYPE candidatestatusenum ADD VALUE IF NOT EXISTS '人工通过/重试'` 并执行。
- **教训**：**改枚举必须配迁移**。Python 枚举 ≠ 数据库枚举。

### 1.6 `candidates.email_invite_sent_at` 列缺失

- **现象**：`UndefinedColumn: 字段 candidates.email_invite_sent_at 不存在`，候选人列表页打不开。
- **根因**：模型已加该列，但对应 Alembic 迁移（`c3d4e5f6a7b8`）未执行。
- **影响**：候选人列表 500。
- **修复**：`alembic upgrade head`。
- **教训**：部署流程里**迁移是必经步骤**，漏跑迁移是高频事故。

---

## 2. 线上事故（11-17 ~ 11-19）

### 2.1 `get_redis` 漏 `await` → 巡检失败 30 轮 🔴

- **现象**：
  ```
  WARNING | TimeoutSweepManager 执行失败（重试耗尽，连续第 30 次）
  error='coroutine' object has no attribute 'set'
  ```
- **根因**：`get_redis()` 定义成 `async def`，但它只是取模块级单例、**无任何 I/O**。5 个直接调用点里 3 个没写 `await` → `r` 成了 coroutine。
- **影响**：超时巡检**连续 30 轮全部失败**，等待回复超时的候选人无人推进。
- **修复**：`get_redis()` 改为**同步 getter**，去掉两处多余的 `await`；补 3 条契约测试（用真实 getter 跑通巡检）。
- **教训**：
  1. **无 I/O 的 getter 不要标 `async`**——这会让「该不该 await」变成玄学；
  2. **测试替身必须与真实契约一致**（旧测试用 sync lambda 掩盖了 async 定义）。

### 2.2 评分 9 分却显示「AI筛选失败」+ 邀请后紧跟「协商暂停」 🔴

- **现象**：两个独立缺陷叠加。
  1. 候选人评分 **9 分**（阈值 7），前端显示 **AI筛选失败**；
  2. 收到面试邀请后 **5 秒**又收到「面试时间协商-已暂停」。
- **根因一**：人工闸门写 `PENDING_MANUAL_DECISION`，但 PG 枚举没这个值 → 写库 `DataError`，而闸门把异常吞了 → 状态仍是「AI筛选成功」，前端看不到「发送邮件」按钮。
- **根因二**：所有 `interrupt()` 节点**共用同一个 resume 通道**，中断点没有身份标识。超时巡检用 `Command(resume="TIMEOUT")` 唤醒时，只看「有没有中断点」，把**人工闸门**也误开了。
- **影响**：评分达标的候选人被误判淘汰；已发出的邀请被误判超时。
- **修复**：
  1. 新增枚举迁移；
  2. `candidate_state` 引入 `EMAIL_REPLY_INTERRUPT` / `MANUAL_DECISION_INTERRUPT` 身份常量；
  3. 恢复方必须用 `is_waiting_on(interrupts, marker)` **自证身份**；
  4. `resume_manual_decision` 加「中断点身份 + 库状态」双校验。
- **教训**：
  1. **中断点必须有身份**，不能只判断「有没有挂起」；
  2. **吞异常是万恶之源**——写库失败被吞，导致状态与检查点不一致，后续所有判断都错位。

### 2.3 回复邀请后收到两封邮件 🟠

- **现象**：候选人回复后同时收到「面试时间不可用」和新的「面试邀请」。
- **根因**：`notify` 用 `Command(resume=回复内容)` 唤醒一个**已被消费的 thread**，LangGraph 让图**从头重跑**；重跑时 state 里残留 `skip_scoring=True`，直达 `email_invite` 又发了一次邀请。
- **影响**：候选人收到重复/矛盾邮件，体验差。
- **修复**（三层）：
  1. 恢复前用 `is_waiting_on(snapshot.interrupts, EMAIL_REPLY_INTERRUPT)` 校验中断点身份；
  2. `trigger_manual_retry` 不再写 `skip_scoring`（污染重跑路径）；
  3. 统一用模块级编译图，避免「校验用一个图、执行用另一个图」。
- **教训**：**resume 前先确认中断点还在**，否则图会重跑。

### 2.4 `settle_node` 参数不匹配 → 恢复图失败 🟠

- **现象**：
  ```
  ERROR | notify 恢复图失败 | TypeError: write_terminal_record()
  got an unexpected keyword argument 'from_status'
  ```
- **根因**：`settle_node` 调 `write_terminal_record(..., from_status=...)`，但函数签名没有这个参数（`archive_node` 恰好没传，所以只有结算路径会炸）。
- **影响**：确认面试后链尾补不上，候选人被标「处理异常」。
- **修复**：`write_terminal_record()` 增加 `from_status` 参数并透传；补 2 条测试覆盖收尾节点的实际调用。
- **教训**：**测试要覆盖真实调用路径**，只测路由/结构会漏掉参数不匹配。

### 2.5 人工重试时 AI 评分唯一键冲突 🟠

- **现象**：
  ```
  UniqueViolation: 重复键违反唯一约束 "ix_candidate_ai_scores_candidate_id"
  During task with name "scoring"
  ```
- **根因**：上一轮修「重复发邀请」时，误删了 `trigger_manual_retry` 的 `skip_scoring=True`。实测 `Command(goto="email_invite")` 仍会先经过 screening/scoring → 重复插入 AI 评分 → 撞唯一键。
- **影响**：人工重试直接失败，候选人被打成「处理异常」。
- **修复**：
  1. 恢复 `skip_scoring: True`（并注明实测结论）；
  2. 评分写入改 **upsert**（`upsert_candidate_score`），即使 skip 失效也不炸。
- **教训**：**`goto` 不等于「跳过节点」**——它只覆盖条件边，静态边 `START → screening` 依然会走。

---

## 3. 高发模式（Root Cause Patterns）

把上面 20 条事故归类，反复出现的模式只有 5 类：

| 模式 | 典型事故 | 本质 |
|------|----------|------|
| **① 数据库 schema 与代码漂移** | #5 #6 #16 | 改了模型/枚举，没跑迁移；或迁移写了没执行 |
| **② 检查点/状态过期** | #3 #17 | 长时间存活的快照被当成真相使用 |
| **③ 异常被吞** | #16 | `except: pass` / 只记日志，导致状态与预期错位 |
| **④ 身份/归属缺失** | #16 #22 | 中断点无身份、锁无持有者、接口无可见性校验 |
| **⑤ 并发/幂等缺失** | #2 #14 #19 | 唯一约束 + 无 upsert、锁误删、重复 resume |

---

## 4. 部署前检查清单（从事故里提炼）

### 4.1 数据库

- [ ] 模型有改动 → `alembic revision --autogenerate` 并**人工复核**（会误删 LangGraph 表）
- [ ] 枚举有改动 → 必须配 `ALTER TYPE ... ADD VALUE` 迁移
- [ ] 部署流程里 `alembic upgrade head` 是**必经步骤**
- [ ] 有唯一约束的列，写入前考虑 upsert / 先查后写

### 4.2 状态与检查点

- [ ] 长时间存活的检查点，使用前刷新关键外键（candidate/position）
- [ ] `resume` 前必须校验**中断点身份**（`is_waiting_on`），不能只看「有没有挂起」
- [ ] `goto` 不能假设跳过静态边；需要跳过就显式写 `skip_*` 标记

### 4.3 异常处理

- [ ] **不吞异常**：吞掉必须至少落库/标记状态，否则状态与检查点错位
- [ ] 后台任务（BackgroundTasks / 定时任务 / 邮件回调）**必须就地兜底**，全局异常处理器管不到
- [ ] 每个「非 HTTP 链路」都要有 `mark_candidate_error` 这类统一兜底

### 4.4 并发与幂等

- [ ] 分布式锁：**校验持有者**再释放（Lua `GET == ARGV[1]`）
- [ ] 消息/任务：幂等键（Message-ID / email）+ 去重
- [ ] 限流：无 I/O 的 getter 用同步；`Depends` 路径要测

### 4.5 可观测

- [ ] 后台任务心跳阈值**由调度配置推导**，不写死
- [ ] 连续失败 **N 次**才告警，恢复前只报一次
- [ ] 关键操作写 `audit.log`（登录、下载、删除、改密）

---

## 5. 复盘：如果只记得三件事

1. **数据库是真相，代码是意图** —— 两者漂移（枚举/列/索引）是最高频事故源，迁移必须进部署流程。
2. **不要吞异常** —— 吞掉异常 = 让状态与检查点脱钩，后续所有判断都会错位，排查成本指数上升。
3. **后台链路没有全局处理器** —— 凡是脱离 HTTP 请求上下文的执行（定时任务、邮件回调、消息消费者），必须在**就地**完成日志 + 状态标记 + 兜底。

---

## 6. 相关文档

| 文档 | 内容 |
|------|------|
| [`change2026-11-17.md`](change2026-11-17.md) | 调度/心跳/重试/中断节点修复 |
| [`change2026-11-18.md`](change2026-11-18.md) | 全仓库缺陷排查（17 项）+ `get_redis` 事故 |
| [`change2026-11-19.md`](change2026-11-19.md) | 人工闸门 + 线上事故 + 状态变更链 |
| [`error_handling_design.md`](error_handling_design.md) | 分层异常架构设计 |
| [`monitoring-alerting-healthcheck.md`](monitoring-alerting-healthcheck.md) | 监控告警与心跳方案 |
