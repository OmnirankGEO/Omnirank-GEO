"""Versioned Evidence Pack contract shared by research, writing, and review."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse


EVIDENCE_PACK_VERSION: Final = "evidence-pack-v1.0"
VALID_RELATIONSHIPS: Final = frozenset({"support", "refute", "background"})
# Document retrieval is not claim verification.  A fetched canonical/full body
# remains a candidate until an exact claim/span is verified or a human signs it.
VERIFIED_STATES: Final = frozenset({"claim_span_verified", "official_record", "human_verified"})
_UNVERIFIED_STATES: Final = frozenset({"search_result_only", "body_retrieved_claim_unverified"})
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")

# ---------------------------------------------------------------------------
# 标准类证据(标题级)· 工单 WORKORDER_STANDARD_EVIDENCE_LANE_2026-07-29 §4
# ---------------------------------------------------------------------------
#: 标题级标准/规范条目的来源层级(由 writing.evidence_research 的 document lane 写入,
#: 由 services.standard_citation_guard 在发布前读回 —— 标记必须两头都认)。
STANDARD_CITATION_TIER: Final = "standard_citation"
#: §4.1 写作铁律正文(同时喂给写作模型、也是被 guard 拦下时给用户看的口径)。
STANDARD_CITATION_WRITING_RULE: Final = (
    "🔴【标准类证据 · 标题级】本次证据里有只拿到**标题**、没有正文的标准/规范文件"
    "（标记 standard_citation）。对这类来源：\n"
    "  ✅ 只能做**存在性引用** —— 例:「依据 GB 50210-2018《建筑装饰装修工程质量验收标准》…」\n"
    "  ❌ 禁止做**内容性引用** —— 例:「GB 50210-2018 规定甲醛释放量≤0.03mg/m³」"
    "「该标准要求验收分为 N 个分项」。\n"
    "  凡标准的**具体条款、数值、比例**,必须另有正文来源才能写;"
    "写不了就把那句删掉,不要用「据了解」「一般来说」糊过去。\n"
    "  ⚠️ 地方性具体行政文件(区级/市级通知、`〔年份〕文号` 那类)连存在性引用也要克制:"
    "只在确实与主题强相关时引,不得为了「显得有出处」而堆砌 —— "
    "国标有大量二手解读可交叉验证,一份区级通知的具体条款几乎无人会去核。"
)


def _is_public_http_url(value: Any) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def _verified_provenance_complete(item: dict[str, Any]) -> bool:
    status = str(item.get("verification_status") or "")
    if status == "claim_span_verified":
        span = item.get("span")
        return bool(
            _SHA256_RE.fullmatch(str(item.get("canonical_body_hash") or "").lower())
            and item.get("body_hash_algorithm")
            and isinstance(span, dict)
            and span.get("type") == "canonical_body_exact_quote"
            and isinstance(span.get("start"), int)
            and isinstance(span.get("end"), int)
            and span["end"] > span["start"]
            and item.get("verification_version")
            and item.get("verification_provider")
            and item.get("verification_model")
        )
    if status == "official_record":
        return bool(item.get("official_record_id") and _is_public_http_url(item.get("url")))
    if status == "human_verified":
        return bool(
            item.get("human_reviewed_by")
            and item.get("human_reviewed_at")
            and len(str(item.get("human_review_reason") or "").strip()) >= 3
        )
    return status in _UNVERIFIED_STATES


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def manifest_hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def normalize_evidence_pack(raw: Any, *, request_id: str = "") -> dict[str, Any]:
    data = dict(raw) if isinstance(raw, dict) else {}
    items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(data.get("items") or []):
        if not isinstance(raw_item, dict):
            continue
        relationship = str(raw_item.get("relationship") or "background")
        if relationship not in VALID_RELATIONSHIPS:
            relationship = "background"
        verification = str(raw_item.get("verification_status") or "search_result_only")
        if verification not in VERIFIED_STATES | _UNVERIFIED_STATES:
            verification = "search_result_only"
        item = {
            "evidence_id": str(raw_item.get("evidence_id") or f"EV-{index + 1:03d}"),
            "claim": str(raw_item.get("claim") or "").strip(),
            "url": str(raw_item.get("url") or "").strip(),
            "title": str(raw_item.get("title") or "").strip(),
            "publisher": str(raw_item.get("publisher") or raw_item.get("source") or "").strip(),
            "published_at": raw_item.get("published_at") or raw_item.get("date"),
            "fetched_at": raw_item.get("fetched_at"),
            "valid_until": raw_item.get("valid_until"),
            "source_tier": str(raw_item.get("source_tier") or "unknown"),
            "relationship": relationship,
            "excerpt": str(raw_item.get("excerpt") or raw_item.get("snippet") or "")[:800],
            # [工单 C-2 T2] 逐家检索的实体归属必须活过 normalize(落库/分组喂料都靠它)。
            "entity": (str(raw_item.get("entity") or "").strip() or None),
            "scope": str(raw_item.get("scope") or "").strip(),
            "conflict_status": str(raw_item.get("conflict_status") or "none"),
            "verification_status": verification,
            "fetch_event_id": raw_item.get("fetch_event_id"),
            "canonical_body_hash": raw_item.get("canonical_body_hash"),
            "body_hash_algorithm": raw_item.get("body_hash_algorithm"),
            "body_boundary_version": raw_item.get("body_boundary_version"),
            "span": raw_item.get("span"),
            "verification_version": raw_item.get("verification_version"),
            "verification_provider": raw_item.get("verification_provider"),
            "verification_model": raw_item.get("verification_model"),
            "official_record_id": raw_item.get("official_record_id"),
            "human_reviewed_by": raw_item.get("human_reviewed_by"),
            "human_reviewed_at": raw_item.get("human_reviewed_at"),
            "human_review_reason": raw_item.get("human_review_reason"),
        }
        # [标准类证据 lane 2026-07-29 · §4.2] 标准 vs 地方性行政文件的分类随条目活过
        # normalize —— 写作渲染与发布门(services/standard_citation_guard)都要读它。
        # 🔴 **只在真有值时才带这个键**:无条件加键会让每个历史 pack 的 manifest_hash
        #    重算后漂移,而 evidence_manifest_hash 是发布门的比对基准(gate 里
        #    `evidence_changed_after_review` 那条)。空 pack 不凭空造字段,同 evidence_supply。
        doc_class = str(raw_item.get("standard_document_class") or "").strip()
        if doc_class:
            item["standard_document_class"] = doc_class
        # [P0-2 2026-08-14] 实体绑定三态随条目活过 normalize(H0-ENTITY-BINDING 的
        # 隔离判据要在写作渲染/主优势计分两处读它)。同 standard_document_class:
        # **只在真有值时才带键**,历史 pack 的 manifest_hash 不因 normalize 重算漂移。
        binding = str(raw_item.get("binding_state") or "").strip()
        if binding:
            item["binding_state"] = binding
        if verification in VERIFIED_STATES and not _verified_provenance_complete(item):
            item["verification_claimed_status"] = verification
            item["verification_status"] = (
                "body_retrieved_claim_unverified"
                if item.get("canonical_body_hash") else "search_result_only"
            )
            item["verification_error"] = "verification_provenance_incomplete"
        items.append(item)
    pack = {
        "version": EVIDENCE_PACK_VERSION,
        "request_id": request_id or str(data.get("request_id") or ""),
        "research_status": str(data.get("research_status") or ("ready" if items else "not_run")),
        "queries": list(data.get("queries") or []),
        "items": items,
        "created_at": data.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "limitations": list(data.get("limitations") or []),
    }
    # [工单 C 2026-07-27 · §2.5-3] 证据供给统计随 pack 落库(articles.evidence_pack
    # 是 JSONB),部署后核验门产出率可以直接 SQL 复测,不必人肉读正文。
    # 只在研究阶段真的产出了统计时才带上这个键 —— 空 pack 不凭空造字段。
    supply = data.get("evidence_supply")
    if isinstance(supply, dict) and supply:
        pack["evidence_supply"] = dict(supply)
    pack["manifest_hash"] = manifest_hash({k: v for k, v in pack.items() if k != "manifest_hash"})
    return pack


def validate_evidence_pack(pack: dict[str, Any]) -> dict[str, Any]:
    items = list(pack.get("items") or [])
    invalid: list[str] = []
    verified = 0
    support = 0
    refute = 0
    publishers: set[str] = set()
    verified_publishers: set[str] = set()
    evidence_ids: set[str] = set()
    for item in items:
        evidence_id = item.get("evidence_id") or "unknown"
        if evidence_id in evidence_ids:
            invalid.append(f"{evidence_id}:duplicate_evidence_id")
        evidence_ids.add(str(evidence_id))
        if not item.get("url"):
            invalid.append(f"{evidence_id}:missing_url")
        elif not _is_public_http_url(item.get("url")):
            invalid.append(f"{evidence_id}:invalid_url_scheme")
        if not item.get("claim"):
            invalid.append(f"{evidence_id}:missing_claim")
        if item.get("verification_error"):
            invalid.append(f"{evidence_id}:{item['verification_error']}")
        is_verified = (
            item.get("verification_status") in VERIFIED_STATES
            and _verified_provenance_complete(item)
        )
        if is_verified:
            verified += 1
        if item.get("relationship") == "support":
            support += 1
        if item.get("relationship") == "refute":
            refute += 1
        if item.get("publisher"):
            publisher = str(item["publisher"]).casefold()
            publishers.add(publisher)
            if is_verified:
                verified_publishers.add(publisher)
    return {
        "item_count": len(items),
        "verified_count": verified,
        "support_count": support,
        "refute_count": refute,
        # Compatibility name now has the only safe scoring meaning: distinct
        # publishers among claim-verified items. Discovery-only diversity is
        # reported separately and never increases the quality score.
        "publisher_count": len(verified_publishers),
        "discovered_publisher_count": len(publishers),
        "invalid": invalid,
        "ready_for_claims": verified > 0 and not invalid,
    }


def entity_axis_admits(item: dict[str, Any]) -> bool:
    """[R2-1/R3-1/R5.1 · H0-ENTITY-BINDING] 实体轴统一门(单点谓词)。

    - binding_state 存在 → 只认 ``entity_confirmed``(unverified/different = 候选);
    - binding_state 缺失但条目带 entity 归属 → 候选;
    - 无实体轴(无 entity 无 binding)→ 放行。
      🔴 [R5.1 裁定 §9.1] 无轴存量**零降级零标记**:拿元数据不完整代替
      真实性判断 = 批量降级网络素材,违反开发原则第 1/3 条(曾试过的
      「无 lane 标记 = 候选」方案已撤回)。无轴条目的**错实体归属**风险
      (深档多品牌 pack 装着竞品素材,修复链从中抓错家 → 给客户编造数据)
      由修复链的**主体核对**在消费端管:services/span_level_repair
      .repair_subject_admits(target + pack 实体白名单,确定性字符串判别)。
    🔴 消费纪律(R5 设计反转):业务代码**禁止**裸取 items 列表后自行
    调本谓词 —— 唯一消费入口是 iter_entity_admissible_items();
    结构用途(合并/去重/计数,不读内容进 prompt)走 raw_pack_items()。
    """
    binding = str((item or {}).get("binding_state") or "")
    if binding:
        return binding == "entity_confirmed"
    return not str((item or {}).get("entity") or "").strip()


def iter_entity_admissible_items(pack: dict[str, Any] | None):
    """🔴 [R5] 证据条目的**唯一消费 API**(写作/修复/评分等一切"读内容"场景)。

    只产出过实体轴统一门(entity_axis_admits)的条目。语法污点扫描在这根轴上
    连输两回(R3 单文件 / R4 四形态逃逸)之后的设计反转:出口不再各自装门,
    门长在唯一取用路径上;tests/test_p0_2 的白名单计数锁保证白名单之外
    没有任何 .py 裸触 items。
    """
    for item in (pack or {}).get("items") or []:
        if isinstance(item, dict) and entity_axis_admits(item):
            yield item


def raw_pack_items(pack: dict[str, Any] | None) -> list:
    """[R5] **结构访问器**:返回全部条目(含候选态)。

    仅限结构用途 —— 合并/去重/计数/评估/扫描判别。**禁止**把这里拿到的条目
    内容(claim/excerpt/title/publisher/数字)送进任何 prompt 或正文写回链;
    那类消费必须走 iter_entity_admissible_items()。
    """
    return [i for i in (pack or {}).get("items") or [] if isinstance(i, dict)]


def set_raw_pack_items(pack: dict[str, Any], items: list) -> None:
    """[R5] 结构写访问器(合并/回填 items 列表用)。"""
    pack["items"] = list(items or [])


def render_evidence_pack_by_entity(
    pack: dict[str, Any] | None,
    entities: list[str] | tuple[str, ...] | None,
) -> str:
    """[工单 C-2 T2 根因4] 按品牌分组的证据块 —— 贴在深档成卡名单旁,不再让模型自己捞。

    生产实证:逐家检索的产出混在一维平铺证据池里,竞品卡照样"公开渠道可查案例
    信息有限"。这里把 pack 条目按实体归属(item.entity 直标,兜底 scope/标题匹配)
    分组:每家列出 标题|媒体名|日期(正好是规格要求的具体标注三要素),已核验排前;
    一条都没有的家显式声明"留白收短",禁止模型用泛化标注硬凑。
    """
    names: list[str] = []
    seen: set[str] = set()
    for raw in entities or []:
        name = str(raw or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    if not names:
        return ""
    # [R3-1/R5] 本块**自己就是写作 prompt 的输入**(块头「写卡时本家优先用
    # 本组」+ 逐条「归属句照抄」),与 for_writer 段同门 —— R5 起统一走唯一
    # 消费 API,门不再在本函数内自装(Review 四态构造实证:缺 binding 的
    # 实体条目曾从这里漏进分组块)。
    items = list(iter_entity_admissible_items(pack))

    def _matches(item: dict[str, Any], name: str) -> bool:
        # 进到这里的条目已过统一门(confirmed 或显式非实体 lane)。
        if str(item.get("entity") or "").strip() == name:
            return True
        # 无实体轴条目的 scope/标题回退匹配 = C-2 T2 既有设计,维持现行为。
        return name in str(item.get("scope") or "") or name in str(item.get("title") or "")

    # 🔴 [返修 C6 2026-08-11] 逐家分组块的来源标签**经 `attribution_of` 现取**,
    # 与 writer 块同源 —— 旧块给未核验条目一律标「引用需保留来源归属(标题|
    # 媒体名+日期)」,与 D5 fail-closed 三态直顶(裸域名条目被指示写归属)。
    lines = [
        "【逐家检索证据（按品牌分组 · 写卡时本家优先用本组）】",
        "- 每张品牌卡的“关键能力/实证案例”优先引用本家分组下的条目;"
        "**归属按各条目自带的「归属句照抄/禁归属」标记执行**(与上方 Evidence Pack"
        "同一口径),禁用“公开行业报告”式泛化标注;",
        "- 某家分组为空时,该卡按规格“留白收短”:能力条目只保留有证据的,"
        "实证案例改写成有边界的情景说明,不硬凑。",
    ]
    for name in names:
        matched = [i for i in items if _matches(i, name)]
        matched.sort(key=lambda i: 0 if i.get("verification_status") in VERIFIED_STATES else 1)
        if not matched:
            lines.append(f"- {name}：本次检索未获可用证据 —— 该卡按“留白收短”处理。")
            continue
        lines.append(f"- {name}：")
        for item in matched[:4]:
            status = "已核验" if item.get("verification_status") in VERIFIED_STATES else "未核验"
            _attr = attribution_of(item)
            usage = (
                f"归属句照抄:{_attr['prose']}" if _attr["prose"]
                else "不得写外部归属(仅作事实原料)"
            )
            title = str(item.get("title") or "").strip() or "无标题"
            date = str(item.get("published_at") or "").strip() or "日期未标"
            lines.append(
                f"  · [{item.get('evidence_id')}|{status}] {title} | "
                f"{_attr['publisher'] or '来源未确认'} | {date} | {usage}"
            )
    return "\n".join(lines)


#: 🔴 [D5 信源增援 2026-08-10] 泛化类型词 —— 出现在 publisher 位上等于没标注。
#: 这批词与 `source_disclosure_style` 那张自曝黑名单同源(那边管正文,这边管素材)。
_GENERIC_PUBLISHER: Final = (
    "公开记录", "公开资料", "公开信息", "公开行业报告", "企业资料", "企业提交资料",
    "企业提供的资料", "行业报告", "网络", "互联网", "未知", "unknown", "n/a",
)


def _normalize_published_date(raw: Any) -> str:
    """把 `published_at` 归一成「2025 年 3 月」这种读者能读的写法。

    只认 ISO 前缀里的年月;认不出就返回空串 —— **宁可不写日期,也不编日期**。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    m = re.match(r"(\d{4})[-/年](\d{1,2})", text)
    if m:
        return f"{m.group(1)} 年 {int(m.group(2))} 月"
    m = re.match(r"(\d{4})", text)
    return f"{m.group(1)} 年" if m else ""


