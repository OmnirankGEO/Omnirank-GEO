"""
P14-v6 (2026-05-27) · 行业注册公共函数

定位:
  geo_research_industries 是行业的唯一权威源 (single source of truth)
  所有写 geo_research_raw / geo_engine_stats / geo_research_articles 的入口
  必须先调 ensure_research_industry(name) · 保证 raw 不会出现 industries 没有的孤儿行业

否则:
  - 跑批 dialog 读 industries 表 · 看不到孤儿行业
  - 引用明细读 raw distinct · 看得到孤儿行业
  - 两边对不上 (review HIGH)

适用调用点:
  - services/placement_service.py::import_research_csv  (CSV 离线导入)
  - scripts/migrate_legacy_csv_to_research_monitor.py    (legacy CSV 迁移)
  - services/research_monitor/round_runner.py 写 raw 前 (跑批入口已经走 industries 表所以隐式 OK)
  - 任何 future 直接 INSERT INTO geo_research_raw 的脚本
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

logger = logging.getLogger("GEO-ResearchMonitor.IndustryRegistry")


def slug_for_name(name: str) -> str:
    """确定性 slug 生成 · md5(name) 前 12 位

    P14-v7 (2026-05-27 review MEDIUM-HIGH fix):
      旧版用 ind_<epoch_ms> · 批量补 orphan 时同毫秒可能撞 slug UNIQUE
      改成 md5 · 同 name 永远同 slug · 幂等 · 不会撞 · 不同 name 撞概率 ~10^-14 可忽略
    格式: ind_<hex12> · 满足 ^[a-z0-9_]+$ 正则
    """
    h = hashlib.md5(name.encode('utf-8')).hexdigest()[:12]
    return f"ind_{h}"


def ensure_research_industry(
    name: str,
    *,
    active: bool = True,
    conn=None,
    return_created: bool = False,
):
    """确保 industries 表里存在 name 的行业 · 返回 id

    幂等:
      - 已存在 → 直接返回 id (不更新 active · 不覆盖人工配置)
      - 不存在 → INSERT (active=参数 · 默认 true · slug auto · sort_order=999 排末尾)

    参数:
      name           : 行业中文名 (必填非空)
      active         : 仅新建时生效 · 默认 true
      conn           : 可选 · 复用调用方 conn (避免 nested transaction). 不传则自取自关.
      return_created : [#2/#5] 默认 False → 返回 int|None(**现有 caller 零改**,签名/返回都不变)。
                       True → 返回 (id, was_created) 元组;was_created **仅当**走 RETURNING-id 的
                       真 INSERT 路径为 True,先 SELECT 命中 / ON CONFLICT re-SELECT(并发败者拿到
                       别人刚 INSERT 的 id)均为 False。调用方据此判「本请求是否真新建了这行业」,
                       避免把 find-or-create 命中他人已建的行业误标为自己新建、进而在自身失败时
                       误软删他人刚付费建的合法行业。

    返回:
      return_created=False → industries.id (int) · 异常/空返 None 并 log
      return_created=True  → (industries.id|None, was_created: bool)
    """
    def _ret(_id, _created: bool):
        return (_id, _created) if return_created else _id

    if not name or not name.strip():
        logger.warning("[ensure_research_industry] 跳过空 name")
        return _ret(None, False)
    name = name.strip()

    own_conn = False
    if conn is None:
        from db.connection import get_connection
        conn = get_connection()
        own_conn = True

    try:
        cur = conn.cursor()
        # 先查(命中 → 已存在 · was_created=False)
        cur.execute("SELECT id FROM geo_research_industries WHERE name = %s", (name,))
        row = cur.fetchone()
        if row:
            return _ret(row['id'] if isinstance(row, dict) else row[0], False)

        # 不存在 · 新增 (sort_order=999 排到现有人工配置的末尾 · 不挤位)
        # slug 用 md5(name) 前 12 位 · 同 name 同 slug · 幂等 · 防 UNIQUE 冲突
        cur.execute(
            """
            INSERT INTO geo_research_industries (name, slug, sort_order, active)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (name) DO NOTHING
            RETURNING id
            """,
            (name, slug_for_name(name), 999, active),
        )
        row = cur.fetchone()
        if row:
            # RETURNING 有值 = 本调用真 INSERT 了这一行 → was_created=True(唯一为真的路径)。
            new_id = row['id'] if isinstance(row, dict) else row[0]
            if own_conn:
                conn.commit()
            logger.info(f"[ensure_research_industry] 新增 industry name={name!r} id={new_id}")
            return _ret(new_id, True)

        # ON CONFLICT 命中 (并发场景) → 再查一次拿 id(是别人 INSERT 的 · was_created=False)
        cur.execute("SELECT id FROM geo_research_industries WHERE name = %s", (name,))
        row = cur.fetchone()
        if row:
            return _ret(row['id'] if isinstance(row, dict) else row[0], False)
        return _ret(None, False)
    except Exception as e:
        logger.exception(f"[ensure_research_industry] 失败 name={name!r}: {type(e).__name__}: {e}")
        if own_conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return _ret(None, False)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def research_row_by_slug(slug: str) -> Optional[dict]:
    """[WO_267] 按 slug 只读取调研行 `{id, name, active}`(行业大类字典 → 调研行的连接键是 slug:
    管理员编辑接口能改 name、不能改 slug)。取不到 / 异常 ⇒ None。"""
    s = (slug or "").strip()
    if not s:
        return None
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, active FROM geo_research_industries WHERE slug = %s", (s,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def ensure_taxonomy_research_row(slug: str, name: str) -> tuple:
    """[WO_267 · Review Q2「付费即点亮」] 付费轮落到行业大类字典指定的那一行:
    不存在 ⇒ 按**字典的 slug**建行(active=true);存在但停用 ⇒ 翻成 active(付了就点亮那一行)。

    返回 `(id|None, was_created, was_activated)` —— 调用方在付费后续步骤失败时据此撤销
    (与 `ensure_research_industry(return_created=True)` 同一套清理语义)。
    不用 `ensure_research_industry`:它按 name 找行、按 `slug_for_name(name)` 建行,
    管理员改过名的行会被它当成「不存在」再建一行。
    """
    s, n = (slug or "").strip(), (name or "").strip()
    if not s or not n:
        return None, False, False
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, active FROM geo_research_industries WHERE slug = %s", (s,))
        row = cur.fetchone()
        if row:
            if row["active"]:
                return int(row["id"]), False, False
            cur.execute("UPDATE geo_research_industries SET active = TRUE, updated_at = NOW() "
                        "WHERE id = %s AND active = FALSE", (row["id"],))
            flipped = cur.rowcount == 1
            conn.commit()
            return int(row["id"]), False, flipped
        cur.execute(
            """
            INSERT INTO geo_research_industries (name, slug, sort_order, active)
            VALUES (%s, %s, 999, TRUE)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (n, s),
        )
        row = cur.fetchone()
        conn.commit()
        if row:
            logger.info(f"[ensure_taxonomy_research_row] 新增 name={n!r} slug={s!r} id={row['id']}")
            return int(row["id"]), True, False
        # 并发 / name 撞了别的 slug:再按 slug 取一次;仍取不到 ⇒ None(调用方按失败处理)
        cur.execute("SELECT id FROM geo_research_industries WHERE slug = %s", (s,))
        row = cur.fetchone()
        return (int(row["id"]) if row else None), False, False
    except Exception as e:
        logger.exception(f"[ensure_taxonomy_research_row] 失败 slug={s!r}: {type(e).__name__}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None, False, False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def backfill_orphan_industries(conn=None) -> dict:
    """补齐 raw/stats/articles 三表中存在但 industries 表缺失的孤儿行业
    返回 {新增的 industry name: id} · 全部补成 active=true

    幂等: 重复跑不会重复插
    """
    own_conn = False
    if conn is None:
        from db.connection import get_connection
        conn = get_connection()
        own_conn = True

    backfilled = {}
    try:
        cur = conn.cursor()
        # 拉所有三表 distinct industry · UNION 去重
        cur.execute("""
            SELECT DISTINCT industry FROM (
                SELECT industry FROM geo_research_raw
                 WHERE industry IS NOT NULL AND industry <> ''
                UNION
                SELECT industry FROM geo_engine_stats
                 WHERE industry IS NOT NULL AND industry <> ''
                UNION
                SELECT primary_industry AS industry FROM geo_research_articles
                 WHERE primary_industry IS NOT NULL AND primary_industry <> ''
            ) u
            WHERE NOT EXISTS (
                SELECT 1 FROM geo_research_industries i WHERE i.name = u.industry
            )
        """)
        orphans = [r['industry'] if isinstance(r, dict) else r[0] for r in (cur.fetchall() or [])]

        for name in orphans:
            ind_id = ensure_research_industry(name, conn=conn)
            if ind_id is not None:
                backfilled[name] = ind_id

        if own_conn:
            conn.commit()
        if backfilled:
            logger.info(f"[backfill_orphan_industries] 补齐 {len(backfilled)} 个: {list(backfilled.keys())}")
        return backfilled
    except Exception as e:
        logger.exception(f"[backfill_orphan_industries] 失败: {type(e).__name__}: {e}")
        if own_conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return backfilled
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass
