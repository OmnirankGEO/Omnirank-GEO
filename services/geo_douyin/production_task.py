"""GEO 抖音图文管线 v1 · 图文生产任务编排(B 类异步长任务)

计费口径(工单 §3.1 · 与既有 B 类任务同一套,不新造):
    freeze_points(feature_code) → 干活 → commit_freeze(成功) / release_freeze(失败)

🔴 "完成才扣"的心智不能破:任何一步失败都必须 release_freeze 退回,
   **绝不允许失败了还把冻结留着**(资金面,出问题只标记不自改 —— 但这条是本单
   自己新增的调用点,属本单职责)。

🔴 事件循环红线(§3.2):本模块只 await 真异步函数;同步 DB 调用统一走 to_thread。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import List, Optional

# 🔴 合同链的"得有人看一眼"终态取自**枚举唯一处**,不在这里另写一个字符串。
#    (contract_states 只依赖 logging/typing,不会成环。)
from services.geo_douyin.contract_states import (
    TASK_STATUS_NEEDS_ACTION as CONTRACT_STATUS_NEEDS_ACTION,
)
from services.geo_douyin.contract_seams import (
    SETTLEMENT_INTENT_COMMITTED,
    settlement_actually_happened,
)
from services.geo_douyin.config import (
    CARD_COUNT_DEFAULT,
    CARD_COUNT_MAX,
    CARD_COUNT_MIN,
    FEATURE_CODE_IMAGE_POST,
    TASK_TOTAL_TIMEOUT_SECONDS,
)
from services.geo_douyin.settlement import (
    AUTO_FILL_MAX_ROUNDS,
    STATUS_COMPLETING,
    is_deliverable,
    pending_indices,
)

logger = logging.getLogger("GEO-Douyin-Task")

#: 榜单形态的触发值。**唯一入口** —— 除此之外任何取值都走原有卡组型路径。
RANKING_FORM = "ranking"


def _set_contract_settlement(task_id: int, settlement_status: str) -> None:
    """只改合同链任务的 `settlement_status`。**一列一句**,不顺手改别的。

    🔴 用独立连接而不是复用调用方事务:这一笔发生在 provider 跑完之后,
       入口那笔事务早就 commit 了。这里的"原子"只需要覆盖这一列本身。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_douyin_post_tasks SET settlement_status = %s, updated_at = now()"
            " WHERE id = %s", (str(settlement_status), int(task_id)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def build_task_ref(post_id: int) -> str:
    """task_ref 是 freeze/commit/release 的对账键,必须唯一(表上有 UNIQUE 索引)。"""
    return f"geo_douyin_post:{int(post_id)}:{int(time.time())}:{secrets.token_hex(3)}"


@dataclass
class ProductionOutcome:
    ok: bool
    post_id: int
    task_id: Optional[int] = None
    error: str = ""
    cards_done: int = 0
    cards_total: int = 0
    refunded: bool = False
    # §15:达成品门槛但还有卡没补齐。ok=True(有成品)但尚未 commit 扣款。
    completing: bool = False
    # 🔴 [第 4 棒 · Codex R3 P0-B] 资金结算**真的**成功了没有。
    #    `commit_freeze` / `release_freeze` 找不到冻结或跨表撞号时返回
    #    `{"success": False}` 而**不抛** —— 少了这一维,"东西做完了"与
    #    "钱结清了"就被压成同一个 ok=True,库里写 committed 而钱没动。
    settlement_ok: bool = True
    # [#184 d1] 本次结果被**新一代生成**接管:不是失败(没有任何东西坏掉),
    #   也不是成功(结果作废、钱已退)。压进 ok/error 会让调用方分不清
    #   "做砸了要重试" 与 "用户自己又点了一次重做"。
    superseded: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "post_id": self.post_id, "task_id": self.task_id,
            "error": self.error, "cards_done": self.cards_done,
            "cards_total": self.cards_total, "refunded": self.refunded,
            "completing": self.completing, "settlement_ok": self.settlement_ok,
            "superseded": self.superseded,
        }


def _settlement_handle(contract_task: Optional[dict], *,
                       freeze_id, task_ref: str) -> dict:
    """组装 `commit_freeze` / `release_freeze` 的**整组定位句柄**。

    🔴 [第 4 棒 · Codex R3 P0-B] 上一版只传 `freeze_id + task_ref`。
       两张冻结表(`point_freezes` / `customer_credit_freezes`)的 id 是
       **各自独立自增**的,必然撞号;不传 `freeze_table` / `user_id`,
       `_route_freeze_table` 就只能去猜表,猜错 = 结算到**别人那一笔**。
       句柄该带哪几个键,不是我拍的 —— 是照 `middleware/billing.py` 里
       `commit_freeze` / `release_freeze` 的**真实签名**取的(它们只认
       `freeze_id / task_ref / user_id / freeze_table`;`physical_split_snapshot`
       是冻结时落库的审计快照,**不是**结算入参,所以这里不传)。

    🔴 老链(`contract_task is None`)**一个字节都不变**:它的任务行上根本
       没有这组列(034 只给合同链建的),硬传 None 会把 `user_id=None` 这条
       消歧路径的行为改掉。默认路径保持原样,新键只在有句柄时追加。
    """
    handle: dict = {"freeze_id": freeze_id, "task_ref": task_ref}
    if not contract_task:
        return handle
    if contract_task.get("payer_user_id") is not None:
        handle["user_id"] = int(contract_task["payer_user_id"])
    if str(contract_task.get("freeze_table") or "").strip():
        handle["freeze_table"] = str(contract_task["freeze_table"]).strip()
    return handle


