# notify 意图识别改造方案（含参考实现代码）

> 状态：**设计稿，尚未落地**。本文档中的代码是目标实现的参考示例，用于评审与后续实施，**当前代码库未做任何改动**。
>
> 涉及现状文件：`utils/emailbot/notify.py`、`utils/emailbot/rules.py`、`utils/emailbot/bot.py`、`utils/emailbot/mail_manager.py`、`app/core/config.py`、`app/workflow/candidate_nodes/check_availability_node.py`、`app/agents/prompts.py`。

---

## 一、现状与核心问题

当前 `notify` 的"意图识别"本质是**规则名路由**：

- `utils/emailbot/notify.py#L45-L47`：`rule.name == "简历投递"` → 空 `pass`（TODO）。
- `utils/emailbot/notify.py#L49-L81`：`rule.name == "询问面试时间或进度"` → 恢复图 / Agent。
- `utils/emailbot/notify.py#L84-L87`：其余全部落到兜底 `_handle_candidate_reply`。

意图真值来自 `utils/emailbot/rules.py#L63-L94` 的关键词**子串匹配**（`utils/emailbot/rules.py#L57`）。

### 问题清单

| # | 问题 | 证据 |
|---|---|---|
| 1 | 泛词误报极严重：`确认/好的/可以/方便/收到/明白/上午/下午/晚上/点/分` 是单字或高频词，几乎命中任何邮件 | `utils/emailbot/rules.py#L78-L82` |
| 2 | 兜底分支对所有邮件生效：`catch_all` 保证 `matched` 永不为 None，任何邮件只要发件邮箱能查到候选人且状态为 `AI筛选成功/人工通过`，就会 `Command(resume=...)` 恢复图 | `utils/emailbot/bot.py#L181-L198`、`utils/emailbot/notify.py#L90-L130` |
| 3 | 主分支不校验状态：`询问面试时间` 分支只校验"候选人存在"，不校验是否在协商中、thread 是否存在 | `utils/emailbot/notify.py#L52-L81` |
| 4 | 意图与规则名强耦合：无法表达复合意图；规则顺序决定优先级 | `utils/emailbot/bot.py#L168-L175` |
| 5 | 自动回复文案与意图不符：确认面试的邮件也收到"您的应聘状态正在评估中" | `utils/emailbot/rules.py#L84` |
| 6 | 身份维度缺失：不区分候选人 / 面试官 / HR / 系统通知 | `utils/emailbot/notify.py#L22-L34` |
| 7 | 线程归属脆弱：`thread_id` 直接用发件邮箱地址 | `utils/emailbot/notify.py#L65`、`#L80`、`#L129` |
| 8 | 无关邮件照样喂给图，与系统提示词"无关邮件不予理会"矛盾 | `app/agents/prompts.py#L76` |
| 9 | `GRAPH_OPEN` 双轨，意图逻辑要写两遍，易漂移 | `utils/emailbot/notify.py#L56-L81`、`app/core/config.py#L110` |
| 10 | 无置信度、无人工兜底、无评测、无埋点 | 整体缺失 |

---

## 二、改造目标

1. 意图识别从"关键词规则名"升级为**五层管道**：技术过滤 → 身份识别 → 意图分类 → 上下文校验 → 动作分发。
2. 意图与动作解耦；规则降为**先验特征与兜底**，不再是意图真值。
3. 恢复图前必须通过**状态 + checkpoint 存在性 + 中断点 + 幂等**四道校验。
4. 自动回复按"意图 × 阶段 × 槽位"决定，取消一刀切固定文案。
5. 全流程可观测、可灰度、可回退、可离线评测。

---

## 三、总体架构

```
邮件进入
  │
  ├─ L0 技术过滤（无 LLM）
  │    自己发的信 / 自动回复·退信·系统通知 / Message-ID 去重 / 邮件类型识别
  │
  ├─ L1 身份识别
  │    发件人 → 候选人 / 面试官 / HR / 外部未知
  │    归属：In-Reply-To / References 匹配我方邀请邮件的 Message-ID
  │
  ├─ L2 意图分类（LLM 结构化输出，规则兜底）
  │    主意图 + 次意图 + 槽位 + 置信度 + 判定依据
  │
  ├─ L3 上下文校验
  │    候选人状态 ∈ 允许集合 ？ thread_id 有 checkpoint ？ 图停在等待回复中断点 ？ 幂等 ？
  │
  ├─ L4 动作分发
  │    恢复图 / 起新流程 / 自动回复 / 通知 HR / 人工复核 / 忽略
  │
  └─ L5 回复策略
        按 (意图, 阶段, 槽位) 选模板；高风险动作不自动回
```

---

## 四、意图体系设计

### 4.1 邮件级意图枚举

| 意图 | 含义 | 典型示例 | 默认动作 |
|---|---|---|---|
| `RESUME_SUBMISSION` | 新简历投递 | "附上我的简历，应聘 Java 工程师" | 落待确认 → 人工确认 → 建候选人 → 起图 |
| `INTERVIEW_REPLY` | 面试协商回复 | "确认""明天下午 3 点可以""我换个时间""不去了" | 恢复图，交由 `check_availability` |
| `PROGRESS_INQUIRY` | 询问进度 / 结果 | "有结果了吗""什么时候面试" | 自动回进度模板，同时通知 HR |
| `INTERVIEWER_MAIL` | 面试官反馈 / 改期请求 | "这个候选人我有冲突，换天吧" | 落库 + 通知 HR，不自动回 |
| `AUTO_REPLY` | 自动回复 / OOO / 退信 / 系统通知 | "外出办公，稍后回复" | 直接忽略 |
| `OUT_OF_SCOPE` | 无关邮件 | 营销、错发、骚扰 | 忽略（可选记录） |
| `AMBIGUOUS` | 低置信 / 信息不足 | "好的" | 人工复核，不 resume、不自动回 |

### 4.2 槽位（复用下游已有能力）

下游已有结构化抽取 `ProposedTime`（`confirm` / `refused` / `proposed_time` / `exactly`）与提示词 `PROPOSED_TIME_SYSTEM_PROMPT`：

- `app/workflow/candidate_nodes/check_availability_node.py#L31-L44`
- `app/agents/prompts.py#L135-L213`

**改造原则**：把该 schema 提升为公共模块，notify 的 L2 与图内 `check_availability` 共用，避免两处提示词漂移。L2 只做"邮件级分流 + 粗槽位"，精细时间解析仍由图内节点负责（依赖 `get_current_time` 工具，见 `app/workflow/candidate_nodes/check_availability_node.py#L47-L70`）。

### 4.3 分类特征（关键）

仅凭正文，"好的"无法判定意图。必须把上下文一并作为输入：

- 发件人身份（L1 结果）
- 候选人当前状态（`app/models/candidate.py#L17-L42`）
- **是否处于"等待回复"阶段**（checkpoint 是否停在 `get_candidate_email_reply_node`）
- 最近一次我方发出邮件的主题 / 摘要
- 是否携带简历附件

