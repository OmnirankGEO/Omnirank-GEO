"""E3-2 · 广告法修复:正文与机审刷新**同事务全成或全退**(Codex 二审 P1-F7)
   + 多命中 typed 歧义(同报告 P2)。

为什么判据落在**这个**包而不是新开一个
------------------------------------
本项要读的三样东西 —— ``articles.content`` / ``current_content_hash``
(article hash)/ ``article_review``(review hash)—— 全都长在生产 dump 的
``articles`` 表上,而 C 包的底座已经装了生产 dump、跑过真机审、
``_reviewed_article`` 那三条同构条件也是逐条实测出来的。
另起一个底座 = 同一个夹具谓词写两处,必有一处没人验(本仓记过)。

被修的缺陷(亲证,坐标见 legal_repair.py 里那段注释)
----------------------------------------------------
``apply_repair`` 先 UPDATE 正文,再用裸 ``except Exception`` 吞掉
``refresh_article_review`` 的异常,然后**照样返回新 hash**;
API 于是返回 200 + "这一句已经改进稿子里了"。两条分支都坏:
  · DB 异常 ⇒ 事务 aborted ⇒ 调用方那次 commit 实际是 ROLLBACK ⇒
    正文根本没改,而她被告知改好了;
  · 普通异常 ⇒ 正文提交、review hash 停在旧值 ⇒ 重新确认命中
    ``content_changed_after_review``(§3A 起是 H0 硬拦,不可覆盖)。
"""

from __future__ import annotations

import pytest

from services.defensive_geo import legal_repair as _repair
from tests.defgeo_woc_closure_2026_08_25 import _seed
from tests.defgeo_woc_closure_2026_08_25.conftest import connect

from tests.defgeo_woc_closure_2026_08_25.test_c5_legal_repair_apply_pg import (
    BAD, GOOD, _new_article, _passage_ref, _review_row, _reviewed_article,
)

def _new_article_with_two_hits() -> tuple[int, int]:
    """歧义臂的输入:同一句违规话在正文里出现**两次**。

    🔴 正文复用本文件既有的 ``_TWICE``(见下方 ② 组)——
       第一版我另起了一个同形状的常量,那就是"同一个夹具谓词写两处",
       两份哪天分叉都不会有判据红。``_TWICE`` 在本文件后面定义,
       所以这里在函数体内取,不在模块级。
    """
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    quote = _seed.new_quote(brand_id=brand)
    return _seed.new_article(quote_id=quote, content=_TWICE), quote


@pytest.fixture(scope="module", autouse=True)
def _identities():
    _seed.install_identities()


#: review hash 在 ``articles.article_review`` 里的**真键名**。
#:
#: 🔴 第一版我按猜的写成 ``body_hash`` / ``bodyHash`` —— 库里没有这个键,
#:    于是 ``_snapshot`` 的第三格恒为 ``''``,``'' == ''`` 让 e2_01 因为
#:    **一个恒空的字段**而通过。空字段两次相等,与"它真的没变"长得一模一样。
#:    真键名是 ``reviewed_content_hash``(``refresh_article_review`` 落的那一格,
#:    也正是发布门用来判 ``content_changed_after_review`` 的那一格)。
#:    下面 ``_assert_snapshot_is_not_vacuous`` 钉住它不许再退回空。
_REVIEW_HASH_KEY = "reviewed_content_hash"


def _snapshot(article_id: int) -> tuple[str, str, str]:
    """(正文, article hash, review hash)—— 三者必须**同时**不变。"""
    row = _review_row(article_id)
    review = row.get("article_review") or {}
    return (
        str(row.get("content") or ""),
        str(row.get("current_content_hash") or ""),
        str(review.get(_REVIEW_HASH_KEY) or ""),
    )


def _assert_snapshot_is_not_vacuous(snap: tuple[str, str, str]) -> None:
    """三格都必须**非空**,否则"没变"这件事是对空气说的。"""
    body, article_hash, review_hash = snap
    assert body, "正文是空的 —— 前提没立住"
    assert article_hash, "article hash 是空的 —— 前提没立住"
    assert review_hash, (
        f"review hash({_REVIEW_HASH_KEY})取不到 —— 键名写错时它恒为空,"
        "而空 == 空会让「三者不变」恒真")


