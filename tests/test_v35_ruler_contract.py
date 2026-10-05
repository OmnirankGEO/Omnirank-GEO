# -*- coding: utf-8 -*-
"""#91 · 尺子自身的判据 —— 每条修好的尺子各配一发毒。

修的是「判据坏了」,所以**判据的判据**不能只有「跑完是绿的」:
`test_v35_ui_audit.py` 里 6 条尺子今天之前分两种坏法 ——
  · `p3` / `r6` 硬编码了已改名的文件 ⇒ **FileNotFoundError**,连本该检查的其它文件
    也没读到,而在 `-q` 输出里跟断言失败**同形**;
  · `r7` / `r9` / `r11` / `r12` 判存在就 `continue` / `skip` ⇒ **静默少检**,
    skip 不进 failed 计数,没人会发现。
两种都让分母悄悄缩水。所以本文件逐条钉住:**分母塌了必须响,不许静默**。
"""
from __future__ import annotations

import pytest

from tests.test_v35_ui_audit import (
    RM_DIR,
    is_dev_placeholder_comment,
    named_page,
    rm_tsx_files,
)


# ══ ① 目录机械枚举:分母塌了必须**抛**,不许返回空 ═══════════════════
def test_rm_enumeration_is_nonempty_and_matches_disk():
    files = rm_tsx_files()
    on_disk = sorted(RM_DIR.rglob("*.tsx"))
    assert [f.name for f in files] == [f.name for f in on_disk], "枚举与盘上不一致"
    assert len(files) >= 5, f"只枚举到 {len(files)} 个 —— 分母塌了"


def test_rm_enumeration_raises_when_directory_is_empty(tmp_path, monkeypatch):
    """🔴 毒:目录里一个 .tsx 都没有 ⇒ 必须**红**,不许安静地返回空清单。

    空分母的绿是本仓的头号假绿形态:「什么都没量到」被读成「没有违规」。
    """
    import tests.test_v35_ui_audit as mod
    empty = tmp_path / "ResearchMonitor"
    empty.mkdir()
    monkeypatch.setattr(mod, "RM_DIR", empty)
    with pytest.raises(AssertionError, match="分母塌了"):
        mod.rm_tsx_files()


def test_rm_enumeration_raises_when_directory_is_gone(tmp_path, monkeypatch):
    """毒:目录整个改名/搬走 ⇒ 必须红。原来 `if not rm_dir.exists(): skip` 是静默出口。"""
    import tests.test_v35_ui_audit as mod
    monkeypatch.setattr(mod, "RM_DIR", tmp_path / "__never_exists__")
    with pytest.raises(AssertionError, match="目录不在"):
        mod.rm_tsx_files()


# ══ ② 具名页:文件不在 = 红,不是 skip ═══════════════════════════════
def test_named_page_returns_existing_file():
    assert named_page("Admin/InventoryAudit.tsx").is_file()


def test_named_page_raises_on_missing_file():
    """🔴 毒:点名的页面不见了 ⇒ 必须红。

    这正是 `p3` / `r6` 硬编码 `PromptsPanel.tsx` 之后发生的事 —— 只不过当时是
    `FileNotFoundError`(异常),现在是带解释的断言:报文要说清「要么改名跟上,
    要么按退役处理并写明继任者」,而不是让人以为是环境问题。
    """
    with pytest.raises(AssertionError, match="判据点名的页面不存在"):
        named_page("Admin/__never_exists_zzz__.tsx")


# ══ ③ r13 开发占位标记:两臂 ═════════════════════════════════════════
DEV_MARKERS = [
    "// TODO 这里先放占位文案",
    "// FIXME 占位,待接后端",
    "//   Day 3 审核界面占位",
    "// 待实现:占位卡片",
    "// 未实现 · 占位",
]
NOT_DEV_MARKERS = [
    "// 核心: 用占位符 swap 法保护配对 ** 再删孤立 **",
    "//   step  1: 配对 **X** 转占位符 (保护) → SOH X STX",
    "// SOH(\\x01) / STX(\\x02) 是 ASCII 控制字符 · 用作临时占位",
    "// 占位符 swap 法 + 零宽空格修中文边界的实现见该文件)",
]


