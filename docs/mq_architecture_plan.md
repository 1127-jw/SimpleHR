# 消息队列（MQ）架构设计方案

## 一、现状分析

### 1.1 当前后台任务

| 任务 | 触发方式 | 入口文件 | 执行方式 |
|------|---------|---------|---------|
| 候选人 Agent 流程 | `POST /candidate/create` | `candidate_router.py` | BackgroundTasks |
| OCR 简历解析 | `POST /candidate/resume/parse` | `candidate_ser.py` | BackgroundTasks |
| 邀请邮件发送 | `POST /user/invite` | `user_router.py` | BackgroundTasks |
| 人工重试 | `PATCH /candidate/{id}/status` | `candidate_router.py` | BackgroundTasks |
| 邮件机器人轮询 | APScheduler 定时任务 | `mail_manager.py` | 定时轮询 |

### 1.2 当前问题

```
┌─────────────────────────────────────────────────────────────┐
│  问题 1：BackgroundTasks 不可靠                              │
│  - 进程重启 → 任务丢失                                       │
│  - 无重试机制 → 失败即丢失                                    │
│  - 无法分布式部署 → 多实例时任务分散                           │
│  - 阻塞响应 → 大任务影响接口性能                               │
├─────────────────────────────────────────────────────────────┤
│  问题 2：APScheduler 轮询效率低                               │
│  - 每 20 秒轮询一次 → 延迟高                                  │
│  - 无法水平扩展 → 多实例重复轮询                               │
│  - 进程崩溃 → 轮询中断                                        │
├─────────────────────────────────────────────────────────────┤
│  问题 3：任务状态不透明                                       │
│  - 无法追踪任务执行进度                                       │
│  - 无法统计任务成功率                                         │
│  - 失败任务难以重试                                           │
└─────────────────────────────────────────────────────────────┘
```

---

## 二、MQ 选型对比

### 2.1 RabbitMQ vs Redis

| 维度 | RabbitMQ | Redis (List/Stream) |
|------|----------|---------------------|
| **消息持久化** | ✅ 原生支持 | ⚠️ 需配置 AOF |
| **消息确认** | ✅ ACK 机制 | ⚠️ 需手动实现 |
| **死信队列** | ✅ 原生支持 | ❌ 需自行实现 |
| **重试机制** | ✅ 原生支持 | ❌ 需自行实现 |
| **延迟队列** | ✅ 插件支持 | ⚠️ Stream 支持 |
| **优先级队列** | ✅ 原生支持 | ⚠️ Stream 支持 |
| **消息顺序** | ✅ 单队列有序 | ✅ 单队列有序 |
| **吞吐量** | 万级/秒 | 十万级/秒 |
| **运维复杂度** | 中（需部署） | 低（已有 Redis） |
| **可靠性** | 高（金融级） | 中（缓存级） |
| **适用场景** | 任务队列、事件驱动 | 轻量级队列、实时性要求高 |

### 2.2 选型建议

| 场景 | 推荐 | 理由 |
|------|------|------|
| 当前项目（中小规模） | **Redis Stream** | 已有 Redis，运维成本低 |
| 未来扩展（大规模） | **RabbitMQ** | 可靠性、死信队列、重试机制完善 |
| 任务可靠性要求高 | **RabbitMQ** | ACK + 死信队列 + 持久化 |
| 快速上线、轻量级 | **Redis Stream** | 无需额外部署 |

---

## 三、Redis Stream 方案设计

### 3.1 架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                           应用进程                                   │
│                                                                     │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐│
│  │ 候选人接口   │  │ OCR 接口    │  │ 邮件接口    │  │ 状态接口    ││
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘│
│         │                │                │                │       │
│         └────────────────┴────────────────┴────────────────┘       │
│                                   │                                 │
│                                   ▼                                 │
│                    ┌─────────────────────────────┐                 │
│                    │        MQ Producer           │                 │
│                    │  (发布任务到 Redis Stream)    │                 │
│                    └─────────────────────────────┘                 │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         Redis Stream                                │
│                                                                     │
│  Stream: candidate_agent                                            │
│  Stream: ocr_parse                                                  │
│  Stream: email_send                                                 │
│  Stream: mail_poll                                                  │
│                                                                     │
│  Consumer Group: workers                                            │
│    ├── Consumer-1                                                   │
│    ├── Consumer-2                                                   │
│    └── Consumer-3                                                   │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          消费者进程                                  │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    Worker 服务                               │   │
│  │                                                              │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │   │
│  │  │ Agent 消费者 │  │ OCR 消费者   │  │ 邮件消费者   │         │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘         │   │
│  │                                                              │   │
│  │  特性：                                                       │   │
│  │  - 消息确认（ACK）                                            │   │
│  │  - 失败重试（最多 3 次）                                       │   │
│  │  - 死信队列（超过重试次数）                                    │   │
│  │  - 心跳检测（消费者宕机自动转移）                               │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 消息格式

