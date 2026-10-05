"""报价投放组合 SSOT（WO_QUOTE_MEDIA_MIX_DYNAMIC_2026-08-12 v2 · P0）。

把**已经确定的**交付额度 `capacity_total` 拆成三类：

    重点媒体锚点 A + 行业与平台覆盖 C + 抖音图文 D（豆包专项）  ，且 A + C + D == capacity_total

🔴 本模块**不决定总额度**。总额度仍由 `tools/pricing_bands.py` 那条链产出并冻结，
   本包对它零差异（工单 §1.2 / §2.3）。这里只做「同一个数怎么拆、怎么讲」。

🔴 比例只是**初始组合先验**，不是效果等价系数。
   `R = 覆盖 / 锚点` 描述的是「AI 引用池里常见来源的构成」，
   **不**表示「两篇覆盖内容等于一篇重点媒体」。当前样本不足以签发这种承诺，
   所以对外一律带 `status="provisional_classified_pool"`（裁定书 v2 §1.2）。

数据口径（裁定书 v2 §1.2，三条都是 v1 被判红后的订正）：
  · 抖音是 self_media 的**子集**，必须先从覆盖侧扣掉再算比例 —— 否则同一批引用被用两次；
  · 未分类长尾**不进**任何一侧，只披露计数 —— v1 把它整体算进覆盖，把比例从 2.32 抬到 3.32；
  · 不设 3–5 的运营区间；clamp 只用 [1.5, 4.0] 防小分母极端值，且不是合格线。

术语对照（内部 → 对外）：anchor=重点媒体锚点 / coverage=行业与平台覆盖 / douyin=抖音图文。
内部字段名（R / anchor_n / unclassified / ratio_source）**不得**出现在客户可见文案里（工单 §6）。
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger("GEO-QuoteMediaMix")

STRATEGY_VERSION = "quote-media-mix-v2-provisional"
RATIO_STATUS = "provisional_classified_pool"
RECHECK_DAYS = (7, 14, 30)
REMAINING_CAPACITY_POLICY = "reallocate_after_recheck"

# 比例夹紧：只防小分母失真，**不是**商业资格线，也不是效果区间（裁定书 v2 §1.3）
RATIO_CLAMP_MIN = 1.5
RATIO_CLAMP_MAX = 4.0
#: 全局先验 = (10,916 + 8,931 − 1,724) / (479 + 8,088) = 18,123 / 8,567（裁定书 v2 §1.2）
#: 🔴 池表读不到时用它兜底,**不是**用 clamp 下限 ——
#:    2026-08-12 Review 判红:降级时走 1.5 会把 21 条拆成 8/13/0,
#:    而按已裁定的全局先验应当是 7/14/0。降级要落到"有出处的默认值",
#:    不是落到"夹紧区间的边界"（边界只是防失真的护栏,本身没有业务含义）。
GLOBAL_RATIO_RAW = 2.1154429789
# 取用某一格前的最低样本要求（工单 §4.1）
MIN_CELL_N = 30
MIN_ANCHOR_N = 20
# 抖音专项闸（工单 §4.2）
DOUYIN_MIN_SHARE = 0.10
DOUYIN_MAX_SHARE = 0.20
DOUYIN_ENGINE = "doubao"
# 锚点至少 2 篇，否则「锚点」这一层名存实亡
MIN_ANCHOR_COUNT = 2

#: 引用池分层表（行业×引擎逐格）。抖音列是 self_media/vertical 的**子集**，不是第六层。
POOL_CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "quote_media_mix_pool_v2.csv",
)


def round_half_up(value: float) -> int:
    """四舍五入到整数，`.5` 一律进位。

    🔴 不能用内置 `round()`：Python 用 banker's rounding（`round(2.5) == 2`），
       而前端 `Math.round(2.5) === 3`。同一个容量在两端算出不同拆分 = 数字对不上，
       工单 §4.3 明令两端共享同一套取整语义。TS 侧同名函数见
       `frontend/src/lib/quoteMediaMix.ts`。
    """
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class PoolCounts:
    """某个统计范围（格 / 行业 / 引擎 / 全局）里的分层引用计数。"""

    cell_n: int = 0
    authoritative_n: int = 0
    portal_n: int = 0
    vertical_n: int = 0
    self_media_n: int = 0
    douyin_n: int = 0
    unclassified_n: int = 0

    @property
    def anchor_n(self) -> int:
        """重点媒体锚点 = 权威来源 + 门户。

        ⚠️ 当前目录是**域名级**分类，`sohu.com` / `163.com` 这类域同时承载编辑内容和
           平台账号内容，所以门户侧偏高。裁定书 v2 §0 已把它记为已知限制，
           目录升到 URL/资源级之前，本值只能作初始先验（工单 §9.3）。
        """
        return self.authoritative_n + self.portal_n

    @property
    def coverage_n(self) -> int:
        """覆盖侧 = 垂直 + 自媒体 **− 抖音**（抖音单列，扣掉才不会被算两次）。"""
        return self.vertical_n + self.self_media_n - self.douyin_n

    @property
    def is_consistent(self) -> bool:
        """五层之和应当等于 cell_n（抖音是子集，不参与求和）。"""
        total = (self.authoritative_n + self.portal_n + self.vertical_n
                 + self.self_media_n + self.unclassified_n)
        return total == self.cell_n

    def __add__(self, other: "PoolCounts") -> "PoolCounts":
        return PoolCounts(
            cell_n=self.cell_n + other.cell_n,
            authoritative_n=self.authoritative_n + other.authoritative_n,
            portal_n=self.portal_n + other.portal_n,
            vertical_n=self.vertical_n + other.vertical_n,
            self_media_n=self.self_media_n + other.self_media_n,
            douyin_n=self.douyin_n + other.douyin_n,
            unclassified_n=self.unclassified_n + other.unclassified_n,
        )


@dataclass
class MediaMixPool:
    """引用池，**按已裁定的口径范围逐行存**（不是逐格存后再聚合）。

    🔴 为什么不逐格存再聚合(2026-08-12 实测后改的):
       裁定书 v2 的全局抖音总数 1,724 条**分不到**具体格上 —— 08-07 那份 315 行证据表
       根本没有抖音列。逐格存就必须给未知格填 0，聚合出来的全局抖音会变成 224，
       与已裁定的 1,724 对不上 —— 那等于用一个我编的数覆盖掉裁定过的数。
       所以这里直接存「global / industry / industry_engine」三种范围的**裁定值**，
       查不到的范围一律回退，绝不由代码现场拼一个出来。

    🔴 数据时点 = 2026-08-07 快照(`pool_version` 列自证)。**当前生产已复现不出它**
       (2026-08-12 实测:citations 全表只剩 25,536 行、id 序列缺口 15,991,
        而该池是 36,980 条)。是否按新数据重算属于业务裁决,不由本包擅自替换。
    """

    rows: dict[tuple[str, str, str], PoolCounts] = field(default_factory=dict)
    version: str = ""

    def lookup(self, scope: str, industry: str = "", engine: str = "") -> Optional[PoolCounts]:
        return self.rows.get((scope, industry, engine))

    def cell(self, industry: str, engines: Sequence[str]) -> Optional[PoolCounts]:
        """行业×目标引擎。多引擎时只有**每一个**都有裁定值才合并,缺一个就返回 None 走回退
        —— 半份数据拼出来的比例没有出处。"""
        found = [self.lookup("industry_engine", industry, e) for e in engines]
        if not found or any(c is None for c in found):
            return None
        acc = PoolCounts()
        for c in found:
            acc = acc + c  # type: ignore[arg-type]
        return acc

    def industry(self, industry: str) -> Optional[PoolCounts]:
        return self.lookup("industry", industry, "")

    def engine(self, engines: Sequence[str]) -> Optional[PoolCounts]:
        found = [self.lookup("engine", "", e) for e in engines]
        if not found or any(c is None for c in found):
            return None
        acc = PoolCounts()
        for c in found:
            acc = acc + c  # type: ignore[arg-type]
        return acc

    def global_(self) -> Optional[PoolCounts]:
        return self.lookup("global", "", "")


def _row_to_counts(row: dict[str, str]) -> PoolCounts:
    g = lambda key: int(row.get(key) or 0)  # noqa: E731
    return PoolCounts(
        cell_n=g("cell_n"),
        authoritative_n=g("authoritative_n"),
        portal_n=g("portal_n"),
        vertical_n=g("vertical_n"),
        self_media_n=g("self_media_n"),
        douyin_n=g("douyin_n"),
        unclassified_n=g("unclassified_n"),
    )


def load_pool(path: str = POOL_CSV_PATH) -> MediaMixPool:
    """读引用池表。读不到就返回空池 —— 调用方会走全局回退，**不阻断报价主链**（工单 §1.1.5）。"""
    pool = MediaMixPool()
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                scope = (row.get("scope") or "").strip()
                if not scope:
                    continue
                industry = (row.get("industry") or "").strip()
                engine = (row.get("engine") or "").strip()
                pool.rows[(scope, industry, engine)] = _row_to_counts(row)
                pool.version = (row.get("pool_version") or pool.version or "").strip()
    except FileNotFoundError:
        logger.warning("[quote_media_mix] 引用池表缺失,退全局兜底: %s", path)
    except Exception as exc:  # pragma: no cover - 读表异常不该拖垮报价
        logger.warning("[quote_media_mix] 引用池表读取失败(%s),退全局兜底", type(exc).__name__)
    return pool


def _eligible(counts: Optional[PoolCounts]) -> bool:
    """样本够不够用这一范围。`anchor_n >= 20` 只防小分母失真，
    不满足自动回退，**不得**对客户提示"不合格"（工单 §4.1）。"""
    if counts is None:
        return False
    return counts.cell_n >= MIN_CELL_N and counts.anchor_n >= MIN_ANCHOR_N


def resolve_ratio(
    pool: MediaMixPool, industry: Optional[str], target_engines: Sequence[str],
) -> tuple[PoolCounts, str]:
    """四级回退链，返回（选中范围的计数, 来源标识）。

    顺序：行业×目标引擎 → 行业汇总 → 引擎汇总 → 全局（工单 §4.1）。
    多个目标引擎时，"行业×目标引擎"取这些引擎的合计 —— 与全局同为聚合口径，不挑单个引擎。
    """
    engines = [e for e in (target_engines or []) if e]
    if industry and engines:
        counts = pool.cell(industry, engines)
        if _eligible(counts):
            return counts, "industry_engine"  # type: ignore[return-value]
    if industry:
        counts = pool.industry(industry)
        if _eligible(counts):
            return counts, "industry"  # type: ignore[return-value]
    if engines:
        counts = pool.engine(engines)
        if _eligible(counts):
            return counts, "engine"  # type: ignore[return-value]
    # 全局也拿不到(池表缺失/损坏)时给空计数 —— 下游用 clamp 下界兜底,报价不阻断
    return pool.global_() or PoolCounts(), "global"


def resolve_douyin_share(
    pool: MediaMixPool, industry: Optional[str], target_engines: Sequence[str],
) -> float:
    """抖音专项占比。三条同时满足才启用（工单 §4.2），否则 0。

    🔴 闸门一律在**行业×豆包这一格**上判，不用比例回退后的那个范围 ——
       工单 §4.2 的措辞就是"行业×豆包格"。两者可能不同范围（例如比例因锚点太少
       回退到了行业汇总，而抖音信号仍应看豆包本格），这里刻意保持字面口径。
    """
    engines = [e for e in (target_engines or []) if e]
    if DOUYIN_ENGINE not in engines or not industry:
        return 0.0
    cell = pool.cell(industry, [DOUYIN_ENGINE])
    if cell is None or cell.cell_n < MIN_CELL_N or cell.cell_n <= 0:
        return 0.0
    share = cell.douyin_n / cell.cell_n
    if share < DOUYIN_MIN_SHARE:
        return 0.0
    return min(share, DOUYIN_MAX_SHARE)


def _conversion_block(total: int, mix: dict, perspective: Optional[str]) -> dict:
    """逐桶「一槽要几条」+ 逐桶条数估算(WO_225-c1 §8.2)。

    🔴 本块**不改** `mix`。`mix` 是引用池拆出来的**槽**的分布,飞轮与指纹都在读它;
       口径(perspective)回答的是另一个问题 ——「这些槽按哪类媒体去发」,
       它只影响**条数估算**。两者混写会让切一次口径就重写一遍组合决策,
       而 `ratio` 那一串样本数会变成和 `mix` 对不上的孤儿。

    · portal    = 全部槽记锚点(一槽一条)
    · self_media= 全部槽记覆盖(一槽约 5 条)· **默认**(Owner 2026-09-15 ③)
    · mixed     = 按 `mix` 现有拆分逐桶换算

    🔴 取不到换算表 ⇒ 三桶全 10000(即今天的行为)。见 `resolve_for_quote` 抬头:
       读不出来时按 5 倍算等于凭空放大 5 倍,而没有任何东西会报错。
    """
    from services.media_slot_conversion import (
        BUCKET_ANCHOR, BUCKET_COVERAGE, BUCKET_DOUYIN, BUCKETS,
        DEFAULT_PERSPECTIVE, PERSPECTIVE_PORTAL, PERSPECTIVE_SELF_MEDIA,
        PERSPECTIVES, posts_for_slots, resolve_for_quote,
    )
    perspective = perspective if perspective in PERSPECTIVES else DEFAULT_PERSPECTIVE
    resolved = resolve_for_quote(None)
    bps = dict(resolved["bps"])  # type: ignore[arg-type]
    if perspective == PERSPECTIVE_PORTAL:
        slots = {BUCKET_ANCHOR: total, BUCKET_COVERAGE: 0, BUCKET_DOUYIN: 0}
    elif perspective == PERSPECTIVE_SELF_MEDIA:
        slots = {BUCKET_ANCHOR: 0, BUCKET_COVERAGE: total, BUCKET_DOUYIN: 0}
    else:
        slots = {b: int((mix or {}).get(b) or 0) for b in BUCKETS}
    estimate = {b: posts_for_slots(int(slots.get(b) or 0), b, bps) for b in BUCKETS}
    return {
        "delivery_perspective": perspective,
        "conversion_version": int(resolved["version"]),  # type: ignore[arg-type]
        "posts_per_slot_bps": {b: int(bps.get(b) or 0) for b in BUCKETS},
        "posts_estimate": estimate,
        "posts_estimate_total": sum(estimate.values()),
    }


def plan_media_mix(
    capacity_total: int,
    *,
    industry: Optional[str] = None,
    target_engines: Optional[Sequence[str]] = None,
    pool: Optional[MediaMixPool] = None,
    perspective: Optional[str] = None,
) -> dict[str, Any]:
    """把已确定的交付额度拆成三类，并带上每个数字的来源（工单 §5）。

    幂等：同样入参必然同样出参（纯函数 + 只读表），报价重复计算不会漂移。
    """
    engines = [e for e in (target_engines or []) if e]
    pool = pool if pool is not None else load_pool()
    total = max(0, int(capacity_total or 0))

    counts, source = resolve_ratio(pool, industry, engines)
    anchor_n = counts.anchor_n
    coverage_n = counts.coverage_n
    if anchor_n > 0:
        ratio_raw = coverage_n / anchor_n
        degraded = False
    else:
        # 池表缺失/损坏 → 用**有出处的全局先验**兜底,不是用 clamp 边界(见常量注释)
        ratio_raw = GLOBAL_RATIO_RAW
        degraded = True
    ratio_used = min(RATIO_CLAMP_MAX, max(RATIO_CLAMP_MIN, ratio_raw))

    douyin_share = resolve_douyin_share(pool, industry, engines)
    douyin = round_half_up(total * douyin_share)
    douyin = max(0, min(douyin, total))

    remaining = total - douyin
    anchor = max(MIN_ANCHOR_COUNT, round_half_up(remaining / (1 + ratio_used))) if remaining > 0 else 0
    anchor = min(anchor, remaining)
    coverage = remaining - anchor

    assert anchor + coverage + douyin == total, "三类之和必须等于交付额度"

    return {
        "capacity_total": total,
        "mix": {
            "focus_media_anchor": anchor,
            "industry_platform_coverage": coverage,
            "douyin_doubao_only": douyin,
        },
        "ratio": {
            "raw": ratio_raw,
            "used": ratio_used,
            "source": source,
            "cell_n": counts.cell_n,
            "anchor_n": anchor_n,
            "coverage_n_excluding_douyin": coverage_n,
            "douyin_n": counts.douyin_n,
            "unclassified_n": counts.unclassified_n,
            "status": RATIO_STATUS,
            # 池表读不到时为 true —— 调用方据此把对外文案降级成"按全行业平均估算"
            "degraded": degraded,
        },
        "target_engines": engines,
        # [WO_225-c1 §8.2] 换算层:口径 + 逐桶 bps + 逐桶条数估算。
        #   🔴 这几个键是**内部运营口径**,客户端点必须剥除(Owner ③「不要看到」)。
        #      剥除点在 `api/selection_api._strip_internal_pricing_fields`,判据钉它。
        **_conversion_block(total, {
            "focus_media_anchor": anchor,
            "industry_platform_coverage": coverage,
            "douyin_doubao_only": douyin,
        }, perspective),
        "strategy_version": STRATEGY_VERSION,
        "pool_version": pool.version,
        "recheck_days": list(RECHECK_DAYS),
        "remaining_capacity_policy": REMAINING_CAPACITY_POLICY,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 落库：策略版本 / 样本数 / 回退层级 / 回查键（工单 §1.1.4）
# ─────────────────────────────────────────────────────────────────────────────
#
# 🔴 [2026-08-12 Review P1-2 返修] 这些字段原来只活在一个零调用的返回对象里 ——
#    "算得出来" 不等于 "存下来了",飞轮后续没有任何东西可消费。
#
# 写法**照抄** `services/quote_pricing_snapshot.record_capacity_evaluation_request`：
# 往 `quote_pricing_snapshots` 追加一个**新的不可变版本**，把组合塞进 `pricing_snapshot`
# 这个 JSONB 列的新 key 里，不动表结构、不动已冻结那一版、不动 `active_pricing_snapshot_id`。
# 于是本函数对 `quote_pricing_snapshot.py` **零差异**（只 import，不修改）。
#
# 🔴 幂等：同一个 quote 的组合没变就**一条都不写**。否则每看一次报价就多一个版本，
#    快照表会被刷爆 —— append-only 表尤其不能靠"反正只是加一行"糊过去。

MEDIA_MIX_SNAPSHOT_KEY = "media_mix"


def _mix_fingerprint(payload: dict[str, Any]) -> str:
    """组合指纹。只取**会影响交付**的字段;时间戳之类不进指纹,否则幂等永远不成立。"""
    import hashlib
    import json as _json

    material = {
        "capacity_total": payload.get("capacity_total"),
        "mix": payload.get("mix"),
        "ratio": {k: payload.get("ratio", {}).get(k)
                  for k in ("used", "source", "cell_n", "anchor_n",
                            "coverage_n_excluding_douyin", "douyin_n", "unclassified_n")},
        "strategy_version": payload.get("strategy_version"),
        "pool_version": payload.get("pool_version"),
        # [WO_225-c1 §8.2] 🔴 口径与换算版本**必须进指纹**:换口径 = 交付条数变了。
        #   不进指纹的话,服务商从「门户」切到「自媒体」,快照一条都不写,
        #   而报价页显示的条数已经变了 —— 存的和看的对不上,且没有任何东西会报错。
        #   代价:本单上线后每单首次查看会多追加一版(键集变了 ⇒ 指纹必变)。
        #   这是**如实**的:快照内容确实变了。append-only 表 +1 版/单,不动 active。
        "delivery_perspective": payload.get("delivery_perspective"),
        "conversion_version": payload.get("conversion_version"),
        "posts_estimate": payload.get("posts_estimate"),
    }
    return hashlib.sha256(
        _json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def record_media_mix_snapshot(
    quote_id: int, mix_payload: dict[str, Any], *,
    actor_user_id: int, actor_membership_id: Optional[int] = None,
) -> dict[str, Any]:
    """把组合决策追加成一个新的报价快照版本。已记过同样的组合则不写（幂等）。

    返回 `{"recorded": bool, "reason": str, "version": int | None}`。
    🔴 任何失败都**不抛给调用方**（工单 §1.1.5：飞轮/媒体数据出问题不得阻断报价主链），
       只返回 recorded=False + 原因，由上层决定要不要提示。
    """
    from db.connection import get_connection
    from services.quote_pricing_snapshot import _insert_snapshot

    fingerprint = _mix_fingerprint(mix_payload)
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT q.id, q.brand_id, s.id AS session_id
              FROM quotes q
              LEFT JOIN keyword_selection_sessions s ON s.quote_id = q.id
             WHERE q.id = %s AND q.deleted_at IS NULL
            """,
            (quote_id,),
        )
        row = cursor.fetchone()
        if not row or not row.get("session_id"):
            return {"recorded": False, "reason": "quote_or_session_missing", "version": None}

        cursor.execute(
            """
            SELECT version, pricing_snapshot
              FROM quote_pricing_snapshots
             WHERE quote_id = %s
             ORDER BY version DESC
             LIMIT 1
            """,
            (quote_id,),
        )
        latest = cursor.fetchone()
        if not latest:
            # 还没有任何报价快照 → 说明报价链还没走到冻结那一步,现在写会凭空造一版
            return {"recorded": False, "reason": "no_base_snapshot", "version": None}

        base = dict(latest.get("pricing_snapshot") or {})
        existing = (base.get(MEDIA_MIX_SNAPSHOT_KEY) or {}).get("fingerprint")
        if existing == fingerprint:
            return {"recorded": False, "reason": "unchanged", "version": latest.get("version")}

        base[MEDIA_MIX_SNAPSHOT_KEY] = {
            "fingerprint": fingerprint,
            "capacity_total": mix_payload.get("capacity_total"),
            "mix": mix_payload.get("mix"),
            "ratio": mix_payload.get("ratio"),
            "target_engines": mix_payload.get("target_engines"),
            "strategy_version": mix_payload.get("strategy_version"),
            "pool_version": mix_payload.get("pool_version"),
            "recheck_days": mix_payload.get("recheck_days"),
            "remaining_capacity_policy": mix_payload.get("remaining_capacity_policy"),
            # [WO_225-c1 §8.2] 冻结换算口径。`services/media_slot_conversion.resolve_for_quote`
            #   只认 `media_mix.conversion_version` 这一个键;改名要同改那一处。
            #   🔴 旧快照没有这几个键 ⇒ 读侧一律回落 10000(历史不重算,08_billing §7.5)。
            "conversion_version": mix_payload.get("conversion_version"),
            "posts_per_slot_bps": mix_payload.get("posts_per_slot_bps"),
            "posts_estimate": mix_payload.get("posts_estimate"),
            "delivery_perspective": mix_payload.get("delivery_perspective"),
        }
        inserted = _insert_snapshot(
            cursor,
            quote_id=quote_id,
            brand_id=row["brand_id"],
            session_id=row["session_id"],
            actor_user_id=actor_user_id,
            actor_membership_id=actor_membership_id,
            old_coefficient=None,
            new_coefficient=None,
            reason=f"记录投放组合决策({STRATEGY_VERSION})",
            calculation_version=STRATEGY_VERSION,
            pricing_data=base,
            clusters_data=None,
        )
        conn.commit()
        return {"recorded": True, "reason": "inserted", "version": inserted.get("version")}
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # pragma: no cover
                pass
        logger.warning("[quote_media_mix] 组合落库失败(%s),不阻断报价主链", type(exc).__name__)
        return {"recorded": False, "reason": f"error:{type(exc).__name__}", "version": None}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass
