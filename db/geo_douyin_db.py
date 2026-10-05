"""GEO 抖音图文管线 v1 · 作品库 / 任务状态机 CRUD

schema 见 db/migration_017_geo_douyin_posts_2026_08_01.sql。

🔴 软删除:一律 `deleted_at IS NULL` 过滤,不做物理删除(工单 §5)。
🔴 本模块不做任何扣费/退款判定 —— 资金语义在 middleware/billing 与既有 publish 链,
   这里只存任务与作品的元数据。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from db.connection import get_connection

logger = logging.getLogger("GEO-Douyin-DB")

_POST_FIELDS = """id, brand_id, created_by, industry_key, city, keyword,
                  content_type, title, body_text, hashtags, cards,
                  oss_keys, cover_oss_key, status,
                  publish_order_id, publish_item_ids, publish_status,
                  published_url, published_at,
                  generation_meta, created_at, updated_at,
                  redraw_count, style_key, contact_enabled, closing_stale,
                  aspect_ratio, active_revision_id"""


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def create_post(*, created_by: int, brand_id: Optional[int], keyword: str,
                industry_key: str = "", city: str = "",
                content_type: str = "image_post",
                aspect_ratio: str = "",
                quote_id: Optional[int] = None,
                confirmed_keyword_id: Optional[int] = None) -> int:
    """建作品(draft 态)。返回 post_id。

    aspect_ratio 空 = 走列上的 DEFAULT(即默认画幅),行为与加这个参数之前相同。
    传进来的值已在 API 层经 normalize_aspect_ratio 收敛,库里存不进白名单外的字符串。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO geo_douyin_posts
                   (created_by, brand_id, keyword, industry_key, city, content_type,
                    status, aspect_ratio, quote_id, confirmed_keyword_id)
               VALUES (%s, %s, %s, %s, %s, %s, 'draft', COALESCE(NULLIF(%s,''), '3:4'),
                       %s, %s)
               RETURNING id""",
            (created_by, brand_id, keyword, industry_key or None,
             city or None, content_type, aspect_ratio or '',
             # [#150 §3.2] 手填词两列留 NULL —— NULL 是诚实的"未知",不是缺陷。
             # 🔴 显式写这两列是刻意的:迁移漏跑时 UndefinedColumn 当场抛,
             #    不会退化成"成品建出来了但没记账"。
             quote_id, confirmed_keyword_id),
        )
        post_id = cur.fetchone()["id"]
        conn.commit()
        return int(post_id)
    finally:
        conn.close()


def update_post_content(post_id: int, *, title: str = "", body_text: str = "",
                        hashtags: Optional[list] = None,
                        cards: Optional[list] = None,
                        generation_meta: Optional[dict] = None) -> None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_posts
                  SET title = COALESCE(NULLIF(%s,''), title),
                      body_text = COALESCE(NULLIF(%s,''), body_text),
                      hashtags = COALESCE(%s::jsonb, hashtags),
                      cards = COALESCE(%s::jsonb, cards),
                      generation_meta = COALESCE(%s::jsonb, generation_meta),
                      updated_at = NOW()
                WHERE id = %s AND deleted_at IS NULL""",
            (title, body_text,
             _dumps(hashtags) if hashtags is not None else None,
             _dumps(cards) if cards is not None else None,
             json.dumps(generation_meta, ensure_ascii=False)
             if generation_meta is not None else None,
             post_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_post_assets(post_id: int, *, oss_keys: List[str],
                       cover_oss_key: str = "",
                       cards: Optional[list] = None) -> None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_posts
                  SET oss_keys = %s::jsonb,
                      cover_oss_key = COALESCE(NULLIF(%s,''), cover_oss_key),
                      cards = COALESCE(%s::jsonb, cards),
                      updated_at = NOW()
                WHERE id = %s AND deleted_at IS NULL""",
            (_dumps(oss_keys), cover_oss_key,
             _dumps(cards) if cards is not None else None, post_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_post_status(post_id: int, status: str) -> None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_posts SET status = %s, updated_at = NOW()
                WHERE id = %s AND deleted_at IS NULL""",
            (status, post_id),
        )
        conn.commit()
    finally:
        conn.close()


