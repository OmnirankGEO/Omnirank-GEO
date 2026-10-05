"""WP5 · 一篇一账号发布 command:校验 / 原子日容量 / 状态投影(规格 02 §7)。

## 三件互不替代的事

  1. **一篇一账号的校验**(§7.3):一个 item 恰好一个
     `geo_post_id + post_revision_id + prepared_artifact_id + manifest_hash` 和一个 `media_id`;
     同一 active post revision 映射 A/B 两账号 → **整批拒绝**,零订单零预占零冻结零外调。
  2. **账号日容量的原子预占**(§3.7 末):按冻结 business timezone 分日,
     `(capacity_date, media_type, media_id)` 取稳定锁,聚合既有 reserved/consumed 再插本批。
  3. **对客状态的动态投影**(§7.5):`command_status` **不是** root 的 `coordination_state`,
     而是按四条固定优先级从当前有效 attempts + settlement 算出来的。

## 为什么状态必须是投影而不是存字段

存字段必然漂移:retry 建了 child attempt 之后,root 上那个字段不会自己变回 processing。
规格 §7.5 末:「同一 idempotent replay 必须读取当前关系化状态,不能返回首次保存的陈旧 summary」。
存字段做不到这一点 —— 它保存的就是首次那一份。
"""
from __future__ import annotations

from typing import Any, Final, Iterable, Mapping, Optional, Sequence

#: 冻结的业务时区(规格 §3.7:随 item snapshot 保存,不猜机器本地)。
DEFAULT_BUSINESS_TIMEZONE: Final = "Asia/Shanghai"

# ── Command 对外状态(§7.5)──
CMD_PENDING_APPROVAL: Final = "pending_approval"
CMD_ACCEPTED: Final = "accepted"
CMD_PROCESSING: Final = "processing"
CMD_NEEDS_ACTION: Final = "needs_action"
CMD_COMPLETED: Final = "completed"
CMD_PARTIAL_SUCCESS: Final = "partial_success"
CMD_FAILED: Final = "failed"
CMD_CANCELLED: Final = "cancelled"

# ── Item delivery state(§7.5)──
TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {"published", "failed", "rejected", "withdrawn", "cancelled"})
NON_TERMINAL_HOLD: Final[frozenset[str]] = frozenset({"awaiting_sync", "needs_action"})
SUCCESS_STATES: Final[frozenset[str]] = frozenset({"published"})
CANCELLED_STATES: Final[frozenset[str]] = frozenset({"cancelled", "withdrawn"})

#: 需要人工处理的结算态 —— 命中即整个 command 进 needs_action。
MANUAL_SETTLEMENT: Final[frozenset[str]] = frozenset({"quarantined", "manual"})


class OneToOneViolation(ValueError):
    """同一 post revision 被映射到多个账号,或同一 item 带了多个账号。

    规格 §7.3:**整批拒绝**,零订单、零频控预占、零冻结、零外调。
    整批而不是丢掉冲突项 —— 部分接受会让用户以为提交成功了,
    而实际发出去的组合不是他确认的那一组。
    """


class CapacityExceeded(RuntimeError):
    """账号当日余量不足。零外调并安全 release 相应资金。"""


class PublishAttemptConflict(RuntimeError):
    """同一版本已有不可替换的发布项；调用方加载其结果，不重复冻结。"""

    def __init__(self, message: str, *, command_request_id: str = ""):
        super().__init__(message)
        self.command_id = f"pubcmd_{command_request_id}" if command_request_id else None


# ---------------------------------------------------------------------------
# 1. 一篇一账号校验
# ---------------------------------------------------------------------------

_REQUIRED_ITEM_FIELDS: Final[tuple[str, ...]] = (
    "item_request_id", "geo_post_id", "post_revision_id",
    "prepared_artifact_id", "manifest_hash", "media_id", "expected_price_fingerprint",
)


