"""防御型 GEO · **销售自救面**三条链(包H · §0.5.5 U-9 / §0.5.6 Z-3.1 / Z-3.3)。

三条链放一个文件是因为它们回答的是**同一个问题**:
「她被卡住了,现在点哪一颗按钮能自己走下去。」

  · Z-3.1  资料缺 → 让 AI 联网查(复用现役 ``autofill_brand``,扣算力,确认后生效)
  · Z-3.3  广告法命中 → 让 AI 按 rule_id 改好这一句(销售确认/微调 → 新 revision)
  · U-9    要发给客户 → token 链接统一面板 + 人话前缀 + 一键重签发

═══════════════════════════════════════════════════════════════════
🔴 三条铁律(违反其一,这个文件就不该存在)
═══════════════════════════════════════════════════════════════════
1. **禁自定价**。Z-3.1 明写「扣费走现役 ``autofill_brand`` 价目」——
   所以这里没有任何数字,只有 ``_bill_ctx(request, "autofill_brand")``。
   价格由服务端实时签发(资金 SSOT = ``docs/SYSTEM_TRUTH/08_billing.md``)。
2. **禁造第二套写入路径**。AI 查到的值**不落库**:本端点零副作用,
   返回 draft + 四按钮卡,真正写回走现役 ``PUT /api/my-clients/{brand_id}``。
   这与现役元指令「永远不中断对话」的四按钮语义逐字一致
   (``agents/brand_field_suggester``:AI 推断 → 用户确认 → 写回)。
3. **禁纯手工文本框**(Z-3.3 逐字)。广告法修复的**第一颗**按钮是
   「看 AI 改好的版本」;手工微调是第二步,不是唯一出口。
   一期就是退化成"跳通用编辑器"被判未兑现。

🔴 错误一律走 ``_safe_error``:本 router 与 façade 共用同一信封与同一 route_class。
   ``_bill_ctx`` 抛的是**框架 HTTPException**(402 裸信封),必须在这里翻译成
   typed ``INSUFFICIENT_POINTS`` —— 否则她拿到一个没有 publicExplanation
   也没有 nextAction 的 402,读完不知道该充值还是该改哪里。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal, Union

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.defensive_geo_api import _action, _safe_error, _tenant
from services.defensive_geo.copy_registry import (
    COPY_REGISTRY_VERSION, assert_public_copy_clean, user_label,
)
from services.defensive_geo.customer_links import (
    LINK_KINDS, CustomerLinksUnavailable, build_panel, reissue_supported,
)
from services.defensive_geo.legal_repair import (
    LegalRepairUnavailable, build_candidate_payload, candidate_prompt,
)
from services.defensive_geo.presentation import copy_registry as _pcopy
from services.defensive_geo.presentation import recommended_facts as rf
from services.defensive_geo.typed_error_route import TypedErrorRoute

logger = logging.getLogger("GEO-DefGeoAssist")


class AiAutofillEmpty(RuntimeError):
    """AI 一项都没查到。

    单独一个异常类型,不复用广告法那个 —— 复用会让日志里
    「广告法不可用」和「资料没查到」长得一样,排障时分不开。
    """


router = APIRouter(prefix="/api/defensive-geo", tags=["防御型 GEO · 销售自救面"],
                   route_class=TypedErrorRoute)


class _Envelope(BaseModel):
    """G-4:响应也 ``extra='forbid'`` —— 多返回一键 = 受控失败,不是裸 500。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _reason(key: str) -> str:
    """人话出口。取不到就抛(U-1:裸枚举上屏 = 红)。"""
    return assert_public_copy_clean(user_label("reason", key), field=f"reason.{key}")


