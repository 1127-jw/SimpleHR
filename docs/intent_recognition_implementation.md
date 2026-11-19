# 意图识别改造 · 落地实施稿

> **性质**：本文档是可直接照抄的实施代码（新增文件给全文，修改文件给改动片段）。
> **当前代码库尚未改动**——本文档只描述"要改什么、改成什么"，不含任何对现有源码的修改。
> **配套设计**：[docs/intent_recognition_plan.md](intent_recognition_plan.md)（背景、架构、路线图）。

---

## 架构总览

### 分层意图管道

```
┌───────────────────────────────────────────────────────────────────────────┐
│                           邮件机器人（MailBot）                            │
│  APScheduler 每 20s 轮询 IMAP → 解析邮件 → 落盘附件 → 规则预过滤           │
└───────────────────────────────────┬───────────────────────────────────────┘
                                     │
                                     ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  L0  规则预过滤（rules.py，无 LLM，纯关键词）                              │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐                     │
│  │ BLOCK 拦截  │   │ ALLOW 放行  │   │ PRIOR 先验  │                     │
│  │ 自动回复    │   │             │   │ 简历/面试   │                     │
│  │ 退信/营销   │   │             │   │ 打意图标签 │                     │
│  └─────────────┘   └─────────────┘   └─────────────┘                     │
└───────────────────────────────────┬───────────────────────────────────────┘
                                     │ 命中 BLOCK → 直接忽略
                                     ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  L1  身份识别（notify.py）                                                 │
│  按发件人邮箱查 candidates 表 → CANDIDATE / UNKNOWN                        │
└───────────────────────────────────┬───────────────────────────────────────┘
                                     ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  L2  意图分类（intent.py，LLM + 规则兜底）                                 │
│  ┌─────────────────────────────┐   ┌─────────────────────────────┐        │
│  │ 主模型 deepseek（结构化输出）│ → │ 备用 qwen（RunnableFallback）│        │
│  └─────────────────────────────┘   └─────────────────────────────┘        │
│  失败回落纯规则引擎；置信度 < 阈值 → needs_review=True                    │
└───────────────────────────────────┬───────────────────────────────────────┘
                                     ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  L3  流程校验（notify.py）                                                 │
│  _waiting_reply() 用 StateSnapshot 判断图是否停在中断点                   │
└───────────────────────────────────┬───────────────────────────────────────┘
                                     ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  L4  动作分发（notify.py）                                                 │
│  AUTO_REPLY → 自动回复    RESUME_SUBMISSION → 落 PENDING（投递确认）      │
│  INTERVIEW_REPLY → 恢复图  PROGRESS_INQUIRY → 通知 HR                    │
│  INTERVIEWER_MAIL → 通知 HR   needs_review → 人工复核                     │
└───────────────────────────────────────────────────────────────────────────┘
```

### 意图枚举

| 意图 | 含义 | 典型动作 |
|------|------|---------|
| `RESUME_SUBMISSION` | 新简历投递 | 落「待确认」PENDING，HR 确认后建人起图 |
| `INTERVIEW_REPLY` | 面试协商回复 | 恢复 LangGraph 图（仅协商中候选人） |
| `PROGRESS_INQUIRY` | 询问进度/结果 | 记录 + 通知 HR |
| `INTERVIEWER_MAIL` | 面试官邮件 | 记录 + 通知 HR |
| `AUTO_REPLY` | 自动回复/OOO/退信 | 按意图自动回复 |
| `OUT_OF_SCOPE` | 无关邮件 | 忽略 |
| `AMBIGUOUS` | 信息不足 | 转人工复核 |

---

## 新增功能（2026-11-15 修复）

### 1. 投递优先落库

**问题**：`RESUME_SUBMISSION` 的置信度常低于阈值（如 0.72 < 0.80），被 `needs_review` 拦截，导致真实投递永远不落库。

**改动**：把 `RESUME_SUBMISSION` 判断**移到 `needs_review` 之前**，无条件先落 PENDING。

**理由**：「邮件投递确认」本身就是 HR 人工确认环节，落 PENDING 不会自动建人/起图，是安全动作。漏掉真实投递的代价远大于误落库。

### 2. Checkpoint 判断修复

**问题**：新版 LangGraph 的 `CheckpointTuple` 移除了 `next` 字段，`_waiting_reply` 抛 `AttributeError`，导致整个 `notify` 在意图分类前中断。

**改动**：改用 `graph.aget_state()` 获取 `StateSnapshot`（含 `next` / `interrupts`），并模块级缓存图实例。

---

## 代码学习指导

### 阅读顺序（建议 30 分钟上手）

| 步骤 | 文件 | 关注点 |
|------|------|--------|
| 1 | `utils/emailbot/rules.py` | 理解 L0 规则：BLOCK / ALLOW / PRIOR 三种动作 |
| 2 | `utils/emailbot/intent.py` | 理解 LLM 意图分类：Prompt、结构化输出、降级链、规则兜底 |
| 3 | `utils/emailbot/notify.py` | 理解五层管道：身份 → 分类 → 校验 → 分发 |
| 4 | `utils/emailbot/bot.py` | 理解轮询主循环：拉邮件 → 附件落盘 → 调 on_match |
| 5 | `app/service/mail_submission_ser.py` | 理解投递确认闭环：落库 / 确认 / 拒绝 / 过期 |
| 6 | `app/tasks/agent_tasks.py` | 理解图如何被恢复（`Command(resume=...)`） |

### 核心调用链