def _apply_with_broken_refresh(article_id: int, *, kind: str) -> Exception:
    """驱动真 ``apply_repair``,只把 ``refresh_article_review`` 换成会抛的。

    🔴 换的是**被调方**,不是 apply_repair 自己 —— 换 apply_repair 就是
       让夹具替被测代码干活,判据会恒绿(本仓记过)。
    🔴 两臂的区别是异常**类别**,不是文案:
       · ``db`` 臂发一条真会报错的 SQL ⇒ psycopg2 把事务标成 aborted。
         这一臂才是 P1-F7 最毒的那半:不修的话正文根本没落库。
       · ``plain`` 臂抛一个纯 Python 异常 ⇒ 事务仍然可用,正文会提交、
         review hash 停在旧值。
    """
    import services.article_review_gate as _gate
    from db.connection import get_db

    original = _gate.refresh_article_review

    def _boom(aid, *, cursor=None):
        if kind == "db":
            cursor.execute("SELECT 1 FROM defgeo_no_such_table_e2")
        raise RuntimeError("refresh blew up (plain)")

    _gate.refresh_article_review = _boom
    try:
        with get_db() as conn:
            with pytest.raises(_repair.LegalRepairNotApplied) as exc:
                _repair.apply_repair(
                    conn.cursor(), article_id=article_id,
                    passage_ref=_passage_ref(article_id, 0, len(BAD)),
                    passage_excerpt=BAD, chosen_text=GOOD)
        return exc.value
    finally:
        _gate.refresh_article_review = original


# ══════════════════════════════════════════════════════════════════════
# ① 两臂:刷新炸了 ⇒ 三者全不变 + typed「未应用」
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("kind", ["db", "plain"])
def test_e2_01_a_failed_refresh_leaves_body_and_both_hashes_untouched(kind: str) -> None:
    article_id = _reviewed_article()
    before = _snapshot(article_id)
    _assert_snapshot_is_not_vacuous(before)

    _apply_with_broken_refresh(article_id, kind=kind)

    after = _snapshot(article_id)
    assert after == before, (
        f"[{kind} 臂] 刷新失败之后状态变了 —— 正文/article hash/review hash "
        f"必须三者全不变。\n  before={before}\n  after ={after}")


def test_e2_02_the_failure_is_typed_as_not_applied_not_as_a_rejection() -> None:
    """typed 结果必须是「未应用 + 可重试」,不是「你给的句子不行」。

    🔴 责任方不同,她该做的动作正好相反:``LegalRepairApplyError`` 要她改输入,
       ``LegalRepairNotApplied`` 要她原样再点一次。合成一条 = 用同一句话
       解释两件相反的事。
    """
    article_id = _reviewed_article()
    exc = _apply_with_broken_refresh(article_id, kind="db")
    assert isinstance(exc, _repair.LegalRepairNotApplied)
    assert not isinstance(exc, _repair.LegalRepairApplyError), (
        "「我们没做成」被归进了「你给的句子不行」那一类")


def test_e2_03_the_connection_survives_the_rollback_and_the_next_apply_works() -> None:
    """判别力自证 + 真实后果:回滚之后这条连接必须还能用。

    🔴 没有这一条,"抛个异常就算修好了"也能让上面两条全绿 ——
       而那种实现会把调用方的事务留在 aborted 状态,``get_db()`` 出块时
       连 commit 都发不出去。``ROLLBACK TO SAVEPOINT`` 是把 aborted 事务
       救回来的**唯一**语句,这一条打的就是它真的被发了。
    """
    from db.connection import get_db

    article_id = _reviewed_article()
    before = _snapshot(article_id)

    import services.article_review_gate as _gate
    original = _gate.refresh_article_review

    def _boom(aid, *, cursor=None):
        cursor.execute("SELECT 1 FROM defgeo_no_such_table_e2")

    with get_db() as conn:
        cur = conn.cursor()
        _gate.refresh_article_review = _boom
        try:
            with pytest.raises(_repair.LegalRepairNotApplied):
                _repair.apply_repair(
                    cur, article_id=article_id,
                    passage_ref=_passage_ref(article_id, 0, len(BAD)),
                    passage_excerpt=BAD, chosen_text=GOOD)
        finally:
            _gate.refresh_article_review = original
        # 同一条连接、同一个事务:救回来之后必须能正常跑完一次真修复。
        out = _repair.apply_repair(
            cur, article_id=article_id,
            passage_ref=_passage_ref(article_id, 0, len(BAD)),
            passage_excerpt=BAD, chosen_text=GOOD)
        assert out["articleHash"] != out["previousArticleHash"]

    after = _snapshot(article_id)
    assert after != before, "第二次(正常)修复没落库 —— 那说明连接没被救回来"
    assert GOOD in after[0]


