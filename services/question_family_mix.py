"""问题族被引组合引擎（工单 T2）。

**要解决的错配**：我方全部发布史只有 1 条 URL 拿到 1 次被引（证据 §8），
而发布组合（房天下/土巴兔/杂牌）和 AI 真正引用的域（搜狐 1969 / 网易 1483 /
博客园 961 / 知乎 619）几乎不重叠。

**做法**：不再用手写的 ``_GENERIC_PLATFORMS`` 13 名单去切"垂直/通用"，而是
按**问题族**去数真实被引 mix，再折算成"主干 N + 垂类 M"。实测（证据 §6）
"装修 + 哪家/靠谱/推荐"的真实 mix 是 to8to 36 / sohu 33 / cnblogs 18 /
知乎 17 / 163 9 / 新浪家居 9 ≈ 垂类 45% : 主干 55%。

回落链（每一级都标注 ``source``，前端要能说清凭什么这么配）：
``question_family`` → ``industry`` → ``global``。

缺货降位三规则（Owner 2026-07-28 拍板）：

1. **同角色替代优先** —— 博客园（被引 #3）无货时补 CSDN/51CTO/开源中国，
   而不是让下一名门户把组合挤成全门户，破坏 AI 引用的混合结构。
   角色族谱由 ``citation_domain_weights`` 的域族先验定义。
2. **显式标注 + 需求计数** —— 降位不静默，推荐面写明替代原因；每次记一笔
   ``geo_media_substitution_demand``，按"被引强度 × 降位次数"回流补货清单。
3. **可自助平台转自助通道** —— 缺的若是用户能自己注册发布的 UGC 平台
   （博客园/知乎/公众号），给「自助发布」入口，不让用户空手而归。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Callable, Final, Sequence

from services.citation_domain_weights import (
    DomainFamilyPrior,
    ROLE_LABELS,
    SEED_DOMAIN_FAMILIES,
    TRUNK_ROLES,
    family_for_domain,
    normalize_domain,
)

logger = logging.getLogger("GEO-QuestionFamilyMix")

QUESTION_FAMILY_MIX_VERSION: Final = "geo-question-family-mix-v1.0"

#: A family needs at least this many observed citations before we trust its
#: shape over the broader fallback.  Below it we would be reading noise.
MIN_FAMILY_CITATIONS: Final = 20
DEFAULT_WINDOW_DAYS: Final = 180

#: Commercial-intent shapes, derived from the live prompt corpus
#: (``geo_research_prompts``: 409 prompts, all commercial "推荐/哪家/排行" forms).
#: ``family_key`` on that table is empty for all 409 rows, so the family must be
#: derived from ``prompt_text`` here rather than read off the column.
INTENT_BUCKETS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    ("ranking", "排行榜单", ("排名", "排行", "榜单", "十大", "前十", "TOP", "top")),
    ("recommendation", "推荐选型", ("推荐", "哪家", "哪个", "哪些", "哪几家", "怎么选", "选择")),
    ("reputation", "口碑评价", ("靠谱", "口碑", "好用", "怎么样", "评价", "好不好")),
    ("price", "价格费用", ("多少钱", "价格", "费用", "报价", "收费", "贵不贵")),
    ("howto", "方法流程", ("怎么做", "如何", "方法", "步骤", "流程", "注意事项")),
)

_INTENT_LABELS: Final[dict[str, str]] = {key: label for key, label, _ in INTENT_BUCKETS}


def derive_intent_bucket(text: str) -> str:
    """Map one keyword / question to its intent bucket; ``general`` when unsure."""
    value = str(text or "")
    if not value.strip():
        return "general"
    for key, _label, tokens in INTENT_BUCKETS:
        if any(token in value for token in tokens):
            return key
    return "general"


def intent_label(bucket: str) -> str:
    return _INTENT_LABELS.get(str(bucket or ""), "通用问法")


def _intent_tokens(bucket: str) -> tuple[str, ...]:
    for key, _label, tokens in INTENT_BUCKETS:
        if key == bucket:
            return tokens
    return ()


# ---------------------------------------------------------------------------
# mix model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MixEntry:
    domain: str
    citations: int
    share: float
    role: str
    role_label: str
    family_key: str
    family_label: str
    is_trunk: bool
    self_serve: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "citations": self.citations,
            "share": round(self.share, 4),
            "share_pct": round(self.share * 100, 1),
            "role": self.role,
            "role_label": self.role_label,
            "family_key": self.family_key,
            "family_label": self.family_label,
            "is_trunk": self.is_trunk,
            "self_serve": self.self_serve,
        }


@dataclass
class QuestionFamilyMix:
    keyword: str
    industry: str
    intent_bucket: str
    source: str  # question_family | industry | global | unavailable
    window_days: int
    total_citations: int
    entries: list[MixEntry] = field(default_factory=list)
    degraded_reason: str = ""
    version: str = QUESTION_FAMILY_MIX_VERSION

    @property
    def trunk_share(self) -> float:
        if not self.total_citations:
            return 0.0
        return sum(e.share for e in self.entries if e.is_trunk)

    @property
    def vertical_share(self) -> float:
        return max(0.0, 1.0 - self.trunk_share)

    def role_shares(self) -> dict[str, float]:
        shares: dict[str, float] = {}
        for entry in self.entries:
            shares[entry.role] = shares.get(entry.role, 0.0) + entry.share
        return dict(sorted(shares.items(), key=lambda kv: -kv[1]))

    def explanation(self, top_n: int = 3) -> str:
        """代理面话术：凭什么这么配 —— 带真实数字，不讲玄学。"""
        if not self.entries:
            return "暂时没有足够的真实被引数据，本次组合回落到通用配比。"
        head = "、".join(
            f"{e.domain} {e.share_pct_text}" for e in self.entries[:max(1, top_n)]
        )
        scope = {
            "question_family": f"客户问「{self.keyword or self.industry or '这类问题'}」这类问题时，",
            "industry": f"{self.industry or '本行业'}整体来看，",
            "global": "全行业来看，",
        }.get(self.source, "全行业来看，")
        return (f"{scope}AI 最常引用 {head}"
                f"（近 {self.window_days} 天共 {self.total_citations} 次引用）")

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "keyword": self.keyword,
            "industry": self.industry,
            "intent_bucket": self.intent_bucket,
            "intent_label": intent_label(self.intent_bucket),
            "source": self.source,
            "window_days": self.window_days,
            "total_citations": self.total_citations,
            "trunk_share": round(self.trunk_share, 4),
            "vertical_share": round(self.vertical_share, 4),
            "role_shares": {k: round(v, 4) for k, v in self.role_shares().items()},
            "entries": [e.as_dict() for e in self.entries],
            "explanation": self.explanation(),
            "degraded_reason": self.degraded_reason,
        }


# ``MixEntry.share_pct_text`` as a light helper without bloating the dataclass.
def _share_pct_text(self: MixEntry) -> str:
    return f"{round(self.share * 100)}%"


MixEntry.share_pct_text = property(_share_pct_text)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------
def _entry_for(domain: str, citations: int, total: int, raw_domain: str = "") -> MixEntry:
    normalized = normalize_domain(domain) or str(domain or "")
    # Resolve the family from the *raw* host so 微信公众号 (ugc/self-serve) is not
    # folded into 腾讯网 (portal) by domain normalisation.
    family: DomainFamilyPrior | None = family_for_domain(raw_domain or domain or normalized)
    role = family.role if family else "vertical"
    return MixEntry(
        domain=normalized,
        citations=int(citations or 0),
        share=(float(citations) / float(total)) if total else 0.0,
        role=role,
        role_label=ROLE_LABELS.get(role, role),
        family_key=family.key if family else "",
        family_label=family.label if family else "",
        is_trunk=bool(family and family.is_trunk),
        self_serve=bool(family and family.self_serve),
    )


def _rows_to_mix(rows: Sequence[dict[str, Any]], limit: int) -> tuple[list[MixEntry], int]:
    grouped: dict[str, int] = {}
    #: normalized key -> the most-cited raw host, kept for family resolution.
    representative: dict[str, tuple[int, str]] = {}
    for row in rows or ():
        raw = str(row.get("domain") or "").strip().lower()
        domain = normalize_domain(raw)
        if not domain:
            continue
        count = int(row.get("citations") or 0)
        grouped[domain] = grouped.get(domain, 0) + count
        if raw and (domain not in representative or count > representative[domain][0]):
            representative[domain] = (count, raw)
    total = sum(grouped.values())
    ranked = sorted(grouped.items(), key=lambda kv: (-kv[1], kv[0]))[: max(1, limit)]
    return (
        [_entry_for(d, c, total, raw_domain=representative.get(d, (0, d))[1]) for d, c in ranked],
        total,
    )


def mix_from_rows(
    rows: Sequence[dict[str, Any]],
    *,
    keyword: str = "",
    industry: str = "",
    source: str = "question_family",
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = 12,
) -> QuestionFamilyMix:
    """Build a mix from already-fetched ``{domain, citations}`` rows.

    Public so the combination rules can be exercised against real measured
    distributions without a database round-trip.
    """
    entries, total = _rows_to_mix(rows, limit)
    return QuestionFamilyMix(
        keyword=str(keyword or ""),
        industry=str(industry or ""),
        intent_bucket=derive_intent_bucket(keyword),
        source=source if total else "unavailable",
        window_days=int(window_days),
        total_citations=total,
        entries=entries,
    )


def _fetch(sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, list(params))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


_BASE_SELECT = """
    SELECT a.domain AS domain, COUNT(*) AS citations
      FROM geo_research_article_citations c
      JOIN geo_research_articles a ON a.id = c.article_id