def _require_brand(request: Request, brand_id: int) -> dict[str, Any]:
    """归属**每请求现做**。复用现役 ``auth.brand_access`` —— 不另写一套边界。"""
    try:
        from auth.brand_access import require_brand_access

        require_brand_access(request, brand_id)
    except HTTPException:
        # 跨租户与不存在**同形** —— 不靠错误码区分对象存不存在。
        raise _safe_error("NOT_FOUND")
    from db.diagnosis_db import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 🔴 [工单 V5-A 外发现 · 2026-08-28] 这条 SELECT 原本还点名
        #    ``city, business, description`` —— **三列在 brands 上都不存在**
        #    (生产 dump 2026-08-19 实核:brands 33 列里有 ``cities`` 与 ``city_scope``,
        #     没有 ``city``;``business`` 长在 ``client_profiles`` 上;``description`` 没有)。
        #    于是这条语句每次都 ``UndefinedColumn``,而 ``_require_brand`` 挡在
        #    **三个**端点前面(Z-3.1 AI 补齐 / GET 客户链接面板 / POST 重签)——
        #    三个从上线起就是确定性 500,一次都没成功过。
        #
        #    这与 ``CustomerLinksUnavailable`` 文档里记的那条(``agent_quotes``
        #    没有 ``brand_id`` 列)是**同一族**:那一轮修的是面板内部那条查询,
        #    而这一条排在它**前面**,先炸,所以那次修完仍然通不了。
        #
        #    列名照着"调用方想要哪些键"写、没照着"表上有哪些列"写 —— 这正是
        #    本仓 SQL 四维核验第一条(列名肉眼核对)要挡的东西。
        #    现在只取**真实存在**的列;``brand_field_suggester`` 对缺键是
        #    ``.get()`` 取 None 的容错形态,而那三个键它**本来也从未拿到过**。
        cur.execute(
            "SELECT id, name, company_name, industry "
            "FROM brands WHERE id = %s", (brand_id,),
        )
        row = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()
    if not row:
        raise _safe_error("NOT_FOUND")
    return dict(row)


# ══════════════════════════════════════════════════════════════════════════
# Z-3.1 · 「AI 联网补齐」第四出口
# ══════════════════════════════════════════════════════════════════════════
#: §15.6 ``RequestedFactKey`` → 现役 ``brand_field_suggester`` 字段。
#:
#: 🔴 **不是每一项 AI 都查得到**。把查不到的也当成"AI 能补",结果是她花了算力
#:    换回一堆空值 —— 那比不给这颗按钮更糟。所以这张表是**部分映射**,
#:    没有映射的 key 只走「自己填」,并且在响应里如实说明。
_FACT_KEY_TO_FIELD: dict[str, str] = {
    "legal_name": "business",          # 公司全称由业务描述链推断(现役 business prompt 带公司名)
    "official_website": "business",
    "service_scope": "service_scope",
    "product_scope": "business",
    "service_location": "city",
    "brand_alias": "keywords",
}

#: 现役 AI 覆盖不到的两项。**写成数据**,判据拿它当分母,不靠读注释。
AI_UNCOVERED_FACT_KEYS: tuple[str, ...] = tuple(
    k for k in rf.REQUESTED_FACT_KEYS if k not in _FACT_KEY_TO_FIELD
)


class AiAutofillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    brand_id: int = Field(alias="brandId", ge=1)
    requested_fact_keys: list[str] = Field(alias="requestedFactKeys", min_length=1, max_length=8)


class AiAutofillResponse(_Envelope):
    brand_id: int = Field(alias="brandId")
    #: AI 真的查了的那几项(逐项带建议值与人话字段名)。
    drafts: list[dict[str, Any]]
    #: AI 覆盖不到、只能自己填的那几项。
    manual_only: list[dict[str, str]] = Field(alias="manualOnly")
    public_explanation: str = Field(alias="publicExplanation")
    #: 四按钮(现役「永远不中断对话」形态)。
    actions: list[dict[str, Any]]
    copy_registry_version: str = Field(alias="copyRegistryVersion")


class _SuggesterCtx:
    """``suggest_brand_field`` 只从 ctx 取 deps.current_brand_id / current_profile_id。

    给它一个最小 shim 而不是伪造整个 agent ctx —— 伪造大对象会让判据
    以为这条链跑通了,其实跑的是夹具自己。
    """

    def __init__(self, brand_id: int) -> None:
        self.deps = type("_D", (), {"current_brand_id": brand_id, "current_profile_id": None})()


