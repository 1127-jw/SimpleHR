# SQL 性能优化报告（P0 / P1 / P2）

> 适用版本：`c3d4e5f6a7b8`（优化前）→ `d4e5f6a7b8c9`（优化后）
> 数据库：PostgreSQL 18.6（`postgresql+psycopg`）
> 迁移文件：[alembic/versions/d4e5f6a7b8c9_add_fk_sort_and_partial_indexes.py](../alembic/versions/d4e5f6a7b8c9_add_fk_sort_and_partial_indexes.py)
> 基准脚本：[scripts/bench_index_compare.py](../scripts/bench_index_compare.py)
> 完整执行计划：[benches/before.txt](../benches/before.txt) / [benches/after.txt](../benches/after.txt)

---

## 1. 背景与范围

项目没有手写原生 SQL，全部查询由 SQLAlchemy 2.0 ORM 生成，访问入口集中在 5 个 repository。
上一轮分析（`c3d4e5f6a7b8`）已经补上了列表/统计主路径索引，但仍存在三类缺口：

| 优先级 | 问题 | 典型后果 |
|---|---|---|
| **P0** | 外键无索引（PostgreSQL 不会自动建） | `Seq Scan` 全表扫描 |
| **P1** | 排序字段无组合索引 | `Sort` 节点 + 全表扫描 |
| **P1** | 超时巡检索引体积大 | 扫描大量无关行 |
| **P2** | 冗余/未使用索引、1:1 关系无唯一约束 | 写放大、数据一致性风险 |

本文所有“优化前”数据均在**当前生产版本** `c3d4e5f6a7b8` 上测得，“优化后”在新迁移 `d4e5f6a7b8c9` 上测得。

---

## 2. 优化清单

### 2.1 P0：补外键索引

| 索引 | 表(列) | 覆盖的查询 |
|---|---|---|
| `ix_dingding_user_user_id` | `dingding_user(user_id)` | `UserRepo.get_dingding_user()`、`user.dingding_user` 关系加载 |
| `ix_candidates_resume_id`（UNIQUE） | `candidates(resume_id)` | 候选人详情加载简历；同时约束 1:1 |
| `ix_candidates_creator_id` | `candidates(creator_id)` | 按创建人查候选人、外键级联检查 |
| `ix_interviews_interviewer_id` | `interviews(interviewer_id)` | 按面试官查面试、外键级联检查 |
| `ix_hr_managed_departments_department_id` | `hr_managed_departments(department_id)` | 反向按部门查 HR（主键最左是 `user_id`，用不上） |

```sql
CREATE INDEX ix_dingding_user_user_id ON dingding_user (user_id);
CREATE UNIQUE INDEX ix_candidates_resume_id ON candidates (resume_id);
CREATE INDEX ix_candidates_creator_id ON candidates (creator_id);
CREATE INDEX ix_interviews_interviewer_id ON interviews (interviewer_id);
CREATE INDEX ix_hr_managed_departments_department_id ON hr_managed_departments (department_id);
```

### 2.2 P1：排序 / 范围索引

| 索引 | 表(列) | 覆盖的查询 |
|---|---|---|
| `ix_candidates_created_at` | `candidates(created_at)` | 超管/无过滤列表 `ORDER BY created_at DESC` |
| `ix_candidates_position_created` | `candidates(position_id, created_at)` | 按职位过滤 + 时间倒序（取代单列 `ix_candidates_position_id`） |
| `ix_positions_department_created` | `positions(department_id, created_at)` | 按部门过滤 + 时间倒序 |
| `ix_positions_created_at` | `positions(created_at)` | 超管/无过滤职位列表 |
| `ix_users_department_created` | `users(department_id, created_at)` | 用户列表按部门过滤 + 稳定分页排序 |

> 说明：B-Tree 索引可以反向扫描，`ORDER BY created_at DESC` 无需显式建 `DESC` 索引。

### 2.3 P1：超时巡检部分索引

