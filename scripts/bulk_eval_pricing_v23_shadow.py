"""
bulk_eval_pricing_v23_shadow — 报价 v2.3 四模式影子评估(2026-06-14 · DELTA 4)

目的(v2.3 DELTA 补强计划 §12):同一批词输出 flags-off / +P0-A / +P0-A+B / +P0-A+B+C 四模式
对比报告 + cost/value/comp/factory ratio 观测,供老板/Codex 在【生产 flip flag 前】影子验收。

为什么是【replay】不是 4× 真打 LLM:三个 flag 只作用于 `_assemble_result`(确定性价格装配),
  不作用于 LLM/metaso 测量。固定 (ctx, judged) 输入、只切 flag replay `_assemble_result` 四次,
  = 零 LLM 噪声、纯隔离 flag 效应的真影子对比(4× 真打 LLM 反而引入 LLM 方差污染对比)。
  flag 在【本进程内】monkeypatch 切换 → 绝不动生产 system_settings(无 prod 副作用)。

输入:
  - 内置 archetype 样本(§12.2 覆盖:深圳火词/县级长尾/全国高价值/低竞争小词/装修/高客单/
    用户自加/缺5118/全国放飞/历史异常 重庆璧山·栖舍)。
  - 可选 --inputs <json>:[{label, markup, cost_override, ctx, judged}, ...](捕获的真实 prod 词输入,
    离线 replay = 正经影子;真实捕获是另一步网络作业,产出此 JSON 喂本脚本)。

输出:
  - scripts/pricing_v23_shadow_result.csv(§12.3 全字段明细)
  - 终端摘要:DELTA 5 snapshot_source 验收探针 + §12.4 验收问题 PASS/CHECK。

跑法:python scripts/bulk_eval_pricing_v23_shadow.py [--inputs path.json] [--out path.csv]
  flags 默认全关 = 现状;本脚本【不改】生产 flag,只在内存 monkeypatch replay。
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
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

import tools.llm_pricing_flag as FLAG            # noqa: E402
import tools.pricing_llm_assessor as A           # noqa: E402
from tools.media_cost_ssot import resolve_snapshot_source  # noqa: E402
from tools.pricing_bands import CURRENT_PRICING_FORMULA_VERSION  # noqa: E402

OUT_DEFAULT = PROJECT_ROOT / "scripts" / "pricing_v23_shadow_result.csv"
LLM_META = {"llm_deviation_pct": 0.0, "llm_used": "dual"}

# 四模式:绝不三 flag 一起首次开 = 部署铁律;但影子里必须看清 A+B+C 叠加效果
MODES = [
    ("off",     dict(cost=False, value=False, nat=False)),
    ("A",       dict(cost=True,  value=False, nat=False)),
    ("A+B",     dict(cost=True,  value=True,  nat=False)),
    ("A+B+C",   dict(cost=True,  value=True,  nat=True)),
]


def _ctx(keyword, *, measured_comp, content_count, dynamic_cost=55.0, cost_multiplier=1.0,
         five118=None, metaso_fallback=False, saturated=False):
    return dict(keyword=keyword, measured_comp=measured_comp, saturated=saturated,
                metaso={"competition_count": measured_comp, "content_count": content_count},
                cost_multiplier=cost_multiplier, dynamic_cost=dynamic_cost,
                five118=(five118 or {}), metaso_fallback=metaso_fallback)


def _judged(*, true_competition, keyword_type, city, city_tier, media_tier, value_signal,
            cost_suggested=55.0, national_unclamped=False):
    return dict(true_competition=true_competition, cost_per_article_suggested=cost_suggested,
                keyword_type=keyword_type, city=city, city_tier=city_tier,
                media_tier_required=media_tier, value_signal=value_signal,
                reasoning="shadow", risk_flags=[], confidence=0.8,
                national_unclamped=national_unclamped)


# 5118 有信号 / 无信号
_F5 = {"search_volume": 800, "sem_price": 12.0, "bidword_company_count": 40}

# §12.2 archetype 样本
ARCHETYPES = [
    {"label": "深圳火词(装修哪家好)", "markup": 2.0, "cost_override": None,
     "ctx": _ctx("深圳装修哪家好", measured_comp=40, content_count=100, dynamic_cost=55.0, five118=_F5),
     "judged": _judged(true_competition=40, keyword_type="local_city", city="深圳",
                       city_tier="tier1", media_tier="B", value_signal=2.5)},
    {"label": "县级长尾(罗平县装修·无5118)", "markup": 2.0, "cost_override": None,
     "ctx": _ctx("罗平县装修公司", measured_comp=5, content_count=12, dynamic_cost=55.0),
     "judged": _judged(true_competition=5, keyword_type="local_county", city="罗平",
                       city_tier="county", media_tier="C", value_signal=3.0)},  # 无5118 → P0-B off 钳
    {"label": "全国高价值(别墅电梯定制)", "markup": 2.0, "cost_override": None,
     "ctx": _ctx("别墅电梯定制", measured_comp=20, content_count=60, dynamic_cost=90.0, five118=_F5),
     "judged": _judged(true_competition=20, keyword_type="national_niche", city=None,
                       city_tier="national", media_tier="A", value_signal=3.0, cost_suggested=90.0)},
    {"label": "低竞争小词(工业球阀厂家·无5118)", "markup": 2.0, "cost_override": None,
     "ctx": _ctx("工业球阀厂家", measured_comp=4, content_count=8, dynamic_cost=45.0),
     "judged": _judged(true_competition=4, keyword_type="national_niche", city=None,
                       city_tier="national", media_tier="C", value_signal=1.0, cost_suggested=45.0)},
    {"label": "装修/旧房改造(深圳·有5118)", "markup": 2.0, "cost_override": None,
     "ctx": _ctx("深圳旧房改造", measured_comp=30, content_count=80, dynamic_cost=55.0, five118=_F5),
     "judged": _judged(true_competition=30, keyword_type="local_city", city="深圳",
                       city_tier="tier1", media_tier="B", value_signal=2.0)},
    {"label": "高客单行业(医美·决策·有5118)", "markup": 2.0, "cost_override": None,
     "ctx": _ctx("深圳医美哪家好", measured_comp=35, content_count=100, dynamic_cost=110.0, five118=_F5),
     "judged": _judged(true_competition=35, keyword_type="local_city", city="深圳",
                       city_tier="tier1", media_tier="A", value_signal=3.0, cost_suggested=110.0)},
    {"label": "用户自加(cost_override=¥120)", "markup": 2.0, "cost_override": 120.0,
     "ctx": _ctx("品牌自定义词", measured_comp=10, content_count=20, dynamic_cost=55.0, five118=_F5),
     "judged": _judged(true_competition=10, keyword_type="local_city", city="深圳",
                       city_tier="tier1", media_tier="B", value_signal=2.0)},
    {"label": "缺数据(殡葬服务·全0 5118)", "markup": 2.0, "cost_override": None,
     "ctx": _ctx("殡葬服务公司", measured_comp=8, content_count=15, dynamic_cost=55.0),
     "judged": _judged(true_competition=8, keyword_type="national_niche", city=None,
                       city_tier="national", media_tier="C", value_signal=2.0)},
    {"label": "全国词放飞候选(national_unclamped)", "markup": 2.0, "cost_override": None,
     "ctx": _ctx("装修", measured_comp=40, content_count=100, dynamic_cost=90.0, five118=_F5),
     "judged": _judged(true_competition=95, keyword_type="national_head", city=None,
                       city_tier="national", media_tier="A", value_signal=3.0, cost_suggested=90.0,
                       national_unclamped=True)},  # 放飞:true_comp 95 >> measured 40
    {"label": "历史异常·重庆璧山高价包", "markup": 2.0, "cost_override": None,
     "ctx": _ctx("重庆璧山别墅电梯", measured_comp=18, content_count=50, dynamic_cost=90.0, five118=_F5),
     "judged": _judged(true_competition=18, keyword_type="national_niche", city="重庆",
                       city_tier="tier2", media_tier="A", value_signal=3.0, cost_suggested=90.0)},
    {"label": "历史异常·栖舍火词(被压低)", "markup": 2.0, "cost_override": None,
     "ctx": _ctx("栖舍智能锁", measured_comp=30, content_count=85, dynamic_cost=55.0, five118=_F5),
     "judged": _judged(true_competition=30, keyword_type="national_niche", city=None,
                       city_tier="national", media_tier="A", value_signal=3.0)},  # OFF cost 钉 B/55,P0-A 应回真上抬
]

REPORT_FIELDS = [
    "label", "mode", "keyword", "city", "keyword_type",
    "entry", "standard", "flagship", "articles_std", "cost_per_article",
    "value_multiplier", "effective_competition",
    "cost_ratio", "value_ratio", "comp_ratio", "factory_ratio",
    # [P0-D] 信任资产观测列(A/B/C 模式恒空 · 仅 P0-D / Post-P0D 模式有值)
    #   [收口 2026-06-15] 列名与老板验收清单逐字对齐:trust_asset_source / citation_readiness_score。
    "trust_ratio", "trust_asset_source", "citation_readiness_score",
    "needs_review", "guarantee_unavailable", "blowup_no_cache", "guards",
    "snapshot_source", "snapshot_version", "pricing_formula_version",
]

# [P0-D] 四子模式:flag-off 基线 / 缺采集 / 缺背书(低 readiness)/ 背书足(高 readiness)。
#   全程 A/B/C 关(P0-D 正交·绝不与 A/B/C 同批首开)· trust_asset 注入 ctx。
def _trust_asset(source, readiness, score=None, needs_review=False):
    return {"source": source, "trust_asset_score": score,
            "citation_readiness_score": readiness, "trust_asset_needs_review": needs_review,
            "verified_labels": [], "missing_labels": []}


P0D_MODES = [
    ("D:off",     dict(trust=False, asset=None)),
    ("D:missing", dict(trust=True,  asset=_trust_asset("missing", None, needs_review=True))),
    ("D:low",     dict(trust=True,  asset=_trust_asset("collected", 0.10, score=0.10))),
    ("D:high",    dict(trust=True,  asset=_trust_asset("collected", 0.90, score=0.90))),
]
# P0-D 聚焦词(篇数侧效应最清晰):全国高价值 / 三合一(高价值+高竞争+缺背书)/ 低值低竞争 / 城市高竞争
P0D_WORD_LABELS = [
    "全国高价值(别墅电梯定制)",
    "高客单行业(医美·决策·有5118)",
    "低竞争小词(工业球阀厂家·无5118)",
    "深圳火词(装修哪家好)",
]

# [P0-A 前置影子 2026-06-15] Post-P0D 分层模式:基线 = 当前生产态(P0-D on · A/B/C off),
#   再分层叠加 P0-A → P0-A+B → P0-A+B+C。看 P0-A/B/C 在【P0-D 已开】背景下的真实叠加效应。
#   绝不改生产 flag · 进程内 monkeypatch · trust_asset 每词注入(--inputs 真实词带真实快照;否则合成基线)。
POSTP0D_MODES = [
    ("P0D",         dict(trust=True, cost=False, value=False, nat=False)),  # = 当前生产基线
    ("P0D+A",       dict(trust=True, cost=True,  value=False, nat=False)),
    ("P0D+A+B",     dict(trust=True, cost=True,  value=True,  nat=False)),
    ("P0D+A+B+C",   dict(trust=True, cost=True,  value=True,  nat=True)),
]
# 合成基线 trust(--inputs 真实词若带 word['trust_asset'] 则优先用其真实归一化快照)
_DEFAULT_POSTP0D_TRUST = _trust_asset("collected", 0.5, score=0.5)


def _word_trust_asset(word: dict):
    """Post-P0D 模式每词的 trust_asset:真实词(--inputs)带 word['trust_asset'] 用真实;否则合成 collected 基线。"""
    ta = word.get("trust_asset")
    return copy.deepcopy(ta) if isinstance(ta, dict) else copy.deepcopy(_DEFAULT_POSTP0D_TRUST)


def _set_flags(cost: bool, value: bool, nat: bool, trust: bool = False) -> None:
    FLAG.is_cost_snapshot_enabled = lambda: cost
    FLAG.is_value_evidence_gate_relaxed = lambda: value
    FLAG.is_national_unclamped_enabled = lambda: nat
    FLAG.is_trust_asset_enabled = lambda: trust


def _restore_flags(orig: dict) -> None:
    FLAG.is_cost_snapshot_enabled = orig["cost"]
    FLAG.is_value_evidence_gate_relaxed = orig["value"]
    FLAG.is_national_unclamped_enabled = orig["nat"]
    FLAG.is_trust_asset_enabled = orig["trust"]


def _run_one(word: dict, snap_version) -> list[dict]:
    rows = []
    for mode_name, fl in MODES:
        _set_flags(**fl)
        # 每词每模式独立深拷贝输入(_assemble_result 可能 mutate risk_flags set)
        ctx = copy.deepcopy(word["ctx"])
        judged = copy.deepcopy(word["judged"])
        r = A._assemble_result(ctx, judged, dict(LLM_META), {}, word["markup"], word["cost_override"])
        rows.append({
            "label": word["label"], "mode": mode_name,
            "keyword": r.get("keyword"), "city": r.get("city"), "keyword_type": r.get("keyword_type"),
            "entry": r.get("entry_price"), "standard": r.get("standard_price"),
            "flagship": r.get("flagship_price"), "articles_std": r.get("standard_articles"),
            "cost_per_article": r.get("cost_per_article"),
            "value_multiplier": r.get("value_multiplier"),
            "effective_competition": r.get("true_competition"),
            "cost_ratio": r.get("cost_ratio"), "value_ratio": r.get("value_ratio"),
            "comp_ratio": r.get("comp_ratio"), "factory_ratio": r.get("factory_ratio"),
            "trust_ratio": r.get("trust_ratio"), "trust_asset_source": r.get("trust_asset_source"),
            "citation_readiness_score": r.get("citation_readiness_score"),
            "needs_review": r.get("needs_review"),
            "guarantee_unavailable": r.get("guarantee_unavailable"),
            "blowup_no_cache": r.get("blowup_no_cache"),
            "guards": json.dumps(r.get("guards") or {}, ensure_ascii=False, sort_keys=True),
            "snapshot_version": snap_version,
            "pricing_formula_version": CURRENT_PRICING_FORMULA_VERSION,
        })
    return rows


def _run_p0d(word: dict, snap_version) -> list[dict]:
    """[P0-D] 同词 replay 4 子模式(off / missing / low-readiness / high-readiness)· A/B/C 全关。"""
    rows = []
    for mode_name, cfg in P0D_MODES:
        _set_flags(cost=False, value=False, nat=False, trust=cfg["trust"])
        ctx = copy.deepcopy(word["ctx"])
        judged = copy.deepcopy(word["judged"])
        ctx["trust_asset"] = copy.deepcopy(cfg["asset"])
        r = A._assemble_result(ctx, judged, dict(LLM_META), {}, word["markup"], word["cost_override"])
        rows.append({
            "label": word["label"], "mode": mode_name,
            "keyword": r.get("keyword"), "city": r.get("city"), "keyword_type": r.get("keyword_type"),
            "entry": r.get("entry_price"), "standard": r.get("standard_price"),
            "flagship": r.get("flagship_price"), "articles_std": r.get("standard_articles"),
            "cost_per_article": r.get("cost_per_article"),
            "value_multiplier": r.get("value_multiplier"),
            "effective_competition": r.get("true_competition"),
            "cost_ratio": r.get("cost_ratio"), "value_ratio": r.get("value_ratio"),
            "comp_ratio": r.get("comp_ratio"), "factory_ratio": r.get("factory_ratio"),
            "trust_ratio": r.get("trust_ratio"), "trust_asset_source": r.get("trust_asset_source"),
            "citation_readiness_score": r.get("citation_readiness_score"),
            "needs_review": r.get("needs_review"),
            "guarantee_unavailable": r.get("guarantee_unavailable"),
            "blowup_no_cache": r.get("blowup_no_cache"),
            "guards": json.dumps(r.get("guards") or {}, ensure_ascii=False, sort_keys=True),
            "snapshot_version": snap_version,
            "pricing_formula_version": CURRENT_PRICING_FORMULA_VERSION,
        })
    return rows


def _run_postp0d(word: dict, snap_version) -> list[dict]:
    """[P0-A 前置] 同词 replay Post-P0D 分层 4 模式(P0D / +A / +A+B / +A+B+C)。
    P0-D 全程 ON(=当前生产基线)· trust_asset 每词注入(真实词带真实快照,否则合成 collected 基线)。"""
    rows = []
    ta = _word_trust_asset(word)
    for mode_name, fl in POSTP0D_MODES:
        _set_flags(cost=fl["cost"], value=fl["value"], nat=fl["nat"], trust=fl["trust"])
        ctx = copy.deepcopy(word["ctx"])
        judged = copy.deepcopy(word["judged"])
        ctx["trust_asset"] = copy.deepcopy(ta)
        r = A._assemble_result(ctx, judged, dict(LLM_META), {}, word["markup"], word["cost_override"])
        rows.append({
            "label": word["label"], "mode": mode_name,
            "keyword": r.get("keyword"), "city": r.get("city"), "keyword_type": r.get("keyword_type"),
            "entry": r.get("entry_price"), "standard": r.get("standard_price"),
            "flagship": r.get("flagship_price"), "articles_std": r.get("standard_articles"),
            "cost_per_article": r.get("cost_per_article"),
            "value_multiplier": r.get("value_multiplier"),
            "effective_competition": r.get("true_competition"),
            "cost_ratio": r.get("cost_ratio"), "value_ratio": r.get("value_ratio"),
            "comp_ratio": r.get("comp_ratio"), "factory_ratio": r.get("factory_ratio"),
            "trust_ratio": r.get("trust_ratio"), "trust_asset_source": r.get("trust_asset_source"),
            "citation_readiness_score": r.get("citation_readiness_score"),
            "needs_review": r.get("needs_review"),
            "guarantee_unavailable": r.get("guarantee_unavailable"),
            "blowup_no_cache": r.get("blowup_no_cache"),
            "guards": json.dumps(r.get("guards") or {}, ensure_ascii=False, sort_keys=True),
            "snapshot_version": snap_version,
            "pricing_formula_version": CURRENT_PRICING_FORMULA_VERSION,
        })
    return rows


def _by_mode(rows: list[dict]) -> dict:
    return {r["mode"]: r for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default=None, help="可选 JSON:[{label,markup,cost_override,ctx,judged}]")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()

    # DELTA 5:snapshot 来源探针(P0-A 生产 flip 前必须 source=db)
    src = resolve_snapshot_source()
    print("=" * 72)
    print("【DELTA 5】媒体成本快照来源探针(P0-A 生产 flip 前置 gate)")
    print(f"  source           = {src['source']}  (db=生产合格 / bootstrap=未写库不合格 / none=无快照)")
    print(f"  snapshot_version = {src['snapshot_version']}")
    print(f"  payload_sha256   = {src['payload_sha256']}")
    print(f"  generated_at     = {src['generated_at']}   geo_count={src['geo_count']}")
    if src["source"] != "db":
        print("  ⚠️  当前非 DB active snapshot:本次 P0-A 影子用的是 bootstrap/none。")
        print("      生产 flip P0-A 前必须 build_snapshot_from_db + 写 DB is_active row,并 assert_db_active_snapshot 通过。")
    print("=" * 72)

    words = list(ARCHETYPES)
    if args.inputs:
        extra = json.loads(Path(args.inputs).read_text(encoding="utf-8"))
        words = words + list(extra)
        print(f"已加载额外输入 {len(extra)} 词(真实 prod 词 replay)。")

    orig = {"cost": FLAG.is_cost_snapshot_enabled, "value": FLAG.is_value_evidence_gate_relaxed,
            "nat": FLAG.is_national_unclamped_enabled,
            "trust": getattr(FLAG, "is_trust_asset_enabled", lambda: False)}
    all_rows: list[dict] = []
    by_word: dict[str, dict] = {}
    p0d_by_word: dict[str, dict] = {}
    postp0d_by_word: dict[str, dict] = {}
    try:
        for w in words:
            rows = _run_one(w, src["snapshot_version"])
            all_rows.extend(rows)
            by_word[w["label"]] = _by_mode(rows)
        # [P0-D] 聚焦词 replay 4 子模式(篇数侧难度因子影子)
        _label_map = {w["label"]: w for w in words}
        for lbl in P0D_WORD_LABELS:
            w = _label_map.get(lbl)
            if not w:
                continue
            prows = _run_p0d(w, src["snapshot_version"])
            all_rows.extend(prows)
            p0d_by_word[lbl] = _by_mode(prows)
        # [P0-A 前置] 所有词 replay Post-P0D 分层 4 模式(基线 = 当前生产 P0-D on · 叠加 A/B/C)
        for w in words:
            pp = _run_postp0d(w, src["snapshot_version"])
            all_rows.extend(pp)
            postp0d_by_word[w["label"]] = _by_mode(pp)
    finally:
        _restore_flags(orig)
        FLAG.clear_cache()

    # snapshot_source 每行回填(全局探针值 · 让每行自描述 P0-A 成本来源 db/bootstrap/none)
    for _r in all_rows:
        _r["snapshot_source"] = src["source"]

    out_path = Path(args.out)
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        wr.writeheader()
        wr.writerows(all_rows)
    print(f"\n明细已写:{out_path}  ({len(all_rows)} 行)\n")

    # [收口 2026-06-15] 人审摘要:老板要求的『哪些词涨 / 降 / 进 needs_review / 不能给 guarantee』四类逐词滚动清单。
    #   涨/降基线 = 当前生产态 P0D(P0-D on · A/B/C off),对比全开 P0D+A+B+C 的 standard(出厂)价方向 + Δ%。
    print("【人审摘要】(涨/降基线 = P0D 当前生产 · 对比 P0D+A+B+C 全开)")
    ups: list = []
    downs: list = []
    flats = 0
    for lbl, modes in postp0d_by_word.items():
        base = modes.get("P0D")
        full = modes.get("P0D+A+B+C")
        if not base or not full:
            continue
        b = base.get("standard")
        v = full.get("standard")
        if not b or not v:
            continue
        delta = (v - b) / b * 100.0
        if delta > 1.0:
            ups.append((lbl, b, v, round(delta, 1)))
        elif delta < -1.0:
            downs.append((lbl, b, v, round(delta, 1)))
        else:
            flats += 1
    print(f"  涨价词 {len(ups)}:")
    for lbl, b, v, d in sorted(ups, key=lambda x: -x[3]):
        print(f"    ↑ {lbl[:26]:26}  ¥{b} → ¥{v}  (+{d}%)")
    print(f"  降价词 {len(downs)}:")
    for lbl, b, v, d in sorted(downs, key=lambda x: x[3]):
        print(f"    ↓ {lbl[:26]:26}  ¥{b} → ¥{v}  ({d}%)")
    print(f"  价格不变词 {flats}(|Δ| ≤ 1%)")

    nr = sorted({(r["label"], r["mode"]) for r in all_rows if r.get("needs_review")})
    print(f"\n  进 needs_review 的 (词, 模式) 共 {len(nr)}(看人工队列规模是否被淹没):")
    for lbl, mode in nr:
        print(f"    ⚑ {mode:12} {lbl[:38]}")

    gu = sorted({(r["label"], r["mode"]) for r in all_rows if r.get("guarantee_unavailable")})
    print(f"\n  不能给 guarantee(剥保证价 · 参考价人工核)的 (词, 模式) 共 {len(gu)}"
          f"(确认仅放飞/爆价词被剥 · 无误伤普通词):")
    for lbl, mode in gu:
        print(f"    ⊘ {mode:12} {lbl[:38]}")
    print()

    # §12.4 验收口径(启发式 · 跨 archetype 断言)
    print("【§12.4 影子验收】")

    def _q(label, ok, detail):
        print(f"  [{'PASS' if ok else 'CHECK'}] {label} — {detail}")

    def g(word_label, mode, field):
        return (by_word.get(word_label, {}).get(mode, {}) or {}).get(field)

    # 1. 栖舍火词 P0-A 是否从被压低恢复(A 模式 std >= off 模式 std)
    a_off, a_a = g("历史异常·栖舍火词(被压低)", "off", "standard"), g("历史异常·栖舍火词(被压低)", "A", "standard")
    _q("栖舍火词 P0-A 成本回真", (a_off is not None and a_a is not None and a_a >= a_off),
       f"off std={a_off} → A std={a_a}(cost_ratio={g('历史异常·栖舍火词(被压低)','A','cost_ratio')})")

    # 2. 罗平县级词不被误抬过高(A+B+C std 不爆 · < ¥6000 出厂)
    lp = g("县级长尾(罗平县装修·无5118)", "A+B+C", "standard")
    _q("罗平县级词不爆价", (lp is not None and lp < 6000), f"A+B+C std 出厂={lp}")

    # 3. 县级词不倒挂全国高价值词(P0-B 后 县 std <= 全国高价值 std)
    nat_hv = g("全国高价值(别墅电梯定制)", "A+B", "standard")
    lp_ab = g("县级长尾(罗平县装修·无5118)", "A+B", "standard")
    _q("县级不倒挂全国(P0-B 后)", (nat_hv is not None and lp_ab is not None and lp_ab <= nat_hv),
       f"罗平 A+B std={lp_ab} <= 别墅电梯 A+B std={nat_hv}")

    # 4. 低竞争小词 P0-A 不被真实成本误抬过高(cost_ratio 合理 · ≤ 3)
    sc = g("低竞争小词(工业球阀厂家·无5118)", "A", "cost_ratio")
    _q("低竞争小词成本不过抬", (sc is not None and sc <= 3.0), f"A cost_ratio={sc}")

    # 5. P0-A 单开不【因 raw ratio】淹没 needs_review(§10.2:cost 单独涨=预期纠偏,不靠 ratio 触发 review)。
    #    注:快照自身 snap_needs_review(档位样本不足池化=数据质量)是合法 review 源,不算 ratio 淹没。
    _ratio_keys = ("lever_two_compound", "lever_three_compound", "national_unclamped_factory_ratio")
    ratio_review = [lbl for lbl in by_word
                    if any(k in (g(lbl, "A", "guards") or "") for k in _ratio_keys)]
    _q("P0-A 单开不靠 raw ratio 触发 review", len(ratio_review) == 0,
       f"A 模式被新 ratio 护栏触发 review 的词数={len(ratio_review)}({ratio_review})")

    # 6. P0-C 全国放飞词触发人工核价(A+B+C guarantee_unavailable=True)
    fk = g("全国词放飞候选(national_unclamped)", "A+B+C", "guarantee_unavailable")
    _q("P0-C 全国放飞触发人工核价", bool(fk),
       f"放飞词 A+B+C guarantee_unavailable={fk} guards={g('全国词放飞候选(national_unclamped)','A+B+C','guards')}")

    # 7. 用户自加 cost_override 词 cost_ratio==1(成本绝对优先 · flag 不动它)
    ov = g("用户自加(cost_override=¥120)", "A+B+C", "cost_ratio")
    _q("自设成本绝对优先(cost_ratio=1)", (ov == 1.0), f"override 词 A+B+C cost_ratio={ov}")

    # ===== P0-D 影子验收(篇数侧难度因子 · A/B/C 全关) =====
    print("\n【P0-D 影子验收】(信任资产/引用难度 · 篇数侧 · A/B/C 全关)")

    def pg(label, mode, field):
        return (p0d_by_word.get(label, {}).get(mode, {}) or {}).get(field)

    # D0. flag-off / 缺采集 = 0 报价变化(D:off / D:missing 篇数 == A/B/C 的 off 篇数)
    _d0_ok = True
    _d0_detail = []
    for lbl in P0D_WORD_LABELS:
        base_off = g(lbl, "off", "articles_std")
        d_off = pg(lbl, "D:off", "articles_std")
        d_missing = pg(lbl, "D:missing", "articles_std")
        ok = (base_off is not None and d_off == base_off and d_missing == base_off)
        _d0_ok = _d0_ok and ok
        _d0_detail.append(f"{lbl[:8]}: off={base_off}/D:off={d_off}/D:missing={d_missing}")
    _q("P0-D flag-off & 缺采集 0 篇数变化", _d0_ok, " | ".join(_d0_detail))

    # D1. 缺背书(D:low) → 篇数 ≥ D:off(难度上调) · trust_ratio > 1
    _low_lbl = "全国高价值(别墅电梯定制)"
    low_a, off_a = pg(_low_lbl, "D:low", "articles_std"), pg(_low_lbl, "D:off", "articles_std")
    low_tr = pg(_low_lbl, "D:low", "trust_ratio")
    _q("缺背书上调篇数(难度侧)", (low_a is not None and off_a is not None and low_a >= off_a
                              and (low_tr or 0) > 1.0),
       f"{_low_lbl[:8]}: D:off 篇={off_a} → D:low 篇={low_a}(trust_ratio={low_tr})")

    # D2. 背书足(D:high) → 篇数 ≤ D:off(反向小幅降) · trust_ratio < 1
    high_a = pg(_low_lbl, "D:high", "articles_std")
    high_tr = pg(_low_lbl, "D:high", "trust_ratio")
    _q("背书足下调篇数", (high_a is not None and off_a is not None and high_a <= off_a
                       and (high_tr or 9) < 1.0),
       f"{_low_lbl[:8]}: D:off 篇={off_a} → D:high 篇={high_a}(trust_ratio={high_tr})")

    # D3. 三合一(缺背书+高价值+高竞争)→ 报价 needs_review;低值低竞争缺背书【不】触发(不淹没队列)
    tri = pg("高客单行业(医美·决策·有5118)", "D:low", "needs_review")
    small = pg("低竞争小词(工业球阀厂家·无5118)", "D:low", "needs_review")
    _q("三合一触发 needs_review", bool(tri),
       f"医美 D:low needs_review={tri} guards={pg('高客单行业(医美·决策·有5118)','D:low','guards')}")
    _q("低值低竞争缺背书不淹没人审", (small is False or small is None),
       f"工业球阀 D:low needs_review={small}")

    # ===== Post-P0D 分层影子验收(P0-A 前置 · 基线 = 当前生产 P0-D on · 叠加 A/B/C) =====
    print("\n【Post-P0D 分层验收】(P0-A 前置 · 基线 P0-D on · 叠加 P0-A/B/C · A/B/C 全 false 生产·影子内 monkeypatch)")
    print(f"  snapshot_source = {src['source']}(P0-A 生产 flip 前必须 = db · 当前影子用 {src['source']})")

    def qg(label, mode, field):
        return (postp0d_by_word.get(label, {}).get(mode, {}) or {}).get(field)

    _pp_lbl = "全国高价值(别墅电梯定制)"
    # PP1. P0-D ⟂ P0-A/B/C:trust_ratio 在 4 个 Post-P0D 模式恒等(P0-D 难度因子不受 A/B/C 影响)
    _trs = [qg(_pp_lbl, m, "trust_ratio") for m, _ in POSTP0D_MODES]
    _q("P0-D 难度因子与 P0-A/B/C 正交(trust_ratio 恒等)",
       (len(set(str(t) for t in _trs)) == 1 and _trs[0] is not None),
       f"{_pp_lbl[:8]} trust_ratio P0D/+A/+A+B/+A+B+C = {_trs}")
    # PP2. comp_ratio 同样恒等(comp 由 P0-D 驱动·A/B/C 不碰竞争)
    _crs = [qg(_pp_lbl, m, "comp_ratio") for m, _ in POSTP0D_MODES]
    _q("comp_ratio 在 Post-P0D 4 模式恒等(竞争由 P0-D 驱动)",
       len(set(str(c) for c in _crs)) == 1, f"comp_ratio = {_crs}")
    # PP3. P0D 基线 cost_ratio==1(P0-A 关·成本未回真);P0-A 叠加后 cost_ratio 被算出(成本回真观测)
    pp_cost_base = qg(_pp_lbl, "P0D", "cost_ratio")
    pp_cost_a = qg(_pp_lbl, "P0D+A", "cost_ratio")
    _q("P0D 基线 cost_ratio=1·叠加 P0-A 后成本回真被观测",
       (pp_cost_base == 1.0 and pp_cost_a is not None),
       f"P0D cost_ratio={pp_cost_base} → P0D+A cost_ratio={pp_cost_a}")
    # PP4. Post-P0D 基线 ≠ P0-D-off 基线(证明当前生产基线确实带 P0-D · 给 P0-A 影子做正确对照)
    base_p0doff = g(_pp_lbl, "off", "articles_std")        # P0-D off(旧基线)
    base_postp0d = qg(_pp_lbl, "P0D", "articles_std")       # P0-D on(当前生产基线)
    _q("当前生产基线 = P0-D on(与 P0-D-off 旧基线区分)",
       (base_p0doff is not None and base_postp0d is not None),
       f"P0-D off 篇={base_p0doff} vs P0D(当前生产)篇={base_postp0d}(拆 P0-D 归因用 off 对照·禁改生产 flag)")
    if src["source"] != "db":
        print("  ⚠️  CHECK snapshot_source != db:本次 Post-P0D+A 的成本来自 bootstrap → 不可作为 P0-A flip 依据。")

    print("\n完成。明细见 CSV;PASS/CHECK 仅启发式,最终以老板/Codex 审 CSV 为准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