```
MailManager._poll_once()                     # 定时轮询
  └── MailBot.poll_once()                    # 拉取 UID > last_uid 的邮件
        └── MailBot._handle_one(email)       # 单封处理
              ├── MailRule.matches()         # L0 规则
              ├── MailBot._mark_email_seen() # 标记已读
              └── notify(email, rule)        # ★ 业务分发
                    ├── _find_candidate_by_email()   # L1 身份
                    ├── _waiting_reply()             # L3 校验
                    ├── classify_intent()            # L2 分类
                    └── _handle_resume_submission()  # L4 投递落库
```

### 关键概念

| 概念 | 说明 |
|------|------|
| **L0/L1/L2/L3/L4** | 五层管道：规则 → 身份 → 意图 → 校验 → 动作 |
| **PRIOR 先验** | 规则只提供「线索」（如带简历附件→投递），不决定意图 |
| **needs_review** | 低置信标记，仅影响是否转人工，不再拦截投递落库 |
| **StateSnapshot.next** | 非空 = 图停在中断点（等待回复）；空 = 图已结束 |
| **RunnableWithFallbacks** | LangChain 降级链，主模型异常时自动切备用模型 |
| **PENDING 投递** | 邮件投递不自动建人，先落库由 HR 确认，避免误建 |

### 常见调试手段

```bash
# 1. 看意图分类结果
Select-String -Path "logs\workflow.log" -Pattern "\[intent\]" -Encoding UTF8

# 2. 看投递是否落库
Select-String -Path "logs\workflow.log" -Pattern "mail_submission" -Encoding UTF8

# 3. 看轮询游标
Get-Content mailbot_state.json

# 4. 查投递记录
# SELECT * FROM mail_resume_submissions ORDER BY created_at DESC;

# 5. 重置游标让邮件重跑（改小 last_uid 后重启）
```

---

## 0. 改动总览

| # | 文件 | 动作 | 说明 |
|---|---|---|---|
| 1 | `utils/emailbot/intent.py` | 新增 | 意图枚举、身份、LLM 分类器、规则兜底 |
| 2 | `app/schemas/intent_schema.py` | 新增 | 公共 `ProposedTime` schema |
| 3 | `utils/emailbot/rules.py` | 重写 | 降级为 L0 预过滤层 |
| 4 | `utils/emailbot/notify.py` | 重写 | 五层管道分发器 |
| 5 | `utils/emailbot/bot.py` | 修改 | 多规则命中 + L0 拦截 + 回复策略 + Message-ID 落库 |
| 6 | `utils/emailbot/mail_manager.py` | 修改（可选） | 传入意图开关 |
| 7 | `app/core/config.py` | 修改 | 新增 `INTENT_*` / `MAIL_SUBMISSION_*` 配置 |
| 8 | `app/models/mail_submission.py` | 新增 | 待确认投递 ORM |
| 8b | `app/models/__init__.py` | 修改 | 导出 `MailResumeSubmissionModel`（否则 alembic/导入可能漏掉） |
| 9 | `alembic/versions/xxx_add_mail_submission.py` | 新增 | 建表迁移 |
| 10 | `app/repository/mail_submission_repo.py` | 新增 | 待确认记录 CRUD |
| 11 | `app/service/mail_submission_ser.py` | 新增 | 落库 / 确认 / 拒绝 / 过期 |
| 12 | `app/routers/mail_submission_router.py` | 新增 | 待确认接口 |
| 13 | `app/workflow/candidate_nodes/check_availability_node.py` | 修改 | `ProposedTime` 迁至公共 schema |
| 14 | `tests/test_intent.py` | 新增 | 单元测试 |
| 15 | `hr-frontend/src/pages/candidates/mail-submissions.vue` | 新增 | 确认台页面 |

**落地顺序**：7 → 9 → 8 → 2 → 3 → 1 → 13 → 4 → 5 → 10 → 11 → 12 → 14 → 15 → 6。

---

## 1. 新增 `utils/emailbot/intent.py`

