from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.candidate import ResumeModel
from app.schemas.candidate_schema import TaskInfoSchema
from app.schemas.agent_schema import AgentCandidateSchema
from utils.ORC_ser import PaddleOcr
from loguru import logger
from app.agents.resume import extract_candidate_info


async def ocr_parse_resume_task(
        file_path:str,
        r:Redis,
        task_id:str,
        key:str,
):

    try:
        paddle_ocr = PaddleOcr()
        job_id = await paddle_ocr.create_job(file_path)
        jsonl_url = await paddle_ocr.poll_for_state(job_id)
        contents = await paddle_ocr.fetch_parsed_contents(jsonl_url)
        content = "\n\n".join(contents)
        logger.debug(f"解析后文件为{content}")
        # 2. 设置当前状态为done
        candidate_info: AgentCandidateSchema = await extract_candidate_info(content)
        task_info = TaskInfoSchema(task_id=task_id, status="done", result=candidate_info)
        await r.set(key, task_info.model_dump_json(), ex=30 * 60)
    except Exception as e:
        # 3. 如果出现了异常，就把状态设置failed
        logger.error(f"PaddleOCR识别失败：{e}")
        task_info = TaskInfoSchema(task_id=task_id, status="failed", error=str(e))
        await r.set(key, task_info.model_dump_json(), ex=30 * 60)

