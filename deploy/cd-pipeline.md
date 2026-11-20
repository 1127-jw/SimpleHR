# CD 流水线方案

> 适用：多人协作、持续交付。基于 GitHub Actions，覆盖 **测试 → 构建 → 推送镜像 → 部署**。

---

## 一、流水线总览

```
push / PR
   │
   ▼
┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  CI: 测试     │──▶│  CI: 构建    │──▶│  CD: 推送    │──▶│  CD: 部署    │
│  pytest      │   │  镜像/前端   │   │  Registry    │   │  服务器     │
└──────────────┘   └──────────────┘   └──────────────┘   └──────────────┘
```

| 阶段 | 触发 | 产出 |
|------|------|------|
| CI 测试 | push / PR | 测试报告 |
| CI 构建 | push master | 前端 dist / 后端 wheel |
| CD 推送 | push master / tag | Docker 镜像 |
| CD 部署 | tag `v*` / 手动 | 生产环境更新 |

---

## 二、CI：测试与构建（`.github/workflows/ci.yml`）

项目已有 `ci.yml`，核心步骤：

```yaml
jobs:
  backend:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --frozen
      - run: uv run pytest tests/ -q
      - run: uv run python -c "import app.main"

  frontend:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: '24' }
      - run: npm ci
        working-directory: hr-frontend
      - run: npm run type-check
        working-directory: hr-frontend
      - run: npm run build-only
        working-directory: hr-frontend
```

---

## 三、CD：镜像构建与推送（`.github/workflows/cd.yml`）

```yaml
name: CD

on:
  push:
    tags: ['v*']
  workflow_dispatch:

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
  build-and-push:
    runs-on: ubuntu-24.04
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: 元数据
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}
          tags: |
            type=ref,event=tag
            type=sha
            latest

      - name: 构建后端镜像
        uses: docker/build-push-action@v6
        with:
          context: .
          file: deploy/docker/Dockerfile.backend
          push: true
          tags: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}-backend:${{ github.ref_name }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: 构建前端镜像
        uses: docker/build-push-action@v6
        with:
          context: .
          file: deploy/docker/Dockerfile.frontend
          push: true
          tags: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}-frontend:${{ github.ref_name }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

---

## 四、CD：部署到服务器

### 4.1 方式 A：SSH 触发（最简单）

```yaml
  deploy:
    needs: build-and-push
    runs-on: ubuntu-24.04
    steps:
      - name: 远程部署
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.SERVER_HOST }}
          username: ${{ secrets.SERVER_USER }}
          key: ${{ secrets.SERVER_SSH_KEY }}
          script: |
            cd /opt/simplejob/deploy
            ./scripts/deploy.sh ${{ github.ref_name }}
```

### 4.2 方式 B：服务器拉取（Pull 模式）

服务器上跑一个 watcher，收到 webhook 后执行 `deploy.sh`。

```yaml
      - name: 触发服务器更新
        run: |
          curl -X POST https://your-server.com/hooks/deploy \
            -H "Authorization: Bearer ${{ secrets.DEPLOY_TOKEN }}"
```

---

## 五、密钥管理（GitHub Secrets）

| Secret | 说明 |
|--------|------|
| `SERVER_HOST` | 服务器 IP / 域名 |
| `SERVER_USER` | 登录用户 |
| `SERVER_SSH_KEY` | SSH 私钥 |
| `DEPLOY_TOKEN` | webhook 令牌（Pull 模式） |
| `GITHUB_TOKEN` | 自动提供，用于推送镜像 |

---

## 六、环境策略

| 环境 | 触发 | 镜像标签 | 数据库 |
|------|------|---------|--------|
| **staging** | push `main` | `sha-xxx` | 预发库 |
| **production** | tag `v*` | `v1.2.3` | 生产库 |

建议：
- staging 自动部署
- production **手动确认**（`environment` + `reviewers`）

```yaml
  deploy-prod:
    environment:
      name: production
      url: https://your-domain.com
    # 需在 GitHub Environments 配置 reviewers
```

---

## 七、回滚

```bash
# 服务器上
cd /opt/simplejob/deploy
./scripts/deploy.sh v1.2.2      # 回退到上一个 tag
```

或镜像层面：
```bash
docker compose up -d --no-deps backend
# 重新打旧 tag 的镜像
```

---

## 八、数据库迁移在 CD 中的位置

**部署脚本里自动执行**（`deploy.sh`）：

```bash
docker compose exec -T backend uv run alembic upgrade head
```

⚠️ 迁移是**向前兼容**的（只加列/加表），回滚镜像不会破坏新数据。

---

## 九、完整流水线文件

```
.github/workflows/
├── ci.yml          # 测试 + 构建
└── cd.yml          # 镜像推送 + 部署
```

---

## 十、 Checklist

- [ ] GitHub Secrets 配置完整
- [ ] Registry（ghcr.io）可访问
- [ ] 服务器可 SSH / 可接收 webhook
- [ ] `deploy/scripts/deploy.sh` 已就绪
- [ ] 数据库迁移向后兼容
- [ ] 生产环境配置 reviewers
- [ ] 监控告警接入（见 `docs/monitoring-alerting-healthcheck.md`）