```python
"""意图识别层：把「规则名路由」升级为「分层意图管道」。

L0 确定性预过滤（rules.py，无 LLM）→ L1 身份识别 → L2 意图分类（LLM + 规则兜底）
→ 产出 MailIntentResult 交给 notify 分发。

调用方式：直连模型 + 结构化输出（不使用 Agent），主模型失败降级备用模型。
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
3. AUTO_REPLY 优先级最高：命中自动回复特征时直接判定，不再看其他意图。
4. 「规则先验」只是线索，不是结论；与邮件内容冲突时以邮件内容为准。
5. 无法确定时判 AMBIGUOUS 并把 needs_review 设为 true，不要猜。

# 输出
严格输出 JSON：intent、sub_intent、confidence(0-1)、slots、evidence、reason、needs_review。
"""

INTENT_USER_PROMPT = """【发件人身份】{role}
【候选人状态】{candidate_status}
【是否在等待候选人回复】{waiting_reply}
【最近一次我方邮件】{last_outbound_subject}
【是否带附件】{has_attachment}
【规则先验】{prior_intents}

【邮件主题】{subject}

【邮件正文】
{body}
"""


# ==================== 分类器 ====================

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
    """直接调用大模型做结构化分类（主模型失败降级备用模型）。"""
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


def _cache_key(email: ParsedEmail, prior_intents: list[str] | None) -> str:
    raw = f"{email.message_id}|{email.subject}|{email.text[:2000]}|{sorted(prior_intents or [])}"
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
    prior_intents: list[str] | None = None,
) -> MailIntentResult:
    """LLM 意图分类；失败或低置信时回落规则兜底。"""
    key = _cache_key(email, prior_intents)
    if key in _cache:
        return _cache[key]

    prompt = INTENT_USER_PROMPT.format(
        role=role.value,
        candidate_status=candidate_status or "未知",
        waiting_reply="是" if waiting_reply else "否",
        last_outbound_subject=last_outbound_subject or "无",
        has_attachment="是" if has_attachment else "否",
        prior_intents="、".join(prior_intents) if prior_intents else "无",
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

    if len(_cache) >= settings.INTENT_CACHE_SIZE:
        _cache.clear()
    _cache[key] = result
    return result


# ==================== 规则兜底 ====================
# 关键词真相只在 rules.py 的 DEFAULT_RULES 一处，这里复用 MailRule.matches()。

async def _rule_classify(
    email: ParsedEmail,
    *,
    role: SenderRole,
    waiting_reply: bool,
    rules: list[MailRule] | None = None,
) -> MailIntentResult:
    """纯规则兜底：仅在 LLM 不可用时使用。

    只认强特征：L0 拦截（自动回复/退信/营销）、简历附件先验、面试强词先验；
    其余一律返回 AMBIGUOUS 低置信，转人工，绝不猜。
    """
    rules = rules if rules is not None else DEFAULT_RULES

    if any(r.action is RuleAction.BLOCK and _match(r, email) for r in rules):
        return MailIntentResult(
            intent=MailIntent.OUT_OF_SCOPE, confidence=0.9,
            reason="命中 L0 拦截规则（BLOCK）",
        )

    if role is SenderRole.INTERVIEWER:
        return MailIntentResult(
            intent=MailIntent.INTERVIEWER_MAIL, confidence=0.8,
            reason="发件人为面试官",
        )

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

---

## 2. 新增 `app/schemas/intent_schema.py`

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

---

## 3. 重写 `utils/emailbot/rules.py`

```python
"""规则层（L0 确定性预过滤）：只判「要不要进 LLM」，不判「邮件是什么意思」。

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
    # 强特征：带简历附件的投递 → 先验标签
    MailRule(
        name="简历投递",
        keywords=["简历", "应聘", "求职", "投递", "resume", "cv"],
        fields=["subject", "body", "attachment"],
        require_attachment=True,
        action=RuleAction.PRIOR,
        prior_intent="RESUME_SUBMISSION",
    ),
    # 强特征：面试协商（只用强词；命中后仍低置信 → 转人工，不自动恢复图）
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

> `catch_all`、`reply_text`、以及 `确认/好的/可以/方便/收到/明白/上午/下午/晚上/点/分` 全部下线。

---

## 4. 重写 `utils/emailbot/notify.py`

```python
"""邮件命中后的业务分发：意图管道 + 动作分发。"""
from __future__ import annotations

import logging

from langgraph.types import Command

from utils.emailbot.intent import MailIntent, MailIntentResult, SenderRole, classify_intent
from utils.emailbot.models import ParsedEmail
from utils.emailbot.rules import MailRule

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

_RESUMABLE_STATUS = ("AI筛选成功", "人工通过/重试")


async def _find_candidate_by_email(email_address: str):
    """根据邮箱地址查找候选人（含职位与面试官）。"""
    async with AsyncSessionFactory() as session:
        repo = CandidateRepo(session)
        candidate = await repo.get_by_email(email_address)
        if not candidate:
            return None, None, None
        return (
            CandidateSchema.model_validate(candidate),
            PositionSchema.model_validate(candidate.position),
            UserSH.model_validate(candidate.creator),
        )


async def _waiting_reply(thread_id: str) -> bool:
    """该 thread 是否有 checkpoint，且图停在中断点（next 非空）。"""
    try:
        snapshot = await get_checkpointer().aget_tuple(
            {"configurable": {"thread_id": thread_id}}
        )
    except Exception:
        logger.exception("读取 checkpoint 失败 | thread_id=%s", thread_id)
        return False
    return bool(snapshot and snapshot.next)


async def notify(email: ParsedEmail, rule: MailRule | None = None) -> MailIntentResult | None:
    """邮件命中后的回调：识别意图 → 校验 → 分发动作，返回分类结果供上层决定回复。"""
    # L1 身份识别
    candidate_schema, position_schema, interviewer_schema = \
        await _find_candidate_by_email(email.from_.address)

    if candidate_schema:
        role, candidate_status = SenderRole.CANDIDATE, candidate_schema.status
        thread_id = f"candidate_{candidate_schema.id}"
    else:
        role, candidate_status = SenderRole.UNKNOWN, ""
        thread_id = f"email_{email.from_.address}"

    # L3 前置校验：是否在等待回复
    waiting_reply = await _waiting_reply(thread_id) if candidate_schema else False

    # L2 意图分类
    result = await classify_intent(
        email,
        role=role,
        candidate_status=str(candidate_status),
        waiting_reply=waiting_reply,
        has_attachment=bool(email.attachments),
        prior_intents=getattr(email, "prior_intents", None),
    )
    logger.info(
        "[intent] uid=%s | intent=%s | sub=%s | conf=%.2f | review=%s | reason=%s",
        email.uid, result.intent.value, result.sub_intent,
        result.confidence, result.needs_review, result.reason,
    )

    # L4 动作分发
    if result.intent is MailIntent.AUTO_REPLY:
        return result
    if result.needs_review or result.intent is MailIntent.AMBIGUOUS:
        await _enqueue_human_review(email, result)
        return result
    if result.intent is MailIntent.INTERVIEW_REPLY:
        await _resume_candidate_graph(
            email, candidate_schema, position_schema, interviewer_schema
        )
        return result
    if result.intent is MailIntent.PROGRESS_INQUIRY:
        await _reply_progress(email, candidate_schema)
        return result
    if result.intent is MailIntent.RESUME_SUBMISSION:
        await _handle_resume_submission(email)
        return result
    if result.intent is MailIntent.INTERVIEWER_MAIL:
        await _notify_hr(email, result)
        return result
    # OUT_OF_SCOPE：忽略
    return result


async def _resume_candidate_graph(
    email: ParsedEmail,
    candidate_schema: CandidateSchema | None,
    position_schema: PositionSchema | None,
    interviewer_schema: UserSH | None,
) -> None:
    """恢复招聘图：仅在候选人在协商中、且 checkpoint 停在等待回复中断点。"""
    if not candidate_schema:
        return
    if str(candidate_schema.status) not in _RESUMABLE_STATUS:
        logger.info("不在协商中，不恢复图 | candidate_id=%s | status=%s",
                    candidate_schema.id, candidate_schema.status)
        return

    thread_id = f"candidate_{candidate_schema.id}"
    if not await _waiting_reply(thread_id):
        logger.info("无有效等待回复 checkpoint，不恢复图 | thread_id=%s", thread_id)
        return
    if not settings.GRAPH_OPEN:
        logger.info("GRAPH_OPEN=false，仅记录 | thread_id=%s", thread_id)
        return

    graph = build_candidate_graph(get_checkpointer())
    await graph.ainvoke(
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


async def _handle_resume_submission(email: ParsedEmail) -> None:
    """新简历投递：落「待确认」记录 + 通知 HR，不自动建人、不起图。"""
    from app.service.mail_submission_ser import (
        create_mail_resume_submission, notify_hr_new_submission,
    )
    await create_mail_resume_submission(email)
    await notify_hr_new_submission(email)


async def _reply_progress(email: ParsedEmail, candidate_schema) -> None:
    logger.info("[progress_inquiry] 来自 %s | candidate_id=%s",
                email.from_.address, candidate_schema.id if candidate_schema else None)


async def _notify_hr(email: ParsedEmail, result) -> None:
    logger.info("[notify_hr] 来自 %s | intent=%s | evidence=%s",
                email.from_.address, result.intent.value, result.evidence)


async def _enqueue_human_review(email: ParsedEmail, result) -> None:
    logger.warning("[human_review] uid=%s | intent=%s | conf=%.2f | reason=%s",
                   email.uid, result.intent.value, result.confidence, result.reason)
```

---

## 5. 修改 `utils/emailbot/bot.py`

### 5.1 头部新增

```python
from app.core.config import get_settings
from utils.emailbot.intent import MailIntent
from utils.emailbot.rules import MailRule, RuleAction

settings = get_settings()

# 按意图决定的自动回复文案（原 matched.reply_text 的职责迁移到这里）
INTENT_REPLY_TEXT = {
    MailIntent.RESUME_SUBMISSION: "您好，您的简历已收到，我们会在评估后与您联系。",
    MailIntent.PROGRESS_INQUIRY: "您好，您的应聘状态正在评估中，请耐心等待通知。",
}
```

### 5.2 替换 `_handle_one` 的第 3 步与第 5 步

```python
        # 3) 规则匹配：收集全部命中（不再"第一条命中即停"）
        matched_rules: list[MailRule] = [
            r for r in self.rules
            if r.matches(
                subject=email.subject,
                sender=f"{email.from_.name} {email.from_.address}",
                body=email.text,
                attachment_names=[a.filename for a in attachments],
            )
        ]

        # L0 拦截：任一 BLOCK 命中 → 直接忽略，不进意图层
        if any(r.action is RuleAction.BLOCK for r in matched_rules):
            logger.info("[L0 BLOCK] uid=%s | 命中=%s", email.uid,
                        [r.name for r in matched_rules if r.action is RuleAction.BLOCK])
            return

        # 先验标签（规则线索，挂在 email 上传给 on_match，不在这里调 LLM）
        prior_intents = [r.prior_intent for r in matched_rules
                         if r.action is RuleAction.PRIOR and r.prior_intent]
        email.prior_intents = prior_intents

        # 4) 标记已读（不变）
        await self._mark_email_seen(email)

        # 5) 通知业务方：on_match 内部完成意图分类，并返回分类结果
        #    （只分类一次，且能带上候选人状态 / 是否在等待回复等上下文）
        intent_result = None
        if self.on_match:
            try:
                intent_result = await self.on_match(
                    email, matched_rules[0] if matched_rules else None
                )
            except Exception:
                logger.exception("on_match hook failed")

        # 6) 自动回复：由意图决定，而不是由规则文案决定
        should_reply = (
            self.settings.auto_reply
            and intent_result is not None
            and intent_result.intent.value in settings.INTENT_AUTO_REPLY_INTENTS
            and not intent_result.needs_review
            and intent_result.intent in INTENT_REPLY_TEXT
        )
        if should_reply and not self._state.is_seen(email.message_id):
            await self.reply(email, text=INTENT_REPLY_TEXT[intent_result.intent])
        self._state.mark_seen(email.message_id)
```

> **`MatchHook` 签名变更**：由 `async def hook(email, rule) -> None` 改为
> `async def hook(email, rule) -> Optional[MailIntentResult]`。`notify` 返回分类结果，供 `bot` 决定自动回复。

> 说明：意图分类**不在 `bot` 里做**。`bot` 只做 L0 拦截，分类所需的候选人状态 / checkpoint 上下文由 `notify` 统一准备，避免重复调用 LLM。

### 5.3 发信后保存 Message-ID（供 L1 归属）

```python
        # send_email() 内，发送成功后
        await save_outbound_message_id(to=msg["To"], message_id=msg["Message-ID"])
```

`save_outbound_message_id` 可复用 Redis：`HSET outbound_message_ids <message_id> <to>`。

---

## 6. `app/core/config.py` 新增配置

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
INTENT_CACHE_SIZE: int = Field(default=1000)
INTENT_LLM_TIMEOUT_SECONDS: float = Field(default=10.0, ge=1.0)

# ---------- 邮件投递待确认 ----------
MAIL_SUBMISSION_EXPIRE_DAYS: int = Field(default=7, ge=1)
MAIL_SUBMISSION_DEFAULT_POSITION_ID: str = Field(default="", description="匹配不到职位时的默认职位，留空则转人工")
MAIL_SUBMISSION_SWEEP_INTERVAL_MINUTES: int = Field(default=60, ge=1)
```

---

## 7. 新增 `app/models/mail_submission.py`

```python
from datetime import datetime
from enum import StrEnum
from typing import Optional

from sqlalchemy import String, Text, JSON, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import BaseModel


class MailSubmissionStatusEnum(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class MailResumeSubmissionModel(BaseModel):
    __tablename__ = "mail_resume_submissions"
    __table_args__ = (
        Index("ix_mail_submission_status_created", "status", "created_at"),
    )

    email_uid: Mapped[str] = mapped_column(String(64), default="")
    message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    from_address: Mapped[str] = mapped_column(String(255), index=True, default="")
    from_name: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    body_excerpt: Mapped[str] = mapped_column(Text, default="")
    attachment_paths: Mapped[list] = mapped_column(JSON, default=list)
    parsed_info: Mapped[dict] = mapped_column(JSON, default=dict)
    suggested_position_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    position_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    resume_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=MailSubmissionStatusEnum.PENDING)
    reviewer_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
```

> 记得在 `app/models/__init__.py` 中导出 `MailResumeSubmissionModel`，否则 alembic 自动迁移与全局导入可能感知不到该表。

---

## 8. alembic 迁移

```python
"""add mail_resume_submissions

