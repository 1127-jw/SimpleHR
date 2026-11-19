"""candidate status history

Revision ID: ccbe36322325
Revises: b2c3d4e5f6a7
Create Date: 2026-11-19 14:07:55.473468

落地顺序：**先建模型**（``CandidateStatusHistoryModel`` + ``CandidateModel.archived_at``）
→ ``alembic revision --autogenerate`` → **手工裁剪/补充**。

本迁移刻意只包含四件事：枚举加值、``candidates.archived_at``、状态变更链表、回填历史数据。

> ⚠️ autogenerate 另外报了两类**与本次无关**的差异，已剔除，不要合进来：
> 1. LangGraph 内部表 ``checkpoints`` / ``checkpoint_blobs`` / ``checkpoint_writes`` /
>    ``checkpoint_migrations`` 被当成「库里有、模型里没有」→ 生成删表语句。
>    这些表由 LangGraph 自己管理，**删掉等于清空所有流程检查点**。
>    （根因：``alembic/env.py`` 未配置 ``include_object`` 过滤非本项目表。）
> 2. ``mail_resume_submissions`` 的索引名/可空性与模型不一致——既有漂移，独立处理。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'ccbe36322325'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 归档态：图流程终止（淘汰/放弃/异常收尾）
_ARCHIVED_STATUSES = ('已入职', '已拒绝', '拒绝面试', '协商超时', '处理异常', 'AI筛选失败')
# 结算态：流程正常完成，转人工维护（confirm 确认面试时间）
_SETTLED_STATUSES = ('待面试',)


def upgrade() -> None:
    """Upgrade schema."""
    # 1) 枚举加值：新增「面试时间协商中」。
    #    ALTER TYPE ... ADD VALUE 无法回滚（PostgreSQL 限制），且不影响既有数据。
    op.execute(
        "ALTER TYPE candidatestatusenum ADD VALUE IF NOT EXISTS '面试时间协商中'"
    )

    # 2) 归档时间：图流程终止时写入，NULL 表示未归档。
    #    刻意不把 status 覆盖成「已归档」——细分终态留给 HR 事后标注，
    #    旧的按状态筛选/统计查询因此一行都不用改。
    op.add_column('candidates', sa.Column('archived_at', sa.DateTime(), nullable=True))
    op.create_index(
        op.f('ix_candidates_archived_at'), 'candidates', ['archived_at'], unique=False
    )

    # 3) 状态变更链表
    op.create_table(
        'candidate_status_history',
        sa.Column('candidate_id', sa.String(length=100), nullable=False),
        sa.Column('candidate_email', sa.String(length=255), nullable=False),
        sa.Column('position_id', sa.String(length=100), nullable=True),
        sa.Column('position_title', sa.String(length=255), nullable=True),
        sa.Column('from_status', sa.String(length=50), nullable=True),
        sa.Column('to_status', sa.String(length=50), nullable=False),
        sa.Column('changed_at', sa.DateTime(), nullable=False),
        sa.Column('last_email_received_at', sa.DateTime(), nullable=True),
        sa.Column('next_id', sa.String(length=100), nullable=True),
        sa.Column('id', sa.String(length=100), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_candidate_status_history')),
    )
    op.create_index(
        op.f('ix_candidate_status_history_candidate_id'),
        'candidate_status_history', ['candidate_id'], unique=False,
    )
    # 部分唯一索引：每个候选人最多一条「链上最新」记录，
    # 防止并发写入把 next_id 链写断（超时巡检判据依赖它）
    op.create_index(
        'ux_csh_active', 'candidate_status_history', ['candidate_id'], unique=True,
        postgresql_where=sa.text('next_id IS NULL'),
    )

    # 4) 回填历史数据。
    #    老候选人没有链表记录，不补的话「建图查重」会认为该邮箱无记录 → 允许重复建图。
    status_case = "CASE WHEN c.status IN ({archived}) THEN '-1' " \
                  "WHEN c.status IN ({settled}) THEN '-2' ELSE NULL END".format(
        archived=', '.join("'%s'" % s for s in _ARCHIVED_STATUSES),
        settled=', '.join("'%s'" % s for s in _SETTLED_STATUSES),
    )
    op.execute(f"""
        INSERT INTO candidate_status_history
            (id, candidate_id, candidate_email, position_id, position_title,
             from_status, to_status, changed_at, last_email_received_at, next_id,
             created_at, updated_at)
        SELECT
            'csh_' || c.id,
            c.id,
            c.email,
            c.position_id,
            p.title,
            NULL,
            c.status,
            COALESCE(c.updated_at, c.created_at),
            c.email_invite_sent_at,
            {status_case},
            COALESCE(c.updated_at, c.created_at),
            COALESCE(c.updated_at, c.created_at)
        FROM candidates c
        LEFT JOIN positions p ON p.id = c.position_id
        WHERE NOT EXISTS (
            SELECT 1 FROM candidate_status_history h WHERE h.candidate_id = c.id
        )
    """)

    # 回填归档标记，与链表的 '-1' 对齐
    op.execute(
        "UPDATE candidates SET archived_at = updated_at "
        "WHERE status IN ({}) AND archived_at IS NULL".format(
            ', '.join("'%s'" % s for s in _ARCHIVED_STATUSES)
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ux_csh_active', table_name='candidate_status_history',
                  postgresql_where=sa.text('next_id IS NULL'))
    op.drop_index(op.f('ix_candidate_status_history_candidate_id'),
                  table_name='candidate_status_history')
    op.drop_table('candidate_status_history')
    op.drop_index(op.f('ix_candidates_archived_at'), table_name='candidates')
    op.drop_column('candidates', 'archived_at')
    # candidatestatusenum 的 '面试时间协商中' 删不掉（PostgreSQL 限制），
    # 留在类型里不影响任何逻辑（没有行会引用它）。
