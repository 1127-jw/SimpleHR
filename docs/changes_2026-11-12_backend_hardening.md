# 后端加固改动记录（2026-11-12）

> 本轮改动对应代码评审中确认的 5 个改进项：**登录错误信息与限流双维度**、
> **gunicorn 配置清理**、**refresh token 原子消费**、**检查点单例 + 超时兜底**、
> **索引补充 + 关联加载策略收敛**。
>
> 所有改动均向后兼容，不涉及接口签名变更（唯一例外是 `/user/me` 的响应体
> 不再包含 `_password`，属于安全修复）。

---

## 一、总览

| # | 改进项 | 涉及文件 | 风险等级 |
|---|--------|----------|----------|
| 2 | 登录错误信息统一 + 双维度限流 | `app/service/auth_ser.py`、`app/routers/user_router.py`、`app/core/config.py` | 低 |
| 4 | gunicorn 配置清理、日志路径环境变量化 | `gunicorn.conf.py` | 低 |
| 5 | refresh token 原子消费 | `app/core/auth.py` | 低 |
| 8 | 检查点存储单例化 | `app/core/checkpointer.py`、`app/main.py`、`app/tasks/agent_tasks.py`、`app/service/candidate_status_ser.py`、`utils/emailbot/notify.py` | 中 |
| 8b | 邮件回复超时兜底巡检 | `app/tasks/timeout_tasks.py`（新增）、`app/main.py`、`app/models/candidate.py`、`app/repository/resume_repo.py`、`app/workflow/candidate_nodes/email_invite_node.py`、`app/core/config.py` | 中 |
| 10 | 索引补充 + 关联加载收敛 | `app/models/*.py`、`app/repository/*.py`、`app/service/position_ser.py`、`alembic/versions/c3d4e5f6a7b8_*.py`（新增） | 中 |
| 附带 | `/user/me` 密码哈希泄露修复 | `app/routers/user_router.py`、`app/schemas/user_schema.py` | 高（安全） |
| 附带 | 用户未分配部门时创建/操作职位报 500 | `app/service/position_ser.py` | 低 |
| 附带 | 死代码清理 | `app/repository/resume_repo.py` | 低 |

---

## 二、改进项 2：登录错误信息与限流双维度

### 问题

1. `authenticate_user` 对「用户不存在」返回 `no such user`、对「密码错误」返回
   `wrong password`，两者组合即可**枚举系统内所有已注册邮箱**。
2. 限流键是 `rl:login:{ip}:{email}`，攻击者换一个邮箱就能重置计数，
   等于对每个账号都有无限次尝试机会（只要分散邮箱）。

### 改动

**`app/service/auth_ser.py`**

```python
# 统一错误信息：区分「用户不存在」与「密码错误」会变成账号枚举入口。
# 保持 400 是为了兼容前端 axios 拦截器（401 会触发自动刷新令牌流程）。
if user is None or not user.check_password(password):
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邮箱或密码错误")
if user.status != UserStatus.ACTIVE:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="账号已被禁用，请联系管理员")
```

**`app/routers/user_router.py`** —— 限流从「IP+账号」一维改为两个独立维度：

| 维度 | Redis key | 默认阈值 | 防御目标 |
|------|-----------|----------|----------|
| 单 IP | `rl:login:ip:{ip}` | 20 次 / 60 秒 | 同机脚本枚举、批量爆破 |
| 单账号 | `rl:login:email:{email}` | 5 次 / 60 秒 | 针对特定账号的定向爆破 |

两档阈值均可配置（`LOGIN_RATE_LIMIT_PER_IP` / `LOGIN_RATE_LIMIT_PER_EMAIL`）。

### 有意保留的兼容决策

- **状态码保持 400 而非 401**：前端 `hr-frontend/src/apis/requests.ts` 的响应
  拦截器在 401 时会尝试刷新令牌并跳转登录页，登录接口返回 401 会让错误处理
  走偏。若后续要改成语义更准确的 401，需要先在拦截器中排除 `/user/token`
  与 `/user/login`（见文末「后续建议」）。
- **时间侧信道**：用户不存在时跳过 Argon2 校验，理论上仍可通过响应时间区分
  账号是否存在。彻底修复需要在用户不存在时也执行一次哑校验，本轮未做。

---

## 三、改进项 4：gunicorn 配置清理

### 问题

