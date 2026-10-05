"""
小榜知识库数据层(Phase 1 · 2026-05-25)

单表 kb_chunks · 帮助文档 / FAQ / 预设答案 统一索引。
不依赖 pgvector / zhparser · 用 jieba + Python 端 BM25 跑检索(MVP)。
后续加 embedding 时只需要补 embedding 字段 + ivfflat 索引,不影响现有 CRUD。

字段说明:
- source_type: 'doc' | 'faq' | 'preset' | 'sys_page' | 'sys_field' | 'sys_error' | 'sys_button' | 'sys_qa'（后五者为系统知识库类型）
- source_slug: doc 用 slug · faq 用 'faq_{id}' · preset 用 'preset_{key}'
- section_title: 文档内 H2/H3(如"在线报价 6 步走完") · FAQ/preset 留空
- route: 推荐跳转路径(如 /diagnosis/new) · null 表示不推跳转
- route_label: 跳转按钮文案
- category: 帮助中心分类 id(onboarding / sales / writing / ...)
- is_admin_only: True 表示只有 is_admin 的用户检索能命中
- token_keywords: jieba 分完词去停用词后的 list(JSON 数组 · 用于 BM25)

token_keywords 不存 GIN tsvector 是因为 PG 没装 zhparser · 不靠 PG 索引,靠应用层全表扫(<200 chunks,几毫秒)
"""

import json
import logging
from typing import Any, Iterable, Mapping, Optional

from services.kb_terminology_gate import (
    assert_chunk_rows_clean,
    assert_kb_text_clean,
)
import psycopg2

logger = logging.getLogger("KB-DB")

KB_RELEASE_MANIFEST_SLUG = "__kb_release_manifest__"


def _get_conn():
    from db.connection import get_connection
    return get_connection()


# ==========================================
# 初始化
# ==========================================

def init_kb_tables():
    """幂等建表 · 不 seed(由 tools/xiaobang_kb_indexer.py 单独跑)

    embedding 字段:
      - 用 JSONB 存 list of float(DashScope text-embedding-v4 默认 1024 维)
      - 不用 pgvector 是因为 145 chunks 规模下 Python cosine 5ms 完事,不值得装扩展
      - 规模 >5000 chunks 时可以平滑迁到 pgvector(只改 kb_db + xiaobang_api 检索层)
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kb_chunks (
                id              SERIAL PRIMARY KEY,
                source_type     VARCHAR(10) NOT NULL CHECK (source_type IN ('doc','faq','preset')),
                source_slug     VARCHAR(128) NOT NULL,
                source_title    TEXT NOT NULL,
                section_title   TEXT,
                content         TEXT NOT NULL,
                route           VARCHAR(256),
                route_label     VARCHAR(64),
                category        VARCHAR(32) NOT NULL DEFAULT 'general',
                is_admin_only   BOOLEAN NOT NULL DEFAULT FALSE,
                token_keywords  JSONB NOT NULL DEFAULT '[]'::jsonb,
                embedding       JSONB,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
        """)
        # 老库幂等加列 · 已有列不报错
        cur.execute("ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS embedding JSONB;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_source ON kb_chunks (source_type, source_slug);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_admin ON kb_chunks (is_admin_only);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_category ON kb_chunks (category);")
        conn.commit()

        # ---- Task 1 迁移：扩 source_type + 加 origin 列 ----
        # ALTER TABLE 需要 autocommit，否则在同一事务里 DDL 可能被回滚
        old_autocommit = conn.autocommit
        conn.autocommit = True
        try:
            # 1. 扩列宽 VARCHAR(10) → VARCHAR(16)
            cur.execute("ALTER TABLE kb_chunks ALTER COLUMN source_type TYPE VARCHAR(16);")

            # 2. 重建 source_type CHECK（DROP IF EXISTS 幂等安全）
            cur.execute("ALTER TABLE kb_chunks DROP CONSTRAINT IF EXISTS kb_chunks_source_type_check;")
            cur.execute("""
                ALTER TABLE kb_chunks ADD CONSTRAINT kb_chunks_source_type_check
                  CHECK (source_type IN ('doc','faq','preset','sys_page','sys_field','sys_error','sys_button','sys_qa'));
            """)

            # 3. 加 origin 列（幂等）
            cur.execute(
                "ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS origin VARCHAR(8) NOT NULL DEFAULT 'manual';"
            )
            # 4. 重建 origin CHECK（幂等）
            cur.execute("ALTER TABLE kb_chunks DROP CONSTRAINT IF EXISTS kb_chunks_origin_check;")
            cur.execute("""
                ALTER TABLE kb_chunks ADD CONSTRAINT kb_chunks_origin_check
                  CHECK (origin IN ('auto','manual'));
            """)

            # 5. 加 visible_to 列（两套知识库身份路由 · 幂等）
            #    agent=仅代理 KB · normal_user=仅普通用户 KB · both=两套都进
            #    老库既有 doc/faq/preset chunk 默认 'both'（通用帮助内容，两身份均可见）
            cur.execute(
                "ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS visible_to VARCHAR(16) NOT NULL DEFAULT 'both';"
            )
            cur.execute("ALTER TABLE kb_chunks DROP CONSTRAINT IF EXISTS kb_chunks_visible_to_check;")
            cur.execute("""
                ALTER TABLE kb_chunks ADD CONSTRAINT kb_chunks_visible_to_check
                  CHECK (visible_to IN ('agent','normal_user','both'));
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_chunks_visible_to ON kb_chunks (visible_to);"
            )
        finally:
            # 任何路径都恢复 autocommit，防止连接池复用时泄漏 autocommit=True
            conn.autocommit = old_autocommit
        # ---- 迁移结束 ----

        logger.info("[kb] kb_chunks 表初始化完成")
    finally:
        conn.close()


# ==========================================
# CRUD
# ==========================================

def clear_chunks_by_type(source_type: str) -> int:
    """重建索引时按类型清空(doc / faq / preset 各管各的)"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM kb_chunks WHERE source_type = %s", (source_type,))
        deleted = cur.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


