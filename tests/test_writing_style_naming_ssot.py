"""写作文体命名 SSOT 判别测试（2026-07-28）。

权威：`docs/AI-CONTEXT/WRITING_STYLE_NAMING_SSOT_2026-07-28.md`

每条都按「删掉守卫会转红」写。反向锁（应当放行/应当保持为 None 的场景）单独成条，
防止把守卫写成"什么都拦"。

覆盖：
  P0-1  排名文断链 —— active style 必有家族；家族配比折算合计必须 100
  P0-2  字数数值 SSOT 唯一 —— 活链路只有 render_length_instruction 提字数
  P0-3  归档模板不得被生产代码引用
  P1-4  六家族 UI members 重建结果 == 后端默认档（逐键）
  P1-5  content_ratios 消费方冻结
  命名  prompt_source / 死 prompt dict 不得复活为运行时依赖
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================
# P0-1 · 排名文断链
# ===========================================================================
def test_every_active_style_code_has_a_family():
    from writing.direction_distribution import (
        STYLE_CODES_WITHOUT_FAMILY,
        validate_style_family_mapping,
    )

    assert validate_style_family_mapping() == []
    # None 的合法持有者只有这两个，且理由不同（退役 / 固定槽位）
    assert STYLE_CODES_WITHOUT_FAMILY == frozenset({"trojan_horse", "company_profile"})


def test_ranking_styles_belong_to_multi_brand_comparison_family():
    from writing.direction_distribution import style_code_to_user_choice

    for code in ("ranking_v2", "authority_ranking", "recommendation_review", "comparison_review"):
        assert style_code_to_user_choice(code) == "multi_brand_comparison", code
    # 反向锁：退役与固定槽位仍必须是 None，不能顺手也给个家族
    assert style_code_to_user_choice("trojan_horse") is None
    assert style_code_to_user_choice("company_profile") is None


def test_family_ratio_coverage_loses_nothing():
    """断链的直接观测量：折算到六家族后合计必须 100，且零蒸发。

    删掉 ranking_v2 的家族映射 → 合计掉到 80、dropped_total 变 20 → 本条转红。
    """
    from writing.direction_distribution import family_ratio_coverage

    for industry in ("装修建材", "旅游酒店", "医疗健康", "法律商务", ""):
        coverage = family_ratio_coverage(industry)
        assert coverage["dropped_style_ratios"] == {}, (industry, coverage)
        assert coverage["family_total"] == pytest.approx(100.0), (industry, coverage)
        assert coverage["complete"] is True, industry


def test_default_category_ranking_family_reaches_configured_share():
    """默认档榜单族必须真的到 52%，而不是只在后端配比里好看。"""
    from writing.direction_distribution import family_ratio_coverage

    coverage = family_ratio_coverage("装修建材")
    assert coverage["family_ratios"]["multi_brand_comparison"] == pytest.approx(52.0)

    # 反向锁：高供给 B2C 仍是指南/问答为主，分治没有被这次修复冲掉
    hotel = family_ratio_coverage("旅游酒店")
    assert hotel["family_ratios"]["multi_brand_comparison"] == pytest.approx(8.0)
    assert hotel["family_ratios"]["implementation_guide"] == pytest.approx(30.0)


def test_title_generator_ratio_block_sums_to_100():
    """喂给选题 LLM 的六类配比表 —— 事故当时这里是 80%。"""
    from writing.keyword_topic_generator import _family_ratio_prompt

    text = _family_ratio_prompt("装修建材")
    values = [float(v) for v in re.findall(r"：([0-9.]+)%", text)]
    assert values, text
    assert sum(values) == pytest.approx(100.0), text
    assert "选购与多品牌比较：52%" in text


# ===========================================================================
# P0-2 · 字数数值 SSOT 唯一
# ===========================================================================
_WORD_COUNT_RE = re.compile(r"\d{3,5}\s*[-~—]?\s*\d{0,5}\s*字")


def _live_prompt_fragments(style_code: str = "ranking_v2") -> dict[str, str]:
    """复现 article_generator_service 拼装 system_prompt 的活片段。"""
    from writing.article_length_contract import build_article_length_plan, render_length_instruction
    from writing.article_style_contract import RANKING_REVIVAL_CONTRACT
    from writing.client_presence_policy import build_client_presence_prompt
    from writing.ranking_prompt_v9 import format_keyword_intent_funnel
    from writing.source_disclosure_style import SOURCE_DISCLOSURE_PROMPT
    from writing.style_registry import get_prompt_for_style

    pack = {
        "items": [
            {
                "evidence_id": f"EV-{i}",
                "url": f"https://pub{i % 5}.example.com/a{i}",
                "claim": "c",
                "verification_status": "official_record",
                "official_record_id": f"R{i}",
                "publisher": f"pub{i % 5}",
                "relationship": "support",
            }
            for i in range(12)
        ]
    }
    plan = build_article_length_plan(style_code, evidence_pack=pack, verified_candidate_count=8)
    return {
        "style_registry_canonical_family": get_prompt_for_style(style_code),
        "keyword_intent_funnel": format_keyword_intent_funnel("commercial", "decision"),
        "length_instruction": render_length_instruction(plan),
        "source_disclosure": SOURCE_DISCLOSURE_PROMPT,
        "client_presence": build_client_presence_prompt("测试品牌", ["竞品甲"]),
        "ranking_revival": RANKING_REVIVAL_CONTRACT,
    }


def test_live_prompt_chain_has_exactly_one_word_count_source(monkeypatch):
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    fragments = _live_prompt_fragments()
    with_counts = {
        name for name, text in fragments.items() if _WORD_COUNT_RE.search(text)
    }
    assert with_counts == {"length_instruction"}, with_counts


def test_length_instruction_is_the_only_authority_and_matches_the_plan(monkeypatch):
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    from writing.article_length_contract import RANKING_FAMILY_MINIMUM_CHARS

    instruction = _live_prompt_fragments()["length_instruction"]
    numbers = {int(n) for n in re.findall(r"(\d{4,5})\s*字", instruction)}
    assert RANKING_FAMILY_MINIMUM_CHARS in numbers
    # 反向锁：V9 的 3500/4000 绝不能出现在活链路里
    assert 3500 not in numbers and 4000 not in numbers


def test_v9_dangerous_constants_never_reach_any_generation_prompt(monkeypatch):
    """3500/4000 与"客户必须高于竞品"评分段不在任何生成 prompt 里。

    归属订正：``RANKING_PROMPT_V9`` 本身已是安全兼容存根，危险内容在
    ``GEO_SIGNAL_CHECK``（正文）与 ``GEO_RANKING_SIGNALS``（字典）里。
    """
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    from writing.ranking_prompt_v9 import GEO_RANKING_SIGNALS, GEO_SIGNAL_CHECK
    from writing.style_registry import WRITING_STYLES, get_prompt_for_style

    # 锚点用不含标点的独特子串（全角标点会让精确 grep 扑空）
    anchors = ("区间随机", "客户必须高于竞品")
    for anchor in anchors:
        assert anchor in GEO_SIGNAL_CHECK, anchor
    assert GEO_RANKING_SIGNALS["word_count"]["min"] == 3500

    for style_code in WRITING_STYLES:
        prompt = get_prompt_for_style(style_code)
        for anchor in anchors:
            assert anchor not in prompt, (style_code, anchor)
        assert "3500" not in prompt, style_code


def test_v9_dangerous_constants_have_no_production_consumer():
    """它们目前是休眠的。谁把它们接进生产代码，本条转红。"""
    offenders = []
    for d in ("writing", "services", "api", "routes", "tools", "db", "config"):
        for path in (ROOT / d).rglob("*.py"):
            if path.name == "ranking_prompt_v9.py" or "_archived" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "GEO_SIGNAL_CHECK" in text or "GEO_RANKING_SIGNALS" in text:
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], offenders


def test_v9_carries_the_do_not_reconnect_banner():
    source = (ROOT / "writing" / "ranking_prompt_v9.py").read_text(encoding="utf-8")
    head = source[:3500]
    assert "不得接回生成链" in head
    assert "article_length_contract" in head
    assert "self_invented_scoring_system" in head
    assert "canonical_family_templates" in head


def test_ranking_registry_metadata_is_not_stale():
    """复活后 status/description 必须同步，否则 admin 前端仍显示"已停用"。"""
    from writing.article_style_contract import DISABLED_NEW_GENERATION_STYLES
    from writing.style_registry import WRITING_STYLES

    for code in ("ranking_v2", "authority_ranking"):
        entry = WRITING_STYLES[code]
        assert entry["status"] == "active", code
        assert "历史" not in entry["description"], code
    # 反向锁：真退役的仍是 legacy_disabled
    assert WRITING_STYLES["trojan_horse"]["status"] == "legacy_disabled"
    assert "trojan_horse" in DISABLED_NEW_GENERATION_STYLES


def test_prompt_source_metadata_is_never_read_at_runtime():
    """prompt_source 是历史元数据。谁把它接回运行时解析，本条转红。"""
    offenders = []
    for path in list(ROOT.glob("*.py")) + [
        p for d in ("writing", "services", "api", "routes", "tools", "db", "config")
        for p in (ROOT / d).rglob("*.py")
    ]:
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        # 只看真代码：剥掉注释行与 docstring，避免"说明它不被读取"的文字自触发
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        literals = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                key = node.slice
                if isinstance(key, ast.Constant) and key.value == "prompt_source":
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} subscript")
            elif isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "prompt_source"
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} .get()")
        del literals
    assert offenders == [], offenders


def test_dead_prompt_dicts_stay_dead():
    """ARTICLE_PROMPTS / QUERY_TYPE_PROMPTS 是死 dict，复活它们等于绕开合同。"""
    consumers = []
    for d in ("writing", "services", "api", "routes", "tools"):
        for path in (ROOT / d).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name in ("ARTICLE_PROMPTS", "QUERY_TYPE_PROMPTS"):
                for match in re.finditer(rf"\b{name}\b", text):
                    line_start = text.rfind("\n", 0, match.start()) + 1
                    line = text[line_start:text.find("\n", match.start())]
                    if re.match(rf"\s*{name}\s*=", line):
                        continue  # 定义本身允许存在
                    consumers.append(f"{path.relative_to(ROOT)}: {line.strip()[:80]}")
    assert consumers == [], consumers


# ===========================================================================
# P0-3 · 归档模板不得被生产代码引用
# ===========================================================================
def test_archived_prompts_are_not_imported_by_production_code():
    offenders = []
    for d in ("writing", "services", "api", "routes", "tools", "db", "config"):
        for path in (ROOT / d).rglob("*.py"):
            if "_archived" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"_archived[.\s]*import|from\s+\S*_archived", text):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], offenders


def test_archived_v10_carries_candidate_banner():
    head = (ROOT / "writing" / "_archived" / "ranking_prompt_v10.py").read_text(
        encoding="utf-8"
    )[:1500]
    assert "候选未接入" in head
    assert "Owner" in head


# ===========================================================================
# P1-4 · 六家族 UI 与 11 码读写对接
# ===========================================================================
def _parse_settings_page_ratios() -> tuple[dict[str, int], dict[str, int]]:
    """从 TSX 里解析 FAMILY_RATIO_FIELDS.members 与 PINNED_ZERO_STYLE_RATIOS。"""
    source = (ROOT / "frontend" / "src" / "pages" / "Settings" / "SettingsPage.tsx").read_text(
        encoding="utf-8"
    )
    members: dict[str, int] = {}
    block = source[source.index("const FAMILY_RATIO_FIELDS"):]
    block = block[: block.index("] as const;")]
    for chunk in re.findall(r"members:\s*\{([^}]*)\}", block):
        for code, value in re.findall(r"(\w+)\s*:\s*(\d+)", chunk):
            members[code] = int(value)

    pinned_block = source[source.index("const PINNED_ZERO_STYLE_RATIOS"):]
    pinned_block = pinned_block[: pinned_block.index("};")]
    pinned = {c: int(v) for c, v in re.findall(r"(\w+)\s*:\s*(\d+)", pinned_block)}
    return members, pinned


def test_settings_page_family_members_rebuild_backend_default():
    """admin 保存一次不得把后端默认档改掉 —— 这是 2026-07-28 修的真 bug。

    旧代码里 ranking_v2 不在 members、又被 DISABLED_STYLE_RATIOS 最后 spread 清零，
    保存一次就把 20% 打回 0。把 ranking_v2 从 members 里删掉 → 本条转红。
    """
    from config.settings_manager import get_effective_style_ratios

    members, pinned = _parse_settings_page_ratios()
    rebuilt = {**pinned, **members}
    backend_default = get_effective_style_ratios("装修建材", unit="percent")

    assert len(rebuilt) == 11, rebuilt
    assert sum(rebuilt.values()) == 100, rebuilt
    assert rebuilt == backend_default, {
        k: (rebuilt.get(k), backend_default.get(k))
        for k in set(rebuilt) | set(backend_default)
        if rebuilt.get(k) != backend_default.get(k)
    }


def test_settings_page_pins_only_retired_and_unallocated_styles():
    from writing.article_style_contract import DISABLED_NEW_GENERATION_STYLES

    _, pinned = _parse_settings_page_ratios()
    # 真退役集必须在钉零表里
    assert set(DISABLED_NEW_GENERATION_STYLES) <= set(pinned)
    # 反向锁：ranking_v2 绝不能再被钉零
    assert "ranking_v2" not in pinned


def test_settings_page_survives_backend_validator():
    """前端重建的 payload 必须能过 server.py 的 style_ratios validator。"""
    from writing.article_style_contract import DISABLED_NEW_GENERATION_STYLES
    from writing.style_registry import WRITING_STYLES

    members, pinned = _parse_settings_page_ratios()
    payload = {**pinned, **members}
    expected_keys = set(WRITING_STYLES) - {"company_profile"}
    assert set(payload) == expected_keys, set(payload) ^ expected_keys
    assert sum(payload.values()) == 100
    assert all(payload[code] == 0 for code in DISABLED_NEW_GENERATION_STYLES)


def test_ui_copy_no_longer_contradicts_revived_ranking():
    hall = (ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx").read_text(
        encoding="utf-8"
    )
    # 用不含标点的独特子串锚定（全角标点会让精确 grep 扑空）
    assert "不编名次和评分" not in hall
    assert "禁自创评分体系" in hall


# ===========================================================================
# P1-5 · content_ratios 冻结
# ===========================================================================
CONTENT_RATIOS_FROZEN_CONSUMERS = {
    "config/settings_manager.py",
    "writing/config.py",
    "tools/article_generator.py",
    "writing/topic_dispatcher.py",
    "server.py",
    "frontend/src/pages/Settings/SettingsPage.tsx",
    "frontend/src/components/writing/DistributionConfigDialog.tsx",
    "frontend/src/lib/api.ts",
}


def test_content_ratios_has_no_new_consumers():
    """content_ratios 已 deprecated（standby 待淘汰）。新增消费方即判违规。

    要新增必须先改命名 SSOT 文档并在此登记，逼一次显式决策。
    """
    found: set[str] = set()
    roots = [ROOT / d for d in ("config", "writing", "services", "api", "routes", "tools", "db")]
    roots += [ROOT / "frontend" / "src"]
    files = [ROOT / "server.py"]
    for r in roots:
        if r.exists():
            files += [p for p in r.rglob("*") if p.suffix in (".py", ".ts", ".tsx")]
    for path in files:
        if "_archived" in path.parts or path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "content_ratios" in text:
            found.add(str(path.relative_to(ROOT)).replace("\\", "/"))
    assert found <= CONTENT_RATIOS_FROZEN_CONSUMERS, found - CONTENT_RATIOS_FROZEN_CONSUMERS


def test_naming_ssot_document_exists_and_pins_the_four_layers():
    doc = (ROOT / "docs" / "AI-CONTEXT" / "WRITING_STYLE_NAMING_SSOT_2026-07-28.md").read_text(
        encoding="utf-8"
    )
    for anchor in ("style_code", "family_code", "content_ratios", "article_length_contract"):
        assert anchor in doc, anchor
    assert "映射铁律" in doc


# ===========================================================================
# 结构性自检：判别测试自身不能是恒真
# ===========================================================================
def test_family_mapping_validator_actually_detects_a_break(monkeypatch):
    """把 ranking_v2 的家族改回 None，validate/coverage 必须立刻转红。

    这条锁住"守卫真的会响"，避免上面几条退化成恒真断言。
    """
    import writing.direction_distribution as dd

    broken = dict(dd.STYLE_CODE_TO_USER_CHOICE)
    broken["ranking_v2"] = None
    monkeypatch.setattr(dd, "STYLE_CODE_TO_USER_CHOICE", broken)

    errors = dd.validate_style_family_mapping()
    assert "active_style_missing_family:ranking_v2" in errors

    coverage = dd.family_ratio_coverage("装修建材")
    assert coverage["complete"] is False
    assert coverage["dropped_style_ratios"] == {"ranking_v2": 20.0}
    assert coverage["family_total"] == pytest.approx(80.0)
