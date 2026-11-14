from enum import StrEnum
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    String, Text, Integer, Enum as SQLAlchemyEnum, ForeignKey, JSON, DateTime, Index, text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import BaseModel
from .user import UserModel

if TYPE_CHECKING:
    from .position import PositionModel


class CandidateStatusEnum(StrEnum):
    # 1. 已投递
    APPLICATION = "已投递"
    # 2. AI筛选失败
    AI_FILTER_FAILED = "AI筛选失败"
    # 3. AI筛选成功
    AI_FILTER_PASSED = "AI筛选成功"
    # 4. 人工通过/重试（HR 手动干预，重新启动协商流程）
    MANUAL_PASSED = "人工通过/重试"
    # 5. 待面试
    WAITING_FOR_INTERVIEW = "待面试"
    # 6. 拒绝面试
    REFUSED_INTERVIEW = "拒绝面试"
    # 7. 面试通过
    INTERVIEW_PASSED = "面试通过"
    # 8. 面试未通过
    INTERVIEW_REJECTED = "面试未通过"
    # 9. 已入职
    HIRED = "已入职"
    # 10. 已拒绝
    REJECTED = "已拒绝"
    # 11. 协商超时（超过协商轮次上限或候选人回复超时）
    NEGOTIATION_EXPIRED = "协商超时"
    # 12. 处理异常（基础设施故障，可重试）
    PROCESSING_ERROR = "处理异常"


class GenderEnum(StrEnum):
    # 1. 男
    MALE = "男"
    # 2. 女
    FEMALE = "女"
    # 3. 其他
    UNKNOWN = "未知"

#候选人
class CandidateModel(BaseModel):
    __tablename__ = "candidates"

    # 列表页高频过滤 + 排序路径：position_id / status / created_at
    __table_args__ = (
        # 主路径：职位 + 状态过滤，按创建时间倒序
        Index("ix_candidates_position_status_created", "position_id", "status", "created_at"),
        # 仅按职位过滤 + 按创建时间倒序（position_id 单条件可用左缀）
        Index("ix_candidates_position_created", "position_id", "created_at"),
        # 超管 / 无过滤列表：纯 created_at 倒序
        Index("ix_candidates_created_at", "created_at"),
        # 等待回复超时巡检：只覆盖「等待中且已发邀请」的行，索引体积远小于全列索引
        Index(
            "ix_candidates_email_waiting",
            "email_invite_sent_at",
            postgresql_where=text(
                "status IN ('AI筛选成功', '人工通过/重试') AND email_invite_sent_at IS NOT NULL"
            ),
        ),
        # 候选人与简历 1:1，唯一索引同时保证完整性与点查性能
        Index("ix_candidates_resume_id", "resume_id", unique=True),
    )

    name: Mapped[str] = mapped_column(String(50), nullable=False)
    gender: Mapped[GenderEnum] = mapped_column(SQLAlchemyEnum(GenderEnum), default=GenderEnum.UNKNOWN, nullable=False)
    birthday: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    email: Mapped[str] = mapped_column(String(100), nullable=True, index=True)
    phone_number: Mapped[Optional[str]] = mapped_column(String(20))
    work_experience: Mapped[Optional[str]] = mapped_column(Text)
    project_experience: Mapped[Optional[str]] = mapped_column(Text)
    education_experience: Mapped[Optional[str]] = mapped_column(Text)
    self_evaluation: Mapped[Optional[str]] = mapped_column(Text)
    other_information: Mapped[Optional[str]] = mapped_column(Text)
    skills: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[CandidateStatusEnum] = mapped_column(
        SQLAlchemyEnum(CandidateStatusEnum, values_callable=lambda obj: [e.value for e in obj]),
        default=CandidateStatusEnum.APPLICATION, nullable=False, index=True
    )
    work_year:Mapped[int] = mapped_column(Integer,default=0, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 异常时的错误信息
    # 面试邀请邮件发送时间：用于「等待候选人回复」的超时巡检
    # （索引见 __table_args__ 的部分索引 ix_candidates_email_waiting）
    email_invite_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    position_id: Mapped[str] = mapped_column(ForeignKey("positions.id"))
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"))
    # 这条数据是由谁创建的
    creator_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    # 关联默认懒加载，由仓储层按查询场景显式 selectinload
    # （原 lazy="joined" 会让每个查询都强制 JOIN 三张表）
    position: Mapped["PositionModel"] = relationship("PositionModel", back_populates="candidates")
    resume: Mapped["ResumeModel"] = relationship(back_populates="candidate", uselist=False)
    creator: Mapped["UserModel"] = relationship()
    ai_score: Mapped["CandidateAIScoreModel"] = relationship(back_populates="candidate", uselist=False)

#候选人AI评分
class CandidateAIScoreModel(BaseModel):
    __tablename__ = "candidate_ai_scores"

    # 候选人与 AI 评分 1:1，唯一索引防止重复评分
    __table_args__ = (
        Index("ix_candidate_ai_scores_candidate_id", "candidate_id", unique=True),
    )

    work_experience_score: Mapped[int] = mapped_column(Integer, nullable=False)
    technical_skills_score: Mapped[int] = mapped_column(Integer, nullable=False)
    soft_skills_score: Mapped[int] = mapped_column(Integer, nullable=False)
    educational_background_score: Mapped[int] = mapped_column(Integer, nullable=False)
    project_experience_score: Mapped[int] = mapped_column(Integer, nullable=False)
    overall_score: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    strengths: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    weaknesses: Mapped[list[str]] = mapped_column(JSON, nullable=False)

    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"))

    candidate: Mapped["CandidateModel"] = relationship(back_populates="ai_score")

#简历
class ResumeModel(BaseModel):
    __tablename__ = "resumes"

    file_path: Mapped[str] = mapped_column(String(255), nullable=False)
    uploader_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    uploader: Mapped["UserModel"] = relationship()
    candidate: Mapped["CandidateModel"] = relationship(back_populates="resume")
