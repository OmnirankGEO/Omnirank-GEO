"""W1 返工 · R1 判别测试:**落库正文里 `##` 真出现**。

## 这一份为什么必须走真保存流程

上一版(20 班)交付时,我给规格卡写的锁验的是「小标题(97.62%) 这个数字渲染对不对」——
**统计数值对,产物照样是粗体伪标题**。Codex 终审拿历史批 0/10 vs 新批 4/10 把这件事
钉在脸上:锁在数字上就是锁了个跟目标无关的东西。

所以这一份的判据只有一句:**把伪标题正文喂进真保存流程,落库那一版必须有真 `##`**。
断言全部打在 `INSERT INTO articles` 的参数上,不是任何 helper 的返回值 ——
「调用点丢弃返回值」「接线被删」这两种最常见的假绿,在这里都会当场转红。

harness 沿用 `tests/test_c4_review_autopilot_2026_07_27.py` 那一套(同一个仓库里
已经验证过能真跑到 INSERT 的假库游标),不另起炉灶。
"""
from __future__ import annotations

import asyncio
import inspect
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_c4_review_autopilot_2026_07_27 import (  # noqa: E402
    _make_service,
    _wire_fake_db,
)
from writing.article_writer import H2_PATTERN, MIN_H2_HEADINGS  # noqa: E402

# 一篇「模型用粗体冒充小标题」的正文 —— 这正是生产 4/10 的形态。
_PSEUDO_SECTIONS = [
    "怎么核验一家服务商的资质",
    "价格区间怎么看",
    "交付周期一般多久",
    "常见的三个坑",
    "签合同前要确认什么",
    "售后怎么算",
]
_PSEUDO_BODY = "# 本地装修怎么选\n\n先说结论:核对公开资质与交付记录。\n\n" + "\n\n".join(
    f"**{name}**\n\n这一节的正文说明,公开记录可自行查证。" for name in _PSEUDO_SECTIONS
)
# 反向对照用:本来就写对了的正文,过一遍保存链必须原样保留。
_REAL_BODY = "# 本地装修怎么选\n\n先说结论。\n\n" + "\n\n".join(
    f"## {name}\n\n这一节的正文说明,公开记录可自行查证。" for name in _PSEUDO_SECTIONS
)


