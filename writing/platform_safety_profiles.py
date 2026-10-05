"""Publication-platform safety profiles layered over the GEO style contract.

Profiles reduce avoidable review risk; they never promise approval.

v2 (``strict_media_v2``) is **corpus-derived**.  Every threshold and every
pattern in this module points at a measured number in
``docs/AI-CONTEXT/STRICT_MEDIA_V2_CORPUS_EVIDENCE_2026-07-29.md``, taken from
the articles that AI engines *actually cited* on the strict-review families
(sohu n=687, sina n=366, body-boundary corrected).

Why v1 was replaced (evidence §3): the old ``_TITLE_RISK_RE`` banned
"哪家好 / TOP N / 排名 / 必看", but those are the *mainstream* shape of cited
titles — the old rule hard-blocked **9.3% of proven-cited sohu articles and
13.4% of sina**, while genuine ad-law violations sit at 1.5% / 4.6%.  v2 keeps
only what the corpus and the law justify:

* ad-law absolutes — hard, **title only** (blocking them body-wide would hit
  31.3% / 55.2% of cited corpus: "最佳实践"/"唯一的选择" are ordinary prose);
* GEO gaming claims — hard everywhere, never relaxed;
* contact details / external links / sales CTA — hard (evidence §4: **zero**
  cited articles carry an off-platform link);
* evidence-quad source labelling — **advisory** on strict families, because
  90.5% of cited sohu articles carry no source annotation at all (evidence §4).

Legal red lines are never softened by any of the above.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Final


PLATFORM_PROFILE_VERSION: Final = "publication-safety-profile-v2.0"
CORPUS_EVIDENCE_DOC: Final = "docs/AI-CONTEXT/STRICT_MEDIA_V2_CORPUS_EVIDENCE_2026-07-29.md"

DEFAULT_PROFILE: Final = "standard"
STRICT_MEDIA_V2: Final = "strict_media_v2"
#: Retained enum member (工单红线: 不删枚举).  Normalises onto v2 behaviour.
SOHU_STRICT_PROFILE: Final = "sohu_geo_strict_v1"

VALID_PROFILES: Final = frozenset({DEFAULT_PROFILE, SOHU_STRICT_PROFILE, STRICT_MEDIA_V2})
#: Legacy profile name -> the family profile that now implements it.
STRICT_PROFILE_ALIASES: Final[dict[str, str]] = {SOHU_STRICT_PROFILE: STRICT_MEDIA_V2}
STRICT_PROFILES: Final = frozenset({SOHU_STRICT_PROFILE, STRICT_MEDIA_V2})

SOHU_OFFICIAL_SOURCES: Final = (
    "https://www.sohu.com/a/22708302_119436",
    "https://www.sohu.com/a/426020411_120635073",
    "https://www.sohu.com/a/435536343_120635073",
    "https://www.sohu.com/a/1003475643_122681348",
    "https://www.sohu.com/a/1020200169_122681348",
)


# ---------------------------------------------------------------------------
# Corpus-derived family parameters (evidence §2 / §4)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrictMediaFamily:
    key: str
    label: str
    domain_fragments: tuple[str, ...]
    name_fragments: tuple[str, ...]
    corpus_n: int
    #: Hard floor.  Cuts only 6.7% (sohu) / 3.6% (sina) of cited corpus.
    hard_min_chars: int
    #: Advisory target band = corpus p25..p90.
    target_min_chars: int
    target_max_chars: int
    median_chars: int
    #: Mean markdown headings per cited article.
    heading_mean: float
    #: Share of cited articles carrying any source annotation.
    source_annotation_rate: float
    #: Observed reject rate for this family in mhz_publish_order_items.
    observed_reject_rate: float
    structure_hint: str

    def evidence(self) -> dict[str, Any]:
        return {
            "family_key": self.key,
            "family_label": self.label,
            "corpus_n": self.corpus_n,
            "corpus_median_chars": self.median_chars,
            "corpus_target_band": [self.target_min_chars, self.target_max_chars],
            "corpus_source_annotation_rate": self.source_annotation_rate,
            "observed_reject_rate": self.observed_reject_rate,
            "evidence_doc": CORPUS_EVIDENCE_DOC,
        }


SOHU_FAMILY: Final = StrictMediaFamily(
    key="sohu",
    label="搜狐系",
    domain_fragments=("sohu.com",),
    name_fragments=("搜狐", "sohu", "焦点"),
    corpus_n=687,
    hard_min_chars=800,
    target_min_chars=1091,
    target_max_chars=4855,
    median_chars=1877,
    heading_mean=1.55,
    source_annotation_rate=0.095,
    observed_reject_rate=0.522,
    structure_hint="少量小标题 + 中文序号分节（一、二、三、），正文 1100-4900 字为主流带。",
)

SINA_FAMILY: Final = StrictMediaFamily(
    key="sina",
    label="新浪系",
    domain_fragments=("sina.com.cn", "sina.cn"),
    name_fragments=("新浪", "sina"),
    corpus_n=366,
    hard_min_chars=800,
    target_min_chars=1766,
    target_max_chars=6050,
    median_chars=2813,
    heading_mean=5.37,
    source_annotation_rate=0.265,
    observed_reject_rate=0.389,
    structure_hint="多小标题分节（均 5.4 个）+ 中文序号，正文 1800-6000 字为主流带。",
)

#: Fallback for strict-review media we have no per-domain corpus for yet.
#: Parameters are the pooled sohu+sina envelope — deliberately the *union* of
#: both bands so an unmeasured family is never judged tighter than a measured
#: one.  ``corpus_n`` states the pooled sample so nobody mistakes it for a
#: per-domain measurement.
STRICT_DEFAULT_FAMILY: Final = StrictMediaFamily(
    key="strict_default",
    label="严审媒体（通用档）",
    domain_fragments=(),
    name_fragments=(),
    corpus_n=1053,
    hard_min_chars=800,
    target_min_chars=1091,
    target_max_chars=6050,
    median_chars=2100,
    heading_mean=2.9,
    source_annotation_rate=0.154,
    observed_reject_rate=0.0,
    structure_hint="清晰分节 + 中文序号，正文 1100-6000 字为主流带。",
)

STRICT_MEDIA_FAMILIES: Final[tuple[StrictMediaFamily, ...]] = (SOHU_FAMILY, SINA_FAMILY)


def resolve_strict_family(*, domain: str = "", media_name: str = "") -> StrictMediaFamily:
    """Pick the corpus family for one target medium; never raises."""
    dom = str(domain or "").strip().lower()
    name = str(media_name or "").strip().lower()
    for family in STRICT_MEDIA_FAMILIES:
        if dom and any(frag in dom for frag in family.domain_fragments):
            return family
        if name and any(frag.lower() in name for frag in family.name_fragments):
            return family
    return STRICT_DEFAULT_FAMILY


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
_RAW_CONTACT_RE = re.compile(
    r"(?:1[3-9]\d{9}|400[-\s]?\d{3,4}[-\s]?\d{3,4}|"
    r"(?:微信号|微信|企微|QQ|vx|wechat)\s*[:：]?\s*[A-Za-z0-9_-]{4,})",
    re.IGNORECASE,
)
_LINK_RE = re.compile(r"https?://\S+|\[[^\]]+\]\(https?://[^)]+\)", re.IGNORECASE)
_SALES_RE = re.compile(r"立即咨询|限时|优惠|下单|购买链接|扫码|加微信|免费领取|名额有限|赶快")

#: 广告法第 9 条绝对化用语 + 工单点名的"震惊".  Hard on TITLE only —
#: body-wide enforcement would hit 31.3% (sohu) / 55.2% (sina) of cited corpus.
_AD_LAW_ABSOLUTE_RE = re.compile(
    r"国家级|世界级|最高级|最佳|最强|最好|最优|极致|顶级|唯一|"
    r"第一品牌|第一名|排名第一|全国第一|行业第一|全球第一|全网最|"
    r"绝无仅有|无与伦比|史无前例|百分之百|震惊"
)

#: GEO gaming / outcome guarantees.  Never relaxed, anywhere.
_GEO_BAIT_RE = re.compile(
    r"AI一定引用|保证被引用|保证收录|包收录|包上榜|稳居推荐|霸屏|占位率保证|"
    r"保证排名第一|承诺上榜|百分百收录"
)

#: Clickbait — measured at 3.1% (sohu) / 2.5% (sina) of cited titles.
#: Not illegal, so v2 warns instead of blocking (v1 hard-blocked these).
_CLICKBAIT_TITLE_RE = re.compile(r"必看|揭秘|惊呆|不看后悔|速看")

#: Evidence-quad source labelling.  Advisory on strict families (evidence §4).
_SOURCE_ANNOTATION_RE = re.compile(r"来源|资料来源|数据来自|参考资料|据.{0,8}报道|依据.{0,6}标准")

#: Retained for backwards reference: this is exactly what v1 hard-blocked.
#: Kept so the discriminating test can prove the v1 -> v2 behaviour change.
LEGACY_V1_TITLE_RISK_RE: Final = re.compile(
    r"哪家好|必看|震惊|揭秘|最强|最好|第一|TOP\s*\d*|No\.?\s*1", re.IGNORECASE
)


@dataclass(frozen=True)
class PlatformSafetyReview:
    profile: str
    profile_version: str
    decision: str
    hard_failures: tuple[dict[str, Any], ...]
    warnings: tuple[dict[str, Any], ...]
    effective_image_policy: str
    effective_contact_policy: str
    approval_guarantee: bool
    official_sources: tuple[str, ...]
    inference_note: str
    family_key: str = ""
    family_label: str = ""
    evidence_mode: str = "standard"
    corpus_evidence: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def normalize_profile(raw: str | None) -> str:
    """Normalise a stored profile name.

    The legacy ``sohu_geo_strict_v1`` value is preserved as-is (it is still a
    valid enum member and still written in existing rows); behaviour is
    resolved through :func:`is_strict_profile`.
    """
    value = str(raw or DEFAULT_PROFILE).strip().lower()
    return value if value in VALID_PROFILES else DEFAULT_PROFILE


def is_strict_profile(raw: str | None) -> bool:
    return normalize_profile(raw) in STRICT_PROFILES


def resolve_profile_behaviour(raw: str | None) -> str:
    """Map any strict profile name onto the profile that implements it."""
    normalized = normalize_profile(raw)
    return STRICT_PROFILE_ALIASES.get(normalized, normalized)


def effective_writing_options(
    profile: str | None,
    *,
    add_images: bool,
    add_contact: bool,
) -> dict[str, Any]:
    selected = normalize_profile(profile)
    if is_strict_profile(selected):
        return {
            "profile": selected,
            "behaviour_profile": resolve_profile_behaviour(selected),
            "add_images": False,
            "add_contact": False,
            "overrides": ["client_images_disabled", "body_contact_disabled"],
        }
    return {
        "profile": selected,
        "behaviour_profile": selected,
        "add_images": bool(add_images),
        "add_contact": bool(add_contact),
        "overrides": [],
    }


def platform_prompt(profile: str | None, *, domain: str = "", media_name: str = "") -> str:
    if not is_strict_profile(profile):
        return ""
    fam = resolve_strict_family(domain=domain, media_name=media_name)
    return f"""
