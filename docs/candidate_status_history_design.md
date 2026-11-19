# 候选人状态变更链（candidate_status_history）设计

> 状态：**方案已确认，待实现**
> 决策口径（2026-11-19 确认）：
> ① **细分终态 + `archived_at`**（不把 `candidates.status` 覆盖成「已归档」）；
> ② 超时起算用 `COALESCE(last_email_received_at, changed_at)`；
> ③ `candidates.status` 保留细分值，归档信息放 `archived_at`；
> ④ `confirm` 走**独立结算节点**（`next_id = '-2'`，状态置「待面试」）；
>    其余终态（淘汰/拒绝/协商超时/异常）走归档（`next_id = '-1'`）；
> ⑤ **归档与结算都写 `archived_at`**——它表示「图流程走完」，不是「被淘汰」；
> ⑥ 创建候选人支持 `skip_scoring`：跳过筛选与评分，**直达 `email_invite`**（见 §3.4）；
> ⑦ 修复「人工重试不写链记录」的 bug（见 §5.4）。

---

## 一、背景

### 1.1 一次线上事故

2026-11-19：① 候选人评分 **9 分**（阈值 7）却显示 **AI筛选失败**；
② 收到面试邀请邮件后 **5 秒**又收到「面试时间协商-已暂停」。

### 1.2 根因（两个独立缺陷叠加）

| # | 缺陷 | 后果 |
|---|---|---|
| A | `candidates.status` 是 **PostgreSQL ENUM**，Python 侧新增的 `PENDING_MANUAL_DECISION` 没同步到库，写状态抛 `DataError`，而闸门把异常吞了 | 状态卡在「AI筛选成功」，前端看不到「发送邮件」按钮 |
| B | 检查点 `thread_id = candidate.email`，**同一邮箱的多条候选人记录共用一个 thread** | 超时巡检用旧记录的 email 去 `Command(resume="TIMEOUT")`，唤醒了**另一条记录**的 interrupt |

实测时序：

```
10:16:00  新建候选人 PQQA（email=11272664@qq.com，该邮箱已有 7 条记录）
10:16:09  评分 9 分 → 人工闸门挂起
10:16:20  超时巡检扫到旧记录 jeS5（其 email_invite_sent_at=11-12），
          用 email 当 thread_id → Command(resume="TIMEOUT") 打到 PQQA 的闸门
10:16:20  闸门把 "TIMEOUT" 当成「不发送」→ 淘汰 → 状态 AI筛选失败
10:18:20  又一次 → 这次中断点是邮件回复 → negotiation_expired → 发「协商暂停」邮件
```

### 1.3 现有模型为什么不够

- `candidates.status` 是**单值**，表达不了「怎么走过来的」，也无法区分同一邮箱的多次流程；
- 超时判据 `email_invite_sent_at` 是**流程级字段**，分不清它属于哪条记录；
- 没有「归档」概念，终态与「可再次发起」之间没有明确边界。

---

## 二、目标

1. **状态可追踪**：每次状态变更留痕，串成时间链；
2. **流程按记录隔离**：`thread_id` 不再用邮箱；
3. **超时判据精确**：只针对「协商中 + 链上最新 + 超时」的记录，不误伤他人；
4. **终态区分「归档」与「结算」**：真正的淘汰走归档；确认面试时间走结算（转人工维护）。

---

## 三、状态定义

### 3.1 枚举变更（`CandidateStatusEnum`）

| 值 | 含义 | 变化 |
|---|---|---|
| `NEGOTIATING` = `面试时间协商中` | 流程进入面试时间协商阶段（含等回复、改约） | **新增**（PG 需同步） |
| `PENDING_MANUAL_DECISION` = `待人工确认` | 评分通过后等 HR 拍板 | 已有 |
| `WAITING_FOR_INTERVIEW` = `待面试` | 面试时间已确认 | 已有（confirm 写入） |
| 其余（AI筛选成功/失败、拒绝面试、协商超时、处理异常…） | 细分终态 | **保留，不再被覆盖** |
| ~~`ARCHIVED`~~ | —— | **不新增**，改用 `archived_at` |

