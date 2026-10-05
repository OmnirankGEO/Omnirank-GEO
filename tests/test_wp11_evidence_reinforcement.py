"""WP11 · 写作证据增援化 判别测试（D11 · SSOT v2.1 §11）。

生产客诉：新文章"核验"字样篇均 10+、BF-编号泄入正文、全篇零图。
D11 裁决：证据体系 = **增援客户**，不是审查客户。

覆盖五条 + 一条不可越的底线：
  ① 正文零内部记号        TestBodyHasNoInternalMarkers / TestSanitizer
  ② 公开信源 = 可引用证据  TestPublicSourcesAreCitable
  ③ 搜索方向 = 佐证        TestSearchDirectionIsCorroboration
  ④ 图片保底              TestImageFallback
  ⑤ 引用必须真实存在(H0)  TestCitationTruthfulnessH0
"""
import re

import pytest

from writing.body_internal_marker_sanitizer import (
    sanitize_article_body,
    verify_citations_against_manifest,
)

BODY_WITH_MARKERS = """# 深圳装修公司怎么选

据《中国建筑装饰行业发展报告》显示，行业集中度持续提升 [EV-3]。
清华大学的一项研究显示，工期延误主要来自设计变更（EV-7）。

某公司成立于 2015 年 [BF-12]，主营全屋定制。该数据待核验。

## 核验清单
- 需逐家核验营业执照
- 需交叉验证工商信息

正常段落必须保留。

## 证据状态
| 事实 | 证据状态 |
|---|---|
| 成立时间 | 待核验 |

结尾段也要保留。
"""

INTERNAL_MARKER_RE = re.compile(r"(?:EV|BF)[-‑—]\d+", re.I)
REVIEW_WORDS = ("待核验", "需逐家核验", "需交叉验证", "核验清单", "证据状态", "暂无资料")


# ===========================================================================
# ① 正文零内部记号
# ===========================================================================
class TestSanitizer:
    def test_strips_all_evidence_ids(self):
        out, _ = sanitize_article_body(BODY_WITH_MARKERS)
        assert INTERNAL_MARKER_RE.search(out) is None, f"正文残留内部编号: {out}"

    def test_strips_all_review_language(self):
        out, _ = sanitize_article_body(BODY_WITH_MARKERS)
        for word in REVIEW_WORDS:
            assert word not in out, f"正文残留审查语言「{word}」"

    def test_keeps_natural_citations(self):
        """🔴 只剥内部记号，自然引用是文章价值本身，必须原样保留。"""
        out, _ = sanitize_article_body(BODY_WITH_MARKERS)
        assert "据《中国建筑装饰行业发展报告》显示" in out
        assert "清华大学的一项研究显示" in out

    def test_never_swallows_legitimate_content(self):
        """🔴 清洗器丢正文比留记号严重得多（两次自查缺陷的回归锁）。"""
        out, _ = sanitize_article_body(BODY_WITH_MARKERS)
        assert "某公司成立于 2015 年" in out, "含待核验的整行被删，连带丢了合法首句"
        assert "正常段落必须保留" in out, "核验清单节吞掉了后续正文"
        assert "结尾段也要保留" in out, "证据状态表吞掉了后续正文"

    def test_removed_content_preserved_in_metadata(self):
        """剥除 ≠ 丢失：内部审阅仍须可查。"""
        _, meta = sanitize_article_body(BODY_WITH_MARKERS)
        assert meta["removed_counts"]["evidence_ids"] >= 3
        assert meta["removed_counts"]["checklist_sections"] >= 1
        assert meta["review_phrase_lines"]
        assert meta["sanitizer_version"]

    def test_clean_body_untouched(self):
        clean = "# 标题\n\n据《报告》显示，行业稳定增长。\n"
        out, meta = sanitize_article_body(clean)
        assert out.strip() == clean.strip()
        assert meta == {}

    def test_never_fails_or_empties(self):
        """D8 零阻断：清洗永不拒存；洗空则保留原文。"""
        for body in ("", "   ", "待核验", "## 核验清单\n- 待核验\n"):
            out, meta = sanitize_article_body(body)
            if body.strip():
                assert out.strip(), f"清洗把正文洗空了: {body!r}"

    def test_sanitizer_is_red_before_green(self):
        """先红后绿：未清洗的正文必然含记号，清洗后必然不含。"""
        assert INTERNAL_MARKER_RE.search(BODY_WITH_MARKERS) is not None
        assert any(w in BODY_WITH_MARKERS for w in REVIEW_WORDS)
        out, _ = sanitize_article_body(BODY_WITH_MARKERS)
        assert INTERNAL_MARKER_RE.search(out) is None
        assert not any(w in out for w in REVIEW_WORDS)


