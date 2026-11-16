"""意图/槽位公共 schema。

``ProposedTime`` 由 ``utils/emailbot/intent.py``（邮件级分流）与
``app/workflow/candidate_nodes/check_availability_node.py``（图内时间精抽取）共用，
集中在此避免两处提示词/结构漂移。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ProposedTime(BaseModel):
    """候选人回复的结构化抽取结果。"""

    confirm: bool = Field(
        description="True 表示邮件内容为同意上次提供的邀约时间，False 表示邮件不同意上次提供的邀约时间"
    )
    refused: bool = Field(
        description="True 表示邮件内容为回拒该次面试，False 表示邮件中提供了面试时间"
    )
    proposed_time: str = Field(
        description="面试时间，格式：YYYY-MM-DD HH:MM:SS"
    )
    exactly: str = Field(
        description="am/pm 表示上午/下午；如果是准确时间则为hours"
    )