def test_e2_04_the_happy_path_still_moves_all_three(_wipe=None) -> None:
    """判别力自证:正常路径必须**三者都动**。

    没有这一条,把 ``apply_repair`` 改成"永远抛 NotApplied"也能让
    e2_01 全绿 —— 那是一个"永远不修复"的实现。
    """
    from db.connection import get_db

    article_id = _reviewed_article()
    before = _snapshot(article_id)
    _assert_snapshot_is_not_vacuous(before)
    with get_db() as conn:
        _repair.apply_repair(
            conn.cursor(), article_id=article_id,
            passage_ref=_passage_ref(article_id, 0, len(BAD)),
            passage_excerpt=BAD, chosen_text=GOOD)
    after = _snapshot(article_id)
    assert after[0] != before[0], "正文没变"
    assert after[1] != before[1], "article hash 没变"
    assert after[2] != before[2], "review hash 没变(机审没跟着刷新)"


# ══════════════════════════════════════════════════════════════════════
# ② 多命中 ⇒ typed 歧义,不取第一处
# ══════════════════════════════════════════════════════════════════════

_TWICE = "开头一段。\n\n" + BAD + "\n\n中间一段。\n\n" + BAD + "\n\n结尾一段。"


def test_e2_10_a_passage_that_appears_twice_is_ambiguous_not_first_hit() -> None:
    """同一句在正文里出现两次、且旧偏移对不上 ⇒ typed 歧义,正文不动。

    🔴 旧行为取**第一处**。真实后果不是"少改一句",是:她看到"已修复",
       而真正命中违规的那一处原封不动地发出去了。
    """
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    quote = _seed.new_quote(brand_id=brand)
    article_id = _seed.new_article(quote_id=quote, content=_TWICE)
    before = _review_row(article_id)["content"]

    from db.connection import get_db

    with get_db() as conn:
        with pytest.raises(_repair.LegalRepairAmbiguousError) as exc:
            _repair.apply_repair(
                conn.cursor(), article_id=article_id,
                # 偏移**故意**给一个对不上的(模拟"中间有人改过稿"),
                # 逼它走回落那一级 —— 歧义只可能在那一级出现。
                passage_ref=_passage_ref(article_id, 0, len(BAD)),
                passage_excerpt=BAD, chosen_text=GOOD)
    assert "不止一处" in str(exc.value) or "2 处" in str(exc.value), str(exc.value)
    assert _review_row(article_id)["content"] == before, "歧义时正文动了"


def test_e2_11_an_exact_offset_still_wins_even_when_the_text_repeats() -> None:
    """判别力自证:偏移**对得上**时,重复出现不算歧义。

    🔴 没有这一条,把歧义判定写成"只要 count>1 就抛"也能让上一条全绿 ——
       而那会把「偏移精确命中第二处」这条完全正常的路径一起打死。
    """
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    quote = _seed.new_quote(brand_id=brand)
    article_id = _seed.new_article(quote_id=quote, content=_TWICE)
    second = _TWICE.rindex(BAD)

    from db.connection import get_db

    with get_db() as conn:
        out = _repair.apply_repair(
            conn.cursor(), article_id=article_id,
            passage_ref=_passage_ref(article_id, second, second + len(BAD)),
            passage_excerpt=BAD, chosen_text=GOOD)
    assert out["articleHash"] != out["previousArticleHash"]
    body = _review_row(article_id)["content"]
    assert body.count(BAD) == 1, "改的不是偏移指定的那一处"
    assert body.index(GOOD) > body.index(BAD), "改成了第一处 —— 偏移没被尊重"


