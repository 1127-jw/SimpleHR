# LLM 调用方式改造：从 Agent 循环到直接结构化调用

## 一、改造背景

### 1.1 原实现

`scoring_node` 和 `check_availability_node` 两个图节点使用 LangGraph 的 `create_agent` 构建 Agent：

```python
# 原实现
agent = create_agent(
    model=deepseek_llm,
    system_prompt=...,
    middleware=[ModelFallbackMiddleware(first_model=qwen_llm)],
    tools=[...],
    response_format=Schema,
)
response = await agent.ainvoke({"messages": [...]})
result = response["structured_response"]
```

### 1.2 存在的问题

| 问题 | 说明 |
|------|------|
| **Agent 循环开销** | 这两个节点实际只做一次 LLM 调用，Agent 框架引入的多轮循环、工具调度都是多余开销 |
| **工具往返** | `check_availability_node` 为获取当前时间调用 `get_current_time` 工具，产生 2 次 LLM 往返 |
| **Token 浪费** | Agent 框架的 system prompt、工具描述、中间推理都消耗 token |
| **调试困难** | Agent 循环链路长，出问题难以定位 |

---

## 二、改造方案

### 2.1 核心思路

```
create_agent（Agent 循环）
    ↓
llm.with_structured_output(Schema)（单次结构化调用）
    ↓
RunnableWithFallbacks（保留模型降级能力）
```

### 2.2 时间上下文处理

`check_availability_node` 原本通过工具 `get_current_time` 获取当前时间，改造后：

```
原：LLM → 调用工具 → 拿到时间 → LLM 继续推理 → 输出
现：生成时间文本 → 注入 System Prompt → LLM 一次推理 → 输出
```

---

## 三、改动清单

### 3.1 `app/workflow/candidate_nodes/scoring_node.py`

| 项目 | 改动 |
|------|------|
| 删除 | `create_agent`、`ModelFallbackMiddleware`、`ChatPromptTemplate`、`_get_score_agent()` |
| 新增 | `_build_score_llm()`、`_build_score_messages()` |
| 引入 | `RunnableWithFallbacks`、`SystemMessage`、`HumanMessage` |

**核心代码：**

```python
def _build_score_llm() -> RunnableWithFallbacks:
    primary = deepseek_llm.with_structured_output(AgentCandidateScoreSchema)
    fallback = qwen_llm.with_structured_output(AgentCandidateScoreSchema)
    return RunnableWithFallbacks(runnable=primary, fallbacks=[fallback])


def _build_score_messages(candidate, position) -> list:
    user_content = SCORE_FOR_CANDIDATE_USER_PROMPT.format(
        position=position.model_dump_json(),
        candidate=candidate.model_dump_json(),
    )
    return [
        SystemMessage(content=SCORE_FOR_CANDIDATE_SYSTEM_PROMPT),
        HumanMessage(content=user_content),
    ]
```

### 3.2 `app/workflow/candidate_nodes/check_availability_node.py`

| 项目 | 改动 |
|------|------|
| 删除 | `create_agent`、`ModelFallbackMiddleware`、`get_current_time` 工具、`_get_proposed_time_agent()` |
| 新增 | `_build_current_time_text()`、`_build_time_llm()` |
| 改造 | `_extract_time_from_reply()` 注入时间到 Prompt |

**核心代码：**

```python
def _build_current_time_text() -> str:
    now_bj = now_beijing()
    weekday_cn = {...}[now_bj.weekday()]
    return (
        f"{now_bj.year}年{now_bj.month}月{now_bj.day}日 "
        f"{now_bj.hour:02d}:{now_bj.minute:02d}:{now_bj.second:02d} "
        f"{weekday_cn}（本月第{now_bj.day}天）"
    )


async def _extract_time_from_reply(state, reply) -> ProposedTime:
    current_time_text = _build_current_time_text()
    system_prompt = (
        f"{PROPOSED_TIME_SYSTEM_PROMPT}\n\n"
        f"【当前时间】{current_time_text}\n"
        f"（请直接以上述当前时间为基准推算相对日期，无需调用任何工具）"
    )
    llm = _build_time_llm()
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"候选人邮件回复内容：\n{reply}"),
    ]
    return await llm.ainvoke(messages)
```

