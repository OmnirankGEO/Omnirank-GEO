"""判别锁 · 前端 assets 保留代数(2026-07-28 Deploy-CTO)。

修的是什么:`merge_frontend_assets` 的 docstring 一直写 "retain **one** previous
generation",但实现是把 legacy 目录**全量补齐** —— 而 legacy 目录就是上一个 active 的
assets,它自己已经累积了历史所有代。于是传递式累积:249 → 500 → 750 → …
实测生产活跃容器 **5477 个 / 201M**,当前构建自带只有 249 个。

本锁同时钉两个方向,缺一不可:
  ① **会收敛** —— 超过保留代数的老 chunk 不再被带入下一代;
  ② **不会误删** —— 当前构建的文件、以及保留窗口内的老 chunk,一个都不能少。
只钉①会让人把窗口调到 1 代(线上老用户立刻 404);只钉②等于没治理。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.merge_frontend_assets import (
    GENERATIONS_FILE,
    merge_legacy_assets,
)


def _make_dist(root: Path, names: list[str], entry: str) -> Path:
    """造一个最小 dist:index.html 引用 entry,assets/ 放 names。"""
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    for n in names:
        (dist / "assets" / n).write_text(f"// {n}", encoding="utf-8")
    (dist / "index.html").write_text(
        f'<html><script src="/assets/{entry}"></script></html>', encoding="utf-8"
    )
    return dist


def _make_legacy(root: Path, names: list[str], generations: list[dict] | None = None) -> Path:
    legacy = root / "legacy"
    legacy.mkdir(parents=True)
    for n in names:
        (legacy / n).write_text(f"// {n}", encoding="utf-8")
    if generations is not None:
        (legacy / GENERATIONS_FILE).write_text(
            json.dumps({"generations": generations}), encoding="utf-8"
        )
    return legacy


def _assets(dist: Path) -> set[str]:
    return {p.name for p in (dist / "assets").iterdir()
            if p.is_file() and p.name != GENERATIONS_FILE}


def test_first_run_keeps_everything_and_starts_the_ledger(tmp_path):
    """首轮(legacy 无账本):一个都不删,只把当前代记下来。

    这条保证「引入治理的那一轮」不会让老用户 404。
    """
    dist = _make_dist(tmp_path, ["index-new.js"], "index-new.js")
    legacy = _make_legacy(tmp_path, [f"old-{i}.js" for i in range(50)])

    out = merge_legacy_assets(dist, legacy, keep_generations=3, release="relA")

    assert out["first_run_no_prune"] is True
    assert out["skipped_expired_files"] == 0, "首轮不得裁剪"
    assert len(_assets(dist)) == 51, "首轮应全量保留"
    ledger = json.loads((dist / "assets" / GENERATIONS_FILE).read_text(encoding="utf-8"))
    assert ledger["generations"][0]["release"] == "relA"
    assert ledger["generations"][0]["files"] == ["index-new.js"]


def test_second_run_prunes_beyond_the_window(tmp_path):
    """🔴 收敛方向:窗口外的老 chunk 不再被带入。"""
    dist = _make_dist(tmp_path, ["index-C.js"], "index-C.js")
    legacy = _make_legacy(
        tmp_path,
        ["index-C.js", "index-B.js", "index-A.js", "index-ANCIENT.js"],
        generations=[
            {"release": "relB", "files": ["index-B.js"]},
            {"release": "relA", "files": ["index-A.js"]},
            {"release": "relAncient", "files": ["index-ANCIENT.js"]},
        ],
    )

    out = merge_legacy_assets(dist, legacy, keep_generations=3, release="relC")

    names = _assets(dist)
    assert "index-C.js" in names, "当前构建文件必须在"
    assert "index-B.js" in names, "前一代必须保留"
    assert "index-A.js" in names, "前两代必须保留(keep_generations=3)"
    assert "index-ANCIENT.js" not in names, "窗口外的必须被裁掉 —— 否则治理无效"
    assert out["skipped_expired_files"] == 1


def test_never_drops_current_build_files(tmp_path):
    """🔴 安全方向:当前构建的文件一个都不能少(哪怕账本里没有)。"""
    dist = _make_dist(tmp_path, ["index-C.js", "chunk-x.js", "style.css"], "index-C.js")
    legacy = _make_legacy(
        tmp_path, ["index-ANCIENT.js"],
        generations=[{"release": "old", "files": ["index-ANCIENT.js"]}],
    )

    merge_legacy_assets(dist, legacy, keep_generations=2, release="relC")

    names = _assets(dist)
    for must in ("index-C.js", "chunk-x.js", "style.css"):
        assert must in names, f"当前构建文件 {must} 被弄丢了"


def test_ledger_is_truncated_to_the_window(tmp_path):
    """账本本身也要截断,否则它会无限长大。"""
    dist = _make_dist(tmp_path, ["i-D.js"], "i-D.js")
    legacy = _make_legacy(
        tmp_path, ["i-C.js", "i-B.js", "i-A.js"],
        generations=[
            {"release": "C", "files": ["i-C.js"]},
            {"release": "B", "files": ["i-B.js"]},
            {"release": "A", "files": ["i-A.js"]},
        ],
    )

    merge_legacy_assets(dist, legacy, keep_generations=3, release="D")

    ledger = json.loads((dist / "assets" / GENERATIONS_FILE).read_text(encoding="utf-8"))
    assert [g["release"] for g in ledger["generations"]] == ["D", "C", "B"]


def test_corrupt_ledger_fails_open_to_no_pruning(tmp_path):
    """账本损坏 → 退回"不删"(fail-open 到安全侧),不得按半个账本删文件。"""
    dist = _make_dist(tmp_path, ["index-new.js"], "index-new.js")
    legacy = _make_legacy(tmp_path, ["old-1.js", "old-2.js"])
    (legacy / GENERATIONS_FILE).write_text("{ this is not json", encoding="utf-8")

    out = merge_legacy_assets(dist, legacy, keep_generations=2, release="relX")

    assert out["first_run_no_prune"] is True
    assert out["skipped_expired_files"] == 0
    assert len(_assets(dist)) == 3


def test_window_of_one_generation_is_refused(tmp_path):
    """禁止把窗口调到 1 代 —— 那等于切流瞬间让所有老 bundle 用户 404。"""
    dist = _make_dist(tmp_path, ["a.js"], "a.js")
    legacy = _make_legacy(tmp_path, ["b.js"])
    with pytest.raises(ValueError):
        merge_legacy_assets(dist, legacy, keep_generations=1, release="x")


def test_candidate_guard_still_catches_real_corruption(tmp_path):
    """账本被排除出守卫,但守卫本身不能因此失效:改动候选文件仍须被抓。"""
    dist = _make_dist(tmp_path, ["index-C.js"], "index-C.js")
    legacy = _make_legacy(tmp_path, [])

    import scripts.merge_frontend_assets as m

    original = m.shutil.copy2

    def _sabotage(src, dst):  # 模拟合并过程篡改候选文件
        original(src, dst)
        (dist / "assets" / "index-C.js").write_text("TAMPERED", encoding="utf-8")

    (legacy / "x.js").write_text("// x", encoding="utf-8")
    m.shutil.copy2 = _sabotage
    try:
        with pytest.raises(RuntimeError, match="changed during legacy merge"):
            merge_legacy_assets(dist, legacy, keep_generations=2, release="relC")
    finally:
        m.shutil.copy2 = original
