import logging
from app.core.config import get_settings
from app.workflow.candidate_state import CandidateRecruitmentState
from app.workflow.error_handler import node_error_wrapper

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


@node_error_wrapper
async def screening_node(state: CandidateRecruitmentState) -> dict:
    """硬性条件筛查——纯规则判断，不调用 LLM。"""
    candidate = state["candidate"]
    position = state["position"]
    cid = candidate.id

    logger.info("[screening] start | candidate_id=%s", cid)

    # 规则 1：最低工作年限
    if candidate.work_year < position.work_year:
        logger.info(
            "[screening] rejected: work_year insufficient | "
            "candidate_id=%s | required=%d, actual=%d",
            cid, position.work_year, candidate.work_year,
        )
        return {
            "screening_passed": False,
            "screening_reason": f"工作年限不足：要求 {position.work_year} 年，实际 {candidate.work_year} 年",
            "current_stage": "rejected",
        }

    logger.info("[screening] passed | candidate_id=%s", cid)
    return {"screening_passed": True, "current_stage": "scoring"}
