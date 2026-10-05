"""包H · 域层判据(R-4 / Z-3.1 四出口 / Z-3.3 / U-9 / Z-4)。

每一条「必须命中」都配一条「必须不命中」—— 只有正样本的锁与恒绿无法区分。
分母一律从 ``census()`` 现取,不手抄。
"""

from __future__ import annotations

import asyncio
import io
import os
import re

import pytest

from services.defensive_geo import customer_links as cl
from services.defensive_geo import legal_repair as lr
from services.defensive_geo.presentation import priority_availability as pa
from services.defensive_geo.presentation import recommended_facts as rf

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ══════════════════════════════════════════════════════════════════════════
# R-4 · accepted_brand_fact 在 hasConflict=true 时**仍然可用**
# ══════════════════════════════════════════════════════════════════════════
def _manifest(**over):
    entry = {"status": "accepted", "visibility": "public",
             "publicText": "某某科技有限公司", "hasConflict": False}
    entry.update(over)
    return {"legal_name": entry}


def test_accepted_fact_with_conflict_is_still_usable():
    """R-4 的核心。冲突**不删事实**,只随行提示。"""
    fact = rf.resolve_accepted_brand_fact(
        audience="customer", profile_revision_id="rev-7",
        fact_key="legal_name", manifest=_manifest(hasConflict=True),
    )
    assert fact.sanitized_text == "某某科技有限公司"
    assert fact.has_conflict is True
    assert fact.conflict_notice, "冲突信息必须随行下发,不能只有一个布尔"
    assert "核对" in fact.conflict_notice


def test_no_conflict_means_no_notice():
    """必须不命中的那一半:没冲突时不许挂一句莫名其妙的提示。"""
    fact = rf.resolve_accepted_brand_fact(
        audience="customer", profile_revision_id="rev-7",
        fact_key="legal_name", manifest=_manifest(hasConflict=False),
    )
    assert fact.has_conflict is False
    assert fact.conflict_notice is None


@pytest.mark.parametrize("over", [
    {"visibility": "private"},
    {"visibility": "unknown"},
    {"status": "pending"},
    {"publicText": ""},
])
def test_rejection_surface_is_exactly_three_classes(over):
    """拒绝面:私密/未知可见性、非客户确认、无可公开文本。"""
    with pytest.raises(rf.FactRejected):
        rf.resolve_accepted_brand_fact(
            audience="customer", profile_revision_id="rev-7",
            fact_key="legal_name", manifest=_manifest(**over),
        )


def test_on_the_fly_fact_is_rejected():
    """不在冻结 manifest 里 = 现场生成 = 拒。"""
    with pytest.raises(rf.FactRejected):
        rf.resolve_accepted_brand_fact(
            audience="customer", profile_revision_id="rev-7",
            fact_key="official_website", manifest=_manifest(),
        )


def test_corroborated_claim_requires_sources():
    claims = {"c1": {"publicText": "获得 X 认证", "publicSourceRefs": []}}
    with pytest.raises(rf.FactRejected):
        rf.resolve_corroborated_public_claim(
            audience="customer", profile_revision_id="rev-7",
            fact_key="public_credential", claim_ref="c1", snapshot_claims=claims,
        )


def test_corroborated_claim_text_comes_from_claim_not_caller():
    """``sanitizedText`` 只能来自 claim —— 调用方传不进文本。

    能传文本的话「与签发的公开 fact 文本相等」就是自证式空话。
    """
    claims = {"c1": {"publicText": "获得 X 认证", "publicSourceRefs": ["s1", "s2"]}}
    fact = rf.resolve_corroborated_public_claim(
        audience="customer", profile_revision_id="rev-7",
        fact_key="public_credential", claim_ref="c1", snapshot_claims=claims,
    )
    assert fact.sanitized_text == "获得 X 认证"
    assert fact.public_source_refs == ("s1", "s2")
    # 签名里根本没有 text 参数
    import inspect
    assert "text" not in inspect.signature(rf.resolve_corroborated_public_claim).parameters


