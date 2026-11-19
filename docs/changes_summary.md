# 本次改造总览（Changes Summary）

> 记录本轮从「日志统一」到「安全加固 + 测试」的全部改动，便于回溯与交接。
> 对应代码版本：simplejob 0.1.0

---

## 0. 变更概览

| 类别 | 内容 | 主要文件 |
|---|---|---|
| 日志 | loguru → 标准库 logging；非阻塞队列；多进程独立监听进程 | `app/core/logging_config.py` |
| 日志 | 请求 ID 贯穿、审计日志独立落盘 | `app/core/request_context.py`、`app/core/audit.py` |
| 安全 | `/media` 静态挂载改为鉴权下载接口 | `app/routers/candidate_router.py` |
| 安全 | 登录滑动窗口限流 | `app/core/rate_limit.py` |
| 安全 | 密钥启动校验 | `app/core/config.py` |
| 质量 | 分页双重切片修复、列表返回 `total` | `app/repository/*.py`、`app/routers/*.py` |
| 质量 | 魔法字符串常量化、日志惰性格式化、死代码清理、命名重构 | 多处 |
| 质量 | 导入副作用收敛、import 顺序统一 | 多处 |
| 性能 | 复用 `httpx.AsyncClient` | `app/core/http_client.py` |
| 测试 | pytest + pytest-asyncio，276 个用例 | `tests/` |
| 工程 | 依赖清理、仓库卫生、部署文档重写 | `pyproject.toml`、`docs/deployment.md` |

---

## 1. 日志系统演进

整个过程分四步，每一步都解决了上一阶段的实际问题。

### 1.1 统一到标准库 logging（移除 loguru）

**问题**：项目同时使用 loguru 和标准库 logging 两套日志，配置互不相通；
loguru 默认写 stderr，不进文件。

**改动**：

- 16 个文件 `from loguru import logger` → `import logging` + `logger = logging.getLogger(__name__)`
- loguru 的 `{}` 占位符 → 标准库 `%s` 惰性格式化
- `logger.opt(exception=True).error(...)` → `logger.error(..., exc_info=True)`
- `pyproject.toml` 移除 `loguru`，`uv.lock` 同步（连带移除 `win32-setctime`）

**校验**：AST 扫描确认无残留 `{}` 占位符、无 `logger.opt`。

### 1.2 非阻塞：QueueHandler + QueueListener

**问题**：同步 handler 在调用线程内完成写盘，阻塞业务线程；在 `async def`
里打日志会卡住整个事件循环。

**设计**：

```
logger.info() → QueueHandler → queue.Queue → QueueListener 线程 → handler
```

调用方只做 `queue.put_nowait`（纯内存），IO 全部在监听线程。

**实测**：慢速 sink（每次 write 阻塞 2ms）下，200 次日志调用
**0.41s → 0.002s**。

### 1.3 多进程安全：独立日志监听进程

**问题**：多 worker 各自写同一批文件时，`os.rename` 轮转没有跨进程协调，
会导致丢日志、备份链断裂（Windows 下直接 `PermissionError`）。

**设计**：

```
主进程（gunicorn master）
  ├─ bootstrap_logging()  → multiprocessing.Queue
  │                          └─ 日志监听进程（daemon，全局唯一写盘者）
  └─ fork ─┬─ worker 1 → QueueHandler ─┐
            ├─ worker 2 → QueueHandler ─┼→ 同一 Queue → 监听进程 → 文件
            └─ worker N → QueueHandler ─┘
```

**关键时序**：gunicorn 的 `Arbiter.start()` 先调 `cfg.when_ready()`
（arbiter.py:224），之后 `run()` 才 `manage_workers()` → `os.fork()`
（arbiter.py:248/734）。队列和监听进程必须在 fork 之前创建，worker 才能继承。

**为什么必须 gunicorn**：`uvicorn/_subprocess.py` 写死
`multiprocessing.get_context("spawn")`，spawn 的 worker 是全新解释器，
拿不到 `multiprocessing.Queue`。**多 worker 必须用 gunicorn（fork）**。
单进程 `uvicorn --workers 1` 走进程内监听线程，同样只有一个写入者。

**移除 concurrent-log-handler**：既然只有一个写入者，跨进程文件锁就没有必要。

### 1.4 请求 ID + 审计日志

- `app/core/request_context.py`：`contextvars` 保存 request_id（异步安全）
- `app/main.py`：中间件读取/生成 `X-Request-ID`，写回响应头
- `logs/audit.log`：独立 `audit` logger，JSON Lines，记录
  `login.success` / `login.failed` / `logout` / `resume.download` / `resume.preview`

**一个容易踩的坑**：request_id 必须在**产生记录的进程**注入。多进程模式下
记录会被序列化后送到监听进程，监听进程的 contextvar 与 worker 无关，所以用
`logging.setLogRecordFactory` 在 worker 侧打标签，并用 `_RequestIdFilter` 兜底。

**日志分流**（同一队列，靠 `record.name` 路由）：