def assert_one_to_one(items: Sequence[Mapping[str, Any]]) -> None:
    """初始 command 内 post revision / artifact 必须 distinct(§7.3)。

    🔴 判的是 **post_revision_id**,不是 geo_post_id:同一篇作品的**不同版本**
       各自可以有自己的发布根(内容修复产生新 revision → 新根,§3.7)。
       拿 geo_post_id 判会把合法的"修复后重发"也拒掉。
    """
    if not items:
        raise ValueError("EMPTY_BATCH")

    seen_item_keys: set[str] = set()
    seen_revisions: dict[str, int] = {}
    for index, item in enumerate(items):
        missing = [f for f in _REQUIRED_ITEM_FIELDS if item.get(f) in (None, "")]
        if missing:
            raise OneToOneViolation(f"第 {index + 1} 项缺少 {missing}")

        # 同一 request body 内重复 item key → 422/409 且零 freeze(§3.7)
        key = str(item["item_request_id"])
        if key in seen_item_keys:
            raise OneToOneViolation(f"同一请求内出现重复的 item_request_id: {key}")
        seen_item_keys.add(key)

        # 一个 item 只能带**一个** media_id —— 传数组即违例(03 §12「接受 media_ids[]」)
        media = item.get("media_id")
        if isinstance(media, (list, tuple, set)):
            raise OneToOneViolation(
                f"第 {index + 1} 项带了多个账号;一篇作品恰好对应一个账号"
            )

        revision = str(item["post_revision_id"])
        if revision in seen_revisions:
            raise OneToOneViolation(
                f"作品版本 {revision} 被映射到了多个账号(第 {seen_revisions[revision] + 1} 项与"
                f"第 {index + 1} 项)—— 一篇作品恰好对应一个账号,整批拒绝"
            )
        seen_revisions[revision] = index


def aggregate_account_demand(items: Sequence[Mapping[str, Any]]) -> dict[int, int]:
    """本批对每个账号的用量。同一账号承接多篇是允许的(A4 已裁),
    但**必须先聚合再预占** —— 逐项独立预占会在批内自己超额。"""
    demand: dict[int, int] = {}
    for item in items:
        media_id = int(item["media_id"])
        demand[media_id] = demand.get(media_id, 0) + 1
    return demand


# ---------------------------------------------------------------------------
# 2. 原子日容量预占
# ---------------------------------------------------------------------------

_LOCK_SQL = "SELECT pg_advisory_xact_lock(%(key1)s, %(key2)s)"

_CURRENT_USAGE_SQL = """
SELECT COALESCE(count(*), 0) AS used
  FROM mhz_publish_order_items
 WHERE media_type = %(media_type)s
   AND media_id = %(media_id)s
   AND capacity_date = %(capacity_date)s
   AND capacity_state IN ('reserved', 'consumed')
"""

# 🔴 [返修 2026-08-18] `created_at` 是 **timestamp without time zone** ——
#    它存的是哪个墙钟,**取决于 DB 的 TimeZone 设置**。实测:
#      · 生产 omnirank-db:`TimeZone = Asia/Shanghai` ⇒ 存的是上海墙钟;
#      · 本机 PG16 夹具:  `TimeZone = Etc/UTC`       ⇒ 存的是 UTC 墙钟。
#    第一版直接拿它跟"上海业务日"的字符串边界比,在生产**碰巧**是对的
#    (DB 时区恰好等于业务时区),在 UTC 环境下就少数 —— 会超发。
#    这正是规格 §11.1 点名的陷阱:「旧 timestamp without time zone 必须按核实的
#    DB/业务时区显式 AT TIME ZONE,**不得猜机器本地**」。
#    是本包自己的容量用例在时钟跨过上海午夜时把它顶红的(昨天绿今天红,不是抖动)。
#
#    正确形态:先用 **DB 自己的时区**把 naive 解释成 timestamptz,
#    再转到**冻结的业务时区**,最后取日期。两次转换缺一不可:
#      第一次回答"这个数字是哪个时区的墙钟",第二次回答"它落在哪个业务日"。
_LEGACY_USAGE_SQL = """
SELECT COALESCE(count(*), 0) AS used
  FROM mhz_publish_order_items
 WHERE media_type = %(media_type)s
   AND media_id = %(media_id)s
   AND capacity_state IS NULL
   AND ((created_at AT TIME ZONE current_setting('TimeZone'))
             AT TIME ZONE %(tz)s)::date = %(capacity_date)s::date
   AND (status IS NULL OR status <> ALL(%(non_consuming)s::text[]))
"""


