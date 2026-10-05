"""标题生成受理凭据 · 先落库再生成(2026-07-28 P0)

工单:docs/AI-CONTEXT/WORKORDER_ATTRIBUTION_FIX_2026-07-28.md §5.5(T5)

事故:QZQZ(quote 386)写 12 篇 → `topics` 表**从未有过这批行**。失败发生在
标题生成阶段(topics 落库之前),整批任务无痕蒸发 —— 无行、无错误码、无失败阶段、
前端一片空白。`topics` 本就有 `generation_error_code` / `generation_failure_phase` /
`generation_refund_status` 全套字段,只因行都没创建而无从写起。

修法(不新造结构,只用既有列):
  1. 进 LLM **之前**先为每个关键词落一行 `topics`:`status='pending'`、
     `quote_id`、`original_keyword`、`generation_request_id`、
     `generation_operation='title_batch'`,`optimized_title` 留空;
  2. 生成成功 → `save_topics_batch` 照旧写正式行(flat 模式它本就会 DELETE 掉
     `status IN ('draft','pending','regenerating')` 的旧行,凭据自然被顶掉);
     残留的未被顶掉的凭据由 `clear_reservations` 显式清掉,两种模式都不留孤儿;
  3. 生成失败(抛异常 / LLM 零产出 / 写库失败)→ 凭据就地更新为
     `status='failed'` + 错误码 + 失败阶段,**整批不再蒸发**,前端看得见、可重试。

🔴 边界(工单 §5.5-4 明令):
  - **扣费 / 退费口径一条不动** —— 本模块不 import billing,不碰任何积分;
  - 管理员免单路径不动 —— 本模块不判身份、不看 agent_level;
  - 凭据行 `optimized_title IS NULL`,不是"成品",不参与任何交付计数。
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("GEO-TopicReservation")

# 受理凭据的 generation_operation 值(既有列,新取值)。
TITLE_RESERVATION_OPERATION = "title_batch"

# 凭据阶段的失败码。沿用既有 ArticleGenerationFailure 契约的形状。
TITLE_PHASE = "title_generation"
TITLE_OUTPUT_PHASE = "title_output"
TITLE_PERSIST_PHASE = "title_persist"

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")


def new_title_request_id() -> str:
    return f"titles-{uuid.uuid4().hex}"


def normalize_request_id(value: Optional[str]) -> str:
    """请求编号必须可写入 VARCHAR(128) 且格式受控;非法/缺失时自动生成。"""
    candidate = str(value or "").strip()
    if candidate and _REQUEST_ID_RE.fullmatch(candidate):
        return candidate
    return new_title_request_id()


def _keyword_rows(keywords: Iterable[Any]) -> List[Dict[str, Any]]:
    """把项目详情里的 keyword 列表规整成 (keyword_id, original_keyword)。

    只认既有形状:dict 带 `id`/`keyword_id` 与 `keyword`/`original_keyword`。
    认不出来的条目**不丢弃** —— 丢弃就等于又制造一次静默蒸发,
    改为落一行 keyword_id=NULL 的凭据,让它在前端可见。
    """
    rows: List[Dict[str, Any]] = []
    for item in keywords or []:
        if isinstance(item, dict):
            keyword_id = item.get("id", item.get("keyword_id"))
            text = item.get("keyword") or item.get("original_keyword") or ""
        else:
            keyword_id, text = None, str(item or "")
        try:
            keyword_id = int(keyword_id) if keyword_id is not None else None
        except (TypeError, ValueError):
            keyword_id = None
        rows.append({"keyword_id": keyword_id, "original_keyword": str(text)[:500]})
    return rows


def reserve_title_slots(
    quote_id: int,
    keywords: Iterable[Any],
    request_id: str,
    *,
    cursor=None,
) -> List[int]:
    """进 LLM 前先落受理凭据。返回凭据 topic id 列表。

    幂等:同一 ``generation_request_id`` 已有凭据则原样返回,不重复落行。
    落库失败不抛异常(返回空列表)—— 凭据是可观测性增强,绝不能反过来
    把一次本可成功的生成打掉。
    """
    rows = _keyword_rows(keywords)
    if not rows:
        return []

    own_conn = None
    if cursor is None:
        from db.diagnosis_db import get_connection

        own_conn = get_connection()
        cursor = own_conn.cursor()
    try:
        cursor.execute(
            """SELECT id FROM topics
               WHERE quote_id=%s AND generation_request_id=%s
               ORDER BY id""",
            (int(quote_id), str(request_id)),
        )
        existing = [int(r["id"] if isinstance(r, dict) else r[0]) for r in (cursor.fetchall() or [])]
        if existing:
            return existing

        # [WO_225-c1 §8.3] 凭据行也写桶。
        #   🔴 凭据本身**不占槽**(status='pending',见本模块抬头),写桶不改变这一点;
        #      写的理由是凭据会**就地**转成 completed/failed —— 转态时不再有口径可查,
        #      那时才补桶等于事后猜。建时写下,转态时原样留着。
        from services.media_slot_conversion import plan_buckets
        _buckets = plan_buckets(quote_id, len(rows), cursor=cursor)
        reserved: List[int] = []
        for _bidx, row in enumerate(rows):
            cursor.execute(
                """INSERT INTO topics (
                       keyword_id, quote_id, original_keyword, optimized_title,
                       status, generation_request_id, generation_operation, media_bucket
                   ) VALUES (%s, %s, %s, NULL, 'pending', %s, %s, %s)
                RETURNING id""",
                (
                    row["keyword_id"], int(quote_id), row["original_keyword"],
                    str(request_id), TITLE_RESERVATION_OPERATION,
                    row.get("media_bucket") or (
                        _buckets[_bidx] if _bidx < len(_buckets) else None),
                ),
            )
            got = cursor.fetchone()
            if got is not None:
                reserved.append(int(got["id"] if isinstance(got, dict) else got[0]))
        if own_conn is not None:
            own_conn.commit()
        logger.info(
            "[title-reservation] quote=%s request=%s 已受理 %s 条",
            quote_id, request_id, len(reserved),
        )
        return reserved
    except Exception as exc:  # noqa: BLE001 — 凭据失败不许打掉生成
        if own_conn is not None:
            try:
                own_conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        logger.error(
            "[title-reservation] 落受理凭据失败 quote=%s request=%s: %s",
            quote_id, request_id, exc,
        )
        return []
    finally:
        if own_conn is not None:
            try:
                own_conn.close()
            except Exception:  # noqa: BLE001
                pass


def fail_title_reservations(
    quote_id: int,
    request_id: str,
    *,
    error_code: str,
    error_message: str,
    phase: str,
    retryable: bool = True,
    keyword_ids: Optional[Iterable[Any]] = None,
) -> int:
    """把本次请求还挂着的受理凭据就地标记为失败。

    只动 ``status='pending'`` 且 ``article_id IS NULL`` 且属于本 request 的行 ——
    绝不碰别人的 topic、绝不碰已成稿的行。返回被标记的条数。

    ``keyword_ids``([标题 AI-only 2026-08-17] 新增,默认 None = 旧行为逐字不变):
    只标记这些关键词的凭据。**部分失败**场景要用它 —— 整批标记会把
    生成成功的关键词也标成失败,那比不标还糟。
    """
    from db.diagnosis_db import get_connection

    scoped_ids: Optional[List[int]] = None
    if keyword_ids is not None:
        scoped_ids = []
        for value in keyword_ids:
            try:
                scoped_ids.append(int(value))
            except (TypeError, ValueError):
                continue
        if not scoped_ids:
            # 传了空集合 = 没有要标记的行。绝不退化成"标记全部"。
            return 0

    # 🔴 默认路径的 SQL 与参数**逐字不变**:限定条件是**追加**上去的,不是把原
    # 谓词改写成三元式。改写会让"没传 keyword_ids"这条老路也走上新语法 ——
    # 本仓的既有锁用替身 cursor 解析谓词,当场就把这种"顺手改老路"抓了出来。
    sql = """UPDATE topics
                  SET status='failed',
                      generation_error_code=%s,
                      generation_error_message=%s,
                      generation_retryable=%s,
                      generation_failure_phase=%s,
                      fail_reason=%s
                WHERE quote_id=%s
                  AND generation_request_id=%s
                  AND status='pending'
                  AND article_id IS NULL"""
    params: List[Any] = [
        str(error_code)[:80], str(error_message)[:2000], bool(retryable),
        str(phase)[:32], str(error_message)[:2000],
        int(quote_id), str(request_id),
    ]
    if scoped_ids is not None:
        sql += "\n                  AND keyword_id = ANY(%s)"
        params.append(scoped_ids)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        changed = cur.rowcount
        conn.commit()
        logger.warning(
            "[title-reservation] quote=%s request=%s 标记失败 %s 条 code=%s phase=%s",
            quote_id, request_id, changed, error_code, phase,
        )
        return changed
    except Exception as exc:  # noqa: BLE001 — 标记失败不许再往上抛,原始异常更重要
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.error(
            "[title-reservation] 标记失败态失败 quote=%s request=%s: %s",
            quote_id, request_id, exc,
        )
        return 0
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def clear_title_reservations(
    quote_id: int, request_id: str, *, cursor=None
) -> int:
    """生成成功后清掉残留的空壳受理凭据。

    两条,都只删"空壳"(**没有标题**、没有成稿、operation='title_batch'):

    1. **本次请求**还挂着的 pending 凭据 —— 没被正式行顶掉的那些;
    2. **此前失败**的 failed 凭据(别的 request_id)—— 本次成功已经把它们顶替掉了,
       留着只会让用户在写作大厅同时看到"新标题"和一堆过期的失败条目。
       刻意只扫 failed(终态),**绝不碰别的 request 的 pending** ——
       那可能是同一 quote 上另一批正在跑的任务,删了就又是一次凭空蒸发。

    带标题的正式行、已挂成稿的行一律不动。
    """
    own_conn = None
    if cursor is None:
        from db.diagnosis_db import get_connection

        own_conn = get_connection()
        cursor = own_conn.cursor()
    try:
        cursor.execute(
            """DELETE FROM topics
                WHERE quote_id=%s
                  AND generation_request_id=%s
                  AND generation_operation=%s
                  AND status='pending'
                  AND optimized_title IS NULL
                  AND article_id IS NULL""",
            (int(quote_id), str(request_id), TITLE_RESERVATION_OPERATION),
        )
        removed = cursor.rowcount
        cursor.execute(
            """DELETE FROM topics
                WHERE quote_id=%s
                  AND generation_request_id<>%s
                  AND generation_operation=%s
                  AND status='failed'
                  AND optimized_title IS NULL
                  AND article_id IS NULL""",
            (int(quote_id), str(request_id), TITLE_RESERVATION_OPERATION),
        )
        removed += cursor.rowcount
        if own_conn is not None:
            own_conn.commit()
        return removed
    except Exception as exc:  # noqa: BLE001
        if own_conn is not None:
            try:
                own_conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        logger.error(
            "[title-reservation] 清理残留凭据失败 quote=%s request=%s: %s",
            quote_id, request_id, exc,
        )
        return 0
    finally:
        if own_conn is not None:
            try:
                own_conn.close()
            except Exception:  # noqa: BLE001
                pass


def failure_from_exception(exc: BaseException | None, *, phase: str = TITLE_PHASE):
    """复用既有 ArticleGenerationFailure 契约,给标题阶段一套稳定错误码。"""
    from writing.article_generation_failure import (
        ArticleGenerationFailure,
        classify_article_generation_failure,
    )

    classified = classify_article_generation_failure(exc, phase=phase)
    # 分类器是为正文阶段写的;标题阶段沿用它的 retryable 判断与文案,
    # 但把 phase 钉回标题阶段,免得前端把标题失败显示成"正文保存失败"。
    return ArticleGenerationFailure(
        classified.code, classified.message, classified.retryable, phase
    )