def _real_h2_titles(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^##\s+(.+)$", text, re.MULTILINE)]


@pytest.fixture()
def _no_autopilot(monkeypatch):
    """把自动驾驶按住 —— 本锁只想看格式修复那一环,不想被证据修复的 LLM 干扰。"""
    import writing.article_generator_service as svc_mod

    async def _noop(title, content, topic, article, trust, target_entity=""):  # [R5.1] 契约同签名
        return content, trust

    monkeypatch.setattr(svc_mod, "apply_review_autopilot", _noop)
    monkeypatch.setattr(svc_mod, "_copy_article_distilled_lineage", lambda *a, **k: None)
    return monkeypatch


def _save_path_rows():
    return [
        ("SELECT q.brand_id, b.name AS brand_name", None),
        ("SELECT id FROM topics WHERE id=%s FOR UPDATE", {"id": 11}),
        ("COALESCE(MAX(version),0)", {"max_version": 0}),
        ("SELECT COALESCE(q.owner_user_id", None),
        ("SELECT brand_id FROM quotes", None),
    ]


def _topic():
    return {
        "id": 11,
        "publication_profile": "standard",
        "evidence_mode": "unknown",
        "style_code": "buying_guide",
        "title": "本地装修怎么选",
        "keyword": "本地装修",
    }


def _article(body: str):
    return {
        "topic_id": 11,
        "title": "本地装修怎么选",
        "content": body,
        "word_count": len(body),
        "style": "buying_guide",
        "publication_profile": "standard",
    }


def _run_save_path(monkeypatch, body: str) -> tuple[str, dict]:
    """跑保存路径 1/3(首次生成),返回 (落库正文, 落库 quality_warning)。"""
    inserts: list = []
    _wire_fake_db(monkeypatch, _save_path_rows(), inserts)
    service = _make_service()
    monkeypatch.setattr(
        service, "_freeze_topic_delivery_options",
        lambda topic: topic.update(
            {"_effective_add_images": False, "_effective_add_contact": False}
        ),
        raising=False,
    )
    article_id = asyncio.run(service._save_article(_topic(), _article(body)))
    assert article_id == 777
    assert inserts, "必须真的走到 INSERT INTO articles"
    saved_qw = inserts[0][7]
    return inserts[0][3], getattr(saved_qw, "adapted", saved_qw)


# ===========================================================================
# 主判据:落库正文里 `##` 真出现
# ===========================================================================

def test_save_path_persists_real_markdown_headings(_no_autopilot):
    """路径 1/3:喂进去全是粗体伪标题,**落库那一版必须是真 `##`**。"""
    saved, _qw = _run_save_path(_no_autopilot, _PSEUDO_BODY)

    titles = _real_h2_titles(saved)
    assert titles == _PSEUDO_SECTIONS, f"落库正文的真小标题不对:{titles}"
    for name in _PSEUDO_SECTIONS:
        assert f"**{name}**" not in saved, f"粗体伪标题 {name} 还在落库正文里"


def test_persisted_body_clears_the_h4_gate_it_used_to_fail(_no_autopilot):
    """修完必须真的把 H4 从失败变通过 —— 这是这次返工要的**结果**,不是中间量。"""
    from writing.article_writer import validate_article_structure

    assert len(H2_PATTERN.findall(_PSEUDO_BODY)) == 0
    _passed_before, hard_before, _soft = validate_article_structure(
        _PSEUDO_BODY, style_code="buying_guide")
    assert "H4" in hard_before, "前提不成立:伪标题正文本来就该 H4 失败"

    saved, _qw = _run_save_path(_no_autopilot, _PSEUDO_BODY)
    assert len(H2_PATTERN.findall(saved)) >= MIN_H2_HEADINGS
    _passed_after, hard_after, _soft2 = validate_article_structure(
        saved, style_code="buying_guide")
    assert "H4" not in hard_after, "落库正文仍然过不了 H4"


def test_repair_is_recorded_not_silent(_no_autopilot):
    """改了正文就必须留痕 —— 静默改写不可追溯。"""
    _saved, qw = _run_save_path(_no_autopilot, _PSEUDO_BODY)
    assert qw, "quality_warning 没落库"
    note = qw.get("heading_repair")
    assert note, f"没有 heading_repair 留痕:{qw}"
    assert note["converted_count"] == len(_PSEUDO_SECTIONS)
    assert note["converted_titles"] == _PSEUDO_SECTIONS


def test_already_correct_body_is_not_rewritten(_no_autopilot):
    """反向对照:本来就写对的正文过一遍保存链,小标题一个不多一个不少、也不留痕。

    少了这条,上面那些锁可以被"无脑给每行加 ##"的实现骗过去。
    """
    saved, qw = _run_save_path(_no_autopilot, _REAL_BODY)
    assert _real_h2_titles(saved) == _PSEUDO_SECTIONS
    assert not (qw or {}).get("heading_repair"), "没改动却留了修复痕迹"


def test_rewrite_path_persists_real_markdown_headings(_no_autopilot):
    """路径 2/3(rewrite):同一条判据。两条路径各写各的接线,必须各锁一次。"""
    import writing.article_generator_service as svc_mod

    monkeypatch = _no_autopilot
    monkeypatch.setattr(
        svc_mod, "get_llm_config", lambda *a, **k: ("http://x", "key", "model", None),
        raising=False,
    )
    inserts: list = []
    topic_row = {
        "id": 11, "quote_id": 9, "optimized_title": "本地装修怎么选",
        "original_keyword": "本地装修", "article_style": "buying_guide",
        "style_code": "buying_guide", "user_choice": "implementation_guide",
        "publication_profile": "standard", "evidence_mode": "unknown",
        "status": "completed",
    }
    latest_row = {
        "version": 1, "publication_profile": "standard", "style_version": None,
        "generation_request_snapshot": None, "content": "旧正文",
        "quality_warning": None, "evidence_pack": None,
        "brand_fact_snapshot": None, "brand_id": None,
    }
    _wire_fake_db(monkeypatch, [
        ("SELECT * FROM topics WHERE id=%s", topic_row),
        ("FROM articles a LEFT JOIN quotes q", latest_row),
        *_save_path_rows(),
    ], inserts)

    service = _make_service()

    async def _fake_generate(topic, api_url, api_key, model):
        return {"title": "本地装修怎么选", "content": _PSEUDO_BODY}

    monkeypatch.setattr(service, "_generate_validated_with_rewrite_once", _fake_generate, raising=False)
    monkeypatch.setattr(service, "_is_invalid_content", lambda s: False, raising=False)
    monkeypatch.setattr(
        service, "_freeze_rewrite_delivery_options",
        lambda topic, latest: topic.update(
            {"_effective_add_images": False, "_effective_add_contact": False}
        ),
        raising=False,
    )

    result = asyncio.run(service.rewrite_article(11))
    assert not (isinstance(result, dict) and result.get("error")), f"rewrite 失败: {result}"
    assert inserts, "必须真的走到 INSERT"
    saved = inserts[0][3]
    assert _real_h2_titles(saved) == _PSEUDO_SECTIONS
    for name in _PSEUDO_SECTIONS:
        assert f"**{name}**" not in saved


# ===========================================================================
# 接线锁:四个正文写入点一个不许漏,且必须在 lineage 之前
# ===========================================================================
_WIRED_CALL = "repair_article_for_save("


def _src(obj) -> str:
    return inspect.getsource(obj)


def test_save_path_repairs_before_lineage():
    from writing.article_generator_service import ArticleGeneratorService

    src = _src(ArticleGeneratorService._save_article)
    assert _WIRED_CALL in src, "保存路径 1/3 没接格式修复"
    assert src.index(_WIRED_CALL) < src.index("build_article_lineage("), (
        "修复必须在 lineage 之前,否则 current_content_hash 覆盖的是修复前正文"
    )


def test_rewrite_path_repairs_before_lineage():
    from writing.article_generator_service import ArticleGeneratorService

    src = _src(ArticleGeneratorService.rewrite_article)
    assert _WIRED_CALL in src, "保存路径 2/3 没接格式修复"
    assert src.index(_WIRED_CALL) < src.index("build_article_lineage(")


def test_replacement_path_repairs_before_lineage():
    """路径 3/3 补发链在 `tools/article_generator.py`,不在生成服务里 ——
    上一批 D11 清洗就是靠这条锁才没漏掉它。"""
    src = (ROOT / "tools" / "article_generator.py").read_text(encoding="utf-8")
    assert _WIRED_CALL in src, "补发链没接格式修复"
    assert src.index(_WIRED_CALL) < src.index("build_article_lineage("), (
        "补发链的修复也必须在 lineage 之前"
    )


def test_placement_kb_fix_path_repairs_before_update():
    """第 4 个写入点:`placement_service` 的一键修复。

    它是子代理扫全仓扫出来的 —— 原工单只点了三条保存路径。它整篇 LLM 重写后
    直接 `UPDATE articles SET content`,伪标题概率一点不比另三条低。
    """
    src = (ROOT / "services" / "placement_service.py").read_text(encoding="utf-8")
    assert "repair_pseudo_headings(" in src, "一键修复链没接格式修复"
    assert src.index("repair_pseudo_headings(") < src.index(
        "UPDATE articles SET content=%s"
    ), "格式修复必须在 UPDATE 之前"


def test_every_known_body_write_point_is_covered():
    """元判据:已知的正文写入点清单不许悄悄变长而没人接线。

    新增一个写 `articles.content` 的地方,这条会转红,逼人显式处置
    (接上,或在这里写明为什么不接)。
    """
    covered = {
        "writing/article_generator_service.py",  # 路径 1/3 + 2/3
        "tools/article_generator.py",            # 路径 3/3 补发链
        "services/placement_service.py",         # 一键修复
    }
    # 逐条写明**为什么不接** —— 一个不写理由的豁免名单等于没有名单。
    exempt = {
        # 只做占位符定点替换([NEED_IMAGE]→[CLIENT_IMAGE]),不产出散文。
        "api/image_asset_api.py",
        # 存量回溯清洗脚本(手动触发),不是生成链。
        "scripts/sanitize_legacy_article_bodies.py",
        # 集成 fixture / schema 校验脚本:写的是合成行,且在未提交事务里回滚。
        "scripts/validate_geo_article_v14_pg16.py",
        "scripts/validate_geo_article_v14_schema_pg16.py",
        "scripts/validate_geo_article_review_concurrency_pg16.py",
        "scripts/validate_legacy_article_plan_route.py",
        # server.py 三处写 content,三处都**不该**在本包接:
        #   · 片段级修复端点 ×2(`writing_span_repair` / `article_review_autopilot`)
        #     —— 它们替换的是既有正文里的一段;这篇正文本身已经过修复后的保存路径
        #     落库(真 `##` 满足门槛),修复器在这种文档上按设计是空操作。
        #   · 人工编辑保存(`payload.content`)—— **绝不能**自动改写用户亲手写的正文。
        #     "局部"这个词的另一半意思就是:人写的东西不碰。
        "server.py",
    }
    hits: set[str] = set()
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "docs/", ".venv/", "frontend/")):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "INSERT INTO articles" in text or re.search(
            r"UPDATE\s+articles\s+SET\s+content", text, re.IGNORECASE
        ):
            hits.add(rel)
    unexpected = hits - covered - exempt
    assert not unexpected, f"出现未处置的正文写入点:{sorted(unexpected)}"


