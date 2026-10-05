# 在本机部署 OmniRank

你是协助用户安装这个仓库的 AI。按下面顺序完成部署和登录验收；步骤来自 README 的快速开始。只在本机操作，完成后告诉用户登录地址和管理员口令保存位置。

## 操作边界

- 不提交或展示 `.env`、管理员口令、API Key、JWT 或登录响应中的 token。日志和最终回复也不能含这些值。
- 所有宿主端口只绑定 `127.0.0.1`，不对公网开端口。
- 不运行 `docker compose down -v`，不删数据卷，不做 Docker 全局 prune，不停止、重建或删除其他项目的容器、卷、网络、镜像。
- 为本次部署选独立的 Compose 项目名、容器名、卷名、网络名和应用镜像名。只设置 `-p` 不够：原编排还定义了固定名称。
- 不切换仓库分支，不提交配置，不改 `docker-compose.yml`。本机调整写进未提交的 `docker-compose.override.yml`。
- 部署失败时先记录原始错误并检查环境和配置；不要删除断言、跳过构建检查或改应用源码来凑出成功结果。需要源码修复时将具体输入和错误交给维护者，修复后重新部署。

## 1. 检查环境和既有资源

1. 运行 `docker version`、`docker compose version`、`docker info`。Docker Engine 至少 24；Compose 至少 2.24.4，因为下面使用 `!override` 替换端口列表。Docker Compose 5 也满足要求。
2. Docker 可用内存至少 4 GB；构建和镜像存储建议预留 20 GB。检查工作目录所在盘与 Docker Desktop 虚拟磁盘所在盘的可用空间；不能只看容器内的逻辑磁盘空间。若不足，记录空间与已有镜像缓存，构建期间留意剩余空间；不要替用户清理其他项目。
3. 记录 `docker ps -a`、`docker volume ls`、`docker image ls`、`docker network ls` 的部署前名单。本次不复用已有数据库卷。
4. 检查 8001 端口是否占用。Windows 可用 `Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue`；macOS/Linux 可用 `lsof -iTCP:8001 -sTCP:LISTEN`。被占用时选另一个空闲端口，不停止占用它的进程。
5. Windows/macOS 先启动 Docker Desktop，切到 Linux containers，等待 Engine 可用；Windows 建议启用 WSL 2 后端，macOS 注意给 Docker VM 分配足够内存。允许 Docker Desktop 访问当前工作目录，否则 bind mount 会失败。Linux 需有 Docker daemon 使用权限。
6. 不需要在宿主安装 Node.js 或 Python 依赖，应用依赖在镜像里构建。宿主若已有 Python，可以用来生成配置及做 HTTP 验收；否则用当前系统的 PowerShell、openssl 或 curl 等工具完成同等操作。

## 2. 配置 `.env`

从 `.env.example` 复制为 `.env`。用当前系统的加密随机数工具生成并写入以下值，不把值打印到终端：

| 项 | 配置 |
|---|---|
| `POSTGRES_PASSWORD` | 32 字节随机数的十六进制串，避免 URL 特殊字符影响数据库连接 |
| `JWT_SECRET` | 独立的 32 字节随机十六进制串；模板中的该项是注释，需要启用 |
| `ADMIN_INITIAL_PASSWORD` | 独立随机口令，至少 12 位；显式设置后不用从首次启动输出找随机口令 |
| `COMPOSE_PROJECT_NAME` | 本次独有的小写名称，例如 `omnirank_local_` 加随机后缀 |
| `OMNIRANK_COMPOSE_PROJECT_NAME` | 与 `COMPOSE_PROJECT_NAME` 相同 |
| `OMNIRANK_HTTP_PORT` | 本次选中的空闲端口，默认 8001 |
| `OMNIRANK_DATABASE_NAME` | 普通本机安装用 `geo_agentscope`；临时部署验收用包含 `test` 的名字 |

配置值只写进本机 `.env`；删掉模板里的同名有效项再写入，不留下互相覆盖的重复项。不要添加空的 `KEY=`：需要缺省值时删除或注释对应行；不配置外部大模型 Key 也能启动和登录。不要自行买 Key、启用付费调用或接真实发布渠道。

若进程环境中提供 `OMNIRANK_DEPLOY_TEST=1`，这是临时验收环境：使用进程环境给出的 `COMPOSE_PROJECT_NAME`、`OMNIRANK_HTTP_PORT` 和 `OMNIRANK_DATABASE_NAME`，不能改用普通安装默认值；把它们写入 `.env`。测试数据库名必须包含 `test`。管理员口令仍自行随机生成并只保存在 `.env`。