class TestBodyHasNoInternalMarkers:
    """提示词层：模板不得再要求 LLM 输出内部记号。"""

    def _prompt(self):
        from writing.templates.canonical_family_templates import prompt_for_style
        return prompt_for_style("evidence_qa")

    def test_prompt_forbids_evidence_id_in_body(self):
        p = self._prompt()
        assert "绝不出现 Evidence ID" in p or "不得出现 Evidence ID" in p

    def test_prompt_no_longer_orders_inline_evidence_id(self):
        p = self._prompt()
        assert "每条可核验事实在相邻位置标注" not in p

    def test_prompt_asks_for_natural_citation(self):
        p = self._prompt()
        assert "据《XX》报道" in p or "自然的方式" in p

    def test_required_sections_have_no_checklist(self):
        from writing.article_style_contract import STYLE_FAMILIES
        for family in STYLE_FAMILIES.values():
            assert "核验清单" not in family.required_sections, family.code


# ===========================================================================
# ② 公开信源 = 可引用证据
# ===========================================================================
class TestPublicSourcesAreCitable:
    def test_downgrade_rule_removed(self):
        # [R3-A3 适配 2026-08-11] C5 把头部句从「全体条目均可直接引用」收窄为
        # **按来源可信状态分档**(生产 88.9% 裸域名,旧全称句会产出「据cnblogs.com」
        # 式假第三方归属)。本锁原意图=「search_result_only 降权规则已废除、公开信源
        # 可直接引用」——意图保留:降权语句仍必须不在;可引用性断言改钉 C5 分档句
        # (已核验档照抄归属句直接引用),不再断言旧全称措辞。
        from writing.evidence_pack import render_evidence_pack_for_writer
        rendered = render_evidence_pack_for_writer({"items": []})
        assert "search_result_only 与 body_retrieved_claim_unverified 都只是待核验线索" not in rendered
        assert "照抄其「归属句照抄」直接引用" in rendered
        # 反向:不许回退成对全体条目的无差别引用授权(C5 的收窄不许被悄悄放开)
        assert "均可直接引用并保留归属" not in rendered

    def test_precision_policy_no_longer_blocks_search_results(self):
        import writing.evidence_precision_policy as p
        import inspect
        src = inspect.getsource(p)
        assert "不得让 search_result_only" not in src

    def test_manifest_only_rule_still_present(self):
        """废降权 ≠ 放开造假：只准引真实存在的来源这条必须还在。"""
        from writing.evidence_pack import render_evidence_pack_for_writer
        rendered = render_evidence_pack_for_writer({"items": []})
        assert "严禁编造" in rendered


# ===========================================================================
# ③ 搜索方向 = 佐证
# ===========================================================================
class TestSearchDirectionIsCorroboration:
    def _lanes_src(self):
        import inspect
        import writing.evidence_research as er
        return inspect.getsource(er.collect_evidence_pack)

    def test_no_refute_hunting_lane(self):
        src = self._lanes_src()
        assert '"限制 风险 召回 监管"' not in src
        assert "限制 风险 召回 监管" not in src, "仍在主动构造核查型 query"

    def test_academic_first(self):
        src = self._lanes_src()
        assert "研究 论文" in src or "论文" in src
        assert "行业报告" in src

    def test_auto_trigger_on_insufficient_pack(self):
        import inspect
        import writing.article_generator_service as svc
        src = inspect.getsource(svc)
        assert "_EVIDENCE_MIN_ITEMS" in src
        assert "_pack_insufficient" in src
        assert "force=True" in src, "证据不足时未强制触发检索"


