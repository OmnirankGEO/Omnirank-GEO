"""C-5 · 「用这一句」必须**真的**落成新 revision(Codex 终审 P1-12)。

被测的那一格:前端点完"用这一句"只是
``navigate('/writing?revision=…&repaired=<句子>')``,而那个 ``repaired``
查询参数**全仓零消费者**。于是重新确认冻的还是同一份正文、同一个
``articleHash``、同一个 ``canonicalHash``,发布门原样再拦一次。

端到端判据(工单逐字):**点了之后 confirm 的内容 hash 变了**。
"""

from __future__ import annotations

import pytest

from services.defensive_geo import legal_repair as _repair
from services.defensive_geo.publish import body_hash as _bh
from tests.defgeo_woc_closure_2026_08_25 import _seed
from tests.defgeo_woc_closure_2026_08_25.conftest import connect

BAD = "我们是全国第一的家装服务商。"
GOOD = "我们在深圳家装口碑榜上连续三年进入前十。"
BODY = "第一段正文。\n\n" + BAD + "\n\n第三段正文。"


@pytest.fixture(scope="module", autouse=True)
def _identities():
    _seed.install_identities()


def _passage_ref(article_id: int, start: int, end: int) -> str:
    """逐字用生产那条构造(``publish.legal_gate._passage_ref``),不手拼格式。"""
    from services.defensive_geo.publish.legal_gate import _passage_ref as _pr

    return _pr(f"article:{article_id}", start, end)


def _new_article() -> tuple[int, int]:
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    quote = _seed.new_quote(brand_id=brand)
    return _seed.new_article(quote_id=quote, content=BODY), quote


def _apply(article_id: int, *, chosen: str, ref: str, excerpt: str) -> dict:
    from db.connection import get_db

    with get_db() as conn:
        return _repair.apply_repair(
            conn.cursor(), article_id=article_id,
            passage_ref=ref, passage_excerpt=excerpt, chosen_text=chosen)


# ══════════════════════════════════════════════════════════════════════════
# A. 真落库 + 内容指纹真的变了
# ══════════════════════════════════════════════════════════════════════════
def test_c5_00_premise_the_bad_sentence_really_hits_the_signed_catalog() -> None:
    """前提自证:原句真的命中签发禁词表,改写句真的不命中。

    两向都验。只验前者的话,"改好了"可能只是因为尺子对所有句子都返空
    (那时下面每一条都是恒真)。
    """
    from services.marketing.legal_context import find_absolute_violations

    assert find_absolute_violations(BAD), "原句不命中禁词表 —— 本组判据失去被测对象"
    assert not find_absolute_violations(GOOD), "改写句仍命中禁词表 —— 样本选错了"


def test_c5_01_apply_changes_the_article_hash_end_to_end() -> None:
    """🔴 **本项的本体判据**:点了之后内容 hash 变了。

    ``publish_worker.load_frozen_body`` 逐字节核对冻结指纹,所以
    hash 一变,旧快照自动失效、重新预览/确认必然拿到新 hash ——
    这就是「重新确认消费新 revision」。

    拆红:把 ``apply_repair`` 的 UPDATE 摘掉 ⇒ hash 不变 ⇒ 本条红。
    """
    article_id, _ = _new_article()
    before = _bh.body_hash(_seed.article_content(article_id))
    start = BODY.index(BAD)

    out = _apply(article_id, chosen=GOOD,
                 ref=_passage_ref(article_id, start, start + len(BAD)),
                 excerpt=BAD)

    after_body = _seed.article_content(article_id)
    assert BAD not in after_body, "原句还在正文里 —— 根本没落库"
    assert GOOD in after_body, after_body
    assert out["previousArticleHash"] == before, out
    assert out["articleHash"] == _bh.body_hash(after_body), out
    assert out["articleHash"] != before, "内容 hash 没变 —— 重新确认还会冻同一份"


def test_c5_02_the_old_frozen_snapshot_can_no_longer_be_dispatched() -> None:
    """落库之后,拿**旧** hash 去取冻结正文必须失败。

    这一条比"hash 变了"更硬:它证明变化真的传导到了发布链那一侧
    ——「审 A 发 B」的防线用的就是这条逐字节核对。
    """
    from services.defensive_geo.publish.publish_worker import (
        BodyResolutionError, load_frozen_body,
    )

    article_id, _ = _new_article()
    old_hash = _bh.body_hash(_seed.article_content(article_id))
    start = BODY.index(BAD)
    _apply(article_id, chosen=GOOD,
           ref=_passage_ref(article_id, start, start + len(BAD)), excerpt=BAD)

    conn = connect()
    try:
        cur = conn.cursor()
        with pytest.raises(BodyResolutionError):
            load_frozen_body(cur, article_revision_id=f"article:{article_id}",
                             article_hash=old_hash)
        # 用**新** hash 取得到 —— 反向自证:失败不是因为取数本身坏了
        title, body = load_frozen_body(
            cur, article_revision_id=f"article:{article_id}",
            article_hash=_bh.body_hash(_seed.article_content(article_id)))
        assert GOOD in body, body
        conn.rollback()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# A2. [外选 EXTC-10] 最后一跳:「回去重新确认一次就能继续」到底能不能继续
