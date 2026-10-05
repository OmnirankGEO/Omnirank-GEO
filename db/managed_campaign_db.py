"""
v3.3/v3.4 GEO 全自动托管 — DB 访问层

涵盖表：
  - managed_campaigns      托管套餐主表（充值池模式）
  - managed_actions        操作审计日志（AI 视角）
  - pending_review_articles 半自动模式待审队列
  - brand_strategies       v3.4 品牌策略档案（护城河）
  - brand_managed_packages v3.4 全品牌托管主记录

核心规则（来自 v2 决策）：
  - 充值即消费，不退款
  - 余额 = total_recharged_yuan - total_consumed_yuan（实时算）
  - 同品牌+同关键词唯一约束（DB 层强一致）
  - 12 个月无操作 → 转赠送积分
  - 报价 3 天有效期
  - 屏蔽词连续 7 天 0 检出 → 自动暂停
"""

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Any

from db.connection import get_connection, get_db

logger = logging.getLogger("GEO-Managed-DB")


# ==================== 工具函数 ====================

def _to_dict(row) -> Optional[dict]:
    """RealDictRow → 普通 dict（防序列化问题）"""
    if row is None:
        return None
    return dict(row)


def _safe_json(val, default=None):
    """JSONB 字段安全反序列化（psycopg2 已自动反序列化时直接返回）"""
    if val is None:
        return default
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (ValueError, TypeError):
            return default
    return default


def _decimal_to_float(val) -> float:
    """Decimal → float（JSON 序列化前用）"""
    if val is None:
        return 0.0
    if isinstance(val, Decimal):
        return float(val)
    return float(val)


def get_balance(campaign: dict) -> float:
    """计算套餐当前余额"""
    if not campaign:
        return 0.0
    return _decimal_to_float(campaign.get("total_recharged_yuan", 0)) - \
           _decimal_to_float(campaign.get("total_consumed_yuan", 0))


# ==================== managed_campaigns CRUD ====================

def create_campaign(
    user_id: int,
    brand_id: int,
    keyword: str,
    target_sov_pct: int,
    target_display_label: str,
    tier_label: str,
    initial_recharge_yuan: float,
    mode: str = "semi_auto",
    max_per_article_yuan: float = 200,
    check_frequency_per_day: int = 1,
    estimate_quoted_at: Optional[datetime] = None,
    current_plan: Optional[dict] = None,
    authorized_ip: Optional[str] = None,
    agreement_version: str = "v3.3-2026-04",
) -> dict:
    """创建新托管套餐（同品牌+同关键词唯一约束 → DuplicateCampaignError）

    Raises:
      psycopg2.errors.UniqueViolation: 同品牌+同关键词已有 active 套餐
    """
    quoted_at = estimate_quoted_at or datetime.now()
    valid_until = quoted_at + timedelta(days=3)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO managed_campaigns (
              user_id, brand_id, keyword,
              target_sov_pct, target_display_label, tier_label,
              initial_recharge_yuan, total_recharged_yuan, total_consumed_yuan,
              mode, max_per_article_yuan, check_frequency_per_day,
              estimate_quoted_at, estimate_valid_until,
              current_plan, authorized_ip, agreement_version,
              status
            )
            VALUES (
              %s, %s, %s,
              %s, %s, %s,
              %s, %s, 0,
              %s, %s, %s,
              %s, %s,
              %s, %s, %s,
              'active'
            )
            RETURNING *
            """,
            (
                user_id, brand_id, keyword,
                target_sov_pct, target_display_label, tier_label,
                initial_recharge_yuan, initial_recharge_yuan,
                mode, max_per_article_yuan, check_frequency_per_day,
                quoted_at, valid_until,
                json.dumps(current_plan) if current_plan else None,
                authorized_ip, agreement_version,
            ),
        )
        row = cursor.fetchone()
        return _to_dict(row)


def get_campaign(campaign_id: int) -> Optional[dict]:
    """读取套餐详情"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM managed_campaigns WHERE id = %s",
            (campaign_id,),
        )
        row = cursor.fetchone()
        if row:
            data = _to_dict(row)
            data["balance_yuan"] = get_balance(data)
            data["current_plan"] = _safe_json(data.get("current_plan"))
            return data
        return None
    finally:
        conn.close()


