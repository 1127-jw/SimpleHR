# GitHub Actions 零基础教学

> 面向完全没有 CI/CD 经验的同学，用 simplejob 项目的自动部署作为实战案例。
> 读完你能：看懂一条流水线 → 自己写一条 → 把 simplejob 的部署自动化。

---

## 目录

- [一、先搞懂：什么是 CI/CD](#一先搞懂什么是-cicd)
- [二、GitHub Actions 是什么](#二github-actions-是什么)
- [三、6 个核心概念](#三6-个核心概念)
- [四、5 分钟跑通第一个 Workflow](#四5-分钟跑通第一个-workflow)
- [五、语法详解](#五语法详解)
- [六、实战：simplejob 的 CI/CD](#六实战simplejob-的-cicd)
- [七、怎么看运行结果 / 排查失败](#七怎么看运行结果--排查失败)
- [八、常见坑与最佳实践](#八常见坑与最佳实践)
- [九、术语速查表](#九术语速查表)
- [十、动手练习](#十动手练习)

---

## 一、先搞懂：什么是 CI/CD

| 缩写 | 全称 | 人话 |
|---|---|---|
| **CI** | Continuous Integration（持续集成） | 代码一推送，自动构建、跑测试，尽早发现问题 |
| **CD** | Continuous Delivery / Deployment（持续交付/部署） | 自动把通过检查的代码部署到服务器 |

**没有 CI/CD 时**（你现在的状态）：

```
本地改代码 → git push → 上服务器 → git pull → uv sync → npm build → systemctl restart
```

每一步都是手工，容易忘、容易错（这次部署踩的坑全是手工环节出的）。

**有 CI/CD 后**：

```
本地改代码 → git push → 机器自动完成剩下全部动作 ✅
```

GitHub Actions 就是帮你干这事的工具之一。

---

## 二、GitHub Actions 是什么

- **GitHub 官方内置的自动化工具**：在仓库里放一个 YAML 文件，GitHub 就会在指定事件（如 push）发生时，**在云端启动一台虚拟服务器**（叫 Runner），按文件里的定义一步步执行。
- **不用自建服务器**、不用装 Jenkins、和 GitHub 仓库无缝集成。
- **免费额度**：私有仓库每月 **2000 分钟** Linux 构建时长（个人套餐），对个人/小团队基本用不完。

一个 YAML 文件 = 一条流水线，长这样：

```yaml
name: Deploy

on:
  push:
    branches: [master]     # master 有推送就触发

jobs:
  deploy:
    runs-on: ubuntu-latest   # 在 Ubuntu 虚拟机上跑
    steps:
      - run: echo "开始部署……"
```

---

## 三、6 个核心概念

先把这 6 个词搞明白，后面看任何流水线都不迷糊：

| 概念 | 英文 | 说明 | 类比 |
|---|---|---|---|
| **工作流** | Workflow | 一个 `.yml` 文件 = 一条流水线 | 一整份「自动化作业指导书」 |
| **事件** | Event | 触发流水线的条件：push / PR / 定时 / 手动 | 「什么时候开工」 |
| **任务** | Job | 流水线里的一组步骤，多个 Job 可以并行跑 | 「一道工序」 |
| **步骤** | Step | Job 里的单个动作：跑命令，或调用 Action | 「一个具体动作」 |
| **动作** | Action | 别人封装好的可复用步骤（如 `actions/checkout`） | 「现成的工具，不用自己造」 |
| **运行器** | Runner | 真正执行任务的虚拟机（GitHub 托管 or 自建） | 「干活的工人」 |

**层级关系**：

```
Workflow（一个 yml 文件）
 └── Job 1（如 build）        ← 多个 Job 默认并行
 │    ├── Step 1
 │    ├── Step 2
 │    └── Step 3
 └── Job 2（如 deploy）
      ├── Step 1
      └── Step 2
```

---

## 四、5 分钟跑通第一个 Workflow

**第 1 步**：在仓库里创建文件 `.github/workflows/hello.yml`（目录名必须是 `.github/workflows`）。

**第 2 步**：写入：

```yaml
name: Hello                        # 流水线名字（显示在 Actions 页面上）

on:                                # 触发条件
  push:
    branches: [master]             # master 分支有推送时
  workflow_dispatch:               # 允许在页面上手动点「Run workflow」

jobs:
  hello:                           # Job 名字（随便起）
    runs-on: ubuntu-latest           # 用 GitHub 托管的最新版 Ubuntu
    steps:                         # 步骤列表
      - name: 打招呼                # 步骤名字
        run: echo "Hello, GitHub Actions!"
```

**第 3 步**：提交并推送。

```bash
git add .github/workflows/hello.yml
git commit -m "ci: 第一个 workflow"
git push
```

**第 4 步**：打开 GitHub 仓库 → 顶部 **Actions** 标签 → 能看到一条「Hello」记录 → 点开 → 点 `hello` Job → 点「打招呼」步骤 → 看到输出 `Hello, GitHub Actions!`。

🎉 恭喜，你的第一条流水线跑通了。

---

## 五、语法详解

### 5.1 `on`：什么时候触发

```yaml
on:
  push:
    branches: [master]              # 只有推 master 才触发
    paths:                          # 只有这些目录变化才触发（可选，省构建次数）
      - 'app/**'
      - 'hr-frontend/**'

  pull_request:
    branches: [master]              # 有人提 PR 到 master 时触发

  schedule:                         # 定时触发（UTC 时间）
    - cron: '0 2 * * *'             # 每天凌晨 2:00（UTC）

  workflow_dispatch:                # 允许手动触发
    inputs:                         # 手动触发时可以填参数（可选）
      target:
        description: '部署目标'
        required: false
        default: 'prod'
```

> 常见组合：`push`（自动）+ `workflow_dispatch`（手动补部署/回滚）。

### 5.2 `runs-on`：在哪台机器上跑

```yaml
runs-on: ubuntu-latest      # GitHub 托管的 Ubuntu（最常用）
runs-on: ubuntu-22.04       # 指定版本
runs-on: self-hosted        # 自建 Runner（跑在你自己的服务器上）
```

### 5.3 `steps`：两种步骤写法

**① `uses`** —— 调用别人写好的 Action：

```yaml
steps:
  - name: 检出代码
    uses: actions/checkout@v4        # 把仓库代码拉到 Runner 上（几乎每个流水线第一步）

  - name: 安装 Python
    uses: actions/setup-python@v5
    with:                            # 传给 Action 的参数
      python-version: '3.13'
```

**② `run`** —— 直接执行 shell 命令：

```yaml
steps:
  - name: 装依赖
    run: pip install -r requirements.txt

  - name: 多行命令
    run: |
      echo "第一行"
      echo "第二行"
```

> `run` 默认用 `bash`（Linux）。`|` 表示多行字符串。

### 5.4 `${{ }}` 表达式：动态取值

在 YAML 里用 `${{ }}` 取各种上下文信息：

| 写法 | 含义 | 例子 |
|---|---|---|
| `${{ github.ref_name }}` | 当前分支名 | `master` |
| `${{ github.sha }}` | 当前提交的 SHA | `0f124a2...` |
| `${{ github.actor }}` | 触发者用户名 | `jiawei0012` |
| `${{ secrets.SERVER_HOST }}` | 密钥（见 5.5） | 你的服务器 IP |
| `${{ env.MY_VAR }}` | 环境变量 | 自定义值 |
| `${{ needs.build.result }}` | 上一个 Job 的结果 | `success` |

例子：

```yaml
steps:
  - name: 打印分支
    run: echo "当前分支是 ${{ github.ref_name }}，提交者是 ${{ github.actor }}"
```

### 5.5 `secrets`：密钥（重点）

服务器 IP、SSH 私钥这类**不能写进代码**的东西，放在 GitHub 的加密配置里：

**配置位置**：仓库 → **Settings → Secrets and variables → Actions → New repository secret**

**YAML 里引用**：

```yaml
steps:
  - name: SSH 登录服务器
    env:
      SSH_KEY: ${{ secrets.SSH_PRIVATE_KEY }}     # 从 secrets 取出，注入到环境变量
    run: |
      echo "$SSH_KEY" > ~/.ssh/id_ed25519
      chmod 600 ~/.ssh/id_ed25519
      ssh admin@${{ secrets.SERVER_HOST }} 'echo 登录成功'
```

**特性**：
- secret 的值**永远不会**出现在日志里（GitHub 自动打码成 `***`）
- 名字区分大小写，拼错了会取到空值
- 修改 secret 后，**正在运行的流水线不会生效**，下次运行才用新值

### 5.6 多 Job 与依赖（`needs`）

多个 Job 默认**并行**执行；用 `needs` 声明依赖，让它们**串行**：

```yaml
jobs:
  build:                            # Job 1：构建/检查
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo "构建中……"

  deploy:                           # Job 2：部署
    needs: build                    # 等 build 成功后才执行
    if: github.ref == 'refs/heads/master'   # 且只在 master 上执行
    runs-on: ubuntu-latest
    steps:
      - run: echo "部署中……"
```

`if` 的常见写法：

```yaml
if: github.ref == 'refs/heads/master'        # 只在 master
if: github.event_name == 'push'             # 只在 push 事件（排除手动触发等）
if: ${{ success() }}                         # 前面的步骤都成功
if: ${{ failure() }}                         # 前面有失败时才跑（如发通知）
```

### 5.7 环境变量（`env`）

三个层级，作用范围从大到小：

```yaml
env:
  GLOBAL_VAR: '全局的值'          # 整个 Workflow 可用

jobs:
  build:
    env:
      JOB_VAR: 'Job 级的值'       # 这个 Job 里可用
    steps:
      - name: 用变量
        env:
          STEP_VAR: '步骤级的值'   # 只这一步可用
        run: echo "$GLOBAL_VAR / $JOB_VAR / $STEP_VAR"
```

---

## 六、实战：simplejob 的 CI/CD

本章内容来自 simplejob 项目的真实配置，可直接对照仓库里的文件学习：

| 文件 | 作用 |
|---|---|
| `.github/workflows/ci.yml` | CI + CD 的全部定义（四个 Job） |
| `scripts/deploy.sh` | 服务器端部署脚本（CD 时执行） |
| `scripts/ci/smoke.py` | 冒烟测试：请求核心接口 |
| `app/core/config.py` | `MAIL_BOT_ENABLED` 开关（CI 跳过邮件机器人） |

### 6.1 完整链路

```
git push (master)
   ↓
GitHub Actions（同一条 workflow，四个 Job 串起来）
   ├── backend   ✅ uv sync + import 校验 + pytest（52 个测试）
   ├── frontend  ✅ npm ci + 类型检查 + vite build
   ├── smoke     ✅ service containers（PG+Redis）→ 起应用 → 接口冒烟
   └── deploy    ✅ needs 上面三个 → SSH 到 ECS → 执行 scripts/deploy.sh
                     ├── git fetch && reset --hard origin/master
                     ├── uv sync --all-groups
                     ├── npm ci && npm run build
                     └── sudo simplejob-restart（systemd 拉起后端）
```

**为什么 CI 和 CD 写在同一个 workflow 里？**
用 `needs` 把「检查」和「部署」串成一条链，比拆成两个文件更简单、更可靠：
任一检查失败，deploy 自动跳过。

### 6.2 服务器前置准备（一次性，admin 执行）

**① 重启脚本 + 最小 sudo**（让 simplejob 只能重启服务，不能干别的）：

```bash
sudo tee /usr/local/bin/simplejob-restart <<'EOF'
#!/usr/bin/env bash
systemctl restart simplejob
EOF
sudo chmod 755 /usr/local/bin/simplejob-restart

sudo visudo
# 添加一行：
# simplejob ALL=(root) NOPASSWD: /usr/local/bin/simplejob-restart
```

**② SSH 部署密钥**（专用，别复用个人密钥）：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""
sudo -u simplejob mkdir -p /home/simplejob/.ssh
sudo -u simplejob tee -a /home/simplejob/.ssh/authorized_keys < ~/.ssh/github_deploy.pub
sudo -u simplejob chmod 700 /home/simplejob/.ssh
sudo -u simplejob chmod 600 /home/simplejob/.ssh/authorized_keys
```

**③ 服务器 origin 指向 GitHub**（关键，否则部署拉不到新代码）：

```bash
sudo -u simplejob bash -c 'cd /opt/simplejob/simple-hr && git remote set-url origin git@github.com:<你的用户名>/simple-hr.git'
```

### 6.3 配置 GitHub Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret：

| Name | Value |
|---|---|
| `SERVER_HOST` | ECS 公网 IP |
| `SERVER_USER` | `simplejob` |
| `SSH_PRIVATE_KEY` | `~/.ssh/github_deploy` 私钥全文（含 `-----BEGIN ...` 头尾） |

### 6.4 CI：三个检查 Job

#### backend（后端测试）

```yaml
  backend:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        python-version: ['3.13']
    env:
      # 项目在「导入期」就读配置，CI 没有 .env，必须给占位值
      MAIL_USERNAME: ci@example.com
      MAIL_PASSWORD: ci-password
      MAIL_FROM: ci@example.com
      ACCESS_SECRET: ci-access-secret-0123456789abcdef0123456789
      REFRESH_SECRET: ci-refresh-secret-fedcba9876543210fedcba9876543
      DASHSCOPE_API_KEY: ci-dummy-key
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: ${{ matrix.python-version }}
          enable-cache: true
      - run: uv sync --all-groups
      - run: uv run python -c "import app.main; print('backend import OK')"
      - run: uv run python -m pytest
```

> **两个必须注意的点**：
> 1. **必须有 env 占位值**：`app/core/config.py` 的校验器要求 `MAIL_*` 必填、
>    `ACCESS_SECRET` / `REFRESH_SECRET` 非空，否则 import 阶段直接 `ValidationError`。
> 2. **必须 `python -m pytest`**：`tests/` 没有 `__init__.py`，直接跑 `pytest`
>    不会把项目根目录加进 `sys.path`，会报 `No module named 'app'`。

#### frontend（前端构建）

```yaml
  frontend:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        node-version: [24]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ matrix.node-version }}
          cache: npm
          cache-dependency-path: hr-frontend/package-lock.json
      - run: npm ci
        working-directory: hr-frontend
      - run: npm run build
        working-directory: hr-frontend
        env:
          VITE_API_BASE_URL: ''
```

#### smoke（集成冒烟：service containers）

```yaml
  smoke:
    runs-on: ubuntu-24.04
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_DB: hr_system
          POSTGRES_USER: simplejob
          POSTGRES_PASSWORD: ci-password
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U simplejob -d hr_system"
          --health-interval 5s
          --health-timeout 3s
          --health-retries 10
      redis:
        image: redis:7-alpine
        ports:
          - 6379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 5s
          --health-timeout 3s
          --health-retries 10
    env:
      DB_USERNAME: simplejob
      DB_PASSWORD: ci-password
      DB_HOST: 127.0.0.1
      DB_PORT: '5432'
      DB_NAME: hr_system
      REDIS_HOST: 127.0.0.1
      REDIS_PORT: '6379'
      REDIS_DB: '0'
      ACCESS_SECRET: ci-access-secret-0123456789abcdef0123456789
      REFRESH_SECRET: ci-refresh-secret-fedcba9876543210fedcba9876543
      MAIL_USERNAME: ci@example.com
      MAIL_PASSWORD: ci-password
      MAIL_FROM: ci@example.com
      DASHSCOPE_API_KEY: ci-dummy-key
      MAIL_BOT_ENABLED: 'false'      # 跳过邮件机器人（CI 没有邮箱服务）
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: '3.13'
      - run: uv sync --all-groups
      - name: 初始化数据库（迁移 + 种子数据）
        run: |
          uv run alembic upgrade head
          uv run python init_data.py
      - name: 启动应用 + 冒烟测试
        run: |
          .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 &
          # 等待应用就绪后，请求核心接口
          uv run python scripts/ci/smoke.py
```

**service containers 是 GitHub Actions 里最接近真实环境的测试方式**：
在 Runner 里用 Docker 起临时的 PostgreSQL / Redis，应用连它们跑起来，任务结束即销毁。
`MAIL_BOT_ENABLED=false` 让应用跳过邮件机器人（它需要真实邮箱，CI 里没有）。

### 6.5 CD：deploy Job

```yaml
  deploy:
    name: 部署到服务器
    needs: [backend, frontend, smoke]
    if: |
      github.ref == 'refs/heads/master' && (
        github.event_name == 'push' ||
        github.event_name == 'workflow_dispatch'
      )
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - name: 配置 SSH 密钥
        run: |
          mkdir -p ~/.ssh
          printf '%s\n' "${{ secrets.SSH_PRIVATE_KEY }}" > ~/.ssh/id_ed25519
          chmod 600 ~/.ssh/id_ed25519
      - name: 上传部署脚本
        run: |
          ssh -o StrictHostKeyChecking=accept-new \
              "${{ secrets.SERVER_USER }}@${{ secrets.SERVER_HOST }}" \
              'install -d -m 755 /opt/simplejob/simple-hr/scripts'
          ssh -o StrictHostKeyChecking=accept-new \
              "${{ secrets.SERVER_USER }}@${{ secrets.SERVER_HOST }}" \
              'cat > /opt/simplejob/simple-hr/scripts/deploy.sh && chmod +x /opt/simplejob/simple-hr/scripts/deploy.sh' \
              < scripts/deploy.sh
      - name: 执行部署
        run: |
          ssh -o StrictHostKeyChecking=accept-new \
              "${{ secrets.SERVER_USER }}@${{ secrets.SERVER_HOST }}" \
              '/opt/simplejob/simple-hr/scripts/deploy.sh'
```

**关键点**：
- `needs: [backend, frontend, smoke]` —— 任一检查失败，部署自动跳过
- `if` —— 只部署 master；push 自动部署，`workflow_dispatch` 支持手动补部署/回滚
- **先上传脚本再执行** —— 避免「脚本还没上线」的鸡生蛋问题

### 6.6 部署脚本 scripts/deploy.sh

```bash
#!/usr/bin/env bash
set -euo pipefail
PROJECT="/opt/simplejob/simple-hr"
LOG="$PROJECT/logs/deploy.log"
UV="/home/simplejob/.local/bin/uv"
[ -x "$UV" ] || UV="$(command -v uv || true)"

exec 9>/tmp/simplejob-deploy.lock
flock -n 9 || exit 0

echo "[$(date '+%F %T')] ===== 开始部署 =====" | tee -a "$LOG"
cd "$PROJECT"
git fetch origin 2>&1 | tee -a "$LOG"
git reset --hard origin/master 2>&1 | tee -a "$LOG"
"$UV" sync --all-groups 2>&1 | tee -a "$LOG"
cd hr-frontend && rm -rf dist && npm ci 2>&1 | tee -a "$LOG"
VITE_API_BASE_URL='' npm run build 2>&1 | tee -a "$LOG"
sudo /usr/local/bin/simplejob-restart
sleep 3
systemctl is-active --quiet simplejob && echo "✅ 部署完成" | tee -a "$LOG"
```

> **为什么用 `reset --hard` 而不是 `pull`**：仓库历史遗留跟踪了
> `__pycache__/*.pyc`，服务器运行时会不断重新生成，`git pull` 会因
> 「local changes would be overwritten」中止；`reset --hard` 直接对齐远端。

### 6.7 验证与回滚

- **验证**：Actions 页面四个 Job 全绿；或服务器上看
  `tail -f /opt/simplejob/simple-hr/logs/deploy.log`
- **手动补部署**：Actions → CI → Run workflow
- **回滚**：`git revert <坏提交> && git push` → 自动部署上一个好版本

### 6.8 本项目踩过的坑（都是真实报错）

| 现象 | 原因 | 解决 |
|---|---|---|
| `校验应用可导入` 报 `ValidationError` | backend job 没有 env 占位值 | 补 `env` 块 |
| 测试报 `No module named 'app'` | 直接跑 `pytest` | 改 `python -m pytest` |
| 冒烟测试卡在启动 | 邮件机器人连不上邮箱 | `MAIL_BOT_ENABLED=false`（开关内聚在 `MailManager.init`） |
| 部署脚本报 `local changes would be overwritten` | `.pyc` 本地改动 | 用 `reset --hard` |
| SSH 报 `dubious ownership` | 用 admin 操作 simplejob 的仓库 | 统一用 simplejob 身份 |

---

## 七、怎么看运行结果 / 排查失败

**查看路径**：仓库 → Actions → 点某次运行 → 点 Job（如 `deploy`）→ 点 Step → 看实时/历史日志。

```
Actions 页面
 └── 某次运行（如 "fix: 修复统计bug"）
      └── Job: deploy  ✅/❌
           ├── Step: 配置 SSH 密钥   ✅
           └── Step: 执行部署        ❌ ← 点开看红色日志
```

**常见失败原因排查表**：

| 现象 | 可能原因 | 怎么查 |
|---|---|---|
| 某个 `run` 步骤报错 | 命令本身失败 | 看该步骤日志最后几行 |
| SSH 连接被拒 | 公钥没加对 / 安全组没放行 22 端口 | 本地 `ssh simplejob@<IP>` 试试 |
| secret 取到空值 | 名字拼错 / 大小写不一致 | 对照 Settings 里的名字 |
| 流水线没触发 | 分支不对 / `paths` 过滤掉了 | 看 Actions 页有没有记录 |
| 服务重启失败 | simplejob 没有 sudo 权限 | 服务器上 `sudo -u simplejob sudo -l` 查看授权 |
| Actions 里 `Resource not accessible` | 仓库没开启 Actions 权限 | Settings → Actions → General → 允许 |

**调试技巧**：在步骤里加 `run: env` 或 `run: ls -la` 打印环境，能快速定位问题（**注意别打印 secrets**）。

---

## 八、常见坑与最佳实践

1. **密钥绝不写进 YAML**：一律用 `secrets`，GitHub 会自动对日志打码。
2. **锁定 Action 版本**：用 `actions/checkout@v4`，**不要**用 `@main`——否则上游改代码你可能被动中招（供应链投毒）。
3. **善用 `workflow_dispatch`**：手动触发，补部署、回滚时特别好用。
4. **部署类流水线加分支/事件限制**：
   ```yaml
   if: github.ref == 'refs/heads/master' && github.event_name == 'push'
   ```
   避免 PR、手动触发时误部署。
5. **失败要能感知**：可以加一个失败通知步骤（发到钉钉/飞书/邮箱），否则流水线挂了都不知道。
6. **构建产物不进 git**：`dist/` 在 `.gitignore` 里，部署时重新构建，别把产物提交上来。
7. **日志里别主动 `echo` 密钥**：虽然 GitHub 会打码，但养成习惯更安全。
8. **Runner 是全新的虚拟机**：每个 Job 都跑在干净环境里，需要的东西（Python/Node/依赖）都要显式安装。

---

## 九、术语速查表

| 术语 | 一句话解释 |
|---|---|
| Workflow | 一个 `.github/workflows/*.yml` 文件，一条完整的流水线 |
| Event | 触发条件（push / PR / 定时 / 手动） |
| Job | 一组步骤的集合，多个 Job 可并行 |
| Step | 一个具体动作（`run` 或 `uses`） |
| Action | 可复用的封装步骤，如 `actions/checkout@v4` |
| Runner | 执行任务的虚拟机（GitHub 托管或自建） |
| Secret | 存在 GitHub 里的加密配置，YAML 里用 `${{ secrets.X }}` 引用 |
| Expression | `${{ }}` 包裹的动态表达式，如 `${{ github.ref_name }}` |
| `needs` | 声明 Job 之间的依赖（串行） |
| `if` | 条件执行 |
| `runs-on` | 指定运行环境（`ubuntu-latest` / `self-hosted`） |

---

## 十、动手练习

1. **练习 1**：把第四章的 `hello.yml` 改成「push 时打印当前分支名和提交者」。
2. **练习 2**：给 `hello.yml` 加一个定时触发（每天早上 8 点 UTC）和一个手动触发。
3. **练习 3**：按第六章给 simplejob 配好 `deploy.yml`，然后**手动触发**一次（Actions → Deploy to ECS → Run workflow），观察服务器上的部署日志。
4. **练习 4（进阶）**：在部署前加一个 `build` Job 做验证（`uv sync` + 前端 `npm run build`），用 `needs` 让 `deploy` 依赖它——构建失败就不部署。

---

> 参考资料：[GitHub Actions 官方文档](https://docs.github.com/zh/actions) ｜ [Expression 语法](https://docs.github.com/zh/actions/learn-github-actions/expressions) ｜ [actions/checkout](https://github.com/actions/checkout)
