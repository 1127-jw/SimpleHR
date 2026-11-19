from typing import cast

from sqlalchemy import func, select, update

from app.models.mail_submission import MailResumeSubmissionModel
from . import BaseRepo


class MailSubmissionRepo(BaseRepo):
    """邮件投递待确认记录的 CRUD。"""

    async def create(self, data: dict) -> MailResumeSubmissionModel:
        obj = MailResumeSubmissionModel(**data)
        self.session.add(obj)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def get_by_id(self, submission_id: str) -> MailResumeSubmissionModel | None:
        return cast(
            MailResumeSubmissionModel | None,
            await self.session.scalar(
                select(MailResumeSubmissionModel).where(
                    MailResumeSubmissionModel.id == submission_id
                )
            ),
        )

    async def get_by_message_id(self, message_id: str) -> MailResumeSubmissionModel | None:
        return cast(
            MailResumeSubmissionModel | None,
            await self.session.scalar(
                select(MailResumeSubmissionModel).where(
                    MailResumeSubmissionModel.message_id == message_id
                )
            ),
        )

    async def list_by_status(
        self, status: str, limit: int = 50
    ) -> list[MailResumeSubmissionModel]:
        result = await self.session.scalars(
            select(MailResumeSubmissionModel)
            .where(MailResumeSubmissionModel.status == status)
            .order_by(MailResumeSubmissionModel.created_at.desc())
            .limit(limit)
        )
        return list(result)

    async def list_by_status_page(
        self, status: str, *, page: int = 1, size: int = 20
    ) -> tuple[list[MailResumeSubmissionModel], int]:
        """按状态分页取投递记录，返回 (本页数据, 总数)。

        ``CONFIRMED`` / ``REJECTED`` 是只增不减的历史数据，列表接口若不分页，
        每刷新一次都要全表读完再全部塞进响应。
        """
        total = await self.session.scalar(
            select(func.count())
            .select_from(MailResumeSubmissionModel)
            .where(MailResumeSubmissionModel.status == status)
        )
        result = await self.session.scalars(
            select(MailResumeSubmissionModel)
            .where(MailResumeSubmissionModel.status == status)
            .order_by(MailResumeSubmissionModel.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        return list(result), int(total or 0)

    async def mark_status(self, submission_id: str, status: str, **fields) -> None:
        await self.session.execute(
            update(MailResumeSubmissionModel)
            .where(MailResumeSubmissionModel.id == submission_id)
            .values(status=status, **fields)
        )

    async def set_parsed_info(
        self, submission_id: str, parsed_info: dict, suggested_position_id: str | None = None
    ) -> None:
        """回填简历解析结果（best-effort，失败不影响主流程）。"""
        values = {"parsed_info": parsed_info}
        if suggested_position_id:
            values["suggested_position_id"] = suggested_position_id
        await self.session.execute(
            update(MailResumeSubmissionModel)
            .where(MailResumeSubmissionModel.id == submission_id)
            .values(**values)
        )