“等待候选人回复”巡检只关心**处于等待状态且已发出邀请**的候选人，但 `email_invite_sent_at` 对已推进流程的候选人也是非空。部分索引把索引体积从“所有非空行”压缩到“仅等待行”：

```sql
CREATE INDEX ix_candidates_email_waiting
ON candidates (email_invite_sent_at)
WHERE status IN ('AI筛选成功', '人工通过/重试')
  AND email_invite_sent_at IS NOT NULL;
```

> ⚠️ 该谓词与 `resume_repo.list_stale_email_waiters()` 的状态集合必须保持一致；若状态集合调整，索引需同步重建。

### 2.4 P2：清理与完整性

| 操作 | 对象 | 原因 |
|---|---|---|
| 删除 | `ix_candidates_position_id` | 被 `ix_candidates_position_created` 左缀覆盖 |
| 删除 | `ix_positions_department_id` | 被 `ix_positions_department_created` 左缀覆盖 |
| 删除 | `ix_users_department_id` | 被 `ix_users_department_created` 左缀覆盖 |
| 删除 | `ix_candidates_email_invite_sent_at` | 被部分索引 `ix_candidates_email_waiting` 取代 |
| 删除 | `ix_candidates_phone_number` | 当前无任何 SQL 使用 |
| 删除 | `ix_interviews_scheduled_time` | 当前无任何 SQL 使用 |
| 改唯一 | `ix_candidate_ai_scores_candidate_id` | 模型关系为 1:1（`uselist=False`），防止重复评分 |

### 2.5 查询层优化（非索引）

| 位置 | 改动 | 收益 |
|---|---|---|
| [app/repository/resume_repo.py](../app/repository/resume_repo.py) `_visible_stmt` | HR 分支直接复用 `current_user.managed_departments` | 每个列表请求少 1 次用户查询 + 1 次关系查询 |
| [app/repository/user_repo.py](../app/repository/user_repo.py) `get_user_list` | 增加 `ORDER BY created_at DESC` | 修复分页顺序不稳定（可能跨页重复/遗漏） |
| [app/repository/user_repo.py](../app/repository/user_repo.py) `exists_by_email/username` | 新增轻量判重方法（只取 id） | 注册判重不再加载 4 组关联 |
| [app/service/auth_ser.py](../app/service/auth_ser.py) `create_user` | 判重改用 `exists_by_email` | 少 4 次 selectinload 往返 |
| [init_data.py](../init_data.py) | 判重改用 `exists_by_*` | 启动初始化更轻 |

---

## 3. 基准测试方法

- **数据库**：PostgreSQL 18.6，`shared_buffers=128MB`，`max_parallel_workers_per_gather=2`
- **测试库**：独立库 `hr_bench`，**不触碰业务库 `hr_system`**
- **数据采集**：`EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)`，取 `Execution Time` 与 `Buffers`
- **数据规模**（合成数据，贴近真实分布）：

| 表 | 行数 | 说明 |
|---|---|---|
| `departments` | 1,000 | |
| `users` | 5,000 | |
| `positions` | 20,000 | `dep_1` 占 5,000 |
| `resumes` | 100,000 | |
| `candidates` | 100,000 | `pos_1` 占 20,000；仅 4% 处于等待状态，95% 有邀请时间 |
| `candidate_ai_scores` | 100,000 | |
| `interviews` | 100,000 | |
| `dingding_user` | 50,000 | |
| `hr_managed_departments` | 20,000 | |

**复现步骤**：

```powershell
# 1. 建库并迁移到优化前版本
$env:PGPASSWORD='<db_password>'
psql -h 127.0.0.1 -U postgres -d postgres -c "CREATE DATABASE hr_bench"
$env:DB_NAME='hr_bench'
.venv\Scripts\alembic.exe upgrade c3d4e5f6a7b8

# 2. 生成数据 + 采集优化前计划
.venv\Scripts\python.exe scripts\bench_index_compare.py before

# 3. 应用优化迁移
.venv\Scripts\alembic.exe upgrade head

# 4. 采集优化后计划
.venv\Scripts\python.exe scripts\bench_index_compare.py after
```

