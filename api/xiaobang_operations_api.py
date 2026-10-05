"""小榜受控操作网关 · 五阶段端点(规格 §10 · WP1 骨架)。

## 这一轮到哪儿为止(说清楚,不含糊)

WP1 的门是**零算力、零 provider、零业务写**。因此:

* ``query`` / ``prepare`` / ``GET intent`` / ``confirm`` / ``cancel`` / ``status``
  是**真的**:真鉴权、真幂等、真 CAS、真 DLP、真双时钟;
* ``execute`` **[WO-B ② · 2026-08-20 起] 跑到底**:身份 → 对象归属 → revision →
  状态 → 漂移 → 确认档 全部按最终顺序跑完,然后**消费回执**并调用领域 adapter。
  🔴 检查顺序一个字没动:那样「external 类没确认不执行」这条判据仍然打在真实路径上,
     不会被一个更早的错误吞掉。
  🔴 回执**到最后一刻才消费**:它是单次消费品,前置没过就烧掉等于让一次注定
     失败的调用把用户的确认作废。所以 ``_assert_confirmation_present`` 仍然是
     「只验不消费」,``consume_confirmation_receipt`` 在全部前置通过之后才调。

execute 的领域侧走 :mod:`services.xiaobang_publish_execute`,它内部调
:func:`services.geo_douyin.publish_batch_core.materialize_publish_batch` ——
与 ``POST /api/meijiehezi/image-notes/publish-batch`` **同一条产线**。
本模块不 import ``middleware.billing``、不解析 payer、不写任何钱包表:
「不新开资金路径」在物理上就是这一条。

## 为什么不进 OpenAPI

全部路由 ``include_in_schema=False``(§10 · P1-8)。默认 ``/docs`` 会枚举内部
路由,而本网关的路径本身就带 operation 语义。外部网关另有分层计划(WP5)。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services import gap_operation_map as omap
from services.defensive_geo.xiaobang.drift_notice import drift_notice
from services.defensive_geo.xiaobang.compute_estimate import (
    DOMAIN_BLOCKER_KEY as ESTIMATE_DOMAIN_BLOCKED_KEY,
    EstimateNotShown,
    assert_estimate_shown,
    build_public_estimate,
    estimate_next_action,
)
from services.defensive_geo.xiaobang.receipt_guard import (
    ReceiptGuardError,
    assert_bind_fields_cover_por13,
    assert_receipt_usable,
)
from services.xiaobang_command_contract import (
    SIDE_EFFECT_COMPUTE_ONLY,
    SIDE_EFFECT_EXTERNAL,
)
from services.xiaobang_facade_dlp import CAPABILITIES_FACADE, assert_facade_clean
from services.xiaobang_page_prefill import build_prefill
from services.xiaobang_intent import (
    ActorBinding,
    ComputeQuote,
    IntentError,
    IntentNotFound,
    STATE_APPROVAL_PENDING,
    STATE_AWAITING_CONFIRMATION,
    STATE_CANCELLED,
    STATE_CONFIRMED,
    STATE_EXECUTABLE,
    STATE_EXECUTION_LINKED,
    STATE_EXPIRED,
    STATE_PREPARED,
    cancel_intent,
    canonical_input,
    consume_confirmation_receipt,
    content_hash,
    drift_reason,
    issue_confirmation_receipt,
    link_execution,
    load_intent,
    rebind_receipt_to_revision,
    mark_awaiting_confirmation,
    mark_confirmed,
    prepare_intent,
    quote_expired,
    status_projection,
)

logger = logging.getLogger("GEO-XiaobangOps")

router = APIRouter(prefix="/api/xiaobang/operations", tags=["小榜受控操作"])

#: prepare 允许的选择字段。服务端按这张表重建 canonical input,
#: 客户端多塞的键在此消失 —— 允许它进 hash 就等于让客户端左右幂等边界。
_SELECTION_ALLOWLIST: tuple[str, ...] = (
    "brand_id", "quote_id", "article_id", "content_id",
    "publication_id", "monitoring_task_id", "channel_option_id",
    "geo_post_id",                      # [R3-P11 ①] GEO 图文
)

#: 只有登记了 command_contract 的 operation 才有五阶段。
#: 没登记 = 它只是个导航项,连存在性都不该在这里被确认。


# ── 请求模型 ──────────────────────────────────────────────────────────────
class QueryRequest(BaseModel):
    contract_version: str = "1"
    interaction_id: Optional[str] = None
    context_refs: dict = Field(default_factory=dict)
    filters: dict = Field(default_factory=dict)


class PrepareRequest(BaseModel):
    contract_version: str = "1"
    interaction_id: Optional[str] = None
    prepare_request_id: str = Field(min_length=8, max_length=128)
    selection: dict = Field(default_factory=dict)


class ConfirmRequest(BaseModel):
    intent_revision: int = Field(ge=1)
    interaction_id: Optional[str] = None
    user_action_challenge: str = Field(default="", max_length=256)


class CancelRequest(BaseModel):
    expected_intent_revision: int = Field(ge=1)
    cancel_request_id: str = Field(min_length=8, max_length=128)


class ExecuteRequest(BaseModel):
    intent_revision: int = Field(ge=1)
    execution_request_id: str = Field(min_length=8, max_length=128)


# ── 公共辅助 ──────────────────────────────────────────────────────────────
def _require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return dict(user)


def _actor_user_id(user: dict) -> int:
    """当前身份的 user id。

    🔴 [WO-B 2026-08-20 · 与 capabilities 同一天查出来的第二个洞]
       鉴权中间件写进 ``request.state.user`` 的键是 **``user_id``**
       (``auth/jwt_utils.create_jwt`` 的 payload / ``auth/middleware.py`` 的
       ``user_data_source``),**没有 ``id``**。而本模块原来一律写 ``user["id"]``
       ⇒ 生产上 ``_actor_binding`` 抛 ``KeyError: 'id'`` ⇒ **除 capabilities 之外的
       全部五阶段端点 500**(capabilities 恰好不调 ``_actor_binding``,
       所以它先以 DLP 的形态被发现)。

       判据一直是绿的,因为夹具自己造的是 ``{"id": 601}`` ——
       「夹具替被测代码干活」的教科书形态:夹具用的键**生产从来不会发**。
       同仓 ``api/xiaobang_api.py:55`` 早就写着 ``user.get("user_id") or user.get("id")``,
       也就是说正确写法在隔壁文件里躺着。

       两个键都认,取不到就 **401 而不是 KeyError→500**:身份取不出来是
       「你没登录」,不是「服务器坏了」。
    """
    raw = user.get("user_id")
    if raw in (None, ""):
        raw = user.get("id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="未登录")


def _resolve_entry(operation_id: str):
    """取 operation 并要求它有命令合同。

    🔴 无合同 / 不存在 —— 返回**同一个** 404。区分这两者等于告诉调用方
       「这个 operation 存在但你用不了」,那是存在性泄漏(§8.2)。
    """
    entry = omap.resolve_operation(operation_id)
    if entry is None or entry.command_contract is None:
        raise HTTPException(status_code=404, detail={
            "error_code": "OPERATION_NOT_FOUND",
            "message": "这个操作不可用。",
            "next_action": "回到小榜重新开始",
        })
    return entry


def _authorize(request: Request, entry) -> None:
    """能力发现之上的第二道:当前身份是否真的能用这个 operation。"""
    user = _require_user(request)
    identity = getattr(request.state, "organization_identity", None)
    if not omap.is_operation_allowed(entry, user, identity=identity):
        # 同样不回显存在性。
        raise HTTPException(status_code=404, detail={
            "error_code": "OPERATION_NOT_FOUND",
            "message": "这个操作不可用。",
            "next_action": "联系有权限的人",
        })
    contract = entry.command_contract
    is_member = bool(getattr(identity, "is_member", False))
    if is_member and not user.get("is_admin"):
        capabilities = frozenset(
            str(v) for v in (getattr(identity, "capabilities", ()) or ())
        )
        if contract.required_capability not in capabilities:
            raise HTTPException(status_code=404, detail={
                "error_code": "OPERATION_NOT_FOUND",
                "message": "这个操作不可用。",
                "next_action": "请负责人确认",
            })


def _actor_binding(request: Request, context) -> ActorBinding:
    """按**当前请求**取身份与授权代际快照,绝不从 intent 行反读。"""
    user = _require_user(request)
    identity = getattr(request.state, "organization_identity", None)
    actor_user_id = _actor_user_id(user)
    # 🔴 [工单 V3-A · Codex 三审 P1-9 · 2026-08-28] 租户 owner 取**组织 principal**。
    #
    #    上一版是 `int(getattr(context, "owner_user_id", 0) or actor_user_id or 0)`,
    #    并且代码里自曝了「已知边界」。那个 getattr **恒取到 0** ——
    #    现役 `services.customer_operation_plan.AuthorizedAssistantContext` 里
    #    `owner_user_id` 出现 **0 次**(我机械核过)。于是 tenant_owner_id 恒等于
    #    操作者本人:老板自己操作时两值本就相等所以无感,而**组织席位代操作**时
    #    真正的租户是老板,账本却把员工冻成了租户 —— intent/执行归属整条错位,
    #    含 execute 的 `WHERE tenant_owner_id=%s`。
    #
    #    🔴 披露不等于可发车:注释写清楚了不改变这条链在生产上是错的。
    #
    #    解析走 `auth.principal_identity.resolve_principal_user_id` —— 全仓
    #    **那一个**口径(非组织员工、含组织 owner 本人,一律回落 fallback,
    #    行为与个人场景完全一致)。不在这里另写一份 `identity.principal_user_id`
    #    的取法:同一谓词写两处必有一处没人验。
    from auth.principal_identity import resolve_principal_user_id

    principal_user_id, _is_org_member = resolve_principal_user_id(
        request, fallback_user_id=actor_user_id)
    tenant_owner_id = int(principal_user_id or actor_user_id or 0)
    payer_user_id = getattr(identity, "payer_user_id", None) or tenant_owner_id
    return ActorBinding(
        actor_user_id=actor_user_id,
        tenant_owner_id=tenant_owner_id,
        payer_user_id=int(payer_user_id) if payer_user_id else None,
        organization_id=getattr(identity, "organization_id", None),
        membership_version=_str_or_none(getattr(identity, "membership_version", None)),
        assignment_authority_version=_str_or_none(
            getattr(identity, "assignment_authority_version", None)
        ),
        approval_policy_version=_str_or_none(
            getattr(identity, "approval_policy_version", None)
        ),
        permission_version=_str_or_none(getattr(request.state, "permission_version", None)),
    )


def _str_or_none(value: Any) -> Optional[str]:
    return None if value is None else str(value)


#: [R3-P11 ①] 冻结对象清单里的 ``resource_kind`` → 恢复授权用的 ref 字段。
#: 单一出处:``_display_object_items`` 产出这些 kind,本表把它们读回来,
#: 两边对不上就是「冻结时写了一种对象、恢复时认不出它」—— 那正是空 refs 的翻版
#: (对象在那儿,但没人拿它去问「还归不归你」)。判据 test_every_frozen_object_kind_recovers。
_OBJECT_KIND_TO_REF_FIELD: dict[str, str] = {
    "brand": "brand_id",
    "quote": "quote_id",
    "article": "article_id",
    "geo_image_post": "geo_post_id",
}


def _refs_from_frozen_intent(row) -> dict:
    """[R3-P8 ②] 从**冻结的 intent** 恢复对象 refs,交给现役授权函数重验。

    🔴 修的是一个真洞:恢复类端点(prefill / GET intent / confirm / cancel /
    execute / status)原来一律 ``_authorized_context(request, {})`` —— **空 refs**。
    空 refs 意味着 ``resolve_authorized_context`` 没有对象可校验,于是
    「这个客户还归不归你」这件事**在这些端点上根本没被问过**;
    客户被撤销分配之后,员工照样能把 intent 恢复出来、照样能 confirm。

    refs 只能来自**服务端冻结的那份**(preview.form_prefill / object_items),
    不能来自请求 —— 从请求拿等于让调用方自己声明"我要校验哪个对象"。
    """
    preview = dict((row or {}).get("preview") or {})
    refs: dict = {}
    for field, value in (preview.get("form_prefill") or {}).items():
        if field in _SELECTION_ALLOWLIST and value not in (None, "", 0):
            refs[field] = value
    # object_items 是 prepare 时冻结的对象清单,作为 form_prefill 的补充来源。
    for item in preview.get("object_items") or []:
        kind = str((item or {}).get("resource_kind") or "")
        ref = (item or {}).get("ref")
        field = _OBJECT_KIND_TO_REF_FIELD.get(kind)
        if field and field in _SELECTION_ALLOWLIST and ref and field not in refs:
            refs[field] = ref
    return refs


def _authorized_context(request: Request, context_refs: dict):
    """对象级鉴权。跨租户/已撤权/猜 id —— 由该函数统一 404,不回显名称或 id。"""
    from services.customer_operation_plan import resolve_authorized_context

    return resolve_authorized_context(request, context_refs or {}, "")


#: manifest 里**还没覆盖**的输入项。抽成常量是为了让 ③ 的「核过就摘掉」
#: 有一个单一出处 —— 两处各写一份列表的话,摘的和留的会各算各的。
_PENDING_MANIFEST_INPUTS: tuple[str, ...] = (
    "channel_eligibility", "rate_limit", "inventory_version",
)


def _object_manifest(context, selection: dict, resource_kind: str | None = None,
                     channel=None, *, tenant_owner_id: int) -> dict:
    """服务端 canonical manifest 的 WP1 覆盖面。

    覆盖 tenant owner / brand / quote / article / 版本水位。渠道资格、频控、
    库存版本要等发布域 adapter 接入后补(§10.2 列的完整面)。
    这里**不**假装已经覆盖全 —— manifest 少一项,漂移就少一条判据,
    所以缺哪一项要说出来,而不是让它静默地不在 hash 里。
    """
    public = context.public_context() if hasattr(context, "public_context") else {}
    return {
        "manifest_scope": "wp1_identity_and_object_refs",
        # [R3-P11 ②] 合同登记的对象类型(不是猜的)——目标渠道那一栏要用它说明
        # 「这条动作往哪种载体上发」。由调用方传入,见 _object_manifest 签名。
        "object_resource_kind": resource_kind,
        "covered": ["tenant_owner", "brand", "quote", "article", "geo_image_post",
                    "updated_watermark"],
        # 🔴 [WO-B ③] ``channel_eligibility`` 不再是恒 pending:渠道项能解析成
        #    一个真账号时,资格**这一轮就核过了**,它要从 pending 移到 covered。
        #    ``channel=None``(没传解析结果)时逐字节回到旧行为 —— 既有判据
        #    ``test_channel_reports_eligibility_as_unverified_while_it_is_pending``
        #    走的就是这一支,它的前提断言因此仍然成立。
        "pending": (channel.pending_inputs(_PENDING_MANIFEST_INPUTS)
                    if channel is not None else list(_PENDING_MANIFEST_INPUTS)),
        "covered_channel_media_id": (channel.media_id if channel is not None else None),
        # 🔴 [工单 V3-A · Codex 三审 P1-9] 上一版是
        #    ``getattr(context, "owner_user_id", None)`` —— 现役 context 类上
        #    **没有**这个字段(机械核过:出现 0 次)⇒ 这一格恒 None,
        #    manifest 里「租户是谁」这一栏从上线起就是空的,而它被算进
        #    ``object_manifest_hash``:也就是说漂移检测对「换了租户」零判别。
        #    现在由调用方把 ``_actor_binding`` 现算的租户传进来(**必传关键字**,
        #    不给默认值:给了默认值就等于允许下一个调用方悄悄漏掉它)。
        "tenant_owner_id": int(tenant_owner_id) if tenant_owner_id else None,
        "brand_id": public.get("brand_id"),
        # 🔴 [R3-P11 ①] 三个对象**对称**地「已授权上下文优先、canonical selection 兜底」。
        #    存量缺陷(本轮三对象矩阵抓出来的):`public_context()` 从来不暴露
        #    `article_id` —— 于是 `_display_object_items` 里那一支恒取不到值,
        #    **文章从来没进过 object_items**。也就是说「当前对象」这一栏在发文章时
        #    只显示得出客户与报价,唯独不显示那篇文章(§12.3 要求当前对象同屏可见)。
        #    单对象夹具看不见这件事,因为它只断言 object.label 非空 —— 而客户那一条
        #    就让它非空了。
        #    兜底取 selection 是安全的:`operation_prepare` 里
        #    `context = _authorized_context(request, payload.selection)` 已经把
        #    每一个 ref 逐个重验过,没过的在那一步就 404 了。
        "quote_id": public.get("quote_id") or selection.get("quote_id"),
        "article_id": public.get("article_id") or selection.get("article_id"),
        "geo_post_id": public.get("geo_post_id") or selection.get("geo_post_id"),
        "data_updated_at": public.get("data_updated_at"),
        "selection": {k: selection.get(k) for k in _SELECTION_ALLOWLIST
                      if selection.get(k) not in (None, "")},
    }


#: [R3-P8 ①] 允许冻结进 form_prefill 的字段。与 ``_SELECTION_ALLOWLIST`` 同源:
#: 前端要填的就是这几个选择项,多一个键都不该由服务端塞给页面。
_FORM_PREFILL_FIELDS: tuple[str, ...] = (
    "brand_id", "quote_id", "article_id",
    "geo_post_id",                  # [R3-P11 ①] PublishCenter 的 geo post 选择
)


def _frozen_form_prefill(context, canonical: dict) -> dict:
    """页面要填的那几个字段,取自**已过对象级鉴权**的 canonical selection。

    🔴 只从 canonical 取,不从请求体原样搬:canonical 是服务端按 allowlist 重建的
    (客户端多塞的键在那一步就没了)。从请求体搬等于让客户端决定页面被填成什么。

    brand_id 允许从已鉴权上下文补 —— 它是 ``resolve_authorized_context`` 校验过的
    那个客户,不是浏览器提示的那个。
    """
    selection = dict((canonical or {}).get("selection") or {})
    public = context.public_context() if hasattr(context, "public_context") else {}
    out: dict = {}
    for field in _FORM_PREFILL_FIELDS:
        value = selection.get(field) or public.get(field)
        if value not in (None, "", 0):
            out[field] = value
    return out


def _frozen_channel(context, canonical: dict, manifest: dict, channel=None) -> dict:
    """[R3-P11 ②] 目标渠道/账号,随 intent 一起冻结。

    ## 🔴 先说清楚:这一项在本包之前**从来没有被写过**

    工单说「后端 prefill.channel 已返齐」。实测不是:
    ``operation_prepare`` 写进 ``preview`` 的键只有 5 个
    (``customer_label`` / ``scope`` / ``pending_manifest_inputs`` /
    ``object_items`` / ``form_prefill``),没有 ``channel``;
    ``build_prefill`` 那句 ``preview.get("channel") or {}`` 于是**恒返空字典**。
    也就是说:只在前端加一段渲染,得到的是一个**永远走降级文案**的死元素。
    所以本包把 producer 这一半补上 —— 与 R3-P8 ① 修 ``form_prefill`` 是同一个形态
    (「夹具里有、生产端一个字没写」)。

    ## 能说的与不能说的

    渠道资格(``channel_eligibility``)仍在 manifest 的 **pending** 里 ——
    投放账号资不资格、频控、库存版本要等发布域 adapter 接入才算得准。
    所以这里**不编账号名**:只说三件我们此刻真知道的事 ——

    * ``channel_option_id``:调用方选的渠道项(已过 allowlist 的 canonical selection)。
      🔴 键名**刻意**用全名而不是 ``option_id``:DLP-A 按**叶子键名**决定这一格
      能不能放机读值,而 ``channel_option_id`` 早已在机读白名单里。
      真 HTTP 实测:叫 ``option_id`` 时 ``GET /prefill`` 直接 500
      (``$.channel.option_id: 未翻译的内部枚举 'douyin_main'``)。
      复用已登记的机读键,**不去把共享白名单改宽** —— 与 R3-P7 用
      ``next_action_id`` 而不是新加 ``kind`` 是同一条纪律;
    * ``resource_kind``:这条 operation 的对象类型(合同里登记的,不是猜的);
    * ``verified``:渠道资格**有没有**核过。今天恒为 ``False``,
      因为 ``channel_eligibility`` 就在 pending 里 —— 与其让页面显示一个
      「看起来已确认」的账号,不如如实说还没核。

    拿不到就返回**空字典**,由前端走降级文案(判据把降级那一档钉死了),
    绝不返回 ``{"label": ""}`` 之类的空串 —— 空串会在页面上渲染成"有这一项、
    但它是空的",那比"还没核"更误导。
    """
    # 🔴 [WO-B ③] 传进来解析结果就用真值。``channel=None`` 时**逐字节**回到
    #    R3-P11 的那一版(``verified`` 跟着 manifest.pending 走,不编账号名)。
    if channel is not None:
        return channel.as_channel_dict(
            resource_kind=manifest.get("object_resource_kind"))
    selection = dict((canonical or {}).get("selection") or {})
    option_id = selection.get("channel_option_id")
    pending = list(manifest.get("pending") or [])
    out: dict = {
        "resource_kind": manifest.get("object_resource_kind"),
        "verified": "channel_eligibility" not in pending,
    }
    if option_id not in (None, "", 0):
        out["channel_option_id"] = option_id
    return {k: v for k, v in out.items() if v is not None}


def _display_object_items(context, manifest: dict) -> list[dict]:
    """[R3-P7 ③] 从**已授权**的上下文与 manifest 拼可展示对象清单。

    只用已经过对象级鉴权的字段;拿不到名字就退回「<类型> #<id>」,**不编名字**——
    编一个名字会让用户以为自己选的是别的对象。
    """
    public = context.public_context() if hasattr(context, "public_context") else {}
    out: list[dict] = []
    if public.get("brand_id"):
        out.append({"resource_kind": "brand", "ref": public.get("brand_id"),
                    "label": public.get("brand_name")})
    for kind, key in (("quote", "quote_id"), ("article", "article_id"),
                      ("geo_image_post", "geo_post_id")):     # [R3-P11 ①]
        ref = manifest.get(key) or public.get(key)
        if ref:
            out.append({"resource_kind": kind, "ref": ref, "label": None})
    return out


def _resolve_compute_quote(cursor, contract) -> tuple[Optional[ComputeQuote], dict]:
    """算力报价。

    🔴 **同一个 resolver**:这里调的 ``db.wallet_db.get_feature_pricing`` 就是
       ``middleware/billing.py`` 扣费时调的那一个(同函数、同 cursor)。
       规格 §10.2:不能 prepare 用目录价、execute 再走另一套公式。

    🔴 拿不到目录价时**返回空报价 + 说明**,不编数字。发布类的真实算力取决于
       所选媒体,是发布域 adapter 的职责;在它接进来之前给一个平价数字,
       比不给数字坏得多 —— 用户会按那个数字做决定。
    """
    feature = getattr(contract, "billing_feature", None)
    if not feature:
        return None, {"quote_state": "not_applicable"}
    from db.wallet_db import get_feature_pricing

    try:
        pricing = get_feature_pricing(feature, cursor=cursor)
    except ValueError:
        return None, {
            "quote_state": "unavailable",
            "quote_note": "这个功能的算力价格暂时取不到,先不显示数字。",
        }
    cost = pricing.get("cost_points")
    if cost is None:
        return None, {"quote_state": "unavailable"}
    if contract.side_effect == SIDE_EFFECT_EXTERNAL:
        # 发布类:目录价只是**下限**,真实算力取决于所选媒体/账号。
        # 给一个"看起来精确"的数字会误导决策,所以这一档在发布域 adapter
        # 接入前一律不出数字(§P2-1 允许降级预览,不允许假报价)。
        return None, {
            "quote_state": "pending_domain_adapter",
            "quote_note": "具体算力要按你选的投放账号算,打开核对页后显示。",
        }
    return (
        ComputeQuote(
            amount=int(cost),
            pricing_version=str(pricing.get("updated_at") or pricing.get("feature_code")),
        ),
        {"quote_state": "quoted"},
    )


def _channel_compute_quote(quote, quote_meta: dict, channel):
    """[WO-B ③] 渠道核出来之后,发布类**给得出真实算力**。

    在这之前 ``_resolve_compute_quote`` 对 ``side_effect=external`` 一律返回
    ``pending_domain_adapter``(不出数字),理由写在那个函数里:
    「真实算力取决于所选媒体,给一个平价数字比不给数字坏得多」。
    那个理由现在被 ③ 解掉了 —— 媒体已经选定、资格已核、价格来自与下单
    **同一个** resolver。所以这一档可以、也必须出数字:
    §12.3 要求主按钮写「确认并执行 · 使用 X 算力」,而 execute 冻的就是这个 X。

    🔴 只在**核过且可用且有价**时覆盖。不可用的账号不给价 ——
       给了就会有人拿它去显示"要花 X 算力",而这一笔根本发不出去。
    🔴 写成独立纯函数而不是给 ``_resolve_compute_quote`` 加参数:那个函数被既有
       判据用 2 参 lambda monkeypatch 掉了,加参数会把那条判据打成 TypeError ——
       而它钉的是别的东西。
    """
    if channel is None or not channel.resolved or not channel.eligible:
        return quote, quote_meta
    if channel.price_points is None:
        return quote, quote_meta
    return (
        ComputeQuote(amount=int(channel.price_points),
                     pricing_version=str(channel.price_version or "")),
        {"quote_state": "quoted"},
    )


def _execution_binding(channel) -> dict:
    """随 intent 冻结的**执行绑定**(execute adapter 读它)。

    🔴 刻意**不进** ``build_prefill`` 的投影,所以它不会出现在页面 DTO 里:
       里面有 media_id 与价格指纹,那是服务端执行用的,不是给页面看的。
       (``build_prefill`` 只挑固定几个键,新增键默认不出门 —— 判据钉了这一点。)
    """
    if channel is None or not channel.resolved:
        # 连账号都解析不出来 —— 没有任何可冻结的绑定。
        return {}
    # 🔴 **资格不合格也要把渠道项冻进来**(实拆演练订正的):
    #    不冻的话 execute 那一侧拿到空绑定,只能报"还没选定投放账号" ——
    #    而用户明明选了,只是那个号发不了图文。答非所问比不回答更坏。
    #    只有**价格**三项在不合格时不写:不可用的账号不该有一个价躺在那里。
    out = {
        "channel_option_id": channel.channel_option_id,
        "media_id": channel.media_id,
    }
    if channel.eligible:
        out["price_points"] = channel.price_points
        out["price_version"] = channel.price_version
        out["price_fingerprint"] = channel.price_fingerprint
    return out


def _recomputed_drift_hashes(request: Request, cursor, *, row: dict, contract,
                             include_price: bool = True) -> dict:
    """🔴 [窗G 段二 · Owner 2026-08-24 批] 按**当前**事实重算 POR-13 的三个 hash。

    ## 在这之前它们是自比较

    ``operation_confirm`` 与 ``operation_execute`` 原本都是把 ``row`` **自己的列**
    传回给 ``drift_reason``::

        drift_reason(row, actor=actor,
                     payload_hash=row["payload_hash"],                  # ← 自己比自己
                     object_manifest_hash=row["object_manifest_hash"],  # ← 自己比自己
                     compute_quote_hash=row.get("compute_quote_hash"))  # ← 自己比自己

    于是 ``services/xiaobang_intent.drift_reason`` 里那三支
    (L444 / L446 / L448-450)**结构性恒 False**,永远不会返回这三个 reason。
    真正有区分力的只剩六列 actor 快照 —— 也就是说 POR-13 逐字要求的
    「逐值绑定 object / snapshot / hash」在运行时**根本没人守**:
    prepare 之后对象被改掉、价目被调过,确认照样过。

    ## 重算的输入必须与 prepare **逐字同源**

    否则每一次正常确认都会误判成漂移。所以:

    * ``selection`` 取 prepare 时**冻结**的那份(``preview.canonical_input.selection``),
      不是从请求拿(从请求拿等于让调用方自己声明"我要比什么");
    * ``context`` 用同一份 selection 重新走 ``_authorized_context`` —— 与 prepare 同形,
      顺带把「这个客户还归不归你」再问一次;
    * ``channel`` / ``quote`` 走**现役同一批函数**,不另拼公式。

    差别只在:context / channel / 价目是**现在**取的。变了 ⇒ hash 变 ⇒ 漂移命中。

    ## 存量 intent 的兼容支 —— **已于 2026-08-25 撤销**(工单 C-6 / Codex 终审 P1-14)

    窗G 那一版在 ``canonical_input`` 缺失时回落到"自比较",理由是
    「部署那一刻正在确认页上的人不该因为我们上线而被拦下」。

    🔴 那个回落是**结构性恒真**,而它守的正是资金相邻的那道门:
       自己的 hash 与自己比,三支 drift 判断永远返 ``None``。也就是说
       存量 intent 上「确认的对象 == 现在要执行的对象」这句话**从来没有被验过**。
       Codex 终审 P1-14 逐字:「旧 intent 可以确认 A、执行当前 B」。
       "不拦"在这里不是体贴,是把一个**必然放行**的旁路留在确认闸上。

    所以现在:``canonical_input`` 缺失 ⇒ 调用方**拒绝确认/执行**,
    而不是拿一个恒真的比较冒充"已核对"。本函数仍然返回 ``recomputed: False``
    (它是纯取数,不认识 HTTP),由 :func:`_assert_reverifiable` 在两个
    调用点各拒一次 —— 两处共用同一个谓词,改坏它两侧判据一起红。

    影响面如实说:窗G 之前建、且**还在 TTL 内**的 external intent 会被要求
    重新准备一次。它们本来就是"我们证不了它没漂移"的那一批,
    而重新准备是免费的估价段(零资金副作用)。
    """
    frozen = dict((dict(row.get("preview") or {})).get("canonical_input") or {})
    if not frozen:
        return {
            "payload_hash": row["payload_hash"],
            "object_manifest_hash": row["object_manifest_hash"],
            "compute_quote_hash": row.get("compute_quote_hash"),
            "recomputed": False,
        }

    from services.xiaobang_channel_eligibility import resolve_channel_option

    selection = dict(frozen.get("selection") or {})
    context = _authorized_context(request, selection)
    channel = resolve_channel_option(cursor, selection.get("channel_option_id"))
    manifest = _object_manifest(
        context, selection,
        resource_kind=getattr(contract, "resource_kind", None), channel=channel,
        tenant_owner_id=_actor_binding(request, context).tenant_owner_id)
    quote, quote_meta = _resolve_compute_quote(cursor, contract)
    quote, quote_meta = _channel_compute_quote(quote, quote_meta, channel)
    return {
        "payload_hash": content_hash(frozen),
        "object_manifest_hash": content_hash(manifest),
        "compute_quote_hash": ((quote.quote_hash if quote is not None else None)
                               if include_price else row.get("compute_quote_hash")),
        "recomputed": True,
        "price_recomputed": bool(include_price),
    }


def _assert_reverifiable(live: dict) -> None:
    """🔴 [工单 C-6 · Codex 终审 P1-14] 三个 hash **必须是重算出来的**,不许自比较。

    ``_recomputed_drift_hashes`` 在 intent 的 preview 里没有 ``canonical_input``
    时会把 ``row`` 自己的列原样返回。那时 ``drift_reason`` 的三支比较是
    ``x == x`` —— **结构性恒真**,永远返 ``None``,于是「确认的对象 ==
    现在要执行的对象」这句话在那一批 intent 上从来没有被验过。

    这个函数是那条旁路的封口。放在**共用咽喉**(confirm 与 execute 都调
    ``_recomputed_drift_hashes``,两处都紧跟着调它),而不是在两个 handler
    里各写一份 ``if not live["recomputed"]`` —— 同一谓词写两处必有一处没人验。

    🔴 拒绝的是**确认/执行**,不是准备:重新走一次 prepare 是免费估价段,
       零资金副作用,而且新建的 intent 一定带 canonical_input。
       所以这条拒绝对用户是"再点一次准备",不是"这单做不了"。
    """
    if live.get("recomputed"):
        return
    raise IntentError(
        "INTENT_NOT_REVERIFIABLE",
        "这次安排是旧版本准备的，没法再核对一遍对象和价格；重新准备一次就能继续，不扣算力。",
        http_status=409, next_action="重新准备",
        detail={"reason_code": "canonical_input_missing"},
    )


def _raise_drift(reason: str):
    """把一次漂移翻成**明示**的 409,而不是一句含糊的「有变化」。

    Owner 2026-08-24 批:漂移命中不静默拒单,按「内容变了 / 价格变了 / 权限变了」
    三档各给各的下一步(``drift_notice``)。
    """
    notice = drift_notice(reason)
    raise IntentError(
        "OBJECT_OR_AUTHORITY_DRIFT", notice["message"],
        http_status=409, next_action=notice["next_action"],
        detail={"reason_code": reason, "drift_kind": notice["bucket"]},
    )


def _frozen_reason_facts(cursor, actor: ActorBinding, manifest: dict) -> list[dict]:
    """§9.6:小榜只读**公开、冻结**的推荐理由,**不现场生成**(窗G 段二①)。

    在这之前 ``operation_prepare`` 传的是 ``reason_facts=[]`` 写死 ——
    ``recommendation_reasons`` 恒空,``frozen_reasons`` 整个模块是死函数。

    🔴 租户身份取 ``actor.tenant_owner_id``,**不是** ``context.owner_user_id``
    ------------------------------------------------------------------------
    对抗复审实测:现役 ``services.customer_operation_plan.AuthorizedAssistantContext``
    (L117-157)**根本没有** ``owner_user_id`` 这个字段 —— 只有判据里那个 ``_Ctx``
    假对象有。第一版写成 ``getattr(context, "owner_user_id", None)`` 的后果是:
    生产上恒 ``None`` ⇒ 恒返 ``[]`` ⇒ **接线在生产上等于没接**,而判据因为夹具
    自造了那个属性所以全绿。这正是本仓记过的「夹具替被测代码干活 ⇒ 判据恒绿」。
    ``ActorBinding.tenant_owner_id`` 是 ``_actor_binding`` 现算的真值(拿不到
    context 归属时回落到 actor 本人),那才是生产上真实可用的租户身份。

    ✅ **上面那条「已知边界」已修**(工单 V3-A · Codex 三审 P1-9 · 2026-08-28)
    -------------------------------------------------------------------
    曾经的边界是:``context.owner_user_id`` 在生产上从来不存在 ⇒
    ``_actor_binding`` 的 ``getattr(context, "owner_user_id", 0)`` 恒取到 0 ⇒
    ``tenant_owner_id`` 恒等于操作者本人。组织席位代操作时租户被记成员工,
    这里按员工去 ``frozen_reasons.load`` 会查空。

    现在 ``_actor_binding`` 走 ``auth.principal_identity.resolve_principal_user_id``:
    组织身份在场时租户 owner = ``identity.principal_user_id``,个人场景才回落 actor。
    本函数这一行 ``actor.tenant_owner_id`` **不用改** —— 它读的一直是
    ``ActorBinding`` 现算的真值,变的是那个真值现在真的对了。

    🔴 理由不合法时**整批丢弃 + 响亮记录**,而不是让 prepare 崩掉
    ------------------------------------------------------------
    §9.6 的口径是「**不下发**那句话」,不是「不让用户继续」。prepare 是免费的
    估价段 —— 因为一条理由文本有问题就把整条链打成 500,等于用一个展示性字段
    绑架了用户的主流程(而且那是裸 500,违反 G-4)。
    但也**不做"挑掉坏的、留下好的"**:好坏混排会让人以为剩下的都审过了。
    整批丢 + ERROR 日志,判据用「同一对象上同时有好理由和坏理由 ⇒ 一条都不出」
    把它与「本来就没有理由」区分开。
    """
    subject_ref = manifest.get("quote_id")
    if not subject_ref:
        return []
    owner = getattr(actor, "tenant_owner_id", None)
    if not owner:
        return []

    from services.defensive_geo.xiaobang import frozen_reasons as _fr

    try:
        rows = _fr.load(cursor, tenant_owner_user_id=int(owner),
                        subject_kind="quote_snapshot", subject_ref=str(subject_ref))
    except _fr.FrozenReasonError as exc:
        # fail-closed 到**这个字段**,不是到整条链。
        logger.error("[xiaobang] 冻结理由不合法,本次整批不下发(subject=%s):%s",
                     subject_ref, exc)
        return []
    # 🔴 只出**人话**:``next_action_kind`` 是裸 ASCII 枚举,上屏会被 DLP-A
    #    判泄漏(真 HTTP 打过去就是 500)。它的价值在 ``load`` 里已经兑现了 ——
    #    ``assert_frozen_shape`` 逐条核过"付费动作不许从理由这一侧长出来"。
    return [{"text": str(r["public_text"])} for r in rows]


def _intent_dto(row: dict, *, entry, extra: Optional[dict] = None) -> dict:
    contract = entry.command_contract
    dto = {
        "intent_id": row["intent_id"],
        "intent_state": row["intent_state"],
        "intent_revision": int(row["intent_revision"]),
        "operation_id": entry.operation_id,
        "operation_version": int(row["operation_version"]),
        "registry_version": row["registry_version"],
        "side_effect": row["side_effect"],
        "confirmation_policy": {"mode": row["confirmation_mode"]},
        "payload_hash": row["payload_hash"],
        "object_manifest_hash": row["object_manifest_hash"],
        "compute_quote_expires_at": row.get("compute_quote_expires_at"),
        "approval_window_expires_at": row.get("approval_window_expires_at"),
        # 🔴 [R3-P7 ③] **不原样透传 preview**:它含 `scope`
        #    (`wp1_identity_and_object_refs`)与 `pending_manifest_inputs` 这类
        #    裸 ASCII 枚举,DLP-A 判泄漏并抛异常 ⇒ 真 HTTP 打过去就是 500。
        #    素树(生产尖)实测同形 ⇒ 存量缺陷,见交付单上报项。这里改人话投影。
        "preview": _public_preview(row.get("preview") or {}),
        "recommendation_reasons": row.get("reason_facts") or [],
        "deep_link": {
            "target_route": entry.route_template,
            "help_target": entry.help_target,
        },
    }
    if row.get("compute_quote_amount") is not None:
        dto["compute_quote"] = {
            "unit": row.get("compute_quote_unit") or "算力",
            "amount": int(row["compute_quote_amount"]),
            "pricing_version": row.get("pricing_version"),
            "quote_hash": row.get("compute_quote_hash"),
        }
    # 🔴 [窗G · Owner 2026-08-24]「执行前必须示算力预估」的**用户面**那一半。
    #    ``compute_quote`` 是机读的(amount/hash/version),她读不懂;
    #    ``compute_estimate.headline`` 才是屏幕上那句「本次预计消耗你的算力 X,
    #    确认后开始」。两个都要:前者给前端算,后者给人看。
    #
    #    刻意放在 ``dto.update(extra)`` **之前** —— prepare 那一跳会用 live
    #    ``quote_meta`` 覆盖同名的 ``quote_state``,而这一项按冻结值算,
    #    两者同源(prepare 把 quote_state 冻进 preview),覆盖后逐值相同。
    #    🔴 [对抗复审自查] 必须把**过期**传进去:GET intent 恢复时 extra 会把
    #    quote_state 覆盖成 "expired",而人话句子如果还写「确认后开始」,
    #    机读键与上屏文案当场打架 —— 前端渲染的是后者。
    dto["compute_estimate"] = build_public_estimate(row, expired=quote_expired(row))
    # 两档确认制的用户面口径(§8.1 / §15.4)。
    if row["side_effect"] == SIDE_EFFECT_EXTERNAL:
        dto["confirmation_required"] = True
        dto["confirmation_note"] = "发布后公开可见,需要你在页面上点一次确认。"
    else:
        dto["confirmation_required"] = False
        dto["confirmation_note"] = "这一步只消耗算力、可以撤销,完成后会通知你用了多少算力。"
    dto.update(extra or {})
    return dto


def _public_preview(preview: dict) -> dict:
    """preview 的用户面投影。内部工程标记不出门,未覆盖项翻人话。"""
    from services.xiaobang_page_prefill import humanize_pending_inputs

    return {
        "customer_label": preview.get("customer_label"),
        "pending_checks": humanize_pending_inputs(preview.get("pending_manifest_inputs")),
    }


def _raise(err: IntentError):
    raise HTTPException(status_code=err.http_status, detail=err.as_payload())


def _clean(payload: dict, *, where: str = "xiaobang_operations") -> dict:
    """façade 唯一出口。DLP-A 不过就不返回(§14.1)。

    ``where`` 默认值 = 本函数历史行为,既有 20 处调用点逐字节不变。
    传别的值只为了让**字段级机器面豁免**能按 (端点, 字段路径, 取值) 收窄 ——
    豁免登记在 :data:`services.xiaobang_facade_dlp.MACHINE_FACE_EXEMPTIONS`,
    共用一个 ``where`` 就等于把豁免面放宽到所有五阶段端点。
    """
    assert_facade_clean(payload, where=where)
    return payload


# ── 0. 能力发现(GET · 只读)───────────────────────────────────────────────
# 🔴 这两条**必须**声明在 ``/{operation_id}/...`` 那批之前:FastAPI 按声明顺序
#    匹配,``/{operation_id}/intents/{intent_id}`` 会把 ``operations/intents/xxx``
#    里的 ``intents`` 吃成 operation_id。顺序错了不会报错,只会静默走错 handler ——
#    配套判据 ``test_prefill_route_is_not_swallowed_by_the_operation_id_pattern``。
@router.get("/capabilities", include_in_schema=False)
async def operation_capabilities(request: Request):
    """按当前服务端身份、组织、租户和 capability 过滤后的能力目录(§8.2)。

    🔴 无权 operation 的**名称、schema 和存在性**都不下发 —— 过滤在
    :func:`services.gap_operation_map.discover_commands` 里做,这里只负责
    鉴权入口与 DLP 出口。之前这个函数只有测试在调 = 死函数;能力发现
    没有端点,模型就只能靠硬编码知道有哪些能力,那正是 §8.2 要防的。
    """
    user = _require_user(request)
    identity = getattr(request.state, "organization_identity", None)
    commands = omap.discover_commands(user, identity=identity)
    # 🔴 [2026-08-20 返修] 本端点用**自己的** where。它是全仓唯一会下发
    #    ``confirmation_policy.bind`` 的出口(五阶段 DTO 只发 mode),
    #    而 bind 的 11 个字段名在既有闸眼里是「未翻译的内部枚举」⇒
    #    上线以来对任何看得见命令的身份一律 500。
    #    修法是**字段级机器面豁免**(消费方普查:零显示面),不是补翻译表 ——
    #    理由与三重收窄写在 MACHINE_FACE_EXEMPTIONS 的注释里。
    return _clean({
        "registry_version": omap.OPERATION_REGISTRY_VERSION,
        "compatible_versions": list(omap.OPERATION_REGISTRY_COMPATIBLE_VERSIONS),
        "commands": commands,
        "count": len(commands),
    }, where=CAPABILITIES_FACADE)


@router.get("/intents/{intent_id}/prefill", include_in_schema=False)
async def operation_intent_prefill(intent_id: str, request: Request):
    """现役业务页面按 intent 取**已授权的预填数据**(§12.3 · XO-03)。

    deep link 只带 ``xint_...``(§12.2:URL 不放业务含义),所以页面进来时
    只有这一个 opaque id —— operation 从 intent 行反解,不从 URL 拿。

    鉴权顺序刻意如此:先 ``load_intent`` 用 actor 绑定确认这行归当前身份,
    再按行里的 operation_id 过 :func:`_authorize`。反过来做的话,
    「猜一个别人的 intent_id + 一个自己有权的 operation_id」就能过第一道。
    """
    from db.connection import get_db

    context = _authorized_context(request, {})
    with get_db() as conn:
        cursor = conn.cursor()
        actor = _actor_binding(request, context)
        try:
            row = load_intent(cursor, intent_id=intent_id, actor=actor)
            # [R3-P8 ②] 从冻结对象恢复 refs → **逐次现役对象授权**。
            # 空 refs 的老写法等于这些端点上从没问过「这个客户还归不归你」。
            _authorized_context(request, _refs_from_frozen_intent(row))
        except IntentError as err:
            conn.rollback()
            _raise(err)
        conn.commit()

    entry = _resolve_entry(str(row["operation_id"]))
    _authorize(request, entry)

    projection = status_projection(row)
    quote_state = "expired" if quote_expired(row) else None
    quote_note = ("算力报价已过期,重新核对一下就能继续。" if quote_state == "expired" else None)
    payload = build_prefill(
        row,
        entry=entry,
        projection=projection,
        needs_approval=bool(row["intent_state"] == STATE_APPROVAL_PENDING),
        quote_state=quote_state,
        quote_note=quote_note,
    )
    payload["status"] = projection
    return _clean(payload)


# ── 1. query(只读)────────────────────────────────────────────────────────
@router.post("/{operation_id}/query", include_in_schema=False)
async def operation_query(operation_id: str, payload: QueryRequest, request: Request):
    """读取当前实时事实与可执行范围。只读:不建任务、不冻结、不调供应商。"""
    entry = _resolve_entry(operation_id)
    _authorize(request, entry)
    context = _authorized_context(request, payload.context_refs)
    contract = entry.command_contract
    return _clean({
        "interaction_id": payload.interaction_id,
        "operation_id": entry.operation_id,
        "operation_version": 1,
        "registry_version": omap.OPERATION_REGISTRY_VERSION,
        "side_effect": contract.side_effect,
        "facts": context.public_context(),
        "available_next_phases": ["prepare"],
        "deep_link": {
            "target_route": entry.route_template,
            "help_target": entry.help_target,
        },
    })


# ── 2. prepare(零业务写)──────────────────────────────────────────────────
@router.post("/{operation_id}/prepare", include_in_schema=False)
async def operation_prepare(operation_id: str, payload: PrepareRequest, request: Request):
    """生成不可变操作预览与服务端算力报价。不扣算力、不发布、不启动生成。"""
    entry = _resolve_entry(operation_id)
    _authorize(request, entry)
    context = _authorized_context(request, payload.selection)
    contract = entry.command_contract

    canonical = canonical_input(
        operation_id=entry.operation_id,
        operation_version=1,
        request_schema_version=contract.request_schema_version,
        selection=payload.selection,
        allowlist=_SELECTION_ALLOWLIST,
    )
    from db.connection import get_db
    from services.xiaobang_channel_eligibility import resolve_channel_option

    with get_db() as conn:
        cursor = conn.cursor()
        actor = _actor_binding(request, context)
        # 🔴 [WO-B ③] 渠道资格在 **prepare 就核**,不是留到 execute 才发现发不了。
        #    核不出账号(线上既有的那些不可解析取值)时返回「还没核」那一档,
        #    manifest 的 pending 原样保留 —— prepare **不拦**:
        #    选渠道本来就可以稍后在页面上做,在这里 404 等于把"还没选好"变成"你错了"。
        channel = resolve_channel_option(
            cursor, (payload.selection or {}).get("channel_option_id"))
        manifest = _object_manifest(
            context, payload.selection,
            resource_kind=getattr(contract, "resource_kind", None),
            channel=channel,
            tenant_owner_id=_actor_binding(request, context).tenant_owner_id)
        quote, quote_meta = _resolve_compute_quote(cursor, contract)
        quote, quote_meta = _channel_compute_quote(quote, quote_meta, channel)
        try:
            row, created = prepare_intent(
                cursor,
                entry_operation_id=entry.operation_id,
                contract=contract,
                registry_version=omap.OPERATION_REGISTRY_VERSION,
                actor=actor,
                prepare_request_id=payload.prepare_request_id,
                canonical=canonical,
                payload_hash=content_hash(canonical),
                object_manifest_hash=content_hash(manifest),
                quote=quote,
                preview={
                    "customer_label": context.public_context().get("brand_name"),
                    "scope": manifest["manifest_scope"],
                    "pending_manifest_inputs": manifest["pending"],
                    # [R3-P7 ③] 可展示对象标识随 intent 一起冻结:预填页面要能
                    # 逐条显示"在动哪个东西",而 intent 是不可变的 ⇒ 标识也必须
                    # 在 prepare 时定下来,不能在预填时再去现查(那样刷新一次
                    # 可能显示成另一个对象)。
                    "object_items": _display_object_items(context, manifest),
                    # 🔴 [R3-P8 ①] **producer 真写** form_prefill,随 intent 一起冻结。
                    #    R3-P7 只有判据夹具里有这个键,生产端一个字没写 ——
                    #    也就是说浏览器判据那条"表被真填了"打的是**夹具自己造的字段**。
                    #    冻结在 prepare 而不是预填时现查:intent 不可变,
                    #    刷新一次不能填成另一个对象。
                    "form_prefill": _frozen_form_prefill(context, canonical),
                    # 🔴 [R3-P11 ②] 目标渠道/账号随 intent 冻结。
                    #    在本包之前 preview 里根本没有这个键 ⇒ prefill.channel 恒空 ⇒
                    #    前端就算渲染了也永远只能显示降级文案(死元素)。
                    "channel": _frozen_channel(context, canonical, manifest,
                                               channel=channel),
                    # 🔴 [WO-B ②] execute 要用的绑定随 intent 一起冻结,
                    #    **服务端内部**用,不进页面 DTO(见 _execution_binding)。
                    "execution_binding": _execution_binding(channel),
                    # 🔴 [窗G] **报不出价时,把"为什么报不出"也一起冻结。**
                    #    没有它,GET 恢复只知道"没数字",不知道是"还没选账号"
                    #    还是"价格取不到" —— 那两句话的下一步动作不一样,
                    #    猜错等于把用户支到错的地方去(compute_estimate.estimate_state)。
                    #    冻结而不是恢复时现查:intent 不可变,刷新一次不能换一档说法。
                    # 🔴 [窗G 段二] **冻结 canonical 入参**。confirm/execute 重算
                    #    object_manifest_hash 与 compute_quote_hash 时必须喂
                    #    与 prepare **逐字相同**的 selection,否则每次正常确认
                    #    都会误判成漂移。冻在这里而不是重算时从请求拿:
                    #    从请求拿等于让调用方自己声明"我要比什么"。
                    "canonical_input": dict(canonical),
                    "quote_state": quote_meta.get("quote_state"),
                    # 🔴 [窗G] **领域是不是已经知道"为什么估不出价"。**
                    #    True ⇒ 执行段会给出这一档自己的那句人话(账号发不了图文 /
                    #    今天额度用完 / 还没选账号),confirm 不拦、不抢答;
                    #    False 而又没有数字 ⇒ 谁也解释不了,confirm 当场拦下,
                    #    不让她在一无所知的情况下按确认(compute_estimate.DOMAIN_BLOCKER_KEY)。
                    ESTIMATE_DOMAIN_BLOCKED_KEY: (
                        channel is None or not channel.resolved or not channel.eligible),
                },
                # 🔴 [窗G 段二①] §9.6:只读冻结理由。在这之前这里写死 []
                #    ⇒ recommendation_reasons 恒空 ⇒ frozen_reasons 整个模块是死函数。
                reason_facts=_frozen_reason_facts(cursor, actor, manifest),
                interaction_id=payload.interaction_id,
            )
        except IntentError as err:
            conn.rollback()
            _raise(err)
        conn.commit()

    return _clean(_intent_dto(row, entry=entry, extra={
        "interaction_id": payload.interaction_id,
        "replayed": not created,
        **quote_meta,
    }))


# ── 3. GET intent(恢复)───────────────────────────────────────────────────
@router.get("/{operation_id}/intents/{intent_id}", include_in_schema=False)
async def operation_get_intent(
    operation_id: str, intent_id: str, request: Request, page_ready: bool = False,
):
    """按当前授权恢复同一个 intent。

    ``page_ready=true`` 触发 ``prepared → awaiting_confirmation``:纯协调迁移,
    零业务写、零算力变化,只是把「已生成预览」与「已展示等待点击」分开记。
    """
    entry = _resolve_entry(operation_id)
    _authorize(request, entry)
    context = _authorized_context(request, {})
    from db.connection import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        actor = _actor_binding(request, context)
        try:
            row = load_intent(cursor, intent_id=intent_id, actor=actor, for_update=page_ready)
            # [R3-P8 ②] 从冻结对象恢复 refs → **逐次现役对象授权**。
            # 空 refs 的老写法等于这些端点上从没问过「这个客户还归不归你」。
            _authorized_context(request, _refs_from_frozen_intent(row))
            if page_ready and row["intent_state"] == STATE_PREPARED:
                moved = mark_awaiting_confirmation(
                    cursor, intent_id=intent_id,
                    expected_revision=int(row["intent_revision"]),
                )
                if moved is not None:
                    row = moved
        except IntentError as err:
            conn.rollback()
            _raise(err)
        conn.commit()

    extra = dict(status_projection(row))
    if quote_expired(row):
        # 价格锁到期 ≠ intent 失效(P0-C)。只提示重新报价。
        extra["quote_state"] = "expired"
        extra["quote_note"] = "算力报价已过期,重新核对一下就能继续。"
    return _clean(_intent_dto(row, entry=entry, extra=extra))


# ── 4. confirm ────────────────────────────────────────────────────────────
@router.post("/{operation_id}/intents/{intent_id}/confirm", include_in_schema=False)
async def operation_confirm(
    operation_id: str, intent_id: str, payload: ConfirmRequest, request: Request,
):
    """记录真实用户对不可变意图的明确同意(仅 ``side_effect=external``)。"""
    entry = _resolve_entry(operation_id)
    _authorize(request, entry)
    context = _authorized_context(request, {})
    from db.connection import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        actor = _actor_binding(request, context)
        try:
            row = load_intent(cursor, intent_id=intent_id, actor=actor, for_update=True)
            # [R3-P8 ②] 从冻结对象恢复 refs → **逐次现役对象授权**。
            # 空 refs 的老写法等于这些端点上从没问过「这个客户还归不归你」。
            _authorized_context(request, _refs_from_frozen_intent(row))
            if row["side_effect"] == SIDE_EFFECT_COMPUTE_ONLY:
                # 🔴 纯算力档没有"确认"这个动作(已签发 DP-A6.1:静默扣 + 事后通知)。
                #    给它套一个确认门 = 与 A6.1 正面冲突,也是 A4 明令禁止的
                #    "给正常流程加确认弹窗"。这里明确拒绝,而不是默默接受。
                raise IntentError(
                    "CONFIRMATION_NOT_APPLICABLE",
                    "这一步只消耗算力,不需要额外确认,可以直接继续。",
                    http_status=409, next_action="直接继续",
                )
            if int(row["intent_revision"]) != int(payload.intent_revision):
                raise IntentError(
                    "INTENT_REVISION_CONFLICT", "这个操作刚刚有变化,请刷新后再试。",
                    http_status=409, next_action="刷新",
                )
            if row["intent_state"] not in (STATE_PREPARED, STATE_AWAITING_CONFIRMATION):
                raise IntentError(
                    "CONFIRM_NOT_ALLOWED", "这一步现在不能确认。",
                    http_status=409, next_action="查看进度",
                    detail={"intent_state": row["intent_state"]},
                )
            # 🔴 [窗G 段二] 三个 hash 与**当前**事实比,不再自比较(见 _recomputed_drift_hashes)。
            live = _recomputed_drift_hashes(
                request, cursor, row=row, contract=entry.command_contract)
            # 🔴 [工单 C-6] 先问「这三个 hash 是不是真的重算出来的」,再比。
            #    缺 canonical_input 时下面那三支是 x == x(恒真),
            #    比了等于没比 —— 那正是「确认 A 执行 B」的入口。
            _assert_reverifiable(live)
            drift = drift_reason(
                row, actor=actor,
                payload_hash=live["payload_hash"],
                object_manifest_hash=live["object_manifest_hash"],
                compute_quote_hash=live["compute_quote_hash"],
            )
            if drift is not None:
                _raise_drift(drift)
            if quote_expired(row):
                raise IntentError(
                    "COMPUTE_QUOTE_EXPIRED", "算力报价已过期,重新核对一下就能继续。",
                    http_status=409, next_action="重新准备",
                )
            # 🔴 [窗G · Owner 2026-08-24] **没有预估,不许确认。**
            #
            #    Owner 原话:执行前必须示算力预估、用户确认后才开始。
            #    在这之前这条链上**没有任何一行**要求意图上存在报价:
            #    ``quote_expired`` 在 ``compute_quote_expires_at IS NULL`` 时恒 False,
            #    ``drift_reason`` 在 ``compute_quote_hash`` 为空时整段跳过 ——
            #    于是一个报不出价的 external 意图照样签得出回执,
            #    用户在**一个数字都没看见**的情况下按下了确认。
            #
            #    这道门刻意排在 ``issue_confirmation_receipt`` **之前**:确认在本系统里
            #    就是授权,授权一旦记下就不该再回头说"其实我还没算出要花多少"。
            #    现役那道 ``COMPUTE_QUOTE_MISSING`` 长在发布 adapter 内部(确认之后),
            #    而且第二个 adapter 接进来不会继承它 —— 谓词搬到这里,全仓只剩一处。
            try:
                assert_estimate_shown(row)
            except EstimateNotShown as exc:
                raise IntentError(
                    "COMPUTE_QUOTE_MISSING", exc.public_message,
                    http_status=409, next_action=estimate_next_action(exc.state),
                    detail={"quote_state": exc.state},
                ) from exc
            receipt, created = issue_confirmation_receipt(
                cursor, intent_row=row, actor=actor,
                challenge=payload.user_action_challenge,
                user_agent=request.headers.get("user-agent", ""),
            )
            moved = mark_confirmed(
                cursor, intent_id=intent_id,
                expected_revision=int(row["intent_revision"]),
            ) if created else row
            if moved is None:
                raise IntentError(
                    "INTENT_REVISION_CONFLICT", "这个操作刚刚有变化,请刷新后再试。",
                    http_status=409, next_action="刷新",
                )
            if created:
                # 🔴 [WO-B ② 2026-08-20] confirm 的 CAS 把 revision 从 N 推到 N+1,
                #    而回执是按 N 签发的 ⇒ execute 拿着 N+1 **永远找不到**它。
                #    在本包之前,external 档没有任何一条路能走到 execute。
                #    详见 rebind_receipt_to_revision 的 docstring(含为什么不是让
                #    execute 去猜 N-1)。同一事务内改绑,回执与 intent 一起提交。
                rebind_receipt_to_revision(
                    cursor, intent_id=intent_id,
                    from_revision=int(row["intent_revision"]),
                    to_revision=int(moved["intent_revision"]))
                receipt = dict(receipt)
                receipt["intent_revision"] = int(moved["intent_revision"])
            row = moved
        except IntentError as err:
            conn.rollback()
            _raise(err)
        conn.commit()

    return _clean({
        "intent_id": row["intent_id"],
        "intent_revision": int(row["intent_revision"]),
        "intent_state": row["intent_state"],
        "confirmation_receipt_id": receipt["receipt_id"],
        "receipt_expires_at": receipt["receipt_expires_at"],
        "approval": {
            "state": "not_required",
            "approval_request_id": None,
            "approval_policy_version": row.get("approval_policy_version"),
        },
        "execution_id": None,
        "available_actions": ["cancel"],
        "replayed": not created,
    })


# ── 5. cancel ─────────────────────────────────────────────────────────────
@router.post("/{operation_id}/intents/{intent_id}/cancel", include_in_schema=False)
async def operation_cancel(
    operation_id: str, intent_id: str, payload: CancelRequest, request: Request,
):
    """external side effect 未开始前的 CAS 撤销。已开始的不伪装成已取消。"""
    entry = _resolve_entry(operation_id)
    _authorize(request, entry)
    context = _authorized_context(request, {})
    from db.connection import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        actor = _actor_binding(request, context)
        try:
            row = load_intent(cursor, intent_id=intent_id, actor=actor)
            # [R3-P8 ②] 从冻结对象恢复 refs → **逐次现役对象授权**。
            # 空 refs 的老写法等于这些端点上从没问过「这个客户还归不归你」。
            _authorized_context(request, _refs_from_frozen_intent(row))
            row = cancel_intent(
                cursor, intent_id=intent_id,
                expected_revision=payload.expected_intent_revision,
                cancel_request_id=payload.cancel_request_id,
            )
        except IntentError as err:
            conn.rollback()
            _raise(err)
        conn.commit()

    return _clean(_intent_dto(row, entry=entry, extra={
        "cancelled": row["intent_state"] == STATE_CANCELLED,
        "next_actions": ["重新准备"],
    }))


# ── 6. execute(WP1 停在最后一步)──────────────────────────────────────────
@router.post("/{operation_id}/intents/{intent_id}/execute", include_in_schema=False)
async def operation_execute(
    operation_id: str, intent_id: str, payload: ExecuteRequest, request: Request,
):
    """按最终顺序跑完全部前置检查,然后**消费回执 + 调领域 adapter**。

    检查顺序(身份 → 归属 → revision → 状态 → 漂移 → 确认档 → adapter)
    与 WP1 时**一个字没动**,所以「external 类没确认不执行」这条判据仍然打在真路径上。
    回执只在全部前置通过之后才消费(前面 ``_assert_confirmation_present`` 只验不消费)。
    """
    entry = _resolve_entry(operation_id)
    _authorize(request, entry)
    context = _authorized_context(request, {})
    contract = entry.command_contract
    import asyncio

    from db.connection import get_db

    # 🔴 [WO-B ② 2026-08-20] 整段 DB 工作放到**工作线程**里跑,与
    #    `POST /api/meijiehezi/image-notes/publish-batch` 的 `asyncio.to_thread(_materialize)`
    #    完全同形。理由是硬性的:产线里的逐项冻结要在**同一事务**内调 async 的
    #    `freeze_points`,做法是起一个独立事件循环 `run_until_complete` ——
    #    而在已经有 loop 在跑的 async handler 里这么做会直接
    #    `RuntimeError: Cannot run the event loop while another loop is running`。
    #    换成"在这一侧改 freeze 的调用方式"就等于给资金路径开第二种写法,
    #    正是本包要避免的事。
    def _run() -> tuple:

        with get_db() as conn:
            cursor = conn.cursor()
            actor = _actor_binding(request, context)
            try:
                row = load_intent(cursor, intent_id=intent_id, actor=actor)
                # [R3-P8 ②] 从冻结对象恢复 refs → **逐次现役对象授权**。
                # 空 refs 的老写法等于这些端点上从没问过「这个客户还归不归你」。
                _authorized_context(request, _refs_from_frozen_intent(row))
                # 🔴 [WO-B ② 2026-08-20] **同一次执行请求的重放,在 revision 检查之前认掉。**
                #    第一次执行成功之后 `link_execution` 把 revision 推到了 N+2,
                #    而重试的客户端手里还是 N+1 —— 按顺序走下去会得到
                #    `INTENT_REVISION_CONFLICT`(「请刷新」),那是**答非所问**:
                #    这次操作明明已经成功了。本仓的口径是「幂等不是第二次别再做一遍,
                #    而是第二次给同一个答案」,所以这里回放**同一个 execution_id**。
                #    🔴 键是 `execution_request_id`,不是"状态是 execution_linked":
                #    后者会让**另一个**请求也拿到这份回放。
                if (row["intent_state"] == STATE_EXECUTION_LINKED
                        and str(row.get("execution_request_id") or "")
                        == str(payload.execution_request_id)):
                    return dict(row), {"replayed": True,
                                       "command_id": row.get("execution_id")}
                if int(row["intent_revision"]) != int(payload.intent_revision):
                    raise IntentError(
                        "INTENT_REVISION_CONFLICT", "这个操作刚刚有变化,请刷新后再试。",
                        http_status=409, next_action="刷新",
                    )
                if row["intent_state"] in (STATE_CANCELLED, STATE_EXPIRED):
                    raise IntentError(
                        "INTENT_NOT_EXECUTABLE", "这次操作已经取消或过期了。",
                        http_status=409, next_action="重新准备",
                        detail={"intent_state": row["intent_state"]},
                    )
                if row["intent_state"] == STATE_APPROVAL_PENDING:
                    raise IntentError(
                        "APPROVAL_PENDING", "已提交给负责人,还没有开始执行。",
                        http_status=409, next_action="查看我的审批",
                    )
                # 🔴 [窗G 段二] 同上:execute 这一侧也必须与当前事实比。
                #    两处共用 _recomputed_drift_hashes 一个实现 ——
                #    同一条谓词写两处 ⇒ 必有一处没人验。
                live = _recomputed_drift_hashes(
                    request, cursor, row=row, contract=contract,
                    include_price=False)   # 价格由产线的 PRICE_CHANGED 收口,见 docstring
                # 🔴 [工单 C-6] execute 侧同一道门。**两处都要**:
                #    只在 confirm 拦的话,本次上线**之前**已经拿到回执的那批
                #    存量 intent 仍然可以执行,而它们恰恰是证不了
                #    「确认对象 == 执行对象」的那一批。
                _assert_reverifiable(live)
                drift = drift_reason(
                    row, actor=actor,
                    payload_hash=live["payload_hash"],
                    object_manifest_hash=live["object_manifest_hash"],
                    compute_quote_hash=live["compute_quote_hash"],
                )
                if drift is not None:
                    _raise_drift(drift)
                if row["side_effect"] == SIDE_EFFECT_EXTERNAL:
                    # 🔴 [窗G · POR-13 对账] 七维是否**都还**落在现役绑定字段里。
                    #    这不是装饰:合同哪天加/删一个绑定字段,这里立刻告诉我们
                    #    "POR-13 的哪一维现在没人绑了" —— 而不是等到某个跨维重放
                    #    真的被放行。fail-closed 成受控 503,不是裸 500(G-4)。
                    try:
                        assert_bind_fields_cover_por13(str(row["side_effect"]))
                    except ReceiptGuardError as exc:
                        raise IntentError(
                            "CONFIRMATION_BINDING_INCOMPLETE",
                            "这次操作的确认方式要由平台核对一下,已经准备好的内容都在,"
                            "没有扣除任何算力。",
                            http_status=503, next_action="稍后再试一次",
                            detail={"reason": str(exc)[:160]}) from exc
                    _assert_confirmation_present(
                        cursor, row,
                        execution_request_id=payload.execution_request_id)
                # ── 到这里为止,解锁前要做的前置全部通过 ──
                if not contract.executable:
                    raise IntentError(
                        "DOMAIN_ADAPTER_NOT_AVAILABLE",
                        "这个操作的执行链还没开放,已经准备好的内容都在,不会重复。",
                        http_status=409,
                        next_action="打开现役页面继续",
                        detail={"operation_id": entry.operation_id},
                    )
                if row["side_effect"] != SIDE_EFFECT_EXTERNAL:
                    # compute_only 档没有「等用户点击」这个动作 ⇒ 也就没有回执可以当
                    # 幂等键。现役 executable 的只有 external 一档;真要开 compute_only,
                    # 必须先给它一个**别的**幂等身份,而不是让它借用一张不存在的回执。
                    raise IntentError(
                        "DOMAIN_ADAPTER_NOT_AVAILABLE",
                        "这个操作的执行链还没开放。",
                        http_status=409, next_action="打开现役页面继续")

                # 🔴 [WO-B ②] 回执在这里才被**消费**(事务内条件 UPDATE = 抢占)。
                #    前面 `_assert_confirmation_present` 只验不消费 —— 那是为了不让
                #    一次注定失败的调用把用户的确认作废。到这一行为止前置全过了,
                #    才有资格烧掉它。同一个 execution_request_id 重放拿回同一张,
                #    不是第二次消费。
                receipt = consume_confirmation_receipt(
                    cursor, intent_id=intent_id,
                    intent_revision=int(row["intent_revision"]),
                    execution_request_id=payload.execution_request_id)

                result = _domain_execute(request, cursor, contract=contract,
                                         row=row, receipt=receipt)

                # 🔴 与 cancel 共用同一把 CAS:并发时只有一方能成功。
                linked = link_execution(
                    cursor, intent_id=intent_id,
                    expected_revision=int(row["intent_revision"]),
                    execution_id=str(result["command_id"]),
                    execution_request_id=str(payload.execution_request_id),
                    domain_ref={
                        "state": "queued",
                        "reference": str(result["command_id"]),
                        # 资金态 = **已冻结**。不写 committed —— 实扣要等回执核对,
                        # 说成 committed 是替资金下一个我们还不知道的结论。
                        "settlement_state": "reserved",
                        "external_state": "not_started",
                    },
                )
                if linked is None:
                    raise IntentError(
                        "INTENT_REVISION_CONFLICT", "这个操作刚刚有变化,请刷新后再试。",
                        http_status=409, next_action="刷新")
                row = linked
            except IntentError as err:
                conn.rollback()
                _raise(err)
            conn.commit()
        return row, result

    row, result = await asyncio.to_thread(_run)

    return _clean(_intent_dto(row, entry=entry, extra={
        "replayed": bool(result.get("replayed")),
        **status_projection(row),
    }))


#: ``contract.adapters["execute"]`` → 真正的领域执行函数。
#:
#: 🔴 **注册表驱动派发**,不在 handler 里写 ``if operation_id == …``:
#:    合同里登记了一个 execute adapter 名,这里就必须认得它。认不得 = 那条
#:    operation 的 ``executable=True`` 是一句空话(端点会在最后一刻 500)。
#:    配套接线锁 ``test_every_executable_contract_has_a_dispatchable_adapter``
#:    从注册表机械枚举,漏一个即红 —— 分母不是手写的。
def _execute_adapters() -> dict:
    from services.xiaobang_publish_execute import execute_publish_image_note

    return {"publish_image_note_execute": execute_publish_image_note}


def _domain_execute(request: Request, cursor, *, contract, row: dict, receipt: dict) -> dict:
    """把已确认的 intent 交给**现役领域产线**。

    🔴 结算权威与 payer 走发布域**那一个**解析器
       (``resolve_settlement_authority``),小榜这一侧不解析 payer、不碰钱包 ——
       「不新开资金路径」这句话的物理含义就是这一行。
    """
    from api.geo_image_note_api import _resolve_identity
    from services.geo_douyin.contract_funding import (
        PlatformAccountUnavailable, resolve_settlement_authority,
    )

    adapter_name = str((contract.adapters or {}).get("execute") or "")
    adapter = _execute_adapters().get(adapter_name)
    if adapter is None:
        raise IntentError(
            "DOMAIN_ADAPTER_NOT_AVAILABLE",
            "这个操作的执行链还没开放,已经准备好的内容都在,不会重复。",
            http_status=409, next_action="打开现役页面继续")

    user = _require_user(request)
    identity = _resolve_identity(request)
    try:
        settlement = resolve_settlement_authority(
            is_admin=bool(user.get("is_admin")),
            organization_id=identity.get("organization_id"),
            organization_billing_ready=bool(identity.get("organization_billing_ready")),
            owner_user_id=int(identity["payer_user_id"]))
    except PlatformAccountUnavailable as exc:
        # fail-closed:绝不回落成「扣服务商」(2026-08-16 生产事故形态)
        raise IntentError(
            "SETTLEMENT_ACCOUNT_UNAVAILABLE",
            "平台承担账户暂时不可用,这次没有扣任何算力,稍后再试。",
            http_status=503, next_action="重试", detail={"reason": str(exc)[:120]})
    if settlement.get("authority") is None:
        raise IntentError(
            "APPROVAL_HANDOFF_REQUIRED",
            "这次投放要由团队负责人发起,已经准备好的内容都在。",
            http_status=409, next_action="交给团队负责人")

    return adapter(cursor, intent_row=row, receipt=receipt,
                   identity=identity, settlement=settlement)


def _assert_confirmation_present(cursor, row: dict,
                                 *, execution_request_id: str = "") -> None:
    """external 档:必须存在**可用**的确认回执才允许继续。

    只验不消费(见模块 docstring)。拔掉这段 → 未确认的 external 操作会走到
    adapter 检查,判据必红(§19.1 确认门两档映射)。

    🔴 [WO-B ② 2026-08-20] 「可用」有**两种**,少一种就把幂等重放判成未确认:
      · 还没消费且没过期 —— 第一次执行;
      · **已被同一个 ``execution_request_id`` 消费过** —— 用户双击 / 客户端重试。
        回执是单次消费品,第一次执行已经烧掉它了;这时再要求"未消费"
        等于让第二发拿到 ``CONFIRMATION_REQUIRED``,而正确答案是**回放同一个结果**。
        判断口径与 ``consume_confirmation_receipt`` 里那一支**逐字同源**
        (同一个 ``consumed_by_execution_request_id`` 比较),不是另写一份。
    """
    # 🔴 [窗G · POR-13 接线] SQL 只负责**取行 + 用库的钟算过期**,
    #    「这张回执能不能用」这条谓词交给
    #    ``services.defensive_geo.xiaobang.receipt_guard.assert_receipt_usable``。
    #
    #    为什么是委派而不是"再加一道":同一条谓词写两处 ⇒ 必有一处没人验,
    #    而且两把锁叠在同一条路径上时,其中一把的变异会被另一把吞掉 ——
    #    于是「相关判据全绿」证明不了任何一行被验过(本仓记过的形态)。
    #    委派之后全仓只有一份实现:改坏它,execute 侧判据当场红。
    #
    #    🔴 **不再把 intent_revision 写进 WHERE**:写进去的话,守卫里
    #    「回执绑的是上一版」那一支永远拿不到反例(查不到行就退化成
    #    "还没有你的确认"),等于把一条本该有区分力的检查做成自比较 ——
    #    正是本包在 ``drift_reason`` 上发现的那种形态。改成取**最新一版**回执,
    #    由守卫逐值比对 revision,用户因此也能听见准确的那句话。
    cursor.execute(
        """
        SELECT receipt_id, intent_id, intent_revision, consumed_at,
               consumed_by_execution_request_id,
               (receipt_expires_at <= NOW()) AS expired
          FROM xiaobang_confirmation_receipts
         WHERE intent_id=%s
         ORDER BY intent_revision DESC
         LIMIT 1
        """,
        (str(row["intent_id"]),),
    )
    fetched = cursor.fetchone()
    receipt_row = dict(fetched) if fetched is not None else None
    try:
        assert_receipt_usable(
            receipt_row,
            intent_id=str(row["intent_id"]),
            intent_revision=int(row["intent_revision"]),
            execution_request_id=str(execution_request_id or ""),
            expired=bool(receipt_row and receipt_row.get("expired")),
        )
    except ReceiptGuardError as exc:
        # G-4:受控失败,不裸 500。守卫的话本来就是人话,直接下发。
        raise IntentError(
            "CONFIRMATION_REQUIRED", str(exc),
            http_status=409, next_action="打开核对并确认",
        ) from exc


# ── 7. status ─────────────────────────────────────────────────────────────
@router.get("/{operation_id}/executions/{execution_id}/status", include_in_schema=False)
async def operation_status(operation_id: str, execution_id: str, request: Request):
    """把现役领域状态翻成人话。不创造第二状态机。"""
    entry = _resolve_entry(operation_id)
    _authorize(request, entry)
    context = _authorized_context(request, {})
    from db.connection import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        actor = _actor_binding(request, context)
        cursor.execute(
            "SELECT * FROM xiaobang_operation_intents "
            " WHERE execution_id=%s AND actor_user_id=%s AND tenant_owner_id=%s",
            (str(execution_id), int(actor.actor_user_id), int(actor.tenant_owner_id)),
        )
        row = cursor.fetchone()
        conn.commit()
    if row is None:
        # 猜 id / 跨租户 / 真不存在 —— 同一个 404。
        _raise(IntentNotFound())
    row = dict(row)
    # 🔴 [R3-P9 ①] status 与另外五个恢复端点同款:从**冻结的 intent** 恢复对象 refs
    #    → 逐次现役对象授权。R3-P8 修了五个、漏了这一个 —— 漏的原因不是想不到,
    #    是那一轮的撤权 404 判据分母是**手写的五元组**,第六个端点从来没进过分母。
    #    所以本轮真正的修法是判据分母改机械枚举(见
    #    ``test_release_atomicity_and_recovery_authz_pg.frozen_object_routes``),
    #    否则第七个端点还会以同样的方式漏。
    #    这一跳按 execution_id 查,行本身已按 actor+tenant 收窄;但「这个客户还归不归你」
    #    是**现役**问题 —— 撤销分配之后,执行记录照样查得到才是洞。
    _authorized_context(request, _refs_from_frozen_intent(row))
    return _clean({
        "execution_id": execution_id,
        "operation_id": entry.operation_id,
        **status_projection(row),
        "next_actions": [],
        "deep_link": {
            "target_route": entry.route_template,
            "help_target": entry.help_target,
        },
    })