> **只要"不在等待回复状态"，"好的 / 收到 / 可以"这类简短回复就必须判为 `OUT_OF_SCOPE` 或 `AMBIGUOUS`。** 这是解决泛词误报的根本手段。

---

## 五、参考实现代码

> 以下为设计稿代码，未写入代码库。

### 5.0 调用方式约定：直连模型，不使用 Agent

意图识别是**单次、无工具、结构化输出**的分类任务，不需要 Agent 的 ReAct 循环，因此不采用 `create_agent`，而是复用图内节点的调用方式：

| 项 | 说明 |
|---|---|
| 调用形式 | `llm.with_structured_output(MailIntentResult).ainvoke([SystemMessage, HumanMessage])` |
| 与图内一致 | 直接对模型 `ainvoke`，不走 Agent 包装（图内节点同样是"构造消息 → `ainvoke` → 取结构化结果"这一调用方式） |
| 降级 | 主模型 `qwen_llm` 失败 → 备用模型 `deepseek_llm`（语义等价于图内的 `ModelFallbackMiddleware`，但由我们自己控制重试节奏） |
| 兜底 | 两个模型都失败 → 回落纯规则引擎 `_rule_classify` |
| 超时 | 通过 `INTENT_LLM_TIMEOUT_SECONDS` 控制，超时视同失败降级 |

> 好处：省去 Agent 的提示词组装、工具循环与额外 token 成本；分类结果稳定、可缓存、易评测。

### 5.1 新增 `utils/emailbot/intent.py`

```python
"""意图识别层：把「规则名路由」升级为「分层意图管道」。

五层：
L0 确定性预过滤（rules.py，无 LLM）→ L1 身份识别 → L2 意图分类（LLM + 规则兜底）
→ L3 上下文校验 → 产出 MailIntentEvent 交给 notify 分发。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from enum import Enum
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agents.llms import deepseek_llm, qwen_llm
from app.core.config import get_settings
from utils.emailbot.models import ParsedEmail
from utils.emailbot.rules import DEFAULT_RULES, MailRule, RuleAction

logger = logging.getLogger(__name__)
settings = get_settings()


# ==================== 枚举 ====================

class MailIntent(str, Enum):
    RESUME_SUBMISSION = "RESUME_SUBMISSION"   # 新简历投递
    INTERVIEW_REPLY = "INTERVIEW_REPLY"       # 面试协商回复
    PROGRESS_INQUIRY = "PROGRESS_INQUIRY"     # 询问进度/结果
    INTERVIEWER_MAIL = "INTERVIEWER_MAIL"     # 面试官邮件
    AUTO_REPLY = "AUTO_REPLY"                 # 自动回复/OOO/退信/系统通知
    OUT_OF_SCOPE = "OUT_OF_SCOPE"             # 无关邮件
    AMBIGUOUS = "AMBIGUOUS"                   # 低置信，转人工


class SenderRole(str, Enum):
    CANDIDATE = "CANDIDATE"
    INTERVIEWER = "INTERVIEWER"
    HR = "HR"
    SYSTEM = "SYSTEM"
    UNKNOWN = "UNKNOWN"


# ==================== 结构化输出 ====================

class IntentSlots(BaseModel):
    """粗槽位：精细时间解析仍由图内 check_availability 负责。"""
    proposed_time: str = Field(default="", description="候选人期望时间，YYYY-MM-DD HH:MM:SS")
    exactly: str = Field(default="", description="hours / am / pm / all / 空")
    refuse_reason: str = Field(default="", description="拒绝面试的原因")
    position_ref: str = Field(default="", description="提及的职位名，可为空")


class MailIntentResult(BaseModel):
    intent: MailIntent
    sub_intent: str = Field(default="", description="细分，如 confirm / reschedule / refuse")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    slots: IntentSlots = Field(default_factory=IntentSlots)
    evidence: str = Field(default="", description="判定依据原文片段，便于人工复核")
    reason: str = Field(default="", description="分类理由")
    needs_review: bool = Field(default=False, description="低置信或信息不足，需人工复核")


# ==================== 提示词 ====================

INTENT_SYSTEM_PROMPT = """你是招聘邮箱的意图分类器。根据【发件人身份】和【流程上下文】判断一封邮件的意图。

# 意图定义
- RESUME_SUBMISSION：投递新简历（通常带简历附件，或正文明确求职/应聘）
- INTERVIEW_REPLY：对面试邀约的回复（确认时间、改期、拒绝、询问时间）
- PROGRESS_INQUIRY：询问招聘进度、结果、后续安排
- INTERVIEWER_MAIL：面试官（非候选人）发来的反馈、改期请求等
- AUTO_REPLY：自动回复、外出提醒、退信、系统通知、订阅邮件
- OUT_OF_SCOPE：与招聘流程无关（营销、错发、骚扰）
- AMBIGUOUS：信息不足或低置信，无法归入以上任何一类

# 硬规则
1. 若「是否在等待候选人回复」为 false，则简短回复（如"好的""收到""可以"）不得判为 INTERVIEW_REPLY，
   应判为 OUT_OF_SCOPE 或 AMBIGUOUS。
2. 判定 INTERVIEW_REPLY 必须能对应到具体的面试协商语义，不能仅凭"确认/好的"这类泛词。
3.  AUTO_REPLY 优先级最高：命中自动回复特征时直接判定，不再看其他意图。
4. 无法确定时判 AMBIGUOUS 并把 needs_review 设为 true，不要猜。

# 输出
严格输出 JSON：intent、sub_intent、confidence(0-1)、slots、evidence、reason、needs_review。
"""


INTENT_USER_PROMPT = """【发件人身份】{role}
【候选人状态】{candidate_status}
【是否在等待候选人回复】{waiting_reply}
【最近一次我方邮件】{last_outbound_subject}
【是否带附件】{has_attachment}

【邮件主题】{subject}

【邮件正文】
{body}
"""


# ==================== 分类器 ====================

# 直连模型（非 Agent）：意图分类不需要工具循环，用结构化输出一次调用即可
_intent_llm = None


def _get_intent_llm():
    """懒加载主模型；失败时由 _classify_with_llm 自动降级到备用模型。"""
    global _intent_llm
    if _intent_llm is None:
        _intent_llm = qwen_llm.with_structured_output(MailIntentResult)
    return _intent_llm


def _build_intent_messages(prompt: str) -> list:
    """与图内节点一致的调用方式：SystemMessage + UserMessage 直接 ainvoke。"""
    return [
        SystemMessage(content=INTENT_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]


async def _classify_with_llm(prompt: str) -> MailIntentResult:
    """直接调用大模型做结构化分类。

    主模型失败降级备用模型，语义对齐图内 `ModelFallbackMiddleware`：
    不引入 Agent / 工具循环，只做一次带结构化输出的模型调用。
    """
    last_exc: Optional[Exception] = None
    runnables = (
        ("qwen", _get_intent_llm()),
        ("deepseek", deepseek_llm.with_structured_output(MailIntentResult)),
    )
    for name, runnable in runnables:
        try:
            result = await asyncio.wait_for(
                runnable.ainvoke(_build_intent_messages(prompt)),
                timeout=settings.INTENT_LLM_TIMEOUT_SECONDS,
            )
            logger.debug("[intent] 分类模型=%s | result=%s", name, result)
            return result
        except Exception as exc:  # noqa: BLE001 - 逐个模型降级，最后统一抛
            last_exc = exc
            logger.warning("[intent] 模型 %s 分类失败，尝试下一个 | error=%s", name, exc)
    assert last_exc is not None
    raise last_exc


# 进程内缓存：相同邮件内容只分类一次（重复轮询 / 重放时零成本）
_cache: dict[str, MailIntentResult] = {}


def _cache_key(email: ParsedEmail) -> str:
    raw = f"{email.message_id}|{email.subject}|{email.text[:2000]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def classify_intent(
    email: ParsedEmail,
    *,
    role: SenderRole = SenderRole.UNKNOWN,
    candidate_status: str = "",
    waiting_reply: bool = False,
    last_outbound_subject: str = "",
    has_attachment: bool = False,
    rules: list[MailRule] | None = None,
) -> MailIntentResult:
    """LLM 意图分类；失败或低置信时回落规则兜底。"""
    key = _cache_key(email)
    if key in _cache:
        return _cache[key]

    prompt = INTENT_USER_PROMPT.format(
        role=role.value,
        candidate_status=candidate_status or "未知",
        waiting_reply="是" if waiting_reply else "否",
        last_outbound_subject=last_outbound_subject or "无",
        has_attachment="是" if has_attachment else "否",
        subject=email.subject,
        body=email.text[: settings.INTENT_MAX_MAIL_CHARS],
    )

    result: Optional[MailIntentResult] = None
    if settings.INTENT_ENGINE in ("llm", "hybrid"):
        try:
            result = await _classify_with_llm(prompt)
        except Exception:
            logger.exception("LLM 意图分类失败，回落规则引擎 | uid=%s", email.uid)

    if result is None:
        result = await _rule_classify(
            email, role=role, waiting_reply=waiting_reply, rules=rules
        )

    if result.confidence < settings.INTENT_CONFIDENCE_THRESHOLD:
        result.needs_review = True

    # 简单淘汰：超出上限直接清空（分类结果本就是短生命周期，无需 LRU）
    if len(_cache) >= settings.INTENT_CACHE_SIZE:
        _cache.clear()
    _cache[key] = result
    return result


# ==================== 规则兜底 ====================
# 关键词真相只在 rules.py 的 DEFAULT_RULES 一处，这里不重复维护关键词表，
# 直接复用 MailRule.matches()，避免两套词表漂移。

async def _rule_classify(
    email: ParsedEmail,
    *,
    role: SenderRole,
    waiting_reply: bool,
    rules: list[MailRule] | None = None,
) -> MailIntentResult:
    """纯规则兜底：仅在 LLM 不可用时使用。

    只认"强特征"：L0 拦截（自动回复/退信/营销）、简历附件先验；
    其余一律返回 AMBIGUOUS 低置信，转人工，绝不猜。
    """
    rules = rules if rules is not None else DEFAULT_RULES

    # 1) L0 拦截规则命中：自动回复 / 退信 / 营销 / 黑名单 → 直接忽略
    if any(r.action is RuleAction.BLOCK and _match(r, email) for r in rules):
        return MailIntentResult(
            intent=MailIntent.OUT_OF_SCOPE, confidence=0.9,
            reason="命中 L0 拦截规则（BLOCK）",
        )

    # 2) 发件人是面试官
    if role is SenderRole.INTERVIEWER:
        return MailIntentResult(
            intent=MailIntent.INTERVIEWER_MAIL, confidence=0.8,
            reason="发件人为面试官",
        )

    # 3) 命中带先验标签的规则：给低置信结果，等待人工/LLM 复核
    for r in rules:
        if r.action is not RuleAction.PRIOR or not _match(r, email):
            continue
        try:
            intent = MailIntent(r.prior_intent) if r.prior_intent else MailIntent.AMBIGUOUS
        except ValueError:
            intent = MailIntent.AMBIGUOUS
        return MailIntentResult(
            intent=intent, confidence=0.6,
            reason=f"命中规则 {r.name}，仅作先验，置信度低",
        )

    return MailIntentResult(
        intent=MailIntent.AMBIGUOUS, confidence=0.3, needs_review=True,
        reason="规则未命中任何强特征，转人工复核",
    )


def _match(rule: MailRule, email: ParsedEmail) -> bool:
    """薄封装：把 ParsedEmail 摊平后交给 MailRule.matches()。"""
    return rule.matches(
        subject=email.subject,
        sender=f"{email.from_.name} {email.from_.address}",
        body=email.text,
        attachment_names=[a.filename for a in email.attachments],
    )
```

