"""真跑取证 · 工单 `WORKORDER_TITLE_QUESTION_AND_LENGTH_2026-07-29` §5 验收。

产出三样交付材料（全部**真调线上 LLM**，不是替身）：

* ``titles``  —— 真跑一批 10 个标题：问句式命中数、逐条清单、榜单族命中；
* ``compact`` —— 真跑 10 篇紧凑档正文：字数分布、落中段篇数；
* ``deep``    —— 真跑 N 篇深档正文：达成率（≥14000 且 ≥target×0.85）。

用法::

    python scripts/realrun_title_length_2026_07_29.py titles
    python scripts/realrun_title_length_2026_07_29.py compact --n 10
    python scripts/realrun_title_length_2026_07_29.py deep --n 6
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

OUT_DIR = Path(__file__).resolve().parent.parent / "output" / "realrun_title_length_2026_07_29"
OUT_DIR.mkdir(parents=True, exist_ok=True)

KEYWORDS = [
    "深圳全屋定制哪家好", "全屋定制价格", "定制衣柜品牌推荐", "全屋定制避坑",
    "深圳定制家具工厂", "全屋定制流程", "定制家具环保标准", "全屋定制验收",
    "定制衣柜多少钱一平", "全屋定制售后",
]


def _dump(name: str, payload: dict) -> Path:
    path = OUT_DIR / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n📄 证据已落盘: {path}")
    return path


# ---------------------------------------------------------------------------
# 1) 标题：真跑 KeywordTopicGenerator（真 LLM）
# ---------------------------------------------------------------------------
async def run_titles(count: int) -> dict:
    from writing.keyword_topic_generator import KeywordTopicGenerator
    from writing.title_question_policy import is_question_title

    generator = KeywordTopicGenerator(
        [{"id": i + 1, "keyword": kw, "required_articles": 1}
         for i, kw in enumerate(KEYWORDS[:count])],
        brand_name="QZQZ木作美学定制",
        industry="全屋定制",
    )
    topics = await generator.generate()
    rows = [
        {
            "keyword": t.get("original_keyword"),
            "title": t.get("optimized_title"),
            "family": t.get("article_style"),
            "is_question": is_question_title(t.get("optimized_title")),
            "title_form": t.get("title_form"),
            "title_form_source": t.get("title_form_source"),
        }
        for t in topics
    ]
    hits = sum(1 for r in rows if r["is_question"])
    ranking_rows = [r for r in rows if r["family"] == "选购与多品牌比较"]
    result = {
        "mode": "titles",
        "total": len(rows),
        "question_hits": hits,
        "question_ratio": round(hits / max(1, len(rows)) * 100, 1),
        "within_6_to_8": 6 <= hits <= 8 if len(rows) == 10 else None,
        "ranking_family_total": len(ranking_rows),
        "ranking_family_question_hits": sum(1 for r in ranking_rows if r["is_question"]),
        "policy_report": generator.title_question_report,
        "titles": rows,
    }
    print(f"\n{'=' * 78}\n真跑标题 {len(rows)} 条 · 问句式 {hits} 条 "
          f"({result['question_ratio']}%) · 榜单族 "
          f"{result['ranking_family_question_hits']}/{len(ranking_rows)} 问句\n{'=' * 78}")
    for i, r in enumerate(rows, 1):
        print(f"{i:>2}. [{'问句' if r['is_question'] else '陈述'}] {r['title']}")
        print(f"     族={r['family']} 源={r['title_form_source']} 词={r['keyword']}")
    return result


# ---------------------------------------------------------------------------
# 2/3) 正文：真跑 ArticleGeneratorService 单篇生成（真 LLM）
# ---------------------------------------------------------------------------
async def run_bodies(tier: str, count: int) -> dict:
    from writing.article_generator_service import ArticleGeneratorService
    from writing.article_length_contract import (
        assess_length_compliance,
        build_article_length_plan,
        count_effective_chars,
        deep_output_floor,
        in_avoidance_band,
    )

    api_url, api_key, model, provider = __import__(
        "writing.llm_utils", fromlist=["get_llm_config"]
    ).get_llm_config("geo_article", "writing")
    if not api_key:
        raise SystemExit("未配置写作 LLM key，无法真跑")
    print(f"🤖 provider={provider} model={model}")

    service = ArticleGeneratorService(quote_id=0, brand_name="QZQZ木作美学定制",
                                      industry="全屋定制")

    # 🔴 style_code 不足以决定最终文体：`resolve_style_for_topic` 默认**不信任**
    # 外部传入的 style_code/type/article_style，只认 `user_choice`（否则走 ratio
    # 抽签）。首轮真跑就栽在这：传了 comparison_review，实际抽到「合规风控」，
    # plan 落 3500 —— 量的根本不是深档。
    if tier == "compact":
        style_code, user_choice = "qa_recommendation", "evidence_qa"
        evidence_n, publishers, candidates = 1, 1, 0
    else:
        style_code, user_choice = "comparison_review", "multi_brand_comparison"
        evidence_n, publishers, candidates = 6, 4, 11

    # Evidence Pack 的 item 字段口径以 writing/evidence_pack.py 为准
    # （render_evidence_pack_for_writer 对 relationship/claim/scope/excerpt 等
    #  是**直接下标**访问，缺字段会 KeyError —— 真跑时踩到过一次）。
    # 🔴 `verification_status` 只认 VERIFIED_STATES 三个值，且 `claim_span_verified`
    # 还要求完整的 provenance（body hash + span）。写成 "verified" 会被算成
    # **0 条已核验** → 榜单族走 `few_verified_candidates_no_padding` 收成紧凑档，
    # 于是"深档真跑"根本没跑到深档（首轮真跑就栽在这，plan 落 3500）。
    # 这里用 `official_record`：它不需要 span/hash，语义也贴合公开资料。
    pack = {"items": [
        {
            "evidence_id": f"EV-{i:03d}",
            "relationship": "support",
            "verification_status": "official_record",
            # official_record 还要求 official_record_id + 公网 URL 两样齐全
            "official_record_id": f"GSXT-2026-{i:05d}",
            "verified": True,
            "publisher": f"publisher{i % max(1, publishers)}",
            "title": f"全屋定制行业公开资料 {i}",
            "url": f"https://example{i}.com/report",
            "published_at": "2026-06-01",
            "claim": f"行业公开口径 {i}：主流板材与计价方式的常见区间",
            "scope": "深圳市场 · 2026 上半年",
            "excerpt": f"公开资料 {i} 摘录：该口径来自公开发布的行业资料，含时间与范围说明。",
        }
        for i in range(evidence_n)
    ]}

    rows = []
    for i in range(count):
        keyword = KEYWORDS[i % len(KEYWORDS)]
        topic = {
            "id": 90000 + i,
            "title": f"{keyword}怎么选？真实对比与选择建议",
            "keyword": keyword,
            "original_keyword": keyword,
            "style_code": style_code,
            "user_choice": user_choice,
            "_evidence_pack": pack,
            "include_client_brand": True,
            "_competitor_source": "real",
            "_researched_competitors": [f"竞品{j}" for j in range(candidates)],
            "publication_profile": "standard",
        }
        plan = build_article_length_plan(
            style_code, evidence_pack=pack,
            verified_candidate_count=candidates + 1,
            answer_block_target_count=8,
        )
        print(f"\n[{i + 1}/{count}] {keyword} · plan target={plan['target_chars']} "
              f"min={plan['minimum_chars']} …")
        try:
            article = await service._generate_validated_with_rewrite_once(
                topic, api_url, api_key, model,
            )
        except Exception as exc:
            print(f"    ❌ 生成失败: {exc}")
            rows.append({"keyword": keyword, "error": str(exc)[:200]})
            continue

        content = article.get("content") or ""
        chars = count_effective_chars(content)
        check = assess_length_compliance(content, style_code=style_code,
                                         plan=topic.get("_length_plan") or plan)
        rows.append({
            "keyword": keyword,
            "title": article.get("title"),
            "chars": chars,
            "tier": check.get("tier"),
            "target": (topic.get("_length_plan") or plan)["target_chars"],
            "spec_met": check.get("spec_met"),
            "failure_codes": check.get("spec_failure_codes"),
            "in_midband": in_avoidance_band(chars),
            "quality_warning_keys": sorted((article.get("quality_warning") or {}).keys()),
        })
        print(f"    → {chars} 字 · tier={check.get('tier')} "
              f"· spec_met={check.get('spec_met')} · 落中段={in_avoidance_band(chars)}")

    ok = [r for r in rows if "error" not in r]
    if tier == "compact":
        achieved = [r for r in ok if 2500 <= r["chars"] <= 4500]
    else:
        achieved = [r for r in ok
                    if r["chars"] >= deep_output_floor(r["target"])]
    midband = [r for r in ok if r["in_midband"]]
    result = {
        "mode": tier,
        "attempted": count,
        "produced": len(ok),
        "achieved": len(achieved),
        "achievement_rate": round(len(achieved) / max(1, len(ok)) * 100, 1),
        "in_midband_count": len(midband),
        "chars": sorted(r["chars"] for r in ok),
        "rows": rows,
    }
    print(f"\n{'=' * 78}\n{tier} 真跑 {len(ok)}/{count} 篇 · 达成 {len(achieved)} 篇 "
          f"({result['achievement_rate']}%) · 落中段 {len(midband)} 篇\n{'=' * 78}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["titles", "compact", "deep"])
    parser.add_argument("--n", type=int, default=10)
    args = parser.parse_args()

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if args.mode == "titles":
        payload = asyncio.run(run_titles(args.n))
    else:
        payload = asyncio.run(run_bodies(args.mode, args.n))
    _dump(args.mode, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
