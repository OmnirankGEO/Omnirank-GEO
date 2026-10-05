"""
P0.4 发布事实源收束薄服务(CTO-15.7 2026-04-24)

触发:538 篇已生成文章 · 真发布 0 篇 · 代理缺"标记已发布"入口。

Codex 修订版 · 复用现有 publish_orders / publish_order_items / media_publications 5 张表:
 · media_publications 已有 article_id / screenshot_path 字段(无需 migration)
 · 本薄服务只做协调 · 不拥有新事实源

老板 §10 批保留 1 字段:
 · articles 加 first_published_at TIMESTAMP · denormalized 查询加速
 · 事实源仍是 media_publications · first_published_at 仅用于 O(1) dashboard 指标

职责:
 · record_manual_publication(quote_id, article_id, platform_name, platform_url, operator_id, screenshot_path)
   - 查重:同 (quote_id, article_id, platform_url) 已存在 → return existing id
   - 写入:调 db.monitoring_db.add_publication(扩参版)
   - 冗余回写:articles.first_published_at = COALESCE(first_published_at, NOW())(仅首次)
   - 不处理:publish_order_items(代发订单仍由外部发布通道同步负责)
"""
from __future__ import annotations
import logging
from typing import Optional, TypedDict

logger = logging.getLogger("GEO-PublicationFacts")


class PublicationRecord(TypedDict):
    publication_id: int
    is_new: bool  # True=新建 · False=已存在(幂等)
    first_published: bool  # True=articles 首次标记


