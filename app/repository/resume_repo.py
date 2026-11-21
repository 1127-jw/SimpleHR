from typing import cast
from datetime import datetime

from app.models.position import PositionModel
from . import BaseRepo
from app.models.candidate import ResumeModel, CandidateModel
from sqlalchemy import select, update, delete
from app.models.candidate import CandidateAIScoreModel
from sqlalchemy.orm import selectinload
from app.models.candidate import CandidateStatusEnum
from app.models.user import UserModel
from sqlalchemy import func, and_
from app.models.InterviewModel import InterviewModel
from app.models.candidate_status_history import CandidateStatusHistoryModel


def candidate_eager_options():
    """候选人详情 / 列表所需的关联预加载。

    历史实现给 ``CandidateModel`` 的关系统一加了 ``lazy="joined"``，
    结果是任何查询（哪怕只需要 name）都会强制 JOIN position / resume /
    creator 三张表。这里改为按查询场景显式 ``selectinload``：

    - ``selectinload`` 走额外一条 IN 查询，避免 joined-eager 的行膨胀
    - 层级按需展开到 ``UserSH`` / ``PositionSchema`` 真正需要的深度
    """
    return (
        selectinload(CandidateModel.position)
        .selectinload(PositionModel.creator)
        .selectinload(UserModel.department),
        selectinload(CandidateModel.position).selectinload(PositionModel.department),
        selectinload(CandidateModel.resume),
        selectinload(CandidateModel.creator).selectinload(UserModel.department),
    )