- 第 8 行 `from sqlalchemy.dialects.mysql import limit` 是无用导入（项目使用
  PostgreSQL），会让读者误以为存在 MySQL 依赖。
- 访问日志 / 错误日志路径硬编码为 `/home/hrsystem/hr-backend/log/...`，
  换部署环境必须改代码。

### 改动（`gunicorn.conf.py`）

- 删除无用导入。
- 以下配置全部支持环境变量覆盖，并给出与项目日志目录一致的默认值：

| 变量 | 默认值 |
|------|--------|
| `GUNICORN_BIND` | `127.0.0.1:8000` |
| `GUNICORN_WORKERS` | `1` |
| `GUNICORN_ACCESS_LOG` | `logs/access.log` |
| `GUNICORN_ERROR_LOG` | `logs/error.log` |
| `GUNICORN_TIMEOUT` | `60` |

> `GUNICORN_WORKERS` 默认仍为 1：邮件机器人（IMAP 长连接 + APScheduler）
> 目前只支持单 worker，拆分邮件机器人之前不要调大。

---

## 四、改进项 5：refresh token 原子消费

### 问题

```python
if not await r.exists(f"refresh:{jti}"):   # ← 两次往返之间存在窗口
    raise ValueError("Refresh token has been revoked")
await r.delete(f"refresh:{jti}")
```

两个并发刷新请求会同时通过 `exists()` 检查，导致**同一 refresh token 被
兑换两次**，refresh token 轮换（防重放）形同虚设。

### 改动（`app/core/auth.py`）

用一段 Lua 脚本把「取出来 + 删掉」合并成一次原子操作：

```lua
local value = redis.call('GET', KEYS[1])
if value then
    redis.call('DEL', KEYS[1])
    return value
end
return nil
```

```python
async def _consume_refresh_token(r: Redis, jti: str) -> str | None:
    return await r.eval(_CONSUME_REFRESH_LUA, 1, f"refresh:{jti}")
```

`refresh_access_token` 改为「消费成功才签发新令牌」，消费失败即视为已吊销。
`revoke_refresh_token`（登出）仍走 `delete`，语义不变。

---

## 五、改进项 8：检查点存储单例化

### 问题

`AsyncPostgresSaver.from_conn_string` 是一个 `asynccontextmanager`，
原实现每次调用都：

1. 新建一条 Postgres 连接（`AsyncConnection.connect`）；
2. 执行 `setup()`（建表 + 迁移 DDL 版本检查）；
3. 用完关闭。

也就是说，**每次候选人流程执行、每次邮件恢复、每次人工重试都会重建连接并重跑
DDL**。连接数随请求量线性放大，`setup()` 也有潜在的并发建表竞争。

### 改动

**`app/core/checkpointer.py` 重写为进程级单例**：

| 函数 | 职责 |
|------|------|
| `init_checkpointer()` | lifespan 中调用一次，建连 + `setup()`，幂等 |
| `get_checkpointer()` | 返回单例；未初始化时 fail-fast 抛 `RuntimeError` |
| `close_checkpointer()` | 进程退出时释放连接，幂等 |

```python
conn = await AsyncConnection.connect(
    pg_url, autocommit=True, prepare_threshold=0, row_factory=dict_row
)
saver = AsyncPostgresSaver(conn=conn, serde=_build_serde())
await saver.setup()
```

**调用点改造**（去掉 `async with`）：

- `app/tasks/agent_tasks.py` —— 图模式主流程
- `app/service/candidate_status_ser.py` —— 人工重试
- `utils/emailbot/notify.py` —— 邮件恢复（2 处）

**`app/main.py`** —— lifespan 启动时 `init_checkpointer()`，关闭时
`close_checkpointer()`。

### 效果

- 每进程恒定 **1 条**检查点连接（原先每次调用 1 条，且反复创建销毁）。
- `setup()` 从「每次调用」降为「每进程一次」。

---

## 六、改进项 8b：邮件回复超时兜底

### 问题

状态图在 `get_candidate_email_reply_node` 通过 `interrupt()` 暂停等待候选人
回复。代码里定义了 `EMAIL_REPLY_TIMEOUT` 常量和
`route_after_email_reply` 的超时分支，但**没有任何地方会写入这个标记**——
候选人始终不回信时，流程会永久停在 interrupt 上，既不会超时也不会告警。

### 设计

采用「**DB 时间戳 + 周期巡检 + 中断态校验**」三段式，而不是把调度逻辑
塞进状态图：

