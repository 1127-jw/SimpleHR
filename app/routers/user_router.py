from fastapi import APIRouter, HTTPException, Depends, status, BackgroundTasks
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request

from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated, List
from urllib.parse import urlencode, urljoin
from redis.asyncio import Redis

from app.core.db import get_db
from app.core.redis_client import get_redis
from app.schemas.user_schema import UserLoginReqSH, UserLoginResSH, RefreshRequest, LogoutRequest, UserInviteSchema, \
    UserRegisterReqSchema, UserRegisterResSchema, UserListResponseSchema, UserStatusUpdateSchema, \
    DepartmentListRespSchema, DingdingUserRespSchema, AssignDepartmentSchema, HrListRespSchema
from app.schemas.response_schema import ResponseSchema
from app.core.auth import create_tokens, revoke_refresh_token, refresh_access_token, get_current_user, get_super_user
from app.service import auth_ser
from app.models.user import UserModel
from app.service.email_ser import get_invite_code
from app.tasks.email_tasks import send_invite_email_task
from app.repository.user_repo import DepartmentRepo
from app.core.config import get_settings
from app.service import dingtalk_ser
from app.repository.user_repo import UserRepo

settings = get_settings()

jinja2Engine = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/user", tags=["user"])

@router.post("/token",summary="登录",response_model=UserLoginResSH)
async def login(
    login_data:UserLoginReqSH,
    session:Annotated[AsyncSession, Depends(get_db)],
    r: Redis = Depends(get_redis),
):
    user = await auth_ser.authenticate_user(login_data.email,login_data.password,session)
    tokens = await create_tokens(login_data.email,r)

    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": tokens["token_type"],
        "user": user
    }

@router.post("/refresh")
async def refresh(
        body: RefreshRequest,
        r: Redis = Depends(get_redis),
):

    return await refresh_access_token(body.refresh_token, r)


@router.post("/logout")
async def logout(
        body: LogoutRequest,
        r: Redis = Depends(get_redis),
):
    await revoke_refresh_token(body.refresh_token, r)
    return {"detail": "Logged out"}


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return user

@router.post("/invite", summary="邀请用户", response_model=ResponseSchema)
async def invite_user(
    invite_data: UserInviteSchema,
    background_tasks: BackgroundTasks,
    session:Annotated[AsyncSession, Depends(get_db)],
    r: Redis = Depends(get_redis),
    user: UserModel = Depends(get_super_user),
):
    invite_code = await get_invite_code(invite_data,session,r,user.department_id)
    background_tasks.add_task(send_invite_email_task, str(invite_data.email), invite_code)
    return ResponseSchema()

@router.post("/register",summary="注册", response_model=UserRegisterResSchema)
async def register(
    register_data: UserRegisterReqSchema,
    session: Annotated[AsyncSession, Depends(get_db)],
    r: Redis = Depends(get_redis),

):
    user = await auth_ser.create_user(register_data,session,r)

    return {
        "email": user.email,
        "username": user.username,
    }

@router.get("/list",summary="获取员工列表",response_model=UserListResponseSchema)
async def list_users(
        session: Annotated[AsyncSession, Depends(get_db)],
        page: int = 1,
        size: int = 10,
        user: UserModel = Depends(get_current_user),
):
    user_list =await auth_ser.get_list_users(session,user,page,size)
    return {
        "total": len(user_list),
        "items": user_list[(page - 1) * size : page * size],
    }

@router.patch("/status/update",summary="修改员工状态", response_model=ResponseSchema)
async def update_status(
    status_data: UserStatusUpdateSchema,
    session: Annotated[AsyncSession, Depends(get_db)],
    _: UserModel = Depends(get_super_user),
):
    await  auth_ser.update_user_status(session,status_data)
    return ResponseSchema()

@router.get("/department/list", summary="获取所有部门列表", response_model=DepartmentListRespSchema)
async def department_list(
    session: Annotated[AsyncSession, Depends(get_db)],
    _: str = Depends(get_current_user),
):
    async with session.begin():
        department_repo = DepartmentRepo(session)
        departments = await department_repo.get_department_list()
        return {"departments": departments}

@router.get("/dingtalk/authorize", summary="获取登录钉钉的URL")
async def dingtalk_authorize(
    current_user: UserModel = Depends(get_current_user),
):
    # redirect_uri：必须是公网能够访问的url
    redirect_uri = urljoin(settings.BACKEND_BASE_URL, "/user/dingtalk/callback")
    params = {
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "client_id": settings.DINGTALK_CLIENT_ID,
        "scope": "openid",
        "state": current_user.id,
        "prompt": "consent"
    }
    authorize_url = f"https://login.dingtalk.com/oauth2/auth?{urlencode(params)}"
    return {"authorize_url": authorize_url}

@router.get("/dingtalk/callback")
async def dingtalk_callback(
    session: Annotated[AsyncSession, Depends(get_db)],
    state: str,
    code: str | None = None,
    authCode: str | None = None,
    error: str | None = None,
    r: Redis = Depends(get_redis),

):
    if error:
        raise HTTPException(status_code=400, detail=f"钉钉授权失败: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="缺少钉钉授权码")
    if not state:
        raise HTTPException(status_code=400, detail="缺少 state 参数")

    re_cache_key = f"dingtalk:user_retoken:{state}"
    retoken = await r.get(re_cache_key)
    if retoken is None:
        print("无retoken")
        actoken_state = await dingtalk_ser.call_dingtalk_service(r,state,code)
    else:
        print("retoken",retoken)
        actoken_state = await dingtalk_ser.refresh_user_access_token(r, state, retoken)
    redirecot_url = await dingtalk_ser.get_dingtalk_user(session,state,actoken_state.get("access_token"))

    return RedirectResponse(redirecot_url)

@router.get("/dingtalk/authorize/success")
async def dingtalk_authorize_success(
    nick: str,
    request: Request,
):
    return jinja2Engine.TemplateResponse(
        request,
        "ding_authorize_success.html",
        {"username": nick}
    )

@router.get("/dingtalk/account", summary="获取自己的钉钉账号", response_model=DingdingUserRespSchema)
async def dingtalk_account(
    session: Annotated[AsyncSession, Depends(get_db)],
    current_user: UserModel = Depends(get_current_user),
):
    async with session.begin():
        user_repo = UserRepo(session)
        dingding_user = await user_repo.get_dingding_user(current_user.id)
    return {"dingding_user": dingding_user}


@router.post("/assign/department", summary="分配部门给指定的HR", response_model=ResponseSchema)
async def assign_department(
    assign_data: AssignDepartmentSchema,
    session: Annotated[AsyncSession, Depends(get_db)],
    _: UserModel = Depends(get_super_user),
):
    async with session.begin():
        user_repo = UserRepo(session)
        try:
            await user_repo.assign_department(hr_id=assign_data.hr_id, department_ids=assign_data.department_ids)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        return ResponseSchema()

@router.get("/hr/list", summary="获取HR列表", response_model=HrListRespSchema)
async def get_hr_list(
    session: Annotated[AsyncSession, Depends(get_db)],
    _: UserModel = Depends(get_super_user),
):
    async with session.begin():
        user_repo = UserRepo(session)
        hrs = await user_repo.get_hr_list()
        return {"hrs": hrs}