@router.post("/fact-collection/ai-autofill", response_model_by_alias=True)
async def fact_collection_ai_autofill(body: AiAutofillRequest, request: Request) -> dict[str, Any]:
    """Z-3.1 第四出口的**执行端点**。

    ``fact_collection`` 在规格里原本只有「等报价 / 等人工 / 等归属确认」三条路,
    对销售来说是同一件事:今天做不了。这一颗是她当场点得下去的那一个。

    🔴 零副作用:查到的值**不写库**。返回 draft,由她确认后走现役
       ``PUT /api/my-clients/{brand_id}`` 落库(确认后生效)。
    🔴 扣费复用现役 ``autofill_brand``,本文件不含任何价格数字。
    """
    _tenant(request)
    bad = [k for k in body.requested_fact_keys if k not in rf.REQUESTED_FACT_KEYS]
    if bad:
        raise _safe_error("VALIDATION_FAILED")
    brand = _require_brand(request, body.brand_id)

    coverable = [k for k in body.requested_fact_keys if k in _FACT_KEY_TO_FIELD]
    manual_only = [k for k in body.requested_fact_keys if k not in _FACT_KEY_TO_FIELD]

    drafts: list[dict[str, Any]] = []
    if coverable:
        from agents.brand_field_suggester import suggest_brand_field
        from api.content_api import _bill_ctx

        ctx = _SuggesterCtx(body.brand_id)
        try:
            # 🔴 一次调用扣一次。逐字段各扣一次是**自定价**的一种变体
            #    (同一个动作被拆成 N 次收费),Z-3.1 明令禁止。
            async with _bill_ctx(request, "autofill_brand"):
                for key in coverable:
                    field = _FACT_KEY_TO_FIELD[key]
                    card = await suggest_brand_field(ctx, field, brand=brand, profile=None)
                    drafts.append({
                        "factKey": key,
                        "factLabel": _pcopy.translate("requested_fact", key),
                        "targetField": field,
                        "suggested": card.get("action_payload", {}).get("value")
                        if isinstance(card.get("action_payload"), dict) else None,
                        "suggestedDisplay": str(card.get("suggested_display") or ""),
                        "confident": bool(card.get("needs_confirmation")),
                    })
                if not any(d["suggestedDisplay"] for d in drafts):
                    # 一条都没查到 ⇒ 抛,让 _bill_ctx 走"不扣"分支。
                    # 🔴 她付钱换回一堆空值,比一开始就说"没查到"糟得多。
                    raise AiAutofillEmpty("AI 这次一项都没查到")
        except HTTPException as exc:
            # _bill_ctx 的 402 是**框架裸信封**,翻成 typed —— 见模块头第三条。
            if exc.status_code == 402:
                raise _safe_error("INSUFFICIENT_POINTS")
            raise
        except AiAutofillEmpty:
            # 🔴 走到这里 _bill_ctx 已按"未成功"回滚,没有扣算力。
            raise _safe_error("POLICY_UNAVAILABLE")

    payload = {
        "brandId": body.brand_id,
        "drafts": drafts,
        "manualOnly": rf.requested_fact_labels(manual_only),
        "publicExplanation": _reason(
            "ai_autofill_needs_confirm" if drafts else "brand_facts_missing"),
        # 四按钮:确认 / 再查 / 自己填 / 取消(现役 confirm_card 形态)。
        "actions": [
            _action("confirm_ai_facts", target={"kind": "brand", "id": str(body.brand_id)}),
            _action("ai_autofill_facts", target={"kind": "brand", "id": str(body.brand_id)}),
            _action("edit_facts_myself",
                    target={"kind": "page", "page": f"/my-clients/{body.brand_id}"}),
            _action("cancel_ai_autofill", target={"kind": "page", "page": "back"}),
        ],
        "copyRegistryVersion": COPY_REGISTRY_VERSION,
    }
    return AiAutofillResponse.model_validate(payload).model_dump(by_alias=True)


# ══════════════════════════════════════════════════════════════════════════
# Z-3.3 · 广告法一键修复(按 rule_id 出候选改写)
# ══════════════════════════════════════════════════════════════════════════
class LegalRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    article_revision_id: str = Field(alias="articleRevisionId", min_length=1, max_length=64)
    rule_id: str = Field(alias="ruleId", min_length=1, max_length=120)
    passage_ref: str = Field(alias="passageRef", min_length=1, max_length=200)
    passage_excerpt: str = Field(alias="passageExcerpt", min_length=1, max_length=2000)