def _advisory_key(media_type: str, media_id: int, capacity_date: str) -> tuple[int, int]:
    """稳定 advisory lock 键。

    🔴 必须由 `(capacity_date, media_type, media_id)` 三者共同决定 ——
       只用 media_id 会让不同日期的预占互相阻塞(无谓的串行),
       只用日期会让所有账号抢同一把锁(整个提交面串行)。
    """
    import zlib
    key1 = zlib.crc32(f"{media_type}:{capacity_date}".encode("utf-8")) & 0x7FFFFFFF
    return int(key1), int(media_id)


class CapacityLockUnsafe(RuntimeError):
    """在 autocommit 连接上预占容量。**这是"锁了等于没锁"**,必须响亮失败。"""


def _assert_in_transaction(cur) -> None:
    """`pg_advisory_xact_lock` 是**事务级**锁 —— autocommit 下,取锁那条语句自己
    就是一个事务,语句一结束锁立刻释放。于是"取锁 → 读用量 → 判断"这三步之间
    完全没有互斥,两个并发批次会读到同一份用量、双双通过 → **超发**。

    🔴 这一条是本包自己的判据顶出来的:第一版没有它,容量用例在 autocommit
       fixture 下"通过"了,而通过的原因是锁根本没起作用。
       静默的失效比报错危险得多 —— 报错至少会有人来看。
    """
    conn = getattr(cur, "connection", None)
    if conn is not None and getattr(conn, "autocommit", False):
        raise CapacityLockUnsafe(
            "容量预占必须在显式事务内进行:pg_advisory_xact_lock 是事务级锁,"
            "autocommit 下语句一结束就释放,两个并发批次会双双通过 → 超发。"
            "调用方需在同一事务内完成 预占 → 建单 → freeze → commit(规格 02 §8.2)。"
        )


def business_capacity_date(cur, *, timezone: str = DEFAULT_BUSINESS_TIMEZONE) -> str:
    """按**冻结的业务时区**取当前业务日,不猜机器本地(§11.1 末 + §3.7)。"""
    cur.execute("SELECT (now() AT TIME ZONE %(tz)s)::date AS d", {"tz": timezone})
    return str(dict(cur.fetchone())["d"])


def reserve_daily_capacity(cur, *, media_id: int, needed: int, daily_limit: int,
                           capacity_date: str, media_type: str = "svideo",
                           timezone: str = DEFAULT_BUSINESS_TIMEZONE) -> int:
    """在调用方事务内原子预占。返回预占后的剩余量。

    🔴 双写期必须把 **legacy 行**(`capacity_state IS NULL` 的老单)也算进占用 ——
       规格 §3.7 原文:「双写切换期还要把 legacy 当日 pending/submitted 等真实占用
       纳入聚合」。漏掉它 = 老链已经发了 2 条,新链以为还剩满额 → 超发。
    """
    _assert_in_transaction(cur)
    key1, key2 = _advisory_key(media_type, int(media_id), capacity_date)
    cur.execute(_LOCK_SQL, {"key1": key1, "key2": key2})

    cur.execute(_CURRENT_USAGE_SQL, {
        "media_type": media_type, "media_id": int(media_id), "capacity_date": capacity_date})
    used = int(dict(cur.fetchone())["used"])

    cur.execute(_LEGACY_USAGE_SQL, {
        "media_type": media_type, "media_id": int(media_id),
        "capacity_date": capacity_date, "tz": timezone,
        "non_consuming": ["cancelled", "withdrawn", "failed", "rejected"]})
    used += int(dict(cur.fetchone())["used"])

    remaining = int(daily_limit) - used
    if needed > remaining:
        raise CapacityExceeded(
            f"账号 {media_id} 今天只剩 {max(0, remaining)} 次,本批需要 {needed} 次"
        )
    return remaining - int(needed)


# ---------------------------------------------------------------------------
# 3. Command 状态投影(§7.5 四条固定优先级)
# ---------------------------------------------------------------------------

