"""WP8 判据 —— §9.6 冻结理由 / POR-13 回执 / 两条 Owner 铁律。

🔴 本文件**不**重新实现五阶段,也不判"五阶段还在不在" ——
   那是 39/40 班的判据的作用域。这里只判**窗D 新加的那一层**。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from services.defensive_geo.xiaobang import frozen_reasons as FR
from services.defensive_geo.xiaobang import kb_entries as KB
from services.defensive_geo.xiaobang import receipt_guard as RG
from services.xiaobang_command_contract import (
    COMPUTE_ONLY_MIN_BIND_FIELDS,
    EXTERNAL_BIND_FIELDS,
)

ROOT = Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════════════
# §9.6 冻结公开理由
# ══════════════════════════════════════════════════════════════════════

class TestFrozenReasons:
    @pytest.mark.parametrize("bad", [
        "这次大约消耗 300 算力", "首发 ¥1200 起", "预计提升 40%",
        "折合 500 元左右",
    ])
    def test_business_numbers_are_refused(self, bad):
        """§9.6「不现场生成……商业数字」。

        真实后果:模型在聊天里顺口给个数,客户会当成我们的报价。
        拆红:把 ``_MONEY_PATTERNS`` 清空 —— 本组 param 立刻全红。
        """
        with pytest.raises(FR.FrozenReasonError, match="商业数字|绝对化"):
            FR.assert_no_business_numbers(bad)

    def test_plain_reason_passes(self):
        """成对正样本:人话理由必须放行(证明上面那组不是恒抛)。"""
        FR.assert_no_business_numbers(
            "这家媒体的读者和你的客户重合度高,适合先发这一篇。")

    @pytest.mark.parametrize("promise", [
        "保证上榜", "必然推荐", "永久收录", "一定能上榜",
    ])
    def test_absolute_promises_are_refused(self, promise):
        """§1.3:不承诺固定排名、固定推荐率、永久引用。

        🔴 正样本必须**点名自己那条规则**:断言的是「绝对化」这句错误,
           不是"有没有报错"。第一版把 ``100%推荐`` 放在这一组,结果它先被
           **百分比**规则命中 —— 于是把绝对化规则整条删掉,这组判据照样绿。
           本仓记过:多规则扫描器的正样本只断言"有命中" = 被别的规则顺手判红。
           所以这一组刻意**不含数字**,只有绝对化规则能命中它。
        """
        with pytest.raises(FR.FrozenReasonError, match="绝对化"):
            FR.assert_no_business_numbers(f"我们{promise}")

    def test_percentage_promise_is_caught_by_the_number_rule(self):
        """``100%推荐`` 同时踩两条规则 —— 这里明确它由**百分比**规则拦下。

        写清楚归谁管,下次有人调其中一条规则时才知道该看哪一组判据。
        """
        with pytest.raises(FR.FrozenReasonError, match="商业数字"):
            FR.assert_no_business_numbers("我们100%推荐")

    def test_paid_next_action_cannot_ride_on_a_reason(self):
        """§9.6 末两条:付费动作必须走五阶段,不能从"理由"这一侧长出扣费入口。"""
        with pytest.raises(FR.FrozenReasonError, match="只读动作"):
            FR.assert_frozen_shape({
                "subject_kind": "report_snapshot", "reason_ref": "r1",
                "public_text": "建议先发这一篇", "next_action_kind": "publish_now",
            })

    def test_read_only_next_action_passes(self):
        FR.assert_frozen_shape({
            "subject_kind": "report_snapshot", "reason_ref": "r1",
            "public_text": "建议先看这份报告", "next_action_kind": "view_report",
        })

    @pytest.mark.parametrize("action", FR.READ_ONLY_NEXT_ACTIONS)
    def test_every_declared_read_only_action_is_accepted(self, action):
        """分母 = ``READ_ONLY_NEXT_ACTIONS`` 机械遍历,不手抄。"""
        FR.assert_frozen_shape({
            "subject_kind": "report_snapshot", "reason_ref": "r1",
            "public_text": "看看这个", "next_action_kind": action,
        })

    def test_rewriting_a_frozen_reason_is_still_generating(self):
        """§9.6「**不现场生成**」—— 改写也是生成。

        拆红:把 ``assert_not_generated_here`` 改成子串包含判断
        (``any(c in t for t in frozen)``)—— 本判据立刻红。
        """
        frozen = [{"public_text": "这家媒体的读者与你的客户重合度高。"}]
        FR.assert_not_generated_here("这家媒体的读者与你的客户重合度高。", frozen)
        with pytest.raises(FR.FrozenReasonError, match="现场生成"):
            FR.assert_not_generated_here(
                "这家媒体读者跟你客户挺重合的,挺合适。", frozen)


# ══════════════════════════════════════════════════════════════════════
# POR-13
# ══════════════════════════════════════════════════════════════════════

class TestReceiptGuard:
    @pytest.mark.parametrize("side_effect", ["external", "compute_only"])
    def test_por13_dimensions_are_all_bound(self, side_effect):
        """POR-13 七维对账。分母 = 现役 bind field 常量,**不手抄**。

        拆红:把 ``EXTERNAL_BIND_FIELDS`` 里的 ``object_manifest_hash`` 删掉
        —— 本判据立刻红,并指出是 object 维没人绑。
        """
        RG.assert_bind_fields_cover_por13(side_effect)

    def test_bind_field_map_points_at_real_contract_fields(self):
        """映射表里的每个字段必须**真的存在**于现役合同常量里。

        这条拦的是"映射表写了个不存在的字段名,于是对账恒绿"。
        """
        available = set(EXTERNAL_BIND_FIELDS) | set(COMPUTE_ONLY_MIN_BIND_FIELDS)
        for dim, field in RG.POR13_DIMENSION_TO_BIND_FIELD.items():
            assert field in available, f"{dim} 指向了不存在的绑定字段 {field}"

    def test_command_dimension_is_honestly_declared_as_not_a_bind_field(self):
        """诚实性判据:第七维 command **不是**靠 bind field 绑的。

        把它硬塞进映射表(比如指向 ``assignment_authority_version``)
        会得到一条恒绿对账 —— 那个字段绑的是"谁有权指派",不是"哪条命令"。
        """
        assert "command" not in RG.POR13_DIMENSION_TO_BIND_FIELD
        assert RG.POR13_COMMAND_DIMENSION_BOUND_BY == "unique(intent_id, intent_revision)"

    @pytest.mark.parametrize("kind", RG.NON_AUTHORIZATIONS)
    def test_chat_affirmation_is_never_an_authorization(self, kind):
        """POR-13 末句。分母机械遍历。"""
        with pytest.raises(RG.ReceiptGuardError, match="不是有效授权"):
            RG.assert_not_a_chat_affirmation(kind)

    def test_a_real_receipt_kind_is_accepted(self):
        """成对正样本:证明上面那组不是恒抛。"""
        RG.assert_not_a_chat_affirmation("confirmation_receipt")

    def test_receipt_bound_to_another_intent_is_refused(self):
        with pytest.raises(RG.ReceiptGuardError, match="另一个操作"):
            RG.assert_receipt_usable(
                {"intent_id": "i2", "intent_revision": 1, "consumed_at": None},
                intent_id="i1", intent_revision=1)

    def test_receipt_bound_to_a_previous_revision_is_refused(self):
        with pytest.raises(RG.ReceiptGuardError, match="上一版"):
            RG.assert_receipt_usable(
                {"intent_id": "i1", "intent_revision": 1, "consumed_at": None},
                intent_id="i1", intent_revision=2)

    def test_double_click_replays_instead_of_demanding_reconfirmation(self):
        """与现役 ``_assert_confirmation_present`` **同口径**的第二种"可用"。

        少了这一支,用户双击的第二发会拿到 CONFIRMATION_REQUIRED,
        而正确答案是回放同一结果。
        """
        row = {"intent_id": "i1", "intent_revision": 1, "consumed_at": "t",
               "consumed_by_execution_request_id": "e1"}
        RG.assert_receipt_usable(row, intent_id="i1", intent_revision=1,
                                 execution_request_id="e1")
        with pytest.raises(RG.ReceiptGuardError, match="已经用过"):
            RG.assert_receipt_usable(row, intent_id="i1", intent_revision=1,
                                     execution_request_id="e2")

    def test_bound_columns_are_documented_as_not_load_bearing(self):
        """把「``bound_*`` 三列没人读」钉成一条会红的判据。

        今天的真相就是 0。哪天有人开始读它们,这条会红并提醒他
        同时重锚依赖 ``drift_reason`` 的结论。
        """
        RG.assert_bound_columns_are_not_load_bearing(
            {"bound_payload_hash": 0, "bound_object_manifest_hash": 0,
             "bound_compute_quote_hash": 0})
        with pytest.raises(RG.ReceiptGuardError, match="运行时读取方"):
            RG.assert_bound_columns_are_not_load_bearing(
                {"bound_payload_hash": 1})


# ══════════════════════════════════════════════════════════════════════
# 两条 Owner 铁律 —— KB 侧此前**零判据**
# ══════════════════════════════════════════════════════════════════════

class TestIronRules:
    def test_both_iron_rules_are_registered_with_an_enforcer(self):
        """铁律必须指名**谁在执行** —— 只写一句话的铁律不会拦住任何人。"""
        ids = {r["rule_id"] for r in KB.IRON_RULES}
        assert ids == {"ORG-SEAT-NO-SOCIAL", "XIAOBANG-GEO-ONLY"}
        for rule in KB.IRON_RULES:
            assert rule["enforced_by"], f"{rule['rule_id']} 没有执行方"

    @pytest.mark.parametrize("entry", [
        KB.KbEntry("social_studio_help", "社媒工作台", "/social", "social.publish", ""),
        KB.KbEntry("persona_setup", "人设", "/s/persona", "persona.edit", ""),
        KB.KbEntry("myip_intro", "MyIP", "/s", "ip.view", ""),
    ])
    def test_social_entries_are_refused_structurally(self, entry):
        """XIAOBANG-GEO-ONLY:按**结构**判(路由/capability/词段),不按措辞。"""
        with pytest.raises(KB.KbEntryError, match="社媒域"):
            KB.assert_entry_is_geo_only(entry)

    @pytest.mark.parametrize("entry", [
        KB.KbEntry("defensive_geo", "先守住品牌", "/diagnosis/new",
                   "diagnosis.run", ""),
        KB.KbEntry("personal_settings", "个人设置", "/settings", "account.edit", ""),
    ])
    def test_geo_and_lookalike_entries_pass(self, entry):
        """成对正样本 + **误伤自证**。

        ``personal_settings`` 含 ``persona`` 子串 —— 第一版社媒锁就栽在这里
        (``token in identifier``)。它必须**放行**,否则锁拦的是无辜的人。
        """
        KB.assert_entry_is_geo_only(entry)

    def test_new_kb_page_exists_and_is_under_an_indexed_glob(self):
        """本仓记过的假绿形态:页写了、锁扫了、线上答不出来 ——
        因为那个文件根本不进索引。"""
        for page in KB.NEW_KB_PAGES:
            assert (ROOT / page).exists(), f"{page} 不存在"
        KB.assert_pages_are_indexed(
            KB.NEW_KB_PAGES, ["knowledge/system_kb/pages/*.md"])

    def test_page_outside_the_index_is_caught(self):
        with pytest.raises(KB.KbEntryError, match="不在任何索引 glob"):
            KB.assert_pages_are_indexed(
                ["docs/somewhere/else.md"], ["knowledge/system_kb/pages/*.md"])

    def test_new_kb_page_passes_the_live_terminology_gate(self):
        """新增 KB 文本受现役运行时术语门约束(§0.5.1 第 6 条)。"""
        from services.kb_terminology_gate import all_violations, kb_write_violations

        for page in KB.NEW_KB_PAGES:
            text = io.open(ROOT / page, encoding="utf-8").read()
            assert not all_violations(text), f"{page} 术语违规"
            assert not kb_write_violations(text), f"{page} 写入门违规"

    def test_new_kb_page_has_zero_social_content(self):
        """XIAOBANG-GEO-ONLY 在 KB 侧的正样本:本包新增页零社媒字样。"""
        for page in KB.NEW_KB_PAGES:
            text = io.open(ROOT / page, encoding="utf-8").read()
            KB.assert_no_unregistered_social_mention(page, text)

    def test_unregistered_social_mention_is_caught(self):
        """措辞层兜底闸的活性自证 —— 没有它,上一条"全绿"可能只是探测器瞎了。"""
        with pytest.raises(KB.KbEntryError, match="未登记"):
            KB.assert_no_unregistered_social_mention(
                "knowledge/system_kb/pages/whatever.md",
                "小榜也能帮你写社媒脚本")

    def test_registered_incidental_mention_is_allowed_with_a_reason(self):
        """如实描述别处 UI 的偶发字样 —— 登记在案的放行,不是漏网。

        🔴 为什么不是"把那句真话删掉":价目页上确实有「社媒IP」这个 Tab。
           删掉它,KB 与真实界面对不上,用户照 KB 找 Tab 会找不到 ——
           那是把真话删成假话,且不降低任何风险(开发原则第 1/3 条)。
        """
        registered = {m["path"] for m in KB.INCIDENTAL_SOCIAL_MENTIONS}
        assert "knowledge/system_kb/pages/feature-pricing.md" in registered
        for m in KB.INCIDENTAL_SOCIAL_MENTIONS:
            assert len(m["why_allowed"]) > 30, f"{m['path']} 的放行理由太薄"
            # 那句真话必须**还在**文件里 —— 登记了却被人顺手删掉才是真问题
            text = io.open(ROOT / m["path"], encoding="utf-8").read()
            assert "社媒" in text, (
                f"{m['path']} 登记了偶发社媒字样,但文件里已经没有了 —— "
                "要么是被删了(真话变假话),要么是登记过期了")
