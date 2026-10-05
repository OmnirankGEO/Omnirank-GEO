"""对外文案的供应商信息隔离。

Owner 铁律（2026-07-27 重申）：**上线后普通用户只应感知到"媒体变多了"，
不得看到任何供应商痕迹。** 与既有 `feedback_no_supplier_names_to_users` 一致。

需要遮蔽的不只是新渠道 —— 媒介盒子同样是供应商，一起遮。仓内既有的中性说法是
**「外部发布通道」**（见 `api/meijiehezi_api.py` 的报错文案），本模块沿用它，
不另造词。

三类泄漏面，逐个堵：

1. **上游原文**：拒稿理由、失败原因直接来自对方接口，可能带品牌名/域名/订单号；
2. **结构字段**：``provider`` / ``provider_media_id`` / 上游订单号本来就不该出现在
   面向代理的响应里；
3. **异常信息**：把 requests 的异常字符串直接抛给前端会带上渠道域名。

内部审计（admin / 日志 / 原始响应表）**不受影响** —— 排障需要真名。
"""
from __future__ import annotations

import re
from typing import Any, Final, Iterable

#: 对代理/客户统一的中性说法。
NEUTRAL_CHANNEL_LABEL: Final = "外部发布通道"

#: 供应商可识别标记。大小写不敏感；域名放在名字前面，先长后短避免半截替换。
_VENDOR_TOKENS: Final[tuple[str, ...]] = (
    "kuaiyibo.cn", "kuaiyibo", "快易播",
    "meijiehezi", "媒介盒子", "媒介盒",
)

_VENDOR_RE: Final = re.compile("|".join(re.escape(t) for t in _VENDOR_TOKENS), re.IGNORECASE)

#: 不得出现在对代理响应里的字段名。
SUPPLIER_ONLY_FIELDS: Final[frozenset[str]] = frozenset({
    "provider", "provider_media_id", "provider_order_id", "mhz_order_id",
    "mhz_raw_response", "resource_id", "kuaiyibo_order_id",
})


def scrub_text(text: Any) -> str:
    """把任意文案里的供应商痕迹换成中性说法。"""
    value = str(text or "")
    if not value:
        return ""
    return _VENDOR_RE.sub(NEUTRAL_CHANNEL_LABEL, value)


def contains_vendor_trace(text: Any) -> bool:
    return bool(_VENDOR_RE.search(str(text or "")))


#: 词表**外**的主机名 / URL。按**形状**认,不按名字认。
#:
#: 🔴 [#196 c1c] 这不是 `_VENDOR_TOKENS` 的替代,是它的**纵深**。词表只认得我们
#:    今天已经知道的那几个供应商;换一家渠道、对方换一个 CDN 域、上游把自己的
#:    对象存储域名写进报错里 —— 词表当天就是过期的,而**过期的词表不会报错**。
#:    形状不会过期:认不出「这是谁」,但认得出「这是个主机名」。
#:
#: 边界(先长后短:URL 整条先吃掉,否则只剩 host 被换、协议和路径留在原地):
#:  · 左右用「不是 ASCII 主机字符」而不是 `\b` —— `\b` 在「渠道x.cn超时」这种
#:    中文紧贴的写法上不成立(中文也是 `\w`),而这恰恰是上游最常见的拼法。
#:  · 只认 ASCII:纯中文句子不会被误伤。宁可多洗掉一个 `a.png`,不可漏掉一个域名。
_URL_RE: Final = re.compile(r"https?://\S+", re.IGNORECASE)
_HOSTNAME_RE: Final = re.compile(
    r"(?<![A-Za-z0-9.\-])(?:[a-z0-9\-]+\.)+[a-z]{2,}(?![A-Za-z0-9.\-])", re.IGNORECASE)


def scrub_hosts_and_urls(text: Any) -> str:
    """把任何**看起来像**主机名 / URL 的东西换成中性说法。

    与 `scrub_text` 的分工:那个按名字认(词表),这个按形状认。两道都要 ——
    词表永远落后于现实,形状不会。
    """
    value = str(text or "")
    if not value:
        return ""
    value = _URL_RE.sub(NEUTRAL_CHANNEL_LABEL, value)
    return _HOSTNAME_RE.sub(NEUTRAL_CHANNEL_LABEL, value)


