# -*- coding: utf-8 -*-
"""标题 AI-only · 判据七组(工单 `WO_TITLE_AI_ONLY_NO_HARDCODED_FALLBACK_2026-08-17.md` §3)。

Owner 裁决(2026-08-17):
  「拒绝任何一切兜底行为,必须根据用户的主题来进行标题的创作,AI 必须介入,
   否则就生成失败都行,或者用其他标题来进行兜底,但是都一定是 AI 生成的,不是硬编码。」

每一组都**成对**:一条正向必须命中,一条反向必须不命中 —— 单向断言在本仓
反复被证明会变成恒真(「全阴性先怀疑尺子」)。
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.fixtures.retired_title_templates_2026_08_17 import (  # noqa: E402
    ALL_RETIRED_TEMPLATES,
    CANONICAL_RETIRED_TEMPLATE,
    scan_titles_for_template_signatures,
    template_signature_hits,
)
from writing.keyword_topic_generator import KeywordTopicGenerator  # noqa: E402
from writing.title_ai_only import (  # noqa: E402
    TITLE_FAILURE_USER_MESSAGE,
    TITLE_ORIGIN_BATCH_SURPLUS,
    TITLE_ORIGIN_RETRY,
    TitleSlotRequest,
    resolve_titles_ai_only,
)
from writing.title_intent_style_gate import (  # noqa: E402
    allowed_families_for,
    classify_purchase_intent,
    reassign_family,
)

KEYWORDS = [
    {"id": 1, "keyword": "深圳哪家装修公司靠谱", "required_articles": 3},
    {"id": 2, "keyword": "全屋定制是什么意思", "required_articles": 2},
]


def _generator(keywords=None, **kwargs):
    return KeywordTopicGenerator(
        keywords=keywords or KEYWORDS,
        brand_name="测试品牌",
        industry="家装",
        **kwargs,
    )


def _titles(topics):
    return [str(t.get("optimized_title") or "") for t in topics if isinstance(t, dict)]


# ===========================================================================
# §3.1 拔 AI 变异(核心)· 成对:注入一条模板,扫描器必须抓到
# ===========================================================================

def test_g1_llm_dead_yields_zero_template_signature__must_hit(monkeypatch):
    """LLM 全灭 → 产出要么显式失败、要么来自 AI;**零模板签名**。"""
    # 主模型 / 兜底模型全部拔掉:`get_llm_config` 返回空 key。
    monkeypatch.setattr(
        "writing.llm_utils.get_llm_config",
        lambda *a, **k: ("", "", "", "none"), raising=False,
    )
    monkeypatch.setattr(
        "writing.llm_utils.get_fallback_llm_config",
        lambda *a, **k: ("", "", "", "none"), raising=False,
    )

    generator = _generator()
    topics = asyncio.run(generator.generate())

    produced = _titles(topics)
    hits = scan_titles_for_template_signatures(produced)
    assert not hits, f"AI 全灭却产出了模板串:{hits}"
    # 显式失败必须记账,不许静默返回空。
    report = generator.title_failure_report or {}
    assert report.get("failed", 0) >= 1, f"AI 全灭却没有失败记账:{report}"
    assert report.get("user_message") == TITLE_FAILURE_USER_MESSAGE
    # 一条标题都没有 = 全部显式失败,而不是"用模板凑了 5 条"。
    assert produced == [], f"AI 全灭却仍有标题产出:{produced}"


def test_g1_scanner_catches_injected_template__must_hit():
    """判别力对照:人工注入一条真模板,扫描器必须抓到(防恒真)。"""
    injected = CANONICAL_RETIRED_TEMPLATE.replace("{kw}", "深圳装修公司")
    hits = scan_titles_for_template_signatures(["一个正常的 AI 标题", injected])
    assert injected in hits, "扫描器抓不到注入的模板串 → §3.1 的'零命中'是恒真的假绿"
    assert len(ALL_RETIRED_TEMPLATES) >= 40, "签名源为空/过小 = 判据恒真"


def test_g1_scanner_does_not_flag_clean_ai_title__must_not_hit():
    """反向:正常 AI 标题不该被误判(否则扫描器是恒红,同样没判别力)。

    🔴 这几条是 2026-08-17 真 LLM 一发的**实际产出**。第一版签名源用片段匹配,
    其中「深圳装修公司怎么选？2026年看资质、报价与工地实况这3点」被误判成模板复现
    —— 真数据当场证伪了我的判据。改成整条模板匹配后这几条必须全部干净。
    """
    real_ai_titles = (
        "深圳装修公司哪家靠谱？2026年按半包与全包分场景对比",
        "深圳装修公司怎么选？2026年看资质、报价与工地实况这3点",
        "深圳装修公司怎么考察？从测试品牌的服务边界到资质核实路径",
        "全屋定制是什么意思？2026年与成品家具、木工打柜子的区别",
        "深圳装修公司怎么挑？先看这三份可查的资料",
    )
    for title in real_ai_titles:
        assert template_signature_hits(title) == [], (
            f"正常 AI 标题被误判成模板:{title} → {template_signature_hits(title)}"
        )


# ===========================================================================
# §3.2 意图过滤成对
# ===========================================================================

def test_g2_vendor_intent_excludes_knowledge_and_trend_families__must_hit():
    """选服务商意图 → 科普(证据型问答)/趋势族**零产出**。"""
    keyword = "深圳哪家装修公司靠谱"
    assert classify_purchase_intent(keyword) == "vendor_selection"
    allowed = allowed_families_for(keyword)
    assert "趋势、政策与风险分析" not in allowed
    assert "证据型问答" not in allowed
    # 排到这两族的槽位必须被改判走。
    for banned in ("趋势、政策与风险分析", "证据型问答"):
        assert reassign_family(keyword, banned, slot_index=0) != banned


def test_g2_knowledge_intent_keeps_knowledge_family__must_not_hit():
    """成对反向:真科普型关键词,该族仍正常产出(防'能拒只是什么都拒')。"""
    keyword = "全屋定制是什么意思"
    assert classify_purchase_intent(keyword) == "definition"
    assert "证据型问答" in allowed_families_for(keyword)
    assert reassign_family(keyword, "证据型问答", slot_index=0) == "证据型问答"
    # 趋势型关键词同理保留趋势族。
    assert "趋势、政策与风险分析" in allowed_families_for("家装行业政策有什么变化")


def test_g2_intent_floor_applies_to_model_output__must_hit():
    """地板:模型没听 prompt 时,族标签就地改判(只改标签不改标题文本)。"""
    from writing.title_intent_style_gate import enforce_topic_intent_fit

    topics = [{
        "original_keyword": "深圳哪家装修公司靠谱",
        "article_style": "趋势、政策与风险分析",
        "optimized_title": "深圳装修公司怎么挑？三份可查资料先看",
        "slot_index": 0,
    }]
    before_title = topics[0]["optimized_title"]
    enforce_topic_intent_fit(topics)
    assert topics[0]["article_style"] != "趋势、政策与风险分析"
    assert topics[0]["optimized_title"] == before_title, "地板不许改标题文本(改就得造串)"


# ===========================================================================
# §3.3 去重冲突路径:重生成走 AI,零模板串
# ===========================================================================

def test_g3_dedupe_conflict_uses_ai_candidate__must_hit():
    from writing.title_batch_dedupe import dedupe_topic_titles

    topics = [
        {"optimized_title": "深圳装修公司怎么挑？先看这三份资料",
         "original_keyword": "深圳装修公司", "article_style": "选购与多品牌比较",
         "slot_index": 0},
        {"optimized_title": "深圳装修公司怎么挑？先看这三份资料",
         "original_keyword": "深圳装修公司", "article_style": "选购与多品牌比较",
         "slot_index": 1},
    ]
    report = dedupe_topic_titles(
        topics,
        candidates_by_index={1: ["深圳装修公司报价怎么比？逐项对齐口径再谈价"]},
    )
    assert report["regenerated"] == 1, report
    assert topics[1]["optimized_title"] == "深圳装修公司报价怎么比？逐项对齐口径再谈价"
    assert not scan_titles_for_template_signatures(_titles(topics))


def test_g3_dedupe_without_ai_candidate_is_unresolved_not_template__must_not_hit():
    """成对反向:拿不出 AI 候选时**保留原标题并记 unresolved**,绝不换模板串。"""
    from writing.title_batch_dedupe import dedupe_topic_titles

    dup = "深圳装修公司怎么挑？先看这三份资料"
    topics = [
        {"optimized_title": dup, "original_keyword": "深圳装修公司",
         "article_style": "选购与多品牌比较", "slot_index": 0},
        {"optimized_title": dup, "original_keyword": "深圳装修公司",
         "article_style": "选购与多品牌比较", "slot_index": 1},
    ]
    report = dedupe_topic_titles(topics)  # 没有任何 AI 候选
    assert report["regenerated"] == 0, report
    assert report["unresolved"] == 1, report
    assert topics[1]["optimized_title"] == dup, "没有 AI 候选却改了标题 = 一定是模板"
    assert not scan_titles_for_template_signatures(_titles(topics))


# ===========================================================================
# §3.4 census 全量分母自证
# ===========================================================================

_CENSUS_SKIP_DIRS = (
    "tests", "scripts", "docs", "agent-test-artifacts", "agent-test-artifacts-round3",
    "node_modules", "frontend", ".git", "_archive", "prompt_archive",
)

# 命中 `{kw}` 结构锚但**不是标题产出面**的地方,逐条登记理由。
# 新增一处不登记 → 本文件转红,逼着新增者先判"它产不产标题"。
CENSUS_NON_TITLE_SITES = {
    "api/agent_api.py": "f-string 里的中文文案片段,`{kw}` 是循环变量不是模板槽",
    "data/analyze_responses.py": "离线分析脚本的打印串",
    "db/diagnosis_db.py": "SQL LIKE 通配拼接",
    "server.py": "日志/接口 message 文案(含标题**失败**提示),不是标题本身",
    "services/article_fact_check.py": "质检提示语",
    "services/placement_service.py": "SQL LIKE 通配 + 日志",
    "services/geo_douyin/card_templates.py": "图文包卡片模板(另一窗口在制 · 工单 §4 禁区)",
    "services/geo_douyin/production_task.py": "图文包(工单 §4 禁区)",
    "services/geo_douyin/title_engine.py": "图文包短视频标题池(工单 §4 禁区)",
    "services/geo_title_hygiene.py": "地名去重共用件自己的 docstring 示例",
    "writing/title_intent_style_gate.py": "意图闸 docstring,不产标题",
    "writing/title_ai_only.py": "AI-only 契约模块 docstring,不产标题",
    # 以下九处的 `{kw}` 全是**循环变量**(`for kw in keywords`)落进 f-string,
    # 不是模板槽 —— 它们产的是日志行 / 送进模型的关键词清单,不产标题。
    "services/quote_intent_gate.py": "报价意图闸的日志行,`{kw}` 是循环变量",
    "tools/cluster_gatekeeper.py": "喂给模型的关键词清单,不是标题",
    "tools/competition_analyzer.py": "竞品分析 prompt 的关键词分节标记",
    "tools/keyword/keyword_tier.py": "词级分档的打印行",
    "tools/keyword_classifier.py": "关键词分类 prompt 的清单行",
    "tools/keyword_cluster.py": "聚类 prompt 的清单行",
    "tools/keyword_value_scorer.py": "价值评分的打印行与 prompt 清单行",
    "workflows/content_workflow.py": "选题骨架的 `keywords` 列表(派生词),标题已改由 AI 产出",
}

# 本包**已退役**的标题产出面(全仓零 caller · 由下面的接线锁逐条钉死)。
CENSUS_RETIRED_SYMBOLS = {
    "writing/keyword_topic_generator.py": (
        "_fallback_style_title_map", "_fallback_form_completion_map",
        "fallback_templates_for_form", "_safe_fallback_title",
    ),
    "writing/title_batch_dedupe.py": (
        "_stable_seed", "template_is_question", "filter_templates_by_form",
        "pick_diverse_template",
    ),
    "writing/topic_dispatcher.py": (
        "_fallback_topics", "_generate_smart_title", "_detect_keyword_type",
        "_tier1_title", "_tier2_title", "_tier3_title",
        "_style_title_templates",
    ),
}


def _runtime_py_files():
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if any(rel.startswith(f"{d}/") or rel == d for d in _CENSUS_SKIP_DIRS):
            continue
        yield rel, path


def _census_hits():
    hits = set()
    for rel, path in _runtime_py_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "{kw}" in text:
            hits.add(rel)
    return sorted(hits)


def test_g4_census_denominator_is_not_empty__must_hit():
    """分母为空 = 判据恒真。结构锚必须真的命中一批文件。"""
    hits = _census_hits()
    assert len(hits) >= 8, f"`{{kw}}` 结构锚只命中 {len(hits)} 个文件,口径可能坏了:{hits}"


def test_g4_every_census_hit_is_classified__must_hit():
    """每处落进「已退役 / 非标题用途」两类,无第三类。"""
    unknown = sorted(set(_census_hits()) - set(CENSUS_NON_TITLE_SITES))
    assert not unknown, (
        "下列文件含 `{kw}` 结构锚,但既没登记为非标题用途、也不在已退役清单里:\n"
        f"{unknown}\n→ 先判它产不产标题:产就退役,不产就登记理由。"
    )


def test_g4_retired_symbols_have_zero_runtime_caller__must_hit():
    """接线锁:退役符号在**整个运行时**零定义、零引用(AST 级)。"""
    offenders = {}
    all_retired = {s for names in CENSUS_RETIRED_SYMBOLS.values() for s in names}
    for rel, path in _runtime_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            name = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.alias):
                name = node.name.rsplit(".", 1)[-1]
            if name in all_retired:
                offenders.setdefault(rel, set()).add(name)
    assert not offenders, f"退役符号仍在运行时被定义/引用:{ {k: sorted(v) for k, v in offenders.items()} }"


def test_g4_wiring_lock_would_catch_a_reintroduced_symbol__must_hit():
    """成对反向:锁不是恒绿 —— 对一个**确实存在**的符号必须报出来。"""
    live = "resolve_titles_ai_only"
    found = set()
    for rel, path in _runtime_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == live:
                found.add(rel)
            elif isinstance(node, ast.Name) and node.id == live:
                found.add(rel)
            elif isinstance(node, ast.alias) and node.name.rsplit(".", 1)[-1] == live:
                found.add(rel)
    assert found, "AST 扫描对一个真实存在的符号也扫不出来 → 上面的'零 caller'是恒真"


# ===========================================================================
# §3.5 显式失败形态
# ===========================================================================

def test_g5_failure_message_is_plain_language_with_an_exit__must_hit():
    assert "重新生成" in TITLE_FAILURE_USER_MESSAGE, "失败提示必须带出口动作"
    jargon = (
        "fallback", "template", "LLM", "prompt", "token", "slot", "null",
        "兜底", "模板", "槽位", "异常", "状态码",
    )
    hit = [w for w in jargon if w.lower() in TITLE_FAILURE_USER_MESSAGE.lower()]
    assert not hit, f"用户面失败文案含工程术语:{hit}"


def test_g5_failure_is_not_silent_and_not_empty_success__must_hit(monkeypatch):
    monkeypatch.setattr(
        "writing.llm_utils.get_llm_config",
        lambda *a, **k: ("", "", "", "none"), raising=False,
    )
    monkeypatch.setattr(
        "writing.llm_utils.get_fallback_llm_config",
        lambda *a, **k: ("", "", "", "none"), raising=False,
    )
    generator = _generator()
    asyncio.run(generator.generate())
    report = generator.title_failure_report or {}
    assert report.get("requested") == 5, report        # 3 + 2 篇
    assert report.get("produced") == 0, report
    assert report.get("failed") == 5, report
    assert report.get("failed_slots"), "失败槽位必须逐条列出,不能只给一个数字"


def test_g5_success_path_reports_zero_failure__must_not_hit():
    """成对反向:AI 正常时不许硬报失败(否则上面那条恒真)。"""
    generator = _generator()
    topics = [{
        "keyword_id": 1, "original_keyword": "深圳哪家装修公司靠谱",
        "optimized_title": "深圳装修公司怎么挑？先看这三份资料",
        "article_style": "选购与多品牌比较", "slot_index": 0,
    }]
    out = asyncio.run(generator._resolve_pending_titles(topics))
    assert len(out) == 1
    assert (generator.title_failure_report or {}).get("failed") == 0


# ===========================================================================
# §1.2 失败梯的三级:① 同批候选 ② AI 重试 ③ 显式失败
# ===========================================================================

def test_ladder_step1_uses_same_keyword_surplus__must_hit():
    """① 同批合格 AI 候选补位 —— 不调 LLM 也能补上。"""
    result = asyncio.run(resolve_titles_ai_only(
        [TitleSlotRequest(key="a", keyword="深圳装修公司", article_style="选购与多品牌比较")],
        surplus_pool={"深圳装修公司": ["深圳装修公司怎么比？三份可查资料对齐口径"]},
    ))
    assert result.titles["a"] == "深圳装修公司怎么比？三份可查资料对齐口径"
    assert result.origins["a"] == TITLE_ORIGIN_BATCH_SURPLUS
    assert result.retry_attempted is False, "① 命中就不该再打 LLM"


def test_ladder_step1_never_crosses_keywords__must_not_hit(monkeypatch):
    """成对反向:别的关键词的候选**不许**跨词补位(身份红线)。

    🔴 必须同时把 ② 重试掐掉。第一版没掐,本机 .env 有真 key → 梯子走到 ②
    真打了一次 LLM 并成功返回,断言当场红 —— 红得对,但红的原因不是跨词补位。
    掐掉重试后,① 拒绝跨词 = 直接落到 ③ 显式失败,这才是本条要测的东西。
    """
    monkeypatch.setattr("writing.title_ai_only._retry_model_attempts", lambda: [])
    result = asyncio.run(resolve_titles_ai_only(
        [TitleSlotRequest(key="a", keyword="深圳装修公司")],
        surplus_pool={"广州搬家公司": ["广州搬家公司怎么挑？先看这三份资料"]},
    ))
    assert "a" not in result.titles, "跨关键词补位 = 把 A 的标题安到 B 头上"
    assert result.failed_keys == ["a"]


def test_ladder_step2_retry_is_ai_and_model_comes_from_config(monkeypatch):
    """② 重试走 model config(不硬编码型号),产出计为 ai_retry。"""
    seen = {}

    async def _fake_call(api_url, api_key, model, prompt, timeout=90.0):
        seen["model"] = model
        return {"a": "深圳装修公司怎么挑？按可查资料逐项对齐"}

    monkeypatch.setattr("writing.title_ai_only._call_one_model", _fake_call)
    monkeypatch.setattr(
        "writing.title_ai_only._retry_model_attempts",
        lambda: [("http://example/api", "k", "model-from-config")],
    )
    result = asyncio.run(resolve_titles_ai_only(
        [TitleSlotRequest(key="a", keyword="深圳装修公司")],
    ))
    assert result.origins["a"] == TITLE_ORIGIN_RETRY
    assert seen["model"] == "model-from-config"
    assert not template_signature_hits(result.titles["a"])


def test_ladder_step2_source_has_no_hardcoded_model_id__must_not_hit():
    """成对反向:重试实现里不许出现写死的型号串。"""
    src = (REPO / "writing" / "title_ai_only.py").read_text(encoding="utf-8")
    for model_id in ("qwen3-max", "deepseek-chat", "deepseek-v3", "kimi", "doubao", "gpt-"):
        assert model_id not in src, f"title_ai_only 里硬编码了型号 {model_id}"


# ===========================================================================
# §3.7 全量:既有写作链判据零回归 + 五保护零 diff
# ===========================================================================

PROTECTED_FILES = (
    "middleware/billing.py",
    "db/wallet_db.py",
    "db/connection.py",
    "auth/middleware.py",
    "auth/jwt_utils.py",
)


def test_g7_protected_files_untouched__must_hit():
    import subprocess

    base = "ecce9985293467bcc6765798609a7b2882e0b8a8"
    out = subprocess.run(
        ["git", "diff", "--name-only", base, "--", *PROTECTED_FILES],
        cwd=REPO, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    # 🔴 [2026-09-05] **收窄,不是放宽**:本轮 `middleware/billing.py` 经授权改动。
    #    Owner 2026-09-05 亲口授权(「billing.py 这笔我批了」)· OWNER_AUTHORIZATIONS_2026-09-05.md sha256 b673b188ff01e377
    #    其余保护文件任一被改 ⇒ 仍然红;billing.py 改回不动 ⇒ **也红**
    #    (授权用完必须显式收回,不留「曾经批过所以永远敞着」的门)。
    _changed = tuple(x for x in out.stdout.split() if x.strip())
    # 🔴 [#106b · 2026-09-06] 授权集从 1 个变 2 个 —— **收窄不是放宽**:
    #    多出 db/wallet_db.py 是因为 Owner 在 C 窗口又批了一笔;
    #    任何**第三个**保护文件被改仍然红,而这两个之一改回不动 **也红**
    #    (授权是一笔一授,用完要显式收回,不是永久解锁)。
    #    顺序按 git 的输出(字典序),不是按批准先后。
    assert _changed == ("db/wallet_db.py", "middleware/billing.py"), (
        f"保护文件改动集 {_changed} != 已授权集 · Owner 2026-09-05 亲口授权(「billing.py 这笔我批了」)· OWNER_AUTHORIZATIONS_2026-09-05.md sha256 b673b188ff01e377")


def test_g7_year_contract_and_jargon_blacklist_untouched__must_hit():
    """禁区:标题年份合同语义与术语黑名单一个字节都不动。"""
    import subprocess

    base = "ecce9985293467bcc6765798609a7b2882e0b8a8"
    for path in ("writing/title_element_contract.py", "writing/title_jargon_blacklist.py"):
        out = subprocess.run(
            ["git", "diff", "--name-only", base, "--", path],
            cwd=REPO, capture_output=True, text=True,
        )
        assert out.stdout.strip() == "", f"{path} 被改动(工单 §4 禁区)"


def test_g7_geo_douyin_untouched__must_hit():
    import subprocess

    base = "ecce9985293467bcc6765798609a7b2882e0b8a8"
    out = subprocess.run(
        ["git", "diff", "--name-only", base, "--", "services/geo_douyin"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert out.stdout.strip() == "", f"图文包目录被改动(另一窗口在制):{out.stdout}"


def test_g7_diff_detector_is_not_always_empty__must_hit():
    """成对反向:diff 探测器对**确实改过**的文件必须报出来(否则上面三条恒真)。"""
    import subprocess

    base = "ecce9985293467bcc6765798609a7b2882e0b8a8"
    out = subprocess.run(
        ["git", "diff", "--name-only", base, "--", "writing/keyword_topic_generator.py"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert out.stdout.strip(), "diff 探测器对改过的文件也返回空 → 五保护零 diff 是恒真"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
