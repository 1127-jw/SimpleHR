"""安全审计日志。

与业务日志分开：独立的 ``audit`` logger + ``logs/audit.log``（JSON Lines），
便于对接 SIEM / 审计系统。

约定：**只记录安全敏感操作**，不要往里写普通业务日志。

    audit("login.success", user_id="u1", ip="1.2.3.4")
    audit("login.failed",  detail="bad password", email="a@b.com")
    audit("resume.download", user_id="u1", target="candidate:123")
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.core.request_context import get_request_id

# 审计日志的 logger 名；logging_config 会为它单独挂 audit.log
AUDIT_LOGGER_NAME = "audit"

_audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)


def audit(action: str, **fields: Any) -> None:
    """写一条审计记录。

    Args:
        action: 动作标识，建议 ``模块.动作``，如 ``login.success``。
        **fields: 附加字段（user_id / target / ip / detail ...）。
    """
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "request_id": get_request_id(),
        "action": action,
        **fields,
    }
    _audit_logger.info(json.dumps(payload, ensure_ascii=False, default=str))