def clear_system_chunks() -> int:
    """清空所有 sys_* 类型 chunk(重建系统知识库前调用)"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM kb_chunks WHERE source_type IN ('sys_page','sys_field','sys_error','sys_button','sys_qa');"
        )
        deleted = cur.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


def insert_chunk(
    source_type: str,
    source_slug: str,
    source_title: str,
    content: str,
    section_title: Optional[str] = None,
    route: Optional[str] = None,
    route_label: Optional[str] = None,
    category: str = 'general',
    is_admin_only: bool = False,
    token_keywords: Optional[list[str]] = None,
    embedding: Optional[list[float]] = None,
    origin: str = 'manual',
    visible_to: str = 'both',
) -> int:
    """插入一条 chunk · 返回 id

    visible_to: 'agent' | 'normal_user' | 'both' —— 两套知识库身份路由。
    """
    # [R3-P7 ①] 术语门 · fail-closed。开在 writer 而不是各个 builder 里:
    # 这两个 writer 是 admin 触发重建与全量重建**唯一共同的收口点**,
    # 门开在收口点 ⇒ 以后新增写入路径自动被管住,不靠新增路径的人记得来加校验。
    _blob = (source_title or "") + "\n" + (content or "")
    assert_kb_text_clean(_blob, where="kb_db.insert_chunk:" + str(source_slug))
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO kb_chunks
               (source_type, source_slug, source_title, section_title, content,
                route, route_label, category, is_admin_only, token_keywords, embedding, origin, visible_to)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
               RETURNING id""",
            (
                source_type, source_slug, source_title, section_title, content,
                route, route_label, category, is_admin_only,
                json.dumps(token_keywords or [], ensure_ascii=False),
                json.dumps(embedding) if embedding else None,
                origin,
                visible_to,
            ),
        )
        new_id = cur.fetchone()['id']
        conn.commit()
        return new_id
    finally:
        conn.close()


#: [R3-P8 ③] 可被「单事务 clear+insert」整类替换的 source_type 全集。
#: 原来写死 {doc, faq, preset},于是 sys_* 只能走 clear_system_chunks() + 逐条
#: insert_chunk() —— 每条一个事务,中途失败留下**半份 release**。
#: 🔴 这份集合必须覆盖 builder 实际产出的每一个 source_type;判据用 AST 从
#:    builder 机械取全集与它对账(见 test_kb_index_denominator 闸①),
#:    新增一类 chunk 而忘了加进来 → 转红,而不是悄悄退回逐条写。
REPLACEABLE_SOURCE_TYPES: frozenset[str] = frozenset({
    "doc", "faq", "preset",
    "sys_page", "sys_field", "sys_button", "sys_error", "sys_qa",
})


