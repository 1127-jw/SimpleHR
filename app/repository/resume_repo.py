from typing import cast
from datetime import datetime

from app.models.position import PositionModel
from . import BaseRepo
from app.models.candidate import ResumeModel, CandidateModel
from sqlalchemy import select, update
from app.models.candidate import CandidateAIScoreModel
from sqlalchemy.orm import selectinload
from app.models.candidate import CandidateStatusEnum
from app.models.user import UserModel
from sqlalchemy import func, and_


class ResumeRepo(BaseRepo):
    async def create_resume(self, file_path: str, uploader_id: str) -> ResumeModel:
        resume = ResumeModel(file_path=file_path, uploader_id=uploader_id)
        self.session.add(resume)
        return resume

    async def get_by_id(self, resume_id: str) -> ResumeModel:
        stmt = select(ResumeModel).where(ResumeModel.id == resume_id)
        resume = await self.session.scalar(stmt)
        if resume is None:
            raise ValueError(f"Resume not found: {resume_id}")
        return cast(ResumeModel, resume)

    async def get_by_id_or_none(self, resume_id: str) -> ResumeModel | None:
        """按 id 取简历，不存在返回 None（不做异常）。"""
        return await self.session.scalar(
            select(ResumeModel).where(ResumeModel.id == resume_id)
        )


class CandidateRepo(BaseRepo):
    async def create_candidate(self, candidate_info: dict) -> CandidateModel:
        candidate = CandidateModel(**candidate_info)
        self.session.add(candidate)
        # .flush 内存 → 数据库 把当前对象的变更同步到数据库，但不一定提交事务
        # .refresh 数据库 → 内存 重新查一次数据库，覆盖对象当前属性
        # .commit 内存 → 数据库并持久化	提交事务，使变更真正生效
        await self.session.flush([candidate])
        await self.session.refresh(candidate, ['position', 'resume'])
        return candidate

    async def update_candidate_status(self, candidate_id: str, status: CandidateStatusEnum):
        stmt = update(CandidateModel).where(CandidateModel.id==candidate_id).values(status=status)
        await self.session.execute(stmt)

    async def update_error_message(self, candidate_id: str, error_message: str):
        stmt = update(CandidateModel).where(CandidateModel.id==candidate_id).values(error_message=error_message)
        await self.session.execute(stmt)

    async def get_by_id(self, candidate_id: str) -> CandidateModel | None:
        return await self.session.scalar(
            select(CandidateModel)
            .where(CandidateModel.id==candidate_id)
            .options(
                selectinload(CandidateModel.position),
                selectinload(CandidateModel.resume),
                selectinload(CandidateModel.creator)
            )
        )

    async def get_by_email(self, email: str) -> CandidateModel | None:
        return await self.session.scalar(
            select(CandidateModel)
            .where(CandidateModel.email == email)
            .options(
                selectinload(CandidateModel.position),
                selectinload(CandidateModel.resume),
                selectinload(CandidateModel.creator)
            )
        )

    async def _visible_stmt(self, current_user: UserModel):
        """候选人可见性条件。

        - superuser：全部
        - hr：所负责部门的候选人
        - 其他：自己发布的职位的候选人
        """
        stmt = select(CandidateModel)
        if current_user.is_superuser:
            return stmt

        stmt = stmt.join(PositionModel)
        if current_user.is_hr:
            hr_user = await self.session.scalar(
                select(UserModel)
                .where(UserModel.id == current_user.id)
                .options(selectinload(UserModel.managed_departments))
            )
            department_ids = [d.id for d in hr_user.managed_departments]
            if not department_ids:
                # 没有负责部门：用恒假条件代替「提前 return []」，保证 count 也是 0
                return stmt.where(CandidateModel.id.is_(None))
            return stmt.where(PositionModel.department_id.in_(department_ids))

        return stmt.where(PositionModel.creator_id == current_user.id)

    async def get_list(
        self,
        current_user: UserModel,
        position_id: str|None = None,
        status: CandidateStatusEnum|None = None,
        page: int = 1,
        size: int = 10,
    ) -> tuple[list[CandidateModel], int]:
        """返回 (当前页数据, 符合条件的总数)。"""
        stmt = await self._visible_stmt(current_user)

        if position_id is not None:
            stmt = stmt.where(CandidateModel.position_id == position_id)
        if status is not None:
            stmt = stmt.where(CandidateModel.status == status)

        total = await self.session.scalar(
            select(func.count()).select_from(stmt.subquery())
        ) or 0

        offset = (page - 1) * size
        rows = await self.session.scalars(
            stmt.order_by(CandidateModel.created_at.desc()).offset(offset).limit(size)
        )
        return list(rows), total

    async def get_for_user(
        self, candidate_id: str, current_user: UserModel
    ) -> CandidateModel | None:
        """按用户权限取单个候选人；不可见时返回 None。"""
        stmt = await self._visible_stmt(current_user)
        stmt = stmt.where(CandidateModel.id == candidate_id).options(
            selectinload(CandidateModel.resume)
        )
        return await self.session.scalar(stmt)

    async def candidate_count(self, start_time: datetime, end_time: datetime):
        stmt = select(
            func.date(CandidateModel.created_at),
            func.count(CandidateModel.id)
        ).where(
            and_(
                CandidateModel.created_at >= start_time,
                CandidateModel.created_at <= end_time
            )
        ).group_by(
            func.date(CandidateModel.created_at),
        ).order_by(
            func.date(CandidateModel.created_at)
        )
        return (await self.session.execute(stmt)).all()



class CandidateAIScoreRepo(BaseRepo):
    async def create_candidate_score(self, candidate_id: str, candidate_score_dict: dict):
        candidate_score = CandidateAIScoreModel(**candidate_score_dict, candidate_id=candidate_id)
        self.session.add(candidate_score)
        return candidate_score

    async def get_by_candidate_id(self, candidate_id: str):
        return await self.session.scalar(select(CandidateAIScoreModel).where(CandidateAIScoreModel.candidate_id==candidate_id).options(selectinload(CandidateAIScoreModel.candidate)))

    async def update_candidate_status(self, candidate_id: str, status: CandidateStatusEnum):
        candidate_score = self.get_by_candidate_id(candidate_id)
        candidate_score.status = status