> **为什么不用「统一覆盖成已归档」**：`已入职 / 已拒绝 / 拒绝面试 / 协商超时 / 处理异常`
> 这些细分值目前**工作流根本不会写入**（工作流终态只有 待面试/AI筛选失败/拒绝面试/协商超时/处理异常），
> 而 `已入职` 等是 HR 在界面上手动改的。所以：
> - 归档**不覆盖** status → 细分信息不丢，**旧的筛选/统计查询一行都不用改**；
> - 归档用 `archived_at` 表达 → 可筛可统计。
>
> 这样既满足「已归档表示流程走完」，又不牺牲既有查询。

### 3.2 `candidates` 表新增字段

```sql
ALTER TABLE candidates ADD COLUMN archived_at TIMESTAMP;   -- NULL=流程仍在进行
CREATE INDEX ix_candidates_archived_at ON candidates (archived_at);
```

### 3.3 状态流转

```
scoring 通过
   └─> PENDING_MANUAL_DECISION（人工闸门）
         ├─ approve ─> NEGOTIATING（发邀请邮件，进入协商）
         │                 ├─ 确认时间 ─> WAITING_FOR_INTERVIEW ─> [结算] ─> END
         │                 ├─ 拒绝     ─> REFUSED_INTERVIEW ──┐
         │                 ├─ 轮次耗尽 ─> NEGOTIATION_EXPIRED ─┤
         │                 └─ 回复超时 ─> NEGOTIATION_EXPIRED ─┤
         ├─ reject  ─> AI_FILTER_FAILED ─────────────────────┤
         └─ archive ─> （终态，写 archived_at）──────────────┤
scoring 未通过 ─> AI_FILTER_FAILED ──────────────────────────┤
error ───────────────────────────────────────────────────────┤
              ↓ 收尾节点（archive: next_id='-1'；settle: next_id='-2'；两者都写 archived_at）
                                END
```

### 3.4 跳过筛选直达邀请（`skip_scoring`）

创建候选人时允许 HR 直接发起面试邀请，跳过 AI 筛选与评分。

**要激活的字段**：`skip_scoring` 当前是**死字段**——全仓只有
`candidate_status_ser.py:213`（人工重试的 update）写入，**没有任何节点或路由读取**
（grep 确认）。正好用它，不用新增字段。

| 位置 | 改动 |
|---|---|
| `CandidateCreateSchema` | 新增 `skip_scoring: bool = Field(False, description="跳过筛选与评分，直达面试邀请")` |
| `create_candidate` 路由 / `create_candidate_ser` | 把该值透传进起图的初始 state |
| `agent_tasks._run_candidate_agent_impl` | 初始 state 增加 `"skip_scoring": skip_scoring` |
| `screening_node` | `if state.get("skip_scoring"): return {"screening_passed": True, "current_stage": "scoring"}`（不筛选，直接放行） |
| `scoring_node` | `if state.get("skip_scoring"): return {"current_stage": "email_invite"}`（不评分、不写 AI 评分） |
| `_route_after_scoring` | `if state.get("skip_scoring"): return "email_invite"`——**这一步是关键**，否则仍会掉进 `PENDING_MANUAL_DECISION` 闸门，不算「直达」 |

**注意**：`skip_scoring=True` 时**不产生 AI 评分记录**，`/candidate/ai-score/{id}` 会返回
400「候选人的AI评分不存在」。这是预期，前端应据此隐藏「AI评分」入口。

**权限**：`create_candidate` 目前只要求登录。跳过筛选等于绕过 AI 评估，
建议限定 HR / 超管（`get_hr_or_superuser`），或至少记审计。**待确认**。

**两种链终止**：

| `next_id` | 含义 | 触发 | `candidates.status` | `archived_at` |
|---|---|---|---|---|
| `NULL` | 链上最新（活跃） | 每次新变更 | 当前状态 | NULL |
| `'-1'` | **归档**终止 | 淘汰/拒绝/协商超时/异常 | **保留细分值** | 写时间 |
| `'-2'` | **结算**终止 | `confirm` 确认面试时间 | `待面试`（细分保留） | **写时间** |

**`archived_at` 的语义是「图流程走完」，不是「被淘汰」**：归档与结算**都会写**。
区别只在 `next_id`——`-1` 是淘汰/放弃/异常收尾，`-2` 是流程正常完成（转人工维护，
后续 HR 手动改「已入职 / 已拒绝 / …」）。两者都不再需要图自动跟进，
所以归档筛选（`archived_at IS NOT NULL`）会把「待面试」的结算态也包含进来，这是预期。

