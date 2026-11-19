# 46 个测试，教会你 pytest

> **SimpleJob 测试套件零基础教程** · 以 `simplejob` 仓库的真实测试为教材：从一句 `assert` 开始，讲到 `fixture`、`parametrize`、手写测试替身，以及一条从 `logger.info()` 到磁盘文件的端到端验证链路。全部示例取自仓库真实代码，不是玩具。

| 通过用例 | 测试文件 | 全量耗时 | 外部依赖 |
|:---:|:---:|:---:|:---:|
| **46** | **4** | **3.94s** | **0** |

---

## 目录

| | | |
|---|---|---|
| [00 先看结果：46 passed in 3.94s](#00-先看结果46-passed-in-394s) | [01 pytest 的三条约定](#01-pytest-的三条约定) | [02 配置先行：pyproject.toml 的 6 行](#02-配置先行pyprojecttoml-的-6-行) |
| [03 全景：测试的边界画在哪](#03-全景测试的边界画在哪) | [04 fixture：pytest 的依赖注入](#04-fixturepytest-的依赖注入) | [05 一次运行经过的七个阶段](#05-一次运行经过的七个阶段) |
| [06 手写替身：FakeRedis](#06-手写替身fakeredis) | [07 数据驱动：parametrize 真值表](#07-数据驱动parametrize-真值表) | [08 断言"应该失败"：pytest.raises](#08-断言应该失败pytestraises) |
| [09 为可测性而设计：抽纯函数](#09-为可测性而设计抽纯函数) | [10 用例精读：refresh token 十例](#10-用例精读refresh-token-十例) | [11 分层测试：日志分流的三种测法](#11-分层测试日志分流的三种测法) |
| [12 可读性：测试名就是规格书](#12-可读性测试名就是规格书) | [13 命令速查表](#13-命令速查表) | [14 动手练习（含答案）](#14-动手练习含答案) |
| [15 坦白局：还没覆盖什么](#15-坦白局还没覆盖什么) | [16 一页纸总结](#16-一页纸总结) | |

---

## 00 先看结果：46 passed in 3.94s

### 先读输出，再读代码

拿到一个陌生项目，最省力的入门方式不是打开源码，而是**跑一遍测试并读懂输出**。下面这段是本项目 `pytest -v` 的真实输出，后面每一个数字我们都会解释到。

```text
============================= test session starts =============================
platform win32 -- Python 3.13.9, pytest-9.2.0, pluggy-1.7.0
rootdir: E:\dj\boke\simplejob
configfile: pyproject.toml          # ← 配置不是写在 ini 里，而是 pyproject.toml（第 02 节）
testpaths: tests                     # ← 只收集 tests/ 目录
plugins: anyio-4.15.1, langsmith-0.14.8, asyncio-1.4.1.post1
asyncio: mode=Mode.AUTO, debug=False  # ← 关键：async 测试不用写 mark（第 02 节）
collected 46 items                   # ← 收集到 46 个用例

tests\test_auth_tokens.py ..........                    [ 21%]  # 10 个
tests\test_exception_wrapper.py .....                   [ 32%]  # 5 个
tests\test_graph_routes.py ......................       [ 80%]  # 22 个
tests\test_logging_routing.py .........                 [100%]  # 9 个

============================= 46 passed in 3.94s ==============================
```

**怎么读**：每个 `.` 是一个通过的用例，`F` 是失败，`E` 是用例出错（fixture 崩了、语法错），`s` 是跳过，`x` 是预期失败。方括号里的百分比是**进度**，不是通过率。最后一行才是结论：**46 个用例全部通过，用时 3.94 秒**。

### 四个文件，四个战场

这套测试不是"随便补几个用例"，而是按**被测风险**分工的。先看这张地图，后面逐行拆解。

| 测试文件 | 用例数 | 被测对象 | 核心手法 |
|---|:---:|---|---|
| `test_auth_tokens.py` | 10 | `app/core/auth.py` 签发 / 轮换 / 吊销 | 手写 FakeRedis 替身；自己签发一枚"已过期"的 token 来做时间旅行 |
| `test_exception_wrapper.py` | 5 | `error_handler.py` 的 `@node_error_wrapper` | 三层异常策略的真实调用；`pytest.raises` + 异常链断言 |
| `test_graph_routes.py` | 22 | `candidate_graph.py` 4 个路由函数 + 1 个守卫 | 纯函数 + `parametrize` 真值表，占全量 48% |
| `test_logging_routing.py` | 9 | `logging_config.py` + `audit.py` 日志分流 | 过滤器真值表 → 真实 handler 落盘 → QueueListener 端到端，三层递进 |

> ⚠️ **这套测试最值得学的一点**：整份 `conftest.py` 的第一行注释就是设计纲领——"**不依赖数据库 / Redis / 网络 / LLM，纯内存与纯函数，保证测试能在任何机器上秒级跑完**"。3.94 秒这个数字不是运气，是被刻意设计出来的——这是本项目最值得抄走的工程决策。

---

## 01 pytest 的三条约定（零基础从这里开始）

### 没有任何魔法：只靠名字和关键字

pytest 不需要继承 `TestCase`、不需要写 `self.assertEqual`、甚至连测试类都不用写。它只靠三条约定找测试：

1. **文件名**：`test_*.py` 或 `*_test.py`。本项目统一用 `test_*.py`。
2. **函数名**：`def test_` 开头。只要名字对，pytest 就会执行它——包括 `async def`（配置见下节）。
3. **断言方式**：直接用 Python 的 `assert`。pytest 会重写 assert 语句，失败时打印出两边的实际值。

```python
# 文件路径：tests/test_exception_wrapper.py:57
async def test_success_is_passed_through(state_factory):
    @node_error_wrapper              # ← 被测对象：这个装饰器
    async def node(state):
        return {"ok": True}           # ← 节点本体什么都不做，只返回一个 dict

    assert await node(state_factory()) == {"ok": True}   # ← 正常路径：装饰器不该动返回值
```

### 逐行拆解这个 8 行测试

- **为什么是 `async def`？** 因为被测的装饰器返回的是协程函数。pytest-asyncio 会自动运行它——这个"自动"来自下一节的配置。
- **为什么参数里有个 `state_factory`？** 这就是 fixture（第 04 节）。测试函数*声明*自己要什么，pytest 负责*交给*它。不需要 import，不需要手动构造。
- **为什么节点内部只 `return`？** 这是在测装饰器，不是在测业务。被测对象越单一，失败时的信息越准确。这叫"一个测试只验证一个行为"。
- **这个测试的名字就是规格**：`test_success_is_passed_through`——"成功路径的返回值被原样透传"。看完名字就不需要读实现。

> 💡 **零基础最容易踩的坑**：测试函数**必须不接收没被注册的参数**。写成 `def test_x(foo)` 而项目里没有叫 `foo` 的 fixture，pytest 会直接报 `fixture 'foo' not found`——这是新手第一个高频错误，看到这句话就去查 conftest.py 或拼写。

---

## 02 配置先行：pyproject.toml 的 6 行决定了后面一切

### 项目没有 pytest.ini，配置在 pyproject.toml

这也是现代 Python 项目的趋势：所有工具配置集中在一个文件。本项目只用了 5 个键，但每个都很关键。

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]                              # ① 只在这些目录里找测试
asyncio_mode = "auto"                              # ② async def test_* 自动被当成异步测试
asyncio_default_fixture_loop_scope = "function"      # ③ 每个用例一个独立事件循环
addopts = "-q -p no:cacheprovider"                 # ④ 默认安静 + 不生成 .pytest_cache

[dependency-groups]
dev = [
    "pytest>=9.2.0",
    "pytest-asyncio>=1.4.1.post1",   # ← 提供 asyncio_mode 的插件
]
```

| 配置项 | 作用 | 如果不写会怎样 |
|---|---|---|
| `testpaths` | 把搜索范围锁定在 `tests/` | pytest 会扫描整个工程，包括 `.venv/` 里上万个第三方测试文件，收集阶段就要几分钟 |
| `asyncio_mode = "auto"` | 所有 `async def test_*` 自动运行，无需装饰器 | 必须给每个异步用例手写 `@pytest.mark.asyncio`，本项目 46 个用例里 40 个是 async，会极其啰嗦 |
| `asyncio_default_fixture_loop_scope` | 异步 fixture 与用例共享**同一个** function 级事件循环 | 新版 pytest-asyncio 会告警；跨 loop 复用的连接对象（如 Redis 客户端）会报 "attached to a different loop" |
| `-q` | quiet：少打印环境信息 | 默认输出更啰嗦，CI 日志更长 |
| `-p no:cacheprovider` | 禁用 `.pytest_cache` 插件 | 仓库根目录会多出缓存目录；`--lf`（只跑上次失败的）也随之不可用 |

> ⚠️ **一个真实的副作用，顺手教你排查**：仓库根目录躺着两个空目录 `pytest-cache-files-0kwk5kae` / `pytest-cache-files-52998pce`。它们是 pytest 临时目录机制在**启用缓存插件时**留下的痕迹。本项目现在已经禁用了 `cacheprovider`，所以这两个目录可以安全删除——但它们也顺带解释了下节要讲的 `work_dir` fixture 为什么不用 `tmp_path`。

---

## 03 全景：测试的边界画在哪

### 把"重"的东西挡在测试之外

一个招聘系统真实运行时要连 Redis、PostgreSQL、LLM API、SMTP 服务器。如果测试也去连这些，就会出现三种灾难：**慢**（每次几秒到几分钟）、**脆**（网络抖一下就红）、**脏**（测试数据污染开发库）。

本项目的解法是：测试侧只用**替身**（FakeRedis、最小 state、临时目录），真实依赖一个都不启动。

> 📊 **测试体系全景图**（可交互，可点节点看上下游、可切明暗主题）：[打开 test-suite-architecture.html](diagrams/test-suite-architecture.html)

**图注**：左侧是 `tests/` 里的三层结构（运行器 → 夹具 → 用例），中间是四个被测模块，右侧是测试中**一律不启动**的生产依赖。橙色虚线表示"被替身替代"的关系——这是整张图的关键。

| 特性 | 说明 |
|---|---|
| 🚀 **3.94 秒 = 纯内存** | 没有一次 TCP 连接、没有一次磁盘写（除了日志用例刻意写的临时目录）。所以开发时愿意反复跑。 |
| 🧪 **互不影响** | 每个用例自己的 FakeRedis 实例、自己的临时目录，跑 100 遍结果一致，可并行、可乱序。 |
| 📦 **换机器就能跑** | 克隆仓库 → `uv sync` → `pytest`。不需要 Docker、不需要本地 PostgreSQL。 |

---

## 04 fixture：pytest 的依赖注入，也是这套测试的地基

### conftest.py 是什么

`conftest.py` 是 pytest 的**自动加载**文件：放在目录里，该目录及子目录下所有测试都能**按名字**使用里面定义的 fixture，**不需要 import**。本项目整个测试基础设施就 102 行，全在这一个文件里。它的四段内容，我们逐个看：

```python
class FakeRedis:
    """够用的内存版 Redis，覆盖 token 相关命令。"""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}      # ← 用一个 dict 冒充 Redis 的键空间
        self.ttl: dict[str, int] = {}        # ← 额外记录过期秒数，便于断言

    async def setex(self, name: str, time: int, value: Any) -> bool:
        self.store[name] = value
        self.ttl[name] = time
        return True

    async def get(self, name: str) -> Any:
        return self.store.get(name)

    async def exists(self, *names: str) -> int:
        return sum(1 for n in names if n in self.store)

    async def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            if name in self.store:
                del self.store[name]
                removed += 1
        return removed
```

> 💡 **关键洞察**：测试能不能替身化，取决于**被测代码用了多宽的接口**。本项目 `auth.py` 只调用了 `setex / get / exists / delete` 四个命令，所以 FakeRedis 只需要四个方法，20 行就够。**接口窄 = 替身便宜**，这反过来也在提醒你：不要为了"以后可能用到"而在业务代码里到处直连 Redis 客户端。

```python
@pytest.fixture                       # ① 最简单的一种：每次用例前新建一个空 FakeRedis
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture                       # ② 返回一个「工厂函数」：调用它才真正造数据
def state_factory():
    """构造一份最小的 CandidateRecruitmentState。

    路由函数只用到 ``candidate.id`` / ``candidate.name``，所以用
    SimpleNamespace 代替重量级的 Pydantic schema。
    """

    def _make(**overrides) -> dict:
        state: dict[str, Any] = {
            "candidate": SimpleNamespace(id="cand-001", name="张三"),
            "position": SimpleNamespace(id="pos-001", title="后端工程师"),
            "interviewer": SimpleNamespace(id="user-001"),
            "current_stage": "screening",
        }
        state.update(overrides)        # ← 只覆盖本用例关心的字段，其余用默认值
        return state

    return _make                     # ← 注意：返回的是函数，不是 dict


@pytest.fixture                       # ③ 带清理的 fixture：yield 之前是 setup，之后是 teardown
def work_dir():
    """工作区内的临时目录。

    不用 pytest 的 ``tmp_path``：它落在系统 Temp 下，部分受限环境
    （沙箱 / 只读 Temp）会写不进去。
    """
    base = Path(__file__).resolve().parent / ".tmp"
    path = base / uuid4().hex           # ← uuid 保证并行/重复运行时互不冲突
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path                     # ← 交出目录；用例结束后回到这里继续
    finally:
        shutil.rmtree(path, ignore_errors=True)   # ← 即使用例抛异常也一定清理


@pytest.fixture(autouse=True)          # ④ autouse：不写参数也会自动生效
def _quiet_logging():
    """测试期间把 root logger 压到 WARNING，避免日志刷屏。"""
    root = logging.getLogger()
    previous = root.level              # ← 先存旧值
    root.setLevel(logging.WARNING)
    try:
        yield
    finally:
        root.setLevel(previous)         # ← 恢复旧值，别让测试污染全局状态
```

### 四个 fixture，四种常见形态

| fixture | 形态 | 零基础要点 |
|---|---|---|
| `fake_redis` | 返回值 | 最普通的写法。`return` 什么，测试函数的同名参数就拿到什么。 |
| `state_factory` | 返回工厂函数 | 当同一个测试里要造**多份**数据时用它。`state_factory(screening_passed=True)` 一句话覆盖字段，比手抄 7 个键安全得多。 |
| `work_dir` | `yield` + 清理 | `yield` 之前是准备，`yield` 之后是善后，**`finally` 保证异常时也清理**。这是"会自己打扫卫生的测试"。 |
| `_quiet_logging` | `autouse` | 名字带下划线表示"不打算被引用"，`autouse=True` 让它在每个用例前后自动跑。改全局状态 + 恢复，是它的标准模式。 |

> ℹ️ **为什么不用 pytest 自带的 tmp_path？** `tmp_path` 把目录建在系统 Temp（如 `C:\Users\...\AppData\Local\Temp\pytest-xxx`）。在沙箱或只读 Temp 环境里会直接 `PermissionError`。本项目改成在工作区内建 `tests/.tmp/<uuid>`，随项目走、永远可写。注释里写明了原因——**好注释解释"为什么"，而不是"做了什么"**。

---

## 05 一次 pytest 运行，实际经过七个阶段

### 你以为只按了一次回车，其实流水线很长

理解这七个阶段，你就能把 90% 的报错**定位到具体阶段**：报错信息在收集阶段说明名字/导入有问题，在装配阶段说明 fixture 有问题，在执行阶段才是真的逻辑挂了。

下面的图是**压缩成五段的视图**（配置 → 夹具 → 替身 → 执行 → 判定），把"收集"和"装配"合并了；紧随其后的表格再把它们拆回七个更细的步骤，两者对照着看最清楚。

> 📊 **一次 pytest 运行的数据流图**（可交互）：[打开 pytest-execution-dataflow.html](diagrams/pytest-execution-dataflow.html)

**图注**：注意"夹具 → 替身 → 执行"这一段：`conftest` 按参数名把 `fake_redis` 和 `work_dir` 分别交给同一个测试函数——这正是第 04 节那些 fixture 在流水线上的位置。

### 把阶段和报错对上号

| 阶段 | pytest 在做什么 | 典型报错关键字 |
|---|---|---|
| ① 配置 | 读 pyproject.toml 的 ini_options | `invalid ini option` |
| ② 收集 | 按 `testpaths` 扫描文件、导入模块、找 `test_` 开头函数 | `ERROR collecting`、`ImportError`、`ModuleNotFoundError` |
| ③ 装配 | 按参数名解析 fixture，递归准备它依赖的 fixture | `fixture 'x' not found`、`RuntimeError: no running event loop` |
| ④ 执行 | 调用测试函数（async 用例交给事件循环） | 业务代码自己的异常栈 |
| ⑤ 判定 | 执行 assert；`pytest.raises` 判断"是否按预期抛错" | `AssertionError`、`DID NOT RAISE` |
| ⑥ 报告 | 统计 . / F / E，给出耗时与失败详情 | `short test summary info` |
| ⑦ 清理 | 执行 fixture 里 `yield` 之后的代码 | teardown 阶段的 `ERROR` |

> ⚠️ **零基础必看：teardown 报错长什么样**：如果一个用例显示"PASSED"但整个运行仍然红，大概率是 **teardown 阶段**挂了——也就是 fixture 里 `yield` 之后那句清理代码抛了异常。本项目用 `try / finally` + `ignore_errors=True` 就是为了避免这种"莫名其妙的红"。

---

## 06 手写替身：FakeRedis 为什么比 Mock 更好读

### Fake 与 Mock 的区别

很多教程一上来就教 `unittest.mock.MagicMock`。本项目**一个 mock 都没用**，全是手写的假对象。原因值得记住：

| | |
|---|---|
| **MagicMock：什么都能答应** | 你写 `redis.any_typo()` 它也不报错，返回一个假对象。测试**通过**了，但生产代码里那行是 `AttributeError`。 |
| **FakeRedis：只会四个命令** | 调用不存在的命令直接 `AttributeError`。**替身越"窄"，测试越诚实**——它逼你和真实接口对齐。 |

| FakeRedis 方法 | `auth.py` 里在哪用 | 测试在验证什么 |
|---|---|---|
| `setex` | `create_tokens()` 签发后登记 `refresh:{jti}` | "新 refresh token 一定在 Redis 里留了记录" |
| `exists` | `refresh_access_token()` 判断是否被吊销 | "旧 token 用过一次后必须查不到"（防重放） |
| `delete` | 轮换时删旧 jti / 吊销时删 jti | "吊销后立刻失效" |
| `get` | 当前版本未直接使用，但接口保留 | 为将来扩展留的最小面 |

> 💡 **这就是"契约测试"的雏形**：FakeRedis 是一个**接口契约的可执行文档**：读 20 行代码，你就知道系统对 Redis 的全部依赖面。等到某天有人想加 `r.incr()`，FakeRedis 会立刻报错提醒他"替身还没实现"，这反而是好事。

```python
async def test_issue_tokens_registers_refresh_jti(fake_redis):
    tokens = await auth.create_tokens("a@b.com", fake_redis)   # ← 注入替身
    refresh = auth._decode(tokens["refresh_token"], auth.REFRESH_SECRET)

    # 断言 1：签发的同时，Redis 里必须存在 refresh:{jti}
    assert await fake_redis.exists(f"refresh:{refresh['jti']}") == 1
```

> ℹ️ **注意 `auth._decode` 这个下划线方法**：测试里直接调了私有函数。这在测试里**是允许且常见的**：测试是"内部实现的白盒观察者"，为了**精确观察**而用私有钩子，比为了测试去污染公开 API 要好。前提是——它只用于**读取和构造**，不用于绕过核心逻辑。

---

## 07 数据驱动：parametrize 与"真值表"测试法

### 当一个行为有 4 种输入，别写 4 个函数

`@pytest.mark.parametrize` 让一个测试函数跑多组数据，每组**单独计为一个用例**。本项目 46 个用例里有 22 个来自 `test_graph_routes.py`，其中大部分是参数化展开的。

```python
@pytest.mark.parametrize(
    ("score", "threshold", "expected"),    # ← ① 参数名（字符串元组）
    [
        (8, 7, "email_invite"),   # 高于阈值
        (7, 7, "rejected"),       # 等于阈值不算通过（严格大于）← 边界值！
        (6, 7, "rejected"),
        (9, None, "email_invite"),  # 用默认阈值
    ],
)
def test_route_after_scoring(state_factory, score, threshold, expected):   # ← ② 参数名变成函数形参
    state = state_factory(
        score_result=SimpleNamespace(overall_score=score),
        score_pass_threshold=threshold,
    )
    assert _route_after_scoring(state) == expected    # ← ③ 唯一的断言，放之四组数据皆准
```

### 这三行注释里藏着测试设计的核心思想

- **"等于阈值不算通过"**——这是**边界值分析**。`score > threshold` 写成 `>=` 是极常见的实现错误，而 (7, 7) 这一组数据是唯一能抓住它的。
- **"高于阈值"+"低于阈值"**——这是**等价类划分**：不必穷举 0~10 分，取代表值就够。
- **(9, None)**——这是**默认值分支**。很多 bug 只在"配置没传"时出现，必须单独覆盖。

四组数据 = 四类风险。参数化的价值不只是"少写代码"，而是**让风险的分类一目了然**。

> ℹ️ **运行时看到的用例名**：`pytest -v` 会把每组数据展开成 `test_route_after_scoring[8-7-email_invite]`、`test_route_after_scoring[7-7-rejected]`……**失败时你一眼就知道是哪组数据挂的**，这是 for 循环写法给不了的。

```python
@pytest.mark.parametrize(
    ("passed", "expected"),
    [(True, "scoring"), (False, "rejected")],   # ← 布尔函数的两行真值表
)
def test_route_after_screening(state_factory, passed, expected):
    state = state_factory(screening_passed=passed)
    assert _route_after_screening(state) == expected
```

> ⚠️ **不要用 for 循环代替 parametrize**：`for passed in (True, False): assert ...` 也能"跑两组"，但：① 失败后第一组就中断，第二组永远跑不到；② 报告里只有一个用例名；③ 无法单独重跑某一组（`pytest -k "7-7"` 这种就不存在了）。

---

## 08 断言"应该失败"：pytest.raises 的三层断言

### 好代码的失败路径和成功路径一样重要

`test_exception_wrapper.py` 测的正是"出错时到底会发生什么"。被测的 `@node_error_wrapper` 定义了三层策略，测试就针对性地给了三层断言：

| 异常类型 | 装饰器的处理 | 测试怎么断言 |
|---|---|---|
| `BusinessException` 及其子类 | 捕获 → 返回一个 `error` 状态 dict，让状态图继续流转到错误节点 | **不抛异常**，断言返回 dict 里的 `current_stage == "error"` |
| `ExternalServiceError` 及其子类 | 不捕获，原样向上抛（基础设施故障要交给外层统一标记） | `with pytest.raises(LLMServiceError):` |
| 其他未知异常 | 包装成 `AppException`，并用 `from e` 保留原始异常链 | `pytest.raises(AppException)` + 检查 `excinfo.value.__cause__` |

```python
# ── 第一层：业务异常 → 变成 error state，不抛 ─────────────────
async def test_business_exception_becomes_error_state(state_factory):
    node = _node_raising(InterviewSchedulingError("面试官 7 天内无空闲"))

    result = await node(state_factory())

    assert result["current_stage"] == "error"
    assert result["error_occurred"] is True          # ← 用 is 而不是 ==，断言"真的是 True"
    assert "无空闲" in result["error_message"]       # ← 只断言关键字，不锁死整句文案
    assert result["error_stage"] == "screening"      # ← 记录的是"抛出时所处阶段"


# ── 第二层：基础设施异常 → 必须穿透，不能被吞 ─────────────────
async def test_external_service_error_propagates(state_factory):
    """基础设施异常不能被吞掉，否则外层无法标记 PROCESSING_ERROR。"""
    node = _node_raising(LLMServiceError("LLM 超时"))

    with pytest.raises(LLMServiceError):      # ← 断言"这里必须抛这个异常"
        await node(state_factory())           # ← await 必须在 with 块内！


# ── 第三层：未知异常 → 包装 + 保留原因链 ──────────────────────
async def test_unknown_exception_wrapped_into_app_exception(state_factory):
    node = _node_raising(ValueError("boom"))

    with pytest.raises(AppException) as excinfo:   # ← as excinfo 拿到异常对象
        await node(state_factory())

    assert "boom" in excinfo.value.message
    assert isinstance(excinfo.value.__cause__, ValueError)   # ← 原异常仍可追溯
```

### 四个必须记住的细节

1. **`with pytest.raises(...)` 里的代码没抛异常 = 测试失败。** 报错叫 `Failed: DID NOT RAISE <class 'LLMServiceError'>`。所以这个测试真正锁定的是"*异常没被偷偷吞掉*"这一行为——正是注释里那句话的意思。
2. **异常类型必须精确。** `pytest.raises(AppException)` 也能接住子类，但如果你要证明"接住的是*这个*子类"，就写最具体的那个。类型越具体，回归越早暴露。
3. **`excinfo.value.__cause__` 是在测"异常链"。** 生产代码写的是 `raise AppException(...) from e`。如果没有 `from e`，日志里就丢掉了根因。**这一行断言把"可调试性"也变成了被测试的特性**。
4. **`assert "无空闲" in result["error_message"]` 的取舍。** 只断言关键字，不断言整句。这样将来调整文案不会被一个无意义的测试卡住，但核心信息（哪个面试官、什么原因）仍然被锁定。**测试要绑行为，不要绑字面量。**

> ⚠️ **`match=` 参数（本项目用了 6 次）**：`pytest.raises(ValueError, match="revoked")` 会对异常**字符串**做一次**正则 search**（不是全文匹配）。好处是异常多、消息像的时候，能区分"是吊销那条路径还是过期那条路径"。注意 `match` 用的是 `re.search`，所以 `match="拒绝"` 能匹配到"刷新被拒绝"。特殊字符要转义。

---

## 09 为可测性而设计：把分支抽成纯函数

### 22 个用例为什么能写得这么快

`test_graph_routes.py` 占了全量 48% 的用例，却是最好写的一个文件。原因是**源码先为它让了路**：LangGraph 的"下一步去哪"被抽成了**独立的纯函数**——只读一个 dict，只返回一个字符串，不碰数据库、不调 LLM、不需要编译状态图。

```python
def _with_error_check(router):
    """给路由函数加一层「节点已标记错误 → 直接去 error 节点」的守卫。"""

    def guarded(state: CandidateRecruitmentState) -> str:
        if state.get("error_occurred"):       # ① 一句 if 挡住所有错误路径
            return "error"
        return router(state)                  # ② 否则委托给真正的路由

    return guarded                            # ③ 返回新函数（装饰器的另一种写法）


def _route_after_screening(state) -> str:
    """screening 节点之后的路由：通过去评分，否则淘汰。"""
    cid = state["candidate"].id
    passed = bool(state.get("screening_passed"))   # ← .get() 而不是 []：字段缺失不 KeyError
    dest = "scoring" if passed else "rejected"
    logger.info("[route_screening] candidate_id=%s | passed=%s | dest=%s", cid, passed, dest)
    return dest
```

> 💡 **设计决策，不是测试技巧**：在 LangGraph 里，路由函数完全可以写成内联 lambda 塞进 `add_conditional_edges`。本项目选择**具名 + 可 import**，就是为了"能被 22 个测试直接调用"。这是零基础读者最该带走的一句话：**可测性首先是设计属性，其次才是测试技巧。**

> 📊 **4 条路由函数 + 1 个 error 守卫的决策图**（可交互）：[打开 route-guard-workflow.html](diagrams/route-guard-workflow.html)

**图注**：中间泳道是判定主线，上方泳道是"淘汰与超时"出口，下方泳道是"成交通道"。注意那条标着"继续协商"的虚线回路 `avail → reply`：它靠轮次上限兜底，防止无限循环。

```python
def test_error_guard_short_circuits(state_factory):
    router = _with_error_check(_route_after_screening)      # 现场给路由套上守卫
    state = state_factory(screening_passed=True, error_occurred=True)
    assert router(state) == "error"                    # 即使 screening 通过了，也强制去 error


@pytest.mark.parametrize(
    "router",                                       # ① 直接参数化「函数对象」本身
    [
        _route_after_screening,
        _route_after_scoring,
        route_after_email_reply,
        route_after_check_availability,
    ],
)
def test_all_routes_respect_error_guard(state_factory, router):
    """4 条路由都被同一个守卫包住：标记错误后一律去 error 节点。"""
    guarded = _with_error_check(router)
    assert guarded(state_factory(error_occurred=True)) == "error"
```

### 这个测试聪明在哪

- **参数化的是行为，不是数据。** 它把"4 条路由"当成 4 个数据点，一行断言覆盖 4 个函数。
- **它锁定了一条*架构约束***：将来有人新增第 5 条路由却忘了加 `_with_error_check`，只要把新函数加进这个列表，测试就会红。这是"防遗忘"型测试。
- **注意它没有断言其他任何东西。** 不检查日志、不检查 state 是否被修改——只检查守卫的职责。职责单一，失败信息才干净。

> ℹ️ **state_factory 的取舍也是可测性设计**：完整的 `CandidateRecruitmentState` 有 20 个字段、依赖 Pydantic schema。但路由函数只读 `candidate.id` 等少数几个键，所以测试用 `SimpleNamespace` 造一个**只有 4 个键的假 state**。原则：**替身只需满足被测代码*实际使用*的契约**，不必模拟整个对象。这样 fixture 永远跟得上源码变化。

---

## 10 用例精读：refresh token 的 10 个测试

### 10 个用例 = 一条 token 生命的完整剧本

`auth.py` 的 refresh token 机制只有 3 个公开函数（签发 / 刷新 / 吊销），但它涉及签名、类型、有效期、吊销状态四种校验。10 个测试把它们逐一钉死。

> 📊 **refresh token 生命周期图**（可交互）：[打开 refresh-token-lifecycle.html](diagrams/refresh-token-lifecycle.html)

**图注**：四步依次是：签发 → 轮换 → 失效（过期 / 吊销 / 篡改都归到此）→ 刷新被拒。下方三张卡片把这 10 个用例按阶段归了档，每个转换都能指出是哪个测试在守护。

### 不用 sleep 的"时间旅行"

```python
def _forged_refresh_token(seconds_to_live: int, jti: str = "forged-jti") -> str:
    """直接构造一个 refresh token（用于模拟已过期 / 已吊销）。"""
    return auth._encode(
        {
            "sub": "a@b.com",
            "jti": jti,
            "type": "refresh",
            "exp": dt.datetime.now(dt.timezone.utc)
                   + dt.timedelta(seconds=seconds_to_live),   # ← 传负数就是「已过期」
        },
        auth.REFRESH_SECRET,                                   # ← 用真密钥签，签名合法
    )
```

> 💡 **这一个辅助函数解决了最经典的测试难题**："怎么测过期？" 教科书答案是 `time.sleep(7 天)`——现实中不可行。本项目的做法是 **直接伪造一个已经过期的 token**：用真密钥签名（所以签名校验能通过），但把 `exp` 设成 1 秒前。于是 `test_expired_refresh_token_is_rejected` 能在**毫秒内**验证"过期 token 会被拒"。同类技巧的通用名：**控制时间，而不是等待时间**。

### 被测的四道关卡

```python
async def refresh_access_token(refresh_token: str, r: Redis) -> dict:
    try:
        payload = _decode(refresh_token, REFRESH_SECRET)
    except jwt.ExpiredSignatureError:              # 关卡② 过期  → "Refresh token expired"
        raise ValueError("Refresh token expired")
    except jwt.InvalidTokenError:                  # 关卡① 签名 → "Invalid refresh token"
        raise ValueError("Invalid refresh token")

    if payload.get("type") != "refresh":           # 关卡③ 类型 → "Invalid token type"
        raise ValueError("Invalid token type")

    jti = payload["jti"]
    user_id = payload["sub"]

    if not await r.exists(f"refresh:{jti}"):      # 关卡④ 吊销 → "…has been revoked"
        raise ValueError("Refresh token has been revoked")

    await r.delete(f"refresh:{jti}")               # ← 立刻消费掉旧 token（一次性）

    return await create_tokens(user_id, r)         # ← 发一对新 token，完成轮换
```

### 10 个用例与 4 道关卡的对应表

| 测试 | 打的是哪道关卡 | 手法上值得学的点 |
|---|---|---|
| `test_issue_tokens_has_correct_claims` | 签发 | 用真函数解码回来验证载荷，而不是去猜字符串 |
| `test_issue_tokens_registers_refresh_jti` | 签发 → 登记 | 断言 `exists(...) == 1`，把"Redis 里必须有记录"变成契约 |
| `test_expiry_follows_settings` | 配置一致性 | **模块常量必须等于 Settings**，防止有人硬编码 30 分钟 |
| `test_refresh_rotates_and_invalidates_old_token` | 关卡④ + 轮换 | 连做两次刷新，第二次必须 `match="revoked"`（防重放攻击） |
| `test_token_with_access_type_rejected_as_refresh` | 关卡③ 类型 | 用 `REFRESH_SECRET` 签一个 `type=access` 的 token，才走得到类型校验 |
| `test_access_token_from_another_secret_is_rejected` | 关卡① 签名 | 拿 access token 去刷新，签名就过不了——双密钥隔离的直接证据 |
| `test_expired_refresh_token_is_rejected` | 关卡② 过期 | 用"时间旅行"辅助函数，无需等待 |
| `test_tampered_token_is_rejected` | 关卡① 签名 | 只追加一个字符 `+ "x"`，模拟传输损坏 |
| `test_revoke_blocks_subsequent_refresh` | 关卡④ 吊销 | 吊销后再刷新必须失败 |
| `test_revoke_with_garbage_token_is_noop` | 健壮性 | 传 `"not-a-jwt"` 不抛异常——**幂等的吊销接口** |

> ⚠️ **最后那个用例只有一行，却很有分量**：`test_revoke_with_garbage_token_is_noop` 的函数体只有一句 `await auth.revoke_refresh_token("not-a-jwt", fake_redis)`，**一个 assert 都没有**。它合法吗？合法——这叫**smoke test（冒烟测试）**："这么调用*不应该炸*"。而它守护的是一个真实的设计决策：登出接口必须容错，否则用户拿一个坏 token 就登不出去。零基础提醒：**"没有 assert"不等于"没在测试"**，但它必须**名字 + docstring 说清楚在测什么**，否则半年后没人敢删。

> ℹ️ **为什么要断言配置一致性？** `test_expiry_follows_settings` 测的是"`auth.py` 里的 `ACCESS_EXPIRE_MINUTES` 是否仍然读自 `Settings`"。这类测试叫**架构约束测试 / 退化防护**：它不测功能，它防止有人图省事写死一个 `30`。一旦有人这么干，`.env` 里改配置就悄悄失效了——这种 bug 极难在生产中发现。

---

## 11 分层测试：日志分流的三种测法，层层加码

### 同一个功能，三种"真实度"

`test_logging_routing.py` 是全部 46 个用例里最有方法论价值的一份。它测的是"日志该进哪个文件"，而这件事**可以只测过滤器，也可以真写文件**。本项目三种都做了：

| 层次 | 测试 | 真实度 | 能发现什么 / 发现不了什么 |
|---|---|---|---|
| L1 单元 | `test_include_true_is_whitelist`<br>`test_include_false_is_blacklist` | 只调 `filter()`，纯布尔 | ✅ 白名单/黑名单逻辑写反<br>❌ handler 配错、文件没打开、级别判断错 |
| L2 集成 | `test_business_logs_go_to_expected_files`<br>`test_audit_log_is_isolated` | 真 handler + 真文件（在 `work_dir` 里） | ✅ 真的写进了哪个文件、有没有串台<br>❌ "audit 有没有接进日志管道"（handler 全对，但 root logger 没挂） |
| L3 端到端 | `test_audit_call_reaches_audit_log`<br>`test_audit_payload_is_valid_json` | 真 `QueueHandler` + 真 `QueueListener` 线程 | ✅ 整条生产链路是否通、JSON 是否可解析<br>❌ 多进程下的跨进程序列化（那属于集成测试范畴） |

> 📊 **一个异步测试的完整调用链图**（可交互）：[打开 async-test-call-chain.html](diagrams/async-test-call-chain.html)

**图注**：L3 端到端测试里真实发生的事。注意第 10 步：真正写文件的是监听线程，不是测试线程——这就是为什么必须用真的 `QueueListener`，而不能只是逐个调用 handler。

```python
def _dispatch(handlers, record: logging.LogRecord) -> None:
    """复刻监听进程的派发逻辑：先比 handler level，再交给 handle()（内部跑 filter）。"""
    for handler in handlers:
        if record.levelno >= handler.level:   # ① 级别判断：生产代码里就必须显式做
            handler.handle(record)             # ② filter 在 handle() 内部执行


def test_business_logs_go_to_expected_files(handlers):
    built, tmp = handlers

    _dispatch(built, _record("app.demo", logging.INFO,  "plain-info"))
    _dispatch(built, _record("app.demo", logging.ERROR, "plain-error"))
    _dispatch(built, _record(lc.NODE_LOGGER_NAME, logging.INFO,  "node-info"))
    _dispatch(built, _record(lc.NODE_LOGGER_NAME, logging.ERROR, "node-error"))

    workflow = (tmp / "workflow.log").read_text(encoding="utf-8")
    nodes    = (tmp / "nodes.log").read_text(encoding="utf-8")
    errors   = (tmp / "error.log").read_text(encoding="utf-8")

    # workflow.log：全量，但排除节点日志
    assert "plain-info" in workflow
    assert "plain-error" in workflow
    assert "node-info" not in workflow          # ← 反向断言同样重要

    # nodes.log：只收节点日志（含 ERROR 级）
    assert "node-info" in nodes
    assert "node-error" in nodes
    assert "plain-info" not in nodes

    # error.log：只收 ERROR，且排除节点日志
    assert "plain-error" in errors
    assert "plain-info" not in errors
    assert "node-error" not in errors
```

### 三个关键手法

1. **`_dispatch` 复刻了生产派发逻辑，并在注释里写明为什么。** `Handler.handle()` *不*检查级别——级别检查是 logger 侧 `callHandlers()` 的职责。曾经的真实 bug 就是漏了这句：INFO 日志被写进了 `error.log`。测试把这个坑固化成一行可执行的文档。
2. **"不在"断言比"在"断言更容易被漏掉。** 只写 `assert "node-info" in nodes` 是不够的——万一同一条记录同时进了 `workflow.log`，你也测不出来。所以每个"应该进"的断言旁边，几乎都有一个 `assert ... not in ...`。**分流测试的本质是矩阵，不是单点。**
3. **用 `monkeypatch` 换掉 `sys.stdout`。** `StreamHandler(sys.stdout)` 在**构造时**就抓住了 stdout 对象，所以必须在 `_build_handlers()` 之前替换。看下面这段 fixture：

```python
@pytest.fixture
def handlers(monkeypatch, work_dir):
    # console handler 在构造时捕获 sys.stdout，先替换掉避免刷屏
    monkeypatch.setattr(sys, "stdout", io.StringIO())     # ① 换成一个假 stdout

    formatter = logging.Formatter(fmt=lc.LOG_FORMAT, datefmt=lc.DATE_FORMAT)
    built = lc._build_handlers(formatter, log_dir=work_dir)  # ② 日志写进临时目录
    try:
        yield built, work_dir
    finally:
        for handler in built:
            handler.flush()                                # ③ 先 flush，否则断言时文件还是空的
            handler.close()                                # ④ 再 close，释放 Windows 文件句柄
```

> 💡 **monkeypatch 是 pytest 最被低估的 fixture**：它做三件事：**自动还原**（用例结束就恢复原对象，你不需要写 teardown）、**可叠加**（多次 setattr 全部回滚）、**点路径替换**（`setattr(sys, "stdout", ...)` 精确到属性）。注意本项目**只替换 sys.stdout，没有去 mock 任何业务对象**——这也是原则：**替身给外部边界，mock 留给万不得已。**

> ⚠️ **Windows 上必须 flush + close**：本项目是 Windows 开发环境。`FileHandler` 不 flush 就拿不到内容；不 close 会留下文件句柄，后续 `rmtree` 直接 `PermissionError`。这就是 fixture 里那两行的真正原因——**跨平台测试要注意文件句柄生命周期**。

---

## 12 可读性规范：测试名就是规格书

### 半年后回来看，你只有名字和 docstring

测试代码的读者不是编译器，是**未来维护它的人**。本项目在命名上非常克制且一致，有明确的公式：

> ℹ️ **命名公式**：`test_<被测行为>_<条件>_<期望结果>`
> —— 读起来应该像一句英文陈述句，且**不要**出现 `_test` 后缀、`test1`、`happy_path` 这类无信息量的词。

| 项目里的真实测试名 | 拆解 | 为什么好 |
|---|---|---|
| `test_refresh_rotates_and_invalidates_old_token` | refresh 轮换 / 并使旧 token 失效 | 一句话说清**两个**行为，失败时不用打开文件就知道哪坏了 |
| `test_expired_refresh_token_is_rejected` | 过期的 refresh token / 被拒绝 | 条件（已过期）+ 期望（拒绝）都在名字里 |
| `test_route_after_screening_missing_flag_is_rejected` | screening 后路由 / 缺字段 / 判为淘汰 | 连**边界条件**（字段缺失）都点明了，这是最容易漏的场景 |
| `test_all_routes_respect_error_guard` | 所有路由 / 都遵守 error 守卫 | 名字里带 "all"，读者立刻知道这是**架构约束**型测试 |
| `test_revoke_with_garbage_token_is_noop` | 用垃圾 token 吊销 / 无副作用 | "noop" 精确表达了"什么都不做也不报错"这个语义 |

### 四条落地习惯（本项目全都在用）

1. **docstring 写"为什么"，不写"做什么"。** 对照两句注释：
   ❌ `"""测试吊销功能"""` —— 名字已经说了，等于没写。
   ✅ `"""基础设施异常不能被吞掉，否则外层无法标记 PROCESSING_ERROR。"""` —— 解释了**不做这件事会出什么事故**，这才是半年后最需要的信息。
2. **用空行切出 AAA 三段：Arrange（准备）/ Act（执行）/ Assert（断言）。** 看第 06 节那个测试：造 token → 解码 → 断言。三段之间一个空行，读者的眼睛会自动分段。这是**零成本**的可读性提升。
3. **文件内用注释分节。** 本项目用 `# ---------------- 签发 ----------------` 这种横幅把一个文件切成"签发 / 刷新轮换 / 过期 / 吊销"四段。当测试文件超过 100 行，这种分割比 pytest 的目录层级更有用。
4. **常量代替魔法字符串。** `REPLY_REFUSED` / `REPLY_TIME_AVAILABLE` / `EMAIL_REPLY_TIMEOUT` 都定义在**被测模块旁边**（`candidate_state.py`），测试导入它们而不是硬抄字符串。源文件里的注释解释了原因：*"两端共用同一份常量，避免改一处漏一处"*。

| 反模式 | 为什么坏 | 改法 |
|---|---|---|
| `def test_1()` | 零信息量，失败时只能打开文件读代码 | 用命名公式重写 |
| 一个测试里断言 5 件事 | 第一处失败就中断，后 4 件事永远测不到 | 拆成 5 个测试，或合并为同一件事的 5 个侧面 |
| 用 `time.sleep()` 等状态 | 慢且不稳定（flaky），CI 上最容易红 | 控制时间（见第 10 节）或注入可控时钟 |
| 测试之间共享可变全局状态 | 单独跑绿、一起跑红；顺序一变就崩 | 每个用例自己 new 一个替身（`fake_redis` 就是这么做的） |
| 断言整段日志/文案字面量 | 改一句文案就红一片，最后大家都去改测试 | 断言关键子串或结构化字段 |
| 连真实网络/数据库 | 慢、脆、污染环境 | 替身 + 分层（本项目第 03 节） |

---

## 13 命令速查表：日常只用这 10 条

### 先记住入口

本项目用 uv 管理依赖，所以推荐 `uv run pytest`；也可以直接用虚拟环境里的解释器 `.venv\Scripts\python.exe -m pytest`。`-m pytest` 比直接敲 `pytest` 更稳——它保证用的是当前环境的 pytest。

| 命令 | 作用 | 什么时候用 |
|---|---|---|
| `uv run pytest` | 跑全部 46 个用例（`-q` 已在 addopts 里） | 提交前必跑 |
| `pytest -v` | 显示每个用例名与结果 | 想看参数化展开后的具体数据组 |
| `pytest tests/test_graph_routes.py` | 只跑一个文件 | 改动了单个模块 |
| `pytest tests/test_graph_routes.py::test_route_after_scoring` | 只跑一个测试函数 | 定位一个具体行为 |
| `pytest -k "screening"` | 按名字关键字筛选 | 名字里有共同词，跨文件筛 |
| `pytest -k "7-7"` | 只跑某个参数化数据组 | 报告里看到 `[7-7-rejected]` 挂了 |
| `pytest -x` | 第一个失败就停 | 改了大改动，先看最前面的问题 |
| `pytest --lf` | 只重跑上次失败（last-failed） | 修 bug 时的迭代循环 |
| `pytest --collect-only -q` | 只收集不执行，列出全部用例名 | 想盘点测试清单 / 排查收集错误 |
| `pytest --durations=10` | 列出最慢的 10 个用例 | 测试变慢时找元凶 |
| `pytest --setup-show` | 打印 fixture 的 SETUP/TEARDOWN 顺序 | 搞不清 fixture 谁先谁后（第 04、05 节） |

```text
# ① 全量：46 个用例，3.94 秒
$ uv run pytest
46 passed in 3.94s

# ② 只跑某个文件
$ uv run pytest tests/test_logging_routing.py
9 passed in 1.21s

# ③ 只看收集到哪些用例（不动业务代码，最安全）
$ uv run pytest --collect-only -q
tests/test_auth_tokens.py: 10
tests/test_exception_wrapper.py: 5
tests/test_graph_routes.py: 22
tests/test_logging_routing.py: 9
```

> ⚠️ **注意 `--lf` 在本项目不可用**：因为 `addopts` 里有 `-p no:cacheprovider`，缓存插件被禁用了，所以 `--lf` / `--ff` 都会失效。想用就先临时去掉那个参数：`uv run pytest --lf -p cacheprovider`。这是一个"配置换干净目录"的明确取舍，知道就行。

---

## 14 动手练习：三道题，从会读到会写

> ℹ️ **怎么练**：在 `tests/` 下新建一个文件 `test_practice.py`，把下面的答案贴进去跑。**先不看答案，自己写 5 分钟**，效果差 10 倍。练习不会改动任何生产代码（除了练习 1 明确要求的那一步）。

### 练习 01 · 难度 ★★☆

**用一个布尔开关跳过评分**

需求：`state` 里有一个 `skip_scoring` 字段（人工干预场景）。当它为真时，screening 应该**直接去发邀请邮件**，跳过 AI 评分。

任务：改 `_route_after_screening`，并写两个测试——开关为真、开关为假（默认）。

<details>
<summary>查看参考答案</summary>

**① 源码改动：`app/workflow/candidate_graph.py`**

```python
def _route_after_screening(state) -> str:
    cid = state["candidate"].id
    if state.get("skip_scoring"):          # ← 新增分支：人工干预优先
        return "email_invite"
    passed = bool(state.get("screening_passed"))
    dest = "scoring" if passed else "rejected"
    logger.info(...)
    return dest
```

**② 测试：`tests/test_practice.py`**

```python
import pytest

from app.workflow.candidate_graph import _route_after_screening


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"skip_scoring": True},  "email_invite"),  # 开关打开 → 跳过评分
        ({"skip_scoring": True, "screening_passed": False},
                                          "email_invite"),  # 开关优先于筛选结果
        ({},                                 "rejected"),      # 默认无开关 → 走原逻辑
        ({"screening_passed": True},      "scoring"),
    ],
)
def test_skip_scoring_shortcut(state_factory, overrides, expected):
    assert _route_after_screening(state_factory(**overrides)) == expected
```

</details>

### 练习 02 · 难度 ★★☆

**把 4 个测试合并成一张表**

`test_graph_routes.py` 里有 4 个独立的 `test_route_check_availability_*` 函数。用 `parametrize` 把它们合并成一个"真值表"测试，并覆盖"默认轮次上限"这一组。

<details>
<summary>查看参考答案</summary>

```python
from app.workflow.candidate_graph import route_after_check_availability
from app.workflow.candidate_state import REPLY_REFUSED, REPLY_TIME_AVAILABLE


@pytest.mark.parametrize(
    ("reply", "round_no", "max_rounds", "expected"),
    [
        (REPLY_REFUSED,           0, None, "refused"),                    # 明确拒绝
        (REPLY_TIME_AVAILABLE,    0, None, "confirmed"),                  # 时间确认
        ("下周一可以吗",           1, 3,    "get_candidate_email_reply_node"), # 继续协商
        ("下周一可以吗",           3, 3,    "negotiation_expired"),            # 轮次用尽（边界：3 >= 3）
        ("再约",                  5, None, "negotiation_expired"),            # 不传上限 → 默认 3
    ],
)
def test_check_availability_truth_table(
    state_factory, reply, round_no, max_rounds, expected
):
    state = state_factory(
        candidate_email_reply=reply,
        negotiation_round=round_no,
        max_negotiation_rounds=max_rounds,
    )
    assert route_after_check_availability(state) == expected
```

</details>

### 练习 03 · 难度 ★★★

**补一个"没人测过"的断言**

读 `auth.create_tokens()`，你会发现它以 `setex(name, int(REFRESH_EXPIRE_DAYS * 86400), user_email)` 登记 jti。**过期秒数从来没被测过**。用 `fake_redis.ttl` 补上这个测试。

<details>
<summary>查看参考答案</summary>

```python
import datetime as dt

from app.core import auth
from app.core.config import get_settings


async def test_refresh_jti_ttl_matches_configured_days(fake_redis):
    """TTL 必须等于配置的天数 × 86400，不能写死。"""
    tokens = await auth.create_tokens("a@b.com", fake_redis)

    jti = auth._decode(tokens["refresh_token"], auth.REFRESH_SECRET)["jti"]

    expected = get_settings().REFRESH_EXPIRE_DAYS * 86400
    assert fake_redis.ttl[f"refresh:{jti}"] == expected
```

为什么这个测试值得写？因为 TTL 写错（比如把天当成秒、或者忘了乘 86400）**不会让任何现有测试变红**，但在生产里意味着 refresh token 提前失效、用户天天被踢下线。这类"沉默的配置错误"只有专门写测试才能抓住。

</details>

> ✅ 练习 02、03 的答案我已经实际抽成临时测试跑过：**6 passed**（5 组参数化 + 1 个 TTL 断言）。练习 01 需要改源码，未在仓库里落盘。

---

## 15 坦白局：这套测试*没*覆盖什么，以及下一步

### 诚实盘点（照实说，才教得会）

把这套测试当范本，也要清楚它的边界。以下是实话：

| 区域 | 现状 | 风险 |
|---|---|---|
| 路由层 `app/routers/` | 无测试 | 请求校验、权限依赖、状态码都靠人工点 |
| Service / Repository 层 | 无测试 | SQL 拼接、事务边界、异步 session 用法只能靠联调验证 |
| 其余大模块 | 无测试 | `PDFser.py`(17KB)、`emailbot/`(10 个文件)、`ocr_service.py` 均无覆盖，而它们恰恰最需要 |
| 覆盖率 | 未接入 `pytest-cov` | 无法量化"还有多少没测"，也不知道这次改动动了哪些未覆盖代码 |
| CI | 无 `.github/workflows` 等配置 | 测试只在本地跑，靠自觉；坏代码能直接合入 |
| 真实依赖集成 | 刻意不测 | FakeRedis 与真 Redis 的行为差异（如 TTL 到期、集群重定向）测不出来 |

### 按投入产出排序的四步

1. **接入覆盖率（半小时）。** `uv add --group dev pytest-cov`，然后 `uv run pytest --cov=app --cov-report=term-missing`。先**不要**设门槛，只用来找"完全没人碰过的大块区域"。
2. **路由层用 httpx.ASGITransport 测（收益最大）。** 不需要起服务器、不需要真数据库，只要覆盖 `get_db` 依赖即可。这一层能一次性覆盖鉴权、参数校验、错误码映射——正是当前最大的盲区。
3. **用 marker 把"慢测试"隔离成独立层。** 在 `pyproject.toml` 里加 `markers = ["integration: 需要真实依赖"]`，日常 `pytest -m "not integration"` 保持秒级，CI 上再跑全量。**这是保证"测试跑得快"这件事不被新用例破坏的机制。**
4. **接 CI，门槛设为"必须全绿"。** 先只跑 `pytest`，跑通之后再考虑覆盖率门槛。顺序很重要：**先有 CI 保证不退化，再谈覆盖率数字**，否则门槛只会让人写无意义测试来凑数。

> ❌ **不要做的事**：不要为了覆盖率数字去给简单 getter 写测试，也不要为了"有测试"而写出 `assert result is not None` 这种**永不失败**的断言。**一个什么都不断言的测试，价值是负的**——它让覆盖率好看，却让人误以为这里有防护。

---

## 16 一页纸总结

### 五条心智模型（带走这五句就够）

1. **测试是给未来的自己写的一份可执行规格。** 名字、docstring、断言，三样都要能独立读懂。
2. **快是设计出来的，不是优化出来的。** 3.94 秒来自"零外部依赖"这个先决决策，而不是后来调优。
3. **替身要窄。** 手写 FakeRedis（4 个方法）比 MagicMock（什么都能答应）更能暴露真实错误。
4. **可测性首先是源码属性。** 把分支抽成纯函数（第 09 节），测试才好写——这是重构问题，不是测试问题。
5. **分层测，别只测最薄的那层。** 过滤器真值表、真文件落盘、端到端链路，三层覆盖三种不同风险（第 11 节）。

### 零基础上手清单（照做即可）

1. 读输出：`uv run pytest -v`，先记住 `.` / `F` / `E` 分别代表什么。
2. 读配置：`pyproject.toml` 的 `[tool.pytest.ini_options]`，五个键各管一件事。
3. 读 fixture：`tests/conftest.py` 全篇 102 行，是整个测试的地基。
4. 读一个最简单的用例：`test_success_is_passed_through`，8 行。
5. 读一个最复杂的用例：`test_audit_payload_is_valid_json`，看它如何搭起端到端链路。
6. 照着第 14 节练三道题，然后自己加一个——**从"能读懂"到"能写出"的分界线，就是加的第一个测试。**

### 术语速查

| 概念 | 一句话定义 | 本项目的例子 |
|---|---|---|
| 用例 (test case) | 一个 `test_` 开头的函数（或参数化后的一组数据） | 46 个 |
| 断言 (assert) | 用普通 `assert` 表达"必须成立的事实" | `assert access["type"] == "access"` |
| 夹具 (fixture) | 按名字注入的测试依赖，可带清理逻辑 | `fake_redis` / `state_factory` / `work_dir` |
| conftest.py | 目录级共享 fixture，自动加载无需 import | `tests/conftest.py` |
| 参数化 (parametrize) | 一份测试逻辑，多组数据，各自独立计为用例 | `test_route_after_scoring` 4 组 |
| 替身 (fake) | 手写的、只实现被用到接口的假依赖 | `FakeRedis` |
| monkeypatch | 临时替换属性/环境，用例结束自动还原 | 换掉 `sys.stdout` |
| autouse | 不需要被声明就自动生效的 fixture | `_quiet_logging` |
| 冒烟测试 (smoke) | 只验证"调用不炸"的最小测试 | `test_revoke_with_garbage_token_is_noop` |
| 退化防护测试 | 不测功能，测"架构约束没被破坏" | `test_expiry_follows_settings` |

> 🎯 **最后一句**：这套测试真正的亮点不是"写了 46 个"，而是**每一个都能在 100 毫秒内给出一个明确的"是/否"**，并且失败信息足以定位问题。写测试的目标从来不是数字，而是**让下一次改动不必靠运气**。

---

*本教程基于 `simplejob` 仓库 `tests/` 下的 4 个测试文件与 `conftest.py` 撰写 · 所有代码片段与输出均取自项目实测（pytest 9.2.0 · Python 3.13.9 · 46 passed in 3.94s）*

*配套交互式图表位于 `diagrams/` 目录，点击对应链接可在浏览器中查看（支持点选节点、主题切换）。*
