import logging

from langgraph.errors import GraphRecursionError

from app.core.config import get_settings
from app.core.exceptions import AppException, ExternalServiceError
from app.agents.candidate import CandidateProcessAgent
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from app.core.checkpointer import get_checkpointer
from app.service.candidate_status_ser import mark_candidate_error
from app.workflow.candidate_graph import build_candidate_graph

logger = logging.getLogger(__name__)

settings = get_settings()

# 协商轮次上限默认 3，每轮约 2 个超步（等回复 + 校验），留出确认/落库等收尾节点。
# 显式设上限而不依赖 LangGraph 默认 25：环形路由一旦再次出现，
# 让它以 GraphRecursionError 暴露并落到 PROCESSING_ERROR，而不是静默空转。
GRAPH_RECURSION_LIMIT = 40


async def _mark_processing_error(candidate_id: str, error_msg: str) -> None:
    """向后兼容的别名；实现见 ``candidate_status_ser.mark_candidate_error``。"""
    await mark_candidate_error(candidate_id, error_msg)


async def run_candidate_agent(
    candidate: CandidateSchema,
    position: PositionSchema,
    interviewer: UserSH,
    *,
    score_pass_threshold: int | None = None,
    max_negotiation_rounds: int | None = None,
):
    """执行候选人招聘流程 Agent（任务级并发限流）。

    实际逻辑在 ``_run_candidate_agent_impl``；此处统一占用 "recruit" 名额，
    与候选人回复 / 人工重试 / 超时巡检共用同一并发额度。
    """
    from app.core.limiters import task_slot

    async with task_slot("recruit", settings.RECRUIT_TASK_MAX_CONCURRENCY):
        return await _run_candidate_agent_impl(
            candidate, position, interviewer,
            score_pass_threshold=score_pass_threshold,
            max_negotiation_rounds=max_negotiation_rounds,
        )


async def _run_candidate_agent_impl(
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
                f"[agent_task] candidate_id={candidate.id} | infra_error={e.message} | code={e.code}",
                exc_info=True,
            )
            await _mark_processing_error(candidate.id, e.message)
            return None
    else:
        # 图模式：使用 LangGraph 状态图
        try:
            # 检查点存储是进程级单例，直接取用（不再每次新建连接）
            checkpointer = get_checkpointer()

            # 启动新流程前清除旧的 checkpoint，避免重放历史错误状态
            # 注意：notify.py 中通过 Command(resume=...) 恢复流程时不受影响
            await checkpointer.adelete_thread(candidate.email)

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
                config={
                    "configurable": {"thread_id": candidate.email},
                    "recursion_limit": GRAPH_RECURSION_LIMIT,
                },
            )

            # 检查最终状态是否有错误
            final_stage = res.get("current_stage")
            if final_stage == "error":
                error_msg = res.get("error_message", "未知错误")
                logger.warning(
                    f"[agent_task] candidate_id={candidate.id} | final_stage=error | error={error_msg}"
                )
                # 注意：error_node 已经更新了候选人状态，这里只需记录日志
            else:
                logger.info(
                    f"[agent_task] candidate_id={candidate.id} | final_stage={final_stage} | success"
                )

            return res

        except GraphRecursionError as e:
            # 路由环形迭代的兜底：正常流程不应走到这里。
            logger.error(
                f"[agent_task] candidate_id={candidate.id} | recursion_limit={GRAPH_RECURSION_LIMIT} 耗尽 | {e}",
                exc_info=True,
            )
            await _mark_processing_error(
                candidate.id, f"状态图迭代超出上限（{GRAPH_RECURSION_LIMIT}），疑似路由死循环"
            )
            return None
        except (ExternalServiceError, AppException) as e:
            # 注意：不 re-raise！BackgroundTasks 会吞掉异常，raise 毫无意义。
            logger.error(
                f"[agent_task] candidate_id={candidate.id} | infra_error={e.message} | code={e.code}",
                exc_info=True,
            )
            await _mark_processing_error(candidate.id, e.message)
            return None