# ===========================================================================
# 第一道:规格卡文案本身必须说清「小标题 = `## `」
# ===========================================================================
def _families() -> tuple[str, ...]:
    """六族名单取自 SSOT,不在测试里手抄 —— 手抄过一次就写错过一次(`trend_policy`
    的真名是 `trend_policy_risk`,是这条改成 import 之后才发现的)。"""
    from writing.title_element_contract import FAMILY_TITLE_PROFILES

    return tuple(FAMILY_TITLE_PROFILES)


_FAMILIES = _families()
assert len(_FAMILIES) == 6, f"六族名单变了:{_FAMILIES}"


@pytest.mark.parametrize("family", _FAMILIES)
def test_every_family_card_states_the_markdown_heading_format(family):
    from writing.article_type_spec_cards import (
        FEATURE_LABELS, HEADING_FEATURE, build_spec_card_prompt,
    )

    block = build_spec_card_prompt(family, engines=("deepseek", "qwen"))
    assert FEATURE_LABELS[HEADING_FEATURE] in block, "前提不成立:这张卡本来就没提小标题"
    # 三件缺一不可:怎么写(行首 `## `)/ 不许怎么写(粗体行)/ 正反例各一。
    # 只验其中一件时,变异「把祈使句换成空话、只留例子」会存活(W01 实测)。
    assert "行首写 `## `" in block, f"{family} 的卡没有直说小标题要写在行首"
    assert "禁止用独占一行的" in block, f"{family} 的卡没有明确禁止粗体伪标题"
    assert "正确:" in block and "错误:" in block, f"{family} 的卡缺正反例"