def bind_publish_result(post_id: int, *, order_id: Optional[int],
                        item_ids: Optional[list] = None,
                        publish_status: str = "",
                        published_url: str = "") -> None:
    """把既有发布链的订单结果挂回作品(本表不自建订单状态机)。

    published_url 是**豆包引用归因锚**(飞轮反查靠它 ↔ source_url)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_posts
                  SET publish_order_id = COALESCE(%s, publish_order_id),
                      publish_item_ids = COALESCE(%s::jsonb, publish_item_ids),
                      publish_status = COALESCE(NULLIF(%s,''), publish_status),
                      published_url = COALESCE(NULLIF(%s,''), published_url),
                      published_at = CASE WHEN NULLIF(%s,'') IS NOT NULL
                                          THEN NOW() ELSE published_at END,
                      updated_at = NOW()
                WHERE id = %s AND deleted_at IS NULL""",
            (order_id, _dumps(item_ids) if item_ids is not None else None,
             publish_status, published_url, published_url, post_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_post(post_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_POST_FIELDS} FROM geo_douyin_posts "
            f"WHERE id = %s AND deleted_at IS NULL",
            (post_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_posts(*, brand_id: Optional[int] = None, created_by: Optional[int] = None,
               tenant_owner_user_id: Optional[int] = None,
               status: str = "", limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    """作品列表。

    🔴 [返工 2026-08-18 · Codex P0-10] 新增 `tenant_owner_user_id` 作用域。
       `created_by` 是**操作者**,不是租户:组织里员工做的作品 `created_by`
       是员工、`tenant_owner_user_id` 是 owner。只按 created_by 过滤时,
       同一租户里 A 做的作品 B 看不见 —— 团队协作场面下"我的作品不见了"。
       反过来 owner 也看不到员工替他做的东西。
       两个参数并存、语义各不相同,**不合并**:合并会让"谁做的"这条审计线消失。
    """
    clauses = ["deleted_at IS NULL"]
    params: List[Any] = []
    if brand_id is not None:
        clauses.append("brand_id = %s")
        params.append(brand_id)
    if created_by is not None:
        clauses.append("created_by = %s")
        params.append(created_by)
    if tenant_owner_user_id is not None:
        # 合同链的行带 tenant_owner_user_id;老链的行该列为 NULL,
        # 用 `COALESCE(tenant_owner_user_id, created_by)` 让两类行走同一条谓词 ——
        # 否则老作品会在租户视图里凭空消失。
        clauses.append("COALESCE(tenant_owner_user_id, created_by) = %s")
        params.append(tenant_owner_user_id)
    if status:
        clauses.append("status = %s")
        params.append(status)
    params.extend([max(1, min(int(limit), 100)), max(0, int(offset))])
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_POST_FIELDS} FROM geo_douyin_posts "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC LIMIT %s OFFSET %s",
            tuple(params),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def read_publication_page(cur, *, brand_id=None, tenant_owner_user_id=None,
                          status="", bucket="all", limit=20, offset=0):
    """Read-side buckets of existing post states; filter BEFORE paging, never write.

    One SQL snapshot supplies both scoped counts and the page. Existing publish
    states remain authoritative; links or generation success cannot imply published.
    """
    allowed = {"all", "unpublished", "inflight", "published", "failed", "unknown", "incomplete"}
    if bucket not in allowed:
        raise ValueError("请选择有效的发布状态后重试")
    clauses, params = ["deleted_at IS NULL"], []
    if brand_id is not None:
        clauses.append("brand_id=%s")
        params.append(brand_id)
    if tenant_owner_user_id is not None:
        clauses.append("COALESCE(tenant_owner_user_id,created_by)=%s")
        params.append(tenant_owner_user_id)
    if status:
        clauses.append("status=%s")
        params.append(status)
    # Empty/partial asset arrays aren't finished material. This is list placement,
    # not a new publication permission or quality gate.
    cur.execute(f"""WITH scoped AS MATERIALIZED (
        SELECT {_POST_FIELDS}, CASE
          WHEN BTRIM(COALESCE(publish_status,'')) IN ('published','measured') THEN 'published'
          WHEN BTRIM(COALESCE(publish_status,'')) IN ('publishing','self_reported_unverified') THEN 'inflight'
          WHEN BTRIM(COALESCE(publish_status,''))='failed' THEN 'failed'
          WHEN BTRIM(COALESCE(publish_status,''))<>'' THEN 'unknown'
          WHEN status='ready' AND active_revision_id IS NOT NULL
            AND jsonb_typeof(oss_keys)='array' AND oss_keys<>'[]'::jsonb
            AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(
              CASE WHEN jsonb_typeof(oss_keys)='array' THEN oss_keys ELSE '[]'::jsonb END) asset
              WHERE jsonb_typeof(asset)<>'string' OR BTRIM(asset #>> '{{}}')='') THEN 'unpublished'
          ELSE 'incomplete' END AS publication_bucket
        FROM geo_douyin_posts WHERE {' AND '.join(clauses)}
      ), counted AS (
        SELECT publication_bucket, COUNT(*) AS n FROM scoped GROUP BY publication_bucket
      ), page AS (
        SELECT * FROM scoped WHERE (%s='all' OR publication_bucket=%s)
        ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s
      ) SELECT COALESCE((SELECT jsonb_agg(to_jsonb(page) ORDER BY created_at DESC,id DESC) FROM page),'[]'::jsonb) AS posts,
        COALESCE((SELECT jsonb_object_agg(publication_bucket,n) FROM counted),'{{}}'::jsonb) AS counts
    """, (*params, bucket, bucket, max(1, min(int(limit),100)), max(0,int(offset))))
    result = dict(cur.fetchone())
    # Restore the driver's datetime shape for the existing pending-confirm reader.
    from datetime import datetime
    for row in result["posts"]:
        for field in ("created_at", "updated_at", "published_at"):
            if isinstance(row.get(field), str):
                row[field] = datetime.fromisoformat(row[field])
    counts = {key: int(result["counts"].get(key,0)) for key in allowed if key != "all"}
    counts["all"] = sum(counts.values())
    result.update(counts=counts, total=counts[bucket], has_more=max(0,int(offset))+len(result["posts"]) < counts[bucket])
    return result


def list_publication_posts(**kwargs):
    conn = get_connection()
    try:
        return read_publication_page(conn.cursor(), **kwargs)
    finally:
        conn.close()


def soft_delete_post(post_id: int, user_id: int) -> bool:
    """软删除。只有作者能删(RBAC 兜底,API 层还会再查品牌可见性)。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_posts SET deleted_at = NOW(), updated_at = NOW()
                WHERE id = %s AND created_by = %s AND deleted_at IS NULL""",
            (post_id, user_id),
        )
        affected = cur.rowcount
        conn.commit()
        return affected > 0
    finally:
        conn.close()


def update_post_text(post_id: int, *, title: Optional[str] = None,
                     body_text: Optional[str] = None,
                     hashtags: Optional[list] = None,
                     created_by: Optional[int] = None,
                     expected_revision_id: Optional[int] = None,
                     check_revision: bool = False) -> Optional[int]:
    """详情页文案编辑区的「保存修改」。

    🔴 与 update_post_content 的区别:那个用 `COALESCE(NULLIF(%s,''), title)`
       —— 传空串等于**不改**。用户在编辑区把正文清空后点保存,那条路径会静默
       把旧正文留着,界面上看着清空了、发出去的还是老文案。
       所以编辑区必须走这条:传 None = 不改,传空串 = 真清空。
    """
    from services.geo_douyin.post_revisions import save_text_revision
    conn = get_connection()
    try:
        cur = conn.cursor()
        if created_by is None:
            cur.execute("SELECT created_by FROM geo_douyin_posts WHERE id=%s", (post_id,))
            row = cur.fetchone()
            if not row:
                raise LookupError("作品不存在")
            created_by = int(row["created_by"])
        revision_id = save_text_revision(cur, geo_post_id=post_id, created_by=created_by,
            title=title, body=body_text, hashtags=hashtags,
            expected_revision_id=expected_revision_id, check_revision=check_revision)
        conn.commit()
        return revision_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bump_redraw_count(post_id: int, limit: int) -> Optional[int]:
    """原子地"占用一次重抽额度"。成功返回占用后的次数,额度已满/作品不存在返回 None。

    🔴 判额度与加计数必须在**同一条 UPDATE 的 WHERE 里**完成 ——
       "先 SELECT 查够不够、再 UPDATE 加一"在并发下会超发(两个请求都读到 9)。
       重抽会真调生图 API 花钱,超发就是白烧钱。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_posts
                  SET redraw_count = redraw_count + 1, updated_at = NOW()
                WHERE id = %s AND deleted_at IS NULL AND redraw_count < %s
                RETURNING redraw_count""",
            (post_id, int(limit)),
        )
        row = cur.fetchone()
        conn.commit()
        return int(row["redraw_count"]) if row else None
    finally:
        conn.close()


