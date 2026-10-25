import os

from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, UploadFile, File, BackgroundTasks, Query, HTTPException
from typing import Annotated, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import get_db
from app.core.auth import get_current_user
from app.core.config import get_settings
from app.models.user import UserModel
from uuid import uuid4
from app.schemas.candidate_schema import CandidateListSchema, CandidateStatusUpdateSchema, CandidateAIScoreRespSchema
from loguru import logger

from app.schemas.candidate_schema import ResumeUploadRespSchema, ResumePaseSchema, ResumeParseTaskRespSchema, \
    TaskInfoSchema,CandidateCreateSchema
from app.service.candidate_ser import resume_upload_service, parse_resume_service, create_candidate_ser

from redis.asyncio import Redis
from app.core.redis_client import get_redis
from app.schemas.response_schema import ResponseSchema
from app.repository.resume_repo import CandidateRepo, CandidateAIScoreRepo
from app.tasks.agent_tasks import run_candidate_agent
from app.models.candidate import CandidateStatusEnum
from app.models import InterviewModel
from app.models.InterviewModel import InterviewResultEnum
from app.repository.Interview_repo import InterviewRepo
from utils.ORC_ser import PaddleOcr
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
    background_tasks: BackgroundTasks,
):
    candidate_schema, position_schema, interviewer_schema= await create_candidate_ser(session,current_user,candidate_data)

    background_tasks.add_task(
        run_candidate_agent,
        candidate=candidate_schema,
        position=position_schema,
        interviewer=interviewer_schema,
    )
    return ResponseSchema()

@router.get("/list", summary="获取候选人列表", response_model=CandidateListSchema)
async def get_candidate_list(
    session:Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(1, description="页码"),
    size: int = Query(10, description="每页的数量"),
    position_id: str|None = Query(None, description="职位的ID"),
    status: CandidateStatusEnum|None = Query(None, description="候选人状态"),
    current_user: UserModel = Depends(get_current_user),
):
    async with session.begin():
        candidate_repo = CandidateRepo(session=session)
        candidates = await candidate_repo.get_list(
            current_user=current_user,
            position_id=position_id,
            status=status,
            page=page,
            size=size,
        )
        return {"candidates": candidates}

@router.patch("/{candidate_id}/status", summary="更新候选人状态", response_model=ResponseSchema)
async def update_candidate_status(
    session:Annotated[AsyncSession, Depends(get_db)],
    candidate_id: str,
    status_data: CandidateStatusUpdateSchema,
    current_user: UserModel = Depends(get_current_user),
):
    async with session.begin():
        candidate_repo = CandidateRepo(session)
        interview_repo = InterviewRepo(session)
        candidate = await candidate_repo.get_by_id(candidate_id)
        if not candidate:
            raise HTTPException(status_code=404, detail="候选人不存在")
        # 更改状态，状态只能从前往后流转
        status_flow = [
            CandidateStatusEnum.APPLICATION,
            CandidateStatusEnum.AI_FILTER_FAILED,
            CandidateStatusEnum.AI_FILTER_PASSED,
            CandidateStatusEnum.WAITING_FOR_INTERVIEW,
            CandidateStatusEnum.REFUSED_INTERVIEW,
            CandidateStatusEnum.INTERVIEW_PASSED,
            CandidateStatusEnum.INTERVIEW_REJECTED,
            CandidateStatusEnum.HIRED,
            CandidateStatusEnum.REJECTED,
        ]
        try:
            current_idx = status_flow.index(candidate.status)
            target_idx = status_flow.index(status_data.status)
        except ValueError:
            raise HTTPException(status_code=400, detail="非法的候选人状态")

        if target_idx <= current_idx:
            raise HTTPException(status_code=400, detail="候选人状态只能往后流转")

        if status_data.status == CandidateStatusEnum.WAITING_FOR_INTERVIEW:
            if not status_data.interview_time:
                raise HTTPException(status_code=400, detail="变更为待面试时必须填写面试时间")
            await interview_repo.create_interview(
                dict(
                    scheduled_time=status_data.interview_time,
                    candidate_id=candidate_id,
                    interviewer_id=current_user.id,
                )
            )
        elif status_data.status == CandidateStatusEnum.INTERVIEW_REJECTED:
            if not status_data.rejection_reason:
                raise HTTPException(status_code=400, detail="变更为面试未通过时必须填写未通过原因")
            # 如果面试失败了，一般来讲，应该是之前就已经创建过一个面试记录了
            interview: InterviewModel = await interview_repo.get_by_candidate_id(candidate_id)
            if interview is not None:
                await interview_repo.update_interview(
                    interview_id=interview.id,
                    interview_dict={
                        "feeback": status_data.rejection_reason,
                        "result": InterviewResultEnum.FAILED,
                    }
                )
            else:
                await interview_repo.create_interview(
                    dict(
                        scheduled_time=status_data.interview_time,
                        feedback=status_data.rejection_reason,
                        result=InterviewResultEnum.FAILED,
                        candidate_id=candidate_id,
                        interviewer_id=current_user.id,
                    )
                )

        # 更新候选人状态
        await candidate_repo.update_candidate_status(candidate_id=candidate_id, status=status_data.status)
        return ResponseSchema()



@router.get("/ai-score/{candidate_id}", summary="获取候选人AI得分", response_model=CandidateAIScoreRespSchema)
async def get_candidate_ai_score(
    candidate_id: str,
    session:Annotated[AsyncSession, Depends(get_db)],
    _: UserModel = Depends(get_current_user),
):
    async with session.begin():
        score_repo = CandidateAIScoreRepo(session)
        ai_score = await score_repo.get_by_candidate_id(candidate_id)
        if not ai_score:
            raise HTTPException(status_code=400, detail="候选人的AI评分不存在！")
        return {"ai_score": ai_score}


@router.get("/candidate/7d", summary="获取最近7天系统中新增候选人的数量")
async def get_7d_candidate(
    session:Annotated[AsyncSession, Depends(get_db)],
    _: UserModel = Depends(get_current_user),
):
    async with session.begin():
        candidate_repo = CandidateRepo(session)
        now = datetime.now()
        end_time = datetime(year=now.year, month=now.month, day=now.day)
        start_time = end_time - timedelta(days=6)
        rows = await candidate_repo.candidate_count(
            start_time=start_time,
            end_time=end_time,
        )
        day_counts = {day: count for day, count in rows}
        result = []
        for x in range(7):
            day_x = (start_time + timedelta(days=x)).date()
            result.append({
                "day": day_x.isoformat(),
                "count": day_counts.get(day_x, 0),
            })
        return result



# @router.get("/agent/test")
# async def agent_test(
#         session:Annotated[AsyncSession, Depends(get_db)],
#         background_tasks: BackgroundTasks,
# ):
#     async with session.begin():
#         candidate_repo = CandidateRepo(session)
#         position_repo = PositionRepo(session)
#         user_repo = UserRepo(session)
#         #获取ORM模型对象
#         candidate_model = await candidate_repo.get_by_id("fhzsuTctybq4Env92YuR9b")
#         position_model =  await position_repo.get_by_id("RBk6bC648aS7EmK6gcARbD")
#         interviewer_model = await user_repo.get_by_id("9iqEJnR8iZwJSHLxoHcQke")
#         #把ORM模型对象转成schema对象
#         candidate = CandidateSchema.model_validate(candidate_model)
#         position = PositionSchema.model_validate(position_model)
#         interviewer = UserSH.model_validate(interviewer_model)
#
#         background_tasks.add_task(run_candidate_agent, candidate, position, interviewer)
#
#         return {"result": "success"}
