# 邮件回复与 Workflow 图集成方案

## 一、当前问题

```
现状：
┌─────────────┐    ┌───────────────────┐    ┌─────────────────────┐
│ email_invite │───▶│ check_availability│───▶│ confirm / refused   │
│  发送邀请    │    │  检查面试官空闲    │    │ 确认/拒绝面试       │
└─────────────┘    └───────────────────┘    └─────────────────────┘
        │
        │  ← 候选人回复邮件在此处，但图无法感知
        │
   notify.py 独立处理，与图完全脱节
```

**问题：** 候选人回复邮件时，`notify.py` 独立运行一套 `CandidateProcessAgent`，与 Workflow 图的状态完全脱节。

---

## 二、目标架构

```
┌─────────────┐    ┌──────────────────────┐    ┌─────────────────────┐
│ email_invite │───▶│get_candidate_email   │───▶│ check_availability  │
│  发送邀请    │    │    _reply_node       │    │  检查面试官空闲      │
└─────────────┘    │  (interrupt 等待)     │    └──────────┬──────────┘
                    └──────────┬──────────┘               │
                               │              ┌───────────┼───────────┐
                          候选人回复邮件       │           │           │
                               │              ▼           ▼           ▼
                    ┌──────────▼───────────┐  ┌────────┐ ┌────────┐ ┌──────────────────────┐
                    │  notify.py 接收邮件   │  │confirm │ │refused │ │get_candidate_email   │
                    │  找到对应的 thread_id │  │ 确认   │ │ 拒绝   │ │    _reply_node       │
                    │  恢复图执行           │  └────────┘ └────────┘ │  (继续等待回复)       │
                    └──────────────────────┘                         └──────────────────────┘
```

**check_availability 的三个分支：**

| 分支 | 条件 | 目标节点 |
|------|------|---------|
| 时间可用 + 候选人确认 | 面试官空闲 且 候选人回复"确定" | `confirm` |
| 候选人拒绝 | 候选人回复"拒绝" | `refused` |
| 时间不可用 / 候选人提议新时间 | 面试官不空闲 或 候选人提出新时间 | `get_candidate_email_reply`（回到等待状态） |

---

## 三、核心机制：interrupt + resume

### 3.1 图侧：定义中断节点

```python
# check_availability_node.py（改进后）

def get_candidate_email_reply_node(state: CandidateRecruitmentState) -> dict:
    """
    中断点：等待候选人回复邮件。
    图执行到此节点后暂停，直到外部调用 resume。
    """
    candidate_email_reply = interrupt("candidate_email_reply")
    return {
        "candidate_email_reply": candidate_email_reply,
        "current_stage": "check_availability"
    }
```

### 3.2 图侧：条件边路由

```python
# 图的边定义
graph.add_edge("email_invite", "get_candidate_email_reply")
graph.add_edge("get_candidate_email_reply", "check_availability")

# check_availability 的三个条件分支
def route_after_check_availability(state: CandidateRecruitmentState) -> str:
    """根据检查结果决定下一步"""
    if state.interview_created:
        return "confirm"       # 面试时间已确认
    elif state.current_stage == "refused":
        return "refused"       # 候选人拒绝
    else:
        return "get_candidate_email_reply"  # 继续协商

graph.add_conditional_edges(
    "check_availability",
    route_after_check_availability,
    {
        "confirm": "confirm",
        "refused": "refused",
        "get_candidate_email_reply": "get_candidate_email_reply",  # 回到等待状态
    }
)
```

### 3.3 notify.py 侧：恢复图执行

```python
# notify.py 改进后

elif rule.name == "询问面试时间或进度":
    logger.info("询问面试时间/进度: 来自 {} - {}", email.from_.address, email.subject)
    
    # 1. 查找候选人对应的 thread_id（从数据库或缓存）
    thread_id = await get_thread_id_by_candidate(email.from_.address)
    if not thread_id:
        logger.warning("未找到候选人的工作流线程")
        return
    
    # 2. 恢复图执行，传入候选人的邮件回复内容
    app = builder.compile(checkpointer=checkpointer)
    result = await app.ainvoke(
        Command(resume=email.text),  # ← 传入邮件内容
        config={"configurable": {"thread_id": thread_id}}
    )
    logger.info("图执行结果: {}", result)
```

---

## 四、关键实现细节

### 4.1 thread_id 的存储与查找

**方案 A：数据库存储**

```python
# 新建 candidate_workflow 表
class CandidateWorkflow(Base):
    __tablename__ = "candidate_workflow"
    candidate_id = Column(String, ForeignKey("customers.id"), primary_key=True)
    thread_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
```