> 与 [`docs/workflow_fixes.md`](workflow_fixes.md) 中早期草图的差异：
> 草图计划引入 Redis 版的 `workflow_thread_store`（`save_thread_id` /
> `save_thread_deadline`）+ APScheduler 定时任务。本轮实现改为
> **DB 时间戳 + lifespan 常驻任务**：判据落在 `candidates` 表上，
> 进程重启后不会丢失（Redis key 若过期会漏判），也不需要额外的
> Redis 存储封装与第三个调度器（项目已有 MailManager 的 APScheduler，
> 再引入一个会增加关停复杂度）。

```
email_invite_node 发送邮件
        │
        └─→ 写入 candidates.email_invite_sent_at
                    │
                    ▼
        后台巡检（每 EMAIL_REPLY_SWEEP_INTERVAL_MINUTES 分钟）
                    │
                    ├─ 查询 status ∈ {AI_FILTER_PASSED, MANUAL_PASSED}
                    │   且 email_invite_sent_at <= now - EMAIL_REPLY_TIMEOUT_HOURS
                    │
                    ├─ graph.aget_state() 确认该 thread 确实有待响应 interrupt
                    │
                    └─ Command(resume=EMAIL_REPLY_TIMEOUT)
                            │
                            ▼
                    negotiation_expired_node → 状态置为「协商超时」
```

### 改动清单

| 文件 | 改动 |
|------|------|
| `app/core/config.py` | 新增 `EMAIL_REPLY_TIMEOUT_HOURS`（默认 48）、`EMAIL_REPLY_SWEEP_INTERVAL_MINUTES`（默认 15） |
| `app/models/candidate.py` | 新增列 `email_invite_sent_at`（带索引） |
| `app/repository/resume_repo.py` | 新增 `update_email_invite_sent_at()`、`list_stale_email_waiters()` |
| `app/workflow/candidate_nodes/email_invite_node.py` | 邀请邮件发送后写入时间戳（两条分支都写，写失败只记日志不影响主流程） |
| `app/tasks/timeout_tasks.py` | 新增 `sweep_stale_email_replies()` |
| `app/main.py` | lifespan 中启动常驻巡检任务，关停时取消 |

### 关键安全约束

1. **只处理确实停在中断点的 thread**：恢复前调用 `graph.aget_state()`，
   若 `snapshot.interrupts` 为空则跳过。避免对已完成 / 未启动的 thread 误恢复。
2. **跨 worker 互斥**：`Redis SET NX` 抢锁，多进程部署下同一时刻只有一个
   巡检实例在跑。
3. **图模式关闭时空跑**：`GRAPH_OPEN=False` 时直接返回 0，不触碰 DB / Redis。
4. **批次上限**：单轮最多处理 200 个，避免积压时一次扫全场。
5. **异常隔离**：单个候选人推进失败只记日志，不影响其余候选人。

### 残留风险（已知）

- **极小窗口的竞态**：巡检在 `aget_state()` 之后、`ainvoke()` 之前，
  候选人回信恰好到达，理论上可能出现两次 resume。窗口为毫秒级，
  且 LangGraph 的检查点写入是串行化的；本轮未引入分布式锁消除它。
  若线上观察到该现象，可在恢复前用 Postgres 行锁再确认一次状态。
- **进程重启期间错过的巡检**：重启后下一轮会补上（判据是时间戳，不是调度历史）。

---

## 七、改进项 10：索引补充 + 关联加载策略收敛

### 7.1 索引补充

候选人列表页会按 `position_id` / `status` 过滤并按 `created_at` 倒序，
简历、职位、面试也都有按外键过滤的路径，但此前这些列上没有索引。

**新增索引**（迁移 `c3d4e5f6a7b8`）：

| 索引 | 表 | 列 | 服务的查询 |
|------|----|----|------------|
| `ix_candidates_status` | candidates | status | 按状态筛选 |
| `ix_candidates_position_id` | candidates | position_id | 按职位筛选 |
| `ix_candidates_email` | candidates | email | 邮件回复反查候选人 |
| `ix_candidates_email_invite_sent_at` | candidates | email_invite_sent_at | 超时巡检 |
| `ix_candidates_position_status_created` | candidates | (position_id, status, created_at) | 列表页主路径 |
| `ix_candidate_ai_scores_candidate_id` | candidate_ai_scores | candidate_id | 评分详情 |
| `ix_resumes_uploader_id` | resumes | uploader_id | 上传者反查 |
| `ix_positions_department_id` | positions | department_id | 部门可见性过滤 |
| `ix_positions_creator_id` | positions | creator_id | 创建者反查 |
| `ix_users_department_id` | users | department_id | 部门成员查询 |
| `ix_interviews_scheduled_time` | interviews | scheduled_time | 面试日程 |

