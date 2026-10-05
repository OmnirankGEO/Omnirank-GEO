"""Neutral, capped research planner for article Evidence Packs.

Search discovery and full-text verification are deliberately separate.  Search
snippets never become verified claims.  The planner first reuses a JC3+ corpus
body for an exact URL, then uses the existing controlled reader when necessary.
Automatic research is feature-flagged off until its cost/privacy pilot passes.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import ipaddress
import json
import os
import re
from typing import Any, Awaitable, Callable, Final, Sequence
from urllib.parse import urlparse

from .evidence_pack import normalize_evidence_pack


RESEARCH_PLANNER_VERSION: Final = "evidence-research-v1.0"
_TRUE = frozenset({"1", "true", "yes", "on"})
_GOVERNMENT_SUFFIXES = (".gov.cn", ".gov")
_ACADEMIC_SUFFIXES = (".edu", ".edu.cn")


def is_automatic_research_enabled() -> bool:
    return os.getenv("GEO_ARTICLE_EVIDENCE_RESEARCH_ENABLED", "false").strip().lower() in _TRUE


def _cap(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(0, min(maximum, value))


def _public_http_url(raw: str) -> bool:
    try:
        parsed = urlparse(str(raw or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.casefold().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 标准类证据 lane(工单 WORKORDER_STANDARD_EVIDENCE_LANE_2026-07-29 · §2)
#
# 放行条件(**两条都是必要条件,不是 OR**):
#     source == 'metaso.document'  AND  gov_host(item)
#
# 历史留痕(避免重蹈):
#   v1 提议 `authorityType=government` AND 标题含编号 —— 实测 30 条样本是空集
#     (两信号反相关:带编号的来自标准全文库 authorityType=None;government 的
#      标题写中文名、编号不在标题里)。
#   v1.5 改成 OR 仍然错 —— 60 条实测里"仅靠标题含编号"放行的 7 条域名 100% 非权威
#     (www.weboos.cn ×3 / pro5323b5d3-pic11.ysjianzhan.cn ×3 / www.ds-101.com ×1),
#     其中 ysjianzhan 是建站平台随机站点 ID:任何人都能开站传 PDF、标题随便写,
#     而标题看起来完全正规(《住宅性能评定标准 Standard for performance assessment》)。
#     **光看标题分辨不出来源是否权威** → 标题含编号那条路本单砍掉,不产生放行权。
# ---------------------------------------------------------------------------
#: 🔴 本 lane 专用常量,单独定义。**禁止复用 `_GOVERNMENT_SUFFIXES`** ——
#: 那个是 ('.gov.cn', '.gov') 含美国政府域,且它还服务于 `_source_tier`,
#: 改它会波及现有来源分层判定。本 lane 只收 `.gov.cn`(Owner §0-3 裁定)。
_STANDARD_LANE_GOV_SUFFIX: Final = ".gov.cn"
#: 逐字段判定的三个来源字段,任一命中即真。
_STANDARD_LANE_HOST_FIELDS: Final = ("link", "url", "authorityDomain")


def _gov_host(value: str) -> bool:
    """hostname 后缀判定。裸域名(authorityDomain 常无 scheme)先补 '//' 再解析。

    🔴 归一化必须在函数体内做 —— authorityDomain='www.wuda.gov.cn' 没有 scheme,
       urlparse 直接解会得到 hostname=None,静默返回 False,把真权威源判掉。

    🔴 **禁止 `in` 匹配**:`'.gov.cn' in link` 会放行 `https://evil.com/?ref=x.gov.cn`;
       `'.gov.cn' in authorityDomain` 会放行 `fake.gov.cn.attacker.io`。
       这是证据放行闸,不是日志过滤。
    """
    v = str(value or "")
    if "//" not in v:
        v = "//" + v
    host = (urlparse(v).hostname or "").lower()
    return host == "gov.cn" or host.endswith(_STANDARD_LANE_GOV_SUFFIX)


def standard_lane_admits(item: Any) -> bool:
    """标准类证据"标题级免正文"放行闸(工单 §2 最终版)。

    ``source == 'metaso.document' AND gov_host(link|url|authorityDomain)``

    外层 source 是**唯一入口约束**:去掉它,任意 `.gov.cn` 网页会从 evidence /
    research 别的 lane 混进来白拿免正文特权。
    """
    if not isinstance(item, dict):
        return False
    from tools.search.provider_router import STANDARD_LANE_SOURCE

    if str(item.get("source") or "") != STANDARD_LANE_SOURCE:
        return False
    return any(_gov_host(item.get(field)) for field in _STANDARD_LANE_HOST_FIELDS)


#: 标准编号正则。**只用于 §4.2 分类(区分标准类 vs 地方性行政文件),不产生任何放行权。**
#: `(?:/T)?` 不可去:`DB11/T 1234-2020` 是真实形态,去掉就漏判成"地方性行政文件"。
#: 实测不误伤:`GBP 汇率 1234` / `GBK 编码表 2024` / `DB 数据库设计 2018` 均不匹配。
STD_CODE: Final = re.compile(
    r'(GB/T|GBT|GB|JGJ/T|JGJ|CJJ|DB[0-9]{2}(?:/T)?|T/[A-Z]{2,6})\s*[-—]?\s*[0-9]{4,5}'
)
#: 地方性具体行政文件的文号形态:`罗住建[2017]375号` / `京建发〔2019〕12 号`。
_ADMIN_DOC_NO: Final = re.compile(r'[\[\[〔（(]\s*(?:19|20)\d{2}\s*[\]\]〕）)]\s*第?\s*\d+\s*号')
#: 本 lane 条目的分类值(随条目落库,写作侧与发布门都读它)。
STANDARD_CLASS_STANDARD: Final = "standard"
STANDARD_CLASS_LOCAL_ADMIN: Final = "local_administrative"


def classify_standard_document(title: str) -> str:
    """§4.2 分类:含 `〔年份〕文号` 形态 **且不匹配 STD_CODE** → 地方性具体行政文件。

    为什么要分:国标通用、公开、有大量二手解读可交叉验证;**一份区级通知的具体条款
    几乎无人会去核,编造成本最低、被发现概率最小**。这类条目在写作侧连存在性引用
    也要克制(只在确实与主题强相关时引,不得为了"显得有出处"而堆砌)。
    """
    text = str(title or "")
    if _ADMIN_DOC_NO.search(text) and not STD_CODE.search(text):
        return STANDARD_CLASS_LOCAL_ADMIN
    return STANDARD_CLASS_STANDARD


# ---------------------------------------------------------------------------
# [P0-1 · 2026-08-14] 题证对齐 + 同 quote 检索复用
#
# 研究定稿(GEO_ARTICLE_PIPELINE_SYSTEMIC_RESEARCH_2026-08-13 §根因A)实证:
#   `_topic_lanes` 五条 query 全部由 quote 级变量(keyword/industry/brand)构成,
#   `title` 形参从未进入 query → QZQZ 34 篇 696 次检索调用 95.5% 字节级重复
#   (6 套 query 服务 34 篇),证据链与"这一篇要回答的问题"整体脱钩。
# 两个修法(同包,互为表里):
#   1. **query 含文章级变量** —— 标题意图词(去掉 quote 级变量后的剩余)注入
#      文章级 lane 与逐家 lane,让每次检索真的对准本篇问题;
#   2. **同 quote 显式复用** —— 相同 (cache_scope, query) 的检索结果进程内复用,
#      只发一次 provider 调用;不同 quote 绝不互用(scope 进键)。
# ---------------------------------------------------------------------------
_TERM_RE: Final = re.compile(r"[一-鿿a-zA-Z0-9]+")


def _article_terms(title: str, *, keyword: str = "", brand: str = "") -> str:
    """标题里的**文章级**意图词:去掉 quote 级变量(关键词/品牌名)后的剩余文本。

    确定性、无 LLM。产出为空(标题完全由 quote 级变量组成)时调用方不注入,
    行为与旧 lane 一致 —— 空值走现行为,如实降级。"""
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    for chunk in (keyword, brand):
        c = str(chunk or "").strip()
        if c:
            text = text.replace(c, " ")
    return " ".join(_TERM_RE.findall(text))[:24].strip()


def _join_q(*parts: str) -> str:
    """拼 query:去空段、单空格连接(意图词为空时不产生连续空格)。"""
    return " ".join(p for p in (str(x or "").strip() for x in parts) if p)


#: 同 quote 检索缓存:scope → {kind::query → 结果 list}。进程内(生产 WORKERS=1
#: 契约,见手册 §17.1),批内命中即消灭重复调用;跨进程/重启不保证命中 —— 那只是
#: 退化为多付一次检索费,不是错误。scope 上限防长驻进程无界增长。
_LANE_CACHE_MAX_SCOPES: Final = 8
_lane_cache: "OrderedDict[str, dict[str, list[dict[str, Any]]]]" = OrderedDict()


def _lane_cache_get(scope: str, kind: str, query: str) -> list[dict[str, Any]] | None:
    if not scope:
        return None
    bucket = _lane_cache.get(scope)
    if bucket is None:
        return None
    hit = bucket.get(f"{kind}::{query}")
    if hit is None:
        return None
    # 浅拷贝每条,防调用方原地改动污染缓存(条目字段只读消费,一层即够)。
    return [dict(c) for c in hit]


def _lane_cache_put(scope: str, kind: str, query: str, results: list[dict[str, Any]]) -> None:
    if not scope:
        return
    bucket = _lane_cache.get(scope)
    if bucket is None:
        while len(_lane_cache) >= _LANE_CACHE_MAX_SCOPES:
            _lane_cache.popitem(last=False)
        bucket = {}
        _lane_cache[scope] = bucket
    bucket[f"{kind}::{query}"] = [dict(c) for c in results]


# ---------------------------------------------------------------------------
# [R2-3 2026-08-15] single-flight:同 key **并发**去重
#
# R2 独立反例:get/await/put 形态下,同 scope 4 个 query 各并发 2 次 →
# provider_calls=8(批量写作真实并发路径 article_generator_service:912/:1093,
# 每次都是真钱)。改为按 (scope, kind, query) 共享进行中 Future:
#   - 并发同 key:只有 leader 真调 provider,其余等待共享结果(计 cache_hit);
#   - 成功:落缓存 + 清进行中态;
#   - 异常/取消:**同样清进行中态**(后续新请求重新发起,绝不复用坏 Future),
#     当轮等待者如实收到该异常(它们本来也会失败,不造假绿)。
# ---------------------------------------------------------------------------
_inflight: dict[tuple[str, str, str], "asyncio.Future[list[dict[str, Any]]]"] = {}


async def _single_flight_fetch(
    scope: str,
    kind: str,
    query: str,
    factory: Callable[[], Awaitable[list[dict[str, Any]]]],
) -> tuple[list[dict[str, Any]], bool]:
    """返回 ``(结果, cache_hit)``。scope 空 = 不缓存不合并(旧签名行为)。"""
    if not scope:
        return await factory(), False
    cached = _lane_cache_get(scope, kind, query)
    if cached is not None:
        return cached, True
    key = (scope, kind, query)
    existing = _inflight.get(key)
    if existing is not None:
        # 等待者:共享 leader 的结果(浅拷贝防交叉污染);leader 失败则同失败。
        result = await asyncio.shield(existing)
        return [dict(c) for c in result], True
    fut: "asyncio.Future[list[dict[str, Any]]]" = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    try:
        result = await factory()
    except BaseException as exc:  # 含 CancelledError:清理进行中态是三条路径共同义务
        _inflight.pop(key, None)
        if not fut.done():
            fut.set_exception(exc)
            # 无等待者时避免 "exception never retrieved" 噪音
            fut.add_done_callback(lambda f: f.exception())
        raise
    _inflight.pop(key, None)
    _lane_cache_put(scope, kind, query, result)
    if not fut.done():
        fut.set_result(result)
    return result, False


# ---------------------------------------------------------------------------
# [P0-2 · 2026-08-14] 实体绑定三态 —— 确定性判别,禁用 LLM
#
# 研究定稿方法学负结果(§8):20 条已知错实体(kzwoodcrafts.com,42 条同域名
# 证据被贴到客户品牌名下)LLM 判 no = 0/20,全假阴性 → 实体检出必须走确定性
# 判别,不能靠 LLM。
#
# 🔴 新增 H0 按手册 §1.4「新增 H0 必须写清」逐条填:
#   - H0 类别:§1.4-4 对象身份与数据完整性(证据条目归属错实体 = 身份完整性破坏);
#   - rule_id:`H0-ENTITY-BINDING`;rule_version:1;
#   - 拦截对象:**单条 evidence item 的实体绑定**(different_entity 只隔离该条,
#     不阻断整篇文章 —— 工单红线 3);
#   - 修复出口(R2-4:只声称现役真动作):忽略继续(batch-advisory-continue)/
#     重新生成本篇重新检索(既有生成入口);「确认同主体」专用入口未建,不声称;
#   - 判别测试:tests/test_p0_2_entity_binding_2026_08_14.py(含 R2-4 反例矩阵)。
# ---------------------------------------------------------------------------
BINDING_CONFIRMED: Final = "entity_confirmed"
BINDING_UNVERIFIED: Final = "entity_unverified"
BINDING_DIFFERENT: Final = "different_entity"
ENTITY_BINDING_RULE_ID: Final = "H0-ENTITY-BINDING"
ENTITY_BINDING_RULE_VERSION: Final = 1


def _norm_binding_text(text: str) -> str:
    """归一:去空白与标点、casefold。CJK 与拉丁字母数字都保留。"""
    return "".join(_TERM_RE.findall(str(text or ""))).casefold()


# [R2-4 2026-08-15] 弱 token 病灶根治:旧实现把公司名**全部 ≥3 字子串**当判别
# token(含「深圳」「浙江」「有限公」这类城市名/公司后缀切片)→ 任何同城/同后缀
# 的**不同企业**都被从 different 降成 unverified(R2 反例矩阵:深圳栖舍设计/
# 浙江岱林生物/晨光富士电梯互相污染)。新口径:判别只认**去行政区划前缀 +
# 去组织后缀后的核心词**(标准品牌名 + 可选别名),弱词永不参与判定。
#: 行政区划前缀(省/直辖市/常见地级市)。只用于**剥前缀**:列表不全的后果是
#: 「剥不掉 → 核心词更长 → 判定更严(倾向 different)」,方向安全,不会放松。
_ADMIN_REGION_PREFIXES: Final[tuple[str, ...]] = (
    "黑龙江", "内蒙古", "北京", "上海", "天津", "重庆", "河北", "山西", "辽宁",
    "吉林", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北",
    "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海",
    "广西", "西藏", "宁夏", "新疆", "香港", "澳门", "台湾",
    "深圳", "广州", "杭州", "南京", "苏州", "成都", "武汉", "西安", "郑州",
    "长沙", "合肥", "福州", "厦门", "青岛", "济南", "大连", "沈阳", "东莞",
    "佛山", "宁波", "无锡", "揭阳",
)
#: 组织形态后缀(从尾部迭代剥,长在前)。
_ORG_SUFFIXES: Final[tuple[str, ...]] = (
    "股份有限公司", "有限责任公司", "集团有限公司", "科技有限公司",
    "有限公司", "控股集团", "集团", "股份", "公司", "研究院", "事务所",
    "工作室", "门店", "厂",
)


def brand_core_term(name: str) -> str:
    """标准名 → 判别性核心词(去行政区划前缀、去组织后缀;确定性)。

    「深圳栖舍设计有限公司」→「栖舍设计」;「浙江岱林生物技术股份有限公司」→
    「岱林生物技术」。剥到剩 <2 字就停(核心词不能剥空)。"""
    text = _norm_binding_text(name)
    changed = True
    while changed:
        changed = False
        for region in _ADMIN_REGION_PREFIXES:
            r = region.casefold()
            if text.startswith(r) and len(text) - len(r) >= 2:
                text = text[len(r):]
                changed = True
        for suffix in _ORG_SUFFIXES:
            s = suffix.casefold()
            if text.endswith(s) and len(text) - len(s) >= 2:
                text = text[: -len(s)]
                changed = True
    return text


def entity_binding_state(
    item: dict[str, Any], entity: str, *, aliases: Sequence[str] = (),
) -> str:
    """单条证据 × 实体 → 绑定三态(确定性,同输入必同输出)。

    判定阶梯(R2-4:弱词零参与):
    - `entity_confirmed`:**全名或判别性核心词**(或任一别名的同款形态)出现在
      内容位(title/excerpt/claim);
    - `entity_unverified`:全名/核心词只在辅助位(url/publisher)出现,或核心词的
      ≥3 字**子串**命中(如「岱林生物」命中核心「岱林生物技术」的一段)——
      该条**不作为该品牌证据使用**(候选态);可忽略继续,或重新生成本篇以
      重新检索来源(两者都是现役真动作,不声称不存在的出口);
    - `different_entity`:全名/核心词及其 ≥3 字子串在任何位置都不出现 ——
      为该实体检索却整页不提该实体,H0 隔离该条(仅该条,文章照常)。

    🔴 `scope` 字段(内含检索 query,query 必含实体名)**刻意排除**在判定之外,
    否则恒 confirmed、判定力归零。
    🔴 城市名/省名/「有限公司」等弱词**永不参与**判定(R2-4 反例矩阵锁死)。
    """
    full = _norm_binding_text(entity)
    if len(full) < 2:
        return BINDING_UNVERIFIED
    names = [str(entity)] + [str(a) for a in (aliases or []) if str(a or "").strip()]
    fulls = [_norm_binding_text(n) for n in names]
    cores = [c for c in (brand_core_term(n) for n in names) if len(c) >= 2]

    content = _norm_binding_text(
        f"{item.get('title') or ''} {item.get('excerpt') or ''} {item.get('claim') or ''}"
    )
    for probe in fulls + cores:
        if probe and probe in content:
            return BINDING_CONFIRMED
    aux = _norm_binding_text(f"{item.get('url') or ''} {item.get('publisher') or ''}")
    for probe in fulls + cores:
        if probe and probe in aux:
            return BINDING_UNVERIFIED
    haystack = content + " " + aux
    # 🔴 partial 只认**核心词前缀**(≥3 字):品牌词在中文企业名里领头,
    # 前缀命中(如「岱林生物」×核心「岱林生物技术」)才有判别力;任意子串会把
    # 「生物技术」这类行业词切片重新放进闸里(R2-4 反例矩阵② 实测抓获)。
    for core in cores:
        for size in range(len(core) - 1, 2, -1):
            if core[:size] in haystack:
                return BINDING_UNVERIFIED
    return BINDING_DIFFERENT


def _source_tier(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold()
    if host.endswith(_GOVERNMENT_SUFFIXES):
        return "government_domain_unverified_claim"
    if host.endswith(_ACADEMIC_SUFFIXES):
        return "academic_domain_unverified_claim"
    if any(token in host for token in ("doi.org", "pubmed", "cnki", "wanfang", "journal")):
        return "research_index_unverified_claim"
    return "public_web_unclassified"


def _response_payload(response: Any) -> dict[str, Any]:
    try:
        blocks = response.content or []
        block = blocks[0] if blocks else {}
        raw = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data if isinstance(data, dict) else {"content": data}
    except Exception:
        return {}


def _body_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "markdown", "content", "data"):
            text = _body_text(value.get(key))
            if text:
                return text
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, list):
        return "\n".join(filter(None, (_body_text(item) for item in value))).strip()
    return ""


def _claim_from(title: str, snippet: str, lane: str) -> str:
    clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
    clean_snippet = re.sub(r"\s+", " ", str(snippet or "")).strip()
    basis = clean_title or clean_snippet[:180]
    return f"{lane}候选证据：{basis}" if basis else f"{lane}候选证据，需人工提取具体主张"


def _load_jc3_body(url: str) -> tuple[str, dict[str, Any]]:
    """Read an exact-URL body only when corpus admission is at least JC3."""
    conn = None
    try:
        from db.diagnosis_db import get_connection

        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, inline_cleaned_content, oss_key_cleaned, canonical_body_hash,
                   body_hash_algorithm, corpus_grade, fetched_at
            FROM geo_research_articles
            WHERE url = %s AND corpus_grade IN ('JC3', 'JC4', 'JC5')
            ORDER BY fetched_at DESC NULLS LAST, id DESC
            LIMIT 1
            """,
            (url,),
        )
        row = cur.fetchone()
        if not row:
            return "", {}
        text = str(row.get("inline_cleaned_content") or "")
        if not text and row.get("oss_key_cleaned"):
            from services.research_monitor.oss_helper import download_markdown

            text = download_markdown(row["oss_key_cleaned"]) or ""
        if not text.strip():
            return "", {}
        return text, {
            "verification_status": "body_retrieved_claim_unverified",
            "canonical_body_hash": row.get("canonical_body_hash"),
            "body_hash_algorithm": row.get("body_hash_algorithm"),
            "body_boundary_version": "jina-cleaned-markdown-body-v1",
            "fetch_event_id": None,
            "fetched_at": row.get("fetched_at"),
            "corpus_grade": row.get("corpus_grade"),
            "corpus_article_id": row.get("id"),
        }
    except Exception:
        return "", {}
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# [工单 C 2026-07-27 · §2.5] 深档证据供给 —— 按白名单逐家定向检索
#
# 矛盾:预算表要求品牌卡约 9400 字,证据纪律要求"无证据留白收短",而现行检索只围绕
# 主题、每篇核验后仅 ~2.4 条可用 —— 12 张卡摊不到证据,卡片必然收短,14000 达不成。
# **深档的字数上限实际由证据供给决定。**
#
# 只对进深档的篇目生效;紧凑档一次新增调用都没有。
# ---------------------------------------------------------------------------
#: 每家实体的定向 query 条数(官方事实 1 条 + 公开案例/媒体 1 条)。
DEEP_TIER_QUERIES_PER_ENTITY: Final = 2
#: 主题项 query 条数。
DEEP_TIER_TOPIC_QUERIES: Final = 4