Revision ID: add_mail_submission
"""
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.create_table(
        "mail_resume_submissions",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("email_uid", sa.String(64), nullable=False),
        sa.Column("message_id", sa.String(255), nullable=False),
        sa.Column("from_address", sa.String(255), nullable=False),
        sa.Column("from_name", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("body_excerpt", sa.Text(), nullable=False),
        sa.Column("attachment_paths", sa.JSON(), nullable=False),
        sa.Column("parsed_info", sa.JSON(), nullable=False),
        sa.Column("suggested_position_id", sa.String(100), nullable=True),
        sa.Column("position_id", sa.String(100), nullable=True),
        sa.Column("resume_id", sa.String(100), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reviewer_id", sa.String(100), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=False),
    )
    op.create_index("ix_mail_submission_message_id", "mail_resume_submissions", ["message_id"], unique=True)
    op.create_index("ix_mail_submission_from_address", "mail_resume_submissions", ["from_address"])
    op.create_index("ix_mail_submission_status_created", "mail_resume_submissions", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_mail_submission_status_created", table_name="mail_resume_submissions")
    op.drop_index("ix_mail_submission_from_address", table_name="mail_resume_submissions")
    op.drop_index("ix_mail_submission_message_id", table_name="mail_resume_submissions")
    op.drop_table("mail_resume_submissions")
```

---

## 9. 新增 `app/repository/mail_submission_repo.py`

```python
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.models.mail_submission import (
    MailResumeSubmissionModel, MailSubmissionStatusEnum,
)
from app.models.user import UserModel
from . import BaseRepo


class MailSubmissionRepo(BaseRepo):
    async def create(self, data: dict) -> MailResumeSubmissionModel:
        obj = MailResumeSubmissionModel(**data)
        self.session.add(obj)
        await self.session.flush()
        return cast(MailResumeSubmissionModel, await self.get_by_id(obj.id))

    async def get_by_id(self, submission_id: str) -> MailResumeSubmissionModel | None:
        return cast(
            MailResumeSubmissionModel | None,
            await self.session.scalar(
                select(MailResumeSubmissionModel)
                .where(MailResumeSubmissionModel.id == submission_id)
                .options(selectinload(MailResumeSubmissionModel.reviewer))
            ),
        )

    async def get_by_message_id(self, message_id: str) -> MailResumeSubmissionModel | None:
        return cast(
            MailResumeSubmissionModel | None,
            await self.session.scalar(
                select(MailResumeSubmissionModel).where(
                    MailResumeSubmissionModel.message_id == message_id
                )
            ),
        )

    async def list_by_status(self, status: str, limit: int = 50) -> list[MailResumeSubmissionModel]:
        result = await self.session.scalars(
            select(MailResumeSubmissionModel)
            .where(MailResumeSubmissionModel.status == status)
            .order_by(MailResumeSubmissionModel.created_at.desc())
            .limit(limit)
        )
        return list(result)

    async def mark_status(self, submission_id: str, status: str, **fields) -> None:
        await self.session.execute(
            update(MailResumeSubmissionModel)
            .where(MailResumeSubmissionModel.id == submission_id)
            .values(status=status, **fields)
        )
```

---

## 10. 新增 `app/service/mail_submission_ser.py`

```python
"""邮件投递的待确认闭环：落库 / 确认建人起图 / 拒绝 / 过期。"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.models.mail_submission import MailSubmissionStatusEnum
from app.repository.mail_submission_repo import MailSubmissionRepo
from app.repository.resume_repo import CandidateRepo, ResumeRepo
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from utils.emailbot.models import ParsedEmail
from app.core.time import db_now

