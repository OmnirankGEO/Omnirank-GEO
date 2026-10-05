"""Article review state and the explicitly enabled publication hard gate."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
import os
from typing import Any, Final

from services.standard_citation_guard import evaluate_standard_citation_discipline

logger = logging.getLogger("GEO-ReviewGate")


_TRUE: Final = frozenset({"1", "true", "yes", "on"})
# [WP9-P0-7 · D8 ④] skipped = 用户对 company_facts/brand_softarticle 的推荐人审"明示跳过"
# (审计落跳过人/时间/规则版本);它只放行推荐人审,绝不越过 legal_hard/platform_profile_hard
# (评估顺序上法律/平台档在人审之前,天然拦在跳过之前)。
HUMAN_DECISIONS: Final = frozenset({"approved", "rejected", "skipped"})

#: `set_human_review(decision='approved')` 会当场报错的 eligibility.reason。
#:
#: 🔴 [§3A 同步改 2026-08-01] 原集合还含 `blocked` / `rewrite_required` /
#: `standard_citation_content_claim`,理由是"门在签发分支之前就拦住了,记下的 approved
#: 永远兑现不了"。内容类降级后**这条理由正好反转**:门不再拦 → 签发当场生效 →
#: 再抛错就成了"能发布却不能签发"的自相矛盾(用户点签发报错,直接点发布反而成功)。
#: 故三项移出,只留仍是 H0 的 lineage 两项 —— 那两项签了也兑现不了,必须当场报错。
#:
#: 🔴🔴 提成模块常量而不是留在函数里,是因为 tests/ 曾**硬编码一份副本**:
#: 我改了函数里的集合,那条测试照样绿 —— 它断言的是一个已经不成立的事实(假绿)。
#: 测试现在直接 import 本常量,漂移源被消掉。
HUMAN_APPROVAL_BLOCKING_REASONS: Final = frozenset({
    "content_changed_after_review",
    "evidence_changed_after_review",
})


# ===========================================================================
# [发布门三态拆分 2026-07-31 · 工单 §4.1] 三个正交字段
# ===========================================================================
# 病根:单一 `eligible` 同时承载三种互不相干的语义(机审跑没跑过 / A1 提示处理没处理
# / 真正的对外发布阻断),于是"还没跑过机审"被当成了"不许对外发布"。
#
# 🔴 生产实测(2026-07-31,当前生产尖 eac2200b,真函数逐篇跑完 1335 篇):
#     legacy_unreviewed 1110 篇 **全部** article_review IS NULL
#     → 它们撞的是下面那道 content/evidence 快照比对,**根本走不到兜底分支**,
#       返回的是 `content_changed_after_review`(reason_class 为 None)。
#     所以只改兜底分支 = 一篇都放不出来。真正的修法是把"快照缺失(压根没审过)"
#     与"快照存在但对不上(审完又被改了)"拆开——前者是 not_run,后者才是 H0。
REVIEW_NOT_RUN: Final = "not_run"
REVIEW_COMPUTED: Final = "computed"

ADVISORY_NONE: Final = "none"
ADVISORY_OPEN: Final = "open"
ADVISORY_ACKNOWLEDGED: Final = "acknowledged"
ADVISORY_REPAIRED: Final = "repaired"

#: 真正的对外发布阻断。**只有 clear 才 eligible**。
H0_CLEAR: Final = "clear"
H0_LEGAL: Final = "legal_hard"
H0_PLATFORM: Final = "platform_profile_hard"
#: 完整性/授权/运营裁决类硬门合并桶:跨租户、越权、资金守恒、对象 hash lineage 不一致、
#: 对象不存在、以及**人工明确拒稿**(§4.2 反向要求:这些仍是 H0)。
#:
#: [P3 并入项 1 · 2026-08-01] 档名由 `tenant_or_funds` 改为 `operator_hard`(复审
#: REVIEW_VERDICT_PUBLISH_GATE_TRISTATE_P1 §3.2 裁定)。旧名**名不副实**:这个桶里
#: 占比最大的两类(人工明确拒稿 / hash lineage 不一致)既不是租户问题也不是资金问题,
#: 下一任读到 `publication_h0_state == 'tenant_or_funds'` 会误判成越权或资金守恒失败。
#: 🔴 **纯命名,不改行为**:成员集合、判定顺序、`reason` 串、`eligible` 派生全部一字未动
#: —— 调用方要区分具体是哪一类,读的仍然是 `reason`(human_rejected /
#: content_changed_after_review / …),从来不是这个桶名。
H0_OPERATOR_HARD: Final = "operator_hard"

# ===========================================================================
# [内容审核 AI 化 · 核验流融合 2026-08-01 · 工单 §3A] 内容类提示级
# ===========================================================================
# Owner 08-01 拍板:"广告法在写作的提示词中写清楚不要触碰,尽量降低违规可能,剩下的
# 客户审核不审核,是他自己决定。" → 内容类硬门(法律/平台档/编造型证据)**一律不再
# 阻塞发布**,降为提示级;机审照跑、结论照存,只是不再把人堵在发布门外。
#
# 🔴 仍是 H0 的只剩 `operator_hard` 一桶:资金 / 租户隔离 / 对象完整性(hash lineage、
#    对象不存在)/ 人工明确拒稿(§3A 点名保留——那是运营者自己的明确决定)。
#
# 🔴🔴 结构上最关键的一点(P1 作者在 216-226 行已栽过同型的坑,本单不许重蹈):
#    降级**不等于**"在原地 return 一个 clear"。原来这三支都是 `return`,若只把
#    h0 改成 clear 就地返回,一篇 machine='blocked' 且 human='rejected' 的文章会在
#    blocked 分支直接返回 clear,**绕过下面的人工拒稿 H0** —— 降级过界,锁 2 必须抓住。
#    正确做法是把提示**累积进 content_notices 后继续往下走**,让 operator_hard 类
#    检查照常执行,h0 由后续分支决定。
NOTICE_NONE: Final = "none"
NOTICE_OPEN: Final = "open"

#: 内容类提示的类别标识。刻意沿用原 `reason_class` 的取值串,使前端/审计/日志
#: 在降级前后读到的是同一套词汇,不需要翻译表。
NOTICE_LEGAL: Final = "legal_hard"
NOTICE_PLATFORM: Final = "platform_profile_hard"
NOTICE_EVIDENCE_FABRICATION: Final = "evidence_fabrication_hard"

#: 降级为提示级的全部内容类。反向锁用它断言"不在这张表里的一律仍是 H0"。
DOWNGRADED_CONTENT_NOTICE_CLASSES: Final = frozenset(
    {NOTICE_LEGAL, NOTICE_PLATFORM, NOTICE_EVIDENCE_FABRICATION}
)

#: 一篇文章可能同时命中多类。顶层兼容字段取"最该先说的那一条",顺序即严重度。
_NOTICE_PRIORITY: Final = (NOTICE_LEGAL, NOTICE_EVIDENCE_FABRICATION, NOTICE_PLATFORM)


def _primary_notice(notices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按严重度取主提示。

    🔴 存在的理由是**向后兼容**:降级只该改"拦不拦"(`eligible` /
    `publication_h0_state` / `overridable`),不该顺手把 `reason_class` / `message` /
    `actions` 从顶层挪走。那些字段有一堆既有消费方(前端徽章、reason 分层测试、
    span 级修复端点的高风险判定),挪走就是无谓的破坏面 —— 降级包不该附带 API 改造。
    """
    if not notices:
        return None
    by_class = {str(n.get("class") or ""): n for n in notices}
    for cls in _NOTICE_PRIORITY:
        if cls in by_class:
            return by_class[cls]
    return notices[0]


