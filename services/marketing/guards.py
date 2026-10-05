"""
services/marketing/guards.py — 禁承诺守卫 + 违禁词/广告法/竞品清洗(双层)

总设计 §1 硬约束:军师永不生成"保证/承诺/必上/包上榜/100%/几乎每次"类语言;
出现率只用 50/65/75 + "问 N 次出现 M 次"口径;达标倒计时只描述为"系统自动测量"。
双保险(工程 §B.3 / §D.2):
  ① prompt 约束(PROMISE_GUARD_PROMPT 注入 system prompt)
  ② 出口违禁词过滤器(scan_forbidden 在文案落库/出图前再拦一道)

治理对齐(SSOT GEO_COMMERCIAL_INTENT_GOVERNANCE_SSOT_2026-07-23,Owner 签发):
  - 硬拦目录只有法律明令项,全部在 config/legal_prohibited_pack.json(版本化、
    Owner 签发、热加载);Builder/模型/审核器不得临时解释法律并扩大禁区。
  - 包缺失/损坏 → fail-open for content(2026-07-23 外部审查 P1-5 修正):
    未签发目录无权硬拦,扫描只产出 advisory flag(unsigned_catalog_advisory)
    + 告警日志给人;内嵌最小集合仅作提醒参照,绝不进 errors/422/blocked。
  - 违法词子串误判修正:「打击/识别/防范/抵制/严查/拒绝/远离/反+假冒」这类
    执法/风控正面表述不算命中;裸「假冒/假货」仍硬拦。
  - 双重否定还原(2026-07-23 Deploy):「不反对/不打击/不是不+违法词」是否定
    套否定,仍算命中硬拦;「杜绝/严禁+违法词」本就不含否定前缀,同样硬拦。
  - 「唯一」「领先」「专业」「专注」等非明令灰词不进目录,只走 advisory 提醒层
    (GRAY_ADVISORY_WORDS → flags["gray_advisory"],任何路径不得因此硬拦)。
  - PROMISE_WORDS 整体留在 warning 层(内部红线,提醒不阻断,不进硬拦)。
  - scan_forbidden()["passed"] 只反映硬拦类(广告法极限词/违法内容/裸指标/术语
    红线/竞品);promise/gray_advisory 命中不影响 passed,由调用方落 warnings。

本模块被军师建议(touch_copy)与物料工厂(input_fields)共用。零外部依赖、纯函数、可单测。
"""
import json
import logging
import os
import re
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 法律禁止目录(Owner 签发版本化包 · 热加载 · fail-closed)
# ---------------------------------------------------------------------------
PACK_ID = "legal_prohibited"
PACK_VERSION = "1.0.0"  # 本代码随附的包版本(config 文件声明值应与之对齐)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "legal_prohibited_pack.json",
)

# 包缺失/损坏时的内嵌最小 statutory 集合(只放明令项,不扩大)。
# 注意:内嵌集合未经 Owner 签发,按 SSOT 无权硬拦——仅作 advisory 提醒参照。
_EMBEDDED_AD_LAW = ["国家级", "最高级", "最佳", "最好", "第一", "顶级", "首个", "首选"]
_EMBEDDED_ILLEGAL = ["赌博", "毒品", "假冒"]

# —— 灰词 advisory 层(SSOT:非法律明令词不进禁止目录,提醒不硬拦)——
# 从原硬编码 AD_LAW_WORDS 降下的非明令词:措辞偏强只提醒,任何路径不得因此 422/blocked。
GRAY_ADVISORY_WORDS = [
    "唯一", "独一无二", "领先", "领导品牌", "王牌", "垄断", "极致",
]

# —— 承诺类(总设计红线:不做任何承诺;SSOT:整体留 warning 层,不进硬拦)——
PROMISE_WORDS = [
    "保证", "承诺", "必上", "包上榜", "包上", "包排名", "稳上", "稳定上榜",
    "一定能", "一定上", "绝对", "百分百", "100%", "百分之百", "几乎每次",
    "必然", "包过", "包见效", "无效退款", "保证排名", "保证上榜", "确保上榜",
    "躺赢", "轻松上榜", "秒上", "立刻上榜",
]

# —— 广告法极限词(加载结果兼容别名;运行时扫描走 legal_pack() 热加载)——
# 见下方 legal_pack() 初始化后赋值。