def project_command_status(*, root_pending_approval: bool,
                           coordination_failed: bool,
                           attempts: Sequence[Mapping[str, Any]]) -> str:
    """把当前有效 attempts + settlement 投影成对客状态。

    优先级**逐条**照规格 §7.5,顺序不可调:

      1. root 待审批 → pending_approval;协调落单失败且无 item → failed;
      2. 任一有效 attempt 需人工 / 资金 quarantined|manual / 不可自动收敛 → needs_action;
      3. 仍有非终态 lineage 时:全部只处于 queued/reserved 且**均未 external-start** → accepted;
         除此之外一律 processing;
      4. 全部 terminal 后才算终态。

    第 3 条是最容易写错的一格:`published + queued`、`failed + queued`、
    `cancelled + queued` 这些**混合态**必须落到 processing,不能落出枚举。
    这也是为什么"有没有 external-start"要参与判断 —— 只看状态名会把
    "已经发出去一条、另一条还在排队"误判成 accepted(等于告诉用户"还没开始")。
    """
    if root_pending_approval:
        return CMD_PENDING_APPROVAL
    if coordination_failed and not attempts:
        return CMD_FAILED
    if not attempts:
        # 没有 item 又没标协调失败 —— 刚 claim 完还没落单
        return CMD_ACCEPTED

    for attempt in attempts:
        settlement = str(attempt.get("settlement_status") or "")
        state = str(attempt.get("state") or "")
        if settlement in MANUAL_SETTLEMENT or state == "needs_action":
            return CMD_NEEDS_ACTION

    non_terminal = [a for a in attempts if str(a.get("state") or "") not in TERMINAL_STATES]
    if non_terminal:
        # 🔴 两个条件都必须看**全部** attempts,不能只看非终态那些。
        #    第一版把 all_queued 算在 non_terminal 上,结果 `failed+queued` /
        #    `cancelled+queued` 落到了 accepted —— 而规格 §7.5 第 3 条原文是
        #    「**所有**当前有效 lineage 都只处于 queued/reserved 且均未 external-start
        #    → accepted;除此之外一律 processing」。有一条已经终态了,
        #    就不再是"所有 lineage 都只处于 queued/reserved"。
        #    被本包自己那三组混合态判据顶红才发现 —— 规格照着读也会读错,
        #    所以三组混合态各自一条用例,不合并成一条。
        #    用户侧的后果:告诉他"还没开始",而实际上已经有一篇发失败了。
        all_queued = all(str(a.get("state") or "") in ("queued", "reserved") for a in attempts)
        none_started = not any(a.get("external_started_at") for a in attempts)
        return CMD_ACCEPTED if (all_queued and none_started) else CMD_PROCESSING

    states = [str(a.get("state") or "") for a in attempts]
    if all(s in SUCCESS_STATES for s in states):
        return CMD_COMPLETED
    if any(s in SUCCESS_STATES for s in states):
        return CMD_PARTIAL_SUCCESS
    if all(s in CANCELLED_STATES for s in states):
        return CMD_CANCELLED
    return CMD_FAILED


def normalized_availability(attempt: Mapping[str, Any]) -> str:
    """规格 §7.5:`published` 之后的下架不覆写 raw published,另给可用性轴。"""
    if attempt.get("replaced_by_source_id"):
        return "replaced"
    if attempt.get("retracted_at"):
        return "retracted"
    if str(attempt.get("state") or "") in SUCCESS_STATES:
        return "active"
    return "not_published"


def is_retryable(attempt: Mapping[str, Any]) -> bool:
    """`retryable` 是 projector 的**派生值**,不是 raw CAS state(§3.7)。

    🔴 只有「明确失败/被拒」**且**资金已 released 才可重试。
       结果未知(`awaiting_sync`)绝不可盲重投 —— 供应商那边可能已经发了。
    """
    state = str(attempt.get("state") or "")
    settlement = str(attempt.get("settlement_status") or "")
    return (state in ("failed", "rejected") and settlement == "released"
            and not attempt.get("replaced_by_source_id")
            and attempt.get("availability") != "replaced")


def next_action_for(attempt: Mapping[str, Any]) -> str:
    if is_retryable(attempt):
        return "retry_with_another_account"
    state = str(attempt.get("state") or "")
    if state == "awaiting_sync":
        return "wait_for_sync"
    if state == "needs_action":
        return "contact_admin"
    if state in SUCCESS_STATES:
        return "view_publication"
    return "view_detail"