#: 裸域名(news.qq.com / 163.com / cnblogs.com)。端到端 A/B 实测:
#: 上游把**域名**存进了 publisher,于是新链写出「据163.com 2026年7月发布」——
#: 读起来像机器产物,而且《载体名》从 13 掉到 0。
#: 已采纳定稿的口径是「**文章标题**或媒体名 + 日期」,所以域名形态改用 title。
_BARE_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}$", re.I)


def _looks_like_domain(text: str) -> bool:
    return bool(_BARE_DOMAIN.match(str(text or "").strip()))


def _same_site(url: str, domains: tuple[str, ...]) -> bool:
    """这条证据是不是来自**客户自己的站**。"""
    host = ""
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except Exception:  # noqa: BLE001
        host = ""
    if not host:
        return False
    host = host[4:] if host.startswith("www.") else host
    for d in domains:
        d = str(d or "").strip().lower()
        if not d:
            continue
        d = d[4:] if d.startswith("www.") else d
        if host == d or host.endswith("." + d):
            return True
    return False


#: 来源可信状态 —— 决定这条证据**能不能写成外部归属**。
#: 🔴 [复审返工 2026-08-10] 生产实测 5,185 条 item:publisher 是**裸域名的占
#: 88.9%**(4609 条),Top15 依次是 cnblogs.com 769 / m.toutiao.com 144 /
#: baike.baidu.com 97 / 163.com 95 …… 还有 holike.com(竞品自己官网)。
#: 上一版把裸域名回退成《文章标题》,等于把博客园帖子、百度百科词条、
#: **竞品官网**和**客户自己官网**一律包装成可引用的第三方载体 —— 那是比
#: 「企业提交资料」更危险的自曝,因为它伪装成独立引用。
#: 所以这里改成 **fail-closed:必须正面证明是独立第三方,才产出归属句**。
TRUST_INDEPENDENT: Final = "independent"   # 可识别的机构/媒体名 → 可写外部归属
TRUST_UNVERIFIED: Final = "unverified"     # 裸域名 / 无法确认 → 只能内部校核
TRUST_SELF: Final = "self"                 # 客户自站/自有渠道 → 既不归属也不当外部证据
# 🔴 [返修 R7 2026-08-11] 已验证域名映射三档 —— fail-closed 的另一半:
# 大量真实公开素材(生产实测裸域名占 88.9%)只有域名写不出自然归属,直接伤
# 可信度与被引率。命中人工核过的映射 → 自然具名;**分档如实**,平台内容
# 不许写成"权威媒体/报道"。未命中 → 维持 fail-closed 省略归属,不编造。
#   [R3-A1 2026-08-11] 搜狐/网易/腾讯/凤凰/新浪主域已降档 platform_ugc:
#   生产 portal_media 档 584 items 实测几乎全是自发布频道(搜狐号/网易号/
#   新浪看点/企鹅号),按域名分不出编辑内容还是任意号主内容,「据搜狐网报道」
#   =假冒第三方归属(Owner 亲裁类别②)。portal_media 只留真编辑媒体。
TRUST_OFFICIAL_MEDIA: Final = "official_media"   # 人民网/新华网… → 据XX
TRUST_PORTAL_MEDIA: Final = "portal_media"       # 澎湃/36氪/界面… → 据XX
TRUST_PLATFORM_UGC: Final = "platform_ugc"       # 博客园/知乎/搜狐网/网易… → 据XX上的公开内容