# ══════════════════════════════════════════════════════════════════════════
def _reviewed_article() -> int:
    """一篇**跑过机审、机审判 blocked** 的稿件 —— 生产那一刻的形状。

    三件事让它与生产同构(缺任何一件,这条判据测的就是另一个东西):
      · ``generation_request_id`` 有值 —— 否则 ``build_article_lineage``
        每次现造一个 uuid4,证据清单指纹每刷新一次就漂一次;
      · ``evidence_pack`` 是**已 normalize** 的(带 ``created_at``)——
        否则 ``normalize_evidence_pack`` 每次盖一个新时间戳,同样漂;
      · ``evidence_manifest_hash`` 列与 pack 对齐 —— 生产写作时就是这么落的。
    (三条都是 2026-08-26 逐条实测出来的:漂移会让发布门在
     ``evidence_changed_after_review`` 上硬拦,而那与本项无关。)
    """
    import json

    from services.article_review_gate import refresh_article_review
    from writing.evidence_pack import normalize_evidence_pack

    article_id, _quote = _new_article()
    pack = normalize_evidence_pack({"created_at": "2026-08-01T00:00:00+00:00"},
                                   request_id="woc-c5-req")
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE articles SET generation_request_id=%s, evidence_pack=%s::jsonb,"
            " evidence_manifest_hash=%s WHERE id=%s",
            ("woc-c5-req", json.dumps(pack), pack["manifest_hash"], article_id))
        conn.commit()
    finally:
        conn.close()

    refresh_article_review(article_id)                    # 跑一遍真机审
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE articles SET article_review_status='blocked' WHERE id=%s",
                    (article_id,))
        conn.commit()
    finally:
        conn.close()
    return article_id


def _review_row(article_id: int) -> dict:
    import json

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT content, article_review_status, current_content_hash,"
                    "       article_review FROM articles WHERE id=%s", (int(article_id),))
        row = dict(cur.fetchone())
        conn.rollback()
    finally:
        conn.close()
    rv = row.get("article_review") or {}
    row["article_review"] = json.loads(rv) if isinstance(rv, str) else rv
    return row


