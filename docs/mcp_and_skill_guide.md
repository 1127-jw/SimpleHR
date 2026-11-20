# MCP + Skill 最小实现指南

> **定位**：在 SimpleJob（FastAPI + LangGraph）中接入 MCP 与 Agent Skill 的最小落地方案。
> **性质**：设计参考，不含对现有代码的改动。

---

## 一、概念辨析

| 概念 | 本质 | 载体 | 谁执行 |
|------|------|------|--------|
| **MCP Tool** | 运行时可调用函数 | Server 进程 + 协议 | 程序调用 |
| **MCP Resource** | 可读取数据 | Server 进程 + 协议 | 程序读取 |
| **MCP Prompt** | 可复用提示模板 | Server 进程 + 协议 | 程序注入 |
| **Skill** | 模型阅读的知识/流程 | 目录 + Markdown | 模型推理 |

**一句话：**
- **MCP 是「手」**——标准化地调用外部能力
- **Skill 是「说明书」**——告诉模型什么时候、怎么组合这些能力

---

## 二、MCP 最小实现（对外暴露业务能力）

### 2.1 依赖

```bash
uv add mcp
```

### 2.2 目录

```
app/mcp/
├── __init__.py
└── server.py          # MCP Server + tools
```

### 2.3 `app/mcp/server.py`

```python
"""MCP Server：把项目业务能力暴露为标准化工具。"""
from mcp.server.fastmcp import FastMCP

from app.core.db import AsyncSessionFactory
from app.repository.resume_repo import CandidateRepo, PositionRepo

mcp = FastMCP("simplejob")


@mcp.tool()
async def get_candidate(candidate_id: str) -> dict:
    """按 ID 查询候选人信息。"""
    async with AsyncSessionFactory() as session:
        c = await CandidateRepo(session).get_by_id(candidate_id)
        if not c:
            return {"error": "候选人不存在"}
        return {
            "id": c.id,
            "name": c.name,
            "email": c.email,
            "status": c.status.value,
        }


@mcp.tool()
async def list_positions(page: int = 1, size: int = 10) -> dict:
    """查询职位列表。"""
    async with AsyncSessionFactory() as session:
        positions, total = await PositionRepo(session).get_list(page, size)
        return {
            "total": total,
            "items": [{"id": p.id, "title": p.title} for p in positions],
        }


@mcp.resource("system://health")
def health() -> str:
    """服务健康检查。"""
    return "ok"
```

### 2.4 挂载到 FastAPI（`app/main.py`）

```python
from app.mcp.server import mcp

# SSE 传输：客户端连 /mcp/sse
app.mount("/mcp", mcp.sse_app())
```

### 2.5 验证

```bash
# 方式 1：MCP 客户端（Claude Desktop / Cursor）
#   配置 url: http://127.0.0.1:8000/mcp/sse

# 方式 2：命令行
uvx mcp-cli connect http://127.0.0.1:8000/mcp/sse
```

---

## 三、LangGraph 如何调用 MCP

三种方式，按场景选择。

### 3.1 方式 A：`langchain-mcp-adapters` + `ToolNode`（标准）

```bash
uv add langchain-mcp-adapters
```

```python
# app/workflow/mcp_tools.py
from langchain_mcp_adapters.client import MultiServerMCPClient

_client: MultiServerMCPClient | None = None


async def get_mcp_tools():
    """加载 MCP Server 工具，转成 LangChain tools。"""
    global _client
    if _client is None:
        _client = MultiServerMCPClient({
            "simplejob": {
                "url": "http://127.0.0.1:8000/mcp/sse",
                "transport": "sse",
            }
        })
    return await _client.get_tools()
```

```python
# 图中使用
from langgraph.prebuilt import ToolNode

tools = await get_mcp_tools()
graph.add_node("mcp_tools", ToolNode(tools))
graph.add_conditional_edges("agent", should_continue,
                            {"mcp_tools": "mcp_tools", END: END})
graph.add_edge("mcp_tools", "agent")
```

**要点：**
- `MultiServerMCPClient` 是长连接，**进程级复用**
- `ToolNode` 是 LangGraph 预构建的工具执行节点
- 工具由 LLM 的 `tool_calls` 驱动

### 3.2 方式 B：节点内直接调用 MCP（最轻量）

```python
# app/workflow/candidate_nodes/query_node.py
from mcp import ClientSession
from mcp.client.sse import sse_client

MCP_URL = "http://127.0.0.1:8000/mcp/sse"


async def query_mcp_tool(tool_name: str, arguments: dict) -> dict:
    async with sse_client(url=MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            return result.content


@node_error_wrapper
async def get_candidate_info_node(state: CandidateRecruitmentState) -> dict:
    result = await query_mcp_tool(
        "get_candidate", {"candidate_id": state["candidate"].id}
    )
    return {"mcp_result": result}
```

**优点：** 不引入 Agent 循环，直接可控。
**缺点：** 每次调用建连接（可优化为长连接）。