def get_user_campaigns(
    user_id: int,
    status: Optional[str] = None,
    brand_id: Optional[int] = None,
    limit: int = 50,
) -> list[dict]:
    """列出用户的所有套餐（按创建时间倒序）"""
    sql = "SELECT * FROM managed_campaigns WHERE user_id = %s"
    params: list[Any] = [user_id]
    if status:
        sql += " AND status = %s"
        params.append(status)
    if brand_id:
        sql += " AND brand_id = %s"
        params.append(brand_id)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall() or []
        results = []
        for r in rows:
            d = _to_dict(r)
            d["balance_yuan"] = get_balance(d)
            d["current_plan"] = _safe_json(d.get("current_plan"))
            results.append(d)
        return results
    finally:
        conn.close()


def get_active_campaigns_for_tick() -> list[dict]:
    """定时任务：取所有 active 套餐"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM managed_campaigns WHERE status = 'active' ORDER BY id"
        )
        rows = cursor.fetchall() or []
        return [_to_dict(r) for r in rows]
    finally:
        conn.close()


def find_active_campaign_by_keyword(brand_id: int, keyword: str) -> Optional[dict]:
    """检查同品牌+同关键词是否已有活跃套餐（防止重复扣费）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM managed_campaigns "
            "WHERE brand_id = %s AND keyword = %s AND status = 'active' "
            "LIMIT 1",
            (brand_id, keyword),
        )
        return _to_dict(cursor.fetchone())
    finally:
        conn.close()


def consume_balance(
    campaign_id: int,
    cost_yuan: float,
    article_delivered: bool = False,
) -> dict:
    """套餐内消耗（监测/补文/发布）。返回更新后的套餐。"""
    with get_db() as conn:
        cursor = conn.cursor()
        sql = """
            UPDATE managed_campaigns
            SET total_consumed_yuan = total_consumed_yuan + %s,
                last_active_at = NOW()
        """
        params: list[Any] = [cost_yuan]
        if article_delivered:
            sql += ", delivered_articles = delivered_articles + 1"
        sql += " WHERE id = %s RETURNING *"
        params.append(campaign_id)
        cursor.execute(sql, tuple(params))
        return _to_dict(cursor.fetchone())


def top_up_campaign(campaign_id: int, amount_yuan: float) -> dict:
    """加充：增加 total_recharged_yuan + 重置 low_balance_warned + 改回 active"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE managed_campaigns
            SET total_recharged_yuan = total_recharged_yuan + %s,
                low_balance_warned = FALSE,
                last_active_at = NOW(),
                status = CASE WHEN status IN ('depleted','paused') THEN 'active' ELSE status END,
                paused_at = CASE WHEN status = 'paused' THEN NULL ELSE paused_at END,
                depleted_at = CASE WHEN status = 'depleted' THEN NULL ELSE depleted_at END
            WHERE id = %s
            RETURNING *
            """,
            (amount_yuan, campaign_id),
        )
        return _to_dict(cursor.fetchone())


def update_status(
    campaign_id: int,
    status: str,
    reason: Optional[str] = None,
) -> dict:
    """更新状态 (paused / depleted / keyword_blocked / user_cancelled)"""
    with get_db() as conn:
        cursor = conn.cursor()
        sql = "UPDATE managed_campaigns SET status = %s, last_active_at = NOW()"
        params: list[Any] = [status]
        if status == "paused":
            sql += ", paused_at = NOW()"
        elif status == "depleted":
            sql += ", depleted_at = NOW()"
        elif status == "active":
            sql += ", paused_at = NULL, depleted_at = NULL"
        sql += " WHERE id = %s RETURNING *"
        params.append(campaign_id)
        cursor.execute(sql, tuple(params))
        return _to_dict(cursor.fetchone())


def update_estimate_window(campaign_id: int) -> dict:
    """报价过期重新评估时调用，刷新 quoted_at + valid_until"""
    quoted_at = datetime.now()
    valid_until = quoted_at + timedelta(days=3)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE managed_campaigns
            SET estimate_quoted_at = %s,
                estimate_valid_until = %s,
                last_active_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (quoted_at, valid_until, campaign_id),
        )
        return _to_dict(cursor.fetchone())