async def commit_contract_settlement(contract_task: Optional[dict], *, freeze_id,
                                     task_ref: str, task_id) -> bool:
    """成功出口的资金收尾:整组句柄 commit → **逐格判返回值** → 落 settlement 列。

    返回 `True` 表示钱**真的**结清了。

    🔴 [第 4 棒 · P0-B] 单独成函数不是为了好看,是为了**可判**:这段逻辑原来
       内联在 `run_image_post_production` 那个几百行的成功分支里,判据要够到它
       就得把整条生产链(LLM + 出图)跑一遍 —— 于是没人够得到,
       「不看返回值」那一行改坏了也没有任何判据会红(本轮变异 M7 实测存活)。

    🔴 `commit_freeze` 在**函数内**导入:这样判据 monkeypatch
       `middleware.billing.commit_freeze` 才拦得住(模块顶层导入会把旧对象
       绑死在本模块的命名空间里,patch 打在空处 —— 本仓踩过)。
    """
    from db.connection import get_connection
    from middleware.billing import commit_freeze

    if not freeze_id:
        if contract_task:
            await asyncio.to_thread(_set_contract_settlement, task_id, "committed")
        return True

    # 🔴 [第 5 棒 · Codex R5 P0-2] 资金 commit 与 `settlement_status` 落列
    #    **必须同一个事务**。上一版是两段:`commit_freeze` 自带连接先提交,
    #    `_set_contract_settlement` 另开连接再提交 —— 中间崩溃留下
    #    「账已扣、settlement 仍是 frozen」这个**更早的窗口**,
    #    而第 4 棒的收尾收敛器只扫 `committed AND ready`,**盖不住它**。
    #
    #    `commit_freeze` 支持 `_cursor=`(发布链已经在用这条路径),
    #    传进去它就在**调用方事务**里做,不自己 commit。于是这一段要么
    #    「钱扣了 + 列写了」一起生效,要么一起回滚 —— 中间态不可观测。
    def _open():
        return get_connection()

    conn = await asyncio.to_thread(_open)
    try:
        cur = conn.cursor()
        funds = await commit_freeze(
            reason="抖音图文帖生产完成", _cursor=cur,
            **_settlement_handle(contract_task, freeze_id=freeze_id, task_ref=task_ref))
        # 🔴 `commit_freeze` 找不到冻结 / 跨表撞号歧义时返回 `{"success": False}`
        #    且**不抛**。不看返回值 = 库里写 committed 而钱一分没动。
        # 🔴 [第 8 棒 · R8 P0] 但**只看 success 还不够**:对一笔已经被**退掉**的冻结,
        #    它返回 `success=True, idempotent=True, status='released'` ——
        #    "这一笔不用我动了" ≠ "钱按你要的方向动了"。发布链那边(R8 ①)
        #    正因为这一格把 item 写成了 committed;制作链是**同一个病的第二条链**,
        #    一并收(本包已经因为「一个病两条链只修一条」被判过一次)。
        committed, funds_reason = settlement_actually_happened(
            funds, intent=SETTLEMENT_INTENT_COMMITTED)
        if not committed:
            logger.error("[douyin-task] 资金未按 committed 落实 task_ref=%s:%s | 返回=%s",
                         task_ref, funds_reason, funds)
        if contract_task:
            # 🔴 结算没成功 ⇒ 落 `manual` 而不是 `committed`。
            #    写 committed 会让资金视图说"已结算"而账上一分没动 ——
            #    这正是 R3 P0-B 那条"库说结了、钱没动"的最后一格。
            cur.execute(
                "UPDATE geo_douyin_post_tasks"
                "   SET settlement_status = %s, updated_at = now() WHERE id = %s",
                ("committed" if committed else "manual", int(task_id)))
        await asyncio.to_thread(conn.commit)
    except Exception:
        await asyncio.to_thread(conn.rollback)
        raise
    finally:
        await asyncio.to_thread(conn.close)
    return committed


async def _auto_fill(post_id: int, specs: List[dict], rows: List[dict],
                     task_id: int) -> List[dict]:
    """§15-2 系统自动免费补齐失败卡(≤2 轮)。返回更新后的卡片行。

    🔴 用 render_one_card,**不用 redraw_one_card** —— 后者会 bump 用户那
       10 次免费额度,而这是系统在补自己没做完的活,§15 明确不占用户额度。
    🔴 补齐失败不抛异常:补不齐就交还用户手动重抽,不是把整条打回失败。
    """
    from services.geo_douyin.image_pipeline import render_one_card

    out = [dict(r) for r in rows]
    for rnd in range(1, AUTO_FILL_MAX_ROUNDS + 1):
        todo = pending_indices(out)
        if not todo:
            break
        logger.info("[douyin-task] post=%s 自动补齐第 %s/%s 轮,待补 %s 张",
                    post_id, rnd, AUTO_FILL_MAX_ROUNDS, len(todo))
        for i in todo:
            spec = specs[i] if i < len(specs) else {}
            try:
                res = await render_one_card(
                    post_id, i + 1, str(spec.get("headline") or ""), "",
                    prompt_override=str(spec.get("prompt") or ""))
            except Exception as e:  # noqa: BLE001 - 补齐失败 ≠ 整条失败
                logger.warning("[douyin-task] 补齐第 %s 张异常: %s", i + 1, e)
                continue
            if res and res.ok and res.oss_key:
                out[i] = {**out[i], "oss_key": res.oss_key, "status": "ready"}
                try:
                    await asyncio.to_thread(ddb_bump, task_id)
                except Exception:  # noqa: BLE001
                    pass
    return out


def ddb_bump(task_id: int) -> None:
    """补齐成功也要推进度条(否则用户看着 4/5 不动)。"""
    from db import geo_douyin_db as ddb
    ddb.bump_task_progress(task_id)


