"""WP11 返工 · 清洗器**接线**判别测试（Deploy-CTO 门③ staging 实证返工单）。

上一轮 30 条测试全绿却漏了 P0：`sanitize_article_body` 写了、能用、单测全过，
**但没有任何调用方**——三个 `INSERT INTO articles` 都没接。真跑 4 篇，篇篇正文
留着 `〔EV-002〕`，客户直接可见。

根因 R2：那 30 条断言全部落在「函数行为」和「prompt 文本」两层，
**没有一条断言"一篇文章保存之后正文里零内部记号"**，所以接线断了测试一条都不红。

本文件专测**接线**，不重复测函数：
- 每条都必须在「把接线那行删掉」后转红（`test_*_would_fail_without_wiring` 用
  真实源码断言把接线钉死在三个保存路径上）；
- 三个保存入口各一条，防"修了一个漏两个"；
- 全角括号走精确模式（不靠裸编号兜底）；
- 清洗抛异常时文章照常保存（D8 零阻断）。
"""
import inspect
import re

import pytest

from writing.body_internal_marker_sanitizer import (
    _INLINE_ID_PATTERNS,
    sanitize_article_for_save,
)

# staging 真跑实测形态（deepseek-v4-flash 吐的就是全角方头括号）
STAGING_BODY = (
    "人工智能改变了科学、工程和临床研究的开展方式〔EV-002〕。在企业层面，"
    "洞见力以及高瞻远瞩的预测和决策能力〔EV-001〕。这意味着[BF-2]需要重估。"
    "（EV-3）另有说明。"
)
MARKER_RE = re.compile(r"(?:EV|BF)[-‑—]\d+", re.I)


# ===========================================================================
# 1. 三个保存路径都必须接线（防"修了一个漏两个"）
# ===========================================================================
SAVE_PATHS = (
    ("writing.article_generator_service", "保存路径1 首次生成"),
    ("tools.article_generator", "保存路径3 补发链"),
)


class TestAllSavePathsWired:
    def _src(self, module_name: str) -> str:
        import importlib
        return inspect.getsource(importlib.import_module(module_name))

    @pytest.mark.parametrize("module_name,label", SAVE_PATHS)
    def test_module_calls_sanitizer(self, module_name, label):
        src = self._src(module_name)
        assert "sanitize_article_for_save" in src, f"{label} 未接清洗器（返工单 R1）"

    def test_generator_service_wires_both_inserts(self):
        """article_generator_service 有 **两个** INSERT，必须各接一次。"""
        src = self._src("writing.article_generator_service")
        assert src.count("sanitize_article_for_save(") >= 2, (
            "article_generator_service 只接了一个保存路径，另一个仍会漏"
        )

    def test_every_insert_has_a_sanitizer_before_it(self):
        """🔴 结构判别：每个 INSERT INTO articles 之前都要出现过清洗调用。"""
        for module_name, label in SAVE_PATHS:
            src = self._src(module_name)
            for match in re.finditer(r"INSERT INTO articles", src):
                head = src[:match.start()]
                assert "sanitize_article_for_save" in head, (
                    f"{label} 存在未被清洗保护的 INSERT INTO articles"
                )

    def test_sanitize_runs_before_lineage(self):
        """🔴 顺序判别：清洗必须在 build_article_lineage 之前。

        lineage 用 article["content"] 算 current_content_hash；若清洗在其后，
        入库正文与 hash 不一致 = 对象身份漂移（H0）。
        """
        for module_name, label in SAVE_PATHS:
            src = self._src(module_name)
            for match in re.finditer(r"build_article_lineage\(", src):
                head = src[:match.start()]
                if "INSERT INTO articles" not in src[match.start():]:
                    continue
                assert "sanitize_article_for_save" in head, (
                    f"{label} 的清洗晚于 lineage，content_hash 将与入库正文不一致"
                )


