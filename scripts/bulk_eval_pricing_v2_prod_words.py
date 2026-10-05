"""
bulk_eval_pricing_v2_prod_words — prod 全量历史词 v2.1 新算价批量评估(老板 2026-06-11 指令"跑大量数据自己评估")

数据:prod keyword_price_cache 全量底盘(scripts/prod_words_bulk_eval.tsv · 只读拉取)
跑法:还原每词 5118/metaso 底盘 → 真打 LLM 批量评估(本机 deepseek · qwen 网墙挡=单源知情)
对比:旧 standard_price(按 markup_ratio 列归一到出厂层)vs 新 standard_price(出厂层)
输出:bulk_eval_result.csv(全量明细)+ 终端统计摘要(分布/爆价/塌价/类型/风险标)
"""
from __future__ import annotations

import asyncio
import csv
import json
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TSV = PROJECT_ROOT / "scripts" / "prod_words_bulk_eval.tsv"
OUT = PROJECT_ROOT / "scripts" / "bulk_eval_result.csv"
CHUNK = 48          # 每轮喂 48 词(6 个 LLM 批并发)· 防 429
MARKUP_COMPARE = 1.0  # 出厂层对比口径


def load_words() -> list[dict]:
    rows = []
    seen = set()
    for line in TSV.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 16 or not parts[0].strip():
            continue
        kw = parts[0].strip()
        industry, city = parts[1].strip(), parts[2].strip()
        key = (kw, industry, city)
        if key in seen:
            continue
        seen.add(key)
        try:
            sa = json.loads(parts[9]) if parts[9].strip() else {}
        except Exception:
            sa = {}
        try:
            old_std = float(parts[10] or 0)
            old_markup = float(parts[12] or 1.0) or 1.0
        except ValueError:
            old_std, old_markup = 0.0, 1.0
        rows.append({
            "keyword": kw, "industry": industry, "city": city,
            "search_volume": int(float(parts[3] or 0)),
            "sem_price": float(parts[4] or 0),
            "bidword_company_count": int(float(parts[5] or 0)),
            "content_count": int(float(parts[6] or 0)),
            "competitor_count": int(float(parts[7] or 1)),
            "effective_competition": int(float(parts[8] or 1)),
            "source_authority": sa,
            "old_std_factory": round(old_std / old_markup, 1) if old_std > 0 else 0.0,
            "old_formula": parts[13].strip(),
            "old_keyword_type": parts[14].strip(),
            "old_sro": parts[15].strip().lower() == "t",
        })
    return rows


async def main() -> int:
    from tools.pricing_llm_assessor import assess_keywords_pricing_batch

    words = load_words()
    print(f"prod 历史词去重后:{len(words)} 词 · 分 {(len(words) + CHUNK - 1) // CHUNK} 轮 × {CHUNK} 词真打 LLM")

    results = []
    for i in range(0, len(words), CHUNK):
        group = words[i:i + CHUNK]
        f5 = {w["keyword"]: {"search_volume": w["search_volume"], "sem_price": w["sem_price"],
                             "bidword_company_count": w["bidword_company_count"]} for w in group}
        ms = {w["keyword"]: {"content_count": w["content_count"], "competition_count": w["competitor_count"],
                             "effective_competition": w["effective_competition"],
                             "source_authority": w["source_authority"]} for w in group}
        # 行业按组内多数(同 brand 词通常同行业)· 简化:逐词单行业调用代价高 → 按行业分桶
        by_industry: dict[str, list[dict]] = {}
        for w in group:
            by_industry.setdefault(w["industry"], []).append(w)
        for industry, ws in by_industry.items():
            kws = [w["keyword"] for w in ws]
            try:
                res = await assess_keywords_pricing_batch(
                    kws, industry, "(批量评估)", f5, ms, markup=MARKUP_COMPARE,
                    default_city=ws[0]["city"],
                )
            except Exception as exc:
                print(f"  组异常(降级跳过 {len(kws)} 词): {exc}")
                continue
            for w in ws:
                a = res.get(w["keyword"]) or {}
                results.append({**w, "assess": a})
        done = min(i + CHUNK, len(words))
        print(f"  进度 {done}/{len(words)}")

    # ===== 写明细 CSV =====
    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["keyword", "industry", "city", "eff_comp_measured", "content_count",
                     "true_comp_new", "cost_new", "media_tier", "kw_type_new", "kw_type_old",
                     "old_std_factory", "new_std_factory", "ratio_new_old",
                     "articles_std", "llm_used", "risk_flags", "needs_review", "sro_old", "sro_new"])
        for r in results:
            a = r["assess"]
            new_std = float(a.get("standard_price") or 0)
            old = r["old_std_factory"]
            ratio = round(new_std / old, 2) if old > 0 and new_std > 0 else ""
            wr.writerow([r["keyword"], r["industry"], r["city"], r["effective_competition"],
                         r["content_count"], a.get("true_competition"), a.get("cost_per_article"),
                         a.get("media_tier_required"), a.get("keyword_type"), r["old_keyword_type"],
                         old, new_std, ratio, a.get("standard_articles"), a.get("llm_used"),
                         "|".join(a.get("risk_flags") or []), a.get("needs_review"),
                         r["old_sro"], "super_red_ocean" in (a.get("risk_flags") or [])])

    # ===== 统计摘要 =====
    new_prices = [float(r["assess"].get("standard_price") or 0) for r in results if r["assess"]]
    pairs = [(r["old_std_factory"], float(r["assess"].get("standard_price") or 0))
             for r in results if r["assess"] and r["old_std_factory"] > 0
             and float(r["assess"].get("standard_price") or 0) > 0]
    ratios = sorted(n / o for o, n in pairs)
    fallback_n = sum(1 for r in results if r["assess"].get("llm_used") == "fallback")
    single_n = sum(1 for r in results if r["assess"].get("llm_used") in ("primary_only", "secondary_only"))
    review_n = sum(1 for r in results if r["assess"].get("needs_review"))
    sro_n = sum(1 for r in results if "super_red_ocean" in (r["assess"].get("risk_flags") or []))
    blow_n = sum(1 for p in new_prices if p > 10000)
    crash_n = sum(1 for p in new_prices if 0 < p < 200)

    def pct(lst, q):
        return lst[min(int(len(lst) * q), len(lst) - 1)] if lst else 0

    print("\n" + "=" * 72)
    print(f"评估词数: {len(results)}(LLM 单源 {single_n} · 纯公式兜底 {fallback_n})")
    sp = sorted(new_prices)
    print(f"新出厂 std 分布: p10 ¥{pct(sp, 0.1):,.0f} · p50 ¥{pct(sp, 0.5):,.0f} · p90 ¥{pct(sp, 0.9):,.0f} · max ¥{max(sp):,.0f}")
    print(f"新/旧比值(可比 {len(pairs)} 词): p10 {pct(ratios, 0.1):.2f}x · p50 {pct(ratios, 0.5):.2f}x · p90 {pct(ratios, 0.9):.2f}x")
    print(f"爆价候选(出厂 > ¥10,000): {blow_n} · 塌价候选(出厂 < ¥200): {crash_n}")
    print(f"needs_review: {review_n}({review_n / max(len(results), 1) * 100:.0f}%) · 超红海标: {sro_n}")
    print(f"明细 → {OUT}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
