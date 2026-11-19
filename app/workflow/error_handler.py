"""LangGraph 节点的异常处理基础设施。

提供 ``node_error_wrapper`` 装饰器：加在节点函数上，把 ``BusinessException``
转为错误状态返回，让状态图继续流转到 error 节点；``ExternalServiceError``
则继续向上传播，由外层捕获。

用法（在节点函数上直接使用装饰器）::

    # screening_node.py
    @node_error_wrapper
    async def screening_node(state: CandidateRecruitmentState) -> dict:
        ...

然后正常注册到状态图::

    graph.add_node("screening", screening_node)

错误终结点节点在 ``candidate_nodes/error_node.py``。
"""
import logging
from functools import wraps
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

from app.workflow.candidate_state import CandidateRecruitmentState
from app.core.exceptions import AppException, BusinessException, ExternalServiceError


def node_error_wrapper(
    node_func: Callable[[CandidateRecruitmentState], Awaitable[dict]],
) -> Callable[[CandidateRecruitmentState], Awaitable[dict]]:
    """节点函数装饰器。

    - 捕获 ``BusinessException`` 及其子类 → 返回错误状态 dict，让状态图流转到 error 节点。
    - ``ExternalServiceError`` 及其子类 → 不捕获，继续向上传播。
    - 其他未知异常 → 包装为 ``AppException`` 后向上传播（防御性处理）。

    直接用在节点函数上::

        @node_error_wrapper
        async def scoring_node(state: CandidateRecruitmentState) -> dict:
            ...
    """

    @wraps(node_func)
    async def wrapped(state: CandidateRecruitmentState) -> dict:
        try:
            return await node_func(state)
        except BusinessException as e:
            logger.warning(
                "[error_wrap] %s BusinessException: %s (code=%s) | "
                "candidate_id=%s | routing to error node",
                node_func.__name__, e.message, e.code, state["candidate"].id,
            )
            return {
                "current_stage": "error",
                "error_occurred": True,
                "error_message": e.message,
                "error_stage": state.get("current_stage"),
            }
        except ExternalServiceError:
            raise
        except Exception as e:
            logger.error(
                "[error_wrap] %s unexpected exception | candidate_id=%s",
                node_func.__name__, state["candidate"].id,
                exc_info=True,
            )
            raise AppException(
                f"节点 {node_func.__name__} 未知异常: {e}"
            ) from e

    return wrapped