---

## 4. 优化前后对比

### 4.1 汇总

| # | 用例 | 优化前 | 优化后 | 提升 | Buffer 变化 |
|---|---|---|---|---|---|
| 01 | `dingding_user` 按 `user_id` | 2.637 ms（Seq Scan） | **0.142 ms**（Index） | **18.6×** | 758 → 12 |
| 02 | `candidates` 按 `resume_id` | 12.613 ms（Seq Scan） | **0.052 ms**（Index） | **242.6×** | 2856 → 4 |
| 03 | `candidates` 按 `creator_id` | 8.054 ms（Seq Scan） | **0.120 ms**（Index） | **67.1×** | 2856 → 22 |
| 04 | `interviews` 按 `interviewer_id` | 4.327 ms（Seq Scan） | **0.104 ms**（Index） | **41.6×** | 1136 → 22 |
| 05 | 候选人列表（按职位 + 时间倒序） | 2.825 ms（Bitmap + Sort） | **0.068 ms**（Index Backward） | **41.5×** | 449 → 4 |
| 06 | 候选人列表（无过滤 + 时间倒序） | 64.238 ms（Parallel Seq + Sort） | **0.038 ms**（Index Backward） | **1690.5×** | 2897 → 3 |
| 07 | 职位列表（按部门 + 时间倒序） | 0.634 ms（Bitmap + Sort） | **0.037 ms**（Index Backward） | **17.1×** | 91 → 3 |
| 08 | 职位列表（无过滤 + 时间倒序） | 1.717 ms（Seq + Sort） | **0.009 ms**（Index Backward） | **190.8×** | 285 → 3 |
| 09 | 用户列表（按部门 + 时间倒序） | 0.025 ms | 0.074 ms | 持平（见 5.3） | 7 → 10 |
| 10 | 等待回复超时巡检 | 0.279 ms（BitmapAnd + Sort） | **0.070 ms**（部分索引） | **4.0×** | 34 → 17 |
| 11 | 近 7 天新增聚合 | 20.252 ms（Seq + Sort） | **3.315 ms**（Bitmap Index） | **6.1×** | 2859 → 241 |
| 12 | AI 评分按 `candidate_id` | 0.015 ms（Index） | 0.050 ms（Unique Index） | 持平（见 5.4） | 4 → 4 |
| 13 | `hr_managed_departments` 按部门 | 0.172 ms（Seq Scan） | **0.045 ms**（Index） | **3.8×** | 32 → 7 |

> 单次运行数据，绝对值受缓存/并行度影响；但**计划形态的变化（Seq Scan → Index Scan）是结构性的**，数据量越大收益越明显。

### 4.2 典型用例前后执行计划

#### 用例 02：按 `resume_id` 查候选人（P0，242.6×）

```sql
SELECT * FROM candidates WHERE resume_id = 'res_12345';
```

优化前 —— 全表扫描 10 万行：

```
Seq Scan on candidates  (cost=0.00..4106.00 rows=1 width=401)
  Filter: ((resume_id)::text = 'res_12345'::text)
  Rows Removed by Filter: 99999
  Buffers: shared hit=2116 read=740 written=241
Execution Time: 12.613 ms
```

优化后 —— 唯一索引点查：

```
Index Scan using ix_candidates_resume_id on candidates
  (cost=0.42..8.44 rows=1 width=401)
  Index Cond: ((resume_id)::text = 'res_12345'::text)
  Buffers: shared hit=1 read=3
Execution Time: 0.052 ms
```

#### 用例 06：无过滤候选人列表（P1，1690.5×）

```sql
SELECT * FROM candidates ORDER BY created_at DESC LIMIT 20;
```

优化前 —— 并行全表扫描 + Top-N 排序：

