"""发布外调的**唯一边界**(规格 §12.3 窗口③ / §12.1 三态)。

═══════════════════════════════════════════════════════════════════════
🔴 这个模块存在的理由:让「外调」变成一个可以被判据抓住的对象
═══════════════════════════════════════════════════════════════════════
``publish_outbox.dispatch_once`` 的 ``provider_call`` 是**注入**的 ——
注入而不是 import,是因为四个 kill window 的判据必须能在任意一步抛异常。
但注入点如果散落在调用方,就会出现两个后果:

  ① 生产到底注了哪个 transport,没有一个地方能机械回答;
  ② 判据里那个 fake 与生产那个真适配器长得不一样也没人会红。

所以本模块把注入点收成一张**登记表**:生产由 :func:`resolve` 解析,
判据由调用方显式传参。两条路径共用同一个 :class:`ProviderTransport` 形状。

═══════════════════════════════════════════════════════════════════════
🔴 生产路径零 ``is_test`` / ``dry_run`` / ``sandbox`` 短路
═══════════════════════════════════════════════════════════════════════
本仓铁律:发布链上的文件里这三个词零命中。所以这里**没有**
「测试环境走假的」那种分支 —— 判据要用假的,就自己把它当参数传给 worker
(``publish_worker.dispatch_pending(provider_call=...)``)。
生产的 worker 一个参数都不传,走 :func:`resolve`。

「换一个分支」和「换一个参数」的差别不是风格:分支会在生产里被求值,
参数不会。前者要靠"这个环境变量在生产没设"来保证安全,后者靠**物理上不存在**。

═══════════════════════════════════════════════════════════════════════
🔴 ``ready`` 不是布尔洁癖,它决定客户会不会被冻一笔发不出去的钱
═══════════════════════════════════════════════════════════════════════
终审 P0-1 的病根逐字是「确认发布 → 冻算力 → 什么都没发生」。
接了 worker 之后这条链能动了,但如果外发通道本身没配置,症状会退化成
「冻上了 → 派发一直失败 → 停在冻结等人工」。比原来好,但仍然不该发生。

所以 confirm 侧在**冻钱之前**问一次 :func:`readiness`;不 ready 就
typed 503 + 零冻结 + 零 command + 零 outbox。这不是新造闸 ——
§15.7 本来就有 ``ADMISSION_UNAVAILABLE``(503, retryable)这一格。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any, Callable, Mapping, NamedTuple

from services.defensive_geo.publish import media_identity as _mi
from services.defensive_geo.publish.publish_outbox import ProviderResult

logger = logging.getLogger("GEO-DefGeoPublishTransport")

TRANSPORT_REGISTRY_VERSION = "defgeo-publish-transport-v1"

#: 生产 transport 的键。**只有一个** —— 登记表不是给"再加一个测试档"用的。
PRODUCTION_TRANSPORT_KEY = "mhz_order_item"


class TransportNotConfigured(RuntimeError):
    """外发通道没配好。**不是**外调失败 —— 一次外调都没发生过。

    两者必须分开:外调失败要 hold frozen(可能已到达对方),
    "根本没调过"可以安全重试。把前者的处置套在后者上 = 白白冻着钱。
    """


class Readiness(NamedTuple):
    key: str
    ready: bool
    #: 给人看的一句话(不含供应商名/密钥/内部参数 —— POR-14)。
    reason: str


class ProviderTransport(NamedTuple):
    key: str
    call: "ProviderCall"


ProviderCall = Callable[[Mapping[str, Any]], ProviderResult]


# ══════════════════════════════════════════════════════════════════════════
# 登记表
# ══════════════════════════════════════════════════════════════════════════
_REGISTRY: dict[str, tuple[Callable[[], Readiness], "ProviderCall"]] = {}


def register(key: str, *, readiness: Callable[[], Readiness], call: "ProviderCall") -> None:
    """登记一个 transport。同键重复登记 = 覆盖(最后一个赢),并留日志。"""
    if key in _REGISTRY:
        logger.warning("[defgeo-transport] %s 被重复登记,后者生效", key)
    _REGISTRY[key] = (readiness, call)


def registered_keys() -> tuple[str, ...]:
    """机械分母。判据拿它对账,不手抄。"""
    return tuple(sorted(_REGISTRY))


def readiness(key: str = PRODUCTION_TRANSPORT_KEY) -> Readiness:
    """外发通道准备好了没有。**永不抛** —— 它自己失败也只是"没准备好"。"""
    entry = _REGISTRY.get(key)
    if entry is None:
        return Readiness(key, False, "外发通道还没有接入")
    try:
        return entry[0]()
    except Exception as exc:                              # noqa: BLE001
        logger.error("[defgeo-transport] %s 就绪自检异常:%s", key, exc)
        return Readiness(key, False, "外发通道暂时不可用")


def resolve(key: str = PRODUCTION_TRANSPORT_KEY) -> ProviderTransport:
    """解析生产 transport。没登记 / 没就绪 ⇒ 抛 :class:`TransportNotConfigured`。

    🔴 抛而不是返回一个"什么都不做的 transport":后者会让 worker 以为
       自己调过了,于是把「没调过」记成「调了但结果未知」—— 钱就再也回不来了。
    """
    entry = _REGISTRY.get(key)
    if entry is None:
        raise TransportNotConfigured(f"发布外发通道 {key!r} 没有登记")
    state = readiness(key)
    if not state.ready:
        raise TransportNotConfigured(state.reason)
    return ProviderTransport(key, entry[1])


# ══════════════════════════════════════════════════════════════════════════
# 生产适配器 —— 现役媒介外发通道
# ══════════════════════════════════════════════════════════════════════════
def _session_configured() -> tuple[bool, str]:
    """只问「配没配」,不发任何网络请求 —— 就绪自检不许有外部副作用。"""
    try:
        from db.meijiehezi_db import get_config
    except Exception as exc:                              # noqa: BLE001
        return False, "外发通道模块不可用"
    try:
        return (bool(get_config("phpsessid")), "外发通道未配置登录态")
    except Exception:                                     # noqa: BLE001
        return False, "外发通道配置读取失败"


def _mhz_readiness() -> Readiness:
    from services.publish_channels import configured_mode
    if configured_mode():
        return Readiness(PRODUCTION_TRANSPORT_KEY, True, "")
    return Readiness(PRODUCTION_TRANSPORT_KEY, False, "开源版没有接入发布渠道")


def _run_blocking(coro) -> Any:
    """在**独立线程的独立 loop** 里跑一个协程,并阻塞等它。

    🔴 为什么不是 ``asyncio.run``:worker 自己就跑在一个 loop 里,
       在 loop 内再 ``run`` 会直接抛 ``RuntimeError``。
    🔴 为什么 ``provider_call`` 保持同步签名:``dispatch_once`` 的
       ``result = provider_call(...)`` 是既有形状,改成 ``await`` 会让
       所有既有调用方(含判据里的同步 fake)一起改 —— 那是改写原谓词,
       不是追加谓词。桥接的代价局限在本函数里。
    """
    def _runner() -> Any:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            try:
                loop.close()
            except Exception:                             # noqa: BLE001
                pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result()


def resolve_provider_media(cur, public_media_key: str) -> dict[str, Any]:
    """``public_media_key``(HMAC,单向)→ 目录里那一行的 provider 身份。

    🔴 只能**正推比对**:key 是 HMAC,反解不出来。所以扫目录、逐行重算、
       比中的那一行就是它。O(目录) 对一个 cron worker 是可以接受的代价,
       而"把 provider_media_id 写进冻结面"不可以 —— 那是私有列上客户面。
    """
    cur.execute(
        "SELECT id, provider, provider_media_id, media_name, source_domain "
        "FROM mhz_media WHERE is_active = TRUE AND provider IS NOT NULL "
        "  AND provider_media_id IS NOT NULL"
    )
    rows = cur.fetchall() or []
    for row in rows:
        item = dict(row) if not isinstance(row, dict) else row
        try:
            derived = _mi.public_media_key(
                provider=str(item["provider"]),
                provider_media_id=item["provider_media_id"],
            )
        except Exception:                                 # noqa: BLE001
            continue
        if derived == public_media_key:
            return dict(item)
    raise TransportNotConfigured(
        "冻结面里那家媒体在当前目录里已经找不到了 —— 不猜一个替代品"
    )


def _mhz_publish(command: Mapping[str, Any]) -> ProviderResult:
    """真外调。**三态返回**,异常交给 :func:`~publish_outbox.dispatch_once` 收未知。

    raw 坐标写 ``mhz_publish_order_items`` / ``status`` ——
    与 ``publish_settlement.RAW_STATE_CENSUS`` 同源(那张表是分母)。
    """
    from services.meijiehezi.client import (
        ConfirmationRequiredError, PublishError,
    )

    title = str(command.get("_title") or "")
    body = str(command.get("_frozen_body") or "")
    media_ids = list(command.get("_provider_media_ids") or [])
    if not media_ids:
        raise TransportNotConfigured("这条命令没有解析出可投递的媒体")

    from api.meijiehezi_api import _get_client   # 现役唯一 client 工厂,不另造

    client = _get_client()

    async def _go():
        await client.refresh_token()
        try:
            return await client.publish(
                title=title, content_md=body, media_ids=[int(m) for m in media_ids],
                brand_id=command.get("brand_id"),
                # 🔴 [B-5] 本链把上游单号当**结算身份**用
                #    (``defgeo_publish_commands.provider_order_ref``,051 上有唯一索引)。
                #    「标题+媒体」反查命中多条时必须**拒绝绑定**,不挑第一条 ——
                #    挑错一条 = 同一个上游终态去结算两笔冻结。
                strict_order_ref_binding=True,
            )
        finally:
            try:
                await client.close()
            except Exception:                             # noqa: BLE001
                pass

    try:
        result = _run_blocking(_go())
    except ConfirmationRequiredError as exc:
        # 上游要人工补字段 —— 请求**已经到达对方**,结果未知,绝不 release。
        return ProviderResult(
            kind="unknown", raw_table="mhz_publish_order_items", raw_column="status",
            raw_value="awaiting_confirmation", detail=str(getattr(exc, "field", "")),
        )
    except PublishError as exc:
        # 上游权威拒稿 = 零接单证据。这是**唯一**能安全退款的一格。
        return ProviderResult(
            kind="rejected_no_effect", raw_table="mhz_publish_order_items",
            raw_column="status", raw_value="rejected", detail=str(exc)[:200],
        )
    order_sn = str(getattr(result, "order_sn", "") or "")
    if not order_sn:
        return ProviderResult(
            kind="unknown", raw_table="mhz_publish_order_items", raw_column="status",
            raw_value="awaiting_sync", detail="上游接单但未返回单号",
        )
    return ProviderResult(
        kind="accepted", raw_table="mhz_publish_order_items", raw_column="status",
        raw_value="submitted", detail=order_sn,
    )


register(PRODUCTION_TRANSPORT_KEY, readiness=_mhz_readiness, call=_mhz_publish)


def census() -> dict[str, Any]:
    state = readiness()
    return {
        "registryVersion": TRANSPORT_REGISTRY_VERSION,
        "productionKey": PRODUCTION_TRANSPORT_KEY,
        "registeredKeys": list(registered_keys()),
        "productionReady": state.ready,
        "rawStateSource": {"table": "mhz_publish_order_items", "column": "status"},
    }
