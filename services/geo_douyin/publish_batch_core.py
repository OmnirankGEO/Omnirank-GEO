"""图文投放 command 的**单一产线**(WO-B ② · 规格 02 §7.3 / §8.2)。

## 为什么把它从 handler 里搬出来

在这之前,「claim → 审批门 → 资格/定价 → 总价确认锁 → artifact/锁定 revision +
广告法闸 → 逐项 freeze → 建单」这一整条**只存在于**
``api/geo_image_note_api.api_image_note_publish_batch`` 的闭包里。

小榜五阶段的 execute adapter 要接的就是这一条。摆在面前的两条路:

1. 在 execute 那边**再写一遍**同样的顺序 —— 那就是两份资金链。
   本仓已经为这件事付过费:同一谓词写两处,必有一处没人验,
   而没人验的那一处一旦漂移,表现是"扣了钱但没建单"或"建了单但没冻钱";
2. 把这一条搬进一个**双方共用**的函数。

选 2。搬家是**纯移动**:顺序、异常类型、每一句 SQL 一个字没改;
handler 保留它自己的 HTTP 错误映射(那一层本来就该留在 handler)。

## 🔴 provider 只能在 commit 之后

本模块与 ``publish_coordinator`` 同一条纪律:**不发起任何外部调用**。
它在调用方的事务里跑完,由调用方决定何时 commit。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence


def publish_price_resolver(markup: Optional[float] = None) -> Callable[[dict], dict]:
    """投放价 resolver —— **全链唯一一份**。

    🔴 投放价只复用 ``media_price_projection.resolve_price_points``
       (那个函数自己的 docstring 写着「本函数是那处的统一口径」——
       ``publish_recommendation`` 曾漏乘 markup 少收钱)。
       同一 resolver 同时服务:目录显示 / 筛选排序 / ``/production-preview`` /
       正式下单 / 小榜五阶段报价与执行(规格 §6.2)。

    🔴 抽成函数的直接理由:在这之前它是**两份**内联 lambda
       (``api/geo_image_note_api.py`` 的 preview 与 publish-batch 各一份),
       而小榜那边还要第三份。第三份一出现,「prepare 报的价」与「execute 冻的钱」
       就会各算各的 —— 那正是资金判据要打的那一格。
    """
    from services.media_price_projection import get_markup, resolve_price_points

    resolved_markup = get_markup() if markup is None else float(markup)

    def _resolve(row: dict) -> dict:
        return {
            "final_price_points": resolve_price_points(row, resolved_markup),
            "price_version": "markup:{0}".format(resolved_markup),
        }

    return _resolve


def publish_item_fingerprint(*, media_id: int, final_price_points: int,
                             price_version: Any, feature_code: str) -> str:
    """逐项投放价指纹 —— **全链唯一一份构造**。

    🔴 抽出来的理由和 :func:`publish_price_resolver` 一样:小榜那边要在 prepare
       冻结一个指纹、在 execute 用它去比。两处各拼一遍的话,
       「prepare 冻的」和「execute 算的」会因为某个参数写法不同而永远不相等,
       于是那条价格漂移锁要么恒红要么被人删掉。
    """
    from services.geo_douyin.contract_pricing import publish_fingerprint

    return publish_fingerprint(
        feature_code=feature_code, media_id=int(media_id),
        final_price_points=int(final_price_points),
        markup_version=str(price_version or ""),
        resolver_version="media_price_projection.v1",
        catalog_version="mhz_short_video")


def materialize_publish_batch(
    cur,
    *,
    request_id: str,
    endpoint: str,
    identity: Mapping[str, Any],
    settlement: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    expected_total_price_points: int,
    daily_limit: int,
    feature_code: str,
    command_id: Optional[str] = None,
) -> dict:
    """在**调用方的事务**里跑完 §8.2 全序。返回 ``{"replayed": ...}``。

    异常一律用发布域既有类型往外抛(``IdempotencyConflict`` / ``PriceChanged`` /
    ``ArtifactRequestConflict`` / ``ApprovalPending`` / ``LegalGateBlocked`` /
    ``CapacityExceeded`` / ``FundingHandleInvalid`` / ``FreezeProducedNoHandle``),
    由调用方翻成自己那一层的错误形状 —— 本模块不认识 HTTP。

    🔴 ``items`` 会被**就地补全**(title / body_text / brand_id / image_urls),
       与搬家前的行为逐字一致:``materialize_command`` 依赖这几个键。
    """
    from services.geo_douyin.account_eligibility import evaluate_accounts
    from services.geo_douyin.artifact_prepare import ArtifactRequestConflict
    from services.geo_douyin.contract_approval import ACTION_PUBLISH, require_approval
    from services.geo_douyin.contract_freeze import freeze_one_item
    from services.geo_douyin.contract_idempotency import (
        claim_request, publish_request_payload, request_hash,
    )
    from services.geo_douyin.contract_pricing import (
        OPERATION_PUBLISH, PriceChanged, canonical_price_snapshot,
    )
    from services.geo_douyin.legal_gate import assert_outbound_clean
    from services.geo_douyin.publish_command import CapacityExceeded
    from services.geo_douyin.publish_coordinator import materialize_command

    item_dicts = [dict(item) for item in items]
    media_ids = sorted({int(item["media_id"]) for item in item_dicts})

    payload = publish_request_payload(
        items=item_dicts,
        expected_total_price_points=expected_total_price_points)
    rhash = request_hash(owner_user_id=int(identity["tenant_owner_user_id"]),
                         endpoint=endpoint, payload=payload)

    # ① 原子 claim(同一事务;零行即回放/冲突)
    is_new, _row = claim_request(
        cur, request_id=request_id, endpoint=endpoint,
        owner_user_id=int(identity["tenant_owner_user_id"]),
        expected_hash=rhash,
        identity={**dict(identity),
                  "principal_user_id": identity["tenant_owner_user_id"],
                  "payer_policy_snapshot_json": None},
        command_id=command_id or ("pubcmd_" + str(request_id)))
    if not is_new:
        return {"replayed": True}

    # ② 组织审批门(§7.3)。与制作链同一位置口径:claim 之后、
    #    资格/容量/冻结/建单之前。
    require_approval(
        cur, identity=identity, action_type=ACTION_PUBLISH,
        estimated_points=int(expected_total_price_points),
        feature_code=feature_code, payload_hash=rhash)

    # ③ 服务端重跑资格 + 定价(客户端报的价一律不信)
    elig = evaluate_accounts(
        cur, media_ids, daily_limit=int(daily_limit),
        price_resolver=publish_price_resolver())
    names: dict = {}
    priced: dict = {}
    for item in item_dicts:
        account = elig.get(int(item["media_id"]))
        if account is None or not account.available:
            reason = account.reason_code if account else "ACCOUNT_NOT_FOUND"
            raise CapacityExceeded(
                "账号 " + str(item["media_id"]) + " 当前不可用:" + str(reason))
        names[int(item["media_id"])] = ""
        points = int(account.final_price_points or 0)
        priced[item["item_request_id"]] = {
            "final_price_points": points,
            "publish_price_fingerprint": publish_item_fingerprint(
                media_id=int(item["media_id"]), final_price_points=points,
                price_version=account.price_version, feature_code=feature_code),
            "price_snapshot": canonical_price_snapshot(
                operation=OPERATION_PUBLISH, final_price_points=points,
                feature_code=feature_code,
                resolver_version="media_price_projection.v1",
                catalog_version="mhz_short_video",
                markup_version=str(account.price_version or ""),
                policy_version=None, preview_expires_at="", confirmed_at=None),
        }

    # ④ 发布**总价确认锁**。`expected_total_price_points` 只进 request_hash 的话,
    #    那只保证「同一 request_id 两次提交内容一致」,**不保证**服务端算出来的
    #    总价与用户按下按钮时看到的那个数一致。价目在预览与提交之间改了,
    #    用户会按旧价确认、按新价扣款。不一致就零冻结零建单,让用户重新确认。
    _server_total = sum(int(v["final_price_points"]) for v in priced.values())
    if _server_total != int(expected_total_price_points):
        raise PriceChanged(
            "服务端总价 {0} 与确认时的 {1} 不一致".format(
                _server_total, int(expected_total_price_points)))

    # ⑤ artifact 身份 + **锁定版本**的外发内容 + 广告法闸,一趟做完。
    #    扫的是**锁定的 revision**,不是 `geo_douyin_posts` 的当前列 ——
    #    两者在"用户改了文案又没重新准备素材"这一刻就不是同一份 ⇒「审 A 发 B」。
    #    `geo_post_id` 也要逐项核:只比 revision 与 manifest 的话,同租户内
    #    可以拼出「B 的 post + A 的 artifact」。
    artifacts: dict = {}
    for item in item_dicts:
        cur.execute(
            "SELECT prepared_artifact_id, geo_post_id, post_revision_id,"
            "       manifest_hash, state, card_statuses"
            "  FROM geo_douyin_publish_artifacts "
            " WHERE prepared_artifact_id = %s AND tenant_owner_user_id = %s",
            (int(item["prepared_artifact_id"]),
             int(identity["tenant_owner_user_id"])))
        row = cur.fetchone()
        if row is None:
            raise ArtifactRequestConflict(
                "找不到作品 " + str(item["geo_post_id"]) + " 的已准备发布素材")
        artifact = dict(row)
        if int(artifact.get("geo_post_id") or 0) != int(item["geo_post_id"]):
            raise ArtifactRequestConflict(
                "这份发布素材属于另一篇作品(" + str(artifact.get("geo_post_id"))
                + "),不能用来发布作品 " + str(item["geo_post_id"]))
        artifacts[item["item_request_id"]] = artifact

        cur.execute(
            "SELECT r.title, r.body, r.status,"
            "       p.brand_id, p.keyword, b.name AS brand_name"
            "  FROM geo_douyin_post_revisions r"
            "  JOIN geo_douyin_posts p ON p.id = r.geo_post_id"
            "  LEFT JOIN brands b ON b.id = p.brand_id"
            " WHERE r.post_revision_id = %s AND r.geo_post_id = %s",
            (int(item["post_revision_id"]), int(item["geo_post_id"])))
        rev = dict(cur.fetchone() or {})
        if not rev:
            raise ArtifactRequestConflict(
                "找不到作品 " + str(item["geo_post_id"]) + " 的这一版成品,请重新确认")
        if str(rev.get("status") or "") != "active":
            raise ArtifactRequestConflict(
                "作品 " + str(item["geo_post_id"]) + " 已产生新版本,请重新准备并确认")
        assert_outbound_clean({"title": str(rev.get("title") or ""),
                               "body_text": str(rev.get("body") or "")})
        item["title"] = str(rev.get("title") or "")
        item["body_text"] = str(rev.get("body") or "")
        item["brand_id"] = rev.get("brand_id")
        item["customer_name"] = str(rev.get("brand_name") or "")
        item["image_urls"] = ",".join(
            str(c.get("url") or "")
            for c in (artifact.get("card_statuses") or [])
            if str(c.get("state") or "") == "ready" and c.get("url"))

    def _freeze(*, payer_user_id: int, amount: int, task_ref: str) -> dict:
        # 🔴 协调器是同步的,而 freeze_points 是 async。这里在**当前线程**
        #    起一个独立 loop 跑这一笔 —— cursor 仍是同一个,事务边界不变。
        #    绝不换连接:换了就不在同一事务里,异常时回滚不掉冻结。
        #
        # 🔴🔴 [R2 · Owner 2026-08-24 批「修」· 存量 P0] ``extra_cost=amount`` 是**必须**的。
        #    在这之前这里不传 extra_cost,后果是整条链在生产上从来没成功过:
        #      · ``media_proxy_publish`` 是**动态定价** SKU ——
        #        ``feature_pricing.cost_points = 0``(db/wallet_db.py:377,
        #        Review 2026-08-24 生产只读实证 = 0),价钱**整个**来自媒体目录
        #        (``mhz_short_video.our_price_points × markup``,见
        #        :func:`publish_price_resolver`),不在 feature_pricing 里;
        #      · 于是 ``middleware/billing.freeze_points`` 里
        #        ``total_cost = pricing["cost_points"] + extra_cost = 0 + 0 = 0``;
        #      · ``total_cost == 0`` 在**开事务之前**就 return
        #        ``{"freeze_id": None, "free": True}``;
        #      · ``contract_freeze.freeze_one_item`` 不接受「零句柄的成功」⇒
        #        ``FreezeProducedNoHandle`` ⇒ execute 侧翻成
        #        ``SETTLEMENT_HANDLE_INVALID`` **HTTP 500**。
        #    ⇒ Owner 的「先示预估、用户确认、必收费」在这条链上一次都没成立过。
        #
        #    传**全额**而不是差额,是这个 SKU 在本仓的既有口径,不是我新造的:
        #      · ``api/meijiehezi_api.py:939 / 1723 / 2539``(现役、真在收钱)
        #        都是 ``deduct_points(user_id, "media_proxy_publish",
        #        extra_cost=total_cost_points)`` —— 全额走 extra;
        #      · ``tests/defensive_geo_w3_2026_08_21/conftest.py`` 逐字写着
        #        「价格设 0(现役就是 0),exact points 全走 extra_cost」。
        #      · 同族 ``api/geo_image_note_api.py:791`` 传的是
        #        ``_calc["extra_points"]``,因为 ``geo_douyin_image_post``
        #        那个码的目录价**非 0**,差额才是 extra。两处形态一致:
        #        **extra = 确认价 − 目录基价**,这个 SKU 的基价恰好是 0。
        #    ⇒ 冻结额 == 用户在确认屏上看到的那个数(``expected_points`` 同源),
        #      而 ``freeze_one_item`` 内部还会再核一次 amount != expected_points。
        #
        #    ⚠️ 基价若被改成非 0:``total = base + amount != amount`` ⇒
        #      ``FundingHandleInvalid`` ⇒ **响亮拒单、一分不冻**,
        #      不会静默多扣。判据
        #      ``test_a_nonzero_catalog_base_refuses_loudly_instead_of_overcharging``
        #      钉住这一条。
        import asyncio as _a
        loop = _a.new_event_loop()
        try:
            return loop.run_until_complete(freeze_one_item(
                cur, payer_user_id=payer_user_id,
                feature_code=feature_code, task_ref=task_ref,
                brand_id=None, expected_points=amount,
                extra_cost=int(amount),
                authority=str(settlement["authority"])))
        finally:
            loop.close()

    result = materialize_command(
        cur, command_request_id=request_id, identity=identity,
        items=item_dicts, resolved_prices=priced, artifacts=artifacts,
        account_names=names, daily_limit=int(daily_limit),
        settlement=settlement, freeze_fn=_freeze,
        feature_code=feature_code)
    out = {"replayed": False}
    out.update(result)
    return out


__all__ = ["materialize_publish_batch", "publish_item_fingerprint",
           "publish_price_resolver"]
