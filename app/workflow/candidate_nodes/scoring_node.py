import logging
from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware
from langchain_core.prompts import ChatPromptTemplate

logger = logging.getLogger(__name__)

from app.agents.llms import deepseek_llm, qwen_llm
from app.agents.prompts import (
    SCORE_FOR_CANDIDATE_SYSTEM_PROMPT,
    SCORE_FOR_CANDIDATE_USER_PROMPT,
)
from app.core.db import AsyncSessionFactory
from app.core.exceptions import LLMServiceError
from app.models.candidate import CandidateStatusEnum
from app.repository.resume_repo import CandidateAIScoreRepo, CandidateRepo
from app.schemas.agent_schema import AgentCandidateScoreSchema
from app.workflow.candidate_state import CandidateRecruitmentState
from app.workflow.error_handler import node_error_wrapper

# 模块级单例：避免每次调用 scoring_node 都重建 Agent（与 check_availability_node 的 _proposed_time_agent 一致）
_score_agent = create_agent(
    model=deepseek_llm,
    system_prompt=SCORE_FOR_CANDIDATE_SYSTEM_PROMPT,
    middleware=[ModelFallbackMiddleware(first_model=qwen_llm)],
    response_format=AgentCandidateScoreSchema,
)

_score_user_prompt_template = ChatPromptTemplate.from_messages([
    ("user", SCORE_FOR_CANDIDATE_USER_PROMPT)
])


@node_error_wrapper
async def scoring_node(state: CandidateRecruitmentState) -> dict:
    """
    AI 评分节点。
    复用 agents/candidate.py L82-L133 的 score_for_candidate 工具逻辑，
    但将其从"工具"提升为"图节点"，使评分成为显式流程步骤。

    阈值从 ``state["score_pass_threshold"]`` 读取，默认取 Settings.SCORE_PASS_THRESHOLD。
    """
    candidate = state["candidate"]
    position = state["position"]
    cid = candidate.id
    threshold = state.get("score_pass_threshold", 7)

    logger.info("[scoring] start | candidate_id=%s | threshold=%d", cid, threshold)

    messages = _score_user_prompt_template.invoke({
        "candidate": candidate.model_dump_json(),
        "position": position.model_dump_json(),
    })

    try:
        response = await _score_agent.ainvoke({
            "messages": messages.to_messages(),
        })
    except Exception as e:
        logger.error("[scoring] LLM call failed | candidate_id=%s | error=%s", cid, e)
        raise LLMServiceError(f"AI 评分调用失败: {e}") from e

    score_result: AgentCandidateScoreSchema = response["structured_response"]

    logger.info(
        "[scoring] LLM scored | candidate_id=%s | overall_score=%d",
        cid, score_result.overall_score,
    )

    # 将评分结果写入数据库
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                score_repo = CandidateAIScoreRepo(session)
                candidate_repo = CandidateRepo(session)
                await score_repo.create_candidate_score(
                    candidate_id=cid,
                    candidate_score_dict=score_result.model_dump(),
                )
                status = (
                    CandidateStatusEnum.AI_FILTER_PASSED
                    if score_result.overall_score > threshold
                    else CandidateStatusEnum.AI_FILTER_FAILED
                )
                await candidate_repo.update_candidate_status(
                    candidate_id=cid, status=status,
                )
    except LLMServiceError:
        raise
    except Exception as e:
        logger.error("[scoring] DB write failed | candidate_id=%s | error=%s", cid, e)
        raise LLMServiceError(f"AI 评分写入数据库失败: {e}") from e

    logger.info("[scoring] score saved to DB | candidate_id=%s", cid)

    passed = score_result.overall_score > threshold
    next_stage = "email_invite" if passed else "rejected"
    logger.info(
        "[scoring] done | candidate_id=%s | score=%d | next_stage=%s",
        cid, score_result.overall_score, next_stage,
    )

    return {
        "score_result": score_result,
        "current_stage": next_stage,
    }