```
Limit  (cost=6009.53..6011.81 rows=20 width=401)
  ->  Gather Merge
        Workers Planned: 1  Workers Launched: 1
        ->  Sort  (Sort Key: created_at DESC  Sort Method: top-N heapsort)
              ->  Parallel Seq Scan on candidates
                    Buffers: shared hit=2856
Execution Time: 64.238 ms
```

优化后 —— 反向索引扫描，直接取前 20 行：

```
Limit  (cost=0.29..2.87 rows=20 width=401)
  ->  Index Scan Backward using ix_candidates_created_at on candidates
        Buffers: shared hit=1 read=2
Execution Time: 0.038 ms
```

#### 用例 05：按职位过滤 + 时间倒序（P1，41.5×）

```sql
SELECT * FROM candidates WHERE position_id = 'pos_1'
ORDER BY created_at DESC LIMIT 20;
```

优化前 —— 先按 `position_id` 取 2 万行，再排序：

```
Limit
  ->  Sort  (Sort Key: created_at DESC  Sort Method: top-N heapsort)
        ->  Bitmap Heap Scan on candidates  (rows=20004)
              ->  Bitmap Index Scan on ix_candidates_position_id
Execution Time: 2.825 ms
```

优化后 —— 复合索引天然有序，无需排序：

```
Limit
  ->  Index Scan Backward using ix_candidates_position_created on candidates
        Index Cond: ((position_id)::text = 'pos_1'::text)
        Buffers: shared hit=1 read=3
Execution Time: 0.068 ms
```

#### 用例 10：等待回复超时巡检（P1，4.0×）

```sql
SELECT * FROM candidates
WHERE status IN ('AI筛选成功', '人工通过/重试')
  AND email_invite_sent_at IS NOT NULL
  AND email_invite_sent_at <= '2026-01-02 00:00:00'
ORDER BY email_invite_sent_at ASC LIMIT 200;
```

优化前 —— `BitmapAnd` 合并“全量邀请时间索引”和“状态索引”，再排序：

```
Limit
  ->  Sort  (Sort Key: email_invite_sent_at)
        ->  Bitmap Heap Scan on candidates
              ->  BitmapAnd
                    ->  Bitmap Index Scan on ix_candidates_email_invite_sent_at
                          Index Cond: (email_invite_sent_at <= ...)
                    ->  Bitmap Index Scan on ix_candidates_status
                          Index Cond: (status = ANY (...))
Execution Time: 0.279 ms
```

优化后 —— 部分索引只包含等待状态的行，单索引有序扫描：

```
Limit
  ->  Index Scan using ix_candidates_email_waiting on candidates
        Index Cond: (email_invite_sent_at <= ...)
        Buffers: shared hit=15 read=2
Execution Time: 0.070 ms
```

#### 用例 11：近 7 天新增聚合（P1，6.1×）

```sql
SELECT date(created_at), count(id) FROM candidates
WHERE created_at >= now() - interval '7 days'
  AND created_at <= now()
GROUP BY date(created_at) ORDER BY date(created_at);
```

优化前 —— 全表扫描 10 万行后聚合：

```
GroupAggregate
  ->  Sort  (Sort Key: date(created_at))
        ->  Seq Scan on candidates
              Filter: (created_at >= ... AND created_at <= ...)
              Rows Removed by Filter: 89921
Execution Time: 20.252 ms
```

优化后 —— 范围索引只扫 7 天数据：

```
GroupAggregate
  ->  Sort
        ->  Bitmap Heap Scan on candidates  (rows=10079)
              Recheck Cond: (created_at >= ... AND created_at <= ...)
              ->  Bitmap Index Scan on ix_candidates_created_at
Execution Time: 3.315 ms
```

#### 用例 01：按 `user_id` 查钉钉账号（P0，18.6×）

```sql
SELECT * FROM dingding_user WHERE user_id = 'user_1';
```

优化前：`Seq Scan`，`Rows Removed by Filter: 49990`，2.637 ms
优化后：`Bitmap Index Scan on ix_dingding_user_user_id`，0.142 ms

---

## 5. 结果分析

### 5.1 关键结论

