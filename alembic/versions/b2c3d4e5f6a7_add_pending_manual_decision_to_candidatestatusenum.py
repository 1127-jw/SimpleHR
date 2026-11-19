"""add pending_manual_decision to candidatestatusenum

Revision ID: b2c3d4e5f6a7
Revises: f1a2b3c4d5e6
Create Date: 2026-11-19 10:30:00.000000

背景：``CandidateStatusEnum`` 在 Python 侧新增了 ``PENDING_MANUAL_DECISION``
（人工闸门），但 ``candidates.status`` 是 **PostgreSQL ENUM** 类型
（``SQLAlchemyEnum``），只加 Python 值不同步到库里，写库会报
``invalid input value for enum candidatestatusenum: "待人工确认"``。

这个错误的连锁后果很隐蔽：闸门写状态失败被吞掉 → 状态仍是「AI筛选成功」→
前端看不到「发送邮件」按钮；而候选人随后被误 resume 走 reject 分支，
状态被 ``rejected_node`` 改成「AI筛选失败」——**评分达标却显示筛选失败**。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 人工闸门新增的状态：AI 评分通过后、发送邀请邮件前
    op.execute(
        "ALTER TYPE candidatestatusenum ADD VALUE IF NOT EXISTS '待人工确认'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # PostgreSQL 不支持直接删除枚举值，需要重建类型
    pass