**归档后可重新发起**：HR 再点「人工通过/重试」→ **同一个 thread**、用 `Command(goto=...)` 重新进入
（实测：已 END 的图用 `goto` 可唤醒，不需要新图），同时**往链表追加一条新记录**（旧链保持 `-1`/`-2`）。

---

## 四、状态变更表

### 4.1 DDL

```sql
CREATE TABLE candidate_status_history (
    id                   VARCHAR(100)  PRIMARY KEY,
    candidate_id         VARCHAR(100)  NOT NULL,
    candidate_email      VARCHAR(255)  NOT NULL,
    position_id          VARCHAR(100),
    position_title       VARCHAR(255),
    from_status          VARCHAR(50),          -- 首条记录为 NULL
    to_status            VARCHAR(50)  NOT NULL,
    changed_at           TIMESTAMP    NOT NULL, -- 变更时间
    last_email_received_at TIMESTAMP,           -- 最后接到该候选人邮件的时间
    next_id              VARCHAR(100)          -- NULL=活跃；'-1'=归档；'-2'=结算
);

-- 关键：保证每条链「有且只有一条」最新记录
CREATE UNIQUE INDEX ux_csh_active
    ON candidate_status_history (candidate_id) WHERE next_id IS NULL;

CREATE INDEX ix_csh_email      ON candidate_status_history (candidate_email);
CREATE INDEX ix_csh_changed_at ON candidate_status_history (changed_at);
```

字段与你的要求一一对应：

| 你的要求 | 字段 |
|---|---|
| 本条记录 id | `id` |
| 候选人 id 和邮箱 | `candidate_id` / `candidate_email` |
| 候选人面试职位 | `position_id` / `position_title` |
| 状态的变更 | `from_status` → `to_status` |
| 变更时间 | `changed_at` |
| 最后接到候选人的邮件时间 | `last_email_received_at` |
| next_id（串成时间链） | `next_id` |

### 4.2 链的写入（必须单事务）

```
① UPDATE candidate_status_history SET next_id = :new_id
   WHERE candidate_id = :cid AND next_id IS NULL      -- 抢「最新」位置
② INSERT 新记录（from_status = 旧值, to_status = 新值, next_id = NULL）
```

- ① 影响行数为 0 → 链断/并发冲突 → **回滚并报错**，不要静默继续；
- `ux_csh_active` 是唯一索引层面的兜底；
- **归档/结算**：追加最后一条记录，并把**该条**的 `next_id` 置为 `'-1'` / `'-2'`。

### 4.3 读取

| 用途 | 查询 |
|---|---|
| 某候选人当前状态 | `WHERE candidate_id = :id AND next_id IS NULL` |
| 某邮箱是否可建图 | `WHERE candidate_email = :email AND next_id IS NULL`（见 §8） |
| 超时巡检 | 见 §6.2 |
| 时间链 | 从最新记录沿 `next_id` 回退，或按 `changed_at` 正序 |

---

## 五、图改造

### 5.1 两条收尾路径（不再统一归档）

```python
# confirm：结算（保留「待面试」，next_id = '-2'）
graph.add_edge("confirm", "settle")
graph.add_edge("settle", END)

# 其余终态：归档（写 archived_at，next_id = '-1'）
for node in ("rejected", "refused", "negotiation_expired", "error"):
    graph.add_edge(node, "archive")
graph.add_edge("archive", END)
```

- `archive_node`：追加 `to_status = 当前细分状态` 的链记录并置 `next_id = '-1'`，
  同时 `candidates.archived_at = now`。**不动 `candidates.status`**。
- `settle_node`：追加 `to_status = '待面试'` 的链记录并置 `next_id = '-2'`。
  `candidates.status` 已由 `confirm_node` 写成 `待面试`，此处不覆盖。

**附加测试**：断言「除 `archive` / `settle` 外没有节点直连 `END`」，防止以后漏收尾。

### 5.2 人工闸门增加「归档」

| 恢复值 | 行为 |
|---|---|
| `approve` | → `NEGOTIATING`（发邀请邮件） |
| `reject` | → `AI_FILTER_FAILED` → 归档 |
| `archive` | → 直接归档（HR 判定这条流程不用再走） |

### 5.3 thread_id 改造（必须一起做）