def deep_tier_query_budget(entity_count: int) -> int:
    """§2.5-4 成本护栏:深档单篇检索次数上限 = 白名单家数 × 2 + 4(主题项)。"""
    return max(0, int(entity_count or 0)) * DEEP_TIER_QUERIES_PER_ENTITY + DEEP_TIER_TOPIC_QUERIES


def deep_tier_research_probe(style_code: str | None, topic: dict | None) -> bool:
    """[工单 C-2 T2 根因1] 深档逐家检索的**研究前**判定。

    旧判定只看研究前篇幅合同的 target —— 但 plan 的深档门要求 verified>=3,而
    verified 证据恰恰要靠本次研究才会有;研究的触发条件又是 items<3。三者叠加,
    生产上研究前 plan 恒 3500/5500(本机复现:candidates=9、无 pack → target 3500,
    reasons 含 few_verified_candidates_no_padding)→ 深档预判恒 False →
    **逐家定向检索一次都没发出过**(22 次护栏零使用)→ 竞品卡零证据可标。

    修正:候选容量轴在研究前就已确定 —— name_verified 候选(含客户品牌)达到
    RANKING_DEEP_RESEARCH_MIN_CANDIDATES(=3,对齐 plan 榜单族 `candidates<=2→紧凑`
    的同一条界)就值得做逐家检索;plan 已进深档的(pack 预先存在且证据充足)照旧。
    非榜单族一律 False:紧凑档一次新增调用都没有,§2.5 承诺不变。
    """
    try:
        from writing.article_length_contract import (
            RANKING_DEEP_RESEARCH_MIN_CANDIDATES,
            RANKING_FAMILY_MINIMUM_CHARS,
            build_length_plan_for_topic,
            count_verified_candidates,
        )
        from writing.article_style_contract import family_for_style

        if family_for_style(style_code) != "multi_brand_comparison":
            return False
        data = topic if isinstance(topic, dict) else {}
        plan = build_length_plan_for_topic(style_code, data)
        if int(plan.get("target_chars") or 0) >= RANKING_FAMILY_MINIMUM_CHARS:
            return True
        return count_verified_candidates(data) >= RANKING_DEEP_RESEARCH_MIN_CANDIDATES
    except Exception as err:
        print(f"    ⚠️ [证据供给] 深档预判失败,按紧凑档检索: {err}")
        return False