def record_manual_publication(
    quote_id: int,
    platform_name: str,
    platform_url: str,
    *,
    article_id: Optional[int] = None,
    operator_id: Optional[str] = None,
    screenshot_path: Optional[str] = None,
    article_title: Optional[str] = None,
    publish_date: Optional[str] = None,
) -> PublicationRecord:
    """
    代理手动确认"已发布" · 写 media_publications + 冗余回写 articles.first_published_at

    幂等规则:同 (quote_id, article_id, platform_url) 不重复 insert

    Args:
        quote_id: 必 · 报价 ID
        platform_name: 必 · 平台名("小红书"/"知乎" 等)
        platform_url: 必 · 公开可访问的发布 URL
        article_id: 可选 · 关联 articles.id(有值时触发 first_published_at 回写)
        operator_id: 操作人 user_id(string)
        screenshot_path: 可选 · OSS 截图路径(omnirank-publish-evidence bucket · 老板批)
        article_title / publish_date: 可选 · 补充信息

    Returns:
        PublicationRecord(publication_id, is_new, first_published)
    """
    if not quote_id or not platform_name or not platform_url:
        raise ValueError("record_manual_publication: quote_id / platform_name / platform_url 必填")

    from db.connection import get_connection as get_db_conn
    from db.monitoring_db import add_publication

    # 查重
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id FROM media_publications
            WHERE quote_id = %s
              AND platform_url = %s
              AND (article_id = %s OR (article_id IS NULL AND %s IS NULL))
            LIMIT 1
            """,
            (quote_id, platform_url, article_id, article_id),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            existing_id = row["id"] if not isinstance(row, tuple) else row[0]
            logger.info(
                f"[publication_facts] 已存在 · quote_id={quote_id} article_id={article_id} "
                f"url={platform_url[:60]} · 返现有 id={existing_id}"
            )
            return {
                "publication_id": existing_id,
                "is_new": False,
                "first_published": False,
            }
    except Exception as e:
        logger.warning(f"[publication_facts] 查重异常(忽略,继续写入): {e}")

    if article_id:
        from services.article_review_gate import assert_publication_eligible

        assert_publication_eligible(int(article_id))
        conn_article = get_db_conn()
        try:
            cur_article = conn_article.cursor()
            cur_article.execute(
                """
                SELECT a.id
                FROM articles a
                LEFT JOIN topics t ON t.id = a.topic_id
                WHERE a.id = %s
                  AND (
                        a.quote_id = %s
                        OR (a.quote_id IS NULL AND t.quote_id = %s)
                      )
                LIMIT 1
                """,
                (article_id, quote_id, quote_id),
            )
            article_row = cur_article.fetchone()
            conn_article.close()
            if not article_row:
                raise ValueError("record_manual_publication: article_id 不属于 quote_id，拒绝跨客户发布记录")
        except Exception:
            try:
                conn_article.close()
            except Exception:
                pass
            raise

    # 写入(调扩参版 add_publication)
    pub_id = add_publication(
        quote_id=quote_id,
        platform_name=platform_name,
        platform_url=platform_url,
        article_title=article_title,
        publish_date=publish_date,
        operator_id=operator_id,
        article_id=article_id,
        screenshot_path=screenshot_path,
    )

    # Manual registration proves an operator recorded a URL, not which body was
    # submitted at the real publication time.  Preserve the operational marker,
    # but never create an effect-eligible body snapshot from current editable text.
    first_pub_flag = False
    if article_id:
        try:
            conn2 = get_db_conn()
            cur2 = conn2.cursor()
            cur2.execute(
                """
                UPDATE articles
                   SET first_published_at = COALESCE(first_published_at, NOW())
                 WHERE id = %s
                   AND first_published_at IS NULL
                RETURNING id
                """,
                (int(article_id),),
            )
            first_pub_flag = cur2.fetchone() is not None
            conn2.commit()
            conn2.close()
        except Exception as e:
            # articles 表可能缺 first_published_at 字段(migration 未跑)· 降级不中断
            logger.warning(f"[publication_facts] articles.first_published_at 回写失败(迁移未跑?): {e}")

    logger.info(
        f"[publication_facts] 新建 · pub_id={pub_id} quote_id={quote_id} "
        f"article_id={article_id} first={first_pub_flag} "
        f"url={platform_url[:60]}"
    )

    # A8 · 文章→监测 hook(CTO-15.9 2026-04-25 · 轻量版)
    # 首次发布(first_pub_flag=True)· 发 notification 提醒代理"24h 后可看监测效果"
    # 不直接触发 monitor(会扣费 · 代理主动决定)
    if first_pub_flag:
        try:
            from db.connection import get_connection as _gc
            from services.notification_events import NotificationEventType
            from services.notification_outbox import enqueue_brand_owner_notification_event_durable
            # 查 brand_id；通知正文不写公开 URL、账号或内部流水。
            conn3 = _gc()
            try:
                cur3 = conn3.cursor()
                cur3.execute("SELECT brand_id FROM quotes WHERE id = %s", (quote_id,))
                qrow = cur3.fetchone()
            finally:
                conn3.close()
            if qrow and qrow.get("brand_id"):
                from datetime import datetime, timezone
                enqueue_brand_owner_notification_event_durable(
                    brand_id=int(qrow["brand_id"]),
                    event_type=NotificationEventType.PUBLICATION_FIRST_RECORDED,
                    business_id=f"manual_publication:{int(pub_id)}",
                    terminal_state="recorded",
                    facts={
                        "business_no": f"PUB-{int(pub_id)}",
                        "status": "首次发布事实已记录",
                        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "summary": "建议在 24 小时后进入效果监测查看变化。",
                    },
                )
                logger.info(f"[publication_facts] A8 hook · 首发通知已发 pub_id={pub_id}")
        except Exception as e:
            logger.exception("[publication_facts] 首发通知写入 outbox 失败: %s", e)

    # B2.2 (CTO-15.9 session 3 · 2026-04-25 · M1a 主链断点 2)
    # 发布 → 自动追加 monitoring_keywords(extra_keywords)
    # 规则:
    #   1. 取 article 关联 topic.original_keyword
    #   2. 检查该 brand 在 confirmed_keywords + extra_keywords 是否已有此词
    #   3. 若都没有 · 自动 INSERT extra_keywords(免费 · 不扣 monitoring 费)
    #   4. 一次只追加 1 词 · 不批量扫描历史(避免重复 N 次发布反复 INSERT)
    # 老板铁律:不直接触发 monitor 任务(会扣费)· 仅入池 · 等下次 daily monitor 自动覆盖
    if article_id:
        try:
            from db.connection import get_connection as _gc
            from db.monitoring_db import add_keyword

            conn4 = _gc()
            cur4 = conn4.cursor()
            # 查 article → topic → original_keyword
            cur4.execute("""
                SELECT t.original_keyword, q.brand_id, q.brand_name, q.id as quote_id
                FROM articles a
                JOIN topics t ON t.id = a.topic_id
                JOIN quotes q ON q.id = t.quote_id
                WHERE a.id = %s LIMIT 1
            """, (article_id,))
            arow = cur4.fetchone()

            if arow and arow.get("original_keyword") and arow.get("brand_id"):
                target_kw = arow["original_keyword"].strip()
                target_brand = arow.get("brand_name") or ""
                _brand_id = arow["brand_id"]

                # 检查 confirmed_keywords + extra_keywords 是否已有
                cur4.execute("""
                    SELECT 1 FROM confirmed_keywords ck
                    JOIN quotes q ON ck.quote_id = q.id
                    WHERE q.brand_id = %s AND ck.keyword = %s
                    LIMIT 1
                """, (_brand_id, target_kw))
                already_in_confirmed = cur4.fetchone() is not None

                cur4.execute("""
                    SELECT 1 FROM extra_keywords e
                    JOIN quotes q ON e.quote_id = q.id
                    WHERE q.brand_id = %s AND e.keyword = %s AND e.status = 'active'
                    LIMIT 1
                """, (_brand_id, target_kw))
                already_in_extra = cur4.fetchone() is not None

                conn4.close()

                if not already_in_confirmed and not already_in_extra:
                    new_kw_id = add_keyword(
                        brand_id=_brand_id,
                        client_id=str(_brand_id),
                        keyword=target_kw,
                        target_brand=target_brand,
                        difficulty="中等",
                    )
                    logger.info(
                        f"[publication_facts] B2.2 hook · 已自动入监测池 · "
                        f"extra_keyword_id={new_kw_id} brand_id={_brand_id} kw={target_kw[:30]}"
                    )
                else:
                    logger.info(
                        f"[publication_facts] B2.2 hook · 词已在监测池 · 跳过 · "
                        f"brand_id={_brand_id} kw={target_kw[:30]}"
                    )
            else:
                try:
                    conn4.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[publication_facts] B2.2 hook 自动入监测池失败(非阻塞): {e}")

    # M1a T4 publish 主路径埋点(CTO-15.23 2026-05-25 C 方案 A2)
    # · 事实源埋点 · 覆盖任何调用 record_manual_publication 的端点(server.py:3592 已有冗余 · funnel 按 brand 去重不影响)
    # · prod 主路径"代理自助发布"如果直接调本函数(不走 /api/publications/manual)· 这里兜底
    try:
        from db.connection import get_connection as _gc_log
        from db.pipeline_stage_log_db import log_stage_event
        _conn_log = _gc_log()
        _cur_log = _conn_log.cursor()
        _cur_log.execute("SELECT brand_id FROM quotes WHERE id = %s", (quote_id,))
        _brand_row = _cur_log.fetchone()
        _conn_log.close()
        _brand_id_log = _brand_row["brand_id"] if _brand_row else None
        if _brand_id_log:
            _operator_int = int(operator_id) if operator_id and str(operator_id).isdigit() else None
            log_stage_event(
                brand_id=_brand_id_log,
                stage_name="publish",
                event="complete",
                meta={
                    "publication_id": pub_id,
                    "quote_id": quote_id,
                    "article_id": article_id,
                    "platform_name": platform_name,
                    "source": "publication_facts",
                    "first_published": first_pub_flag,
                },
                actor_user_id=_operator_int,
            )
    except Exception as _le:
        logger.warning(f"[publication_facts] stage_log 埋点失败(非阻塞): {_le}")

    return {
        "publication_id": pub_id,
        "is_new": True,
        "first_published": first_pub_flag,
    }


def get_brand_publish_progress(brand_id: int) -> dict:
    """
    查品牌文章发布进度(O(1) 走 articles.first_published_at 索引 · 无需 JOIN)

    Returns:
        {total, published, not_published, pct}
    """
    from db.connection import get_connection as get_db_conn
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE first_published_at IS NOT NULL) AS published
            FROM articles a
            JOIN topics t ON t.id = a.topic_id
            WHERE t.brand_id = %s
            """,
            (brand_id,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return {"total": 0, "published": 0, "not_published": 0, "pct": 0.0}
        total = row["total"] if not isinstance(row, tuple) else row[0]
        published = row["published"] if not isinstance(row, tuple) else row[1]
        pct = round(100.0 * published / total, 1) if total else 0.0
        return {
            "total": total,
            "published": published,
            "not_published": total - published,
            "pct": pct,
        }
    except Exception as e:
        # first_published_at 字段未迁移时降级
        logger.warning(f"[publication_facts] get_brand_publish_progress 降级(迁移未跑?): {e}")
        return {"total": 0, "published": 0, "not_published": 0, "pct": 0.0}


# ---------------------------------------------------------------------------
# WP7 · quote-scoped 六阶段投影的转发入口(规格 03 §10)
#
# 规格写的是「复用 `publication_facts.py` 的 quote-scoped stage projection」。
# 实现放在 `publication_stage_projection` / `_sources` / `_adapters` 三件套里
# (本文件是**写入侧**的人工登记器,把 400 行只读投影塞进来会让两种职责纠缠),
# 这里只做转发 —— 按规格从本模块 import 的调用方拿到的就是同一个 canonical 实现。
# ---------------------------------------------------------------------------
from services.publication_stage_adapters import (  # noqa: E402,F401
    brand_quote_projections,
    frozen_report_body,
    quote_has_published_occurrence,
    quote_published_active,
    quote_service_completion,
    quote_stage_tuple,
    stage_labels,
)
from services.publication_stage_projection import STAGES  # noqa: E402,F401