def update_current_plan(campaign_id: int, current_plan: dict) -> dict:
    """对话调方案后更新 current_plan + 刷新报价窗口"""
    quoted_at = datetime.now()
    valid_until = quoted_at + timedelta(days=3)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE managed_campaigns
            SET current_plan = %s,
                estimate_quoted_at = %s,
                estimate_valid_until = %s,
                last_active_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (json.dumps(current_plan), quoted_at, valid_until, campaign_id),
        )
        return _to_dict(cursor.fetchone())


def increment_zero_detection(campaign_id: int) -> int:
    """监测到 0 检出时累加；返回累计天数"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE managed_campaigns
            SET consecutive_zero_detection_days = consecutive_zero_detection_days + 1,
                last_active_at = NOW()
            WHERE id = %s
            RETURNING consecutive_zero_detection_days
            """,
            (campaign_id,),
        )
        row = cursor.fetchone()
        return int(row["consecutive_zero_detection_days"]) if row else 0


def reset_zero_detection(campaign_id: int) -> None:
    """有检出时重置归 0"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE managed_campaigns SET consecutive_zero_detection_days = 0 WHERE id = %s",
            (campaign_id,),
        )


def mark_low_balance_warned(campaign_id: int) -> None:
    """余额 < 20% 提醒后标记，避免重复提醒"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE managed_campaigns SET low_balance_warned = TRUE WHERE id = %s",
            (campaign_id,),
        )


# ==================== managed_actions ====================

def log_action(
    campaign_id: int,
    action_type: str,
    action_detail: Optional[dict] = None,
    cost_points: int = 0,
    cost_yuan: float = 0,
    result: str = "success",
    reason: Optional[str] = None,
) -> int:
    """记录套餐操作（AI 视角动作日志）"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO managed_actions (
              campaign_id, action_type, action_detail,
              cost_points, cost_yuan, result, reason
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                campaign_id, action_type,
                json.dumps(action_detail) if action_detail else None,
                cost_points, cost_yuan, result, reason,
            ),
        )
        row = cursor.fetchone()
        return int(row["id"]) if row else 0


def get_recent_actions(campaign_id: int, limit: int = 20) -> list[dict]:
    """读取套餐的最近 N 条操作日志"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, action_type, action_detail,
                   cost_points, cost_yuan, result, reason, created_at
            FROM managed_actions
            WHERE campaign_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (campaign_id, limit),
        )
        rows = cursor.fetchall() or []
        results = []
        for r in rows:
            d = _to_dict(r)
            d["action_detail"] = _safe_json(d.get("action_detail"))
            d["cost_yuan"] = _decimal_to_float(d.get("cost_yuan", 0))
            results.append(d)
        return results
    finally:
        conn.close()


# ==================== pending_review_articles ====================

def create_pending_review(
    campaign_id: int,
    title: str,
    content_preview: str,
    full_content: str,
    platforms_to_publish: list,
    ai_reasoning: str,
    estimated_publish_cost_yuan: float,
    article_id: Optional[int] = None,
    auto_publish_after_hours: int = 24,
) -> dict:
    """半自动模式：创建待审记录"""
    auto_publish_at = datetime.now() + timedelta(hours=auto_publish_after_hours)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO pending_review_articles (
              campaign_id, article_id, title, content_preview, full_content,
              platforms_to_publish, ai_reasoning,
              estimated_publish_cost_yuan, auto_publish_at, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
            RETURNING *
            """,
            (
                campaign_id, article_id, title, content_preview, full_content,
                json.dumps(platforms_to_publish), ai_reasoning,
                estimated_publish_cost_yuan, auto_publish_at,
            ),
        )
        return _to_dict(cursor.fetchone())


def get_pending_reviews_for_campaign(campaign_id: int, limit: int = 20) -> list[dict]:
    """读取套餐的待审列表"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM pending_review_articles
            WHERE campaign_id = %s AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (campaign_id, limit),
        )
        rows = cursor.fetchall() or []
        results = []
        for r in rows:
            d = _to_dict(r)
            d["platforms_to_publish"] = _safe_json(d.get("platforms_to_publish"), default=[])
            d["estimated_publish_cost_yuan"] = _decimal_to_float(d.get("estimated_publish_cost_yuan", 0))
            results.append(d)
        return results
    finally:
        conn.close()