def test_fact_ref_is_audience_bound():
    a = rf.public_fact_ref(audience="customer", profile_revision_id="rev-7", fact_key="legal_name")
    b = rf.public_fact_ref(audience="pdf_customer", profile_revision_id="rev-7", fact_key="legal_name")
    assert a != b, "ref 不带受众 ⇒ 跨受众复用这条判据无处可打"
    assert rf.parse_fact_ref(a)[0] == "customer"


def test_conflict_is_not_in_the_rejection_surface_census():
    assert rf.census()["conflictIsRejectable"] is False


# ══════════════════════════════════════════════════════════════════════════
# §15.6 · PriorityActionFactBasis 按 intent 判别
# ══════════════════════════════════════════════════════════════════════════
def _one_fact():
    return rf.resolve_accepted_brand_fact(
        audience="customer", profile_revision_id="rev-7",
        fact_key="legal_name", manifest=_manifest(),
    )


def test_fact_basis_shapes_per_intent():
    rf.assert_fact_basis(action_intent="content_or_publication_repair",
                         recommended_facts=[_one_fact()], requested_fact_keys=[])
    rf.assert_fact_basis(action_intent="fact_collection",
                         recommended_facts=[], requested_fact_keys=["legal_name"])
    rf.assert_fact_basis(action_intent="identity_calibration",
                         recommended_facts=[], requested_fact_keys=["brand_alias"])
    rf.assert_fact_basis(action_intent="comparable_retest",
                         recommended_facts=[], requested_fact_keys=[])


@pytest.mark.parametrize("intent,facts,keys", [
    ("content_or_publication_repair", [], []),                 # 修复却没有依据
    ("content_or_publication_repair", "one", ["legal_name"]),   # 修复还塞了待补字段
    ("fact_collection", "one", ["legal_name"]),                 # 采集伪装已有事实
    ("fact_collection", [], []),                                # 采集没说要补什么
    ("fact_collection", [], ["not_in_allowlist"]),              # key 越界
    ("comparable_retest", [], ["legal_name"]),                  # retest 塞 requestedFact
])
def test_fact_basis_mismatch_is_rejected(intent, facts, keys):
    real = [_one_fact()] if facts == "one" else facts
    with pytest.raises(rf.FactBasisInvalid):
        rf.assert_fact_basis(action_intent=intent, recommended_facts=real,
                             requested_fact_keys=keys)


# ══════════════════════════════════════════════════════════════════════════
# Z-3.1 · 四出口矩阵
# ══════════════════════════════════════════════════════════════════════════
def test_fact_collection_never_business_available_across_full_matrix():
    """分母 = 笛卡尔积全跑,不抽样。"""
    seen = 0
    for scope in pa.ALL_SCOPES:
        for q in (True, False):
            for r in (True, False):
                for a in (True, False):
                    v = pa.resolve(
                        action_intent="fact_collection",
                        basis=pa.AvailabilityBasis(scope=scope, signed_quote_route=q,
                                                   executable_route=r, ai_autofill_route=a),
                    )
                    seen += 1
                    assert v.availability != "business_available", (scope, q, r, a)
                    pa.assert_fact_collection_never_business_available(v)
    assert seen == len(pa.ALL_SCOPES) * 8, f"分母只跑了 {seen} 格"


def test_ai_autofill_exit_is_reachable_and_is_the_new_one():
    v = pa.resolve(
        action_intent="fact_collection",
        basis=pa.AvailabilityBasis(scope="included", signed_quote_route=False,
                                   executable_route=True, ai_autofill_route=True),
    )
    assert v.availability == "ai_autofill_available"
    assert v.manual_reason_code is None
    assert "AI" in v.user_label


