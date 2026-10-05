# -*- coding: utf-8 -*-
"""WO_239-甲 · 「确认未提到」在没有任何候选名时也要能落库。

现场:#727 广东星衍朗(`is_test=false`,有分享链接)千问那格判定依据
`registry_name_correction_requires_review` —— 它**要求人复核名称**,却落库
`matched_text: null` + `identity_candidates: []`。点「确认未提到」⇒
「请选择候选名称或填写正确名称」,**做不完**。
该 reason 覆盖 **61 条**诊断,近期几乎全是真客户且都有分享链接。
🔴 纠错通道在**最需要它的那一类**上是断的。

修法**不是**新放宽的口径,是把**监测面早就有的概念**补到诊断面:
`db/monitoring_db.decide_monitoring_identity_review:5509` 逐字写着
    answer_absent = normalized_action == "no" and not normalized_name
    if not normalized_name and not answer_absent: raise ...
并在 :5649 给 `decision_names = ()` + `decision_scope = "full_answer_absent"`。
生产实证一致(Review 只读):109 次 `action='no'` 的 `normalized_name` 全空、
**109/109 名字表零行**(🔁 反向对照 `action='yes'` 35/35 有行,JOIN 有效)。
⇒ 「零名字」不是没测过的边角,**它是每一次成功的「确认未提到」一直以来的样子**。

同一个谓词长在两处,只有一处拿到了修法 ——
本仓 `feedback_one_predicate_one_place_or_half_goes_unverified`。

🔴 本包**复用既有 fake 夹具**(`tests/test_diagnosis_identity_decision.py` 的
   `FakeDB` / `fake_db` / `_decide`),不另起第二套 —— 同一份夹具抄两处,
   迟早有一处跟不上生产。
"""
from __future__ import annotations

import json

import pytest

from tests.test_diagnosis_identity_decision import (      # noqa: F401  (fake_db 是 fixture)
    BRAND_ID, DIAG_ID, ENGINE, QUESTION, _decide, fake_db,
)


def _cell_of(db):
    """按既有夹具的真实存法取那一格。

    🔴 存的是 `raw_data_json` 字符串(`data.ai_visibility.detail_table[0].results[ENGINE]`),
       不是一个现成的 dict。我第一版直接写 `db.diagnosis[...]["ai_visibility_data"]`
       ⇒ KeyError,7 格全红 —— **这次夹具是大声坏掉的**,不像今早那次回落成一个
       合理的数、伪装成"值不对"。同一族错误,读数天差地别。
    """
    stored = json.loads(db.diagnosis[DIAG_ID]["raw_data_json"])
    return stored, stored["data"]["ai_visibility"]["detail_table"][0]["results"][ENGINE]


def _write_back(db, stored):
    db.diagnosis[DIAG_ID]["raw_data_json"] = json.dumps(stored, ensure_ascii=False)


def _strip_candidates(db):
    """把那一格改成 #727 的真实形态:无候选名、无 matched_text、registry 复核 reason。"""
    stored, cell = _cell_of(db)
    cell["identity_candidates"] = []
    cell["matched_text"] = None
    cell["detection_reason"] = "registry_name_correction_requires_review"
    cell["brand_verdict"] = "UNKNOWN"
    cell["status"] = "success"
    # 🔴 光清 `identity_candidates` / `matched_text` **不够** —— `cell_candidates()`
    #    还会拿 `full_response` 跑一次本地重判、再拿 `mentioned_brands` 算近失候选。
    #    我第一版只清了前两个,夹具里 `full_response` 仍含本品牌名 ⇒ 候选非空 ⇒
    #    **新分支根本没被走到**,而名字表照写、两格当场红。
    #    (这次是**大声**红的:断言直接指着那条 INSERT。同族错误,读数天差地别 ——
    #     今早那次回落成一个合理的数,伪装成"值不对"。)
    #    换成一条**真的没提到本品牌**的回答 —— 这也正是客户会去点「确认未提到」的场景。
    cell["full_response"] = "揭阳市区住宿选择较多,建议按预算和位置筛选。"
    cell["answer_summary"] = cell["full_response"][:150]
    cell["mentioned_brands"] = []
    cell["brand_variants_found"] = []
    _write_back(db, stored)
    return cell


def _name_decision_writes(db):
    return [s for s in db.sql_log
            if s.startswith("insert into public.monitoring_identity_name_decisions")]


def _event_writes(db):
    return [s for s in db.sql_log
            if s.startswith("insert into public.monitoring_identity_decision_events")]


# ── 主判据:#727 那一格现在点得动 ────────────────────────────────────

def test_confirm_no_succeeds_without_any_candidate(fake_db):
    """🔴 无候选名 + 无 matched_text + registry 复核 reason ⇒ 确认未提到**成功落库**。

    这是 #727 的原样形态。改前抛 `请选择候选名称或填写正确名称`。
    """
    _strip_candidates(fake_db)
    result = _decide(fake_db, action="confirm_no", selected_name="",
                     reason="这条回答没提到我")
    cell = result["cell"]
    assert cell["brand_verdict"] == "NO", cell
    assert cell["identity_review_state"] == "rejected", cell
    assert cell["brand_detected"] is False, cell


