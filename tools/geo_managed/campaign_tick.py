"""
v3.3 套餐定时任务引擎 — 每 6 小时跑一次

职责（按顺序）：
  1. 套餐到期/扣完检查 → status='depleted'
  2. 余额 < 20% 一次性提醒
  3. 屏蔽词检测（连续 7 天 0 检出 → 暂停）
  4. 监测频次控制（按 check_frequency_per_day 决定是否真的查）
  5. 排名/检出率检查（消耗 ¥0.29）
  6. 是否需要补文（按目标 SOV 判断）
  7. 模式分流：semi_auto → 进 pending_review；full_auto → 直接发布
  8. 失败处理：写作扣（已交付）+ 发布失败退（未交付）

辅助任务（同一调度器注册）：
  - process_due_pending_reviews: 24h 到期未审 → 自动发布
  - dormancy_scan: 12 个月无操作 → 转赠送积分
"""

import logging
import asyncio
from datetime import datetime

logger = logging.getLogger("GEO-Managed-Tick")


# ============================================================
# 主任务: managed_campaign_tick
# ============================================================

def managed_campaign_tick():
    """同步入口（供 APScheduler 调用），内部跑 asyncio 循环"""
    try:
        asyncio.run(_run_campaign_tick_async())
    except Exception as e:
        logger.exception(f"managed_campaign_tick 失败: {e}")


async def _run_campaign_tick_async():
    """异步主流程"""
    from db.managed_campaign_db import (
        get_active_campaigns_for_tick,
        consume_balance, update_status,
        increment_zero_detection, reset_zero_detection,
        mark_low_balance_warned, log_action,
    )

    campaigns = get_active_campaigns_for_tick()
    logger.info(f"[Tick] 取到 {len(campaigns)} 个活跃套餐")

    for c in campaigns:
        cid = c["id"]
        try:
            await _process_one_campaign(c)
        except Exception as e:
            logger.exception(f"[Tick] 套餐 {cid} 处理异常: {e}")
            log_action(cid, "tick_error", reason=str(e), result="failed")