# ===========================================================================
# 2. 端到端：保存包装器的实际效果（删掉接线这条必红）
# ===========================================================================
class TestSaveWrapperEndToEnd:
    def test_body_is_clean_after_save_wrapper(self):
        article = {"content": STAGING_BODY, "title": "t"}
        sanitize_article_for_save(article)
        assert MARKER_RE.search(article["content"]) is None, (
            f"保存后正文仍含内部编号: {article['content']}"
        )

    def test_removed_markers_queryable_in_quality_warning(self):
        """剥除 ≠ 丢失：内部审阅必须还能查到被剥的内容。"""
        article = {"content": STAGING_BODY}
        sanitize_article_for_save(article)
        markers = article["quality_warning"]["internal_markers"]
        assert markers["removed_counts"]["evidence_ids"] >= 4
        assert any("EV-002" in x for x in markers["evidence_ids"])

    def test_existing_quality_warning_preserved(self):
        """不得覆盖既有 quality_warning（evidence trust 等仍要在）。"""
        article = {"content": STAGING_BODY, "quality_warning": {"evidence": {"k": 1}}}
        sanitize_article_for_save(article)
        assert article["quality_warning"]["evidence"] == {"k": 1}
        assert "internal_markers" in article["quality_warning"]

    def test_non_dict_quality_warning_not_lost(self):
        article = {"content": STAGING_BODY, "quality_warning": "legacy-string"}
        sanitize_article_for_save(article)
        assert article["quality_warning"]["legacy"] == "legacy-string"

    def test_clean_body_untouched_and_no_warning_added(self):
        clean = "据《报告》显示，行业稳定增长。"
        article = {"content": clean}
        sanitize_article_for_save(article)
        assert article["content"] == clean
        assert "quality_warning" not in article


# ===========================================================================
# 3. R3 全角括号：精确模式命中，不依赖裸编号兜底
# ===========================================================================
class TestFullWidthBrackets:
    @pytest.mark.parametrize("token", [
        "〔EV-002〕", "【BF-12】", "「EV-3」", "『EV-4』", "［BF-5］",
        "（EV-1）", "[BF-2]", "(EV-9)",
    ])
    def test_precise_pattern_matches(self, token):
        """🔴 必须由前两条**精确**模式命中；靠第三条裸编号兜底 = 脆。"""
        assert any(p.search(token) for p in _INLINE_ID_PATTERNS[:2]), (
            f"{token} 未被精确括号模式命中，仅剩裸编号兜底"
        )

    def test_multi_id_in_full_width_bracket(self):
        assert any(p.search("〔EV-1、EV-2〕") for p in _INLINE_ID_PATTERNS[:2])

    def test_staging_form_fully_removed(self):
        article = {"content": "研究显示〔EV-002〕。企业层面〔EV-001〕如此。"}
        sanitize_article_for_save(article)
        assert "EV-002" not in article["content"]
        assert "EV-001" not in article["content"]
        assert "〔" not in article["content"] and "〕" not in article["content"]

    def test_real_model_names_not_damaged(self):
        """不得误伤正文里的真实型号名（BF-16 型钢材之类）。"""
        body = "该项目使用 BF16 精度与 EVA 材料。"
        article = {"content": body}
        sanitize_article_for_save(article)
        assert article["content"] == body


# ===========================================================================
# 4. D8 零阻断：清洗出问题也绝不丢稿
# ===========================================================================
class TestNeverBlocksSave:
    def test_sanitizer_exception_is_swallowed(self, monkeypatch):
        """🔴 清洗抛异常 → 文章照常保存、内容一字不丢。"""
        import writing.body_internal_marker_sanitizer as mod

        def boom(_content):
            raise RuntimeError("sanitizer exploded")

        monkeypatch.setattr(mod, "sanitize_article_body", boom)
        article = {"content": STAGING_BODY, "title": "t"}
        result = mod.sanitize_article_for_save(article)   # 不得抛
        assert result["content"] == STAGING_BODY, "清洗异常时正文被改动或丢失"

    def test_empty_and_missing_content_safe(self):
        for article in ({}, {"content": None}, {"content": ""}, {"content": "   "}):
            sanitize_article_for_save(dict(article))      # 不得抛

    def test_non_dict_input_safe(self):
        assert sanitize_article_for_save(None) is None
        assert sanitize_article_for_save("x") == "x"

    def test_body_that_sanitizes_to_empty_keeps_original(self):
        """全是记号的正文洗空 → 保留原文，宁可留记号也不丢稿。"""
        body = "## 核验清单\n- 待核验\n"
        article = {"content": body}
        sanitize_article_for_save(article)
        assert article["content"].strip(), "正文被洗空导致丢稿"
