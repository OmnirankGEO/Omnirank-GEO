"""
教程视频上传 OSS · 一次性 + 后续重录复用
[2026-05-26 P1.2-b] 14 mp4(462MB)上 OSS · 从 git 移除 · 仓库瘦身

# 怎么用
1. 在阿里云 RAM 拿到 AK/SK(omnirank-tutorial-uploader)
2. 设环境变量(临时 · 不写文件 · 不进 git):
     PowerShell:
       $env:OSS_AK_ID = "..."
       $env:OSS_AK_SECRET = "..."
     Bash / Linux:
       export OSS_AK_ID="..."
       export OSS_AK_SECRET="..."
3. 跑:
     python -m tools.upload_tutorials_to_oss
4. 输出 14/14 uploaded · 同时打印每个文件的公共 URL
5. 用完 RAM 控制台立刻禁用 AK · 防泄露

# 上传到哪
bucket: omnirank-tutorials (深圳 oss-cn-shenzhen)
路径:    tutorials/<filename>.mp4
公共 URL: https://omnirank-tutorials.oss-cn-shenzhen.aliyuncs.com/tutorials/<filename>.mp4

# 后续(重录视频后)
- 直接覆盖同名文件即可 · OSS 默认覆盖 · 前端 URL 不变
- 重跑这个脚本只上传 frontend/public/tutorials/ 下的 mp4
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import oss2

BUCKET_NAME = "omnirank-tutorials"
ENDPOINT = "oss-cn-shenzhen.aliyuncs.com"
LOCAL_TUTORIALS_DIR = Path(__file__).resolve().parent.parent / "frontend" / "public" / "tutorials"
OSS_PREFIX = "tutorials/"


def main() -> int:
    ak_id = os.environ.get("OSS_AK_ID")
    ak_secret = os.environ.get("OSS_AK_SECRET")
    if not ak_id or not ak_secret:
        print("[error] 环境变量 OSS_AK_ID / OSS_AK_SECRET 必须设置", file=sys.stderr)
        print("        PowerShell: $env:OSS_AK_ID = '...'", file=sys.stderr)
        print("        Bash:       export OSS_AK_ID=...", file=sys.stderr)
        return 1

    if not LOCAL_TUTORIALS_DIR.is_dir():
        print(f"[error] 本地教程目录不存在: {LOCAL_TUTORIALS_DIR}", file=sys.stderr)
        return 1

    mp4_files = sorted(LOCAL_TUTORIALS_DIR.glob("*.mp4"))
    if not mp4_files:
        print(f"[warn] {LOCAL_TUTORIALS_DIR} 下没有 mp4 文件", file=sys.stderr)
        return 0

    auth = oss2.Auth(ak_id, ak_secret)
    bucket = oss2.Bucket(auth, f"https://{ENDPOINT}", BUCKET_NAME)

    print(f"[info] 准备上传 {len(mp4_files)} 个 mp4 到 oss://{BUCKET_NAME}/{OSS_PREFIX}")
    print()

    success = 0
    failed: list[str] = []
    public_base = f"https://{BUCKET_NAME}.{ENDPOINT}/"

    for i, local_path in enumerate(mp4_files, 1):
        key = f"{OSS_PREFIX}{local_path.name}"
        size_mb = local_path.stat().st_size / 1024 / 1024
        print(f"[{i:2d}/{len(mp4_files)}] 上传 {local_path.name} ({size_mb:.1f} MB) ...", end=" ", flush=True)
        try:
            with open(local_path, "rb") as f:
                bucket.put_object(
                    key,
                    f,
                    headers={
                        "Content-Type": "video/mp4",
                        # Cache 1 年 · 文件名不变 · 重录覆盖后 OSS 内部 etag 变 · 客户端会重拉
                        "Cache-Control": "public, max-age=31536000, immutable",
                    },
                )
            print("✓")
            success += 1
        except oss2.exceptions.OssError as e:
            print(f"✗ {e!r}")
            failed.append(local_path.name)

    print()
    print(f"[done] {success}/{len(mp4_files)} 上传成功")
    if failed:
        print(f"[fail] 失败列表: {failed}", file=sys.stderr)

    print()
    print("=== 公共 URL 一览(粘进 videos-data.ts)===")
    for local_path in mp4_files:
        print(f"  {public_base}{OSS_PREFIX}{local_path.name}")

    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