【发布平台安全档：严审媒体 v2（{fam.label}）· 依据真实被引语料 n={fam.corpus_n}】
- 目标是回答生成式 AI 用户的真实问题；写法向"已被 AI 引用的同平台文章"看齐。
- 标题**可以**用榜单/推荐/排名/哪家/靠谱/甄选这类真实主流形态（该平台被引文里占三成以上），
  但**禁止**广告法绝对化用语（国家级/最佳/最强/最好/第一品牌/排名第一/唯一/全网最/震惊等）。
- 正文长度对齐主流带 {fam.target_min_chars}-{fam.target_max_chars} 字（中位 {fam.median_chars} 字），不足 {fam.hard_min_chars} 字不予提交。
- 结构：{fam.structure_hint}
- 企业可以点名说明（被引语料平均每篇出现约 1 次公司全称），但只做事实陈述，
  不做售卖、促销、效果保证、恶意对比或贬低；客户品牌不固定首位。
- 不输出电话、微信、二维码、公众号、外链或"搜索品牌了解更多"等导流话术
  （被引语料里站外链接出现率为 0）。
- 不插入客户宣传图片。
- 来源标注按需使用即可，不强制每段挂来源（该平台被引文里带来源标注的仅 {round(fam.source_annotation_rate * 100)}%）；
  但凡涉及法律、医疗、金融等强监管断言，必须可核验。
