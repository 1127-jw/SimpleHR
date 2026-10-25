from langchain.agents.middleware import ModelFallbackMiddleware

from app.agents.llms import deepseek_llm, qwen_llm
from app.schemas.agent_schema import AgentCandidateScoreSchema
from app.core.db import AsyncSessionFactory
from app.models.candidate import CandidateStatusEnum
from app.repository.resume_repo import CandidateAIScoreRepo, CandidateRepo
from app.workflow.candidate_state import CandidateRecruitmentState
from langchain.agents import create_agent

async def scoring_node(state: CandidateRecruitmentState) -> dict:
    """
    AI 评分节点。
    复用 agents/candidate.py L82-L133 的 score_for_candidate 工具逻辑，
    但将其从"工具"提升为"图节点"，使评分成为显式流程步骤。
    """
    candidate = state.candidate
    position = state.position

    # 复用 agents/prompts.py L10-L52 的评分 prompt
    from agents.prompts import SCORE_FOR_CANDIDATE_SYSTEM_PROMPT, SCORE_FOR_CANDIDATE_USER_PROMPT
    from langchain_core.prompts import PromptTemplate

    score_agent = create_agent(
        model=deepseek_llm,
        system_prompt=SCORE_FOR_CANDIDATE_SYSTEM_PROMPT,
        middleware=[ModelFallbackMiddleware(first_model=qwen_llm)],
        response_format=AgentCandidateScoreSchema
    )

    user_prompt_template = PromptTemplate.from_template(SCORE_FOR_CANDIDATE_USER_PROMPT)
    user_prompt = user_prompt_template.invoke({
        "candidate": candidate.model_dump_json(),
        "position": position.model_dump_json()
    })

    response = await score_agent.ainvoke({
        "messages": [{"role": "user", "content": user_prompt.text}]
    })
    score_result: AgentCandidateScoreSchema = response["structured_response"]

    # 将评分结果写入数据库（原 tools.py L113-L128 逻辑）
    async with AsyncSessionFactory() as session:
        async with session.begin():
            score_repo = CandidateAIScoreRepo(session)
            candidate_repo = CandidateRepo(session)
            await score_repo.create_candidate_score(
                candidate_id=candidate.id,
                candidate_score_dict=score_result.model_dump()
            )
            status = (CandidateStatusEnum.AI_FILTER_PASSED
                      if score_result.overall_score > 8
                      else CandidateStatusEnum.AI_FILTER_FAILED)
            await candidate_repo.update_candidate_status(
                candidate_id=candidate.id, status=status
            )

    # 根据评分决定下一阶段
    next_stage = "email_invite" if score_result.overall_score > 8 else "rejected"

    return {
        "score_result": score_result,
        "current_stage": next_stage
    }