## 3. 写隔离覆盖文件

将以下内容按 LF 保存到根目录 `docker-compose.override.yml`。它会自动与 `docker-compose.yml` 合并；以后每条命令都在这个仓库根目录执行。

```yaml
services:
  db:
    container_name: ${COMPOSE_PROJECT_NAME}-db
    environment:
      POSTGRES_DB: ${OMNIRANK_DATABASE_NAME:-geo_agentscope}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U geo_admin -d $${POSTGRES_DB}"]
  redis:
    container_name: ${COMPOSE_PROJECT_NAME}-redis
  pgbouncer:
    container_name: ${COMPOSE_PROJECT_NAME}-pgbouncer
    environment:
      DATABASE_URL: postgresql://geo_admin:${POSTGRES_PASSWORD}@db:5432/${OMNIRANK_DATABASE_NAME:-geo_agentscope}
  omnirank-blue:
    container_name: ${COMPOSE_PROJECT_NAME}-web
    image: ${COMPOSE_PROJECT_NAME}-omnirank-blue
    ports: !override
      - "127.0.0.1:${OMNIRANK_HTTP_PORT:-8001}:80"
    environment:
      DATABASE_URL: postgresql://geo_admin:${POSTGRES_PASSWORD}@db:5432/${OMNIRANK_DATABASE_NAME:-geo_agentscope}
  omnirank-green:
    container_name: ${COMPOSE_PROJECT_NAME}-green
    image: ${COMPOSE_PROJECT_NAME}-omnirank-green
    ports: !override []
    environment:
      DATABASE_URL: postgresql://geo_admin:${POSTGRES_PASSWORD}@db:5432/${OMNIRANK_DATABASE_NAME:-geo_agentscope}
  omnirank-cron-blue:
    container_name: ${COMPOSE_PROJECT_NAME}-cron
    image: ${COMPOSE_PROJECT_NAME}-omnirank-blue
    environment:
      DATABASE_URL: postgresql://geo_admin:${POSTGRES_PASSWORD}@db:5432/${OMNIRANK_DATABASE_NAME:-geo_agentscope}
  omnirank-cron-green:
    container_name: ${COMPOSE_PROJECT_NAME}-cron-green
    image: ${COMPOSE_PROJECT_NAME}-omnirank-green
    environment:
      DATABASE_URL: postgresql://geo_admin:${POSTGRES_PASSWORD}@db:5432/${OMNIRANK_DATABASE_NAME:-geo_agentscope}
  wx-decrypt:
    container_name: ${COMPOSE_PROJECT_NAME}-wx-decrypt
    ports: !override []
networks:
  wx-decrypt-net:
    name: ${COMPOSE_PROJECT_NAME}_wx-decrypt-net
volumes:
  pgdata:
    name: ${COMPOSE_PROJECT_NAME}_pgdata
  redis_data:
    name: ${COMPOSE_PROJECT_NAME}_redis_data
```

先执行 `docker compose config --quiet`。需要检查有效配置时，在程序里读取 `docker compose config --format json`，只输出服务名称、容器名、镜像名、端口绑定、卷名和网络名；不要把完整配置写到日志，它含口令。确认：

- web 只有一个宿主端口，绑定 `127.0.0.1` 且等于选定端口；`!override` 没有把原来的 8001 一起留下。
- 数据库、Redis 没有宿主端口；所有名称都属于当前项目，没有引用部署前已有的数据卷。
- web 与 cron-blue 使用同一个本次项目镜像；数据库名称与 `.env` 一致。
- 该覆盖文件不会启动绿色实例、PgBouncer 或视频 sidecar：只运行下面点名的四个服务。不要使用不带服务名的 `docker compose up -d`。

## 4. 构建和启动

逐条执行，有失败先解决当前步骤，不跳过失败继续起应用：

```sh
docker compose up -d db redis
docker compose build omnirank-blue
docker compose run --rm -e ROLE=prestart omnirank-blue
docker compose up -d omnirank-blue
docker compose up -d omnirank-cron-blue
docker compose ps
```

首次构建可能十几分钟。先建库迁移，再启动 web 和 cron。不要省掉 cron，监测和发布结果回写需要它。预启动日志、构建日志可保存到本机未提交的文件；如应用输出含管理员口令，先脱敏再展示或写验收日志。

