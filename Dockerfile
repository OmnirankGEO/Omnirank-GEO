# syntax=docker/dockerfile:1.4
# ============================================
# OmniRank AI - Docker 多阶段构建
# ============================================
# 使用方法：把本文件放在项目根目录（和 server.py 同级）
# 然后执行 docker compose up -d --build
#
# 2026-05-14 BuildKit cache mounts 优化(Deploy-CTO):
#   · pip / npm 缓存跨 build 持久化(在 BuildKit 缓存层 · 不进镜像)
#   · 每次 deploy 不再重下 200+ pip packages / 800+ npm packages
#   · 预期:从 ~11 min → ~3-5 min (省 ~6 min · 70% 速度提升)
# ============================================

# ---- Stage 1: 构建前端 ----
# [WO_307] 锁摘要:0913AE bake 实际用到的那份(索引/清单列表摘要,linux/amd64 子清单 afdf9821…,2026-04-15 建)。
#   升基础镜像 = 单独一小单,流程见 DEPLOY_CTO_SSOT「基础镜像升级」。preflight 5-f 会拦没锁的 FROM。
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS frontend-builder

# npm 使用淘宝镜像（中国加速）
RUN npm config set registry https://registry.npmmirror.com

WORKDIR /app/frontend

# 先复制 package 文件利用 Docker 缓存
COPY frontend/package.json frontend/package-lock.json ./
COPY frontend/patches ./patches

# 安装前端依赖 · BuildKit cache mount 让 npm 下载层级 cache 跨 build 持久化
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm ci --legacy-peer-deps --prefer-offline

# 复制前端源码并构建
# CACHEBUST 参数确保前端代码变更后 Docker 不用旧缓存(部署脚本传入 timestamp)
ARG CACHEBUST=0
COPY frontend/ ./
# Markdown parity is part of `npm run build` and reads the shared fixture from
# the repository root. Keep the production image build self-contained.
COPY tests/fixtures/markdown_parity /app/tests/fixtures/markdown_parity
# [返修单 T4 2026-07-29] 同一类依赖:frontend/src/lib/ownedImagePolicy.ts 静态 import
# ../../../config/owned_image_asset_policy.json(前后端同一份判定口径)。这一层只有
# frontend/ 与上面那份 fixture,少这个文件 tsc -b / vite build / markdown-parity-check
# 三处都解析不到而硬失败。
# 🔴 规则:前端代码新增任何 frontend/ 之外的静态 import,必须在这里同步补一条 COPY。
COPY config/owned_image_asset_policy.json /app/config/owned_image_asset_policy.json
# vite 的 deps pre-bundle 缓存放 node_modules/.vite · 加 cache mount 跨 build 复用
# 登录页页脚备案号(开源版):构建时读 VITE_ICP_BEIAN;docker-compose 从 .env 传进来,不设则不显示
ARG VITE_ICP_BEIAN=
ENV VITE_ICP_BEIAN=${VITE_ICP_BEIAN}
RUN --mount=type=cache,target=/app/frontend/node_modules/.vite,sharing=locked \
    npm run build


# ---- Stage 2: 运行环境 ----
# [WO_307] 锁摘要:0913AE bake 构建日志原文里的那份(索引/清单列表摘要,linux/amd64 子清单 44ff437b…,Python 3.12.14 · Debian 13.7)。
#   升基础镜像 = 单独一小单,流程见 DEPLOY_CTO_SSOT「基础镜像升级」(含 PDF 冒烟:系统 chromium 随基础镜像走)。
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

# ★ 国内镜像源加速（阿里云 apt + pip + 淘宝 npm）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/* \
    && sed -i 's|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/* \
    && pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ \
    && pip config set global.trusted-host mirrors.aliyun.com \
    && pip config set global.timeout 120 \
    && pip config set global.retries 5

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libportaudio2 \
    nginx \
    curl \
    fonts-wqy-microhei \
    fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

# 安装 Node.js（用于 pptxgenjs / playwright / sharp）
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm config set registry https://registry.npmmirror.com

WORKDIR /app

