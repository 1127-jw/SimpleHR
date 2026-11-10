import logging

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from utils.emailbot.models import ParsedEmail

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

async def _find_candidate_by_email(email_address: str):
    """根据邮箱地址查找候选人"""
    async with AsyncSessionFactory() as session:
        candidate_repo = CandidateRepo(session)
        # 预加载所有关联对象，并在 session 内转为 Pydantic Schema
        candidate = await candidate_repo.get_by_email(email_address)
        if not candidate:
            return None, None, None
        # 在 session 内访问关联对象，避免 DetachedInstanceError
        candidate_schema = CandidateSchema.model_validate(candidate)
        position_schema = PositionSchema.model_validate(candidate.position)
        interviewer_schema = UserSH.model_validate(candidate.creator)
        return candidate_schema, position_schema, interviewer_schema


async def notify(email: ParsedEmail, rule) -> None:
    """邮件命中规则后的回调：驱动招聘流程继续执行。

    Args:
        email: 已解析的邮件。
        rule: 命中的规则（``MailRule``）。

    当前只处理「询问面试时间或进度」：

    - ``GRAPH_OPEN=False``：走旧 Agent 模式（``CandidateProcessAgent``）
    - ``GRAPH_OPEN=True``：走 LangGraph 状态图，用 ``Command(resume=...)``
      恢复 ``get_candidate_email_reply`` 处中断的流程
    """
    graph = settings.GRAPH_OPEN
    if rule.name == "简历投递":
        # TODO 简历上传完成后调用CandidateProcessAgent进行解析、评分、面试时间初步确定等动作
        pass

    elif rule.name == "询问面试时间或进度":
        from app.agents.candidate import CandidateProcessAgent
        logger.info("询问面试时间/进度: 来自 %s - %s", email.from_.address, email.subject)
        candidate_schema, position_schema, interviewer_schema = await _find_candidate_by_email(email.from_.address)
        if not candidate_schema:
            logger.warning("未找到对应的候选人: %s", email.from_.address)
            return
        if not graph:
            async with CandidateProcessAgent(
                candidate=candidate_schema,
                position=position_schema,
                interviewer=interviewer_schema,
            ) as agent:
                # 使用候选人ID作为thread_id，恢复之前的对话历史（包含提议的面试时间）
                resp = await agent.ainvoke(
                    messages=[HumanMessage(content=f"候选人回复了以下邮件，请根据回复内容决定下一步操作：\n\n主题：{email.subject}\n\n内容：{email.text}")],
                    thread_id=email.from_.address,
                )
                logger.info("面试时间安排完成: %s", resp)
        else:
            async with get_checkpointer() as checkpointer:
                candidate_graph = build_candidate_graph(checkpointer)
                result = await candidate_graph.ainvoke(
                    Command(resume=f"候选人回复了以下邮件，请根据回复内容决定下一步操作：\n\n主题：{email.subject}\n\n内容：{email.text}"),
                    config={"configurable": {"thread_id": email.from_.address}}
                )


    else:
        # TODO 为命中规则时，用便宜模型进行解析，分为简历投递则和询问面试时间或进度，若都不是则为垃圾邮件
        pass