索引在模型层用 `index=True` / `__table_args__` 声明，与迁移保持一致；
迁移使用 `CREATE INDEX IF NOT EXISTS`，可重复执行。

### 7.2 关联加载策略收敛

**原实现**：`CandidateModel.position / resume / creator`、
`UserModel.department`、`PositionModel.department / creator` 全部标注
`lazy="joined"`。后果是**任何**查询——哪怕只需要 `name`——都会强制
JOIN 三张表，同时 joined-eager 还伴随着结果集行膨胀。

**新实现**：关联统一改回默认懒加载，由仓储层按查询场景显式
`selectinload`，并通过两个工厂函数集中管理：

```python
# app/repository/resume_repo.py
def candidate_eager_options():
    return (
        selectinload(CandidateModel.position)
            .selectinload(PositionModel.creator).selectinload(UserModel.department),
        selectinload(CandidateModel.position).selectinload(PositionModel.department),
        selectinload(CandidateModel.resume),
        selectinload(CandidateModel.creator).selectinload(UserModel.department),
    )

# app/repository/position_repo.py
def position_eager_options():
    return (
        selectinload(PositionModel.creator).selectinload(UserModel.department),
        selectinload(PositionModel.department),
    )
```

**为什么必须这样改**：项目使用 asyncio SQLAlchemy，懒加载在非
greenlet 上下文会抛 `MissingGreenlet`。去掉 `lazy="joined"` 后，所有会被
Pydantic 序列化或代码直接访问的关联都必须显式预加载，否则运行期直接报错。

**改造覆盖的查询路径**：

| 路径 | 预加载内容 |
|------|-----------|
| `CandidateRepo.get_by_id` / `get_by_email` | 完整候选人关联树 |
| `CandidateRepo.get_list` | 完整候选人关联树 |
| `CandidateRepo.create_candidate` | flush 后重新查询带关联 |
| `CandidateRepo.get_for_user` | 仅 resume（下载场景） |
| `ResumeRepo.create_resume` | uploader → department |
| `PositionRepo.get_by_id` / `get_possition_list` / `create_position` / `update_position` | creator → department、department |
| `UserRepo.get_by_email` / `get_by_id` | department + managed_departments |
| `UserRepo.get_user_list` / `get_hr_list` | department（+ managed_departments） |

> 注意：`UserRepo.get_by_email` 现在会预加载 `managed_departments`，
> 顺带修复了「HR 用户访问职位列表时 `user.managed_departments` 懒加载」
> 的潜在 `MissingGreenlet`。

### 7.3 顺带修复：职位操作的部门空值

`position_ser` 原本写 `current_user.department.id`，用户未分配部门时
`department` 为 `None`，直接 `AttributeError` → HTTP 500。现改为
`_require_department_id()`，返回明确的业务错误：

```python
if not current_user.department_id:
    raise HTTPException(status_code=400, detail="当前用户未分配部门，无法执行该操作！")
```

---

## 八、附带修复：`/user/me` 密码哈希泄露（P0）

### 问题

`/user/me` 直接返回 SQLAlchemy ORM 对象且**没有 `response_model`**：

```python
@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return user
```

FastAPI 用 `jsonable_encoder` 序列化 ORM 对象时会走 `vars(obj)`，
把 `_password`（Argon2 哈希）原样输出。实测确认：

```
keys: ['_password', 'email', 'realname', 'username']
leaks password: True
```

### 改动

```python
@router.get("/me", response_model=UserSH)
async def me(user: UserModel = Depends(get_current_user)):
    """获取当前登录用户信息（不含密码哈希）。"""
    return UserSH.model_validate(user)
```

同时把 `UserSH.department` 改为可选（`UserModel.department_id` 本身可空，
原 schema 要求必填，一旦启用 `response_model` 就会对无部门用户报校验错）。

> 同类问题在 `/user/list`、`/hr/list` 也已一并按 `UserSH` / `HrSchema`
> 显式序列化，不再直接暴露 ORM。

---

## 九、数据库迁移

