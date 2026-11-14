from enum import StrEnum
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from app.core.db import BaseModel

from sqlalchemy import (
    String, Text, Integer,
    DateTime, Boolean, Enum as SQLAlchemyEnum, ForeignKey, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .user import UserModel, DepartmentModel

if TYPE_CHECKING:
    from .candidate import CandidateModel



class EducationEnum(StrEnum):
    COLLEGE = "大专"
    BACHELOR = "本科"
    MASTER = "硕士"
    DOCTOR = "博士"
    UNKNOWN = "未知"

class PositionModel(BaseModel):
    __tablename__ = "positions"

    # 职位列表：按部门过滤 + 按创建时间倒序；超管无过滤时走纯 created_at 倒序
    __table_args__ = (
        Index("ix_positions_department_created", "department_id", "created_at"),
        Index("ix_positions_created_at", "created_at"),
    )

    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    requirements: Mapped[Optional[str]] = mapped_column(Text)
    #薪资 min_salary 最低，max_salary 最高
    min_salary: Mapped[Optional[int]] = mapped_column(Integer)
    max_salary: Mapped[Optional[int]] = mapped_column(Integer)
    #招聘截至时间
    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime)
    #需求人数
    recruitment_count: Mapped[int] = mapped_column(Integer, default=1)
    # 最低学历要求
    education: Mapped[EducationEnum] = mapped_column(SQLAlchemyEnum(EducationEnum), default=EducationEnum.UNKNOWN,nullable=False)
    # 最低工作年限要求
    work_year: Mapped[int] = mapped_column(Integer, default=0)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)

    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"))
    creator_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    # 关联默认懒加载，由仓储层按查询场景显式 selectinload
    department: Mapped["DepartmentModel"] = relationship()
    creator: Mapped["UserModel"] = relationship()
    #候选人信息
    candidates: Mapped[List["CandidateModel"]] = relationship("CandidateModel", back_populates="position")