def test_e2_12_a_single_occurrence_with_a_drifted_offset_still_applies() -> None:
    """判别力自证:只出现一次时,回落那一级必须照常工作(不被歧义误伤)。"""
    conn_body = "前面。\n\n" + BAD + "\n\n后面。"
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    quote = _seed.new_quote(brand_id=brand)
    article_id = _seed.new_article(quote_id=quote, content=conn_body)

    from db.connection import get_db

    with get_db() as conn:
        out = _repair.apply_repair(
            conn.cursor(), article_id=article_id,
            passage_ref=_passage_ref(article_id, 0, len(BAD)),   # 偏移对不上
            passage_excerpt=BAD, chosen_text=GOOD)
    assert out["articleHash"] != out["previousArticleHash"]
    assert GOOD in _review_row(article_id)["content"]


# ══════════════════════════════════════════════════════════════════════
# ③ 对外 typed:两条新出口都得有人话 + 可执行的下一步
# ══════════════════════════════════════════════════════════════════════

def test_e2_20_both_new_outcomes_have_public_copy_and_a_next_step() -> None:
    """两条新 typed 出口必须能构造出**带人话 + 带 label** 的信封。

    🔴 ``_safe_error`` 是构造期 fail-closed 的:reason 取不到、
       nextAction 没 label,它当场 RuntimeError。所以这一条不是"看看文案在不在",
       而是真的走一遍那道门。
    """
    from api.defensive_geo_api import _action, _safe_error

    cases = (
        ("VALIDATION_FAILED", "legal_repair_ambiguous", "legal_repair_pick_again", False),
        ("POLICY_UNAVAILABLE", "legal_repair_not_applied", "retry_legal_repair", True),
    )
    for code, reason_key, action_key, retryable in cases:
        exc = _safe_error(
            code, reason_key=reason_key,
            next_action=_action(action_key,
                                target={"kind": "article_revision", "id": 1}))
        payload = exc.detail
        assert payload["code"] == code
        assert payload["retryable"] is retryable, (
            f"{reason_key} 的可重试性判错 —— 「我们没做成」可重试,"
            "「这段选不出唯一一处」重试多少次都一样")
        assert payload["publicExplanation"], f"{reason_key} 没有人话"
        assert payload["nextAction"]["label"], f"{action_key} 没有可渲染的 label"


def test_e2_21_the_new_copy_says_the_draft_was_not_touched() -> None:
    """两句新文案都必须先回答"稿子动了没有"。

    她在这一刻最怕的不是"失败了",是"点了一下,现在稿子成什么样我不知道"。
    """
    from services.defensive_geo.copy_registry import user_label

    for key in ("legal_repair_not_applied", "legal_repair_ambiguous"):
        text = user_label("reason", key)
        assert "一个字都没动" in text, f"{key} 没说清稿子没动:{text}"
    # 工程词禁区(与 U-1 同口径):这两句是给非技术销售看的。
    for key in ("legal_repair_not_applied", "legal_repair_ambiguous"):
        text = user_label("reason", key)
        for banned in ("事务", "回滚", "hash", "SAVEPOINT", "机审", "review"):
            assert banned not in text, f"{key} 里出现工程词 {banned!r}:{text}"

def _repair_apply_handler_mapping() -> tuple[list[tuple[str, str]], list[str]]:
    """从**真源码**取「异常类型 → typed code」的映射,以及 except 子句的**顺序**。

    分母 = 那个 handler 里所有 ``except`` 子句,机械枚举。
    """
    import ast
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[2]
    tree = ast.parse((root / "api/defensive_geo_assist_api.py").read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))                 and "apply_repair" in ast.dump(node):
            target = node
    assert target is not None, "没定位到调 apply_repair 的那个 handler —— 探针失效"

    mapping: list[tuple[str, str]] = []
    order: list[str] = []
    for h in ast.walk(target):
        if not isinstance(h, ast.ExceptHandler) or h.type is None:
            continue
        exc_name = getattr(h.type, "id", None) or getattr(h.type, "attr", None)
        if not exc_name or not exc_name.startswith("LegalRepair"):
            continue
        order.append(exc_name)
        mapping.append((exc_name, _reachable_safe_error_code(h)))
    return mapping, order