# 先复制依赖文件利用 Docker 缓存
# [WO_330] requirements-docker-constraints.txt = 全量版本锁(含传递依赖),两处 pip install 都带 -c;
#   改 requirements-docker.txt 的版本要同笔改它,说明见该文件抬头。
COPY requirements-docker.txt requirements-docker-constraints.txt ./

# 安装 Python 依赖（需要临时安装编译工具，完成后清理）
# audioop-lts: Python 3.12 自带 audioop，创建假包满足传递依赖
# ★ 不使用 --extra-index-url，避免 pip 回退到海外 PyPI 导致超时
# ★ BuildKit cache mount: /root/.cache/pip 跨 build 持久化 wheel 缓存
#    跨 deploy 命中率 90%+ · 从 ~3min 降到 ~30s
#    cache 在 BuildKit 缓存层 · 不进镜像 · 镜像大小不变
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ python3-dev libffi-dev libssl-dev \
    && mkdir -p /tmp/audioop-lts \
    && echo 'from setuptools import setup; setup(name="audioop-lts", version="0.2.2")' > /tmp/audioop-lts/setup.py \
    && pip install -c requirements-docker-constraints.txt /tmp/audioop-lts \
    && rm -rf /tmp/audioop-lts \
    && pip install -r requirements-docker.txt -c requirements-docker-constraints.txt \
    && apt-get purge -y gcc g++ python3-dev libffi-dev libssl-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# 安装根目录 Node.js 依赖（PPT 生成等工具）· 加 npm cache mount
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm ci --legacy-peer-deps --prefer-offline

# 安装 Playwright 系统依赖 + 系统 Chromium（从阿里云 apt 镜像安装，无需海外 CDN）
# ★ 跳过 Playwright 自带的浏览器下载，使用系统 Chromium 代替
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    && rm -rf /var/lib/apt/lists/*
ENV CHROMIUM_PATH=/usr/bin/chromium

# 复制项目源码（.dockerignore 会排除无关文件）
COPY . .

# 从 Stage 1 复制前端构建产物
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# Fail the build if tracked/build-context cleanup or the compiled frontend
# regresses. The scanner stores fingerprints only; it never embeds the
# compromised credential values.
RUN python scripts/scan_repository_secrets.py --root /app --mode image

# reviewed release worktree 使用 umask 027。Docker COPY 会保留源目录权限；若
# /app/frontend 变成 0750，nginx worker 会在 API 健康时仍对整个 SPA 返回 403。
RUN chmod 0755 /app/frontend /app/frontend/dist \
    && find /app/frontend/dist -type d -exec chmod 0755 {} + \
    && find /app/frontend/dist -type f -exec chmod 0644 {} +

# 复制 Nginx 配置
COPY nginx.conf /etc/nginx/conf.d/default.conf
# 删除 Nginx 默认站点配置（避免冲突）
RUN rm -f /etc/nginx/sites-enabled/default

# 创建数据目录（如果 Volume 未挂载时使用）
RUN mkdir -p /app/data /app/output /app/cache /app/logs /app/config

# 设置 UTF-8 编码（避免中文乱码）
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONIOENCODING=utf-8

# 复制启动脚本（防御性处理 Windows CRLF 行尾，避免 exec 失败）
COPY start.sh /app/start.sh
RUN sed -i 's/\r$//' /app/start.sh && chmod +x /app/start.sh

# Release identity is applied after dependency layers so changing only the
# reviewed commit SHA does not invalidate the expensive Python/Node cache.
# [WO_275] The ARG itself must be declared HERE, not at the top of the stage:
# every RUN after an ARG sees it as an environment variable, so declaring it
# early made each new SHA a cache miss for apt / nodesource / pip / npm / chromium.
ARG RELEASE_SHA=development
LABEL org.opencontainers.image.revision=$RELEASE_SHA \
      io.omnirank.release.sha=$RELEASE_SHA
RUN printf '%s\n' "$RELEASE_SHA" > /app/RELEASE_SHA

# 暴露端口：80(Nginx) 和 8000(后端，可选直连)
EXPOSE 80 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/docs || exit 1

# 启动
CMD ["/app/start.sh"]