### 5.2 新增 `app/schemas/intent_schema.py`（公共 schema）

```python
"""意图/槽位公共 schema：notify 与图内 check_availability 共用，避免提示词漂移。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ProposedTime(BaseModel):
    """候选人回复的结构化抽取结果（原 check_availability_node 内的定义迁移而来）。"""
    confirm: bool = Field(description="True=同意上次邀约时间，False=希望改期")
    refused: bool = Field(description="True=拒绝/放弃本次面试，False=愿意继续")
    proposed_time: str = Field(description="面试时间，格式 YYYY-MM-DD HH:MM:SS")
    exactly: str = Field(description="hours / am / pm / all / 空")
```

### 5.3 重写 `utils/emailbot/notify.py`

```python
"""邮件命中后的业务分发：意图管道 + 动作分发。"""
from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from utils.emailbot.intent import (
    MailIntent,
    SenderRole,
    classify_intent,
)
from utils.emailbot.models import ParsedEmail
from utils.emailbot.rules import MailRule

from app.models.candidate import CandidateStatusEnum
from app.core.db import AsyncSessionFactory
from app.core.checkpointer import get_checkpointer
from app.core.config import get_settings
from app.repository.resume_repo import CandidateRepo
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from app.workflow.candidate_graph import build_candidate_graph

logger = logging.getLogger(__name__)
settings = get_settings()

# 允许恢复协商图的候选人状态
_RESUMABLE_STATUS = (
    CandidateStatusEnum.MANUAL_PASSED,
    CandidateStatusEnum.AI_FILTER_PASSED,
)


async def _find_candidate_by_email(email_address: str):
    """根据邮箱地址查找候选人（含职位与面试官）。"""
    async with AsyncSessionFactory() as session:
        repo = CandidateRepo(session)
        candidate = await repo.get_by_email(email_address)
        if not candidate:
            return None, None, None
        # 必须在 session 内访问关联对象并转 Schema，避免 DetachedInstanceError
        return (
            CandidateSchema.model_validate(candidate),
            PositionSchema.model_validate(candidate.position),
            UserSH.model_validate(candidate.creator),
        )


async def _waiting_reply(thread_id: str) -> bool:
    """校验该 thread 是否存在 checkpoint，且图确实停在等待回复中断点。"""
    checkpointer = get_checkpointer()
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await checkpointer.aget_tuple(config)
    except Exception:
        logger.exception("读取 checkpoint 失败 | thread_id=%s", thread_id)
        return False
    if snapshot is None:
        return False
    # next 非空表示还有待执行节点（即停在中断点，等待 resume）
    return bool(snapshot.next)


async def notify(email: ParsedEmail, rule: MailRule | None = None) -> None:
    """邮件命中后的回调：识别意图 → 校验 → 分发动作。

    Args:
        email: 已解析的邮件。
        rule: 命中的规则（仅作为先验特征，不再作为意图真值）。
    """
    # ---- L1 身份识别 ----
    candidate_schema, position_schema, interviewer_schema = await _find_candidate_by_email(
        email.from_.address
    )
    if candidate_schema:
        role = SenderRole.CANDIDATE
        candidate_status = candidate_schema.status
        thread_id = f"candidate_{candidate_schema.id}"
    else:
        role = SenderRole.UNKNOWN
        candidate_status = ""
        thread_id = f"email_{email.from_.address}"

    # ---- L3 前置校验：是否在等待回复 ----
    waiting_reply = await _waiting_reply(thread_id) if candidate_schema else False

    # ---- L2 意图分类 ----
    result = await classify_intent(
        email,
        role=role,
        candidate_status=candidate_status,
        waiting_reply=waiting_reply,
        has_attachment=bool(email.attachments),
    )
    logger.info(
        "[intent] uid=%s | intent=%s | sub=%s | conf=%.2f | review=%s | rule=%s | reason=%s",
        email.uid, result.intent.value, result.sub_intent, result.confidence,
        result.needs_review, getattr(rule, "name", None), result.reason,
    )

    # ---- L4 动作分发 ----
    if result.intent is MailIntent.AUTO_REPLY:
        return

    if result.needs_review or result.intent is MailIntent.AMBIGUOUS:
        await _enqueue_human_review(email, result)
        return

    if result.intent is MailIntent.INTERVIEW_REPLY:
        await _resume_candidate_graph(email, candidate_schema, position_schema,
                                      interviewer_schema, result)
        return

    if result.intent is MailIntent.PROGRESS_INQUIRY:
        await _reply_progress(email, candidate_schema)
        return

    if result.intent is MailIntent.RESUME_SUBMISSION:
        await _handle_resume_submission(email)
        return

    if result.intent is MailIntent.INTERVIEWER_MAIL:
        await _notify_hr(email, result)
        return

    # OUT_OF_SCOPE：忽略
    return


async def _resume_candidate_graph(
    email: ParsedEmail,
    candidate_schema: CandidateSchema | None,
    position_schema: PositionSchema | None,
    interviewer_schema: UserSH | None,
    result,
) -> None:
    """恢复招聘图：仅当候选人在协商中、且 checkpoint 停在等待回复中断点。"""
    if not candidate_schema:
        logger.warning("未找到候选人，不恢复图 | from=%s", email.from_.address)
        return
    if candidate_schema.status not in _RESUMABLE_STATUS:
        logger.info(
            "候选人不在协商中，不恢复图 | candidate_id=%s | status=%s",
            candidate_schema.id, candidate_schema.status,
        )
        return

    thread_id = f"candidate_{candidate_schema.id}"
    if not await _waiting_reply(thread_id):
        logger.info("无有效等待回复 checkpoint，不恢复图 | thread_id=%s", thread_id)
        return

    if not settings.GRAPH_OPEN:
        logger.info("GRAPH_OPEN=false，当前仅记录不执行 Agent 分支 | thread_id=%s", thread_id)
        return

    candidate_graph = build_candidate_graph(get_checkpointer())
    await candidate_graph.ainvoke(
        Command(
            resume=f"候选人回复了以下邮件，请根据回复内容决定下一步操作：\n\n"
                   f"主题：{email.subject}\n\n内容：{email.text}",
            update={
                "candidate": candidate_schema,
                "position": position_schema,
                "interviewer": interviewer_schema,
            },
        ),
        config={"configurable": {"thread_id": thread_id}},
    )


async def _reply_progress(email: ParsedEmail, candidate_schema) -> None:
    """进度询问：自动回模板 + 通知 HR（示例仅打日志）。"""
    logger.info("[progress_inquiry] 来自 %s | candidate_id=%s",
                email.from_.address, candidate_schema.id if candidate_schema else None)


async def _handle_resume_submission(email: ParsedEmail) -> None:
    """新简历投递：落「待确认」记录 + 通知 HR，**不自动建人、不起图**。

    确认环节见「九、RESUME_SUBMISSION 的待确认闭环」。
    """
    await create_mail_resume_submission(email)   # 建 PENDING 记录（含去重）
    await notify_hr_new_submission(email)        # 钉钉 / 前端未读


async def _notify_hr(email: ParsedEmail, result) -> None:
    """面试官邮件 / 转人工：落库并通知 HR（示例仅打日志）。"""
    logger.info("[notify_hr] 来自 %s | intent=%s | evidence=%s",
                email.from_.address, result.intent.value, result.evidence)


async def _enqueue_human_review(email: ParsedEmail, result) -> None:
    """低置信 / 歧义：不 resume、不自动回，进人工复核队列。"""
    logger.warning(
        "[human_review] uid=%s | intent=%s | conf=%.2f | reason=%s",
        email.uid, result.intent.value, result.confidence, result.reason,
    )
```