class LegalRepairResponse(_Envelope):
    article_revision_id: str = Field(alias="articleRevisionId")
    passage_ref: str = Field(alias="passageRef")
    #: 候选改写。**复数** —— 只给一条等于替她做决定。
    candidates: list[dict[str, str]]
    public_explanation: str = Field(alias="publicExplanation")
    actions: list[dict[str, Any]]
    copy_registry_version: str = Field(alias="copyRegistryVersion")


@router.post("/legal-repair/candidates", response_model_by_alias=True)
async def legal_repair_candidates(body: LegalRepairRequest, request: Request) -> dict[str, Any]:
    """Z-3.3:LLM **按 rule_id** 生成候选改写,销售确认/微调后成新 revision。

    🔴 「按 rule_id」不是修辞:改写 prompt 由命中的那条规则决定
       (规则来自 Owner 签发包,本文件零词表)。拿一个通用的"帮我改得合规一点"
       去改,改出来的东西既不针对这条规则,也没法证明改到位了。
    🔴 这里只出**候选**,不落库、不发布、零资金副作用。
       落成新 revision 是她确认之后的第二步,走现役 revision 写入路径。
    """
    _tenant(request)
    try:
        candidates = await build_candidate_payload(
            rule_id=body.rule_id,
            passage_excerpt=body.passage_excerpt,
            prompt=candidate_prompt(rule_id=body.rule_id, passage=body.passage_excerpt),
        )
    except LegalRepairUnavailable as exc:
        logger.info("[defgeo] 广告法候选改写不可用:%s", exc)
        raise _safe_error("POLICY_UNAVAILABLE")

    payload = {
        "articleRevisionId": body.article_revision_id,
        "passageRef": body.passage_ref,
        "candidates": candidates,
        # 逐字取规格给的那句(命中话术三件事都在:哪里不对 / 钱 / 出口)。
        "publicExplanation": _reason("legal_rule_hit_repairable"),
        "actions": [
            _action("apply_legal_repair",
                    target={"kind": "article_revision", "id": body.article_revision_id,
                            "passageRef": body.passage_ref}),
            _action("edit_legal_repair",
                    target={"kind": "article_revision", "id": body.article_revision_id,
                            "passageRef": body.passage_ref}),
        ],
        "copyRegistryVersion": COPY_REGISTRY_VERSION,
    }
    return LegalRepairResponse.model_validate(payload).model_dump(by_alias=True)


# ══════════════════════════════════════════════════════════════════════════
# Z-3.3 · 「用这一句」—— **真的**落成新 revision(工单 C-5 / Codex 终审 P1-12)
# ══════════════════════════════════════════════════════════════════════════
class LegalRepairApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    article_revision_id: str = Field(alias="articleRevisionId", min_length=1, max_length=120)
    rule_id: str = Field(alias="ruleId", min_length=1, max_length=120)
    passage_ref: str = Field(alias="passageRef", min_length=1, max_length=200)
    passage_excerpt: str = Field(alias="passageExcerpt", min_length=1, max_length=2000)
    #: 她最终选中/微调后的那一句。**不是**候选下标 —— 第二步的手工微调
    #: 让最终文本可能与任何一条候选都不同,传下标会把她的改动丢掉。
    chosen_text: str = Field(alias="chosenText", min_length=1, max_length=2000)


class LegalRepairApplyResponse(_Envelope):
    article_revision_id: str = Field(alias="articleRevisionId")
    passage_ref: str = Field(alias="passageRef")
    #: 落库之后的内容指纹。**这一位就是端到端判据**:它与落库前不同,
    #: 就证明「重新确认消费的是新 revision」。
    article_hash: str = Field(alias="articleHash")
    previous_article_hash: str = Field(alias="previousArticleHash")
    public_explanation: str = Field(alias="publicExplanation")
    actions: list[dict[str, Any]]
    copy_registry_version: str = Field(alias="copyRegistryVersion")


