"""§9.6 小榜只读**冻结**公开理由 —— discovery / prefill / 只读解释。

规格 §9.6 逐字:

    「小榜只能读取**公开、冻结**的推荐原因和下一步,
      **不现场生成**媒体理由或商业数字。」

🔴 「冻结」的可执行含义
----------------------
= 签发时写下来、之后只读。现算的理由每次调用可能不同 ——
客户在聊天里问两次会得到两套说法,而**两套都会被当成我们的承诺**。
所以 :func:`load` 只 SELECT,本模块**没有**任何生成理由的函数。

🔴 「不现场生成商业数字」为什么是硬门
------------------------------------
小榜是聊天面。模型很擅长顺口补一句"大概三五百算力吧" —— 那个数字
既不是服务端签发的价,也不进任何报价快照,但客户会记住它。
:func:`assert_no_business_numbers` 因此扫描**将要上屏**的文本,
出现算力/金额/百分比形态即拒。

库层另有 `chk_defgeo_frozen_reason_no_money`(迁移 046),
两道是纵深:运行时门防"服务端自己写错",库层门防"绕过服务端直接写库"。
判据对两层各拆一次。

🔴 DLP 双层复用(§0.5.1 第 6 条:不另造第三层)
----------------------------------------------
人读面走现役 ``services/gap_operation_labels.assert_no_internal_leak``,
机器面走现役 ``services/xiaobang_facade_dlp``。本模块只做**内容**约束
(商业数字 / 内部枚举),不重新实现泄漏检测。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

REASONS_VERSION = "defgeo-xiaobang-frozen-reasons-v1"

TABLE = "defgeo_xiaobang_frozen_reasons"

#: 理由可以挂在哪三种冻结对象上。与迁移 046 的 CHECK 逐值同源。
SUBJECT_KINDS: tuple[str, ...] = (
    "report_snapshot", "media_decision_snapshot", "quote_snapshot",
)

#: 允许的 nextAction 种类 —— 全部是**已有路由**的只读动作。
#: 🔴 刻意不含任何付费动作:付费一律走五阶段 preview→confirm→execute,
#:    不从"理由"这一侧长出扣费入口(§9.6 末两条)。
READ_ONLY_NEXT_ACTIONS: tuple[str, ...] = (
    "view_report", "view_plan", "view_media_decision", "view_quote",
    "contact_provider", "open_existing_page",
)

#: 商业数字形态。三类各有各的骗法:
#:   ① "300 算力"  —— 直接报价
#:   ② "¥1200"     —— 直接报钱
#:   ③ "提升 40%"  —— 承诺效果(还撞 §1.3「不承诺固定推荐率」)
_MONEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[0-9][0-9,\.]*\s*(算力|积分|额度|元|块钱|万元)"),
    re.compile(r"[¥$]\s*[0-9]"),
    re.compile(r"[0-9][0-9\.]*\s*%"),
)

#: §1.3 / 对外话术红线:绝对化承诺。命中即拒。
_ABSOLUTE_PROMISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(保证|必然|一定能|百分之百|100%)\s*(上榜|推荐|排名|收录)"),
    re.compile(r"(永久|终身)\s*(引用|收录|推荐)"),
)


class FrozenReasonError(ValueError):
    """理由不合法。**不下发**,而不是下发一句会被当成承诺的话。"""


def assert_no_business_numbers(text: str, *, where: str = "frozen_reason") -> str:
    """§9.6「不现场生成……商业数字」的运行时门。"""
    for pattern in _MONEY_PATTERNS:
        hit = pattern.search(text)
        if hit:
            raise FrozenReasonError(
                f"{where} 出现商业数字「{hit.group(0)}」。"
                "小榜只读冻结理由,**不下发**任何价格/算力/百分比 —— "
                "客户会把聊天里随口的数字当成我们的报价(§9.6)。"
                "价格一律由服务端在报价链实时签发。"
            )
    for pattern in _ABSOLUTE_PROMISE_PATTERNS:
        hit = pattern.search(text)
        if hit:
            raise FrozenReasonError(
                f"{where} 出现绝对化承诺「{hit.group(0)}」。"
                "§1.3:不承诺固定排名、固定推荐率、永久引用。"
            )
    return text


def assert_frozen_shape(row: Mapping[str, Any]) -> None:
    """一条冻结理由的形态门。"""
    kind = str(row.get("subject_kind") or "")
    if kind not in SUBJECT_KINDS:
        raise FrozenReasonError(
            f"未知 subjectKind {kind!r};合法 = {list(SUBJECT_KINDS)}")
    action = str(row.get("next_action_kind") or "")
    if action not in READ_ONLY_NEXT_ACTIONS:
        raise FrozenReasonError(
            f"nextAction {action!r} 不在只读动作集里。"
            "付费/发布动作**必须**走五阶段 preview→confirm→execute,"
            "不能从冻结理由这一侧长出扣费入口(§9.6)。"
            f"合法只读动作 = {list(READ_ONLY_NEXT_ACTIONS)}"
        )
    if not str(row.get("public_text") or "").strip():
        raise FrozenReasonError(
            "理由文本为空 —— 空理由在聊天里会被渲染成一句没有内容的话,"
            "销售无法向客户解释"
        )
    assert_no_business_numbers(str(row["public_text"]),
                               where=f"frozen_reason[{row.get('reason_ref')}]")


def load(
    cur, *, tenant_owner_user_id: int, subject_kind: str, subject_ref: str,
) -> list[dict[str, Any]]:
    """只读加载。**本模块没有任何生成函数** —— 那就是「冻结」的实现形式。

    🔴 归属逐次重验:``tenant_owner_user_id`` 进 WHERE,不靠上层过滤。
       §14.1「所有公共端点逐次验证」;跨租户与不存在**同形返空**,
       不泄露对象存在性。
    """
    if subject_kind not in SUBJECT_KINDS:
        raise FrozenReasonError(f"未知 subjectKind {subject_kind!r}")
    cur.execute(
        f"""
        SELECT reason_ref, subject_kind, subject_ref, reason_code,
               public_text, next_action_kind, frozen_at, reasons_version
          FROM public.{TABLE}
         WHERE tenant_owner_user_id=%s
           AND subject_kind=%s AND subject_ref=%s
           AND revoked_at IS NULL
         ORDER BY frozen_at, reason_ref
        """,
        (int(tenant_owner_user_id), subject_kind, subject_ref),
    )
    rows = [dict(r) for r in (cur.fetchall() or [])]
    for row in rows:
        assert_frozen_shape(row)
    return rows


def assert_not_generated_here(candidate_text: str, frozen_rows: Sequence[Mapping[str, Any]]) -> str:
    """§9.6「**不现场生成**」的可执行形式。

    小榜要上屏的每一句理由,必须**逐字**等于某一条冻结理由。
    "基于冻结理由改写一下更顺口"也不行 —— 改写就是生成,
    而改写后的那句话没有任何人签发过。
    """
    frozen_texts = {str(r.get("public_text") or "") for r in frozen_rows}
    if candidate_text not in frozen_texts:
        raise FrozenReasonError(
            "要上屏的理由不在冻结集合里 —— 它是现场生成的(哪怕只是改写)。"
            "§9.6:小榜只能**读取**公开、冻结的推荐原因和下一步。"
            "需要新说法请在签发侧新增一条冻结理由,不要在聊天面即兴。"
        )
    return candidate_text


def census() -> dict[str, Any]:
    return {
        "reasonsVersion": REASONS_VERSION,
        "table": TABLE,
        "subjectKinds": list(SUBJECT_KINDS),
        "readOnlyNextActions": list(READ_ONLY_NEXT_ACTIONS),
        "moneyPatternCount": len(_MONEY_PATTERNS),
        "absolutePromisePatternCount": len(_ABSOLUTE_PROMISE_PATTERNS),
    }