### 5.4 修改 `utils/emailbot/rules.py`（L0 确定性预过滤层）

改造后 `rules.py` **只判"要不要进 LLM"，不判"邮件是什么意思"**：

```python
"""规则层（L0 确定性预过滤）：只判「要不要进 LLM」，不判「邮件是什么意思」。

职责：
- BLOCK ：拦截，直接忽略（自动回复 / 退信 / 营销 / 黑名单）
- ALLOW ：放行，不附标签
- PRIOR ：放行 + 打先验意图标签（仅影响置信度，不改变动作）

意图真值唯一来源是 utils/emailbot/intent.py 的 LLM 分类器；
关键词表只在本文件维护，intent.py 通过 MailRule.matches() 复用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

MatchField = Literal["subject", "from", "body", "attachment"]


class RuleAction(str, Enum):
    BLOCK = "BLOCK"   # 拦截，不进意图层
    ALLOW = "ALLOW"   # 放行
    PRIOR = "PRIOR"   # 放行 + 打先验意图标签


@dataclass
class MailRule:
    name: str
    keywords: list[str] = field(default_factory=list)
    fields: list[MatchField] = field(default_factory=lambda: ["subject", "body"])
    action: RuleAction = RuleAction.ALLOW
    prior_intent: str | None = None        # 先验意图标签，仅用于提升/降低置信度
    exclude_keywords: list[str] = field(default_factory=list)
    case_sensitive: bool = False
    require_attachment: bool = False
    enabled: bool = True

    def matches(self, subject: str, sender: str, body: str,
                attachment_names: list[str]) -> bool:
        if not self.enabled or not self.keywords:
            return False
        # 排除词优先：命中即不算命中
        whole = f"{subject}\n{sender}\n{body}\n{' '.join(attachment_names)}"
        if self._contains(whole, self.exclude_keywords):
            return False
        if self.require_attachment and not attachment_names:
            return False
        haystack = {
            "subject": subject,
            "from": sender,
            "body": body,
            "attachment": " ".join(attachment_names),
        }
        for f in self.fields:
            text = haystack.get(f, "") or ""
            if not self.case_sensitive:
                text = text.lower()
                words = [k.lower() for k in self.keywords]
            else:
                words = self.keywords
            if any(w in text for w in words):
                return True
        return False

    def _contains(self, text: str, words: list[str]) -> bool:
        if not words:
            return False
        low = text.lower()
        return any(w.lower() in low for w in words)


# 改造后：只保留确定性强的规则，不再有 catch_all，也不再携带 reply_text
DEFAULT_RULES = [
    # L0 拦截：自动回复 / 退信 / 系统通知（无需语义理解）
    MailRule(
        name="自动回复与系统通知",
        keywords=[
            "自动回复", "automatic reply", "out of office", "退信",
            "投递失败", "undeliverable", "delivery status notification",
        ],
        action=RuleAction.BLOCK,
    ),
    # L0 拦截：明显营销
    MailRule(
        name="营销邮件",
        keywords=["退订", "unsubscribe", "限时优惠"],
        action=RuleAction.BLOCK,
    ),
    # 强特征：带简历附件的投递 → 先验标签（泛词"简历"不上线，靠附件 + 语义）
    MailRule(
        name="简历投递",
        keywords=["简历", "应聘", "求职", "投递", "resume", "cv"],
        fields=["subject", "body", "attachment"],
        require_attachment=True,
        action=RuleAction.PRIOR,
        prior_intent="RESUME_SUBMISSION",
    ),
    # 强特征：面试协商（只用强词，绝不加"确认/好的/可以/上午/下午/点/分"这类泛词）
    # 作用：给意图层一个先验方向；命中后仍低置信 → 转人工，不自动恢复图
    MailRule(
        name="面试协商",
        keywords=[
            "面试时间", "面试安排", "协商面试", "调整面试", "面试进度",
            "改期", "换个时间", "另约", "重新约", "拒绝面试", "放弃面试",
        ],
        fields=["subject", "body"],
        action=RuleAction.PRIOR,
        prior_intent="INTERVIEW_REPLY",
    ),
]
```