| logger | stdout | workflow.log | error.log | nodes.log | audit.log |
|---|:---:|:---:|:---:|:---:|:---:|
| `workflow.nodes` | ✗ | ✗ | ✗ | ✓ | ✗ |
| `audit` | ✗ | ✗ | ✗ | ✗ | ✓ |
| 其他 | ✓ | ✓ | ✓（仅 ERROR+） | ✗ | ✗ |

### 1.5 一个必须显式处理的细节

监听进程绕过 Logger 直接调 `handler.handle(record)`，而
**`Handler.handle()` 本身不做 level 检查**（那是 `Logger.callHandlers` 的职责）。
所以监听端必须自己判断：

```python
for handler in handlers:
    if record.levelno >= handler.level:
        handler.handle(record)
```

漏掉这行会让 INFO 记录写进 `error.log`——开发过程中真实踩过。

---

## 2. 安全加固

### 2.1 简历下载：`/media` → 鉴权接口

**原实现**：

```python
app.mount("/media", StaticFiles(directory=settings.RESUME_DIR), name="media")
```

简历含姓名、手机、住址、工作履历，属于敏感个人信息。任何人拿到 URL 就能下载。

**新实现**：

| 接口 | 权限 |
|---|---|
| `GET /candidate/{candidate_id}/resume` | superuser 全量 / hr 本部门 / 其他人只能看自己职位的候选人 |
| `GET /candidate/resume/{resume_id}` | 仅上传者本人（用于「刚上传、还没建候选人」的预览） |

- 「不存在」与「无权访问」都返回 404，避免枚举候选人 id
- `_resolve_resume_path()` 校验路径前缀，阻断穿越
- 前端改为 `fetch` 取 Blob：列表页用 `URL.createObjectURL` 新窗口打开，
  上传页用 Blob URL 交给 `<embed>` 预览（`<embed>` 无法携带 Authorization 头）

### 2.2 登录限流

`app/core/rate_limit.py`：Redis ZSET 滑动窗口，判断+写入用 Lua 脚本保证原子性。

```python
await rate_limit.enforce(r, f"rl:login:{ip}:{email}", limit=5, window=60)
```

同一 IP + 账号 60 秒内最多 5 次，超出返回 429。

### 2.3 密钥启动校验

`Settings` 增加 `model_validator`：

- 空密钥 → 启动即报错（原来要等到登录时才抛 `InvalidKeyError`，表现为 500）
- 非 debug 环境密钥 < 32 字符 → 启动即失败

### 2.4 请求 ID 与审计

见 1.4。

---

## 3. 代码质量

### 3.1 修复的真实 bug

**用户列表分页双重切片**（`app/routers/user_router.py`）：

```python
user_list = await auth_ser.get_list_users(...)   # 仓储内已 offset/limit
return {
    "total": len(user_list),                              # 只是当前页条数
    "items": user_list[(page - 1) * size : page * size],   # 对已分页结果再切
}
```

第 2 页起 `items` 恒为空。修复后仓储返回 `(items, total)`，router 不再切片。

**候选人列表缺 `total`**：`CandidateRepo.get_list` 返回 `(rows, total)`，
`CandidateListSchema` 增加 `total` 字段，前端类型同步。

### 3.2 分页边界

`page: Query(1, ge=1)`、`size: Query(10, ge=1, le=100)`，`size=1000000` 直接 422。

### 3.3 魔法字符串

`"CANDIDATE_REFUSED"` / `"SYSTEM_TIME_AVAILABLE"` 散落在路由与节点中，
提取到 `app/workflow/candidate_state.py`：

```python
REPLY_REFUSED = "CANDIDATE_REFUSED"
REPLY_TIME_AVAILABLE = "SYSTEM_TIME_AVAILABLE"
```

### 3.4 日志惰性格式化

30 处 `logger.error(f"...")` → `logger.error("...%s", arg)`。
用一次性 codemod（dry-run 预览 + apply）完成，并用 AST 复扫确认 0 残留。

### 3.5 导入副作用

| 位置 | 处理 |
|---|---|
| `logging_config.LOG_DIR.mkdir()` | 移到 `_build_handlers()`，import 不再建目录 |
| `agents/resume.py` 的 `create_agent(...)` | 改为 `_get_agent()` 懒加载 |
| `scoring_node` / `check_availability_node` 的模块级 Agent | 改为 `_get_score_agent()` / `_get_proposed_time_agent()` |

**没有做的部分**：`db.py` 的 engine、`llms.py` 的 client、`redis_client` 的 URL
必须在导入期构造；只下沉叶子模块不会让 import 变轻，反而制造不一致。
真正的惰性化需要把 engine/client 一起重构，建议单独排期。

### 3.6 死代码与命名

- 删除 `candidate_router` 的注释块、`resume.py` 尾部注释、`notify.py` 注释、
  `candidate_schema` 注释、`auth.py` 中未使用的 `REDIS_*`