async def run_image_post_production(
    *,
    post_id: int,
    user_id: int,
    keyword: str,
    brand_id: Optional[int] = None,
    brand_name: str = "",
    city: str = "",
    card_count: int = CARD_COUNT_DEFAULT,
    extra_hint: str = "",
    variant_index: int = 0,
    feature_code: str = FEATURE_CODE_IMAGE_POST,
    style_key: str = "",
    contact_line: str = "",
    aspect_ratio: str = "",
    industry_key: str = "",
    # 🔴 下单前冻好的行业归并件(包 B · `industry_canonical.CanonicalIndustry.to_meta()`)。
    #    空 = 老订单 / 别的入口进来的,`build_ranking_plan` 会现场归并兜底(fail-soft)。
    #    执行期**只消费不重算**:期间 admin 改了行业名也不影响这一单。
    frozen_industry: Optional[dict] = None,
    # 🔴 榜单分支的**唯一触发口**。空 = 卡组型(现有行为逐字不变);
    #    "ranking" = 走榜单主链。默认值保证存量订单一个字都不受影响。
    content_form: str = "",
    # 榜单要点名几家(3-6)。0 = 按母版默认。**与"做几张图"是两件事** ——
    # 上一版拿 card_count 当家数用,选 7 张图就变成想要 7 家,那是把两个旋钮焊在一起。
    ranking_entity_count: int = 0,
    # 版式手动覆盖(十母版之一)。空 = 自动路由(默认路径)。
    # 🔴 它只影响**版式**,不能把无据说成有据 —— 见 `route_template` 的硬约束。
    ranking_template: str = "",
    # 手动坚持榜单式(覆盖"面×行业默认不走榜单"那一层)。同样不能把无据说成有据。
    ranking_force: bool = False,
    # 🔴 [返工 2026-08-18 · 链 3] 合同链模式:任务行与冻结**已经**由
    #    `/api/geo-douyin/batches` 在它自己的事务里建好了(规格 §8.2:
    #    claim → freeze → commit,provider 只能在 commit 之后开始)。
    #    传进来 = 本函数**不再** create_task、**不再** freeze_points,
    #    只消费既有句柄并在同一处 commit / release。
    #
    #    形状:{"task_id", "task_ref", "freeze_id", "status_ready", "status_failed"}
    #    None = 老链(自建任务 + 自冻结),**逐字保持原行为**。
    #    这是「Refactor Not Rewrite」:两条链共用同一套生成实现,
    #    差别只在"钱和任务行是谁建的",而那正是唯一真正不同的地方。
    contract_task: Optional[dict] = None,
    # 🔴 [WO_204 §1.4] 用户定好的选题标题。**空 = 现有行为逐字不变**(标题由 AI 出)。
    #    非空 = 标题**以它为准**,AI 出的那句只当没看见。
    #    Owner 09-13:「修改后就按照标题来进行创作」——
    #    用户改了标题却拿到一个 AI 自己想的标题,那就是"改了个寂寞"。
    topic_title: str = "",
) -> ProductionOutcome:
    """跑完一条图文帖的生产:文案 → 卡片图 → OSS → 落库。

    返回 ProductionOutcome;失败时已完成 release_freeze 退积分。
    """
    from db import geo_douyin_db as ddb
    from middleware.billing import commit_freeze, freeze_points, release_freeze
    from services.geo_douyin.card_templates import build_style_tokens, resolve_style
    from services.geo_douyin.content_generator import generate_image_post_content
    from services.geo_douyin.image_pipeline import (build_prompts_for_group,
                                                    render_prompt_group)

    want = max(CARD_COUNT_MIN, min(int(card_count or CARD_COUNT_DEFAULT), CARD_COUNT_MAX))
    task_ref = build_task_ref(post_id)
    outcome = ProductionOutcome(ok=False, post_id=post_id, cards_total=want)

    # ── 0. 先建任务行,再冻结 ──
    # 🔴 异步化之后顺序必须是这个。冻结失败(最常见:余额不够)如果没有任务行,
    #    后台协程就悄悄退出了,前端轮询查不到任何东西 → 永远转圈。
    #    任务行是"查得到"的那条凭据。
    # 🔴 计费语义一个字没动:freeze / commit / release 的调用点、顺序、条件
    #    与改造前逐字相同,挪的只是**记账行**的创建时机。
    if contract_task:
        # 合同链:任务行是入口那笔事务的产物,worker 只是接手 —— 不再新建。
        # 新建会撞 `uq_geo_douyin_task_active_generation`(同 post 只允许一个活跃任务),
        # 而且会造出一个**没有冻结句柄**的第二行,资金对账当场分叉。
        task_id = int(contract_task["task_id"])
        task_ref = str(contract_task["task_ref"])
    else:
        # [#184 d1] 普通链也要**开一代生成**:`activate_revision` 的 CAS 比的是
        #   `active_generation_task_id + generation_epoch`,而这两列只有
        #   `begin_generation` 写。不开代际 ⇒ 那两列是 NULL/0 ⇒ CAS 恒零行 ⇒
        #   每做完一篇都会被判成"迟到任务"而标作废。三步在同一个事务里。
        task_id, post_epoch = await asyncio.to_thread(
            ddb.create_task_with_generation, post_id=post_id, user_id=user_id,
            task_ref=task_ref, freeze_id=None, progress_total=want,
        )
    outcome.task_id = task_id

    # ── 1a. 算加张费(Owner 2026-08-03:套餐含 4 张,每多一张按价目表加价)──
    # 🔴 fail-closed 且**先于冻结**:价目读不到就不下单,绝不按 0 继续 ——
    #    按 0 继续 = 多做的卡白送,一个读库抖动就能变成资金漏。
    #    这里失败时还没有冻结,所以和 freeze 失败一样**不能**走 _fail()。
    from services.geo_douyin.pricing import PricingUnavailable, extra_card_points
    try:
        extra_cost = await extra_card_points(want)
    except PricingUnavailable as e:
        outcome.error = f"pricing_unavailable: {str(e)[:120]}"
        logger.warning("[douyin-task] 加张价目不可用 post=%s: %s", post_id, outcome.error)
        await asyncio.to_thread(
            ddb.update_task, task_id, status="failed", stage="pricing",
            error_msg=outcome.error, mark_finished=True,
        )
        await asyncio.to_thread(ddb.set_post_status, post_id, "failed")
        return outcome

    # ── 1. 冻结积分(B 类)──
    if contract_task:
        # 合同链:冻结已在入口那笔事务里完成,句柄整组落在 `geo_douyin_post_tasks`。
        # 🔴 这里**绝不能**再冻一次 —— 那会是同一件事扣两笔钱。
        #    `extra_cost` 也已经进过那一笔(P0-02 修的就是它当时没进)。
        #    也不写 `set_task_freeze`:那一列已经是入口写好的权威值。
        freeze_id = contract_task.get("freeze_id")
    else:
        try:
            # feature_code 由调用方决定:首次制作 vs 重新生成走不同档位
            # 🔴 extra_cost 是**既有参数**(middleware/billing.py:1343),
            #    `total_cost = pricing[cost_points] + extra_cost` —— 加张计价因此
            #    **零新增资金路径**:commit/release/退款/§15 部分成功全部原样跟着走。
            frozen = await freeze_points(
                user_id, feature_code,
                task_ref=task_ref, brand_id=brand_id,
                extra_cost=extra_cost,
                reason="抖音图文帖生产",
            )
        except Exception as e:  # noqa: BLE001
            outcome.error = f"freeze_failed: {str(e)[:140]}"
            logger.warning("[douyin-task] 冻结失败 post=%s: %s", post_id, outcome.error)
            # 冻结没成功 → 没有 freeze_id,**不能**走 _fail(那会去 release 一个不存在的冻结)。
            # 这里只落状态:任务失败 + 作品失败,让前端查得到、能给人话("算力不足")。
            await asyncio.to_thread(
                ddb.update_task, task_id, status="failed", stage="freeze",
                error_msg=outcome.error, mark_finished=True,
            )
            await asyncio.to_thread(ddb.set_post_status, post_id, "failed")
            return outcome

        freeze_id = frozen.get("freeze_id")
        await asyncio.to_thread(ddb.set_task_freeze, task_id, freeze_id)

    async def _fail(stage: str, err: str) -> ProductionOutcome:
        """统一失败出口:退积分 + 落顶层 error_msg + 作品置 failed。"""
        outcome.error = err
        released = False
        try:
            if freeze_id:
                # 🔴 [第 4 棒 · P0-B] ①整组句柄 ②**看返回值**。
                #    `release_freeze` 找不到冻结 / 跨表撞号歧义时返回
                #    `{"success": False}` 且**不抛** —— 只 try/except 的话
                #    这一格永远不会触发,于是"已退款"是句假话。
                funds = await release_freeze(
                    reason=f"抖音图文帖生产失败:{stage}",
                    **_settlement_handle(contract_task, freeze_id=freeze_id,
                                         task_ref=task_ref))
                released = bool((funds or {}).get("success"))
                outcome.refunded = released
                outcome.settlement_ok = released
                if not released:
                    logger.error(
                        "[douyin-task] release_freeze 未成功 task_ref=%s: %s",
                        task_ref, funds)
        except Exception as e:  # noqa: BLE001
            # 退款失败必须显式留痕(资金面),不吞
            outcome.settlement_ok = False
            logger.error("[douyin-task] release_freeze 失败 task_ref=%s: %s",
                         task_ref, e)
        if contract_task:
            # 🔴 合同链的 settlement_status 必须跟着走:release 成功 = `released`
            #    (前端文案"算力已退回"才是真话);release **失败** = `manual`,
            #    因为这时候钱既没退也没扣,只有人工能收敛。
            #    写成"失败一律 released"是本仓资金面最常见的谎:
            #    界面说退了、账上没退,对账时才发现。
            await asyncio.to_thread(
                _set_contract_settlement, task_id,
                "released" if released else "manual")
        await asyncio.to_thread(
            ddb.update_task, task_id, status="failed", stage=stage,
            error_msg=err, mark_finished=True,
        )
        await asyncio.to_thread(ddb.set_post_status, post_id, "failed")
        logger.warning("[douyin-task] post=%s 失败于 %s: %s", post_id, stage, err)
        return outcome

    try:
        await asyncio.to_thread(ddb.update_task, task_id, status="running",
                                stage="copy", mark_started=True)
        await asyncio.to_thread(ddb.set_post_status, post_id, "generating")

        # ── 2a. 榜单主链(只在 content_form == "ranking" 时进来)──
        #
        # 🔴 2026-08-06 返工:这是把 `decide_form` / `fetch_ranking_candidates` /
        #    `route_template` / `FrozenRankingPayload` / `rank_statement` 接进
        #    **付费链**的那一跳。此前这五个符号在运行时代码里**零调用方** ——
        #    200 条锁证明的是模块自洽,不是付费链成立。
        #
        # 编排失败或候选不足 → plan 为 None → 原样走卡组型,**不让整单失败**。
        #
        # 🔴 2026-08-06 二次返工:退回卡组型**不再是静默的**。
        #    `build_ranking_plan` 现在恒返 `RankingOutcome`,里面带
        #    requested_form / effective_form / fallback_reason —— 三者全部落
        #    `generation_meta.ranking`,前端据此告诉用户"你要的是榜单、
        #    这次给的是什么、为什么、怎么补"。用户点了榜单拿到卡组却没人说,
        #    那是静默换货。
        ranking_outcome = None
        ranking_plan = None
        ranking_findings: list = []
        if str(content_form or "").strip() == RANKING_FORM:
            from services.geo_douyin.ranking_router import build_ranking_plan
            ranking_outcome = await asyncio.to_thread(
                build_ranking_plan,
                industry_key=industry_key, keyword=keyword, city=city,
                # 🔴 下单前冻好的归并件。执行期不重算(见形参注释)。
                frozen_industry=frozen_industry,
                client_brand=brand_name,
                # 人工确认竞品要按 brand 取(P1-1),血缘再按 keyword 收窄
                client_brand_id=brand_id,
                want=int(ranking_entity_count or 0),
                # 张数预算:榜单每家占一张,封面与收尾各占一张。
                card_budget=want,
                force_template=str(ranking_template or "").strip(),
                # None = 用面×行业默认;True = 用户明示要榜单式。
                # 🔴 不写 `bool(ranking_force)` —— False 会**关掉**那些默认为正的行业,
                #    而"没勾"的语义是"不覆盖",不是"要求非榜单"。
                force_ranking=True if ranking_force else None,
            )
            ranking_plan = ranking_outcome.plan
            if ranking_outcome.degraded:
                logger.info("[douyin-task] post=%s 榜单降级 %s → %s(%s)",
                            post_id, ranking_outcome.requested_form,
                            ranking_outcome.effective_form,
                            ranking_outcome.fallback_reason)

        # ── 2. 文案 + 卡片要点 ──
        content = await generate_image_post_content(
            keyword, city=city or None, brand_name=brand_name,
            card_count=want, extra_hint=extra_hint,
            brand_id=brand_id,          # ← 引用客户自有资料 + 知识库
            # 🔴 行业决定首图钩子与 B端/C端 表达(2026-08-03 实测:同一个手法
            #    换个行业结论会反过来)。这条链原来**断在这里** ——
            #    industry_key 存进了 post,却没传进生成侧,于是全站走同一套话术。
            industry_key=industry_key,
            # 冻结名单与名次表述进 prompt:模型只能用名单里的名字,
            # 名次只能照抄 `rank_statement` 给的那一句(不许自己改写)。
            ranking_plan=ranking_plan,
        )
        if not content.ok:
            return await _fail("copy", content.error or "content_failed")

        # ── 2b. 榜单闸(§11.1 四级裁决:**全部 A1**,只留痕 + 给局部修复出口)──
        # 🔴 R1 比对的是 `content.cards` 的 **entity 结构化槽**,不是 caption 自由文本
        #    —— 从正文里正则猜公司名那条路已经被实测判死(四句普通话全误报)。
        if ranking_plan is not None:
            from services.geo_douyin.ranking_gates import run_gates
            caption = (content.title or "") + "\n" + (content.body or "")
            ranking_findings = run_gates(
                caption=caption,
                entity_slots=list(content.cards),
                allowed_names=ranking_plan.allowed_names(),
                client_brand=brand_name,
                actual_count=len(ranking_plan.payload.items),
                caveats=[str(c.get("caveat") or "") for c in content.cards],
                is_ranking_form=ranking_plan.routed["allows_ranking_wording"],
                items=[i.to_dict() for i in ranking_plan.payload.items],
                # 🔴 2026-08-08 A 层:R9 靠这一份判"卡面有没有整段搬运画像"。
                #    不接这一行,R9 拿到空 dict 会直接 return None —— 闸在、判据不在。
                entity_profiles=getattr(ranking_plan, "profiles", None),
            )

        # ── 3. 标题与标签:**由业务 AI 出**,不再走模板池 ──
        # 🔴 Owner 2026-08-04:「一定要让 AI 根据用户来蒸馏选题正文和标签,
        #    一定不能硬编码,LLM 出问题直接挂掉就行了」。
        #    模板池 `title_engine.build_title` 曾是主路径,后果是生产实测两连错:
        #      ① 无条件拼 `{city}{kw}` → 客户买的词自带城市 → 「深圳深圳AI搜索优化」;
        #      ② 模板是照家装样本写的 → AI 搜索优化的标题里出现「工期」。
        #    所以这里**不兜底**:AI 没给出标题/标签就整条失败退款,
        #    而不是退回一个会写错的模板。
        if not content.title or not content.hashtags:
            return await _fail("copy", "title_or_hashtags_missing")

        # 🔴 [WO_204 §1.4] 选题标题**优先**。上面那道判空仍按 AI 产物判 ——
        #    标题空通常意味着整条生成都坏了(正文/标签一起废),
        #    拿选题标题去盖一具坏产物,用户会拿到"标题对、正文不知所云"的稿子。
        #    所以:判空看 AI,落库看用户。
        _final_title = str(topic_title or "").strip() or content.title
        _title_source = "topic" if str(topic_title or "").strip() else "llm"

        await asyncio.to_thread(
            ddb.update_post_content, post_id,
            title=_final_title, body_text=content.body,
            hashtags=content.hashtags, cards=content.cards,
            generation_meta={
                "model": content.model,
                "ad_law_flags": content.ad_law_flags,
                # 榜单冻结件 + 降级留痕 + 闸结果(P1-4 要素 1:复用现役 jsonb,
                # 不占迁移号)。卡组型订单这两个键**根本不出现** —— 存量产出逐字不变。
                # 🔴 条件是 `ranking_outcome is not None`(用户点了榜单)而不是
                #    `ranking_plan is not None`(榜单做成了):做不成时正是最该
                #    留痕的时候 —— 那一份 meta 里装的就是"为什么给你的是卡组"。
                **({"ranking": ranking_outcome.to_meta(),
                    "ranking_gates": [f.to_dict() for f in ranking_findings]}
                   if ranking_outcome is not None else {}),
                # "llm" = AI 自己出的;"topic" = 用户定的选题标题(WO_204)。
                # 曾经还有 "template",留字段便于回溯。
                "title_source": _title_source,
                "skeleton": content.skeleton,
                # 留痕:这条内容用了客户哪些素材来源(便于复核"是不是真引用了知识库")
                "kb_sources": content.kb_sources,
                # 🔴 单卡重抽的底料。第 5 步的 update_post_assets 会把 cards 整列
                #    覆盖成"产物记录"(idx/kind/oss_key/status),原始的
                #    points/metric/caveat 就没了 —— 没有这份快照,重抽某一张
                #    内容卡时根本重建不出它的 prompt。
                "content": content.to_dict(),
                "variant_index": int(variant_index),
            },
        )

        # ── 4. 卡片图 → OSS(内存直传,零落盘)──
        await asyncio.to_thread(ddb.update_task, task_id, stage="images")
        # 组内风格参数:同一组卡传同一份(§6b M6),否则逐张独立生图出不来"像一组"
        # 🔴 2026-08-05:主色不再是 `seed % 5` 的随机撞色(那和客户没有任何关系)。
        #    Owner:「颜色随机没问题,但是要根据客户的素材或者行业来定」。
        #    取值顺序:业务 AI 按客户素材/行业给的 `visual` → 行业兜底表 → 通用兜底。
        style = build_style_tokens(keyword, city, industry_key=industry_key,
                                   visual=content.visual)
        # 四款风格之一。没有已授权实拍图 → 「实拍叠字」自动降级回文字卡
        # (resolve_style 内部做),绝不凭空造假实景图。
        #
        # 🔴 2026-08-04 返工。原判据是 `"brand_image_assets" in content.kb_sources`
        #    —— **粒度错了**:`sources_used` 在 `logo_hints or image_hints` 任一非空时
        #    就会置位(knowledge_context.py 那一行),于是**只有 LOGO、零实拍图**的客户
        #    信号照样为 True,实拍叠字不降级,模型凭空画一张假实景图挂到客户名下。
        #    交付单 §3.1 自己写的最坏结果,被 Review 当场攻破。
        #    生产量化:9 个有双闸资产的品牌里 5 个是 logo-only(56%)= 多数场景。
        #    现在用 `has_real_photo` 这个**只表达这件事**的专用信号(image_hints 非空)。
        preset = resolve_style(
            style_key,
            has_authorized_photo=bool(getattr(content, "has_real_photo", False)),
        )
        # max_cards=want:生成张数不得超过用户要的卡数(自审缺陷修复)
        specs = build_prompts_for_group(content, style, keyword=keyword,
                                        city=city, max_cards=want,
                                        preset=preset, contact_line=contact_line,
                                        # 规范 §8.8:整组同一个画幅
                                        aspect_ratio=aspect_ratio)
        outcome.cards_total = len(specs)   # 以实际张数为准,不再用 want 虚报
        # 真实张数可能比 want 少(张数预算截断)→ 进度分母要跟着改,
        # 否则进度条会停在 5/6 永远走不完。
        if len(specs) != want:
            await asyncio.to_thread(ddb.set_task_total, task_id, len(specs))

        async def _tick() -> None:
            """一张出完就 +1。

            🔴 必须自增**落库**而不是在内存里数:进度是给**另一个请求**
               (前端轮询)读的,内存里的数它读不到。
            🔴 必须 to_thread:psycopg2 是同步的,直接调用会阻塞事件循环 ——
               而这一刻正好有 7 张卡在并发等 HTTP,阻塞就是全组一起卡。
            """
            try:
                await asyncio.to_thread(ddb.bump_task_progress, task_id)
            except Exception as e:  # noqa: BLE001 - 记进度失败不该影响出图
                logger.warning("[douyin-task] 进度 +1 失败 task=%s: %s", task_id, e)

        # 🔴 硬超时走 wait_for 而**不是**在外层包 —— 这个区别是关键:
        #    wait_for 把 CancelledError 关在内层任务里,超时在**本函数**抛
        #    TimeoutError(Py3.11+ 是 Exception 子类),于是被下面那个 except 接住
        #    → 走统一的 _fail() → release_freeze 退款。
        #    如果反过来在调用方 wait_for 整个本函数,取消会以 CancelledError
        #    (BaseException)穿透,except Exception 接不住,**冻结就漏了**。
        batch = await asyncio.wait_for(
            render_prompt_group(post_id, specs, on_card_done=_tick,
                                aspect_ratio=aspect_ratio),
            timeout=TASK_TOTAL_TIMEOUT_SECONDS,
        )
        outcome.cards_done = batch.ok_count

        # ── 5. 落产物(先落,再判交付)──
        # 🔴 顺序变了,这是 §15 的关键:原来是"没全成就整条作废、什么都不落"。
        #    实测把它打穿了 —— 单卡 ~0.9 时五张联合只有 59%,生产 5 次 0 成品,
        #    两次都是 4/5。**把已经生成并付过费的 4 张丢掉**才是最贵的。
        def _card_row(i: int, spec: dict) -> dict:
            res = batch.cards[i] if i < len(batch.cards) else None
            return {
                "idx": i + 1,
                "kind": spec.get("kind", ""),
                "headline": spec.get("headline", ""),
                # 🔴 职责名落库:详情页缩略条按它标"封面/比较口径/成本结构…"。
                #    不落的话前端只剩 kind(封面/内容/内容/内容/收尾),
                #    四张"内容"在缩略条上分不出谁是谁。
                "role_label": spec.get("role_label", ""),
                # 🔴 2026-08-06 补:版式也要落库。不落的话**重抽单张会静默降级** ——
                #    build_content_prompt 收 layout_role,而重抽路径原本一个都不传,
                #    重抽出来的那张认不出版式、认不出「第 i/N」。榜单形态下这等于
                #    把「第 3/7 家」重抽成一张孤卡。(WO v3 §6.1-5)
                "layout_role": spec.get("layout_role", ""),
                "oss_key": (res.oss_key if (res and res.ok) else ""),
                "status": "ready" if (res and res.ok) else "failed",
                # 原样留下这张的 prompt:重抽时"留空 = 随机重抽"就是**原样再跑一次**
                # (生图本身有随机性),不需要再调一次 LLM 重写要点。
                "prompt": spec.get("prompt", ""),
            }

        enriched: List[dict] = [_card_row(i, s) for i, s in enumerate(specs)]

        async def _persist(rows: List[dict]) -> None:
            """落库。🔴 失败卡在 oss_keys 里留**空串占位**,不能删掉 ——
            预览 / 卡片元数据 / 重抽的 card_index 三者按同一下标对齐。"""
            keys = [str(r.get("oss_key") or "") for r in rows]
            cover = next((str(r.get("oss_key") or "") for r in rows
                          if str(r.get("kind") or "") == "cover"
                          and r.get("oss_key")), "")
            await asyncio.to_thread(
                ddb.update_post_assets, post_id,
                oss_keys=keys, cover_oss_key=cover or (keys[0] if keys else ""),
                cards=rows,
            )

        await _persist(enriched)

        if not batch.all_ok:
            # ── §15-1 成品门槛:封面成功 且 成功卡 ≥ 一半 ──
            if not is_deliverable(enriched):
                failed = [c.to_dict() for c in batch.cards if not c.ok]
                return await _fail(
                    "images",
                    f"卡片图 {batch.ok_count}/{len(batch.cards)} 成功;"
                    f"首个失败: {(failed[0].get('error') if failed else 'unknown')}",
                )

            # ── §15-2 达门槛 → 「补齐中」:冻结保持不动,自动免费补齐 ≤2 轮 ──
            # 🔴 走 render_one_card 而不是 redraw_one_card,两个理由现在都成立:
            #    ① 后者会占用户那 10 次额度;
            #    ② 2026-08-03 起后者那条链**收费 100 算力/次** —— 而这是
            #       **系统**在补自己没做完的活,让用户为此付钱是错的。
            #    §15 明确"不占用户额度",本包再加一条"也不花用户的钱"。
            enriched = await _auto_fill(post_id, specs, enriched, task_id)

        if pending_indices(enriched):
            # 自动补齐没补全 → 交还用户手动重抽(那一次按 100 算力单独计费,
            # 由 API 层 freeze/commit,与这笔整组冻结是两笔);
            # 这笔整组冻结**既不 commit 也不 release**,
            # 12h 平台既有 sweeper 死线兜底全退(零新增资金路径)。
            await _persist(enriched)
            done_n = len(enriched) - len(pending_indices(enriched))
            await asyncio.to_thread(ddb.set_task_completing, task_id,
                                    progress_done=done_n)
            await asyncio.to_thread(ddb.set_post_status, post_id, STATUS_COMPLETING)
            outcome.cards_done = done_n
            outcome.ok = True          # 有成品交付了,不是失败
            outcome.completing = True
            logger.info("[douyin-task] post=%s 部分成功交付,%s 张待补齐",
                        post_id, len(pending_indices(enriched)))
            return outcome

        await _persist(enriched)
        oss_keys = [str(r.get("oss_key") or "") for r in enriched]
        # 记下这条实际用的是哪款风格(可能因缺授权实拍图被降级过)——
        # 详情页顶部「当前风格」显示的必须是**真实生效的那款**,不是用户点的那款。
        await asyncio.to_thread(ddb.set_style_key, post_id, preset.key)
        # 收尾卡刚按当前开关重出过,不再是"待重抽"
        await asyncio.to_thread(ddb.clear_closing_stale, post_id)

        # ── 5b. [#184 d1] 冻成 active revision(**普通链**;合同链在 worker 那边冻)──
        # 🔴 位置刻意在 `_persist` 之后、结算 commit 之前:
        #    · 在 persist 之后 —— 版本快照必须取"库里刚落好的那一份",
        #      从内存再造一份会出现"库是 A、发的是 B"(发布链按 revision 发);
        #    · 在 commit 之前 —— CAS 输了(这一篇已被新一代生成接管)时,
        #      这笔钱该 **release** 而不是 commit,ready 也只能由赢家写。
        if not contract_task:
            _revision_id = await asyncio.to_thread(
                _freeze_ordinary_revision, post_id, user_id, task_id, post_epoch)
            if _revision_id is None:
                # 输家:`freeze_active_revision` 里已把本任务标 superseded。
                # 这里**不**走 `_fail`——那会把作品置 failed,而作品此刻正被
                # 新一代生成做着(状态是 generating),置 failed 是在说假话。
                logger.info("[douyin-task] post=%s task=%s 生成已易主,本次结果作废并退款",
                            post_id, task_id)
                try:
                    if freeze_id:
                        funds = await release_freeze(
                            reason="抖音图文帖生成已被新一代接管(superseded)",
                            **_settlement_handle(contract_task, freeze_id=freeze_id,
                                                 task_ref=task_ref))
                        outcome.refunded = bool((funds or {}).get("success"))
                        outcome.settlement_ok = outcome.refunded
                        if not outcome.refunded:
                            logger.error("[douyin-task] superseded 退款未成功 task_ref=%s: %s",
                                         task_ref, funds)
                except Exception as _rel_err:  # noqa: BLE001
                    outcome.settlement_ok = False
                    logger.error("[douyin-task] superseded 退款失败 task_ref=%s: %s",
                                 task_ref, _rel_err)
                outcome.superseded = True
                return outcome

        # ── 6. 成功才扣 ──
        # 🔴 合同链在 commit **之前**先落 `settlement_pending`(规格 §5.7 那一格):
        #    它是"东西做完了、钱还没结"。少了这一格,commit 中途崩溃时
        #    库里只有"还在跑",恢复逻辑分不清"没做完"与"做完没结账" ——
        #    前者该重跑(重复外调),后者该补结算。两个处置相反。
        if contract_task:
            await asyncio.to_thread(_set_contract_settlement, task_id,
                                    "settlement_pending")
        _committed = await commit_contract_settlement(
            contract_task, freeze_id=freeze_id, task_ref=task_ref, task_id=task_id)
        outcome.settlement_ok = _committed
        await asyncio.to_thread(
            ddb.update_task, task_id,
            status=((contract_task.get("status_ready") if _committed
                     else CONTRACT_STATUS_NEEDS_ACTION)
                    if contract_task else "succeeded"),
            stage="done",
            progress_done=batch.ok_count, mark_finished=True,
            result_meta={"cost_usd": batch.total_cost_usd,
                         "oss_keys": oss_keys},
        )
        await asyncio.to_thread(ddb.set_post_status, post_id, "ready")

        outcome.ok = True
        return outcome

    except Exception as e:  # noqa: BLE001 - 兜底:任何未预期异常也必须退积分
        return await _fail("unexpected", f"{type(e).__name__}: {str(e)[:140]}")