> 注意：`catch_all`、`reply_text`、以及 `确认/好的/可以/方便/收到/明白/上午/下午/晚上/点/分` 这些泛词**全部下线**。泛词是现状误报的头号来源（[rules.py#L78-L82](utils/emailbot/rules.py#L78-L82)）。

### 5.5 修改 `utils/emailbot/bot.py`（过滤 + 回复策略）

规则语义变了，`_handle_one` 也要跟着改：**不再"第一条命中即停"**，而是收集全部命中，先做 L0 拦截，再进意图管道。

```python
# 需要新增/合并的 import
from app.core.config import get_settings
from utils.emailbot.intent import MailIntent
from utils.emailbot.rules import MailRule, RuleAction

settings = get_settings()

# 按意图决定的自动回复文案（原 matched.reply_text 的职责迁移到这里）
INTENT_REPLY_TEXT = {
    MailIntent.RESUME_SUBMISSION: "您好，您的简历已收到，我们会在评估后与您联系。",
    MailIntent.PROGRESS_INQUIRY: "您好，您的应聘状态正在评估中，请耐心等待通知。",
}

# ---- _handle_one 内，替换原第 3 步"遍历 rules 匹配" ----
matched_rules: list[MailRule] = [
    r for r in self.rules
    if r.matches(
        subject=email.subject,
        sender=f"{email.from_.name} {email.from_.address}",
        body=email.text,
        attachment_names=[a.filename for a in attachments],
    )
]

# L0 拦截：任一 BLOCK 命中 → 直接忽略，不进意图层（省掉一次 LLM 调用）
if any(r.action is RuleAction.BLOCK for r in matched_rules):
    logger.info("[L0 BLOCK] uid=%s | 命中=%s",
                email.uid, [r.name for r in matched_rules if r.action is RuleAction.BLOCK])
    return

# 命中规则只作为先验特征传给意图层，不再决定动作
# （可在 intent.classify_intent 增加 prior_intents 入参，用于加权置信度）
prior_intents = [r.prior_intent for r in matched_rules if r.action is RuleAction.PRIOR]

# ---- 自动回复：由意图决定，而不是由规则文案决定 ----
intent_result = getattr(email, "intent_result", None)
should_reply = (
    self.settings.auto_reply
    and intent_result is not None
    and intent_result.intent.value in settings.INTENT_AUTO_REPLY_INTENTS
    and not intent_result.needs_review
)
if should_reply and not self._state.is_seen(email.message_id):
    await self.reply(email, text=INTENT_REPLY_TEXT[intent_result.intent])
self._state.mark_seen(email.message_id)
```

同时：发信后保存 `Message-ID` 与候选人/线程的映射，供 L1 归属使用。

```python
# send_email 之后（或 reply 之后）落库
await save_outbound_message_id(
    candidate_email=msg["To"],
    message_id=msg["Message-ID"],
)
```

> **实现细节以实施稿为准**：最终实现中，意图分类只在 `notify`（`on_match`）里做一次，`MatchHook` 返回 `MailIntentResult` 供 `bot` 决定自动回复，避免重复调用 LLM。详见 [intent_recognition_implementation.md](intent_recognition_implementation.md) 第 5 节。

### 5.6 `app/core/config.py` 新增配置

```python
# ---------- 意图识别 ----------
INTENT_ENGINE: str = Field(default="hybrid", description="rule / llm / hybrid")
INTENT_CONFIDENCE_THRESHOLD: float = Field(default=0.80, ge=0.0, le=1.0)
INTENT_AUTO_REPLY_INTENTS: list[str] = Field(
    default=["RESUME_SUBMISSION", "PROGRESS_INQUIRY"],
    description="允许自动回复的意图白名单",
)
INTENT_HUMAN_REVIEW_ENABLED: bool = Field(default=True)
INTENT_MAX_MAIL_CHARS: int = Field(default=4000, ge=500)
INTENT_CACHE_SIZE: int = Field(default=1000, description="进程内分类结果缓存上限")
INTENT_LLM_TIMEOUT_SECONDS: float = Field(default=10.0, ge=1.0, description="单次分类调用超时")
```

### 5.7 新增 `mail_intent_log` 表（alembic 迁移骨架）

```python
"""add mail_intent_log

Revision ID: xxx
"""
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.create_table(
        "mail_intent_log",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("uid", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=True),
        sa.Column("from_address", sa.String(), nullable=False),
        sa.Column("intent", sa.String(), nullable=False),
        sa.Column("sub_intent", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("slots", sa.JSON(), nullable=True),
        sa.Column("hit_rule", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("needs_review", sa.Boolean(), default=False),
        sa.Column("review_result", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_mail_intent_log_uid", "mail_intent_log", ["uid"])
    op.create_index("ix_mail_intent_log_intent", "mail_intent_log", ["intent"])
```

### 5.8 测试示例 `tests/test_intent.py`

```python
import asyncio

from utils.emailbot import intent as intent_module
from utils.emailbot.intent import MailIntent, SenderRole, classify_intent
from utils.emailbot.models import EmailAddress, ParsedEmail
from utils.emailbot.rules import DEFAULT_RULES, RuleAction


def _mail(subject: str, text: str, attachments=None) -> ParsedEmail:
    return ParsedEmail(
        uid="1",
        message_id="<t@local>",
        subject=subject,
        from_=EmailAddress(name="张三", address="z@ex.com"),
        text=text,
        attachments=attachments or [],
    )


def _rule(name: str):
    return next(r for r in DEFAULT_RULES if r.name == name)


def test_l0_block_rule_hits_auto_reply():
    """自动回复命中 BLOCK 规则：廉价拦截，不进 LLM。"""
    rule = _rule("自动回复与系统通知")
    assert rule.action is RuleAction.BLOCK
    assert rule.matches("自动回复", "z@ex.com", "外出办公，稍后回复", [])


def test_generic_words_no_longer_hit_any_rule():
    """"好的""确认"这类泛词不再命中任何规则（误报根源已下线）。"""
    mail = _mail("Re: 面试邀请", "好的，确认")
    hit = [r for r in DEFAULT_RULES
           if r.matches(mail.subject, "张三 z@ex.com", mail.text, [])]
    assert hit == []


def test_resume_prior_rule_requires_attachment():
    rule = _rule("简历投递")
    assert rule.action is RuleAction.PRIOR
    assert rule.prior_intent == "RESUME_SUBMISSION"
    # 无附件不命中（require_attachment=True）
    assert not rule.matches("应聘 Java", "张三 z@ex.com", "请查收简历", [])
    # 有附件才命中
    assert rule.matches("应聘 Java", "张三 z@ex.com", "请查收简历", ["resume.pdf"])


def test_rule_fallback_marks_generic_as_review(monkeypatch):
    """规则兜底下，"好的"判为 AMBIGUOUS 且转人工，绝不猜。"""
    intent_module._cache.clear()
    monkeypatch.setattr(intent_module.settings, "INTENT_ENGINE", "rule")
    r = asyncio.run(classify_intent(
        _mail("Re: 面试邀请", "好的"),
        role=SenderRole.CANDIDATE, waiting_reply=False,
    ))
    assert r.intent is MailIntent.AMBIGUOUS
    assert r.needs_review is True
```

---

## 六、rules.py 的定位与改造

### 6.1 结论

`rules.py` **保留**，但角色从"意图识别器"降级为 **L0 确定性预过滤层 + 规则配置表**。

> 关键词真相只在 `rules.py` 一处；意图真值只在 `intent.py` 一处。

"廉价"指**判定成本**（字符串匹配、不调 LLM、零 token），不是重要性低——它专门处理那些不需要理解语义的判断，把 LLM 的预算留给真正需要理解的部分。

### 6.2 职责边界

| 层 | 位置 | 输出 |
|---|---|---|
| L0 确定性过滤 | `rules.py`（`BLOCK` / `ALLOW` / `PRIOR`） | 放行 / 拦截 + 先验标签，**不产出意图** |
| L2 意图判定 | `intent.py`（LLM 结构化输出） | 意图 + 槽位 + 置信度 |
| 规则兜底 | `intent.py::_rule_classify` | 调 `MailRule.matches()`，**不维护自己的关键词表** |

### 6.3 三种规则动作

| action | 语义 | 例子 |
|---|---|---|
| `BLOCK` | 拦截，不进 LLM | 自动回复、退信、营销、黑名单 |
| `ALLOW` | 放行，不附标签 | 默认 |
| `PRIOR` | 放行 + 打先验意图标签 | 带附件 + 简历词 → `RESUME_SUBMISSION`；面试强词 → `INTERVIEW_REPLY` |

### 6.4 必须做的四件事

1. **删 `catch_all`**（[rules.py#L87-L93](utils/emailbot/rules.py#L87-L93)）：它是"所有邮件都触发 `on_match`"的根源（[bot.py#L168-L175](utils/emailbot/bot.py#L168-L175)）。
2. **`matches()` 不再"第一条命中即停"**：改为收集全部命中，允许多个先验特征叠加（[bot.py#L168-L175](utils/emailbot/bot.py#L168-L175)）。
3. **删 `reply_text`**：回复文案由意图决定（见 5.5 的 `INTENT_REPLY_TEXT`）。
4. **下线泛词**（[rules.py#L78-L82](utils/emailbot/rules.py#L78-L82)）：`确认/好的/可以/方便/收到/明白/上午/下午/晚上/点/分` 全部删除。

### 6.5 一封邮件的流转

| 邮件 | L0 | 进 LLM | L2 | 动作 |
|---|---|---|---|---|
| "自动回复：外出办公" | `BLOCK` | 否 | — | 忽略 |
| "请查收我的简历.pdf" | `PRIOR(RESUME_SUBMISSION)` | 是 | 确认 0.95 | 落待确认 → 确认后建人起图 |
| "好的" | 放行 | 是 | `OUT_OF_SCOPE` 0.9 | 忽略 |
| "确认，明天下午 3 点" | 放行 | 是 | `INTERVIEW_REPLY` 0.92 | 恢复图 |

### 6.6 硬性边界

> **过滤器不产出意图，只产出放行决定 + 先验标签。**
> 一旦规则里再出现"命中 X 就是面试回复"，就退回到 `rule.name == "询问面试时间或进度"` 的老问题。

---

## 七、上下文与安全校验（L3 四道防线）

恢复图前必须同时满足：

1. **状态校验**：候选人状态 ∈ `{AI_FILTER_PASSED, MANUAL_PASSED}`，统一应用到所有回复类意图（对齐 `utils/emailbot/notify.py#L106-L110` 的既有兜底条件）。
2. **checkpoint 存在性**：`checkpointer.aget_tuple(...)` 非空（`app/core/checkpointer.py#L77-L87` 返回进程单例，可直接用）。现状 `utils/emailbot/notify.py#L69-L81`、`#L118-L130` 无任何校验，thread 不存在时行为未定义，必须防御。
3. **中断点校验**：确认图停在 `get_candidate_email_reply_node`，避免对已结束流程重复 resume。
4. **幂等**：同一 `message_id` 只处理一次；`thread_id` + `uid` 加 Redis 锁，防并发重入。

### 7.1 线程归属改造

`thread_id` 由"发件邮箱地址"改为 **`candidate_{id}`**；归属用 `In-Reply-To` / `References` 匹配我方邀请邮件的 Message-ID（`utils/emailbot/bot.py#L236` 已能生成 Message-ID，需落库）。

解决两个隐患：

- 候选人换邮箱回复 → 丢上下文（现状 `utils/emailbot/notify.py#L65`）。
- 多人共用邮箱 → 串 thread。

> **落地状态**：本轮**未实施**该改造，`thread_id` 仍为 `candidate.email`。原因是建图（`app/tasks/agent_tasks.py`）与超时巡检（`app/tasks/timeout_tasks.py`）都用邮箱做 thread_id，单方面修改会让 resume 落空；需三处同步改造，列为后续项。详见 [intent_recognition_implementation.md](intent_recognition_implementation.md) 第 16 节。

---

## 八、动作分发与回复策略

### 8.1 分发映射表

| 意图 | 校验 | 动作 |
|---|---|---|
| `RESUME_SUBMISSION` | — | 落 `mail_resume_submissions(PENDING)` + 通知 HR；**人工确认后**才建人 → 起图（见第九节） |
| `INTERVIEW_REPLY` | 状态 + checkpoint + 中断点 | `graph.ainvoke(Command(resume=...), thread_id=candidate_id)` |
| `PROGRESS_INQUIRY` | 候选人在流程中 | 自动回进度模板 + 通知 HR |
| `INTERVIEWER_MAIL` | 发件人=面试官 | 落库 + 通知 HR，不自动回 |
| `AUTO_REPLY` | — | 忽略 |
| `OUT_OF_SCOPE` | — | 忽略（可选记录） |
| `AMBIGUOUS` | — | 人工复核，不 resume、不自动回 |

### 8.2 回复策略

- `rules.py` 不再携带 `reply_text`；文案改为按意图映射（见 5.5 `INTENT_REPLY_TEXT`）。
- **自动回复白名单**：只有 `PROGRESS_INQUIRY`、`RESUME_SUBMISSION` 这类低风险动作自动回；`INTERVIEW_REPLY` 的确认/改期/拒绝由图处理结果决定，不自动回。
- 消除"确认面试却收到『正在评估中』"的错配（现 `utils/emailbot/rules.py#L84`）。

---

## 九、RESUME_SUBMISSION 的待确认闭环

### 9.1 结论

`RESUME_SUBMISSION` 的动作定义为：**落「待确认」记录 + 通知 HR，人工确认后再走正常建人与起图流程**。不自动建人、不自动起图。

### 9.2 为什么要改

| 理由 | 说明 |
|---|---|
| 补齐 `position_id` | `CandidateCreateSchema` 必填（[candidate_schema.py#L39](app/schemas/candidate_schema.py#L39)），邮件里没有职位信息；由 HR 在确认时选择，**不猜** |
| 补齐 `creator_id` | [create_candidate_ser](app/service/candidate_ser.py#L103-L117) 需要 `current_user.id`；确认动作本就是登录 HR 在操作，天然满足 |
| 高风险动作不自动执行 | 建人会触发评分、发邀请邮件、建钉钉日程，误判代价大（与自动回复白名单同一原则） |
| 复用现成链路 | 现有 [add.vue](hr-frontend/src/pages/candidates/add.vue) 就是"上传 → 解析 → 选职位 → 确定"，确认台可照搬 |
| 不污染数据 | 未确认的投递不进 `candidates` 表，避免错误职位导致后续评分/面试官/日程错位 |

### 9.3 数据表 `mail_resume_submissions`

业务实体表，**与埋点表 `mail_intent_log` 分开**（后者只做统计，不做业务流转）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | String PK | |
| `email_uid` | String | 邮件 UID，追溯用 |
| `message_id` | String, **unique** | 去重（`utils/emailbot/models.py#L36`） |
| `from_address` / `from_name` | String | 投递人 |
| `subject` | String | 主题 |
| `body_excerpt` | Text | 正文摘要（不存全文，见 9.6 合规） |
| `attachment_paths` | JSON | 附件磁盘路径（[bot.py#L163-L165](utils/emailbot/bot.py#L163-L165) 已落盘） |
| `parsed_info` | JSON | LLM 抽取的 `AgentCandidateSchema` |
| `suggested_position_id` | String, null | 自动匹配到的职位 |
| `position_id` | String, null | 确认时 HR 选择 |
| `resume_id` | String, null | 确认时创建 |
| `status` | Enum | `PENDING` / `CONFIRMED` / `REJECTED` / `EXPIRED` |
| `reviewer_id` / `reviewed_at` / `review_note` | | 确认痕迹 |
| `created_at` / `updated_at` | | |

索引：`unique(message_id)`、`(status, created_at)`。

### 9.4 流程

```
邮件命中 RESUME_SUBMISSION
   └─ 落 mail_resume_submissions（PENDING）+ 通知 HR（钉钉 / 前端未读）

HR 在确认台：
   预览附件 → 核对 parsed_info → 选 position_id → 确认 / 拒绝

确认：
   附件迁入 RESUME_DIR → 建 ResumeModel（复用 Word→PDF）
   → create_candidate_ser(current_user=HR, position_id, resume_id)
   → run_candidate_agent(...)     ← 完全复用现有起图，零新逻辑
   → status = CONFIRMED
```

### 9.5 确认后如何起图（零新逻辑）

起图代码一行都不用改：确认后调用的就是现有的 [create_candidate_ser](app/service/candidate_ser.py#L103-L117) + [run_candidate_agent](app/tasks/agent_tasks.py#L43-L50)，甚至可以直接复用 `POST /candidate/create` 接口。新增的只是"从待确认记录触发"这一步。

### 9.6 关键实现点（坑）

| 坑 | 对策 |
|---|---|
| 附件位置 | 邮件附件落在 `settings.attach_dir`（默认 `./attachments`），而 [ResumeModel](app/models/candidate.py#L133-L137) 语义上是 `RESUME_DIR`；确认时迁移并复用 [Word→PDF 转换](app/service/candidate_ser.py#L62-L74) |
| 重复投递 | `message_id` 唯一 + 同邮箱判重（[CandidateRepo.get_by_email](utils/emailbot/notify.py#L22-L34)），重复则合并或提示 |
| 重复确认 | 确认接口幂等：`status != PENDING` 直接返回，不重复建人 |
| 长期积压 | 定时任务把超 N 天 `PENDING` 置 `EXPIRED` |
| 隐私合规 | 邮件正文可能含敏感信息，只存摘要或加密，设保留期 |
| 通知到达 | 只落库不够，必须推钉钉 / 前端未读，否则 HR 不知道有新投递 |

### 9.7 API 与前端

建议新增（前缀 `/mail-submissions`，或挂在 candidate 路由下）：

| 接口 | 说明 |
|---|---|
| `GET /mail-submissions?status=PENDING` | 待确认列表 |
| `GET /mail-submissions/{id}` | 详情（`parsed_info` + 附件预览） |
| `POST /mail-submissions/{id}/confirm` |  body `{position_id}` → 建人 + 起图 |
| `POST /mail-submissions/{id}/reject` | body `{note}` → 置 `REJECTED` |

前端新增确认台页面，交互对照 [add.vue](hr-frontend/src/pages/candidates/add.vue)（预览 → 核对 → 选职位 → 确定）。

### 9.8 灰度自动确认（可选，M4 再做）

仅当 `confidence ≥ 阈值` **且**职位唯一命中时才自动确认，默认关闭，开关独立。

---

## 十、可观测性与评测

### 10.1 指标

- 意图分布、规则/LLM 占比、平均置信度
- **误判率 = 人工复核翻转数 / 总处理数**（核心质量指标）
- 自动回复率、图恢复成功率、resume 失败率
- LLM 调用耗时与 token 成本

### 10.2 离线评测

- 从 `mailbot_state.json`、日志、邮箱样本收集历史邮件（脱敏），人工标注 100–300 封，覆盖"好的/收到"等边界样本。
- 每次改提示词或规则前跑评测，输出每个意图的准召与混淆矩阵；**重点关注 `INTERVIEW_REPLY` ↔ `OUT_OF_SCOPE`**。

### 10.3 灰度开关

`app/core/config.py#L109-L126` 区域新增 `INTENT_*` 配置：

| 配置 | 建议默认 | 说明 |
|---|---|---|
| `INTENT_ENGINE` | `hybrid` | `rule` / `llm` / `hybrid`，一键回退规则模式 |
| `INTENT_CONFIDENCE_THRESHOLD` | `0.80` | 低于阈值置 `needs_review` 转人工 |
| `INTENT_AUTO_REPLY_INTENTS` | `[RESUME_SUBMISSION, PROGRESS_INQUIRY]` | 自动回复白名单 |
| `INTENT_HUMAN_REVIEW_ENABLED` | `True` | 人工复核队列开关 |
| `INTENT_MAX_MAIL_CHARS` | `4000` | 正文截断长度 |
| `INTENT_CACHE_SIZE` | `1000` | 进程内分类缓存上限 |
| `INTENT_LLM_TIMEOUT_SECONDS` | `10.0` | 单次模型调用超时，超时降级/兜底 |

---

## 十一、文件改动清单

| 文件 | 改动 | 类型 |
|---|---|---|
| `utils/emailbot/intent.py` | 意图枚举、身份识别、LLM 分类器、规则兜底 | 新增 |
| `app/schemas/intent_schema.py` | 公共意图/槽位 schema | 新增 |
| `tests/test_intent.py` | 规则引擎单元测试 | 新增 |
| `utils/emailbot/notify.py` | 重写为五层管道分发器；保留旧签名兼容 | 修改 |
| `utils/emailbot/rules.py` | 降级为 L0 预过滤层：`BLOCK`/`ALLOW`/`PRIOR` 三种动作；删 `catch_all` / `reply_text` / 泛词 | 修改 |
| `utils/emailbot/bot.py` | 多规则命中 + L0 拦截；回复改读意图（`INTENT_REPLY_TEXT`）；发信后保存 Message-ID 映射 | 修改 |
| `utils/emailbot/mail_manager.py` | `init` 传入意图引擎与开关 | 修改 |
| `app/core/config.py` | 新增 `INTENT_*` 配置 | 修改 |
| `app/workflow/candidate_nodes/check_availability_node.py` | 复用公共 `ProposedTime`（`#L31-L44` 迁出） | 修改 |
| alembic 迁移 + `app/models` | 新增 `mail_intent_log` 表 | 新增 |
| `app/models` + alembic 迁移 | 新增 `mail_resume_submissions` 表（待确认投递） | 新增 |
| `app/repository` + `app/service` | 待确认记录的 CRUD、附件迁移建 Resume、确认建人起图 | 新增 |
| `app/routers` | 待确认接口：列表 / 详情 / 确认 / 拒绝 | 新增 |
| `hr-frontend/src/pages/candidates/` | 新增「邮件投递确认台」页面（对照 `add.vue`） | 新增 |
| `app/routers`（可选） | 人工复核接口：列表、翻转意图、重放 | 新增 |

---

## 十二、分阶段落地

| 阶段 | 内容 | 风险 |
|---|---|---|
| **M1 止血** | 下线泛词；L0 过滤（自动回复/退信/自己发的信）；状态 + checkpoint 校验；日志埋点。不动 LLM | 低，纯防御 |
| **M2 意图引擎** | 落地 `intent.py`，LLM 结构化分类 + 规则兜底 + 评测集；灰度 `hybrid`；低置信转人工 | 中，需评测验证 |
| **M3 闭环** | 回复策略按意图模板化；`RESUME_SUBMISSION` 落 `mail_resume_submissions` + 确认台（API/前端/通知）；`thread_id` 改 candidate_id + Message-ID 归属 | 中，涉及数据迁移与前端 |
| **M4 运营** | 指标看板、人工复核界面、提示词/规则持续迭代、灰度"自动确认"投递、多邮箱/转发场景 | 低 |

---

## 十三、风险与对策

| 风险 | 对策 |
|---|---|
| LLM 延迟/不可用 | 超时 + 规则兜底 + `INTENT_ENGINE=rule` 一键降级 |
| 误判导致错误恢复图，污染流程状态 | L3 四道校验 + 幂等 + 低置信转人工 |
| 错误自动回复影响候选人体验 | 自动回复白名单，高风险动作一律不自动回 |
| LLM 成本 | 缓存、截断、仅对过滤后邮件调用、按意图路由模型 |
| 提示词/关键词漂移（两处各写一份） | schema 与提示词集中公共模块；关键词只在 `rules.py` 维护，`intent.py` 复用 `MailRule.matches()` |
| 邮件内容出域合规 | 分类前脱敏选项、日志不记全文、确认服务商合规条款 |
| 待确认投递积压 / 确认延迟 | 超期自动 `EXPIRED`；钉钉 + 前端未读双通道通知；积压看板 |
| 旧逻辑回退成本高 | 保留 `MailRule` 与旧签名一个版本周期，开关可回退 |

---

## 十四、关键判断（优先级）

1. **先修误报，再上 LLM**：M1 的泛词下线 + checkpoint 校验能立刻止血，投入产出比最高。
2. **意图判定的核心是上下文而非正文**：把"是否在等待回复"作为分类特征，是解决"好的/收到"误判的根本手段。
3. **notify 与图内抽取分层**：notify 做邮件级分流，图内 `check_availability` 继续做时间槽位精抽取，避免重复 LLM 调用与提示词漂移。
4. **线程归属改用 candidate_id + Message-ID**：现有 `thread_id=发件邮箱` 是结构性缺陷，越早改迁移成本越低。
5. **rules.py 降级为 L0 预过滤层**：关键词真相只在 `rules.py`，意图真值只在 `intent.py`；`catch_all` 与泛词必须删，否则 L0 形同虚设。
6. **RESUME_SUBMISSION 走"待确认"而不是自动起图**：它让 `position_id` 与 `creator_id` 两个缺口自然消失，也避免误判直接放大成错误建人、错误邀请邮件与错误钉钉日程。
