"""WP7 · quote-scoped 六阶段投影(规格 03 §10 的唯一 canonical 投影器)。

## 为什么必须是 quote-scoped,而不是 brand-scoped

生产现状:`strict_article_outcomes.attribute_observations` 按 **brand_id** 归因,
`customer_operation_plan._publication_outcome_summary(brand_id)` 也按 brand 取。
同一个品牌可以同时有 Q1 / Q2 两张报价(续费、加词、不同服务期都会产生),于是:

  Q1 有一次严格命中 → 按 brand 取 max/merge → **Q2 的门户也显示"有命中"**。

那是把别的合同的成果卖给这个合同。规格 03 §10 判别测试写得很直接:
「同品牌 Q1 有 strict hit、Q2 无 hit:Q2 的 strictly_attributed 必须为 0/unavailable;
  恢复 brand max merge 或删除 quote predicate 时测试红。」

本模块的每一条 SQL 都带 quote predicate,且带反向变异测试(删掉 predicate 必红)。

## 六阶段是"元组",不是"漏斗百分比"

`allocated_capacity → produced_ready → submitted → published_active
 → monitored_covered → strictly_attributed`

**各级无需相等,也不保证单调**。举两个真实形态:
  · 容量 10 篇但只做了 3 篇 → produced < allocated,正常;
  · 一篇发布后撤稿 → published_active 减 1,但 submitted 不减(提交这件事发生过)。

所以本模块**不算比率**、不存比率。比率一旦落库就把分母冻死了,下游再也看不见它
(`publication_outcome_facts` 那三条口径纪律同源)。

## 计数单位 = 交付单元(slot),不是 attempt

「同一 slot 多次 retry / 两个发布成功仍在各阶段最多计 1」——所以每一阶段都对
**交付单元键** 去重。单元键优先级:
  1. `delivery_slot_key`(合同槽位;WP1 之后是 canonical)
  2. `post:<geo_post_id>`(图文 lane 未挂槽位时)
  3. `article:<article_id>`(文章 lane 历史数据)
没有任何一个稳定键 → 该行进 `unattributed`,**不猜**(规格:「没有规范化 URL /
provider publication id 等稳定键时保持 unattributed;按品牌+日期猜绑变异必须红」)。

## cutoff 是投影的一部分,不是过滤器

同一 quote 在不同 cutoff 下是不同的六元组。冻结报告必须把 cutoff 与
`source_versions` 一起存下来才可复现 —— `freeze_projection()` 就是干这个的。

## 服务完成不由内容阶段推导

规格 03 §10:「合同服务完成不由内容阶段推导;只透传 W6 的 per-keyword
`compliant_days/service_days/complete`。W6 未 live 或 quote 聚合未签发时显示
unavailable,不猜 quote 总 complete。」

本模块因此**只透传 per-keyword**,并且**永不产出 quote 级 complete 布尔**。
想要那个数的调用方会拿到 `available=False` + 原因,这是故意的。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Iterable, Mapping, Optional, Sequence

from services.monitoring_identity_review import AGGREGATE_ELIGIBLE_SQL
from services.strict_article_outcomes import (
    OUTCOME_METRIC_VERSION,
    URL_NORMALIZATION_VERSION,
    normalize_publication_url,
    parse_citation_urls,
)

STAGE_PROJECTION_VERSION: Final = "quote-stage-projection-v1.0"
SERVICE_COMPLETION_PASSTHROUGH_VERSION: Final = "w6-raw-day-passthrough-v1.0"

#: 六阶段的**固定顺序**。顺序本身是契约的一部分(前端/报告按序渲染),
#: 增删阶段必须同步改本常量与它的锁测试。
STAGES: Final[tuple[str, ...]] = (
    "allocated_capacity",
    "produced_ready",
    "submitted",
    "published_active",
    "monitored_covered",
    "strictly_attributed",
)

#: 只有这些发布态算"真发布"。pending / failed / unknown / submitted 一律不算 ——
#: 规格判别测试:「pending/failed/unknown 不计 published_active」。
#:
#: 🔴 词表不是拍脑袋列的,是 2026-08-17 生产只读枚举出来的实际值(否则就是又一个
#:    「零区分力判据」):
#:      mhz_publish_order_items  published 325 / rejected 102 / failed 87 /
#:                               cancelled 42 / withdrawn 29 / submitted 22 /
#:                               awaiting_action 9
#:      publish_records          success 50 / failed 8
#:      publish_order_items      0 行(整表空)
#:      geo_douyin_posts         ready 18 / failed 12
#:      articles                 draft 1528(**只有这一个值**)
#:    → 所以 `articles.status='completed'` 这种判据在本仓恒假,已弃用;
#:      文章 lane 的"已制作"改判正文非空(见 `publication_stage_sources._PRODUCED_SQL`)。
PUBLISHED_STATES: Final[frozenset[str]] = frozenset({
    "published", "success", "manual_confirmed", "completed",
})
#: "已提交"的口径比"已发布"宽:提交这件事发生过就算,后续失败也不回退。
#: `awaiting_action` / `awaiting_sync` 是**提交之后**的等待态,必须算已提交。
SUBMITTED_STATES: Final[frozenset[str]] = frozenset(PUBLISHED_STATES | {
    "submitted", "publishing", "pending_confirm", "awaiting_sync",
    "awaiting_action", "processing",
})
#: 失败/未知**不是**已提交的反义词 —— 它们是已提交之后的终态,仍然计入 submitted。
FAILED_STATES: Final[frozenset[str]] = frozenset({
    "failed", "rejected", "cancelled", "withdrawn", "unknown", "error",
})
#: 投影必须能分类的全部状态值。三个集合的并集必须覆盖它 —— 少一个值就意味着
#: 那一类行在投影里既不算已提交也不算失败,**静默消失**。
#:
#: 来源分两类,不混为一谈:
#:   · 生产只读枚举得到(2026-08-17):published / rejected / failed / cancelled /
#:     withdrawn / submitted / awaiting_action / success;
#:   · 取数层**合成**的字面量:`manual_confirmed`
#:     (`media_publications` 没有 status 列,人工登记链在 SQL 里贴的标签)。
#: `completed` 两边都没出现过,只作为兼容值留在 `PUBLISHED_STATES` 里,不进本集合 ——
#: 把没见过的值写进"观察到的"集合,就是在给自己造一份假的实证。
OBSERVED_PUBLICATION_STATES: Final[frozenset[str]] = frozenset({
    "published", "rejected", "failed", "cancelled", "withdrawn", "submitted",
    "awaiting_action", "success", "manual_confirmed",
})


class ProjectionUnavailable(RuntimeError):
    """投影无法产出。**响亮失败**,绝不返回一个看起来正常的 0。

    0 与"不可用"在门户上长得一模一样,但业务含义相反:
    前者是"这个合同确实一篇都没发",后者是"我们不知道"。
    """


def _eligibility_version() -> str:
    """监测资格谓词的版本 = 谓词本身的哈希。

    🔴 为什么不写成一个手维护的常量:手维护的版本号会**漏跟**。
       谓词改了而版本号没改 → 冻结报告里两份不同口径的数字带着同一个版本号,
       事后无法分辨。哈希做不到漏跟 —— 改谓词必然改版本。
    """
    digest = hashlib.sha256(AGGREGATE_ELIGIBLE_SQL.encode("utf-8")).hexdigest()[:12]
    return f"monitoring-eligibility-sql-{digest}"


def source_versions() -> dict[str, str]:
    """冻结报告必须整组存下来的版本指纹。

    规格 03 §10:「保存报告冻结 cutoff、六阶段元组、必要 watermark,以及
    projector/schema/monitoring eligibility/attribution metric/URL normalization
    全部 source versions。」少存一个,后来就无法判断两份报告能不能比。
    """
    return {
        "projector": STAGE_PROJECTION_VERSION,
        "schema": "migration_034+035_geo_image_note",
        "monitoring_eligibility": _eligibility_version(),
        "attribution_metric": OUTCOME_METRIC_VERSION,
        "url_normalization": URL_NORMALIZATION_VERSION,
        "service_completion": SERVICE_COMPLETION_PASSTHROUGH_VERSION,
    }


@dataclass(frozen=True)
class StageValue:
    """一个阶段的值。`count` 与 `available` **必须一起读**。

    `available=False` 时 `count` 恒为 None —— 不给一个"看起来能用的 0"。
    """
    count: Optional[int]
    available: bool = True
    reason: Optional[str] = None
    unit_keys: tuple[str, ...] = field(default=(), compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {"count": self.count, "available": self.available, "reason": self.reason}

    @classmethod
    def unavailable(cls, reason: str) -> "StageValue":
        return cls(count=None, available=False, reason=reason)


def _aware(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def unit_key(row: Mapping[str, Any]) -> Optional[str]:
    """交付单元键。**没有稳定键就返回 None** —— 调用方必须把它记成 unattributed。

    🔴 这里绝不能加"按 brand_id + 日期兜底猜一个"的分支。规格判别测试专门盯着它:
       「按品牌+日期猜绑变异必须红」。猜出来的绑定会把别人的成果算进这个合同。
    """
    slot = row.get("delivery_slot_key")
    if slot:
        return f"slot:{slot}"
    post = row.get("geo_post_id") or row.get("source_geo_post_id")
    if post:
        return f"post:{post}"
    article = row.get("article_id")
    if article:
        return f"article:{article}"
    # 规范化 URL 是规格明写的稳定键之一(「没有**规范化 URL / provider publication id**
    # 等稳定键时保持 unattributed」)。人工登记链常常既没有 slot 也没有 article,
    # 但一定有 URL —— 少了这一级,合法的人工发布会被整条判成 unattributed。
    url = row.get("normalized_url") or row.get("publish_url") or row.get("platform_url")
    normalized = normalize_publication_url(url or "")
    if normalized:
        return f"url:{normalized}"
    return None


def _active_at(row: Mapping[str, Any], cutoff: datetime) -> bool:
    """该发布物在 cutoff 时点是否仍然有效(未撤稿)。

    撤稿是**追加**语义:`published_at` 不被覆写,只多一个 `retracted_at`。
    所以 cutoff 早于撤稿时间的历史报告仍然应该看到它是有效的 —— 这不是
    宽容,是可复现性:同一 cutoff 重跑必须得到同一组数。
    """
    availability = str(row.get("availability") or "active")
    if availability not in {"active", ""} and not row.get("retracted_at"):
        # availability 已显式写成 retracted/replaced 但连时间都没有(历史脏行)
        # 才用状态字面量兜底;有时间时一律以时间为准,保证 cutoff 可复现。
        return False
    retracted = _aware(row.get("retracted_at"))
    if retracted and retracted <= cutoff:
        return False
    return True


def project_stages(
    *,
    quote_id: int,
    cutoff: datetime,
    allocated_capacity: Optional[int],
    produced_units: Iterable[Mapping[str, Any]],
    publication_attempts: Iterable[Mapping[str, Any]],
    monitoring_rows: Iterable[Mapping[str, Any]],
    quote_keyword_ids: Optional[Sequence[int]] = None,
) -> dict[str, Any]:
    """纯函数投影核心。**不碰数据库** —— 这样反向变异测试可以直接喂构造行。

    `publication_attempts` 里的每一行是一次 attempt(retry 会有多行);
    去重发生在本函数内,按 `unit_key`。
    """
    cutoff = _aware(cutoff) or datetime.now(timezone.utc)
    quality: dict[str, int] = {}

    def bump(key: str, n: int = 1) -> None:
        quality[key] = quality.get(key, 0) + n

    # ---- 阶段 1:已分配容量 -------------------------------------------------
    if allocated_capacity is None:
        stage_allocated = StageValue.unavailable("capacity_not_issued")
    else:
        stage_allocated = StageValue(count=max(0, int(allocated_capacity)))

    # ---- 阶段 2:已制作(ready 可校对)---------------------------------------
    produced: set[str] = set()
    for row in produced_units:
        key = unit_key(row)
        if not key:
            bump("produced_unit_unattributed")
            continue
        created = _aware(row.get("ready_at") or row.get("created_at"))
        if created and created > cutoff:
            continue
        produced.add(key)
    stage_produced = StageValue(count=len(produced), unit_keys=tuple(sorted(produced)))

    # ---- 阶段 3/4:已提交、真发布有效 ----------------------------------------
    submitted: set[str] = set()
    published: set[str] = set()
    #: 单元 → 该单元在 cutoff 时点**仍有效**的发布物(取最早一条,replacement 链取现役那条)
    active_publication: dict[str, dict[str, Any]] = {}

    for row in publication_attempts:
        if int(row.get("quote_id") or 0) != int(quote_id):
            # 防御性:loader 已带 quote predicate,这里再挡一次。
            # 两道闸不是冗余 —— 纯函数会被测试直接喂行,少这道闸测试就能骗过自己。
            bump("attempt_wrong_quote")
            continue
        key = unit_key(row)
        if not key:
            bump("attempt_unattributed")
            continue
        state = str(row.get("status") or "").casefold()
        submitted_at = _aware(row.get("submitted_at"))
        published_at = _aware(row.get("published_at"))

        if (submitted_at and submitted_at <= cutoff) or (
            state in SUBMITTED_STATES and published_at and published_at <= cutoff
        ):
            submitted.add(key)
        elif state in FAILED_STATES and submitted_at is None and published_at is None:
            # 从未提交过的失败(例如资格校验就拒了)—— 不计 submitted。
            bump("attempt_failed_before_submit")

        if state not in PUBLISHED_STATES:
            if state in FAILED_STATES:
                bump(f"attempt_state_{state}")
            continue
        if not published_at:
            bump("published_time_unknown")
            continue
        if published_at > cutoff:
            continue
        if not _active_at(row, cutoff):
            bump("publication_retracted_at_cutoff")
            continue
        published.add(key)
        prior = active_publication.get(key)
        if prior is None or published_at < _aware(prior["published_at"]):
            active_publication[key] = {**row, "published_at": published_at}

    stage_submitted = StageValue(count=len(submitted), unit_keys=tuple(sorted(submitted)))
    stage_published = StageValue(count=len(published), unit_keys=tuple(sorted(published)))

    # ---- 阶段 5:已完成监测覆盖 ----------------------------------------------
    # 口径(规格 03 §10):同 slot 的 quote + keyword + engine,且
    #   `active.published_at <= tested_at <= cutoff`。
    # 🔴 published_at 取的是**现役发布物**的,不是被撤稿那条的 —— 否则
    #    「A 已监测后撤稿、B replacement 刚发布尚未监测」会让 B 白捡 A 的覆盖。
    allowed_kw = {int(k) for k in (quote_keyword_ids or []) if k is not None}
    covered: set[str] = set()
    eligible_monitoring: list[dict[str, Any]] = []
    for row in monitoring_rows:
        if int(row.get("quote_id") or 0) != int(quote_id):
            bump("monitoring_wrong_quote")
            continue
        tested_at = _aware(row.get("tested_at"))
        if not tested_at or tested_at > cutoff:
            continue
        kw_id = row.get("confirmed_keyword_id") or row.get("keyword_id")
        if allowed_kw and (kw_id is None or int(kw_id) not in allowed_kw):
            bump("monitoring_keyword_outside_quote")
            continue
        eligible_monitoring.append({**row, "tested_at": tested_at})

    for key, pub in active_publication.items():
        pub_at = _aware(pub["published_at"])
        for row in eligible_monitoring:
            if row["tested_at"] < pub_at:
                continue
            covered.add(key)
            break
    stage_monitored = StageValue(count=len(covered), unit_keys=tuple(sorted(covered)))

    # ---- 阶段 6:严格归因命中 -----------------------------------------------
    # 严格 = 同一 canonical source tuple(规范化 URL 相同)+ 正文证据 + 时序正确。
    # 🔴 只对**现役**发布物计。撤稿掉的 A 的历史命中留在 ledger 里(不删),
    #    但不能和 replacement B 的 active 拼成一个 strictly_attributed。
    strict: set[str] = set()
    strict_ledger: list[dict[str, Any]] = []
    url_index: dict[str, str] = {}
    for key, pub in active_publication.items():
        normalized = pub.get("normalized_url") or normalize_publication_url(
            pub.get("publish_url") or pub.get("platform_url") or "")
        if not normalized:
            bump("publication_url_missing_or_invalid")
            continue
        if not pub.get("body_proof"):
            bump("publication_snapshot_missing")
            continue
        url_index[normalized] = key

    for row in eligible_monitoring:
        parsed = parse_citation_urls(row.get("search_citations"))
        if parsed["status"] in {"invalid_json", "not_array"}:
            bump(f"citation_{parsed['status']}")
            continue
        for url in parsed["urls"]:
            key = url_index.get(url)
            if not key:
                continue
            pub = active_publication[key]
            if row["tested_at"] <= _aware(pub["published_at"]):
                bump("prepublication_citation")
                continue
            strict.add(key)
            strict_ledger.append({
                "unit_key": key,
                "monitoring_result_id": row.get("id"),
                "normalized_url": url,
                "tested_at": row["tested_at"].isoformat(),
                "published_at": _aware(pub["published_at"]).isoformat(),
                "metric_version": OUTCOME_METRIC_VERSION,
            })
    stage_strict = StageValue(count=len(strict), unit_keys=tuple(sorted(strict)))

    stages = {
        "allocated_capacity": stage_allocated,
        "produced_ready": stage_produced,
        "submitted": stage_submitted,
        "published_active": stage_published,
        "monitored_covered": stage_monitored,
        "strictly_attributed": stage_strict,
    }
    assert tuple(stages) == STAGES, "阶段顺序与 STAGES 契约不一致"
    return {
        "quote_id": int(quote_id),
        "cutoff": cutoff.isoformat(),
        "stages": {name: value.as_dict() for name, value in stages.items()},
        "unit_keys": {name: list(value.unit_keys) for name, value in stages.items()},
        "strict_attribution_ledger": strict_ledger,
        "source_versions": source_versions(),
        "quality_counts": dict(sorted(quality.items())),
    }


def has_published_occurrence(projection: Mapping[str, Any]) -> bool:
    """该 quote 在 cutoff 前**发生过**发布(含已撤稿的)。

    规格 03 §10:「撤稿仍保留 occurrence,是否停止监测不由本功能擅改;
    active 另按 cutoff 归零。」所以 occurrence 看的是 submitted/曾发布,
    不是 published_active —— 两者故意分开。
    """
    stages = projection.get("stages") or {}
    submitted = stages.get("submitted") or {}
    published = stages.get("published_active") or {}
    retracted = int((projection.get("quality_counts") or {}).get(
        "publication_retracted_at_cutoff", 0))
    return bool((published.get("count") or 0) > 0
                or retracted > 0
                or (submitted.get("count") or 0) > 0)


def freeze_projection(projection: Mapping[str, Any], *,
                      watermark: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """把一次投影冻结成可存库、可复现的报告体。

    冻结体必须自带 cutoff + 六元组 + 全套 source_versions + watermark。
    少任何一项,事后就无法判断「这两份报告能不能放在一起比」。
    """
    frozen = {
        "quote_id": projection.get("quote_id"),
        "cutoff": projection.get("cutoff"),
        "stages": dict(projection.get("stages") or {}),
        "source_versions": dict(projection.get("source_versions") or {}),
        "watermark": dict(watermark or {}),
    }
    payload = (repr(sorted(frozen["stages"].items()))
               + repr(sorted(frozen["source_versions"].items())))
    frozen["reproducibility_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return frozen


def projections_comparable(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """两份冻结报告能不能放在一起比 = source_versions 逐字相同。

    版本不同就**不许**偷偷混聚合(规格判别测试:「版本变化不能偷偷混聚合」)。
    """
    return dict(a.get("source_versions") or {}) == dict(b.get("source_versions") or {})