- `notify.py` 中 18 处 `print` 其实全在一个废弃 docstring 里（从未执行），已换成正式 docstring
- `utils/ORC_ser.py` → `utils/ocr_service.py`（`git mv` + 2 处 import）
- **`InterviewModel.py` 未改名**：模块路径出现在 checkpoint 的 msgpack 白名单
  （`("app.models.InterviewModel", "InterviewResultEnum")`）里，改名会让已持久化的
  检查点反序列化失败，需要数据迁移

### 3.7 仓库卫生

- `git rm --cached` 共 **153 项**：91 个 `.pyc`、`upload/`（20）、`attachments/`（9）、
  `logs/`（3）、`mailbot_state.json`
- `.gitignore` 补齐 `__pycache__/`、`*.py[cod]`、`.uv-cache/`、`.uv-python/`、
  `upload/`、`attachments/`、`mailbot_state.json`、`tests/.tmp/`

### 3.8 死依赖

- `datetime>=6.0`（与 stdlib 同名，纯误导）
- `logger>=1.4`（2013 年的包，0 处引用）

---

## 4. 性能

### 4.1 复用 httpx.AsyncClient

原来钉钉和 OCR 每次外部调用都 `async with httpx.AsyncClient()`，
每次都重建连接池（TCP + TLS 握手）。

新增 `app/core/http_client.py`：全局懒创建的单例，`FastAPI` lifespan 退出时
`await close_http_client()`。已替换 `dingtalk_ser.py`（4 处）与
`ocr_service.py`（4 处）。

---

## 5. 测试

### 5.1 基础设施

```toml
[dependency-groups]
dev = ["pytest>=9.2.0", "pytest-asyncio>=1.4.1.post1"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
addopts = "-q -p no:cacheprovider"
```

**原则：不依赖数据库 / Redis / 网络 / LLM**，纯内存 + 纯函数，3 秒内跑完。

- `tests/conftest.py` 提供 `FakeRedis`（内存版）、`state_factory`（最小 state）、
  `work_dir`（工作区内临时目录，避开系统 Temp 的权限限制）

### 5.2 覆盖范围（276 个用例）

| 文件 | 覆盖 | 用例数 |
|---|---|---|
| `tests/test_graph_routes.py` | 状态图 4 条路由 + error 守卫 | 19 |
| `tests/test_exception_wrapper.py` | `@node_error_wrapper` 三层策略 | 5 |
| `tests/test_auth_tokens.py` | Token 签发 / 轮换 / 过期 / 吊销 / 类型校验 | 9 |
| `tests/test_logging_routing.py` | 过滤器真值表、落盘分流、request_id | 11 |

**为了让路由可测做的重构**：`candidate_graph.py` 里原本是内联 lambda，
提取为 `_route_after_screening` / `_with_error_check`，4 条路由统一套 error 守卫
（`route_after_email_reply` 之前没有守卫，这次补齐）。

### 5.3 运行

```bash
uv run pytest          # 276 passed in ~8s
```

---

## 6. 破坏性变更

| 变更 | 影响 | 兼容处理 |
|---|---|---|
| `/media/*` 删除 | 旧书签/截图里的简历链接失效 | 前端已改为调用鉴权接口 |
| `CandidateListSchema` 新增必填 `total` | 外部调用方需适配 | 前端类型已同步 |
| 登录限流 | 同 IP+账号 1 分钟 5 次 | 压测/脚本需注意 |
| 密钥启动校验 | 缺密钥时应用**直接起不来** | 预期行为（fail fast） |
| `utils/ORC_ser.py` 改名 | 外部 import 需改 | 已更新全部 2 处引用 |
| 新增 `logs/audit.log` | 备份策略需包含 | — |
| `get_list` / `get_user_list` 返回元组 | 内部接口 | 已更新全部调用点 |

---

## 7. 已知限制 / 后续建议

| 项 | 现状 | 建议 |
|---|---|---|
| 测试覆盖 | 只覆盖纯逻辑（路由/异常/Token/日志），**没有 DB 集成测试** | 引入 testcontainers 或 sqlite+aiosqlite，覆盖分页与权限 |
| CI | 无 | GitHub Actions：`uv sync && uv run pytest && ruff check` |
| `settings` 惰性化 | 部分（叶子模块） | 需重构 engine / LLM client，单独排期 |
| `except Exception` 32 处 | 未收窄 | 先补测试再改，否则回归风险高 |
| 上传校验 | 只校验客户端 `Content-Type`，无大小上限 | 用魔数校验真实类型 + 服务层限流 |
| Access Token 吊销 | 只能吊销 Refresh（30 分钟窗口） | JWT 加 `token_version`，改密/封禁时自增 |
| 审计落库 | 只写文件 | 需要检索/告警时写 `audit_log` 表 |
| CORS | `allow_origins=["*"]` | 生产改白名单 |
| `/docs` | 生产也开放 | 按 `debug` 开关 |
| 双流程 | `GRAPH_OPEN` 两套实现并存 | 确认状态图稳定后删除旧 Agent |
