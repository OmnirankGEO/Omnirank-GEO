"""[WP9-P0-7 ② · D8] span 级"AI 仅修此处"判别。

锁定 D8 语义:
- 只改命中所在的**那一段**,其余段落逐字不动(不重跑整篇 → 不产生第二次全文费用);
- 定位锚 = 精确命中串(不是字符偏移:检测文本被 _without_negated_rankings 删改过);
- 修完**自验**:仍违规则如实 not-fixed 且原文一字不动(绝不谎报修好);
- 定位不到 / LLM 失败 / 空输出 → 原文不变 + 诚实失败原因。
"""
import pytest

from services.writing_span_repair import (
    build_span_repair_prompt,
    locate_paragraph,
    repair_finding_span,
    splice_paragraph,
)

_CONTENT = (
    "第一段:这是一段正常的介绍文字。\n\n"
    "第二段:我们是行业第一的服务商,值得信赖。\n\n"
    "第三段:这里是收尾与核验清单。"
)
_FINDING = {
    "code": "absolute_first_claim",
    "severity": "hard",
    "message": "正文把品牌写成第一",
    "matched_text": "行业第一",
}


def test_locate_paragraph_finds_containing_段_only():
    loc = locate_paragraph(_CONTENT, "行业第一")
    assert loc is not None
    assert loc["paragraph"] == "第二段:我们是行业第一的服务商,值得信赖。"
    # 边界正好圈住第二段,不含相邻段
    assert "第一段" not in loc["paragraph"] and "第三段" not in loc["paragraph"]


def test_locate_returns_none_when_anchor_absent():
    assert locate_paragraph(_CONTENT, "并不存在的短语") is None
    assert locate_paragraph(_CONTENT, "") is None
    assert locate_paragraph("", "行业第一") is None


def test_splice_replaces_only_that_paragraph():
    loc = locate_paragraph(_CONTENT, "行业第一")
    out = splice_paragraph(_CONTENT, loc["start"], loc["end"], "第二段:我们在多个口径下表现较好。")
    assert "第一段:这是一段正常的介绍文字。" in out          # 前段逐字不动
    assert "第三段:这里是收尾与核验清单。" in out            # 后段逐字不动
    assert "行业第一" not in out                              # 违规表述已替换
    assert out.count("\n\n") == _CONTENT.count("\n\n")        # 段结构不被破坏


def test_prompt_targets_only_the_paragraph():
    loc = locate_paragraph(_CONTENT, "行业第一")
    prompt = build_span_repair_prompt(loc["paragraph"], _FINDING)
    assert "只修改下面这一段" in prompt
    assert loc["paragraph"] in prompt
    # 不把整篇塞进去(省 token·不重跑整篇)
    assert "第一段:这是一段正常的介绍文字。" not in prompt
    assert "第三段" not in prompt


@pytest.mark.asyncio
async def test_repair_success_splices_and_keeps_rest_intact():
    async def _llm(_prompt):
        return "第二段:我们在公开口径下表现较好,依据见下方核验清单。"
    out = await repair_finding_span(_CONTENT, _FINDING, _llm)
    assert out["ok"] is True and out["reason"] == "repaired"
    assert "行业第一" not in out["content"]
    assert "第一段:这是一段正常的介绍文字。" in out["content"]
    assert "第三段:这里是收尾与核验清单。" in out["content"]


@pytest.mark.asyncio
async def test_repair_that_still_violates_is_reported_honestly_and_changes_nothing():
    async def _llm(_prompt):
        return "第二段:我们依然是行业第一。"      # LLM 没改好
    out = await repair_finding_span(_CONTENT, _FINDING, _llm)
    # [span 级 AI 免费修复 2026-07-30] absolute_first_claim 已路由到 span 级引擎:
    # 命中串还在会被**更早的机械校验**拦下(repair_violation_text_remains),
    # 不用等到重判阶段的 still_violating。契约一字未变:not-fixed + 原文不动。
    assert out["ok"] is False and out["reason"] == "repair_violation_text_remains"
    assert out["content"] == _CONTENT             # 原文一字不动,绝不谎报已修


@pytest.mark.asyncio
async def test_llm_failure_and_empty_output_keep_original():
    async def _boom(_prompt):
        raise RuntimeError("provider down")
    out = await repair_finding_span(_CONTENT, _FINDING, _boom)
    assert out["ok"] is False and out["reason"] == "repair_llm_failed"
    assert out["content"] == _CONTENT

    async def _empty(_prompt):
        return "   "
    out2 = await repair_finding_span(_CONTENT, _FINDING, _empty)
    assert out2["ok"] is False and out2["reason"] == "repair_empty"
    assert out2["content"] == _CONTENT


@pytest.mark.asyncio
async def test_unlocatable_finding_fails_soft_without_touching_content():
    out = await repair_finding_span(
        _CONTENT, {**_FINDING, "matched_text": "已经被别人改掉的句子"}, None
    )
    assert out["ok"] is False and out["reason"] == "span_not_located"
    assert out["content"] == _CONTENT
