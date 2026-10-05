# 教程视频 · 已迁阿里云 OSS

[2026-05-26 P1.2-b] 14 个教程 mp4 已上 OSS · 不再放 git 里(原本 462MB 进 git
会让仓库克隆慢 / CI 拉镜像慢 · 而且仓库历史里再也清不掉)。

## 视频去哪了

存 OSS bucket:`omnirank-tutorials`(深圳)· **公共读**

公共 URL 格式:
```
https://omnirank-tutorials.oss-cn-shenzhen.aliyuncs.com/tutorials/<filename>.mp4
```

完整 14 个文件:见 [`../../src/components/help/videos-data.ts`](../../src/components/help/videos-data.ts) · 由 `tutorialUrl()` 拼接。

## 前端拉 OSS 还是 CDN

- **默认**:OSS 直链(深圳 · 国内 ~50MB/s)
- **走 CDN**(国内更快 · 国外能访问):前端 `.env` 设 `VITE_TUTORIAL_CDN_BASE=https://cdn.omnirank.top` 即可,代码 0 改动

## 重录视频怎么发版

1. 把新 mp4 放到 `frontend/public/tutorials/<同名>.mp4`(本地能预览 dev 不依赖 OSS,但.gitignore 拦不 commit)
2. 跑上传脚本:
   ```bash
   $env:OSS_AK_ID = "..."
   $env:OSS_AK_SECRET = "..."
   python -m tools.upload_tutorials_to_oss
   ```
3. OSS 默认覆盖同名文件 · `Cache-Control: immutable` 配 etag 变更 · 客户端自动重拉
4. 不用动 `videos-data.ts` · 文件名不变 URL 也不变
5. 不用重新部署前端

## 本地 dev 不想从 OSS 拉(没网/慢)

把 14 个 mp4 拷到 `frontend/public/tutorials/` · 然后前端 `.env.local`:
```
VITE_TUTORIAL_CDN_BASE=
```
留空 → 走 vite dev server 本地 `/tutorials/<name>.mp4`(需要改 `tutorialUrl()` 逻辑兼容 · 当前默认指向 OSS)

或者直接在 `videos-data.ts` 临时把 `CDN_BASE` 改成空字符串。

## 关于安全

- bucket 是**公共读**:视频不敏感所以 OK · 别往这个 bucket 放任何客户数据/内部资料
- AK/SK 只放在跑上传脚本的环境变量里 · 不写代码、不进 git
- 长期上传要 AK 轮换 · 或者改用 RAM 角色 STS 临时凭证
