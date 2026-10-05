"""新链复用旧执行器的**缝合面**:两侧状态枚举对表 + 逐值封闭映射。

## 这个模块存在的理由(第 3 棒 · Codex R2 P0-1 / P0-2)

第 2 棒把三条链"接上了",判据也证明了**调用发生**。可是其中两处缝合面
在语义上根本没接通,而"调用发生"这件事看不出来:

  · **发布提交**:worker 先把 item 置 `submitting` 再调旧提交器,
    而旧提交器只领 `status == 'pending'`(`meijiehezi_api.py:2199`)⇒
    `locked` 恒空 ⇒ 恒 `{'submitted': 0}` ⇒ **保证空转**,
    而且空转会被 `ok = submitted > 0` 读成"失败" ⇒ 每一项都 release + 标 failed;
  · **生产终态**:`outcome.ok` 是个布尔,`completing`(部分成功、资金仍冻结、
    缺图仍在)也是 `ok=True` ⇒ `TASK_STATUS_READY if outcome.ok else FAILED`
    把它提升成 `ready` ⇒ 「作品显示完成、钱还冻着、图还缺着」。

两条同一个形状:**把"调用发生 / 布尔为真"当成了"语义接通 / 状态确定"**。

## 因此本模块只做两件事

1. **对表**:把缝合面两侧的状态枚举写成代码里的常量与表,让"被调方到底
   领什么"这件事不再散落在另一个文件的第 2199 行;
2. **逐值封闭映射**:调用结果 → 终态,穷举每一个取值组合,没覆盖到的组合
   **显式抛**,不许有一条布尔中继悄悄兜住所有情况。

🔴 这里**不做**任何 I/O。它是一张表 + 两个纯函数,所以判据可以对每一个
   取值组合各打一发,而不需要真库。真库那一层由 worker 的行为判据打。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Optional

from services.geo_douyin.contract_states import (
    SETTLEMENT_COMMITTED,
    SETTLEMENT_FROZEN,
    SETTLEMENT_MANUAL,
    SETTLEMENT_RELEASED,
    TASK_STATUS_FAILED,
    TASK_STATUS_NEEDS_ACTION,
    TASK_STATUS_READY,
)

# ═══════════════════════════════════════════════════════════════════
# 缝合面 A · 发布提交:新链 worker ⇄ 现役 `_submit_short_video_order`
# ═══════════════════════════════════════════════════════════════════
#
# ## 两侧状态枚举对表(不许只写一侧)
#
# | 位置 | 谁写 | 取值 | 含义 |
# |---|---|---|---|
# | `mhz_publish_order_items.status` | 新链协调器建单 | `queued` | 合同链已建单已冻结,等 worker |
# | 同上 | **旧提交器的领取谓词** | `pending` | ← `_submit_short_video_order` 只认这一个值 |
# | 同上 | 旧提交器 `try_lock_for_submit` | `submitting` | 它自己 CAS 上去的在途标记 |
# | 同上 | 旧提交器成功 | `submitted` | 拿到单号 |
# | 同上 | 旧提交器不确定 | `awaiting_sync` | 接单数不足 / 无单号 / 歧义回执 |
# | 同上 | 旧提交器失败 | `failed` | 明确没接单(它自己会走老退款,新链已被隔离) |
#
# 🔴 **`queued` 与 `pending` 之间没有任何人搭桥**,这就是 P0-1 的全部内容。
#    worker 领取时必须把 item 置成**被调方真正会领的那个值**,而不是置成
#    一个我们自己觉得好听的在途词(`submitting` 恰好还是旧提交器的**下一态**,
#    所以它看起来非常像对的 —— 这是这条 bug 能活下来的原因)。

#: 旧提交器 `_submit_short_video_order` 的**领取谓词值**(它 `oi["status"] == "pending"`)。
#: 新链 worker 的 claim **必须**把 item 写成这个值,否则被调方永远领不到活。
LEGACY_SUBMITTER_CLAIM_STATUS: Final = "pending"

#: 旧提交器 CAS 之后的在途值(`try_lock_for_submit`:pending → submitting)。
#: 🔴 新链 claim **不得**写它 —— 写了就跳过了被调方的领取谓词。
LEGACY_SUBMITTER_INFLIGHT_STATUS: Final = "submitting"

#: 图文笔记的权威 `article_type`(1=视频直发 / 3=图文笔记 / 2 原创寄拍不在开闸范围)。
#: 见 `api/meijiehezi_api.py` 的 `ShortVideoPublishRequest.article_type` 字段注释。
ARTICLE_TYPE_IMAGE_NOTE: Final = 3
ARTICLE_TYPE_VIDEO: Final = 1

# ── 提交结果的四种语义(§7.5:结果未知不许猜成成功或退款)──
SUBMIT_DELIVERED: Final = "delivered"          # 明确接单
SUBMIT_UNKNOWN: Final = "unknown"              # 已外调、结果未知(awaiting_sync)
SUBMIT_REJECTED: Final = "rejected"            # 明确没接单、零外部效果
SUBMIT_NOT_CLAIMED: Final = "not_claimed"      # 被调方**没领到活** = 接线断裂


@dataclass(frozen=True)
class SubmitOutcome:
    """一次发布提交的**完整**处置:交付语义 + 资金 + 容量 + 是否覆写 item 状态。

    四个字段一起给,是因为它们必须由**同一个**判断产生。分开算就会出现
    「资金按失败退了、容量按成功占着」这种自相矛盾的组合。
    """
    kind: str                             # SUBMIT_* 之一
    settlement: str                       # SETTLEMENT_* 之一
    capacity_state: str                   # reserved / consumed / released
    item_status_override: Optional[str]   # None = 不覆写(旧提交器已经写过了)
    needs_manual: bool
    reason: str


def classify_submit_result(result: Optional[Mapping[str, Any]], *,
                           exception: Optional[BaseException] = None) -> SubmitOutcome:
    """把旧提交器的返回值**逐值**翻成处置。

    🔴 旧提交器的返回形态是**五种**(见 `_submit_short_video_order` 的五个 return):

      | 返回 | 发生了什么 | 本链处置 |
      |---|---|---|
      | `{submitted: 0}`(**没有** `failed` 键) | `locked` 为空 —— 它一条都没领到 | `not_claimed`:接线断裂、**确定零外调** ⇒ release 退回 + `needs_action` + 拉告警 |
      | `{submitted: 0, failed: n, ...}` | 明确全失败(远端拒 / 异常 / 接单数 0) | `rejected`:release + 释放容量 |
      | `{submitted: n, pending_sync: n}` | 接单数不足 / 无单号 / 歧义回执 | `unknown`:**保持 frozen**,容量不释放 |
      | `{submitted: n}` | 拿到单号 | `delivered`:commit + 容量 consumed |
      | 抛异常 | 网络等,远端可能已收 | `unknown`:保持冻结,转人工 |

    🔴 第一行是这次返工的核心:`submitted == 0` 有**两种**完全相反的成因,
       而 `ok = submitted > 0` 把它们压成同一个"失败"。压完之后:
       接线断了 → 每一项都被 release + 标 failed → 用户看到"发布失败",
       而供应商侧**根本没收到过任何东西**,也没有任何信号说这是我们自己断了。

    🔴 [第 4 棒 · P1-C] `not_claimed` 现在**走 release**。上一版走 manual(钱继续冻着),
       理由写的是"退了钱这条 bug 就被抹平成一次正常失败"。那个理由把两件事混在了一起:

         · **资金处置**由「外部副作用确定不确定」决定 —— 这一格是**确定零外调**
           (被调方的领取谓词没匹配上,它连远端都没联系过)⇒ 依 `08_billing §10`
           「功能失败退款 = release」,必须退;
         · **故障可见性**由**我们自己的告警面**承载(`ai_ops_alerts` + `needs_action`
           终态 + `logger.error`),不是靠**扣着用户的算力**来提醒我们自己。

       WP3 那条「卡死任务不 release」的原话是「release 的语义是确定没发生,
       而**那里恰不确定**」——**确定性反转,处置就得跟着反转**。同一条纪律,
       两个方向:不确定不许退,确定了就必须退。

    🔴 `needs_manual` 仍为 True、item 仍落 `needs_action`:钱退了不等于事情完了,
       这一篇**没发出去**,得有人来决定重投还是取消。
    """
    if exception is not None:
        return SubmitOutcome(
            kind=SUBMIT_UNKNOWN, settlement=SETTLEMENT_MANUAL,
            capacity_state="reserved", item_status_override=None, needs_manual=True,
            reason="提交外调异常,结果未知:"
                   + type(exception).__name__ + ": " + str(exception)[:120])

    if result is None:
        return SubmitOutcome(
            kind=SUBMIT_UNKNOWN, settlement=SETTLEMENT_MANUAL,
            capacity_state="reserved", item_status_override=None, needs_manual=True,
            reason="提交器没有返回任何结果,结果未知")

    data = dict(result)
    submitted = int(data.get("submitted") or 0)
    pending_sync = int(data.get("pending_sync") or 0)
    has_failed_key = "failed" in data

    if submitted > 0 and pending_sync > 0:
        return SubmitOutcome(
            kind=SUBMIT_UNKNOWN, settlement=SETTLEMENT_FROZEN,
            capacity_state="reserved", item_status_override=None, needs_manual=False,
            reason="发布渠道已收但回执不完整,等回执同步")
    if submitted > 0:
        return SubmitOutcome(
            kind=SUBMIT_DELIVERED, settlement=SETTLEMENT_COMMITTED,
            capacity_state="consumed", item_status_override=None, needs_manual=False,
            reason="")
    if has_failed_key:
        return SubmitOutcome(
            kind=SUBMIT_REJECTED, settlement=SETTLEMENT_RELEASED,
            capacity_state="released", item_status_override=None, needs_manual=False,
            reason=str(data.get("reason") or "发布渠道未接单"))
    # submitted == 0 且没有 failed 键 ⇒ 旧提交器**一条都没领到**。
    return SubmitOutcome(
        kind=SUBMIT_NOT_CLAIMED, settlement=SETTLEMENT_RELEASED,
        capacity_state="released", item_status_override="needs_action",
        needs_manual=True,
        reason="发布提交器未领到本项(领取谓词不匹配 = 接线断裂),"
               "确定未联系发布渠道,算力已原路退回,这一篇需要重新发")


# ═══════════════════════════════════════════════════════════════════
# 缝合面 B · 生产终态:`ProductionOutcome` ⇄ §5.7 封闭状态集
# ═══════════════════════════════════════════════════════════════════
#
# | `outcome.ok` | `outcome.completing` | `settlement_ok` | 真实含义 | §5.7 终态 |
# |---|---|---|---|---|
# | True | False | **True** | 全部卡片就位、已 commit | `ready` |
# | True | False | **False** | 卡片就位但 **commit 没成功**(找不到冻结/跨表撞号) | `needs_action` |
# | True | True | (不看) | **部分**交付:缺图仍在、冻结**未**收敛 | `needs_action` |
# | False | * | (不看) | 失败,已走 release 出口 | `failed` |
# | False | True | * | 不可能(部分交付蕴含 ok=True) | **抛** |
#
# 🔴 [第 4 棒 · P0-B] `settlement_ok` 是**追加**的第三维,默认 True ——
#    没传它的老调用路径行为**逐字节不变**,新分支只有在结算真失败时才够得着。
#    加维而不是改写原谓词:改写会把"没传参数的老路"也换掉语义。


@dataclass(frozen=True)
class ProductionTerminal:
    status: str
    advance_slot_to_ready: bool
    settlement_expectation: str
    reason: str


class ImpossibleOutcome(AssertionError):
    """出现了枚举表里没有的取值组合。**抛**,不回落到某个"安全"默认值。

    回落是这类 bug 的温床:一个新加的 outcome 维度会被静默归进旧格子,
    而没有任何一条判据会红(本仓「未知状态原样透传 / 静默归类」同族)。
    """


def resolve_production_terminal(*, ok: bool, completing: bool,
                                settlement_ok: bool = True) -> ProductionTerminal:
    """生产结果 → §5.7 封闭状态集。**逐值**,不是布尔中继。

    🔴 `completing` 为真时必须是 `needs_action` 而不是 `ready`:
       那一格的三个事实同时成立 —— 有成品、**缺图**、**钱还冻着**。
       写成 `ready` 会让三个读侧同时说谎:批次进度显示完成、
       资金视图显示已结算、槽位推进到 ready(于是这一篇被当作已交付计入合同)。

    🔴 [第 4 棒 · P0-B] `settlement_ok=False` 也不能是 `ready`:图做出来了,
       但 `commit_freeze` 返回了 `{"success": False}`(找不到冻结 / 跨表撞号)。
       这时候钱**既没扣也没退**,槽位更不能推进 —— 推进就等于把一篇
       "还不知道钱在哪"的作品计入合同交付。
    """
    if bool(ok) and not bool(completing) and not bool(settlement_ok):
        return ProductionTerminal(
            status=TASK_STATUS_NEEDS_ACTION, advance_slot_to_ready=False,
            settlement_expectation=SETTLEMENT_MANUAL,
            reason="成品已生成但算力结算未完成,需人工核对后再确认交付")
    if bool(ok) and not bool(completing):
        return ProductionTerminal(
            status=TASK_STATUS_READY, advance_slot_to_ready=True,
            settlement_expectation=SETTLEMENT_COMMITTED, reason="")
    if bool(ok) and bool(completing):
        return ProductionTerminal(
            status=TASK_STATUS_NEEDS_ACTION, advance_slot_to_ready=False,
            settlement_expectation=SETTLEMENT_FROZEN,
            reason="部分卡片仍待补齐,算力保持冻结待收敛")
    if not bool(ok) and not bool(completing):
        return ProductionTerminal(
            status=TASK_STATUS_FAILED, advance_slot_to_ready=False,
            settlement_expectation=SETTLEMENT_RELEASED, reason="生成失败")
    raise ImpossibleOutcome(
        "outcome.ok=False 且 completing=True 不是合法组合:"
        "部分交付蕴含 ok=True(见 production_task 的 §15-2 分支)")


# ═══════════════════════════════════════════════════════════════════
# 缝合面 C · 崩溃窗口:被调方已自行提交终态,我方结算事务还没提交
# ═══════════════════════════════════════════════════════════════════
#
# ## 为什么会有这个缝(第 4 棒 · Codex R3 P0-A)
#
# 缝合面 A 修好之后,发布提交**真的**走进了旧提交器。而旧提交器
# (`_submit_short_video_order`)自带**独立连接、独立 commit** ——
# 它成功时 `update_order_item_submitted` 当场提交 `status='submitted'`,
# 失败时 `update_order_item_status` 当场提交 `status='failed'`。
# 我方的结算(`_settle_publish_item`)是**之后另开的一个事务**。
#
# 于是中间有一条**测不到也收不掉**的缝:
#
# ```
# 旧提交器 commit(status=submitted/failed)   ← 已落库、已提交
#            │
#            ▼   ← 进程在这里被 kill / OOM / 蓝绿切换掐掉
#   _settle_publish_item 的事务(资金 + settlement_status)   ← 从没提交
# ```
#
# 留下的组合是 `status='submitted' + settlement_status='frozen'`
# 或 `status='failed' + settlement_status='frozen'`,而既有回收器
# `_reconcile_stuck_publish_items` 的扫描集是 `('pending','submitting')` ——
# **这两个态一个都不在里面**。没有任何其它 writer 会再碰它们:
# 钱永久冻着,用户看到"已提交/失败",账上既没扣也没退。
#
# ## 三条口径(定这条缝怎么收)
#
# 1. **做不到同事务就别假装做得到**。旧提交器不归我们管,它的 commit 无法并进
#    我们的事务。做不到 ⇒ **逐态枚举 + 逐态收敛**,而不是留一个测不到的窗口;
# 2. **收敛器由 `settlement_status` 驱动**,不由 item `status` 反猜。
#    `status` 是**被调方**写的,值域不归我们管(它随时可能多一个值);
#    `settlement_status` 是我们自己的资金态,它才是"这笔钱到底结没结"的唯一真值。
#    扫描谓词写成 `settlement_status='frozen' AND status IN (被调方终态集合)`,
#    "还没结账"这件事由前半句认定,后半句只是把窗口限定在**已经外调过**的那些;
# 3. **处置只认耐久证据,不猜**(`08_billing §11`):`submitted` 这一格之所以敢
#    commit,是因为 `update_order_item_submitted` 是 fail-closed 的 ——
#    它拒绝写空单号(`meijiehezi_db.py`),所以库里有 `mhz_order_id`
#    **就等于**供应商给过回执。单号为空的 `submitted` 是自相矛盾,落人工。
#
# ## 逐态处置表
#
# | `status`(被调方写的) | 耐久证据 | 确定性 | 资金 | 容量 | item 终态 |
# |---|---|---|---|---|---|
# | `submitted` + `mhz_order_id` 非空 | 供应商单号 | 确定已接单 | **commit** | consumed | 不覆写 |
# | `submitted` + 单号为空 | 无(自相矛盾) | 不确定 | 保持冻结 → `manual` | reserved | `needs_action` |
# | `awaiting_sync` | 有回执但不完整 | **不确定** | 保持冻结 → `manual` | reserved | 不覆写 |
# | `failed` | 明确未接单(老退款对新链已隔离) | 确定零外部效果 | **release** | released | 不覆写 |
#
# 🔴 `awaiting_sync` 那一格是**故意不动钱**的:它的语义就是"远端可能收了"。
#    与 WP3「不确定不 release」同一条纪律 —— 这里没有确定性反转,所以处置不反转。

#: 旧提交器**自己会写**的终态集合。这三个值一旦落库,我方 claim 的谓词
#: (`queued`)与既有回收器的扫描集(`pending`/`submitting`)**都够不着**,
#: 于是它们只能靠本缝合面的收敛器来收。
LEGACY_SUBMITTER_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({
    "submitted", "awaiting_sync", "failed",
})


def classify_unsettled_publish_item(*, item_status: object,
                                    mhz_order_id: object) -> SubmitOutcome:
    """崩溃后遗留的「已外调、未结算」item → 处置。**逐值,不猜**。

    与 `classify_submit_result` 的区别:那个读的是**函数返回值**(在途),
    这个读的是**库里的行**(崩溃之后)。两者必须分开写 —— 崩溃之后我们
    手上没有返回值了,只有被调方留下的痕迹,能用的证据集合是不一样的。

    🔴 值域外的 `status` **抛**,不回落。收敛器的扫描谓词已经把范围限定在
       `LEGACY_SUBMITTER_TERMINAL_STATUSES`,所以走到这里还认不出来,
       说明被调方新增了一个我们没对过表的终态 —— 那必须响亮,不能静默归类。
    """
    status = str(item_status or "").strip()
    order_no = str(mhz_order_id or "").strip()

    if status == "submitted":
        if order_no:
            return SubmitOutcome(
                kind=SUBMIT_DELIVERED, settlement=SETTLEMENT_COMMITTED,
                capacity_state="consumed", item_status_override=None,
                needs_manual=False,
                reason="崩溃前已拿到发布渠道单号,补结算")
        return SubmitOutcome(
            kind=SUBMIT_UNKNOWN, settlement=SETTLEMENT_MANUAL,
            capacity_state="reserved", item_status_override="needs_action",
            needs_manual=True,
            reason="标为已提交却没有发布渠道单号,结果无法确认,算力保持冻结待人工核对")

    if status == "awaiting_sync":
        return SubmitOutcome(
            kind=SUBMIT_UNKNOWN, settlement=SETTLEMENT_MANUAL,
            capacity_state="reserved", item_status_override=None,
            needs_manual=True,
            reason="发布渠道回执不完整,结果未知,算力保持冻结待人工核对")

    if status == "failed":
        return SubmitOutcome(
            kind=SUBMIT_REJECTED, settlement=SETTLEMENT_RELEASED,
            capacity_state="released", item_status_override=None,
            needs_manual=False,
            reason="发布渠道明确未接单,算力已原路退回")

    raise ImpossibleOutcome(
        "未结算窗口里出现了没对过表的 item 状态 %r —— "
        "被调方的终态集合变了,必须先更新 LEGACY_SUBMITTER_TERMINAL_STATUSES" % (status,))


# ═══════════════════════════════════════════════════════════════════
# 缝合面 D · 制作链:资金已 commit,成品版本还没冻
# ═══════════════════════════════════════════════════════════════════
#
# 同一个形状的第三个窗口。`run_image_post_production` 内部**自己**提交了
# 「task=ready + settlement=committed + post=ready」,而 worker 的收尾
# (`finish_task` + `_freeze_active_revision` + `_advance_slot_to_ready`)
# 是**返回之后另开的事务**。崩在中间留下:
#
#   `task.status='ready'` + `settlement_status='committed'`
#   + `posts.active_revision_id IS NULL` + 槽位没推进 + 租约留在死掉的 worker 上
#
# 后果不是"少一行记录":素材准备入口**硬要求** `active_revision_id`,
# 所以这一篇**永久 409**,而钱已经扣了。既有的
# `reconcile_stuck_external_tasks` 只扫 `status='running'`,够不着它。
#
# 🔴 这一格**不涉及资金决策**(钱已经 commit 完了,是对的),收敛器要做的
#    是**把没做完的收尾补上**,所以判别谓词写在 SQL 里(见 contract_worker),
#    这里只留一个常量把"什么算这个窗口"写成代码里的一句话。

#: 制作链未收尾窗口的判别式:资金已结清、任务已终态,但成品版本指针还是空的。
UNFINISHED_PRODUCTION_TAIL: Final = (
    "settlement_status = 'committed' AND status = 'ready' "
    "AND active_revision_id IS NULL")


# ═══════════════════════════════════════════════════════════════════
# 缝 E · 制作链收尾收敛的**身份闸**(第 7 棒 · Codex R7 P0-A)
# ═══════════════════════════════════════════════════════════════════
#
# 🔴 为什么必须有这一闸:收敛器的 claim 语句只对 **task 行**加了锁
#    (`FOR UPDATE SKIP LOCKED` 打在 task 的子查询上),**post 行没被锁**。
#    于是 claim 与真正动手之间,post 可以被别的事务改掉 —— 而收敛器接下来要写的
#    四个对象里有三个挂在 post 上(成品版本 / 作品状态 / 槽位)。
#
# 🔴 Codex 的反例:`post.status = 'failed'`(这一篇已经被判失败)时,上一版收敛器
#    照样冻成品版本、推槽位、把 task 写成 ready —— 把一篇失败的作品**复活**成
#    已完成。收敛器的职责是"补写没写完的那一笔",不是"改写已经定下的事实"。
#
# 🔴 谓词只写在这里一处。task 侧的条件(settlement/status/superseded)留在 claim
#    SQL 里 —— 那一行**已经被 claim 的 UPDATE 锁住**,再判一遍是恒真的重复。
#    两处判同一件事,永远有一处没人验(第 6 棒变异 Q7 的教训)。

#: 收敛器唯一允许动手的作品状态。窄谓词是刻意的:`settlement='committed'` 只在
#: **全量成功**路径产生,所以这一格的作品确实是做完了、只差把状态写上。
HEALABLE_POST_STATUS = "generating"


@dataclass(frozen=True)
class TailIdentity:
    """收尾收敛的身份核验结果。`ok=False` ⇒ 调用方必须**四对象零变更**退出。"""
    ok: bool
    reasons: tuple[str, ...]


def classify_production_tail_identity(*, task_id: int,
                                      row: Optional[Mapping[str, Any]]) -> TailIdentity:
    """在**同一个事务、post 行已加锁**之后核验身份。

    逐条列出不符项(而不是返回一个布尔),是为了让告警能说清"哪一项不符" ——
    "身份不符"这四个字对排障没有任何帮助。
    """
    if not row:
        return TailIdentity(False, ("task 行在 claim 与加锁之间消失了",))
    reasons: list[str] = []
    if row.get("post_deleted_at") is not None:
        reasons.append("作品已删除")
    if str(row.get("post_status") or "") != HEALABLE_POST_STATUS:
        reasons.append("作品状态是 %s,不是 %s —— 这一篇的事实已经由别人定下"
                       % (row.get("post_status"), HEALABLE_POST_STATUS))
    if row.get("active_revision_id") is not None:
        reasons.append("成品版本已经冻好了(active_revision_id=%s),没有窗口"
                       % (row.get("active_revision_id"),))
    active_task = row.get("active_generation_task_id")
    if active_task is None or int(active_task) != int(task_id):
        reasons.append("作品当前的活动生成是 task=%s,不是 task=%s —— 已易主"
                       % (active_task, task_id))
    return TailIdentity(not reasons, tuple(reasons))


# ═══════════════════════════════════════════════════════════════════
# 缝 F · **幂等成功 ≠ 真结算**(第 8 棒 · R8 P0)
# ═══════════════════════════════════════════════════════════════════
#
# 🔴 `commit_freeze` / `release_freeze` 在冻结**已经是终态**时返回
#    `{"success": True, "idempotent": True, "status": <终态>}` —— 它说的是
#    "这一笔不用我动了",**不是**"钱按你要的方向动了"。
#
#    只看 `success` 的调用方会在这一格说谎:冻结早被**退**掉(`released`),
#    而我们把 item 写成 `committed` —— 用户拿了退款,账上却记着已付。
#    R8 的四步反例正是这条:
#      ① 老 sweeper 抢先把合同项标 failed 并按老口径退款
#      ② 合同链自己 release 掉冻结(确定未外调)
#      ③ 渠道迟到接单 ⇒ 走 `committed` 结算
#      ④ `commit_freeze` 返 idempotent(status='released')⇒ 上一版写 committed
#
# 🔴 **判据是「冻结的终态等于我们要的方向」,不是「调用返回了 success」。**


#: 真结算的两种合法形态:本次真的动了(非幂等),或**之前就已经动到同一方向**。
SETTLEMENT_INTENT_COMMITTED: Final = "committed"
SETTLEMENT_INTENT_RELEASED: Final = "released"


def settlement_actually_happened(funds: Optional[Mapping[str, Any]], *,
                                 intent: str) -> tuple[bool, str]:
    """资金调用的返回值 → (钱真的按 `intent` 动了吗, 说不通时的原因)。

    ⚠️ **与工单字面口径的一处偏差,已在交付单 §1 ① 显式声明**:
       工单写「idempotent=True 而 status≠frozen 时禁写 committed」。
       但 `idempotent=True` **必然**伴随 `status≠frozen`(那正是它的触发条件),
       照字面实现会把**合法的崩溃重放**(上一次已经 commit 成功、item 终态还没写完)
       也打成 needs_action —— 而那恰是本包 R3/R4 花了两棒收敛的窗口,
       等于把它反向拆掉,并且会给用户面造一条假的"待处理"。
       所以这里判的是**冻结终态 == 本次意图**:
         · 非幂等成功            ⇒ 钱这次真动了            ✅
         · 幂等 + status == 意图 ⇒ 钱之前已经动到同一方向  ✅(合法重放)
         · 幂等 + status != 意图 ⇒ **方向相反**            ❌ needs_action + 告警
       这样既堵住 R8 的反例,又不误伤重放。
    """
    if funds is None:
        return True, ""
    if not bool(funds.get("success")):
        return False, str(funds.get("reason") or "结算调用未成功")
    if not bool(funds.get("idempotent")):
        return True, ""
    actual = str(funds.get("status") or "")
    if actual == str(intent):
        return True, ""
    return False, ("冻结已是终态 `%s`,与本次要写的 `%s` 方向不一致 —— "
                   "幂等返回的是「不用我动了」,不是「按你要的动了」" % (actual, intent))