class ResumeRepo(BaseRepo):
    async def create_resume(self, file_path: str, uploader_id: str) -> ResumeModel:
        resume = ResumeModel(file_path=file_path, uploader_id=uploader_id)
        self.session.add(resume)
        await self.session.flush()
        # 重新查询以带上响应所需的关联（ResumeSchema.uploader → UserSH.department）
        return cast(
            ResumeModel,
            await self.session.scalar(
                select(ResumeModel)
                .where(ResumeModel.id == resume.id)
                .options(selectinload(ResumeModel.uploader).selectinload(UserModel.department))
            ),
        )

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
        # 重新查询以带上 CandidateSchema 所需的全部关联
        return cast(CandidateModel, await self.get_by_id(candidate.id))

    async def delete_candidate(self, candidate_id: str) -> bool:
        """删除候选人及其关联数据（AI 评分、面试记录）。

        Returns:
            True 表示删除成功，False 表示候选人不存在。
        """
        candidate = await self.get_by_id(candidate_id)
        if candidate is None:
            return False

        # 删除关联的 AI 评分
        await self.session.execute(
            delete(CandidateAIScoreModel).where(
                CandidateAIScoreModel.candidate_id == candidate_id
            )
        )

        # 删除关联的面试记录
        await self.session.execute(
            delete(InterviewModel).where(
                InterviewModel.candidate_id == candidate_id
            )
        )

        # 删除状态变更链：候选人没了，链记录留着只会让
        # 「建图查重」误判该邮箱仍有历史（虽然归档后本就允许重建，
        # 但留着脏数据会让审计与统计失真）。
        await self.session.execute(
            delete(CandidateStatusHistoryModel).where(
                CandidateStatusHistoryModel.candidate_id == candidate_id
            )
        )

        # 删除候选人
        await self.session.delete(candidate)
        return True

    async def update_candidate_status(self, candidate_id: str, status: CandidateStatusEnum):
        stmt = update(CandidateModel).where(CandidateModel.id==candidate_id).values(status=status)
        await self.session.execute(stmt)

    async def update_error_message(self, candidate_id: str, error_message: str):
        stmt = update(CandidateModel).where(CandidateModel.id==candidate_id).values(error_message=error_message)
        await self.session.execute(stmt)

    async def update_email_invite_sent_at(self, candidate_id: str, sent_at: datetime):
        """记录面试邀请邮件的发送时间，供「等待回复」超时巡检使用。"""
        stmt = (
            update(CandidateModel)
            .where(CandidateModel.id == candidate_id)
            .values(email_invite_sent_at=sent_at)
        )
        await self.session.execute(stmt)

    async def get_by_id(self, candidate_id: str) -> CandidateModel | None:
        return await self.session.scalar(
            select(CandidateModel)
            .where(CandidateModel.id==candidate_id)
            .options(*candidate_eager_options())
        )

    async def get_by_email(self, email: str) -> CandidateModel | None:
        return await self.session.scalar(
            select(CandidateModel)
            .where(CandidateModel.email == email)
            .options(*candidate_eager_options())
        )

    async def list_by_email(self, email: str) -> list[CandidateModel]:
        """按邮箱取**全部**记录（同一候选人多次投递会有多条）。

        ``get_by_email`` 只返回任意一条，同邮箱多记录时会选错——恢复流程
        （``notify``）必须挑「当前活跃」的那条。
        """
        return list(await self.session.scalars(
            select(CandidateModel)
            .where(CandidateModel.email == email)
            .order_by(CandidateModel.created_at.desc())
            .options(*candidate_eager_options())
        ))

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
            # get_current_user 已通过 user_eager_options 预加载 managed_departments，
            # 直接复用，避免同一请求内重复查询用户与部门关系
            department_ids = [d.id for d in current_user.managed_departments]
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
            stmt.options(*candidate_eager_options())
            .order_by(CandidateModel.created_at.desc())
            .offset(offset)
            .limit(size)
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

    async def list_stale_email_waiters(self, cutoff: datetime, limit: int = 200) -> list[CandidateModel]:
        """查询「等待候选人回复」已超过 ``cutoff`` 的候选人。

        用于邮件回复超时巡检：这些候选人的状态图正停在
        ``get_candidate_email_reply_node`` 的 interrupt 上。
        """
        stmt = (
            select(CandidateModel)
            .where(
                CandidateModel.status.in_([
                    CandidateStatusEnum.AI_FILTER_PASSED,
                    CandidateStatusEnum.MANUAL_PASSED,
                ]),
                CandidateModel.email_invite_sent_at.is_not(None),
                CandidateModel.email_invite_sent_at <= cutoff,
            )
            .order_by(CandidateModel.email_invite_sent_at.asc())
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))

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

    async def distribution_rows(
        self, current_user: UserModel
    ) -> list[tuple[str, str | None, str]]:
        """看板聚合用：返回 (候选人状态, 教育经历文本, 职位名称)。

        必须带上与列表一致的可见性过滤：否则任意登录用户都能拿到全公司
        候选人的学历/状态/职位分布，与列表、下载、改状态、评分的收敛口径不一致。
        """
        stmt = (
            select(
                CandidateModel.status,
                CandidateModel.education_experience,
                PositionModel.title,
            )
            .join(PositionModel, CandidateModel.position_id == PositionModel.id)
        )
        # 复用列表的可见性条件（这里已自行 join 了 positions，只取 where 部分）
        visibility = await self._visible_stmt(current_user)
        if visibility.whereclause is not None:
            stmt = stmt.where(visibility.whereclause)
        return list((await self.session.execute(stmt)).all())



class CandidateAIScoreRepo(BaseRepo):
    async def create_candidate_score(self, candidate_id: str, candidate_score_dict: dict):
        candidate_score = CandidateAIScoreModel(**candidate_score_dict, candidate_id=candidate_id)
        self.session.add(candidate_score)
        return candidate_score

    async def upsert_candidate_score(self, candidate_id: str, candidate_score_dict: dict):
        """写入评分，**已有则更新**（``candidate_id`` 上有唯一索引）。

        为什么需要 upsert：人工重试时图可能再次经过 scoring 节点，
        直接 insert 会撞 ``ix_candidate_ai_scores_candidate_id`` 并让整个流程落进 error。
        """
        existing = await self.get_by_candidate_id(candidate_id)
        if existing is None:
            row = CandidateAIScoreModel(**candidate_score_dict, candidate_id=candidate_id)
            self.session.add(row)
            return row
        for key, value in candidate_score_dict.items():
            setattr(existing, key, value)
        return existing

    async def get_by_candidate_id(self, candidate_id: str):
        return await self.session.scalar(select(CandidateAIScoreModel).where(CandidateAIScoreModel.candidate_id==candidate_id))