def refund_redraw_count(post_id: int) -> None:
    """退回一次重抽额度(生图失败时用)。

    额度是"成功才算用掉"的:生图失败还扣额度,等于用户白丢一次机会。
    带下界保护,不会减成负数(DB 侧另有 CHECK >= 0 兜底)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_posts
                  SET redraw_count = GREATEST(redraw_count - 1, 0), updated_at = NOW()
                WHERE id = %s AND deleted_at IS NULL""",
            (post_id,),
        )
        conn.commit()
    finally:
        conn.close()


def replace_one_card(post_id: int, card_index: int, *, oss_key: str,
                     card_patch: Optional[dict] = None) -> bool:
    """把【第 card_index 张】(0 基)换成新图,**其他卡一个字节都不动**。

    🔴 用 jsonb_set 定点替换,不是"整份 oss_keys 重写" ——
       整份重写时任何一处读旧值的竞态都会把别的卡覆盖回去。
       工单验收清单里"单卡重抽不覆盖他卡"就是这条。
    """
    idx = int(card_index)
    if idx < 0:
        return False
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_posts
                  SET oss_keys = jsonb_set(oss_keys, %s::text[], to_jsonb(%s::text), false),
                      cover_oss_key = CASE WHEN %s = 0 THEN %s ELSE cover_oss_key END,
                      updated_at = NOW()
                WHERE id = %s AND deleted_at IS NULL
                  AND jsonb_array_length(oss_keys) > %s
                RETURNING id""",
            ("{%d}" % idx, oss_key, idx, oss_key, post_id, idx),
        )
        row = cur.fetchone()
        if row and card_patch:
            cur.execute(
                """UPDATE geo_douyin_posts
                      SET cards = CASE
                            WHEN jsonb_array_length(cards) > %s
                            THEN jsonb_set(cards, %s::text[], %s::jsonb, false)
                            ELSE cards END,
                          updated_at = NOW()
                    WHERE id = %s AND deleted_at IS NULL""",
                (idx, "{%d}" % idx,
                 json.dumps(card_patch, ensure_ascii=False), post_id),
            )
        conn.commit()
        return bool(row)
    finally:
        conn.close()


def set_style_key(post_id: int, style_key: str) -> None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_posts SET style_key = %s, updated_at = NOW()
                WHERE id = %s AND deleted_at IS NULL""",
            (style_key or None, post_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_contact_enabled(post_id: int, enabled: bool) -> None:
    """拨「插入联系方式」开关。

    🔴 同时把 closing_stale 置 TRUE ——【开关拨了,那张已经渲染好的收尾卡并不会
       自己变】。置位后前端显式提示"要重抽一次收尾卡才生效",这是诚实做法;
       只改开关不置位 = 界面显示"已插入"而图里根本没有 = 假联动。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_posts
                  SET contact_enabled = %s,
                      closing_stale = TRUE,
                      updated_at = NOW()
                WHERE id = %s AND deleted_at IS NULL
                  AND contact_enabled IS DISTINCT FROM %s""",
            (bool(enabled), post_id, bool(enabled)),
        )
        conn.commit()
    finally:
        conn.close()


def clear_closing_stale(post_id: int) -> None:
    """收尾卡重抽成功后清掉"待重抽"标记。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_posts SET closing_stale = FALSE, updated_at = NOW()
                WHERE id = %s AND deleted_at IS NULL""",
            (post_id,),
        )
        conn.commit()
    finally:
        conn.close()