# ─────────────────────────────────────────────────────────────
# 后台调度(异步化)
# ─────────────────────────────────────────────────────────────
# 🔴 fire-and-forget 的 asyncio.Task 必须有**强引用**,否则事件循环可能把它
#    当垃圾回收掉 —— 表现是"任务偶尔跑一半没了",极难复现。
#    形态照抄当时仓内的 C 端 GEO 方案 worker 的 _RUNNING_WORKERS(该 worker 已随开源 E3 删除),
#    不自创第二套写法。
_RUNNING_PRODUCTIONS: set = set()


def running_production_count() -> int:
    """当前进程里在跑的图文生产任务数(给自检/排查用)。"""
    return len(_RUNNING_PRODUCTIONS)


def _freeze_ordinary_revision(post_id: int, user_id: int, task_id: int,
                              post_epoch: Optional[int]):
    """[#184 d1] 普通链:把刚落库的成品冻成 active revision。返回 revision_id 或 None。

    None = **CAS 输了**(这一篇已被新一代生成接管)。`freeze_active_revision`
    内部已经把本任务标 superseded,调用方只需善后(退款、不落 ready)。

    🔴 复用 `post_revisions.freeze_active_revision` —— 合同链用的是**同一个函数**
       (`contract_worker._freeze_active_revision` 现在只是它的别名)。
       复制一份的代价是可预期的:将来谁给其中一份多冻一个字段,另一份不会跟。
    """
    from db.connection import get_connection
    from services.geo_douyin.post_revisions import freeze_active_revision

    if post_epoch is None:
        # 没有代际就没有可比的 CAS —— 这只会发生在合同链(它自己冻),
        # 走到这里说明接线错了,宁可吵也不要静默跳过。
        raise RuntimeError("post_epoch 缺失:普通链必须先 begin_generation")
    conn = get_connection()
    try:
        cur = conn.cursor()
        revision_id = freeze_active_revision(cur, {
            "post_id": int(post_id),
            "actor_user_id": int(user_id or 0),
            "task_id": int(task_id),
            "post_epoch": int(post_epoch),
            "topic_snapshot": None,
        })
        conn.commit()
        return revision_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def dispatch_production(**kwargs) -> "asyncio.Task":
    """把一次生产丢到后台跑,立刻返回。

    为什么必须这样:生产 nginx 对 /api/geo-douyin/* 是 60s 超时,
    而一组卡实测 ≈137s —— 同步等于**必然 504**。用户看到"请求失败",
    后台却还在跑、积分还冻着,这是最糟的一种失败。

    🔴 这里**不碰任何计费**:冻结/扣费/退款全在 run_image_post_production 里,
       与同步调用时逐字相同。本函数只负责"在哪跑"。

    🔴 [WO_204 §1.4] `topic_id` 在**这里**摘下来,不进 `run_image_post_production`:
       那只函数有两个成功出口、六七个失败出口,逐个挂钩必漏一个,
       而漏掉的那个只表现为"一条选题永远卡在制作中"。
       挂在这层外壳上只有一处,且覆盖包括"它自己兜不住的异常"在内的**全部**出口。
       (标题 `topic_title` 仍然要进去 —— 它是生成的输入,不是状态机的事。)
    """
    topic_id = kwargs.pop("topic_id", None)

    async def _settle_topic(ok: bool) -> None:
        if not topic_id:
            return
        try:
            from db import geo_douyin_db as ddb
            if ok:
                await asyncio.to_thread(ddb.finish_topic,
                                        topic_id=int(topic_id), post_id=int(kwargs["post_id"]))
            else:
                await asyncio.to_thread(ddb.release_topic, topic_id=int(topic_id))
        except Exception as e:  # noqa: BLE001
            # 🔴 不吞:选题卡在 making = 用户再也点不动这一条,而列表上看不出原因。
            #    (这里只能记日志 —— 已经在后台任务里,没有别的出口。)
            logger.error("[douyin-task] 选题状态收尾失败 topic=%s ok=%s: %s: %s",
                         topic_id, ok, type(e).__name__, e)

    async def _runner():
        try:
            outcome = await run_image_post_production(**kwargs)
            await _settle_topic(bool(getattr(outcome, "ok", False)))
            return outcome
        except Exception as e:  # noqa: BLE001
            # run_image_post_production 自己有兜底 except,走到这里说明是它兜不住的
            # (比如落库时连不上 DB)。至少把作品置失败,别让前端永远转圈。
            post_id = kwargs.get("post_id")
            logger.error("[douyin-task] 后台任务异常 post=%s: %s: %s",
                         post_id, type(e).__name__, e)
            try:
                from db import geo_douyin_db as ddb
                await asyncio.to_thread(ddb.set_post_status, int(post_id), "failed")
            except Exception:  # noqa: BLE001
                pass
            await _settle_topic(False)
            return None

    task = asyncio.create_task(_runner())
    _RUNNING_PRODUCTIONS.add(task)
    task.add_done_callback(_RUNNING_PRODUCTIONS.discard)
    return task