新增迁移：`alembic/versions/c3d4e5f6a7b8_add_indexes_and_email_invite_sent_at.py`

```bash
uv run alembic upgrade head
```

- `down_revision` = `a1b2c3d4e5f6`（当前唯一 head，迁移历史保持线性）
- `upgrade`：新增 `candidates.email_invite_sent_at` 列 + 11 个索引
- `downgrade`：删除索引 + 删除列
- 索引语句带 `IF NOT EXISTS`，可安全重复执行

> LangGraph 的 `checkpoint_*` 表由 LangGraph 自行管理，不纳入 Alembic。

---

## 十、配置项变更

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `EMAIL_REPLY_TIMEOUT_HOURS` | 48 | 等待候选人回复的超时时间（小时） |
| `EMAIL_REPLY_SWEEP_INTERVAL_MINUTES` | 15 | 超时巡检间隔（分钟） |
| `LOGIN_RATE_LIMIT_PER_IP` | 20 | 单 IP 登录尝试上限 / 60 秒 |
| `LOGIN_RATE_LIMIT_PER_EMAIL` | 5 | 单账号登录尝试上限 / 60 秒 |
| `GUNICORN_*` | 见第三节 | gunicorn 监听地址 / worker 数 / 日志路径 / 超时 |

`README.md` 的「快速开始 → 环境变量」与「可配置项」两处表格已同步更新。

---

## 十一、测试

新增 `tests/test_hardening.py`，共 6 个用例：

| 用例 | 覆盖点 |
|------|--------|
| `test_refresh_token_consumed_atomically` | 并发刷新同一 token，只有一个成功 |
| `test_consume_refresh_token_returns_none_when_missing` | 原子消费脚本的空值语义 |
| `test_refresh_still_issues_new_token_pair` | 正常轮换路径未被破坏 |
| `test_user_sh_has_no_password_field` | 响应模型不含 `password` / `_password` |
| `test_get_checkpointer_before_init_raises` | 检查点未初始化时 fail-fast |
| `test_sweep_noop_when_graph_mode_disabled` | 图模式关闭时巡检空跑 |

测试基建同步调整：`tests/conftest.py` 的 `FakeRedis` 新增 `eval()`，
用同步 dict 操作模拟 `app.core.auth` 的原子消费脚本语义。

```bash
$ .venv\Scripts\python.exe -m pytest -q
....................................................                     [100%]
# 52 passed（原 46 + 新增 6）
```

---

## 十二、部署注意事项

1. **必须先执行迁移**：`uv run alembic upgrade head`，否则超时巡检查询
   `email_invite_sent_at` 会因列不存在而报错（巡检有异常兜底，不会崩进程，
   但功能不生效）。
2. **gunicorn 日志目录**：默认写到 `logs/`，确保该目录存在且 worker 有写权限
   （应用自身的日志模块会自动创建 `logs/`，但 gunicorn 的 access log 由
   gunicorn 自己打开，需目录已存在）。
3. **多 worker 部署**：巡检任务靠 Redis 锁互斥，多 worker 安全；
   但邮件机器人仍限制单 worker。
4. **首次启用超时兜底**：`GRAPH_OPEN=True` 后才会生效。上线后建议观察
   `logs/workflow.log` 中 `[timeout_sweep]` 相关日志，确认推进数量符合预期。

---

## 十三、后续建议（本轮未做）

按优先级排列：

1. **Access Token 不可吊销**：README 宣称「Redis 黑名单吊销」，实际只吊销
   refresh token。建议给 access token 也存 `jti`（TTL = access 有效期），
   或在 user 表加 `token_version`，改密码 / 封号时自增。
2. **CORS `allow_origins=["*"]`**：生产环境应收敛为前端域名白名单。
3. **登录接口的 401 语义**：先在前端响应拦截器中排除 `/user/token`，
   再把登录失败改成 401，语义更准确。
4. **邀请码**：`random.sample` 改 `secrets`、核销后删除、部门取值修正
   （当前写入的是邀请人部门而非请求中的部门）。
5. **长流程脱离 `BackgroundTasks`**：改用 ARQ / Celery，获得重试、死信、
   水平扩展能力。检查点单例化之后，这一步的收益会更明显。
6. **时间存储统一 `TIMESTAMPTZ`**：当前「手工剥时区 + 存 naive」依赖
   每个写入点都记得转换，新增字段时容易漏。
