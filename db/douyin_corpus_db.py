"""抖音「被采纳内容」语料库 · 读写

给选题蒸馏器提供 few-shot 样本。表由 migration_022 建,数据由
`scripts/research/douyin_corpus_ingest.py` 离线灌(调 TIKHUB)。

🔴 取样顺序是 Review 硬要求(§18a ①):**同行业图文帖优先**。
   降级顺序写在 `load_fewshot` 里,并且每一档都**如实回报降级原因** ——
   悄悄用视频样本去教模型写图文帖,产出会走形而没人知道为什么。
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

logger = logging.getLogger("GEO-Douyin-Corpus")

# few-shot 里单条 caption 的截断长度。
# 🔴 不是省 token:超长的那几条(生产最长 1029 字)会把整个 prompt 的重心压过去,
#    而实证中位只有 70 字 —— 喂进去的样本分布本身就该像真实分布。
CAPTION_MAX = 320


def upsert_corpus_row(*, aweme_id: str, industry_key: str, aweme_type: Optional[int],
                      is_image_post: bool, image_count: int, caption: str,
                      hashtags: Optional[List[str]] = None,
                      digg_count: Optional[int] = None,
                      source_url: str = "") -> None:
    """离线采集写入。同一条 aweme 再采一次就覆盖(内容会变,比如作者改了文案)。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO douyin_adopted_corpus
                   (aweme_id, industry_key, aweme_type, is_image_post,
                    image_count, caption, hashtags, digg_count, source_url)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
               ON CONFLICT (aweme_id) DO UPDATE SET
                   industry_key = EXCLUDED.industry_key,
                   aweme_type = EXCLUDED.aweme_type,
                   is_image_post = EXCLUDED.is_image_post,
                   image_count = EXCLUDED.image_count,
                   caption = EXCLUDED.caption,
                   hashtags = EXCLUDED.hashtags,
                   digg_count = EXCLUDED.digg_count,
                   source_url = EXCLUDED.source_url,
                   collected_at = NOW()""",
            (str(aweme_id), str(industry_key or "general"), aweme_type,
             bool(is_image_post), int(image_count or 0), str(caption or ""),
             json.dumps(list(hashtags or []), ensure_ascii=False),
             digg_count, str(source_url or "")),
        )
        conn.commit()
    finally:
        conn.close()


def _rows(sql: str, params: tuple) -> List[dict]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _select(where: str, params: tuple, limit: int) -> List[dict]:
    """🔴 截断在 **Python 侧**做,不在 SQL 里做。

    原来这里写的是 `left(caption, 320) AS caption`。2026-08-03 用真实语料
    (12 条生产「被采纳」条目经 TIKHUB 采回)实跑,发现某些 caption 上
    `left()` / `substr()` 会**服务端报错**:

        ERROR:  invalid byte sequence for encoding "UTF8": 0xe7 0x84

    同一行整取回来与源串**逐字节相同**(octet_length 2458 = 源串 utf-8 长度,
    往返比对 True),`left()` 对等价字面量也正常 —— 所以既不是数据脏,
    也不是 left() 全面坏掉。**我没能查清它为什么在这一行上会切错**,
    只能确认可稳定复现(psql 里直接跑也报,与客户端无关)。

    但不管成因是什么,修法是确定的:Python 的字符串切片按码点切,
    **构造上不可能**切出半个字符。顺带把一个 f-string 拼进 SQL 的整数也去掉了。

    症状有多难发现:`load_fewshot` 只 try 了第一条查询,失败被吞成
    `corpus_unavailable` —— 于是"表里明明有 12 条语料,蒸馏器却一直报
    没有语料"。不真跑一遍数据是看不出来的。
    """
    rows = _rows(
        f"""SELECT aweme_id, industry_key, is_image_post, image_count,
                   caption, digg_count
              FROM douyin_adopted_corpus
             WHERE {where}
             ORDER BY collected_at DESC
             LIMIT %s""",
        params + (max(1, int(limit)),),
    )
    for r in rows:
        r["caption"] = str(r.get("caption") or "")[:CAPTION_MAX]
    return rows


def load_fewshot(industry_key: str, limit: int = 6) -> tuple[List[dict], str]:
    """取 few-shot 样本。返回 (样本, 降级说明)。降级说明为空串 = 没降级。

    取样顺序(Review §18a ① 硬要求):
      1. 同行业 **图文帖**            ← 最贴,首选
      2. 同行业 视频(形态不同但选题/语感同源)
      3. 跨行业 图文帖(形态对,行业不对)
      4. 什么都没有 → 返回空 + 说明,由调用方决定要不要继续

    🔴 每一档都回报降级原因。「悄悄降级」比「没有语料」更糟:
       产出走形时没人知道是因为喂进去的样本根本不是图文帖。
    """
    industry = str(industry_key or "general")
    n = max(1, int(limit))

    # 🔴 三条查询**整体**兜住,不是只兜第一条。
    #    原来只有 primary 那条在 try 里,后两条裸奔 —— 于是同一种读失败,
    #    发生在第一条是"优雅降级",发生在第二三条是**未捕获异常 → 蒸馏端点 500**。
    #    2026-08-03 实跑真语料时两种都撞到了(第一条被吞成 corpus_unavailable,
    #    第三条直接抛到调用方)。同一类故障必须有同一种表现,否则排查时会被带偏。
    try:
        primary = _select("industry_key = %s AND is_image_post = TRUE", (industry,), n)
        if len(primary) >= n:
            return primary, ""

        got = list(primary)
        seen = {r["aweme_id"] for r in got}

        same_industry_video = _select(
            "industry_key = %s AND is_image_post = FALSE", (industry,), n - len(got))
        for r in same_industry_video:
            if r["aweme_id"] not in seen:
                got.append(r)
                seen.add(r["aweme_id"])

        if len(got) < n:
            cross = _select("industry_key <> %s AND is_image_post = TRUE", (industry,),
                            n - len(got))
            for r in cross:
                if r["aweme_id"] not in seen:
                    got.append(r)
                    seen.add(r["aweme_id"])
    except Exception as e:  # noqa: BLE001 - 表没建/读失败都不该让蒸馏整个挂掉
        logger.warning("[douyin-corpus] 语料读取失败 industry=%s: %s",
                       industry, str(e)[:160])
        return [], "corpus_unavailable"

    if not got:
        return [], "corpus_empty"
    if not primary:
        return got, "no_same_industry_image_post"
    return got, "partial_same_industry_image_post"


def corpus_stats() -> dict:
    """给运维看的:语料够不够。空表时蒸馏会降级,这个数字是先兆。"""
    try:
        rows = _rows(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE is_image_post) AS image_posts,
                      count(DISTINCT industry_key) AS industries
                 FROM douyin_adopted_corpus""",
            (),
        )
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": str(e)[:160]}
    row = rows[0] if rows else {}
    return {"available": True,
            "total": int(row.get("total") or 0),
            "image_posts": int(row.get("image_posts") or 0),
            "industries": int(row.get("industries") or 0)}
