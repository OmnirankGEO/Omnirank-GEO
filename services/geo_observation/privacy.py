"""隐私清洗(确定性 · 无 LLM)。

- prompt_family_key:由规范化客户购买短句确定(R6:非 LLM 自由生成),剥离品牌/PII → 匿名题族。
- query_kind / prompt_intent:确定性关键词映射(品牌词与非品牌词硬分层)。
- URL:只留规范注册 domain,去 query/fragment/token/签名。
- signal denylist 守卫:插入公共 signal 前断言无 owner/brand/原文/自定义问题/带参 URL/手机/邮箱(belt-and-suspenders)。
"""
from __future__ import annotations

import hashlib
import re
from typing import Iterable, Optional
from urllib.parse import urlparse

from .contracts import PromptIntent, QueryKind, Sentiment

_WS = re.compile(r"\s+")
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")
_PUNCT = re.compile(r"[\s　·•・,，.。;；:：'\"“”‘’\-_/\\（）()【】\[\]《》<>?？!！~、]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?86)?1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_LONG_DIGITS = re.compile(r"\d{6,}")  # 账号/订单等长数字串

_TRACKING_PARAM_HINT = ("token", "sig", "signature", "utm_", "share", "from", "spm", "key", "auth")

# 意图关键词(确定性)
_INTENT_RULES = [
    (PromptIntent.transaction, ("多少钱", "价格", "报价", "费用", "收费", "价位", "预算")),
    (PromptIntent.risk, ("风险", "投诉", "被骗", "靠谱吗", "正规吗", "有资质", "合法", "维权", "纠纷")),
    (PromptIntent.comparison, ("对比", "比较", "哪个好", "哪家好", "vs", "还是", "区别")),
    (PromptIntent.category_recommendation, ("推荐", "有哪些", "排名", "排行", "十大", "靠前")),
    (PromptIntent.evaluation, ("怎么样", "好不好", "如何", "评价", "口碑")),
    (PromptIntent.awareness, ("是什么", "介绍", "了解", "简介")),
]
_VERIFICATION_HINT = ("是不是", "真的吗", "是否", "确实", "有没有")
_COMPARISON_HINT = ("对比", "比较", "vs", "还是", "哪个好", "哪家好")

_POS_WORDS = ("推荐", "首选", "优选", "值得", "口碑好", "领先", "优质", "靠谱", "专业", "知名")
_NEG_WORDS = ("不推荐", "避免", "差评", "投诉", "谨慎", "不靠谱", "问题多")
_COND_WORDS = ("如果", "视情况", "取决于", "预算内", "本地", "根据", "假如")


def normalize_text(text: str) -> str:
    return _WS.sub(" ", (text or "").strip()).lower()


# 受控行业字典(allowlist)。brands.industry / 调研 industry 是自由文本,可能含客户名/项目名
# (实证"贵州AI数字技能就业实训基地-张三")→ 必须映射到受控 ID 才能进公共层;命不中 → None → 只能 private_only。
_INDUSTRY_CANON: dict[str, tuple[str, ...]] = {
    "home_decoration": ("装修", "家装", "装饰", "家居", "建材", "定制家居", "全屋定制"),
    "elevator": ("电梯", "扶梯", "观光电梯"),
    "education": ("教育", "培训", "留学", "考研", "学历", "职业教育", "实训", "课程", "辅导"),
    "medical": ("医疗", "医院", "口腔", "牙科", "医美", "整形", "诊所", "健康", "体检"),
    "finance": ("金融", "保险", "理财", "贷款", "基金", "证券", "财税", "会计", "记账"),
    "catering": ("餐饮", "餐厅", "饭店", "小吃", "烘焙", "火锅", "外卖"),
    "legal": ("法律", "律师", "法务", "知识产权", "专利", "商标", "维权"),
    "beauty": ("美容", "美发", "护肤", "化妆", "美甲", "spa"),
    "real_estate": ("房产", "地产", "楼盘", "租房", "二手房", "物业", "公寓"),
    "automotive": ("汽车", "4s", "二手车", "车险", "汽修", "维修保养"),
    "tourism": ("旅游", "酒店", "民宿", "景区", "旅行", "度假"),
    "it_software": ("软件", "互联网", "saas", "系统开发", "小程序", "网站建设", "app开发"),
    "manufacturing": ("制造", "工厂", "加工", "设备", "机械", "五金", "模具"),
    "logistics": ("物流", "快递", "货运", "仓储", "供应链"),
    "wedding": ("婚庆", "婚礼", "婚纱", "婚宴"),
    "home_service": ("家政", "保洁", "月嫂", "保姆", "搬家"),
    "agriculture": ("农业", "养殖", "种植", "农产品"),
    "energy_env": ("能源", "光伏", "新能源", "电力", "环保", "节能"),
    "advertising": ("广告", "营销", "传媒", "设计", "印刷", "会展"),
    "apparel": ("服装", "鞋", "箱包", "纺织", "面料"),
}


def canonical_industry(raw: Optional[str]) -> Optional[str]:
    """原始行业 → 受控 ID(命中关键词);未知/空/自由文本 → None(调用方判 private_only,防客户名/项目名泄露)。"""
    if not raw:
        return None
    r = normalize_text(raw)
    for canon, kws in _INDUSTRY_CANON.items():
        if any(kw in r for kw in kws):
            return canon
    return None


def scrub_pii(text: str) -> str:
    """去手机号/邮箱/长数字串(账号)。"""
    t = _PHONE.sub("", text or "")
    t = _EMAIL.sub("", t)
    t = _LONG_DIGITS.sub("", t)
    return _WS.sub(" ", t).strip()


def _slug(text: str) -> str:
    return _PUNCT.sub("-", (text or "").strip().lower()).strip("-")


def is_branded_prompt(question: str, brand_names: Iterable[str]) -> bool:
    q = normalize_text(question)
    return any(normalize_text(n) and normalize_text(n) in q for n in brand_names)


def classify_query_kind(question: str, brand_names: Iterable[str]) -> QueryKind:
    q = question or ""
    if any(h in q for h in _COMPARISON_HINT):
        return QueryKind.comparative
    if any(h in q for h in _VERIFICATION_HINT):
        return QueryKind.verification
    if is_branded_prompt(question, brand_names):
        return QueryKind.branded
    return QueryKind.non_branded


def derive_prompt_intent(question: str, branded: bool) -> PromptIntent:
    q = question or ""
    for intent, hints in _INTENT_RULES:
        if any(h in q for h in hints):
            return intent
    if branded:
        return PromptIntent.branded
    return PromptIntent.other


def anonymize_prompt_family_key(question: str, brand_names: Iterable[str] = (), industry: Optional[str] = None) -> str:
    """确定性匿名题族键(R6)。格式 `industry:intent:hash16`。绝不调 LLM。

    对"剥离品牌 + 去 PII + 规范化"后的题面做不可逆 SHA-256 → 公共 signal 的 prompt_family_key
    绝不携带可读的品牌/竞品/PII 文本(即便某竞品名未在 brand_names 中被剥离,也被哈希吞没)。
    identical 规范化题面 → identical hash → 跨来源同题聚合到同一题族(R6 cap 生效)。
    """
    q = normalize_text(question)
    branded = False
    for name in brand_names:
        n = normalize_text(name)
        if n and n in q:
            branded = True
            q = q.replace(n, " ")
    q = scrub_pii(q)
    q = _WS.sub(" ", q).strip()
    ind = _slug(industry or "") or "general"
    intent = derive_prompt_intent(question, branded).value
    digest = hashlib.sha256(q.encode("utf-8")).hexdigest()[:16] if q else "generic"
    return f"{ind}:{intent}:{digest}"


def _positive_present(answer: str) -> bool:
    """判正面前先剥离否定短语,防"不推荐"含"推荐"、"差评"等误判为正面。"""
    a = answer or ""
    for w in _NEG_WORDS:
        a = a.replace(w, "")
    return any(w in a for w in _POS_WORDS)


def derive_sentiment(answer: str) -> Sentiment:
    a = answer or ""
    neg = any(w in a for w in _NEG_WORDS)
    pos = _positive_present(a)
    if pos and neg:
        return Sentiment.mixed
    if pos:
        return Sentiment.positive
    if neg:
        return Sentiment.negative
    return Sentiment.neutral


def has_positive_recommendation(answer: str) -> bool:
    return _positive_present(answer)


def has_conditional_language(answer: str) -> bool:
    return any(w in (answer or "") for w in _COND_WORDS)


# ────────────────────────────── URL / domain ──────────────────────────────
def normalize_domain(url: str) -> Optional[str]:
    """提取规范注册 domain,去 scheme/path/query/fragment/端口。无法解析返回 None。"""
    if not url:
        return None
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = urlparse(raw).hostname
    except Exception:  # noqa: BLE001
        return None
    if not host:
        return None
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    # 拒绝内网 / IP / 非法(需含 TLD 点 + 合法字符,防"not a url"之类混入)
    if host in ("localhost",) or host.replace(".", "").isdigit():
        return None
    if not _HOSTNAME_RE.match(host):
        return None
    return host


def clean_source_domains(citations: Iterable[dict]) -> list[dict]:
    """citations → 仅规范 domain + source/citation 类型(去完整 URL/query/token)。去重保序。"""
    seen = set()
    out = []
    for c in citations or []:
        if not isinstance(c, dict):
            continue
        dom = normalize_domain(str(c.get("url") or ""))
        if not dom or dom in seen:
            continue
        seen.add(dom)
        stype = c.get("source_type") or "citation"
        out.append({"domain": dom, "type": "citation" if stype not in ("citation", "source") else stype})
    return out


# ────────────────────────────── signal denylist 守卫 ──────────────────────────────
_FORBIDDEN_SIGNAL_KEYS = {
    "owner_user_id", "brand_id", "agent_user_id", "upstream_user_id",
    "service_account_code", "channel_account_code", "source_record_id", "event_id_leak",
    "provider_trace_id", "cost_multiplier", "internal_cost", "answer_text", "question_text",
    "prompt_text", "raw_answer",
}


def assert_signal_clean(signal: dict) -> None:
    """插入公共 signal 前的隐私硬断言。发现禁字段/带参 URL/PII → 抛 ValueError(fail-closed)。"""
    for k in _FORBIDDEN_SIGNAL_KEYS:
        if k in signal:
            raise ValueError(f"signal 含禁字段(隐私泄露): {k}")
    # source_domains 只能是 domain,不得含 '/'(路径)、'?'(query)
    for d in signal.get("source_domains", []) or []:
        dom = d.get("domain") if isinstance(d, dict) else str(d)
        if not dom or "/" in dom or "?" in dom or "#" in dom:
            raise ValueError(f"source_domains 含非规范 domain(可能带路径/参数): {dom!r}")
    # theme keys 不得含手机/邮箱/长数字(账号/订单)PII
    for tk in signal.get("search_query_theme_keys", []) or []:
        s = tk if isinstance(tk, str) else str(tk)
        if _PHONE.search(s) or _EMAIL.search(s) or _LONG_DIGITS.search(s):
            raise ValueError(f"search_query_theme_keys 含 PII: {s!r}")
    # family_key = industry:intent:sha256-hex(hex 尾段不可逆且可能含 11 位数字串误触 _PHONE)。
    # 只扫可读前缀(industry:intent,anonymize 已 scrub_pii);hex 尾段跳过,防误报 → 防晋升事务崩溃卡死。
    fam = signal.get("prompt_family_key", "")
    fam_readable = fam.rsplit(":", 1)[0] if ":" in fam else fam
    if _PHONE.search(fam_readable) or _EMAIL.search(fam_readable):
        raise ValueError(f"prompt_family_key 含 PII: {fam!r}")
