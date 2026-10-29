from app.workflow.candidate_state import CandidateRecruitmentState


async def screening_node(state: CandidateRecruitmentState) -> dict:
    """
    硬性条件筛查——纯规则判断，不调用 LLM。
    替代原 prompt 中隐含的"先看看候选人是否符合基本要求"逻辑。
    """
    candidate = state["candidate"]
    position = state["position"]

    # 规则 1：最低工作年限
    if candidate.work_year < position.work_year:
        return {
            "screening_passed": False,
            "screening_reason": f"工作年限不足：要求 {position.work_year} 年，实际 {candidate.work_year} 年",
            "current_stage": "rejected"
        }

    # TODO 其他要求

    return {
        "screening_passed": True,
        "current_stage": "scoring"
    }