**链表只解决「该处理谁」，`thread_id` 才决定「实际唤醒谁」。** 只改链表不改 thread_id，缺陷 B 依然存在。

实测证据：一个已到 `END` 的图，`Command(goto=...)` 可以重新进入（也能用普通输入唤醒）；
但 `thread_id` 若仍用 email，`goto` 唤醒的还是**同邮箱那条 thread**。统一：

```python
def thread_id_of(candidate_id: str) -> str:
    """检查点 thread_id：按候选人记录隔离。

    邮箱会重复（同一候选人多次投递会产生多条记录），用它当 thread_id 会让
    不同记录的操作打到同一个 thread 上——实测导致 A 的超时唤醒了 B 的闸门。
    """
    return f"candidate:{candidate_id}"
```

替换点：`agent_tasks`（建图 + `adelete_thread`）、`timeout_tasks`、
`candidate_status_ser`（两处）、`notify`。

> 你提的 `{candidate.id}:{email}` 也能用（id 已唯一），但 email 部分不产生额外收益。
> 用 `candidate:{id}`；排查时 candidate_id / email 都在链表里。

### 5.4 修复：人工重试不写链记录

**现状 bug**：`trigger_manual_retry()`（`candidate_status_ser.py:163`）只做了
`ainvoke(Command(goto="email_invite", ...))`，**完全没有写状态变更表**。后果：

- 重试产生的新一轮流程在链表里**不存在**——`next_id IS NULL` 永远指向旧链，
  §6 的超时巡检、§8 的建图查重都覆盖不到它；
- 重试后状态变化（进入 `NEGOTIATING`、后续到 `待面试`/归档）在链表里断档，
  审计上只看到「人工通过」然后直接跳到归档。

**修法**：在 `trigger_manual_retry` 的 `ainvoke` **之前**（同一请求内）写两条链记录：

1. 旧链终止：把该 candidate 当前活跃记录（`next_id IS NULL`）置为 `'-1'` 或 `'-2'`
   ——取决于归档还是结算，重试属于「旧流程终止」，用 `'-1'`；
2. 新链开始：`record_change(from_status=旧状态, to_status='待人工确认')`，
   `next_id = NULL`。

> 注意：重试是 `goto="email_invite"`，会**跳过人工闸门**（HR 点重试即已拍板），
> 所以新链第一条应是 `待人工确认` 还是 `面试时间协商中`？
> 按现有代码它直接进 `email_invite` → `NEGOTIATING`，
> 因此新链记 `from=人工通过/重试, to=面试时间协商中`，`next_id=NULL`。
> **并保持 `goto` 目标为 `email_invite` 不变**（不要改成 `manual_decision`，
> 否则 HR 点重试还要再确认一次，与现状语义不符）。

**同一 bug 的另一处**：`resume_manual_decision()` 的 `approve` 分支也只是
`goto` 级别的状态推进，同样要补 `record_change(to='面试时间协商中')`。

**归档 / 结算节点**：`archive_node` / `settle_node` 本身就要写终止记录（见 §5.1），
这部分是新增而非修复。

---

## 六、超时巡检改造

### 6.1 判据

只处理满足**全部**三个条件的记录：

1. 链上最新（`next_id IS NULL`）——不是最新说明这条流程已有后续；
2. 状态是 `NEGOTIATING`（面试时间协商中）；
3. 起算时间超时：`COALESCE(last_email_received_at, changed_at) < now - 48h`。

> 用 `COALESCE` 是因为「邀请刚发出、候选人还没回过信」时 `last_email_received_at` 是 NULL，
> 直接比较会**永远漏判**。以「进入协商的时间」为兜底起算点。

### 6.2 SQL

```sql
SELECT *
FROM candidate_status_history
WHERE to_status = '面试时间协商中'
  AND next_id IS NULL
  AND COALESCE(last_email_received_at, changed_at) < :cutoff
ORDER BY changed_at
LIMIT 200;
```

拿到记录后 `thread_id = thread_id_of(candidate_id)`，再走既有的
「校验中断点身份 → `Command(resume=EMAIL_REPLY_TIMEOUT)`」流程。

---

## 七、「最后接到候选人邮件时间」的维护

位置：`utils/emailbot/notify.py` 的 `notify()` 入口。

- 判定：`role == SenderRole.CANDIDATE`（**只有候选人本人的邮件才算**；
  面试官邮件、系统通知不算——否则面试官一封邮件就把 48h 超时重置了）；