def replace_chunks_transactionally(
    *,
    source_types: Iterable[str],
    chunks: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """同一事务淘汰旧 release 并写入完整新 release。

    不新增表/列：release manifest 作为不可召回的内部 preset 行保存；
    ``load_all_chunks`` 明确排除该 slug。
    """
    allowed = set(REPLACEABLE_SOURCE_TYPES)
    types = tuple(dict.fromkeys(str(t) for t in source_types))
    if not types or any(t not in allowed for t in types):
        raise ValueError("invalid source_types")
    rows = [dict(row) for row in chunks]
    # [R3-P7 ①] 🔴 在 DELETE 之前**全部扫完**再决定。本函数是 clear-then-insert,
    # 边扫边写会留下半份 release —— 半份 = 用户当场问不出答案,比拒绝坏得多。
    assert_chunk_rows_clean(rows, where="kb_db.replace_chunks_transactionally")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM kb_chunks WHERE source_type = ANY(%s)", (list(types),))
        deleted = int(cur.rowcount or 0)
        inserted = 0
        sql = """
            INSERT INTO kb_chunks
              (source_type, source_slug, source_title, section_title, content,
               route, route_label, category, is_admin_only, token_keywords,
               embedding, origin, visible_to)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)
        """
        for row in rows:
            source_type = str(row.get("source_type") or "")
            if source_type not in types:
                raise ValueError(f"chunk source_type outside replacement: {source_type}")
            cur.execute(sql, (
                source_type,
                str(row.get("source_slug") or ""),
                str(row.get("source_title") or ""),
                row.get("section_title"),
                str(row.get("content") or ""),
                row.get("route"),
                row.get("route_label"),
                str(row.get("category") or "general"),
                bool(row.get("is_admin_only")),
                json.dumps(row.get("token_keywords") or [], ensure_ascii=False),
                json.dumps(row.get("embedding"), ensure_ascii=False)
                if row.get("embedding") is not None else None,
                str(row.get("origin") or "manual"),
                str(row.get("visible_to") or "both"),
            ))
            inserted += 1
        if manifest is not None:
            cur.execute("DELETE FROM kb_chunks WHERE source_slug = %s", (KB_RELEASE_MANIFEST_SLUG,))
            cur.execute(sql, (
                "preset", KB_RELEASE_MANIFEST_SLUG, "小榜知识 release manifest", None,
                json.dumps(dict(manifest), ensure_ascii=False, sort_keys=True),
                None, None, "internal", True, "[]", None, "auto", "both",
            ))
        conn.commit()
        return {"deleted": deleted, "inserted": inserted}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_all_chunks(include_admin: bool = False) -> list[dict]:
    """加载所有 chunks(内存检索用)· include_admin=False 过滤管理员节点

    返回的 chunk dict 含:
      - token_keywords: list[str] (BM25 用)
      - embedding: list[float] | None (cosine 用 · None 表示未生成)
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        sql = """SELECT id, source_type, source_slug, source_title,
                        section_title, content, route, route_label,
                        category, is_admin_only, token_keywords, embedding, origin, visible_to
                 FROM kb_chunks WHERE source_slug <> %s"""
        params: list[Any] = [KB_RELEASE_MANIFEST_SLUG]
        if not include_admin:
            sql += " AND is_admin_only = FALSE"
        sql += " ORDER BY id"
        cur.execute(sql, params)
        rows = cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for fld in ('token_keywords', 'embedding'):
                v = d.get(fld)
                if isinstance(v, str):
                    try:
                        d[fld] = json.loads(v)
                    except Exception:
                        d[fld] = None if fld == 'embedding' else []
            if d.get('token_keywords') is None:
                d['token_keywords'] = []
            out.append(d)
        return out
    finally:
        conn.close()


def count_chunks(*, include_internal: bool = False) -> dict[str, int]:
    """各类型 chunk 数量(运维用)。

    🔴 [WO-A ④ · 2026-08-20] 默认**排除** release manifest 那一行。

    ## 诊断:多出来的那条是什么、谁写进去的

    生产上 ``preset`` 长期显示 16,而同一次重建写下的
    ``manifest["chunk_counts"]["preset"]`` 是 15。多的那一条不是知识,是
    :data:`KB_RELEASE_MANIFEST_SLUG` —— manifest 自己。它由
    :func:`replace_chunks_transactionally` 写入,为了「不新增表/列」而**借用**
    ``source_type='preset'`` 存放(见该函数 docstring)。

    ## 哪边错

    错的是这个计数函数,不是 manifest:manifest 的 ``chunk_counts`` 按定义
    描述的是**这一版 release 的知识条数**,15 是对的;而检索路径
    :func:`load_all_chunks` **早就**把这一行排除了(``source_slug <> %s``)。
    也就是说全仓只有这个计数器把内部记账行当成知识在数,三个消费方
    (``/api/xiaobang/health`` · admin 重建返回值 · indexer 收尾日志)
    因此都比真实知识量多报一条。对齐方向 = 让计数与检索口径一致。

    ``include_internal=True`` 保留原始行数视角(排查「manifest 行到底在不在」时用)。
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        sql = "SELECT source_type, COUNT(*) AS c FROM kb_chunks"
        params: list[Any] = []
        if not include_internal:
            sql += " WHERE source_slug <> %s"
            params.append(KB_RELEASE_MANIFEST_SLUG)
        sql += " GROUP BY source_type"
        cur.execute(sql, params)
        return {r['source_type']: r['c'] for r in cur.fetchall()}
    finally:
        conn.close()


def load_release_manifest() -> dict[str, Any] | None:
    """读回上一次 :func:`replace_chunks_transactionally` 写下的 release manifest。

    没有它的话,「manifest 说 15、库里有几条」这种对账只能靠人肉 psql ——
    而对账正是 ``preset 16 vs manifest 15`` 这条一直没闭的原因。
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT content FROM kb_chunks WHERE source_slug = %s",
            (KB_RELEASE_MANIFEST_SLUG,),
        )
        row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["content"])
        except (TypeError, ValueError):
            logger.warning("[kb] release manifest 行内容不是合法 JSON,按缺失处理")
            return None
    finally:
        conn.close()