def list_city_siblings(post_id: int) -> List[Dict[str, Any]]:
    """同一条内容的其他城市版本(同 brand + 同 keyword,城市不同)。

    详情页左下「城市版本」chips 用它 —— 点一下切到对应那条**真作品**,
    不是在当前作品上改个城市名字。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT s.id, s.city, s.status, s.title
                 FROM geo_douyin_posts s
                 JOIN geo_douyin_posts cur ON cur.id = %s
                WHERE s.deleted_at IS NULL
                  AND s.keyword IS NOT DISTINCT FROM cur.keyword
                  AND s.brand_id IS NOT DISTINCT FROM cur.brand_id
                  AND s.created_by = cur.created_by
                ORDER BY (s.id = cur.id) DESC, s.created_at ASC""",
            (post_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def count_today_posts_for_brand(brand_id: int) -> int:
    """频控辅助:该品牌今天已产出多少条(账号维度的日限在发布链侧)。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT count(*) AS c FROM geo_douyin_posts
                WHERE brand_id = %s AND deleted_at IS NULL
                  AND created_at >= date_trunc('day', NOW())""",
            (brand_id,),
        )
        return int(cur.fetchone()["c"])
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# 任务状态机
# ─────────────────────────────────────────────────────────────
#: [#184 d2] 「已经发出去了」的两种终态。新一笔提交**不许**把它们盖回 publishing。
#:   `publish_status` 这一列没有 CHECK 约束(生产 schema 核过),取值域靠代码维持;
#:   所以守卫**同时**看事实(`published_url` 非空)与标签,不只看标签。
PUBLISH_TERMINAL_STATUSES = ("published", "measured")