@router.post("/legal-repair/apply", response_model_by_alias=True)
async def legal_repair_apply(body: LegalRepairApplyRequest, request: Request) -> dict[str, Any]:
    """Z-3.3 的后半句:「销售确认/微调后**成新 revision**」。

    🔴 在这之前这后半句没有落点:前端只是带着改好的句子跳去 /writing,
       而那个 ``repaired`` 查询参数**全仓零消费者**(grep 实核)。
       点完"用这一句"什么都没发生 —— 重新确认冻的还是同一份正文、
       同一个 articleHash,发布门再拦一次。

    🔴 归属校验走现役 ``_require_article_access``(article → quote → brand),
       不另写一份:资金/权限相邻的谓词写两处必有一处没人验。
    🔴 零资金副作用:修复已付费内容的局部瑕疵不另计费(与现役
       ``POST /api/articles/{id}/repair-finding`` 同口径,那条端点
       ``"charged": False``)。本函数不 import 任何计费面。
    """
    _tenant(request)
    # revision id → 现役 articles.id。**复用**发布 worker 里那条解析,
    # 不在这里手写第二份 "article:<n>" 解析 —— 两份解析迟早对不上,
    # 而对不上的后果是改到另一篇文章。
    from services.defensive_geo.legal_repair import (
        LegalRepairAmbiguousError, LegalRepairApplyError, LegalRepairNotApplied,
        apply_repair,
    )
    from services.defensive_geo.publish.publish_worker import (
        BodyResolutionError, _article_id_of as _resolve_article_id,
    )

    try:
        article_id = _resolve_article_id(body.article_revision_id)
    except BodyResolutionError:
        raise _safe_error("VALIDATION_FAILED") from None

    from api.meijiehezi_api import _require_article_access

    _require_article_access(request, article_id)

    from db.connection import get_db

    try:
        with get_db() as conn:
            result = apply_repair(
                conn.cursor(),
                article_id=article_id,
                passage_ref=body.passage_ref,
                passage_excerpt=body.passage_excerpt,
                chosen_text=body.chosen_text,
            )
    except LegalRepairAmbiguousError as exc:
        # [工单 E3-2 · Codex 二审 P2] 多命中 ⇒ 歧义。
        # 🔴 **不可重试**(422 而不是 503):再点一次仍然是同样多处,
        #    要解决只能她把要改的那一段选得更完整。retryable=true 会让前端
        #    自动重试,而自动重试在这里永远不会成功 —— 那是一个死循环。
        logger.info("[defgeo] 广告法修复定位歧义:%s", exc)
        raise _safe_error(
            "VALIDATION_FAILED",
            reason_key="legal_repair_ambiguous",
            next_action=_action(
                "legal_repair_pick_again",
                target={"kind": "article_revision", "id": body.article_revision_id}),
        ) from exc
    except LegalRepairNotApplied as exc:
        # [工单 E3-2 · Codex 二审 P1-F7] 这一跳没做成,正文/两个 hash 全没动。
        # 🔴 **可重试**(POLICY_UNAVAILABLE = 503 + retryable):她的输入没问题。
        #    这条与下面那条 ApplyError 的区别是**责任方**,不是严重程度 ——
        #    合成一条会让"你给的句子不行"和"我们没做成"用同一句话解释,
        #    而她对这两件事该做的动作正好相反。
        logger.error("[defgeo] 广告法修复未应用(已整体回退):%s", exc)
        raise _safe_error(
            "POLICY_UNAVAILABLE",
            reason_key="legal_repair_not_applied",
            next_action=_action(
                "retry_legal_repair",
                target={"kind": "article_revision", "id": body.article_revision_id}),
        ) from exc
    except LegalRepairApplyError as exc:
        # 🔴 typed 拒绝 + 正文一个字不动。裸 500 会让她以为系统坏了、反复重试,
        #    而真因常常是"中间有人改过稿"这种她自己能处理的事。
        #
        # 🔴 [工单 V3-C · C-5 · Codex 三审 P2-2] 这里原来映射的是
        #    ``POLICY_UNAVAILABLE``(503 + retryable)。逐条看 raiser 就知道那是
        #    错档:空句 / 仍命中广告法目录 / 与原句一模一样 / 原话已不在正文里 /
        #    文章不存在 —— **全部**是她自己能处理的输入问题,没有一条是
        #    "我们的服务不可用"。retryable=true 会让前端自动重试一件永远不会
        #    成功的事,而她看到的仍然是"稍后再试"。
        #    改成 ``VALIDATION_FAILED``(422 + 不可重试)+ 重新选段的 next_action,
        #    与上面那条歧义分支同档 —— 它们的差别是**原因**不是严重程度。
        logger.info("[defgeo] 广告法修复落库被拒:%s", exc)
        raise _safe_error(
            "VALIDATION_FAILED",
            reason_key="legal_repair_input_rejected",
            next_action=_action(
                "legal_repair_pick_again",
                target={"kind": "article_revision", "id": body.article_revision_id}),
        ) from exc

    payload = {
        "articleRevisionId": body.article_revision_id,
        "passageRef": body.passage_ref,
        "articleHash": result["articleHash"],
        "previousArticleHash": result["previousArticleHash"],
        "publicExplanation": _reason("legal_repair_applied"),
        "actions": [
            _action("retry_publish_confirm",
                    target={"kind": "article_revision", "id": body.article_revision_id}),
        ],
        "copyRegistryVersion": COPY_REGISTRY_VERSION,
    }
    return LegalRepairApplyResponse.model_validate(payload).model_dump(by_alias=True)