async def _process_one_campaign(c: dict):
    """单个套餐处理"""
    from db.managed_campaign_db import (
        consume_balance, update_status, get_balance,
        increment_zero_detection, reset_zero_detection,
        mark_low_balance_warned, log_action,
        create_pending_review, claim_campaign_tick,
    )

    cid = c["id"]
    user_id = c["user_id"]

    # === 0. [蓝绿双跑幂等] CAS 占用本轮 tick ===
    # 锁层非唯一防线:redis scheduler 锁兜单实例,这里数据层 CAS 兜锁失效/蓝绿切换窗口双跑双扣双发。
    # claim 失败(近 60min 已被另一实例/重跑处理)→ 跳过整轮(监测/写作/发布全不重复)。
    if not claim_campaign_tick(cid):
        logger.info(f"[Tick] 套餐 {cid} 本轮已被并发占用·跳过(幂等)")
        return

    # === 1. 余额检查 (扣完 → depleted) ===
    balance = get_balance(c)
    if balance <= 0:
        update_status(cid, "depleted", reason="balance_exhausted")
        log_action(cid, "depleted", reason="余额扣完", result="success")
        await _notify_user(user_id, f'"{c["keyword"]}" 套餐余额扣完，AI 已自动暂停。'
                           f'想继续优化告诉 AI 加充即可。')
        return

    # === 2. 余额 < 20% 提醒（一次性）===
    total_recharged = float(c.get("total_recharged_yuan", 0))
    if (total_recharged > 0
        and balance / total_recharged < 0.20
        and not c.get("low_balance_warned", False)):
        mark_low_balance_warned(cid)
        await _notify_user(
            user_id,
            f'"{c["keyword"]}" 套餐余额剩 {int(balance / total_recharged * 100)}%，'
            f'建议加充避免中断'
        )
        log_action(cid, "low_balance_warned", action_detail={"balance_pct": int(balance/total_recharged*100)})

    # === 3. 屏蔽词检测（连续 7 天 0 检出）===
    if int(c.get("consecutive_zero_detection_days", 0)) >= 7:
        update_status(cid, "keyword_blocked", reason="consecutive_zero_detection_7d")
        log_action(cid, "keyword_blocked_paused", reason="连续 7 天无 AI 收录")
        await _notify_user(
            user_id,
            f'"{c["keyword"]}" 已连续 7 天未被 AI 引擎收录，可能受政策/算法变化影响。'
            f'AI 已暂停，余额 ¥{balance:.0f} 保留。建议换词或恢复后人工评估。'
        )
        return

    # === 4. 频次控制（按 check_frequency_per_day 决定是否查）===
    if not _should_check_now(c):
        return  # 不到时间，跳过

    # === 5. 排名/检出率检查 ===
    try:
        check_result = await _check_rank_and_detection(c)
        current_detection_rate = check_result.get("detection_rate_pct", 0)
        current_rank = check_result.get("best_rank")
    except Exception as e:
        logger.warning(f"[Tick] 套餐 {cid} 查排名失败: {e}")
        log_action(cid, "check_rank_failed", reason=str(e), result="failed")
        return

    # 扣监测费
    consume_balance(cid, float(c.get("monitoring_cost_per_check", 0.29)))
    log_action(
        cid, "check_rank",
        action_detail={"detection_rate_pct": current_detection_rate, "rank": current_rank},
        cost_yuan=float(c.get("monitoring_cost_per_check", 0.29)),
        result="success",
    )

    # 屏蔽词计数
    if current_detection_rate == 0:
        increment_zero_detection(cid)
    else:
        reset_zero_detection(cid)

    # === 6. 是否需要补文 (按目标 SOV 判断) ===
    target_sov = int(c.get("target_sov_pct", 25))
    # 简化：用检出率与 SOV 大致映射判断是否需要补
    # （25% SOV ≈ 检出率 60-75%）
    target_detection_rate = _sov_to_target_detection_rate(target_sov)
    if current_detection_rate >= target_detection_rate:
        return  # 已达标，无需补文

    # === 7. 单篇上限检查 ===
    estimated_cost = await _estimate_article_cost(c["keyword"])
    max_per = float(c.get("max_per_article_yuan", 200))
    if estimated_cost > max_per:
        log_action(
            cid, "skip_expensive",
            reason=f"单篇估算 ¥{estimated_cost:.0f} > 上限 ¥{max_per:.0f}",
            result="skipped",
        )
        return

    # === 8. 生成文章（写作环节）===
    article = await _generate_article(c)
    write_cost = 10.0  # ¥10/篇（feature_pricing）
    consume_balance(cid, write_cost)

    # === 9. 模式分流 ===
    platforms = await _ai_pick_platforms(c)
    if c.get("mode", "semi_auto") == "semi_auto":
        # 半自动：进待审队列
        try:
            create_pending_review(
                campaign_id=cid,
                title=article.get("title", "AI 生成文章"),
                content_preview=article.get("content", "")[:200],
                full_content=article.get("content", ""),
                platforms_to_publish=[p.get("platform", "") for p in platforms],
                ai_reasoning=article.get("reasoning", "AI 检测到检出率低于目标，自动补发"),
                estimated_publish_cost_yuan=sum(float(p.get("our_price_yuan", 0)) for p in platforms),
                article_id=article.get("article_id"),
            )
            log_action(
                cid, "pending_review_created",
                action_detail={"title": article.get("title"), "platforms": [p.get("platform") for p in platforms]},
                cost_yuan=write_cost,
            )
            await _notify_user(
                user_id,
                f'AI 给您写了 1 篇待审"{article.get("title", "")}", 24h 内不审将自动发布'
            )
        except Exception as e:
            logger.error(f"[Tick] 创建待审失败 {cid}: {e}")
            log_action(cid, "pending_review_failed", reason=str(e), result="failed", cost_yuan=write_cost)
        return

    # full_auto: AI 自主发布
    await _publish_article(c, article, platforms)


# ============================================================
# 辅助任务: process_due_pending_reviews（24h 到期自动发）
# ============================================================

def process_due_pending_reviews():
    """同步入口"""
    try:
        asyncio.run(_process_due_pending_async())
    except Exception as e:
        logger.exception(f"process_due_pending_reviews 失败: {e}")