def evidence_pack_needs_research(pack: Any, *, deep_tier: bool, min_items: int = 3) -> bool:
    """[工单 C-2 T2] 研究触发判定:除"条目不足"外,深档篇目还要求 pack 带逐家覆盖。

    旧判定只看 items<3 —— 一个由主题式检索攒出来的 3+ 条旧 pack 会让深档篇目
    跳过研究,逐家 query 又一次不发。深档口径:pack 没有 evidence_supply.deep_tier
    标记(= 不是逐家检索产出)就仍需研究;紧凑档行为逐字不变。
    """
    if not isinstance(pack, dict):
        return True
    from writing.evidence_pack import raw_pack_items as _raw_items

    if len(_raw_items(pack)) < max(0, int(min_items)):
        return True
    if deep_tier:
        supply = pack.get("evidence_supply")
        if not (isinstance(supply, dict) and supply.get("deep_tier")):
            return True
    return False


def _interleave_discovered_by_lane(
    discovered: list[tuple[str, dict[str, Any], str, str]],
    cap: int,
) -> list[tuple[str, dict[str, Any], str, str]]:
    """[工单 C-2 T2 根因2] 深档来源截断改按 lane 公平轮转。

    旧 `discovered[:source_cap]` 是头部截断:主题 lane 排在前(4×5=20 条先占位),
    9 家实体时 source_cap=35 只剩 15 个名额 → 竞品第 2 家往后的检索结果**一条都
    进不了 items**,更谈不上核验 —— 检索费花了,产出全被切掉。
    改法:按 lane(主题按 query、实体按家)分组,逐轮各取 1 条直到 cap,
    每家都有公平配额;组内与组间顺序保持确定性,不引入随机。
    """
    groups: list[list[tuple[str, dict[str, Any], str, str]]] = []
    index: dict[str, int] = {}
    for tup in discovered:
        key = tup[3] or f"topic::{tup[2]}"
        if key not in index:
            index[key] = len(groups)
            groups.append([])
        groups[index[key]].append(tup)
    out: list[tuple[str, dict[str, Any], str, str]] = []
    cap = max(0, int(cap))
    round_idx = 0
    while len(out) < cap:
        advanced = False
        for bucket in groups:
            if round_idx < len(bucket):
                out.append(bucket[round_idx])
                advanced = True
                if len(out) >= cap:
                    break
        if not advanced:
            break
        round_idx += 1
    return out