# ══════════════════════════════════════════════════════════════════════════
# U-9 · 「发给客户」统一面板
# ══════════════════════════════════════════════════════════════════════════
class CustomerLinksResponse(_Envelope):
    brand_id: int = Field(alias="brandId")
    links: list[dict[str, Any]]
    hint: str
    copy_registry_version: str = Field(alias="copyRegistryVersion")


# 🔴 [工单 V5-C · C-3 · Codex fix-of-fix2 P2-3] 下面那条 docstring 里
#    **不列类别、也不写数量**。实际下发哪几类 = ``customer_links.LINK_KINDS``
#    减去 ``SCOPED_OUT_KINDS``(见 ``build_panel``)。
#    上一版在 docstring 里手抄了一份类别清单,而代码层早已把其中一类
#    scoped-out —— 代码藏起来了,说明书还写着。OpenAPI 是**对外**的:
#    上面写着的东西必须真能拿到,否则就是替一条不存在的能力打包票。
#    改成写死"三类"同样不行:数量由常量决定,手抄的那份下次照样烂。
#
# 🔴 这段说明**只能待在这里**,不能写进 docstring。
#    FastAPI 把 docstring 原样发进 OpenAPI ——
#    连"我们不再承诺 X"这句话本身也会把 X 送上说明书,
#    然后被 test_openapi_never_mentions_a_scoped_out_link_kind 判红。
#    这一条是判据当场抓出来的,不是事后补的说明。
@router.get("/customer-links", response_model_by_alias=True)
async def customer_links(
    request: Request,
    brand_id: int = Query(..., alias="brandId", ge=1),
) -> dict[str, Any]:
    """U-9:发给客户的 token 链接统一生成。

    每条都带**人话前缀**(【诊断报告】…),复制出去就是能直接发微信的一整段。
    过期/撤销的那几条带 ``reissuable`` —— 前端据此画「重新签发一个新链接」。

    🔴 只读。签发新链接是 ``POST /customer-links/reissue`` 那条路,
       GET 顺手换 token 会让"打开一下页面"变成"把客户手里的旧链接弄失效"。
    """
    _tenant(request)
    brand = _require_brand(request, brand_id)
    try:
        links = build_panel(brand_id=brand_id, brand_name=str(brand.get("name") or ""))
    except CustomerLinksUnavailable as exc:
        # 🔴 [工单 C-4 · Codex 终审 P1-11] 读不出来就**明说**,不再静默 not_ready。
        #
        #    改动前 build_panel 自己吞掉异常回一组 not_ready,于是
        #    「这一步还没做到」与「我们的查询写错了」在屏幕上一模一样。
        #    实际发生的正是后者(agent_quotes 没有 brand_id 列),报价与门户
        #    两类从上线起恒 not_ready,而没有任何东西报错。
        #
        #    503 + retryable 是对的档:这不是"没到时候"(那是 not_ready),
        #    也不是"你没权限",而是**我们这边这次读不出来**。
        logger.error("[defgeo] 客户链接面板不可用 brand=%s:%s", brand_id, exc)
        raise _safe_error("POLICY_UNAVAILABLE") from exc
    payload = {
        "brandId": brand_id,
        "links": links,
        "hint": assert_public_copy_clean(
            _pcopy.translate("sentence", "customer_links_hint"), field="links.hint"),
        "copyRegistryVersion": COPY_REGISTRY_VERSION,
    }
    return CustomerLinksResponse.model_validate(payload).model_dump(by_alias=True)