def test_c5_04_after_apply_the_machine_verdict_and_the_publish_gate_move_on() -> None:
    """🔴 [外选 EXTC-10] 落库之后,**机审结论**与**发布门**都要跟着走。

    C-5 原有的 11 条判据全部停在内容指纹那一层(hash 变 / 旧快照失效 /
    逐字节最小改动 / 禁词再过尺 / 文案),**没有一条**读
    ``article_review_status``、也没有一条走到发布门。而工单语义的终点是
    成功文案对她说的那句「回去重新确认一次就能继续」——
    差的正是这最后一跳。

    ═══════════════════════════════════════════════════════════════════
    🔴 先把外选草单的说法订正:漏刷新的后果**比它说的更重**
    ═══════════════════════════════════════════════════════════════════
    草单写「``article_review_status`` 还是 blocked,发布门原样再拦一次」。
    实核:``blocked`` 自 2026-08-01 §3A 起**已经不是硬拦**了 ——
    它只进 ``content_notices``(``overridable: True``),``eligible`` 仍是 True。
    所以"原样再拦一次"这半句不成立。

    真实后果是另一条、且更糟:``apply_repair`` 改了 ``articles.content``,
    而机审快照里的 ``reviewed_content_hash`` 还是旧正文的 ⇒ 发布门命中
    ``content_changed_after_review``(``h0=operator_hard``、
    ``overridable: False`` —— **人工签发都盖不掉**)。
    也就是说:漏刷新不是"照旧被拦",是**从可发变成不可发**。

    四条锚一起打(任何一条单独都可能被别的原因满足):
      ① 机审结论离开了 ``blocked``;
      ② 机审快照的正文指纹追上了新正文;
      ③ 发布门 ``eligible``、``h0=clear``;
      ④ 而且不是撞在 ``content_changed_after_review`` 上。

    拆红:把 ``apply_repair`` 里那句 ``refresh_article_review`` 摘掉 ⇒ 四条全红。
    """
    import hashlib

    from services.article_review_gate import (
        evaluate_publication_eligibility, is_publication_review_gate_enabled,
    )

    assert is_publication_review_gate_enabled() is True, (
        "发布门被环境变量关掉了 —— 这条判据在当前环境下没有被测对象")

    article_id = _reviewed_article()

    # ── 前提自证:修之前确实是"机审判了 blocked、法律提示在" ────────────
    before_row = _review_row(article_id)
    assert before_row["article_review_status"] == "blocked", before_row
    before = evaluate_publication_eligibility(article_id)
    assert "legal_hard" in (before.get("content_notice_classes") or []), before
    assert before.get("review_state") == "computed", (
        f"机审快照没建起来(review_state={before.get('review_state')!r})—— "
        "那样下面的 hash lineage 检查根本不会被执行")
    assert before.get("reason") != "content_changed_after_review", before

    start = BODY.index(BAD)
    _apply(article_id, chosen=GOOD,
           ref=_passage_ref(article_id, start, start + len(BAD)), excerpt=BAD)

    after_row = _review_row(article_id)
    assert GOOD in after_row["content"], "正文没落库 —— 前一组判据该先红"
    # ① 机审结论真的重跑了
    assert after_row["article_review_status"] != "blocked", (
        f"正文改好了,机审结论还停在 blocked:{after_row['article_review_status']!r} —— "
        "「回去重新确认一次就能继续」这句话是假的")
    # ② 快照的正文指纹追上了新正文
    new_hash = hashlib.sha256(str(after_row["content"]).encode("utf-8")).hexdigest()
    assert after_row["article_review"].get("reviewed_content_hash") == new_hash, (
        "机审快照还挂在旧正文上 —— 发布门会判成「审完又被改过」,"
        "而那一档人工签发都盖不掉")
    assert str(after_row["current_content_hash"]) == new_hash, after_row

    # ③④ 发布门这一跳
    after = evaluate_publication_eligibility(article_id)
    assert after.get("reason") != "content_changed_after_review", (
        f"修完反而撞上 hash lineage 硬门:{after}")
    assert after.get("publication_h0_state") == "clear", after
    assert after.get("eligible") is True, after
    assert "legal_hard" not in (after.get("content_notice_classes") or []), after


def test_c5_05_the_gate_probe_can_still_say_no() -> None:
    """判别力反臂:同一把尺子在"审完又被改过"时必须判 **operator_hard**。

    没有这一条,上一条的 ``eligible is True`` 可能只是因为这个环境里
    发布门恒放行(比如闸被关了、或者 h0 派生位坏了)——
    那时它对"刷没刷新"零区分力。
    """
    import hashlib

    from services.article_review_gate import evaluate_publication_eligibility

    article_id = _reviewed_article()
    # 绕过 apply_repair,**直接**制造"正文变了、快照没跟上"这个状态
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE articles SET content = content || %s WHERE id=%s",
                    ("\n\n后补的一段。", int(article_id)))
        conn.commit()
    finally:
        conn.close()

    row = _review_row(article_id)
    stale = row["article_review"].get("reviewed_content_hash")
    assert stale != hashlib.sha256(
        str(row["content"]).encode("utf-8")).hexdigest(), "样本没造出漂移"

    verdict = evaluate_publication_eligibility(article_id)
    assert verdict.get("reason") == "content_changed_after_review", verdict
    assert verdict.get("publication_h0_state") == "operator_hard", verdict
    assert verdict.get("eligible") is False, verdict
    assert verdict.get("overridable") is False, (
        "这一档标成可覆盖了 —— 「审完再改」就成了绕过审核的后门")


def test_c5_03_only_that_passage_changes_the_rest_is_byte_identical() -> None:
    """除这一段外正文**逐字节不变**。

    修一句话不该顺手重写别的段落 —— 那是把"修复"变成"重写",
    而客户签的是原来那一版。
    """
    article_id, _ = _new_article()
    start = BODY.index(BAD)
    _apply(article_id, chosen=GOOD,
           ref=_passage_ref(article_id, start, start + len(BAD)), excerpt=BAD)
    after = _seed.article_content(article_id)
    assert after == BODY.replace(BAD, GOOD), after


# ══════════════════════════════════════════════════════════════════════════
# B. 落库前**再过一遍同一把尺子**
# ══════════════════════════════════════════════════════════════════════════
def test_c5_10_a_hand_edited_sentence_that_still_violates_is_refused() -> None:
    """她可以在候选基础上手改 ⇒ 最终那一串必须**再**过一遍尺子。

    候选生成时过了不代表定稿过了。判定必须打在**真正要落库的那串**上。
    """
    article_id, _ = _new_article()
    start = BODY.index(BAD)
    with pytest.raises(_repair.LegalRepairApplyError):
        _apply(article_id, chosen="我们仍然是全国第一。",
               ref=_passage_ref(article_id, start, start + len(BAD)), excerpt=BAD)
    assert _seed.article_content(article_id) == BODY, "被拒之后正文还是被改了"


