"""GEO 抖音图文 · 单张卡片重抽(每条内容累计限额 · 只替换该张)

工单原文三条硬要求,逐条落点:

  1. **本模块零计费** —— 不 import `middleware.billing`,一行扣费代码都没有。
     反向锁 tests/test_geo_douyin_redraw.py::test_redraw_is_free 用 AST 断言这件事。
     🔴 2026-08-03 起用户手动重抽**收费 100 算力**(Owner 拍板),但扣费在
        `api/geo_douyin_api.py` 那一层做,本模块只收一个 freeze_id 记账 +
        在成功/失败时回调。这条 AST 锁**照旧成立且必须成立**:
        资金动作集中在一处才审得动,散进 worker 模块就没人能审。
     🔴 另一条不能混的语义:**系统**在 §15 部分成功里的自动补齐走
        `image_pipeline.render_one_card`,**根本不经过本模块**,
        所以它既不收费也不占那 10 次额度 —— 那是系统在补自己没做完的活。

  2. **每条内容累计限 10 次,计数落库,后端强制** ——
     额度在 `geo_douyin_posts.redraw_count`,占额走
     `bump_redraw_count()` 的**条件 UPDATE**(判额度与加计数在同一条 SQL 里),
     不是"先查再加"。前端那个 "已用 N/10" 只是显示,拦截在后端。

  3. **只替换该张,其他成功卡不动** —— 落库走 `replace_one_card()` 的 jsonb_set
     定点替换,不整份重写 oss_keys。

🔴 额度是「成功才算用掉」:生图失败会 `refund_redraw_count()` 退回一次。
   失败还扣额度 = 用户白丢一次机会,而失败往往还是我们这边的问题。

🔴 本模块不生成新文案(不调 LLM):重抽换的是**画面**,不是内容。
   用户填了"想怎么改"就把它作为额外作画要求追加进 prompt;留空 = 原样再跑一次
   (生图本身有随机性,同一条 prompt 每次出图不同)—— 这正是工单说的"随机重抽"。
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger("GEO-Douyin-Redraw")

# 每条内容累计重抽上限。改成收费(100 算力/次)之后**仍然保留** ——
# 收费解决的是成本转嫁,限额解决的是单条内容被反复重画到失控。两件事,都要。
REDRAW_LIMIT_PER_POST = 10

# 用户填的"想怎么改"进 prompt 的字数上限(防把整段小作文塞进去)
_HINT_MAX = 120


@dataclass
class RedrawResult:
    ok: bool
    error: str = ""
    card_index: int = -1
    oss_key: str = ""
    redraw_used: int = 0
    limit_reached: bool = False
    # [#184 d1b] 这一版已被**新一代生成**顶掉:不是失败(图做出来了),
    #   也不能算成功(这张图属于一个已经不存在的版本)。调用方据此 release 冻结。
    superseded: bool = False

    def to_dict(self) -> dict:
        return {"ok": self.ok, "error": self.error, "card_index": self.card_index,
                "oss_key": self.oss_key, "redraw_used": self.redraw_used,
                "redraw_limit": REDRAW_LIMIT_PER_POST,
                "limit_reached": self.limit_reached,
                "superseded": self.superseded}


def _content_card_ordinal(cards_meta: List[dict], card_index: int) -> int:
    """这张内容卡是第几张内容卡(0 基)。

    组的形状是 封面 + 内容卡×N + 收尾,但封面/收尾都可能因张数预算被砍掉,
    所以**不能**用 `card_index - 1` 这种位置推断,必须按 kind 数前面有几张内容卡。
    """
    return sum(1 for c in cards_meta[:card_index]
               if str(c.get("kind") or "") == "content")


def build_redraw_prompt(post: dict, card_index: int, *, hint: str = "",
                        contact_line: str = "") -> str:
    """重建【这一张】的 prompt。

    优先从 generation_meta.content 快照重建 —— 这样风格切换、联系方式开关
    这类**后来才改的设置**才会真的生效;快照缺失时退回落库时存的原 prompt。
    """
    from services.geo_douyin.card_templates import (build_closing_prompt,
                                                    build_content_prompt,
                                                    build_cover_prompt,
                                                    build_style_tokens,
                                                    resolve_style)

    cards_meta = list(post.get("cards") or [])
    if card_index < 0 or card_index >= len(cards_meta):
        return ""
    spec = cards_meta[card_index] or {}
    kind = str(spec.get("kind") or "")

    meta = post.get("generation_meta") or {}
    snapshot = meta.get("content") if isinstance(meta.get("content"), dict) else {}
    keyword = str(post.get("keyword") or "")
    city = str(post.get("city") or "")

    # 🔴 2026-08-05:视觉身份从**这条内容当初冻结的那一份**重建,不再按 seed 重算。
    #    快照里的 `visual` 是业务 AI 当时按客户素材/行业定的那一套 ——
    #    不从快照读的话,重抽出来的那一张会换配色/换场景,
    #    组内一致性正好破在"用户看着不顺眼所以重抽"的那一张上。
    #    快照缺失(老数据)→ 退回按行业兜底,仍然不随机。
    style = build_style_tokens(
        keyword, city,
        industry_key=str(post.get("industry_key") or ""),
        visual=snapshot.get("visual") if isinstance(snapshot.get("visual"), dict) else None,
    )
    # 重抽时不重新查授权图:风格已在生产时定过并落库(可能已降级过),
    # 这里照那个**真实生效的**风格走,不给用户一个"重抽后风格自己变了"的意外。
    preset = resolve_style(str(post.get("style_key") or ""),
                           has_authorized_photo=True)
    # 🔴 画幅必须跟这条内容原来的走(规范 §8.8)。漏传 = 重抽出来的那一张
    #    画幅和其余几张不一样,而这种错只有肉眼能发现、发现时图已经付过费了。
    aspect_ratio = str(post.get("aspect_ratio") or "")

    prompt = ""
    if snapshot:
        if kind == "cover":
            cover = snapshot.get("cover") or {}
            prompt = build_cover_prompt(
                str(cover.get("title") or keyword),
                str(cover.get("subtitle") or ""), style, preset,
                aspect_ratio=aspect_ratio)
        elif kind == "closing":
            closing = snapshot.get("closing") or {}
            prompt = build_closing_prompt(
                str(closing.get("headline") or "选购总结"),
                str(closing.get("summary") or ""), style, preset,
                contact_line=contact_line, aspect_ratio=aspect_ratio)
        elif kind == "content":
            snap_cards = list(snapshot.get("cards") or [])
            ordinal = _content_card_ordinal(cards_meta, card_index)
            if 0 <= ordinal < len(snap_cards):
                c = snap_cards[ordinal] or {}
                # 🔴 2026-08-06:版式/职责/组内进度必须跟着重抽走。
                #    原来这四个参数一个都不传 —— build_content_prompt 收它们,
                #    于是重抽出来的那张**静默丢掉版式与「第 i/N」标识**。
                #    榜单形态下 = 把「第 3/7 家」重抽成一张认不出序号的孤卡,
                #    而这种错只有肉眼能发现、发现时图已经付过费了。
                #    layout_role 老数据没落库 → 按张数计划回推,不留空。
                total_cards = len(cards_meta)
                layout_role = str(spec.get("layout_role") or "")
                if not layout_role:
                    from services.geo_douyin.series_plan import plan_roles
                    plan = plan_roles(total_cards)
                    if 0 <= card_index < len(plan):
                        layout_role = str(plan[card_index].get("layout_role") or "")
                prompt = build_content_prompt(
                    ordinal + 1,
                    str(c.get("entity") or c.get("headline") or ""),
                    list(c.get("points") or []), style,
                    metric=str(c.get("metric") or ""),
                    caveat=str(c.get("caveat") or ""),
                    preset=preset, aspect_ratio=aspect_ratio,
                    card_index=card_index + 1, total=total_cards,
                    layout_role=layout_role,
                    role_label=str(spec.get("role_label") or ""))

    if not prompt:
        prompt = str(spec.get("prompt") or "")
    if not prompt:
        return ""

    h = (hint or "").strip()[:_HINT_MAX]
    if h:
        # 追加而不是替换:用户说的是"在这张的基础上改什么",不是"重写这张"。
        prompt = f"{prompt}\n【额外要求】{h}"
    return prompt


def _sync_active_revision_card(post_id: int, base_revision_id, card_index: int,
                               new_oss_key: str, card_patch: dict):
    """[#184 d1b] 把这张新图同步进**锁定版本**。返回 True / False / None。

    None = 这条作品没有 active revision(d1 之前的存量、或合同链自己管的那些)
           ⇒ 与改前逐字同行为,什么都不做。
    False = 版本已被**新一代生成**顶掉(`replace_card_in_active_revision` 的
           `AND status='active'` CAS 零行)⇒ 调用方 release 这笔冻结,
           **不把旧图插回新版**(那只函数的 docstring 写死了这条口径)。

    🔴 为什么必须做:d1 之后 ready 作品有了锁定版本,而发布链读的是**版本**
       (`publish_batch_core` 按 revision/artifact 取内容),不是 `posts.oss_keys`。
       只改 post 不改版本 ⇒ **重抽完发出去的还是旧图**,而且页面上看是新的。
       这是 d1 带出来的不一致,不是重抽本来的毛病。

    🔴 manifest 要跟 `replace_one_card` 的口径**逐条对齐**:它在 `card_index == 0`
       时连 `cover_oss_key` 一起换(见其 SQL 的 CASE),这里也必须换,
       否则版本里的封面会指向一张已经被替换掉的图。
    """
    from db.connection import get_connection
    from services.geo_douyin.post_revisions import (
        compute_manifest_hash, replace_card_in_active_revision,
    )

    # 🔴 参照物是**用户点重抽时看的那一版**(`base_revision_id`,由调用方从
    #    post 快照带进来),**不是**"现在的 active"。
    #    重新读一次当前指针是错的:新一代生成接管后指针已经指向新版本,
    #    CAS 于是必然命中 ⇒ 把旧图写进新版 —— 那正是这条 CAS 要挡的事。
    #    (这个洞是判据 test_d1b_superseded_… 抓出来的,不是推出来的。)
    revision_id = base_revision_id
    if not revision_id:
        return None
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT asset_manifest FROM geo_douyin_post_revisions "
                    "WHERE post_revision_id=%s AND status='active'", (int(revision_id),))
        rev = cur.fetchone()
        if rev is None:
            # 指针还指着它、但它已不是 active ⇒ 与 CAS 零行同义
            return False
        manifest = dict(dict(rev).get("asset_manifest") or {})
        oss_keys = list(manifest.get("oss_keys") or [])
        if card_index < 0 or card_index >= len(oss_keys):
            # 版本里的资产数与这次重抽的下标对不上 —— 宁可不写也不写错一张
            logger.error("[douyin-redraw] post=%s 版本资产数 %d 容不下 card=%s,跳过版本同步",
                         post_id, len(oss_keys), card_index)
            return False
        oss_keys[int(card_index)] = str(new_oss_key)
        manifest["oss_keys"] = oss_keys
        if int(card_index) == 0:
            manifest["cover_oss_key"] = str(new_oss_key)
        ok = replace_card_in_active_revision(
            cur, post_revision_id=int(revision_id), card_index=int(card_index),
            card=dict(card_patch or {}), asset_manifest=manifest)
        conn.commit()
        _ = compute_manifest_hash  # 由 replace_card_in_active_revision 内部重算
        return bool(ok)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def redraw_one_card(post: dict, card_index: int, *,
                          hint: str = "") -> RedrawResult:
    """重抽一张卡。返回 RedrawResult;失败已把额度退回。"""
    from db import geo_douyin_db as ddb
    from services.geo_douyin.image_pipeline import render_one_card

    import asyncio

    post_id = int(post.get("id") or 0)
    cards_meta = list(post.get("cards") or [])
    oss_keys = list(post.get("oss_keys") or [])

    if card_index < 0 or card_index >= len(oss_keys):
        return RedrawResult(ok=False, error="这张卡不存在", card_index=card_index)

    contact_line = ""
    if post.get("contact_enabled"):
        contact_line = str((post.get("generation_meta") or {}).get("contact_line") or "")

    prompt = build_redraw_prompt(post, card_index, hint=hint,
                                 contact_line=contact_line)
    if not prompt:
        return RedrawResult(ok=False, error="这张卡缺少可重做的底稿，试试整条重新创作",
                            card_index=card_index)

    # ── 先占额度(条件 UPDATE,并发安全)──
    used = await asyncio.to_thread(ddb.bump_redraw_count, post_id,
                                   REDRAW_LIMIT_PER_POST)
    if used is None:
        return RedrawResult(
            ok=False, card_index=card_index, limit_reached=True,
            redraw_used=REDRAW_LIMIT_PER_POST,
            error=f"这条内容的重做已经用满 {REDRAW_LIMIT_PER_POST} 次了，"
                  f"想继续调整可以整条重新创作")

    headline = str((cards_meta[card_index] if card_index < len(cards_meta) else {})
                   .get("headline") or "")
    res = await render_one_card(post_id, card_index + 1, headline,
                                prompt_override=prompt,
                                # 🔴 与组内其余卡同画幅(§8.8)
                                aspect_ratio=str(post.get("aspect_ratio") or ""))
    if not res.ok:
        # 没出图 → 额度退回(成功才算用掉)
        await asyncio.to_thread(ddb.refund_redraw_count, post_id)
        logger.warning("[douyin-redraw] post=%s card=%s 生图失败: %s",
                       post_id, card_index, res.error)
        return RedrawResult(ok=False, card_index=card_index,
                            redraw_used=max(0, used - 1),
                            error="这张没做出来，没有消耗次数，稍后再试")

    patch = dict(cards_meta[card_index]) if card_index < len(cards_meta) else {}
    patch.update({"oss_key": res.oss_key, "status": "ready", "prompt": prompt})

    # [#184 d1b] **先**同步锁定版本,再改 post。
    #   顺序不是风格:版本那一步带 CAS(`AND status='active'`)。它输了说明
    #   这一篇已被新一代生成接管 —— 此时**连 post 都不该碰**,否则就是把旧图
    #   写进新一代刚做好的那组卡里(而那正是 `replace_card_in_active_revision`
    #   的 docstring 说的"不把旧图插回新版")。
    synced = await asyncio.to_thread(
        _sync_active_revision_card, post_id, post.get("active_revision_id"),
        card_index, res.oss_key, patch)
    if synced is False:
        await asyncio.to_thread(ddb.refund_redraw_count, post_id)
        logger.info("[douyin-redraw] post=%s card=%s 的版本已被新一代顶掉,本次作废并退款",
                    post_id, card_index)
        return RedrawResult(ok=False, card_index=card_index, superseded=True,
                            redraw_used=max(0, used - 1),
                            error="这条内容刚被重新创作过，这次重做的图已经不适用了，"
                                  "没有消耗次数，请在新版本上重试")

    replaced = await asyncio.to_thread(
        ddb.replace_one_card, post_id, card_index,
        oss_key=res.oss_key, card_patch=patch)
    if not replaced:
        await asyncio.to_thread(ddb.refund_redraw_count, post_id)
        return RedrawResult(ok=False, card_index=card_index,
                            redraw_used=max(0, used - 1),
                            error="这张没保存成功，没有消耗次数，稍后再试")

    # 重抽的正好是收尾卡 → 它已按当前联系方式开关重出过,清掉"待重抽"
    if str((cards_meta[card_index] or {}).get("kind") or "") == "closing":
        await asyncio.to_thread(ddb.clear_closing_stale, post_id)

    return RedrawResult(ok=True, card_index=card_index, oss_key=res.oss_key,
                        redraw_used=used)


# ─────────────────────────────────────────────────────────────
# 后台调度(异步化)
# ─────────────────────────────────────────────────────────────
# 🔴 重抽也必须异步:单张实测 49-73s,而生产 nginx 对 /api/geo-douyin/* 是
#    **60s** 超时 —— 同步跑有相当一部分会 504,而那时图其实已经生成了、
#    额度也已经占掉了。用户看到"失败"、额度却少一次,是最难解释的一种。
_RUNNING_REDRAWS: set = set()


def running_redraw_count() -> int:
    return len(_RUNNING_REDRAWS)


async def dispatch_redraw(post: dict, card_index: int, *, hint: str = "",
                          user_id: int = 0, freeze_id=None,
                          on_settled=None, on_failed=None):
    """把一次重抽丢到后台跑,立刻返回 (task_id, asyncio.Task)。

    复用**同一张任务表**记进度,前端因此只有一套轮询机制,不是两套。

    freeze_id: 只是**记账**用(落进任务行,让人能从任务反查到那笔冻结)。
        🔴 2026-08-03 起重抽收费(Owner 拍板 100 算力),所以这个值不再恒 NULL。
           但本模块**仍然一行计费代码都没有** —— 冻结在 API 层做,
           本模块只是把它写进任务行,并在成功/失败时喊一声。
           (`test_redraw_is_free_no_billing_import` 那把 AST 锁照旧成立,
            且必须照旧成立:计费集中在一处才审得动。)

    on_settled: 重抽**成功**后回调一次(无入参,可为协程)。
        🔴 两件事都挂在这里:提交这次重抽的冻结 + §15「补齐全组才 commit」
           的整组结算(用户手动补齐最后一张时把原来那笔生产冻结结算掉)。
    on_failed: 重抽**失败/异常**后回调一次。存在的唯一理由是**退掉这次冻结** ——
        没有它,失败一次就漏一笔冻结,只能等 12h sweeper 兜底。

    🔴 是 async 的原因只有一个:建任务行那两次 psycopg2 往返是**同步**的,
       直接在事件循环里调就是阻塞红线,必须 to_thread。
    """
    import asyncio

    from db import geo_douyin_db as ddb
    from services.geo_douyin.production_task import build_task_ref

    post_id = int(post.get("id") or 0)
    # [#184 d1b] 任务行走**同一道门**:唯一索引 `uq_geo_douyin_task_active_generation`
    #   管的是这张表上所有在途任务,不分种类 —— 重抽任务照样占那一格。
    #   走旧 `create_task` 的话,只要 post 上还有孤儿 pending,重抽就永久 UniqueViolation。
    #   `begin_generation` 的 epoch +1 只影响**任务**簿记;active revision 由
    #   `replace_card_in_active_revision` 按 `status='active'` 认,不看 epoch,
    #   所以「同一代际内换一张卡」这条语义不受影响。
    task_id, _epoch = await asyncio.to_thread(
        ddb.create_task_with_generation,
        post_id=post_id, user_id=int(user_id or post.get("created_by") or 0),
        task_ref=f"redraw:{build_task_ref(post_id)}",
        freeze_id=freeze_id, progress_total=1,
    )
    await asyncio.to_thread(ddb.update_task, task_id, status="running",
                            stage="redraw", mark_started=True)

    async def _notify(cb) -> None:
        """回调失败绝不能改变重抽本身的结局(图已经换好了/确实没换成)。"""
        if cb is None:
            return
        try:
            r = cb()
            if inspect.isawaitable(r):
                await r
        except Exception as e:  # noqa: BLE001
            logger.error("[douyin-redraw] 回调失败 post=%s: %s", post_id, e)

    async def _runner():
        try:
            result = await redraw_one_card(post, card_index, hint=hint)
        except Exception as e:  # noqa: BLE001
            logger.error("[douyin-redraw] 后台重抽异常 post=%s card=%s: %s",
                         post_id, card_index, e)
            await asyncio.to_thread(
                ddb.update_task, task_id, status="failed", stage="redraw",
                error_msg=f"{type(e).__name__}: {str(e)[:140]}", mark_finished=True)
            # 🔴 异常分支**也**要退款。漏在这里 = 每一次后台异常漏一笔冻结,
            #    而异常恰恰是最容易被漏测的那条路径。
            await _notify(on_failed)
            return None
        await asyncio.to_thread(
            ddb.update_task, task_id,
            status="succeeded" if result.ok else "failed",
            stage="redraw",
            progress_done=1 if result.ok else 0,
            # 额度用满这类"业务性拒绝"也要落 error_msg,否则前端只看到 failed
            # 却说不出为什么。
            error_msg="" if result.ok else (result.error or "redraw_failed"),
            mark_finished=True,
            result_meta={"card_index": result.card_index,
                         "redraw_used": result.redraw_used,
                         "limit_reached": result.limit_reached,
                         # [#184 d1b] 「被新一代顶掉」与「真失败」在任务行上要分得开:
                         #   前者不是我们做砸了,重试的正确做法是去新版本上重抽。
                         "superseded": result.superseded},
        )
        # 🔴 结算/退款失败都不能改变"重抽成功还是失败"这个结局:
        #    图已经换好了,没结算成只是钱还冻着,12h sweeper 会兜底。
        await _notify(on_settled if result.ok else on_failed)
        return result

    task = asyncio.create_task(_runner())
    _RUNNING_REDRAWS.add(task)
    task.add_done_callback(_RUNNING_REDRAWS.discard)
    return task_id, task
