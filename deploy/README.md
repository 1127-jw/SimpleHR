# SimpleJob 部署中心

> 本目录提供三套可落地的部署方案，以及配套的 Dockerfile / Compose / 脚本。

---

## 方案索引

| 方案 | 文档 | 适用 |
|------|------|------|
| **Docker 部署** | [docker-deployment.md](docker-deployment.md) | 生产环境，容器化编排 |
| **CD 流水线** | [cd-pipeline.md](cd-pipeline.md) | 持续集成 / 持续交付（GitHub Actions） |
| **一键部署** | [one-click-deploy.md](one-click-deploy.md) | 单台服务器，一条命令上线 |

---

## 目录结构

```
deploy/
├── README.md                      # 本文件
├── docker-deployment.md           # Docker 部署方案
├── cd-pipeline.md                 # CD 方案
├── one-click-deploy.md            # 一键部署方案
├── docker-compose.yml             # 编排（后端 + 前端 + PG + Redis）
├── docker/
│   ├── Dockerfile.backend         # 后端镜像
│   ├── Dockerfile.frontend        # 前端镜像
│   └── nginx.conf                 # 反代配置
└── scripts/
    ├── deploy.sh                  # 一键部署脚本
    └── init-db.sh                 # 数据库初始化
```

---

## 快速选择

| 你的情况 | 推荐 |
|----------|------|
| 有服务器，想快速跑起来 | **一键部署** |
| 生产环境，要容器化 | **Docker 部署** |
| 多人协作，要自动发布 | **CD 流水线** |

---

## 外部依赖（三者共用）

| 依赖 | 用途 |
|------|------|
| PostgreSQL | 业务数据 + LangGraph 检查点 |
| Redis | 缓存 / Token / 任务状态 |
| 邮件服务器（IMAP+SMTP） | 收发面试邮件 |
| LLM（OpenAI 兼容） | 评分 / 意图解析 |
| PaddleOCR | 简历 OCR |
| 钉钉开放平台 | 授权 / 日历 |
| 域名 + HTTPS | 钉钉 OAuth 回调 |
