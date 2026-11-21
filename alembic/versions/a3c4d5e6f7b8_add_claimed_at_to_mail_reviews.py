"""add claimed_at to mail_reviews

Revision ID: a3c4d5e6f7b8
Revises: f2b3c4d5e6a7
Create Date: 2026-11-21 00:30:00.000000

背景
----
意图复核的「并发抢占」需要一个**租约起点**。原先复用了 ``resolved_at``，
但它表示「处理完成时间」并且已经通过 API 暴露给前端——把两种语义压在同一列上
会让「执行中」的行看起来像「已完成」，也无法区分「谁在什么时候开始处理」。

因此拆开：

* ``claimed_at`` —— 认领时间，仅执行期间非空，动作失败会清空，用于租约判断；
* ``resolved_at`` —— 处理完成时间，只在最终 resolve 时写入，面向展示与统计。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c4d5e6f7b8'
down_revision: Union[str, Sequence[str], None] = 'f2b3c4d5e6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'mail_reviews',
        sa.Column('claimed_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('mail_reviews', 'claimed_at')
