from pydantic import BaseModel, Field, ConfigDict, field_validator
from app.schemas.user_schema import UserSH
# from core.cache import TaskInfoSchema
from app.models.candidate import GenderEnum, CandidateStatusEnum
from app.schemas.position_schema import PositionSchema
from typing import Optional, Literal
from datetime import datetime


class ResumeSchema(BaseModel):
    id: str = Field(..., description="简历ID")
    file_path: str = Field(..., description="简历存储路径")
    uploader: UserSH = Field(..., description="建立上传者")

    model_config = ConfigDict(from_attributes=True)

class ResumeUploadRespSchema(BaseModel):
    resume: ResumeSchema | None = Field(..., description="简历信息")

class ResumePaseSchema(BaseModel):
    resume_id: str = Field(..., description="简历ID")

class ResumeParseTaskRespSchema(BaseModel):
    task_id: str = Field(..., description="任务ID")

# class ResumeParseTaskInfoRespSchema(TaskInfoSchema):
#     pass

class CandidateCreateSchema(BaseModel):
    name: str = Field(..., description="候选人姓名")
    email: str = Field(..., description="候选人邮箱")
    gender: GenderEnum = Field(GenderEnum.UNKNOWN, description="候选人性别")
    birthday: str | None = Field(None, description="候选人出生日期，如果只有年份，那么就把日期设置为1月1日")
    phone_number: str | None = Field(None, description="候选人电话")
    work_experience: str | None = Field(None, description="候选人工作经历")
    project_experience: str | None = Field(None, description="候选人项目经历")
    education_experience: str | None = Field(None, description="候选人教育经历")
    self_evaluation: str | None = Field(None, description="候选人自我评价")
    other_information: str | None = Field(None, description="候选人其他信息")
    skills: str | None = Field(None, description="候选人技能")

    position_id: str = Field(..., description="候选人申请职位ID")
    resume_id: str = Field(..., description="候选人的简历ID")


class CandidateSchema(BaseModel):
    id: str = Field(..., description="候选人ID")
    name: str = Field(..., description="候选人姓名")
    email: str = Field(..., description="候选人邮箱")
    gender: GenderEnum = Field(GenderEnum.UNKNOWN, description="候选人性别")
    birthday: str | None = Field(None, description="候选人出生日期，如果只有年份，那么就把日期设置为1月1日")
    phone_number: str | None = Field(None, description="候选人电话")
    work_experience: str | None = Field(None, description="候选人工作经历")
    project_experience: str | None = Field(None, description="候选人项目经历")
    education_experience: str | None = Field(None, description="候选人教育经历")
    self_evaluation: str | None = Field(None, description="候选人自我评价")
    other_information: str | None = Field(None, description="候选人其他信息")
    skills: str | None = Field(None, description="候选人技能")
    status: CandidateStatusEnum | str | None = Field(None, description="候选人状态")
    work_year: int = Field(0, description="候选人工作经验")

    position: PositionSchema = Field(..., description="候选人申请的职位信息")
    resume: ResumeSchema = Field(..., description="候选人的简历信息")
    creator: UserSH = Field(..., description="创建该候选人的信息")

    model_config = ConfigDict(from_attributes=True)

    @field_validator('work_year', mode='before')
    @classmethod
    def default_work_year(cls, v):
        """数据库中 work_year 可能为 NULL（历史数据），统一转为 0"""
        return v if v is not None else 0

class CandidateStatusUpdateSchema(BaseModel):
    status: CandidateStatusEnum = Field(..., description="候选人状态")
    interview_time: Optional[datetime] = Field(None, description="面试时间，当状态变更为待面试时必填")
    rejection_reason: Optional[str] = Field(None, description="未通过原因，当状态变更为面试未通过时必填")

class CandidateListSchema(BaseModel):
    candidates: list[CandidateSchema]

class CandidateAIScoreSchema(BaseModel):
    id: str
    work_experience_score: int
    technical_skills_score: int
    soft_skills_score: int
    educational_background_score: int
    project_experience_score: int
    overall_score: int
    summary: str
    strengths: list[str]
    weaknesses: list[str]
    candidate_id: str

    model_config = ConfigDict(from_attributes=True)

class CandidateAIScoreRespSchema(BaseModel):
    ai_score: CandidateAIScoreSchema


from app.schemas.agent_schema import AgentCandidateSchema
class TaskInfoSchema(BaseModel):
    task_id: str
    status: Literal['pending', 'done', 'failed']
    result: AgentCandidateSchema | None = None
    error: str | None = None