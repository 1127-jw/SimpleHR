import os

import aiofiles
import logger
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status, BackgroundTasks, Query
from typing import Annotated, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import get_db
from app.core.auth import create_tokens, revoke_refresh_token, refresh_access_token, get_current_user, get_super_user
from app.core.config import get_settings
from app.models.user import UserModel
from uuid import uuid4
from pathlib import Path

from app.repository.resume_repo import ResumeRepo
from app.schemas.candidate_schema import ResumeUploadRespSchema
from app.service.candidate_ser import resume_upload_service
from utils.PDFser import WordToPdfConverter

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