def test_unknown_scope_beats_ai_route():
    """商业归属没弄清就先扣钱 = 把安全解释态换成"先收钱再说"。"""
    for scope in ("unknown", "conflict"):
        v = pa.resolve(
            action_intent="fact_collection",
            basis=pa.AvailabilityBasis(scope=scope, signed_quote_route=True,
                                       executable_route=True, ai_autofill_route=True),
        )
        assert v.availability == "manual_route"
        assert v.manual_reason_code == "scope_confirmation_required"
        assert v.handoff_route_key == "confirm_commercial_scope"


def test_manual_triples_are_exact():
    """三条 manual 三元组 basis→reason→handoff 一个都不许换。"""
    expected = {
        "included_route_unavailable": ("included_manual_fulfillment", "manual_fulfillment"),
        "out_of_scope_quote_unavailable": ("manual_quote_required", "manual_quote"),
        "commercial_basis_unknown": ("scope_confirmation_required", "confirm_commercial_scope"),
        "commercial_basis_conflict": ("scope_confirmation_required", "confirm_commercial_scope"),
    }
    assert dict(pa.MANUAL_TRIPLES) == expected


def test_other_intents_can_still_be_business_available():
    """必须不命中的那一半:Z-3.1 只动 fact_collection,没把别的 intent 一起改了。"""
    v = pa.resolve(
        action_intent="content_or_publication_repair",
        basis=pa.AvailabilityBasis(scope="included", signed_quote_route=False,
                                   executable_route=True, ai_autofill_route=False),
    )
    assert v.availability == "business_available"


def test_availability_labels_are_human_and_complete():
    from services.defensive_geo.presentation import copy_registry as pc

    for key in pa.ALL_AVAILABILITIES:
        label = pc.translate("availability", key)
        assert label and not pc.looks_like_internal_enum(label)


def test_requested_fact_keys_all_translated():
    """八个 key 全部命中内部枚举形态 ⇒ 没有译文就是裸串上屏。"""
    from services.defensive_geo.presentation import copy_registry as pc

    for key in rf.REQUESTED_FACT_KEYS:
        assert pc.looks_like_internal_enum(key), f"{key} 不像内部枚举 —— 这条判据的前提不成立"
        label = pc.translate("requested_fact", key)
        assert label and not pc.looks_like_internal_enum(label)


# ══════════════════════════════════════════════════════════════════════════
# U-9 · 四类链接
# ══════════════════════════════════════════════════════════════════════════
def test_link_kinds_and_prefixes():
    c = cl.census()
    assert c["kinds"] == ["diagnosis_report", "quote_proposal",
                          "monitoring_snapshot", "customer_portal"]
    for kind, prefix in c["prefixes"].items():
        assert prefix.startswith("【") and prefix.endswith("】"), (kind, prefix)
    assert c["prefixes"]["quote_proposal"] == "【报价单·请确认】"


def test_reissuable_kinds_all_have_a_real_path_and_the_rest_admit_they_dont():
    """**如实**:声称能重签的,必须真的有一条现役原语;没有的不许声称。

    🔴 [工单 V3-A · Codex 三审 P1-8 · 2026-08-28] 本条原来写死
    ``reissuableKinds == ["customer_portal"]``。那个写法把「现状」当成了「不变量」——
    而 Codex 打穿的恰恰是现状与文案不一致:过期文案逐字写着「点一下就能重新签发」,
    ``quote_proposal`` 却不在集合里、UI 因此不显示按钮。
    现在 ``quote_proposal`` 接了现役 ``extend_session``,所以它**该**在里面。

    把断言从「集合等于这个字面量」改成**不变量**:
      · 集合里的每一类,``reissue_supported`` 必须为真(自洽);
      · 集合外的每一类必须为假;
      · 且集合里的每一类都要在 ``reissue_link`` 里有**自己的**一段实现 ——
        没有实现却在集合里,就又变成"看着能点、点了什么也没发生"。
    这样下次谁再接一类进来,这条判据不必跟着改数;而谁把一类塞进集合却不实现,
    当场红。
    """
    import inspect

    kinds = set(cl.census()["kinds"])
    reissuable = set(cl.census()["reissuableKinds"])
    assert reissuable <= kinds, f"可重签集合里有不存在的类别:{reissuable - kinds}"
    assert reissuable, "一类都不能重签的话,过期文案里那句「点一下就能重新签发」是假的"
    for kind in kinds:
        assert cl.reissue_supported(kind) is (kind in reissuable), kind

    src = inspect.getsource(cl.reissue_link)
    for kind in reissuable:
        assert f'"{kind}"' in src, (
            f"{kind} 在可重签集合里,``reissue_link`` 里却找不到它的实现分支 —— "
            "那就是「看着能点、点了什么也没发生」")


