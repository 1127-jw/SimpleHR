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
        candidate_schema = CandidateSchema.model_validate(candidate)
        position_schema = PositionSchema.model_validate(candidate.position)
        interviewer_schema = UserSH.model_validate(candidate.creator)
        return candidate_schema, position_schema, interviewer_schema


async def notify(email: ParsedEmail, rule) -> None:
    """邮件命中规则后的回调：驱动招聘流程继续执行。

    Args:
        email: 已解析的邮件。
        rule: 命中的规则（``MailRule``）。
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
            candidate_graph = build_candidate_graph(get_checkpointer())
            await candidate_graph.ainvoke(
                Command(
                    resume=f"候选人回复了以下邮件，请根据回复内容决定下一步操作：\n\n主题：{email.subject}\n\n内容：{email.text}",
                    update={
                        # 刷新候选人/职位/面试官信息（防止检查点中的旧数据）
                        "candidate": candidate_schema,
                        "position": position_schema,
                        "interviewer": interviewer_schema,
                    },
                ),
                config={"configurable": {"thread_id": email.from_.address}}
            )


    else:
        # 兜底：即使规则没命中，只要候选人在等待面试协商，也恢复流程
        # 候选人回复"确认"等简短内容时，可能不命中"询问面试时间或进度"规则
        await _handle_candidate_reply(email)


async def _handle_candidate_reply(email: ParsedEmail) -> None:
    """兜底处理：检查候选人是否在等待面试协商，如果是则恢复流程。

    场景：候选人回复"确认"、"好的"、"我明天下午3点方便"等简短内容时，
    可能不命中"询问面试时间或进度"规则，但流程需要恢复。
    """
    # 只处理图模式
    if not settings.GRAPH_OPEN:
        return

    # 查找候选人
    candidate_schema, position_schema, interviewer_schema = await _find_candidate_by_email(email.from_.address)
    if not candidate_schema:
        return

    # 只处理等待面试协商的状态
    if candidate_schema.status not in (
        CandidateStatusEnum.MANUAL_PASSED,
        CandidateStatusEnum.AI_FILTER_PASSED,
    ):
        return

    logger.info(
        "[candidate_reply] 兜底恢复: 来自 %s, 状态 %s",
        email.from_.address, candidate_schema.status,
    )

    # 恢复流程
    candidate_graph = build_candidate_graph(get_checkpointer())
    await candidate_graph.ainvoke(
        Command(
            resume=f"候选人回复了以下邮件，请根据回复内容决定下一步操作：\n\n主题：{email.subject}\n\n内容：{email.text}",
            update={
                # 刷新候选人/职位/面试官信息（防止检查点中的旧数据）
                "candidate": candidate_schema,
                "position": position_schema,
                "interviewer": interviewer_schema,
            },
        ),
        config={"configurable": {"thread_id": email.from_.address}},
    )


