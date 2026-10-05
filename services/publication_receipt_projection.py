"""用户面**双轴**投影:「操作回执」与「已核实发布」是两件事(R2 §②)。

## 为什么要两根轴

`publish_records.status='success'` 的真实含义是:**用户点了发布,浏览器扩展回报
说提交成功了**。它是一条**操作回执**。而系统此前把它直接念成「已发布」——
于是被测方的一句自报,变成了产品里对客户的陈述。

R2 定的口径是把两件事分开说,谁也不冒充谁:

| 轴 | 取值 | 说的是 |
|---|---|---|
| 回执轴 `receipt` | `success` / `failed` / `pending` | 那次**操作**成没成 |
| 发布轴 `publication` | `verified_published` / `reported_success_unverified` / `not_published` | 那篇**到底发出去了没有**(以核实为准) |

`reported_success_unverified` 是本次新增的中间档 —— 对应生产上那 26 篇:
回执确实成功,但从没有任何权威核实过。它**不进**已核实计数 / 成功率 / 严格效果链,
但**保留**回执与防重复发布的占位(用户不该因为我们的口径收紧而重复发一遍)。

🔴 本模块是这套口径的**单一权威源**:Python 侧用 :func:`project`,SQL 侧用
   :data:`PUBLICATION_AXIS_SQL`。两处必须同源 —— 各写一份 CASE 必然漂移,
   而漂移出来的正是「一个接口说已发布、另一个说没有」这种最难查的 bug。
"""
from __future__ import annotations

from typing import Any

# ── 发布轴 ────────────────────────────────────────────────────────────────
PUBLICATION_VERIFIED = "verified_published"
PUBLICATION_REPORTED_UNVERIFIED = "reported_success_unverified"
PUBLICATION_NOT_PUBLISHED = "not_published"

PUBLICATION_AXIS_VALUES = (
    PUBLICATION_VERIFIED, PUBLICATION_REPORTED_UNVERIFIED, PUBLICATION_NOT_PUBLISHED,
)

# ── 回执轴 ────────────────────────────────────────────────────────────────
RECEIPT_SUCCESS = "success"
RECEIPT_FAILED = "failed"
RECEIPT_PENDING = "pending"

RECEIPT_AXIS_VALUES = (RECEIPT_SUCCESS, RECEIPT_FAILED, RECEIPT_PENDING)

#: 只有这一档算「已核实发布」。计数 / 成功率 / 严格效果链一律以它为准。
PUBLICATION_COUNTS_AS_PUBLISHED = frozenset({PUBLICATION_VERIFIED})

PUBLICATION_LABELS = {
    PUBLICATION_VERIFIED: "已核实发布",
    # 🔴 这句文案是 R2 §③ 逐字定的口径。它同时说清两件事:回执有、核实没有。
    #    不许简化成「未发布」(抹掉了用户确实操作过)也不许写成「已发布(待核实)」
    #    (那还是在说已发布)。
    PUBLICATION_REPORTED_UNVERIFIED: "浏览器曾回报成功 · 尚未核实",
    PUBLICATION_NOT_PUBLISHED: "未发布",
}

RECEIPT_LABELS = {
    RECEIPT_SUCCESS: "提交成功",
    RECEIPT_FAILED: "提交失败",
    RECEIPT_PENDING: "提交中",
}


def receipt_axis(status: Any) -> str:
    s = str(status or "").strip().lower()
    if s == "success":
        return RECEIPT_SUCCESS
    if s == "failed":
        return RECEIPT_FAILED
    return RECEIPT_PENDING


def publication_axis(status: Any, verification_state: Any) -> str:
    """回执 + 核实态 → 发布轴。

    🔴 只有 `verified` 进 `verified_published`。`content_matched` 是探针给的**线索**,
       攻击者做个同标题页面就能造出来 —— 它和 `pending` / `needs_action` 一样,
       统统归 `reported_success_unverified`。
    """
    if receipt_axis(status) != RECEIPT_SUCCESS:
        return PUBLICATION_NOT_PUBLISHED
    if str(verification_state or "").strip().lower() == "verified":
        return PUBLICATION_VERIFIED
    return PUBLICATION_REPORTED_UNVERIFIED