**方案 B：Redis 存储（推荐，因为已有 Redis 依赖）**

```python
async def save_thread_id(candidate_email: str, thread_id: str):
    """保存候选人的工作流线程 ID"""
    r = await get_redis()
    await r.hset("candidate_workflow_threads", candidate_email, thread_id)

async def get_thread_id(candidate_email: str) -> str | None:
    """查找候选人的工作流线程 ID"""
    r = await get_redis()
    return await r.hget("candidate_workflow_threads", candidate_email)
```

### 4.2 图执行入口（发送邮件时创建）

```python
# 在 email_invite 节点中，发送邮件后创建图实例并保存 thread_id
async def email_invite_node(state: CandidateRecruitmentState) -> dict:
    # ... 发送邮件 ...
    
    # 保存 thread_id，供后续 notify.py 恢复使用
    thread_id = f"candidate_{state.candidate.id}"
    await save_thread_id(state.candidate.email, thread_id)
    
    return { "email_sent": True, ... }
```

### 4.3 完整交互流程

```
时间线（正常流程）：

T1. 图执行到 get_candidate_email_reply_node
    → interrupt("candidate_email_reply")
    → 图暂停，状态保存到 PostgreSQL

T2. 候选人回复邮件
    → 邮件服务器收到邮件

T3. MailBot.poll_once() 收到新邮件
    → 匹配规则 "询问面试时间或进度"
    → 调用 notify()

T4. notify() 执行：
    → 从 Redis 查找 thread_id
    → app.ainvoke(Command(resume=email.text), config={"thread_id": "candidate_xxx"})
    → 图从 interrupt 点恢复

T5. get_candidate_email_reply_node 收到 email.text
    → 写入 state.candidate_email_reply
    → 路由到 check_availability 节点

T6. check_availability 检查面试官是否空闲
    → 空闲 + 候选人确认 → 确认面试时间 → confirm 节点
    → 候选人拒绝 → refused 节点
    → 不空闲 / 候选人提议新时间 → 回到 get_candidate_email_reply（T1）


时间线（协商循环）：

... T6. 面试官不空闲
    → 发送邮件告知候选人重新选择时间
    → 回到 get_candidate_email_reply（interrupt 等待）
    → 候选人再次回复
    → T3 → T4 → T5 → T6
    → 直到确认或拒绝
```

---

## 五、notify.py 改进方案

```python
# notify.py 改进后

async def notify(email: ParsedEmail, rule) -> None:
    if rule.name == "简历投递":
        pass

    elif rule.name == "询问面试时间或进度":
        logger.info("询问面试时间/进度: 来自 {} - {}", email.from_.address, email.subject)
        
        # 1. 查找候选人
        candidate = await _find_candidate_by_email(email.from_.address)
        if not candidate:
            logger.warning("未找到对应的候选人")
            return
        
        # 2. 查找对应的图执行线程
        thread_id = await get_thread_id(email.from_.address)
        if not thread_id:
            logger.warning("候选人的工作流线程不存在，可能尚未发送面试邀请")
            return
        
        # 3. 恢复图执行，传入邮件内容
        app = builder.compile(checkpointer=checkpointer)
        result = await app.ainvoke(
            Command(resume=email.text),
            config={"configurable": {"thread_id": thread_id}}
        )
        logger.info("图执行结果: {}", result)

    else:
        pass
```

---

## 六、需要新增/修改的文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `candidate_workflow_nodes.py` | 新增 | 包含 `get_candidate_email_reply_node` |
| `check_availability_node.py` | 修改 | 增加 `route_after_check_availability` 条件路由函数，三分支：confirm/refused/get_candidate_email_reply |
| `notify.py` | 修改 | 将 `CandidateProcessAgent` 调用替换为 `app.ainvoke(Command(resume=...))` |
| `bot.py` | 不变 | MailBot 的轮询逻辑不变 |
| `mail_manager.py` | 新增 | 添加 `save_thread_id` / `get_thread_id` 辅助函数 |

---

## 七、注意事项

1. **thread_id 生命周期**：候选人流程结束后（confirmed/refused），应清理 Redis 中的 thread_id
2. **中断超时**：如果候选人长时间不回复，应有超时处理机制（如 7 天后自动取消）
3. **并发安全**：同一候选人多次回复时，需要确保只恢复一次
4. **状态一致性**：中断期间图状态保存在 PostgreSQL，恢复时自动加载
5. **向后兼容**：旧版 `CandidateProcessAgent` 保留，通过配置开关切换
