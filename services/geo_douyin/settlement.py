"""GEO 图文 · 部分成功交付与结算(Review §15 裁定 · 2026-08-03)

## 为什么要有这个模块

原规则是 `if not batch.all_ok: 整条作废`。实测把它打穿了:
单卡成功率 ~0.9 时,五张联合只有 0.9⁵ ≈ 59% —— 生产 5 次跑出 0 个成品,
**两次都是 4/5**。那不是质量问题,是**把联合概率当成了门槛**。

## §15 四条(逐条落点)

1. **成品门槛** = 封面成功 **且** 成功卡 ≥ 一半。低于门槛 → 维持现行全退。
   → `is_deliverable()`(纯函数,可直接喂输入断输出)
2. **达门槛有失败卡 → 「补齐中」**:冻结**保持不动**,已生成的卡全留,
   失败卡由系统**自动免费重抽**(≤2 轮,**不占用户 10 次额度**)。
   → 自动补齐在 production_task 里(它握着 specs);本模块只管结算。
3. **补齐全组才 commit** —— "完成才扣"一个字不破,只是"完成"从一次性变成可补齐。
   → `try_settle()`
4. **补不齐交还用户手动重抽;12h sweeper 死线兜底全退** —— 本模块**零新增资金路径**。
   ⚠️ 2026-08-03 起用户手动重抽本身收费(100 算力,Owner 拍板),那笔冻结由
   API 层单独 freeze/commit,与本模块管的**整组生产冻结**是两笔,不要混。
   系统自动补齐(第 2 条)照旧免费且不占额度。
   → 不写任何新的退款逻辑:冻结就搁在那儿,平台既有的
     `services/freeze_sweeper.py`(每小时扫 12h+ frozen 自动 release)兜底。

## 🔴 恰好 commit 一次

补齐可能由两条路触发(系统自动重抽 / 用户手动重抽),而用户可以并发点。
所以"这一组齐了吗"**不能**先查后改 —— 必须用**条件 UPDATE 抢占**:
谁把任务行从 `completing` 改成 `succeeded` 谁才去 commit,抢不到的直接返回。
先查后改在并发下会 commit 两次 = 扣两次钱。

🔴 本模块是**唯一**碰 commit_freeze 的补齐出口。redraw.py 一行计费代码都不许有
   (`test_redraw_is_free_no_billing_import` 锁着),所以调用方向是
   API → settlement,不是 redraw → settlement。
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("GEO-Douyin-Settle")

# 「补齐中」的状态字面量。
# 🔴 已核实 geo_douyin_posts / geo_douyin_post_tasks 的 status 列**没有 CHECK 约束**
#    (唯一的 CHECK 是 redraw_count >= 0)→ 加新状态值不需要迁移,也不会让
#    "两槽失去可启动性"(往 CHECK 加允许值不是 additive,本项目栽过 P0)。
STATUS_COMPLETING = "completing"

# 系统自动补齐的轮数上限(§15:≤2 轮)。
AUTO_FILL_MAX_ROUNDS = 2


def card_is_ok(card: Dict[str, Any]) -> bool:
    """一张卡算不算成功 —— 判据统一在这里,别在三处各写一遍。"""
    return str((card or {}).get("status") or "") == "ready" and bool((card or {}).get("oss_key"))


def min_ok_required(total: int) -> int:
    """成品门槛所要求的最少成功张数 = 总数的一半(向上取整)。

    5 张 → 3;4 张 → 2;1 张 → 1。
    """
    return max(1, math.ceil(int(total) / 2))


def is_deliverable(cards: Sequence[Dict[str, Any]]) -> bool:
    """§15-1 成品门槛:**封面成功** 且 **成功卡 ≥ 一半**。

    🔴 封面是硬条件,不是"算一张"。封面没出来的组在预览、缩略图、
       发布封面三处都残缺,留下来对用户没有价值 —— 那种情况维持现行全退。
    """
    items = list(cards or [])
    if not items:
        return False
    cover = next((c for c in items if str((c or {}).get("kind") or "") == "cover"), None)
    if cover is None:
        # 没有封面这一类(张数预算把封面砍了)→ 退回只看数量
        return sum(1 for c in items if card_is_ok(c)) >= min_ok_required(len(items))
    if not card_is_ok(cover):
        return False
    return sum(1 for c in items if card_is_ok(c)) >= min_ok_required(len(items))


def pending_indices(cards: Sequence[Dict[str, Any]]) -> List[int]:
    """还没出来的卡的下标(0 基)。补齐就是把这些补上。"""
    return [i for i, c in enumerate(cards or []) if not card_is_ok(c)]


async def try_settle(post_id: int) -> str:
    """这一组齐了就结算(commit 恰好一次)。返回本次动作的描述。

    返回值:
        "settled"      —— 本次抢到并 commit 了
        "not_complete" —— 还有卡没出来,什么都没做
        "already"      —— 别人已经结算过(或本任务不在补齐中),什么都没做
        "no_task"      —— 找不到任务行

    🔴 `commit_freeze` 只在**抢占成功**之后调用。抢占是一条
       `WHERE status='completing'` 的条件 UPDATE —— 并发下只有一个赢家。
    """
    import asyncio

    from db import geo_douyin_db as ddb

    post = await asyncio.to_thread(ddb.get_post, post_id)
    if not post:
        return "no_task"
    cards = list(post.get("cards") or [])
    if pending_indices(cards):
        return "not_complete"

    task = await asyncio.to_thread(ddb.get_task_by_post, post_id)
    if not task:
        return "no_task"

    # 🔴 抢占:先查后改在并发下会 commit 两次 = 扣两次钱
    claimed = await asyncio.to_thread(ddb.claim_task_settlement, int(task["id"]))
    if not claimed:
        return "already"

    freeze_id = claimed.get("freeze_id")
    task_ref = claimed.get("task_ref") or ""
    if freeze_id:
        from middleware.billing import commit_freeze
        try:
            await commit_freeze(freeze_id=freeze_id, task_ref=task_ref,
                                reason="抖音图文帖补齐完成")
        except Exception as e:  # noqa: BLE001
            # 🔴 commit 失败必须显式留痕(资金面),不吞。
            #    任务行已被抢占成 succeeded —— 不回滚它:回滚会让下一次重试
            #    再抢一次、再 commit 一次,反而制造重复扣款。
            #    冻结留在 frozen,12h sweeper 会 release(用户不吃亏)。
            logger.error("[douyin-settle] commit 失败 task_ref=%s freeze_id=%s: %s",
                         task_ref, freeze_id, e)
    await asyncio.to_thread(ddb.set_post_status, post_id, "ready")
    logger.info("[douyin-settle] post=%s 补齐完成已结算", post_id)
    return "settled"