## 5. 验收登录

成功必须同时满足：

1. `docker compose ps` 中 db、redis、omnirank-blue、omnirank-cron-blue 均显示 running 和 healthy。cron 虽然不发布宿主端口，仍启动容器内的 API 并使用镜像原有的 HTTP 健康探针，不要关闭探针。
2. `http://127.0.0.1:<OMNIRANK_HTTP_PORT>/login` 返回登录页面，HTML 和引用的前端资源能加载；不能只验证后端 `/docs`。
3. 用 `.env` 中的初始管理员口令，以 `admin` 登录成功。可以通过网页或 `POST /api/auth/login` 验证；在程序内读取口令，不能把口令放到命令行参数或打印响应 body。记录 HTTP 状态和是否拿到 token，不能记录 token 本身。首次登录的强制改密提示属于正常结果。

首次登录若返回 HTTP 428、`detail.code=REGISTRATION_AGREEMENTS_REQUIRED`，表示口令已验证，需要完成页面上的协议确认。普通安装让用户在页面完成确认；`OMNIRANK_DEPLOY_TEST=1` 的临时验收账号可走同一流程完成测试：在程序内保留 `detail.agreement_session_token`，从 `detail.agreements` 按 `agreement_type` 找到 `user_terms` 与 `privacy` 的 `agreement_version`，再向 `POST /api/auth/registration-agreements/accept` 发送 `agreement_session_token`、`terms_accepted=true`、`privacy_accepted=true`、`terms_version` 和 `privacy_version`。确认接口应返回 200、`success=true` 和 token，然后重新登录应返回 200。只记录状态码和布尔结果，不输出任何凭证；不要直接写数据库来跳过此流程。

把服务状态、登录地址、登录 HTTP 状态、项目名和本次创建的资源名保存到本机 `.deployment-result.json`，不要保存密码或 token。最终回复告知用户登录地址、用户名 `admin`、口令在 `.env` 的 `ADMIN_INITIAL_PASSWORD`；不要把口令抄进回复。部署结束保留数据和服务，不主动删卷。

## 常见卡点

- Docker Desktop 未启动或切到 Windows containers：先让 Engine 和 Linux containers 可用，再执行 Compose。
- Compose 不认识 `!override`：升级到至少 2.24.4；不要改成普通列表，那会合并旧端口。不要尝试停止别的项目来抢端口。
- `.env` 的空值覆盖缺省值：删除或注释非必填空项，数据库口令和 JWT 使用刚生成的非空值。不要输出 `.env` 排错。
- Windows 换行：仓库 `.gitattributes` 为 `.sh`、Dockerfile、编排、Nginx 配置及前端源码/构建脚本固定 LF；遇到 `/bin/bash^M`、`bad interpreter`、`$'\r'` 或前端控制字符检查中的 CRLF 报错，先检查实际文件字节并转 LF，不要反复重启。规则更新不会自动重写已经检出的文件，新克隆按当前规则检出。
- 前端构建检查未通过：保留失败断言和 `passed/failed` 数。若提示样例标题与断言不一致，把涉及的文件和报错交给维护者；不要自行改测试或省掉 `npm run build` 中的检查。
- bind mount 报权限或找不到文件：确认 Docker Desktop 文件共享权限，以及命令运行在仓库根目录；不要拿其他项目的数据目录来替代。
- 镜像源慢：Dockerfile 当前使用阿里云 apt/PyPI 和 npmmirror。境外可将该 Dockerfile 中对应地址换为 Debian 官方源、`https://pypi.org/simple` 和 `https://registry.npmjs.org` 后重新构建；不要换基础镜像摘要或 Python/npm 依赖版本。记录实际换源内容。国内保留现有源；基础镜像拉取失败检查 Docker Desktop 的镜像/代理设置，不覆盖全机配置。
- web 不 healthy：检查本项目 db/redis 是否 healthy，再看本项目 web 日志，核对 prestart 是否成功；日志需脱敏。cron 反复退出时同样只检查本项目，不停其他容器。
- 管理员登录失败：确认本次数据库、prestart 返回码与 `.env`；不要重建或删数据库卷。初始口令只用于首次初始化，已有用户不会随 `.env` 改动自动改密。

运行中查看状态用 `docker compose ps`，临时停止用 `docker compose stop`。只有用户需要升级时才重新构建、执行 prestart 并启动服务。