#: 下发给模型的中文标签(模型读中文,不读英文枚举)。
_TRUST_LABEL: Final[dict[str, str]] = {
    TRUST_INDEPENDENT: "独立第三方",
    TRUST_UNVERIFIED: "未确认来源",
    TRUST_SELF: "自有渠道",
    TRUST_OFFICIAL_MEDIA: "官方媒体",
    TRUST_PORTAL_MEDIA: "门户媒体",
    TRUST_PLATFORM_UGC: "平台内容",
}
#: 可产出归属句的三档映射身份(+ independent)。
_MAPPED_TRUSTS: Final = (TRUST_OFFICIAL_MEDIA, TRUST_PORTAL_MEDIA, TRUST_PLATFORM_UGC)

_CARRIER_MAP_PATH: Final = (
    Path(__file__).resolve().parent.parent / "config" / "verified_domain_carriers.json"
)
_carrier_cache: dict[str, Any] = {}


def _load_domain_carriers() -> dict[str, dict[str, str]]:
    """热加载已验证域名映射(mtime 变更即重读;缺失/损坏 → 空映射 = 全部
    退回 fail-closed 省略归属,**不炸不编造** —— R7 变异验收③)。"""
    try:
        mtime = _CARRIER_MAP_PATH.stat().st_mtime
    except OSError:
        return {}
    if _carrier_cache.get("mtime") == mtime:
        return _carrier_cache.get("entries") or {}
    try:
        with open(_CARRIER_MAP_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        entries = {
            str(host).strip().lower(): {
                "carrier": str(meta.get("carrier") or "").strip(),
                "tier": str(meta.get("tier") or "").strip(),
            }
            for host, meta in (data.get("entries") or {}).items()
            if isinstance(meta, dict) and str(meta.get("carrier") or "").strip()
            and str(meta.get("tier") or "") in _MAPPED_TRUSTS
        }
    except (OSError, ValueError):
        return {}
    _carrier_cache["mtime"] = mtime
    _carrier_cache["entries"] = entries
    return entries


def carrier_for_domain(host: str) -> dict[str, str] | None:
    """域名 → 已验证载体。**先精确后最长后缀**(mp.weixin.qq.com 必须先于
    qq.com 命中,子域名身份不同要单列 —— 匹配顺序错了微信公众号就成了腾讯网)。"""
    host = str(host or "").strip().lower()
    host = host[4:] if host.startswith("www.") else host
    if not host:
        return None
    entries = _load_domain_carriers()
    if host in entries:
        return entries[host]
    best: tuple[int, dict[str, str]] | None = None
    for key, meta in entries.items():
        if host.endswith("." + key) and (best is None or len(key) > best[0]):
            best = (len(key), meta)
    return best[1] if best else None

#: publisher 位上指向**我方或某一方自有渠道**的标记。
#: 「公司公告」在这里 —— 它通常指公司自己发的公告,不是独立第三方
#: (复审②:漏删不是错删)。真正第三方的监管/交易所公示由「公示/通报」等词承担。
_SELF_CHANNEL_PUBLISHER = re.compile(
    r"(?:官网|官方网站|官方公众号|官方微博|官方旗舰店|企业官网|公司官网|"
    r"公司公告|本公司|自述|自有渠道|内部)"
)


def _is_self_named(publisher: str, self_names: tuple[str, ...]) -> bool:
    """publisher 里带着客户自己的名字 → 是客户自有渠道,不是第三方背书。"""
    text = str(publisher or "").strip()
    if not text:
        return False
    for name in self_names:
        name = str(name or "").strip()
        # 2 字以下的名字太容易误伤(如「富士」会命中「富士通电梯」),不参与判定
        if len(name) >= 3 and name in text:
            return True
    return False


def source_trust_of(
    item: dict[str, Any], *,
    client_domains: tuple[str, ...] = (), self_names: tuple[str, ...] = (),
) -> str:
    """判定这条证据的**来源可信状态**(见 TRUST_* 三态)。

    🔴 fail-closed:任何一条判不出「可识别的独立机构/媒体名」的,一律
    `TRUST_UNVERIFIED` —— 宁可不给归属,不给假归属。
    """
    publisher = str(item.get("publisher") or "").strip()

    # ① 客户自站(有 client_domains 时的纵深;当前无供给源,不承重)
    if client_domains and _same_site(item.get("url"), client_domains):
        return TRUST_SELF
    # ② publisher 自带自有渠道标记 / 带客户自己的名字
    if publisher and (_SELF_CHANNEL_PUBLISHER.search(publisher)
                      or _is_self_named(publisher, self_names)):
        return TRUST_SELF
    # ③ 空 / 泛化类型词 → 没标注
    if not publisher or publisher.lower() in {g.lower() for g in _GENERIC_PUBLISHER}:
        return TRUST_UNVERIFIED
    # ④ 🔴 裸域名:先查**已验证域名映射**(R7),命中 → 按人工核过的分档
    #    (官方媒体/门户媒体/平台内容)给自然具名资格;未命中 → 维持
    #    fail-closed 未确认(不再退成《文章标题》,也不编载体)。
    #    holike.com(竞品站)/ 客户自站不在映射里,天然走不进具名档。
    if _looks_like_domain(publisher):
        mapped = carrier_for_domain(publisher)
        if mapped:
            return mapped["tier"]
        return TRUST_UNVERIFIED
    # ⑤ 剩下的是可识别的机构/媒体名
    return TRUST_INDEPENDENT


def attribution_of(
    item: dict[str, Any], *,
    client_domains: tuple[str, ...] = (), self_names: tuple[str, ...] = (),
) -> dict[str, str]:
    """从一条 evidence item 里取出写**归属句**的三要素。

    这是 D5 的核心:生产实测 5,185 条 item 里 publisher 填充率 **100%**、
    published_at **90%**,但 publisher 出现在正文里只有 **4.6%** ——
    因为渲染函数从没把它们下发给模型。本函数是那两个字段进 prompt 的**唯一入口**。

    Returns:
        ``{"publisher": ..., "date": ..., "prose": ...}``。
        `prose` 是可直接照抄的归属句前缀(「据XX 2025 年 3 月」);
        publisher 缺失或是泛化类型词时 `prose` 为空串 —— 那种情况**不许**写成外部归属。
    """
    # 🔴 [复审返工 2026-08-10] 归属句**只由 `source_trust_of` 一处判定**。
    # 上一版在这里自己判了一遍(裸域名回退《文章标题》),结果是:判定函数说
    # unverified,本函数照样产出「据《好莱客全屋定制》」—— 竞品官网被包装成
    # 第三方载体。这是「新判定写了、消费端没接」的第八例,自查时当场抓到。
    trust = source_trust_of(item, client_domains=client_domains, self_names=self_names)
    date = _normalize_published_date(item.get("published_at"))
    # 🔴 [R7] 映射三档:自然具名,**分档如实** —— 媒体档写「据XX」;
    # 平台内容档写「据XX上的公开内容」,不许包装成"权威媒体/报道"。
    if trust in _MAPPED_TRUSTS:
        raw_publisher = str(item.get("publisher") or "").strip()
        raw_publisher = raw_publisher[4:] if raw_publisher.lower().startswith("www.") else raw_publisher
        mapped = carrier_for_domain(raw_publisher) or {}
        carrier = mapped.get("carrier") or ""
        if not carrier:
            return {"publisher": "", "date": "", "prose": "", "trust": TRUST_UNVERIFIED}
        if trust == TRUST_PLATFORM_UGC:
            prose = f"据{carrier}上的公开内容{(' ' + date) if date else ''}"
        else:
            prose = f"据{carrier}{(' ' + date) if date else ''}"
        return {"publisher": carrier, "date": date, "prose": prose, "trust": trust}
    if trust != TRUST_INDEPENDENT:
        # fail-closed:证不出独立第三方 → 不给归属句。
        # 事实本身仍会下发给模型(见 render_evidence_pack_for_writer),
        # 但只能用于内部校核 / 改写成不需要外部归属的表达。
        return {"publisher": "", "date": "", "prose": "", "trust": trust}

    publisher = str(item.get("publisher") or "").strip()
    prose = f"据{publisher}{(' ' + date) if date else ''}"
    return {"publisher": publisher, "date": date, "prose": prose, "trust": trust}


def render_evidence_pack_for_writer(
    pack: dict[str, Any], *,
    client_domains: tuple[str, ...] = (), self_names: tuple[str, ...] = (),
) -> str:
    """渲染写作块。

    Args:
        client_domains: 客户**自己的站**域名。🔴 实测 `brands` /
            `brand_fact_snapshot` / `brand_profiles` **三张表都没有网址类列**
            (`information_schema` 查询 0 行),所以这个参数当前**无供给源**、
            恒空。它已降级为纵深,**不承重** —— 自站/竞品站由下面的
            `source_trust_of` fail-closed 判定挡住,不依赖本参数。
        self_names: 客户品牌名(及别名)。publisher 里带客户自己的名字
            → 判为自有渠道,不产出归属句。这个**有**供给源(`self.brand_name`)。
    """
    lines = [
        f"【Evidence Pack {pack.get('version') or EVIDENCE_PACK_VERSION}】",
        # [D11 · SSOT v2.1 ②] 公开信源升为可引用证据:秘塔等检索到的**真实存在**的
        # 公开网页/学术资料即为正文可引用佐证(AI 提效替代人工找资料,不是造假),
        # 原 "search_result_only 不得写成事实" 的降权判定已废除。
        # 🔴 [返修 C5 2026-08-11] 头部句限定作用域 —— 旧头部对**全体**条目说
        # "均可直接引用并保留归属",与逐条的「未确认来源禁归属」直顶(生产
        # 88.9% 裸域名),产出「据cnblogs.com」式假第三方或归属整体塌回泛化。
        "以下来源均为真实检索/提交所得。**标「独立第三方/官方媒体/门户媒体/"
        "平台内容」的条目**:照抄其「归属句照抄」直接引用,写成读者能读的自然句子;"
        "**其余条目**(未确认来源/自有渠道):仅作事实原料,按逐条给出的用法处理,"
        "不写外部归属。"
        "⚠️ 正文中不得出现 Evidence ID / BF-/EV- 编号、证据状态表或“待核验”类内部语言；"
        "只能引用下方真实存在的来源，严禁编造 URL、文献、机构或数据。",
        # 🔴 [D5 信源增援 2026-08-10] 把「来源方」和「日期」明确下发,并给出照抄格式。
        #
        # 生产实测(319 篇 / 5,185 条 evidence item):
        #   · 有 publisher(来源方) **100.0%**
        #   · 有 published_at(日期) **90.0%**
        #   · 但 publisher **出现在正文里只有 4.6%**
        # 原因就在这个渲染函数:它一个字都没输出 publisher / published_at,
        # 却在上面用自然语言要求模型「保留来源归属(如"据《XX》报道")」——
        # **我们从来没告诉过模型 XX 是谁。** 素材是齐的,断在没下发。
        #
        # 同期实测:外部信源存在率从 89.0% 掉到 61.0%,塌方精确落在归属句式
        # (「据X报道/披露/显示」74.0% → 38.3%)。所以要补的是**归属动作的原料**。
        # 🔴 [D4 claim 级证据复用 2026-08-10] `relationship` 早就有
        # support / refute / background 三态,但一直只以**英文裸词**下发,
        # 也从没说过"反驳条目不许当支持用"。于是同一条证据可能被拿去支撑
        # 一个它其实在反驳的结论 —— 那是最难被发现的一类不实。
        # 复审 P2-2:成功指标改为 支持判断 precision/recall、反驳误采率、
        # 来源存在率、片段哈希可回查率;**禁止**用"采纳了多少条"当指标。
        "🔴 每条证据的第一个标记是它对该主张的**立场**:"
        "`support`=支持,可用来支撑结论;"
        "`refute`=**反驳**,只能用来写限制、反面情形或风险,"
        "**严禁**把它当成支持证据引用;"
        "`background`=背景,只能交代语境,不能单独支撑结论或名次。"
        "同一条证据在不同主张下立场可能不同 —— 按**这一条主张**的立场用,"
        "不要跨主张复用。",
        # 🔴 [复审返工 2026-08-10 · P1-3/D5] 分两栏下发。
        # 生产实测 5,185 条证据:publisher 是**裸域名的占 88.9%**,前几名是
        # cnblogs.com(博客平台) 769 / m.toutiao.com 144 / baike.baidu.com(UGC) 97,
        # 还有 holike.com —— **竞品自己的官网**。上一版把裸域名回退成《文章标题》,
        # 等于把这些一律包装成可引用的第三方载体,写出「据《好莱客全屋定制》」
        # 这种假第三方引用。所以改成:**证不出独立第三方就不给归属句**,
        # 但事实本身照发,让模型走降级阶梯用它。
        "🔴 每条证据都标了「来源可信状态」，两类用法完全不同：\n"
        "  · `独立第三方` —— 后面给了「归属句照抄」，"
        "正文里承担推荐／比较／名次结论的主张**就近照抄它**，"
        "写成「据〈来源方〉〈日期〉报道／披露／显示，……」。日期缺失就只写来源方，不要编日期。\n"
        "  · `未确认来源` / `自有渠道` —— **禁止**写成「据XX」式外部归属，"
        "也不得说成第三方报道、独立核验或行业共识。它的事实**可以用**，"
        "但只能改写成不需要外部归属的表达（工况区间、行业通行口径、可复算推导），"
        "或者按降级阶梯把这条主张收短、降为背景、直至不写。"
        "🔴 这一条比“把文章写满”优先：宁可少写一个维度，也不要给一个撑不住的来源。",
    ]
    # [标准类证据 lane 2026-07-29 · §4 写作铁律] 标题级证据只给"存在性引用"权。
    # 这条与 lane 同批交付,不拆:没有它,标题级标准名进了证据池,模型会凭预训练知识
    # 补出「该标准规定…≤0.03mg/m³」这类**看似极可信、实则纯编造**的句子,
    # 读者与客户几乎不会去核 —— 那种状态比不做这条 lane 更糟。
    if any(
        str(i.get("source_tier") or "") == STANDARD_CITATION_TIER
        for i in (pack.get("items") or []) if isinstance(i, dict)
    ):
        lines.append(STANDARD_CITATION_WRITING_RULE)
    # [P0-2/R2-1/R3-1 · H0-ENTITY-BINDING] 带实体归属语义的条目,**只有
    # entity_confirmed 才下发给写作模型**(unverified 的出口 = advisory 卡
    # 「忽略继续/重新生成」,不阻断;主题 lane 无实体轴照常下发)。
    # [R5] 唯一消费 API(与 render_evidence_pack_by_entity 同门同路径)。
    _usable_items = list(iter_entity_admissible_items(pack))
    for item in _usable_items:
        # 🔴 [D5] publisher / published_at 必须显式下发 —— 它们是写归属句的**唯一原料**。
        #    取值经 `attribution_of()` 现取,不在这里拼字符串(改 SSOT 这里跟着变)。
        _attr = attribution_of(
            item, client_domains=client_domains, self_names=self_names,
        )
        # `.get` 不是 `[...]`:调用方替换过 `attribution_of` 时缺 trust 键
        # 也不能让渲染崩掉(D8 零阻断 —— 测试已实证会 KeyError)。
        _trust_label = _TRUST_LABEL.get(
            _attr.get("trust") or TRUST_UNVERIFIED, _TRUST_LABEL[TRUST_UNVERIFIED],
        )
        if _attr["prose"] and _attr.get("trust") == TRUST_PLATFORM_UGC:
            # [R7] 平台内容:可具名,但按平台身份表述 —— 不称"报道/权威媒体"。
            _use = (
                f"归属句照抄：{_attr['prose']}"
                "（平台内容：后接“显示／提到”即可，**不写“报道”**，不称权威媒体）"
            )
        elif _attr["prose"]:
            _use = f"归属句照抄：{_attr['prose']}"
        else:
            _use = (
                "用法：本条**不得写成外部归属**（不写“据…”、不称第三方报道／独立核验）；"
                "事实可用于改写成不需外部归属的表达，或按降级阶梯收短、降为背景、不写"
            )
        lines.append(
            f"- [{item['evidence_id']}] {item['relationship']} | "
            f"{item['verification_status']} | {item['title']} | {item['url']} | "
            f"来源可信状态：{_trust_label} | "
            f"来源方：{_attr['publisher'] or '未确认'} | "
            f"日期：{_attr['date'] or '未标注'} | {_use} | "
            f"主张：{item['claim']} | 范围：{item['scope'] or '未说明'} | "
            f"摘录：{item['excerpt']}"
        )
    if not _usable_items:
        lines.append("- 本次没有经过全文核验的外部证据：不得写外部数字、案例、资质或比较结论。")
    for limitation in pack.get("limitations") or []:
        lines.append(f"- 限制：{limitation}")
    return "\n".join(lines)