def test_reissue_returns_none_for_unsupported_kind():
    # [工单 V3-A · 2026-08-28] ``expected_object_ref`` 现在是必传参数
    #  (Codex 三审 P1-8:POST 必须带回 GET 下发的那个对象)。
    #  这一条验的是"不可重签的类别如实返 None",与对象绑定无关,传什么都不影响结论。
    assert cl.reissue_link(kind="diagnosis_report", brand_id=1, brand_name="X",
                           expected_object_ref={"kind": "diagnosis_record", "id": 1}) is None


def test_link_entry_rejects_unknown_status():
    with pytest.raises(ValueError):
        cl._entry(kind="diagnosis_report", url=None, status="whatever", brand_name="X")


# ══════════════════════════════════════════════════════════════════════════
# Z-3.3 · 广告法一键修复
# ══════════════════════════════════════════════════════════════════════════
VIOLATING = "我们是全国最好的机构，效果第一。"


def _banned_line(prompt: str) -> str:
    """prompt 里「必须换掉的表述:」那一行。判据只打这一行,不打整段 ——
    整段末尾带着原句,任何"原句的切片"都会是它的子串,那条断言就没有区分力。"""
    for line in prompt.splitlines():
        if line.startswith("必须换掉的表述:"):
            return line.split(":", 1)[1]
    raise AssertionError("prompt 里没有「必须换掉的表述」这一行 —— 判据锚点失效")


def test_prompt_names_the_hit_terms():
    """「按 rule_id」的可验证形态:**命中词本身**逐个进 prompt。

    🔴 这条判据一开始写成「hits 里每一项都在 prompt 里」,而 prompt 末尾就带着
       原句 —— 于是把 `h.term` 换成 `h.excerpt`(整句上下文)照样全绿
       (撕锁 MUT-B4 当场存活)。真正要打的是:那一行点名的是**短语**,
       不是整句。所以下面既断言短语在、也断言整句的前缀**不**在。
    """
    prompt = lr.candidate_prompt(rule_id="defgeo.publish.legal_catalog", passage=VIOLATING)
    banned = _banned_line(prompt)
    assert "最好" in banned and "第一" in banned, banned
    assert "我们是全国" not in banned, (
        f"点名的是整句上下文而不是命中词:{banned!r}"
    )
    assert all(len(t) <= 8 for t in banned.split("、") if t), banned
    assert "defgeo.publish.legal_catalog" in prompt


def test_prompt_refuses_when_nothing_hit():
    """必须不命中的那一半:没命中却来修 ⇒ 拒,不硬编一句改写。"""
    with pytest.raises(lr.LegalRepairUnavailable):
        lr.candidate_prompt(rule_id="r", passage="我们提供教育培训服务。")


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_candidates_that_still_violate_are_dropped():
    """改完还犯规 ⇒ 丢。她确认了也会在外调那一刻被同一道门拦下。"""
    async def fake_llm(_prompt: str) -> str:
        return "我们是全国最好的机构。\n我们在多个地区提供服务。\n效果第一。"

    out = _run(lr.build_candidate_payload(
        rule_id="r", passage_excerpt=VIOLATING,
        prompt="x", llm_fn=fake_llm,
    ))
    texts = [c["text"] for c in out]
    assert texts == ["我们在多个地区提供服务。"], texts


