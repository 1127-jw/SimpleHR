# Grafana 零基础入门教程

> 适用项目：**simplejob**（数据源：Prometheus 指标 / Loki 日志 / Tempo 链路，版本 Grafana 11.2）
>
> 配套文档：[总览篇](./00-监控告警健康检查-总览.md)（总入口）、[指标篇](./01-Prometheus-指标监控.md)、[日志篇](./02-Loki与Promtail-日志.md)、[链路篇](./03-OpenTelemetry与Tempo-链路追踪.md)、[告警触达篇](./04-Alertmanager-告警触达.md)
>
> 前置知识：能看懂 JSON，知道「图表」是什么。**不需要**运维背景。
>
> 阅读方式：第 0～4 章建立概念并跑起来，第 5～7 章是面板与仪表板基础，**第 8～11 章是本项目四类看板的实战**，第 12～14 章是进阶，第 15 章往后是排障与速查。只想要结论直接跳 [17. 一页纸速查](#17-一页纸速查)。

---

## 目录

- [0. Grafana 是什么](#0-grafana-是什么)
- [1. 七个核心概念](#1-七个核心概念)
- [2. 架构与数据流](#2-架构与数据流)
- [3. 五分钟跑起来](#3-五分钟跑起来)
- [4. 数据源配置](#4-数据源配置)
- [5. 面板基础](#5-面板基础)
- [6. 仪表板基础](#6-仪表板基础)
- [7. 变量 Templates](#7-变量-templates)
- [8. 四类看板设计](#8-四类看板设计)
- [9. Prometheus 面板](#9-prometheus-面板)
- [10. Loki 面板](#10-loki-面板)
- [11. Tempo 面板](#11-tempo-面板)
- [12. 注释与告警面板](#12-注释与告警面板)
- [13. Grafana 自带告警](#13-grafana-自带告警)
- [14. 权限与 API](#14-权限与-api)
- [15. 排障](#15-排障)
- [16. 最佳实践清单](#16-最佳实践清单)
- [17. 一页纸速查](#17-一页纸速查)

---

## 0. Grafana 是什么

一句话：

> **Grafana 是「把各种数据源的数字和日志，画成能一眼看懂的图」的界面。**

它本身**不存数据**（除了自己的配置），只是数据的「展示层」。

```text
Prometheus ──┐
Loki ────────┼──► Grafana ──► 人
Tempo ───────┤
PostgreSQL ──┤
MySQL ───────┘
```

### 0.1 它**不**做什么

| 常见误解 | 实际 |
|---|---|
| 「Grafana 能存数据」 | ❌ 数据都在 Prometheus / Loki / Tempo 里 |
| 「Grafana 能替代 Prometheus」 | ❌ 它只查询和展示 |
| 「Grafana 能告警」 | ⚠️ 有自带告警，但本项目走 Alertmanager（见 [第 13 章](#13-grafana-自带告警)） |
| 「面板越多越好」 | ❌ 看板是给人快速判断用的，不是数据仓库 |
| 「Grafana 只是个画图工具」 | ⚠️ 它还有 Explore 排障、告警、权限、API |

### 0.2 为什么选 Grafana

| 优点 | 说明 |
|---|---|
| **数据源生态庞大** | Prometheus / Loki / Tempo / PG / MySQL / ES / CloudWatch… |
| **排障闭环** | Explore 里能在指标、日志、链路之间跳转 |
| **一套界面** | 不用为每个系统学一个 UI |
| **社区看板多** | Grafana 官网有上千个现成 dashboard 可导入 |

> **一句话**：Grafana 是监控体系的「**最后一公里**」——数据采得再好，看不清就等于没有。

---

## 1. 七个核心概念

| 概念 | 一句话 | 例子 |
|---|---|---|
| **数据源 Data Source** | 连到哪个后端 | `Prometheus`（UID: prometheus） |
| **面板 Panel** | 一个可视化单元 | QPS 曲线、日志列表 |
| **仪表板 Dashboard** | 面板的集合 | 「SimpleJob 总览」 |
| **变量 Variable** | 下拉选择器 | 选环境、选路由 |
| **文件夹 Folder** | 仪表板的分组 | `SimpleJob` 文件夹 |
| **注释 Annotation** | 时间轴上的标记 | 「发布 v0.2.0」 |
| **告警 Alert** | 面板上的阈值规则 | 5xx > 1% |

### 1.1 层级关系

```text
Grafana 实例
├── 文件夹 SimpleJob
│   ├── 仪表板「总览」
│   │   ├── 面板 QPS
│   │   ├── 面板 5xx 比例
│   │   └── 面板 日志
│   └── 仪表板「依赖」
└── 文件夹 基础设施
    └── 仪表板「PostgreSQL」
```

### 1.2 数据源 UID 很重要

在 provisioning 里必须显式指定 `uid`，因为**面板 JSON 靠 UID 引用数据源**：

```yaml
datasources:
  - name: Prometheus
    uid: prometheus          # ← 面板里写 {"uid": "prometheus"}
    type: prometheus
```

> **常见坑**：换 Grafana 实例后，如果 provisioning 里的 `uid` 变了，所有面板会报「数据源不存在」。

---

## 2. 架构与数据流

```mermaid
flowchart LR
    subgraph DS["数据源（真正存数据）"]
        P[("Prometheus")]
        L[("Loki")]
        T[("Tempo")]
        PG[("PostgreSQL")]
    end

    subgraph GRAF["Grafana"]
        FE["前端<br/>React"]
        BE["后端<br/>Go"]
        META[("元数据库<br/>SQLite / MySQL / PG")]
        CACHE["查询缓存"]
    end

    subgraph USER["用户"]
        BROWSER["浏览器"]
        EXPLORE["Explore 排障"]
        ALERT["Alerting"]
    end

    BROWSER --> FE
    FE --> BE
    BE --> META
    BE --> CACHE
    BE -->|代理查询| P
    BE --> L
    BE --> T
    BE --> PG
    EXPLORE --> BE
    ALERT --> BE
```

### 2.1 关键设计 access: proxy

Grafana 查数据有**两种模式**：

| 模式 | 谁去查 | 说明 |
|---|---|---|
| `proxy`（推荐） | **Grafana 后端** | 浏览器不直连数据源，跨域/认证都由 Grafana 处理 |
| `direct` | 浏览器 | 需要数据源允许跨域，生产不推荐 |

### 2.2 元数据库

Grafana 自己也要存东西：仪表板、数据源配置、用户、权限。

| 选项 | 适用 |
|---|---|
| **SQLite**（默认） | 单机、小规模 |
| **MySQL / PostgreSQL** | 多副本 Grafana 共享配置 |

> **建议**：把仪表板 **JSON 存进 Git**（见 [14.2](#142-仪表板即代码)），元数据库只当缓存。

---

## 3. 五分钟跑起来

```yaml
services:
  grafana:
    image: grafana/grafana:11.2.0
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?must set}
      GF_USERS_ALLOW_SIGN_UP: "false"
      GF_SERVER_ROOT_URL: https://grafana.example.com
    volumes:
      - ./deploy/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./deploy/grafana/dashboards:/var/lib/grafana/dashboards:ro
      - grafana_data:/var/lib/grafana
    ports: ["3000:3000"]
    depends_on: [prometheus, loki, tempo]

volumes:
  grafana_data:
```

打开 `http://localhost:3000`，用 `admin` + 你设的密码登录。

**第一个检查**：左下角 ⚙️ → Data sources，应该能看到 Prometheus / Loki / Tempo 三个，并且每个都能 **Save & test** 通过。

---

## 4. 数据源配置

### 4.1 用 provisioning 管理（推荐）

目录约定：`/etc/grafana/provisioning/<类型>/*.yml`

```yaml
# deploy/grafana/provisioning/datasources/prometheus.yml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    uid: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    jsonData:
      httpMethod: POST
      timeInterval: 15s        # 与 Prometheus 的 scrape_interval 对齐

  - name: Loki
    type: loki
    uid: loki
    access: proxy
    url: http://loki:3100
    jsonData:
      maxLines: 1000
      derivedFields:           # 日志里提取字段做跳转（见第 11 章）
        - name: TraceID
          matcherRegex: 'trace=([0-9a-f]+)'
          datasourceUid: tempo
          url: '$${__value.raw}'

  - name: Tempo
    type: tempo
    uid: tempo
    access: proxy
    url: http://tempo:3200
    jsonData:
      tracesToLogsV2:
        datasourceUid: loki
        filterByTraceID: true
        filterBySpanID: false
      nodeGraph:
        enabled: true
```

### 4.2 关键字段

| 字段 | 说明 |
|---|---|
| `name` | 显示名 |
| `uid` | **唯一标识，面板靠它引用** |
| `type` | `prometheus` / `loki` / `tempo` / `mysql`… |
| `access` | `proxy`（推荐）/ `direct` |
| `url` | 后端地址（容器内用服务名） |
| `isDefault` | 默认数据源 |
| `jsonData` | 各数据源专属配置 |

### 4.3 验证

```bash
# 列出数据源
curl -s http://admin:password@localhost:3000/api/datasources | python -m json.tool

# 测试 Prometheus 数据源是否可用
curl -s -XPOST http://admin:password@localhost:3000/api/datasources/uid/prometheus/resources/api/v1/query \
  -H 'Content-Type: application/json' -d '{"query":"up"}' | python -m json.tool
```

> **排障第一招**：面板没数据时，先确认数据源 **Save & test** 是绿的。数据源不通，面板一定空白。

---

## 5. 面板基础

### 5.1 可视化类型怎么选

| 类型 | 适合 | 例子 |
|---|---|---|
| **Time series** | 随时间变化的曲线 | QPS、P95 延迟 |
| **Stat** | 单个数字 + 阈值变色 | 5xx 比例 |
| **Gauge / Bar gauge** | 当前值 + 范围 | 磁盘使用率 |
| **Table** | 明细列表 | 慢接口 Top 10 |
| **Pie chart** | 占比分布 | 候选人状态分布 |
| **Heatmap** | 分布密度 | 延迟直方图 |
| **Logs** | 日志列表 | ERROR 日志 |
| **Traces** | 单条链路 | trace 详情 |
| **Node graph** | 服务拓扑 | 调用关系 |
| **Text** | 静态说明 | 看板简介 |

### 5.2 单位 unit 别选错

| 单位 | 显示 |
|---|---|
| `short` | 1.2K、3.4M |
| `percent` | 12（表示 12%） |
| `percentunit` | 0.12（表示 12%） |
| `bytes` | 1.2 KiB |
| `s` / `ms` | 1.2 s |
| `reqps` | 请求/秒 |
| `locale` | 千分位 |

> **高频错误**：阈值写 `0.01` 但单位选了 `percent`（那表示 0.01%，比你想的严格 100 倍）。**比例类用 `percentunit`**。

### 5.3 阈值 thresholds

让面板「变色」的关键：

```json
"fieldConfig": {
  "defaults": {
    "unit": "percentunit",
    "thresholds": {
      "mode": "absolute",
      "steps": [
        { "color": "green", "value": null },
        { "color": "orange", "value": 0.01 },
        { "color": "red", "value": 0.05 }
      ]
    }
  }
}
```

读法：`< 1%` 绿，`1%~5%` 橙，`> 5%` 红。

### 5.4 图例 legend

曲线多了要能区分：

```json
"legend": { "displayMode": "list", "placement": "right" },
"targets": [
  { "expr": "...", "legendFormat": "{{route}}" }
]
```

`legendFormat` 里用 `{{标签名}}` 取标签值。

> **注意**：`legendFormat` 里的标签必须**真的存在**，否则会显示成空。

### 5.5 一个完整面板 JSON 的样子

```json
{
  "id": 2,
  "type": "stat",
  "title": "5xx 比例",
  "gridPos": { "h": 6, "w": 6, "x": 6, "y": 0 },
  "datasource": { "type": "prometheus", "uid": "prometheus" },
  "targets": [
    {
      "expr": "sum(rate(http_requests_total{status=~\"5..\"}[5m])) / sum(rate(http_requests_total[5m]))",
      "legendFormat": "error ratio"
    }
  ],
  "fieldConfig": {
    "defaults": {
      "unit": "percentunit",
      "thresholds": {
        "mode": "absolute",
        "steps": [
          { "color": "green", "value": null },
          { "color": "orange", "value": 0.01 },
          { "color": "red", "value": 0.05 }
        ]
      }
    },
    "overrides": []
  },
  "options": {
    "colorMode": "background",
    "graphMode": "none",
    "textMode": "value"
  }
}
```

逐块看：

| 字段 | 作用 |
|---|---|
| `type` | 可视化类型 |
| `gridPos` | 位置和尺寸（24 列网格，`x/y` 是左上角，`w/h` 是宽高） |
| `datasource` | 用哪个数据源（靠 UID） |
| `targets` | 查询列表（可多个） |
| `fieldConfig` | 单位、阈值、自定义显示 |
| `options` | 该可视化类型的专属选项 |

---

## 6. 仪表板基础

### 6.1 布局与时间

| 设置 | 说明 |
|---|---|
| `time.from` / `time.to` | 默认时间范围，如 `now-6h` / `now` |
| `refresh` | 自动刷新间隔，如 `30s` |
| `timezone` | `browser`（跟随浏览器）或 `utc` |
| `schemaVersion` | 面板 schema 版本（11.x 一般是 39） |

```json
{
  "uid": "simplejob-overview",
  "title": "SimpleJob 总览",
  "tags": ["simplejob"],
  "timezone": "browser",
  "schemaVersion": 39,
  "version": 1,
  "time": { "from": "now-6h", "to": "now" },
  "refresh": "30s",
  "panels": []
}
```

### 6.2 面板重复 Repeat

一条查询对多个变量值重复渲染（如对每个 route 画一条）：

```json
"repeat": "route",
"repeatDirection": "h"
```

### 6.3 分享与导出

| 方式 | 说明 |
|---|---|
| **Share → Link** | 带时间范围的链接（排障时发给同事最常用） |
| **Share → Snapshot** | 把当前数据固化成一个只读快照 |
| **Export → Save as JSON** | 导出 JSON 存进 Git |
| **Export → PDF / PNG** | 报表 |

> **实战建议**：排障时**发链接**（对方能看到实时数据、能自己调时间），比发截图有用得多。

### 6.4 仪表板即代码

把 JSON 存 Git，好处：

- 有变更历史、能 code review
- 换 Grafana 实例能直接导入
- 不会「某个人在界面上点了几下就再也回不去了」

配合 provisioning 自动加载：

```yaml
# deploy/grafana/provisioning/dashboards/default.yml
apiVersion: 1

providers:
  - name: simplejob
    folder: SimpleJob
    type: file
    allowUiUpdates: true
    options:
      path: /var/lib/grafana/dashboards
```

> ⚠️ `allowUiUpdates: true` 允许在 UI 上改，但**文件不会自动回写**。所以规范是：**在 UI 上调试，满意后导出 JSON 提 PR**。

---

## 7. 变量 Templates

变量让看板「可复用」——同一块板，选不同环境/路由看不同数据。

```json
"templating": {
  "list": [
    {
      "name": "env",
      "label": "环境",
      "type": "custom",
      "query": "prod,staging,dev",
      "current": { "text": "prod", "value": "prod" }
    },
    {
      "name": "route",
      "label": "路由",
      "type": "query",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "query": "label_values(http_requests_total, route)",
      "refresh": 1,
      "includeAll": true,
      "multi": true,
      "allValue": ".*"
    }
  ]
}
```

### 7.1 常用变量类型

| 类型 | 用途 |
|---|---|
| `custom` | 手写固定选项（环境、机房） |
| `query` | 从数据源查（`label_values(...)`） |
| `interval` | 时间粒度（`1m,5m,1h`） |
| `datasource` | 切换数据源 |
| `textbox` | 自由输入 |
| `constant` | 隐藏常量 |

### 7.2 在查询里用变量

```promql
sum(rate(http_requests_total{env="$env", route=~"$route"}[5m]))
```

| 写法 | 含义 |
|---|---|
| `$env` | 单值 |
| `$route` | 多选时自动展开成 `(a|b|c)` |
| `${var}` | 明确边界（变量名含特殊字符时） |
| `[[var]]` | 旧语法（不推荐） |
| `$__rate_interval` | Grafana 内置变量，自动匹配步长 |

> **重要**：**正则匹配时不要手动加引号**。`route=~"$route"` 而不是 `route=~'$route'`——Grafana 会自动处理转义。

### 7.3 内置变量

| 变量 | 含义 |
|---|---|
| `$__rate_interval` | 适合 `rate()` 的步长（≥ 4 × 采集间隔） |
| `$__interval` | 图表步长 |
| `$__range` | 时间范围 |
| `$__from` / `$__to` | 起止时间戳 |
| `$__dashboard` | 仪表板名 |

> **`$__rate_interval` 是解决「rate 窗口太小导致断点」的标准做法**，比手写 `[5m]` 更稳。

---

## 8. 四类看板设计

本项目按「**问题域**」组织看板，而不是按数据源。

```mermaid
flowchart TD
    D1["① 总览<br/>SimpleJob 总览"]
    D2["② 依赖<br/>PG / Redis / OCR / 邮件"]
    D3["③ 后台任务<br/>心跳 / 队列 / 失败"]
    D4["④ 日志<br/>ERROR / 审计"]

    D1 -->|"发现异常"| D2
    D2 -->|"定位依赖"| D4
    D1 -->|"需要下钻"| D4
    D4 -->|"拿 trace_id"| D5["Tempo 链路"]

    style D1 fill:#1e3a5f,stroke:#38bdf8,color:#e2e4ed
    style D5 fill:#3b2f5b,stroke:#a78bfa,color:#e2e4ed
```

### 8.1 看板一 总览（最重要）

**目标**：10 秒内回答「现在服务正常吗」。

面板清单：

| 面板 | 类型 | 查询 |
|---|---|---|
| QPS | stat | `sum(rate(http_requests_total[5m]))` |
| 5xx 比例 | stat（带阈值） | `sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))` |
| P95 延迟（按路由） | time series | `histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, route))` |
| QPS（按状态） | time series | `sum by (status) (rate(http_requests_total[5m]))` |
| 候选人状态分布 | pie chart | `simplejob_candidate_status_count` |
| 后台任务存活 | stat（0 红 1 绿） | `(time() - simplejob_bg_task_last_run_seconds) > simplejob_bg_task_expected_interval_seconds * 3` |

> **设计原则**：总览看板**只放 RED**（Rate / Errors / Duration）+ 关键业务量。别的都放子看板。

### 8.2 看板二 依赖

**目标**：出故障时，快速判断「是我们挂了还是依赖挂了」。

面板：

| 面板 | 查询 |
|---|---|
| PostgreSQL 可用性 | `pg_up` |
| PG 连接数 | `pg_stat_activity_count` |
| Redis 可用性 | `redis_up` |
| Redis 连接数 | `redis_connected_clients` |
| OCR/AI 耗时 | `rate(simplejob_external_request_duration_seconds_sum[5m]) / rate(..._count[5m])` |
| OCR/AI 失败率 | 见 [指标篇 §9.5](./01-Prometheus-指标监控.md#95-外部依赖失败率) |

### 8.3 看板三 后台任务

**目标**：发现「进程活着但任务死了」的静默故障。

面板：

| 面板 | 查询 |
|---|---|
| 后台任务存活 | `(time() - simplejob_bg_task_last_run_seconds) > simplejob_bg_task_expected_interval_seconds * 3` |
| 后台任务连续失败 | `simplejob_bg_task_consecutive_failures` |
| 日志监听进程 | `simplejob_log_listener_alive` |
| 日志队列积压 | `simplejob_log_queue_size` |
| 工作流节点耗时 | `simplejob_workflow_node_duration_seconds` |
| 节点失败次数 | `simplejob_workflow_node_fail_total` |

### 8.4 看板四 日志

**目标**：从指标下钻到具体日志。

面板：

```yaml
# Logs 面板配置要点
type: logs
datasource: {type: loki, uid: loki}
targets:
  - expr: '{job="simplejob"} |~ "ERROR|Exception"'
    refId: A
options:
  showTime: true
  showLabels: false
  showCommonLabels: false
  wrapLogMessage: true
  prettifyLogMessage: false
  enableLogDetails: true
  dedupStrategy: none
```

> **关键**：用 **`Derived fields`**（见 4.1）把日志里的 `trace=` 提取成可点击链接，点一下跳 Tempo。

---

## 9. Prometheus 面板

### 9.1 三个必会写法

```promql
# 1) 错误率
sum(rate(http_requests_total{status=~"5.."}[5m]))
  / sum(rate(http_requests_total[5m]))

# 2) P95 延迟
histogram_quantile(0.95,
  sum(rate(http_request_duration_seconds_bucket[5m])) by (le, route))

# 3) Top 10 慢接口
topk(10, histogram_quantile(0.95,
  sum(rate(http_request_duration_seconds_bucket[5m])) by (le, route)))
```

> 详细 PromQL 见 [指标篇](./01-Prometheus-指标监控.md)。

### 9.2 步长与 `$__rate_interval`

面板的**步长（step）** 如果比 `rate()` 的窗口还小，曲线会出现断点（因为窗口内没有两个样本）。

**解决**：用 Grafana 内置变量

```promql
rate(http_requests_total[$__rate_interval])
```

`$__rate_interval` 会自动 ≥ 4 × 数据源采集间隔，比手写 `[5m]` 安全。

### 9.3 阈值面板的完整配置

```json
{
  "type": "stat",
  "title": "5xx 比例",
  "datasource": { "type": "prometheus", "uid": "prometheus" },
  "targets": [
    {
      "expr": "sum(rate(http_requests_total{status=~\"5..\"}[5m])) / sum(rate(http_requests_total[5m]))"
    }
  ],
  "fieldConfig": {
    "defaults": {
      "unit": "percentunit",
      "noValue": "N/A",
      "decimals": 2,
      "thresholds": {
        "mode": "absolute",
        "steps": [
          { "color": "green", "value": null },
          { "color": "orange", "value": 0.01 },
          { "color": "red", "value": 0.05 }
        ]
      }
    }
  },
  "options": {
    "colorMode": "background",
    "graphMode": "area",
    "textMode": "value_and_name"
  }
}
```

---

## 10. Loki 面板

### 10.1 Logs 面板配置

```json
{
  "id": 8,
  "type": "logs",
  "title": "ERROR 日志",
  "gridPos": { "h": 10, "w": 24, "x": 0, "y": 24 },
  "datasource": { "type": "loki", "uid": "loki" },
  "targets": [
    {
      "expr": "{job=\"simplejob\"} |~ \"ERROR|Exception\"",
      "refId": "A",
      "datasource": { "type": "loki", "uid": "loki" }
    }
  ],
  "options": {
    "showTime": true,
    "showLabels": false,
    "enableLogDetails": true,
    "wrapLogMessage": true,
    "dedupStrategy": "none",
    "sortOrder": "Descending"
  }
}
```

### 10.2 常用 LogQL 面板

| 面板 | 查询 |
|---|---|
| ERROR 日志 | `{job="simplejob"} \|= "ERROR"` |
| 按 request_id 串联 | `{job="simplejob"} \|= "a3f9c2d8"` |
| 审计下载 | `{job="simplejob-audit"} \| json \| action="resume.download"` |
| 登录失败 | `{job="simplejob"} \|= "登录失败"` |
| ERROR 数量（画曲线） | `sum(count_over_time({job="simplejob"} \|= "ERROR" [5m]))` |

> 详细 LogQL 见 [日志篇](./02-Loki与Promtail-日志.md)。

### 10.3 日志面板的三个实用设置

| 设置 | 作用 |
|---|---|
| **Derived fields** | 从日志提取 `trace_id`，点击跳 Tempo（见 4.1） |
| **Wrap lines** | 长日志换行，不横向滚动 |
| **Prettify JSON** | JSON 日志自动缩进 |

---

## 11. Tempo 面板

### 11.1 两种展示方式

| 方式 | 说明 |
|---|---|
| **Search** | 按 TraceQL 搜 trace 列表 |
| **TraceID 查询** | 直接贴一个 trace id |

### 11.2 Trace 面板配置

```json
{
  "type": "traces",
  "title": "慢请求链路",
  "datasource": { "type": "tempo", "uid": "tempo" },
  "targets": [
    {
      "query": "{ resource.service.name = \"simplejob\" } | duration > 2s",
      "refId": "A",
      "queryType": "traceql"
    }
  ]
}
```

### 11.3 Node graph 服务拓扑

Tempo 数据源打开 `jsonData.nodeGraph.enabled: true` 后，会自动用 span 关系画调用图：

```text
simplejob ──► PostgreSQL
    │
    ├──► Redis
    ├──► paddleocr（外部）
    └──► DashScope（外部）
```

> **这是零配置的系统拓扑图**，新接手项目时非常有用。

### 11.4 Traces → Logs 反查

在 Tempo 数据源里配：

```yaml
jsonData:
  tracesToLogsV2:
    datasourceUid: loki
    filterByTraceID: true
```

之后点开任意一条 trace，就能直接看到**同一条请求的日志**。

> **这就是三件套联动的核心**：trace 里看到慢 span → 点一下 → 日志已经筛好了。

---

## 12. 注释与告警面板

### 12.1 Annotation 注释

在曲线上标记事件（发布、故障）：

```yaml
# 仪表板 JSON
"annotations": {
  "list": [
    {
      "name": "告警",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "expr": "ALERTS",
      "iconColor": "red",
      "enable": true,
      "tagKeys": "alertname,severity",
      "textFormat": "{{ alertname }}",
      "titleFormat": "{{ alertname }}"
    },
    {
      "name": "发布",
      "datasource": { "type": "loki", "uid": "loki" },
      "expr": "{job=\"deploy\"} |= \"released\"",
      "iconColor": "blue"
    }
  ]
}
```

**价值**：看到曲线突变时，能立刻知道「那个时间点发过版」。

### 12.2 Alert list 面板

把当前活跃告警显示在看板上：

```json
{
  "type": "alertlist",
  "title": "活跃告警",
  "options": {
    "showOptions": "firing",
    "sortOrder": 1,
    "dashboardAlerts": "only",
    "alertName": "",
    "maxItems": 20
  }
}
```

---

## 13. Grafana 自带告警

### 13.1 两套告警体系

Grafana 有两个告警系统：

| | **Legacy alerting** | **Unified alerting**（默认，9.0+） |
|---|---|---|
| 状态 | 已废弃 | ✅ 推荐 |
| 告警存在哪 | 面板里 | 独立的 Alert rules 页面 |
| 多数据源 | 受限 | ✅ 支持（Prometheus/Loki/Tempo/SQL…） |
| 通知 | 面板内 | Contact points + Notification policies |
| 与 Alertmanager | 独立 | 独立 |

### 13.2 本项目为什么用 Alertmanager

| | Grafana Unified Alerting | Alertmanager |
|---|---|---|
| 强项 | 多数据源、和看板一体 | **告警路由/抑制/静默/值班** |
| 抑制规则 | 较弱 | ✅ 强大 |
| 模板 | 够用 | ✅ Go template 更灵活 |
| 多 Prometheus 聚合 | 需要配置 | ✅ 天然支持 |
| 学习成本 | 低 | 中 |

> **本项目选择**：告警规则写在 **Prometheus**（`alerts.yml`），路由/通知交给 **Alertmanager**。
> Grafana 只负责**展示**（Alert list 面板、注释），不重复做告警判断。
>
> 理由：告警判断需要 PromQL 和 `for` 语义；路由/抑制/静默是 Alertmanager 的主场。详见 [告警触达篇](./04-Alertmanager-告警触达.md)。

### 13.3 什么时候用 Grafana 告警

| 场景 | 建议 |
|---|---|
| 只有 Loki，想按日志告警 | ✅ Grafana 告警（或 Loki Ruler → AM） |
| 数据源是 SQL / CloudWatch | ✅ Grafana 告警 |
| 需要复杂的抑制/分级/值班 | ✅ Alertmanager |
| 已经有 Prometheus 告警体系 | ✅ 统一走 Alertmanager，别搞两套 |

---

## 14. 权限与 API

### 14.1 权限模型

```text
Grafana 组织（Org）
├── 团队 Team（如 hr-ops）
│   ├── 成员（Viewer / Editor / Admin）
│   └── 文件夹权限
└── API Key / Service Account
```

| 角色 | 能做什么 |
|---|---|
| **Viewer** | 只看 |
| **Editor** | 能改仪表板 |
| **Admin** | 能改数据源、权限 |

> **生产建议**：把「能改数据源和权限」的人限制到最少，其他人给 Editor。

### 14.2 仪表板即代码

```bash
# 导出仪表板 JSON（给 API Key）
curl -s http://grafana:3000/api/dashboards/uid/simplejob-overview \
  -H "Authorization: Bearer $GF_API_KEY" | python -m json.tool > simplejob-overview.json
```

配合 provisioning 自动加载（见 [6.4](#64-仪表板即代码)）。

### 14.3 常用 API

```bash
# 搜索仪表板
curl -s -H "Authorization: Bearer $GF_API_KEY" \
  "http://grafana:3000/api/search?type=dash-db" | python -m json.tool

# 创建/更新仪表板
curl -XPOST -H "Authorization: Bearer $GF_API_KEY" \
  -H 'Content-Type: application/json' \
  -d @dashboard.json http://grafana:3000/api/dashboards/db

# 数据源健康检查
curl -s -H "Authorization: Bearer $GF_API_KEY" \
  http://grafana:3000/api/datasources/uid/prometheus/health
```

---

## 15. 排障

### 15.1 面板没数据

**按顺序排查**：

```text
1. 数据源 Save & test 是绿的吗？        → 数据源页
2. 时间范围对吗？                        → 右上角；是不是选到了未来
3. 查询本身有结果吗？                    → Explore 里跑一遍
4. 变量筛选太严？                        → 变量选了不存在的值
5. 面板查询超时？                        → 缩小时间范围
6. 数据真的存在吗？                      → 直接 curl 数据源 API
```

### 15.2 曲线有断点

| 原因 | 处置 |
|---|---|
| `rate()` 窗口内样本不够 | 改用 `$__rate_interval` |
| 采集间隔变大 | 对齐 `timeInterval` |
| 计数器重启归零 | 正常，`rate()` 已处理；断点明显就缩小窗口 |
| 数据源超时 | 缩短时间范围 / 加 recording rule |

### 15.3 单位 / 阈值不对

| 现象 | 原因 |
|---|---|
| 阈值 0.01 永远不触发 | 单位选成 `percent`，0.01 表示 0.01% |
| 数字显示成 1234.5678 | 忘了设 `decimals` |
| 曲线显示 "No data" | `noValue` 没配，或标签写错 |

### 15.4 Explore 排障流程

```mermaid
flowchart TD
    A["发现异常<br/>Prometheus 曲线突变"] --> B["Explore 选 Prometheus<br/>确认异常时间点"]
    B --> C["切到 Loki<br/>同时间范围搜 ERROR"]
    C --> D["拿到 request_id / trace_id"]
    D --> E["切到 Tempo<br/>贴 trace_id 看卡在哪一环"]
    E --> F["定位根因<br/>修依赖 / 回滚"]

    style A fill:#1e3a5f,stroke:#38bdf8,color:#e2e4ed
    style C fill:#14532d,stroke:#4ade80,color:#e2e4ed
    style E fill:#3b2f5b,stroke:#a78bfa,color:#e2e4ed
```

**Explore 的两个好用功能**：

- **Split view**：左右同时开 Prometheus 和 Loki
- **Query history**：自动记录，方便回退

### 15.5 权限问题

| 现象 | 原因 |
|---|---|
| 看不到数据源 | 文件夹/数据源权限不足 |
| 无法编辑面板 | 角色是 Viewer |
| API 401 | API Key 过期 / 权限不足 |
| 数据源被删 | 面板报「数据源不存在」 |

---

## 16. 最佳实践清单

**设计**

- [ ] 每个面板回答**一个**明确的问题
- [ ] 总览看板只放 RED + 关键业务量
- [ ] 用文件夹按「问题域」组织，不按数据源
- [ ] 面板标题写「什么」，不写「哪个指标」
- [ ] 单位、阈值、小数位显式配置

**查询**

- [ ] `rate()` 用 `$__rate_interval`
- [ ] 变量用 `=~"$var"` 而非手写引号
- [ ] 避免高基数查询（会拖慢面板）
- [ ] 常用查询做成 recording rule

**可维护**

- [ ] 仪表板 JSON 存 Git
- [ ] 数据源配置走 provisioning，UID 固定
- [ ] 变更走 PR + review
- [ ] 定期清理无用面板

**排障**

- [ ] 日志面板配 Derived fields 跳 Tempo
- [ ] 关键仪表板加 Annotation（发布、告警）
- [ ] 加 Alert list 面板看活跃告警

---

## 17. 一页纸速查

### 17.1 概念层级

```text
Grafana 实例
└── 文件夹（Folder）
    └── 仪表板（Dashboard）
        ├── 变量（Variable）
        ├── 注释（Annotation）
        └── 面板（Panel）
            ├── 数据源（Data Source，靠 UID 引用）
            └── 查询（Target）
```

### 17.2 面板四要素

| 要素 | 说明 |
|---|---|
| `type` | 可视化类型 |
| `datasource` | 数据源 UID |
| `targets` | 查询表达式 |
| `fieldConfig` | 单位 / 阈值 / 显示 |

### 17.3 变量写法

| 场景 | 写法 |
|---|---|
| 单值精确匹配 | `{job="x", env="$env"}` |
| 多选正则匹配 | `{job="x", route=~"$route"}` |
| 全部 | `allValue: ".*"` |
| rate 窗口 | `$__rate_interval` |

### 17.4 数据源速查

| 数据源 | type | 典型用途 |
|---|---|---|
| Prometheus | `prometheus` | RED 指标、告警注释 |
| Loki | `loki` | 日志检索 |
| Tempo | `tempo` | 链路下钻 |
| PostgreSQL | `postgres` | 业务数据 |

### 17.5 三件套跳转

```text
Prometheus 曲线异常
   → Loki 同时间范围搜 ERROR（拿 trace_id）
   → Tempo 贴 trace_id（看卡在哪一环）
   → Tempo 点 trace → 反查 Loki 日志
```

### 17.6 五个易错点

```text
① 比例用 percent 而不是 percentunit   → 阈值差 100 倍
② rate() 窗口小于采集间隔             → 曲线断点
③ 数据源 UID 变更                      → 全部面板失效
④ 变量查询写 route=~'$route'           → 多选时不生效
⑤ 面板堆太多                           → 看板没人看
```

### 17.7 相关文档

- [总览篇](./00-监控告警健康检查-总览.md) —— 监控 / 告警 / 健康检查全景
- [指标篇](./01-Prometheus-指标监控.md) —— 指标与 PromQL（面板查询怎么写）
- [日志篇](./02-Loki与Promtail-日志.md) —— 日志与 LogQL（日志面板怎么写）
- [链路篇](./03-OpenTelemetry与Tempo-链路追踪.md) —— 链路与 TraceQL（Trace 面板怎么写）
- [告警触达篇](./04-Alertmanager-告警触达.md) —— 告警触达（与 Grafana 告警的分工）
- [monitoring-alerting-healthcheck.md](../monitoring-alerting-healthcheck.md) —— 完整 dashboard JSON
