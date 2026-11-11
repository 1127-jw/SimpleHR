"""add indexes for list/statistics query paths + candidates.email_invite_sent_at

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-11-12 00:00:00.000000

背景
----
候选人列表页会按 ``position_id`` / ``status`` 过滤并按 ``created_at`` 倒序，
简历与职位也各自有按外键过滤的路径，但此前这些列上没有索引，
数据量增长后列表与统计查询会退化为全表扫描。

本迁移同时补上 ``candidates.email_invite_sent_at`` 列——
「等待候选人回复」超时巡检需要按该列做时间范围过滤。

索引全部使用 ``CREATE INDEX IF NOT EXISTS``，保证迁移可重复执行
（例如在与 LangGraph checkpoint 表并存的环境里手工重跑）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (索引名, 表名, 列)
_INDEXES = [
    ("ix_candidates_status", "candidates", ["status"]),
    ("ix_candidates_position_id", "candidates", ["position_id"]),
    ("ix_candidates_email", "candidates", ["email"]),
    ("ix_candidates_email_invite_sent_at", "candidates", ["email_invite_sent_at"]),
    # 列表页主路径：按职位 + 状态过滤，按创建时间倒序
    ("ix_candidates_position_status_created", "candidates", ["position_id", "status", "created_at"]),
    ("ix_candidate_ai_scores_candidate_id", "candidate_ai_scores", ["candidate_id"]),
    ("ix_resumes_uploader_id", "resumes", ["uploader_id"]),
    ("ix_positions_department_id", "positions", ["department_id"]),
    ("ix_positions_creator_id", "positions", ["creator_id"]),
    ("ix_users_department_id", "users", ["department_id"]),
    ("ix_interviews_scheduled_time", "interviews", ["scheduled_time"]),
]


def upgrade() -> None:
    """Upgrade schema."""
    # LangGraph 的 checkpoint_* 表由 LangGraph 自行管理，不纳入 Alembic 迁移
    op.add_column(
        'candidates',
        sa.Column('email_invite_sent_at', sa.DateTime(), nullable=True),
    )
    for name, table, columns in _INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({', '.join(columns)})"
        )


def downgrade() -> None:
    """Downgrade schema."""
    for name, table, columns in reversed(_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.drop_column('candidates', 'email_invite_sent_at')