```json
{
  "task_id": "uuid-xxx",
  "task_type": "candidate_agent | ocr_parse | email_send | mail_poll",
  "payload": {
    "candidate_id": "abc123",
    "resume_id": "def456",
    "email": "user@example.com"
  },
  "retry_count": 0,
  "max_retry": 3,
  "created_at": "2026-11-08T17:30:00"
}
```

### 3.3 任务类型

| Stream | 任务 | 消费者 | 说明 |
|--------|------|--------|------|
| `candidate_agent` | 候选人 Agent 流程 | AgentWorker | 创建候选人后触发 |
| `ocr_parse` | OCR 简历解析 | OCRWorker | 上传简历后触发 |
| `email_send` | 邮件发送 | EmailWorker | 邀请用户时触发 |
| `mail_poll` | 邮件机器人轮询 | MailWorker | 定时轮询（替代 APScheduler） |

---

## 四、RabbitMQ 方案设计

### 4.1 架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                           应用进程                                   │
│                                                                     │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐│
│  │ 候选人接口   │  │ OCR 接口    │  │ 邮件接口    │  │ 状态接口    ││
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘│
│         │                │                │                │       │
│         └────────────────┴────────────────┴────────────────┘       │
│                                   │                                 │
│                                   ▼                                 │
│                    ┌─────────────────────────────┐                 │
│                    │        MQ Producer           │                 │
│                    │     (发布任务到 RabbitMQ)     │                 │
│                    └─────────────────────────────┘                 │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                           RabbitMQ                                   │
│                                                                     │
│  Exchange: task.direct                                              │
│    ├── Queue: candidate_agent      → Routing Key: agent            │
│    ├── Queue: ocr_parse            → Routing Key: ocr              │
│    ├── Queue: email_send           → Routing Key: email            │
│    └── Queue: mail_poll            → Routing Key: poll             │
│                                                                     │
│  Dead Letter Exchange: task.dlx                                     │
│    └── Queue: task.dlq             → 死信队列                       │
│                                                                     │
│  队列配置：                                                          │
│    - durable: true（持久化）                                        │
│    - TTL: 1 小时（消息过期）                                         │
│    - max-length: 100000（最大长度）                                  │
│    - dead-letter-exchange: task.dlx（死信交换机）                    │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          消费者进程                                  │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    Worker 服务                               │   │
│  │                                                              │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │   │
│  │  │ Agent 消费者 │  │ OCR 消费者   │  │ 邮件消费者   │         │   │
│  │  │ prefetch=10 │  │ prefetch=10 │  │ prefetch=10 │         │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘         │   │
│  │                                                              │   │
│  │  特性：                                                       │   │
│  │  - 消息确认（ACK/NACK）                                       │   │
│  │  - 失败重试（最多 3 次）                                       │   │
│  │  - 死信队列（超过重试次数）                                    │   │
│  │  - 心跳检测（消费者宕机自动转移）                               │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.2 消息格式

```json
{
  "task_id": "uuid-xxx",
  "task_type": "candidate_agent",
  "payload": {
    "candidate_id": "abc123",
    "resume_id": "def456"
  },
  "retry_count": 0,
  "max_retry": 3
}
```

---

## 五、任务剥离方案

### 5.1 任务映射

| 当前任务 | MQ 任务类型 | 消费者 | 触发方式 |
|---------|------------|--------|---------|
| `run_candidate_agent` | `candidate_agent` | AgentWorker | 创建候选人后 |
| `ocr_parse_resume_task` | `ocr_parse` | OCRWorker | 上传简历后 |
| `send_invite_email_task` | `email_send` | EmailWorker | 邀请用户时 |
| `trigger_manual_retry` | `candidate_agent` | AgentWorker | 人工重试时 |
| `MailManager._poll_once` | `mail_poll` | MailWorker | 定时任务（改为 MQ 延迟消息） |

### 5.2 改造点

#### 5.2.1 应用端（Producer）

```python
# app/core/mq_producer.py
import json
import uuid
from datetime import datetime
from redis.asyncio import Redis

class MQProducer:
    """MQ 生产者"""

    STREAMS = {
        "agent": "candidate_agent",
        "ocr": "ocr_parse",
        "email": "email_send",
        "poll": "mail_poll",
    }

    def __init__(self, redis: Redis):
        self.redis = redis

    async def publish(self, task_type: str, payload: dict) -> str:
        """发布任务"""
        task_id = str(uuid.uuid4())
        message = {
            "task_id": task_id,
            "task_type": task_type,
            "payload": payload,
            "retry_count": 0,
            "max_retry": 3,
            "created_at": datetime.now().isoformat(),
        }

        stream = self.STREAMS.get(task_type)
        await self.redis.xadd(stream, {"data": json.dumps(message)})
        return task_id
```

#### 5.2.2 消费者端（Worker）

