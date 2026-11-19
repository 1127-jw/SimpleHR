"""中断节点：等待候选人邮件回复。

单独成文件：图里每个节点一个 ``*.py``，节点之间不互相藏函数。
"""
from __future__ import annotations

import logging

from langgraph.types import interrupt

from app.core.config import get_settings
from app.workflow.candidate_state import (
    EMAIL_REPLY_INTERRUPT,
    CandidateRecruitmentState,
)

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


async def get_candidate_email_reply_node(state: CandidateRecruitmentState):
    """中断节点：等待候选人邮件回复。

    这里打一条「挂起前」的日志即可：``thread_id`` 的真正用武之地是**恢复侧**
    （``notify._resume_candidate_graph`` / 超时巡检发出 ``Command(resume=...)``
    的那一刻），在那之前打出「恢复线程」只会误导排查。

    注意：**不要**给这个节点加 ``@node_error_wrapper``。它靠 ``interrupt()``
    抛出的 ``GraphInterrupt`` 让图暂停，而包装器的「未知异常」分支会把
    ``GraphInterrupt`` 吞成 ``AppException``，图就永远不会暂停了。
    """
    cid = state["candidate"].id
    logger.info(
        "[email_reply] 挂起，等待候选人回复 | candidate_id=%s | thread_id=%s",
        cid, state["candidate"].email,
    )

    reply = interrupt(EMAIL_REPLY_INTERRUPT)
    if reply is None:
        reply = ""

    return {
        "candidate_email_reply": reply,
        "current_stage": "email_reply",
    }
