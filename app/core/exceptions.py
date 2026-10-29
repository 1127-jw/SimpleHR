"""应用自定义异常层级。

设计说明
--------
所有业务异常继承自 ``AppException``，便于中间件/全局异常处理器统一捕获并转换为 HTTP 响应。

层级结构::

    AppException
    ├── BusinessException          # 业务逻辑错误（面向用户）
    │   ├── ResumeProcessingError
    │   ├── CandidateNotFoundError
    │   ├── InterviewSchedulingError
    │   ├── TokenExpiredError
    │   ├── TokenInvalidError
    │   ├── DingTalkAPIError
    │   ├── EmailSendError
    │   ├── StatusFlowError
    │   └── NegotiationError
    └── ExternalServiceError       # 第三方服务错误（面向运维）
        ├── OCRServiceError
        ├── LLMServiceError
        └── DingTalkServiceError

用法::

    raise CandidateNotFoundError(candidate_id="xxx")

    try:
        ...
    except LLMServiceError as e:
        logger.error(f"LLM 调用失败: {e}")
        raise
"""

from typing import Optional


# ==================== 基类 ====================

class AppException(Exception):
    """应用异常基类。"""

    def __init__(self, message: str, code: Optional[str] = None):
        self.message = message
        self.code = code or self.__class__.__name__
        super().__init__(self.message)


# ==================== 业务异常（面向用户） ====================

class BusinessException(AppException):
    """业务逻辑错误基类，HTTP 4xx 范畴。"""
    pass


class ResumeProcessingError(BusinessException):
    """简历处理相关错误。"""
    pass


class CandidateNotFoundError(BusinessException):
    """候选人不存在。"""
    pass


class InterviewSchedulingError(BusinessException):
    """面试时间排期相关错误。"""
    pass


class TokenExpiredError(BusinessException):
    """令牌已过期。"""
    pass


class TokenInvalidError(BusinessException):
    """令牌无效。"""
    pass


class DingTalkAPIError(BusinessException):
    """钉钉 API 调用错误。"""
    pass


class EmailSendError(BusinessException):
    """邮件发送错误。"""
    pass


class StatusFlowError(BusinessException):
    """状态流转非法。"""
    pass


class NegotiationError(BusinessException):
    """面试时间协商相关错误。"""
    pass


# ==================== 第三方服务异常（面向运维） ====================

class ExternalServiceError(AppException):
    """第三方服务调用错误基类，HTTP 502/503 范畴。"""
    pass


class OCRServiceError(ExternalServiceError):
    """OCR 识别服务错误。"""
    pass


class LLMServiceError(ExternalServiceError):
    """大模型服务调用错误。"""
    pass


class DingTalkServiceError(ExternalServiceError):
    """钉钉服务调用错误（区别于业务层的 DingTalkAPIError）。"""
    pass