```python
# app/core/mq_worker.py
import json
import asyncio
from redis.asyncio import Redis

class MQWorker:
    """MQ 消费者"""

    def __init__(self, redis: Redis, consumer_name: str):
        self.redis = redis
        self.consumer_name = consumer_name
        self.handlers = {}

    def register(self, task_type: str, handler):
        """注册任务处理器"""
        self.handlers[task_type] = handler

    async def start(self):
        """启动消费者"""
        # 创建消费者组
        for stream in self.handlers.keys():
            try:
                await self.redis.xgroup_create(
                    stream, "workers", id="0", mkstream=True
                )
            except Exception:
                pass  # 已存在

        # 消费循环
        while True:
            for stream, handler in self.handlers.items():
                messages = await self.redis.xreadgroup(
                    "workers",
                    self.consumer_name,
                    {stream: ">"},
                    count=10,
                    block=5000,
                )
                for msg in messages:
                    await self._process_message(msg, handler)

    async def _process_message(self, msg, handler):
        """处理消息"""
        try:
            data = json.loads(msg["data"])
            await handler(data["payload"])
            # 确认消息
            await self.redis.xack(msg["stream"], "workers", msg["id"])
        except Exception as e:
            # 重试逻辑
            retry_count = data.get("retry_count", 0)
            if retry_count < data.get("max_retry", 3):
                # 重新入队
                await self._retry_message(data, msg["stream"])
            else:
                # 进入死信队列
                await self._dead_letter(data, msg["stream"], str(e))
            # 确认原消息
            await self.redis.xack(msg["stream"], "workers", msg["id"])
```

---

## 六、可行性评估

### 6.1 Redis Stream 方案

| 维度 | 评估 | 说明 |
|------|------|------|
| **技术可行性** | ✅ 高 | Redis 5.0+ 原生支持，无需额外部署 |
| **改造成本** | ✅ 低 | 已有 Redis，只需添加 Producer/Worker |
| **可靠性** | ⚠️ 中 | 需自行实现 ACK、重试、死信队列 |
| **性能** | ✅ 高 | 十万级/秒 |
| **运维成本** | ✅ 低 | 无额外组件 |

### 6.2 RabbitMQ 方案

| 维度 | 评估 | 说明 |
|------|------|------|
| **技术可行性** | ✅ 高 | 成熟方案，生态完善 |
| **改造成本** | ⚠️ 中 | 需部署 RabbitMQ，添加 Producer/Worker |
| **可靠性** | ✅ 高 | ACK + 死信队列 + 持久化 |
| **性能** | ✅ 高 | 万级/秒 |
| **运维成本** | ⚠️ 中 | 需维护 RabbitMQ 集群 |

---

## 七、推荐方案

### 7.1 当前阶段（推荐 Redis Stream）

```
理由：
1. 已有 Redis，无需额外部署
2. 中小规模，Redis Stream 足够
3. 快速上线，改造成本低
4. 后续可平滑迁移到 RabbitMQ
```

### 7.2 未来扩展（迁移到 RabbitMQ）

```
触发条件：
1. 日均任务量 > 10 万
2. 需要更可靠的消息保证
3. 需要更复杂的路由规则
4. 需要死信队列、延迟队列等高级特性
```

---

## 八、实施步骤

### 阶段 1：基础设施（1-2 天）

1. 创建 `app/core/mq_producer.py`
2. 创建 `app/core/mq_worker.py`
3. 编写单元测试

### 阶段 2：任务迁移（2-3 天）

1. 候选人 Agent 任务迁移
2. OCR 解析任务迁移
3. 邮件发送任务迁移
4. 人工重试任务迁移

### 阶段 3：邮件机器人改造（1-2 天）

1. 移除 APScheduler
2. 使用 MQ 延迟消息替代定时轮询
3. 启动 MailWorker 消费者

### 阶段 4：监控和优化（持续）

1. 添加任务监控
2. 压力测试
3. 参数调优

---

## 九、风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| Redis Stream 数据丢失 | 中 | 配置 AOF 持久化 |
| 消费者处理失败 | 中 | 重试 + 死信队列 |
| 任务积压 | 中 | 增加消费者数量 |
| 消息顺序问题 | 低 | 单 Stream 单队列保证顺序 |
| 消费者宕机 | 低 | Consumer Group 自动转移 |

---

## 十、总结

| 方案 | 适用场景 | 改造成本 | 可靠性 | 推荐度 |
|------|---------|---------|--------|--------|
| **Redis Stream** | 中小规模、快速上线 | 低 | 中 | ⭐⭐⭐⭐⭐ |
| **RabbitMQ** | 大规模、高可靠性 | 中 | 高 | ⭐⭐⭐⭐ |

**当前推荐：Redis Stream**

- 无需额外部署
- 与现有 Redis 集成
- 支持平滑迁移到 RabbitMQ
