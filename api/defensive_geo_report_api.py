"""防御型 GEO 报告呈现路由(CUR-01)—— **只读**。

一个端点:告诉前端"这份报告该走新五卡还是 legacy",以及走新的时给出五卡投影。

🔴 单独一个文件、不塞进 `api/defensive_geo_api.py`
--------------------------------------------------
那个文件是窗A 的两阶段 façade;本包与它**并行开发**,共用一个文件必然在
路由注册段撞车。分文件后各自 include_router,合流时零冲突。

🔴 对客 terminal 呈现仍受签发闸约束
-----------------------------------
未签 policy 时,``crop_for_audience`` 对客受众直接抛 ``PolicyNotSigned``;
本端点把它翻成 **200 + notSigned 标记**,而不是 4xx ——
服务商自己看报告不该因为"Owner 还没签对客口径"就打不开页面。
对客受众(customer/pdf)则如实拒绝。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from services.defensive_geo.typed_error_route import TypedErrorRoute

logger = logging.getLogger("GEO-DefGeo-Report")


class _CardsUnbound(Exception):
    """[工单 E3-4 · P1-10] 五卡绑不到 snapshot / 采样窗 ⇒ **隐藏这张卡**。

    🔴 用一个**独立**类型而不是复用下面那条兜底 ``except Exception``:
       "没有可绑的运行"是一个正常业务状态(这个品牌还没出过 v2 报告快照),
       而那条兜底会把它记成 warning 噪声。两件事混在一条日志里,
       真正的呈现故障就再也捞不出来了。
    """

#: [返修③ P1-5] 与 façade 同一个 route_class —— 两个 router、一种信封。
router = APIRouter(prefix="/api/defensive-geo", tags=["防御型 GEO · 报告呈现"],
                   route_class=TypedErrorRoute)


class _Strict(BaseModel):
    # 🔴 本仓三次生产事故都源自 response_model + extra=forbid 多返回一个键 → 裸 500。
    #    这里 forbid 的是**入参**;出参形状由本文件唯一构造,不拼未知键。
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CardOut(_Strict):
    key: str
    question: str
    ordinal: int
    level_key: str = Field(alias="levelKey")
    level_label: str = Field(alias="levelLabel")
    level_tone: str = Field(alias="levelTone")
    state: str
    actions: list[str]


#: CardOut 的 alias 全集 —— 从模型**机械导出**,不手抄。
#: 用它过滤 producer payload:多出来的键(如 provider 面的 summary)不上 wire,
#: 而不是让 ``extra='forbid'`` 在构造期把端点炸成 500。
_CARD_OUT_ALIASES: frozenset[str] = frozenset(
    (f.alias or name) for name, f in CardOut.model_fields.items()
)


class ReportPresentationResponse(_Strict):
    diagnosis_id: int = Field(alias="diagnosisId")
    #: false ⇒ 前端走 legacy 分支(ReportV2View / Markdown)。CUR-01 的开关。
    is_v2: bool = Field(alias="isV2")
    campaign_mode: Optional[str] = Field(default=None, alias="campaignMode")
    mode_user_label: Optional[str] = Field(default=None, alias="modeUserLabel")
    question_plan_id: Optional[str] = Field(default=None, alias="questionPlanId")
    question_plan_revision: Optional[int] = Field(
        default=None, alias="questionPlanRevision")
    #: 对客终态呈现是否已获 Owner 签发。未签时前端只画 fixture/shadow 水印版。
    customer_presentation_signed: bool = Field(
        default=False, alias="customerPresentationSigned")
    unsigned_policies: list[str] = Field(
        default_factory=list, alias="unsignedPolicies")
    cards: list[CardOut] = Field(default_factory=list)
    projection_version: Optional[str] = Field(default=None, alias="projectionVersion")


def _db():
    from db.connection import get_connection
    return get_connection()


# ══════════════════════════════════════════════════════════════════════════
# [返修② 1] 对象级归属 —— 每个按 diagnosis_id 取数的端点都必须先过这道门
# ══════════════════════════════════════════════════════════════════════════
def _not_found() -> HTTPException:
    """跨租户与不存在返回**同形**信封。

    复用 `api.defensive_geo_api._safe_error` 而不是在这里另写一个 404:
    信封形状(code/retryable/publicExplanation/nextAction)只该有一处定义。
    两处各写一份,迟早有一处少一格 —— 而这一格恰恰是"她看到的是什么"。
    """
    from api.defensive_geo_api import _safe_error

    return _safe_error("NOT_FOUND")


def _require_diagnosis_ownership(request: Request, diagnosis_id: int) -> None:
    """诊断必须属于当前租户,**读任何数据之前**。

    🔴 这里修的是一个真 IDOR:``diagnosis_id`` 是自增整数,可枚举。
       本端点原来只验"登录了没",不验"这份诊断是不是你的" ——
       任何已登录用户 for 循环打一遍就能拿到全站每份报告的
       campaign_mode / 题单 id / 题单版本。那是别人客户的商业信息。

    🔴 归属判定复用现役 ``require_diagnosis_access``,不另造第二套
       (它走 ``diagnosis_records.brand_id → brands.owner_user_id``,
       并且对 NULL-brand 残缺行 **fail-closed**,那正是 GEO-R1-CAN-139
       修过的同一类洞)。另写一套归属谓词 = 两套规则迟早分叉,
       而分叉的那一天没有任何判据会变红。

    🔴 拒绝一律翻成同一个 NOT_FOUND 信封:``require_diagnosis_access`` 自己
       会按"不存在"给 404「诊断记录不存在」、按"不是你的"给 404「资源不存在」、
       按"没关联品牌"给 403 —— 三种文案就是三个枚举 oracle,
       攻击者据此能问出"这个 id 到底存不存在"。同形之后问不出来。
    """
    from auth.brand_access import require_diagnosis_access

    try:
        require_diagnosis_access(request, diagnosis_id, allow_null=False)
    except HTTPException:
        raise _not_found() from None


@router.get("/reports/{diagnosis_id}/presentation",
            response_model=ReportPresentationResponse,
            response_model_by_alias=True)
async def get_report_presentation(
    diagnosis_id: int, request: Request
) -> ReportPresentationResponse:
    """解析报告绑定 + (若是 v2)给出五卡投影。"""
    user = getattr(request.state, "user", None)
    if not user:
        # [返修③ P1-5] 原来这里是手写的裸信封 {"code": "NOT_AUTHENTICATED"} ——
        # 三形态里的「minimal typed」那一种:没有 publicExplanation ⇒ 前端弹窗空白,
        # 没有 nextAction ⇒ 她读完不知道该干什么。现在走同一个家族。
        from api.defensive_geo_api import _safe_error

        raise _safe_error("NOT_AUTHENTICATED")

    # 🔴 归属在**取数之前**。先读后授权即便最终拒绝,数据也已经出了库,
    #    而任何一次 except 漏网都会把它带出去。
    _require_diagnosis_ownership(request, diagnosis_id)

    from services.defensive_geo.presentation import registries as R
    from services.defensive_geo.presentation.report_binding import resolve_binding

    conn = _db()
    try:
        with conn.cursor() as cur:
            # 🔴 [窗D 合流微单 2026-08-22] 授权**不在这里** ——
            #    它已经在上面由窗A 的 ``_require_diagnosis_ownership`` 做完了
            #    (走现役 ``require_diagnosis_access``,typed 同形信封,
            #    且排在**取数之前**)。
            #
            #    窗D 原来在这一格有一份内联授权(SELECT brand_id +
            #    require_brand_access + 裸 404)。两份并存是本仓记过的
            #    「同一谓词写两处 ⇒ 必有一处没人验」:
            #      · 我那份返的是**裸** 404 字典,窗A 那份返 SafeError 信封 ——
            #        两种形状同时存在,客户端得按哪一种解析?
            #      · 归属规则分叉的那一天,不会有任何判据变红。
            #    所以这里只留**卡片链要用的 brand_id 查询**,它不再承担授权职责。
            cur.execute(
                "SELECT brand_id FROM diagnosis_records "
                " WHERE id = %s AND COALESCE(is_deleted, FALSE) = FALSE",
                (int(diagnosis_id),))
            _owner_row = cur.fetchone()
            # 上面已授权通过,这里查不到只可能是并发删除 —— 卡片链无数据可算,
            # 如实留白即可,**不**在这里再造一个 404 分支(那会变成第二个
            # "对象存在性" oracle,与窗A 的同形信封打架)。
            _brand_id = None
            if _owner_row:
                _brand_id = (_owner_row["brand_id"] if isinstance(_owner_row, dict)
                             else _owner_row[0])

            binding = resolve_binding(cur, diagnosis_id)
            cards_payload = []
            if binding.is_v2 and _brand_id is not None:
                # 五卡数据 —— 全链:attempt 账本 → evidence → summary → cards。
                # 账本没上(迁移 046 未执行)时 producer 返空,如实留白。
                try:
                    from services.defensive_geo.monitoring import (
                        card_producer as _cards,
                    )
                    # [工单 E3-4 · P1-10 第二半 · Codex 二审] 五卡必须绑
                    # **snapshot / 采样窗**,绑不上就**隐藏这张卡**。
                    #
                    # 🔴 这条查询原来只按 brand_id 取格,不带任何时间/运行边界。
                    #    后果不是"多算几格":这份报告是**冻结**的一份结论,
                    #    而卡片会跟着这个品牌**未来每一次**监测漂 ——
                    #    今天打开是一个数,下周打开是另一个数,报告却还是同一份。
                    #    她拿这份报告去跟客户对账时,对不上的是我们。
                    #
                    # 🔴 绑定键取 ``defgeo_report_snapshots`` 的采样窗
                    #    (§6.1 十一项冻结里的 sampling_window_start/end)——
                    #    它就是"这份报告看的是哪一段时间"的权威答案。
                    #    取最新那一版(revision 最大)。
                    # 🔴 取不到 snapshot ⇒ ``_window`` 为 None ⇒ **不查、不出卡**。
                    #    宁可这张卡不出现,也不出一张会自己变的卡。
                    # 🔴 [工单 V3-C · C-4 · Codex 三审 P1-6] 取的是**冻结面**,
                    #    所以三项冻结键一起取,一项都不能少:
                    #      · sampling_window_* —— 这份报告看的是哪一段时间;
                    #      · cutoff_at         —— 冻结在哪一刻(迟到 retry 的分界);
                    #      · raw_result_ids    —— 冻结时认的是哪些 raw result。
                    #    上一版只取采样窗,于是 cutoff 之后的 retry 会把一份
                    #    **已经发给客户**的报告改写(她拿去对账时对不上的是我们)。
                    cur.execute(
                        "SELECT sampling_window_start, sampling_window_end, "
                        "       cutoff_at, raw_result_ids, plan_snapshot_id "
                        "  FROM public.defgeo_report_snapshots "
                        " WHERE diagnosis_id = %s AND brand_id = %s "
                        " ORDER BY revision DESC, id DESC LIMIT 1",
                        (int(diagnosis_id), _brand_id))
                    _window = cur.fetchone()
                    if not _window:
                        logger.info(
                            "[defgeo-report] 诊断 %s 没有报告快照,五卡按"
                            "「绑不了就隐藏」留白(不按品牌汇总全部历史监测)",
                            diagnosis_id)
                        raise _CardsUnbound
                    # 🔴 取格也要绑 cutoff:冻结之后新建的格不属于这份报告。
                    #    (采样窗右端与 cutoff 通常相同,但它们是**两个**冻结项 ——
                    #     当成一个用,窗改了 cutoff 没改的那天就说不清了。)
                    cur.execute(
                        "SELECT DISTINCT plan_hash FROM public.monitoring_run_cells "
                        " WHERE brand_id = %s AND plan_hash IS NOT NULL "
                        "   AND created_at >= %s AND created_at <= %s "
                        "   AND created_at <= %s",
                        (_brand_id,
                         _window["sampling_window_start"],
                         _window["sampling_window_end"],
                         _window["cutoff_at"]))
                    _pcids = [r["plan_hash"] for r in (cur.fetchall() or [])]
                    if _pcids:
                        cards_payload = _cards.cards_as_payload(
                            _cards.produce_cards(
                                cur, plan_cell_ids=_pcids,
                                cutoff_at=_window["cutoff_at"],
                                raw_result_ids=_window["raw_result_ids"]))
                except _CardsUnbound:
                    # 绑不到快照 —— 这不是"失败",是**按规矩隐藏**。
                    # 与下面那条分开是为了不把"没有可绑的运行"记成 warning 噪声。
                    cards_payload = []
                except Exception as exc:
                    # 🔴 呈现失败**不阻断**报告页(§20.3「report presentation 失败:
                    #    保留上一个冻结 snapshot,不回退到实时拼装假报告」)。
                    #    如实留空,而不是编一份看起来完整的五卡。
                    logger.warning("五卡投影失败,如实留白:%s", exc)
                    cards_payload = []
    finally:
        conn.close()

    if not binding.is_v2:
        # legacy:如实说"不是 v2",让前端走原路径。**不编一个空的五卡**。
        return ReportPresentationResponse(
            diagnosisId=diagnosis_id, isV2=False,
        )

    unsigned = list(R.unsigned_policies())
    return ReportPresentationResponse(
        diagnosisId=diagnosis_id,
        isV2=True,
        campaignMode=binding.campaign_mode,
        modeUserLabel=binding.mode_user_label,
        questionPlanId=binding.question_plan_id,
        questionPlanRevision=binding.question_plan_revision,
        customerPresentationSigned=not unsigned,
        unsignedPolicies=unsigned,
        # [窗D 2026-08-22] 指标 producer 已接上:
        # services/defensive_geo/monitoring/card_producer.produce_cards
        # 全链驱动(attempt 账本 → evidence → summary → cards),
        # 无观测数据时仍是**如实留白**的空列表,不造假数。
        cards=[
            # 🔴 **按 CardOut 声明字段过滤**,不是 ``CardOut(**c)``。
            #    producer 的 payload 还带 ``summary``(provider 面要的
            #    SampleSummary),而 CardOut 是 ``extra='forbid'`` 的 wire 模型 ——
            #    直接展开会在**构造期**抛,端点裸 500。
            #    这正是本仓三次生产事故的形态(response_model + forbid)。
            #    过滤 = 受控:多出来的键静默不上 wire,而不是把端点炸掉。
            CardOut(**{k: v for k, v in c.items()
                       if k in CardOut.model_fields
                       or k in _CARD_OUT_ALIASES})
            for c in cards_payload
        ],
        projectionVersion="defensive-geo-presentation-v2",
    )
