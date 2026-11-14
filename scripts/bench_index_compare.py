"""索引优化前后性能对比基准（独立库 hr_bench，不触碰业务库 hr_system）。

用法
----
1. 重建 bench 库并迁移到旧 head：
       $env:DB_NAME='hr_bench'; alembic upgrade c3d4e5f6a7b8
2. 生成数据并采集优化前执行计划：
       $env:DB_NAME='hr_bench'; python scripts/bench_index_compare.py before
3. 应用新迁移：
       $env:DB_NAME='hr_bench'; alembic upgrade head
4. 采集优化后执行计划：
       $env:DB_NAME='hr_bench'; python scripts/bench_index_compare.py after

结果写入 ``benches/<label>.txt``（EXPLAIN ANALYZE, BUFFERS）。
"""
import sys
from pathlib import Path

import psycopg

# 允许以 `python scripts/bench_index_compare.py` 方式直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings


# (用例名, SQL)
QUERIES: list[tuple[str, str]] = [
    (
        "01_dingding_user_by_user_id",
        "SELECT * FROM dingding_user WHERE user_id = 'user_1';",
    ),
    (
        "02_candidates_by_resume_id",
        "SELECT * FROM candidates WHERE resume_id = 'res_12345';",
    ),
    (
        "03_candidates_by_creator_id",
        "SELECT * FROM candidates WHERE creator_id = 'user_500';",
    ),
    (
        "04_interviews_by_interviewer_id",
        "SELECT * FROM interviews WHERE interviewer_id = 'user_500';",
    ),
    (
        "05_candidates_list_by_position_order",
        "SELECT * FROM candidates WHERE position_id = 'pos_1' "
        "ORDER BY created_at DESC LIMIT 20;",
    ),
    (
        "06_candidates_list_no_filter_order",
        "SELECT * FROM candidates ORDER BY created_at DESC LIMIT 20;",
    ),
    (
        "07_positions_list_by_department_order",
        "SELECT * FROM positions WHERE department_id = 'dep_1' "
        "ORDER BY created_at DESC LIMIT 10;",
    ),
    (
        "08_positions_list_no_filter_order",
        "SELECT * FROM positions ORDER BY created_at DESC LIMIT 10;",
    ),
    (
        "09_users_list_by_department_order",
        "SELECT * FROM users WHERE department_id = 'dep_1' "
        "ORDER BY created_at DESC LIMIT 10;",
    ),
    (
        "10_stale_email_waiters",
        "SELECT * FROM candidates "
        "WHERE status IN ('AI筛选成功', '人工通过/重试') "
        "AND email_invite_sent_at IS NOT NULL "
        "AND email_invite_sent_at <= '2026-01-02 00:00:00' "
        "ORDER BY email_invite_sent_at ASC LIMIT 200;",
    ),
    (
        "11_candidate_count_7d",
        "SELECT date(created_at), count(id) FROM candidates "
        "WHERE created_at >= now() - interval '7 days' "
        "AND created_at <= now() "
        "GROUP BY date(created_at) ORDER BY date(created_at);",
    ),
    (
        "12_candidate_ai_score_by_candidate_id",
        "SELECT * FROM candidate_ai_scores WHERE candidate_id = 'cand_12345';",
    ),
    (
        "13_hr_managed_departments_by_department_id",
        "SELECT * FROM hr_managed_departments WHERE department_id = 'dep_1';",
    ),
]


