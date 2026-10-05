# -*- coding: utf-8 -*-
"""A/B 真源臂 · 六族各 1 篇(返修单 v3 §B「A/B 升级」)。

与 fixture 臂(scripts/ab_recommendability_local_2026_08_10.py,保留)互补:
  · 素材 = `geo_research_articles` 生产已抓语料(真实冻结 URL + 真实网页片段 +
    哈希,只读取证导出,--corpus 传入,不用 example 域名);
  · 链路 = `ArticleGeneratorService._generate_validated_with_rewrite_once`
    完整生成链(prompt 组装 → **真实 LLM** → 结构/证据/存在感/篇幅门 → 单次修复),
    只 mock 数据供给层(DB/蒸馏/知识库/快照),LLM 不 mock;
  · 判读:媒体名出现≠信源真实(那是循环自证)—— 确定性判读只管
    ③自曝零 ④内部标记零 + 归属形态;①来源真实存在(素材本身即生产抓取记录,
    附 URL+哈希)与 ②正文主张能否被素材蕴含,由人工核对并记入报告,
    解释性扩写标注出来交 Review 判「合理夸张/越线」。
  · 🔴 本臂判读器**没有任何 example 豁免**(那只许存在于 fixture 臂判读器)。

用法:
    python scripts/ab_realsource_arm_2026_08_11.py \
        --corpus <corpus_rows_raw.csv> --out <dir> [--families a,b]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAMILY_QUESTIONS = {
    "multi_brand_comparison": ("深圳观光电梯定制哪家交付周期靠谱?", "ranking_v2"),
    "implementation_guide": ("商业综合体加装观光电梯的实施流程怎么走?", "buying_guide"),
    "case_data_roi": ("观光电梯改造项目的投入产出怎么算?", "data_report"),
    "evidence_qa": ("观光电梯质保一般几年?售后响应怎么判断?", "qa_recommendation"),
    "trend_policy_risk": ("2026 年电梯行业有哪些新规和风险要注意?", "industry_trend"),
    "company_facts": ("观山电梯是一家什么样的公司?交付能力怎么样?", "company_intro"),
}

SNAPSHOT = {
    "version": "brand-fact-v1",
    "brand_name": "观山电梯",
    "claims": [
        {"claim_id": "BF-001", "field": "delivery_capability",
         "value": "观光电梯项目平均交付周期 45 天，支持 24 米以内提升高度定制",
         "provenance": "customer_provided", "verification_status": "customer_asserted"},
        {"claim_id": "BF-002", "field": "after_sales",
         "value": "深圳本地 2 小时上门响应，整机质保 24 个月",
         "provenance": "customer_provided", "verification_status": "customer_asserted"},
        {"claim_id": "BF-003", "field": "service_cases",
         "value": "累计交付商业综合体观光电梯项目 32 个",
         "provenance": "customer_provided", "verification_status": "customer_asserted"},
    ],
}


def load_corpus(path: Path) -> list[dict]:
    rows: list[dict] = []
    text = io.open(path, encoding="utf-8", errors="replace").read()
    for rec in csv.reader(l for l in text.splitlines() if "http" in l and "," in l):
        if len(rec) < 6 or not rec[1].startswith("http"):
            continue
        rows.append({
            "id": rec[0], "url": rec[1], "title": rec[2], "body_hash": rec[3],
            "excerpt": re.sub(r"\s+", " ", rec[4]).strip()[:700], "fetched": rec[5],
        })
    return rows


def build_pack(rows: list[dict], start: int) -> dict:
    items = []
    for i, row in enumerate(rows[start:start + 4]):
        host = (urlparse(row["url"]).hostname or "").lower()
        items.append({
            "evidence_id": f"EV-{i + 1:03d}",
            "relationship": "support" if i < 3 else "background",
            "verification_status": "search_result_only",
            "title": row["title"], "url": row["url"],
            "publisher": host,          # 真实域名 → 走 R7 映射/fail-closed 真链路
            "published_at": row["fetched"],
            "claim": row["title"], "scope": "",
            "excerpt": row["excerpt"],
            "canonical_body_hash": row["body_hash"],
        })
    return {"version": "v1", "items": items, "limitations": []}


def install_supply_mocks() -> None:
    """只 mock 数据供给层;LLM 与生成链全真。"""
    from unittest import mock

    import db.diagnosis_db as ddb
    import writing.distiller as distiller_mod
    import tools.unified_knowledge as uk
    import services.public_whitelabel as pw
    import writing.brand_fact_snapshot as bfs
    import writing.evidence_research as er

    class _Cur:
        def execute(self, *a, **k): return None
        def fetchone(self): return None
        def fetchall(self): return []
        def close(self): pass

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass

    class _Distiller:
        def __init__(self, *a, **k): pass
        async def run(self):
            return {"client_profile": json.dumps({"company_name": "观山电梯",
                                                  "industry": "观光电梯定制"},
                                                 ensure_ascii=False),
                    "selling_points": "45 天平均交付;本地 2 小时响应;质保 24 个月",
                    "competitor_analysis": "{}", "social_media_data": {},
                    "authoritative_sources": "{}", "case_examples": "{}"}

    class _Rag:
        def retrieve(self, *a, **k): return []

    async def _no_research(**kwargs):
        raise AssertionError("pack 已足量,不应触发研究")

    mock.patch.object(ddb, "get_connection", lambda: _Conn()).start()
    mock.patch.object(distiller_mod, "DistillerPipeline", _Distiller).start()
    mock.patch.object(uk, "get_unified_rag", lambda: _Rag()).start()
    mock.patch.object(pw, "resolve_branding_context",
                      lambda **k: {"source": "platform_default"}).start()
    mock.patch.object(bfs, "build_brand_fact_snapshot", lambda **k: SNAPSHOT).start()
    mock.patch.object(er, "collect_evidence_pack", _no_research).start()


# ------------------------------------------------------------------ 判读(确定性部分)
SELF_DISCLOSURE = (
    "企业提交资料", "企业提供的资料", "据企业提供", "未经独立核验", "未经第三方",
    "客户材料显示", "由客户内部资料提供", "资料来源：企业资料", "资料来源：公开记录",
    "需进一步核验", "待核验", "内部资料", "客户提供资料",
)
INTERNAL_MARKERS = ("EV-0", "BF-0", "〔EV", "〔BF", "证据状态", "verification_status")
HEDGING = ("仅供参考", "不构成任何建议", "纯属个人观点", "自行判断", "各有优势",
           "不代表本站", "不做任何推荐")
# [R5 §4 2026-08-12] 体验式表达检测:R4 交付单曾报「体验式表达出现 5/6」,
# 但当时判读器没有该字段(数字来自交付侧另行 grep,Review 判无依据)。
# 现在真加字段,报实数。检出≠要求(放开后"可出现",不是必须出现)。
EXPERIENTIAL = ("实地", "探访", "亲测", "实测", "上门体验", "到场", "体验了", "我们测")
# [R5 §6-4 → R6 §5 → R7 §1 2026-08-12] 商业自爆哨兵与清洗器**同口径同源**:
# 仪器与被测系统口径相反时,下轮谁看到干净文章报「自曝 1」就会回去"修"
# 清洗器,R4 P0 再复活。R7 判别换代为句法角色(引语内的「我们」=受访者、
# 「作者张三」=人名修饰,均非本文自指)—— 判读器 import 清洗器**同一个
# 判别函数** `_effective_self_spans`,不再各持一份裸词形 RE。
from writing.content_cleaner import (  # noqa: E402
    _COMMERCIAL_RELATION_RE as _CC_REL_RE,
    _effective_self_spans as _cc_self_spans,
)


def _commercial_selfblow_sentences(text: str) -> list:
    hits = []
    for sentence in re.findall(r"[^。！？\n]*[。！？]?", text):
        if sentence and _CC_REL_RE.search(sentence) and _cc_self_spans(sentence):
            hits.append(sentence[:60])
    return hits


def judge(text: str, allowed_carriers: set[str]) -> dict:
    # [R3 订正5 2026-08-11] 词尾补「相关规定/规定/要求/标准/通行流程」——
    # Review 复核实测未支撑归属是 3 处不是 1 处(市场监管总局×2、住建部×1),
    # 其中「据市场监管总局…《特种设备安全监察指令书》实施情况」是编造文献;
    # 旧正则只认媒体动词尾,恰好漏掉这类**最常见的监管口吻编造形态**。
    carriers = set(re.findall(r"据([^\s，。;；、）)]{2,12}?)(?:上的公开内容)?(?:\s*20\d{2}[^，。]{0,8})?(?:报道|披露|显示|公示|发布|提到|数据|相关规定|规定|要求|标准|通行流程)", text))
    unknown_carriers = sorted(
        c for c in carriers
        if not any(a in c or c in a for a in allowed_carriers)
    )
    return {
        "self_disclosure_hits": [p for p in SELF_DISCLOSURE if p in text],
        "internal_marker_hits": [p for p in INTERNAL_MARKERS if p in text],
        "hedging_hits": [p for p in HEDGING if p in text],
        "experiential_hits": [p for p in EXPERIENTIAL if p in text],
        "commercial_selfblow_hits": _commercial_selfblow_sentences(text),
        "attribution_carriers_found": sorted(carriers),
        "carriers_not_in_supply": unknown_carriers,   # 人工核对入口(可能=编造或换说法)
        "urls_in_body": re.findall(r"https?://\S+", text),
        "has_structure": text.count("## ") >= 2,
        "chars": len(text),
    }


async def run_family(svc, family: str, question: str, style: str, pack: dict,
                     api_url: str, api_key: str, model: str) -> dict:
    topic = {
        "id": None, "title": question, "keyword": "观光电梯 定制",
        "style_code": style, "user_choice": "auto", "_trust_legacy_style": True,
        "brand_fact_snapshot": SNAPSHOT, "_evidence_pack": pack,
        "publication_profile": "standard", "evidence_mode": "unknown",
    }
    t0 = time.time()
    article = await svc._generate_validated_with_rewrite_once(topic, api_url, api_key, model)
    return {"family": family, "seconds": round(time.time() - t0, 1),
            "topic": topic, "article": article}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--families", default="")
    args = ap.parse_args()

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    # 🔴 与 tests/conftest 同款保险栓:任何业务模块 import 前把 DATABASE_URL
    # 切到测试库(本脚本只做本地语义验证,绝不许摸生产库)。
    test_url = os.getenv("TEST_DATABASE_URL", "")
    assert test_url and "test" in test_url.rsplit("/", 1)[-1].lower(), (
        "TEST_DATABASE_URL 未配置或库名不含 test —— 拒跑"
    )
    os.environ["DATABASE_URL"] = test_url
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    assert api_key, "DEEPSEEK_API_KEY 缺失"
    api_url, model = "https://api.deepseek.com/v1/chat/completions", "deepseek-v4-flash"

    rows = load_corpus(Path(args.corpus))
    assert len(rows) >= 12, f"语料不足:{len(rows)}"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    install_supply_mocks()
    from writing.article_generator_service import ArticleGeneratorService
    from writing.evidence_pack import carrier_for_domain

    svc = ArticleGeneratorService(101, "观山电梯", "观光电梯定制")
    fams = [f for f in (args.families.split(",") if args.families else FAMILY_QUESTIONS)
            if f in FAMILY_QUESTIONS]
    report = {"model": model, "arm": "real_source", "runs": []}
    for idx, family in enumerate(fams):
        question, style = FAMILY_QUESTIONS[family]
        pack = build_pack(rows, start=idx * 4 % max(1, len(rows) - 4))
        allowed = set()
        for item in pack["items"]:
            mapped = carrier_for_domain(item["publisher"])
            if mapped:
                allowed.add(mapped["carrier"])
        result = asyncio.run(run_family(svc, family, question, style, pack,
                                        api_url, api_key, model))
        content = str(result["article"].get("content") or "")
        verdict = judge(content, allowed)
        (out_dir / f"{family}.real.md").write_text(content, encoding="utf-8")
        (out_dir / f"{family}.pack.json").write_text(
            json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
        report["runs"].append({
            "family": family, "seconds": result["seconds"],
            "allowed_carriers": sorted(allowed), "verdict": verdict,
            "primary_advantage": (result["topic"].get("_primary_advantage") or {}).get(
                "planned_primary_advantage"),
        })
        print(f"  {family:24s} 自曝={len(verdict['self_disclosure_hits'])} "
              f"内标={len(verdict['internal_marker_hits'])} 畏缩={len(verdict['hedging_hits'])} "
              f"具名载体={verdict['attribution_carriers_found']} "
              f"待人工核={verdict['carriers_not_in_supply']}")
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