def _reachable_statements(block):
    """一个语句块里**会执行到**的那些语句。

    规则只有一条:块内出现第一条无条件 ``raise`` / ``return`` /
    ``continue`` / ``break`` 之后,后面的语句在这个块里**不可达**。
    """
    import ast

    out = []
    for stmt in block or []:
        out.append(stmt)
        if isinstance(stmt, (ast.Raise, ast.Return, ast.Continue, ast.Break)):
            break
    return out


def _reachable_safe_error_code(handler):
    """这个 except 子句**实际会 raise 出去**的那个 typed code。

    🔴 [外选 MUT-EXTE3-07] 原来这里是 ``for sub in ast.walk(h)`` + 无 break 的
       后见覆盖:谁最后被访问到,谁就是答案。于是在 ``raise ... from exc``
       **之后**多放一个死的 ``_safe_error("VALIDATION_FAILED")``,就能把 census
       喂回正确答案,而真正 raise 出去的是另一个 code —— 判据全绿。
       这与 ``if False and ...`` 骗过接线锁是同一类:**AST 只认在场,不认会走**。
       所以这里先按可达性裁掉死代码,再在**可达**范围里取 code。
    """
    import ast

    code = None
    for stmt in _reachable_statements(getattr(handler, "body", [])):
        for sub in ast.walk(stmt):
            if (isinstance(sub, ast.Call)
                    and getattr(sub.func, "id", "") == "_safe_error"
                    and sub.args and isinstance(sub.args[0], ast.Constant)):
                code = sub.args[0].value
    return code


def _unreachable_statements(fn):
    """枚举函数里**排在无条件 raise/return 之后**的语句(同一个块内)。

    这是上面那条可达性规则的另一半:裁掉死代码只让 census 不被骗,
    而死代码**本身**还留在源码里 —— 下一个读代码的人会以为它会跑。
    """
    import ast

    dead = []
    for node in ast.walk(fn):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block):
                if isinstance(stmt, (ast.Raise, ast.Return, ast.Continue, ast.Break)):
                    for ghost in block[i + 1:]:
                        dead.append((getattr(ghost, "lineno", -1),
                                     type(ghost).__name__))
                    break
    return dead


def test_e2_22_the_handler_really_maps_each_outcome_to_the_right_code() -> None:
    """🔴 [撕锁补洞 · MUT-E2-04 存活] 判据必须读**handler 实际 raise 的那个 code**。

    ``test_e2_20`` 拿 code 当**入参**自己构造信封 —— 它验的是「给定这个 code,
    信封长得对不对」,而不是「handler 会不会给这个 code」。
    于是把歧义那一支从 ``VALIDATION_FAILED`` 改成 ``POLICY_UNAVAILABLE``
    (= 变成**可重试**,前端会对一个永远不会变的答案自动重试)整发**存活**。
    本仓记过这一条:「判据自己构造了中间值」——为可判性抽出映射再验,才算有人守。
    """
    mapping, _order = _repair_apply_handler_mapping()
    assert len(mapping) >= 3, f"except 子句 census 塌了:{mapping}"

    from api.defensive_geo_api import _ERROR_TABLE

    got = dict(mapping)
    assert got.get("LegalRepairAmbiguousError") == "VALIDATION_FAILED", (
        f"歧义映射成了 {got.get('LegalRepairAmbiguousError')!r} —— "
        "它必须**不可重试**:再点一次仍然是同样多处")
    assert got.get("LegalRepairNotApplied") == "POLICY_UNAVAILABLE", (
        f"「我们没做成」映射成了 {got.get('LegalRepairNotApplied')!r} —— "
        "它必须**可重试**:她的输入没问题")

    # 语义那一位:两条出口的可重试性必须**相反**,否则合成一条就没区别了。
    amb_retryable = _ERROR_TABLE[got["LegalRepairAmbiguousError"]][1]
    not_applied_retryable = _ERROR_TABLE[got["LegalRepairNotApplied"]][1]
    assert amb_retryable is False and not_applied_retryable is True, (
        f"可重试性判错:歧义={amb_retryable} / 未应用={not_applied_retryable}")