# —— 裸露技术指标(面向非代理必须走出现率话术层,禁裸 SOV/成本/d)——
NAKED_METRIC_PATTERNS = [
    r"SOV", r"share\s*of\s*voice", r"出现率\s*\d+%",  # 直接裸百分比
    r"effective_competition", r"target_share",
]

# 注入 LLM system prompt 的约束文本(第①层)
PROMISE_GUARD_PROMPT = (
    "【禁承诺守卫·硬约束】你是 OmniRank 营销军师,平台只卖算力。严格禁止:\n"
    "1. 任何'保证/承诺/必上/包上榜/100%/几乎每次/稳上/一定/绝对/包排名'类承诺性语言;\n"
    "2. 广告法极限词'最/第一/顶级/国家级'等(以 Owner 签发的法律禁止目录包为准);\n"
    "3. 裸露 SOV/成本/技术百分比 —— 出现率只用'问 N 次约出现 M 次'口语话术;\n"
    "4. '累计达标30天'只能描述为'系统自动测量',绝不描述为'我们承诺';\n"
    "5. 只引用既有事实做风险反转('做成才扣·失败自动退'),不得造新承诺。\n"
    "全部对客文案用'算力',禁用'积分'。竞品名一律清洗不出现。"
)

# advisory 提醒层 flag 类别(SSOT §5.1:未命中法律禁止项 → advisory,不得硬拦)。
# unsigned_catalog_advisory(2026-07-23 外部审查 P1-5):法律目录包缺失/损坏时
# 内嵌未签发集合的命中只落这一类——未签发目录无权硬拦,告警给人。
UNSIGNED_CATALOG_FLAG = "unsigned_catalog_advisory"
ADVISORY_FLAG_KINDS = ("promise", "gray_advisory", UNSIGNED_CATALOG_FLAG)
# 法律红线硬拦 flag 类别(命中即 errors/422/blocked,人工不可绕过)。
HARD_FLAG_KINDS = ("ad_law", "illegal_content")

_pack_cache: dict = {"mtime": None, "pack": None}


def _embedded_pack(reason: str) -> dict:
    """fail-open 回落:内嵌最小 statutory 集合,未签发(source 标记便于审计与告警)。

    SSOT 2026-07-23:法律禁止目录必须是 Owner 签发、版本化、可审计的独立事实源;
    未签发目录无权硬拦。包缺失/损坏时扫描只产出 advisory + 告警日志,内容放行。
    """
    logger.warning(
        "[guards] 法律禁止目录包不可用(%s),内容扫描降级为 advisory 提醒(未签发目录无权硬拦)", reason,
    )
    return {
        "pack_id": PACK_ID,
        "version": "0.0.0-embedded",
        "owner_signed": {},
        "ad_law": list(_EMBEDDED_AD_LAW),
        "illegal_content": list(_EMBEDDED_ILLEGAL),
        "source": "embedded_fallback",
    }