def test_all_candidates_violating_raises_not_empty_list():
    async def fake_llm(_prompt: str) -> str:
        return "全国最好。\n效果第一。"

    with pytest.raises(lr.LegalRepairUnavailable):
        _run(lr.build_candidate_payload(
            rule_id="r", passage_excerpt=VIOLATING, prompt="x", llm_fn=fake_llm))


def test_legal_repair_owns_no_term_list():
    """本模块零词表:禁区只来自 Owner 签发包。"""
    assert lr.census()["ownsTermList"] is False
    src = io.open(os.path.join(REPO, "services", "defensive_geo", "legal_repair.py"),
                  encoding="utf-8").read()
    # 结构锚:不许出现自带的词表/正则
    assert "re.compile" not in src
    assert not re.search(r"^\s*_TERMS\s*=", src, re.M)


# ══════════════════════════════════════════════════════════════════════════
# Z-4 · 通知受众边界(前端谓词的后端对账)
# ══════════════════════════════════════════════════════════════════════════
def test_same_tier_replacement_notice_is_provider_only():
    """Z-4 逐字:同角色同预算的自动替换**只进服务商工作台操作日志**。

    判据打在前端那个谓词的源码上 —— 它是唯一决定"这条给不给客户看"的地方。
    """
    src = io.open(os.path.join(
        REPO, "frontend", "src", "pages", "DefensivePublish", "deliveryTodo.ts"),
        encoding="utf-8").read()
    block = src.split("CUSTOMER_VISIBLE_NOTICE_KINDS")[1].split("]")[0]
    assert "media_replaced_same_tier" not in block, (
        "同角色同预算替换出现在客户可见集合里 —— 违反 Z-4"
    )
    assert "media_downgraded_needs_reconfirm" in block, (
        "真降档动了交付承诺,必须回客户重签 ⇒ 客户必须看得到"
    )
    assert "isProviderLogNotice" in src


# ══════════════════════════════════════════════════════════════════════════
# 端点 census:三条链的路由确实注册了
# ══════════════════════════════════════════════════════════════════════════
def test_assist_routes_census():
    from api.defensive_geo_assist_api import census

    c = census()
    assert c["routes"] == [
        "GET /api/defensive-geo/customer-links",
        "POST /api/defensive-geo/customer-links/reissue",
        "POST /api/defensive-geo/fact-collection/ai-autofill",
        # 🔴 [工单C · C-5 2026-08-25] Z-3.3 的**后半句**落地新增的端点。
        #    在这之前「用这一句」只是 `navigate('/writing?...&repaired=<句子>')`,
        #    而那个查询参数**全仓零消费者** —— 点完什么都没发生。
        #    本条 census 正确地抓到了这次新增:路由面是**闭集**,
        #    多一条少一条都要有人复核过(这正是它存在的理由,不是它挡路)。
        "POST /api/defensive-geo/legal-repair/apply",
        "POST /api/defensive-geo/legal-repair/candidates",
    ]
    # AI 覆盖不到的两项必须如实记着,不许被悄悄并进"能补"
    assert c["aiUncoveredFactKeys"] == ["public_credential", "public_contact_channel"]


def test_assist_router_is_registered_in_server():
    """接线锁:只在 api/ 里定义 router 而没 include_router = 死路由。"""
    src = io.open(os.path.join(REPO, "server.py"), encoding="utf-8").read()
    assert "from api.defensive_geo_assist_api import router as defensive_geo_assist_router" in src
    assert "app.include_router(defensive_geo_assist_router)" in src


def test_assist_api_contains_no_price_number():
    """Z-3.1「扣费走现役 autofill_brand 价目,禁自定价」的结构锚。"""
    src = io.open(os.path.join(REPO, "api", "defensive_geo_assist_api.py"),
                  encoding="utf-8").read()
    assert '_bill_ctx(request, "autofill_brand")' in src
    # 不许出现 deduct/freeze 之类的自建资金动作
    for forbidden in ("deduct_points", "freeze_points", "check_and_lock_budget"):
        assert forbidden not in src, f"自建资金动作 {forbidden} —— 资金腿必须复用现役"