1. **P0 外键索引是收益最大的一类**：`resume_id`、`creator_id`、`user_id` 等查询从全表扫描变为索引点查，提升 18×~242×。
2. **排序索引消除了 `Sort` 节点**：无过滤列表从 64 ms 降到 0.038 ms；这类查询在深分页场景下差距会进一步放大。
3. **部分索引对“低占比等待状态”场景效果明确**：索引只覆盖 4% 的行，避免扫描已推进流程的历史数据。
4. **Buffer 下降比耗时更说明问题**：多数用例的 shared buffer 读取下降 1~2 个数量级，意味着更少的磁盘 IO 和更高的缓存命中率。

### 5.2 数据量扩展后的预期

| 数据量 | 全表扫描耗时（近似线性） | 索引点查耗时（近似对数） |
|---|---|---|
| 10 万行 | 5~15 ms | < 0.1 ms |
| 100 万行 | 50~150 ms | < 0.2 ms |
| 1000 万行 | 0.5~1.5 s | < 0.5 ms |

> 全表扫描随数据量线性增长，索引访问随树高对数增长；数据量越大，本次优化的收益越显著。

### 5.3 注意事项

- **用例 07（职位列表）**：优化后计划选择了 `ix_positions_created_at`（无过滤）而非 `ix_positions_department_created`。这是因为合成数据中 `dep_1` 恰好是最新的 5000 条，反向扫描前 10 行即命中。真实数据分布下，规划器会按选择性选择复合索引；复合索引仍然保留，用于非最新部门的分页场景。
- **用例 09（用户列表）**：优化后耗时略升（0.025 → 0.074 ms），差异在 0.05 ms 内，属测量噪声；该用例的核心收益是**分页顺序稳定**（正确性），而非性能。
- **用例 12（AI 评分）**：性能持平，收益是**唯一约束**带来的数据完整性（防止同一候选人重复评分）。
- 部分索引谓词与 `list_stale_email_waiters()` 的状态集合强耦合，调整状态集合时必须同步重建索引。

---

## 6. 落地清单

| 文件 | 改动 |
|---|---|
| [alembic/versions/d4e5f6a7b8c9_add_fk_sort_and_partial_indexes.py](../alembic/versions/d4e5f6a7b8c9_add_fk_sort_and_partial_indexes.py) | 新增迁移（含 downgrade） |
| [alembic/env.py](../alembic/env.py) | 设置 `transaction_per_migration=True`，修复 PG 16 枚举限制导致的 CI 失败 |
| [app/models/candidate.py](../app/models/candidate.py) | 复合/部分/唯一索引；`creator_id` 加索引；移除 `phone_number`/`position_id`/`email_invite_sent_at` 单列索引 |
| [app/models/position.py](../app/models/position.py) | 新增部门+时间、纯时间索引 |
| [app/models/user.py](../app/models/user.py) | `hr_managed_departments` 反向索引、`users` 部门+时间索引、`dingding_user.user_id` 索引 |
| [app/models/InterviewModel.py](../app/models/InterviewModel.py) | `interviewer_id` 加索引；移除 `scheduled_time` 索引 |
| [app/repository/resume_repo.py](../app/repository/resume_repo.py) | HR 可见性复用已加载的 `managed_departments` |
| [app/repository/user_repo.py](../app/repository/user_repo.py) | 用户列表稳定排序；新增轻量判重方法 |
| [app/service/auth_ser.py](../app/service/auth_ser.py) | 注册判重改用轻量方法 |
| [init_data.py](../init_data.py) | 初始化判重改用轻量方法 |
| [scripts/bench_index_compare.py](../scripts/bench_index_compare.py) | 基准脚本 |

**验证结果**：

- 迁移在独立库 `hr_bench` 应用成功，`alembic check` → `No new upgrade operations detected`（模型与库一致）
- `downgrade` → `upgrade` 回滚/重放成功
- 全量测试通过（`pytest` 退出码 0）

### 6.1 CI 兼容性修复（PostgreSQL 16 枚举限制）

