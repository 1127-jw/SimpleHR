"""状态图路由的纯函数测试。

覆盖 4 条路由：
1. screening 之后（通过 → scoring / 未通过 → rejected）
2. scoring 之后（达阈值 → email_invite / 未达 → rejected）
3. 收到候选人邮件之后（超时 → negotiation_expired / 否则 → check_availability）
4. check_availability 之后（拒绝 / 确认 / 继续协商 / 超轮次）

另外覆盖统一的 error 守卫 ``_with_error_check``。
"""
from types import SimpleNamespace

import pytest

from app.workflow.candidate_graph import (
    _route_after_scoring,
    _route_after_screening,
    _with_error_check,
    route_after_check_availability,
    route_after_email_reply,
)
from app.workflow.candidate_state import (
    EMAIL_REPLY_TIMEOUT,
    REPLY_REFUSED,
    REPLY_TIME_AVAILABLE,
)


# ---------------- 1. screening 之后 ----------------

@pytest.mark.parametrize(
    ("passed", "expected"),
    [(True, "scoring"), (False, "rejected")],
)
def test_route_after_screening(state_factory, passed, expected):
    state = state_factory(screening_passed=passed)
    assert _route_after_screening(state) == expected


def test_route_after_screening_missing_flag_is_rejected(state_factory):
    """没写 screening_passed 时按未通过处理（不会 KeyError）。"""
    assert _route_after_screening(state_factory()) == "rejected"


# ---------------- 2. scoring 之后 ----------------

@pytest.mark.parametrize(
    ("score", "threshold", "expected"),
    [
        (8, 7, "email_invite"),   # 高于阈值
        (7, 7, "rejected"),       # 等于阈值不算通过（严格大于）
        (6, 7, "rejected"),
        (9, None, "email_invite"),  # 用默认阈值
    ],
)
def test_route_after_scoring(state_factory, score, threshold, expected):
    state = state_factory(
        score_result=SimpleNamespace(overall_score=score),
        score_pass_threshold=threshold,
    )
    assert _route_after_scoring(state) == expected


def test_route_after_scoring_without_result_is_rejected(state_factory):
    assert _route_after_scoring(state_factory(score_result=None)) == "rejected"


def test_route_after_scoring_uses_configured_threshold(state_factory):
    """阈值生效：8 分在阈值 9 下应被拒绝。"""
    state = state_factory(
        score_result=SimpleNamespace(overall_score=8),
        score_pass_threshold=9,
    )
    assert _route_after_scoring(state) == "rejected"


# ---------------- 3. 收到邮件之后 ----------------

def test_route_after_email_reply_timeout(state_factory):
    state = state_factory(candidate_email_reply=EMAIL_REPLY_TIMEOUT)
    assert route_after_email_reply(state) == "negotiation_expired"


def test_route_after_email_reply_normal(state_factory):
    state = state_factory(candidate_email_reply="我周三下午有空")
    assert route_after_email_reply(state) == "check_availability"


# ---------------- 4. check_availability 之后 ----------------

def test_route_check_availability_refused(state_factory):
    state = state_factory(candidate_email_reply=REPLY_REFUSED)
    assert route_after_check_availability(state) == "refused"


def test_route_check_availability_confirmed(state_factory):
    state = state_factory(candidate_email_reply=REPLY_TIME_AVAILABLE)
    assert route_after_check_availability(state) == "confirmed"


def test_route_check_availability_continue_negotiation(state_factory):
    state = state_factory(
        candidate_email_reply="下周一可以吗",
        negotiation_round=1,
        max_negotiation_rounds=3,
    )
    assert route_after_check_availability(state) == "get_candidate_email_reply_node"


def test_route_check_availability_exhausted_rounds(state_factory):
    state = state_factory(
        candidate_email_reply="下周一可以吗",
        negotiation_round=3,
        max_negotiation_rounds=3,
    )
    assert route_after_check_availability(state) == "negotiation_expired"


def test_route_check_availability_default_round_limit(state_factory):
    """不传 max_negotiation_rounds 时用默认值（3）。"""
    state = state_factory(candidate_email_reply="再约", negotiation_round=5)
    assert route_after_check_availability(state) == "negotiation_expired"


# ---------------- 统一的 error 守卫 ----------------

def test_error_guard_short_circuits(state_factory):
    router = _with_error_check(_route_after_screening)
    state = state_factory(screening_passed=True, error_occurred=True)
    assert router(state) == "error"


def test_error_guard_delegates_when_no_error(state_factory):
    router = _with_error_check(_route_after_screening)
    state = state_factory(screening_passed=True, error_occurred=False)
    assert router(state) == "scoring"


@pytest.mark.parametrize(
    "router",
    [
        _route_after_screening,
        _route_after_scoring,
        route_after_email_reply,
        route_after_check_availability,
    ],
)
def test_all_routes_respect_error_guard(state_factory, router):
    """4 条路由都被同一个守卫包住：标记错误后一律去 error 节点。"""
    guarded = _with_error_check(router)
    assert guarded(state_factory(error_occurred=True)) == "error"
