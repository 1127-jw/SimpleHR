# Docker 与 Linux 常用命令速查手册

> 按"日常 80% 场景"整理，左侧命令、右侧说明与示例。遇到具体问题可直接按 **场景** 查目录。
>
> 约定：`<>` 必填参数，`[]` 可选参数；`#` 后是注释。

---

## 目录

| | | |
|---|---|---|
| [一、Linux 基础](#一linux-基础) | [二、进程与性能排查](#二进程与性能排查) | [三、网络诊断](#三网络诊断) |
| [四、文本处理](#四文本处理) | [五、用户·权限·系统管理](#五用户权限系统管理) | [六、打包与传输](#六打包与传输) |
| [七、Docker 镜像](#七docker-镜像) | [八、Docker 容器](#八docker-容器) | [九、Docker 网络与存储](#九docker-网络与存储) |
| [十、Dockerfile 编写](#十dockerfile-编写) | [十一、Docker Compose](#十一docker-compose) | [十二、Docker 排查与清理](#十二docker-排查与清理) |
| [十三、高频场景速查](#十三高频场景速查) | [十四、安全红线](#十四安全红线) | |

---

# 一、Linux 基础

## 1.1 文件与目录

```bash
pwd                          # 当前路径
ls -lah                      # 人类可读、含隐藏文件（最常用）
ls -lt                       # 按修改时间倒序（找最新文件）
ls -lSh                      # 按大小倒序（找大文件）

cd -                         # 回到上一个目录
cd ~                         # 回家目录
mkdir -p a/b/c               # 递归创建
cp -r src dst                # 递归复制目录
cp -a src dst                # 保留权限/时间/软链
mv old new                   # 移动/重命名
rm -rf dir/                  # 强制递归删除（危险，见 §14）
rm -i file                   # 删除前逐个确认

ln -s /real/path /link       # 建软链接（目标不存在也不报错）
ln /real/path /hardlink      # 硬链接
readlink -f /link            # 解析软链真实路径

tree -L 2 -I node_modules    # 目录树，限 2 层，忽略指定目录
stat file                    # 大小/权限/时间戳
file file                    # 判断文件类型
wc -l file                   # 行数
```

## 1.2 查找文件

```bash
find . -name "*.log"                        # 按名字（区分大小写）
find . -iname "*.LOG"                       # 忽略大小写
find /var -name "*.log" -mtime -1           # 1 天内修改过
find . -size +100M -size -1G                # 大小区间
find . -type f -name "*.py" | xargs rm      # 找到即删（先去掉 rm 预览）
find . -type f -exec grep -l "TODO" {} +    # 找到并执行命令

locate nginx.conf           # 基于数据库秒搜（需 updatedb）
which python3               # 命令在 PATH 里的位置
type -a ls                  # 命令解析顺序（别名/函数/可执行）
```

## 1.3 磁盘与容量

```bash
df -h                       # 各分区使用率（-h 人类可读）
df -i                       # inode 使用率（小文件过多时会满）
du -sh dir/                 # 目录总大小
du -h --max-depth=1 /var | sort -rh | head   # 找出谁最占空间
lsblk                       # 块设备与挂载点
fdisk -l                    # 分区表（需 root）
mount | column -t           # 已挂载文件系统
free -h                     # 内存
```

## 1.4 文件权限

```bash
ls -l file          # -rw-r--r--  1 user group 1024 Jan  1 10:00 file
#                     ↑类型  ↑属主 ↑属组 ↑其他
# 类型: - 普通文件  d 目录  l 软链  b 块设备  c 字符设备
# r=4 w=2 x=1 → 755 = rwxr-xr-x

chmod 755 file                  # rwxr-xr-x
chmod u+x file                  # 仅属主加执行
chmod -R go-w dir/              # 递归去掉组和其他人的写权限
chown user:group file           # 改属主与属组
chown -R www-data:www-data /var/www
umask 022                       # 新建文件默认权限 = 666-022 / 777-022
getfacl file                    # 查看 ACL（比 chmod 更细）
setfacl -m u:bob:rw file        # 给 bob 单独授权
```

## 1.5 查看文件内容

```bash
cat file                    # 全部输出（小文件）
less file                   # 分页查看：空格翻页、/搜索、q 退出、G 末尾
head -n 20 file             # 前 20 行
tail -n 50 file             # 后 50 行
tail -f app.log             # 实时跟踪日志（排查首选）
tail -n 1000 app.log | grep ERROR   # 先看尾部再过滤

# 只关心列/字段
cut -d: -f1 /etc/passwd     # 按 ":" 分割取第 1 列（列出所有用户名）
awk -F: '{print $1, $3}' /etc/passwd   # 取第 1、3 列
sort file | uniq -c | sort -rn | head  # 词频统计 Top N
```

---

# 二、进程与性能排查

## 2.1 进程管理

```bash
ps aux                      # 全量进程（BSD 风格，最常用）
ps -ef                      # 全量进程（System V 风格）
ps aux | grep nginx         # 找进程
ps -eo pid,ppid,user,%cpu,%mem,cmd --sort=-%cpu | head   # 按 CPU 排序

top                         # 交互式（按 M 按内存排、P 按 CPU 排）
htop                        # 更友好的 top（需安装）
pgrep -a nginx              # 按名字查 PID
pidof nginx

kill <PID>                  # 发 SIGTERM（15，允许优雅退出）
kill -9 <PID>               # SIGKILL（强杀，抓不到就重启）
killall nginx               # 按名字杀
pkill -f "python app.py"    # 按完整命令行匹配杀

jobs                        # 当前 shell 的后台任务
fg %1                       # 调到前台
Ctrl+C                      # 终止前台进程
Ctrl+Z                      # 挂起前台进程
nohup ./app.sh > app.log 2>&1 &   # 后台运行且关终端不断
```

## 2.2 性能排查四件套

```bash
# ① CPU / 负载
uptime                      # 1/5/15 分钟平均负载（>CPU 核数即为过载）
lscpu                       # 核数、架构
vmstat 1 5                  # 每秒采样，共 5 次（r=可运行队列, b=阻塞, us/sy=CPU 占比）
mpstat -P ALL 1             # 每核利用率（sysstat 包）
top -Hp <PID>               # 看某进程内各线程 CPU

# ② 内存
free -h                     # 重点看 available，不是 free
cat /proc/meminfo           # 明细
smem -rk | head             # 按 PSS 排序（更真实）
vmstat 1                    # si/so 不为 0 说明在 swap

# ③ 磁盘 IO
iostat -xz 1                # %util 接近 100% 说明磁盘打满
iotop                      # 谁在读盘（需安装）
du -sh * | sort -rh | head  # 大文件定位

# ④ 句柄/连接数
lsof | wc -l                # 系统打开句柄总数
lsof -p <PID> | wc -l       # 单进程句柄数（接近 ulimit 就会报 EMFILE）
ls /proc/<PID>/fd | wc -l   # 不装 lsof 的替代法
ulimit -n                   # 当前进程句柄上限
cat /proc/sys/fs/file-nr    # 系统句柄使用情况
```

## 2.3 服务管理（systemd）

```bash
systemctl status nginx              # 状态
systemctl start|stop|restart nginx
systemctl reload nginx              # 不中断服务地重载配置
systemctl enable nginx              # 开机自启
systemctl disable nginx
systemctl list-units --type=service --state=running   # 所有运行中服务
journalctl -u nginx -f             # 跟踪服务日志
journalctl -u nginx --since "10 min ago"
journalctl -p err -b               # 本次启动的错误级日志
dmesg -T | tail -50                 # 内核日志（-T 转可读时间）
```

---

# 三、网络诊断

```bash
ip addr                     # 查看 IP（新命令，取代 ifconfig）
ip route                    # 路由表
ip -s link                  # 接口收发包统计
ss -tulnp                   # 端口监听（-t TCP -u UDP -l listening -n 数字 -p 进程）
ss -s                       # 连接统计概览
netstat -tulnp              # 老命令，部分系统已移除

ping -c 4 8.8.8.8           # 连通性 + 丢包
mtr 8.8.8.8                 # 逐跳延迟+丢包（排查链路问题神器）
traceroute -n 8.8.8.8       # 路径追踪
dig example.com             # DNS 解析（看 A/CNAME/耗时）
nslookup example.com
host example.com
curl -I https://example.com         # 只看响应头
curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" https://example.com
wget -c https://example.com/a.iso  # 断点续传下载
nc -zv 1.2.3.4 6379         # 探测端口是否通（z 不发数据，v 详细）
telnet 1.2.3.4 3306         # 老式端口探测
tcpdump -i eth0 port 80 -w /tmp/a.pcap   # 抓包

lsof -i :8080               # 谁占用了 8080
lsof -i -P -n | grep LISTEN
```

**排查顺序口诀**：先 `ping` 通不通 → 再 `nc -zv` 端口通不通 → 再 `curl` 看 HTTP 状态码 → 最后 `tcpdump` 抓包。

---

# 四、文本处理

## 4.1 grep / sed / awk 三件套

```bash
# grep：找内容
grep -rn "ERROR" /var/log/app/        # 递归 + 显示行号
grep -i "error" file                  # 忽略大小写
grep -v "^#" config.conf              # 反向：排除注释行
grep -E "ERROR|WARN" app.log          # 多模式（正则）
grep -A 3 -B 3 "Exception" app.log    # 命中行 前后各 3 行
grep -c "200" access.log              # 只统计条数
grep -l "TODO" -r src/                # 只列文件名

# sed：流式替换/删除/插入
sed -n '10,20p' file                  # 打印 10~20 行
sed -i 's/foo/bar/g' file             # 原地替换（-i 直接改文件）
sed -i.bak 's/foo/bar/g' file         # 改前备份 file.bak（推荐）
sed -i '/^$/d' file                   # 删除空行
sed -n '/start/,/end/p' file          # 打印两个标记之间的内容
sed -i '5i INSERTED' file             # 第 5 行前插入

# awk：按列处理
awk '{print $1, $NF}' file            # 第 1 列与最后一列
awk -F, '$3 > 100 {print $1}' a.csv   # 第 3 列 >100 时打印第 1 列
awk '{sum+=$1} END {print sum}' num   # 求和
awk 'length($0)>80' file              # 超过 80 字符的行
awk '!seen[$0]++' file                # 去重（保留首次出现）
```

## 4.2 管道、重定向与常用组合

```bash
cmd > out.txt               # 标准输出覆盖
cmd >> out.txt              # 标准输出追加
cmd 2> err.txt              # 标准错误单独存
cmd > all.txt 2>&1          # 标准输出和错误合并（最常用）
cmd 2>/dev/null             # 丢弃错误输出
cmd &>/dev/null             # 全部丢弃
cat file | grep x | wc -l   # 管道串联
cmd | tee out.txt           # 既显示又落盘
cmd < input.txt             # 从文件读入

# 组合技
grep -rn "def " src/ | wc -l                    # 统计函数数量
ps aux | awk '{print $11}' | sort | uniq -c | sort -rn | head
ls -lt | head -n 1                              # 最新文件
tar -czf - dir/ | ssh user@host "cat > /tmp/d.tar.gz"   # 边打包边传
```

## 4.3 vim 最低生存操作

```
i            进入编辑模式
Esc          回到普通模式
:w           保存
:q           退出
:wq 或 :x    保存并退出
:q!          不保存强制退出
dd           删除当前行
yy / p       复制 / 粘贴
u  / Ctrl+r  撤销 / 重做
/text        向下搜索，n 下一个，N 上一个
:%s/old/new/g      全文替换
:%s/old/new/gc     逐个确认替换
gg / G       跳到文件头 / 尾
:set nu      显示行号
```

---

# 五、用户·权限·系统管理

```bash
whoami                      # 我是谁
id                          # UID/GID/所属组
groups                      # 所属组
su -                        # 切到 root（加载 root 环境变量）
su - bob                    # 切到 bob
sudo -i                     # 以 root 开一个 shell
sudo -u www-data ls /var/www   # 以指定用户执行单条命令

useradd -m -s /bin/bash bob     # 建用户并建家目录
passwd bob                      # 改密码
usermod -aG sudo bob            # 加入 sudo 组（-a 追加，别漏）
userdel -r bob                  # 删用户及家目录

# 包管理
apt update && apt upgrade -y            # Debian/Ubuntu
apt install -y nginx
apt remove --purge nginx
apt autoremove -y                       # 清理无用依赖
apt search nginx                        # 搜索
dpkg -l | grep nginx                    # 查已装包

dnf install -y nginx                    # CentOS/RHEL 8+
yum install -y nginx                    # 老 RHEL
rpm -qa | grep nginx
pacman -Syu                             # Arch

# 系统信息
uname -a                                # 内核版本+架构
hostnamectl                             # 主机名/系统版本
lsb_release -a                          # 发行版
cat /etc/os-release                     # 通用发行版信息
date                                    # 时间
timedatectl status                      # 时区与 NTP 状态
```

---

# 六、打包与传输

```bash
# tar（Linux 首选）
tar -czvf a.tar.gz dir/         # 打包并 gzip 压缩
tar -xzvf a.tar.gz              # 解压
tar -xzvf a.tar.gz -C /tmp      # 解压到指定目录
tar -tzvf a.tar.gz | head       # 只看不解
tar -czvf - dir/ | ssh u@h "tar -xzvf - -C /opt"   # 打包直传

# zip
zip -r a.zip dir/               # 递归打包
unzip -l a.zip                  # 看内容
unzip a.zip -d /tmp/out         # 解压到目录

gzip -k file                    # 压缩并保留原文件
gunzip file.gz

# 传输
scp file user@host:/tmp/        # 单文件
scp -r dir/ user@host:/opt/     # 目录
rsync -avzP dir/ user@host:/opt/   # 增量同步，断点续传，最推荐
rsync -avz --delete src/ dst/   # 镜像同步（dst 多余文件会被删，谨慎）
sftp user@host                  # 交互式传输
```

---

# 七、Docker 镜像

```bash
docker images                   # 本地镜像列表
docker images -a                # 含中间层
docker image ls

docker pull nginx:1.25           # 拉取指定版本（别用 latest 上生产）
docker pull registry.example.com/app:1.2.0
docker push registry.example.com/app:1.2.0
docker tag app:1.2.0 registry.example.com/app:1.2.0   # 打标签

docker build -t app:1.2.0 .              # 用当前目录 Dockerfile 构建
docker build -t app:latest -f Dockerfile.prod .
docker build --no-cache -t app:1.2.0 .  # 忽略缓存（调试用）
docker build --platform linux/amd64 -t app:amd64 .   # 指定平台

docker rmi app:1.0               # 删除镜像
docker rmi $(docker images -f dangling=true -q)     # 删所有悬空镜像
docker image prune -a            # 清理未使用镜像（小心）

docker history app:1.2.0         # 看每层构建历史（排查体积）
docker inspect app:1.2.0         # 镜像元数据（层、环境变量、入口）
docker save app:1.2.0 -o app.tar         # 导出为 tar（离线迁移）
docker load -i app.tar                   # 导入
```

---

# 八、Docker 容器

## 8.1 运行容器

```bash
docker run -d \                 # -d 后台运行
  --name web \                  # 命名（必须唯一，否则冲突）
  -p 8080:80 \                  # 宿主机 8080 → 容器 80
  -v /data/nginx:/usr/share/nginx/html \   # 挂载目录
  -e TZ=Asia/Shanghai \         # 环境变量
  --restart unless-stopped \     # 重启策略
  --network app-net \           # 指定网络
  --memory 512m --cpus 1.5 \    # 资源限制
  --log-opt max-size=10m --log-opt max-file=3 \  # 日志轮转
  nginx:1.25

# 常用一次性容器
docker run --rm -it alpine sh           # --rm 退出即删，进去排查
docker run --rm -v $PWD:/data -w /data alpine ls -la   # 挂当前目录执行命令
docker run -it ubuntu:24.04 bash        # 交互式（不加 -d）
docker run --entrypoint sh app:1.2.0    # 覆盖入口（进自己镜像里看）
```

## 8.2 容器生命周期

```bash
docker ps                       # 运行中的容器
docker ps -a                     # 全部（含已退出）
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

docker start|stop|restart <容器>
docker kill <容器>               # 强杀
docker pause / unpause <容器>    # 暂停（冻结进程）
docker rm <容器>
docker rm $(docker ps -aq)       # 删所有容器（危险）

docker exec -it <容器> bash      # 进容器（容器内要有 bash，alpine 用 sh）
docker exec <容器> env            # 看容器环境变量
docker exec <容器> sh -c "ls -la /app"

docker logs <容器>                # 看日志
docker logs -f --tail 200 <容器>  # 实时 + 只看最后 200 行
docker logs --since 10m <容器>
docker attach <容器>              # 附着到主进程（Ctrl+C 会停容器，慎用）

docker inspect <容器>             # 完整元数据（IP、挂载、重启次数）
docker inspect -f '{{.State.ExitCode}}' <容器>       # 只看退出码
docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' <容器>
docker top <容器>                 # 容器内进程
docker stats                      # 实时资源占用
docker stats <容器> --no-stream   # 采样一次（写脚本用）
docker diff <容器>                # 容器层改了哪些文件
docker cp <容器>:/app/log.txt ./  # 从容器拷文件出来
docker cp ./app.conf <容器>:/etc/ # 拷进去
docker commit <容器> app:debug    # 把现场保存成镜像（临时救急，别当流程）
docker rename <容器> new-name
```

---

# 九、Docker 网络与存储

## 9.1 网络

```bash
docker network ls
docker network create app-net                     # 默认 bridge
docker network create --subnet 172.28.0.0/16 app-net
docker network inspect app-net                    # 看连了哪些容器
docker network rm app-net

# 三种内置模式
# bridge（默认，容器间可通过 IP 通信）
# host（直接用宿主机网络，性能最好，无端口映射）
# none（完全隔离）
docker run -d --network host nginx
docker run -d --network container:<容器> nginx     # 共享另一个容器的网络栈
docker run -d --add-host db:10.0.0.5 nginx        # 手动加 hosts
```

> ⚠️ 同一自定义 bridge 网络内的容器**可以用容器名互相解析**；默认 bridge 不行，只能靠 IP。所以微服务请用 `docker network create`。

## 9.2 数据卷

```bash
docker volume ls
docker volume create pgdata
docker volume inspect pgdata            # Mountpoint 在宿主机哪里
docker volume rm pgdata

docker run -d -v pgdata:/var/lib/postgresql/data postgres:16
docker run -d -v /host/path:/container/path:ro nginx   # :ro 只读挂载

# --mount 写法更明确（推荐用于脚本）
docker run -d --mount type=volume,src=pgdata,dst=/var/lib/postgresql/data postgres:16

# 清理未被任何容器使用的卷（危险：会删掉"暂时没挂"的数据）
docker volume prune
```

> 经验：**有状态服务一律用 named volume**，别用 bind mount 到某个具体路径——换机器/换用户时路径权限最坑人。

---

# 十、Dockerfile 编写

## 10.1 常用指令

```dockerfile
FROM nginx:1.25-alpine AS base        # 多阶段构建第一阶段
FROM golang:1.23 AS builder           # 构建阶段
WORKDIR /build                        # 后续指令的当前目录
COPY go.mod go.sum ./                 # 先拷依赖清单，利用层缓存
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /out/app

FROM alpine:3.20                      # 最终镜像（只留产物，体积小）
RUN apk add --no-cache ca-certificates tzdata   # 最小化安装
ENV TZ=Asia/Shanghai \
    APP_PORT=8080
ARG BUILD_VERSION=dev                 # 构建期变量（不会留在运行环境）
WORKDIR /app
COPY --from=builder /out/app /app/app # 跨阶段拷贝
USER 10001                            # 别用 root 跑
EXPOSE 8080                           # 声明端口（仅文档作用）
HEALTHCHECK --interval=30s --timeout=3s \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["/app/app"]               # 固定入口（不可被 run 覆盖）
CMD ["--config", "/etc/app.yaml"]     # 默认参数（可被 run 覆盖）
```

## 10.2 编写要点

| 要点 | 说明 |
|---|---|
| **层缓存** | 变化少的指令放前面：先 `COPY` 依赖清单 → `RUN install` → 再 `COPY` 代码 |
| **合并 RUN** | 多条 `RUN` 用 `&&` 串起来，减少层数与体积 |
| **`.dockerignore`** | 必须写：`node_modules` `.git` `*.log` `dist` `.venv` `__pycache__` |
| **不用 `latest`** | 基础镜像锁定版本，保证构建可复现 |
| **非 root 运行** | `USER` 指令，配合镜像里的低权用户 |
| **多阶段构建** | 编译产物拷到精简基础镜像，体积可降一个数量级 |
| **ENTRYPOINT vs CMD** | ENTRYPOINT 是固定程序，CMD 是默认参数；二者可组合 |
| **ADD vs COPY** | 优先 `COPY`；`ADD` 会解压 tar、支持 URL，行为隐式，容易踩坑 |

---

# 十一、Docker Compose

```bash
docker compose up -d              # 后台启动全部服务
docker compose up -d --build      # 先构建再启动
docker compose up -d --force-recreate   # 强制重建容器
docker compose ps                 # 服务状态
docker compose logs -f app        # 跟踪某服务日志
docker compose logs --tail 200 -f
docker compose exec app bash      # 进服务容器
docker compose restart app
docker compose stop / start
docker compose down               # 停并删容器（保留卷）
docker compose down -v            # 连卷一起删（危险：数据没了）
docker compose pull               # 拉最新镜像
docker compose build --no-cache
docker compose config             # 校验并展开最终配置
docker compose -f docker-compose.prod.yml up -d
```

**最小示例**：

```yaml
services:
  web:
    image: nginx:1.25-alpine
    container_name: web
    restart: unless-stopped
    ports:
      - "8080:80"
    volumes:
      - ./html:/usr/share/nginx/html:ro
    networks:
      - app-net
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://127.0.0.1/"]
      interval: 30s
      timeout: 3s
      retries: 3

  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD_FILE: /run/secrets/db_pass   # 密码走 secret，别写明文
      TZ: Asia/Shanghai
    volumes:
      - pgdata:/var/lib/postgresql/data
    networks:
      - app-net

volumes:
  pgdata:

networks:
  app-net:
```

---

# 十二、Docker 排查与清理

## 12.1 容器起不来 / 行为异常

```bash
docker ps -a | grep <容器>                # 先看状态：Exited / Restarting / Created
docker logs --tail 100 <容器>              # 退出码非 0 时先看日志
docker inspect -f '{{.State.ExitCode}} {{.State.Error}}' <容器>

# 容器一直在重启
docker inspect -f '{{.RestartCount}}' <容器>     # 重启次数
docker logs --previous <容器>                   # 看上一次挂掉的日志

# 进容器里查
docker exec -it <容器> sh
docker exec <容器> ps aux
docker exec <容器> netstat -tulnp 2>/dev/null || docker exec <容器> ss -tulnp
docker exec <容器> df -h
docker exec <容器> cat /etc/hosts

# 端口冲突
ss -tulnp | grep 8080                  # 宿主机谁占了
docker port <容器>                     # 看容器映射了哪些端口

# 资源问题
docker stats <容器> --no-stream
docker inspect -f '{{.HostConfig.Memory}}' <容器>   # 0 表示未限制
```

## 12.2 镜像/构建问题

```bash
docker system df                       # 磁盘占用总览（镜像/容器/卷/构建缓存）
docker system df -v                    # 明细
docker build --progress=plain -t app . # 看完整构建输出（CI 定位问题）
docker history --no-trunc app:1.0      # 每层完整命令
```

## 12.3 磁盘清理（按危险程度递增）

```bash
docker container prune          # 清理已停止的容器
docker image prune              # 清理悬空（dangling）镜像，安全
docker volume prune             # 清理未使用的卷 ⚠️ 可能删掉暂时没挂的数据
docker network prune            # 清理未使用的网络
docker builder prune            # 清理构建缓存
docker system prune             # 上面几项一起做 ⚠️
docker system prune -a --volumes  # 连未使用的镜像和卷都删 ⚠️⚠️ 生产慎用
```

## 12.4 常见问题速查

| 报错 | 原因 / 解法 |
|---|---|
| `permission denied ... /var/run/docker.sock` | 当前用户不在 docker 组：`sudo usermod -aG docker $USER` 后重新登录 |
| `port is already allocated` | 宿主机端口被占：`ss -tulnp \| grep 8080` 或改映射端口 |
| `No space left on device` | 磁盘满：`docker system df` → `docker system prune` |
| `image not found` / `manifest unknown` | 版本写错，或私有仓库未 `docker login` |
| `container name already in use` | 同名容器存在：`docker rm` 旧的或改名 |
| `OCI runtime create failed` | 常见于镜像架构不匹配（arm 镜像跑 amd64）或入口命令不存在 |
| 容器内无法解析域名 | 检查 `--dns` 或 `/etc/resolv.conf`；自定义网络内可用容器名 |
| 容器时间不对 | 加 `-e TZ=Asia/Shanghai` 或挂 `/etc/localtime` |

---

# 十三、高频场景速查

| 场景 | 命令 |
|---|---|
| 一键起一个临时数据库调试 | `docker run --rm -it -e MYSQL_ROOT_PASSWORD=123456 -p 3306:3306 mysql:8` |
| 把本地目录当容器工作区 | `docker run --rm -it -v $PWD:/w -w /w python:3.13 bash` |
| 查容器实际监听端口 | `docker port <容器>` / `docker inspect --format='{{json .NetworkSettings.Ports}}' <容器>` |
| 批量停掉某项目的容器 | `docker ps -a --filter name=myapp -q \| xargs -r docker stop` |
| 批量删退出码非 0 的容器 | `docker ps -aq --filter status=exited \| xargs -r docker rm` |
| 看某个镜像会跑什么 | `docker inspect --format='{{json .Config.Cmd}} {{json .Config.Entrypoint}}' <镜像>` |
| 容器内装临时工具（alpine） | `docker exec -it <容器> sh -c "apk add --no-cache curl"` |
| 备份一个卷的数据 | `docker run --rm -v pgdata:/src -v $PWD:/dst alpine tar -czf /dst/pgdata.tar.gz -C /src .` |
| 恢复卷数据 | 反向操作，先解压到 `/src` |
| 进宿主机的网络命名空间排查 | `docker run --rm --network host nicolaka/netshoot` |
| 跟踪某容器所有日志 | `docker logs -f --since 5m <容器> 2>&1 \| grep -i error` |
| 看容器重启原因 | `docker inspect -f '{{.State.OOMKilled}} {{.State.ExitCode}}' <容器>` |
| Linux 下查大文件 | `du -ah / 2>/dev/null \| sort -rh \| head -20` |
| Linux 下查端口占用 | `ss -tulnp \| grep :80` |
| Linux 下抓某个进程的调用 | `strace -p <PID> -c`（需安装） |
| Linux 下定时任务 | `crontab -e`，`crontab -l`，`systemctl status cron` |

---

# 十四、安全红线

```bash
# 以下命令会"没有后悔药"，执行前务必确认路径与影响范围
rm -rf /                     # 删根目录
rm -rf $VAR/                 # VAR 为空时等价于 rm -rf /
chmod -R 777 /               # 全盘可写
chown -R root:root /home     # 家目录权限全乱
mkfs.ext4 /dev/sda           # 格式化磁盘
dd if=/dev/zero of=/dev/sda # 清零磁盘
iptables -F                  # 清空防火墙规则
docker system prune -a --volumes   # 删镜像和卷
docker compose down -v       # 删卷，数据不可恢复
kill -9 1                    # 杀 init（容器/系统可能直接挂）
> /dev/sda                   # 覆盖磁盘
```

**三条保命习惯**：

1. `rm -rf` 前先 `ls` 一遍目标路径，或先跑一次去掉 `rm` 的版本看会删什么。
2. 危险命令用 `echo` 前缀预演：`echo rm -rf $VAR/`，确认输出无误再去掉 `echo`。
3. 生产环境操作前先备份：`docker inspect` 存一份、数据库先 dump、配置文件先 `cp` 一份。

---

*最后更新：2025 年 · 适用于 Ubuntu/Debian/CentOS/RHEL 及 Docker Engine 24+ / Compose v2*