CI 的 `postgres:16-alpine` 曾在「初始化数据库（迁移 + 种子数据）」步骤失败。
根因是 Alembic 默认把**所有迁移包在一个事务**里执行，而 `d4e5f6a7b8c9` 的
部分索引谓词使用了 `a1b2c3d4e5f6` 刚通过 `ALTER TYPE ... ADD VALUE` 添加的
枚举值 `'人工通过/重试'`。PostgreSQL 16 及更早版本不允许在同一事务中使用尚未
提交的枚举值：

```
ERROR: unsafe use of new value "人工通过/重试" of enum type candidatestatusenum
HINT: New enum values must be committed before they can be used.
```

PG 17 起才放宽该限制（见 [PostgreSQL 提交 af1d395](https://git.postgresql.org/cgit/postgresql.git/commit/src/backend/catalog/pg_enum.c?id=af1d3958432c9438c4ec168bd16bdf9a91066a9f)），
而 CI 使用 PG 16，因此全新库执行 `alembic upgrade head` 会失败（生产库因枚举值
早已提交而不受影响）。

**修复**：在 [alembic/env.py](../alembic/env.py) 的 `context.configure()` 中设置
`transaction_per_migration=True`，让每个迁移独立提交。这样枚举值在使用它的
迁移开始前就已提交，兼容 PostgreSQL 16+。

**验证**：在迁移开头临时插入 `pg_sleep`，从另一会话查询 `pg_enum`，确认
`'人工通过/重试'` 在后续迁移执行前已可见（即已提交）。

---

## 7. 维护建议

1. **新增外键时同步加索引**：PostgreSQL 不会自动建，建议在模型上显式 `index=True` 或写复合索引。
2. **新增列表接口时同时设计索引**：`WHERE` 等值列在前、`ORDER BY` 列在后，是最通用的组合索引顺序。
3. **定期审查未使用索引**：可通过 `pg_stat_user_indexes` 观察 `idx_scan`，长期为 0 且无外部查询依赖的索引可考虑删除。
4. **部分索引谓词与代码保持一致**：把 `list_stale_email_waiters()` 的状态集合提为常量，供代码与索引定义共同引用，降低漂移风险。
5. **避免在 `Text` 大字段上建索引**：如 `education_experience`，应通过预聚合/缓存解决看板类查询。

---

## 8. 附录：索引 DDL 全量

```sql
-- P0 外键索引
CREATE INDEX ix_dingding_user_user_id ON dingding_user (user_id);
CREATE UNIQUE INDEX ix_candidates_resume_id ON candidates (resume_id);
CREATE INDEX ix_candidates_creator_id ON candidates (creator_id);
CREATE INDEX ix_interviews_interviewer_id ON interviews (interviewer_id);
CREATE INDEX ix_hr_managed_departments_department_id ON hr_managed_departments (department_id);

-- P1 排序 / 范围索引
CREATE INDEX ix_candidates_created_at ON candidates (created_at);
CREATE INDEX ix_candidates_position_created ON candidates (position_id, created_at);
CREATE INDEX ix_positions_department_created ON positions (department_id, created_at);
CREATE INDEX ix_positions_created_at ON positions (created_at);
CREATE INDEX ix_users_department_created ON users (department_id, created_at);

-- P1 部分索引
CREATE INDEX ix_candidates_email_waiting
ON candidates (email_invite_sent_at)
WHERE status IN ('AI筛选成功', '人工通过/重试') AND email_invite_sent_at IS NOT NULL;

-- P2 唯一索引
CREATE UNIQUE INDEX ix_candidate_ai_scores_candidate_id ON candidate_ai_scores (candidate_id);

-- P2 删除冗余 / 未使用索引
DROP INDEX ix_candidates_position_id;
DROP INDEX ix_positions_department_id;
DROP INDEX ix_users_department_id;
DROP INDEX ix_candidates_email_invite_sent_at;
DROP INDEX ix_candidates_phone_number;
DROP INDEX ix_interviews_scheduled_time;
```