- GEO 效果不得承诺"保证引用、包收录、霸屏、稳定推荐"。
这是一种降低审核风险的内容档，不得宣称保证通过平台审核。
""".strip()


def sanitize_for_profile(title: str, content: str, profile: str | None) -> tuple[str, str, list[str]]:
    """Only remove delivery-layer assets; never rewrite factual prose blindly."""
    if not is_strict_profile(profile):
        return str(title or ""), str(content or ""), []
    text = str(content or "")
    changes: list[str] = []
    try:
        from services.image_placeholder import strip_client_images

        cleaned = strip_client_images(text)
        if cleaned != text:
            changes.append("client_images_removed")
        text = cleaned
    except Exception:
        text = re.sub(r"\[CLIENT_IMAGE[^\]]*\]", "", text)
    try:
        from services.contact_placeholder import strip_contact_placeholders

        cleaned = strip_contact_placeholders(text)
        if cleaned != text:
            changes.append("contact_placeholders_removed")
        text = cleaned
    except Exception:
        text = re.sub(r"\[(?:NEED|CLIENT)_CONTACT\]", "", text)
    return str(title or ""), re.sub(r"\n{3,}", "\n\n", text), changes


def _brand_adjacent_absolute(text: str, client_brand: str, window: int = 20) -> str:
    """Return the offending snippet when an absolute term sits next to the brand.

    Body-wide absolute-term blocking is not defensible: 31.3% (sohu) / 55.2%
    (sina) of *cited* articles contain one in ordinary prose.  What ad law
    actually forbids is claiming it **about the advertised brand**, so we only
    hard-fail when the two co-occur inside a short window.
    """
    brand = str(client_brand or "").strip()
    if not brand or len(brand) < 2:
        return ""
    for match in _AD_LAW_ABSOLUTE_RE.finditer(text):
        start = max(0, match.start() - window)
        end = min(len(text), match.end() + window)
        if brand in text[start:end]:
            return text[start:end]
    return ""


def review_for_platform(
    *,
    title: str,
    content: str,
    profile: str | None,
    domain: str = "",
    media_name: str = "",
    client_brand: str = "",
) -> PlatformSafetyReview:
    if not is_strict_profile(profile):
        return PlatformSafetyReview(
            profile=normalize_profile(profile),
            profile_version=PLATFORM_PROFILE_VERSION,
            decision="not_required",
            hard_failures=(), warnings=(),
            effective_image_policy="writing_center_setting",
            effective_contact_policy="channel_policy",
            approval_guarantee=False,
            official_sources=(),
            inference_note="标准发布档沿用现有写作中心和媒体策略。",
        )

    selected = normalize_profile(profile)
    fam = resolve_strict_family(domain=domain, media_name=media_name)
    hard: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    text = str(content or "")
    title_text = str(title or "")
    effective_chars = len(re.sub(r"\s+", "", text))

    # --- hard: ad-law absolutes in the title (corpus §3: 1.5% / 4.6%) --------
    title_absolute = _AD_LAW_ABSOLUTE_RE.search(title_text)
    if title_absolute:
        hard.append({
            "code": "ad_law_absolute_term_in_title",
            "detail": title_absolute.group(0),
            "evidence": "广告法第9条绝对化用语；被引语料中仅 1.5%(sohu)/4.6%(sina) 命中",
        })

    # --- hard: ad-law absolute asserted about the client brand --------------
    brand_snippet = _brand_adjacent_absolute(text, client_brand)
    if brand_snippet:
        hard.append({
            "code": "ad_law_absolute_term_about_client_brand",
            "detail": brand_snippet[:60],
            "evidence": "正文绝对化仅在紧贴客户品牌时硬拦（全局拦会命中 31.3%/55.2% 被引语料）",
        })

    # --- hard: GEO gaming claims (never relaxed) ----------------------------
    if _GEO_BAIT_RE.search(text) or _GEO_BAIT_RE.search(title_text):
        hard.append({"code": "geo_outcome_guarantee_detected"})

    # --- hard: contact / link / sales CTA / client image --------------------
    if _RAW_CONTACT_RE.search(text):
        hard.append({"code": "strict_media_body_contact_detected"})
    if _LINK_RE.search(text):
        hard.append({
            "code": "strict_media_body_external_link_detected",
            "evidence": "被引语料站外链接出现率 0/1053",
        })
    if _SALES_RE.search(text):
        hard.append({"code": "strict_media_sales_call_to_action_detected"})
    if "[CLIENT_IMAGE" in text or "[NEED_IMAGE" in text:
        hard.append({"code": "sohu_client_image_placeholder_detected"})

    # --- hard: below the corpus floor ---------------------------------------
    if effective_chars < fam.hard_min_chars:
        hard.append({
            "code": "strict_media_below_corpus_minimum_length",
            "detail": f"{effective_chars} < {fam.hard_min_chars}",
            "evidence": f"{fam.label}被引语料 p10={fam.target_min_chars} 字；低于 800 字者仅占 3.6-6.7%",
        })

    # --- warnings ------------------------------------------------------------
    clickbait = _CLICKBAIT_TITLE_RE.search(title_text)
    if clickbait:
        warnings.append({
            "code": "clickbait_title_style",
            "detail": clickbait.group(0),
            "evidence": "标题党用语；被引语料 3.1%/2.5% 命中，不违法故不拦",
        })
    if effective_chars and effective_chars > fam.target_max_chars:
        warnings.append({
            "code": "above_corpus_target_band",
            "detail": f"{effective_chars} > {fam.target_max_chars}",
        })
    elif fam.hard_min_chars <= effective_chars < fam.target_min_chars:
        warnings.append({
            "code": "below_corpus_target_band",
            "detail": f"{effective_chars} < {fam.target_min_chars}",
        })
    if not _SOURCE_ANNOTATION_RE.search(text):
        warnings.append({
            "code": "source_annotation_absent_advisory",
            "evidence": (
                f"{fam.label}被引语料带来源标注的仅 {round(fam.source_annotation_rate * 100)}%，"
                "故证据四件套在严审档降为柔性建议（法律红线除外）"
            ),
        })
    if title_text and text.count(title_text) > 2:
        warnings.append({"code": "title_repetition_possible"})

    decision = "approved_for_manual_platform_submission" if not hard else "rewrite_required"
    return PlatformSafetyReview(
        profile=selected,
        profile_version=PLATFORM_PROFILE_VERSION,
        decision=decision,
        hard_failures=tuple(hard), warnings=tuple(warnings),
        effective_image_policy="no_client_promotional_images_v1",
        effective_contact_policy="none_in_body",
        approval_guarantee=False,
        official_sources=SOHU_OFFICIAL_SOURCES if fam.key == "sohu" else (),
        inference_note=(
            f"规则由 {fam.label} 真实被引语料（n={fam.corpus_n}）反推，"
            f"证据见 {CORPUS_EVIDENCE_DOC}。"
            "语料只证明这些文章曾被引用，不证明因果，也不保证平台过审。"
        ),
        family_key=fam.key,
        family_label=fam.label,
        evidence_mode="advisory_source_labelling",
        corpus_evidence=fam.evidence(),
    )