def test_e2_23_the_subclass_clause_comes_before_its_parent() -> None:
    """``LegalRepairAmbiguousError`` 是 ``LegalRepairApplyError`` 的子类。

    🔴 except 子句按**书写顺序**匹配:父类子句排在前面时,歧义会被它吃掉,
       悄悄退回一个泛化的 503 可重试 —— 而这正是上一条要挡的那件事,
       只是换成了"改顺序"这种看起来无害的编辑。
    """
    from services.defensive_geo import legal_repair as _lr

    assert issubclass(_lr.LegalRepairAmbiguousError, _lr.LegalRepairApplyError), (
        "前提变了:歧义不再是 ApplyError 的子类,本条的论证要重写")

    _mapping, order = _repair_apply_handler_mapping()
    assert "LegalRepairAmbiguousError" in order and "LegalRepairApplyError" in order, order
    assert order.index("LegalRepairAmbiguousError") < order.index("LegalRepairApplyError"), (
        f"子类子句排在父类之后 {order} —— 歧义会被父类子句吃掉")


# ══════════════════════════════════════════════════════════════════════
# ④ [外选 MUT-EXTE3-07] 可达性 + 真 HTTP
#
# 那一发是**配对**的两半:
#   a. 歧义臂 raise 的 code 从 VALIDATION_FAILED 改成 POLICY_UNAVAILABLE
#      (= 把"这段选不出唯一一处"变成**可重试** ⇒ 前端对一个永远不变的答案
#       自动重试,死循环);
#   b. 在同一个 handler 里、``raise`` **之后**多放一个死的
#      ``_safe_error("VALIDATION_FAILED")`` —— 专门喂饱那个不看可达性的 census。
# 三条既有判据(e2_20 自构信封 / e2_22 被死影子喂饱 / e2_23 顺序不变)全绿,
# 而歧义臂**没有任何真 HTTP 判据** —— 全分母零红。
# ══════════════════════════════════════════════════════════════════════

