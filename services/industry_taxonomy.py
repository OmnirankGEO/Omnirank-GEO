# -*- coding: utf-8 -*-
"""行业大类字典 —— 全仓**唯一**的行业归类入口(WO_267 · 2026-09-23)。

🔴 起因(客户反馈):一家做**光伏**的客户,点亮调研被路由到**建筑**。
   读码发现行业名字**六处各一套**、互不引用,而且全是子串/双字 bigram 模糊匹配:
     ① `placement_service.INDUSTRY_KEYWORDS`       媒体匹配(11 类)
     ② `placement_service._match_research_industry` 调研行业(bigram 跨大类命中邻居)
     ③ `placement_service._resolve_industry_head_fallback.keyword_map` 头部白名单(14 类)
     ④ `brands.industry_category` 的存量 12 个值
     ⑤ `brand_field_suggester.INDUSTRY_PROMPT` 自由文本
     ⑥ `db/models.get_industry_category` —— **④ 的实际写入方**,子串匹配,
        还有 `"ai" in "maintenance"` 这种 ASCII 误中(工单漏数、C 补的第六处)
   更要紧的一层:那位客户 `brands.industry` 列写的就是「建筑装饰、装修和其他建筑业」,
   **光伏只出现在品牌名/备注/种子词里** ⇒ 只看 industry 列时,路到建筑是**正确地算错**。

所以这里做三件事:
  1. 一张字典(`config/industry_taxonomy.json`)定义全部大类、别名、旧名;
  2. **两步判定**:override → 整词别名(强别名可升主类,**吃品牌上下文**)→ LLM 闭集;
     **没有任何跨大类的 bigram/子串模糊回退**;判不出 ⇒ `other` + `needs_review`;
  3. 五六处旧实现全部改成从这里派生。

🔴 与 `geo_research_industries` 的连接键是 **slug 不是 name**:
   管理员 CRUD 的编辑接口 `PUT /industries/{id}` 允许改 name
   (`api/research_monitor_industry_api.py` `UpdateIndustryRequest.name`),不许改 slug ——
   用 name 连,改一次名就断。取引擎分时再用**该行当前的 name**
   (`geo_engine_stats.industry` 存的是中文名,改名接口不同步它)。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("GEO-IndustryTaxonomy")

TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "config" / "industry_taxonomy.json"

#: 判不出时落这里。它不是一个行业,是"没判出来" —— 一律带 needs_review。
OTHER_KEY = "other"

#: `resolve_industry` 的 source 取值(闭集)。
SOURCES = ("override", "alias", "llm", "none")

#: 切段用的分隔符:斜杠、顿号、中英文括号、分号。
#: 🔴 不按逗号切:中文行业串里「,」常在一段之内(「室内外装饰装修、安防工程…」)。
_SEGMENT_SPLIT_RE = re.compile(r"[/／、;；（）()]")

#: ASCII 别名只在**词边界**上算命中 —— 旧 `get_industry_category` 的
#: `"ai" in "maintenance"` 就是没有这一条。
_ASCII_RE = re.compile(r"^[A-Za-z0-9 ]+$")


# ══════════════════════════════════════════════════════════════════
# 字典加载与校验
# ══════════════════════════════════════════════════════════════════

class TaxonomyError(ValueError):
    """字典本身不自洽 —— 加载时就炸,别让一张坏字典安静地路由错。"""


@dataclass(frozen=True)
class Category:
    key: str
    name: str
    aliases: Tuple[str, ...]
    strong_aliases: Tuple[str, ...]
    subcategories: Tuple[Tuple[str, Tuple[str, ...]], ...]
    research_industry_name: str
    research_industry_slug: str
    media_keywords: Tuple[str, ...]
    head_fallback_key: Optional[str]
    legacy_names: Tuple[str, ...]


@dataclass(frozen=True)
class Taxonomy:
    version: str
    categories: Tuple[Category, ...]
    weak_segments: Tuple[str, ...]
    legacy_secondary: Dict[str, dict]
    pending_research_rows: Tuple[str, ...]

    def by_key(self) -> Dict[str, Category]:
        return {c.key: c for c in self.categories}


def _norm(s: str) -> str:
    return str(s or "").strip().lower()


def _load_raw(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_taxonomy(raw: dict) -> Taxonomy:
    """从 JSON 构造并**校验**字典。任何不自洽都抛 `TaxonomyError`。"""
    cats: List[Category] = []
    seen_keys: set = set()
    alias_owner: Dict[str, str] = {}
    legacy_owner: Dict[str, str] = {}
    slug_owner: Dict[str, str] = {}
    for c in raw.get("categories") or []:
        key = str(c["key"]).strip()
        if not key or key in seen_keys:
            raise TaxonomyError("大类 key 为空或重复:%r" % key)
        seen_keys.add(key)
        aliases = tuple(str(a).strip() for a in c.get("aliases") or [])
        strong = tuple(str(a).strip() for a in c.get("strong_aliases") or [])
        for a in aliases + strong:
            if len(a) < 2:
                # 🔴 单字别名全废(Review 09-23 裁定⑦):「药」会把「农药」拉进医疗
                raise TaxonomyError("别名至少 2 个字:%r(大类 %s)" % (a, key))
            owner = alias_owner.get(_norm(a))
            if owner and owner != key:
                raise TaxonomyError("别名 %r 同时属于 %s 与 %s —— 一个别名只许属于一个大类"
                                    % (a, owner, key))
            alias_owner[_norm(a)] = key
        for ln in c.get("legacy_names") or []:
            owner = legacy_owner.get(ln)
            if owner and owner != key:
                raise TaxonomyError("旧名 %r 同时映射到 %s 与 %s" % (ln, owner, key))
            legacy_owner[ln] = key
        slug = str(c.get("research_industry_slug") or "").strip()
        if slug:
            owner = slug_owner.get(slug)
            if owner and owner != key:
                # Review 裁定③:research_industry_name 保持单值,一个调研行只属于一个大类
                raise TaxonomyError("调研行 slug %r 被 %s 与 %s 同时认领" % (slug, owner, key))
            slug_owner[slug] = key
        cats.append(Category(
            key=key,
            name=str(c["name"]),
            aliases=aliases,
            strong_aliases=strong,
            subcategories=tuple(
                (str(s["name"]), tuple(str(x) for x in s.get("aliases") or []))
                for s in c.get("subcategories") or []),
            research_industry_name=str(c.get("research_industry_name") or ""),
            research_industry_slug=slug,
            media_keywords=tuple(c.get("media_keywords") or []),
            head_fallback_key=c.get("head_fallback_key"),
            legacy_names=tuple(c.get("legacy_names") or []),
        ))
    if OTHER_KEY not in seen_keys:
        raise TaxonomyError("字典里必须有兜底大类 %r" % OTHER_KEY)
    other = next(c for c in cats if c.key == OTHER_KEY)
    if other.aliases or other.strong_aliases:
        # 🔴 other 是"没判出来",不是一个行业。给它别名 ⇒「通用设备制造业」里的「通用」
        #    会把它判成一个**确定的**次类,「其他服务」会被判成 other 却不带 needs_review
        #    (285 条差分当场照出)。旧值「其他/通用」走 legacy_names 翻译,不走别名。
        raise TaxonomyError("兜底大类 %r 不许有别名:%r" % (
            OTHER_KEY, other.aliases + other.strong_aliases))
    for legacy, meta in (raw.get("legacy_secondary") or {}).items():
        if legacy not in legacy_owner:
            raise TaxonomyError("legacy_secondary 里的 %r 不是任何大类的旧名" % legacy)
        sec = (meta or {}).get("secondary")
        if sec and sec not in seen_keys:
            raise TaxonomyError("legacy_secondary %r 的 secondary %r 不存在" % (legacy, sec))
    return Taxonomy(
        version=str(raw.get("version") or ""),
        categories=tuple(cats),
        weak_segments=tuple(raw.get("weak_segments") or []),
        legacy_secondary=dict(raw.get("legacy_secondary") or {}),
        pending_research_rows=tuple(raw.get("pending_research_rows") or []),
    )


@lru_cache(maxsize=1)
def load_taxonomy() -> Taxonomy:
    """**单点加载**。全仓只从这里拿字典。"""
    return build_taxonomy(_load_raw(TAXONOMY_PATH))


def category_keys() -> Tuple[str, ...]:
    return tuple(c.key for c in load_taxonomy().categories)


def get_category(key: str) -> Optional[Category]:
    return load_taxonomy().by_key().get(str(key or "").strip())


# ══════════════════════════════════════════════════════════════════
# 两步判定
# ══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Resolution:
    category_key: str
    confidence: float
    source: str
    secondary: Optional[str] = None
    needs_review: bool = False
    #: 命中的那个别名(便于日志与交付单解释"为什么是它")
    matched_alias: str = ""


@dataclass(frozen=True)
class _Hit:
    category_key: str
    alias: str
    strong: bool


def _alias_table(tax: Taxonomy) -> List[Tuple[str, str, bool]]:
    """(别名, 大类 key, 是否强别名),按**别名长度降序** —— 最长整词优先(裁定②)。"""
    rows: List[Tuple[str, str, bool]] = []
    for c in tax.categories:
        for a in c.aliases:
            rows.append((a, c.key, False))
        for a in c.strong_aliases:
            rows.append((a, c.key, True))
    rows.sort(key=lambda r: (-len(r[0]), r[0]))
    return rows


def _alias_in(alias: str, text: str) -> bool:
    if _ASCII_RE.match(alias):
        return re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(alias),
                         text, flags=re.I) is not None
    return alias in text


def _alias_pos(alias: str, text: str) -> int:
    if _ASCII_RE.match(alias):
        m = re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(alias), text, flags=re.I)
        return m.start() if m else -1
    return text.find(alias)


def _best_hit(segment: str, table: List[Tuple[str, str, bool]],
              generic: frozenset = frozenset()) -> Optional[_Hit]:
    """一段文字里**最长**的那个整词别名;等长时取**最靠左**的。

    🔴 这是「整词别名匹配」,不是 bigram:别名本身是字典里写死的完整词,
       不会把大类名拆成双字去碰运气(旧 `_match_research_industry` 就是那样
       把「装修建材」拆出「建材」「修建」去撞任何长串)。
    🔴 等长取最靠左,而不是按字符编码排序:285 条差分里「住宿和餐饮业」这类
       两个等长别名并存的段,原来按 Unicode 顺序挑,等于随机 —— 靠左至少是
       "文字里先说的那个",说得出理由。
    🔴 门类词别名(`generic`,即字典的 weak_segments:制造业…)**输给同段任何具体别名**,
       不论长短:「食品制造业」里 制造业(3 字)按最长优先会压过 食品(2 字),
       而分类名的头部才是它说的那个行业(医药制造业/汽车制造业/家具制造业同形)。
    """
    best: Optional[Tuple[bool, int, int, _Hit]] = None   # (是门类词, -长度, 位置, 命中)
    for alias, key, strong in table:
        pos = _alias_pos(alias, segment)
        if pos < 0:
            continue
        cand = (alias in generic, -len(alias), pos, _Hit(key, alias, strong))
        if best is None or cand[:3] < best[:3]:
            best = cand
    return best[3] if best else None


def _legacy_exact(segment: str, tax: Taxonomy) -> Optional[Resolution]:
    """一段文字**整段**就是某个旧大类名(「文旅娱乐」「房产家居」…)⇒ 按旧名映射。

    否则「文旅娱乐」里的 文旅/娱乐 两个等长别名会各打一半 ——
    而 Review 09-23 裁定⑥已经说了它该落哪、次类是什么。
    """
    s = segment.strip()
    for c in tax.categories:
        if s in c.legacy_names:
            meta = tax.legacy_secondary.get(s) or {}
            return Resolution(c.key, 0.7, "alias", secondary=meta.get("secondary"),
                              needs_review=bool(meta.get("needs_review")) or c.key == OTHER_KEY,
                              matched_alias=s)
    return None


def _strong_hit(text: str, table: List[Tuple[str, str, bool]]) -> Optional[_Hit]:
    for alias, key, strong in table:
        if strong and _alias_in(alias, text):
            return _Hit(key, alias, True)
    return None


#: 国民经济行业分类名的尾巴。「软件和信息技术服务业」「互联网和相关服务」「商务服务业」
#: 「农副食品加工业」…… 生产 285 条里大量是「分类名 / 具体业务」形状。
_NBS_TAIL_RE = re.compile(r"(业|和相关服务)$")


def _is_weak(segment: str, tax: Taxonomy) -> bool:
    """**弱段**:门类词(批发业/零售业/制造业/服务业,Review 09-23 裁定),
    以及任何以「业」「和相关服务」结尾的国民经济行业分类名。

    🔴 后半条是 285 条全量差分**当场逼出来的**:只认那四个词时,
       「AI搜索优化/GEO服务」被 AI 带去科技、「批发业 / 西药批发…」被批发带去电商、
       「互联网和相关服务 / 高端商务出行」被互联网带去科技 —— 分类名排在前面,
       真实业务在后面,而"第一段为主类"让分类名赢了。
       弱段的命中仍然保留:别的段都判不出时,它就是主类(「餐饮业」单独一段 ⇒ 食品餐饮)。
    """
    s = segment.strip()
    return s in tax.weak_segments or bool(_NBS_TAIL_RE.search(s))


def _is_weak_hit(segment: str, hit: _Hit, tax: Taxonomy) -> bool:
    """弱命中 = 命中出在弱段里,**或命中的别名本身就是门类词**。

    后半条管「制造业-酒」这种:段尾不是「业」所以段不弱,但它唯一的命中是门类词
    制造业 —— 让它当主类,「酱香型白酒生产」就只能当次类(285 条里 745/769)。
    """
    return _is_weak(segment, tax) or hit.alias in tax.weak_segments


def _segments(text: str) -> List[str]:
    return [s.strip() for s in _SEGMENT_SPLIT_RE.split(str(text or "")) if s.strip()]


LlmChooser = Callable[[str, Sequence[Tuple[str, str]]], Optional[str]]

#: 品牌行里哪些列算"品牌上下文"(Review 09-23 导出夹具时用的是 名 / notes / 种子词;
#: 公司名、展示名同属品牌自己的名字)。`industry` 列不在这里 —— 它是判定的主输入。
BRAND_CONTEXT_COLUMNS = ("name", "company_name", "brand_display_names", "notes", "seed_keywords")


def brand_context(brand: Optional[dict]) -> Tuple[str, ...]:
    """品牌行 → 上下文串。`seed_keywords` 是 jsonb 列表(驱动解码成 list,也兼容字符串)。"""
    if not brand:
        return ()
    out: List[str] = []
    for col in BRAND_CONTEXT_COLUMNS:
        v = brand.get(col)
        if isinstance(v, (list, tuple)):
            out.extend(str(x) for x in v if str(x or "").strip())
        elif v is not None and str(v).strip():
            out.append(str(v))
    return tuple(out)


def brand_category_override(brand: Optional[dict]) -> Optional[str]:
    """品牌上用户选定的大类(`brands.industry_category`)—— **只认新 key**。
    存量旧值(房产家居/科技服务…)是旧子串匹配器写的,不是用户选的,不当 override。"""
    v = str((brand or {}).get("industry_category") or "").strip()
    return v if v in set(category_keys()) else None


def resolve_industry(
    text: str,
    *,
    brand: Optional[dict] = None,
    brand_override: Optional[str] = None,
    context: Iterable[str] = (),
    llm: Optional[LlmChooser] = None,
) -> Resolution:
    """行业 → 大类。

    `brand`(品牌行,可空):提供上下文(`BRAND_CONTEXT_COLUMNS`)与 override
    (`brands.industry_category` 为新 key 时);显式传入的 `brand_override` / `context` 优先 / 追加。

    第 0 步 · override:品牌上**用户选定**的大类 key 直接用。
        🔴 只认**新 key**。存量的 12 个旧值(房产家居/科技服务…)是旧子串匹配器
           算出来的,不是用户选的 —— 把它们当 override,光伏客户永远修不好。
    第 1 步 · 整词别名:
        · 强别名(光伏/储能/充电桩…)**在 industry 串或品牌上下文(名/备注/种子词)
          任何一处命中** ⇒ 升为主类;industry 串自己的主段降为 secondary;
        · 否则按切段,**第一个非弱段**的最长别名 = 主类,其余段 = secondary;
        · 弱段(批发业/零售业…)只在别的段都判不出时才当主类。
    第 2 步 · LLM **闭集**选择(可注入;只许回大类 key 或 `none`)。
    都不中 ⇒ `other` + `needs_review`。
    """
    tax = load_taxonomy()
    keys = set(category_keys())
    # 下面要遍历两次,生成器会被第一次耗尽 ⇒ 先落成 tuple
    context = tuple(context or ()) + brand_context(brand)

    ov = str(brand_override or "").strip() or (brand_category_override(brand) or "")
    if ov and ov in keys:
        # 用户明确选了「其他」也是「没有一个大类装得下」—— 照样打 needs_review
        return Resolution(ov, 1.0, "override", needs_review=(ov == OTHER_KEY))

    table = _alias_table(tax)
    generic = frozenset(tax.weak_segments)
    segs = _segments(text)
    # 整串就是一个旧大类名(最常见:把 industry_category 的旧值抄进了 industry 列)
    if len(segs) == 1:
        legacy = _legacy_exact(segs[0], tax)
        if legacy is not None and not any(
                _strong_hit(str(c or ""), table) for c in (context or ())):
            return legacy
    seg_hits: List[Tuple[str, Optional[_Hit]]] = [
        (s, _best_hit(s, table, generic)) for s in segs]

    # —— 强别名:industry 串内任一处,或品牌上下文 ——
    strong = None
    for s in segs:
        strong = _strong_hit(s, table)
        if strong:
            break
    if strong is None:
        for ctx in context or ():
            strong = _strong_hit(str(ctx or ""), table)
            if strong:
                break

    ordered: List[_Hit] = []
    weak_hits: List[_Hit] = []
    for s, h in seg_hits:
        if h is None:
            continue
        (weak_hits if _is_weak_hit(s, h, tax) else ordered).append(h)
    if not ordered:
        ordered = weak_hits
    else:
        ordered = ordered + weak_hits

    if strong is not None:
        # 次类:按段序(非弱段在前),每段在**排除强类之后**取最佳命中。
        # 只看每段的「最佳命中」会丢东西:「光伏建筑一体化」里 光伏 / 建筑 等长,
        # 靠左的「光伏」胜出后这一段就只剩它,建筑永远当不上次类(工单明确要求它是次类)。
        excl = [e for e in table if e[1] != strong.category_key]
        sec_hits = [(s, _best_hit(s, excl, generic)) for s in segs]
        sec_order = ([h for s, h in sec_hits if h and not _is_weak_hit(s, h, tax)]
                     + [h for s, h in sec_hits if h and _is_weak_hit(s, h, tax)])
        secondary = sec_order[0].category_key if sec_order else None
        return Resolution(strong.category_key, 0.9, "alias",
                          secondary=secondary, matched_alias=strong.alias)

    if ordered:
        primary = ordered[0]
        secondary = next((h.category_key for h in ordered[1:]
                          if h.category_key != primary.category_key), None)
        only_weak = not any(h and not _is_weak_hit(s, h, tax) for s, h in seg_hits)
        return Resolution(primary.category_key, 0.5 if only_weak else 0.8, "alias",
                          secondary=secondary, needs_review=only_weak,
                          matched_alias=primary.alias)

    if llm is not None and str(text or "").strip():
        choices = [(c.key, c.name) for c in tax.categories]
        try:
            picked = llm(str(text), choices)
        except Exception as exc:          # LLM 失败不许让整条链崩,但要出声
            logger.warning("[industry] LLM 闭集选择失败 text=%r: %s", str(text)[:40], exc)
            picked = None
        picked = str(picked or "").strip()
        if picked in keys and picked != OTHER_KEY:
            return Resolution(picked, 0.6, "llm")

    return Resolution(OTHER_KEY, 0.0, "none", needs_review=True)


# ══════════════════════════════════════════════════════════════════
# 旧值翻译(读侧)· 派生表 · 调研行业
# ══════════════════════════════════════════════════════════════════

def translate_legacy(value: Optional[str]) -> Optional[Resolution]:
    """`brands.industry_category` 的**存量旧值**(或新 key)→ 大类。

    存量不回填(Owner 09-19 口径),读侧按这张映射表翻译。
    认不出 ⇒ None,调用方自己决定怎么显示(不替它编一个)。
    """
    v = str(value or "").strip()
    if not v:
        return None
    tax = load_taxonomy()
    if v in set(category_keys()):
        return Resolution(v, 1.0, "override")
    for c in tax.categories:
        if v in c.legacy_names:
            meta = tax.legacy_secondary.get(v) or {}
            return Resolution(c.key, 0.7, "alias",
                              secondary=meta.get("secondary"),
                              needs_review=bool(meta.get("needs_review")) or c.key == OTHER_KEY,
                              matched_alias=v)
    return None


def display_name(value: Optional[str]) -> str:
    """给页面显示用:新 key 或旧值都翻成大类中文名;认不出就原样返回。"""
    r = translate_legacy(value)
    if r is None:
        return str(value or "")
    return get_category(r.category_key).name


def category_fields(value: Optional[str]) -> Dict[str, Optional[str]]:
    """API 响应里给 `industry_category` **附加**的两个字段(只增不改,原字段原样保留):

    `industry_category_key`  新大类 key(存量旧值按映射翻译;认不出 ⇒ None)
    `industry_category_name` 大类中文名(认不出 ⇒ 原值;空 ⇒ None)
    前端下拉(A)按 key 选中、按 name 显示;存量不回填也能对上。
    """
    r = translate_legacy(value)
    if r is None:
        return {"industry_category_key": None, "industry_category_name": (str(value).strip() or None) if value else None}
    c = get_category(r.category_key)
    return {"industry_category_key": r.category_key, "industry_category_name": c.name if c else None}


def filter_values(value: Optional[str]) -> List[str]:
    """按大类筛选时,SQL 要同时匹配**新 key 与映射到它的全部旧值** ——
    否则存量(旧值)与新写入(新 key)在同一个筛选里互相看不见。"""
    r = translate_legacy(value)
    if r is None:
        return [str(value or "")] if value else []
    cat = get_category(r.category_key)
    return [cat.key] + list(cat.legacy_names)


def media_keywords_table() -> Dict[str, List[str]]:
    """① `INDUSTRY_KEYWORDS` 的派生版:**大类 key** → 媒体关键词(五处同键)。"""
    return {c.key: list(c.media_keywords) for c in load_taxonomy().categories
            if c.media_keywords}


def media_keywords_for(res: Resolution) -> List[str]:
    """一次判定 → 该用的媒体关键词:主类 + 次类(旧实现对"汽车/旅游"这种串两类的词都取)。"""
    table = media_keywords_table()
    out: List[str] = []
    for key in (res.category_key, res.secondary):
        for kw in table.get(key or "", []):
            if kw not in out:
                out.append(kw)
    return out


def head_fallback_key_for(res: Resolution) -> Optional[str]:
    """③ 一次判定 → `INDUSTRY_HEAD_FALLBACK` 的键(没挂白名单的大类 ⇒ None)。
    只看主类:白名单是"这个行业的头部媒体",给次类的头部会把光伏客户带回建筑。"""
    c = get_category(res.category_key)
    return c.head_fallback_key if c else None


def head_fallback_keys() -> List[str]:
    """字典里挂了头部白名单的全部键 —— 与 `INDUSTRY_HEAD_FALLBACK` 的键集合必须相等(锁)。"""
    return [c.head_fallback_key for c in load_taxonomy().categories if c.head_fallback_key]


def research_target(category_key: str) -> Tuple[str, str]:
    """大类 → (调研行 slug, 调研行说明名)。连接键是 slug。"""
    c = get_category(category_key)
    if c is None:
        return "", ""
    return c.research_industry_slug, c.research_industry_name


def taxonomy_public_payload() -> dict:
    """`GET /api/industry-taxonomy` 的返回体:只给前端要用的(key / 名 / 细分)。
    别名、强别名、调研 slug 这些路由内部细节不出后端。"""
    tax = load_taxonomy()
    return {
        "version": tax.version,
        "categories": [
            {"key": c.key, "name": c.name,
             "subcategories": [name for name, _ in c.subcategories]}
            for c in tax.categories
        ],
    }
