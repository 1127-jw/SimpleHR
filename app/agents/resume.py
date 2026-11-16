import logging

from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware
from tenacity import retry, stop_after_attempt, wait_exponential

from app.agents.llms import qwen_llm, deepseek_llm
from app.agents.prompts import EXTRACT_CANDIDATE_SYSTEM_PROMPT
from app.schemas.agent_schema import AgentCandidateSchema
from app.agents.candidate import get_current_time
logger = logging.getLogger(__name__)

_agent = None


def _get_agent():
    """懒加载 Agent：避免在 import 期构造对象（导入副作用）。"""
    global _agent
    if _agent is None:
        _agent = create_agent(
            model=deepseek_llm,
            middleware=[
                ModelFallbackMiddleware(
                    first_model=qwen_llm,
                ),
            ],
            tools=[get_current_time],
            system_prompt=EXTRACT_CANDIDATE_SYSTEM_PROMPT,
            response_format=AgentCandidateSchema,
        )
    return _agent


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def extract_candidate_info(content: str) -> AgentCandidateSchema:
    response = await _get_agent().ainvoke({"messages": [{
        "role": "user",
        "content": f"原始文本内容为：{content}，请从原始文本中提取候选人信息成JSON格式。"
    }]})
    logger.debug("大模型解析候选人资料结果%s", response)
    return response['structured_response']


# result = agent.invoke(
#     {"messages": [{"role": "user", "content": "你好"}]}
# )
# print(result)