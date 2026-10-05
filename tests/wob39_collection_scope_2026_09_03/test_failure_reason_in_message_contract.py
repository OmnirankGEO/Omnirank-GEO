# -*- coding: utf-8 -*-
"""#50 报文合同 —— 守「**报文必须带上失败原因**」。

规则(Review 2026-09-04 改写):不是「禁尾截断」,是「报文必须带上失败原因」。
尾截断只是它的一种失效形态(原因在输出**开头**时);**裸断言**是另一种(完全不说)。

🔴 作用域**只限** `scripts/test_mutation_evidence_verifier_contract.py`:
   它的被测输出有结构化失败标记(`verify_fof_evidence.fail()` 都打 `XX` 前缀),
   而失败原因出现在最开头 —— 尾截断恰好把它切掉。
   全仓另有 **47 处**尾截断分布在 17 个文件,**它们是对的**:
   子进程 traceback 的异常行、pytest 的 summary 都在末尾。
   一刀切的锁会逼那 17 个文件去适配一个它们不需要的形状,所以本锁不覆盖它们。

本文件住在锁包里而**不在被守文件内** —— 被守文件若被改坏,锁不该跟着一起消失。
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
GUARDED = ROOT / "scripts" / "test_mutation_evidence_verifier_contract.py"


def _tail_sliced_assert_msgs(src: str) -> list[str]:
    """分母机械枚举:assert 的 message 里带**负下标切片**的处数。

    用 AST 不用 grep —— grep 会把注释里、字符串里写的同形文本一起数进去
    (今天我在别处已经被「散文触发裸串锁」咬过)。
    """
    out = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Assert) or node.msg is None:
            continue
        for sub in ast.walk(node.msg):
            if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Slice):
                lo = sub.slice.lower
                if isinstance(lo, ast.UnaryOp) and isinstance(lo.op, ast.USub):
                    out.append(f"L{node.lineno}: {ast.unparse(node.msg)[:60]}")
                    break
    return out


def _load_guarded():
    spec = importlib.util.spec_from_file_location("_ev_guarded", GUARDED)
    m = importlib.util.module_from_spec(spec)
    sys.modules["_ev_guarded"] = m
    spec.loader.exec_module(m)
    return m


# ══ ① 该文件里不许再有尾截断报文 ═════════════════════════════════════
def test_w50_01_no_assert_message_is_tail_truncated_in_the_guarded_file():
    left = _tail_sliced_assert_msgs(GUARDED.read_text(encoding="utf-8-sig"))
    assert not left, (
        f"{GUARDED.name} 里仍有 {len(left)} 处尾截断报文:{left[:5]}。"
        f"改用 `_reason(out, N)` —— 它先摘全部 `XX` 行再附尾部,"
        f"对「原因本来就在末尾」的输出向后兼容。")


def test_w50_01b_the_detector_would_flag_a_planted_one():
    """反向臂:喂合成的坏形状,检测器必须点名 —— 否则上一条是恒真的。"""
    bad = 'def t():\n    out = "x"\n    assert 1 == 0, out[-800:]\n'
    good = 'def t():\n    out = "x"\n    assert 1 == 0, _reason(out, 800)\n'
    assert len(_tail_sliced_assert_msgs(bad)) == 1, "检测器对已知坏形状没反应 ⇒ 锁①恒绿"
    assert _tail_sliced_assert_msgs(good) == [], "检测器把好形状也算进去了 ⇒ 锁①会误伤"


# ══ ② `_reason` 的行为:原因在开头必须带上;没有标记必须退回纯尾部 ══
def test_w50_02_reason_carries_the_marker_lines_even_when_they_are_at_the_top():
    """🔴 主锁 —— 直接复现 2026-09-04 那次误判。

    构造一段「失败原因只在**开头**」的输出:旧写法 `out[-N:]` **必然漏掉它**,
    新写法必须带上。这条不是形状锁,是**行为**锁。
    """
    m = _load_guarded()
    head = "XX 树里的权威合同变了:packages 23 != 钉死的 18"
    out = head + "\n" + ("填充行,把原因挤出尾部窗口\n" * 200) + "XX 未过 1 项"

    old = out[-800:]
    assert head not in old, (
        "构造的样本不合格:原因还落在尾 800 字符里 ⇒ 这条判据证明不了任何东西。"
        "把填充加长。")                      # ← 先证样本有区分力,再证 helper

    new = m._reason(out, 800)
    assert head in new, (
        f"`_reason` 没带上开头那条失败原因 —— 它正是 2026-09-04 让我误判"
        f"「6 条根因不同」的那一行。得到:{new[:200]!r}")
    assert "XX 未过 1 项" in new, "尾部那条 XX 也该在"


def test_w50_02b_reason_falls_back_to_plain_tail_when_there_is_no_marker():
    """没有 `XX` 标记时必须**逐字等于**纯尾部 —— 保证不把别处的好报文改哑。"""
    m = _load_guarded()
    out = "".join(f"第 {i} 行:子进程输出\n" for i in range(300)) + "Traceback 最后一行"
    assert m._reason(out, 500) == out[-500:], (
        "无标记时 `_reason` 没有退回纯尾部 —— 那会改变「原因在末尾」那类输出的报文")


def test_w50_03_the_guarded_file_actually_defines_the_helper():
    """接线:光有锁没有 helper 等于把 59 处判据全弄坏。"""
    m = _load_guarded()
    assert callable(getattr(m, "_reason", None)), "被守文件里没有 `_reason`"