def test_c5_11_drifted_body_refuses_instead_of_replacing_a_random_sentence() -> None:
    """正文在命中与修复之间被改过 ⇒ 拒,不按旧偏移替换。

    偏移指到别处时把一段**无关的话**替换掉,比不修糟得多。
    """
    article_id, _ = _new_article()
    start = BODY.index(BAD)
    ref = _passage_ref(article_id, start, start + len(BAD))

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE articles SET content=%s WHERE id=%s",
                    ("完全不同的一篇正文。", article_id))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(_repair.LegalRepairApplyError):
        _apply(article_id, chosen=GOOD, ref=ref, excerpt=BAD)
    assert _seed.article_content(article_id) == "完全不同的一篇正文。"


def test_c5_12_offset_drift_falls_back_to_the_exact_string() -> None:
    """偏移对不上但原句还在 ⇒ 回落到命中串定位(取第一处),不抛。

    命中串在原文里是原样存在的,这比偏移可靠 —— 现役
    ``writing_span_repair.locate_paragraph`` 用的也是这个理由。
    """
    article_id, _ = _new_article()
    # 故意给一个**错的**偏移(指向第一段)
    bad_ref = _passage_ref(article_id, 0, 5)
    out = _apply(article_id, chosen=GOOD, ref=bad_ref, excerpt=BAD)
    after = _seed.article_content(article_id)
    assert after == BODY.replace(BAD, GOOD), after
    assert out["passageBefore"] == BAD, out


def test_c5_13_choosing_the_original_sentence_is_refused() -> None:
    """选中的就是原句 ⇒ 拒。

    落库等于什么都没做,而返回"已修复"会骗人 —— 她会以为可以去确认了。
    """
    article_id, _ = _new_article()
    start = BODY.index(BAD)
    with pytest.raises(_repair.LegalRepairApplyError):
        _apply(article_id, chosen=BAD,
               ref=_passage_ref(article_id, start, start + len(BAD)), excerpt=BAD)


# ══════════════════════════════════════════════════════════════════════════
# C. 候选文案如实降级(Owner 2026-08-25 选 A)
# ══════════════════════════════════════════════════════════════════════════
def test_c5_20_candidate_note_no_longer_says_it_is_ready_to_use() -> None:
    """候选的 note 不许把「未检出禁词」讲成「可以直接用」。

    这里唯一做过的核验是把候选再过一遍 ``find_absolute_violations`` ——
    它不看标题、不看虚构门店数、不看未证实的数字。
    """
    import asyncio

    async def _fake_llm(_prompt: str) -> str:
        return GOOD + "\n" + "我们在深圳家装领域做了十年。"

    out = asyncio.run(_repair.build_candidate_payload(
        rule_id="ad_law_absolute", passage_excerpt=BAD,
        prompt="(判据不打真模型)", llm_fn=_fake_llm))
    assert out, "一条候选都没有 —— 判据没有被测对象"
    note = out[0]["note"]
    assert "可以直接用" not in note, note
    # 正向:必须**点名**没核过的那几类,而不是退成一句免责话
    assert "没核" in note and "数字" in note, note


def test_c5_21_census_declares_apply_has_a_real_landing() -> None:
    """可机读断言:「用这一句」有真落点、且落点会改内容指纹。"""
    c = _repair.census()
    assert c["applyWritesRevision"] is True, c
    assert c["applyReturnsNewArticleHash"] is True, c


def test_c5_30_the_dead_repaired_query_param_is_gone_from_the_frontend() -> None:
    """结构锚:前端不再往 ``/writing?...&repaired=`` 跳。

    那个查询参数**没有任何消费者**(全仓只有那一处产出方)。
    留着它 = 留一个"看起来接了、其实什么都没发生"的出口。
    """
    from tests.defgeo_woc_closure_2026_08_25.conftest import ROOT

    src = (ROOT / "frontend" / "src" / "pages" / "DefensivePublish"
           / "PublishCommandStatus.tsx").read_text(encoding="utf-8")
    # 判**代码**不判病历:注释里逐字引用旧写法是病历。只看模板字符串里的真跳转。
    assert "navigate(`/writing?revision=" not in src, (
        "前端还在往 /writing?revision=…&repaired= 跳 —— 死出口没摘")
    assert "applyLegalRepair(" in src, "前端没有接上真落点"
