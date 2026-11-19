# 监控 / 告警 / 健康检查 —— 零基础教程

> 适用项目：**simplejob**（FastAPI + gunicorn 单 worker + LangGraph + PostgreSQL + Redis + 邮件机器人 + OCR/AI 外部调用）
>
> 面向：**没有运维背景**的开发者。全部示例都来自仓库里的真实代码与真实配置，不是玩具。
>
> 配套落地文档：[monitoring-alerting-healthcheck.md](../monitoring-alerting-healthcheck.md)

---

## 阅读顺序

| # | 文档 | 讲什么 | 建议 |
|---|---|---|---|
| 00 | [监控告警健康检查-总览](./00-监控告警健康检查-总览.md) | 全景：监控/指标/日志/链路/告警/健康检查的关系，三级探针，落地路线 | **先读这篇** |
| 01 | [Prometheus-指标监控](./01-Prometheus-指标监控.md) | 数据模型、四种指标类型、PromQL、抓取配置、告警规则、TSDB、排障 | 核心 |
| 02 | [Loki与Promtail-日志](./02-Loki与Promtail-日志.md) | 标签设计、pipeline stages、LogQL、日志告警、保留策略 | 核心 |
| 03 | [OpenTelemetry与Tempo-链路追踪](./03-OpenTelemetry与Tempo-链路追踪.md) | OTel 埋点、上下文传播、Collector、Tempo、TraceQL、采样 | 进阶 |
| 04 | [Alertmanager-告警触达](./04-Alertmanager-告警触达.md) | 路由、分组、抑制、静默、通知模板、分级与值班 | 核心 |
| 05 | [Grafana-可视化看板](./05-Grafana-可视化看板.md) | 数据源、面板、变量、四类看板设计、Explore 排障 | 核心 |
| 06 | [Cron与Pushgateway-定时任务](./06-Cron与Pushgateway-定时任务.md) | crontab、合成监控、Pushgateway 推送语义、死开关 | 进阶 |

**只想快速上手**：读 `00` → `01` → `04` → `05`。
**想做完整监控体系**：按 00 → 01 → 02 → 03 → 04 → 05 → 06 顺序。

---

## 全栈架构一图

```text
应用 / 依赖
   │
   ├── /metrics ──────────────► Prometheus ──► Alertmanager ──► 钉钉 / 电话 / 邮件
   ├── 日志文件 ──► Promtail ──► Loki ──────────────┐
   ├── OTLP ──────► Collector ──► Tempo ───────────┤
   └── Cron 合成拨测 ──► Pushgateway ──► Prometheus ┤
                                                   ▼
                                              Grafana 看板
```

---

## 每篇的固定结构

为方便检索，七篇都遵循同一套骨架：

1. 「它是什么」+「**它不做什么**」（避免误解）
2. 核心概念 + 图解
3. 五分钟跑起来
4. 逐段配置讲解
5. 实战模板
6. 常见坑 / 排障
7. **一页纸速查** + **五个易错点**

---

## 相关项目文档

- [monitoring-alerting-healthcheck.md](../monitoring-alerting-healthcheck.md) —— 落地方案（完整 `prometheus.yml` / `alerts.yml` / `promtail-config.yml` / 看板 JSON）
- [ops_plan.md](../ops_plan.md) —— 运维方案总纲
- [deployment.md](../deployment.md) —— 部署架构与端口
- [logging_system.md](../logging_system.md) —— simplejob 日志系统架构（队列 + 监听进程）
- [periodic_task_refactor.md](../periodic_task_refactor.md) —— 周期任务框架（心跳阈值推导的依据）
