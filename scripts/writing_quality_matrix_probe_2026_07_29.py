"""写作质量十维矩阵 · 正向拼 prompt 测绘探针(只读 · 不落库 · 不联网 · 不调 LLM)。

工单 `WRITING_QUALITY_FULL_AUDIT_WORKORDER_2026-07-28.md` §3/§4-B 要求:
12 文体 × 10 维 × 2 条现役生成入口,每格证据必须是**从生成入口正向拼出来的
真实 system_prompt / user_message 片段**,禁止反向 grep 模板文件当证据。

本探针把两条入口的外部依赖(DB / RAG / 证据检索 / LLM / 白标)全部替换成
确定性替身,然后**逐字捕获**喂给 LLM 的 system_prompt 与 user_message,
再按十维逐格判定。替身只影响"外部数据从哪来",不改任何一行 prompt 组装代码。

用法:
    python scripts/writing_quality_matrix_probe_2026_07_29.py            # 打印矩阵
    python scripts/writing_quality_matrix_probe_2026_07_29.py --json out.json
    python scripts/writing_quality_matrix_probe_2026_07_29.py --dump-prompt buying_guide
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# 固定测绘输入(与生产同构,但全部是可复现的假数据)
# ---------------------------------------------------------------------------
PROBE_BRAND = "QZQZ木作美学定制"
PROBE_INDUSTRY = "全屋定制"
PROBE_BRAND_ID = 662
PROBE_QUOTE_ID = 99001
PROBE_KEYWORD = "南山区全屋定制"
PROBE_TITLE = "南山区全屋定制公司靠谱吗？证据、适用边界与核验方法"

PROBE_KB_SENTENCE = "QZQZ木作美学定制自建板材前处理线，交付周期公示为 35 个工作日。"
PROBE_DISTILLED = {
    "client_profile": json.dumps(
        {
            "brand_name": PROBE_BRAND,
            "company_name": PROBE_BRAND,
            "industry": PROBE_INDUSTRY,
            "positioning": "深圳南山区中高端全屋定制",
        },
        ensure_ascii=False,
    ),
    "selling_points": json.dumps(
        {"core": ["自建板材前处理线", "35 个工作日交付公示"]}, ensure_ascii=False
    ),
    "competitor_analysis": json.dumps({"competitors": ["甲方木作", "乙方定制"]}, ensure_ascii=False),
    "social_media_data": {"douyin_top_videos": [], "xiaohongshu_top_notes": []},
    "authoritative_sources": "《全屋定制通用技术条件》",
    "case_examples": "南山某住宅 180㎡ 全案",
    "_distilled_probe_marker": "DISTILLED_PROBE_MARKER_7B41",
}

PROBE_COMPETITORS = [
    {"name": f"竞品公司{i}", "name_verified": True} for i in range(1, 8)
]


def _evidence_item(index: int, verified: bool) -> dict[str, Any]:
    item = {
        "evidence_id": f"EV-{index:03d}",
        "claim": f"公开记录显示候选 {index} 具备相应资质。",
        "url": f"https://example-gov-{index}.gov.cn/record/{index}",
        "title": f"公开监管记录 {index}",
        "publisher": f"公开信源{index % 5}",
        "relationship": "support",
        "source_tier": "official",
        "excerpt": "公开监管记录节选。",
    }
    if verified:
        item["verification_status"] = "official_record"
        item["official_record_id"] = f"REG-{index:05d}"
    else:
        item["verification_status"] = "search_result_only"
    return item


def build_probe_evidence_pack(rich: bool = True) -> dict[str, Any]:
    from writing.evidence_pack import normalize_evidence_pack

    count = 10 if rich else 1
    items = [_evidence_item(i, verified=rich) for i in range(1, count + 1)]
    return normalize_evidence_pack(
        {"items": items, "research_status": "ready", "queries": ["probe"]},
        request_id="probe-request",
    )


# ---------------------------------------------------------------------------
# 外部依赖替身
# ---------------------------------------------------------------------------
class _Cursor:
    """按 SQL 文本分派的确定性游标替身。只覆盖两条入口真实发出的语句。"""

    def __init__(self, state: dict[str, Any]):
        self._state = state
        self._rows: list[dict[str, Any]] = []
        self._row: dict[str, Any] | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        text = " ".join(str(sql).split())
        self._rows = []
        self._row = None
        if "q.distilled_data" in text:
            self._row = {
                "distilled_data": None,
                "brand_name": PROBE_BRAND,
                "industry": PROBE_INDUSTRY,
                "brand_id": PROBE_BRAND_ID,
                "raw_data_json": json.dumps(
                    {"brand_name": PROBE_BRAND, "industry": PROBE_INDUSTRY},
                    ensure_ascii=False,
                ),
            }
        elif "competitor_list" in text:
            self._row = {
                "competitor_list": json.dumps(PROBE_COMPETITORS, ensure_ascii=False),
                "competitor_mode": self._state.get("competitor_mode", "real"),
                "brand_id": PROBE_BRAND_ID,
            }
        elif "SELECT brand_id FROM quotes" in text:
            self._row = {"brand_id": PROBE_BRAND_ID}
        elif "optimized_title" in text:
            self._rows = []
        elif "confirmed_keywords" in text:
            self._row = {"intent": "commercial", "funnel_stage": "consideration"}
        else:
            self._row = None

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows

    def close(self):
        return None


class _Connection:
    def __init__(self, state: dict[str, Any]):
        self._state = state

    def cursor(self):
        return _Cursor(self._state)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class _Distiller:
    def __init__(self, *args, **kwargs):
        pass

    async def run(self):
        return dict(PROBE_DISTILLED)


class _Rag:
    async def retrieve(self, *args, **kwargs):
        return [{"source": "客户手册", "content": PROBE_KB_SENTENCE}]


class _Captured:
    def __init__(self):
        self.system_prompt = ""
        self.user_message = ""


def install_stubs(state: dict[str, Any], captured: _Captured) -> None:
    """把两条入口的外部依赖换成替身。只换数据源,不换任何组装逻辑。"""
    import contextlib

    import openai

    import db.connection as db_connection
    import db.diagnosis_db as diagnosis_db
    import db.profile_db as profile_db
    import services.contact_placeholder as contact_placeholder
    import services.public_whitelabel as whitelabel
    import tools.llm_call_tracker as llm_tracker
    import tools.unified_knowledge as unified_knowledge
    import writing.distiller as distiller_module
    import writing.evidence_research as evidence_research

    diagnosis_db.get_connection = lambda *a, **k: _Connection(state)
    diagnosis_db.get_writing_materials_by_brand = lambda brand_id: {
        "_material_source": "confirmed",
        "_confirmed_at": "2026-07-01",
        "company_intro": f"{PROBE_BRAND}成立于 2015 年。",
        "core_selling_points": "自建板材前处理线",
    }
    db_connection.get_connection = lambda *a, **k: _Connection(state)
    distiller_module.DistillerPipeline = _Distiller
    unified_knowledge.get_unified_rag = lambda: _Rag()
    whitelabel.resolve_branding_context = lambda **kwargs: {"source": "platform_default"}
    profile_db.get_effective_brief_by_brand = lambda brand_id: {
        "my_differentiation": "自建板材前处理线",
    }
    profile_db.get_structured_knowledge_by_brand = lambda brand_id: {
        "products": {"name": "全屋定制整装", "features": ["自建前处理线"]},
    }
    contact_placeholder._get_contact = lambda brand_id: {}

    async def _fake_collect(**kwargs):
        return state["evidence_pack"]

    evidence_research.collect_evidence_pack = _fake_collect
    llm_tracker.llm_tracking_context = lambda **kwargs: contextlib.nullcontext()
    llm_tracker._write_log_row = lambda **kwargs: None

    class _Response:
        usage = None

        def __init__(self):
            self.choices = [
                type(
                    "Choice",
                    (),
                    {"message": type("Msg", (), {"content": "# 探针正文\n\n仅用于捕获 prompt。"})()},
                )()
            ]

    class _Completions:
        async def create(self, **kwargs):
            messages = kwargs.get("messages") or []
            for message in messages:
                if message.get("role") == "system":
                    captured.system_prompt = message.get("content") or ""
                elif message.get("role") == "user":
                    captured.user_message = message.get("content") or ""
            return _Response()

    class _AsyncOpenAI:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": _Completions()})()

    openai.AsyncOpenAI = _AsyncOpenAI


# ---------------------------------------------------------------------------
# 两条入口的正向拼装
# ---------------------------------------------------------------------------
async def probe_service_entry(style_code: str, state: dict[str, Any]) -> dict[str, Any]:
    from writing.article_generator_service import ArticleGeneratorService

    captured = _Captured()
    install_stubs(state, captured)

    service = ArticleGeneratorService(PROBE_QUOTE_ID, PROBE_BRAND, PROBE_INDUSTRY)
    service.add_images = True
    service.add_contact = state.get("add_contact", False)
    topic = {
        "id": None,
        "title": PROBE_TITLE,
        "keyword": PROBE_KEYWORD,
        "style_code": style_code,
        "_trust_legacy_style": True,
        "_evidence_pack": state["evidence_pack"],
    }
    sim = {"dynamic_scores": {}, "case_industry": PROBE_INDUSTRY}
    result = await service._generate_single(
        topic, "https://probe/v1/chat/completions", "k", "m", sim
    )
    return {
        "entry": "ArticleGeneratorService",
        "requested_style": style_code,
        "resolved_style": result.get("style") or style_code,
        "system_prompt": sim.get("_captured_system_prompt") or captured.system_prompt,
        "user_message": sim.get("_captured_user_message") or captured.user_message,
        "length_plan": topic.get("_length_plan") or {},
    }


async def probe_writer_entry(style_code: str, state: dict[str, Any]) -> dict[str, Any]:
    from writing.article_writer import ArticleWriter

    captured = _Captured()
    install_stubs(state, captured)

    async def _capture_llm(self, system_prompt: str, user_message: str) -> str:
        captured.system_prompt = system_prompt
        captured.user_message = user_message
        return "# 探针正文\n\n仅用于捕获 prompt。"

    original = ArticleWriter._call_llm
    ArticleWriter._call_llm = _capture_llm
    try:
        writer = ArticleWriter(dict(PROBE_DISTILLED), brand_id=PROBE_BRAND_ID)
        topic = {
            "id": 1,
            "quote_id": PROBE_QUOTE_ID,
            "title": PROBE_TITLE,
            "keywords": [PROBE_KEYWORD],
            "style_code": style_code,
            "platform": "GEO媒体",
            "_evidence_pack": state["evidence_pack"],
        }
        await writer.write(topic)
        resolved = topic.get("style_code") or style_code
        length_plan = topic.get("_length_plan") or {}
    finally:
        ArticleWriter._call_llm = original
    return {
        "entry": "ArticleWriter",
        "requested_style": style_code,
        "resolved_style": resolved,
        "system_prompt": captured.system_prompt,
        "user_message": captured.user_message,
        "length_plan": length_plan,
    }


# ---------------------------------------------------------------------------
# 十维判定
# ---------------------------------------------------------------------------
def _cell(status: str, evidence: str) -> dict[str, str]:
    return {"status": status, "evidence": evidence}


def _clip(text: str, limit: int = 160) -> str:
    flat = " ".join(str(text).split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def evaluate_dimensions(probe: dict[str, Any], ratio_report: dict[str, Any]) -> dict[str, Any]:
    from writing.article_style_contract import (
        DISABLED_NEW_GENERATION_STYLES,
        RANKING_REVIVAL_CONTRACT_VERSION,
        family_for_style,
    )
    from writing.article_length_contract import build_article_length_plan
    from writing.templates.canonical_family_templates import prompt_for_style

    style = probe["requested_style"]
    resolved = probe["resolved_style"]
    system_prompt = probe["system_prompt"] or ""
    user_message = probe["user_message"] or ""
    whole = system_prompt + "\n" + user_message
    family = family_for_style(resolved)
    cells: dict[str, dict[str, str]] = {}

    # ① 家族归属
    fam_requested = family_for_style(style)
    cells["1_family"] = _cell(
        "通" if fam_requested else "不通",
        f"family_for_style({style!r}) -> {fam_requested!r}",
    )

    # ② 配比真进选题
    cells["2_ratio"] = _cell(
        ratio_report["status"],
        ratio_report["evidence"],
    )

    # ③ 标题公式路由(全局链路,逐格记录该文体是否被公式库覆盖)
    cells["3_title_formula"] = _cell(
        ratio_report["title_status"],
        ratio_report["title_evidence"],
    )

    # ④ 长度注入
    plan = probe.get("length_plan") or {}
    marker = "【篇幅合同"
    if style in DISABLED_NEW_GENERATION_STYLES:
        cells["4_length"] = _cell("不适用", "退役文体不做新生成,长度合同随安全路由的目标文体注入")
    elif marker in whole and plan:
        expect = f"建议有效正文约 {plan.get('minimum_chars')}-{plan.get('maximum_chars')} 字，目标约 {plan.get('target_chars')} 字"
        hit = expect in whole
        cells["4_length"] = _cell(
            "通" if hit else "不通",
            f"plan={plan.get('family_code')} {plan.get('minimum_chars')}/{plan.get('target_chars')}/"
            f"{plan.get('maximum_chars')} · prompt 内含该区间={hit}",
        )
    else:
        cells["4_length"] = _cell("不通", f"prompt 内未找到 {marker}")

    # ⑤ 客户品牌注入
    brand_hit = PROBE_BRAND in whole
    placeholder_hit = "「客户品牌」" in whole
    if placeholder_hit:
        cells["5_client"] = _cell(
            "不通",
            "client_presence 合同里出现字面占位「客户品牌」——真实品牌名没传进 build_client_presence_prompt",
        )
    elif brand_hit:
        count = whole.count(PROBE_BRAND)
        cells["5_client"] = _cell("通", f"prompt 内出现真实品牌 {count} 次,无字面占位")
    else:
        cells["5_client"] = _cell("不通", "prompt 内既无真实品牌名也无占位")

    # ⑥ 模板路由指向活模板(正向拼 prompt 判定)
    if family:
        canonical = prompt_for_style(resolved)
        family_marker = _family_signature(canonical)
        hit = bool(family_marker) and family_marker in system_prompt
        cells["6_template"] = _cell(
            "通" if hit else "不通",
            f"family={family} · 家族指令特征串 {family_marker[:24]!r} 在 system_prompt 中={hit}",
        )
    else:
        cells["6_template"] = _cell("不通", "family 未知,模板路由无法判定")

    # ⑦ 飞轮蒸馏结构被遵循
    distill_marker = PROBE_DISTILLED["_distilled_probe_marker"]
    profile_hit = "自建板材前处理线" in whole
    cells["7_distilled"] = _cell(
        "通" if profile_hit else "不通",
        f"蒸馏 client_profile/selling_points 特征串进 prompt={profile_hit} · "
        f"内部 marker 泄漏={distill_marker in whole}",
    )

    # ⑧ GEO 规则符合
    geo_bits = {
        "evidence_first": "引用必须真实存在" in system_prompt,
        "source_disclosure": "来源" in system_prompt,
        "ranking_revival": RANKING_REVIVAL_CONTRACT_VERSION in system_prompt,
        "no_self_scoring": "自创评分" in whole or "综合分" in whole,
    }
    need_revival = family == "multi_brand_comparison"
    ok = geo_bits["evidence_first"] and geo_bits["no_self_scoring"] and (
        geo_bits["ranking_revival"] if need_revival else True
    )
    cells["8_geo_rules"] = _cell(
        "通" if ok else "不通",
        " · ".join(f"{k}={v}" for k, v in geo_bits.items()) + f" · 需榜单复活合同={need_revival}",
    )

    # ⑨ 图片可进文章 —— 三态。生成端必须**显式**表态:要么给占位规则(有选图链),
    # 要么给关闭合同(无选图链)。"什么都不说"才是不通(模型可能吐编造 URL 的
    # Markdown 图片,而 strip 只认 [NEED_IMAGE]/[CLIENT_IMAGE])。
    image_rule = "[NEED_IMAGE role=X purpose=Y]" in whole
    image_opt_out = "【配图授权:未开启】" in whole
    if image_rule:
        cells["9_image"] = _cell("通", "prompt 内含 [NEED_IMAGE] 占位规则 → 两步占位符链可启动")
    elif image_opt_out:
        cells["9_image"] = _cell(
            "不适用",
            "本入口无 _save_article 选图链,prompt 显式关闭配图(fail-closed,不留死占位/不许编 URL)",
        )
    else:
        cells["9_image"] = _cell("不通", "prompt 对图片只字不提:既不能配图,也拦不住编造的 Markdown 图片")

    # ⑩ 联系方式 + 知识库
    contact_on = "[NEED_CONTACT]" in whole
    contact_opt_out = ("联系方式授权：未开启" in whole) or ("未取得插入联系方式授权" in whole)
    kb_hit = PROBE_KB_SENTENCE[:20] in whole
    contact_ok = contact_on or contact_opt_out
    cells["10_contact_kb"] = _cell(
        "通" if (contact_ok and kb_hit) else "不通",
        f"联系方式开关显式进 prompt={contact_ok}(开启={contact_on}/关闭={contact_opt_out}) · "
        f"知识库检索内容进 prompt={kb_hit}",
    )

    return cells


_FAMILY_SIGNATURES = {
    "evidence_qa": "围绕一个明确问题组织答案",
    "multi_brand_comparison": "先写入选范围和排除条件",
    "implementation_guide": "把读者从目标带到可验收结果",
    "trend_policy_risk": "把变化讲清楚",
    "case_data_roi": "用可验证的案例",
    "company_facts": "说明企业能力与适配场景",
}


def _family_signature(canonical_prompt: str) -> str:
    for signature in _FAMILY_SIGNATURES.values():
        if signature in canonical_prompt:
            return signature
    # 回落:取家族指令块首句
    for line in canonical_prompt.splitlines():
        stripped = line.strip()
        if len(stripped) > 12 and not stripped.startswith(("你是", "【", "-", "1.", "严禁")):
            return stripped[:24]
    return ""


def build_ratio_report() -> dict[str, str]:
    from config.settings_manager import get_effective_style_ratios
    from writing.style_registry import WRITING_STYLES
    from writing.article_style_contract import family_for_style

    ssot = get_effective_style_ratios(PROBE_INDUSTRY, unit="percent")
    ssot_sum = sum(int(v or 0) for v in ssot.values())
    memory_sum = sum(int(s.get("ratio") or 0) for s in WRITING_STYLES.values())
    family_totals: dict[str, int] = {}
    for code, value in ssot.items():
        fam = family_for_style(code) or "unmapped"
        family_totals[fam] = family_totals.get(fam, 0) + int(value or 0)
    status = "通" if ssot_sum == 100 else "不通"

    from writing.title_formula_library import TITLE_FORMULAS  # noqa: F401
    from writing import keyword_topic_generator

    title_prompt = keyword_topic_generator._build_title_generator_prompt(PROBE_INDUSTRY)
    title_ok = "标题公式库" in title_prompt
    return {
        "status": status,
        "evidence": (
            f"SSOT get_effective_style_ratios 合计={ssot_sum} · 六家族折算={family_totals} · "
            f"内存 WRITING_STYLES 合计={memory_sum}"
        ),
        "title_status": "通" if title_ok else "不通",
        "title_evidence": f"_build_title_generator_prompt 内含标题公式库={title_ok}",
        "ssot_sum": str(ssot_sum),
        "memory_sum": str(memory_sum),
    }


# ---------------------------------------------------------------------------
async def run(styles: list[str], dump_prompt: str | None) -> dict[str, Any]:
    state = {
        "evidence_pack": build_probe_evidence_pack(rich=True),
        "competitor_mode": "real",
        "add_contact": False,
    }
    ratio_report = build_ratio_report()
    matrix: dict[str, Any] = {"ratio_report": ratio_report, "rows": []}

    for style in styles:
        row: dict[str, Any] = {"style_code": style, "entries": {}}
        for probe_fn in (probe_service_entry, probe_writer_entry):
            try:
                probe = await probe_fn(style, state)
            except Exception as exc:  # noqa: BLE001 - 测绘要如实记录失败
                row["entries"][probe_fn.__name__] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            cells = evaluate_dimensions(probe, ratio_report)
            row["entries"][probe["entry"]] = {
                "resolved_style": probe["resolved_style"],
                "system_prompt_chars": len(probe["system_prompt"]),
                "user_message_chars": len(probe["user_message"]),
                "cells": cells,
            }
            if dump_prompt and style == dump_prompt:
                print("=" * 78)
                print(f"[{probe['entry']}] style={style} resolved={probe['resolved_style']}")
                print("-" * 78)
                print("SYSTEM_PROMPT:\n" + probe["system_prompt"])
                print("-" * 78)
                print("USER_MESSAGE:\n" + probe["user_message"])
                print("=" * 78)
        matrix["rows"].append(row)
    return matrix


def print_matrix(matrix: dict[str, Any]) -> None:
    dims = [
        "1_family", "2_ratio", "3_title_formula", "4_length", "5_client",
        "6_template", "7_distilled", "8_geo_rules", "9_image", "10_contact_kb",
    ]
    print(f"\n配比 SSOT: {matrix['ratio_report']['evidence']}\n")
    for row in matrix["rows"]:
        print(f"### {row['style_code']}")
        for entry, payload in row["entries"].items():
            if "error" in payload:
                print(f"  [{entry}] ERROR {payload['error']}")
                continue
            marks = " ".join(
                f"{d.split('_')[0]}:{payload['cells'][d]['status']}" for d in dims
            )
            print(f"  [{entry}] resolved={payload['resolved_style']} {marks}")
            for d in dims:
                cell = payload["cells"][d]
                if cell["status"] != "通":
                    print(f"      {d} = {cell['status']} · {cell['evidence']}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--dump-prompt", dest="dump_prompt")
    parser.add_argument("--styles", dest="styles")
    args = parser.parse_args()

    from writing.style_registry import WRITING_STYLES

    styles = args.styles.split(",") if args.styles else list(WRITING_STYLES.keys())
    matrix = asyncio.run(run(styles, args.dump_prompt))
    print_matrix(matrix)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"→ JSON 已写出: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