def test_format_rule_sits_right_under_the_level_table():
    """位置也是判据:格式要求必须紧跟分级表,中间隔了别的内容就失去关联。"""
    from writing.article_type_spec_cards import build_spec_card_prompt

    block = build_spec_card_prompt("multi_brand_comparison")
    lines = [ln for ln in block.splitlines() if ln.strip()]
    level_idx = max(i for i, ln in enumerate(lines) if ln.startswith("- **"))
    rule_idx = next(i for i, ln in enumerate(lines) if "`## `" in ln)
    assert rule_idx == level_idx + 1, (
        f"格式要求不在分级表正下方(分级表末行 {level_idx},格式要求 {rule_idx})"
    )


def test_validator_catches_a_card_that_forgets_the_format_rule():
    """元判据的反向对照:把格式要求摘掉,`validate_article_spec_cards()` 必须转红。

    没有这条,上面那些锁只证明"今天写了这句话",证不了"少了这句话会被发现"。
    """
    import writing.article_type_spec_cards as cards

    assert cards.validate_article_spec_cards() == []
    original = cards.HEADING_MARKDOWN_RULE
    try:
        cards.HEADING_MARKDOWN_RULE = "小标题要写清楚。"  # 一句不含 `## ` 的空话
        errors = cards.validate_article_spec_cards()
        assert any(e.startswith("heading_feature_without_markdown_format_rule") for e in errors), (
            f"摘掉格式要求后 validate 居然没报错:{errors}"
        )
    finally:
        cards.HEADING_MARKDOWN_RULE = original
    assert cards.validate_article_spec_cards() == []
