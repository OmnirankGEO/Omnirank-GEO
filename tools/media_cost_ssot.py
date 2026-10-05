"""
媒体成本 SSOT (P0-0 数据地基 · 2026-06-13)

报价底价的唯一权威来源 + 版本化快照 + 字段映射 fail-closed 核验。
本模块【不改任何生产报价公式】,只建立可审计、可版本化的成本数据源 + 读取契约。

────────────────────────────────────────────────────────────────────────
为什么用 `mhz_media.price` 作底价 —— 代码实证,而非 "sivp 语义" 假设
────────────────────────────────────────────────────────────────────────
代发真实扣费 `_recompute_publish_charge`(api/meijiehezi_api.py:246,262,265)直接
`SELECT price FROM mhz_media WHERE is_active=TRUE` 取 base_yuan(外采价/成本),
按 `ceil(price × markup × 130)` 扣分(markup = mhz_config.markup_ratio,默认 1.5)。
→ `price` 列【就是】平台真实采买成本列。GEO 报价复用同一列 = 与代发同源 SSOT。

────────────────────────────────────────────────────────────────────────
字段映射(三方 · 2026-06-13 Deploy-CTO prod 只读实证)
────────────────────────────────────────────────────────────────────────
  原始 API JSON 字段名 :  price / price1 / price2   (无中文标签)
  DB 列                :  price / price1 / price2   (api/meijiehezi_api.py:_map_media_to_db 逐字拷)
  Excel 人工导出列     :  普通会员 / vip / sivp

  抽样实证(case_link 匹配,3 家):DB `price` 值 == Excel "sivp" 列值;`price1` == "vip" 列值;
  Excel "普通会员"(零售挂牌价)未进任何 DB price 列。
  一致性不变量:`price == price2`(prod 412/412 GEO + 16668/16668 active 全等)。

  ⚠️ "price == sivp 语义" 仅为 Excel 标签下的【假设】,非证明。本模块【不依赖】该标签:
     用 `price` 的唯一理由 = "它是代发外采成本列"(扣费代码已证)。`price == price2`
     仅作 drift 一致性检查(上游若改列序会触发 fail-closed)。

────────────────────────────────────────────────────────────────────────
覆盖范围(显式)
────────────────────────────────────────────────────────────────────────
  - GEO 成本档位【只覆盖 mhz_media】软文 GEO 池(geo_rank>0 AND is_active = 412 家 @2026-06-13)。
  - mhz_wemedia(自媒体 · GEO 池 463 家)仅作【信息块 wemedia_geo】记录,【不并入】GEO 成本档位。
    是否把自媒体折进 E/social 档 = 待老板拍板(P0A 决策),P0-0 不决定。

────────────────────────────────────────────────────────────────────────
档位分类【刻意不使用 price】(避免 price→质量→price 循环)
────────────────────────────────────────────────────────────────────────
  仅用 authority_media + geo 引擎覆盖数(geo_rank_platform 中 a-f 计数) + portal_media 白名单。
  有意区别于 meijiehezi_db._GEO_AUTHORITY_SCORE_EXPR(后者含 `price BETWEEN ...` 档加分 → 循环)。

────────────────────────────────────────────────────────────────────────
陈旧(stale)契约
────────────────────────────────────────────────────────────────────────
  快照 schema_version 不符 / 超龄(COST_SNAPSHOT_MAX_AGE_DAYS)→ is_stale()=True。
  未来报价消费方【必须】对 stale 走 fail-closed 或 needs_review(本批无消费方 · 契约 + 测试先行)。

关联文档:qa-artifacts/PRICING_P0_0_DATA_VERIFICATION_2026-06-13.md
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("GEO-MediaCostSSOT")

# ============================================================
# 常量(schema/方法学版本 · 与单次构建的内容版本区分)
# ============================================================

COST_SNAPSHOT_SCHEMA_VERSION = "mcs_v1"          # 档位模型/方法学变更时 bump
COST_SNAPSHOT_MAX_AGE_DAYS = 30                  # 超龄即 stale
_MIN_TIER_SAMPLE = 8                             # 档位样本下限;不足则回退更宽维度 + needs_review
_GEO_ENGINE_LETTERS = "abcdef"                   # geo_rank_platform 中计入覆盖的引擎位(z 不计)
_PERCENTILES = ("p50", "p75", "p90", "p95", "p99")

# portal_media 白名单(与 meijiehezi_db._GEO_AUTHORITY_SCORE_EXPR 同一组主门户;此处仅作质量信号,不取其 price 档加分)
_PORTAL_WHITELIST = frozenset([
    "网易网", "新华网", "人民网", "光明网", "凤凰网", "央视网",
    "新浪网", "中华网", "环球网", "国际在线", "中国广播网", "中国网",
])

_BOOTSTRAP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "media_cost_snapshot.json",
)

# legacy S/A/B/C/D/E/social 档 → 快照维度选择器。
# ⚠️ 这是 P0-A / 老板【待校准】的 seam:真实 ¥ 永远来自 snapshot,这里【不写死任何金额】。
#   authority=None 表示不按 authority 区分,走 bucket 池化值。
_TIER_TO_SNAPSHOT_SELECTOR: Dict[str, Dict[str, Any]] = {
    "S":      {"authority": 1,    "bucket": "high", "percentile": "p90"},
    "A":      {"authority": 1,    "bucket": "mid",  "percentile": "p90"},
    "B":      {"authority": None, "bucket": "low",  "percentile": "p75"},
    "C":      {"authority": 0,    "bucket": "mid",  "percentile": "p50"},
    "D":      {"authority": 0,    "bucket": "low",  "percentile": "p75"},
    "E":      {"authority": 0,    "bucket": "low",  "percentile": "p50"},
    "social": {"authority": 0,    "bucket": "low",  "percentile": "p50"},
}


class MediaCostMappingError(Exception):
    """字段映射 / 数据核验失败 —— fail-closed,绝不继续派生成本。"""


# ============================================================
# 纯函数:分类 + 分布(无 DB · 完全可单测)
# ============================================================

def geo_engine_coverage(geo_rank_platform: Optional[str]) -> int:
    """geo_rank_platform(逗号分隔字母,如 'a,b,c,d,e,f' / 'a,e')→ 计入 a-f 的去重引擎数(z 不计)。"""
    if not geo_rank_platform:
        return 0
    letters = {c.strip().lower() for c in str(geo_rank_platform).split(",")}
    return sum(1 for ch in _GEO_ENGINE_LETTERS if ch in letters)


def coverage_bucket(coverage: int) -> str:
    if coverage >= 5:
        return "high"
    if coverage >= 3:
        return "mid"
    return "low"


def classify_media_tier(
    *,
    authority_media: int = 0,
    geo_rank_platform: Optional[str] = "",
    portal_media: str = "",
    area: str = "",
) -> Dict[str, Any]:
    """从【非 price】字段推媒体质量档。

    刻意不接收 price —— 防止 "price → 质量 → 反推 price" 的循环。
    """
    cov = geo_engine_coverage(geo_rank_platform)
    auth = 1 if int(authority_media or 0) == 1 else 0
    return {
        "authority": auth,
        "coverage": cov,
        "coverage_bucket": coverage_bucket(cov),
        # portal_major:【P0-0 不参与定价分档】。tier_key 只用 authority + coverage_bucket。
        #   此字段仅作信息暴露,为未来"门户权威定价维度"预留;若 P0-A 决定纳入,
        #   需显式扩展 tier_key/by_tier 分组 + 老板拍板(本批不擅自加维度)。
        "portal_major": (portal_media or "") in _PORTAL_WHITELIST,
        "tier_key": _tier_key(auth, coverage_bucket(cov)),
    }


def _tier_key(authority: int, bucket: str) -> str:
    return f"auth{authority}_{bucket}"


def _percentile_cont(sorted_vals: Sequence[float], p: float) -> float:
    """PostgreSQL percentile_cont 等价(线性插值 / numpy type-7),与 prod 取数口径一致。"""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_vals[0])
    rank = p * (n - 1)
    lo = int(rank)
    frac = rank - lo
    if lo + 1 >= n:
        return float(sorted_vals[-1])
    return float(sorted_vals[lo]) + frac * (float(sorted_vals[lo + 1]) - float(sorted_vals[lo]))


def _dist(prices: Sequence[float]) -> Dict[str, Any]:
    """对一组价格出分布(已剔除非正值)。空 → n=0 占位(消费方靠 needs_review 兜底)。"""
    vals = sorted(float(v) for v in prices if v is not None and float(v) > 0)
    if not vals:
        return {"n": 0}
    out: Dict[str, Any] = {
        "n": len(vals),
        "min": round(vals[0], 2),
        "max": round(vals[-1], 2),
        "avg": round(sum(vals) / len(vals), 1),
    }
    for label, p in (("p50", 0.5), ("p75", 0.75), ("p90", 0.9), ("p95", 0.95), ("p99", 0.99)):
        out[label] = round(_percentile_cont(vals, p), 2)
    return out


# ============================================================
# 纯函数:构建 snapshot payload(给定已取数据 · 无 DB)
# ============================================================

def build_snapshot_payload(
    *,
    geo_rows: Sequence[Dict[str, Any]],
    platform_media_markup: float,
    mapping_report: Dict[str, Any],
    generated_at: datetime,
    source: str,
    wemedia_geo_rows: Optional[Sequence[Dict[str, Any]]] = None,
    row_count_total: Optional[int] = None,
    active_count: Optional[int] = None,
) -> Dict[str, Any]:
    """geo_rows: 每行须含 price / authority_media / geo_rank_platform(portal_media/area 可选)。

    只统计 price>0 的行;price<=0/NULL 计入 excluded_nonpositive 而非污染分布(绝不产出 0 成本)。
    """
    valid: List[Dict[str, Any]] = []
    excluded = 0
    for r in geo_rows:
        price = r.get("price")
        try:
            price = float(price) if price is not None else 0.0
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            excluded += 1
            continue
        cov = geo_engine_coverage(r.get("geo_rank_platform"))
        valid.append({
            "price": price,
            "authority": 1 if int(r.get("authority_media") or 0) == 1 else 0,
            "bucket": coverage_bucket(cov),
        })

    # 分组聚合(精确档 / bucket 池化 / authority 池化 / 总体)
    by_tier: Dict[str, Any] = {}
    for auth in (0, 1):
        for b in ("low", "mid", "high"):
            sub = [v["price"] for v in valid if v["authority"] == auth and v["bucket"] == b]
            by_tier[_tier_key(auth, b)] = _dist(sub)
    for b in ("low", "mid", "high"):
        by_tier[f"bucket_{b}"] = _dist([v["price"] for v in valid if v["bucket"] == b])
    for auth in (0, 1):
        by_tier[f"auth{auth}"] = _dist([v["price"] for v in valid if v["authority"] == auth])

    overall = _dist([v["price"] for v in valid])

    wemedia_geo: Dict[str, Any] = {
        "note": "informational only — NOT part of GEO cost tiers; folding 自媒体 into E/social 档 is pending boss decision (P0A)",
        "dist": _dist([float(r.get("price") or 0) for r in (wemedia_geo_rows or [])]),
    }

    payload: Dict[str, Any] = {
        "schema_version": COST_SNAPSHOT_SCHEMA_VERSION,
        "source": source,
        "generated_at": generated_at.isoformat(),
        "price_column": "price",
        "price_semantics": (
            "procurement cost (外采价) — the column 代发 bills off via "
            "_recompute_publish_charge(api/meijiehezi_api.py:246,262,265). "
            "Matches Excel 'sivp' column in sampled media (hypothesis: super-VIP tier), NOT proven beyond sample."
        ),
        "platform_media_markup_ratio": round(float(platform_media_markup), 3),
        "row_count_total": row_count_total,
        "active_count": active_count,
        "geo_count": len(valid),
        "excluded_nonpositive": excluded,
        "overall": overall,
        "by_tier": by_tier,
        "wemedia_geo": wemedia_geo,
        "field_mapping_check": mapping_report,
        "tier_selector": _TIER_TO_SNAPSHOT_SELECTOR,
    }

    # 内容寻址版本(builder 与 bootstrap 共用 finalize_snapshot):同内容 → 同 version(幂等 no-op);
    # 不同内容 → 不同 version(append-only)。杜绝"同 version 静默覆盖不同内容"。
    return finalize_snapshot(payload)


def _payload_sha256(payload: Dict[str, Any]) -> str:
    """对 payload 的稳定 hash —— 排除 generated_at / 已有的 version/hash 字段。"""
    clean = {k: v for k, v in payload.items()
             if k not in ("generated_at", "snapshot_version", "payload_sha256")}
    canon = json.dumps(clean, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def finalize_snapshot(payload: Dict[str, Any]) -> Dict[str, Any]:
    """统一的内容寻址版本戳 —— builder 与 bootstrap 共用这一个函数,
    保证 snapshot_version / payload_sha256 永远是【真实内容 hash】,而非手写占位。
    要求 payload['generated_at'] 已为 ISO 字符串。
    """
    sha = _payload_sha256(payload)
    try:
        date_str = datetime.fromisoformat(payload["generated_at"]).strftime("%Y-%m-%d")
    except (KeyError, ValueError, TypeError):
        date_str = "undated"
    payload["payload_sha256"] = sha
    payload["snapshot_version"] = f"{COST_SNAPSHOT_SCHEMA_VERSION}_{date_str}_{sha[:8]}"
    return payload


def compare_to_bootstrap(candidate: Dict[str, Any], bootstrap: Dict[str, Any],
                         tolerance: float = 0.01) -> Dict[str, Any]:
    """核心字段逐项比对(geo_count / 平台系数 / overall + by_tier 各百分位)。

    返回 {ok, diffs, tolerance}。ok=True 表示所有核心字段相对偏差 ≤ tolerance。
    供 build CLI 的 --compare-bootstrap / --strict-bootstrap 使用(把"dry-run 比对"落成代码)。
    """
    diffs: List[Dict[str, Any]] = []

    def chk(field: str, a, b):
        """数值字段:相对偏差 > tolerance 记差异。"""
        if a is None or b is None:
            if a != b:
                diffs.append({"field": field, "candidate": a, "bootstrap": b, "rel_delta": None})
            return
        denom = abs(float(b)) if b else 1.0
        d = abs(float(a) - float(b)) / denom
        if d > tolerance:
            diffs.append({"field": field, "candidate": a, "bootstrap": b, "rel_delta": round(d, 4)})

    def chk_eq(field: str, a, b):
        """精确相等字段(schema/列名等):不等即记差异。"""
        if a != b:
            diffs.append({"field": field, "candidate": a, "bootstrap": b, "rel_delta": None})

    # schema / 语义字段(精确相等)
    chk_eq("schema_version", candidate.get("schema_version"), bootstrap.get("schema_version"))
    chk_eq("price_column", candidate.get("price_column"), bootstrap.get("price_column"))
    # 计数 / 系数 / 总体分布(数值容差)
    chk("geo_count", candidate.get("geo_count"), bootstrap.get("geo_count"))
    chk("platform_media_markup_ratio",
        candidate.get("platform_media_markup_ratio"), bootstrap.get("platform_media_markup_ratio"))
    chk("overall.n", candidate.get("overall", {}).get("n"), bootstrap.get("overall", {}).get("n"))
    for p in _PERCENTILES:
        chk(f"overall.{p}", candidate.get("overall", {}).get(p), bootstrap.get("overall", {}).get(p))

    bt_c = candidate.get("by_tier", {})
    bt_b = bootstrap.get("by_tier", {})
    # 档位 key 缺失 / 新增(结构漂移)—— 不能只比交集,否则 --strict 漏报
    for k in sorted(set(bt_b) - set(bt_c)):
        diffs.append({"field": f"by_tier.{k}", "candidate": "MISSING", "bootstrap": "present", "rel_delta": None})
    for k in sorted(set(bt_c) - set(bt_b)):
        diffs.append({"field": f"by_tier.{k}", "candidate": "present", "bootstrap": "MISSING", "rel_delta": None})
    for key in sorted(set(bt_c) & set(bt_b)):
        chk(f"by_tier.{key}.n", bt_c[key].get("n"), bt_b[key].get("n"))
        for p in _PERCENTILES:
            chk(f"by_tier.{key}.{p}", bt_c[key].get(p), bt_b[key].get(p))
    return {"ok": not diffs, "diffs": diffs, "tolerance": tolerance}


def load_excel_anchors(excel_path: str, limit: int = 40) -> List[Dict[str, Any]]:
    """从媒介盒子 Excel 导出表自动派生 Excel↔DB 锚点(可发GEO=1 行)。

    这是"稳定可重复"的三方核验数据源:expect_sivp/expect_vip 来自 Excel(外部真值),
    无任何硬编码魔数。跨价位段均匀采样至多 limit 条以限制查询量。
    """
    from openpyxl import load_workbook
    wb = load_workbook(excel_path, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        hdr = [str(h).strip() if h is not None else "" for h in next(it)]
        need = ("案例链接", "vip", "sivp", "可发GEO")
        if any(c not in hdr for c in need):
            raise MediaCostMappingError(f"Excel 表头缺列(需 {need}),实际: {hdr}")
        i_link, i_vip, i_sivp, i_geo = (hdr.index(c) for c in need)
        rows: List[Dict[str, Any]] = []
        for r in it:
            if str(r[i_geo]).strip() != "1":
                continue
            link = str(r[i_link] or "")
            if not link or "?" in link:
                continue
            try:
                sivp = float(r[i_sivp]); vip = float(r[i_vip])
            except (TypeError, ValueError):
                continue
            sub = link.replace("https://", "").replace("http://", "").split("?")[0]
            rows.append({"case_link": sub, "expect_sivp": sivp, "expect_vip": vip})
    finally:
        wb.close()
    rows.sort(key=lambda a: a["expect_sivp"])
    if limit and len(rows) > limit:
        step = len(rows) / limit
        rows = [rows[int(i * step)] for i in range(limit)]
    return rows


# ============================================================
# 纯函数:字段映射 fail-closed 核验
# ============================================================

def verify_field_mapping(
    *,
    price_eq_price2_ratio: float,
    geo_count: int,
    zero_price_count: int,
    anchors: Sequence[Dict[str, Any]],
    consistency_min_ratio: float = 0.995,
    geo_count_band: tuple = (200, 800),
    zero_price_max_ratio: float = 0.01,
) -> Dict[str, Any]:
    """三方映射核验(纯函数 · 给定已取统计)。任一违例 → raise MediaCostMappingError(fail-closed)。

    anchors: 每条 {case_link, db_price, db_price1, expect_sivp, expect_vip} —— Excel↔DB 锚点。
      DB price 须等于 Excel sivp、price1 须等于 Excel vip;缺失(上游下架)可跳过,但不可不匹配。
    """
    problems: List[str] = []

    if price_eq_price2_ratio < consistency_min_ratio:
        problems.append(
            f"price==price2 一致性 {price_eq_price2_ratio:.4f} < {consistency_min_ratio} "
            f"(上游列序可能漂移 · 拒绝派生成本)")

    lo, hi = geo_count_band
    if not (lo <= geo_count <= hi):
        problems.append(f"GEO 池规模 {geo_count} 不在合理区间 [{lo},{hi}]")

    if geo_count > 0 and (zero_price_count / max(geo_count, 1)) > zero_price_max_ratio:
        problems.append(
            f"零/负价占比 {zero_price_count}/{geo_count} 超 {zero_price_max_ratio:.1%}")

    anchors_checked = 0
    for a in anchors:
        if a.get("db_price") is None:  # 上游下架 → 跳过(不算违例)
            continue
        anchors_checked += 1
        if a.get("expect_sivp") is not None and round(float(a["db_price"]), 2) != round(float(a["expect_sivp"]), 2):
            problems.append(
                f"锚点 {a.get('case_link','?')}: DB price={a['db_price']} != Excel sivp={a['expect_sivp']} "
                f"(列映射漂移 — price 不再对应 sivp)")
        if a.get("db_price1") is not None and a.get("expect_vip") is not None \
                and round(float(a["db_price1"]), 2) != round(float(a["expect_vip"]), 2):
            problems.append(
                f"锚点 {a.get('case_link','?')}: DB price1={a['db_price1']} != Excel vip={a['expect_vip']}")

    if anchors and anchors_checked == 0:
        problems.append("所有 Excel↔DB 锚点媒体均缺失 — 无法核验三方映射(fail-closed)")

    if problems:
        raise MediaCostMappingError("; ".join(problems))

    return {
        "verified": True,
        "price_eq_price2_ratio": round(float(price_eq_price2_ratio), 4),
        "geo_count": geo_count,
        "zero_price_count": zero_price_count,
        "anchors_checked": anchors_checked,
        "note": "price==price2 是一致性检查;'price==sivp' 由 Excel 锚点交叉证据支持(假设),用 price 的依据是代发扣费代码",
    }


# ============================================================
# 读取契约:MediaCostSnapshot
# ============================================================

@dataclass
class MediaCostSnapshot:
    snapshot_version: str
    schema_version: str
    source: str
    generated_at: str
    geo_count: int
    platform_media_markup: float
    overall: Dict[str, Any]
    by_tier: Dict[str, Any]
    wemedia_geo: Dict[str, Any]
    price_column: str
    price_semantics: str
    raw: Dict[str, Any] = field(default_factory=dict)
    # [v2.3 DELTA 1] 加载通道(load_media_cost_snapshot 写)· 'db' / 'bootstrap' / ''(直接 from_payload)。
    #   ⚠️ 区别于 payload['source'](= 快照【构建】来源字符串)· 此字段记录【本次 load 实际命中】DB 还是 bootstrap。
    source_channel: str = ""

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "MediaCostSnapshot":
        return cls(
            snapshot_version=payload.get("snapshot_version", "unknown"),
            schema_version=payload.get("schema_version", "unknown"),
            source=payload.get("source", ""),
            generated_at=payload.get("generated_at", ""),
            geo_count=int(payload.get("geo_count", 0) or 0),
            platform_media_markup=float(payload.get("platform_media_markup_ratio", 2.0) or 2.0),
            overall=payload.get("overall", {}),
            by_tier=payload.get("by_tier", {}),
            wemedia_geo=payload.get("wemedia_geo", {}),
            price_column=payload.get("price_column", "price"),
            price_semantics=payload.get("price_semantics", ""),
            raw=payload,
        )

    def is_stale(self, now: Optional[datetime] = None) -> bool:
        if self.schema_version != COST_SNAPSHOT_SCHEMA_VERSION:
            return True
        try:
            gen = datetime.fromisoformat(self.generated_at)
        except (ValueError, TypeError):
            return True
        now = now or datetime.now(gen.tzinfo)
        return (now - gen) > timedelta(days=COST_SNAPSHOT_MAX_AGE_DAYS)

    def _lookup(self, key: str, percentile: str) -> Optional[float]:
        stats = self.by_tier.get(key) or {}
        if int(stats.get("n", 0) or 0) >= _MIN_TIER_SAMPLE and percentile in stats:
            return float(stats[percentile])
        return None

    def real_cost_for(
        self,
        *,
        authority: Optional[int],
        coverage_bucket: str,
        percentile: str = "p75",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """真实底价查询。回退链:精确档 → bucket 池化 → authority 池化 → 总体。

        - 永不返回 0 / 全局最小作为"默认低价";数据不足/陈旧一律 needs_review=True。
        - stale 快照 → 强制 needs_review=True(消费方须 fail-closed 或人工复核)。
        - 全无数据 → cost=None + confidence='no_data'(消费方必须 fail-closed)。
        """
        stale = self.is_stale(now)
        chain: List[tuple] = []
        if authority is not None:
            chain.append((_tier_key(authority, coverage_bucket), "exact"))
        chain.append((f"bucket_{coverage_bucket}", "bucket_pooled"))
        if authority is not None:
            chain.append((f"auth{authority}", "authority_pooled"))
        chain.append(("__overall__", "overall"))

        for idx, (key, conf) in enumerate(chain):
            cost = (self.overall.get(percentile) if key == "__overall__"
                    else self._lookup(key, percentile))
            if cost is not None and cost > 0:
                # [tier-B P0-A flip 修 2026-06-15] needs_review 判据:从"conf != exact"改为
                #   "是否命中 selector 首选档(回退链首项 idx==0)"。
                #   链首项 = selector 设计的首选维度:
                #     · authority is not None(S/A/C/D/E)→ 精确档 exact;
                #     · authority is None(tier-B)→ 设计性 bucket 池化(bucket_<b>·样本由 _lookup
                #       强制 ≥ _MIN_TIER_SAMPLE 才命中,故命中即样本充足,不是缺数据 fallback)。
                #   命中首选档(idx==0)= 不是降级 → 不强制 needs_review;
                #   命中 idx>0 = 真降级(请求档不存在/样本不足,退到更宽 bucket/authority/overall)→ needs_review。
                #   保留:stale 恒 needs_review;全无数据 → 下方单独返回 needs_review=True。
                #   ⚠️ 只放行"设计性首选 bucket"(authority=None 的链首);"请求 exact 却退到 bucket"
                #      (idx>0 的 bucket_pooled)仍 needs_review=True —— 不放行所有 bucket_pooled。
                hit_primary = (idx == 0)
                needs_review = stale or (not hit_primary)
                return {
                    "cost": round(float(cost), 2),
                    "source": self.snapshot_version,
                    "confidence": ("stale" if stale else conf),
                    "needs_review": needs_review,
                    "tier_key": key,
                    "percentile": percentile,
                }

        return {
            "cost": None,
            "source": self.snapshot_version,
            "confidence": "no_data",
            "needs_review": True,
            "tier_key": None,
            "percentile": percentile,
        }

    def media_tier_cost(self, tier_letter: str, percentile: Optional[str] = None,
                        now: Optional[datetime] = None) -> Dict[str, Any]:
        """legacy S/A/B/C/D/E/social → 真实底价(经 _TIER_TO_SNAPSHOT_SELECTOR)。

        这是 batch-2 把 MEDIA_TIER_COSTS[tier] 换成真实快照的 seam(本批无人调用)。
        """
        sel = _TIER_TO_SNAPSHOT_SELECTOR.get(tier_letter)
        if not sel:
            return {"cost": None, "confidence": "unknown_tier", "needs_review": True,
                    "tier_key": None, "source": self.snapshot_version}
        return self.real_cost_for(
            authority=sel["authority"],
            coverage_bucket=sel["bucket"],
            percentile=percentile or sel["percentile"],
            now=now,
        )


# ============================================================
# DB 边界(薄封装 · 生产用;单测走上面的纯函数)
# ============================================================

def _fetch_geo_rows(cur) -> List[Dict[str, Any]]:
    cur.execute(
        "SELECT price, authority_media, geo_rank_platform, portal_media, area "
        "FROM mhz_media WHERE geo_rank>0 AND is_active"
    )
    return [dict(r) for r in cur.fetchall()]


def _fetch_wemedia_geo_rows(cur) -> List[Dict[str, Any]]:
    try:
        cur.execute("SELECT price FROM mhz_wemedia WHERE geo_rank>0 AND is_active")
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def verify_field_mapping_db(conn=None, anchors: Optional[Sequence[Dict[str, Any]]] = None,
                            excel_path: Optional[str] = None) -> Dict[str, Any]:
    """prod/DB 版字段映射核验。读取统计 + 锚点后调纯函数 verify_field_mapping。

    anchors 解析优先级:显式 anchors > excel_path(自动派生全表锚点) > _DEFAULT_ANCHORS(9 个 prod 实证)。
    传 excel_path(docs/媒体成本 表)= 稳定可重复的三方核验(expect 值来自 Excel 外部真值,无硬编码)。
    """
    from db.connection import get_connection
    if anchors is None:
        if excel_path:
            anchors = load_excel_anchors(excel_path)
            if not anchors:
                # 显式指定 Excel 做三方核验,却派生出 0 个可用锚点(无 可发GEO=1 且 case_link 稳定的行)
                # → 文件错/空/格式变 → 无法核验,fail-closed,绝不"无锚点静默通过"。
                raise MediaCostMappingError(
                    f"excel_path={excel_path} 派生出 0 个可用锚点(无 可发GEO=1 + 稳定 case_link 行)"
                    f" — 无法做三方映射核验,fail-closed")
        else:
            anchors = _DEFAULT_ANCHORS
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) AS n, "
            "count(*) FILTER (WHERE price=price2) AS eq, "
            "count(*) FILTER (WHERE price IS NULL OR price<=0) AS zero "
            "FROM mhz_media WHERE geo_rank>0 AND is_active"
        )
        row = cur.fetchone()
        n = int(row["n"]); eq = int(row["eq"]); zero = int(row["zero"])
        anchor_results = []
        for a in anchors:
            cur.execute(
                "SELECT price, price1 FROM mhz_media WHERE case_link LIKE %s ORDER BY id LIMIT 1",
                (f"%{a['case_link']}%",),
            )
            r = cur.fetchone()
            anchor_results.append({
                "case_link": a["case_link"],
                "db_price": float(r["price"]) if r else None,
                "db_price1": float(r["price1"]) if r else None,
                "expect_sivp": a["expect_sivp"],
                "expect_vip": a["expect_vip"],
            })
        return verify_field_mapping(
            price_eq_price2_ratio=(eq / n if n else 0.0),
            geo_count=n,
            zero_price_count=zero,
            anchors=anchor_results,
        )
    finally:
        if close:
            conn.close()


# Excel↔DB 锚点(always-on fallback · 完整核验请用 --excel-path)
#   全部 2026-06-13 prod 实证:DB price==Excel sivp · price1==Excel vip。
#   跨价位段 ¥15→¥1000 + 地域/全国 + 多门户,降低单点失效风险。
_DEFAULT_ANCHORS = [
    {"case_link": "sjzyzjt.net/2026/cj_0609/40927",                    "expect_sivp": 15.0,   "expect_vip": 18.0},    # 燕赵新闻网(河北·地域)
    {"case_link": "lfcmw.com/rdzt/content/2026-06/09/content_1002032", "expect_sivp": 30.0,   "expect_vip": 35.0},    # 廊坊传媒网(河北·地域)
    {"case_link": "cool-de.com/thread-2310069",                        "expect_sivp": 90.0,   "expect_vip": 95.0},    # 室内设计联盟(全国)
    {"case_link": "baby.ifeng.com/c/8toyqKJZMLT",                      "expect_sivp": 110.0,  "expect_vip": 120.0},   # 凤凰网资讯
    {"case_link": "biz.ifeng.com/c/8tpACv8KOra",                       "expect_sivp": 170.0,  "expect_vip": 180.0},   # 凤凰网商业
    {"case_link": "cpnn.com.cn/qiye/jishu2023/202605/t20260512_1886952", "expect_sivp": 180.0, "expect_vip": 200.0},  # 中国能源新闻网
    {"case_link": "finance.ifeng.com/c/8tpACv8KOrZ",                   "expect_sivp": 200.0,  "expect_vip": 210.0},   # 凤凰网财经
    {"case_link": "linkshop.com/news/2023498617",                      "expect_sivp": 597.0,  "expect_vip": 650.0},   # 联商网
    {"case_link": "tmtpost.com/nictation/7993872",                     "expect_sivp": 1000.0, "expect_vip": 1100.0},  # 钛媒体首页文字链
]


def build_snapshot_from_db(conn=None, generated_at: Optional[datetime] = None,
                           excel_path: Optional[str] = None) -> Dict[str, Any]:
    """生产构建:取数 + markup + 映射核验 → build_snapshot_payload。

    excel_path 透传给 verify_field_mapping_db(给定则用 Excel 全表锚点做三方核验)。
    """
    from db.connection import get_connection
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        cur = conn.cursor()
        mapping_report = verify_field_mapping_db(conn=conn, excel_path=excel_path)  # 先核验,fail-closed
        geo_rows = _fetch_geo_rows(cur)
        wm_rows = _fetch_wemedia_geo_rows(cur)
        cur.execute("SELECT count(*) AS t, count(*) FILTER (WHERE is_active) AS a FROM mhz_media")
        cnt = cur.fetchone()
        try:
            from db.meijiehezi_db import get_config
            markup = float(get_config("markup_ratio") or 2.0)
        except Exception:
            markup = 2.0
        return build_snapshot_payload(
            geo_rows=geo_rows,
            platform_media_markup=markup,
            mapping_report=mapping_report,
            generated_at=generated_at or datetime.now(),
            source="prod:omnirank-db.mhz_media (geo_rank>0 AND is_active)",
            wemedia_geo_rows=wm_rows,
            row_count_total=int(cnt["t"]),
            active_count=int(cnt["a"]),
        )
    finally:
        if close:
            conn.close()


def load_media_cost_snapshot(conn=None, json_path: Optional[str] = None) -> MediaCostSnapshot:
    """最新快照:优先 DB 表 media_cost_snapshot(latest active),回落 committed bootstrap JSON。

    两者皆无 → MediaCostMappingError(消费方 fail-closed)。
    """
    # 1) DB 表
    try:
        from db.connection import get_connection
        close = False
        if conn is None:
            conn = get_connection()
            close = True
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT payload FROM media_cost_snapshot WHERE is_active=TRUE "
                "ORDER BY generated_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                payload = row["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                snap = MediaCostSnapshot.from_payload(payload)
                snap.source_channel = "db"
                return snap
        finally:
            if close:
                conn.close()
    except Exception as exc:
        logger.warning("media_cost_snapshot DB 读取失败,回落 bootstrap JSON: %s", exc)

    # 2) committed bootstrap JSON
    path = json_path or _BOOTSTRAP_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            snap = MediaCostSnapshot.from_payload(json.load(f))
            snap.source_channel = "bootstrap"
            return snap
    except Exception as exc:
        raise MediaCostMappingError(f"无可用媒体成本快照(DB 与 bootstrap 皆失败): {exc}")


# ============================================================
# [v2.3 DELTA 1 + 5] 快照来源探针 + P0-A 生产开关验收 gate
#   现有接口事实:load_media_cost_snapshot() DB 无 active row 时【静默回落 bootstrap】。
#   → 生产开 P0-A 前若 media_cost_snapshot 表仍空,系统"看起来能算"实际用的是随代码提交的
#     bootstrap,不是生产 DB 最新快照。本组探针/gate 把"是否真用 DB active snapshot"显式落成代码。
#   ⚠️ 【不改】load_media_cost_snapshot 默认 bootstrap fallback(本地单测/flags-off 旁路依赖它)。
#     fail-closed 只发生在【显式调用 assert_db_active_snapshot 的验收场景】。
# ============================================================

def resolve_snapshot_source(conn=None, json_path: Optional[str] = None) -> Dict[str, Any]:
    """验收专用探针:报告当前媒体成本快照的【真实加载来源】,绝不抛、绝不改 load 默认行为。

    返回 {source: 'db'|'bootstrap'|'none', snapshot_version, payload_sha256, generated_at,
          geo_count, schema_version, has_db_active_row}。
      - DB 有 is_active row → source='db'(生产合格态);
      - 否则 bootstrap JSON 可读 → source='bootstrap'(P0-A 验收【不合格】= 没写库);
      - 皆无 → source='none'。
    """
    info: Dict[str, Any] = {
        "source": "none", "snapshot_version": None, "payload_sha256": None,
        "generated_at": None, "geo_count": None, "schema_version": None,
        "has_db_active_row": False,
    }

    def _fill(payload: Dict[str, Any], source: str) -> None:
        info["source"] = source
        info["snapshot_version"] = payload.get("snapshot_version")
        info["payload_sha256"] = payload.get("payload_sha256")
        info["generated_at"] = payload.get("generated_at")
        info["geo_count"] = payload.get("geo_count")
        info["schema_version"] = payload.get("schema_version")

    # 1) DB active row
    try:
        from db.connection import get_connection
        close = False
        _conn = conn
        if _conn is None:
            _conn = get_connection()
            close = True
        try:
            cur = _conn.cursor()
            cur.execute(
                "SELECT payload FROM media_cost_snapshot WHERE is_active=TRUE "
                "ORDER BY generated_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                payload = row["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                info["has_db_active_row"] = True
                _fill(payload, "db")
                return info
        finally:
            if close:
                _conn.close()
    except Exception as exc:
        logger.debug("resolve_snapshot_source: DB 探测失败(回落 bootstrap 判定): %s", exc)

    # 2) bootstrap JSON
    path = json_path or _BOOTSTRAP_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            _fill(json.load(f), "bootstrap")
    except Exception as exc:
        logger.debug("resolve_snapshot_source: bootstrap 读取失败 → source=none: %s", exc)
    return info


def assert_db_active_snapshot(conn=None, *, expected_snapshot_version: Optional[str] = None,
                              expected_payload_sha256: Optional[str] = None,
                              now: Optional[datetime] = None) -> Dict[str, Any]:
    """P0-A/B/C 生产开关验收 gate(fail-closed)。逐项校验(任一不满足 → raise · 阻止签 verified):
      ① source == 'db'(非 bootstrap fallback / none —— 不能把 bootstrap 当已写库);
      ② schema_version == COST_SNAPSHOT_SCHEMA_VERSION(旧 schema active row 拒);
      ③ payload_sha256 非空(内容寻址完整性);
      ④ generated_at 未 stale(> COST_SNAPSHOT_MAX_AGE_DAYS 拒 · 防过期快照定价);
      ⑤ 若给 expected_snapshot_version / expected_payload_sha256 → 逐字匹配(Deploy 对账:
         证明生产 active row 就是部署核验过的那一份)。
    返回探针 dict(含 snapshot_version / payload_sha256 / generated_at 供留痕)。"""
    info = resolve_snapshot_source(conn=conn)
    problems: List[str] = []
    if info.get("source") != "db":
        problems.append(
            f"来源 {info.get('source')!r} 非 DB active snapshot(先 build_snapshot_from_db 写 DB is_active row)")
    if info.get("schema_version") != COST_SNAPSHOT_SCHEMA_VERSION:
        problems.append(f"schema_version {info.get('schema_version')!r} != {COST_SNAPSHOT_SCHEMA_VERSION}(旧 schema)")
    if not info.get("payload_sha256"):
        problems.append("payload_sha256 缺失(内容寻址不完整)")
    gen = info.get("generated_at")
    try:
        gdt = datetime.fromisoformat(gen)
        _now = now or datetime.now(gdt.tzinfo)
        if (_now - gdt) > timedelta(days=COST_SNAPSHOT_MAX_AGE_DAYS):
            problems.append(f"快照已陈旧(generated_at={gen} · 超 {COST_SNAPSHOT_MAX_AGE_DAYS} 天)")
    except (ValueError, TypeError):
        problems.append(f"generated_at 非法或缺失({gen!r})")
    if expected_snapshot_version is not None and info.get("snapshot_version") != expected_snapshot_version:
        problems.append(
            f"snapshot_version {info.get('snapshot_version')!r} != 期望 {expected_snapshot_version!r}(部署对账失败)")
    if expected_payload_sha256 is not None and info.get("payload_sha256") != expected_payload_sha256:
        problems.append("payload_sha256 != 期望(部署对账失败)")
    if problems:
        raise MediaCostMappingError("P0-A 快照验收失败:" + "; ".join(problems))
    return info
