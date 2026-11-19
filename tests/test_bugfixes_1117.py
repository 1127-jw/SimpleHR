"""2026-11-17 缺陷修复的回归测试。

对应 ``11-17-bug.md`` 里列出的每一条问题，一条或数条断言锁定一个行为。
全部为纯内存测试：不碰数据库 / Redis / 网络 / LLM。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, time

import pytest
from fastapi import HTTPException

from app.core import auth
from app.core import rate_limit
from app.core.exceptions import InterviewSchedulingError
from app.core.rate_limit import _SLIDING_WINDOW_LUA
from app.core.time import BEIJING_TZ, to_iso8601
from app.repository.mail_submission_repo import MailSubmissionRepo
from app.schemas.intent_schema import ProposedTime
from app.service import dingtalk_ser
from app.workflow.candidate_nodes import check_availability_node as node_mod
from app.workflow.candidate_nodes import confirm_node as confirm_mod
from app.workflow.candidate_nodes import negotiation_expired_node as expired_mod
from app.workflow.candidate_state import REPLY_TIME_AVAILABLE

router_mod = sys.modules.get("app.routers.candidate_router") or __import__(
    "app.routers.candidate_router", fromlist=["candidate_router"]
)

DAY = datetime(2026, 11, 20, tzinfo=BEIJING_TZ)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


@dataclass
class _Candidate:
    id: str = "cand-001"
    name: str = "张三"
    email: str = "c@example.com"


@dataclass
class _Interviewer:
    id: str = "user-001"
    email: str = "hr@example.com"


@dataclass
class _Position:
    title: str = "后端工程师"


def _state(**overrides) -> dict:
    state = {
        "candidate": _Candidate(),
        "position": _Position(),
        "interviewer": _Interviewer(),
        "current_stage": "email_reply",
        "negotiation_round": 0,
        "max_negotiation_rounds": 3,
        "candidate_email_reply": "20号上午方便",
    }
    state.update(overrides)
    return state


def _patch_llm(monkeypatch, **fields) -> None:
    async def _fake_extract(state, reply):
        return ProposedTime(**fields)

    monkeypatch.setattr(node_mod, "_extract_time_from_reply", _fake_extract)


class _RecordingBot:
    """记录发信内容的假邮件机器人。"""

    is_connected = True

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def connect(self) -> None:
        pass

    async def send_email(self, **kwargs) -> None:
        self.sent.append(kwargs)


class _BrokenBot:
    is_connected = True

    async def connect(self) -> None:
        pass

    async def send_email(self, **kwargs) -> None:
        raise RuntimeError("SMTP 挂了")


# =====================================================================
# H1：模糊提议（上午/下午/全天）必须换成具体时刻
# =====================================================================

class TestResolveInterviewSlot:
    """旧实现把「当日 00:00」当成面试时间，会把面试排到零点。"""

    def test_hours_returns_candidate_exact_time(self):
        slots = [(at(9), at(10))]
        got = node_mod.resolve_interview_slot(
            "hours", slots, at(9, 30), timedelta(minutes=30)
        )
        assert got == at(9, 30)

    def test_hours_rejects_busy_time(self):
        slots = [(at(9), at(9, 30))]
        assert node_mod.resolve_interview_slot(
            "hours", slots, at(10), timedelta(minutes=30)
        ) is None

    def test_am_returns_slot_start_not_midnight(self):
        """「上午方便」的 proposed_dt 是 00:00，返回的必须是窗口内的真实起点。"""
        slots = [(at(9), at(10))]
        got = node_mod.resolve_interview_slot(
            "am", slots, DAY, timedelta(minutes=30), node_mod.DAILY_WINDOWS["am"]
        )
        assert got == at(9)
        assert got.hour != 0, "面试时间不能被排到零点"

    def test_pm_returns_slot_start(self):
        slots = [(at(14, 30), at(15, 30))]
        got = node_mod.resolve_interview_slot(
            "pm", slots, DAY, timedelta(minutes=30), node_mod.DAILY_WINDOWS["pm"]
        )
        assert got == at(14, 30)

    def test_am_ignores_slot_outside_window(self):
        """只有下午有空，但候选人说上午 —— 不能拿下午的空档去顶。"""
        slots = [(at(15), at(16))]
        assert node_mod.resolve_interview_slot(
            "am", slots, DAY, timedelta(minutes=30), node_mod.DAILY_WINDOWS["am"]
        ) is None

    def test_am_requires_full_duration_inside_window(self):
        """窗口内剩余不足一场面试时长时不可用。"""
        slots = [(at(11, 45), at(12))]
        assert node_mod.resolve_interview_slot(
            "am", slots, DAY, timedelta(minutes=30), node_mod.DAILY_WINDOWS["am"]
        ) is None

    def test_all_returns_first_long_enough_slot(self):
        slots = [(at(9), at(9, 15)), (at(14), at(15))]
        got = node_mod.resolve_interview_slot("all", slots, DAY, timedelta(minutes=30))
        assert got == at(14)

    def test_unknown_window_strategy_raises(self):
        with pytest.raises(Exception):
            node_mod.resolve_interview_slot("pm", [(at(14), at(15))], DAY, timedelta(minutes=30), None)


async def test_am_proposal_sets_concrete_time(monkeypatch):
    """节点输出的 proposed_interview_time 必须是具体时刻，不是当日 00:00。"""
    _patch_llm(
        monkeypatch,
        confirm=False,
        refused=False,
        proposed_time=f"{DAY:%Y-%m-%d} 00:00:00",
        exactly="am",
    )

    async def _slots(**kwargs):
        return [(at(9), at(10))]

    monkeypatch.setattr(node_mod, "get_user_available_slots", _slots)

    result = await node_mod.check_availability_node(_state())

    assert result["candidate_email_reply"] == REPLY_TIME_AVAILABLE
    assert result["proposed_interview_time"] == at(9)
    assert result["proposed_interview_time"].hour != 0


async def test_all_proposal_sets_concrete_time(monkeypatch):
    _patch_llm(
        monkeypatch,
        confirm=False,
        refused=False,
        proposed_time=f"{DAY:%Y-%m-%d} 00:00:00",
        exactly="all",
    )

    async def _slots(**kwargs):
        return [(at(14), at(15))]

    monkeypatch.setattr(node_mod, "get_user_available_slots", _slots)

    result = await node_mod.check_availability_node(_state())

    assert result["proposed_interview_time"] == at(14)


# =====================================================================
# H1b：提议时间解析更宽容
# =====================================================================

class TestParseProposedTime:
    @pytest.mark.parametrize(
        "raw",
        [
            "2026-11-20 10:30:00",
            "2026-11-20 10:30",
            "2026-11-20T10:30:00",
            "2026-11-20T10:30:00+08:00",
            "2026-11-20 10:30:00.123456",
        ],
    )
    def test_accepts_common_variants(self, raw):
        assert node_mod.parse_proposed_time(raw) == at(10, 30)

    def test_is_timezone_aware(self):
        assert node_mod.parse_proposed_time("2026-11-20 10:30:00").tzinfo is not None

    @pytest.mark.parametrize("raw", ["", "   ", "明天上午", "2026/13/45 99:99"])
    def test_rejects_garbage(self, raw):
        with pytest.raises(node_mod.NegotiationError):
            node_mod.parse_proposed_time(raw)


# =====================================================================
# H2：无空闲时段时候选人回「确认」不再 KeyError
# =====================================================================

async def test_confirm_without_proposed_time_negotiates_instead_of_crashing(monkeypatch):
    """首次邀请走了「7 天内无空档」分支，state 里没有任何具体时间。

    此时候选人回「可以」，若直接放行到 confirm 分支，confirm_node 会抛
    KeyError（读 state["proposed_interview_time"]）。必须回到协商。
    """
    _patch_llm(monkeypatch, confirm=True, refused=False, proposed_time="", exactly="")
    state = _state()
    state.pop("proposed_interview_time", None)

    result = await node_mod.check_availability_node(state)

    assert result.get("candidate_email_reply") != REPLY_TIME_AVAILABLE
    assert result.get("negotiation_round") == 1


async def test_confirm_with_proposed_time_still_works(monkeypatch):
    """对照：正常邀约过的候选人回「确认」，行为不变。"""
    _patch_llm(monkeypatch, confirm=True, refused=False, proposed_time="", exactly="")
    result = await node_mod.check_availability_node(
        _state(proposed_interview_time=at(10, 30))
    )

    assert result["candidate_email_reply"] == REPLY_TIME_AVAILABLE


async def test_confirm_node_raises_readable_error_when_time_missing():
    """confirm_node 兜底：缺时间要抛可读的业务异常，而不是裸 KeyError。"""
    state = _state()
    state.pop("proposed_interview_time", None)

    result = await confirm_mod.confirm_node(state)

    # node_error_wrapper 把 BusinessException 转成错误状态
    assert result["error_occurred"] is True
    assert "面试时间" in result["error_message"]


# =====================================================================
# M5：confirm_node 先排日程再落库，外部失败不留脏数据
# =====================================================================

class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    def reset(self) -> None:
        self.events.clear()


async def _noop(*args, **kwargs):
    return None


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def begin(self):
        return _FakeTx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_calendar_created_before_db_commit(monkeypatch):
    """日程必须先建：钉钉失败时数据库里不该出现「待面试」。"""
    order = _Recorder()

    async def _calendar(**kwargs):
        order.events.append("calendar")

    async def _token(interviewer):
        return ("union-1", {"access_token": "tk"})

    class _Repo:
        def __init__(self, session=None):
            pass

        async def get_by_candidate_id(self, cid):
            order.events.append("db_read")
            return None

        async def create_interview(self, data):
            order.events.append("db_write")

        async def update_candidate_status(self, **kwargs):
            order.events.append("db_status")

    bot = _RecordingBot()

    monkeypatch.setattr(confirm_mod, "get_access_token", _token)
    monkeypatch.setattr(confirm_mod, "create_calendar", _calendar)
    monkeypatch.setattr(confirm_mod, "AsyncSessionFactory", lambda: _FakeSession())
    monkeypatch.setattr(confirm_mod, "InterviewRepo", _Repo)
    monkeypatch.setattr(confirm_mod, "CandidateRepo", _Repo)
    monkeypatch.setattr("utils.emailbot.mail_manager.get_mail_bot_", lambda: bot)

    result = await confirm_mod.confirm_node(_state(proposed_interview_time=at(10, 30)))

    assert result["current_stage"] == "confirmed"
    assert order.events.index("calendar") < order.events.index("db_write")
    assert len(bot.sent) == 1


async def test_calendar_failure_leaves_db_untouched(monkeypatch):
    """钉钉建日程失败 → 一行数据库都不该写。

    ``DingTalkServiceError`` 属于 ``ExternalServiceError``，按设计**不**被
    ``node_error_wrapper`` 吞成错误状态，而是向上抛出由外层统一处理；
    关键在于抛之前数据库里什么都没留下。
    """
    order = _Recorder()

    async def _calendar(**kwargs):
        raise RuntimeError("钉钉 500")

    async def _token(interviewer):
        return ("union-1", {"access_token": "tk"})

    class _Repo:
        def __init__(self, session=None):
            pass

        async def get_by_candidate_id(self, cid):
            order.events.append("db_read")
            return None

        async def create_interview(self, data):
            order.events.append("db_write")

        async def update_candidate_status(self, **kwargs):
            order.events.append("db_status")

    monkeypatch.setattr(confirm_mod, "get_access_token", _token)
    monkeypatch.setattr(confirm_mod, "create_calendar", _calendar)
    monkeypatch.setattr(confirm_mod, "AsyncSessionFactory", lambda: _FakeSession())
    monkeypatch.setattr(confirm_mod, "InterviewRepo", _Repo)
    monkeypatch.setattr(confirm_mod, "CandidateRepo", _Repo)

    with pytest.raises(Exception):
        await confirm_mod.confirm_node(_state(proposed_interview_time=at(10, 30)))

    assert order.events == [], f"外部失败后不应写库，实际={order.events}"


# =====================================================================
# M1：协商失败 / 超时要给候选人回信
# =====================================================================

async def test_busy_slot_notifies_candidate(monkeypatch):
    """提议时间不可用时要回信，而不是静默等一封不会来的邮件。"""
    _patch_llm(
        monkeypatch,
        confirm=False,
        refused=False,
        proposed_time=f"{DAY:%Y-%m-%d} 10:30:00",
        exactly="hours",
    )

    async def _slots(**kwargs):
        return [(at(14), at(15))]

    bot = _RecordingBot()
    monkeypatch.setattr(node_mod, "get_user_available_slots", _slots)
    monkeypatch.setattr("utils.emailbot.mail_manager.get_mail_bot_", lambda: bot)

    result = await node_mod.check_availability_node(_state())

    assert result["negotiation_round"] == 1
    assert len(bot.sent) == 1
    assert "10:30" in bot.sent[0]["text"]


async def test_notification_failure_does_not_block_negotiation(monkeypatch):
    """回信是增强项：SMTP 挂了也必须照常推进轮次。"""
    _patch_llm(
        monkeypatch,
        confirm=False,
        refused=False,
        proposed_time=f"{DAY:%Y-%m-%d} 10:30:00",
        exactly="hours",
    )

    async def _slots(**kwargs):
        return [(at(14), at(15))]

    monkeypatch.setattr(node_mod, "get_user_available_slots", _slots)
    monkeypatch.setattr("utils.emailbot.mail_manager.get_mail_bot_", lambda: _BrokenBot())

    result = await node_mod.check_availability_node(_state())

    assert result["negotiation_round"] == 1


async def test_empty_reply_does_not_send_email(monkeypatch):
    """空回复没有「被拒绝的时间」，不该发信。"""
    bot = _RecordingBot()
    monkeypatch.setattr("utils.emailbot.mail_manager.get_mail_bot_", lambda: bot)

    result = await node_mod.check_availability_node(_state(candidate_email_reply=""))

    assert result["negotiation_round"] == 1
    assert bot.sent == []


async def test_negotiation_expired_notifies_candidate(monkeypatch):
    """协商超时要通知候选人，避免流程无声结束。"""

    class _Repo:
        def __init__(self, session=None):
            pass

        async def update_candidate_status(self, **kwargs):
            pass

    bot = _RecordingBot()
    monkeypatch.setattr(expired_mod, "AsyncSessionFactory", lambda: _FakeSession())
    monkeypatch.setattr(expired_mod, "CandidateRepo", _Repo)
    monkeypatch.setattr("utils.emailbot.mail_manager.get_mail_bot_", lambda: bot)

    result = await expired_mod.negotiation_expired_node(_state())

    assert result["current_stage"] == "negotiation_expired"
    assert len(bot.sent) == 1


async def test_expired_notification_failure_is_swallowed(monkeypatch):
    class _Repo:
        def __init__(self, session=None):
            pass

        async def update_candidate_status(self, **kwargs):
            pass

    monkeypatch.setattr(expired_mod, "AsyncSessionFactory", lambda: _FakeSession())
    monkeypatch.setattr(expired_mod, "CandidateRepo", _Repo)
    monkeypatch.setattr("utils.emailbot.mail_manager.get_mail_bot_", lambda: _BrokenBot())

    result = await expired_mod.negotiation_expired_node(_state())

    assert result["current_stage"] == "negotiation_expired"


# =====================================================================
# M2 / M3：钉钉日程字段防御
# =====================================================================

class TestDingTalkEventParsing:
    def test_datetime_event(self):
        event = {
            "start": {"dateTime": to_iso8601(at(10))},
            "end": {"dateTime": to_iso8601(at(11))},
        }
        assert dingtalk_ser.parse_event_boundary(event, "start") == at(10)
        assert dingtalk_ser.parse_event_boundary(event, "end") == at(11)

    def test_all_day_event_uses_date(self):
        """全天日程只给 date 字段，旧实现 KeyError。"""
        event = {"start": {"date": "2026-11-20"}, "end": {"date": "2026-11-21"}}
        assert dingtalk_ser.parse_event_boundary(event, "start") == at(0)
        assert dingtalk_ser.parse_event_boundary(event, "end") == DAY + timedelta(days=1)

    def test_missing_boundary_returns_none(self):
        assert dingtalk_ser.parse_event_boundary({}, "start") is None
        assert dingtalk_ser.parse_event_boundary({"start": {}}, "start") is None

    async def test_calendar_list_without_events_returns_empty_list(self, monkeypatch):
        """钉钉没有日程时可能不返回 events 字段，必须兜成 []。"""

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {}

        class _Client:
            async def get(self, *args, **kwargs):
                return _Resp()

        monkeypatch.setattr(dingtalk_ser, "get_http_client", lambda: _Client())

        got = await dingtalk_ser.get_calendar_list(
            union_id="u1", access_token="t", time_min=at(0), time_max=DAY + timedelta(days=1)
        )
        assert got == []

    async def test_calendar_list_null_body_returns_empty_list(self, monkeypatch):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return None

        class _Client:
            async def get(self, *args, **kwargs):
                return _Resp()

        monkeypatch.setattr(dingtalk_ser, "get_http_client", lambda: _Client())

        got = await dingtalk_ser.get_calendar_list(
            union_id="u1", access_token="t", time_min=at(0), time_max=DAY + timedelta(days=1)
        )
        assert got == []


# =====================================================================
# M4：巡检锁必须校验持有者
# =====================================================================

class _LockProbe:
    """在巡检执行期间模拟「锁过期后被别的 worker 抢走」。"""

    def __init__(self, redis, key: str, takeover_value: str) -> None:
        self.redis = redis
        self.key = key
        self.takeover_value = takeover_value

    async def list_stale_negotiating(self, cutoff, limit=200):
        self.redis.store[self.key] = self.takeover_value
        return []


@pytest.fixture
def sweep_env(monkeypatch, fake_redis):
    from app.tasks import timeout_tasks

    monkeypatch.setattr(timeout_tasks.settings, "GRAPH_OPEN", True)
    monkeypatch.setattr(timeout_tasks, "get_redis", lambda: fake_redis)

    class _Repo:
        def __init__(self, session=None):
            pass

        async def list_stale_negotiating(self, cutoff, limit=200):
            return []

    monkeypatch.setattr(timeout_tasks, "AsyncSessionFactory", lambda: _FakeSession())
    monkeypatch.setattr(timeout_tasks, "CandidateStatusHistoryRepo", _Repo)
    return timeout_tasks


async def test_sweep_releases_lock_with_owner_token(sweep_env, fake_redis):
    from app.tasks.timeout_tasks import _SWEEP_LOCK_KEY

    assert await sweep_env.sweep_stale_email_replies() == 0
    assert _SWEEP_LOCK_KEY not in fake_redis.store, "本轮应释放自己加的锁"


async def test_sweep_skips_when_lock_held(sweep_env, fake_redis):
    from app.tasks.timeout_tasks import _SWEEP_LOCK_KEY

    fake_redis.store[_SWEEP_LOCK_KEY] = "another-worker"

    assert await sweep_env.sweep_stale_email_replies() == 0
    assert fake_redis.store[_SWEEP_LOCK_KEY] == "another-worker", "别人的锁不能被改动"


async def test_sweep_does_not_delete_lock_taken_over_by_peer(monkeypatch, fake_redis):
    """本轮超时后锁被别的 worker 抢走，收尾时不能把别人的锁删掉。"""
    from app.tasks import timeout_tasks
    from app.tasks.timeout_tasks import _SWEEP_LOCK_KEY

    monkeypatch.setattr(timeout_tasks.settings, "GRAPH_OPEN", True)
    monkeypatch.setattr(timeout_tasks, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(timeout_tasks, "AsyncSessionFactory", lambda: _FakeSession())
    monkeypatch.setattr(
        timeout_tasks,
        "CandidateStatusHistoryRepo",
        lambda session=None: _LockProbe(fake_redis, _SWEEP_LOCK_KEY, "peer-worker"),
    )

    await timeout_tasks.sweep_stale_email_replies()

    assert fake_redis.store[_SWEEP_LOCK_KEY] == "peer-worker", (
        "释放锁必须校验持有者，否则会删掉别的 worker 的锁导致并发巡检"
    )


# =====================================================================
# H3：AI 评分接口必须校验可见性
# =====================================================================

@dataclass
class _User:
    id: str = "user-001"
    is_hr: bool = True
    is_superuser: bool = False


def _install_score_repos(monkeypatch, *, visible: bool) -> list:
    calls: list = []

    class _CandidateRepo:
        def __init__(self, session=None):
            pass

        async def get_for_user(self, candidate_id, current_user):
            calls.append("get_for_user")
            return object() if visible else None

    class _ScoreRepo:
        def __init__(self, session=None):
            pass

        async def get_by_candidate_id(self, candidate_id):
            calls.append("get_by_candidate_id")
            return {"overall_score": 9}

    monkeypatch.setattr(router_mod, "CandidateRepo", _CandidateRepo)
    monkeypatch.setattr(router_mod, "CandidateAIScoreRepo", _ScoreRepo)
    return calls


async def test_ai_score_rejects_invisible_candidate(monkeypatch):
    calls = _install_score_repos(monkeypatch, visible=False)

    with pytest.raises(HTTPException) as exc:
        await router_mod.get_candidate_ai_score(
            candidate_id="cand-001", session=_FakeSession(), current_user=_User()
        )

    assert exc.value.status_code == 404
    assert calls == ["get_for_user"], "无权访问时不应再去读评分"


async def test_ai_score_allows_visible_candidate(monkeypatch):
    calls = _install_score_repos(monkeypatch, visible=True)

    got = await router_mod.get_candidate_ai_score(
        candidate_id="cand-001", session=_FakeSession(), current_user=_User()
    )

    assert got == {"ai_score": {"overall_score": 9}}
    assert calls == ["get_for_user", "get_by_candidate_id"]


# =====================================================================
# M6：投递列表分页
# =====================================================================

class _PagedSession:
    def __init__(self, total: int, rows: list) -> None:
        self.total = total
        self.rows = rows
        self.statements: list = []

    async def scalar(self, stmt):
        self.statements.append(stmt)
        return self.total

    async def scalars(self, stmt):
        self.statements.append(stmt)
        return list(self.rows)


async def test_list_by_status_page_returns_total_and_slices():
    rows = [object(), object()]
    session = _PagedSession(total=57, rows=rows)

    got_rows, total = await MailSubmissionRepo(session).list_by_status_page(
        "CONFIRMED", page=3, size=20
    )

    assert total == 57
    assert got_rows == rows
    paged = session.statements[-1]
    assert paged._limit == 20, "必须有 LIMIT"
    assert paged._offset == 40, "page=3 & size=20 → OFFSET 40"


# =====================================================================
# L：限流 limit<=0 表示关闭
# =====================================================================

async def test_rate_limit_disabled_when_limit_is_zero(fake_redis):
    for _ in range(5):
        assert await rate_limit.hit(fake_redis, "k", limit=0, window=60) is True


async def test_rate_limit_still_blocks_positive_limit(fake_redis):
    assert await rate_limit.hit(fake_redis, "k", limit=1, window=60) is True
    assert await rate_limit.hit(fake_redis, "k", limit=1, window=60) is False


# =====================================================================
# L：改密后吊销该用户所有 refresh token
# =====================================================================

async def test_password_change_revokes_previous_refresh_tokens(fake_redis):
    tokens = await auth.create_tokens("a@b.com", fake_redis)

    await auth.revoke_user_sessions("a@b.com", fake_redis)

    with pytest.raises(ValueError, match="revoked"):
        await auth.refresh_access_token(tokens["refresh_token"], fake_redis)


async def test_token_issued_after_password_change_still_works(fake_redis):
    await auth.revoke_user_sessions("a@b.com", fake_redis)
    fresh = await auth.create_tokens("a@b.com", fake_redis)

    renewed = await auth.refresh_access_token(fresh["refresh_token"], fake_redis)

    assert renewed["access_token"] != fresh["access_token"]


async def test_revocation_is_per_user(fake_redis):
    alice = await auth.create_tokens("alice@b.com", fake_redis)
    bob = await auth.create_tokens("bob@b.com", fake_redis)

    await auth.revoke_user_sessions("alice@b.com", fake_redis)

    with pytest.raises(ValueError, match="revoked"):
        await auth.refresh_access_token(alice["refresh_token"], fake_redis)
    assert await auth.refresh_access_token(bob["refresh_token"], fake_redis)


# =====================================================================
# L：缺失 refresh_token 不能变成字符串 "None"
# =====================================================================

async def test_missing_refresh_token_reports_reauth_instead_of_sending_none(fake_redis):
    """``str(await r.get(...))`` 在 key 缺失时得到 "None"（真值），

    旧实现会把它当成合法 refresh_token 发给钉钉，最终变成一个难以定位的 400。
    """
    with pytest.raises(HTTPException) as exc:
        await dingtalk_ser.get_user_access_token("u1", fake_redis)

    assert exc.value.status_code == 400
    assert "重新授权" in str(exc.value.detail)


async def test_existing_access_token_is_reused(fake_redis):
    await fake_redis.set("dingtalk:user_actoken:u1", "cached-token")

    got = await dingtalk_ser.get_user_access_token("u1", fake_redis)

    assert got["access_token"] == "cached-token"


# =====================================================================
# L：简历下载的 Content-Type 跟随真实扩展名
# =====================================================================

@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (".pdf", "application/pdf"),
        (".doc", "application/msword"),
        (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        (".bin", "application/octet-stream"),
    ],
)
def test_resume_response_media_type(work_dir, suffix, expected):
    path = work_dir / f"resume{suffix}"
    path.write_bytes(b"x")

    response = router_mod._resume_response(path)

    assert response.media_type == expected
    assert response.filename == f"resume{suffix}"
