"""节点异常包装 ``@node_error_wrapper`` 的行为测试。

对应三层策略：
- BusinessException        → 返回 error state，让状态图流转到 error 节点
- ExternalServiceError     → 不捕获，向上传播
- 其他未知异常              → 包装成 AppException 后传播
"""
import pytest

from app.core.exceptions import (
    AppException,
    InterviewSchedulingError,
    LLMServiceError,
)
from app.workflow.error_handler import node_error_wrapper


def _node_raising(exc: Exception):
    @node_error_wrapper
    async def node(state):
        raise exc

    return node


async def test_business_exception_becomes_error_state(state_factory):
    node = _node_raising(InterviewSchedulingError("面试官 7 天内无空闲"))

    result = await node(state_factory())

    assert result["current_stage"] == "error"
    assert result["error_occurred"] is True
    assert "无空闲" in result["error_message"]
    # error_stage 记录的是抛出异常时所处的阶段
    assert result["error_stage"] == "screening"


async def test_external_service_error_propagates(state_factory):
    """基础设施异常不能被吞掉，否则外层无法标记 PROCESSING_ERROR。"""
    node = _node_raising(LLMServiceError("LLM 超时"))

    with pytest.raises(LLMServiceError):
        await node(state_factory())


async def test_unknown_exception_wrapped_into_app_exception(state_factory):
    node = _node_raising(ValueError("boom"))

    with pytest.raises(AppException) as excinfo:
        await node(state_factory())

    assert "boom" in excinfo.value.message
    # 原异常仍可通过 __cause__ 追溯
    assert isinstance(excinfo.value.__cause__, ValueError)


async def test_success_is_passed_through(state_factory):
    @node_error_wrapper
    async def node(state):
        return {"ok": True}

    assert await node(state_factory()) == {"ok": True}


async def test_wrapper_preserves_function_metadata():
    """@wraps 保留函数名，日志里才能显示 scoring_node 而不是 wrapped。"""

    @node_error_wrapper
    async def scoring_node(state):
        return {}

    assert scoring_node.__name__ == "scoring_node"
