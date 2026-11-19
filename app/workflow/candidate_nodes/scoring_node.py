import logging

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import RunnableWithFallbacks

from app.agents.llms import get_deepseek_llm, get_qwen_llm
from app.agents.prompts import (
    SCORE_FOR_CANDIDATE_SYSTEM_PROMPT,
    SCORE_FOR_CANDIDATE_USER_PROMPT,
)
from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.core.exceptions import LLMServiceError
from app.models.candidate import CandidateStatusEnum
from app.repository.candidate_history_repo import CandidateStatusHistoryRepo
from app.repository.resume_repo import CandidateAIScoreRepo, CandidateRepo
from app.schemas.agent_schema import AgentCandidateScoreSchema
from app.workflow.candidate_state import CandidateRecruitmentState
from app.workflow.error_handler import node_error_wrapper

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


def _build_score_llm() -> RunnableWithFallbacks:
    """构建带 fallback 的结构化评分 LLM。

    deepseek 失败时自动降级到 qwen，保持与旧 Agent ModelFallbackMiddleware 等价的容错能力。
    """
    primary = get_deepseek_llm().with_structured_output(AgentCandidateScoreSchema)
    fallback = get_qwen_llm().with_structured_output(AgentCandidateScoreSchema)
    return RunnableWithFallbacks(runnable=primary, fallbacks=[fallback])


def _build_score_messages(candidate, position) -> list:
    """构造评分用的 messages（system + user）。"""
    user_content = SCORE_FOR_CANDIDATE_USER_PROMPT.format(
        position=position.model_dump_json(),
        candidate=candidate.model_dump_json(),
    )
    return [
        SystemMessage(content=SCORE_FOR_CANDIDATE_SYSTEM_PROMPT),
        HumanMessage(content=user_content),
    ]


@node_error_wrapper
async def scoring_node(state: CandidateRecruitmentState) -> dict:
    """AI 评分节点（直接 LLM 调用，不再走 Agent 循环）。

    用 ``with_structured_output`` 让模型直接产出 ``AgentCandidateScoreSchema``，
    相比 ``create_agent`` 少了一层 Agent 循环开销（无工具调用、无多轮推理）。

    阈值从 ``state["score_pass_threshold"]`` 读取，默认取 Settings.SCORE_PASS_THRESHOLD。
    """
    candidate = state["candidate"]
    position = state["position"]
    cid = candidate.id
    threshold = state.get("score_pass_threshold", 7)

    logger.info("[scoring] start | candidate_id=%s | threshold=%d", cid, threshold)

    # 跳过评分：不调用 LLM、不写 AI 评分记录、不改状态。
    # 路由（_route_after_scoring）看到 skip_scoring 会直达 email_invite。
    # 副作用：``/candidate/ai-score/{id}`` 之后会返回 400「评分不存在」——预期行为。
    if state.get("skip_scoring"):
        logger.info("[scoring] skip_scoring=True，跳过评分 | candidate_id=%s", cid)
        return {"current_stage": "email_invite"}

    llm = _build_score_llm()
    messages = _build_score_messages(candidate, position)

    try:
        from app.core.limiters import llm_slot

        async with llm_slot("llm:score"):
            score_result: AgentCandidateScoreSchema = await llm.ainvoke(messages)
    except Exception as e:
        logger.error("[scoring] LLM call failed | candidate_id=%s | error=%s", cid, e)
        raise LLMServiceError(f"AI 评分调用失败: {e}") from e

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
                await score_repo.upsert_candidate_score(
                    candidate_id=cid,
                    candidate_score_dict=score_result.model_dump(),
                )
                # 评分达标**直接进人工确认**：不让「AI筛选成功」作为一个
                # 需要人工再去点一次的中间态存在（HR 只需要在闸门拍板）。
                # 评分结论仍完整保留在状态变更链里（from_status / to_status）。
                status = (
                    CandidateStatusEnum.PENDING_MANUAL_DECISION
                    if score_result.overall_score > threshold
                    else CandidateStatusEnum.AI_FILTER_FAILED
                )
                await candidate_repo.update_candidate_status(
                    candidate_id=cid, status=status,
                )
                # 状态变更链：AI 筛选的结论（成功/失败）是可追溯的关键节点。
                # 达标时链上记的是「AI筛选成功」——它是评分的**结论**，
                # 而 candidates.status 已经进入下一环「待人工确认」。
                await CandidateStatusHistoryRepo(session).record_change(
                    cid,
                    to_status=(
                        CandidateStatusEnum.AI_FILTER_PASSED.value
                        if score_result.overall_score > threshold
                        else CandidateStatusEnum.AI_FILTER_FAILED.value
                    ),
                    from_status=str(getattr(candidate, "status", "") or None),
                    candidate_email=getattr(candidate, "email", None),
                    position_id=getattr(position, "id", None),
                    position_title=getattr(position, "title", None),
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
