"""日志分流：级别 + logger 名前缀。

对应 ``app/core/logging_config.py`` 的两层过滤：
1. handler 级别（``record.levelno >= handler.level``，由派发方判断）
2. logger 名前缀过滤器 ``_LoggerNameFilter``
"""
import io
import logging
import logging.handlers
import queue as queue_mod
import sys

import pytest

from app.core import logging_config as lc
from app.core.audit import AUDIT_LOGGER_NAME
from app.core.request_context import set_request_id


def _record(name: str, level: int, msg: str = "m") -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


# ---------------- 过滤器真值表 ----------------

def test_include_true_is_whitelist():
    f = lc._LoggerNameFilter(lc.NODE_LOGGER_NAME, include=True)
    assert f.filter(_record(lc.NODE_LOGGER_NAME, logging.INFO)) is True
    assert f.filter(_record(lc.NODE_LOGGER_NAME + ".child", logging.INFO)) is True
    assert f.filter(_record("app.demo", logging.INFO)) is False


def test_include_false_is_blacklist():
    f = lc._LoggerNameFilter(lc.NODE_LOGGER_NAME, include=False)
    assert f.filter(_record(lc.NODE_LOGGER_NAME, logging.INFO)) is False
    assert f.filter(_record("app.demo", logging.INFO)) is True


# ---------------- 实际落盘分流 ----------------

@pytest.fixture
def handlers(monkeypatch, work_dir):
    # console handler 在构造时捕获 sys.stdout，先替换掉避免刷屏
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    formatter = logging.Formatter(fmt=lc.LOG_FORMAT, datefmt=lc.DATE_FORMAT)
    built = lc._build_handlers(formatter, log_dir=work_dir)
    try:
        yield built, work_dir
    finally:
        for handler in built:
            handler.flush()
            handler.close()


def _dispatch(handlers, record: logging.LogRecord) -> None:
    """复刻监听进程的派发逻辑：先比 handler level，再交给 handle()（内部跑 filter）。"""
    for handler in handlers:
        if record.levelno >= handler.level:
            handler.handle(record)


def test_business_logs_go_to_expected_files(handlers):
    built, tmp = handlers

    _dispatch(built, _record("app.demo", logging.INFO, "plain-info"))
    _dispatch(built, _record("app.demo", logging.ERROR, "plain-error"))
    _dispatch(built, _record(lc.NODE_LOGGER_NAME, logging.INFO, "node-info"))
    _dispatch(built, _record(lc.NODE_LOGGER_NAME, logging.ERROR, "node-error"))

    workflow = (tmp / "workflow.log").read_text(encoding="utf-8")
    nodes = (tmp / "nodes.log").read_text(encoding="utf-8")
    errors = (tmp / "error.log").read_text(encoding="utf-8")

    # workflow.log：全量，但排除节点日志
    assert "plain-info" in workflow
    assert "plain-error" in workflow
    assert "node-info" not in workflow

    # nodes.log：只收节点日志（含 ERROR 级）
    assert "node-info" in nodes
    assert "node-error" in nodes
    assert "plain-info" not in nodes

    # error.log：只收 ERROR，且排除节点日志
    assert "plain-error" in errors
    assert "plain-info" not in errors
    assert "node-error" not in errors


def test_audit_log_is_isolated(handlers):
    built, tmp = handlers

    _dispatch(built, _record(AUDIT_LOGGER_NAME, logging.INFO, '{"action":"login.success"}'))

    audit = (tmp / "audit.log").read_text(encoding="utf-8")
    workflow = (tmp / "workflow.log").read_text(encoding="utf-8")

    assert "login.success" in audit
    assert "login.success" not in workflow


# ---------------- request_id ----------------

def test_request_id_filter_sets_default():
    f = lc._RequestIdFilter()
    record = _record("app.demo", logging.INFO)

    assert f.filter(record) is True
    assert record.request_id == "-"


def test_record_factory_injects_request_id():
    lc._install_record_factory()
    set_request_id("rid-123")

    record = logging.getLogger("app.demo").makeRecord(
        "app.demo", logging.INFO, __file__, 1, "m", (), None
    )

    assert record.request_id == "rid-123"


def test_log_format_carries_request_id(handlers):
    built, tmp = handlers

    _dispatch(built, _record("app.demo", logging.INFO, "with-req"))

    workflow = (tmp / "workflow.log").read_text(encoding="utf-8")
    assert "req=" in workflow


# ---------------- audit() 端到端 ----------------

def test_audit_call_reaches_audit_log(work_dir):
    """端到端：``audit()`` → audit logger → root QueueHandler → 监听线程 → audit.log。

    只测过滤器是不够的——那样发现不了「audit 没接进管道」这类问题。
    这里用真实的 QueueHandler + QueueListener 走一遍完整链路。
    """
    from app.core import audit as audit_mod

    formatter = logging.Formatter(fmt=lc.LOG_FORMAT, datefmt=lc.DATE_FORMAT)
    handlers_built = lc._build_handlers(formatter, log_dir=work_dir)

    log_queue: queue_mod.Queue = queue_mod.Queue(-1)
    listener = logging.handlers.QueueListener(
        log_queue, *handlers_built, respect_handler_level=True
    )
    listener.start()

    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.INFO)
    queue_handler = logging.handlers.QueueHandler(log_queue)
    root.addHandler(queue_handler)

    try:
        audit_mod.audit("login.success", user_id="u-1", ip="10.0.0.1")
    finally:
        listener.stop()                      # 投哨兵并等队列排空
        root.removeHandler(queue_handler)
        root.setLevel(previous_level)
        for handler in handlers_built:
            handler.close()

    audit_content = (work_dir / "audit.log").read_text(encoding="utf-8")
    workflow_content = (work_dir / "workflow.log").read_text(encoding="utf-8")

    # 审计内容是否真的落盘
    assert '"action": "login.success"' in audit_content
    assert '"user_id": "u-1"' in audit_content
    # 且没有混进业务日志
    assert "login.success" not in workflow_content


def test_audit_payload_is_valid_json(work_dir):
    """审计行必须是 JSON（便于对接 SIEM），且带 request_id。"""
    import json

    from app.core import audit as audit_mod

    formatter = logging.Formatter(fmt=lc.LOG_FORMAT, datefmt=lc.DATE_FORMAT)
    handlers_built = lc._build_handlers(formatter, log_dir=work_dir)

    log_queue: queue_mod.Queue = queue_mod.Queue(-1)
    listener = logging.handlers.QueueListener(
        log_queue, *handlers_built, respect_handler_level=True
    )
    listener.start()

    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.INFO)
    queue_handler = logging.handlers.QueueHandler(log_queue)
    root.addHandler(queue_handler)

    try:
        audit_mod.audit("resume.download", user_id="u-2", target="candidate:c-1")
    finally:
        listener.stop()
        root.removeHandler(queue_handler)
        root.setLevel(previous_level)
        for handler in handlers_built:
            handler.close()

    line = (work_dir / "audit.log").read_text(encoding="utf-8").strip().splitlines()[-1]
    payload = json.loads(line)

    assert payload["action"] == "resume.download"
    assert payload["user_id"] == "u-2"
    assert payload["target"] == "candidate:c-1"
    assert "ts" in payload and "request_id" in payload
