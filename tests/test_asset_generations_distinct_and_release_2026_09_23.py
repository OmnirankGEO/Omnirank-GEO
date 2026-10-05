"""WO_264 §3-① · 账本两处修正的判据(2026-09-23 Deploy-CTO)。

① 按【不同的前端构建】计代,不按条目数。
   生产实测(0913X 上线后)账本 ``[A, A, B]``:[0] 与 [1] 两格文件集合完全相同。
   纯后端班次产出逐字相同的 dist,旧算法 ``[current] + prior[:K-1]`` 让同一构建占两三格,
   把更老的**不同**构建挤出窗口 —— 下一班若仍纯后端,``keep = A ∪ A ∪ A``,B 被踢出 dist。

② 账本 ``release`` 取自 ``/app/RELEASE_SHA``。
   写方一直支持 ``release``,但 ``deploy-blue-green.sh`` 调用时从没传 ``--release``,
   生产账本三代 release 全是 ``""``。缺的是调用点,不是能力。

🔴 判据纪律:
  * 新旧差分冻成【声明表】—— 不只钉新答案。哪些场景行为变了、哪些没变,多一条少一条都红。
  * 旧算法在本文件里**独立重写一遍**做参照,不 import 被测模块里的任何东西来当"旧版"
    (拿 A 比 A 永远不会红)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.merge_frontend_assets import (
    GENERATIONS_FILE,
    _distinct_generations,
    _resolve_release,
    merge_legacy_assets,
)


def _gen(release: str, files: list[str]) -> dict:
    return {"release": release, "files": list(files)}


A = ["a1.js", "a2.js", "shared.js"]
B = ["b1.js", "b2.js", "shared.js"]
C = ["c1.js", "shared.js"]
D = ["d1.js", "d2.js"]


def _old_algorithm(current: dict, prior: list[dict], keep: int) -> list[dict]:
    """旧算法的【独立重写】:按条目数截。只作参照,绝不 import 被测模块的实现。"""
    return [current] + prior[: keep - 1]


def _files_of(gens: list[dict]) -> list[frozenset]:
    return [frozenset(g["files"]) for g in gens]


# ------------------------------------------------------------------ 声明表
# (场景名, 当前代, 上一代账本, keep, 新算法期望的代(按 release))
SCENARIOS = [
    ("S1_全不同",            _gen("C", C), [_gen("B", B), _gen("A", A), _gen("X", D)], 3, ["C", "B", "A"]),
    ("S2_纯后端且账本已重复", _gen("A3", A), [_gen("A2", A), _gen("A1", A), _gen("B", B)], 3, ["A3", "B"]),
    ("S3_新前端且账本已重复", _gen("D", D), [_gen("A2", A), _gen("A1", A), _gen("B", B)], 3, ["D", "A2", "B"]),
    ("S4_首轮",              _gen("A", A), [], 3, ["A"]),
    ("S5_当前等于上一代",     _gen("A2", A), [_gen("A1", A), _gen("B", B), _gen("C", C)], 3, ["A2", "B", "C"]),
    ("S6_全不同且溢出",       _gen("A", A), [_gen("B", B), _gen("C", C), _gen("D", D)], 3, ["A", "B", "C"]),
]

# 🔴 冻结:新旧算法【产出不同】的场景,恰好是这几个。多一个少一个都红。
#    (这张表是对「这次改动到底改了什么行为」的承诺,不是从代码里算出来的。)
FROZEN_BEHAVIOR_CHANGED = {"S2_纯后端且账本已重复", "S3_新前端且账本已重复", "S5_当前等于上一代"}


@pytest.mark.parametrize("name,current,prior,keep,expected", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_new_algorithm_matches_declared_expectation(name, current, prior, keep, expected):
    got, _ = _distinct_generations(current, prior, keep)
    assert [g["release"] for g in got] == expected, name
    # 不变量:窗口里每一代的文件集合两两不同
    fs = _files_of(got)
    assert len(fs) == len(set(fs)), f"{name}:窗口里出现了重复的前端构建"
    # 不变量:不超过 keep 代,当前代永远在首位
    assert len(got) <= keep and got[0] is current


def test_old_new_behavior_diff_is_exactly_the_frozen_set():
    """钉【新旧差分】,不只钉新答案:行为变了的场景必须恰好是声明的那几个。"""
    changed = set()
    for name, current, prior, keep, _expected in SCENARIOS:
        new, _ = _distinct_generations(current, prior, keep)
        old = _old_algorithm(current, prior, keep)
        if [g["release"] for g in new] != [g["release"] for g in old]:
            changed.add(name)
    assert changed == FROZEN_BEHAVIOR_CHANGED, (
        f"行为变化面与声明不符:多出 {sorted(changed - FROZEN_BEHAVIOR_CHANGED)} · "
        f"缺少 {sorted(FROZEN_BEHAVIOR_CHANGED - changed)}"
    )


def test_collapsed_count_is_reported():
    _, collapsed = _distinct_generations(_gen("A3", A), [_gen("A2", A), _gen("A1", A), _gen("B", B)], 3)
    assert collapsed == 2
    _, none = _distinct_generations(_gen("C", C), [_gen("B", B), _gen("A", A)], 3)
    assert none == 0


# ------------------------------------------------------------------ 端到端:B 不被踢出 dist
def _write(dirpath: Path, names: list[str]) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    for n in names:
        (dirpath / n).write_text(f"// {n}\n", encoding="utf-8")


def _make(tmp_path: Path, current: list[str], legacy: list[str], generations: list[dict]):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    _write(assets, current)
    (dist / "index.html").write_text(
        "".join(f'<script src="/assets/{n}"></script>' for n in current), encoding="utf-8")
    old = tmp_path / "legacy"
    _write(old, legacy)
    (old / GENERATIONS_FILE).write_text(json.dumps({"generations": generations}), encoding="utf-8")
    return dist, old


def test_backend_only_deploy_keeps_the_older_distinct_build_in_dist(tmp_path):
    """生产那个形状:账本 [A, A, B],这一班又是纯后端(当前仍是 A)。

    旧算法:keep = A ∪ A ∪ A ⇒ B 的文件不合进 dist。新算法:B 必须还在。
    """
    dist, legacy = _make(
        tmp_path,
        current=A,
        legacy=sorted(set(A) | set(B)),
        generations=[_gen("A2", A), _gen("A1", A), _gen("B", B)],
    )
    out = merge_legacy_assets(dist, legacy, keep_generations=3, release="A3")
    names = {p.name for p in (dist / "assets").iterdir()}
    assert {"b1.js", "b2.js"} <= names, "更老的不同前端构建 B 被踢出了 dist"
    assert out["collapsed_duplicate_generations"] == 2
    ledger = json.loads((dist / "assets" / GENERATIONS_FILE).read_text(encoding="utf-8"))
    assert [g["release"] for g in ledger["generations"]] == ["A3", "B"]


# ------------------------------------------------------------------ release 取值
SHA = "db862889a5c1dd0039f9092ed2931f075c859ab2"


def test_release_cli_value_wins(tmp_path):
    f = tmp_path / "RELEASE_SHA"
    f.write_text(SHA + "\n", encoding="utf-8")
    assert _resolve_release("explicit-value", f) == ("explicit-value", "cli")


def test_release_falls_back_to_the_release_sha_file(tmp_path):
    f = tmp_path / "RELEASE_SHA"
    f.write_text(SHA + "\n", encoding="utf-8")
    assert _resolve_release("", f) == (SHA, "release_sha_file")


@pytest.mark.parametrize("content", ["", "not-a-sha", SHA[:39], SHA.upper(), SHA + "0"])
def test_garbage_in_the_release_file_is_never_written_as_identity(tmp_path, content):
    f = tmp_path / "RELEASE_SHA"
    f.write_text(content, encoding="utf-8")
    assert _resolve_release("", f) == ("", "none")


def test_missing_release_file_yields_none(tmp_path):
    assert _resolve_release("", tmp_path / "does-not-exist") == ("", "none")
