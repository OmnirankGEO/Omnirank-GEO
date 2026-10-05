"""WP2 · 制作 / 投放**两个独立定价源**与两枚不可互换的价格指纹(规格 02 §6.2)。

## 为什么必须是两条链,而不是"单位相同就合一"

对客确实都只见整数算力,但两者的**权威来源不同**:

| 操作 | 权威定价源 | 指纹 |
|---|---|---|
| 制作 / 重绘 / 再次创作 | `feature_pricing` + `services.geo_douyin.pricing` | `production_price_fingerprint` |
| 账号投放 | `services.media_price_projection`(同一 resolver 同时服务目录显示/筛选/排序/preview/下单/结算) | `publish_price_fingerprint` |

规格 §6.2 原文:「禁止用 production fingerprint 确认 publish,反之亦然」。
这不是洁癖 —— 两条链的价格**各自会漂移**,拿 A 的指纹确认 B 的下单,
等于给 B 开了一扇"价格变了但确认锁没响"的门。本模块用**带前缀的不透明指纹** +
入口硬校验把这件事变成结构性不可能,而不是靠调用方记得传对。

## 指纹的定义(不许拼串)

规格 §6.2:「fingerprint 定义为带 schema/version 的 canonical JSON 完整输入再做 SHA-256,
不能靠字段拼串或漏 target/policy」。

canonical 序列化**复用**本仓已有的
`services.article_closed_loop_contract.canonical_json / snapshot_hash` ——
它已经处理好 Decimal/datetime/NFC 归一,再写第三份就是又一个"第二套口径"。
"""
from __future__ import annotations

from typing import Any, Final, Mapping

from services.article_closed_loop_contract import canonical_json, snapshot_hash

# 指纹的 schema 版本。**改这个值必须让所有在途确认锁失效** —— 这正是它存在的意义:
# 计价输入的结构变了,旧确认就不该再被接受。
PRICING_FINGERPRINT_SCHEMA: Final = "geo-image-note-pricing-v1"

# 两条链的不透明前缀。判据打前缀,不打"长得像不像" ——
# 长得像的两个 sha256 没有任何办法区分,前缀才是可判别的那一位。
PRODUCTION_PREFIX: Final = "production-price-v1:"
PUBLISH_PREFIX: Final = "publish-price-v1:"

OPERATION_PRODUCTION: Final = "production"
OPERATION_PUBLISH: Final = "publish"

_PREFIX_BY_OPERATION: Final[dict[str, str]] = {
    OPERATION_PRODUCTION: PRODUCTION_PREFIX,
    OPERATION_PUBLISH: PUBLISH_PREFIX,
}


class PriceFingerprintMismatch(ValueError):
    """把一条链的指纹送进另一条链。这是 H0(对象身份),不是普通参数错。"""


class PriceChanged(ValueError):
    """服务端权威价格已漂移。调用方必须 409 `PRICE_CHANGED`,零冻结零创建。"""


def _fingerprint(operation: str, payload: Mapping[str, Any]) -> str:
    prefix = _PREFIX_BY_OPERATION.get(operation)
    if prefix is None:
        raise ValueError(f"unknown pricing operation: {operation!r}")
    # schema/version + operation 一起进摘要:同一份输入在两条链下必须得到不同摘要,
    # 否则"前缀不同但摘要相同"会让人以为可以剥掉前缀通用。
    digest = snapshot_hash({
        "schema": PRICING_FINGERPRINT_SCHEMA,
        "operation": operation,
        "inputs": dict(payload),
    })
    return f"{prefix}{digest}"


def production_fingerprint(*, feature_code: str, unit_points: int, card_count: int,
                           extra_card_points: int, final_price_points: int,
                           resolver_version: str, catalog_version: str,
                           style_catalog_version: str | None = None,
                           policy_version: str | None = None) -> str:
    """制作链指纹。

    🔴 `feature_code` 必须进指纹(规格 §7.3 末段:exact feature code/SKU 要冻结,
       「改 SKU 必须导致指纹变化和判别测试转红」)。
    """
    return _fingerprint(OPERATION_PRODUCTION, {
        "feature_code": feature_code,
        "unit_points": int(unit_points),
        "card_count": int(card_count),
        "extra_card_points": int(extra_card_points),
        "final_price_points": int(final_price_points),
        "resolver_version": resolver_version,
        "catalog_version": catalog_version,
        "style_catalog_version": style_catalog_version,
        "policy_version": policy_version,
    })