@pytest.mark.parametrize("line", DEV_MARKERS)
def test_dev_placeholder_markers_are_caught(line):
    """🔴 正样本臂:真的开发周期占位标记必须抓到。

    没有这一臂,收窄之后的「0 命中」最可能的解释是**收过头了**
    ——「修假阳时沿同一根轴滑到另一端,真阳一起没」。
    """
    assert is_dev_placeholder_comment(line), f"该抓没抓到:{line}"


@pytest.mark.parametrize("line", NOT_DEV_MARKERS)
def test_algorithm_term_placeholder_is_not_flagged(line):
    """反臂:「占位符」是 placeholder-swap 算法的术语,不是开发标记。

    实测 ResearchMonitor 里 6 处「占位」**全是**它 —— 裸词规则已退化成纯噪声源。
    """
    assert not is_dev_placeholder_comment(line), f"不该抓却抓了:{line}"


def test_r13_scans_comments_on_purpose_do_not_strip_them():
    """🔴 边界钉死:r13 是**故意**只扫注释的 —— 不许给它套「去注释抽取器」。

    r10 / r11 / w5_4 问的是「露给用户看没有」,剥注释是对的;
    r13 问的是「开发周期标记清干净没有」,**注释正是它的战场**。
    给它剥注释 = 扫无可扫 = 恒绿,比现在的假阳更坏(假阳会红,恒绿不制造任何问题)。
    """
    import inspect

    import tests.test_v35_ui_audit as mod
    src = inspect.getsource(mod.test_r13_research_monitor_no_dev_jargon)
    assert "visible_source" not in src, (
        "r13 被接上了可见文案抽取器 —— 那会把它唯一的扫描面(注释)整个挖空,"
        "这条判据从此恒绿。它要的是收窄**词面**,不是换扫描面。")
    assert "is_comment_line" in src, "r13 不再区分注释行 —— 它的口径被改掉了"


# ══ ④ r13 接线级两臂:往临时树注毒,跑**整条判据** ═══════════════════
def _run_r13_on(tmp_path, monkeypatch, filename: str, content: str):
    """把整条 r13 判据指到一棵只含一个合成文件的临时树上跑。

    🔴 上面 ③ 那两组只验了**谓词**;谓词对不代表判据会红 ——
    判据可能根本没调它,或调了但结果没进断言(本仓栽过「裸串结构锁只证谓词有牙、
    证不了结果进退出码」)。这一组补的就是那一层。
    """
    import tests.test_v35_ui_audit as mod
    d = tmp_path / "ResearchMonitor"
    d.mkdir()
    (d / filename).write_text(content, encoding="utf-8")
    monkeypatch.setattr(mod, "RM_DIR", d)
    return mod


def test_r13_goes_red_on_a_real_dev_marker_comment(tmp_path, monkeypatch):
    """🔴 正样本臂(接线级):注一条「// TODO 占位 待实现」⇒ **整条判据必须红**。"""
    mod = _run_r13_on(tmp_path, monkeypatch, "Fake.tsx",
                      "// TODO 占位 待实现\nexport const X = 1;\n")
    with pytest.raises(AssertionError, match="开发占位词"):
        mod.test_r13_research_monitor_no_dev_jargon()


def test_r13_stays_green_on_the_algorithm_term(tmp_path, monkeypatch):
    """反臂(接线级):注一条算法术语「占位符」⇒ **不许红**。

    这两臂必须成对:只有正样本臂会奖励「把规则放到最宽」,
    只有反臂会奖励「把规则收到最严」。
    """
    mod = _run_r13_on(tmp_path, monkeypatch, "Fake.tsx",
                      "// 占位符 swap 法保护配对 ** 再删孤立\nexport const X = 1;\n")
    mod.test_r13_research_monitor_no_dev_jargon()


def test_r13_denominator_collapse_is_red_not_silent(tmp_path, monkeypatch):
    """毒:目录空了 ⇒ r13 必须抛(经 rm_tsx_files 的空分母断言),不是安静通过。"""
    import tests.test_v35_ui_audit as mod
    empty = tmp_path / "ResearchMonitor"
    empty.mkdir()
    monkeypatch.setattr(mod, "RM_DIR", empty)
    with pytest.raises(AssertionError, match="分母塌了"):
        mod.test_r13_research_monitor_no_dev_jargon()
