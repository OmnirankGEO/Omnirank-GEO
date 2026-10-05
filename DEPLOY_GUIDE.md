# OmniRank AI 部署指南

## 一、服务器要求

| 项目 | 最低配置 | 推荐配置 |
|------|---------|---------|
| CPU | 2核 | 4核 |
| 内存 | 4GB | 8GB |
| 硬盘 | 40GB SSD | 80GB SSD |
| 系统 | Ubuntu 20.04+ / CentOS 8+ / Debian 11+ | Ubuntu 22.04 LTS |
| Docker | Docker 24+ & Docker Compose V2 | 最新稳定版 |

需要开放的端口：**80**（HTTP）、**443**（HTTPS，如配置域名）

---

## 二、安装 Docker

如果服务器还没装 Docker：

```bash
# Ubuntu / Debian
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# 退出重新登录使 docker 组生效
```

验证安装：
```bash
docker --version        # Docker 24+
docker compose version  # Docker Compose V2
```

---

## 三、上传项目文件

将本地 `geo_agentscope` 整个文件夹上传到服务器，例如放在 `/opt/omnirank/`：

```bash
# 方式1：scp 上传（本地执行）
scp -r geo_agentscope/ root@你的服务器IP:/opt/omnirank/

# 方式2：已有 Git 仓库
cd /opt/omnirank
git clone <你的仓库地址> geo_agentscope
```

上传后服务器目录结构应为：
```
/opt/omnirank/geo_agentscope/
├── Dockerfile
├── docker-compose.yml
├── nginx.conf
├── start.sh
├── server.py
├── requirements-docker.txt
├── package.json
├── .env.example
├── api/
├── tools/
├── frontend/
│   ├── src/
│   ├── package.json
│   └── ...
└── ...
```

---

## 四、配置环境变量

```bash
cd /opt/omnirank/geo_agentscope

# 从模板创建 .env 文件
cp .env.example .env

# 编辑 .env，填入实际的 API Key
nano .env
```

**必须配置的项：**

```bash
# 核心 LLM（至少配一个）
DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxx

# 数据服务（报价系统依赖）
METASO_API_KEY=xxxxxxxxxxxxxxxx
API_5118_LONGTAIL_KEY=xxxxxxxxxxxxxxxx
API_5118_SEARCH_VOLUME_KEY=xxxxxxxxxxxxxxxx

# 数据库密码（建议修改默认值）
POSTGRES_PASSWORD=你的安全密码

# JWT 密钥（建议设置，不设则自动生成）
JWT_SECRET=你的随机字符串
```

**可选配置：**

```bash
# 其他 LLM 服务商（按需）
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
OPENROUTER_API_KEY=sk-xxxxxxxxxxxxxxxx
KIMI_API_KEY=xxxxxxxxxxxxxxxx

# 社媒数据
TIKHUB_API_KEY=xxxxxxxxxxxxxxxx

# 跨域配置（绑定域名后）
ALLOWED_ORIGINS=https://yourdomain.com

# Worker 数量（保持1，WebSocket 需要单进程）
WORKERS=1
```

---

## 五、启动服务

```bash
cd /opt/omnirank/geo_agentscope

# 构建并启动（首次约5-10分钟）
docker compose up -d --build
```

首次构建会依次执行：
1. 拉取基础镜像（node:20-alpine + python:3.12-slim + pgvector:pg16）
2. 构建前端（npm ci + npm run build）
3. 安装 Python 依赖（pip install）
4. 安装 Node.js 工具依赖（pptxgenjs, playwright 等）
5. 安装 Playwright Chromium 浏览器
6. 启动 PostgreSQL → 启动应用服务

**查看启动日志：**
```bash
# 实时查看所有日志
docker compose logs -f

# 只看应用日志
docker compose logs -f omnirank

# 只看数据库日志
docker compose logs -f db
```

**确认启动成功：**
```bash
# 检查容器状态（两个容器都应为 Up + healthy）
docker compose ps

# 预期输出：
# omnirank-ai   Up (healthy)   0.0.0.0:80->80/tcp
# omnirank-db   Up (healthy)   0.0.0.0:5432->5432/tcp
```

启动成功后访问：
- 前端界面：`http://服务器IP`
- API 文档：`http://服务器IP/docs`

---

## 六、首次使用

1. 打开 `http://服务器IP`，系统会引导注册管理员账号
2. 登录后进入后台，在「设置」中确认 API Key 已正确配置
3. 创建第一个客户诊断，测试完整流程

---