# ══════════════════════════════════════════════════════════════════════════
# [工单 V4-C · C-2 · Codex fix-of-fix P2-2] 显式 scoped-out —— **可执行**的
# ══════════════════════════════════════════════════════════════════════════
# 上一轮我把「监测快照那一类不该对客户承诺」写成了交付文里的一句话,
# Codex 指出:那道"对外可见性门"**只存在于文字**。这里把它变成判据。
def test_scoped_out_kinds_never_reach_the_panel(monkeypatch):
    """规格声明了、但没交付的类,**不出现在面板上** —— 不是 not_ready 占位。

    🔴 占位 + 「做完会自动出现」的组合是在替一条不存在的能力打包票:
       监测快照既没有客户侧 token 路由,``defgeo_report_snapshots`` 也零生产
       写入方 —— "前一步"根本没有人在做,它永远不会自己亮。
    """
    import services.defensive_geo.customer_links as mod

    assert mod.SCOPED_OUT_KINDS, "scoped-out 集合空了 —— 这条判据会恒真"
    assert mod.SCOPED_OUT_KINDS <= set(mod.LINK_KINDS), (
        "scoped-out 了一个根本不在规格里的类 —— 那不是 scoped-out,是笔误")

    import ast
    import inspect

    src = inspect.getsource(mod.build_panel)
    # build_panel 是模块级函数,getsource 从第 0 列开始 —— 直接 parse 即可
    tree = ast.parse(src)
    emitted = {
        kw.value.value
        for call in ast.walk(tree) if isinstance(call, ast.Call)
        and getattr(call.func, "id", "") == "_entry"
        for kw in call.keywords
        if kw.arg == "kind" and isinstance(kw.value, ast.Constant)}
    assert emitted, "没扫到任何 _entry(kind=...) —— 探针失效"
    leaked = emitted & mod.SCOPED_OUT_KINDS
    assert not leaked, f"scoped-out 的类仍然在面板里被投影:{sorted(leaked)}"
    # 🔴 再钉一道**运行期**兜底:哪怕将来有人手滑加回一个 _entry,
    #    返回值那一步也必须把 scoped-out 的滤掉。只锁源码形状挡不住这一手。
    assert "SCOPED_OUT_KINDS" in src.split("return")[-1], (
        "build_panel 的返回值没有过 scoped-out 过滤 —— 只剩源码形状这一道锁")


def test_scoped_out_is_machine_readable_from_the_census():
    """这件事必须**可机读**,不是靠读注释才知道。"""
    import services.defensive_geo.customer_links as mod

    c = mod.census()
    assert c["scopedOutKinds"] == sorted(mod.SCOPED_OUT_KINDS)
    # 声明保留:规格里那四类一个都没被删掉(删掉就抹去了"规格要求过"这个事实)
    assert set(c["scopedOutKinds"]) <= set(c["kinds"])


def test_the_not_ready_copy_promises_nothing_about_automatic_appearance():
    """文案不许承诺"会自动出现"。

    🔴 承诺的前提是"有人在做前一步、做完系统会自己接上"——这一点我们保证不了。
       判据打的是**承诺语义**的几种写法,不是某一句原文;并且自证这些模式
       确实抓得住一句真的承诺句,否则它会以"全绿"的样子存在。
    """
    from services.defensive_geo.copy_registry import user_label

    text = user_label("reason", "customer_link_not_ready")
    promises = ("自动出现", "会自动", "自动就会", "稍后会出现", "做完会出现")
    hit = [p for p in promises if p in text]
    assert not hit, f"文案仍在承诺自动出现({hit}):{text}"
    assert text.strip(), "文案空了 —— 那是另一种坏"
    # 反向自证:同一组模式必须抓得住一句真的承诺句
    assert any(p in "这一类链接要等前一步做完才会有;做完会自动出现在这里。"
               for p in promises), "承诺模式集失效 —— 它抓不住已知的承诺句"
