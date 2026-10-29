# 招聘工作流重构方案

## 一、从 `create_agent` 重构为 `StateGraph`

### 1.1 现状分析

当前 `CandidateProcessAgent` 使用 `create_agent` + 工具调用模式：

```
create_agent + tools → 黑盒决策 → 工具调用 → 结果
```

**问题：**
- 流程不可见：评分→发邮件→协商→确认的流转隐藏在 LLM 决策中
- 无法调试：出错时难以定位是哪个环节的问题
- 无法扩展：新增阶段需要修改 system prompt 和工具定义
- 状态管理混乱：流程控制字段（`current_stage`）与业务数据混在一起

### 1.2 目标架构

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  screening   │───▶│   scoring   │───▶│ email_invite│───▶│ negotiation │───▶│  confirmed   │
│   (规则筛查)  │    │  (AI评分)   │    │  (发送邀请)  │    │  (时间协商)  │    │  (确认面试)  │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
       │                  │                  │                  │
       ▼                  ▼                  ▼                  ▼
   [rejected]        [rejected]         [无空闲时间]        [refused]
```

### 1.3 重构要点

**节点拆分：**

| 原工具 | 新节点 | 类型 |
|--------|--------|------|
| `score_for_candidate` | `scoring_node` | 异步节点（调用 LLM） |
| `get_interviewer_available_slot` | `email_invite_node` | 异步节点（调用钉钉 API） |
| `send_interview_email` | `email_invite_node` | 合并到同一节点 |
| `confirm_interview_time` | `confirm_node` | 异步节点（写库+创建日程） |
| `refuse_interview` | `refuse_node` | 异步节点（更新状态） |
| `get_current_time` | 移除 | 节点内直接调用 |

**边（路由）定义：**

```python
graph.add_edge("screening", "scoring")           # 通过筛查 → 评分
graph.add_edge("scoring", "email_invite")        # 评分通过 → 发邮件
graph.add_edge("scoring", "rejected")            # 评分不通过 → 拒绝
graph.add_edge("email_invite", "negotiation")    # 邮件发送 → 等待协商
graph.add_edge("negotiation", "confirmed")       # 时间确认 → 完成
graph.add_edge("negotiation", "refused")         # 候选人拒绝 → 结束
```

**条件路由：**

```python
def route_after_screening(state):
    if state.screening_passed:
        return "scoring"
    return "rejected"

def route_after_scoring(state):
    if state.score_result and state.score_result.overall_score > 8:
        return "email_invite"
    return "rejected"
```

### 1.4 状态设计优化

```python
class CandidateRecruitmentState(TypedDict):
    # === 输入（初始化后不变） ===
    candidate: CandidateSchema
    position: PositionSchema
    interviewer: UserSH

    # === 流程控制 ===
    current_stage: str
    negotiation_round: int

    # === 各阶段产出 ===
    screening_passed: Optional[bool]
    screening_reason: Optional[str]
    score_result: Optional[AgentCandidateScoreSchema]
    email_sent: Optional[bool]
    proposed_interview_time: Optional[str]
    confirmed_interview_time: Optional[str]
    interview_created: Optional[bool]

    # === 消息历史 ===
    messages: Annotated[list, add_messages]
```

---

## 二、加入 Interrupt 人工审批节点

### 2.1 设计思路

在关键决策点插入 `interrupt()`，让 HR 可以：
- 审核 AI 评分结果
- 决定是否跳过评分直接发邀请
- 确认最终面试时间

### 2.2 审批节点位置

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   scoring   │───▶│  HR_APPROVE │───▶│ email_invite│───▶│ negotiation  │
│  (AI评分)   │    │  (人工审批)  │    │  (发送邀请)  │    │  (时间协商)  │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
                          │
                    ┌─────┴─────┐
                    │ 跳过评分   │
                    │ (直接发邀请)│
                    └───────────┘
```

### 2.3 实现方式

```python
async def hr_approve_node(state: CandidateRecruitmentState) -> Command:
    """HR 人工审批节点"""
    # 中断并等待人工输入
    approval = interrupt({
        "type": "hr_approval",
        "candidate_name": state.candidate.name,
        "score": state.score_result.overall_score if state.score_result else None,
        "summary": state.score_result.summary if state.score_result else None,
        "options": ["approve", "skip_scoring", "reject"]
    })

    if approval["action"] == "approve":
        return Command(goto="email_invite", update={"current_stage": "email_invite"})
    elif approval["action"] == "skip_scoring":
        return Command(goto="email_invite", update={"skip_scoring": True})
    else:
        return Command(goto="rejected", update={"current_stage": "rejected"})
```