def latest_publish_reject_reasons(post_ids) -> dict:
    """[#195 c1] 每篇作品**最近一条**发布项的 `reject_reason`。返回 {post_id: reason}。

    🔴 关联走 `source_geo_post_id`(新链口径),不走 `posts.publish_order_id`:
       后者对"一篇多单"只装得下一个,而且它是老链那条线用的。
    🔴 只取**最近一条**:一篇被拒两次时,列表要显示的是最新那次的原话,
       不是第一次的(用户看了会以为没更新)。DISTINCT ON 按 id 降序取首行。
    🔴 空原话按**不存在**处理:供应商有时给空串,那时宁可不显示,
       也不要显示一个空的"失败原因:"——比不显示更像坏了。
    """
    ids = [int(p) for p in (post_ids or [])]
    if not ids:
        return {}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT DISTINCT ON (source_geo_post_id)
                      source_geo_post_id AS post_id, reject_reason
                 FROM mhz_publish_order_items
                WHERE source_geo_post_id = ANY(%s)
                  AND COALESCE(reject_reason, '') <> ''
                ORDER BY source_geo_post_id, id DESC""",
            (ids,),
        )
        return {int(r["post_id"]): str(r["reject_reason"] or "")
                for r in (cur.fetchall() or [])}
    finally:
        conn.close()


def bind_publish_submission(cur, *, post_id: int, order_id: int,
                            item_ids: list) -> bool:
    """[#184 d2] 下单成功后把**观测列**写回作品:谁、哪一单、哪几项、正在发。

    走调用方的 cursor ⇒ 与订单行**同一个事务**:订单建出来了而作品上什么都没有,
    正是今天的现状(服务商在创作中心看还是「未发布」)。

    🔴 守卫进 `WHERE`,不能靠 `COALESCE`:
       `bind_publish_result` 用的是 `COALESCE(NULLIF(%s,''), publish_status)` ——
       传非空就**覆盖**。正常时序(提交在前、回执在后)看不出问题,但**重放/重试**
       时再调一次就会把已经 `published` 的作品倒回 `publishing`,
       而那一列正是服务商用来判断"这篇到底发出去没有"的。
       顺利路径看不见这件事,只有重放臂看得见。

    返回 False = 守卫拦下了(作品已是终态),**不是**错误:调用方照常完成下单。
    """
    cur.execute(
        """UPDATE geo_douyin_posts
              SET publish_order_id = %(order_id)s,
                  publish_item_ids = %(item_ids)s::jsonb,
                  publish_status = 'publishing',
                  updated_at = NOW()
            WHERE id = %(post_id)s
              AND deleted_at IS NULL
              AND COALESCE(published_url, '') = ''
              AND COALESCE(publish_status, '') <> ALL(%(terminal)s)
            RETURNING id""",
        {"order_id": int(order_id),
         "item_ids": _dumps([int(i) for i in (item_ids or [])]),
         "post_id": int(post_id),
         "terminal": list(PUBLISH_TERMINAL_STATUSES)},
    )
    return cur.fetchone() is not None


def create_task_with_generation(*, post_id: int, user_id: int, task_ref: str,
                                freeze_id: Optional[int],
                                progress_total: int) -> tuple[int, int]:
    """[#184 d1] 建任务并**开一代生成**,三步一个事务。返回 `(task_id, generation_epoch)`。

    为什么不是"在 `create_task` 外面再包两步":`create_task` 自己开连接并 `commit`,
    包在外面的 supersede/begin 就落在**另外的事务**里,中间崩一下就留下
    「新任务已插入、代际没开」的作品 —— 那种作品的 `activate_revision` 永远 CAS 零行,
    做完一篇标作废一篇,而且没有任何判据会红。

    🔴 三步的**顺序**不是风格问题(`post_revisions.supersede_active_tasks` 的 docstring
       写死了):034 的 `uq_geo_douyin_task_active_generation`
       (同 post 只允许一个 `pending/running` 且未 superseded 的任务)会让
       "先插新任务再接管旧的"在 INSERT 那一步就 UniqueViolation。

    🔴 顺带修掉一个**今天就在生产上的**缺陷:普通链原来不接管就插任务,
       于是任何残留的在途任务(进程重启/崩在中途留下的孤儿 pending)都会让
       **这条作品此后每一次重绘都撞唯一索引**,而且永远撞。改前实测见交付物。
    """
    from services.geo_douyin.post_revisions import (
        begin_generation, supersede_active_tasks,
    )

    conn = get_connection()
    try:
        cur = conn.cursor()
        supersede_active_tasks(cur, geo_post_id=int(post_id))
        cur.execute(
            """INSERT INTO geo_douyin_post_tasks
                   (post_id, user_id, task_ref, freeze_id, progress_total, status)
               VALUES (%s, %s, %s, %s, %s, 'pending')
               RETURNING id""",
            (post_id, user_id, task_ref, freeze_id, progress_total),
        )
        task_id = int(cur.fetchone()["id"])
        gen = begin_generation(cur, geo_post_id=int(post_id), task_id=task_id)
        conn.commit()
        return task_id, int(gen["generation_epoch"])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_task(*, post_id: int, user_id: int, task_ref: str,
                freeze_id: Optional[int], progress_total: int) -> int:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO geo_douyin_post_tasks
                   (post_id, user_id, task_ref, freeze_id, progress_total, status)
               VALUES (%s, %s, %s, %s, %s, 'pending')
               RETURNING id""",
            (post_id, user_id, task_ref, freeze_id, progress_total),
        )
        task_id = cur.fetchone()["id"]
        conn.commit()
        return int(task_id)
    finally:
        conn.close()


def update_task(task_id: int, *, status: str = "", stage: str = "",
                progress_done: Optional[int] = None, error_msg: str = "",
                result_meta: Optional[dict] = None,
                mark_started: bool = False, mark_finished: bool = False) -> None:
    """更新任务态。

    🔴 error_msg 落【顶层列】而不是埋进 result_meta:埋 jsonb 深处会让面板
       "看起来正常"而实际整批失败(飞轮 _safe_stage 静默 14 天的同型教训)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_post_tasks
                  SET status = COALESCE(NULLIF(%s,''), status),
                      stage = COALESCE(NULLIF(%s,''), stage),
                      progress_done = COALESCE(%s, progress_done),
                      error_msg = COALESCE(NULLIF(%s,''), error_msg),
                      result_meta = COALESCE(%s::jsonb, result_meta),
                      started_at = CASE WHEN %s THEN NOW() ELSE started_at END,
                      finished_at = CASE WHEN %s THEN NOW() ELSE finished_at END,
                      updated_at = NOW()
                WHERE id = %s""",
            (status, stage, progress_done, error_msg,
             json.dumps(result_meta, ensure_ascii=False)
             if result_meta is not None else None,
             mark_started, mark_finished, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def bump_task_progress(task_id: int) -> Optional[int]:
    """一张卡出完 → 进度 +1。返回加完之后的值(拿不到行返回 None)。

    🔴 **必须在 SQL 里自增**,不能"先读出来 +1 再写回去"。
       并发是 8:两张卡几乎同时完成时,读改写会两个都读到 3、都写回 4 → 丢一张。
       这类丢计数不会报错,只会让进度条永远差几张、然后突然跳到 100%。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_post_tasks
                  SET progress_done = COALESCE(progress_done, 0) + 1,
                      updated_at = NOW()
                WHERE id = %s
            RETURNING progress_done""",
            (task_id,),
        )
        row = cur.fetchone()
        conn.commit()
        return int(row["progress_done"]) if row else None
    finally:
        conn.close()


def claim_task_settlement(task_id: int) -> Optional[Dict[str, Any]]:
    """抢占结算权:只有把任务行从 `completing` 改成 `succeeded` 的那一次成功。

    返回 {freeze_id, task_ref} 表示抢到;返回 None 表示别人已经结算过。

    🔴 这是"补齐恰好 commit 一次"的**唯一**保证。补齐可能由系统自动重抽和
       用户手动重抽两条路触发,用户还能并发点 —— 先 SELECT 判"齐了没"再
       UPDATE,两个请求会同时判为齐、同时去 commit,**扣两次钱**。
       判定与占位必须在同一条 SQL 里。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_post_tasks
                  SET status = 'succeeded', stage = 'done',
                      finished_at = NOW(), updated_at = NOW()
                WHERE id = %s AND status = 'completing'
            RETURNING freeze_id, task_ref""",
            (task_id,),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        conn.close()


def set_task_completing(task_id: int, *, progress_done: int) -> None:
    """任务进入「补齐中」:冻结保持不动,既不 commit 也不 release。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_post_tasks
                  SET status = 'completing', stage = 'images',
                      progress_done = %s, updated_at = NOW()
                WHERE id = %s""",
            (max(0, int(progress_done)), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_task_total(task_id: int, total: int) -> None:
    """改进度分母。张数预算可能把实际生成数砍到比用户要的少,
    分母不跟着改,进度条就会停在 5/6 永远走不完。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_post_tasks
                  SET progress_total = %s, updated_at = NOW()
                WHERE id = %s""",
            (max(0, int(total)), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_task_freeze(task_id: int, freeze_id: Optional[int]) -> None:
    """任务行先建、冻结后补 freeze_id。

    为什么要分两步:异步化之后,**冻结失败也必须留下一行可查的记录** ——
    否则用户点了"做这条",余额不够,后台悄悄退出,前端轮询什么也查不到,
    只能一直转圈。任务行是那条"查得到"的凭据。
    🔴 这不改任何计费语义:freeze/commit/release 的调用与顺序一个字没动,
       动的只是**记账行**建得早一点。
    """
    if freeze_id is None:
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_post_tasks
                  SET freeze_id = %s, updated_at = NOW()
                WHERE id = %s""",
            (int(freeze_id), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, post_id, user_id, task_ref, freeze_id, status, stage,
                      progress_done, progress_total, error_msg, result_meta,
                      started_at, finished_at, created_at, updated_at
                 FROM geo_douyin_post_tasks WHERE id = %s""",
            (task_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_latest_tasks_for_posts(post_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """一次取多条作品各自【最新那次】任务。返回 {post_id: task}。

    列表页要给失败条目显示人话原因,而原因在任务表的 error_msg 上。
    🔴 一次查完,不在循环里逐条查 —— 20 条列表逐条查就是 20 次往返。
    🔴 不动 list_posts 的 SQL:那条被多处调用,加 JOIN 风险大于收益。
    """
    ids = [int(p) for p in (post_ids or []) if p]
    if not ids:
        return {}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            # created_at / updated_at 是**卡住判定**的输入(进程重启后任务行
            # 会永远停在 running)。少选这两列 → describe_task_progress 永远
            # 判不出 stalled → 前端永远转圈。
            """SELECT DISTINCT ON (post_id)
                      post_id, status, stage, error_msg, progress_done, progress_total,
                      started_at, finished_at, created_at, updated_at
                 FROM geo_douyin_post_tasks
                WHERE post_id = ANY(%s::bigint[])
                ORDER BY post_id, created_at DESC""",
            (ids,),
        )
        return {int(r["post_id"]): dict(r) for r in cur.fetchall()}
    finally:
        conn.close()


def get_task_by_post(post_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, post_id, user_id, task_ref, freeze_id, status, stage,
                      progress_done, progress_total, error_msg, result_meta,
                      started_at, finished_at, created_at, updated_at
                 FROM geo_douyin_post_tasks
                WHERE post_id = %s ORDER BY created_at DESC LIMIT 1""",
            (post_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# 蒸馏任务(WO-DISTILL-TIMEOUT-ASYNC-2026-08-05)
# schema 见 db/migration_025_geo_douyin_distill_tasks_2026_08_05.sql
#
# 🔴 与 geo_douyin_post_tasks 分开是因为那张表 post_id NOT NULL,
#    而蒸馏发生在还没有作品之前。不是重复造轮子。
# 🔴 本组函数同样**不做任何扣费判定** —— charged 只是审计记录,
#    真正的资金动作在 middleware.billing.charge_on_success。
# ─────────────────────────────────────────────────────────────

_DISTILL_FIELDS = """id, brand_id, user_id, status, stage, error_code, error_msg,
                     keywords_used, result, charged,
                     started_at, finished_at, created_at, updated_at"""


class DistillInFlight(Exception):
    """这个客户已经有一单在飞。由部分唯一索引冲突翻译而来(不是靠先查后插)。"""


def create_distill_task(*, brand_id: int, user_id: int, keywords: List[str]) -> int:
    """建蒸馏任务行。

    🔴 同客户并发提交由 **DB 部分唯一索引** `uq_geo_douyin_distill_inflight` 挡,
       不是"先 SELECT 再 INSERT" —— 那中间隔着网络往返,两个并发请求会双双通过,
       改异步之后那就是两次真调用、两次 130。让 DB 去判,唯一冲突翻译成 DistillInFlight。
    """
    import psycopg2

    conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """INSERT INTO geo_douyin_distill_tasks
                       (brand_id, user_id, keywords_used, status, stage)
                   VALUES (%s, %s, %s::jsonb, 'pending', 'queued')
                   RETURNING id""",
                (int(brand_id), int(user_id), _dumps(list(keywords or []))),
            )
            task_id = int(cur.fetchone()["id"])
            conn.commit()
            return task_id
        except psycopg2.errors.UniqueViolation as e:
            conn.rollback()
            raise DistillInFlight(str(e)) from e
    finally:
        conn.close()


def update_distill_task(task_id: int, *, status: str = "", stage: str = "",
                        error_code: str = "", error_msg: str = "",
                        result: Optional[dict] = None,
                        charged: Optional[bool] = None,
                        mark_started: bool = False,
                        mark_finished: bool = False) -> None:
    """改任务行。只改传进来的字段(空串/None = 不动),避免把别处刚写的值抹掉。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_distill_tasks
                  SET status      = COALESCE(NULLIF(%s, ''), status),
                      stage       = COALESCE(NULLIF(%s, ''), stage),
                      error_code  = COALESCE(NULLIF(%s, ''), error_code),
                      error_msg   = COALESCE(NULLIF(%s, ''), error_msg),
                      result      = COALESCE(%s::jsonb, result),
                      charged     = COALESCE(%s, charged),
                      started_at  = CASE WHEN %s THEN NOW() ELSE started_at END,
                      finished_at = CASE WHEN %s THEN NOW() ELSE finished_at END,
                      updated_at  = NOW()
                WHERE id = %s""",
            (status, stage, error_code, error_msg,
             json.dumps(result, ensure_ascii=False) if result is not None else None,
             charged, bool(mark_started), bool(mark_finished), int(task_id)),
        )
        conn.commit()
    finally:
        conn.close()


def get_distill_task(task_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT {_DISTILL_FIELDS} FROM geo_douyin_distill_tasks
                 WHERE id = %s""",
            (int(task_id),),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_latest_distill_task(brand_id: int) -> Optional[Dict[str, Any]]:
    """这个客户**最近一次成功**的蒸馏结果。

    🔴 为什么需要它:选题原来只活在前端组件 state 里 —— 切一次 tab、刷一次页面,
       花了 130 算力蒸出来的 5 条就永久消失,而结果其实一直躺在这张表里。
       用户能看到的东西不该比我们存下来的少。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT {_DISTILL_FIELDS} FROM geo_douyin_distill_tasks
                 WHERE brand_id = %s AND status = 'succeeded'
                 ORDER BY id DESC LIMIT 1""",
            (int(brand_id),),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def reap_stale_distill_tasks(older_than_seconds: float) -> int:
    """把超龄仍未终态的任务判失败,返回回收条数。

    🔴 为什么必须有:进程被杀 / 协程被硬取消时,后台任务的 finally 可能不执行,
       留下一行永远 running 的记录。而那行会被部分唯一索引认成"在飞" →
       **这个客户从此再也蒸不了**,且是静默的(用户只看到"刚蒸过一次",永远)。
       同型问题在同步版的 `_DISTILL_INFLIGHT` 上已经出现过一次并加了自愈上限,
       落库之后这道自愈也必须跟着落库,不然等于把老 bug 搬进了新表。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_distill_tasks
                  SET status = 'failed', stage = 'done',
                      error_code = COALESCE(NULLIF(error_code, ''), 'stale_reaped'),
                      error_msg = COALESCE(NULLIF(error_msg, ''), '任务超时未完成,已回收'),
                      finished_at = NOW(), updated_at = NOW()
                WHERE status IN ('pending', 'running')
                  AND created_at < NOW() - (%s || ' seconds')::interval
                RETURNING id""",
            (str(int(max(1, older_than_seconds))),),
        )
        n = len(cur.fetchall())
        conn.commit()
        return n
    finally:
        conn.close()


def claim_post_batch(*, created_by: int, request_id: str,
                     brand_id: Optional[int] = None) -> Optional[dict]:
    """认领一次批量下单。**抢到返回 None,没抢到返回既有那一行。**

    🔴 幂等靠**唯一插入**,不靠"先查再插" —— 后者在并发双击下两次都查不到,
       两次都插,两次都下单。ON CONFLICT DO NOTHING 让数据库来裁决谁是第一次。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO geo_douyin_post_batches
                   (created_by, request_id, brand_id)
               VALUES (%s, %s, %s)
               ON CONFLICT (created_by, request_id) DO NOTHING
               RETURNING request_id""",
            (int(created_by), str(request_id), brand_id),
        )
        won = cur.fetchone()
        conn.commit()
        if won:
            return None
        cur.execute(
            """SELECT status, result FROM geo_douyin_post_batches
                WHERE created_by = %s AND request_id = %s""",
            (int(created_by), str(request_id)),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def finish_post_batch(*, created_by: int, request_id: str, result: list) -> None:
    """把逐条结果写回批次台账,供重放原样返回。"""
    import json as _json

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_post_batches
                  SET status = 'done', result = %s::jsonb, completed_at = now()
                WHERE created_by = %s AND request_id = %s""",
            (_json.dumps(result, ensure_ascii=False), int(created_by), str(request_id)),
        )
        conn.commit()
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════
# [WO_204 §1] 图文选题(geo_douyin_topics)
#
# Owner 09-13:「和写作一样:默认把这几个选题写出来,用户可以修改,
# 修改后就按照标题来进行创作。」——半自动,不是托管。
#
# 🔴 状态机与写文章那边不是一回事,别照搬词:
#      pending  待做(可改标题、可删)
#      making   制作中(建单那一刻置,**不可改不可删**)
#      done     已做(带 post_id)
#      failed   做失败(可重来 —— 重来走"再建一次单",不是改状态)
#      archived 归档(本单未用,留给后续"不做了"而不删行)
# ═══════════════════════════════════════════════════════════════════════
_TOPIC_FIELDS = """id, brand_id, confirmed_keyword_id, keyword, city,
                   title, angle, card_outline, source, status,
                   post_id, distill_task_id, distill_index,
                   created_by, created_at, updated_at, edited_at"""


def insert_distilled_topics(*, brand_id: int, created_by: int,
                            distill_task_id: int,
                            topics: List[Dict[str, Any]]) -> int:
    """蒸馏产物逐条落表。返回**本次真正插进去的条数**。

    🔴 幂等靠唯一插入 `(distill_task_id, distill_index)` + ON CONFLICT DO NOTHING,
       不靠"先查有没有再插" —— 后者在重放并发时两次都查不到、两次都插。
    🔴 去重键不用 title:两条选题的标题**可以**合法地相同,
       按 title 去重会静默吞掉第二条,而"落表条数 == 回包条数"那条判据
       会红在一个根本没有缺陷的地方。
    🔴 返回的是 rowcount 不是 len(topics):重放时它是 0,
       调用方据此就能说清"这次没新增" —— 返 len 的话重放看起来像又落了一批。
    """
    rows = [t for t in (topics or []) if str((t or {}).get("title") or "").strip()]
    if not rows:
        return 0
    conn = get_connection()
    try:
        cur = conn.cursor()
        inserted = 0
        for idx, t in enumerate(rows):
            cur.execute(
                """INSERT INTO geo_douyin_topics
                       (brand_id, confirmed_keyword_id, keyword, city,
                        title, angle, card_outline, source, status,
                        distill_task_id, distill_index, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb,
                           'distilled', 'pending', %s, %s, %s)
                   -- 🔴 冲突目标必须把 partial 索引的**谓词一起写上**,
                   --    否则 PG 认不出这把索引:InvalidColumnReference
                   --    「there is no unique or exclusion constraint matching」。
                   --    这条只在真跑时才现形 —— 语法检查与 ast 都看不出来。
                   ON CONFLICT (distill_task_id, distill_index)
                       WHERE distill_task_id IS NOT NULL
                         AND distill_index IS NOT NULL
                   DO NOTHING
                   RETURNING id""",
                (int(brand_id),
                 t.get("confirmed_keyword_id"),
                 str(t.get("keyword") or ""),
                 str(t.get("city") or "")[:32] or None,
                 str(t.get("title") or "").strip(),
                 str(t.get("angle") or ""),
                 _dumps(t.get("card_outline")),
                 int(distill_task_id), idx, int(created_by)),
            )
            if cur.fetchone():
                inserted += 1
        conn.commit()
        return inserted
    finally:
        conn.close()


def get_topic(topic_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT %s FROM geo_douyin_topics WHERE id = %%s" % _TOPIC_FIELDS,
                    (int(topic_id),))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_topics(*, brand_id: int, status: str = "",
                limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """列表。返回 ``{"topics": [...], "total": n}``。

    `status` 为空 = 不过滤(前端"全部")。分页必须带 total,
    否则前端只能靠"这一页少于 limit"猜有没有下一页,而那在边界上永远猜错一次。
    """
    where = ["brand_id = %s"]
    args: List[Any] = [int(brand_id)]
    if status:
        where.append("status = %s")
        args.append(str(status))
    clause = " AND ".join(where)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM geo_douyin_topics WHERE " + clause,
                    tuple(args))
        row = cur.fetchone()
        total = int((row["n"] if isinstance(row, dict) else row[0]) or 0)
        cur.execute(
            "SELECT %s FROM geo_douyin_topics WHERE %s ORDER BY id DESC LIMIT %%s OFFSET %%s"
            % (_TOPIC_FIELDS, clause),
            tuple(args) + (max(1, int(limit)), max(0, int(offset))),
        )
        return {"topics": [dict(r) for r in cur.fetchall()], "total": total}
    finally:
        conn.close()


def create_topic(*, brand_id: int, created_by: int, title: str,
                 keyword: str = "", city: str = "", angle: str = "",
                 card_outline: Optional[list] = None,
                 confirmed_keyword_id: Optional[int] = None) -> int:
    """人手加一条。`source='user'`,`edited_at` 落 now()。

    🔴 手加的题**没有** distill_task_id / distill_index ——
       所以它不受那把幂等唯一索引管(索引是 partial 的)。
       手加两条一模一样的题是用户的自由,不是并发缺陷。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO geo_douyin_topics
                   (brand_id, confirmed_keyword_id, keyword, city, title, angle,
                    card_outline, source, status, created_by, edited_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb,
                       'user', 'pending', %s, now())
               RETURNING id""",
            (int(brand_id), confirmed_keyword_id, str(keyword or ""),
             str(city or "")[:32] or None, str(title or "").strip(),
             str(angle or ""), _dumps(card_outline), int(created_by)),
        )
        new_id = int(cur.fetchone()["id"])
        conn.commit()
        return new_id
    finally:
        conn.close()


#: 改标题 / 删除的结果。调用方按它翻 HTTP 码,不自己再查一遍状态。
TOPIC_NOT_FOUND = "not_found"
TOPIC_LOCKED = "locked"
TOPIC_EMPTY_TITLE = "empty_title"
TOPIC_OK = "ok"


def update_topic_title(*, topic_id: int, title: str) -> str:
    """改标题。只有 `pending` 能改;做中/已做返回 `locked`(调用方翻 409)。

    🔴 守卫写在 **WHERE 里**,不是"先查状态再改":先查再改之间那一瞬,
       别人可以把它建成单。守卫在 WHERE 里时,数据库来裁决谁先到。
    🔴 rowcount==0 有**两种**原因(不存在 / 被锁),必须分开 ——
       都返 404 的话,用户会以为选题被删了,然后再点一次建单。
    """
    clean = str(title or "").strip()
    if not clean:
        # 空标题不是"改成空",是"什么都没填"。落库会让列表出现一行没有标题的题。
        return TOPIC_EMPTY_TITLE
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_topics
                  SET title = %s, source = 'user',
                      edited_at = now(), updated_at = now()
                WHERE id = %s AND status = 'pending'
                RETURNING id""",
            (clean, int(topic_id)),
        )
        hit = cur.fetchone()
        conn.commit()
        if hit:
            return TOPIC_OK
        cur.execute("SELECT status FROM geo_douyin_topics WHERE id = %s", (int(topic_id),))
        row = cur.fetchone()
        return TOPIC_LOCKED if row else TOPIC_NOT_FOUND
    finally:
        conn.close()


def delete_topic(topic_id: int) -> str:
    """删一条。只有 `pending` 能删 —— 做中/已做删掉会让成品成孤儿。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM geo_douyin_topics WHERE id = %s AND status = 'pending' RETURNING id",
            (int(topic_id),))
        hit = cur.fetchone()
        conn.commit()
        if hit:
            return TOPIC_OK
        cur.execute("SELECT status FROM geo_douyin_topics WHERE id = %s", (int(topic_id),))
        row = cur.fetchone()
        return TOPIC_LOCKED if row else TOPIC_NOT_FOUND
    finally:
        conn.close()


def claim_topic_for_production(topic_id: int) -> Optional[Dict[str, Any]]:
    """把一条选题**抢**成 `making`,并返回抢到时的那一行。

    抢不到返回 None —— 调用方翻 409「这条已经在做或做过了」。

    🔴 这是"同一 topic 不许重复建单"的**唯一**依据。
       条件写在 WHERE 里、与 UPDATE 同一条语句 ——
       "先查 status 再建单"在双击下两次都读到 pending,两次都建单,
       而两次都会成功(建单本身没有幂等键),用户被扣两次算力。
    🔴 返回抢到时的**那一行**(RETURNING),不是事后再 SELECT 一次:
       事后那次读到的可能已经被别人改过,标题就不是用户看到的那个了
       (#184 d1b 同族:拿当前值当用户看到的值)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_topics
                  SET status = 'making', updated_at = now()
                WHERE id = %%s AND status = 'pending'
                RETURNING %s""" % _TOPIC_FIELDS,
            (int(topic_id),),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        conn.close()


def finish_topic(*, topic_id: int, post_id: int) -> None:
    """做成了:`done` + 写 post_id。

    两件事必须**同一条 UPDATE** —— 建表的 `ck_geo_douyin_topic_post_shape`
    不允许 done 而 post_id 为空,分两步写会在中间那一瞬违约。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_topics
                  SET status = 'done', post_id = %s, updated_at = now()
                WHERE id = %s AND status = 'making'""",
            (int(post_id), int(topic_id)))
        conn.commit()
    finally:
        conn.close()


def release_topic(*, topic_id: int, failed: bool = True) -> None:
    """建单/制作没成:`failed`(或放回 `pending`)。

    🔴 `post_id` 显式清空:建表的 CHECK 只在 done 上要求非空,
       但留着上一次的 post_id 会让"做失败了"的行点进去是**别人的成品**。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE geo_douyin_topics
                  SET status = %s, post_id = NULL, updated_at = now()
                WHERE id = %s AND status = 'making'""",
            ("failed" if failed else "pending", int(topic_id)))
        conn.commit()
    finally:
        conn.close()