"""
_BASE_WHERE = """
     WHERE c.cited_at >= NOW() - make_interval(days => %s)
       AND COALESCE(a.domain, '') <> ''
"""


def build_question_family_mix(
    *,
    keyword: str = "",
    industry: str = "",
    window_days: int | None = None,
    limit: int = 12,
) -> QuestionFamilyMix:
    """Real cited-domain mix for this question family, with a fallback chain.

    Never raises: publishing must not break because the research flywheel is
    momentarily unavailable — a degraded mix simply reports ``source`` and
    ``degraded_reason`` so the caller can say so out loud.
    """
    window = max(7, min(720, int(window_days or DEFAULT_WINDOW_DAYS)))
    bucket = derive_intent_bucket(keyword)
    industry_value = str(industry or "").strip()
    mix = QuestionFamilyMix(
        keyword=str(keyword or ""),
        industry=industry_value,
        intent_bucket=bucket,
        source="unavailable",
        window_days=window,
        total_citations=0,
    )

    tokens = _intent_tokens(bucket)
    industry_token = industry_value or str(keyword or "").strip()

    # ① question family = 行业词 + 同意图形态的题面
    if tokens and industry_token:
        try:
            intent_clause = " OR ".join(["p.prompt_text LIKE %s"] * len(tokens))
            rows = _fetch(
                _BASE_SELECT
                + "      JOIN geo_research_prompts p ON p.id = c.prompt_id\n"
                + _BASE_WHERE
                + f"       AND p.prompt_text LIKE %s\n       AND ({intent_clause})\n"
                + "     GROUP BY a.domain",
                [window, f"%{industry_token}%", *[f"%{t}%" for t in tokens]],
            )
            entries, total = _rows_to_mix(rows, limit)
            if total >= MIN_FAMILY_CITATIONS:
                mix.entries, mix.total_citations, mix.source = entries, total, "question_family"
                return mix
        except Exception as exc:
            logger.warning("[question-mix] 问题族聚合失败: %s", exc)
            mix.degraded_reason = f"question_family_unavailable:{type(exc).__name__}"

    # ② industry mix
    if industry_value:
        try:
            rows = _fetch(
                _BASE_SELECT + _BASE_WHERE + "       AND a.primary_industry = %s\n     GROUP BY a.domain",
                [window, industry_value],
            )
            entries, total = _rows_to_mix(rows, limit)
            if total >= MIN_FAMILY_CITATIONS:
                mix.entries, mix.total_citations, mix.source = entries, total, "industry"
                return mix
        except Exception as exc:
            logger.warning("[question-mix] 行业聚合失败: %s", exc)
            mix.degraded_reason = mix.degraded_reason or f"industry_unavailable:{type(exc).__name__}"

    # ③ global mix
    try:
        rows = _fetch(_BASE_SELECT + _BASE_WHERE + "     GROUP BY a.domain", [window])
        entries, total = _rows_to_mix(rows, limit)
        if total:
            mix.entries, mix.total_citations, mix.source = entries, total, "global"
            return mix
        mix.degraded_reason = mix.degraded_reason or "no_citation_rows_in_window"
    except Exception as exc:
        logger.warning("[question-mix] 全局聚合失败: %s", exc)
        mix.degraded_reason = mix.degraded_reason or f"global_unavailable:{type(exc).__name__}"
    return mix


# ---------------------------------------------------------------------------
# combination plan  (主干 N + 垂类 M)
# ---------------------------------------------------------------------------
@dataclass
class PlannedSlot:
    domain: str
    role: str
    role_label: str
    is_trunk: bool
    share: float
    citations: int
    self_serve: bool
    family_key: str
    family_label: str
    #: filled by :func:`apply_inventory_substitution`
    fulfilled_by: str = ""
    fulfilled_media_id: int | None = None
    substituted: bool = False
    substitution_note: str = ""
    self_serve_action: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "role": self.role,
            "role_label": self.role_label,
            "is_trunk": self.is_trunk,
            "share_pct": round(self.share * 100, 1),
            "citations": self.citations,
            "fulfilled_by": self.fulfilled_by,
            "fulfilled_media_id": self.fulfilled_media_id,
            "substituted": self.substituted,
            "substitution_note": self.substitution_note,
            "self_serve_action": self.self_serve_action,
        }


@dataclass
class CombinationPlan:
    mix: QuestionFamilyMix
    total_slots: int
    trunk_slots: int
    vertical_slots: int
    slots: list[PlannedSlot] = field(default_factory=list)
    advisory: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": QUESTION_FAMILY_MIX_VERSION,
            "advisory": self.advisory,
            "total_slots": self.total_slots,
            "trunk_slots": self.trunk_slots,
            "vertical_slots": self.vertical_slots,
            "reason": self.mix.explanation(),
            "mix": self.mix.as_dict(),
            "slots": [s.as_dict() for s in self.slots],
            "substitutions": [s.as_dict() for s in self.slots if s.substituted],
            "self_serve_offers": [
                s.as_dict() for s in self.slots if s.self_serve_action
            ],
        }


def plan_combination(mix: QuestionFamilyMix, total_slots: int = 8) -> CombinationPlan:
    """Turn an observed mix into a default 主干 N + 垂类 M allocation.

    The split is the mix's own trunk/vertical share, rounded so that whenever
    both sides exist in the data both get at least one slot — a 100%-trunk or
    100%-vertical plan is exactly the错配 this engine exists to stop.
    """
    slots = max(1, int(total_slots or 1))
    if not mix.entries:
        return CombinationPlan(mix=mix, total_slots=slots, trunk_slots=slots, vertical_slots=0)

    has_trunk = any(e.is_trunk for e in mix.entries)
    has_vertical = any(not e.is_trunk for e in mix.entries)
    trunk_n = int(round(mix.trunk_share * slots))
    if has_trunk and has_vertical:
        trunk_n = max(1, min(slots - 1, trunk_n))
    elif has_trunk:
        trunk_n = slots
    else:
        trunk_n = 0
    vertical_n = slots - trunk_n

    trunk_pool = [e for e in mix.entries if e.is_trunk]
    vertical_pool = [e for e in mix.entries if not e.is_trunk]
    chosen: list[MixEntry] = trunk_pool[:trunk_n] + vertical_pool[:vertical_n]
    # If one side is thinner than its allocation, let the other side use the rest.
    if len(chosen) < slots:
        for entry in mix.entries:
            if len(chosen) >= slots:
                break
            if entry not in chosen:
                chosen.append(entry)

    plan = CombinationPlan(
        mix=mix, total_slots=slots,
        trunk_slots=sum(1 for e in chosen if e.is_trunk),
        vertical_slots=sum(1 for e in chosen if not e.is_trunk),
    )
    plan.slots = [
        PlannedSlot(
            domain=e.domain, role=e.role, role_label=e.role_label, is_trunk=e.is_trunk,
            share=e.share, citations=e.citations, self_serve=e.self_serve,
            family_key=e.family_key, family_label=e.family_label,
        )
        for e in chosen
    ]
    return plan


# ---------------------------------------------------------------------------
# 缺货降位三规则
# ---------------------------------------------------------------------------
#: (domain fragment / display name) -> inventory search keyword.
DOMAIN_TO_INVENTORY_KEYWORD: Final[dict[str, str]] = {
    "sohu.com": "搜狐", "163.com": "网易", "sina.com.cn": "新浪", "sina.cn": "新浪",
    "toutiao.com": "头条", "cnblogs.com": "博客园", "csdn.net": "CSDN",
    "51cto.com": "51CTO", "oschina.net": "开源中国", "juejin.cn": "掘金",
    "zhihu.com": "知乎", "zhuanlan.zhihu.com": "知乎", "mp.weixin.qq.com": "公众号",
    "smzdm.com": "什么值得买", "ithome.com": "IT之家", "pcauto.com.cn": "太平洋",
    "pconline.com.cn": "太平洋", "pchouse.com.cn": "太平洋家居",
    "autohome.com.cn": "汽车之家", "bitauto.com": "易车", "yiche.com": "易车",
    "zol.com.cn": "中关村", "jiemian.com": "界面", "36kr.com": "36氪",
    "maigoo.com": "买购", "chinapp.com": "品牌网", "to8to.com": "土巴兔",
    "shejiben.com": "设计本", "66law.cn": "华律", "fang.com": "房天下",
    "eastmoney.com": "东方财富", "hexun.com": "和讯",
}

SELF_SERVE_LABELS: Final[dict[str, str]] = {
    "cnblogs.com": "博客园", "zhihu.com": "知乎", "zhuanlan.zhihu.com": "知乎专栏",
    "mp.weixin.qq.com": "微信公众号", "juejin.cn": "掘金", "csdn.net": "CSDN",
}


def inventory_keyword_for(domain: str) -> str:
    normalized = normalize_domain(domain) or str(domain or "")
    if normalized in DOMAIN_TO_INVENTORY_KEYWORD:
        return DOMAIN_TO_INVENTORY_KEYWORD[normalized]
    for frag, keyword in DOMAIN_TO_INVENTORY_KEYWORD.items():
        if frag in normalized or normalized in frag:
            return keyword
    family = family_for_domain(normalized)
    return family.label if family else normalized


def _same_role_candidates(role: str, exclude: set[str]) -> list[str]:
    """Same-role domains ranked by seed prior — rule ① substitution pool."""
    out: list[tuple[float, str]] = []
    for family in SEED_DOMAIN_FAMILIES:
        if family.role != role:
            continue
        for frag in family.domain_fragments:
            if frag not in exclude:
                out.append((family.prior, frag))
    return [d for _p, d in sorted(out, key=lambda kv: -kv[0])]


def apply_inventory_substitution(
    plan: CombinationPlan,
    *,
    inventory_lookup: Callable[[str], dict[str, Any] | None],
    industry: str = "",
    record_demand: bool = True,
) -> CombinationPlan:
    """Fill each planned slot, applying the three demotion rules.

    ``inventory_lookup`` takes an inventory keyword and returns a media row (or
    ``None``).  It is injected so the rules can be tested without a database.
    """
    taken: set[str] = set()

    for slot in plan.slots:
        primary_kw = inventory_keyword_for(slot.domain)
        row = inventory_lookup(primary_kw)
        if row and str(row.get("media_name") or "") not in taken:
            slot.fulfilled_by = str(row.get("media_name") or primary_kw)
            slot.fulfilled_media_id = row.get("id") or row.get("media_id")
            taken.add(slot.fulfilled_by)
            continue

        # --- rule ①: substitute inside the same role, never across roles -----
        substitute_row = None
        substitute_domain = ""
        for candidate in _same_role_candidates(slot.role, exclude={slot.domain}):
            candidate_row = inventory_lookup(inventory_keyword_for(candidate))
            if candidate_row and str(candidate_row.get("media_name") or "") not in taken:
                substitute_row, substitute_domain = candidate_row, candidate
                break

        # --- rule ③: self-serve path for platforms the user can post on ------
        if slot.self_serve or slot.domain in SELF_SERVE_LABELS:
            label = SELF_SERVE_LABELS.get(slot.domain, slot.family_label or slot.domain)
            slot.self_serve_action = {
                "id": "self_serve_publish",
                "label": f"用自己的{label}账号发（内容我们出）",
                "type": "nav",
                "target": "publish_center_self_serve",
                "platform": label,
            }

        if substitute_row:
            slot.substituted = True
            slot.fulfilled_by = str(substitute_row.get("media_name") or substitute_domain)
            slot.fulfilled_media_id = substitute_row.get("id") or substitute_row.get("media_id")
            taken.add(slot.fulfilled_by)
            # --- rule ②: say it out loud, never substitute silently ----------
            slot.substitution_note = (
                f"AI 常引用的 {slot.domain}（{slot.role_label}）我们暂时买不到发布位，"
                f"已换成同类的 {inventory_keyword_for(substitute_domain)}"
            )
        else:
            slot.substitution_note = (
                f"AI 常引用的 {slot.domain}（{slot.role_label}）我们暂时买不到发布位，同类的也没货了"
            )

        # --- rule ②: count the demand so procurement follows real signal -----
        if record_demand:
            try:
                record_substitution_demand(
                    missing_domain=slot.domain,
                    role=slot.role,
                    family_key=slot.family_key,
                    family_label=slot.family_label,
                    citations=slot.citations,
                    share=slot.share,
                    substituted_domain=substitute_domain,
                    substituted_media_name=slot.fulfilled_by,
                    self_serve=bool(slot.self_serve_action),
                    industry=industry,
                )
            except Exception as exc:  # pragma: no cover - telemetry never blocks
                logger.warning("[question-mix] 需求计数写入失败 %s: %s", slot.domain, exc)
    return plan


def record_substitution_demand(
    *,
    missing_domain: str,
    role: str,
    family_key: str = "",
    family_label: str = "",
    citations: int = 0,
    share: float = 0.0,
    substituted_domain: str = "",
    substituted_media_name: str = "",
    self_serve: bool = False,
    industry: str = "",
) -> None:
    """UPSERT one demand tick (rule ②)."""
    from db.connection import get_connection

    domain = normalize_domain(missing_domain) or str(missing_domain or "")
    if not domain:
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO geo_media_substitution_demand (
                missing_domain, missing_label, role, family_key, family_label,
                citation_count, citation_share, substituted_domain,
                substituted_media_name, self_serve, industry, demand_count
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
            ON CONFLICT (missing_domain, industry) DO UPDATE SET
                demand_count = geo_media_substitution_demand.demand_count + 1,
                citation_count = EXCLUDED.citation_count,
                citation_share = EXCLUDED.citation_share,
                substituted_domain = EXCLUDED.substituted_domain,
                substituted_media_name = EXCLUDED.substituted_media_name,
                self_serve = EXCLUDED.self_serve,
                last_seen_at = NOW()
            """,
            (
                domain, inventory_keyword_for(domain), str(role or ""), str(family_key or ""),
                str(family_label or ""), int(citations or 0), float(share or 0.0),
                str(substituted_domain or ""), str(substituted_media_name or "")[:200],
                bool(self_serve), str(industry or ""),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def top_substitution_demand(limit: int = 50) -> list[dict[str, Any]]:
    """补货清单排序：**被引强度 × 降位次数**（工单 T2 规则② / T4 回流）。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT missing_domain, missing_label, role, family_label,
                   citation_count, citation_share, demand_count, self_serve,
                   substituted_media_name, industry, first_seen_at, last_seen_at,
                   (citation_count * demand_count) AS demand_score
              FROM geo_media_substitution_demand
             ORDER BY demand_score DESC, citation_count DESC
             LIMIT %s
            """,
            (max(1, min(500, int(limit or 50))),),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