def scrub_payload(payload: Any, *, drop_fields: Iterable[str] | None = None) -> Any:
    """递归清洗对外响应：删供应商专属字段 + 洗文案。

    只用于**面向代理/客户**的响应。admin 审计面不要过这层。
    """
    drop = set(SUPPLIER_ONLY_FIELDS) | set(drop_fields or ())
    if isinstance(payload, dict):
        return {
            k: scrub_payload(v, drop_fields=drop)
            for k, v in payload.items() if k not in drop
        }
    if isinstance(payload, (list, tuple)):
        return [scrub_payload(v, drop_fields=drop) for v in payload]
    if isinstance(payload, str):
        return scrub_text(payload)
    return payload


#: 上游失败原因 -> (面向代理的人话, 下一步怎么办)。上游只有三态,「审核未通过」也归在失败里,
#: 所以要按 message 语义分流,给代理的提示才准确。
#:
#: 🔴 [下单备注 P0 · 2026-08-10] 第三列 repair_hint 是新加的。
#:   总册 §13.5「拦截必须带出口」—— 只告诉用户"失败了、钱退了"而不说下一步,
#:   等于把人堵死在原地。生产实证:2026-08-10 全库 84 条 failed 里 **68 条**
#:   `reject_user_message` 为空,剩下的绝大多数只有那句无出口的兜底话。
#:   把出口和文案放**同一张表**,是为了不让两者各写一处然后飘走。
_REASON_PATTERNS: Final[tuple[tuple[str, str, str], ...]] = (
    # 「备注」放最前只是因为它最具体。
    # ⚠️ 我一开始写的理由是「否则会被敏感/违禁抢走」——**那是错的,已实测证伪**:
    #    上游那句「备注字段内容不合法」对现有 9 个 needle 逐个测,只有「备注」命中
    #    (「内容」二字不是 needle)。留着一个假理由比没有理由更坏,故订正。
    #    真正需要小心的是**将来**加 needle:这张表是"先命中先生效",
    #    加进来的短词(如「内容」)会把这条抢走 —— 锁 test_remark_reason_not_swallowed 盯着它。
    ("备注", "下单备注不被该媒体接受，已自动退还算力",
     "请清空「投放地区备注」后重新发布。该备注功能正在与发布通道对齐字段规则，暂时停用。"),
    ("审核", "媒体审核未通过，已自动退还算力",
     "可在发布中心换一家媒体重新发布，或调整正文后再试。"),
    ("拒稿", "媒体拒稿，已自动退还算力",
     "可在发布中心换一家媒体重新发布。"),
    ("敏感", "内容含该媒体不接受的表述，已自动退还算力",
     "请修改正文中可能敏感的表述后重新发布。"),
    ("违禁", "内容含该媒体不接受的表述，已自动退还算力",
     "请修改正文中可能敏感的表述后重新发布。"),
    ("标题", "标题不符合该媒体要求，已自动退还算力",
     "请调整标题后重新发布（多数媒体要求 ≤45 字且不含特殊符号）。"),
    ("重复", "该媒体判定内容重复，已自动退还算力",
     "请换一篇文章，或换一家媒体重新发布。"),
    ("余额", "发布通道暂时不可用，已自动退还算力",
     "稍后重试即可；持续不可用请联系平台。"),
    ("超时", "发布超时，已自动退还算力",
     "稍后在发布中心重新发布即可。"),
)

DEFAULT_FAILURE_MESSAGE: Final = "本次发布未成功，已自动退还算力"
DEFAULT_REPAIR_HINT: Final = "可在发布中心换一家媒体重新发布。"


def _match(upstream_message: Any) -> tuple[str, str] | None:
    raw = str(upstream_message or "").strip()
    if not raw:
        return None
    for needle, message, hint in _REASON_PATTERNS:
        if needle in raw:
            return message, hint
    return None


def user_facing_failure_reason(upstream_message: Any) -> str:
    """上游失败原因 -> 给代理看的话。

    **不做原文透传** —— 上游文案可能带供应商品牌、内部订单号、甚至他们自己的
    客服话术。归类到我们自己的说法，兜底也给一句完整的人话。
    """
    hit = _match(upstream_message)
    return hit[0] if hit else DEFAULT_FAILURE_MESSAGE


def repair_hint_for(upstream_message: Any) -> str:
    """上游失败原因 -> **下一步怎么办**(总册 §13.5:拦截必须带出口)。

    与 `user_facing_failure_reason` 同表同键,永远不会出现"文案说 A、出口指向 B"。
    """
    hit = _match(upstream_message)
    return hit[1] if hit else DEFAULT_REPAIR_HINT


def safe_error_detail(exc: BaseException | str) -> str:
    """异常 -> 可以给前端看的字符串（洗掉域名/品牌）。"""
    return scrub_text(str(exc)) or "发布通道暂时不可用"
