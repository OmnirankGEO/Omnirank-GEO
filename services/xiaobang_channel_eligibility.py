"""小榜 ``channel_option_id`` 的**唯一消费方**(WO-B ③ · 规格 §10.2 / §12.3)。

## 在这之前它是什么

R3-P11 ② 把 ``channel_option_id`` 冻进了 intent,但交付单 §未做 ④ 写得很清楚:

    `channel_option_id` 仍无消费方 —— 它现在只是"调用方选了什么"的忠实回显,
    没有任何地方据它做资格/路由判断。

也就是说 ``manifest["pending"]`` 里那个 ``channel_eligibility`` 一直是 pending,
``channel.verified`` 恒 ``False``,而发布类的算力报价恒走
``pending_domain_adapter``(不出数字)。本模块把这一格接上真值。

## 🔴 为什么必须有一个 canonical 形态

资格与价格的真值源是 ``mhz_short_video``(账号目录),键是**整数 ``id``**。
而 ``channel_option_id`` 是调用方(小榜/模型)从 selection 里传进来的**字符串**。
两者之间必须有一条明写的映射,否则「选了哪个渠道」这件事永远只能是回显。

canonical 形态 = ``svideo:<media_id>``:

* ``svideo`` 是发布域既有的 lane 标识(``account_eligibility.MEDIA_TYPE_SVIDEO``),
  图文与短视频共用抖音账号池 —— 不新造命名空间;
* 前缀是**必需**的:裸数字会在将来接入第二个 lane 时静默串号,
  而串号的表现是"发到了别人的账号上"。

## 🔴 认不出来的值:降级成「还没核」,不是报错、更不是假装核过

线上既有的取值形态(例如判据里那个 ``douyin_main``)解析不出账号。这时:

* ``resolved=False`` ⇒ ``channel_eligibility`` **留在 manifest.pending 里**
  ⇒ ``channel.verified`` 仍是 ``False`` ⇒ 页面照旧显示「账号资格还没核」。
  R3-P11 定下的那三档降级文案与它们的 PW 判据**逐字不动**。
* prepare 阶段**不拦**:选渠道是可以稍后在页面上做的事,
  在 prepare 就 404 等于把「还没选好」变成「你错了」。
* execute 阶段**必拦**:到了要真发的那一刻还解析不出账号,
  就不能猜一个 —— 猜错的后果是发到别人的号上。

## 🔴 ``verified`` 与 ``eligible`` 是两件事,不许压成一位

* ``verified`` = 这一格**核过没有**;
* ``eligible`` = 核过之后**能不能用**。

压成一位的那一天,「核过了但这个号今天满了」会显示成「还没核」——
用户会一直等它变好,而它今天不会变好。所以 DTO 两个字段都给,
人话在 ``eligibility_note``(中文,不是 ASCII 枚举 —— 那会被 DLP 判红且本来就没人读得懂)。

## 🔴 不下发账号名

`materialize_command` 那边落库时 ``media_name`` 也是空串(见
``api/geo_image_note_api.py`` 的 ``names[media_id] = ""``)。这里同样**不编、也不发**
账号名:媒体账号名属于供应商侧信息,对外话术红线禁泄。
用户面能说清「哪一项渠道 + 能不能用 + 为什么不能用」就够做决定了。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Optional

from services.geo_douyin.account_eligibility import (
    MEDIA_TYPE_SVIDEO,
    REASON_BLACKLISTED,
    REASON_FREQUENCY_FULL,
    REASON_INACTIVE,
    REASON_NOT_DOUYIN,
    REASON_NOT_FOUND,
    REASON_NO_IMAGE_NOTE,
    evaluate_accounts,
)

#: manifest 里代表「投放账号资格」的那一项。核过之后要把它从 pending 里摘掉。
MANIFEST_INPUT_CHANNEL_ELIGIBILITY: Final = "channel_eligibility"

#: canonical 渠道项前缀。见模块 docstring 的「为什么必须有前缀」。
CHANNEL_OPTION_SVIDEO_PREFIX: Final = "svideo:"

#: 资格失败原因 → 人话。
#:
#: 🔴 **分母就是 ``account_eligibility`` 声明的那一组 reason**,不是我记得几个:
#:    配套判据 ``test_every_eligibility_reason_has_human_text`` 从那个模块机械枚举,
#:    漏一个即红。漏掉的表现是页面上出现一句英文枚举(DLP 还会把它判成泄漏 ⇒ 500)。
REASON_TEXT: Final[dict[str, str]] = {
    REASON_NOT_FOUND: "这个投放账号找不到了,换一个再试",
    REASON_INACTIVE: "这个投放账号暂时停用了,换一个再试",
    REASON_BLACKLISTED: "这个投放账号已经停投了,换一个再试",
    REASON_NOT_DOUYIN: "这个账号不是抖音号,发不了图文",
    REASON_NO_IMAGE_NOTE: "这个账号发不了图文,换一个能发图文的",
    REASON_FREQUENCY_FULL: "这个账号今天的额度用完了,换一个或者明天再发",
}

#: 解析不出账号时的人话。它**不是**一条失败原因 —— 是「这一格还没核」。
UNRESOLVED_TEXT: Final = "还没选定投放账号,资格和算力要选完才能算"


@dataclass(frozen=True)
class ChannelResolution:
    """一次渠道解析的结果。**不可变**:它要随 intent 一起被冻结。"""

    channel_option_id: str
    media_id: Optional[int]
    #: 能不能把 option 解析成一个具体账号。False = 这一格「还没核」。
    resolved: bool
    #: 核过之后能不能用。``resolved=False`` 时恒 False,但语义是"未知"不是"不能用"。
    eligible: bool
    reason_code: Optional[str]
    reason_text: str
    today_remaining: int
    price_points: Optional[int]
    price_version: Optional[str]
    #: 冻结进 intent 的**逐项价格指纹**。execute 拿它去比「价目变没变」——
    #: 执行时现算一个再和自己比,那条锁永远不会红。
    price_fingerprint: Optional[str] = None

    # ── 投影 ────────────────────────────────────────────────────────────
    def as_channel_dict(self, *, resource_kind: Optional[str]) -> dict:
        """``preview["channel"]`` 那一格。

        🔴 与 R3-P11 的形状**向后兼容**:``channel_option_id`` / ``resource_kind`` /
        ``verified`` 三个键含义一个字没变,只是 ``verified`` 从"跟着 manifest 的
        常量"变成了真值。新增的两个键(``eligible`` / ``eligibility_note``)
        只在**真核过**时出现 —— 没核过时不出现,前端那三档降级文案原样生效。
        """
        out: dict[str, Any] = {
            "resource_kind": resource_kind,
            "verified": bool(self.resolved),
        }
        if self.channel_option_id:
            out["channel_option_id"] = self.channel_option_id
        if self.resolved:
            out["eligible"] = bool(self.eligible)
            out["eligibility_note"] = self.reason_text
        return {k: v for k, v in out.items() if v is not None}

    def pending_inputs(self, pending: list) -> list:
        """核过就把 ``channel_eligibility`` 从 pending 里摘掉,否则原样返回。

        🔴 摘的条件是 ``resolved``(核过没有),**不是** ``eligible``(能不能用)。
        「核过了,结论是不能用」也是核过了 —— 把它留在 pending 里会让页面
        显示「还没核」,而用户其实已经拿到了明确结论。
        """
        if not self.resolved:
            return list(pending)
        return [item for item in pending if item != MANIFEST_INPUT_CHANNEL_ELIGIBILITY]


def parse_channel_option(channel_option_id: Any) -> Optional[int]:
    """``svideo:<media_id>`` → media_id。认不出返回 ``None``,**不抛**。

    认不出是常态(线上既有取值就认不出),抛异常会把「还没选好」变成一次失败。
    """
    raw = str(channel_option_id or "").strip()
    if not raw.startswith(CHANNEL_OPTION_SVIDEO_PREFIX):
        return None
    tail = raw[len(CHANNEL_OPTION_SVIDEO_PREFIX):].strip()
    if not tail.isdigit():
        return None
    media_id = int(tail)
    return media_id if media_id > 0 else None


def channel_option_for_media(media_id: int) -> str:
    """反向构造。给 query 阶段列渠道项用 —— 两个方向共用同一个前缀常量。"""
    return "{0}{1}".format(CHANNEL_OPTION_SVIDEO_PREFIX, int(media_id))


def unresolved(channel_option_id: Any) -> ChannelResolution:
    """「还没核」的那一档。"""
    return ChannelResolution(
        channel_option_id=str(channel_option_id or "").strip(),
        media_id=None, resolved=False, eligible=False,
        reason_code=None, reason_text=UNRESOLVED_TEXT,
        today_remaining=0, price_points=None, price_version=None,
    )


def resolve_channel_option(cur, channel_option_id: Any, *,
                           daily_limit: Optional[int] = None) -> ChannelResolution:
    """把渠道项解析成**真资格 + 真价格**。

    🔴 资格与价格都走发布域**既有**那一个出口(``evaluate_accounts`` +
       ``publish_price_resolver``),不在这里另写一套判定或价公式。
       另写一套的那天,prepare 报的价与 execute 冻的钱就会各算各的。
    """
    media_id = parse_channel_option(channel_option_id)
    if media_id is None:
        return unresolved(channel_option_id)

    if daily_limit is None:
        from api.geo_image_note_api import image_note_daily_limit

        daily_limit = image_note_daily_limit()

    from api.geo_image_note_api import PUBLISH_FEATURE_CODE
    from services.geo_douyin.publish_batch_core import (
        publish_item_fingerprint, publish_price_resolver,
    )

    evaluated = evaluate_accounts(
        cur, [media_id], daily_limit=int(daily_limit),
        price_resolver=publish_price_resolver(),
        media_type=MEDIA_TYPE_SVIDEO,
    )
    account = evaluated.get(media_id)
    if account is None:                      # evaluate_accounts 对空 id 才返空
        return unresolved(channel_option_id)

    reason_code = account.reason_code
    if account.available:
        text = "这个投放账号可以发图文,今天还能发 {0} 条".format(int(account.today_remaining))
    else:
        text = REASON_TEXT.get(str(reason_code or ""), "这个投放账号暂时不能用,换一个再试")

    return ChannelResolution(
        channel_option_id=str(channel_option_id).strip(),
        media_id=media_id,
        resolved=True,
        eligible=bool(account.available),
        reason_code=reason_code,
        reason_text=text,
        today_remaining=int(account.today_remaining),
        # 🔴 不可用的账号**不给价**:给了就会有人拿它去显示"要花 X 算力",
        #    而这一笔根本发不出去。
        price_points=(int(account.final_price_points or 0) if account.available
                      and account.final_price_points is not None else None),
        price_version=(str(account.price_version) if account.available
                       and account.price_version else None),
        price_fingerprint=(
            publish_item_fingerprint(
                media_id=media_id,
                final_price_points=int(account.final_price_points or 0),
                price_version=account.price_version,
                feature_code=PUBLISH_FEATURE_CODE)
            if account.available and account.final_price_points is not None else None),
    )


def eligibility_reason_denominator() -> frozenset:
    """给锁用:资格失败原因的**全集**。

    🔴 从 ``account_eligibility`` 模块**按属性名机械枚举**(``REASON_*``),
       不在这里手抄六个常量。手抄的分母漏掉的那一项不会让任何判据变红 ——
       它会以「页面上冒出一句英文枚举 + DLP 判红 500」的方式在生产暴露。
    """
    from services.geo_douyin import account_eligibility as ae

    return frozenset(
        str(getattr(ae, name)) for name in dir(ae)
        if name.startswith("REASON_") and isinstance(getattr(ae, name), str)
    )


__all__ = [
    "CHANNEL_OPTION_SVIDEO_PREFIX",
    "MANIFEST_INPUT_CHANNEL_ELIGIBILITY",
    "REASON_TEXT",
    "UNRESOLVED_TEXT",
    "ChannelResolution",
    "channel_option_for_media",
    "eligibility_reason_denominator",
    "parse_channel_option",
    "resolve_channel_option",
    "unresolved",
]