def insert_data(conn: psycopg.Connection) -> None:
    """写入合成数据（幂等：已有数据则跳过）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM candidates")
        if cur.fetchone()[0] > 0:
            print("data already present, skipping insert")
            return

        print("inserting departments ...")
        cur.execute(
            """
            INSERT INTO departments (id, name, description, created_at, updated_at)
            SELECT 'dep_' || g, '部门' || g, '描述' || g, now(), now()
            FROM generate_series(1, 1000) g;
            """
        )

        print("inserting users ...")
        cur.execute(
            """
            INSERT INTO users (id, username, _password, email, phone_number, realname,
                               avatar, department_id, status, is_hr, is_superuser,
                               created_at, updated_at)
            SELECT 'user_' || g, 'user' || g, 'x', 'user' || g || '@example.com',
                   '138' || lpad(g::text, 8, '0'), '姓名' || g, NULL,
                   'dep_' || (g % 1000 + 1), 'ACTIVE'::userstatus, (g % 10 = 0), false,
                   now() - (g || ' minutes')::interval,
                   now() - (g || ' minutes')::interval
            FROM generate_series(1, 5000) g;
            """
        )

        print("inserting positions ...")
        cur.execute(
            """
            INSERT INTO positions (id, title, description, requirements, min_salary,
                                   max_salary, deadline, recruitment_count, education,
                                   work_year, is_open, department_id, creator_id,
                                   created_at, updated_at)
            SELECT 'pos_' || g, '职位' || g, NULL, NULL, 10, 20, NULL, 1,
                   'BACHELOR'::educationenum, 1, true,
                   CASE WHEN g <= 5000 THEN 'dep_1' ELSE 'dep_' || (g % 1000 + 1) END,
                   'user_' || (g % 5000 + 1),
                   now() - (g || ' minutes')::interval,
                   now() - (g || ' minutes')::interval
            FROM generate_series(1, 20000) g;
            """
        )

        print("inserting resumes ...")
        cur.execute(
            """
            INSERT INTO resumes (id, file_path, uploader_id, created_at, updated_at)
            SELECT 'res_' || g, 'file' || g || '.pdf', 'user_' || (g % 5000 + 1),
                   now() - (g || ' minutes')::interval,
                   now() - (g || ' minutes')::interval
            FROM generate_series(1, 100000) g;
            """
        )

        print("inserting candidates ...")
        # 状态分布：仅 4% 处于「等待回复」状态，但 95% 的行有邀请时间（已推进流程）
        cur.execute(
            """
            INSERT INTO candidates (id, name, gender, birthday, email, phone_number,
                                    work_experience, project_experience,
                                    education_experience, self_evaluation,
                                    other_information, skills, status, work_year,
                                    error_message, email_invite_sent_at, position_id,
                                    resume_id, creator_id, created_at, updated_at)
            SELECT 'cand_' || g,
                   '候选人' || g,
                   CASE g % 3 WHEN 0 THEN 'MALE' WHEN 1 THEN 'FEMALE' ELSE 'UNKNOWN' END
                        ::genderenum,
                   NULL,
                   'cand' || g || '@example.com',
                   '139' || lpad(g::text, 8, '0'),
                   NULL, NULL, NULL, NULL, NULL, NULL,
                   CASE
                     WHEN g % 100 < 2 THEN 'AI筛选成功'
                     WHEN g % 100 < 4 THEN '人工通过/重试'
                     WHEN g % 100 < 10 THEN '已投递'
                     WHEN g % 100 < 20 THEN 'AI筛选失败'
                     WHEN g % 100 < 40 THEN '待面试'
                     WHEN g % 100 < 50 THEN '拒绝面试'
                     WHEN g % 100 < 70 THEN '面试通过'
                     WHEN g % 100 < 85 THEN '面试未通过'
                     ELSE '已拒绝'
                   END::candidatestatusenum,
                   (g % 10),
                   NULL,
                   CASE WHEN g % 20 <> 0
                        THEN '2026-01-01'::timestamp
                             + ((g % 100000) || ' minutes')::interval
                        ELSE NULL END,
                   CASE WHEN g <= 20000 THEN 'pos_1'
                        ELSE 'pos_' || (g % 20000 + 1) END,
                   'res_' || g,
                   'user_' || (g % 5000 + 1),
                   now() - (g || ' minutes')::interval,
                   now() - (g || ' minutes')::interval
            FROM generate_series(1, 100000) g;
            """
        )

        print("inserting candidate_ai_scores ...")
        cur.execute(
            """
            INSERT INTO candidate_ai_scores (id, work_experience_score,
                                             technical_skills_score, soft_skills_score,
                                             educational_background_score,
                                             project_experience_score, overall_score,
                                             summary, strengths, weaknesses, candidate_id,
                                             created_at, updated_at)
            SELECT 'score_' || g, 5, 5, 5, 5, 5, 5, 'summary', '[]', '[]',
                   'cand_' || g, now(), now()
            FROM generate_series(1, 100000) g;
            """
        )

        print("inserting interviews ...")
        cur.execute(
            """
            INSERT INTO interviews (id, scheduled_time, feedback, result, candidate_id,
                                    interviewer_id, created_at, updated_at)
            SELECT 'iv_' || g, now() + (g || ' hours')::interval, NULL, 'PASSED',
                   'cand_' || g, 'user_' || (g % 5000 + 1), now(), now()
            FROM generate_series(1, 100000) g;
            """
        )

        print("inserting dingding_user ...")
        cur.execute(
            """
            INSERT INTO dingding_user (id, nick, union_id, open_id, mobile, user_id,
                                       created_at, updated_at)
            SELECT 'dd_' || g, '钉钉' || g, 'union_' || g, 'open_' || g,
                   '137' || lpad(g::text, 8, '0'), 'user_' || (g % 5000 + 1),
                   now(), now()
            FROM generate_series(1, 50000) g;
            """
        )

        print("inserting hr_managed_departments ...")
        cur.execute(
            """
            INSERT INTO hr_managed_departments (user_id, department_id)
            SELECT 'user_' || (g % 5000 + 1), 'dep_' || (g % 1000 + 1)
            FROM generate_series(1, 20000) g
            ON CONFLICT DO NOTHING;
            """
        )

        conn.commit()
        print("data inserted")


def run_bench(conn: psycopg.Connection, label: str) -> None:
    out: list[str] = []
    with conn.cursor() as cur:
        for name, sql in QUERIES:
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + sql)
            plan = "\n".join(row[0] for row in cur.fetchall())
            out.append(f"### {name}\n{sql}\n\n{plan}\n")
    path = Path("benches") / f"{label}.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"written {path}")


if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else "before"
    url = get_settings().database_url.replace("+psycopg", "")
    with psycopg.connect(url) as conn:
        if label == "before":
            insert_data(conn)
            with conn.cursor() as cur:
                cur.execute("ANALYZE")
            conn.commit()
        run_bench(conn, label)