async def _process_due_pending_async():
    from db.managed_campaign_db import (
        get_due_pending_reviews, update_review_status,
        get_campaign, log_action, claim_pending_review_autopublish,
    )
    reviews = get_due_pending_reviews()
    logger.info(f"[Tick] {len(reviews)} 条待审到期，自动发布")
    for r in reviews:
        rid = r["id"]
        cid = r["campaign_id"]
        try:
            # [蓝绿双跑幂等] CAS 占用本条待审自动发布 · 赢家发布 · 输家跳过(防媒体双投)
            if not claim_pending_review_autopublish(rid):
                logger.info(f"[Tick] 待审 {rid} 已被并发占用·跳过(幂等·防双投)")
                continue
            campaign = get_campaign(cid)
            if not campaign or campaign.get("status") != "active":
                update_review_status(rid, "expired", review_note="套餐非 active 状态")
                continue

            # 自动发布
            article = {
                "title": r["title"],
                "content": r["full_content"],
            }
            platforms = [{"platform": p, "our_price_yuan": 0} for p in r.get("platforms_to_publish", [])]
            await _publish_article(campaign, article, platforms, force_full_auto=True)
            update_review_status(rid, "auto_published", review_note="24h 未审，自动发布")
            log_action(cid, "auto_published_after_24h",
                       action_detail={"review_id": rid, "title": r["title"]})
        except Exception as e:
            logger.exception(f"自动发布 {rid} 失败: {e}")
            log_action(cid, "auto_publish_failed", reason=str(e), result="failed")


# ============================================================
# 辅助任务: dormancy_scan（12 个月无操作 → 转赠送）
# ============================================================

def dormancy_scan():
    """同步入口"""
    try:
        asyncio.run(_dormancy_scan_async())
    except Exception as e:
        logger.exception(f"dormancy_scan 失败: {e}")


async def _dormancy_scan_async():
    from db.managed_campaign_db import (
        get_dormancy_candidates, mark_dormant_warned,
        mark_dormancy_converted, get_campaign,
    )
    candidates = get_dormancy_candidates()

    # 30 天提醒
    for c in candidates.get("warn_30d", []):
        balance = float(c.get("total_recharged_yuan", 0)) - float(c.get("total_consumed_yuan", 0))
        if balance > 0:
            await _notify_user(
                c["user_id"],
                f'您的"{c["keyword"]}"套餐 11 个月未活动，余额 ¥{balance:.0f}。'
                f'再过 30 天将自动转为赠送积分（仅限 AI 功能用）。'
                f'如需保留，请在套餐里加充或调整。'
            )
            mark_dormant_warned(c["id"])

    # 7 天提醒
    for c in candidates.get("warn_7d", []):
        balance = float(c.get("total_recharged_yuan", 0)) - float(c.get("total_consumed_yuan", 0))
        if balance > 0:
            await _notify_user(
                c["user_id"],
                f'您的"{c["keyword"]}"套餐余额 ¥{balance:.0f}，7 天后自动转为赠送积分。'
            )

    # 12 个月转赠送
    for c in candidates.get("convert", []):
        balance = float(c.get("total_recharged_yuan", 0)) - float(c.get("total_consumed_yuan", 0))
        if balance > 0:
            try:
                _convert_balance_to_bonus(c["user_id"], balance, c["id"], c["keyword"])
                mark_dormancy_converted(c["id"])
                await _notify_user(
                    c["user_id"],
                    f'您的"{c["keyword"]}"套餐 12 个月未活动，'
                    f'余额 ¥{balance:.0f} 已转为赠送积分（仅限 AI 功能用）。'
                )
            except Exception as e:
                logger.error(f"转赠送失败 {c['id']}: {e}")


def _convert_balance_to_bonus(user_id: int, amount_yuan: float, campaign_id: int, keyword: str):
    """套餐余额转 user_wallets.bonus_points"""
    from db.connection import get_db
    from db.wallet_db import insert_transaction

    bonus_points = int(amount_yuan * 130)  # 1 元 = 130 积分

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_wallets SET bonus_points = bonus_points + %s, "
            "updated_at = NOW() WHERE user_id = %s",
            (bonus_points, user_id),
        )
        # 查新余额
        cursor.execute("SELECT bonus_points FROM user_wallets WHERE user_id = %s", (user_id,))
        wallet = cursor.fetchone()
        new_balance = int(wallet["bonus_points"]) if wallet else bonus_points

        insert_transaction(
            cursor,
            user_id=user_id,
            tx_type="bonus",
            point_type="bonus",
            amount=bonus_points,
            balance_after=new_balance,
            feature_code="managed_dormancy_converted",
            description=f"托管套餐\"{keyword}\" 12 个月无操作转赠送（合规规则）",
            order_id=f"dormancy:{campaign_id}",
        )


# ============================================================
# 内部辅助函数
# ============================================================