def get_due_pending_reviews() -> list[dict]:
    """定时任务：取所有到期需自动发布的待审（auto_publish_at <= NOW()）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM pending_review_articles
            WHERE status = 'pending' AND auto_publish_at <= NOW()
            ORDER BY auto_publish_at
            LIMIT 100
            """
        )
        rows = cursor.fetchall() or []
        results = []
        for r in rows:
            d = _to_dict(r)
            d["platforms_to_publish"] = _safe_json(d.get("platforms_to_publish"), default=[])
            d["estimated_publish_cost_yuan"] = _decimal_to_float(d.get("estimated_publish_cost_yuan", 0))
            results.append(d)
        return results
    finally:
        conn.close()


def update_review_status(
    review_id: int,
    status: str,
    reviewed_by_user_id: Optional[int] = None,
    review_note: Optional[str] = None,
) -> dict:
    """更新待审状态 (approved / rejected / withdrawn / auto_published / expired)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE pending_review_articles
            SET status = %s,
                reviewed_at = NOW(),
                reviewed_by_user_id = %s,
                review_note = %s
            WHERE id = %s
            RETURNING *
            """,
            (status, reviewed_by_user_id, review_note, review_id),
        )
        return _to_dict(cursor.fetchone())


def claim_campaign_tick(campaign_id: int, window_minutes: int = 60) -> bool:
    """[蓝绿双跑幂等] CAS 占用本轮 tick:近 window 分钟未被处理才返回 True(防双扣监测/写作 + 双发)。
    tick 每 6h 跑 + claim 窗口 60min → 只挡同轮蓝绿双跑,不挡正常下轮。
    列缺失/异常 → 抛出由调用方 try/except 兜(fail-closed:跳过本轮·绝不双扣)。"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE managed_campaigns
            SET last_tick_claimed_at = NOW()
            WHERE id = %s
              AND (last_tick_claimed_at IS NULL
                   OR last_tick_claimed_at < NOW() - (%s || ' minutes')::INTERVAL)
            RETURNING id
            """,
            (campaign_id, str(int(window_minutes))),
        )
        return cursor.fetchone() is not None


def claim_pending_review_autopublish(review_id: int) -> bool:
    """[蓝绿双跑幂等] CAS 占用待审自动发布:仅 status='pending' 且未被 claim 才返回 True(防媒体双投)。
    赢家发布·输家跳过。列缺失/异常 → 抛出由调用方 try/except 兜(fail-closed:不发布)。"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE pending_review_articles
            SET autopublish_claimed_at = NOW()
            WHERE id = %s AND status = 'pending' AND autopublish_claimed_at IS NULL
            RETURNING id
            """,
            (review_id,),
        )
        return cursor.fetchone() is not None


