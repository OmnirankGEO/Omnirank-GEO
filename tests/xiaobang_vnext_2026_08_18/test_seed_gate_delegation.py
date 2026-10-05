"""判据 · WO-A ⑤:``_assert_seed_text_clean`` 委托给 ``assert_chunk_rows_clean``。

## 要证明两件相反的事

1. **行为等价** —— 委托之后,脏 seed 仍然抛同一个异常、``where`` 不变、
   问题串仍然指得出是哪一条 seed(``seed[前 20 字]: …``)、``fixes`` 仍然给改法。
   等价不成立的话,这就不是重构而是改行为。
2. **多出来的那一点是 advisory** —— 手抄的那份漏了 ``_warn_advisories``:
   同一段文本走 chunk 写径会留下③域建议,走 seed 写径**一条都不留**。
   补上之后 seed 也留痕迹。这条正是这次改动的**唯一**行为差,判据要单独打它。

## 结构锚

``test_seed_gate_does_not_reimplement_the_aggregation`` 打的是「不许再抄第四份」:
函数体里不许再出现自己拼 ``KbTerminologyViolation`` 的写法。
把委托改回手抄循环 ⇒ 这条转红。
"""

from __future__ import annotations

import ast
import inspect
import logging

import pytest

#: ③ 域**建议**样本:含「额度」+ 上限语境 ⇒ 硬门放行(裁定说不算错义),
#: advisory 命中。这一段刻意不含旧计价单位/池名/现金锚点。
ADVISORY_ONLY_ANSWER = "团队额度上限在组织中心那一屏设置。"

#: 硬门样本:旧计价单位「积分」。
DIRTY_ANSWER = "充值之后到账的是充值积分,可以直接用。"


def _seed_entry(question: str, answer: str):
    """造一条与 ``_INITIAL_SEED`` 同形的记录(question, category, sort, up, down, answer)。"""
    return (question, "onboarding", 10, 0, 0, answer)


def test_a_dirty_seed_is_still_rejected_with_the_same_contract(monkeypatch):
    """行为等价:异常类型 / where / 问题串前缀 / fixes 四样都不变。"""
    import db.faq_db as faq_db
    from services.kb_terminology_gate import KbTerminologyViolation

    question = "余额不足时怎么充值"
    monkeypatch.setattr(faq_db, "_INITIAL_SEED",
                        [_seed_entry(question, DIRTY_ANSWER)])
    with pytest.raises(KbTerminologyViolation) as exc:
        faq_db._assert_seed_text_clean()

    err = exc.value
    assert err.where == "faq_db.seed"
    assert err.problems, "抛了但没说是哪一条"
    assert all(p.startswith("seed[") for p in err.problems), err.problems
    assert question[:20] in err.problems[0], err.problems[0]
    assert err.fixes, "拒绝而不给改法,下一个人只会把门关掉"


def test_a_clean_seed_passes(monkeypatch):
    """反向对照:干净 seed 不许被拦 —— 否则上一条靠「一律抛」也能过。"""
    import db.faq_db as faq_db

    monkeypatch.setattr(faq_db, "_INITIAL_SEED",
                        [_seed_entry("怎么改密码", "在账号设置里改,改完不用重新登录。")])
    faq_db._assert_seed_text_clean()


def test_the_real_shipping_seed_passes():
    """现役 ``_INITIAL_SEED`` 自己必须过门(不然每次部署都炸在启动期)。"""
    import db.faq_db as faq_db

    faq_db._assert_seed_text_clean()


def test_seed_advisories_are_now_logged(monkeypatch, caplog):
    """🔴 这次改动**唯一**的行为差:seed 里的③域建议开始留痕。

    手抄那份没有这一步 —— 同一段文本走 chunk 写径会记,走 seed 写径不记。
    """
    import db.faq_db as faq_db

    monkeypatch.setattr(faq_db, "_INITIAL_SEED",
                        [_seed_entry("团队怎么分算力", ADVISORY_ONLY_ANSWER)])
    with caplog.at_level(logging.WARNING, logger="GEO-KbTerminology"):
        faq_db._assert_seed_text_clean()          # advisory 不拦
    messages = [r.getMessage() for r in caplog.records]
    assert any("建议" in m for m in messages), messages
    assert any("faq_db.seed" in m for m in messages), messages


def test_the_advisory_sample_really_is_advisory_only():
    """自证:上一条用的样本必须是「硬门放行 + advisory 命中」。

    样本要是本来就会被硬门拦住,上一条测的就是另一件事(而且会以 raise 收场)。
    """
    from services.kb_terminology_gate import kb_index_advisories, kb_write_violations

    assert kb_write_violations(ADVISORY_ONLY_ANSWER) == []
    assert kb_index_advisories(ADVISORY_ONLY_ANSWER)


def test_batch_rejection_carries_the_fix_hints():
    """🔴 顺手修的那一处也欠一条判据。

    委托上去才发现:``assert_chunk_rows_clean`` 把 ``<slug>: `` 前缀加进
    ``problems`` 之后又拿它去 ``fixes_for`` 匹配前缀 ⇒ **批量写径的异常
    从来没带过改法**(两个 kb_db writer 都走这条)。已改成用未加前缀的原始命中
    去取改法。打在分岔那一格上:``fixes`` 非空 + ``problems`` 仍带 slug。
    """
    from services.kb_terminology_gate import KbTerminologyViolation, assert_chunk_rows_clean

    with pytest.raises(KbTerminologyViolation) as exc:
        assert_chunk_rows_clean(
            [{"source_slug": "faq_9", "source_title": "怎么充值",
              "content": DIRTY_ANSWER}],
            where="probe.batch")
    err = exc.value
    assert err.fixes, "批量写径又变回「拒绝但不给改法」"
    assert any("算力" in f for f in err.fixes), err.fixes
    assert err.problems[0].startswith("faq_9: "), err.problems


def test_fix_hints_are_matched_not_manufactured():
    """反向对照:认不出的命中就该没有改法 —— 否则上一条靠「一律返回点什么」也能过。"""
    from services.kb_terminology_gate import fixes_for

    assert fixes_for(["某个本表里没有的命中类型 → 原句"]) == []


def test_seed_gate_does_not_reimplement_the_aggregation():
    """结构锚:seed 门必须**委托**,不许自己再拼一份聚合器。

    同一个谓词写三处,必有一处漏 —— 上一版漏的就是 advisory。
    """
    import db.faq_db as faq_db

    tree = ast.parse(inspect.getsource(faq_db._assert_seed_text_clean))
    body = ast.unparse(tree)
    # 🔴 不能只搜名字:``import ... as _acrc`` 这种改写会让裸名字照样在源码里
    #    (变异 N17 存活实测)—— 那是把 import 当调用。要的是**真有一次调用**。
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "assert_chunk_rows_clean" in called, ("没有委托,实际调用的是", sorted(called))
    assert "raise KbTerminologyViolation" not in body, "又自己拼了一份异常"
    assert "kb_write_violations" not in called, "又自己扫了一遍"