class _QuoteObjectRef(BaseModel):
    """面板下发的「报价」对象引用。``id`` 是 **int**,不是"看起来像数字的东西"。

    🔴 ``extra="forbid"`` 只加在**请求**模型上。本仓有过三次
       「在 response_model 上 forbid 把出口炸掉」的历史,所以这条边界写死:
       请求侧 forbid(挡住多塞的字段),响应侧不 forbid。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["quote"]
    id: int = Field(ge=1)


class _SelectionSessionObjectRef(BaseModel):
    """面板下发的「报价单·请确认」对象引用 —— 它的身份是 ``token``,不是 id。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["keyword_selection_session"]
    token: str = Field(min_length=1, max_length=128)


#: 按 ``kind`` 判别的闭集。两类各自带**自己那一格身份字段**:
#: 报价是 ``id:int``、选词会话是 ``token:str``。
#: 判别式 union 让"未知 kind"在入口就是 422,而不是走到某个分支里才崩。
ObjectRef = Annotated[
    Union[_QuoteObjectRef, _SelectionSessionObjectRef],
    Field(discriminator="kind"),
]


class ReissueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    brand_id: int = Field(alias="brandId", ge=1)
    #: 🔴 闭集校验放 validator 而不是 ``Literal[...]``:
    #:    LINK_KINDS 是**运行时元组**,Literal 只吃字面量。
    #:    在这里手抄一份字面量 = 同一个闭集写两处,必有一处漏改。
    kind: str = Field(min_length=1, max_length=40)

    #: 🔴 [工单 V3-A · Codex 三审 P1-8] GET 面板下发的那个 ``objectRef``,**必传**。
    #:    改动前 POST 只有 brandId+kind,服务端再跑一次"取最新" ——
    #:    两次取数之间新建一份报价,她看的是 A、被轮换的是 B。
    #:    做成**必填**而不是可选:可选等于把旧的那条不安全路径原样留着,
    #:    而留着的那条不会有任何判据在守。
    #:
    #: 🔴 [工单 V5-A · Codex fix-of-fix2 P2-2] 从 ``dict[str, Any]`` 收成
    #:    **discriminated typed model**。上一版 validator 只看 ``kind`` 在不在、
    #:    字段数超没超,``id`` 是什么完全不管;于是 ``{"kind":"quote","id":"abc"}``
    #:    一路走到 ``int(expected_object_ref["id"])`` 抛 ``ValueError``,
    #:    被 route class 收成 **500 INTERNAL_ERROR**(「稍后再试」——
    #:    而再试一万次也不会好)。类型不合法是**她这次请求**的问题,
    #:    该在入口就 422 说清楚,不该冒充我们这边的故障。
    object_ref: ObjectRef = Field(alias="objectRef")

    @field_validator("kind")
    @classmethod
    def _kind_in_closed_set(cls, v: str) -> str:
        if v not in LINK_KINDS:
            raise ValueError(f"未知链接类别 {v!r};合法 = {list(LINK_KINDS)}")
        return v


class ReissueResponse(_Envelope):
    brand_id: int = Field(alias="brandId")
    kind: str
    link: dict[str, Any]
    public_explanation: str = Field(alias="publicExplanation")
    copy_registry_version: str = Field(alias="copyRegistryVersion")