def _advisory_state(quality_warning: Any) -> tuple[str, int]:
    """A1 提示的处理情况(与 mark-reviewed 端点同一份落库口径)。

    open 计数取 `evidence.soft[*]` + `evidence_precision.warnings[*]` 两路 ——
    这正是 server.py `/api/topics/{id}/mark-reviewed` 判"有没有待确认提示"用的两路,
    两处必须同源,否则前端显示 N 条、后端说没有。
    """
    payload = quality_warning if isinstance(quality_warning, dict) else {}
    human_continue = payload.get("human_continue")
    acknowledged = bool(
        isinstance(human_continue, dict) and human_continue.get("acknowledged")
    )
    soft = payload.get("evidence")
    soft_items = soft.get("soft") if isinstance(soft, dict) else None
    precision = payload.get("evidence_precision")
    warn_items = precision.get("warnings") if isinstance(precision, dict) else None
    open_count = (
        (len(soft_items) if isinstance(soft_items, list) else 0)
        + (len(warn_items) if isinstance(warn_items, list) else 0)
    )
    if acknowledged:
        return ADVISORY_ACKNOWLEDGED, open_count
    if open_count > 0:
        return ADVISORY_OPEN, open_count
    # [P1 已知缺口·诚实登记] `repaired` 目前**没有写入方**:repair-finding 修完是直接
    # 重算 evidence/evidence_precision(提示条目消失),不留修复痕迹。读侧先按标记实现,
    # 写侧属 P2(P2 拥有修复链路)。生产当前不会出现 repaired —— 这一点写进交付说明,
    # 不靠"锁全绿"假装它已经在跑。
    repair_mark = payload.get("advisory_repair")
    if isinstance(repair_mark, dict) and repair_mark.get("repaired"):
        return ADVISORY_REPAIRED, 0
    return ADVISORY_NONE, 0


