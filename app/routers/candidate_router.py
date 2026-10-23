from fastapi import APIRouter, Depends, UploadFile, File, BackgroundTasks
from typing import Annotated, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import get_db
from app.core.auth import get_current_user
from app.core.config import get_settings
from app.models.user import UserModel
from uuid import uuid4
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH


from app.schemas.candidate_schema import ResumeUploadRespSchema, ResumePaseSchema, ResumeParseTaskRespSchema, \
    TaskInfoSchema,CandidateCreateSchema
from app.service.candidate_ser import resume_upload_service, parse_resume_service, create_candidate_ser

from redis.asyncio import Redis
from app.core.redis_client import get_redis
from app.schemas.response_schema import ResponseSchema
from app.repository.position_repo import PositionRepo
from app.repository.resume_repo import CandidateRepo
from app.repository.user_repo import UserRepo
from app.tasks.agent_tasks import run_candidate_agent

settings = get_settings()


router = APIRouter(prefix="/candidate", tags=["candidate"])

@router.post("/resume/upload", summary="上传简历", response_model=ResumeUploadRespSchema)
async def resume_upload(
    session:Annotated[AsyncSession, Depends(get_db)],
    current_user:Annotated[UserModel,Depends(get_current_user)],
    file: UploadFile = File(...),
):
    resume = await resume_upload_service(session,file,current_user)
    return {"resume": resume}

@router.post("/resume/parse", summary="简历解析", response_model=ResumeParseTaskRespSchema)
async def parse_resume(
    resume_data: ResumePaseSchema,

    background_tasks: BackgroundTasks,
    session:Annotated[AsyncSession, Depends(get_db)],
    _: UserModel = Depends(get_current_user),
    r: Redis = Depends(get_redis),
):
    # 创建一个识别简历的后台任务
    task_id = str(uuid4())
    await parse_resume_service(
        session,
        r,
        resume_id=resume_data.resume_id,
        task_id=task_id,
        background_tasks=background_tasks
    )
    return {"task_id": task_id}

@router.get("/resume/parse/{task_id}", summary="获取任务状态", response_model=TaskInfoSchema)
async def get_task_status(
    task_id: str,
    r: Redis = Depends(get_redis),
    _: UserModel = Depends(get_current_user)
):
    key = f"task:{task_id}"
    task_json = await r.get(key)
    if not task_json:
        return None
    # model_validate、model_validate_json 从 Python字典、ORM对象、JSON 字符串直接验证并生成模型实例
    # model_dump_json、model_dump 把模型实例直接序列化成 JSON 字符串、python字典的方法
    task_info = TaskInfoSchema.model_validate_json(task_json)

    return task_info

@router.post("/create",summary="创建候选人")
async def create_candidate(
    candidate_data:CandidateCreateSchema,
    session:Annotated[AsyncSession, Depends(get_db)],
    current_user:Annotated[UserModel,Depends(get_current_user)],
):
    candidate = await create_candidate_ser(session,current_user,candidate_data)

    return ResponseSchema()


@router.get("/agent/test")
async def agent_test(
        session:Annotated[AsyncSession, Depends(get_db)],
        background_tasks: BackgroundTasks,
):
    async with session.begin():
        candidate_repo = CandidateRepo(session)
        position_repo = PositionRepo(session)
        user_repo = UserRepo(session)
        #获取ORM模型对象
        candidate_model = await candidate_repo.get_by_id("fhzsuTctybq4Env92YuR9b")
        position_model =  await position_repo.get_by_id("RBk6bC648aS7EmK6gcARbD")
        interviewer_model = await user_repo.get_by_id("9iqEJnR8iZwJSHLxoHcQke")
        #把ORM模型对象转成schema对象
        candidate = CandidateSchema.model_validate(candidate_model)
        position = PositionSchema.model_validate(position_model)
        interviewer = UserSH.model_validate(interviewer_model)

        background_tasks.add_task(run_candidate_agent, candidate, position, interviewer)

        return {"result": "success"}