### 2.4 前端集成

```python
# 恢复中断的执行
@app.post("/api/workflow/resume")
async def resume_workflow(thread_id: str, approval: dict):
    app = builder.compile(checkpointer=checkpointer)
    result = await app.ainvoke(
        Command(resume=approval),
        config={"configurable": {"thread_id": thread_id}}
    )
    return result
```

---

## 三、用 `Send()` 实现批量候选人并行处理

### 3.1 设计思路

当需要同时处理多个候选人时，使用 `Send()` API 动态创建并行子图：

```
                    ┌─────────────────┐
                    │  batch_process   │
                    │  (批量分发)      │
                    └────────┬────────┘
                             │ Send() × N
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌──────────┐   ┌──────────┐   ┌──────────┐
        │ candidate│   │ candidate│   │ candidate│
        │    A     │   │    B     │   │    C     │
        └──────────┘   └──────────┘   └──────────┘
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                    ┌─────────────────┐
                    │  aggregate       │
                    │  (结果汇总)      │
                    └─────────────────┘
```

### 3.2 实现方式

```python
async def batch_dispatch_node(state: BatchState) -> list[Send]:
    """批量分发候选人到并行子图"""
    sends = []
    for candidate in state.candidates:
        sends.append(
            Send("process_single_candidate", {
                "candidate": candidate,
                "position": state.position,
                "interviewer": state.interviewer,
                "current_stage": "screening",
            })
        )
    return sends

async def aggregate_results(state: BatchState) -> dict:
    """汇总所有候选人的处理结果"""
    results = state.processed_candidates  # 自动收集所有 Send() 的返回值
    return {
        "results": results,
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.get("current_stage") == "confirmed"),
            "rejected": sum(1 for r in results if r.get("current_stage") == "rejected"),
        }
    }
```

### 3.3 图构建

```python
builder = StateGraph(BatchState)

builder.add_node("batch_dispatch", batch_dispatch_node)
builder.add_node("process_single_candidate", process_single_candidate_graph)
builder.add_node("aggregate", aggregate_results)

builder.add_edge(START, "batch_dispatch")
builder.add_edge("batch_dispatch", "process_single_candidate")
builder.add_edge("process_single_candidate", "aggregate")
builder.add_edge("aggregate", END)
```

---

## 四、LangSmith 集成

### 4.1 配置方式

**环境变量：**
```bash
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=your-api-key
LANGCHAIN_PROJECT=recruitment-workflow
LANGCHAIN_ENDPOINT=https://api.smith.langchain.com
```

**代码配置：**
```python
import os

os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_API_KEY"] = settings.LANGSMITH_API_KEY
os.environ["LANGCHAIN_PROJECT"] = "recruitment-workflow"
```

### 4.2 追踪内容

| 追踪项 | 说明 |
|--------|------|
| 每个节点的输入/输出 | 状态变化可追溯 |
| LLM 调用详情 | prompt、completion、token 用量 |
| 工具调用 | 参数、返回值、耗时 |
| 中断点 | 人工审批的时间和决策 |
| 错误信息 | 异常堆栈和上下文 |

### 4.3 自定义 Run 名称

```python
from langchain_core.tracers import LangChainTracer

# 为每次调用设置可识别的 run name
config = {
    "configurable": {
        "thread_id": f"candidate_{candidate.id}",
    },
    "metadata": {
        "candidate_name": candidate.name,
        "position_title": position.title,
        "interviewer": interviewer.username,
    }
}

result = await app.ainvoke(input_data, config=config)
```

### 4.4 监控面板

在 LangSmith 中可以：
- 查看每个候选人的完整处理链路
- 对比不同候选人的处理耗时
- 分析 LLM 调用的 token 成本
- 定位失败节点的错误原因

---

## 五、实施顺序

| 阶段 | 内容 | 预计工作量 |
|------|------|-----------|
| **Phase 1** | 重构为 StateGraph | 2-3 天 |
| **Phase 2** | 加入 Interrupt 审批 | 1-2 天 |
| **Phase 3** | 批量并行处理 | 1 天 |
| **Phase 4** | LangSmith 集成 | 0.5 天 |

---

## 六、注意事项

1. **向后兼容**：重构期间保留旧的 `CandidateProcessAgent`，通过配置开关切换
2. **数据库迁移**：`CandidateRecruitmentState` 的字段变更需要迁移
3. **测试覆盖**：每个节点单独测试，再集成测试完整流程
4. **灰度发布**：先在小批量候选人上验证，再全量切换