## 七、域名 + HTTPS（生产推荐）

如果要绑定域名和 HTTPS，推荐在 Docker 前面加一层 Nginx 反代：

```bash
# 安装 Nginx 和 Certbot
apt install nginx certbot python3-certbot-nginx -y

# 创建 Nginx 站点配置
nano /etc/nginx/sites-available/omnirank
```

配置内容：
```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 600s;
        client_max_body_size 50M;
    }
}
```

```bash
# 启用站点
ln -s /etc/nginx/sites-available/omnirank /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# 申请 SSL 证书（自动配置 HTTPS）
certbot --nginx -d yourdomain.com
```

绑定域名后，更新 `.env`：
```bash
ALLOWED_ORIGINS=https://yourdomain.com
```

更新 docker-compose.yml，将 80 端口改为仅监听本地：
```yaml
    ports:
      - "127.0.0.1:80:80"
```

然后重启：
```bash
docker compose restart omnirank
```

---

## 八、日常运维

### 查看日志
```bash
docker compose logs -f omnirank --tail 100
```

### 重启服务
```bash
docker compose restart omnirank
```

### 更新代码部署
```bash
cd /opt/omnirank/geo_agentscope

# 1. 上传/拉取新代码
git pull   # 如果用Git

# 2. 重新构建并启动
docker compose up -d --build
```

### 热更新（不重新构建镜像，仅替换文件）
```bash
# 适合紧急修复单个文件，无需全量rebuild
docker cp server.py omnirank-ai:/app/server.py
docker restart omnirank-ai
```

### 数据库备份
```bash
# 备份
docker exec omnirank-db pg_dump -U geo_admin geo_agentscope > backup_$(date +%Y%m%d).sql

# 恢复
docker exec -i omnirank-db psql -U geo_admin geo_agentscope < backup_20260324.sql
```

### 查看磁盘占用
```bash
docker system df
du -sh data/ output/ cache/ logs/
```

### 清理构建缓存
```bash
docker builder prune -f
docker image prune -f
```

---

## 九、数据持久化说明

以下目录通过 Docker Volume 挂载，容器删除/重建不会丢失数据：

| 容器路径 | 宿主机路径 | 内容 |
|---------|-----------|------|
| `/var/lib/postgresql/data` | Docker Volume `pgdata` | PostgreSQL 数据库 |
| `/app/data` | `./data` | 应用数据 |
| `/app/output` | `./output` | 生成的文章/报告 |
| `/app/cache` | `./cache` | 缓存文件 |
| `/app/logs` | `./logs` | 运行日志 |
| `/app/config` | `./config` | 运行时配置 |

---

## 十、故障排查

### 容器启动失败
```bash
# 看完整日志
docker compose logs omnirank | tail -50

# 常见原因：
# - .env 文件不存在或 API Key 为空
# - 端口 80 被占用（lsof -i:80）
# - 内存不足（free -h）
```

### 数据库连接失败
```bash
# 检查数据库容器状态
docker compose ps db
docker compose logs db | tail -20

# 手动测试连接
docker exec omnirank-ai python -c "
import psycopg2, os
conn = psycopg2.connect(os.environ['DATABASE_URL'])
print('数据库连接正常')
conn.close()
"
```

### 前端白屏
```bash
# 检查前端文件是否存在
docker exec omnirank-ai ls /app/frontend/dist/index.html

# 检查 Nginx 状态
docker exec omnirank-ai nginx -t
```

### PDF 下载乱码（方框）
```bash
# 检查中文字体
docker exec omnirank-ai ls /usr/share/fonts/truetype/wqy/

# 应该看到 wqy-zenhei.ttc，如果没有：
docker exec omnirank-ai apt-get update && apt-get install -y fonts-wqy-zenhei
docker restart omnirank-ai
```

### Python 包缺失
```bash
# 进入容器安装
docker exec omnirank-ai pip install 包名
docker restart omnirank-ai
```

---

## 十一、安全建议

1. **修改默认数据库密码**：`.env` 中 `POSTGRES_PASSWORD` 务必改掉默认值
2. **设置 JWT_SECRET**：不设置则每次重启后所有用户需重新登录
3. **关闭调试端口**：生产环境注释掉 docker-compose.yml 中 `5432:5432` 端口映射
4. **启用 HTTPS**：绑定域名后使用 Certbot 配置 SSL
5. **定期备份**：数据库 + `data/` + `output/` + `config/` 目录
6. **防火墙**：只开放 80/443 端口，其余全部关闭
