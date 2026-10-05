"""Safely retain a bounded number of previous SPA asset generations during cutover.

The current immutable build is authoritative.  Legacy chunks may only fill
missing filenames; they must never overwrite or delete a current-build file.
File mtimes are deliberately ignored because image layers retain build time,
which can be hours older than the eventual production cutover.

[保留代数 2026-07-28 Deploy-CTO]
本模块的 docstring 一直写着 "retain **one** previous generation",但实现是把 legacy
目录里的文件**全量补齐** —— 而 legacy 目录正是上一个 active 的 assets,它自己已经
累积了历史所有代。于是变成传递式累积:249 → 500 → 750 → … 实测活跃容器已达
**5477 个 / 201M**(最老 chunk 追溯到 7 月 23 日),而当前构建自带只有 249 个。
后果:每轮部署都要多拷 5000+ 文件、吃磁盘(磁盘曾把部署卡死)、合并越来越慢。

修法:在 dist 里维护一份代次账本 ``assets/.asset-generations.json``,记录**每一代
自己的文件名集合**(不是累积目录快照)。合并时只补"最近 KEEP_GENERATIONS 代"里
出现过的文件名,更老的自然不再被带入下一代。

为什么不按 mtime、也不按 index.html 闭包:
  * mtime —— 镜像层保留的是构建时间,可能比部署早几小时,按它删会误删当前 chunk;
  * index.html 闭包 —— 只含 4 个 entry,懒加载 chunk 全不在里面,按它删会删掉
    99% 还在用的分包。代次账本记的是**实际文件名**,不需要推断闭包。

首轮兼容:legacy 侧没有账本时(本改动上线的那一轮),**全量保留**并只记录当前代,
不做任何删除;从下一轮起才开始收敛。这样不会在引入治理的同一轮就让老用户 404。
另外「旧 chunk 失效自愈」已于 2026-07-28 上线,收敛后万一有超龄用户命中,
看到的是「正在更新到新版本 + 立即刷新」而不是 stack trace。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Dict


INDEX_ASSET_RE = re.compile(
    rb"/assets/([A-Za-z0-9_.-]+\.(?:js|css|woff2?|png|svg|webp|jpe?g))"
)

# 代次账本文件名。放在 assets/ 里,随 dist 一起被 docker cp 带到下一代。
GENERATIONS_FILE = ".asset-generations.json"
# 保留几代(含当前)。3 = 当前 + 前两代,按每代约 250 文件计约 750 个。
DEFAULT_KEEP_GENERATIONS = 3
# [WO_264 §3-① · 2026-09-23] 本次发布身份的真源:构建时烤进镜像的 /app/RELEASE_SHA。
#   deploy-blue-green.sh 调本脚本时从没传过 --release(生产账本三代 release 全是 ""),
#   而写方其实一直支持这个字段 —— 缺的是调用点,不是能力。
#   这里在 CLI 没给值时自己去读,不再依赖调用方记得传。
RELEASE_SHA_FILE = Path("/app/RELEASE_SHA")
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_manifest(root: Path) -> Dict[str, str]:
    root = root.resolve(strict=True)
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def referenced_asset_manifest(dist_dir: Path) -> Dict[str, str]:
    dist_dir = dist_dir.resolve(strict=True)
    index = dist_dir / "index.html"
    if not index.is_file():
        raise RuntimeError(f"index.html missing: {index}")
    names = sorted({match.decode("ascii") for match in INDEX_ASSET_RE.findall(index.read_bytes())})
    if not names:
        raise RuntimeError("index.html has no static asset references")

    manifest: Dict[str, str] = {}
    for name in names:
        asset = dist_dir / "assets" / name
        if not asset.is_file():
            raise RuntimeError(f"index.html referenced asset missing: {name}")
        manifest[f"assets/{name}"] = _sha256(asset)
    return manifest


def _read_generations(legacy_assets_dir: Path) -> list[dict]:
    """读上一代带过来的代次账本;没有(首轮)返回空列表。

    账本损坏一律**当作没有**——首轮兼容路径是"全量保留",
    比按半个账本删文件安全得多(fail-open 到不删)。
    """
    ledger = legacy_assets_dir / GENERATIONS_FILE
    if not ledger.is_file():
        return []
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
        gens = data.get("generations")
        if not isinstance(gens, list):
            return []
        out = []
        for gen in gens:
            files = gen.get("files") if isinstance(gen, dict) else None
            if isinstance(files, list) and all(isinstance(f, str) for f in files):
                out.append({"release": str(gen.get("release") or ""), "files": files})
        return out
    except Exception:
        return []


def _distinct_generations(current: dict, prior: list[dict], keep: int) -> tuple[list[dict], int]:
    """当前代置顶,按【不同的前端构建】凑满 keep 代;返回 (代列表, 折叠掉的重复条目数)。

    [WO_264 §3-① · 2026-09-23] 原来按**条目数**截 ``prior[: keep - 1]``。
    纯后端改动的班次会产出与上一代逐字相同的 dist(文件名集合完全一样),
    于是同一个前端构建占掉两三格,把更老的**不同**构建挤出窗口:
    生产实测账本 ``[A, A, B]``(两格同为 283 个文件),下一班若仍是纯后端,
    旧算法 ``keep = A ∪ A ∪ A`` ⇒ **B 被踢出 dist**;连着几班纯后端,dist 里只剩当前前端。
    docstring 的设计目标是「当前 + 前两代,约 750 个」,实测只有 511。

    同一构建的多条记录只留**最新**那一条(当前代总是最新的,所以它顶替旧的同构建记录)。
    """
    out = [current]
    seen = {frozenset(current["files"])}
    collapsed = 0
    for gen in prior:
        if len(out) >= keep:
            break
        key = frozenset(gen["files"])
        if key in seen:
            collapsed += 1
            continue
        seen.add(key)
        out.append(gen)
    return out, collapsed


def merge_legacy_assets(
    dist_dir: Path,
    legacy_assets_dir: Path,
    *,
    keep_generations: int = DEFAULT_KEEP_GENERATIONS,
    release: str = "",
) -> dict:
    dist_dir = dist_dir.resolve(strict=True)
    legacy_assets_dir = legacy_assets_dir.resolve(strict=True)
    assets_dir = dist_dir / "assets"
    if not assets_dir.is_dir():
        raise RuntimeError(f"candidate assets directory missing: {assets_dir}")
    if keep_generations < 2:
        raise ValueError("keep_generations 必须 ≥2(当前代 + 至少一代旧 chunk)")

    candidate_manifest = file_manifest(dist_dir)
    candidate_refs = referenced_asset_manifest(dist_dir)

    # 当前代 = 本次构建自带的 assets 文件名(账本自身不计入)
    current_files = sorted(
        p.name for p in assets_dir.iterdir()
        if p.is_file() and p.name != GENERATIONS_FILE
    )
    prior_generations = _read_generations(legacy_assets_dir)
    first_run = not prior_generations
    new_generations, collapsed = _distinct_generations(
        {"release": release, "files": current_files}, prior_generations, keep_generations,
    )

    if first_run:
        # 首轮:legacy 没有账本 → 不做任何裁剪,全量保留,只把当前代记下来。
        keep_names: set[str] | None = None
    else:
        # 保留:窗口内每一个【不同】前端构建的文件(含当前代)
        keep_names = set()
        for gen in new_generations:
            keep_names.update(gen["files"])

    merged = 0
    skipped_old = 0
    for source in sorted(legacy_assets_dir.iterdir()):
        if not source.is_file() or source.name == GENERATIONS_FILE:
            continue
        destination = assets_dir / source.name
        if destination.exists():
            continue
        if keep_names is not None and source.name not in keep_names:
            skipped_old += 1
            continue
        shutil.copy2(source, destination)
        merged += 1

    # 写新账本:当前代置顶,按不同构建截到 keep_generations 代(上面已算好)
    (assets_dir / GENERATIONS_FILE).write_text(
        json.dumps({"generations": new_generations}, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    current_manifest = file_manifest(dist_dir)
    # 账本是本函数自己写的,不属于候选构建 → 从守卫比对里剔除,
    # 否则「候选构建在合并期间被改动」会被自己触发。
    guard_before = {k: v for k, v in candidate_manifest.items()
                    if not k.endswith(GENERATIONS_FILE)}
    guard_after = {k: v for k, v in current_manifest.items()
                   if not k.endswith(GENERATIONS_FILE)}
    changed = {
        name: (digest, guard_after.get(name))
        for name, digest in guard_before.items()
        if guard_after.get(name) != digest
    }
    if changed:
        raise RuntimeError(
            "candidate frontend build changed during legacy merge: "
            + ", ".join(sorted(changed))
        )
    if referenced_asset_manifest(dist_dir) != candidate_refs:
        raise RuntimeError("candidate index/assets closure changed during legacy merge")

    return {
        "candidate_files": len(guard_before),
        "candidate_references": len(candidate_refs),
        "merged_legacy_files": merged,
        "skipped_expired_files": skipped_old,
        "kept_generations": len(new_generations),
        "collapsed_duplicate_generations": collapsed,
        "first_run_no_prune": first_run,
        "total_files": len(guard_after),
    }


def _resolve_release(cli_value: str, path: Path = RELEASE_SHA_FILE) -> tuple[str, str]:
    """返回 (release, 来源)。来源 ∈ {"cli", "release_sha_file", "none"}。

    文件里不是 40 位小写 hex 就**不写进账本**(宁可空,也不把垃圾当身份)。
    """
    value = (cli_value or "").strip()
    if value:
        return value, "cli"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "", "none"
    if _SHA40_RE.match(text):
        return text, "release_sha_file"
    return "", "none"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", type=Path)
    parser.add_argument("legacy_assets_dir", type=Path)
    parser.add_argument("--keep-generations", type=int, default=DEFAULT_KEEP_GENERATIONS)
    parser.add_argument("--release", default="")
    args = parser.parse_args()
    release, release_source = _resolve_release(args.release)
    result = merge_legacy_assets(
        args.dist_dir,
        args.legacy_assets_dir,
        keep_generations=args.keep_generations,
        release=release,
    )
    result["release"] = release
    result["release_source"] = release_source
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