### 3.3 `app/agents/prompts.py`

| 位置 | 改动 |
|------|------|
| §3.3 日期推断 | "请先调用 current_time 工具" → "以 System Prompt 中提供的【当前时间】为基准" |
| 注意事项 §2 | "以 current_time 工具返回为基准" → "以【当前时间】为基准" |
| 注意事项 §6 | 新增 "无需调用任何工具" |

---

## 四、模型降级机制

### 4.1 实现

```python
primary = deepseek_llm.with_structured_output(Schema)
fallback = qwen_llm.with_structured_output(Schema)
llm = RunnableWithFallbacks(runnable=primary, fallbacks=[fallback])
```

### 4.2 降级流程

```
deepseek（主）
    │
    ├── 成功 → 返回结构化结果 ✅
    │
    └── 抛出异常 → qwen（备）
                    │
                    ├── 成功 → 返回结构化结果 ✅
                    └── 失败 → 抛出异常 ❌
```

### 4.3 触发条件

`RunnableWithFallbacks` 默认对**任何异常**降级：
- 网络超时 / 连接失败
- API 限流（429）
- 服务不可用（5xx）
- 结构化输出解析失败

### 4.4 与原 `ModelFallbackMiddleware` 对比

| 维度 | 原方案 | 现方案 |
|------|--------|--------|
| 触发条件 | 特定错误类型 | 任何异常 |
| 覆盖范围 | 较窄 | 更全 |
| 配置方式 | `first_model=qwen_llm` | `fallbacks=[qwen_llm...]` |

---

## 五、改动前后对比

### 5.1 `scoring_node`

| 指标 | 改造前 | 改造后 |
|------|--------|--------|
| LLM 往返次数 | 1 次（无工具） | 1 次 |
| 框架开销 | Agent 循环 | 直接调用 |
| 代码行数 | ~116 行 | ~100 行 |

### 5.2 `check_availability_node`

| 指标 | 改造前 | 改造后 |
|------|--------|--------|
| LLM 往返次数 | 2 次（工具 + 推理） | 1 次 |
| 工具调用 | `get_current_time` | 无（Prompt 注入） |
| 代码复杂度 | Agent + Tool + 缓存 | 直接调用 |

---

## 六、测试验证

```bash
python -m pytest tests/ -v
```

**结果：** 74 passed

覆盖：
- `test_graph_routes.py`（22 个图路由测试）
- `test_auth_tokens.py`、`test_change_password.py` 等

---

## 七、收益评估

| 维度 | 收益 |
|------|------|
| **延迟** | `check_availability` 减少 1 次 LLM 往返（约 1-3 秒） |
| **Token** | 减少 Agent 框架/工具描述开销 |
| **可靠性** | 保留 deepseek → qwen 降级链 |
| **可维护性** | 节点逻辑更直观，无 Agent 循环 |
| **可观测性** | 日志更清晰（直接看到 LLM 输入输出） |

---

## 八、风险与后续优化

| 风险 | 应对 |
|------|------|
| fallback 对业务性解析错误也降级 | 可指定 `exceptions_to_handle` 只对网络类错误降级 |
| 降级过程无日志 | 可包装 Runnable 记录主/备切换 |
| qwen 结构化输出兼容性 | 已验证（项目原 Agent 已用同模型） |

**后续可优化：**
1. 为 `RunnableWithFallbacks` 增加降级日志
2. 细化 `exceptions_to_handle`，只对基础设施异常降级
3. 将 `_build_*_llm()` 提取为公共工厂（两个节点复用）

---

## 九、涉及文件

```
app/workflow/candidate_nodes/
├── scoring_node.py            # 改造
└── check_availability_node.py # 改造

app/agents/
└── prompts.py                 # Prompt 调整
```

**不影响：**
- 图结构（`candidate_graph.py`）
- 状态定义（`candidate_state.py`）
- 路由逻辑
- 前端 / API / 数据库
