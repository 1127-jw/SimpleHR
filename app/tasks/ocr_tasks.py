from redis.asyncio import Redis
from app.schemas.candidate_schema import TaskInfoSchema
from app.schemas.agent_schema import AgentCandidateSchema
from utils.ocr_service import PaddleOcr
from app.agents.resume import extract_candidate_info


async def ocr_parse_resume_task(
        file_path:str,
        r:Redis,
        task_id:str,
        key:str,
):
    import logging
    logger = logging.getLogger(__name__)

    try:
        logger.info("[OCR] 开始处理任务: %s", task_id)
        paddle_ocr = PaddleOcr()
        job_id = await paddle_ocr.create_job(file_path)
        logger.info("[OCR] 任务已创建: job_id=%s", job_id)
        jsonl_url = await paddle_ocr.poll_for_state(job_id)
        logger.info("[OCR] 解析完成: jsonl_url=%s", jsonl_url)
        contents = await paddle_ocr.fetch_parsed_contents(jsonl_url)
        content = "\n\n".join(contents)
        logger.info("[OCR] 内容长度: %s", len(content))
        # 2. 设置当前状态为done
        candidate_info: AgentCandidateSchema = await extract_candidate_info(content)
        logger.info("[OCR] LLM 解析完成: name=%s", candidate_info.name)
        task_info = TaskInfoSchema(task_id=task_id, status="done", result=candidate_info)
        await r.set(key, task_info.model_dump_json(), ex=30 * 60)
        logger.info("[OCR] 任务状态已更新为 done")
    except Exception as e:
        # 3. 如果出现了异常，就把状态设置failed
        logger.error("[OCR] 任务失败: %s", e, exc_info=True)
        task_info = TaskInfoSchema(task_id=task_id, status="failed", error=str(e))
        await r.set(key, task_info.model_dump_json(), ex=30 * 60)