logger = logging.getLogger(__name__)
settings = get_settings()


async def create_mail_resume_submission(email: ParsedEmail) -> None:
    """落一条 PENDING 记录；同 message_id 幂等。"""
    async with AsyncSessionFactory() as session:
        async with session.begin():
            repo = MailSubmissionRepo(session)
            if await repo.get_by_message_id(email.message_id or email.uid):
                logger.info("[mail_submission] 重复投递，跳过 | uid=%s", email.uid)
                return
            await repo.create({
                "email_uid": email.uid,
                "message_id": email.message_id or email.uid,
                "from_address": email.from_.address,
                "from_name": email.from_.name,
                "subject": email.subject[:500],
                "body_excerpt": email.text[:2000],
                "attachment_paths": [str(a.saved_path) for a in email.attachments if a.saved_path],
                "parsed_info": {},
                "status": MailSubmissionStatusEnum.PENDING,
            })
    logger.info("[mail_submission] 已落待确认 | uid=%s | from=%s", email.uid, email.from_.address)


async def confirm_mail_submission(
    session: AsyncSession,
    submission_id: str,
    position_id: str,
    current_user,
) -> tuple:
    """确认：迁移附件 → 建 Resume → 建 Candidate，返回 (candidate, position, interviewer)。

    幂等：status != PENDING 直接抛错，由上层转为 409。
    事务：建 Resume / 建 Candidate / 更新状态在同一个事务内完成。
    """
    repo = MailSubmissionRepo(session)
    sub = await repo.get_by_id(submission_id)
    if sub is None:
        raise ValueError("待确认记录不存在")
    if sub.status != MailSubmissionStatusEnum.PENDING:
        raise ValueError(f"当前状态 {sub.status} 不可确认")
    if not sub.attachment_paths:
        raise ValueError("该投递没有可用附件，无法创建候选人")

    # 1) 附件迁移到 RESUME_DIR（doc/docx 转 PDF 需复用转换逻辑）
    stored_name = await _store_resume_file(sub.attachment_paths[0])

    info = sub.parsed_info or {}
    async with session.begin():
        resume = await ResumeRepo(session).create_resume(
            file_path=stored_name, uploader_id=current_user.id
        )
        candidate_model = await CandidateRepo(session).create_candidate({
            "name": info.get("name") or sub.from_name or sub.from_address,
            "email": sub.from_address,
            "gender": info.get("gender", "未知"),
            "phone_number": info.get("phone_number"),
            "work_experience": info.get("work_experience"),
            "project_experience": info.get("project_experience"),
            "education_experience": info.get("education_experience"),
            "self_evaluation": info.get("self_evaluation"),
            "other_information": info.get("other_information"),
            "skills": info.get("skills"),
            "work_year": info.get("work_year", 0),
            "position_id": position_id,
            "resume_id": resume.id,
            "creator_id": current_user.id,
        })
        await repo.mark_status(
            submission_id, MailSubmissionStatusEnum.CONFIRMED,
            position_id=position_id, resume_id=resume.id,
            reviewer_id=current_user.id,
            reviewed_at=db_now(),
        )

    # 会话内转 Schema（create_candidate 已带 eager 关联）
    candidate_schema = CandidateSchema.model_validate(candidate_model)
    position_schema = PositionSchema.model_validate(candidate_model.position)
    interviewer_schema = UserSH.model_validate(candidate_model.position.creator)
    return candidate_schema, position_schema, interviewer_schema