def test_e2_24_the_repair_handler_has_no_unreachable_statements() -> None:
    """那个 handler 里不许有排在 ``raise`` 之后的语句。

    🔴 死代码在这里不是"风格问题":它是**喂 AST census 的饲料**。
       ``raise`` 之后那一行永远不会执行,却会被 ``ast.walk`` 访问到 ——
       任何"看见就算数"的静态判据都会把它当成真答案。
       (同族:``if False and not plan_cell_ids`` 骗过接线锁。)
    """
    import ast
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[2]
    tree = ast.parse((root / "api/defensive_geo_assist_api.py").read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and "apply_repair" in ast.dump(node):
            target = node
    assert target is not None, "没定位到调 apply_repair 的那个 handler —— 探针失效"

    dead = _unreachable_statements(target)
    assert not dead, (
        f"handler 里有不可达语句(行号, 类型):{dead} —— "
        "raise 之后的代码永远不会跑,但会被静态 census 当成真答案")


def test_e2_25_the_reachability_probe_can_actually_fail() -> None:
    """判别力自证:可达性两件工具必须真的认得出 MUT-EXTE3-07 的那半。

    正样本**逐字取自**那一发的替换文本(死影子那半),不是我另编一个好抓的形状。
    """
    import ast

    import textwrap

    poisoned = ast.parse(textwrap.dedent("""
        def h():
            try:
                pass
            except A as exc:
                raise _safe_error('POLICY_UNAVAILABLE') from exc
                _safe_error('VALIDATION_FAILED')
        """))
    clean = ast.parse(textwrap.dedent("""
        def h():
            try:
                pass
            except A as exc:
                raise _safe_error('VALIDATION_FAILED') from exc
        """))

    assert _unreachable_statements(poisoned), "死代码探测器放过了 raise 之后的语句"
    assert not _unreachable_statements(clean), "干净代码被误判成有死代码(恒红的锁没人看)"

    # census 的那一半:死影子**不许**改变答案。
    def _handler(mod):
        return next(n for n in ast.walk(mod) if isinstance(n, ast.ExceptHandler))

    assert _reachable_safe_error_code(_handler(poisoned)) == "POLICY_UNAVAILABLE", (
        "census 被 raise 之后的死影子喂饱了 —— 它读到的不是真正 raise 出去的 code")
    assert _reachable_safe_error_code(_handler(clean)) == "VALIDATION_FAILED"


# ── 真 HTTP 臂 ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def assist_client():
    """只挂**本端点的 router**,不挂 server.py 全量 app。

    挂全量会把 200+ 个无关 router 拉进来,任何一个 import 失败都会让本条
    以"跟本端点无关的理由"变红 —— 分不清是端点坏了还是别人的。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.defensive_geo_assist_api as mod

    app = FastAPI()

    @app.middleware("http")
    async def _principal(request, call_next):        # noqa: ANN001,ANN202
        # 🔴 键名 ``user_id`` —— 生产中间件写的就是这个。夹具写 ``id``
        #    会造出"生产从来不会发的键",端点线上必挂而判据全绿(本仓记过)。
        request.state.user = {"user_id": _seed.TENANT_A}
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(mod.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def _allow_article(monkeypatch):
    """放行**归属**校验,让判据打得到 handler 主体。

    🔴 归属本身不在这条判据的作用域里 —— 它由现役
       ``_require_article_access`` 的自有判据守。这里放行是为了不把
       "授权没配好"和"错误映射错了"两件事混成同一个红。
    """
    import api.meijiehezi_api as _mh

    monkeypatch.setattr(_mh, "_require_article_access",
                        lambda request, article_id: None)


def _apply_over_http(client, *, article_id: int, ref: str, excerpt: str, chosen: str):
    return client.post(
        "/api/defensive-geo/legal-repair/apply",
        json={"articleRevisionId": f"article:{article_id}", "ruleId": "abs-superlative",
              "passageRef": ref, "passageExcerpt": excerpt, "chosenText": chosen})


def test_e2_26_the_ambiguous_arm_is_a_non_retryable_422_over_real_http(
        assist_client, _allow_article) -> None:
    """🔴 歧义臂的**真 HTTP** 判据 —— 缺的就是这一条。

    上面那些判据要么自己构造信封(e2_20),要么读源码(e2_22/23);
    没有一条真的打过这条端点。于是"歧义 ⇒ 可重试 503"这个谎
    在全分母下零红:前端会对一个**永远不会变**的答案自动重试。

    这一条从 HTTP 那一侧问同一个问题,答案必须是:
      · 422(**不可重试**),不是 503;
      · 信封里 ``retryable is False``,有人话、有可渲染的下一步;
      · 正文**一个字没动**(两处原句都还在)。
    """
    article_id, _quote = _new_article_with_two_hits()
    before = _seed.article_content(article_id)

    r = _apply_over_http(assist_client, article_id=article_id,
                         ref=_passage_ref(article_id, 0, 1),   # 偏移故意对不上
                         excerpt=BAD, chosen=GOOD)

    assert r.status_code == 422, (
        f"歧义臂返回 {r.status_code}(期望 422 不可重试)。"
        f"503 = 可重试 ⇒ 前端会对一个永远不会变的答案死循环重试。响应:{r.text}")
    detail = (r.json() or {}).get("detail") or {}
    assert detail.get("code") == "VALIDATION_FAILED", detail
    assert detail.get("retryable") is False, (
        f"歧义被标成可重试:{detail} —— 再点一次仍然是同样多处")
    assert detail.get("publicExplanation"), f"没有人话:{detail}"
    assert (detail.get("nextAction") or {}).get("label"), f"没有可渲染的下一步:{detail}"
    assert _seed.article_content(article_id) == before, (
        "歧义被拒绝了,正文却动过 —— 拒绝路径必须零副作用")


def test_e2_27_the_same_client_really_reaches_the_success_path(
        assist_client, _allow_article) -> None:
    """判别力自证:同一个 client、同一条端点,单命中时必须 200 并真的改到稿子。

    没有这一条,上一条的 422 可能来自任何别的原因(入参没过、授权没放行、
    路由没挂上)—— 那样"歧义被正确拒绝"就成了一句碰巧成立的话。
    """
    article_id, _quote = _new_article()
    before = _seed.article_content(article_id)
    start = before.index(BAD)

    r = _apply_over_http(assist_client, article_id=article_id,
                         ref=_passage_ref(article_id, start, start + len(BAD)),
                         excerpt=BAD, chosen=GOOD)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["articleHash"] != body["previousArticleHash"], body
    after = _seed.article_content(article_id)
    assert GOOD in after and BAD not in after, after


# ══════════════════════════════════════════════════════════════════════
# [工单 V3-C · C-5 · Codex 三审 P2-2] 输入类拒绝不许是"可重试 503"
# ══════════════════════════════════════════════════════════════════════
def test_e2_28_a_plain_input_rejection_is_a_non_retryable_422_over_real_http(
        assist_client, _allow_article) -> None:
    """``LegalRepairApplyError`` 的全部 raiser 都是**她能自己处理**的输入问题。

    逐条看:空句 / 这一句仍然命中广告法目录 / 与原句一模一样 /
    原话已不在正文里 / 文章不存在。没有一条是"我们的服务不可用"。
    上一版把它们统一映射成 ``POLICY_UNAVAILABLE``(503 + retryable),
    于是前端会自动重试一件**永远不会成功**的事,而她看到的是"稍后再试"。

    这里打的形态是最常见的那一种:她在候选基础上手改,又改回了一个禁词
    (``LegalRepairPanel`` 第二步就是那个输入框)。
    """
    article_id, _quote = _new_article()
    before = _seed.article_content(article_id)
    start = before.index(BAD)

    r = _apply_over_http(assist_client, article_id=article_id,
                         ref=_passage_ref(article_id, start, start + len(BAD)),
                         excerpt=BAD, chosen=BAD)      # 改回禁词 = 输入问题

    assert r.status_code == 422, (
        f"输入类拒绝返回 {r.status_code}(期望 422 不可重试)。"
        f"503 = 可重试 ⇒ 前端会对一个永远不会成功的请求死循环。响应:{r.text}")
    detail = (r.json() or {}).get("detail") or {}
    assert detail.get("code") == "VALIDATION_FAILED", detail
    assert detail.get("retryable") is False, (
        f"输入问题被标成可重试:{detail}")
    assert detail.get("publicExplanation"), f"没有人话:{detail}"
    assert (detail.get("nextAction") or {}).get("label"), (
        f"没有可渲染的下一步 —— 她不知道该改什么:{detail}")
    assert _seed.article_content(article_id) == before, (
        "被拒绝了,正文却动过 —— 拒绝路径必须零副作用")


def test_e2_29_the_three_outcomes_split_by_who_can_fix_it() -> None:
    """三条出口的可重试性必须**按责任方**分,而不是三条都一样。

    · ``LegalRepairAmbiguousError``(定位不了)  → 不可重试,她重新选段;
    · ``LegalRepairApplyError``(这句落不下去)   → 不可重试,她改句子;
    · ``LegalRepairNotApplied``(我们没做成)     → **可重试**,原样再点一次。

    🔴 三条都读 handler **实际 raise 的 code**(不是判据自己构造的),
       所以把任意一条改档都会当场红。
    """
    from api.defensive_geo_api import _ERROR_TABLE

    mapping, _order = _repair_apply_handler_mapping()
    got = dict(mapping)
    expected = {
        "LegalRepairAmbiguousError": False,
        "LegalRepairApplyError": False,
        "LegalRepairNotApplied": True,
    }
    missing = [k for k in expected if k not in got]
    assert not missing, f"except 子句 census 少了 {missing}:{got}"
    for exc_name, retryable in expected.items():
        code = got[exc_name]
        assert _ERROR_TABLE[code][1] is retryable, (
            f"{exc_name} 映射成 {code}(retryable={_ERROR_TABLE[code][1]}),"
            f"期望 retryable={retryable} —— 可重试性按**责任方**分:"
            "她能修的不许自动重试,我们没做成的才可以")
    # 判别力自证:三条不许全同档,否则"按责任方分"这句话没有内容
    assert len(set(expected.values())) == 2, "期望表自己塌了"