def _validate_pack(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("legal_pack_invalid")
    if str(data.get("pack_id") or "") != PACK_ID:
        raise ValueError("legal_pack_id_mismatch")
    if not str(data.get("version") or "").strip():
        raise ValueError("legal_pack_version_missing")
    signed = data.get("owner_signed") or {}
    if not str(signed.get("signed_by") or "").strip() or not str(signed.get("signed_at") or "").strip():
        raise ValueError("legal_pack_owner_signature_missing")
    categories = data.get("categories") or {}
    ad_law = [str(item.get("word") or "").strip() for item in (categories.get("ad_law_absolute") or {}).get("words") or []]
    illegal = [str(item.get("word") or "").strip() for item in (categories.get("illegal_content") or {}).get("words") or []]
    ad_law = [word for word in ad_law if word]
    illegal = [word for word in illegal if word]
    if not ad_law:
        raise ValueError("legal_pack_ad_law_missing")
    return {
        "pack_id": PACK_ID,
        "version": str(data["version"]),
        "owner_signed": dict(signed),
        "ad_law": ad_law,
        "illegal_content": illegal,
        "source": "config",
    }


def _load_pack(path: Optional[str] = None) -> dict:
    """热加载:mtime 不变走缓存,变了重读重校验;读取/校验失败走未签发回落(advisory)。"""
    path = path or _CONFIG_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _embedded_pack(f"pack_not_found:{path}")
    cached = _pack_cache.get("pack")
    if cached is not None and _pack_cache.get("mtime") == mtime:
        return cached
    try:
        with open(path, "r", encoding="utf-8") as handle:
            pack = _validate_pack(json.load(handle))
    except (OSError, ValueError) as exc:
        return _embedded_pack(str(exc))
    _pack_cache["mtime"] = mtime
    _pack_cache["pack"] = pack
    return pack


def legal_pack() -> dict:
    """当前生效的法律禁止目录(热加载)。"""
    return _load_pack()


def legal_pack_version() -> str:
    """规则版本透出:审核响应 rule_version 用,热加载后跟随包文件。"""
    return str(legal_pack().get("version") or "")


def legal_pack_info() -> dict:
    """管理/测试透出:版本 + Owner 签发 + 来源(config / embedded_fallback)。"""
    pack = legal_pack()
    return {
        "pack_id": str(pack.get("pack_id") or PACK_ID),
        "version": str(pack.get("version") or ""),
        "owner_signed": dict(pack.get("owner_signed") or {}),
        "source": str(pack.get("source") or ""),
    }


# AD_LAW_WORDS 保留为加载结果的兼容别名(存量 import 不破);运行时扫描始终走
# legal_pack() 热加载,别名只反映进程首次加载的版本。
AD_LAW_WORDS = legal_pack()["ad_law"]


def _find_words(text: str, words: Iterable[str]) -> list[str]:
    hits = []
    if not text:
        return hits
    for w in words:
        if w and w in text:
            hits.append(w)
    return hits


# 违法词否定前缀(2026-07-23 外部审查 P1-5):「打击假冒」「识别假货」「防范/抵制/
# 严查/拒绝/远离/反对/反+违法词」是执法与风控的正面表述,不算命中;裸词仍硬拦。
_ILLEGAL_NEGATION_PREFIXES = ("打击", "识别", "防范", "抵制", "严查", "拒绝", "远离", "反对", "反")

# 双重否定还原(2026-07-23 Deploy 非主阻断③):「不反对假冒」「不打击假冒」
# 「不是不打击假冒」是否定套否定——强否定前缀自身被「不/不是不」再否定,语境
# 仍落在违法词上,必须还原为命中;「杜绝/严禁」本就不含否定前缀,天然硬拦。
_ILLEGAL_DOUBLE_NEGATION_MARKERS = ("不是不", "不")


def _find_illegal_words(text: str, words: Iterable[str]) -> list[str]:
    """违法内容词命中判定:逐处出现检查紧邻前缀——先判强否定放行,再判双重否定还原。"""
    hits: list[str] = []
    if not text:
        return hits
    ordered_prefixes = sorted(_ILLEGAL_NEGATION_PREFIXES, key=len, reverse=True)
    max_marker = max(len(marker) for marker in _ILLEGAL_DOUBLE_NEGATION_MARKERS)
    for word in words:
        if not word:
            continue
        start = 0
        matched = False
        while True:
            index = text.find(word, start)
            if index < 0:
                break
            prefix = next(
                (p for p in ordered_prefixes if text[index - len(p):index] == p),
                None,
            )
            if prefix is None:
                matched = True  # 裸出现(含「杜绝/严禁+违法词」)→ 硬拦
                break
            before = text[max(0, index - len(prefix) - max_marker):index - len(prefix)]
            if any(before.endswith(marker) for marker in _ILLEGAL_DOUBLE_NEGATION_MARKERS):
                matched = True  # 双重否定还原:「不反对/不打击/不是不+违法词」→ 硬拦
                break
            start = index + len(word)
        if matched:
            hits.append(word)
    return hits


def _find_patterns(text: str, patterns: Iterable[str]) -> list[str]:
    hits = []
    if not text:
        return hits
    for p in patterns:
        try:
            if re.search(p, text, re.IGNORECASE):
                hits.append(p)
        except re.error:
            continue
    return hits


def check_no_promise(text: str) -> list[str]:
    """只查承诺词。返回命中列表(空=通过)。"""
    return _find_words(text, PROMISE_WORDS)


def hard_flag_hits(flags: Optional[dict]) -> list[str]:
    """法律红线硬拦命中(广告法极限词 + 违法内容);其余 flag 一律 advisory。"""
    hits: list[str] = []
    for kind in HARD_FLAG_KINDS:
        hits.extend((flags or {}).get(kind) or [])
    return hits


def scan_forbidden(text: str, competitors: Optional[Iterable[str]] = None) -> dict:
    """出口守卫(第②层)。综合扫描承诺/广告法/违法内容/灰词/裸指标/竞品/积分字样。

    返回 {'passed': bool, 'flags': {类别: [命中]}, 'rule_version': 法律包版本}。
    ``passed`` 只反映硬拦类命中;promise/gray_advisory 是 advisory 提醒,不影响
    passed(SSOT:措辞偏强/非明令词提醒放行,由调用方落 warnings)。
    法律目录包缺失/损坏(未签发回落)时,广告法/违法内容命中降级为
    ``unsigned_catalog_advisory`` advisory + 告警日志——未签发目录无权硬拦
    (SSOT 2026-07-23 §1/§5.1;fail-open for content,告警给人)。
    """
    flags: dict[str, list[str]] = {}
    promise = _find_words(text, PROMISE_WORDS)
    if promise:
        flags["promise"] = promise
    pack = legal_pack()
    unsigned_catalog = not str((pack.get("owner_signed") or {}).get("signed_by") or "").strip()
    # 🔴 [V6 单点化 2026-08-10] 广告法词项**不再走纯子串** ——
    # `_find_words` 是 `if w in text`,而签发包里有**裸「第一」**,于是
    # 「第一步」「第一季度」「第一次」「最大功率」「最佳实践」全部误伤。
    # 改走 `legal_context` 的语境判定;词项与版本仍单点来自同一个签发包,
    # 本文件不再自己维护第二份词表(另一份在 evidence_first_policy,同批清理)。
    from services.marketing.legal_context import find_absolute_violations

    adlaw = sorted({hit.term for hit in find_absolute_violations(text, terms=pack["ad_law"])})
    illegal = _find_illegal_words(text, pack["illegal_content"])
    if unsigned_catalog:
        unsigned_hits = list(dict.fromkeys([*adlaw, *illegal]))
        if unsigned_hits:
            # 未签发目录无权硬拦:只落 advisory 提醒 + 告警日志,绝不进硬拦类。
            logger.warning(
                "[guards] 法律禁止目录包未签发(source=%s),命中项仅 advisory 透出: %s",
                pack.get("source"), unsigned_hits,
            )
            flags[UNSIGNED_CATALOG_FLAG] = unsigned_hits
    else:
        if adlaw:
            flags["ad_law"] = adlaw
        if illegal:
            flags["illegal_content"] = illegal
    gray = _find_words(text, GRAY_ADVISORY_WORDS)
    if gray:
        flags["gray_advisory"] = gray
    naked = _find_patterns(text, NAKED_METRIC_PATTERNS)
    if naked:
        flags["naked_metric"] = naked
    # "积分"字样红线(对客必须"算力")
    if text and "积分" in text:
        flags["forbidden_term"] = ["积分"]
    if competitors:
        comp_hits = _find_words(text, [c for c in competitors if c])
        if comp_hits:
            flags["competitor"] = comp_hits
    blocking = {kind: hits for kind, hits in flags.items() if kind not in ADVISORY_FLAG_KINDS}
    return {"passed": len(blocking) == 0, "flags": flags, "rule_version": legal_pack_version()}


def clean_competitors(text: str, competitors: Optional[Iterable[str]] = None,
                      replacement: str = "同行") -> str:
    """竞品名清洗:把竞品名替换为中性词。"""
    if not text or not competitors:
        return text or ""
    out = text
    for c in competitors:
        if c:
            out = out.replace(c, replacement)
    return out


def scan_case_copy(touch_copy: dict, competitors: Optional[Iterable[str]] = None) -> dict:
    """扫描案件 touch_copy(分渠道 dict)。硬拦类或承诺词命中 → 整体不通过。

    军师审批五绿勾内部红线保持原口径(承诺词不通过);只有灰词(SSOT 非明令
    advisory)被降下——gray_advisory 命中不再影响 passed,只留在 flags 供提醒。
    """
    all_flags: dict[str, dict] = {}
    passed = True
    for channel, text in (touch_copy or {}).items():
        if not isinstance(text, str):
            continue
        r = scan_forbidden(text, competitors=competitors)
        blocking = {
            kind: hits for kind, hits in r["flags"].items()
            if kind not in {"gray_advisory", UNSIGNED_CATALOG_FLAG}
        }
        if blocking:
            passed = False
            all_flags[channel] = blocking
    return {"passed": passed, "flags": all_flags}
