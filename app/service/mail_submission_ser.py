"""邮件投递的待确认闭环：落库 / 确认建人起图 / 拒绝 / 过期。

邮件命中 RESUME_SUBMISSION 后**不自动建人**，先落 PENDING，由 HR 确认后
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.models.mail_submission import (
    MailResumeSubmissionModel,
    MailSubmissionStatusEnum,
)
from app.repository.mail_submission_repo import MailSubmissionRepo
from app.repository.resume_repo import CandidateRepo, ResumeRepo
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from utils.emailbot.models import ParsedEmail
from app.core.time import db_now

logger = logging.getLogger(__name__)
settings = get_settings()


async def create_mail_resume_submission(email: ParsedEmail) -> MailResumeSubmissionModel:
    """落一条 PENDING 记录；同 message_id 幂等。"""
    async with AsyncSessionFactory() as session:
        async with session.begin():
            repo = MailSubmissionRepo(session)
            existed = await repo.get_by_message_id(email.message_id or email.uid)
            if existed is not None:
                logger.info("[mail_submission] 重复投递，跳过 | uid=%s", email.uid)
                return existed

            sub = await repo.create({
                "email_uid": email.uid,
                "message_id": email.message_id or email.uid,
                "from_address": email.from_.address,
                "from_name": email.from_.name,
                "subject": email.subject[:500],
                # 只存摘要，避免把邮件全文（可能含敏感信息）长期落库
                "body_excerpt": email.text[:2000],
                "attachment_paths": [
                    str(a.saved_path) for a in email.attachments if a.saved_path
                ],
                "parsed_info": {},
                "status": MailSubmissionStatusEnum.PENDING,
            })
    logger.info("[mail_submission] 已落待确认 | uid=%s | from=%s", email.uid, email.from_.address)
    return sub


async def confirm_mail_submission(
    session: AsyncSession,
    submission_id: str,
    position_id: str,
    current_user,
) -> tuple:
    """确认：迁移附件 → 建 Resume → 建 Candidate，返回 (candidate, position, interviewer)。

    幂等：``status != PENDING`` 直接抛错，由上层转为 409。
    事务：查询 / 建 Resume / 建 Candidate / 更新状态都在同一事务内完成。
    """
    repo = MailSubmissionRepo(session)

    async with session.begin():
        sub = await repo.get_by_id(submission_id)
        if sub is None:
            raise ValueError("待确认记录不存在")
        if sub.status != MailSubmissionStatusEnum.PENDING:
            raise ValueError(f"当前状态 {sub.status} 不可确认")
        if not sub.attachment_paths:
            raise ValueError("该投递没有可用附件，无法创建候选人")

        stored_name = await _store_resume_file(sub.attachment_paths[0])
        info = sub.parsed_info or {}

        resume = await ResumeRepo(session).create_resume(
            file_path=stored_name, uploader_id=current_user.id
        )
        candidate_model = await CandidateRepo(session).create_candidate({
            "name": info.get("name") or sub.from_name or sub.from_address,
            "email": sub.from_address,
            "gender": info.get("gender", "未知"),
            "phone_number": info.get("phone_number"),
            "work_experience": info.get("work_experience"),
            "project_experience": info.get("project_experience"),
            "education_experience": info.get("education_experience"),
            "self_evaluation": info.get("self_evaluation"),
            "other_information": info.get("other_information"),
            "skills": info.get("skills"),
            "work_year": info.get("work_year", 0),
            "position_id": position_id,
            "resume_id": resume.id,
            "creator_id": current_user.id,
        })
        await repo.mark_status(
            submission_id, MailSubmissionStatusEnum.CONFIRMED,
            position_id=position_id, resume_id=resume.id,
            reviewer_id=current_user.id,
            reviewed_at=db_now(),
        )

    # 会话内转 Schema（create_candidate 已带 eager 关联）
    candidate_schema = CandidateSchema.model_validate(candidate_model)
    position_schema = PositionSchema.model_validate(candidate_model.position)
    interviewer_schema = UserSH.model_validate(candidate_model.position.creator)
    return candidate_schema, position_schema, interviewer_schema


async def reject_mail_submission(
    session: AsyncSession, submission_id: str, current_user, note: str = ""
) -> None:
    repo = MailSubmissionRepo(session)
    async with session.begin():
        await repo.mark_status(
            submission_id, MailSubmissionStatusEnum.REJECTED,
            reviewer_id=current_user.id,
            reviewed_at=db_now(),
            review_note=note,
        )


async def enrich_mail_submission(submission_id: str, attachment_path: str) -> None:
    """best-effort：OCR + LLM 解析简历，回填 ``parsed_info``。

    失败只记日志，不阻塞邮件轮询；解析结果供 HR 在确认台核对。
    """
    from app.core.limiters import task_slot

    try:
        from app.agents.resume import extract_candidate_info
        from utils.ocr_service import PaddleOcr

        # 后台富化：独立小配额，避免抢占主流程的 LLM 并发
        async with task_slot("mail_enrich", settings.MAIL_ENRICH_MAX_CONCURRENCY):
            ocr_path = attachment_path
            suffix = Path(attachment_path).suffix.lower()
            if suffix in (".doc", ".docx"):
                # OCR 只吃 PDF，先转换一份临时 PDF
                from utils.PDFser import WordToPdfConverter

                pdf_path = Path(attachment_path).with_suffix(".pdf")
                await WordToPdfConverter(
                    word_path=attachment_path, output_pdf_path=str(pdf_path)
                ).convert()
                ocr_path = str(pdf_path)

            paddle = PaddleOcr()
            job_id = await paddle.create_job(ocr_path)
            jsonl_url = await paddle.poll_for_state(job_id)
            contents = await paddle.fetch_parsed_contents(jsonl_url)
            info = await extract_candidate_info("\n\n".join(contents))

        async with AsyncSessionFactory() as session:
            async with session.begin():
                await MailSubmissionRepo(session).set_parsed_info(
                    submission_id, info.model_dump()
                )
        logger.info("[mail_submission] 简历解析完成 | submission_id=%s", submission_id)
    except Exception:
        logger.warning(
            "[mail_submission] 简历解析失败（不阻塞投递） | submission_id=%s",
            submission_id, exc_info=True,
        )


async def expire_stale_submissions() -> int:
    """定时任务：超期 PENDING → EXPIRED，返回处理条数。"""
    deadline = db_now() - timedelta(
        days=settings.MAIL_SUBMISSION_EXPIRE_DAYS
    )
    async with AsyncSessionFactory() as session:
        async with session.begin():
            repo = MailSubmissionRepo(session)
            stale = [
                s for s in await repo.list_by_status(MailSubmissionStatusEnum.PENDING, limit=500)
                if s.created_at < deadline
            ]
            for s in stale:
                await repo.mark_status(s.id, MailSubmissionStatusEnum.EXPIRED)
    if stale:
        logger.info("[mail_submission] 超期置为 EXPIRED | 条数=%s", len(stale))
    return len(stale)


async def _store_resume_file(src_path: str) -> str:
    """把邮件附件复制进 RESUME_DIR，返回相对文件名。

    doc/docx 需要转 PDF 时，复用与 ``resume_upload_service`` 相同的转换逻辑。
    """
    src = Path(src_path)
    if not src.exists():
        raise ValueError(f"附件不存在: {src_path}")

    os.makedirs(settings.RESUME_DIR, exist_ok=True)
    ext = src.suffix.lower()
    name = f"{uuid4()}{ext}"
    dst = Path(settings.RESUME_DIR) / name
    shutil.copyfile(src, dst)

    if ext in (".doc", ".docx"):
        from utils.PDFser import WordToPdfConverter

        pdf_path = dst.with_suffix(".pdf")
        await WordToPdfConverter(
            word_path=str(dst), output_pdf_path=str(pdf_path)
        ).convert()
        dst = pdf_path

    return dst.name


async def notify_hr_new_submission(email: ParsedEmail) -> None:
    """通知 HR：钉钉 / 前端未读（示例仅打日志，接入实际推送即可）。
    TODO: 接入实际通知（钉钉工作通知 / 邮件），可复用
    """
    logger.info(
        "[mail_submission] 通知 HR：新投递待确认 | from=%s | subject=%s",
        email.from_.address, email.subject,
    )