def project(status: Any, verification_state: Any) -> dict[str, str]:
    """给接口直接铺进响应的双轴投影。"""
    receipt = receipt_axis(status)
    publication = publication_axis(status, verification_state)
    return {
        "receipt": receipt,
        "receiptLabel": RECEIPT_LABELS[receipt],
        "publication": publication,
        "publicationLabel": PUBLICATION_LABELS[publication],
        "countsAsPublished": publication in PUBLICATION_COUNTS_AS_PUBLISHED,
    }


#: SQL 侧同一口径。占位 `{r}` 是 publish_records 的表别名。
#: 用法:`PUBLICATION_AXIS_SQL.format(r="pr")`
#:
#: 🔴 R3 §⑤ —— 两处 `COALESCE` 不是防御性洁癖,是**三值逻辑**在这里会真的岔开:
#:    `NULL <> 'success'` 求值为 NULL(不是 TRUE),CASE 的这一格不命中,于是
#:    一行 `status IS NULL` 的记录会掉进下一格。若它恰好带着 verified,SQL 会
#:    答 `verified_published`,而 Python 侧 `receipt_axis(None)` 归 pending →
#:    `not_published`。两个实现对同一行给出相反答案,正是本模块存在要防的那件事。
#:    `public_url_verification_state` 同理:它有 NOT NULL DEFAULT,但那只保证
#:    **本表**;这段 SQL 会被 format 进子查询/外连接,外连接补出来的就是 NULL。
PUBLICATION_AXIS_SQL = (
    "CASE WHEN COALESCE({r}.status, '') <> 'success' THEN 'not_published' "
    "WHEN COALESCE({r}.public_url_verification_state, '') = 'verified' "
    "THEN 'verified_published' "
    "ELSE 'reported_success_unverified' END"
)

#: 「这一行算不算已核实发布」的 SQL 谓词(同源,别再手写 `status='success'`)。
PUBLICATION_VERIFIED_SQL = (
    "(COALESCE({r}.status, '') = 'success'"
    " AND COALESCE({r}.public_url_verification_state, '') = 'verified')"
)

#: 「回执成功但未核实」的 SQL 谓词 —— 防重复发布的占位面走这个。
#: 🔴 这条的 NULL 洞最贵:`state IS NULL` 时 `state <> 'verified'` 是 NULL 而不是
#:    TRUE,整个谓词求值为 NULL → 该行**从"未核实"这一档里消失**。既不算已核实、
#:    也不算未核实,就是彻底不见了 —— 而防重复发布的占位面正走这条。
PUBLICATION_REPORTED_UNVERIFIED_SQL = (
    "(COALESCE({r}.status, '') = 'success'"
    " AND COALESCE({r}.public_url_verification_state, '') <> 'verified')"
)

# ===========================================================================
# 资金路径专用(R2 §⑤ · Review-CTO 2026-08-19)
# ===========================================================================
# 🔴 **监测入池是扣费路径,不是展示口径。**
#
# 「这张报价发生过发布吗」这个谓词决定监测调度跑不跑,而监测每词每次都扣钱
# (`db/monitoring_db.py::get_monitoring_enabled_clients` 上方那段 CTO-15.23
#  注释写得很清楚:加这道闸就是因为「用户没发文但被自动监测扣费」)。
#
# 所以 `publish_records` 这条臂进入该谓词时,**必须**带核实闸:浏览器自报
# 一句 success 不能把客户放进扣费池。这与展示口径的宽松处理是两回事 ——
# 展示可以说「曾回报成功」,扣费不能。
#
# 用法(给任何构造 occurrence / body-proof 谓词的地方):
#     f"SELECT 1 FROM publish_records rx WHERE ... AND {SELF_REPORT_COUNTS_SQL.format(r='rx')}"
SELF_REPORT_COUNTS_SQL = PUBLICATION_VERIFIED_SQL

#: 明确取名的别名:自报要算作「发生过发布」必须满足这个。
SELF_REPORT_OCCURRENCE_SQL = PUBLICATION_VERIFIED_SQL

#: 自报要能当**正文证据**(body proof)同样必须核实过。
#: 🔴 不要再用 `public_url_reported_explicitly IS TRUE` 当证据 —— 那是**来源位**
#:    (浏览器显式回报过),R2 已把它与权威位拆开;拿它当证据正是本工单的病灶。
SELF_REPORT_BODY_PROOF_SQL = (
    "({r}.submitted_content_snapshot_hash IS NOT NULL"
    " AND COALESCE({r}.public_url_verification_state, '') = 'verified')"
)