def publish_fingerprint(*, feature_code: str, media_id: int, final_price_points: int,
                        markup_version: str, resolver_version: str, catalog_version: str,
                        policy_version: str | None = None) -> str:
    """投放链指纹。

    🔴 `media_id` 必须进指纹:同一批里两项只差账号时,不能因为总价相同就共用确认锁
       (规格 §7.3:「总价相同不能掩盖逐项涨跌」)。
    """
    return _fingerprint(OPERATION_PUBLISH, {
        "feature_code": feature_code,
        "media_id": int(media_id),
        "final_price_points": int(final_price_points),
        "markup_version": markup_version,
        "resolver_version": resolver_version,
        "catalog_version": catalog_version,
        "policy_version": policy_version,
    })


def operation_of(fingerprint: str) -> str | None:
    """从指纹反推它属于哪条链。认不出返回 None(不猜)。"""
    for operation, prefix in _PREFIX_BY_OPERATION.items():
        if isinstance(fingerprint, str) and fingerprint.startswith(prefix):
            return operation
    return None


def assert_fingerprint_operation(fingerprint: str, expected_operation: str) -> None:
    """入口硬校验:这枚指纹**必须**来自本条链。

    🔴 这是「删掉就必须转红」的那种校验。删了它之后,把 publish 指纹送进
       production preview 会被当成普通的"指纹不匹配"→ 409 PRICE_CHANGED,
       看起来还挺像回事 —— 但那掩盖了真正的问题(调用方在跨链传身份)。
       所以它必须是**独立的一条**,报 H0 对象身份,而不是混进价格漂移。
    """
    actual = operation_of(fingerprint)
    if actual is None:
        raise PriceFingerprintMismatch(
            f"无法识别的价格指纹(既不是制作链也不是投放链):{str(fingerprint)[:40]!r}"
        )
    if actual != expected_operation:
        raise PriceFingerprintMismatch(
            f"把 {actual} 链的价格指纹送进了 {expected_operation} 链 —— "
            "制作与投放各有权威定价源,指纹不可互换(规格 02 §6.2)"
        )


def assert_price_unchanged(*, expected_fingerprint: str, actual_fingerprint: str,
                           operation: str) -> None:
    """确认锁。先验链,再验漂移 —— 顺序不能反。

    反了的话,跨链传入会先撞上"指纹不同"而被报成价格漂移,
    真正的对象身份错误就被伪装成了一次普通的重新确认。
    """
    assert_fingerprint_operation(expected_fingerprint, operation)
    assert_fingerprint_operation(actual_fingerprint, operation)
    if expected_fingerprint != actual_fingerprint:
        raise PriceChanged("服务端权威价格版本已变化,请重新确认")


def canonical_price_snapshot(*, operation: str, final_price_points: int,
                             feature_code: str, resolver_version: str,
                             catalog_version: str, markup_version: str | None,
                             policy_version: str | None, preview_expires_at: str,
                             confirmed_at: str | None) -> dict[str, Any]:
    """持久化用的 canonical price snapshot(规格 §6.2 末段逐项列出的那组字段)。

    每个 production task / publish item 都要存这一份;
    非 exempt 模式下物理 reserve/freeze 的金额必须**逐项等于**这里的 `final_price_points`。
    """
    if operation not in _PREFIX_BY_OPERATION:
        raise ValueError(f"unknown pricing operation: {operation!r}")
    snapshot = {
        "schema": PRICING_FINGERPRINT_SCHEMA,
        "operation": operation,
        "final_price_points": int(final_price_points),
        "feature_code": feature_code,
        "resolver_version": resolver_version,
        "catalog_version": catalog_version,
        "markup_version": markup_version,
        "policy_version": policy_version,
        "preview_expires_at": preview_expires_at,
        "confirmed_at": confirmed_at,
    }
    # canonical_json 只是把它稳定序列化一遍,证明这份 snapshot 是可复现的;
    # 不可序列化的值(比如 float NaN)在这里就会炸,而不是等到入库才炸。
    canonical_json(snapshot)
    return snapshot
