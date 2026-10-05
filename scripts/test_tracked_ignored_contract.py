# -*- coding: utf-8 -*-
"""#100 · 被代码/判据引用的产物,必须有**显式取反规则**才能进树。

## 缺陷是什么

`scripts/` 下 19 个判据、若干夹具与迁移 SQL,当初全靠 `git add -f` 硬塞进树 ——
`.gitignore` L196 附近那段注释三周前就抱怨过这个做法「靠人记得加 -f」。
**抱怨传不出去,规则才传得出去。** 而没有规则时,下一个人的 `git add` 会被拒,
`git commit` 却回「nothing to commit, working tree clean」——**看起来像成功**。

## 🔴 分母:按**后果**收敛,不是按类型

    git ls-files | git check-ignore --no-index --stdin        → 3016 条
    其中「文件类型像判据/夹具/脚本」                            →   62 条
    其中**被非文档的代码或判据引用**(掉了会让门或生产代码坏)  →   27 条

拿 3016 立卡会变成三千条取反规则运动,绝大部分毫无价值(2954 条是文档、截图、
历史产物,掉了不坏任何门)。拿 62 也不对:那 18 张 deal-studio 截图与一批
一次性 `*_result.json` 无人引用,同样不坏门。
**「我注意到的」「查询返回的」「后果定义的」是三个不同的数,前两个都不是分母。**

## 🔴 `scripts/*.json` 不加取反 —— 那条 ignore 是**安全规则**

`.gitignore:139` 禁 `scripts/*.json` 的理由写着「历史响应可能含第三方
authentication_token」。而作用域里剩下的 14 条**全是** `scripts/*.json`
(`*_result.json` / `resolved_*.json` / `m3_acceptance_fixtures.json`),
它们正是那条规则针对的形态。给它们加取反 = **拆掉一条安全规则**。

⇒ 处置不同:它们**已经在树里**(先于规则或靠 -f),规则保护的是**将来新增的**。
   所以登记在本文件的冻结例外集里,写明「不许加取反」,而不是放行。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 🔴 冻结例外:**已跟踪且被代码引用,但不许加取反规则**。
#:    理由必须写清「为什么不加取反」——这里全是安全规则 `scripts/*.json` 的辖区。
NO_NEGATION_BY_DESIGN: dict[str, str] = {
    p: "scripts/*.json 是安全规则(历史响应可能含第三方 token)的辖区;"
       "本文件已在树里(先于规则或靠 -f),规则保护的是将来新增的 —— 加取反等于拆掉它。"
    for p in (
        "scripts/ai_entrepreneur_results.json",
        "scripts/ai_fill_billing_paths_smoke_result.json",
        "scripts/breakdown_single_result.json",
        "scripts/compare_result.json",
        "scripts/douyin_search_results.json",
        "scripts/m1c_actual_smoke_result.json",
        "scripts/m1c_baseline_5brands_result.json",
        "scripts/m1c_reachable_5brands_result.json",
        "scripts/m1m2_db_self_heal_result.json",
        "scripts/m2_v2_smoke_result.json",
        "scripts/m3_acceptance_fixtures.json",
        "scripts/resolved_target.json",
        "scripts/resolved_waimao.json",
        "scripts/video_detail.json",
    )
}

_CODE = re.compile(r"\.(py|mjs|js|ts|tsx|sh|ps1|sql|cjs|yml|yaml|json|toml|ini|cfg)$")
_DOC = re.compile(r"\.(md|txt|log|png)$")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, errors="replace").stdout


def tracked_and_ignored() -> list[str]:
    """已跟踪 ∩ 被 .gitignore 命中 —— 即「当初需要 `-f` 才进得来」的路径。

    🔴 必须走**二进制** stdin。`subprocess(text=True)` 在 Windows 上写 stdin 时会把
       `\\n` 译成 `\\r\\n`,git 于是把 `path\\r` 当成路径名,又因其含控制符而**转义输出**
       (`"path\\r"`)。实测代价:2760 条全部以字面 `\\r` 结尾、候选 0,
       而主锁在这个坏分母上**是绿的** —— 假绿,只有「例外表陈旧」那条抓到。
       同族:同一条命令在 shell 管道里对、在 Python subprocess 里错。
    """
    tracked = _git("ls-files")
    r = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"], cwd=ROOT,
                       input=tracked.encode("utf-8"), capture_output=True)
    out = r.stdout.decode("utf-8", "replace")
    paths = [l.strip().strip('"') for l in out.split("\n") if l.strip()]
    # 自证:分母里不许出现被转义的控制符 —— 出现了就说明管道又把数据弄脏了
    dirty = [p for p in paths if "\\r" in p or "\r" in p]
    assert not dirty, (
        f"分母里有 {len(dirty)} 条带 CR 的路径(样本 {dirty[:2]})—— "
        f"stdin 又被按文本模式译过了。这一档的读数**全部不可信**,先修取数。")
    return [p.replace("\\", "/") for p in paths]


def _looks_like_artifact(p: str) -> bool:
    b = p.rsplit("/", 1)[-1]
    return bool(
        re.match(r"test_.*\.py$", b) or "/tests/" in "/" + p or p.endswith(".sql")
        or re.search(r"\.spec\.(ts|tsx|js|mjs)$", b)
        or (p.startswith("scripts/") and p.endswith((".py", ".mjs", ".json")))
        or "fixtures" in p
    )


def in_scope() -> list[str]:
    """作用域 = 被**非文档的代码或判据**引用的那些(掉了会让门或生产代码坏)。"""
    out = []
    for p in tracked_and_ignored():
        if not _looks_like_artifact(p):
            continue
        refs = [x.strip().replace("\\", "/")
                for x in _git("grep", "-l", "--", os.path.basename(p)).split("\n") if x.strip()]
        refs = [x for x in refs if x != p]
        if any(_CODE.search(x) and not _DOC.search(x) for x in refs):
            out.append(p)
    return sorted(out)


# ══ ① 分母自证 ═════════════════════════════════════════════════════
def test_the_denominator_is_mechanical_and_large():
    """先证尺子在量东西:全量应是三千条量级,不是零。"""
    all_ti = tracked_and_ignored()
    assert len(all_ti) > 1000, (
        f"已跟踪∩被 ignore 只有 {len(all_ti)} 条 —— 分母塌了,"
        f"先查 `git check-ignore --no-index --stdin` 是否真跑了,"
        f"别把「什么都没量到」读成「没有欠账」。")


# ══ ② 主锁:作用域内的路径必须有取反,或有书面例外 ═══════════════════
def test_every_code_referenced_artifact_has_an_explicit_negation():
    """在作用域内**还出现在这张表上**,就说明它没有取反规则罩着。

    (`check-ignore` 列出的就是「仍被 ignore」的;有取反的会自动消失。)
    """
    scope = in_scope()
    unexplained = [p for p in scope if p not in NO_NEGATION_BY_DESIGN]
    assert not unexplained, (
        "这些路径被**代码/判据**引用,却仍被 .gitignore 挡着(靠 `-f` 才进得来):\n    "
        + "\n    ".join(unexplained)
        + "\n    二选一:①在 .gitignore 加**显式取反规则**(不是靠人记得加 -f);\n"
          "            ②若那条 ignore 是安全规则(如 scripts/*.json 防第三方 token),\n"
          "              登记进 `NO_NEGATION_BY_DESIGN` 并写清**为什么不加取反**。\n"
          "    🔴 `git add` 被拒之后 `git commit` 会回「nothing to commit, working tree clean」\n"
          "       —— 看起来像成功。这正是本门要拦的形态。")


def test_the_exception_registry_is_not_stale():
    """例外表里的路径若已不在作用域(加了取反 / 文件删了 / 没人引用了)⇒ 同笔清理。"""
    scope = set(in_scope())
    stale = sorted(set(NO_NEGATION_BY_DESIGN) - scope)
    assert not stale, (
        f"这些已不在作用域,却还留在例外表里:{stale} —— 同笔删掉。留着 = 登记表在说谎。")
    for p, why in NO_NEGATION_BY_DESIGN.items():
        assert len(why) > 20, f"{p} 的例外理由太短,等于没写"


# ══ ③ 域外要**显式记**,不是沉默略过 ═══════════════════════════════
def test_out_of_scope_is_recorded_not_silent():
    """2954 条文档/截图/历史产物是**显式判定为域外**的,不是没看见。

    沉默略过的话,下一个人还得把全集重新判一遍。
    本条钉住:域外的量级在 docstring 里写着,且与实测同量级。
    """
    all_ti = len(tracked_and_ignored())
    scope = len(in_scope())
    assert all_ti - scope > 1000, (
        f"域外只有 {all_ti - scope} 条 —— 与 docstring 记录的量级(约 2954)不符,"
        f"说明作用域判定被放宽了,先查 `in_scope()` 的过滤条件。")
    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    assert "2954" in src and "3016" in src, (
        "docstring 里的分母读数不见了 —— 域外那一大批就又变成沉默略过了。")