# ===========================================================================
# ④ 图片保底
# ===========================================================================
class TestImageFallback:
    def test_no_assets_still_gets_placeholders(self):
        """🔴 生产客诉：全篇零图。无授权资产时必须保底占位。"""
        from writing.article_generator_service import _insert_default_image_need_placeholder
        out = _insert_default_image_need_placeholder("# 标题\n\n正文内容。", [])
        assert out.count("[NEED_IMAGE") >= 1
        assert "awaiting_client_asset" in out

    def test_notice_tells_user_to_upload(self):
        from writing.article_generator_service import NO_ASSET_IMAGE_NOTICE
        assert "素材中心" in NO_ASSET_IMAGE_NOTICE
        assert "授权" in NO_ASSET_IMAGE_NOTICE

    def test_authorized_assets_unchanged(self):
        """有授权资产时行为不变（不退化）。"""
        from writing.article_generator_service import _insert_default_image_need_placeholder
        out = _insert_default_image_need_placeholder(
            "# 标题\n\n正文。", [{"image_type": "product"}])
        assert "role=product" in out
        assert "awaiting_client_asset" not in out

    def test_existing_marker_not_duplicated(self):
        from writing.article_generator_service import _insert_default_image_need_placeholder
        text = "# 标题\n\n[NEED_IMAGE role=case purpose=x]\n\n正文。"
        assert _insert_default_image_need_placeholder(text, []) == text

    def test_no_unauthorized_asset_is_ever_used(self):
        """🔴 版权红线：保底只给需求占位，绝不塞入任何素材 URL。"""
        from writing.article_generator_service import _no_asset_image_placeholders
        for block in _no_asset_image_placeholders():
            assert "http" not in block
            assert block.startswith("[NEED_IMAGE")


# ===========================================================================
# ⑤ 引用必须真实存在（H0）
# ===========================================================================
class TestCitationTruthfulnessH0:
    PACK = {"items": [
        {"url": "https://example.gov.cn/report"},
        {"url": "https://journal.example.edu/paper/1"},
    ]}

    def test_cited_urls_in_manifest_pass(self):
        body = "据 https://example.gov.cn/report 显示，行业增长稳定。"
        assert verify_citations_against_manifest(body, self.PACK)["ok"]

    def test_fabricated_url_is_caught(self):
        """🔴 H0：编造 URL 必须被抓出。"""
        body = "据 https://fake-institute.example/study 显示，效果提升 300%。"
        result = verify_citations_against_manifest(body, self.PACK)
        assert result["ok"] is False
        assert "https://fake-institute.example/study" in result["fabricated_urls"]

    def test_empty_manifest_makes_any_url_fabricated(self):
        result = verify_citations_against_manifest("见 https://x.example/y", {"items": []})
        assert result["ok"] is False

    def test_no_urls_is_ok(self):
        assert verify_citations_against_manifest("正文没有任何链接。", self.PACK)["ok"]

    def test_trailing_slash_normalized(self):
        body = "见 https://example.gov.cn/report/ 。"
        assert verify_citations_against_manifest(body, self.PACK)["ok"]


# ===========================================================================
# 端到端形态判别（同题材生成产物应满足的正文合同）
# ===========================================================================
class TestGeneratedBodyContract:
    """指令要求的成品判定：0 编号 / 0 状态表 / 0 待核验 / ≥2 自然引用 / ≥1 图占位。"""

    def _pipeline(self, raw: str, assets=None):
        from writing.article_generator_service import _insert_default_image_need_placeholder
        body, meta = sanitize_article_body(raw)
        return _insert_default_image_need_placeholder(body, assets or []), meta

    def test_full_contract(self):
        raw = """# 装修公司怎么选

据《中国建筑装饰行业发展报告》显示，行业集中度提升 [EV-1]。
同济大学的研究显示，预算超支多源于变更 [EV-2]。

## 核验清单
- 待核验资质

结论段保留。
"""
        out, _ = self._pipeline(raw)
        assert INTERNAL_MARKER_RE.search(out) is None
        assert not any(w in out for w in REVIEW_WORDS)
        assert out.count("显示") >= 2, "自然引用少于 2 条"
        assert out.count("[NEED_IMAGE") >= 1, "无图片占位"
        assert "结论段保留" in out

    def test_empty_search_degrades_without_review_language(self):
        """搜不到佐证时：少写，但正文不得留检查语言。"""
        raw = "# 标题\n\n本节暂无资料，待核验。\n\n可写的结论照常输出。\n"
        out, _ = self._pipeline(raw)
        assert not any(w in out for w in REVIEW_WORDS)
        assert "可写的结论照常输出" in out