async def reject_mail_submission(session: AsyncSession, submission_id: str,
                                current_user, note: str = "") -> None:
    repo = MailSubmissionRepo(session)
    await repo.mark_status(
        submission_id, MailSubmissionStatusEnum.REJECTED,
        reviewer_id=current_user.id,
        reviewed_at=db_now(),
        review_note=note,
    )


async def expire_stale_submissions() -> int:
    """定时任务：超期 PENDING → EXPIRED，返回处理条数。"""
    from datetime import timedelta

    deadline = db_now() - timedelta(
        days=settings.MAIL_SUBMISSION_EXPIRE_DAYS
    )
    async with AsyncSessionFactory() as session:
        async with session.begin():
            repo = MailSubmissionRepo(session)
            stale = [
                s for s in await repo.list_by_status(MailSubmissionStatusEnum.PENDING, limit=500)
                if s.created_at < deadline
            ]
            for s in stale:
                await repo.mark_status(s.id, MailSubmissionStatusEnum.EXPIRED)
    return len(stale)


async def _store_resume_file(src_path: str) -> str:
    """把邮件附件复制进 RESUME_DIR，返回相对文件名（doc/docx 转 PDF 需复用转换逻辑）。"""
    src = Path(src_path)
    if not src.exists():
        raise ValueError(f"附件不存在: {src_path}")
    os.makedirs(settings.RESUME_DIR, exist_ok=True)
    ext = src.suffix
    name = f"{uuid4()}{ext}"
    dst = Path(settings.RESUME_DIR) / name
    shutil.copyfile(src, dst)
    return name


async def notify_hr_new_submission(email: ParsedEmail) -> None:
    """通知 HR：钉钉/前端未读（示例仅打日志，接入实际推送即可）。"""
    logger.info("[mail_submission] 通知 HR：新投递待确认 | from=%s | subject=%s",
                email.from_.address, email.subject)
```

---

## 11. 新增 `app/routers/mail_submission_router.py`

```python
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, get_hr_or_superuser
from app.core.db import get_db
from app.models.user import UserModel
from app.repository.mail_submission_repo import MailSubmissionRepo
from app.schemas.response_schema import ResponseSchema
from app.service.mail_submission_ser import (
    confirm_mail_submission, reject_mail_submission,
)
from app.tasks.agent_tasks import run_candidate_agent

router = APIRouter(prefix="/mail-submissions", tags=["mail-submissions"])


@router.get("", summary="待确认投递列表")
async def list_pending(
    session: Annotated[AsyncSession, Depends(get_db)],
    status: str = Query("PENDING"),
    _: UserModel = Depends(get_hr_or_superuser),
):
    rows = await MailSubmissionRepo(session).list_by_status(status)
    return {"items": [
        {"id": r.id, "from_address": r.from_address, "subject": r.subject,
         "status": r.status, "created_at": r.created_at,
         "attachment_paths": r.attachment_paths, "parsed_info": r.parsed_info}
        for r in rows
    ]}