def get_review_by_id(review_id: int) -> Optional[dict]:
    """读取单个待审"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM pending_review_articles WHERE id = %s",
            (review_id,),
        )
        d = _to_dict(cursor.fetchone())
        if d:
            d["platforms_to_publish"] = _safe_json(d.get("platforms_to_publish"), default=[])
        return d
    finally:
        conn.close()


def get_user_pending_reviews(user_id: int, limit: int = 50) -> list[dict]:
    """读取用户所有套餐的待审（夸 campaign 聚合）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT pr.*, mc.keyword
            FROM pending_review_articles pr
            JOIN managed_campaigns mc ON mc.id = pr.campaign_id
            WHERE mc.user_id = %s AND pr.status = 'pending'
            ORDER BY pr.created_at DESC
            LIMIT %s
            """,
            (user_id, limit),
        )
        rows = cursor.fetchall() or []
        results = []
        for r in rows:
            d = _to_dict(r)
            d["platforms_to_publish"] = _safe_json(d.get("platforms_to_publish"), default=[])
            d["estimated_publish_cost_yuan"] = _decimal_to_float(d.get("estimated_publish_cost_yuan", 0))
            results.append(d)
        return results
    finally:
        conn.close()


# ==================== brand_strategies (v3.4 复盘引擎) ====================

def upsert_brand_strategy(
    brand_id: int,
    top_performing_styles: Optional[dict] = None,
    top_performing_platforms: Optional[dict] = None,
    effective_keywords: Optional[dict] = None,
    losing_patterns: Optional[dict] = None,
    preferred_content_type: Optional[str] = None,
    preferred_platform_mix: Optional[dict] = None,
    article_tone: Optional[str] = None,
    shareable_patterns: Optional[dict] = None,
    share_consent: bool = True,
) -> dict:
    """复盘引擎写入/更新品牌策略档案"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO brand_strategies (
              brand_id,
              top_performing_styles, top_performing_platforms,
              effective_keywords, losing_patterns,
              preferred_content_type, preferred_platform_mix, article_tone,
              shareable_patterns, share_consent,
              last_reviewed_at, review_count
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 1)
            ON CONFLICT (brand_id) DO UPDATE SET
              top_performing_styles = COALESCE(EXCLUDED.top_performing_styles, brand_strategies.top_performing_styles),
              top_performing_platforms = COALESCE(EXCLUDED.top_performing_platforms, brand_strategies.top_performing_platforms),
              effective_keywords = COALESCE(EXCLUDED.effective_keywords, brand_strategies.effective_keywords),
              losing_patterns = COALESCE(EXCLUDED.losing_patterns, brand_strategies.losing_patterns),
              preferred_content_type = COALESCE(EXCLUDED.preferred_content_type, brand_strategies.preferred_content_type),
              preferred_platform_mix = COALESCE(EXCLUDED.preferred_platform_mix, brand_strategies.preferred_platform_mix),
              article_tone = COALESCE(EXCLUDED.article_tone, brand_strategies.article_tone),
              shareable_patterns = COALESCE(EXCLUDED.shareable_patterns, brand_strategies.shareable_patterns),
              share_consent = EXCLUDED.share_consent,
              last_reviewed_at = NOW(),
              review_count = brand_strategies.review_count + 1,
              updated_at = NOW()
            RETURNING *
            """,
            (
                brand_id,
                json.dumps(top_performing_styles) if top_performing_styles else None,
                json.dumps(top_performing_platforms) if top_performing_platforms else None,
                json.dumps(effective_keywords) if effective_keywords else None,
                json.dumps(losing_patterns) if losing_patterns else None,
                preferred_content_type,
                json.dumps(preferred_platform_mix) if preferred_platform_mix else None,
                article_tone,
                json.dumps(shareable_patterns) if shareable_patterns else None,
                share_consent,
            ),
        )
        d = _to_dict(cursor.fetchone())
        for k in ("top_performing_styles", "top_performing_platforms",
                  "effective_keywords", "losing_patterns",
                  "preferred_platform_mix", "shareable_patterns"):
            d[k] = _safe_json(d.get(k))
        return d


def get_brand_strategy(brand_id: int) -> Optional[dict]:
    """读品牌策略档案"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM brand_strategies WHERE brand_id = %s",
            (brand_id,),
        )
        d = _to_dict(cursor.fetchone())
        if d:
            for k in ("top_performing_styles", "top_performing_platforms",
                      "effective_keywords", "losing_patterns",
                      "preferred_platform_mix", "shareable_patterns"):
                d[k] = _safe_json(d.get(k))
        return d
    finally:
        conn.close()


# ==================== brand_managed_packages (v3.4 全品牌套餐) ====================

def create_brand_package(
    user_id: int,
    brand_id: int,
    total_price_yuan: float,
    raw_cost_yuan: float,
    campaign_ids: list[int],
    markup_factor: float = 1.2,
    authorized_ip: Optional[str] = None,
    agreement_version: str = "v3.4-2026-04",
) -> dict:
    """创建全品牌套餐主记录"""
    next_review_at = datetime.now() + timedelta(days=7)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO brand_managed_packages (
              user_id, brand_id,
              total_price_yuan, raw_cost_yuan, markup_factor,
              campaign_ids, status, next_review_at,
              authorized_ip, agreement_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)
            RETURNING *
            """,
            (
                user_id, brand_id,
                total_price_yuan, raw_cost_yuan, markup_factor,
                campaign_ids, next_review_at,
                authorized_ip, agreement_version,
            ),
        )
        return _to_dict(cursor.fetchone())


def get_brand_package(brand_id: int) -> Optional[dict]:
    """读品牌套餐"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM brand_managed_packages WHERE brand_id = %s AND status='active' "
            "ORDER BY created_at DESC LIMIT 1",
            (brand_id,),
        )
        d = _to_dict(cursor.fetchone())
        if d:
            d["campaign_ids"] = list(d.get("campaign_ids") or [])
            d["total_price_yuan"] = _decimal_to_float(d.get("total_price_yuan", 0))
            d["raw_cost_yuan"] = _decimal_to_float(d.get("raw_cost_yuan", 0))
        return d
    finally:
        conn.close()


