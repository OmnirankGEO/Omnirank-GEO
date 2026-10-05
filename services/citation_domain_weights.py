"""WP12 P0-1 · Real citation-derived publishing domain weights.

Owner decision D12 (Master SSOT v2.4 ③④) froze two facts measured on the
production flywheel:

* our own published URLs are almost never cited (185 published -> 1 cited),
  and the domains we publish on barely overlap with the domains AI engines
  actually cite;
* the hand-maintained ``domain_tier`` whitelist has no predictive power
  (whitelist 1.51 citations/article, rank 5.29 vs gray 1.46 / 5.70).

So the primary ordering signal for媒体推荐 must come from *observed* citations
(``geo_research_article_citations``), not from a human whitelist.  This module
owns that aggregation and nothing else: it reads the flywheel, produces one
per-domain strength table, and exposes a name -> strength resolver that the
placement recommender consumes.

Non-goals (deliberate):

* it never blocks anything — a domain with no citation data simply gets the
  neutral prior, it is not filtered out (SSOT §11 A1/O1: new checks are
  advisory, hard blocks stay limited to the four boundaries);
* it never claims causality.  Citation strength is a historical observation
  used for ranking, not a promise about a future article.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
import threading
import time
from typing import Any, Final, Iterable, Sequence

logger = logging.getLogger("GEO-CitationDomainWeights")


CITATION_DOMAIN_WEIGHTS_VERSION: Final = "geo-citation-domain-weights-v1.0"

# Default observation window.  Configurable per call and via system settings so
# operations can widen it when a quarter is sparse.
DEFAULT_WINDOW_DAYS: Final = 90
MIN_WINDOW_DAYS: Final = 7
MAX_WINDOW_DAYS: Final = 720

_CACHE_TTL_SECONDS: Final = 900


# ---------------------------------------------------------------------------
# Seed priors — used ONLY where we have no observed citation for a domain.
#
# These are the domain families Owner named in v2.4 ③ as the ones AI engines
# actually cite.  They are a cold-start prior, never a replacement for real
# data: as soon as a domain has observed citations, the observed value wins and
# ``source`` flips to "observed".
# ---------------------------------------------------------------------------
#: Citation *roles*.  A domain's role is what it does inside an AI answer, and
#: it is the unit of substitution: when the #3 cited domain (博客园) has no
#: inventory we must fall back to another **技术社区**, not to the globally
#: next-best portal — swapping a community slot for a portal slot collapses the
#: mixed structure the answer was actually assembled from (工单 T2 规则①).
ROLE_PORTAL: Final = "portal"
ROLE_TECH_COMMUNITY: Final = "tech_community"
ROLE_UGC_QA: Final = "ugc_qa"
ROLE_VERTICAL: Final = "vertical"
ROLE_RANKING_DIRECTORY: Final = "ranking_directory"

#: 面向代理的说法 —— 全部说人话（CLAUDE.md B.6：工程术语全站翻译）。
#: "门户/垂类/UGC" 是内部行话，代理界面一律不出现。
ROLE_LABELS: Final[dict[str, str]] = {
    ROLE_PORTAL: "大平台",
    ROLE_TECH_COMMUNITY: "技术社区",
    ROLE_UGC_QA: "问答和公众号",
    ROLE_VERTICAL: "行业网站",
    ROLE_RANKING_DIRECTORY: "榜单网站",
}

#: "主干" = the general-purpose backbone AI answers are built on
#: (evidence §0: sohu 1969 / 163 1483 / cnblogs 961 / 知乎 619 / toutiao 425).
#: This replaces the old hand-written ``_GENERIC_PLATFORMS`` 13-name set.
TRUNK_ROLES: Final[frozenset[str]] = frozenset(
    {ROLE_PORTAL, ROLE_TECH_COMMUNITY, ROLE_UGC_QA}
)


@dataclass(frozen=True)
class DomainFamilyPrior:
    key: str
    label: str
    prior: float
    domain_fragments: tuple[str, ...]
    name_fragments: tuple[str, ...]
    role: str = ROLE_PORTAL
    #: Platforms an end user can register on and publish to themselves.  When
    #: we have no inventory we still hand them the path (工单 T2 规则③).
    self_serve: bool = False

    @property
    def is_trunk(self) -> bool:
        return self.role in TRUNK_ROLES

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)


SEED_DOMAIN_FAMILIES: Final[tuple[DomainFamilyPrior, ...]] = (
    DomainFamilyPrior(
        key="party_media_portal",
        label="新闻门户与地方党媒",
        prior=0.72,
        domain_fragments=(
            "dzwww.com", "people.com.cn", "xinhuanet.com", "cnr.cn", "gmw.cn",
            "ce.cn", "china.com.cn", "chinanews.com", "thepaper.cn", "jfdaily.com",
            "hubpd.com", "dahe.cn", "rednet.cn", "sdnews.com.cn", "qlwb.com.cn",
            "yangtse.com", "cnhubei.com", "sznews.com", "dzng.com",
        ),
        name_fragments=("大众网", "人民网", "新华网", "央广网", "光明网", "中国网",
                        "中新网", "澎湃", "党媒", "日报", "晚报", "都市报"),
        role=ROLE_PORTAL,
    ),
    DomainFamilyPrior(
        key="sohu_sina_family",
        label="搜狐/新浪系",
        prior=0.70,
        domain_fragments=("sohu.com", "sina.com.cn", "sina.cn", "weibo.com", "finance.sina"),
        name_fragments=("搜狐", "新浪", "微博"),
        role=ROLE_PORTAL,
    ),
    DomainFamilyPrior(
        key="netease_163",
        label="网易 163",
        prior=0.66,
        domain_fragments=("163.com", "126.net"),
        name_fragments=("网易",),
        role=ROLE_PORTAL,
    ),
    DomainFamilyPrior(
        key="toutiao_family",
        label="今日头条系",
        prior=0.60,
        domain_fragments=("toutiao.com", "iesdouyin.com", "mbd.baidu.com", "baijiahao"),
        name_fragments=("头条", "百家号", "抖音", "百度"),
        role=ROLE_PORTAL,
    ),
    # 腾讯/凤凰只挂名称片段：``mp.weixin.qq.com`` 归一化后就是 ``qq.com``，
    # 若把 qq.com 写成域片段，公众号会被误判成腾讯网门户。
    DomainFamilyPrior(
        key="commercial_portal",
        label="腾讯/凤凰等商业门户",
        prior=0.60,
        domain_fragments=("ifeng.com", "huanqiu.com", "cctv.com", "news.qq.com"),
        name_fragments=("腾讯", "凤凰", "环球网", "央视"),
        role=ROLE_PORTAL,
    ),
    DomainFamilyPrior(
        key="business_media",
        label="财经与商业媒体",
        prior=0.62,
        domain_fragments=("jiemian.com", "36kr.com", "yicai.com", "caixin.com",
                          "eastmoney.com", "hexun.com", "stcn.com"),
        name_fragments=("界面", "36氪", "第一财经", "财新", "东方财富", "和讯"),
        role=ROLE_PORTAL,
    ),
    # Split out of the old catch-all ``vertical_industry_site``: 博客园/CSDN are
    # the #3 cited domain family overall (cnblogs 961 citations) and must be
    # substitutable only by each other.
    DomainFamilyPrior(
        key="tech_community",
        label="技术社区",
        prior=0.60,
        domain_fragments=("cnblogs.com", "csdn.net", "51cto.com", "oschina.net",
                          "juejin.cn", "segmentfault.com", "infoq.cn"),
        name_fragments=("博客园", "CSDN", "51CTO", "开源中国", "掘金", "思否"),
        role=ROLE_TECH_COMMUNITY,
        self_serve=True,
    ),
    DomainFamilyPrior(
        key="ugc_qa_community",
        label="UGC 问答与订阅号",
        prior=0.58,
        domain_fragments=("zhihu.com", "zhuanlan.zhihu.com", "mp.weixin.qq.com",
                          "baike.baidu.com", "smzdm.com", "douban.com"),
        name_fragments=("知乎", "公众号", "微信", "百科", "什么值得买", "豆瓣"),
        role=ROLE_UGC_QA,
        self_serve=True,
    ),
    DomainFamilyPrior(
        key="vertical_industry_site",
        label="行业垂直站",
        prior=0.58,
        domain_fragments=("autohome.com.cn", "pconline.com.cn", "pchouse.com.cn",
                          "pcauto.com.cn", "ithome.com", "zol.com.cn",
                          "bitauto.com", "yiche.com", "jd.com", "to8to.com",
                          "shejiben.com", "66law.cn", "bohe.cn", "youlai.cn"),
        name_fragments=("汽车之家", "太平洋", "IT之家", "中关村", "易车", "京东",
                        "土巴兔", "设计本", "华律", "有来"),
        role=ROLE_VERTICAL,
    ),
    DomainFamilyPrior(
        key="brand_ranking_directory",
        label="品牌库与排行榜站",
        prior=0.56,
        domain_fragments=("chinapp.com", "maigoo.com", "cnpp.cn", "10jqka.com.cn",
                          "china-10.com", "cn10.cn"),
        name_fragments=("品牌网", "品牌库", "买购", "十大品牌", "排行榜"),
        role=ROLE_RANKING_DIRECTORY,
    ),
)

#: domain-family key -> prior, for O(1) lookups by consumers.
DOMAIN_FAMILY_BY_KEY: Final[dict[str, DomainFamilyPrior]] = {
    family.key: family for family in SEED_DOMAIN_FAMILIES
}


def family_for_domain(domain: str = "", display_name: str = "") -> DomainFamilyPrior | None:
    """Public resolver: which seed family does this domain/name belong to.

    The **raw** host is tried first: ``normalize_domain`` folds
    ``mp.weixin.qq.com`` down to ``qq.com``, which would lose the difference
    between 微信公众号 (UGC, self-serve) and 腾讯网 (portal).
    """
    raw = str(domain or "").strip().lower()
    if raw:
        hit = _match_seed_family(raw, display_name)
        if hit is not None:
            return hit
    return _match_seed_family(normalize_domain(domain), display_name)


def is_trunk_domain(domain: str = "", display_name: str = "") -> bool:
    """Replaces the hand-written ``_GENERIC_PLATFORMS`` membership test."""
    family = family_for_domain(domain, display_name)
    return bool(family and family.is_trunk)

# Domains our own publishing pipeline historically used that the flywheel shows
# are NOT cited.  They are not banned — they simply get no prior boost, so real
# observed data (or the neutral prior) decides.
KNOWN_LOW_CITATION_PUBLISH_DOMAINS: Final[frozenset[str]] = frozenset({
    "to8to.com", "zhihu.com", "cjnew.com", "gdqichew.com", "ctrip.com", "fang.com",
})

NEUTRAL_PRIOR: Final = 0.30


@dataclass(frozen=True)
class DomainCitationStat:
    domain: str
    citation_count: int
    cited_article_count: int
    avg_rank_in_response: float | None
    engine_breakdown: dict[str, int]
    strength: float
    source: str  # "observed" | "seed_prior" | "neutral"
    family_key: str = ""
    family_label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "citation_count": self.citation_count,
            "cited_article_count": self.cited_article_count,
            "avg_rank_in_response": self.avg_rank_in_response,
            "engine_breakdown": dict(self.engine_breakdown),
            "strength": round(self.strength, 4),
            "strength_label": strength_label(self.strength),
            "source": self.source,
            "family_key": self.family_key,
            "family_label": self.family_label,
        }


@dataclass
class CitationDomainWeightTable:
    version: str
    window_days: int
    industry: str
    generated_at: float
    total_citations: int
    observed_domain_count: int
    domains: dict[str, DomainCitationStat] = field(default_factory=dict)
    degraded_reason: str = ""

    @property
    def has_observed_data(self) -> bool:
        return self.observed_domain_count > 0

    def summary(self, top_n: int = 12) -> dict[str, Any]:
        ranked = sorted(
            (s for s in self.domains.values() if s.source == "observed"),
            key=lambda s: (-s.citation_count, s.avg_rank_in_response or 99, s.domain),
        )[: max(0, int(top_n))]
        return {
            "version": self.version,
            "window_days": self.window_days,
            "industry": self.industry,
            "total_citations": self.total_citations,
            "observed_domain_count": self.observed_domain_count,
            "has_observed_data": self.has_observed_data,
            "degraded_reason": self.degraded_reason,
            "top_domains": [s.as_dict() for s in ranked],
        }


def strength_label(strength: float) -> str:
    """Customer-facing wording.  Never a percentage promise."""
    value = float(strength or 0.0)
    if value >= 0.70:
        return "引用强"
    if value >= 0.45:
        return "引用中"
    if value > NEUTRAL_PRIOR:
        return "引用弱"
    return "暂无引用数据"


# ---------------------------------------------------------------------------
# domain normalisation
# ---------------------------------------------------------------------------
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_WWW_RE = re.compile(r"^(?:www|m|wap|amp|mobile)\.", re.IGNORECASE)

# Public multi-label suffixes we must not truncate to two labels.
_MULTI_LABEL_SUFFIXES: Final[tuple[str, ...]] = (
    ".com.cn", ".net.cn", ".org.cn", ".gov.cn", ".edu.cn", ".ac.cn",
    ".co.uk", ".com.hk", ".com.tw",
)


def normalize_domain(raw: Any) -> str:
    """Return a lowercase registrable-ish domain for grouping.

    ``dongying.dzwww.com`` -> ``dzwww.com`` so a local party-media portal and
    its city sub-site aggregate into one weight instead of splitting the
    already-thin citation signal.
    """
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    text = _SCHEME_RE.sub("", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split("@")[-1]
    text = text.split(":", 1)[0]
    text = text.strip(".")
    if not text:
        return ""
    text = _WWW_RE.sub("", text)
    labels = [p for p in text.split(".") if p]
    if len(labels) <= 2:
        return ".".join(labels)
    for suffix in _MULTI_LABEL_SUFFIXES:
        if text.endswith(suffix):
            head = text[: -len(suffix)]
            head_labels = [p for p in head.split(".") if p]
            if not head_labels:
                return text
            return f"{head_labels[-1]}{suffix}"
    return ".".join(labels[-2:])


def _match_seed_family(domain: str, display_name: str = "") -> DomainFamilyPrior | None:
    dom = (domain or "").lower()
    name = str(display_name or "")
    for family in SEED_DOMAIN_FAMILIES:
        if dom and any(dom == frag or dom.endswith("." + frag) or frag in dom
                       for frag in family.domain_fragments):
            return family
        if name and any(frag and frag in name for frag in family.name_fragments):
            return family
    return None


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------
def _clamp_window(window_days: Any) -> int:
    try:
        value = int(window_days)
    except (TypeError, ValueError):
        value = DEFAULT_WINDOW_DAYS
    return max(MIN_WINDOW_DAYS, min(MAX_WINDOW_DAYS, value))


def _configured_window_days() -> int:
    """Admin-governable window (SSOT §9.5 style: config, not hardcoded)."""
    try:
        from config.settings_manager import get_current_settings

        settings = get_current_settings()
        raw = getattr(settings, "citation_domain_window_days", None)
        if raw:
            return _clamp_window(raw)
    except Exception:
        pass
    return DEFAULT_WINDOW_DAYS


def _strength_from_observation(
    citation_count: int,
    max_citation_count: int,
    avg_rank: float | None,
) -> float:
    """Map observed citations to 0..1 without pretending to be a probability.

    Volume carries most of the signal; being cited *early* in the answer
    (``rank_in_response`` 1 = primary source, the single success case in v2.4 ②)
    adds a bounded bonus.
    """
    if citation_count <= 0 or max_citation_count <= 0:
        return NEUTRAL_PRIOR
    # log-ish compression: one head domain must not flatten the whole tail.
    ratio = float(citation_count) / float(max_citation_count)
    volume = ratio ** 0.5
    rank_bonus = 0.0
    if avg_rank is not None and avg_rank > 0:
        # rank 1 -> +0.18, rank 5 -> +0.06, rank >=10 -> 0
        rank_bonus = max(0.0, min(0.18, 0.20 - (float(avg_rank) - 1.0) * 0.02))
    return max(0.0, min(1.0, 0.35 + 0.47 * volume + rank_bonus))


def _fetch_citation_aggregates(
    window_days: int, industry: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (per domain+platform rows, per domain distinct-article rows).

    Distinct article counts are queried separately on purpose: summing a
    per-platform ``COUNT(DISTINCT article_id)`` would double count any article
    cited by more than one engine.
    """
    from db.connection import get_connection

    industry_value = str(industry or "").strip()
    industry_clause = "AND a.primary_industry = %s" if industry_value else ""

    def _params() -> list[Any]:
        params: list[Any] = [window_days]
        if industry_value:
            params.append(industry_value)
        return params

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT a.domain                           AS domain,
                   c.platform                         AS platform,
                   COUNT(*)                           AS citation_count,
                   AVG(NULLIF(c.rank_in_response, 0)) AS avg_rank_in_response
              FROM geo_research_article_citations c
              JOIN geo_research_articles a ON a.id = c.article_id
             WHERE c.cited_at >= NOW() - make_interval(days => %s)
               {industry_clause}
               AND COALESCE(a.domain, '') <> ''
             GROUP BY a.domain, c.platform
            """,
            _params(),
        )
        platform_rows = [dict(row) for row in cur.fetchall()]

        cur.execute(
            f"""
            SELECT a.domain                    AS domain,
                   COUNT(DISTINCT c.article_id) AS cited_article_count
              FROM geo_research_article_citations c
              JOIN geo_research_articles a ON a.id = c.article_id
             WHERE c.cited_at >= NOW() - make_interval(days => %s)
               {industry_clause}
               AND COALESCE(a.domain, '') <> ''
             GROUP BY a.domain
            """,
            _params(),
        )
        article_rows = [dict(row) for row in cur.fetchall()]
        return platform_rows, article_rows
    finally:
        conn.close()


def build_citation_domain_weights(
    *,
    window_days: int | None = None,
    industry: str = "",
) -> CitationDomainWeightTable:
    """Aggregate observed citations into one per-domain weight table.

    Any DB failure degrades to a seed-prior-only table (``degraded_reason``
    set) instead of raising: publishing must never be blocked because the
    research flywheel is momentarily unavailable (SSOT §11 O1).
    """
    effective_window = _clamp_window(window_days if window_days is not None else _configured_window_days())
    table = CitationDomainWeightTable(
        version=CITATION_DOMAIN_WEIGHTS_VERSION,
        window_days=effective_window,
        industry=str(industry or ""),
        generated_at=time.time(),
        total_citations=0,
        observed_domain_count=0,
    )
    try:
        platform_rows, article_rows = _fetch_citation_aggregates(effective_window, industry)
    except Exception as exc:  # pragma: no cover - exercised via degraded test
        logger.warning("[citation-weights] aggregation unavailable: %s", exc)
        table.degraded_reason = f"citation_aggregation_unavailable:{type(exc).__name__}"
        return table

    grouped: dict[str, dict[str, Any]] = {}
    for row in platform_rows:
        domain = normalize_domain(row.get("domain"))
        if not domain:
            continue
        bucket = grouped.setdefault(
            domain,
            {"citation_count": 0, "cited_article_count": 0, "rank_sum": 0.0,
             "rank_n": 0, "engines": {}},
        )
        count = int(row.get("citation_count") or 0)
        bucket["citation_count"] += count
        avg_rank = row.get("avg_rank_in_response")
        if avg_rank is not None:
            try:
                bucket["rank_sum"] += float(avg_rank) * count
                bucket["rank_n"] += count
            except (TypeError, ValueError):
                pass
        platform = str(row.get("platform") or "").strip().lower()
        if platform:
            bucket["engines"][platform] = bucket["engines"].get(platform, 0) + count

    # ``normalize_domain`` folds sub-sites into one registrable domain, so the
    # distinct-article counts must be folded the same way before they are read.
    for row in article_rows:
        domain = normalize_domain(row.get("domain"))
        if not domain or domain not in grouped:
            continue
        grouped[domain]["cited_article_count"] += int(row.get("cited_article_count") or 0)

    if not grouped:
        table.degraded_reason = table.degraded_reason or "no_citation_rows_in_window"
        return table

    max_count = max(int(b["citation_count"]) for b in grouped.values())
    total = 0
    for domain, bucket in grouped.items():
        count = int(bucket["citation_count"])
        total += count
        avg_rank = (bucket["rank_sum"] / bucket["rank_n"]) if bucket["rank_n"] else None
        family = _match_seed_family(domain)
        table.domains[domain] = DomainCitationStat(
            domain=domain,
            citation_count=count,
            cited_article_count=int(bucket["cited_article_count"]),
            avg_rank_in_response=round(avg_rank, 3) if avg_rank is not None else None,
            engine_breakdown=dict(bucket["engines"]),
            strength=_strength_from_observation(count, max_count, avg_rank),
            source="observed",
            family_key=family.key if family else "",
            family_label=family.label if family else "",
        )
    table.total_citations = total
    table.observed_domain_count = len(table.domains)
    return table


# ---------------------------------------------------------------------------
# cached accessor
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_cache: dict[tuple[int, str], tuple[float, CitationDomainWeightTable]] = {}


def get_citation_domain_weights(
    *,
    window_days: int | None = None,
    industry: str = "",
    force_refresh: bool = False,
) -> CitationDomainWeightTable:
    key = (
        _clamp_window(window_days if window_days is not None else _configured_window_days()),
        str(industry or ""),
    )
    now = time.time()
    if not force_refresh:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < _CACHE_TTL_SECONDS:
                return hit[1]
    table = build_citation_domain_weights(window_days=key[0], industry=key[1])
    with _cache_lock:
        _cache[key] = (now, table)
    return table


def reset_citation_domain_weight_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# resolver: media row -> citation strength
# ---------------------------------------------------------------------------
def resolve_domain_strength(
    table: CitationDomainWeightTable,
    *,
    domain: str = "",
    media_name: str = "",
    platform_name: str = "",
) -> dict[str, Any]:
    """Resolve one media row to a citation strength descriptor.

    Resolution order (first hit wins):

    1. observed citations for the row's own domain;
    2. observed citations for a domain whose seed family the row's name matches
       (so "搜狐" in the inventory maps onto ``sohu.com`` observations);
    3. the seed family prior;
    4. the neutral prior.
    """
    normalized = normalize_domain(domain)
    if normalized:
        stat = table.domains.get(normalized)
        if stat is not None:
            return {**stat.as_dict(), "matched_by": "domain"}

    family = _match_seed_family(normalized, f"{media_name} {platform_name}")
    if family is not None:
        best: DomainCitationStat | None = None
        for stat in table.domains.values():
            if stat.family_key != family.key:
                continue
            if best is None or stat.citation_count > best.citation_count:
                best = stat
        if best is not None:
            payload = best.as_dict()
            payload.update({"matched_by": "family_observed", "domain": best.domain})
            return payload
        return {
            "domain": normalized,
            "citation_count": 0,
            "cited_article_count": 0,
            "avg_rank_in_response": None,
            "engine_breakdown": {},
            "strength": family.prior,
            "strength_label": strength_label(family.prior),
            "source": "seed_prior",
            "family_key": family.key,
            "family_label": family.label,
            "matched_by": "family_prior",
        }

    return {
        "domain": normalized,
        "citation_count": 0,
        "cited_article_count": 0,
        "avg_rank_in_response": None,
        "engine_breakdown": {},
        "strength": NEUTRAL_PRIOR,
        "strength_label": strength_label(NEUTRAL_PRIOR),
        "source": "neutral",
        "family_key": "",
        "family_label": "",
        "matched_by": "none",
    }


def engine_citation_breakdown(table: CitationDomainWeightTable) -> dict[str, int]:
    """Aggregate observed citations per engine (v2.4 ⑤ engine split)."""
    totals: dict[str, int] = {}
    for stat in table.domains.values():
        for engine, count in stat.engine_breakdown.items():
            totals[engine] = totals.get(engine, 0) + int(count or 0)
    return dict(sorted(totals.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------------------
# customer-facing advisory (v2.4 ⑤ · 建议客户自有官网发榜单类内容)
# ---------------------------------------------------------------------------
OWN_SITE_RANKING_ADVISORY: Final[dict[str, Any]] = {
    "code": "publish_on_own_site_ranking_content",
    "message": "建议同时在客户自有官网发布榜单/推荐类内容。",
    "reason": "飞轮观测显示品牌官网自发的榜单类内容会被 AI 当作权威来源引用（索菲亚案例被引 10 次以上）。",
    "impact": "只投第三方媒体会漏掉一条已被验证的高引用渠道。",
    "repair_hint": "把同一篇榜单/推荐稿同步发到客户官网的资讯/案例栏目，并保留可访问的固定链接。",
    "actions": [
        {"id": "view_citation_domains", "label": "查看真实被引域", "type": "nav"},
    ],
    "rule_version": CITATION_DOMAIN_WEIGHTS_VERSION,
}


def build_channel_advisories(
    table: CitationDomainWeightTable,
    *,
    recommended_domains: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Advisories only — never a block (SSOT §13: every alert needs an action)."""
    advisories: list[dict[str, Any]] = [dict(OWN_SITE_RANKING_ADVISORY)]
    normalized = {normalize_domain(d) for d in recommended_domains if d}
    low_overlap = normalized & KNOWN_LOW_CITATION_PUBLISH_DOMAINS
    if low_overlap:
        advisories.append({
            "code": "recommended_domains_low_observed_citation",
            "message": "本次推荐里有历史上几乎不被 AI 引用的发布域。",
            "reason": "这些域在近 %d 天的引用观测中没有出现，属于我方历史投放与真实被引域的错配区。" % table.window_days,
            "impact": "文章可能发出去但进不了 AI 答案。",
            "repair_hint": "优先选择「引用强/引用中」标记的媒体，或改投新闻门户、地方党媒、搜狐新浪系、163、界面、行业垂直站。",
            "actions": [{"id": "view_citation_domains", "label": "查看真实被引域", "type": "nav"}],
            "rule_version": CITATION_DOMAIN_WEIGHTS_VERSION,
            "details": sorted(low_overlap),
        })
    if not table.has_observed_data:
        advisories.append({
            "code": "citation_weights_unavailable",
            "message": "暂时读不到真实被引数据，本次排序回落到历史权重。",
            "reason": table.degraded_reason or "no_citation_rows_in_window",
            "impact": "推荐仍可用，但没有用上最新的被引观测。",
            "repair_hint": "确认 GEO 调研飞轮近期有完成的 round；也可以扩大观测窗口后重试。",
            "actions": [{"id": "open_research_monitor", "label": "打开调研监测", "type": "nav"}],
            "rule_version": CITATION_DOMAIN_WEIGHTS_VERSION,
        })
    return advisories


def iter_priority_domain_families() -> Iterable[dict[str, Any]]:
    for family in SEED_DOMAIN_FAMILIES:
        yield {
            "key": family.key,
            "label": family.label,
            "prior": family.prior,
            "example_domains": list(family.domain_fragments[:5]),
            "role": family.role,
            "role_label": family.role_label,
            "is_trunk": family.is_trunk,
            "self_serve": family.self_serve,
        }
