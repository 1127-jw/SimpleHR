from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware
from app.agents.llms import qwen_llm, deepseek_llm
from app.agents.prompts import EXTRACT_CANDIDATE_SYSTEM_PROMPT
from app.schemas.agent_schema import AgentCandidateSchema
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential


agent = create_agent(
    model=qwen_llm,
    middleware=[
        ModelFallbackMiddleware(
            first_model=deepseek_llm,
        ),
    ],
    system_prompt=EXTRACT_CANDIDATE_SYSTEM_PROMPT,
    response_format=AgentCandidateSchema,
)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def extract_candidate_info(content: str) -> AgentCandidateSchema:
    response = await agent.ainvoke({"messages": [{
        "role": "user",
        "content": f"原始文本内容为：{content}，请从原始文本中提取候选人信息成JSON格式。"
    }]})
    logger.debug(f"大模型解析候选人资料结果{response}")
    return response['structured_response']







# result = agent.invoke(
#     {"messages": [{"role": "user", "content": "你好"}]}
# )
# print(result)