def _should_check_now(c: dict) -> bool:
    """根据 check_frequency_per_day 决定是否在本次 tick 跑监测

    简化逻辑：
      1 次/天 → 距上次监测 ≥ 24h 才查
      2 次/天 → ≥ 12h
      3 次/天 → ≥ 8h
    """
    from db.connection import get_connection

    freq = max(1, int(c.get("check_frequency_per_day", 1)))
    interval_hours = 24 / freq

    # 查 managed_actions 最近一次 check_rank 时间
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT created_at FROM managed_actions
            WHERE campaign_id = %s AND action_type = 'check_rank'
            ORDER BY created_at DESC LIMIT 1
            """,
            (c["id"],),
        )
        row = cursor.fetchone()
        if not row:
            return True  # 从未跑过，立即查
        last = row["created_at"]
        elapsed_h = (datetime.now() - last.replace(tzinfo=None)).total_seconds() / 3600
        return elapsed_h >= interval_hours - 0.5  # 留 30 min 容差
    except Exception as e:
        logger.warning(f"_should_check_now 异常: {e}")
        return True
    finally:
        conn.close()


def _sov_to_target_detection_rate(sov_pct: int) -> float:
    """SOV → 目标检出率（百分比）

    经验公式：检出率 = SOV * 2.5 + 30，封顶 85
    25% SOV → 92.5% → 封顶 85% (用 75 作为 trigger 阈值更稳)
    """
    raw = sov_pct * 2.5 + 30
    return min(85.0, max(40.0, raw - 10))  # -10 留 buffer，避免抖动反复触发


async def _check_rank_and_detection(c: dict) -> dict:
    """
    真实查询关键词在 AI 引擎的检出情况（对接 monitoring 模块）

    策略（v2 决策：监测费从套餐 budget 扣）：
      1. 用 batch_monitor.PlatformAdapter.query 实际查 4 引擎
      2. 统计 mentioned_count / total_engines → detection_rate
      3. fallback: 读 monitoring_db 历史 / c_end_cost_estimate 预估

    返回: {detection_rate_pct: int, best_rank: int|None, mentioned_engines: list}
    """
    keyword = c.get("keyword", "")
    brand_id = c.get("brand_id")

    # 1. 真实查询：4 引擎
    try:
        from tools.monitoring.batch_monitor import PlatformAdapter, build_question, resolve_monitoring_query
        from db.connection import get_connection

        # 取品牌名 + 尝试读 monitoring_query(P0.8 CTO-15.9 Codex bug 4)
        brand_name = keyword  # fallback
        monitoring_query_value = None
        if brand_id:
            conn = get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM brands WHERE id = %s", (brand_id,))
                row = cursor.fetchone()
                if row:
                    brand_name = row["name"]
                # 按 keyword 查可能的 monitoring_query(managed 场景不一定有 keyword_id · 尽力而为)
                try:
                    cursor.execute(
                        """
                        SELECT monitoring_query FROM confirmed_keywords
                         WHERE keyword = %s AND monitoring_query IS NOT NULL
                         LIMIT 1
                        """,
                        (keyword,),
                    )
                    ck_row = cursor.fetchone()
                    if ck_row and ck_row.get("monitoring_query"):
                        monitoring_query_value = ck_row["monitoring_query"]
                    if not monitoring_query_value:
                        cursor.execute(
                            """
                            SELECT monitoring_query FROM extra_keywords
                             WHERE keyword = %s AND monitoring_query IS NOT NULL
                             LIMIT 1
                            """,
                            (keyword,),
                        )
                        ek_row = cursor.fetchone()
                        if ek_row and ek_row.get("monitoring_query"):
                            monitoring_query_value = ek_row["monitoring_query"]
                except Exception:
                    pass  # 字段不存在或查询失败 · fallback build_question
            finally:
                conn.close()

        question = resolve_monitoring_query({
            "keyword": keyword,
            "monitoring_query": monitoring_query_value,
        })
        engines = PlatformAdapter.get_supported_platforms()
        mentioned_engines = []

        for engine in engines:
            try:
                result = await PlatformAdapter.query(
                    platform=engine,
                    question=question,
                    target_brand=brand_name,
                    caller="geo_managed_monitoring",
                    brand_id=brand_id,
                    keyword=keyword,
                )
                if result.get("is_detected") or result.get("mentioned") or result.get("is_mentioned"):
                    mentioned_engines.append(engine)
            except Exception as e:
                logger.debug(f"_check_rank engine {engine} 失败: {e}")

        detection_rate_pct = int(len(mentioned_engines) / max(1, len(engines)) * 100)
        return {
            "detection_rate_pct": detection_rate_pct,
            "best_rank": None,
            "mentioned_engines": mentioned_engines,
        }
    except Exception as e:
        logger.warning(f"_check_rank_and_detection 真实查询失败，走 fallback: {e}")

    # 2. fallback: 读 monitoring_db 历史
    try:
        from db.monitoring_db import get_keyword_history
        if brand_id:
            history = get_keyword_history(brand_id=brand_id, keyword=keyword, limit=1)
            if history:
                latest = history[0] if isinstance(history, list) else history
                rate = latest.get("detection_rate", 0)
                return {
                    "detection_rate_pct": int(rate * 100) if isinstance(rate, float) and rate <= 1 else int(rate),
                    "best_rank": latest.get("best_rank"),
                    "mentioned_engines": latest.get("mentioned_engines", []),
                }
    except Exception as e:
        logger.debug(f"_check_rank fallback history: {e}")

    # 3. fallback: c_end_cost_estimate 预估
    try:
        from tools.c_end_cost_estimate import estimate_market_saturation, estimate_detection_rate
        market = await estimate_market_saturation(keyword)
        delivered = int(c.get("delivered_articles", 0))
        rate = estimate_detection_rate(delivered, market.get("competition_level", 3))
        return {
            "detection_rate_pct": int(rate * 100),
            "best_rank": None,
            "mentioned_engines": [],
        }
    except Exception:
        return {"detection_rate_pct": 0, "best_rank": None, "mentioned_engines": []}


async def _estimate_article_cost(keyword: str) -> float:
    """单篇文章预估总成本（写作 + 发布加权均价）"""
    from .reverse_calc import _estimate_avg_article_cost
    return await _estimate_avg_article_cost(keyword)


async def _generate_article(c: dict) -> dict:
    """
    生成 GEO 文章（对接真实 ArticleGeneratorService + V9/V10 模板）

    流程：
      1. 从 campaign 取 keyword / brand_name / industry
      2. 用 style_registry.allocate_styles_by_ratio(1, industry=...) 分配文体(v2.7.1 industry 贯穿)
      3. 调 ArticleGeneratorService.generate_articles([topic])
      4. 返回 {title, content, reasoning, article_id}

    fallback: 如果真实生成失败，返回占位（不阻塞 tick）
    """
    keyword = c.get("keyword", "")
    brand_id = c.get("brand_id")
    brand_name = keyword
    industry = ""

    # 取品牌信息 + 关联 quote_id
    quote_id = 0
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT b.name, p.industry FROM brands b "
                "LEFT JOIN client_profiles p ON p.brand_id = b.id "
                "WHERE b.id = %s LIMIT 1",
                (brand_id,),
            )
            row = cursor.fetchone()
            if row:
                brand_name = row.get("name") or keyword
                industry = row.get("industry") or ""
            # 取关联的 quote_id（优先 confirmed，否则最新）
            if brand_id:
                # [#6 2026-06-07] 文章归属优先选"服务锚内"quote(避免挂到草稿/过期 quote)· 无锚回落最新
                #   fail-SOFT:解析失败/无锚都回落原"任意最新"逻辑 · 托管补文绝不因此中断
                #   (资金走 campaign 预付池·与本 quote 选取无关·所以这里只防归属污染·不做 fail-closed)
                anchored_ids = []
                try:
                    from db.monitoring_db import resolve_service_anchored_quote_ids_for_brand
                    anchored_ids = resolve_service_anchored_quote_ids_for_brand(brand_id) or []
                except Exception as _se:
                    logger.debug(f"_generate_article 服务锚解析失败 brand={brand_id}: {_se}")
                if anchored_ids:
                    cursor.execute(
                        "SELECT id FROM quotes WHERE brand_id = %s AND id = ANY(%s) "
                        "ORDER BY (status = 'confirmed') DESC, created_at DESC LIMIT 1",
                        (brand_id, anchored_ids),
                    )
                else:
                    cursor.execute(
                        "SELECT id FROM quotes WHERE brand_id = %s ORDER BY (status = 'confirmed') DESC, created_at DESC LIMIT 1",
                        (brand_id,),
                    )
                q_row = cursor.fetchone()
                if q_row:
                    quote_id = q_row["id"]
        finally:
            try:
                conn.close()
            except Exception: pass
    except Exception as e:
        logger.debug(f"_generate_article 取品牌信息失败: {e}")

    # 真实生成
    try:
        from writing.article_generator_service import ArticleGeneratorService
        from writing.article_style_contract import evidence_first_title_for_family
        from writing.style_registry import allocate_styles_by_ratio

        style_code = allocate_styles_by_ratio(1, industry=industry or None)[0]  # v2.7.1 industry 贯穿
        topic = {
            "id": 0,
            "title": evidence_first_title_for_family(
                brand_name=brand_name,
                industry=industry,
                family_or_style=style_code,
                keywords=[keyword],
                index=1,
            ),
            "style": {"code": style_code},
            "keyword": keyword,
        }

        service = ArticleGeneratorService(
            quote_id=quote_id,
            brand_name=brand_name,
            industry=industry,
        )
        results = await service.generate_articles([topic], max_concurrent=1)

        if results and len(results) > 0:
            art = results[0]
            return {
                "title": art.get("title", topic["title"]),
                "content": art.get("content", ""),
                "reasoning": f"AI 检测到 {keyword} 检出率低于目标 SOV，自动补发（{style_code} 风格）",
                "article_id": art.get("id"),
                "style_code": style_code,
            }
    except Exception as e:
        logger.warning(f"_generate_article 真实生成失败，用 fallback: {e}")

    # Generation failure remains an explicit invalid artifact.  It may be
    # logged for operations but _publish_article rejects it before provider IO.
    from writing.article_style_contract import evidence_first_title_for_family
    return {
        "title": evidence_first_title_for_family(
            brand_name="", industry="", family_or_style="implementation_guide",
            keywords=[keyword], index=1,
        ),
        "content": f"[自动生成失败，占位内容] {keyword} 的 GEO 优化文章",
        "reasoning": f"AI 真实生成失败（fallback），关键词: {keyword}",
        "article_id": None,
        "style_code": "fallback",
        "generation_provenance": "invalid_generation_fallback",
    }


async def _ai_pick_platforms(c: dict) -> list[dict]:
    """AI 动态选媒体（基于 GEO 数据 + brand_strategies）"""
    try:
        from services.placement_service import recommend_for_publish
        return recommend_for_publish(
            industry="",  # campaign 不带 industry，简化处理
            keywords=c.get("keyword", ""),
            limit=3,  # 单次最多发 3 个平台
        )
    except Exception as e:
        logger.warning(f"_ai_pick_platforms 失败: {e}")
        return []


async def _publish_article(c: dict, article: dict, platforms: list[dict],
                            **_kwargs) -> None:
    """
    发布文章到媒体（对接真实 MeiJieHeZiClient）

    流程：
      1. 从 DB 拿 MHZ session_id
      2. 从 platforms 查 media_ids（mhz_media 表）
      3. 调 MeiJieHeZiClient.publish(title, content_md, media_ids)
      4. 成功 → 扣发布费 + delivered_articles +1
      5. 失败 → 发布部分不扣（v2 决策：写作扣 + 发布退）
    """
    from db.managed_campaign_db import (
        consume_balance, log_action,
    )

    cid = c["id"]
    user_id = c["user_id"]
    publish_cost = sum(float(p.get("our_price_yuan", 0)) for p in platforms)

    try:
        if not article.get("article_id") or article.get("style_code") == "fallback":
            raise RuntimeError("托管生成失败或缺少真实 article_id；占位标题/正文禁止进入发布通道")
        # 1. 拿 MHZ session
        from db.publish_db import get_mhz_session
        session_id = get_mhz_session()
        if not session_id:
            raise RuntimeError("MHZ Session 未配置，无法发布")

        # 2. 从平台推荐列表提取 media_ids
        media_ids = []
        for p in platforms:
            mid = p.get("media_id")
            if mid:
                media_ids.append(int(mid))
        if not media_ids:
            # fallback: 从 mhz_media 按平台名搜
            try:
                from db.publish_db import get_media_list as get_pub_media_list
                for p in platforms[:3]:
                    platform_name = p.get("platform") or p.get("media_name", "")
                    if platform_name:
                        result = get_pub_media_list(
                            page=1, limit=1,
                            sort_by="our_price_points", sort_dir="asc",
                        )
                        media_items = result.get("media", [])
                        if media_items:
                            media_ids.append(media_items[0]["id"])
            except Exception as e:
                logger.warning(f"_publish fallback media_ids 失败: {e}")

        if not media_ids:
            raise RuntimeError(f"未找到可用媒体 (platforms={[p.get('platform') for p in platforms]})")

        # 3. 调 MHZ 发布
        from services.meijiehezi_client import MeiJieHeZiClient
        client = MeiJieHeZiClient(session_id)
        from services.article_publish_dispatch import dispatch_article_to_provider
        result = await dispatch_article_to_provider(
            client=client, dispatch_kind="publish", article_id=article["article_id"],
            source_title=article.get("title", ""), source_content=article.get("content", ""),
            outgoing_title=article.get("title", "")[:45],
            outgoing_content=article.get("content", ""),
            source="managed_campaign:softarticle",
            provider_kwargs={
                "media_ids": media_ids,
                "order_remark": f"GEO 托管自动发布 campaign_id={cid}",
            },
        )

        if not result.success:
            raise RuntimeError(f"MHZ 发布失败: code={result.code} msg={result.msg}")

        # 4. 成功：扣发布费 + delivered_articles +1
        consume_balance(cid, publish_cost, article_delivered=True)

        log_action(
            cid, "replenish",
            action_detail={
                "platforms": [p.get("platform") for p in platforms],
                "media_ids": media_ids,
                "title": article.get("title"),
                "mhz_selected_num": result.selected_num if hasattr(result, 'selected_num') else 0,
            },
            cost_yuan=10 + publish_cost,
            result="success",
        )

        if c.get("alert_on_replenish", True):
            from db.managed_campaign_db import get_campaign
            updated = get_campaign(cid)
            balance = updated.get("balance_yuan", 0) if updated else 0
            await _notify_user(
                user_id,
                f'AI 已补发 1 篇 (累计 {updated.get("delivered_articles", 0) if updated else 0} 篇，'
                f'余额 ¥{balance:.0f})'
            )

    except Exception as e:
        logger.error(f"_publish_article 失败 {cid}: {e}")
        # 失败处理：发布部分不扣（v2 决策：写作已扣 ¥10，发布不扣）
        log_action(
            cid, "publish_failed",
            reason=str(e),
            cost_yuan=10,  # 仅写作部分
            result="failed",
        )
        # [GEO-R4-CAN-013] 发布失败必须向上抛,不能吞后正常返回。
        #   否则调用方(approve 端点 / 24h 自动发布调度)会在 await 返回后
        #   误标 review 为 approved/auto_published,记录一次"假交付"。
        #   两个调用方都已用 try/except 包裹并只在干净 await 后才转状态:
        #     - api/managed_campaign_api.py:1045-1052 approve → 抛则 500 且保持 pending
        #     - _process_due_pending_async:219-241 → 抛则记 auto_publish_failed 且不转 auto_published
        #   仅改控制流信号(不改扣费守恒:失败仍不扣发布费,与原逻辑一致)。
        raise


async def _notify_user(user_id: int, message: str) -> None:
    """发通知到用户（站内信 / 邮件 / 短信）

    简化版：写到 logger，实际生产对接 notifications 模块
    """
    try:
        from db.notifications import send_notification
        send_notification(user_id=user_id, content=message, category="managed")
    except Exception:
        # fallback: 仅 log
        logger.info(f"[Notify→{user_id}] {message}")


# ============================================================
# v3.4 复盘引擎触发
# ============================================================

def weekly_review_brand_packages():
    """每周一凌晨跑 — 触发所有到期需复盘的 brand_managed_packages"""
    try:
        asyncio.run(_weekly_review_async())
    except Exception as e:
        logger.exception(f"weekly_review_brand_packages 失败: {e}")


async def _weekly_review_async():
    from db.managed_campaign_db import get_due_review_brand_packages, update_brand_package_review
    from .review_engine import weekly_learning

    packages = get_due_review_brand_packages()
    logger.info(f"[Review] {len(packages)} 个品牌套餐到期复盘")

    for pkg in packages:
        brand_id = pkg["brand_id"]
        try:
            await weekly_learning(brand_id)
            update_brand_package_review(brand_id)
            logger.info(f"[Review] 品牌 {brand_id} 复盘完成")
        except Exception as e:
            logger.exception(f"[Review] 品牌 {brand_id} 复盘失败: {e}")