@router.post("/{submission_id}/confirm", summary="确认投递（建人并起图）", response_model=ResponseSchema)
async def confirm(
    submission_id: str,
    position_id: str,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_db)],
    current_user: UserModel = Depends(get_current_user),
):
    candidate, position, interviewer = await confirm_mail_submission(
        session, submission_id, position_id, current_user
    )
    background_tasks.add_task(
        run_candidate_agent,
        candidate=candidate, position=position, interviewer=interviewer,
    )
    return ResponseSchema()


@router.post("/{submission_id}/reject", summary="拒绝投递", response_model=ResponseSchema)
async def reject(
    submission_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    note: str = "",
    current_user: UserModel = Depends(get_current_user),
):
    await reject_mail_submission(session, submission_id, current_user, note)
    return ResponseSchema()
```

---

## 12. 修改 `check_availability_node.py`（schema 迁移）

```python
# 删除文件内本地的 ProposedTime 定义，改为：
from app.schemas.intent_schema import ProposedTime
```

---

## 13. 新增 `tests/test_intent.py`

```python
import asyncio

from utils.emailbot import intent as intent_module
from utils.emailbot.intent import MailIntent, SenderRole, classify_intent
from utils.emailbot.models import EmailAddress, ParsedEmail
from utils.emailbot.rules import DEFAULT_RULES, RuleAction


def _mail(subject: str, text: str, attachments=None) -> ParsedEmail:
    return ParsedEmail(
        uid="1", message_id="<t@local>", subject=subject,
        from_=EmailAddress(name="张三", address="z@ex.com"),
        text=text, attachments=attachments or [],
    )


def _rule(name: str):
    return next(r for r in DEFAULT_RULES if r.name == name)


def test_l0_block_rule_hits_auto_reply():
    rule = _rule("自动回复与系统通知")
    assert rule.action is RuleAction.BLOCK
    assert rule.matches("自动回复", "z@ex.com", "外出办公，稍后回复", [])


def test_generic_words_no_longer_hit_any_rule():
    mail = _mail("Re: 面试邀请", "好的，确认")
    assert [r for r in DEFAULT_RULES
            if r.matches(mail.subject, "张三 z@ex.com", mail.text, [])] == []


def test_interview_strong_words_hit_prior():
    rule = _rule("面试协商")
    assert rule.action is RuleAction.PRIOR
    assert rule.prior_intent == "INTERVIEW_REPLY"
    assert rule.matches("Re: 面试邀请", "张三 z@ex.com", "面试时间想改期", [])


def test_resume_prior_rule_requires_attachment():
    rule = _rule("简历投递")
    assert not rule.matches("应聘 Java", "张三 z@ex.com", "请查收简历", [])
    assert rule.matches("应聘 Java", "张三 z@ex.com", "请查收简历", ["resume.pdf"])


def test_rule_fallback_marks_generic_as_review(monkeypatch):
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

## 14. 前端确认台（要点）

新增 `hr-frontend/src/pages/candidates/mail-submissions.vue`，交互对照 [add.vue](hr-frontend/src/pages/candidates/add.vue)：

| 区块 | 内容 |
|---|---|
| 列表 | 调 `GET /mail-submissions?status=PENDING`，展示投递人/主题/时间/附件数 |
| 详情 | 附件预览（PDF embed）+ `parsed_info` 字段表单（可编辑）+ 职位下拉 |
| 操作 | 「确认」调 `POST /{id}/confirm`（带 `position_id`）；「拒绝」调 `POST /{id}/reject` |
| 状态 | 已确认/已拒绝/已过期 Tab 切换 |

---

## 15. 落地顺序与自检清单

### 15.1 实施顺序

1. `app/core/config.py` 加配置
2. alembic 迁移 + `app/models/mail_submission.py`
3. `app/schemas/intent_schema.py` + `check_availability_node.py` 改引用
4. `utils/emailbot/rules.py` 重写
5. `utils/emailbot/intent.py` 新增
6. `utils/emailbot/notify.py` 重写
7. `utils/emailbot/bot.py` 修改
8. repository / service / router
9. `tests/test_intent.py`
10. 前端确认台

### 15.2 自检命令

```bash
alembic upgrade head
pytest tests/test_intent.py -v
python -c "from utils.emailbot.rules import DEFAULT_RULES; print(len(DEFAULT_RULES))"
```

### 15.3 上线开关

| 开关 | 默认 | 作用 |
|---|---|---|
| `INTENT_ENGINE` | `hybrid` | 置 `rule` 可整体回退规则模式 |
| `INTENT_AUTO_REPLY_INTENTS` | `[RESUME_SUBMISSION, PROGRESS_INQUIRY]` | 自动回复白名单 |
| `INTENT_HUMAN_REVIEW_ENABLED` | `True` | 低置信转人工 |

### 15.4 回退预案

- 意图层异常：`INTENT_ENGINE=rule`，只保留低风险动作（落待确认、自动回复），其余转人工；
- 待确认闭环异常：`RESUME_SUBMISSION` 退回"仅记录日志 + 通知 HR"，不阻塞邮件轮询；
- 旧逻辑保留一个版本周期（`MailRule` 与 `notify(email, rule)` 签名不变）。

---

## 16. 实施差异说明（代码落地后的实际形态）

本节记录实际落地时相对前文的调整，**以代码为准**：