- 动作：更新该 candidate **当前活跃记录**（`next_id IS NULL`）的 `last_email_received_at`。

```sql
UPDATE candidate_status_history
SET last_email_received_at = :now
WHERE candidate_id = :cid AND next_id IS NULL;
```

---

## 八、建图查重（两个 API）

`POST /mail-submissions/{id}/confirm`、`POST /candidate/create` 在**建图之前**：

```sql
SELECT id, to_status, next_id FROM candidate_status_history
WHERE candidate_email = :email AND next_id IS NULL;
```

| 结果 | 处理 |
|---|---|
| 查不到 | 允许建图 |
| 查到，`next_id = '-1'`（已归档） | 允许（下一瞬间新链开始） |
| 查到，`next_id = '-2'`（已结算，待面试） | **409**：人在后续阶段，不允许重复发起 |
| 查到，仍是活跃（`NULL`） | **409「该邮箱已有进行中的流程」** |

> 也可以直接用 `candidates.archived_at IS NOT NULL` 判「已归档」，二者等价；
> 用 `next_id` 的好处是与链表同源、不依赖 candidates 表的冗余字段。

---

## 九、历史数据与发布

### 9.1 先建模型，再生成迁移（不要手写 DDL）

项目已有 Alembic，且 `alembic/env.py` 已配置 `target_metadata = Base.metadata`（支持
`--autogenerate`）。因此**正确顺序**是「模型 → 迁移」，不是「建表 → 补模型」：

```
1. 在 app/models/ 定义模型
   - CandidateStatusHistoryModel（新表）
   - CandidateModel 增加 archived_at 列
2. 在 app/models/__init__.py 注册新模型（env.py 从 app.models 导入，
   不注册会导致 autogenerate 看不到这张表）
3. alembic revision --autogenerate -m "candidate status history"
4. 人工补 autogenerate 抓不到的三部分：
   - ALTER TYPE candidatestatusenum ADD VALUE '面试时间协商中'（枚举不会自动生成）
   - 部分唯一索引 ux_csh_active（WHERE next_id IS NULL，需确认是否生成）
   - 回填历史数据（§9.2）
5. alembic upgrade head
```

**为什么坚持这个顺序**：手写 DDL 和模型定义容易漂移（字段类型、可空性、索引），
之后 `--autogenerate` 会一直报 diff；反过来以模型为准，迁移永远可由模型重新生成。
`op.execute("ALTER TYPE ...")` 和回填这类**模型无法表达**的改动，留在生成的迁移里手工补。

> 现有迁移文件（如 `a1b2c3d4e5f6_add_manual_passed_to_candidatestatusenum.py`）
> 是早期手写 DDL 的产物，不应当作模板。

### 9.2 历史数据必须初始化

老候选人没有链表记录。若不补，§8 的查重会认为「该邮箱无记录」→ **允许重复建图**。

迁移里给每个现存候选人补一条：

```
from_status            = NULL
to_status              = 当前 candidates.status
changed_at             = 当前 updated_at（或 created_at）
last_email_received_at = candidates.email_invite_sent_at
next_id                = '-1'（若已 archived）/ '-2'（若 status=待面试 且流程已结算）/ NULL
candidates.archived_at = 终态（已入职/已拒绝/拒绝面试/协商超时/处理异常）? now : NULL
```

**这一步不做，整个方案的前半段就白做了。**

### 9.2 发布顺序（严格）

```
1. alembic upgrade head       —— 加 1 个枚举值 + 建表/索引 + 回填历史数据
2. 再发版（新代码）            —— 期间旧代码只读写 candidates.status，不受影响
```

顺序颠倒就是 §1.2 缺陷 A 的重演（写库直接 `DataError`）。

> 注意 §9.1 的顺序是「**先建模型再生成迁移**」，这里是「**先跑迁移再发代码**」——
> 两件事不冲突：模型/迁移的开发顺序，与迁移/发版的执行顺序。

---

## 十、前端

- 状态枚举加 `NEGOTIATING`（协商中）；**不加**「已归档」（归档看 `archived_at`，
  列表加一列/一个筛选项即可）；
- **统计口径不用改**：`candidates.status` 保留细分值，
  `candidate_distribution_ser` 的状态饼图、列表筛选全部照常工作；