def test_the_fixture_really_reproduces_the_blocked_shape(fake_db):
    """🔴 夹具自证:那一格**确实**三级回落全空 —— 否则本包主判据是空的。

    (`chosen_display` 空 / `identity_candidates` 空 / `matched_alias` 不存在。)
    本仓今天连续几次栽在「夹具没跑到被测路径上」,这一格先证明缺口真的在。
    """
    from services.brand_identity_resolver import BrandIdentity
    from services.diagnosis_identity_review import cell_candidates

    cell = _strip_candidates(fake_db)
    identity = BrandIdentity(
        brand_id=BRAND_ID,
        canonical_names=("揭阳滨江南路雅栖酒店", "揭阳滨江南路雅栖酒店有限公司"),
        industry="酒店",
    )
    candidates, _snippet = cell_candidates(cell, identity=identity)
    assert candidates == [], (
        "夹具没落在缺口上:`cell_candidates` 仍算出 %r —— 那样新分支根本走不到,"
        "本包主判据会变成空的" % candidates)


# ── 我加的两格 ──────────────────────────────────────────────────────

def test_no_row_is_written_into_the_shared_name_table(fake_db):
    """🔴 零名字时**一行都不许写**进共享别名真相表。

    `monitoring_identity_name_decisions` 是按 `(brand_id, normalized_name)` 的
    **跨 surface** 真相表。写一个空名字进去等于往它里面塞一条无意义记录,
    而它会被别的 surface 读到。生产实证:109 次「确认未提到」**全部零行**。
    """
    _strip_candidates(fake_db)
    _decide(fake_db, action="confirm_no", selected_name="", reason="没提到")
    assert _name_decision_writes(fake_db) == [], _name_decision_writes(fake_db)


def test_matched_text_is_not_overwritten_with_an_empty_string(fake_db):
    """🔴 零名字时 `matched_text` 不许被覆盖成空串。

    #727 落库本来就是 `matched_text: null`。再写一次空值,会把
    **「从未有过名字」**与**「人工确认没有名字」**压成同一个读数 ——
    两种不同原因长成同一个状态,是本仓反复出现的病,事后再也分不开。
    (实读:`cell["matched_text"] = chosen_display` 本就在 `if decision == "positive"`
     里,负向走不到 —— 这一格把「现在安全」钉住,防止有人把它挪出来。)
    """
    _strip_candidates(fake_db)
    result = _decide(fake_db, action="confirm_no", selected_name="", reason="没提到")
    assert result["cell"].get("matched_text") in (None, ""), result["cell"]
    _stored, raw = _cell_of(fake_db)
    assert raw.get("matched_text") is None, (
        "matched_text 被负向决策覆盖了 —— 「从未有过名字」与「确认没有名字」会分不开")


# ── Review 加的一格:已经在跑的那条路不许被改坏 ──────────────────────

def test_the_shape_matches_the_monitoring_surface(fake_db):
    """🔴 诊断面现在产出**与监测面同一种形态**:事件照落、名字表零行。

    🔴 本条原名 `..._existing_shape_of_those_109_successes_is_preserved`,**名字是错的**。
       Review 起初按 `monitoring_identity_decision_events` 的合计数报「这条路一般是通的,
       109 次成功」,后来按 `source_kind` 分层重查,订正为:
           diagnosis 面 `action='no'` —— **0 次**(42 次动作,成功 0);
           monitoring 面 `action='no'` —— 109 次,且 metadata 含
           `full_answer_absent` 的事件 **恰好 109**。
       ⇒ 那 109 次**不在诊断面**,诊断面这条路**历史成功 0 次**
         (本仓 a-route-with-zero-lifetime-successes-is-not-broken-it-never-worked)。
       所以这一格钉的不是「不许改坏已经在跑的那条路」(诊断面没有那条路),
       而是「**诊断面从此与监测面产出同一种形态**」。
       名字说的必须是断言真正检查的事 —— 本仓 test-name-claims-what-the-assertion-never-checks。

    下面那条反臂守另一半(candidates 非空时写名字表的行为不变)。
    """
    _strip_candidates(fake_db)
    _decide(fake_db, action="confirm_no", selected_name="", reason="没提到")
    events = _event_writes(fake_db)
    assert len(events) == 1, events
    assert _name_decision_writes(fake_db) == []


def test_a_cell_with_candidates_behaves_exactly_as_before(fake_db):
    """🔴 反臂:`candidates` 非空时**仍然**写名字表 —— 没把负向那条路整个掏空。

    没有这一条,把 `decision_names` 改成恒空也会让上面每一格绿,
    那会让所有「确认未提到」都不再记录「这个名字不是我」——
    比原来的报错更糟:它**静默**丢掉了客户的纠错。
    """
    stored, cell = _cell_of(fake_db)
    cell["identity_candidates"] = ["雅栖酒店(揭阳万达店)"]
    cell["matched_text"] = "雅栖酒店(揭阳万达店)"
    cell["brand_verdict"] = "UNKNOWN"
    cell["status"] = "success"
    _write_back(fake_db, stored)
    _decide(fake_db, action="confirm_no", selected_name="", reason="不是这家")
    assert _name_decision_writes(fake_db), (
        "有候选名时反而不写名字表了 —— 负向那条路被掏空了")


def test_the_two_paths_are_distinguishable_in_the_audit_trail(fake_db):
    """🔴 两条路在审计里**分得开**:零名字 ⇒ `decision_scope=full_answer_absent`。

    与监测面同名同义。留这个标记正是为了让「从未有过名字」与
    「人工确认没有名字」在库里分得开 —— 事后归因靠它,不靠猜。
    """
    import json

    _strip_candidates(fake_db)
    _decide(fake_db, action="confirm_no", selected_name="", reason="没提到")
    scopes = [
        json.loads(p)["decision_scope"]
        for p in fake_db.event_metadata
    ] if getattr(fake_db, "event_metadata", None) else []
    if not scopes:
        pytest.skip("fake 夹具未捕获 events metadata —— 这一格改由上线后的真实事件核")
    assert scopes[-1] == "full_answer_absent", scopes
