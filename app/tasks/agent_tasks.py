from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.core.exceptions import AppException, ExternalServiceError
from app.agents.candidate import CandidateProcessAgent
from app.models.candidate import CandidateStatusEnum
from app.repository.resume_repo import CandidateRepo
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from loguru import logger

from app.core.checkpointer import get_checkpointer
from app.workflow.candidate_graph import build_candidate_graph

settings = get_settings()


async def _mark_processing_error(candidate_id: str, error_msg: str) -> None:
    """将候选人状态标记为处理异常。

    独立函数，确保即使主流程异常也能尽力更新状态。
    """
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                repo = CandidateRepo(session)
                await repo.update_candidate_status(
                    candidate_id=candidate_id,
                    status=CandidateStatusEnum.PROCESSING_ERROR,
                )
        logger.info(f"候选人 {candidate_id} 已标记为处理异常")
    except Exception as e:
        logger.critical(
            f"标记候选人 {candidate_id} 处理异常状态失败: {e}",
            exc_info=True,
        )


async def run_candidate_agent(
    candidate: CandidateSchema,
    position: PositionSchema,
    interviewer: UserSH,
    *,
    score_pass_threshold: int | None = None,
    max_negotiation_rounds: int | None = None,
):
    """执行候选人招聘流程 Agent。

    异常处理策略：
    - ``BusinessException``（业务异常）→ 由节点内部的 ``node_error_wrapper`` 捕获，
      状态图流转到 error 节点，候选人状态更新为 ``PROCESSING_ERROR``。
    - ``ExternalServiceError``（基础设施异常）→ 由本函数捕获，更新候选人状态为
      ``PROCESSING_ERROR`` 并记录错误日志。**不重新抛出**，避免被 BackgroundTasks 吞掉。
    - ``AppException``（其他应用异常）→ 同上。

    注意：本函数由 BackgroundTasks 调用，re-raise 的异常会被 FastAPI 静默丢弃。
    因此所有异常必须在此函数内**完全处理**（记录日志 + 更新状态），不得向上传播。

    Args:
        candidate: 候选人信息。
        position: 职位信息。
        interviewer: 面试官信息。
        score_pass_threshold: AI 评分通过阈值，默认取 Settings。
        max_negotiation_rounds: 协商轮次上限，默认取 Settings。

    Returns:
        正常结束时返回状态图结果 dict；基础设施异常结束时返回 None。
    """
    if not settings.GRAPH_OPEN:
        # 非图模式：使用 CandidateProcessAgent（老流程）
        try:
            async with CandidateProcessAgent(
                candidate=candidate,
                position=position,
                interviewer=interviewer,
            ) as agent:
                res = await agent.ainvoke(
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                f"候选人信息：{candidate.model_dump_json()},"
                                f"职位信息：{position.model_dump_json()}"
                            ),
                        }
                    ],
                    thread_id=candidate.email,
                )
                logger.debug(res)
            return res
        except (ExternalServiceError, AppException) as e:
            # 注意：不 re-raise！BackgroundTasks 会吞掉异常，raise 毫无意义。
            logger.error(
                "[agent_task] candidate_id=%s | infra_error=%s | code=%s | trace=%s",
                candidate.id, e.message, e.code,
                exc_info=True,
            )
            await _mark_processing_error(candidate.id, e.message)
            return None
    else:
        # 图模式：使用 LangGraph 状态图
        try:
            async with get_checkpointer() as checkpointer:
                candidate_graph = build_candidate_graph(
                    checkpointer,
                    score_pass_threshold=score_pass_threshold,
                    max_negotiation_rounds=max_negotiation_rounds,
                )
                res = await candidate_graph.ainvoke(
                    {
                        "candidate": candidate,
                        "position": position,
                        "interviewer": interviewer,
                        "current_stage": "screening",
                        "score_pass_threshold": score_pass_threshold or settings.SCORE_PASS_THRESHOLD,
                        "max_negotiation_rounds": max_negotiation_rounds or settings.MAX_NEGOTIATION_ROUNDS,
                    },
                    config={"configurable": {"thread_id": candidate.email}},
                )

            # 检查最终状态是否有错误
            final_stage = res.get("current_stage")
            if final_stage == "error":
                error_msg = res.get("error_message", "未知错误")
                logger.warning(
                    "[agent_task] candidate_id=%s | final_stage=error | error=%s",
                    candidate.id, error_msg,
                )
                # 注意：error_node 已经更新了候选人状态，这里只需记录日志
            else:
                logger.info(
                    "[agent_task] candidate_id=%s | final_stage=%s | success",
                    candidate.id, final_stage,
                )

            return res

        except (ExternalServiceError, AppException) as e:
            # 注意：不 re-raise！BackgroundTasks 会吞掉异常，raise 毫无意义。
            logger.error(
                "[agent_task] candidate_id=%s | infra_error=%s | code=%s | trace=%s",
                candidate.id, e.message, e.code,
                exc_info=True,
            )
            await _mark_processing_error(candidate.id, e.message)
            return None