- 人工闸门弹窗加第三个按钮「归档」。

> 这是选「细分 + archived_at」最大的收益：**旧查询零改动**。

---

## 十一、测试方案

| 类别 | 用例 |
|---|---|
| 链表写入 | 连续两次变更串成链；并发写入只有一条 `next_id IS NULL`（唯一索引兜底） |
| 归档 / 结算 | 归档后该链无 `NULL` 且 `archived_at` 非空、`status` **仍是细分值**；结算后 `next_id = '-2'` 且 `status = 待面试` |
| 重新发起 | 归档/结算后，同一 thread 用 `goto` 重新进入；链表新增一条，旧链保持 `-1`/`-2` |
| 超时巡检 | ① 协商中 + 超时 → 处理；② 协商中 + 未超时 → 跳过；③ `last_email_received_at IS NULL` 且 `changed_at` 超时 → 处理；④ 同邮箱另一条链活跃 → **不受影响**（本次事故的直接回归） |
| 邮件时间 | 候选人来信 → `last_email_received_at` 更新；面试官来信 → **不更新** |
| thread 隔离 | 同邮箱两条记录各自 resume，互不影响 |
| 建图查重 | 无记录 → 放行；活跃 → 409；已归档 → 放行；已结算 → 409 |
| 图结构 | 除 `archive`/`settle` 外无节点直连 `END` |
| **跳过筛选** | ① `skip_scoring=True` → screening 放行、scoring 不产出、路由**直达 `email_invite`**（不进闸门）；② `False` → 走原有 筛选→评分→闸门；③ 跳过后 `/ai-score` 返回 400 |
| **重试写链** | 重试后链表里能查到新的一条（`from=人工通过/重试, to=面试时间协商中`，`next_id=NULL`），且旧链已终止 |

---

## 十二、风险与遗留

1. **`next_id` 一致性**：`ux_csh_active` 唯一索引是最后一道防线，务必加；
   回填历史数据时要**先关索引再回填**，否则老数据里处于活跃状态的多条记录会撞索引。
2. **`candidates.status` 与链表的一致性**：每次变更两处都写，需在同一事务；
   归档/结算后 `status` 保持细分值，链表负责「是否终止」。
3. **归档 vs 结算的口径**：`-1` 归档、`-2` 结算，**只有归档能重新发起**。
   若产品后续想让「已结算（待面试）」也能重新发起，改 §8 的判定即可，不用动表。
4. **`ALTER TYPE ... ADD VALUE` 不可回滚**（PG 限制）。只新增、不影响既有数据。
5. **链表写放大**：每次状态变更多 1 次 UPDATE + 1 次 INSERT，招聘量级下可忽略。
6. **`skip_scoring` 是绕过 AI 评估的旁路**：谁可以用、是否限 HR/超管、是否记审计，
   需要在实现前确认（§3.4 已标「待确认」）。
7. **`skip_scoring=True` 不产生 AI 评分**：列表页/看板若按「有评分」筛选会漏掉这些记录，
   前端需处理「无评分」的展示。

---

## 十三、工作量拆解

| 模块 | 内容 | 人天 |
|---|---|---|
| 迁移 | 1 个枚举值 + `candidates.archived_at` + 建表/索引 + 回填 | 0.5 |
| `candidate_history_repo` | `record_change` / `mark_archived` / `mark_settled` / `touch_last_email` / `get_active_by_email` / `list_stale_negotiating` | 0.5 |
| 图改造 | `archive_node` + `settle_node` + 闸门三选项 + thread_id 统一 | 0.5 |
| 巡检 + 邮件时间 | §6、§7 | 0.5 |
| 两个 API 查重 | §8 | 0.5 |
| **跳过筛选直达** | §3.4：schema 字段 + screening/scoring 判断 + 路由直达 + 透传 | 0.25 |
| **重试写链修复** | §5.4：`trigger_manual_retry` / `resume_manual_decision` 补链记录 | 0.25 |
| 前端 | 状态枚举 + 归档筛选 + 归档按钮 + 创建页「跳过筛选」开关 | 0.5–1 |
| 测试 | §11 全部用例 | 0.5 |
| **合计** | | **约 3.75–4.25** |

> 比「统一覆盖成已归档」那版**少 0.5 天**，因为统计口径不用改；
> 新增的两条（跳过筛选、重试写链）合计 0.5 天。
