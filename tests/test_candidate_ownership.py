"""候选人写接口必须校验归属，不能只按 id 操作。

背景：``PATCH /candidate/{id}/status`` 用 ``get_by_id``、``DELETE /candidate/{id}``
直接 ``delete_candidate``，权限检查只有「是不是 HR」。``_visible_stmt`` 划定的
「自己管理的部门 / 自己发布的职位」这一层完全没用上 —— 任意 HR 可改（乃至删除）
任意候选人的状态、面试记录与 AI 评分。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.models.candidate import CandidateStatusEnum
from app.routers import candidate_router as _router_pkg_attr  # noqa: F401  仅确保子模块已导入
from app.schemas.candidate_schema import CandidateStatusUpdateSchema

# app/routers/__init__.py 里 `from ...candidate_router import router as candidate_router`
# 把包属性同名覆盖成了 APIRouter 对象，`import ... as` 取到的会是路由实例而非模块，
# 因此这里绕过包属性直接取真正加载好的模块。
router_mod = sys.modules["app.routers.candidate_router"]

CANDIDATE_ID = "cand-001"
FORBIDDEN_DETAIL = "候选人不存在或无权访问"


@dataclass
class _Row:
    id: str = CANDIDATE_ID
    status: str = CandidateStatusEnum.APPLICATION


@dataclass
class _User:
    id: str = "user-001"
    is_hr: bool = True
    is_superuser: bool = False


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Session:
    """只满足 ``async with session.begin()`` 的最小替身。"""

    def begin(self):
        return _Tx()


class _InterviewRepo:
    def __init__(self, session=None, **kwargs):
        pass

    async def create_interview(self, data):
        raise AssertionError("本用例不该创建面试记录")


def _install_repo(monkeypatch, *, visible: bool) -> list:
    """把 ``CandidateRepo`` 换成记录调用的替身，返回调用流水。"""
    calls: list = []

    class _Repo:
        def __init__(self, session=None, **kwargs):
            pass

        async def get_for_user(self, candidate_id, current_user):
            calls.append(("get_for_user", candidate_id, current_user.id))
            return _Row() if visible else None

        async def get_by_id(self, candidate_id):
            calls.append(("get_by_id", candidate_id))
            raise AssertionError("写状态/删除前必须先过可见性校验，不能只按 id 取人")

        async def update_candidate_status(self, candidate_id, status):
            calls.append(("update_candidate_status", candidate_id, status))

        async def delete_candidate(self, candidate_id):
            calls.append(("delete_candidate", candidate_id))
            return True

    monkeypatch.setattr(router_mod, "CandidateRepo", _Repo)
    monkeypatch.setattr(router_mod, "InterviewRepo", _InterviewRepo)
    return calls


@pytest.fixture
def audits(monkeypatch) -> list:
    recorded: list = []
    monkeypatch.setattr(router_mod, "audit", lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(router_mod, "validate_transition", lambda before, after: None)
    return recorded


async def _patch_status(user) -> None:
    await router_mod.update_candidate_status(
        session=_Session(),
        candidate_id=CANDIDATE_ID,
        status_data=CandidateStatusUpdateSchema(status=CandidateStatusEnum.AI_FILTER_PASSED),
        background_tasks=BackgroundTasks(),
        current_user=user,
    )


async def _delete(user) -> None:
    await router_mod.delete_candidate(
        session=_Session(), candidate_id=CANDIDATE_ID, current_user=user
    )


# ---------------- PATCH /candidate/{id}/status ----------------

async def test_status_update_rejects_invisible_candidate(monkeypatch, audits):
    calls = _install_repo(monkeypatch, visible=False)

    with pytest.raises(HTTPException) as exc:
        await _patch_status(_User())

    assert exc.value.status_code == 404
    assert exc.value.detail == FORBIDDEN_DETAIL
    assert [c[0] for c in calls] == ["get_for_user"], "不可见时不应继续任何写入"
    assert audits == []


async def test_status_update_allows_visible_candidate(monkeypatch, audits):
    calls = _install_repo(monkeypatch, visible=True)
    user = _User()

    await _patch_status(user)

    assert ("get_for_user", CANDIDATE_ID, user.id) in calls
    assert ("update_candidate_status", CANDIDATE_ID, CandidateStatusEnum.AI_FILTER_PASSED) in calls
    assert len(audits) == 1, "状态流转要留审计记录"


# ---------------- DELETE /candidate/{id} ----------------

async def test_delete_rejects_invisible_candidate(monkeypatch, audits):
    calls = _install_repo(monkeypatch, visible=False)

    with pytest.raises(HTTPException) as exc:
        await _delete(_User())

    assert exc.value.status_code == 404
    assert exc.value.detail == FORBIDDEN_DETAIL
    assert "delete_candidate" not in [c[0] for c in calls]
    assert audits == []


async def test_delete_allows_visible_candidate(monkeypatch, audits):
    calls = _install_repo(monkeypatch, visible=True)

    await _delete(_User())

    assert "delete_candidate" in [c[0] for c in calls]
    assert len(audits) == 1


# ---------------- 文案一致，避免用 404 差异枚举 id ----------------

@pytest.mark.parametrize("handler", [_patch_status, _delete])
async def test_invisible_and_missing_share_one_message(monkeypatch, audits, handler):
    _install_repo(monkeypatch, visible=False)

    with pytest.raises(HTTPException) as exc:
        await handler(_User())

    assert exc.value.detail == FORBIDDEN_DETAIL
