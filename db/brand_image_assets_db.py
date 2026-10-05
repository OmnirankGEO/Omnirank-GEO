"""
客户图库资产数据库模块(brand_image_assets)
- 客户图片素材的真实资产表（非普通知识库附件表）
- 存原图 + 缩略图 + 安全尺寸图 三套 key，外发风控字段 publish_allowed/rights_confirmed/risk_flags
- 写文章按 role/usage_scenarios 选图，发布时 public_url 拼域名绝对化

2026-06-02 GEO CTO · 客户资料中心图片素材能力
"""

import logging
import json
from db.connection import get_connection, get_db
from psycopg2.extras import Json, RealDictCursor

logger = logging.getLogger("GEO-BrandImage")


def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s AND column_name=%s
    """, (table, column))
    return cursor.fetchone() is not None


def _safe_add_column(cursor, table: str, column: str, col_type: str):
    if _column_exists(cursor, table, column):
        return
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except Exception:
        pass


# ==================== 初始化 ====================

def init_brand_image_assets_table():
    """创建客户图库资产表（幂等）。"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS brand_image_assets (
                id                  BIGSERIAL PRIMARY KEY,
                brand_id            INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
                file_name           TEXT NOT NULL,
                storage_key         TEXT NOT NULL,          -- 原图相对路径
                thumbnail_key       TEXT,                   -- 缩略图(列表用)
                safe_size_key       TEXT,                   -- 安全尺寸图(文章/发布展示用·避免超大原图)
                public_url          TEXT NOT NULL,          -- 对外相对 URL(发布时拼 PUBLIC_BASE_URL 绝对化)
                image_type          VARCHAR(20),            -- logo|product|case|certificate|team|storefront|environment|other
                title               TEXT,
                caption             TEXT,
                alt_text            TEXT,
                suggested_placement VARCHAR(30),            -- hero|brand_intro|product_desc|case_proof|not_for_external
                usage_scenarios     JSONB,                  -- 可用于哪些文章场景 ["brand_intro","case_proof"]
                vision_summary      TEXT,                   -- qwen 读图:这张图在表现什么
                ocr_text            TEXT,                   -- 图中文字
                tags                JSONB,
                publish_allowed     SMALLINT DEFAULT 1,     -- 是否允许对外发布(风险图默认 0)
                rights_confirmed    SMALLINT DEFAULT 0,     -- 用户是否确认可外发
                risk_flags          JSONB,                  -- ["qrcode","phone","privacy","watermark","irrelevant"]
                uploaded_by         INTEGER,
                status              VARCHAR(20) DEFAULT 'active',  -- active|archived|deleted
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_brand_image_assets_brand
            ON brand_image_assets(brand_id) WHERE status = 'active'
        """)
        # 幂等补字段(老表已存在时)
        for col, typ in [
            ("thumbnail_key", "TEXT"), ("safe_size_key", "TEXT"),
            ("usage_scenarios", "JSONB"), ("risk_flags", "JSONB"),
            ("publish_allowed", "SMALLINT DEFAULT 1"), ("rights_confirmed", "SMALLINT DEFAULT 0"),
            ("status", "VARCHAR(20) DEFAULT 'active'"),
        ]:
            _safe_add_column(cursor, "brand_image_assets", col, typ)
    logger.info("[brand_image_assets] 表已就绪")


# ==================== 写 ====================

def insert_image_asset(
    brand_id: int, file_name: str, storage_key: str, public_url: str,
    thumbnail_key: str = None, safe_size_key: str = None,
    image_type: str = None, title: str = None, caption: str = None, alt_text: str = None,
    suggested_placement: str = None, usage_scenarios: list = None,
    vision_summary: str = None, ocr_text: str = None, tags: list = None,
    publish_allowed: int = 1, rights_confirmed: int = 0, risk_flags: list = None,
    uploaded_by: int = None,
) -> int:
    """插入一条图库资产，返回 id。"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO brand_image_assets
              (brand_id, file_name, storage_key, thumbnail_key, safe_size_key, public_url,
               image_type, title, caption, alt_text, suggested_placement, usage_scenarios,
               vision_summary, ocr_text, tags, publish_allowed, rights_confirmed, risk_flags, uploaded_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            brand_id, file_name, storage_key, thumbnail_key, safe_size_key, public_url,
            image_type, title, caption, alt_text, suggested_placement,
            Json(usage_scenarios or []), vision_summary, ocr_text, Json(tags or []),
            publish_allowed, rights_confirmed, Json(risk_flags or []), uploaded_by,
        ))
        return cursor.fetchone()["id"]


def update_image_asset(asset_id: int, **fields):
    """更新指定字段(publish_allowed/rights_confirmed/status/title/caption 等)。JSONB 字段自动包 Json。"""
    if not fields:
        return
    json_cols = {"usage_scenarios", "tags", "risk_flags"}
    sets, vals = [], []
    for k, v in fields.items():
        sets.append(f"{k} = %s")
        vals.append(Json(v) if k in json_cols else v)
    sets.append("updated_at = CURRENT_TIMESTAMP")
    vals.append(asset_id)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE brand_image_assets SET {', '.join(sets)} WHERE id = %s", vals)