def corpus_lead_terms(industry: str, *, limit: int = 6) -> list[str]:
    """从飞轮语料标题里取"事实线索"词 —— **只决定去搜什么,永远不作为事实**。

    §2.5-2:`geo_research_articles` 的行业正文按 JC 合同只作"事实线索 + 结构参考",
    线索必须经 Evidence 核验才能写成事实,不得直接引用。这里因此**只读标题**,
    产出的是检索角度词,它们会和其它 query 一样走完整的检索 + 核验门。

    🔴 实测(生产只读 2026-07-29):`geo_research_articles` 16103 篇 **corpus_grade
    全部是 JC0**,所以 `_load_jc3_body`(要求 JC3+)命中率恒 0 —— 正文复用那条路
    今天是死的,归 R1 工单处理,本函数刻意不碰正文,只碰标题。
    """
    industry = str(industry or "").strip()
    if not industry:
        return []
    conn = None
    try:
        from db.diagnosis_db import get_connection

        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT title FROM geo_research_articles
            WHERE title IS NOT NULL AND length(title) BETWEEN 6 AND 60
              AND (primary_industry = %s OR title ILIKE %s)
            ORDER BY id DESC LIMIT 200
            """,
            (industry, f"%{industry}%"),
        )
        rows = cur.fetchall() or []
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    counter: dict[str, int] = {}
    for row in rows:
        title = str((row or {}).get("title") or "")
        for term in _LEAD_ANGLE_TERMS:
            if term in title:
                counter[term] = counter.get(term, 0) + 1
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [term for term, _ in ranked[:limit]]


#: 候选角度词表 —— 这些是"一家公司身上可核验的事实面",不是形容词。
_LEAD_ANGLE_TERMS: Final[tuple[str, ...]] = (
    "资质", "案例", "报价", "交付周期", "工厂", "产能", "质保",
    "认证", "标准", "售后", "服务范围", "团队", "获奖", "融资",
)
_DEFAULT_LEAD_ANGLES: Final[tuple[str, ...]] = ("案例", "资质", "报道")


def build_entity_lanes(
    entities: Sequence[str],
    *,
    keyword: str,
    industry: str,
    lead_terms: Sequence[str] | None = None,
    article_terms: str = "",
) -> list[tuple[str, str, str]]:
    """为白名单每家实体生成定向 query,返回 ``(relationship, query, entity)``。

    第 1 条打官方事实面(官网/简介/资质),第 2 条打公开案例与权威媒体报道 ——
    角度词优先用飞轮语料线索,没有线索就用固定角度。目标每家 ≥2 条可核验事实。

    [P0-1] `article_terms` = 本篇标题意图词:注入第 2 条(案例/报道面),让同一
    quote 下不同篇目的 entity-lane query **必不同** —— 证据检索对准本篇问题,
    而不是 34 篇共用 6 套 query。第 1 条(官方事实面)刻意保持 quote 级:
    官网/资质与本篇问题无关,它正是跨篇缓存复用的正当对象。
    """
    angles = [str(t).strip() for t in (lead_terms or []) if str(t or "").strip()]
    if not angles:
        angles = list(_DEFAULT_LEAD_ANGLES)
    terms = str(article_terms or "").strip()
    lanes: list[tuple[str, str, str]] = []
    for index, raw in enumerate(entities or []):
        name = str(raw or "").strip()
        if not name:
            continue
        angle = angles[index % len(angles)]
        # 角度词与固定词重复时留空,避免拼出"案例 案例 报道"这种降低召回的重复 query。
        angle_part = "" if angle in {"案例", "报道", "数据"} else f"{angle} "
        lanes.append(("support", _join_q(name, "官网 简介 资质", industry), name))
        lanes.append(("support", _join_q(name, keyword, terms, f"{angle_part}案例 报道 数据"), name))
    return lanes


async def collect_evidence_pack(
    *,
    title: str,
    keyword: str,
    industry: str,
    client_brand: str,
    competitor_names: list[str] | None = None,
    request_id: str = "",
    force: bool = False,
    deep_tier: bool = False,
    whitelist_names: Sequence[str] | None = None,
    advantage_hints: Sequence[str] | None = None,
    cache_scope: str = "",
) -> dict[str, Any]:
    """Build a neutral Evidence Pack without sending private materials in queries.

    Args:
        advantage_hints: [D6-A 2026-08-10] 主优势定向增援 query(优势方向词 +
            关键词,**不含客户单方材料原文** —— 由 `primary_advantage.
            advantage_search_hints` 产出并守住隐私边界)。作为 support lane
            追加,受同一套 query/source 预算约束;空或缺省 = 行为与旧签名
            完全一致。搜索失败沿用本函数既有兜底,不阻断主链。
        cache_scope: [P0-1 2026-08-14] 检索复用作用域(调用方传 quote_id)。
            同 scope 内**逐字相同**的 query 只发一次 provider 调用,结果复用
            (QZQZ 形态 95.5% 字节级重复调用的根治);不同 scope 绝不互用。
            空串 = 不缓存,行为与旧签名完全一致。
    """
    if not (force or is_automatic_research_enabled()):
        return normalize_evidence_pack({
            "request_id": request_id,
            "research_status": "feature_disabled",
            "limitations": [
                "自动联网研究默认关闭；启用前需完成成本、隐私和来源核验 pilot。",
                "客户材料仍可作为 customer_provided 使用，但不能冒充独立证据。",
            ],
        }, request_id=request_id)

    query_cap = _cap("GEO_ARTICLE_EVIDENCE_QUERY_CAP", 4, 8)
    source_cap = _cap("GEO_ARTICLE_EVIDENCE_SOURCE_CAP", 8, 20)
    per_query = _cap("GEO_ARTICLE_EVIDENCE_RESULTS_PER_QUERY", 5, 10)
    competitors = [str(name).strip() for name in (competitor_names or []) if str(name).strip()][:5]
    comparison_names = " ".join([client_brand, *competitors]).strip()
    # [D11 · SSOT v2.1 ③] 搜索方向 = **增援客户**,不是核查客户。
    #   旧 lanes 的第二条是一条纯核查道(去搜客户的负面/监管问题),搜回来的天然是
    #   反面材料,写进正文就是给客户贴脸检查 —— 这正是生产客诉的来源之一。
    #   新方向:优先搜**佐证客户主张**的公开资料,按可引用价值排序:
    #     1) 学术论文/研究(最高可信度,读者最认)
    #     2) 行业报告/权威媒体/政府机构公开资料
    #     3) 客户官方与同类产品的公开参数
    #   风险面不再单开一条搜索去"找茬";真实存在的限制若出现在上述来源里,
    #   仍会被正常收录(不屏蔽负面事实),但不再主动构造核查型 query。
    # 🔴 [P0-1 2026-08-14] 第 1 条 lane 注入**文章级**意图词(标题去掉 quote 级
    # 变量后的剩余)—— 研究定稿根因 A:五条 lane 全 quote 级、title 形参从未进
    # query,证据链与本篇问题整体脱钩(QZQZ 34 篇仅 6 套 query)。
    # 其余 4 条刻意保持 quote 级:行业背景/官方资质与本篇问题无关,它们是
    # 跨篇缓存复用的正当对象(见 cache_scope)。意图词为空时第 1 条退回原形态。
    _art_terms = _article_terms(title, keyword=keyword, brand=client_brand)
    _topic_lanes: list[tuple[str, str, str]] = [
        ("support", _join_q(keyword, _art_terms, industry, "研究 论文 数据 报告"), ""),
        ("support", f"{industry} {keyword} 行业报告 白皮书 权威 数据", ""),
        ("support", f"{client_brand} {keyword} 官方 产品 参数 资质", ""),
        ("background", f"{industry} {keyword} 标准 规范 指南", ""),
        ("background", f"{comparison_names} {keyword} 参数 适用场景", ""),
    ]
    # 🔴 [返工 R3 2026-08-11] 主优势定向增援 lane **单列预算(+2),在截断之后追加**。
    # 上一版把它 append 进 `_topic_lanes` 第 6/7 位,而深档切片 `[:DEEP_TIER_TOPIC_QUERIES]`
    # 硬常量 4、紧凑档默认 cap 4 → **结构性永不执行**,lineage 却照记"已增援" ——
    # Review 记档为「接线没接」第八例(新变体:接了线,预算掐死)。
    # 现口径:基础 lanes 截断规则一字不动(基础预算不变);增援 lane 固定 +2,
    # 在截断点之后 extend,默认配置下必然发出。成本账:仅证据不足触发增援研究的
    # 篇目,每篇至多 +2 次检索(与候选缺素材数相关,常为 0-2)。
    _hint_lanes: list[tuple[str, str, str]] = []
    _hint_query_texts: set[str] = set()
    for _hint in (advantage_hints or [])[:2]:
        _hint_text = str(_hint or "").strip()
        if _hint_text:
            _q = f"{client_brand} {_hint_text}".strip()
            _hint_lanes.append(("support", _q, client_brand))
            _hint_query_texts.add(_q)

    # [工单 C · §2.5] 深档:白名单逐家定向检索。
    #   · 实体 = 白名单里的**竞品**(客户侧继续吃 Brand Fact Snapshot + 知识库);
    #   · 逐家 2 条 query,主题项固定 4 条 —— 正好等于成本护栏 家数×2+4;
    #   · 第 5 条"合并名称"主题 lane 被逐家 lane 取代(它本来就是逐家的劣化版)。
    # [工单 C 复审返工 ② · §2.5 客户侧素材自适应] **客户品牌也是检索对象**。
    #   生产实证:6 个活跃品牌里 5 家 brand_fact_snapshot 只有 1556-2374 字符,
    #   光靠知识库喂不满 1500 字客户卡。公开可核验信息(官网/媒体报道/公开案例)
    #   由 AI 补齐进 Evidence Pack;知识库继续是**独有信息**(内部数据/未公开案例)
    #   的唯一来源 —— 两路合并供给客户卡,而不是二选一。
    #   客户放在实体列表**首位**,保证 query 预算优先覆盖它。
    _entities: list[str] = []
    if deep_tier:
        _brand = str(client_brand or "").strip()
        _seen_entity: set[str] = set()
        if _brand:
            _seen_entity.add(_brand)
            _entities.append(_brand)
        for raw in (whitelist_names or competitor_names or []):
            name = str(raw or "").strip()
            if not name or name in _seen_entity:
                continue
            _seen_entity.add(name)
            _entities.append(name)

    if deep_tier and _entities:
        query_cap = deep_tier_query_budget(len(_entities))
        source_cap = _cap(
            "GEO_ARTICLE_EVIDENCE_DEEP_SOURCE_CAP",
            min(60, len(_entities) * 3 + 8),
            80,
        )
        lanes = _topic_lanes[:DEEP_TIER_TOPIC_QUERIES] + build_entity_lanes(
            _entities,
            keyword=keyword,
            industry=industry,
            lead_terms=corpus_lead_terms(industry),
            # [P0-1] 逐家 lane 第 2 条(案例/报道面)带上本篇意图词 —— 同 quote
            # 两 topic 的 entity-lane query 由此必不同(判别测试 ①)。
            article_terms=_art_terms,
        )
        lanes = lanes[:query_cap]
    else:
        lanes = _topic_lanes[:query_cap]
    # 🔴 [返工 R3] 增援 lane 在**截断之后**接上 —— 单列 +2 预算,深档/紧凑档
    # 默认配置下都真的发出去(接线锁断言 hint 落在 pack 实际执行的 queries 里)。
    if _hint_lanes:
        lanes = list(lanes) + _hint_lanes

    from tools.search import provider_router
    from tools.search.metaso_mcp import metaso_web_reader

    discovered: list[tuple[str, dict[str, Any], str, str]] = []
    queries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for relationship, query, entity in lanes:
        lane_kind = "entity" if entity else "topic"
        try:
            # [P0-1/R2-3 2026-08-15] 同 quote 检索复用 + single-flight 并发去重:
            # 相同 (scope, query) 无论串行还是并发都只发一次 provider 调用
            # (批量写作是真并发,R2 反例 provider_calls=8 的根治)。
            # scope 为空 = 不缓存不合并,与旧行为逐字节一致。
            async def _fetch_citations(_q: str = query) -> list[dict[str, Any]]:
                # [Provider 层 2026-07-28] 引用形检索走场景键路由(evidence 是灰度第一批):
                # 默认配置原样委托 metaso_search_with_citations,行为与改前逐字节一致;
                # 灰度开豆包后同结构返回,且 citation 可能带 raw_content(自带正文)。
                result = await provider_router.citation_search(
                    _q, size=per_query, scenario="evidence",
                )
                return list(result.get("citations") or [])

            citations, _cit_hit = await _single_flight_fetch(
                cache_scope, "citation", query, _fetch_citations,
            )
            queries.append({
                "query": query, "relationship": relationship,
                "result_count": len(citations),
                "lane_kind": lane_kind, "entity": entity,
                "cache_hit": _cit_hit,
                # [R3] 增援 lane 打标:lineage 只记**实际执行**的增援,凭这个标回查
                "advantage_hint": query in _hint_query_texts,
            })
            for citation in citations:
                url = str(citation.get("url") or "").strip()
                if url and url not in seen and _public_http_url(url):
                    seen.add(url)
                    discovered.append((relationship, citation, query, entity))
        except Exception as exc:
            queries.append({
                "query": query, "relationship": relationship,
                "error": type(exc).__name__,
                "lane_kind": lane_kind, "entity": entity,
                "advantage_hint": query in _hint_query_texts,
            })

    # [工单 C-2 T2 根因2] 深档按 lane 轮转选源(每家公平配额);紧凑档保持原头部截断。
    if deep_tier and _entities:
        selected_sources = _interleave_discovered_by_lane(discovered, source_cap)
    else:
        selected_sources = discovered[:source_cap]

    items: list[dict[str, Any]] = []
    verification_candidates: list[dict[str, Any]] = []
    failures = 0
    for relationship, citation, query, entity in selected_sources:
        url = str(citation.get("url") or "")
        body, provenance = _load_jc3_body(url)
        if not body:
            # [Provider 层 2026-07-28] 豆包路径的 citation 自带正文(NeedContent 内联),
            # 直接用作 body,免掉一次 reader 调用(¥0.05/条 → 0)。秘塔路径的 citation
            # 没有 raw_content 键 → 走下方原 reader 路,默认行为零变化。
            inline_body = str(citation.get("raw_content") or "").strip()
            if len(inline_body) >= 300:
                from services.research_monitor.corpus_contract import fingerprint_clean_body

                fingerprint = fingerprint_clean_body(inline_body)
                body = inline_body
                provenance = {
                    "verification_status": "body_retrieved_claim_unverified",
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "canonical_body_hash": fingerprint.canonical_body_hash or None,
                    "body_hash_algorithm": fingerprint.body_hash_algorithm,
                    "body_boundary_version": "provider-inline-content-v1",
                    "fetch_event_id": None,
                }
        if not body:
            try:
                response = await metaso_web_reader(url, format="markdown")
                payload = _response_payload(response)
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                body = _body_text(payload.get("content"))
                from services.research_monitor.corpus_contract import fingerprint_clean_body

                fingerprint = fingerprint_clean_body(body)
                provenance = {
                    "verification_status": (
                        "body_retrieved_claim_unverified" if len(body) >= 300
                        else "search_result_only"
                    ),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "canonical_body_hash": fingerprint.canonical_body_hash or None,
                    "body_hash_algorithm": fingerprint.body_hash_algorithm,
                    "body_boundary_version": fingerprint.body_boundary_version,
                    "fetch_event_id": None,
                }
            except Exception:
                failures += 1
                body = ""
                provenance = {"verification_status": "search_result_only"}
        excerpt = re.sub(r"\s+", " ", body).strip()[:800] if body else str(citation.get("snippet") or "")[:800]
        evidence_id = f"EV-{len(items) + 1:03d}"
        item = {
            "evidence_id": evidence_id,
            "claim": _claim_from(citation.get("title"), citation.get("snippet"), relationship),
            "url": url,
            "title": citation.get("title") or "",
            "publisher": citation.get("source") or (urlparse(url).hostname or ""),
            "published_at": citation.get("date") or None,
            "fetched_at": provenance.get("fetched_at"),
            "source_tier": _source_tier(url),
            "relationship": relationship,
            "excerpt": excerpt,
            # [工单 C-2 T2] 实体归属随条目落库:逐家 lane 检索到的证据标上是哪家的,
            # 下游按品牌分组喂进竞品卡(evidence_pack.render_evidence_pack_by_entity)。
            "entity": entity or None,
            "scope": f"搜索问题：{query}",
            "conflict_status": "needs_human_resolution" if relationship == "refute" else "none",
            "verification_status": provenance.get("verification_status"),
            "fetch_event_id": provenance.get("fetch_event_id"),
            "canonical_body_hash": provenance.get("canonical_body_hash"),
            "body_hash_algorithm": provenance.get("body_hash_algorithm"),
            "body_boundary_version": provenance.get("body_boundary_version"),
            "span": {"type": "excerpt", "start": 0, "end": len(excerpt)},
        }
        # [P0-2 2026-08-14] 逐家 lane 条目落绑定三态(确定性域名/名称判别,禁 LLM)。
        # 只有真带实体归属的条目才有这个键 —— 主题 lane 不凭空造字段
        # (与 standard_document_class 同一条 manifest_hash 纪律)。
        if entity:
            item["binding_state"] = entity_binding_state(item, entity)
        items.append(item)
        if body and len(body) >= 300:
            verification_candidates.append({
                "evidence_id": evidence_id,
                "title": item["title"],
                "relationship": relationship,
                "body": body,
            })

    # ------------------------------------------------------------------
    # [T2 Scholar 学术层 2026-07-28] 闲置的秘塔 scholar 接进现役 Evidence 管道。
    # 主题型 query 追加 ≤2 次学术检索(¥0.05/次,B 级秘塔直连,不进 provider 路由);
    # 🔴 响应字段是 scholars 不是 webpages(解析在 scholar_evidence_search,错键得全零);
    # 只放行可查证条目(authors+date 齐 或 万方/doi.org 链接,实测通过率 87-100%),
    # 以学术层级(source_tier=scholar_citation)进 pack —— 写作模板本就鼓励
    # "XX 研究显示"式引用,条目带作者/年份自然被消费,写作侧零模板改动。
    # ------------------------------------------------------------------
    # 只对深档生效:既有判别锁「紧凑档零新增调用」(工单 C)不许回退,
    # 且本单 §5.6 锁口径就是"深档研究计划含学术层 query"。
    scholar_query_cap = _cap("GEO_ARTICLE_EVIDENCE_SCHOLAR_QUERIES", 2, 2)
    scholar_item_count = 0
    if deep_tier and scholar_query_cap > 0:
        scholar_queries = [
            f"{keyword} {industry} 研究",
            f"{industry} {keyword} 标准 数据",
        ][:scholar_query_cap]
        scholar_seen: set[str] = set(seen)
        for scholar_query in scholar_queries:
            try:
                # [P0-1/R2-3] scholar query 是 quote 级,同 quote 跨篇 100% 重复 ——
                # 同一套 single-flight(kind 前缀隔离结果形态,并发同样只付一次)。
                async def _fetch_scholar(_q: str = scholar_query) -> list[dict[str, Any]]:
                    return list(await provider_router.scholar_evidence_search(_q, size=8) or [])

                entries, _sch_hit = await _single_flight_fetch(
                    cache_scope, "scholar", scholar_query, _fetch_scholar,
                )
                queries.append({
                    "query": scholar_query, "relationship": "support",
                    "result_count": len(entries),
                    "lane_kind": "scholar", "entity": "",
                    "cache_hit": _sch_hit,
                })
            except Exception as exc:
                queries.append({
                    "query": scholar_query, "relationship": "support",
                    "error": type(exc).__name__,
                    "lane_kind": "scholar", "entity": "",
                })
                continue
            for entry in entries:
                link = str(entry.get("link") or "")
                if not link or link in scholar_seen or not _public_http_url(link):
                    continue
                if scholar_item_count >= 6:
                    break
                scholar_seen.add(link)
                authors = "、".join(entry.get("authors") or [])
                year = provider_router.scholar_entry_year(entry)
                byline = "，".join(filter(None, [authors, f"{year}年" if year else ""]))
                items.append({
                    "evidence_id": f"EV-{len(items) + 1:03d}",
                    "claim": (
                        f"学术文献《{entry['title']}》"
                        + (f"（{byline}）" if byline else "")
                        + "可为本主题提供研究依据"
                    ),
                    "url": link,
                    "title": entry.get("title") or "",
                    "publisher": f"学术文献·{authors}" if authors else "学术文献",
                    "published_at": entry.get("date") or None,
                    "fetched_at": None,
                    "source_tier": "scholar_citation",
                    "relationship": "support",
                    "excerpt": str(entry.get("snippet") or "")[:800],
                    "scope": f"学术检索：{scholar_query}；可查证性已核（作者/年份或万方/DOI 链接）。",
                    "conflict_status": "none",
                    "verification_status": "search_result_only",
                    "fetch_event_id": None,
                    "canonical_body_hash": None,
                    "body_hash_algorithm": None,
                    "body_boundary_version": None,
                    "span": {"type": "excerpt", "start": 0, "end": len(str(entry.get("snippet") or "")[:800])},
                })
                scholar_item_count += 1

    # ------------------------------------------------------------------
    # [标准类证据 lane 2026-07-29 · 工单 §2/§3] 秘塔 document 文库 → 标题级存在性锚点。
    #
    # Owner 裁定:标准类证据"有标题就行",不要求正文 —— 但**放行只对权威域名生效**,
    # 且本 lane 只收 `.gov.cn`(不含 `.gov`)。判定见 `standard_lane_admits`。
    #
    # 🔴 §3.1 照 scholar 块的范式:`source_tier="standard_citation"`,
    #    `verification_status` 保持 `"search_result_only"`,**不进 verification_candidates**
    #    (免正文条目本来就没有正文可核验) —— 因此**不参与 `verified` 计数**,
    #    不影响深档 `verified >= 3` 的门槛口径,543-548 那段一个字没动。
    #
    # 🔴 只对深档生效:既有判别锁「紧凑档零新增调用」(工单 C / T2 scholar)不许回退。
    #    单次 document 检索 3 credits。
    # ------------------------------------------------------------------
    standard_query_cap = _cap("GEO_ARTICLE_EVIDENCE_STANDARD_QUERIES", 2, 2)
    standard_item_count = 0
    if deep_tier and standard_query_cap > 0:
        standard_queries = [
            f"{keyword} {industry} 标准 规范",
            f"{industry} 验收标准 技术规程",
        ][:standard_query_cap]
        standard_seen: set[str] = set(seen)
        for standard_query in standard_queries:
            try:
                # [P0-1/R2-3] standard query 同为 quote 级,同一套 single-flight。
                async def _fetch_standard(_q: str = standard_query) -> list[dict[str, Any]]:
                    return list(await provider_router.document_evidence_search(_q, size=10) or [])

                entries, _std_hit = await _single_flight_fetch(
                    cache_scope, "standard", standard_query, _fetch_standard,
                )
                queries.append({
                    "query": standard_query, "relationship": "background",
                    "result_count": len(entries),
                    "lane_kind": "standard", "entity": "",
                    "cache_hit": _std_hit,
                })
            except Exception as exc:
                queries.append({
                    "query": standard_query, "relationship": "background",
                    "error": type(exc).__name__,
                    "lane_kind": "standard", "entity": "",
                })
                continue
            for entry in entries:
                # 🔴 放行闸:两条必要条件都过才收。非 .gov.cn / 非本 lane 一律丢。
                if not standard_lane_admits(entry):
                    continue
                link = str(entry.get("link") or entry.get("url") or "")
                if not link or link in standard_seen or not _public_http_url(link):
                    continue
                if standard_item_count >= 6:
                    break
                standard_seen.add(link)
                doc_title = str(entry.get("title") or "").strip()
                doc_class = classify_standard_document(doc_title)
                issuer = "、".join(entry.get("authors") or [])
                excerpt = str(entry.get("snippet") or "")[:800]
                items.append({
                    "evidence_id": f"EV-{len(items) + 1:03d}",
                    # 🔴 claim 只做**存在性**表述。标题级证据不知道正文写了什么,
                    #    claim 里绝不能出现任何条款/数值口径。
                    "claim": (
                        f"《{doc_title}》"
                        + (f"(发布单位：{issuer})" if issuer else "")
                        + "为该主题下公开可查的"
                        + ("标准/规范文件" if doc_class == STANDARD_CLASS_STANDARD else "地方性行政文件")
                        + "，可作存在性引用"
                    ),
                    "url": link,
                    "title": doc_title,
                    "publisher": issuer or (entry.get("authorityDomain") or urlparse(link).hostname or ""),
                    "published_at": entry.get("date") or None,
                    "fetched_at": None,
                    "source_tier": "standard_citation",
                    "standard_document_class": doc_class,
                    "relationship": "background",
                    "excerpt": excerpt,
                    "scope": (
                        f"标准文库检索：{standard_query}；权威域名已核(.gov.cn)。"
                        "⚠️ 标题级证据：只能做存在性引用，"
                        "具体条款/数值/比例必须另有正文来源才能写。"
                    ),
                    "conflict_status": "none",
                    "verification_status": "search_result_only",
                    "fetch_event_id": None,
                    "canonical_body_hash": None,
                    "body_hash_algorithm": None,
                    "body_boundary_version": None,
                    "span": {"type": "excerpt", "start": 0, "end": len(excerpt)},
                })
                standard_item_count += 1

    if verification_candidates:
        try:
            from writing.evidence_verifier import select_and_verify_claim_spans

            verified_spans = await select_and_verify_claim_spans(
                verification_candidates,
                target_question=f"{title}；{keyword}",
                max_body_chars=_cap("GEO_ARTICLE_EVIDENCE_VERIFY_BODY_CHARS", 6000, 12000),
            )
            for item in items:
                verified = verified_spans.get(item["evidence_id"])
                if verified:
                    item.update(verified)
                    item["scope"] = (
                        f"{item.get('scope') or ''}；精确片段核验仅证明该来源如此表述，"
                        "结论强度不得超过来源本身。"
                    )
        except Exception:
            # The article remains safely unverified and must enter human review.
            failures += len(verification_candidates)

    verified = sum(
        1 for item in items
        if item["verification_status"] in {"claim_span_verified", "official_record", "human_verified"}
    )
    # [工单 C · §2.5-3] 核验门产出率复测的埋点。
    #   工单 A §4 已查明瓶颈在 LLM selections 环节(8 items → 2.4 verified,
    #   5/20 次空选择)。这里把"逐家检索之后每篇实际拿到几条已核验"落进 pack,
    #   部署后可以直接用 SQL 统计,不必再靠人肉读正文。
    per_entity_verified: dict[str, int] = {}
    for item in items:
        if item["verification_status"] not in {
            "claim_span_verified", "official_record", "human_verified",
        }:
            continue
        # [P0-2/R2-1] 实体证据供给计数只认 entity_confirmed ——
        # 未确认主体的"已核验"会把供给统计撑成假的(H0-ENTITY-BINDING 隔离面之一)。
        if item.get("entity") and item.get("binding_state") != BINDING_CONFIRMED:
            continue
        # [工单 C-2 T2] 优先用条目自带的实体归属(逐家 lane 直标);老条目退回 scope 匹配。
        item_entity = str(item.get("entity") or "")
        if item_entity:
            per_entity_verified[item_entity] = per_entity_verified.get(item_entity, 0) + 1
            continue
        scope = str(item.get("scope") or "")
        for entity in _entities:
            if entity and entity in scope:
                per_entity_verified[entity] = per_entity_verified.get(entity, 0) + 1
    supply = {
        "deep_tier": bool(deep_tier and _entities),
        "entity_count": len(_entities),
        "query_budget": deep_tier_query_budget(len(_entities)) if (deep_tier and _entities) else query_cap,
        "queries_issued": len(queries),
        "entity_queries_issued": sum(1 for q in queries if q.get("lane_kind") == "entity"),
        "entities_covered": sorted({q.get("entity") for q in queries if q.get("entity")}),
        "verified_count": verified,
        "item_count": len(items),
        "per_entity_verified": per_entity_verified,
        "entities_with_two_or_more": sum(1 for n in per_entity_verified.values() if n >= 2),
        # [T2 Scholar 2026-07-28] 学术层供给埋点(灰度对账三线之一:条目数)。
        "scholar_queries_issued": sum(1 for q in queries if q.get("lane_kind") == "scholar"),
        "scholar_item_count": scholar_item_count,
        # [标准类 lane 2026-07-29] 供给埋点。**验收不看条数**(§6:秘塔结果跨时段会漂移),
        # 这里只作部署后 SQL 复测用;规则是否正确执行才是验收口径。
        "standard_queries_issued": sum(1 for q in queries if q.get("lane_kind") == "standard"),
        "standard_item_count": standard_item_count,
    }
    return normalize_evidence_pack({
        "request_id": request_id,
        "research_status": "ready" if verified else "verification_insufficient",
        "queries": queries,
        "items": items,
        "planner_version": RESEARCH_PLANNER_VERSION,
        "evidence_supply": supply,
        "limitations": [
            f"具体主张核验 {verified}/{len(items)}；抓到全文不等于主张成立，搜索摘要不得用于事实。",
            f"读取失败或不足 {failures} 个来源；Evidence Pack 受 query/source cap 限制。",
            "来源级别为确定性初筛，发布前仍需 Evidence Verifier 或人工确认具体 claim 与适用范围。",
        ],
    }, request_id=request_id)