### 3.3 方式 C：包装成 LangChain tool（复用直连模式）

```python
# app/agents/tools.py
from langchain_core.tools import tool
from app.workflow.mcp_client import query_mcp_tool


@tool
async def get_candidate_tool(candidate_id: str) -> dict:
    """按 ID 查询候选人（走 MCP）。"""
    return await query_mcp_tool("get_candidate", {"candidate_id": candidate_id})
```

节点里 `llm.bind_tools([get_candidate_tool])`。

### 3.4 选择建议

| 场景 | 推荐 |
|------|------|
| 让**外部 Agent** 调用本项目 | 只加 MCP Server（§二） |
| 让**本项目调用外部 MCP** | 方式 A（标准）/ 方式 B（轻量） |
| 让**图的某个节点**用 MCP | 方式 B |
| 已有 `with_structured_output` 直连，想最小改动 | 方式 C |

---

## 四、Skill 最小实现

### 4.1 Skill 是什么

**Claude Agent Skills** 规范：一个可分发目录，含 `SKILL.md`（说明书）+ 可选资源。
模型读完就知道**何时、如何**完成某类任务。

### 4.2 目录结构

```
skills/
└── candidate-progress/
    ├── SKILL.md              # 技能说明（必须）
    ├── scripts/              # 可选：可执行脚本
    │   └── format_report.py
    ├── references/           # 可选：参考文档
    └── assets/               # 可选：模板/素材
```

### 4.3 `SKILL.md` 示例

```markdown
---
name: candidate-progress
description: 查询并总结候选人的招聘进度（状态、AI 评分、面试安排）
---

# 候选人进度查询

## 触发场景
用户问「张三到哪一步了」「这个候选人怎么样」时启用。

## 执行步骤
1. 用 `get_candidate` 工具拿基本信息（id / 姓名 / 状态）
2. 用 `get_ai_score` 工具拿评分
3. 按下方模板输出

## 输出模板
- 姓名 / 邮箱
- 当前状态
- AI 评分：总分 + 各维度
- 下一步建议
```

### 4.4 `scripts/format_report.py` 示例

```python
"""把候选人数据格式化成报告。"""
def format(candidate: dict, score: dict) -> str:
    return f"{candidate['name']} | {candidate['status']} | 评分 {score['overall']}"
```

---

## 五、在 LangGraph / 本项目中落地 Skill

### 5.1 方式 A：作为 Prompt 知识注入（最轻量）

```python
from pathlib import Path

SKILL_PROMPT = Path("skills/candidate-progress/SKILL.md").read_text(encoding="utf-8")
system_prompt = f"{BASE_PROMPT}\n\n# 领域技能\n{SKILL_PROMPT}"
```

**适合：** 技能主要是「指导模型怎么想」。

### 5.2 方式 B：scripts 包成 Tool / MCP

```python
from langchain_core.tools import tool
from skills.candidate_progress.scripts.format_report import format

@tool
def format_candidate_report(candidate_id: str) -> str:
    """把候选人信息格式化成进度报告。"""
    ...
```

**适合：** 技能含明确计算/处理逻辑。

### 5.3 方式 C：仅作为开发期文档

放在 `docs/skills/` 供团队参考，运行时不用。

---

## 六、推荐组合

```
app/mcp/server.py              # MCP：暴露工具（手）
skills/candidate-progress/
├── SKILL.md                   # Skill：流程说明（说明书）
└── scripts/format_report.py   # 可执行逻辑 → 包成 tool
```

| 能力 | 载体 |
|------|------|
| 查候选人 / 查职位 | MCP Tool |
| 如何组合查询、输出格式 | Skill（SKILL.md） |
| 报告格式化逻辑 | Skill script → LangChain tool |

---

## 七、注意事项

| 问题 | 建议 |
|------|------|
| MCP 连接生命周期 | `MultiServerMCPClient` 进程级单例，不要每次新建 |
| MCP 鉴权 | 需要 token 时在 `headers` 传 |
| MCP 超时 | `call_tool` 加 `asyncio.wait_for` |
| MCP 错误处理 | 走 `@node_error_wrapper` → error 节点 |
| Skill 加载时机 | 按需读取，避免一次性全量注入（省 token） |
| 写操作暴露 | 建人/起图等写操作谨慎暴露，只读优先 |

---

## 八、总结

| 你想要的 | 加什么 |
|----------|--------|
| 让外部 Agent **调用**你的能力 | **MCP Server**（tool） |
| 让模型**学会**一套复杂流程 | **Skill**（SKILL.md） |
| 两者都要 | MCP 暴露工具 + Skill 描述如何组合使用 |

**最小落地：**
- MCP：`app/mcp/server.py` + `app.mount("/mcp", mcp.sse_app())`
- Skill：`skills/xxx/SKILL.md`

**依赖：**
```bash
uv add mcp                  # MCP Server
uv add langchain-mcp-adapters  # LangGraph 调用 MCP（可选）
```