def get_due_review_brand_packages() -> list[dict]:
    """定时任务：取所有到期需复盘的品牌套餐"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM brand_managed_packages
            WHERE status = 'active' AND next_review_at <= NOW()
            ORDER BY next_review_at
            LIMIT 50
            """
        )
        rows = cursor.fetchall() or []
        results = []
        for r in rows:
            d = _to_dict(r)
            d["campaign_ids"] = list(d.get("campaign_ids") or [])
            results.append(d)
        return results
    finally:
        conn.close()


def update_brand_package_review(brand_id: int) -> None:
    """复盘后更新 last_review_at 和 next_review_at"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE brand_managed_packages
            SET last_review_at = NOW(),
                next_review_at = NOW() + INTERVAL '7 days'
            WHERE brand_id = %s AND status = 'active'
            """,
            (brand_id,),
        )


# ==================== 12 个月休眠扫描 ====================

def get_dormancy_candidates() -> dict[str, list[dict]]:
    """
    扫描 last_active_at 超 11/11.5/12 个月的套餐
    返回三组：30 天提醒 / 7 天提醒 / 转赠送
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        results: dict[str, list[dict]] = {"warn_30d": [], "warn_7d": [], "convert": []}

        # 11 个月（330 天）— 还没发过 dormant_warned
        cursor.execute("""
            SELECT id, user_id, keyword, total_recharged_yuan, total_consumed_yuan
            FROM managed_campaigns
            WHERE status IN ('active','paused','depleted')
              AND last_active_at < NOW() - INTERVAL '330 days'
              AND last_active_at >= NOW() - INTERVAL '350 days'
              AND dormant_warned_at IS NULL
              AND dormancy_converted_at IS NULL
        """)
        results["warn_30d"] = [_to_dict(r) for r in (cursor.fetchall() or [])]

        # 11.5 个月（350 天）
        cursor.execute("""
            SELECT id, user_id, keyword, total_recharged_yuan, total_consumed_yuan
            FROM managed_campaigns
            WHERE status IN ('active','paused','depleted')
              AND last_active_at < NOW() - INTERVAL '350 days'
              AND last_active_at >= NOW() - INTERVAL '365 days'
              AND dormancy_converted_at IS NULL
        """)
        results["warn_7d"] = [_to_dict(r) for r in (cursor.fetchall() or [])]

        # 12 个月（365 天）→ 转赠送
        cursor.execute("""
            SELECT id, user_id, keyword, total_recharged_yuan, total_consumed_yuan
            FROM managed_campaigns
            WHERE status IN ('active','paused','depleted')
              AND last_active_at < NOW() - INTERVAL '365 days'
              AND dormancy_converted_at IS NULL
        """)
        results["convert"] = [_to_dict(r) for r in (cursor.fetchall() or [])]

        return results
    finally:
        conn.close()


def mark_dormant_warned(campaign_id: int) -> None:
    """标记已发提醒"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE managed_campaigns SET dormant_warned_at = NOW() WHERE id = %s",
            (campaign_id,),
        )


def mark_dormancy_converted(campaign_id: int) -> None:
    """标记已转赠送"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE managed_campaigns
            SET dormancy_converted_at = NOW(),
                status = 'dormancy_converted'
            WHERE id = %s
            """,
            (campaign_id,),
        )


# ==================== 撤回未发文章 ====================

def get_recent_pending_for_withdraw(
    campaign_id: int,
    article_ids: list[int],
    within_hours: int = 24,
) -> list[dict]:
    """读取可撤回的未发文章（campaign 内 + 24h 内 + status=pending）"""
    if not article_ids:
        return []
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM pending_review_articles
            WHERE campaign_id = %s
              AND id = ANY(%s::integer[])
              AND status = 'pending'
              AND created_at >= NOW() - (INTERVAL '1 hour' * %s)
            """,
            (campaign_id, list(article_ids), within_hours),
        )
        rows = cursor.fetchall() or []
        results = []
        for r in rows:
            d = _to_dict(r)
            d["estimated_publish_cost_yuan"] = _decimal_to_float(d.get("estimated_publish_cost_yuan", 0))
            results.append(d)
        return results
    finally:
        conn.close()