| # | 项 | 前文方案 | 实际落地 | 原因 |
|---|---|---|---|---|
| 1 | 模型降级顺序 | qwen 主、deepseek 备 | **deepseek 主、qwen 备**（`RunnableWithFallbacks`） | 与仓库既有的 `llm_refactor_agent_to_direct.md` 改造约定保持一致 |
| 2 | `thread_id` | 改为 `candidate_{id}` + Message-ID 归属 | **保持 `candidate.email`** | 建图（`agent_tasks`）与超时巡检（`timeout_tasks`）都用邮箱做 thread_id，单方面改会导致 resume 落空；归属改造需三处同步，列为后续项 |
| 3 | Message-ID 记录 | `save_outbound_message_id()` 落库 | 写 Redis `HSET outbound_message_ids` | 归属是增强信息，用 Redis 更轻、失败不拖垮发信 |
| 4 | `parsed_info` | 预留字段 | 新增 `enrich_mail_submission()`：落库后 `asyncio.create_task` 后台 OCR + LLM 解析回填 | 让确认台能直接看到结构化信息，且失败不阻塞轮询 |
| 5 | 简历解析调用 | `create_candidate_ser` | 直接 `CandidateRepo.create_candidate` + 单事务 | 避免与 `create_candidate_ser` 的 `session.begin()` 嵌套 |
| 6 | 超期巡检 | 未明确 | 新增 `app/tasks/mail_submission_tasks.py`（`MailSubmissionSweepManager`），接入 lifespan | 与 `TimeoutSweepManager` 同风格 |

### 16.1 顺带修复的既有问题

`app/agents/resume.py` 仍从 `check_availability_node` 导入 `get_current_time`，而该工具已在提交 `b6e5d32`（LLM 直连改造）中被移除，导致 `import app.agents.resume` 直接 `ImportError`。已改为从 `app.agents.candidate` 导入同名工具。

### 16.2 验证结果

```bash
# 后端
.venv/Scripts/python.exe -m pytest tests/ -q      # 83 passed
.venv/Scripts/python.exe -m alembic heads         # f1a2b3c4d5e6 (head)
.venv/Scripts/python.exe -c "import app.main"     # OK

# 前端
pnpm type-check                                   # exit 0
pnpm build-only                                   # built in ~0.5s
```

### 16.3 投递邮件不落库问题修复（2026-11-15）

**现象**：候选人（`11272664@qq.com`）投递带简历附件的求职邮件后，「邮件投递确认」列表始终为空，`mail_resume_submissions` 表无任何记录。

**排查日志**：

```
16:08:27 | on_match hook failed | AttributeError: 'CheckpointTuple' object has no attribute 'next'
16:24:04 | [intent] uid=1703818728 | intent=RESUME_SUBMISSION | conf=0.72 | review=True
```

**根因（两层）**：

| 层 | 问题 | 影响 |
|---|---|---|
| 1 | 新版 LangGraph 的 `CheckpointTuple` 移除了 `next` 字段（只剩 `config/checkpoint/metadata/parent_config/pending_writes`），而 `_waiting_reply` 仍在读 `snapshot.next` | 抛 `AttributeError`，`notify` 在意图分类前就中断，落库代码根本不执行 |
| 2 | `INTENT_CONFIDENCE_THRESHOLD=0.80`，该邮件置信度 `0.72` → `needs_review=True`；而 `needs_review` 判断写在 `RESUME_SUBMISSION` **之前** | 走了「人工复核」分支直接 return， `_handle_resume_submission` 未执行 |

**修复 1：`_waiting_reply` 改用 `StateSnapshot`**

```python
# 修复前
snapshot = await get_checkpointer().aget_tuple({"configurable": {"thread_id": thread_id}})
return bool(snapshot and snapshot.next)   # ❌ CheckpointTuple 无 next

# 修复后
_graph = None

def _get_graph():
    """模块级缓存，避免每次邮件轮询重建图。"""
    global _graph
    if _graph is None:
        _graph = build_candidate_graph(get_checkpointer())
    return _graph

async def _waiting_reply(thread_id: str) -> bool:
    try:
        snapshot = await _get_graph().aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
    except Exception:
        logger.exception("读取 checkpoint 失败 | thread_id=%s", thread_id)
        return False
    if not snapshot:
        return False
    return bool(snapshot.next)   # StateSnapshot.next：非空=停在中断点
```

**修复 2：`RESUME_SUBMISSION` 优先于 `needs_review` 落库**

```python
# 修复前：needs_review 优先 → 低置信投递被拦截
if result.needs_review or result.intent is MailIntent.AMBIGUOUS:
    await _enqueue_human_review(email, result)
    return result
...
if result.intent is MailIntent.RESUME_SUBMISSION:   # 永远到不了
    await _handle_resume_submission(email)

# 修复后：投递先落 PENDING，再判断是否需要人工复核
if result.intent is MailIntent.RESUME_SUBMISSION:
    await _handle_resume_submission(email)
    return result

if result.needs_review or result.intent is MailIntent.AMBIGUOUS:
    await _enqueue_human_review(email, result)
    return result
```

**设计理由**：「邮件投递确认」本身就是 HR 人工确认环节，落 `PENDING` 是安全动作（不会自动建人 / 起图）。漏掉真实投递的代价远大于误落库，因此投递意图应无条件先落库。

**涉及文件**：

| 文件 | 改动 |
|---|---|
| `utils/emailbot/notify.py` | `_waiting_reply` 改用 `StateSnapshot`；`RESUME_SUBMISSION` 判断前移 |

**验证**：

```
# 重置 last_uid 让 16:24 那封邮件重跑（mailbot_state.json: 1703818728 → 1703818727）
# 重启后日志应出现：
#   [mail_submission] 已落待确认 | uid=1703818728 | from=11272664@qq.com
```

> **相关重构**：`MailSubmissionSweepManager` 的生命周期管理已抽到 `PeriodicTaskManager` 基类，
> 与 `TimeoutSweepManager` 共用同一套 init / close / 定时循环模板。
> 详见 [docs/periodic_task_refactor.md](periodic_task_refactor.md)。