def get_image_assets_by_ids(asset_ids: list[int]) -> list[dict]:
    """Load persisted assets for asset-owned authorization, including inactive rows."""
    ids = list(dict.fromkeys(int(asset_id) for asset_id in asset_ids if int(asset_id) > 0))
    if not ids:
        return []
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT * FROM brand_image_assets WHERE id = ANY(%s) ORDER BY id",
            (ids,),
        )
        return [_parse_row(dict(row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def batch_set_article_usage(asset_ids: list[int], *, enabled: bool) -> dict:
    """Idempotently apply article usage while preserving confirmed rights facts."""
    ids = list(dict.fromkeys(int(asset_id) for asset_id in asset_ids if int(asset_id) > 0))
    result = {"updated": 0, "skipped": 0, "failed": 0, "items": []}
    if not ids:
        return result

    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT * FROM brand_image_assets WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (ids,),
        )
        rows = {int(row["id"]): _parse_row(dict(row)) for row in cursor.fetchall()}
        for asset_id in ids:
            asset = rows.get(asset_id)
            if not asset:
                result["failed"] += 1
                result["items"].append({"asset_id": asset_id, "status": "failed", "reason": "图片不存在"})
                continue
            if asset.get("status") != "active":
                result["skipped"] += 1
                result["items"].append({"asset_id": asset_id, "status": "skipped", "reason": "图片不是可用状态"})
                continue
            risks = [str(flag) for flag in (asset.get("risk_flags") or []) if str(flag).strip()]
            if enabled and risks:
                result["skipped"] += 1
                result["items"].append({
                    "asset_id": asset_id,
                    "status": "skipped",
                    "reason": f"检测到风险:{'、'.join(risks)}",
                })
                continue

            target_publish = 1 if enabled else 0
            already_target = int(asset.get("publish_allowed") or 0) == target_publish
            if enabled:
                already_target = already_target and int(asset.get("rights_confirmed") or 0) == 1
            if already_target:
                result["skipped"] += 1
                result["items"].append({"asset_id": asset_id, "status": "skipped", "reason": "已经是目标状态"})
                continue

            if enabled:
                cursor.execute(
                    """
                    UPDATE brand_image_assets
                    SET publish_allowed=%s, rights_confirmed=%s, updated_at=CURRENT_TIMESTAMP
                    WHERE id=%s AND status='active'
                    """,
                    (1, 1, asset_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE brand_image_assets
                    SET publish_allowed=%s, updated_at=CURRENT_TIMESTAMP
                    WHERE id=%s AND status='active'
                    """,
                    (0, asset_id),
                )
            if cursor.rowcount == 1:
                result["updated"] += 1
                result["items"].append({"asset_id": asset_id, "status": "updated", "reason": ""})
            else:
                result["failed"] += 1
                result["items"].append({"asset_id": asset_id, "status": "failed", "reason": "更新时状态已变化"})
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def soft_delete_image_asset(asset_id: int):
    """软删(status=deleted)。物理文件由清理任务异步处理。"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE brand_image_assets SET status='deleted', updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            (asset_id,),
        )


# ==================== 读 ====================

def _parse_row(row: dict) -> dict:
    """JSONB 字段已由 psycopg2 解析为 list/dict,这里仅兜底字符串残留。"""
    if not row:
        return row
    for c in ("usage_scenarios", "tags", "risk_flags"):
        v = row.get(c)
        if isinstance(v, str):
            try:
                row[c] = json.loads(v)
            except Exception:
                row[c] = []
        elif v is None:
            row[c] = []
    return row


def get_image_asset(asset_id: int) -> dict:
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM brand_image_assets WHERE id = %s", (asset_id,))
        row = cursor.fetchone()
        return _parse_row(dict(row)) if row else None
    finally:
        conn.close()


def list_image_assets(brand_id: int, include_archived: bool = False) -> list:
    """列某品牌图库(默认只 active)。前端图库列表 + 写文章选图候选用。"""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        statuses = ("active", "archived") if include_archived else ("active",)
        cursor.execute(
            "SELECT * FROM brand_image_assets WHERE brand_id=%s AND status = ANY(%s) ORDER BY created_at DESC",
            (brand_id, list(statuses)),
        )
        return [_parse_row(dict(r)) for r in cursor.fetchall()]
    finally:
        conn.close()


def list_publishable_assets(brand_id: int) -> list:
    """写文章选图硬规则:只取 status=active AND publish_allowed=1 AND rights_confirmed=1。
    P2 的 article_image_selector 按 role/image_type/usage_scenarios 从这批里挑。"""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT * FROM brand_image_assets
            WHERE brand_id=%s AND status='active' AND publish_allowed=1 AND rights_confirmed=1
            ORDER BY created_at DESC
        """, (brand_id,))
        return [_parse_row(dict(r)) for r in cursor.fetchall()]
    finally:
        conn.close()
