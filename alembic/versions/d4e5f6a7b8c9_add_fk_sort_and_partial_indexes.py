"""add FK/sort indexes, partial sweep index, unique AI score; drop unused indexes

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-11-13 00:00:00.000000

背景
----
``c3d4e5f6a7b8`` 补了列表/统计主路径的索引，但仍有两类缺口：

1. 外键无索引。PostgreSQL 不会为外键自动建索引，以下列被真实查询使用：
   ``dingding_user.user_id``（按用户取钉钉账号 + 关系加载）、
   ``candidates.resume_id``（候选人详情加载简历）、
   ``candidates.creator_id`` / ``interviews.interviewer_id``（外键卫生）。
   ``candidates.resume_id`` 语义上是 1:1，直接建唯一索引。

2. 排序无索引。列表页 ``ORDER BY created_at DESC`` 在
   ``candidates`` / ``positions`` / ``users`` 上都没有与过滤条件组合的索引，
   数据量增长后出现 ``Sort`` 节点和全表扫描。

同时：
- ``candidates.email_invite_sent_at`` 改用部分索引，只覆盖「等待回复」行；
- ``candidate_ai_scores.candidate_id`` 改为唯一索引（模型关系为 1:1）；
- 删除未被任何 SQL 使用的 ``ix_candidates_phone_number`` /
  ``ix_interviews_scheduled_time``，以及被新复合索引左缀覆盖的
  ``ix_candidates_position_id`` / ``ix_positions_department_id`` /
  ``ix_users_department_id``。

注意
----
部分索引 ``ix_candidates_email_waiting`` 的谓词引用了枚举值
``'人工通过/重试'``，该值由 ``a1b2c3d4e5f6`` 通过
``ALTER TYPE ... ADD VALUE`` 添加。PostgreSQL 16 及更早版本不允许在同一
事务中使用尚未提交的枚举值（报错 ``unsafe use of new value``），而 Alembic
默认把所有迁移包在一个事务里。因此 ``alembic/env.py`` 必须设置
``transaction_per_migration=True``（每个迁移独立提交），否则在全新库上
执行 ``alembic upgrade head`` 会失败（CI 的 postgres:16 即为此场景）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 等待候选人回复超时巡检的状态集合（与 resume_repo.list_stale_email_waiters 一致）
_WAITING_STATUSES = "status IN ('AI筛选成功', '人工通过/重试')"


def upgrade() -> None:
    """Upgrade schema."""
    # ---------- P0：外键索引 ----------
    op.create_index('ix_dingding_user_user_id', 'dingding_user', ['user_id'])
    op.create_index('ix_candidates_creator_id', 'candidates', ['creator_id'])
    op.create_index('ix_interviews_interviewer_id', 'interviews', ['interviewer_id'])
    # 候选人与简历 1:1，唯一索引同时保证完整性与点查性能
    op.create_index(
        'ix_candidates_resume_id', 'candidates', ['resume_id'], unique=True
    )
    op.create_index(
        'ix_hr_managed_departments_department_id',
        'hr_managed_departments',
        ['department_id'],
    )

    # ---------- P1：排序 / 范围索引 ----------
    op.create_index('ix_candidates_created_at', 'candidates', ['created_at'])
    op.create_index(
        'ix_candidates_position_created',
        'candidates',
        ['position_id', 'created_at'],
    )
    op.create_index(
        'ix_positions_department_created',
        'positions',
        ['department_id', 'created_at'],
    )
    op.create_index('ix_positions_created_at', 'positions', ['created_at'])
    op.create_index(
        'ix_users_department_created',
        'users',
        ['department_id', 'created_at'],
    )

    # ---------- P1：超时巡检部分索引 ----------
    # 仅覆盖处于等待状态且已发出邀请的候选人，索引体积远小于全列索引
    op.create_index(
        'ix_candidates_email_waiting',
        'candidates',
        ['email_invite_sent_at'],
        postgresql_where=sa.text(
            f"{_WAITING_STATUSES} AND email_invite_sent_at IS NOT NULL"
        ),
    )

    # ---------- P2：AI 评分 1:1 唯一索引 ----------
    op.drop_index('ix_candidate_ai_scores_candidate_id', table_name='candidate_ai_scores')
    op.create_index(
        'ix_candidate_ai_scores_candidate_id',
        'candidate_ai_scores',
        ['candidate_id'],
        unique=True,
    )

    # ---------- P2：删除冗余 / 未使用索引 ----------
    # 被 ix_candidates_position_created 左缀覆盖
    op.drop_index('ix_candidates_position_id', table_name='candidates')
    # 被 ix_candidates_email_waiting 部分索引取代
    op.drop_index('ix_candidates_email_invite_sent_at', table_name='candidates')
    # 被 ix_positions_department_created 左缀覆盖
    op.drop_index('ix_positions_department_id', table_name='positions')
    # 被 ix_users_department_created 左缀覆盖
    op.drop_index('ix_users_department_id', table_name='users')
    # 无 SQL 使用（phone_number / scheduled_time 当前均无查询）
    op.drop_index('ix_candidates_phone_number', table_name='candidates')
    op.drop_index('ix_interviews_scheduled_time', table_name='interviews')


def downgrade() -> None:
    """Downgrade schema."""
    # 恢复被删除的索引
    op.create_index('ix_interviews_scheduled_time', 'interviews', ['scheduled_time'])
    op.create_index('ix_candidates_phone_number', 'candidates', ['phone_number'])
    op.create_index('ix_users_department_id', 'users', ['department_id'])
    op.create_index('ix_positions_department_id', 'positions', ['department_id'])
    op.create_index('ix_candidates_email_invite_sent_at', 'candidates', ['email_invite_sent_at'])
    op.create_index('ix_candidates_position_id', 'candidates', ['position_id'])

    # 恢复 AI 评分为非唯一索引
    op.drop_index('ix_candidate_ai_scores_candidate_id', table_name='candidate_ai_scores')
    op.create_index(
        'ix_candidate_ai_scores_candidate_id',
        'candidate_ai_scores',
        ['candidate_id'],
    )

    # 删除新增索引
    op.drop_index('ix_candidates_email_waiting', table_name='candidates')
    op.drop_index('ix_users_department_created', table_name='users')
    op.drop_index('ix_positions_created_at', table_name='positions')
    op.drop_index('ix_positions_department_created', table_name='positions')
    op.drop_index('ix_candidates_position_created', table_name='candidates')
    op.drop_index('ix_candidates_created_at', table_name='candidates')
    op.drop_index('ix_hr_managed_departments_department_id', table_name='hr_managed_departments')
    op.drop_index('ix_candidates_resume_id', table_name='candidates')
    op.drop_index('ix_interviews_interviewer_id', table_name='interviews')
    op.drop_index('ix_candidates_creator_id', table_name='candidates')
    op.drop_index('ix_dingding_user_user_id', table_name='dingding_user')