def _verdict(
    payload: dict[str, Any],
    *,
    review_state: str,
    advisory_state: str,
    advisory_open_count: int,
    h0_state: str,
    content_notices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """三态 + 内容提示 → 结果。**`eligible` 只在这一处派生**(工单 §4.1)。

    单点派生是刻意的:变异①(把 eligible 改回"非 clear 或 not_run 就 False")只需要
    改这一行就能复现,锁 1/4 必须能当场抓住;若散落在十几个 return 里,反而抓不干净。

    🔴 [2026-08-01 §3A] `content_notices` 是**旁挂**的:它绝不参与 `eligible` 派生。
    这一点必须由锁 8b 守住 —— 一旦谁"顺手"把 `and not content_notices` 加进下面
    那行,内容类就又变回硬拦了,而 UI 上看起来一切正常。
    """
    notices = list(content_notices or [])
    return {
        **payload,
        "review_state": review_state,
        "advisory_state": advisory_state,
        "advisory_open_count": advisory_open_count,
        "publication_h0_state": h0_state,
        # [§3A] 内容类提示:机审结论照存照传,只是不再阻塞发布。
        "content_notices": notices,
        "content_notice_classes": [str(n.get("class") or "") for n in notices],
        "content_notice_state": NOTICE_OPEN if notices else NOTICE_NONE,
        # 向后兼容派生字段(工单 §4.1):review_not_run / machine_not_approved /
        # A1 findings 一律不再让 eligible=False。
        # [§3A] 内容类提示同样不参与 —— eligible 仍**只**由 h0_state 派生。
        "eligible": h0_state == H0_CLEAR,
    }


class ArticlePublicationBlocked(ValueError):
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        super().__init__(str(payload.get("message") or "article_publication_blocked"))


def is_publication_review_gate_enabled() -> bool:
    # [WP9-P0-7 · D8]代码默认 True:文章层已零阻断(草稿随便写),对外发布边界的广告法
    # 绝对化(legal_hard)成为**唯一残留硬点/唯一防线**,必须在岗——发布链路只此一处法律检查。
    # 部署即用(§15.1),无休眠 flag;仍可用环境变量显式关闭做灰度/回滚。
    return os.getenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", "true").strip().lower() in _TRUE


def evaluate_publication_eligibility(
    article_id: int,
    *,
    cursor=None,
    _evaluate_when_rollout_disabled: bool = False,
) -> dict[str, Any]:
    """Evaluate one article against the currently effective publication gate."""
    if not is_publication_review_gate_enabled() and not _evaluate_when_rollout_disabled:
        return _verdict(
            {"reason": "publication_review_gate_disabled", "article_id": article_id},
            review_state=REVIEW_NOT_RUN, advisory_state=ADVISORY_NONE,
            advisory_open_count=0, h0_state=H0_CLEAR,
        )
    if int(article_id) < 0:
        return _verdict(
            {"reason": "non_article_short_video"},
            review_state=REVIEW_NOT_RUN, advisory_state=ADVISORY_NONE,
            advisory_open_count=0, h0_state=H0_CLEAR,
        )
    owns_conn = cursor is None
    conn = None
    try:
        if owns_conn:
            from db.connection import get_connection

            conn = get_connection()
            cursor = conn.cursor()
        cursor.execute(
            """
            SELECT a.id, a.content, a.current_content_hash, a.style_code, a.style,
                   a.style_family,
                   a.article_review_status, a.article_human_review_status,
                   a.article_review, a.evidence_manifest_hash,
                   a.publication_profile, a.platform_review,
                   -- [span 级 AI 免费修复 2026-07-30 · §2.1] 出口按钮要按"是不是
                   -- 医疗/法律/金融高风险"决定 ai_fix_this_span 渲不渲染。
                   -- 判定本身一字未动,只影响 actions 这一层。
                   a.title, q.industry,
                   -- [标准类证据 lane 2026-07-29 · §3.3/§5.6] 判别锁要读 pack 里的
                   -- source_tier='standard_citation' 标记。不取这一列 = 锁拿不到字段 =
                   -- 永远不触发而全绿(本仓刚踩过两次同类"看着在跑、实际空转")。
                   a.evidence_pack,
                   -- [三态拆分 2026-07-31] advisory_state 读的就是这一列(与
                   -- mark-reviewed 端点同源:evidence.soft / evidence_precision.warnings
                   -- / human_continue.acknowledged)。不取这一列 = advisory 恒 none。
                   a.quality_warning
            FROM articles a
            LEFT JOIN quotes q ON q.id = a.quote_id
            WHERE a.id = %s
            """,
            (article_id,),
        )
        row = cursor.fetchone()
        if not row:
            # 对象不存在 = 对象完整性失败,§4.2 反向要求仍是 H0。
            return _verdict(
                {"reason": "article_not_found", "article_id": article_id},
                review_state=REVIEW_NOT_RUN, advisory_state=ADVISORY_NONE,
                advisory_open_count=0, h0_state=H0_OPERATOR_HARD,
            )
        # [§2.1 / 锁 6] 高风险类缺的是**人工签发**,不是措辞 → 不给 AI 修复出口。
        # 与 findings 标注、repair-finding 端点共用同一个 article_ai_repair_blocked。
        try:
            from services.span_level_repair import article_ai_repair_blocked

            ai_repair_blocked = article_ai_repair_blocked(
                str(row.get("industry") or ""), str(row.get("title") or ""),
            )
        except Exception:
            ai_repair_blocked = True  # fail-closed:判不出来就不给 AI 修复按钮
        machine = str(row.get("article_review_status") or "legacy_unreviewed")
        human = str(row.get("article_human_review_status") or "")
        style = str(row.get("style_code") or row.get("style") or "")
        style_family = str(row.get("style_family") or "")
        actual_hash = hashlib.sha256(str(row.get("content") or "").encode("utf-8")).hexdigest()
        review_payload = row.get("article_review") or {}
        if not isinstance(review_payload, dict):
            review_payload = {}
        reviewed_hash = review_payload.get("reviewed_content_hash")
        reviewed_evidence_hash = review_payload.get("reviewed_evidence_manifest_hash")
        advisory_state, advisory_open_count = _advisory_state(row.get("quality_warning"))

        # [§3A] 内容类提示累积器。**先于 `_verdict_here` 声明**,闭包按引用读取,
        # 所以后面 append 的提示会被之后每一个 return 带上 —— 包括走到 operator_hard
        # 的那几个 return(一篇既命中广告法、又被人工拒稿的文章,拒稿仍拦,
        # 但提示照样报给前端)。
        content_notices: list[dict[str, Any]] = []

        def _verdict_here(payload: dict[str, Any], *, review_state: str, h0_state: str):
            return _verdict(
                payload, review_state=review_state, advisory_state=advisory_state,
                advisory_open_count=advisory_open_count, h0_state=h0_state,
                content_notices=content_notices,
            )

        # 🔴 [三态拆分 2026-07-31 · 本单的真修法] 旧代码把两件完全不同的事写成一个
        # 条件 `not reviewed_hash or reviewed_hash != actual_hash`:
        #   (a) 快照**压根不存在** → 这篇从没跑过机审。没有 lineage,也就无所谓
        #       "lineage 对不上";它是 review_state=not_run,不是硬门。
        #       生产 1110 篇 legacy_unreviewed 全部落在这里(article_review IS NULL)。
        #   (b) 快照**存在但对不上** → 审完正文/证据又被改过。这是真的 hash lineage
        #       不一致,§4.2 点名仍是 H0(否则"审完再改"就成了绕过审核的后门)。
        # 只有 (b) 才拦。(a) 继续往下走,**不提前 return** —— 这一点是硬要求:
        # 若 (a) 直接 return clear,一篇 article_review IS NULL 但 machine='blocked'
        # 的文章就会绕开下面的法律硬门。必须让它照常经过 blocked / rewrite_required /
        # 标准类证据 / 人工签发 全部检查,最后落到兜底分支才算 clear。
        review_state = REVIEW_COMPUTED if (reviewed_hash or reviewed_evidence_hash) else REVIEW_NOT_RUN
        if review_state == REVIEW_COMPUTED:
            if reviewed_hash != actual_hash:
                return _verdict_here(
                    {
                        "reason": "content_changed_after_review",
                        "reason_class": "content_lineage_mismatch",
                        "overridable": False,
                        "article_id": article_id,
                        "message": "文章正文已在审核后变化，必须重新执行 GEO 文章审核。",
                    },
                    review_state=review_state, h0_state=H0_OPERATOR_HARD,
                )
            if reviewed_evidence_hash != row.get("evidence_manifest_hash"):
                return _verdict_here(
                    {
                        "reason": "evidence_changed_after_review",
                        "reason_class": "evidence_lineage_mismatch",
                        "overridable": False,
                        "article_id": article_id,
                        "message": "文章证据清单已在审核后变化，必须重新执行 GEO 文章审核。",
                    },
                    review_state=review_state, h0_state=H0_OPERATOR_HARD,
                )
        if machine == "blocked":
            # [§3A 降级 2026-08-01] 原为 legal_hard H0(人工签发亦不可覆盖)。Owner 拍板
            # 广告法防线前移到写作侧 prompt,发布侧硬拦取消 → 这里改为**提示级**:
            # 结论、定位、修复出口一字不减,只是不再让 eligible=False。
            # 🔴 不 return:继续往下走,人工拒稿/签发快照等 operator_hard 类仍须生效。
            content_notices.append({
                "class": NOTICE_LEGAL,
                "reason": "blocked",
                "reason_class": "legal_hard",
                "overridable": True,
                "article_id": article_id,
                "message": "内容可能命中《广告法》绝对化用语等法律风险表述。这不阻止你发布——是否处理由你决定；建议修正后再发。",
                "repair_hint": "定位并修正法律违规表述，再重新审核。",
                # [WP9-P0-7 ③ · D8]提示的同时给**一键修复**出口,让用户一步就能修
                # (span 级 AI 修复,不重跑整篇、不二次计费)。
                # [span 级 2026-07-30 · §2.1]医疗/法律/金融高风险**不给 AI 修复**:
                # 该类唯一诚实出口是按事实手改后重审(降级后依然如此——降的是"拦不拦",
                # 不是"能不能让 AI 代笔改医疗法律表述")。
                "ai_repair_available": not ai_repair_blocked,
                "actions": (
                    [
                        {"id": "view_findings", "label": "查看定位", "type": "nav"},
                        {"id": "edit_manually", "label": "自己手动改", "type": "nav"},
                    ] if ai_repair_blocked else [
                        {"id": "ai_fix_this_span", "label": "AI 修复违规表述", "type": "retry"},
                        {"id": "view_findings", "label": "查看定位", "type": "nav"},
                        {"id": "edit_manually", "label": "自己手动改", "type": "nav"},
                    ]
                ),
            })
        if machine == "rewrite_required":
            # [§3A 降级 2026-08-01] 原为 platform_profile_hard H0(仅该发布档不可覆盖)。
            # 平台档规则属"内容类",同样降为提示级:换档/修正的出口一字不减,只是不拦。
            # 🔴 不 return:理由同上。
            content_notices.append({
                "class": NOTICE_PLATFORM,
                "reason": "rewrite_required",
                "reason_class": "platform_profile_hard",
                "overridable": True,
                "article_id": article_id,
                "message": "内容可能不符合当前发布渠道的平台规则（该渠道严格档建议重写）。这不阻止你发布；也可改投兼容渠道后重新评估。",
                "repair_hint": "按该渠道规则修正后重审，或改投兼容发布渠道。",
                "actions": [
                    {"id": "view_findings", "label": "查看定位", "type": "nav"},
                    {"id": "edit_manually", "label": "自己手动改", "type": "nav"},
                ],
            })
        # [标准类证据 lane 2026-07-29 · §4.3] 标题级标准名被做成"内容性引用"。
        # 这类编造("该标准规定…≤0.03mg/m³")看起来极可信,人审天然拦不住。
        # [§3A 降级 2026-08-01] 工单点名"标准引用类"属内容类 → 同样降为提示级。
        # 🔴 它的判定与文案一字未改,变的只是"拦不拦";编造证据仍会**明确提示**,
        #    出口仍是改正文或补带正文的来源。
        standard_block = evaluate_standard_citation_discipline(
            str(row.get("content") or ""), row.get("evidence_pack"),
        )
        if standard_block:
            content_notices.append({
                **standard_block,
                "class": NOTICE_EVIDENCE_FABRICATION,
                "overridable": True,
                "article_id": article_id,
            })
        if human == "approved":
            # Human approval is an immutable, hash-bound decision, never a
            # free-standing status bit.  The row lock in set_human_review
            # serializes edits; this comparison protects every later dispatch.
            cursor.execute(
                """
                SELECT reviewed_content_hash, evidence_manifest_hash
                  FROM geo_article_review_events
                 WHERE article_id = %s AND decision = 'approved'
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (article_id,),
            )
            approval = cursor.fetchone()
            # 签发快照三连仍是 H0(§4.2 "对象 hash lineage 不一致"直接点名)。
            if not approval:
                return _verdict_here({
                    "reason": "human_approval_snapshot_missing",
                    "overridable": False,
                    "article_id": article_id,
                    "message": "人工签发缺少不可变审核快照，必须重新签发。",
                }, review_state=review_state, h0_state=H0_OPERATOR_HARD)
            if approval.get("reviewed_content_hash") != actual_hash:
                return _verdict_here({
                    "reason": "human_approval_content_changed",
                    "overridable": False,
                    "article_id": article_id,
                    "message": "当前正文不再是人工签发时审核的正文，必须重新签发。",
                }, review_state=review_state, h0_state=H0_OPERATOR_HARD)
            if approval.get("evidence_manifest_hash") != row.get("evidence_manifest_hash"):
                return _verdict_here({
                    "reason": "human_approval_evidence_changed",
                    "overridable": False,
                    "article_id": article_id,
                    "message": "当前证据清单不再是人工签发时审核的版本，必须重新签发。",
                }, review_state=review_state, h0_state=H0_OPERATOR_HARD)
            return _verdict_here(
                {"reason": "human_approved_snapshot_match", "article_id": article_id},
                review_state=review_state, h0_state=H0_CLEAR,
            )
        if human == "rejected":
            # 🔴 判断点(交付说明已单列,请复审裁定):人工**明确拒稿**必须继续拦。
            # 三态里没有"人审拒绝"这一档,而 eligible 只能由 h0 派生 —— 若归 clear,
            # 审核员按下的"拒绝"就当场失效,这是明显回归。故归入完整性/授权桶。
            # reason 保持 `human_rejected` 不变,调用方仍能区分。
            return _verdict_here(
                {"reason": "human_rejected", "overridable": True, "article_id": article_id},
                review_state=review_state, h0_state=H0_OPERATOR_HARD,
            )
        # [WP9-P0-7 · D8 ④] company_facts/brand_softarticle 由强制人审改为**默认推荐**:
        # 不再硬拦(overridable),用户明示跳过(human=='skipped',审计已记)即放行。法律/平台档
        # 已在上方(88-115)拦过,skip 天然越不过。跳过后仍需 machine 判定通过。
        if (
            (style_family == "company_facts" or style in {"brand_softarticle", "company_profile"})
            and human != "skipped"
        ):
            # 🔴 [三态拆分 · 工单 §4.3] 人审是**默认建议**,不是发布死锁。
            # 从 eligible=False 改为 clear:仍然回同一个 reason(set_human_review 的
            # skip 守卫按 reason 判,不能改串)、仍然把"建议人审"摆在前端,
            # 但不再拦住发布。明示跳过依旧走 set_human_review 落五字段审计。
            return _verdict_here({
                "reason": "brand_story_review_recommended",
                "reason_class": "recommended_review",
                "overridable": True,
                "human_review_recommended": True,
                "message": "品牌故事/企业档案类建议人工过目;确认无误可直接发布,也可明示跳过留痕(将记录操作人/时间/理由/前后状态)。",
                "article_id": article_id,
            }, review_state=review_state, h0_state=H0_CLEAR)
        # 🔴 [§3A 兼容层 2026-08-01] 位置是刻意的:**在全部 operator_hard 分支之后、
        # 在所有"clear 且无话可说"的分支之前**。
        #   · 放在这之前 → 会绕过人工拒稿/签发快照等 H0(即本单最该防的降级过界);
        #   · 放在这之后(只塞进兜底) → machine='approved' 且命中编造型证据的文章
        #     会先被 `machine_rules_approved` 截走,顶层 reason 丢失分层信息。
        # 唯一不让它接管的是 brand_story_review_recommended:那个 reason 串是
        # set_human_review 的 skip 守卫判据,改串会让"跳过推荐人审"整条失效。
        # 排序保证:brand_story_review_recommended 分支在上面已经 return 过了,
        # 走到这里说明它不适用 —— 所以这里**不需要**再判一次 company_facts。
        #
        # 铺回顶层的原因:降级后 blocked / rewrite_required 不再提前 return,若照兜底
        # 分支的通用文案返回,顶层 reason_class 会从 'legal_hard' 掉成
        # 'machine_not_approved'、message 变成"还没通过机审" —— 那是**无谓的破坏面**
        # (reason 分层、前端徽章、span 级高风险判定全靠这几个字段)。
        _primary = _primary_notice(content_notices)
        if _primary is not None:
            return _verdict_here({
                "reason": _primary.get("reason") or machine,
                "reason_class": _primary.get("reason_class"),
                "overridable": True,  # 唯一变的:提示级一律可继续发布
                "article_id": article_id,
                "message": _primary.get("message"),
                "repair_hint": _primary.get("repair_hint"),
                "ai_repair_available": _primary.get(
                    "ai_repair_available", not ai_repair_blocked
                ),
                "actions": _primary.get("actions") or [],
            }, review_state=review_state, h0_state=H0_CLEAR)
        if machine == "approved":
            return _verdict_here(
                {"reason": "machine_rules_approved", "article_id": article_id},
                review_state=review_state, h0_state=H0_CLEAR,
            )
        # [返修 P1-3 · §1.6]兜底分支绝不能是"红码无下一步":flag 默认开之后,存量
        # legacy_unreviewed 文章(生产绝大多数)会走到这里。它既不属法律硬门、也不属平台档
        # 硬门,不属四大硬边界任一类 —— 只是"还没跑过机审"。因此必须给**可执行出口**:
        # 一键触发 GEO 机审后重评,而不是把人堵死在这里。
        # 🔴 [三态拆分 · 工单 §4.1] 这里是本单的落点:review_not_run 与
        # machine_not_approved **都不是对外发布硬门**,h0=clear → eligible True。
        # 它们仍然如实出现在 reason/reason_class/review_state 里,前端照旧提示
        # "还没审"并给一键审核出口 —— 区别只是不再把人堵死在发布中心门外。
        _unreviewed = (
            review_state == REVIEW_NOT_RUN
            or machine in ("", "legacy_unreviewed", "pending", "not_reviewed")
        )
        return _verdict_here({
            "reason": machine,
            "reason_class": "review_not_run" if _unreviewed else "machine_not_approved",
            "overridable": True,
            "article_id": article_id,
            "message": (
                "这篇文章还没跑过 GEO 文章审核,可以直接发布;想更稳妥可以先跑一次审核。"
                if _unreviewed else
                "文章尚未通过 GEO 文章审核,不影响发布;可让 AI 局部修复后重新审核,或由有权限的同事人工签发。"
            ),
            "repair_hint": (
                "点下方「立即执行 GEO 审核」可跑一次机审(非发布前置条件)。"
                if _unreviewed else
                "按提示修复后重新审核;确有把握也可走人工签发。"
            ),
            # [span 级 2026-07-30 · §2.1] 高风险类只留"走人工签发 / 自己手动改",
            # AI 修复按钮不渲染(本分支 overridable=True,人工签发是真出口)。
            "ai_repair_available": not ai_repair_blocked,
            "actions": (
                [
                    {"id": "run_article_review", "label": "立即执行 GEO 审核", "type": "retry"},
                    {"id": "view_article", "label": "查看文章", "type": "nav"},
                ] if _unreviewed else [
                    {"id": "request_human_review", "label": "请人工签发", "type": "contact"},
                    {"id": "edit_manually", "label": "自己手动改", "type": "nav"},
                ] if ai_repair_blocked else [
                    {"id": "ai_fix_this_span", "label": "AI 修复问题处", "type": "retry"},
                    {"id": "run_article_review", "label": "重新审核", "type": "retry"},
                    {"id": "request_human_review", "label": "请人工签发", "type": "contact"},
                ]
            ),
        }, review_state=review_state, h0_state=H0_CLEAR)
    finally:
        if owns_conn and conn is not None:
            conn.close()


_NOTICE_AUDIT_SQL: Final = """
    INSERT INTO geo_article_publication_notice_audits
        (article_id, notice_classes, notice_count, gate_snapshot)
    VALUES (%s, %s, %s, %s)
"""


def _record_publication_notice_audit(
    article_id: int, verdict: dict[str, Any], *, cursor=None
) -> bool:
    """[§3A 静默留痕] 带**未处理内容提示**发布 → 后台留一行。零弹窗零确认零打扰。

    这行记录不进任何界面,只在广告法等纠纷时被查,用途是证明"平台已尽提示义务"。
    返回值 = 是否真的写了一行(锁 4 要双向断言:有提示写、无提示不写)。

    🔴 三条硬性质,少一条这个功能就会从"证据链"变成"事故源":
      1. **无提示不写行** —— 否则留痕失去区分度,等于没留。
      2. **借用调用方 cursor 时必须 SAVEPOINT** —— 发布链多处传入共享事务
         (db/publish_db.py、article_publish_dispatch.py 都传 cursor)。若表还没建好
         或写入失败,裸 INSERT 会把调用方整个事务打成 aborted,**发布反而失败**。
         留痕绝不允许反过来阻断发布,所以失败必须能干净回退到 SAVEPOINT。
      3. **best-effort** —— 任何异常只记 warning,永不外抛。
    """
    notices = verdict.get("content_notices") or []
    if not notices:
        # 锁 4 的反向面:无未处理提示的发布,一行都不许写。
        return False
    from psycopg2.extras import Json

    classes = [str(n.get("class") or "") for n in notices]
    snapshot = {
        "reason": verdict.get("reason"),
        "reason_class": verdict.get("reason_class"),
        "review_state": verdict.get("review_state"),
        "advisory_state": verdict.get("advisory_state"),
        "advisory_open_count": verdict.get("advisory_open_count"),
        "publication_h0_state": verdict.get("publication_h0_state"),
        "eligible": verdict.get("eligible"),
        # 逐条提示的类别/原因/文案 —— 事后要能还原"当时提示的到底是什么"。
        "notices": [
            {
                "class": n.get("class"),
                "reason": n.get("reason"),
                "message": n.get("message"),
            }
            for n in notices
        ],
    }
    params = (int(article_id), Json(classes), len(classes), Json(snapshot))

    if cursor is not None:
        try:
            cursor.execute("SAVEPOINT geo_pub_notice_audit")
        except Exception as exc:  # 事务已不可用,放弃留痕但不影响发布
            logger.warning("[notice-audit] savepoint 失败 article=%s: %s", article_id, exc)
            return False
        try:
            cursor.execute(_NOTICE_AUDIT_SQL, params)
            cursor.execute("RELEASE SAVEPOINT geo_pub_notice_audit")
            return True
        except Exception as exc:
            logger.warning("[notice-audit] 写入失败 article=%s: %s", article_id, exc)
            try:
                cursor.execute("ROLLBACK TO SAVEPOINT geo_pub_notice_audit")
                cursor.execute("RELEASE SAVEPOINT geo_pub_notice_audit")
            except Exception:
                pass
            return False

    conn = None
    try:
        from db.connection import get_connection

        conn = get_connection()
        cur = conn.cursor()
        cur.execute(_NOTICE_AUDIT_SQL, params)
        conn.commit()
        return True
    except Exception as exc:
        logger.warning("[notice-audit] 独立连接写入失败 article=%s: %s", article_id, exc)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn is not None:
            conn.close()


def assert_publication_eligible(article_id: int, *, cursor=None) -> dict[str, Any]:
    # Compatibility first: before the additive migration is applied, old
    # deployments do not have the review columns.  A disabled rollout gate must
    # therefore perform no schema-dependent query at all.
    if not is_publication_review_gate_enabled():
        return _verdict(
            {"reason": "publication_review_gate_disabled", "article_id": article_id},
            review_state=REVIEW_NOT_RUN, advisory_state=ADVISORY_NONE,
            advisory_open_count=0, h0_state=H0_CLEAR,
        )
    result = evaluate_publication_eligibility(article_id, cursor=cursor)
    if not result["eligible"]:
        raise ArticlePublicationBlocked(result)
    # [§3A 静默留痕] 走到这里 = 本次发布将被放行。若仍有未处理的内容提示,
    # 后台留一行证据。刻意放在 raise 之后:被 H0 拦下的不算"客户选择带提示发布"。
    _record_publication_notice_audit(article_id, result, cursor=cursor)
    return result


def set_human_review(
    article_id: int,
    *,
    reviewer_user_id: int,
    decision: str,
    reason: str,
    cursor=None,
) -> dict[str, Any]:
    if decision not in HUMAN_DECISIONS:
        raise ValueError("decision must be one of: approved / rejected / skipped")
    if len(str(reason or "").strip()) < 5:
        raise ValueError("human review reason must contain at least 5 characters")
    owns_conn = cursor is None
    conn = None
    try:
        if owns_conn:
            from db.connection import get_connection

            conn = get_connection()
            cursor = conn.cursor()
        # Serialize human sign-off with every article UPDATE.  This row lock is
        # intentionally acquired before reading any review/evidence state.
        # [三态拆分 · §4.3 五字段留痕] 取"前态"必须与行锁同一次读:锁后再单独 SELECT
        # 会被并发签发插进来,写出 before/after 都是新值的假审计。
        cursor.execute(
            "SELECT id, article_human_review_status FROM articles WHERE id = %s FOR UPDATE",
            (article_id,),
        )
        _before_row = cursor.fetchone()
        if not _before_row:
            raise ValueError("article_not_found")
        prior_human_review_status = _before_row.get("article_human_review_status")
        if decision == "approved":
            eligibility = evaluate_publication_eligibility(article_id, cursor=cursor)
            if eligibility.get("reason") in HUMAN_APPROVAL_BLOCKING_REASONS:
                raise ValueError(str(eligibility.get("reason")))
        if decision == "skipped":
            # [WP9-P0-7 · D8 ④] skip 只放行"品牌故事/企业档案推荐人审";绝不越过法律/平台档/
            # 机器未过等任何其它阻断(评估顺序上它们本就排在推荐人审之前)。
            eligibility = evaluate_publication_eligibility(article_id, cursor=cursor)
            if eligibility.get("reason") != "brand_story_review_recommended":
                raise ValueError("skip_only_for_recommended_review")
        cursor.execute(
            """
            UPDATE articles
            SET article_human_review_status = %s,
                article_human_reviewed_by = %s,
                article_human_reviewed_at = %s,
                article_human_review_reason = %s
            WHERE id = %s
            RETURNING id, article_review_status, article_human_review_status,
                      article_human_reviewed_by, article_human_reviewed_at,
                      article_human_review_reason
            """,
            (decision, reviewer_user_id, datetime.now(timezone.utc), reason.strip(), article_id),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError("article_not_found")
        machine_review_version = None
        reviewed_content_hash = None
        evidence_manifest_hash = None
        cursor.execute(
            "SELECT article_review, evidence_manifest_hash FROM articles WHERE id = %s",
            (article_id,),
        )
        review_row = cursor.fetchone() or {}
        review_payload = review_row.get("article_review") or {}
        if isinstance(review_payload, dict):
            machine_review_version = review_payload.get("review_version")
            reviewed_content_hash = review_payload.get("reviewed_content_hash")
        evidence_manifest_hash = review_row.get("evidence_manifest_hash")
        # [§4.3 五字段留痕] operator=actor_user_id · time=created_at(DEFAULT NOW())
        # · reason=reason · before=prior_human_review_status · after=decision。
        # before 这一列是本单唯一 schema 变更:nullable、无默认、无 CHECK、不回填,
        # 由 ensure_review_event_prior_status_column() 幂等补齐(§8 additive 要求)。
        cursor.execute(
            """
            INSERT INTO geo_article_review_events (
                article_id, actor_user_id, decision, reason,
                machine_review_status, machine_review_version,
                reviewed_content_hash, evidence_manifest_hash,
                prior_human_review_status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                article_id, reviewer_user_id, decision, reason.strip(),
                row.get("article_review_status"), machine_review_version,
                reviewed_content_hash, evidence_manifest_hash,
                prior_human_review_status,
            ),
        )
        if owns_conn:
            conn.commit()
        return dict(row)
    except Exception:
        if owns_conn and conn is not None:
            conn.rollback()
        raise
    finally:
        if owns_conn and conn is not None:
            conn.close()


def refresh_article_review(article_id: int, *, cursor=None) -> dict[str, Any]:
    """Re-review the exact editable body and invalidate any earlier human sign-off."""
    owns_conn = cursor is None
    conn = None
    try:
        if owns_conn:
            from db.connection import get_connection

            conn = get_connection()
            cursor = conn.cursor()
        cursor.execute(
            """
            SELECT a.*, q.brand_name, q.industry,
                   t.optimized_title AS topic_title, t.original_keyword, t.user_choice
              FROM articles a
              LEFT JOIN quotes q ON q.id=a.quote_id
              LEFT JOIN topics t ON t.id=a.topic_id
             WHERE a.id=%s
            """,
            (article_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError("article_not_found")
        article = dict(row)
        article["style_version_id"] = article.get("style_version")
        topic = {
            "id": article.get("topic_id"),
            "title": article.get("topic_title") or article.get("title"),
            "optimized_title": article.get("topic_title"),
            "original_keyword": article.get("original_keyword"),
            "user_choice": article.get("user_choice"),
            "generation_request_id": article.get("generation_request_id"),
            "evidence_pack": article.get("evidence_pack"),
            "brand_fact_snapshot": article.get("brand_fact_snapshot"),
            "publication_profile": article.get("publication_profile"),
            "_style_version_id": article.get("style_version"),
        }
        from psycopg2.extras import Json
        from writing.article_lineage import build_article_lineage

        lineage = build_article_lineage(
            topic=topic,
            article=article,
            quote_id=article.get("quote_id"),
            industry=str(article.get("industry") or ""),
            client_brand=str(article.get("brand_name") or ""),
        )
        cursor.execute(
            """
            UPDATE articles
               SET article_review=%s, article_review_status=%s,
                   platform_review=%s, current_content_hash=%s,
                   article_human_review_status=NULL,
                   article_human_reviewed_by=NULL,
                   article_human_reviewed_at=NULL,
                   article_human_review_reason=NULL
             WHERE id=%s
            RETURNING id, article_review_status, publication_profile,
                      platform_review, current_content_hash
            """,
            (
                Json(lineage["article_review"]),
                lineage["article_review_status"],
                Json(lineage["platform_review"]),
                lineage["current_content_hash"],
                article_id,
            ),
        )
        updated = dict(cursor.fetchone())
        if owns_conn:
            conn.commit()
        return updated
    except Exception:
        if owns_conn and conn is not None:
            conn.rollback()
        raise
    finally:
        if owns_conn and conn is not None:
            conn.close()


def list_review_queue(*, limit: int = 100) -> list[dict[str, Any]]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.id, a.title, a.style_code, a.style_family, a.publication_profile,
                   a.article_review_status, a.article_human_review_status,
                   a.article_review, a.evidence_manifest_hash, a.created_at,
                   q.brand_name, q.industry
            FROM articles a
            LEFT JOIN quotes q ON q.id = a.quote_id
            WHERE a.article_review_status IN (
                    'blocked', 'rewrite_required', 'legacy_unreviewed'
                  )
               OR (
                    a.article_review_status='pending_human_review'
                    AND a.article_human_review_status IS NULL
                  )
               OR (a.style_code='brand_softarticle' AND a.article_human_review_status IS NULL)
               OR a.article_human_review_status='rejected'
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT %s
            """,
            (max(1, min(int(limit), 500)),),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