@router.post("/customer-links/reissue", response_model_by_alias=True)
async def customer_links_reissue(body: ReissueRequest, request: Request) -> dict[str, Any]:
    """U-9:「过期/撤销 → 一键重新签发**同对象**新链接」。

    🔴 「同对象」是重点:重签的是**同一个报价 / 同一份报告**的新 token,
       不是新建一个对象。新建对象会让客户手里那份和她这边看的对不上。
    🔴 不是每一类都能重签(现役只有门户 token 有轮换原语)。
       不能重签的如实拒,并给出人工出口 —— 假装能重签然后什么也没发生,
       是最坏的一种"看着能点"。
    """
    _tenant(request)
    brand = _require_brand(request, body.brand_id)
    if not reissue_supported(body.kind):
        raise _safe_error("POLICY_UNAVAILABLE")
    from services.defensive_geo.customer_links import reissue_link

    from services.defensive_geo.customer_links import (
        CustomerLinkNotExtendable, CustomerLinkObjectDrifted,
    )

    user = getattr(request.state, "user", None) or {}
    try:
        link = reissue_link(
            kind=body.kind, brand_id=body.brand_id,
            brand_name=str(brand.get("name") or ""),
            # typed model → 原样的 dict。``reissue_link`` 的对象比对是
            # 字符串化后逐格比,给它 dict 就够,不必让它认识 pydantic 模型。
            expected_object_ref=body.object_ref.model_dump(),
            # [P2-8] 凭据轮换落 actor/request 审计 —— 以前这里一个都没传,
            #        于是 defgeo 这条轮换路径在 audit_logs 里是空白的。
            actor_user_id=user.get("user_id"),
            actor_username=user.get("username") or user.get("display_name"),
            request_id=str(getattr(request.state, "request_id", "") or "") or None,
        )
    except (CustomerLinkObjectDrifted, CustomerLinkNotExtendable):
        # 🔴 [工单 V5-A · Codex fix-of-fix2 P1-5] 两个异常收在同一个出口。
        #    ``CustomerLinkNotExtendable`` 以前**没人接**:面板读出来之后,
        #    会话被并发推进到 confirmed,重签就抛它,由 route class 兜成
        #    typed **500 INTERNAL_ERROR**。500 的含义是「我们这边坏了,稍后再试」,
        #    可这件事再试一万次也不会好 —— 会话不会自己退回选词态。
        #    它和 drift 说的是**同一句话**:你看到的那一份已经不是现在这一份了,
        #    请重新看一眼再操作。所以复用 ``SNAPSHOT_CHANGED``(409)+ 同一个
        #    ``new_preview`` 出口,不新造错误码(新码 = 新文案 + 新注册 +
        #    再来一轮闭集核对,而要表达的东西完全同义)。
        #
        # 🔴 typed drift:她看到的那个对象已经不是当前对象了。
        #    复用 ``SNAPSHOT_CHANGED``(409)—— 语义逐字就是「你看到的那份变了,
        #    请重新看一眼再操作」,而且它已经有登记过的用户文案与 nextAction。
        #    不新造错误码:新码要新文案、新注册、新一轮闭集核对,
        #    而这里要表达的东西与既有那一个**完全同义**。
        raise _safe_error(
            "SNAPSHOT_CHANGED", reason_key="snapshot_changed",
            next_action=_action("new_preview",
                                target={"kind": "page", "page": "customer_links"})) from None
    if link is None:
        raise _safe_error("NOT_FOUND")
    payload = {
        "brandId": body.brand_id,
        "kind": body.kind,
        "link": link,
        "publicExplanation": _reason("customer_link_expired"),
        "copyRegistryVersion": COPY_REGISTRY_VERSION,
    }
    return ReissueResponse.model_validate(payload).model_dump(by_alias=True)


def census() -> dict[str, Any]:
    """机械导出本包新增的三条链。判据拿它当分母。"""
    return {
        "routes": sorted(
            f"{list(r.methods)[0]} {r.path}" for r in router.routes if getattr(r, "methods", None)
        ),
        "requestedFactKeys": list(rf.REQUESTED_FACT_KEYS),
        "aiCoveredFactKeys": sorted(_FACT_KEY_TO_FIELD),
        "aiUncoveredFactKeys": list(AI_UNCOVERED_FACT_KEYS),
        "linkKinds": list(LINK_KINDS),
        "reissuableKinds": sorted(k for k in LINK_KINDS if reissue_supported(k)),
    }
