"""
M3 聚合 BFF API (CTO-15.13 2026-04-25)

老板 §9 优先级 E.1 第一批 · 解 M3 listClients N+1 查询。

设计原则(老板拍板):
  - RBAC 沿用 /api/client-context/list 三段(admin / user_clients / owner)
  - latest quote 用真 ORDER BY created_at DESC LIMIT 1 · 不用旧 quote_status_sql 多重 EXISTS 猜
  - completeness 复用 utils/brand_completeness.py · 不重写
  - 软删 quote 默认过滤(deleted_at IS NULL) · CTO-15.7 P0.5b 标准
  - 单 SQL + 一次性 profile 拉 + Python 内存 compute(代理一般 < 50 brand · 可接受)

未来 M3 endpoint 续接此模块(/today / /customers/:id/lifecycle / /delivery-queue 等)
"""

from fastapi import APIRouter, Request, HTTPException
import logging

# WO_267:industry_category 存量是旧中文名、新写入是大类 key ⇒ 读侧统一翻译(存量不回填)
from services.industry_taxonomy import category_fields, display_name

logger = logging.getLogger("GEO-M3-API")

router = APIRouter(tags=["M3 聚合 BFF"])

_M3_FIRST_BATCH_TABLES_READY = False


def init_m3_first_batch_tables() -> None:
    """Ensure the small M3 persistence tables exist.

    These endpoints were originally shipped with a separate SQL migration. In
    practice, missed migrations make reads fail with 500. Keep startup and
    endpoint-level self-heal here so a deploy can recover the tables without a
    manual psql step.
    """
    global _M3_FIRST_BATCH_TABLES_READY
    if _M3_FIRST_BATCH_TABLES_READY:
        return

    from db.diagnosis_db import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS m3_quick_records (
                id SERIAL PRIMARY KEY,
                brand_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                record_type VARCHAR(40) NOT NULL,
                note TEXT,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_m3_quick_records_brand_created
            ON m3_quick_records (brand_id, created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_m3_quick_records_user_created
            ON m3_quick_records (user_id, created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS m3_brand_notes (
                id SERIAL PRIMARY KEY,
                brand_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                note TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (brand_id, user_id)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_m3_brand_notes_user_brand
            ON m3_brand_notes (user_id, brand_id)
            """
        )
        conn.commit()
        cur.close()
        _M3_FIRST_BATCH_TABLES_READY = True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _get_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


# ============================================================
# 共享 SQL 模板:LEFT JOIN LATERAL 拉 latest quote(过滤软删)
# ============================================================

_BRAND_FIELDS = """
    b.id, b.name, b.brand_code, b.industry, b.industry_category,
    b.company_name, b.cities,
    -- [返工2 修复净增量 P1] latest_score/latest_diagnosis_id 不再读 brands 冗余列(装配期写 pending 分 · 会泄露
    --   未结算/退款诊断分)· 改 published-only 子查询,与 detail/其他读取点同源(排 withheld+pending)。
    (SELECT d.total_score FROM diagnosis_records d WHERE d.brand_id = b.id
        AND (d.result_visibility IS NULL OR d.result_visibility = 'published')
        ORDER BY d.created_at DESC LIMIT 1) AS latest_score,
    (SELECT d.id FROM diagnosis_records d WHERE d.brand_id = b.id
        AND (d.result_visibility IS NULL OR d.result_visibility = 'published')
        ORDER BY d.created_at DESC LIMIT 1) AS latest_diagnosis_id,
    b.created_at, b.updated_at,
    COALESCE(b.brand_type, 'legacy') AS brand_type,
    b.owner_user_id,
    COALESCE(b.is_test, FALSE) AS is_test,
    (SELECT COUNT(*) FROM diagnosis_records dr WHERE dr.brand_id = b.id
        AND (dr.result_visibility IS NULL OR dr.result_visibility = 'published')) AS diagnosis_count,  -- [返工2 P1-2] 排 withheld+pending
    lq.id AS lq_id, lq.status AS lq_status, lq.paid_amount AS lq_paid_amount,
    lq.monthly_price AS lq_monthly_price, lq.service_status AS lq_service_status,
    lq.service_start_date AS lq_service_start_date,
    lq.service_end_date AS lq_service_end_date,
    lq.service_months AS lq_service_months,
    lq.confirmed_at AS lq_confirmed_at, lq.paid_at AS lq_paid_at,
    lq.created_at AS lq_created_at, lq.tier AS lq_tier,
    lq.total_keywords AS lq_total_keywords,
    lq.total_articles AS lq_total_articles,
    lq.source_type AS lq_source_type,
    lq.monitoring_enabled AS lq_monitoring_enabled,
    lq.markdown AS lq_markdown,
    /* Phase A.4 (CTO-15.11):同 brand 第 2 个 draft quote · 用作"有新草稿"banner */
    dq.id AS dq_id, dq.created_at AS dq_created_at,
    dq.total_keywords AS dq_total_keywords, dq.tier AS dq_tier,
    dq.diagnosis_id AS dq_diagnosis_id
"""

# Phase A.4 (CTO-15.11 2026-04-28):修 stage 倒退
# 老板痛点(40 岁老销售实测 brand 270):
#   quote 273 paid+active(stage 5 写作)+ 新建 draft quote 274(stage 3)
#   → 老 LATERAL 用 ORDER BY created_at DESC,quote 274 覆盖显示 → 客户工作台回退到"待报价"
#
# 新策略:
#   1. lq (主 quote)=优先级:active > paid > pending_payment > confirmed > draft
#      同优先级再按 created_at DESC
#   2. dq (草稿 quote)=仅在 lq 已是 paid/active 且存在更新的 draft 时显示
#      让前端展示"主报价 #273 已激活 + 顶部 banner '新草稿 #274 待发'"
_BRAND_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT id, status, paid_amount, monthly_price, service_status,
               service_start_date, service_end_date, service_months,
               confirmed_at, paid_at, created_at, tier, total_keywords,
               total_articles, source_type, monitoring_enabled, markdown
        FROM quotes
        WHERE brand_id = b.id
          AND deleted_at IS NULL
        ORDER BY
            CASE
                WHEN service_status = 'active' THEN 0
                WHEN status = 'paid' THEN 1
                WHEN status = 'pending_payment' THEN 2
                WHEN status = 'confirmed' THEN 3
                ELSE 4
            END,
            created_at DESC
        LIMIT 1
    ) lq ON TRUE
    LEFT JOIN LATERAL (
        SELECT id, created_at, total_keywords, tier, diagnosis_id
        FROM quotes
        WHERE brand_id = b.id
          AND deleted_at IS NULL
          AND status = 'draft'
          AND (
              lq.id IS NULL
              OR (lq.status IN ('paid', 'pending_payment', 'confirmed') AND quotes.id != lq.id)
          )
        ORDER BY created_at DESC
        LIMIT 1
    ) dq ON TRUE
"""

_BRAND_ORDER = """
    ORDER BY
        CASE WHEN b.brand_type = 'self' THEN 0 ELSE 1 END,
        b.updated_at DESC
"""


def _row_to_latest_quote(r: dict) -> dict | None:
    """从 SQL row 抽 latest quote 字段为单独 dict · 没 quote 时返 None"""
    if r.get("lq_id") is None:
        return None
    monthly_price = r["lq_monthly_price"] if r.get("lq_monthly_price") is not None else None
    total_articles = r.get("lq_total_articles")
    if (not monthly_price or not total_articles) and r.get("lq_markdown"):
        try:
            from services.quote_numeric_repair import repair_quote_numeric_fields
            repaired = repair_quote_numeric_fields(int(r["lq_id"]), {
                "id": r["lq_id"],
                "tier": r.get("lq_tier"),
                "monthly_price": monthly_price,
                "total_articles": total_articles,
                "markdown": r.get("lq_markdown"),
            })
            monthly_price = repaired.get("monthly_price") or monthly_price
            total_articles = repaired.get("total_articles") or total_articles
        except Exception as _repair_err:
            logger.warning(f"[m3/latest-quote-repair] quote_id={r.get('lq_id')} skipped: {_repair_err}")
    return {
        "id": r.get("lq_id"),
        "status": r.get("lq_status"),
        "paid_amount": float(r["lq_paid_amount"]) if r.get("lq_paid_amount") is not None else None,
        "monthly_price": float(monthly_price) if monthly_price is not None else None,
        "total_articles": int(total_articles) if total_articles else None,
        "service_status": r.get("lq_service_status"),
        "service_start_date": r["lq_service_start_date"].isoformat() if r.get("lq_service_start_date") else None,
        "service_end_date": r["lq_service_end_date"].isoformat() if r.get("lq_service_end_date") else None,
        "service_months": r.get("lq_service_months"),
        "confirmed_at": r["lq_confirmed_at"].isoformat() if r.get("lq_confirmed_at") else None,
        "paid_at": r["lq_paid_at"].isoformat() if r.get("lq_paid_at") else None,
        "created_at": r["lq_created_at"].isoformat() if r.get("lq_created_at") else None,
        "tier": r.get("lq_tier"),
        "total_keywords": r.get("lq_total_keywords"),
        "source_type": r.get("lq_source_type"),
        "monitoring_enabled": r.get("lq_monitoring_enabled"),
    }


# ============================================================
# GET /api/m3/customers — 聚合客户列表(M3 listClients 解 N+1)
# ============================================================

@router.get("/api/m3/customers")
def get_m3_customers(request: Request):
    """M3 销售/交付端客户聚合列表

    一次返:brand 基础 + latest quote(真 ORDER BY DESC)+ completeness(SSOT 算法)
           + diagnosis_count + latest_score。

    替代 frontend/src/services/m3/api.ts:listClients 的 N+1
    (原:/api/client-context/list + 对每个 brand 并发调 /api/quotes?brand_id=X&limit=1)

    RBAC 沿用 /api/client-context/list 三段:
      - admin → 全部非软删 brand
      - 非 admin + user_clients 分配 → 仅分配 brand
      - 非 admin + 未分配 → owner_user_id 自己的 brand
    """
    try:
        from db.diagnosis_db import get_connection
        user = _get_user(request)
        is_admin = user.get("is_admin", False)
        client_brand_ids = user.get("client_brand_ids", []) or []
        user_id = user.get("user_id")

        # A.2 (CTO-15.18 · 2026-04-28):测试客户隔离 filter
        # 默认 ON 隐藏测试 brand · ?include_test=true 时返全部(admin 调试用)
        include_test_qp = (request.query_params.get("include_test") or "").lower()
        # [BUG6 2026-06-05] include_test 仅 admin 生效 · 非 admin 强制排除测试客户(防 query 参数绕过)
        include_test = is_admin and include_test_qp in ("1", "true", "yes", "y")
        test_filter = "" if include_test else " AND (b.is_test IS NULL OR b.is_test = FALSE)"

        # A.3 (CTO-15.18 · 2026-04-28):客户列表 SSOT 跟旧版 /api/my-clients 对齐
        # 旧版 brand_api.py:685 有 AND b.brand_type = 'client' 过滤
        # M3 之前没这个过滤 → self brand(用户自己的创作空间)被错误显示在客户池
        # 真因 = 6(旧版正确) vs 10(M3 错误)的差异源
        # 修法 = M3 加同样过滤 · 排除 self brand · 跟旧版同源
        # admin show_all=true 时仍可见全部(调试用)
        show_all_qp = (request.query_params.get("show_all") or "").lower()
        show_all = is_admin and show_all_qp in ("1", "true", "yes", "y")
        client_type_filter = "" if show_all else " AND COALESCE(b.brand_type, 'client') = 'client'"

        conn = get_connection()
        try:
            cur = conn.cursor()

            # ===== 1. 拉 brand + latest quote (RBAC 三段 + is_test filter + brand_type filter) =====
            if is_admin:
                cur.execute(f"""
                    SELECT {_BRAND_FIELDS}
                    FROM brands b
                    {_BRAND_LATERAL}
                    WHERE (b.is_deleted IS NULL OR b.is_deleted = FALSE)
                      {test_filter}
                      {client_type_filter}
                    {_BRAND_ORDER}
                """)
            elif client_brand_ids:
                placeholders = ",".join(["%s"] * len(client_brand_ids))
                cur.execute(f"""
                    SELECT {_BRAND_FIELDS}
                    FROM brands b
                    {_BRAND_LATERAL}
                    WHERE b.id IN ({placeholders})
                      AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
                      {test_filter}
                      {client_type_filter}
                    {_BRAND_ORDER}
                """, client_brand_ids)
            elif user_id:
                cur.execute(f"""
                    SELECT {_BRAND_FIELDS}
                    FROM brands b
                    {_BRAND_LATERAL}
                    WHERE b.owner_user_id = %s
                      AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
                      {test_filter}
                      {client_type_filter}
                    {_BRAND_ORDER}
                """, (user_id,))
            else:
                return {"success": True, "customers": [], "total": 0}

            brand_rows = cur.fetchall()
            brand_ids = [r["id"] for r in brand_rows]

            # ===== 2. 一次拉所有 brand 的 latest profile (DISTINCT ON 解 N+1) =====
            profiles_by_brand: dict[int, dict] = {}
            if brand_ids:
                placeholders = ",".join(["%s"] * len(brand_ids))
                cur.execute(f"""
                    SELECT DISTINCT ON (brand_id) *
                    FROM client_profiles
                    WHERE brand_id IN ({placeholders})
                      AND (is_deleted = 0 OR is_deleted IS NULL)
                    ORDER BY brand_id, updated_at DESC
                """, brand_ids)
                for row in cur.fetchall():
                    profiles_by_brand[row["brand_id"]] = dict(row)
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # ===== 3. 组装 + 计算 completeness (Python 内存) =====
        from utils.brand_completeness import compute_brand_completeness

        customers = []
        for r in brand_rows:
            brand_dict = dict(r)
            profile = profiles_by_brand.get(r["id"])
            try:
                comp = compute_brand_completeness(brand_dict, profile)
            except Exception as e:
                logger.warning(f"[m3/customers] completeness fail brand={r['id']}: {e}")
                comp = {"score": 0, "groups": {}, "missing": [], "industry_brief_state": "idle"}

            customers.append({
                "id": r["id"],
                "name": r["name"],
                "brand_code": r.get("brand_code"),
                "industry": r.get("industry") or display_name(r.get("industry_category")),
                "industry_category": r.get("industry_category"),
                **category_fields(r.get("industry_category")),
                "company_name": r.get("company_name"),
                "cities": r.get("cities"),
                "brand_type": r.get("brand_type", "legacy"),
                "diagnosis_count": r.get("diagnosis_count", 0),
                "latest_score": r.get("latest_score"),
                "latest_diagnosis_id": r.get("latest_diagnosis_id"),
                "is_test": bool(r.get("is_test", False)),  # A.2 CTO-15.18 · 测试客户隔离
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
                "latest_quote": _row_to_latest_quote(r),
                "completeness": {
                    "score": int(comp.get("score", 0)),
                    "groups": comp.get("groups", {}),
                    "missing": comp.get("missing", []),
                    "industry_brief_state": comp.get("industry_brief_state"),
                },
            })

        return {"success": True, "customers": customers, "total": len(customers)}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[m3/customers] 聚合失败: {e}")
        raise HTTPException(status_code=500, detail={
            "code": "m3_customers_aggregate_error",
            "message": "聚合失败,请稍后重试",
        })


# ============================================================
# M3 后端第一档(2026-04-26 CTO-15.15-M3-Lead 任务 2):
#   2.1 POST/GET /api/m3/quick-record   "刚做了什么" 入库
#   2.2 GET/POST /api/m3/brand-notes/{brand_id}  客户私人备注
#   2.3 GET /api/m3/quote-audit-log     报价 4 状态审计
#
# 设计:
#   - RBAC 沿用 auth/brand_access · admin 全通 · owner 通自己 · 分配 brand 通分配
#   - 表 m3_quick_records / m3_brand_notes 走 IF NOT EXISTS migration(scripts/migration_m3_first_batch.sql)
#   - 2.3 复用 audit_logs 表 · 不建新表(老板要求"先复用")
# ============================================================

# ============================================================
# 2.2 客户私人备注(per user × brand · upsert)
# ============================================================

# ============================================================
# 2.3 报价 4 状态审计 · 复用 audit_logs(老板要求)
#   server.py 4 处 quote endpoint 配套补 create_audit_log:
#     /api/quotes/{id}/confirm
#     /api/quotes/{id}/offline-confirm
#     /api/quotes/{id}/offline-mark-paid
#     /api/quotes/{id}/service-period
#   对应 action:
#     quote_confirm / quote_offline_confirm / quote_offline_mark_paid / quote_service_period
# ============================================================

QUOTE_AUDIT_ACTIONS = {
    "quote_confirm",
    "quote_offline_confirm",
    "quote_offline_mark_paid",
    "quote_service_period",
}

QUOTE_AUDIT_LABELS = {
    "quote_confirm": "客户确认报价",
    "quote_offline_confirm": "线下确认订单",
    "quote_offline_mark_paid": "标记线下收款",
    "quote_service_period": "调整服务期",
}


@router.get("/api/m3/quote-audit-log")
def get_quote_audit_log(quote_id: int, request: Request, limit: int = 20):
    """查指定 quote 的 audit_logs(4 状态独立审计)

    复用 audit_logs 表(entity_type='quote' AND entity_id=quote_id)· 不建新表

    内联 RBAC: 不调 require_quote_access(它内部借 connection 会跟本 endpoint 的
    connection race · 复现本地 BLOCK-1 connection-already-closed)。改成单 connection
    先查 quote 拿 brand_id · 再调 require_brand_access (admin 直接放行不查 DB ·
    非 admin 走 owner/分配 fallback)。
    """
    from auth.brand_access import require_brand_access
    from db.diagnosis_db import get_connection

    _get_user(request)  # 先校验登录

    if limit < 1:
        limit = 20
    if limit > 100:
        limit = 100

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 1. 先查 quote 拿 brand_id(同一 conn · 避免 race)
        cur.execute(
            "SELECT id, brand_id FROM quotes WHERE id = %s AND deleted_at IS NULL",
            (quote_id,),
        )
        quote_row = cur.fetchone()
        if not quote_row:
            raise HTTPException(status_code=404, detail="报价单不存在")
        brand_id = quote_row["brand_id"]

        # 2. RBAC check · brand 级 access
        require_brand_access(request, brand_id, allow_null=True)

        # 3. 查 audit_logs
        cur.execute(
            """
            SELECT id, user_id, username, action, summary,
                   before_snapshot, after_snapshot, created_at
            FROM audit_logs
            WHERE entity_type = 'quote'
              AND entity_id = %s
              AND action = ANY(%s)
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (quote_id, list(QUOTE_AUDIT_ACTIONS), limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()

        events = []
        for r in rows:
            events.append({
                "id": r["id"],
                "user_id": r.get("user_id"),
                "username": r.get("username"),
                "action": r["action"],
                "label": QUOTE_AUDIT_LABELS.get(r["action"], r["action"]),
                "summary": r.get("summary"),
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            })

        return {
            "success": True,
            "quote_id": quote_id,
            "brand_id": brand_id,
            "events": events,
            "total": len(events),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[m3/quote-audit-log] quote={quote_id} fail: {e}")
        raise HTTPException(status_code=500, detail="读取审计失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# M3 后端第二档(2026-04-26 CTO-15.15-M3-Lead 任务 4):
#   4.1 GET /api/monitoring/anomaly?brand_id=X
#   4.2 GET /api/m3/today?space=sales|delivery
#   4.3 GET /api/m3/customers/:id/lifecycle
#   4.4 GET /api/m3/delivery-queue
#
# 设计:
#   - 全部内联 RBAC(避免 require_quote_access 内部借 conn 跟本 conn race · BLOCK-1)
#   - 单 connection 一次查全 · 大幅减少前端 N+1
#   - 没数据返空 · 不 mock
# ============================================================

# --- 4.2 today · 销售/交付端今日聚合 ---
def _today_sales(brand_rows: list[dict]) -> dict:
    """从 brand_rows 聚合销售端 7 chip 计数 · stage 推断同 lifecycleMapper 简化版"""
    counts = {"all": 0, "follow_up": 0, "inquiry": 0, "diagnosing": 0,
              "to_quote": 0, "to_sign": 0, "renewal": 0}
    p1_brand_ids = []
    for r in brand_rows:
        counts["all"] += 1
        lq_status = r.get("lq_status")
        diag_count = r.get("diagnosis_count", 0)
        # 简化分类(PRD §6.1 心智)
        if lq_status in ("draft", "pricing_pending_review", "quoted"):
            counts["to_sign"] += 1
        elif lq_status in ("confirmed", "pending_payment"):
            counts["to_sign"] += 1
        elif lq_status == "paid":
            # 已签 · 续费窗?粗略看 service_end_date(此处简化跳过)
            pass
        elif diag_count == 0:
            counts["inquiry"] += 1
        elif diag_count > 0 and not lq_status:
            counts["to_quote"] += 1
        # P1 候选: stalled / 报价待签 / 续费窗
        if lq_status in ("draft", "pricing_pending_review", "quoted", "confirmed", "pending_payment"):
            p1_brand_ids.append(r["id"])

    return {"counts": counts, "p1_brand_ids": p1_brand_ids[:10]}


def _today_delivery(brand_rows: list[dict]) -> dict:
    """交付端 6 stat tile · 复用 lq_service_status / monitoring_enabled"""
    counts = {
        "writing": 0, "publishing": 0, "monitoring": 0,
        "pending_report": 0, "renewal_window": 0, "service_active": 0,
    }
    for r in brand_rows:
        if r.get("lq_service_status") == "active":
            counts["service_active"] += 1
        if r.get("lq_monitoring_enabled"):
            counts["monitoring"] += 1
    return {"counts": counts}


@router.get("/api/m3/today")
def get_m3_today(request: Request, space: str = "sales"):
    """今日聚合 · 解前端 N+N 拼

    space=sales: 7 chip 计数 + P1 brand_ids
    space=delivery: 6 stat tile 计数
    """
    from db.diagnosis_db import get_connection

    user = _get_user(request)
    is_admin = user.get("is_admin", False)
    client_brand_ids = user.get("client_brand_ids", []) or []
    user_id = user.get("user_id")

    if space not in ("sales", "delivery"):
        raise HTTPException(status_code=400, detail="space 必须是 sales 或 delivery")

    conn = get_connection()
    try:
        cur = conn.cursor()
        if is_admin:
            cur.execute(f"""
                SELECT {_BRAND_FIELDS}
                FROM brands b
                {_BRAND_LATERAL}
                WHERE (b.is_deleted IS NULL OR b.is_deleted = FALSE)
            """)
        elif client_brand_ids:
            placeholders = ",".join(["%s"] * len(client_brand_ids))
            cur.execute(f"""
                SELECT {_BRAND_FIELDS}
                FROM brands b
                {_BRAND_LATERAL}
                WHERE b.id IN ({placeholders})
                  AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
            """, client_brand_ids)
        elif user_id:
            cur.execute(f"""
                SELECT {_BRAND_FIELDS}
                FROM brands b
                {_BRAND_LATERAL}
                WHERE b.owner_user_id = %s
                  AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
            """, (user_id,))
        else:
            return {"success": True, "space": space, "counts": {}, "total": 0}
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()

        if space == "sales":
            agg = _today_sales(rows)
            return {"success": True, "space": "sales", **agg, "total": len(rows)}
        else:
            agg = _today_delivery(rows)
            return {"success": True, "space": "delivery", **agg, "total": len(rows)}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[m3/today] space={space} fail: {e}")
        raise HTTPException(status_code=500, detail="今日聚合失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# --- 任务 F · monitor engines BFF · 替前端 config.ts 硬编码 ---

# --- 4.4 delivery-queue · 按动作 × 客户聚合 ---
@router.get("/api/m3/delivery-queue")
def get_delivery_queue(request: Request):
    """按 4 类动作分桶 · 每桶按 brand 聚合

    actions:
      - confirm   · 待客户确认报价 (lq_status in 'draft','pricing_pending_review','quoted')
      - quote     · 待生成方案书   (有 latest diag · 没 lq)
      - renewal   · 续费窗内       (服务期已激活过 且 service_end_date <= today + 21 days · 含已到期)
      - inquiry   · 新询价          (无 lq · 无 diag)
    """
    from db.diagnosis_db import get_connection

    user = _get_user(request)
    is_admin = user.get("is_admin", False)
    client_brand_ids = user.get("client_brand_ids", []) or []
    user_id = user.get("user_id")

    conn = get_connection()
    try:
        cur = conn.cursor()
        if is_admin:
            cur.execute(f"""
                SELECT {_BRAND_FIELDS}
                FROM brands b
                {_BRAND_LATERAL}
                WHERE (b.is_deleted IS NULL OR b.is_deleted = FALSE)
            """)
        elif client_brand_ids:
            placeholders = ",".join(["%s"] * len(client_brand_ids))
            cur.execute(f"""
                SELECT {_BRAND_FIELDS}
                FROM brands b
                {_BRAND_LATERAL}
                WHERE b.id IN ({placeholders})
                  AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
            """, client_brand_ids)
        elif user_id:
            cur.execute(f"""
                SELECT {_BRAND_FIELDS}
                FROM brands b
                {_BRAND_LATERAL}
                WHERE b.owner_user_id = %s
                  AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
            """, (user_id,))
        else:
            return {"success": True, "buckets": {}, "total": 0}
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()

        from datetime import date, timedelta
        today = date.today()
        renewal_cutoff = today + timedelta(days=21)

        buckets = {"confirm": [], "quote": [], "renewal": [], "inquiry": []}

        for r in rows:
            entry = {
                "id": r["id"],
                "name": r["name"],
                "industry": r.get("industry") or display_name(r.get("industry_category")),
                "brand_type": r.get("brand_type", "legacy"),
                "diagnosis_count": r.get("diagnosis_count", 0),
                "latest_score": r.get("latest_score"),
                "latest_diagnosis_id": r.get("latest_diagnosis_id"),
                "latest_quote": _row_to_latest_quote(r),
            }

            lq_status = r.get("lq_status")
            lq_service_status = r.get("lq_service_status")
            lq_service_end = r.get("lq_service_end_date")
            diag_count = r.get("diagnosis_count", 0)

            if lq_status in ("draft", "pricing_pending_review", "quoted", "confirmed", "pending_payment"):
                buckets["confirm"].append(entry)
            # [服务期 SSOT 2026-08-06 §1.5] 旧判据是 `lq_service_status == "active"` ——
            #   而 scheduler 在到期前 7 天就把 service_status 翻成 'expiring'(到期后一直留在
            #   'expiring'/'expired')。于是客户**恰好在最该续费的那几天掉出续费桶**,
            #   代理面板上一声不响地消失。生产实证:晨光富士 #286 / 栖舍 #372 两张都是
            #   service_status='expiring' → 两张都不在续费桶里。
            #   现在按"服务期激活过 + 到期日进 21 天窗(含已过期)"取,过期的排在最前面。
            elif (
                lq_service_status in ("active", "expiring", "expired")
                and lq_service_end
                and lq_service_end <= renewal_cutoff
            ):
                buckets["renewal"].append(entry)
            elif diag_count > 0 and not lq_status:
                buckets["quote"].append(entry)
            elif diag_count == 0 and not lq_status:
                buckets["inquiry"].append(entry)

        return {
            "success": True,
            "buckets": buckets,
            "counts": {k: len(v) for k, v in buckets.items()},
            "total": len(rows),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[m3/delivery-queue] fail: {e}")
        raise HTTPException(status_code=500, detail="交付队列聚合失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# Phase E.8 · v2 §F3 · 流转健康度自检(CTO-15.10 2026-04-27)
# ============================================================
#
# 核心痛点(老板原文):
#   "我没有 paid 客户走到 stage 5 · 5→6→7 自动衔接未实测验证"
# 真实跑验收需要老板找真实客户 · 但代码层可以补"前置条件检查"
# 帮代理一眼看出"我这个客户卡在哪一步 / 缺什么"
# ============================================================


@router.get("/api/m3/pipeline-health/{brand_id}")
def get_pipeline_health(brand_id: int, request: Request):
    """检查 brand stage 5-7 自动衔接前置数据 · 帮代理排查"卡哪了"

    返回示例:
    {
        "success": True,
        "brand_id": 270,
        "stage": "writing",
        "checks": [
            {"name": "服务期已激活", "ok": true},
            {"name": "关键词已入库", "ok": true, "detail": "8 个关键词"},
            {"name": "写作项目已生成", "ok": false, "detail": "0 篇 · 进写作大厅触发"},
            {"name": "已发布证据 ≥ 1", "ok": false, "detail": "代理未上传"}
        ],
        "next_blocker": "写作项目尚未生成 · 进 /m3/delivery/writing 触发"
    }
    """
    from auth.brand_access import require_brand_access
    from db.diagnosis_db import get_connection

    require_brand_access(request, brand_id)
    conn = get_connection()
    try:
        cur = conn.cursor()

        # 查 latest quote(stage 5+ 必须有 paid quote)
        cur.execute(
            """SELECT id, status, service_status, service_start_date, service_end_date
               FROM quotes
               WHERE brand_id = %s AND deleted_at IS NULL
               ORDER BY created_at DESC LIMIT 1""",
            (brand_id,),
        )
        quote = cur.fetchone()
        quote_id = quote.get("id") if quote else None
        service_active = quote and quote.get("service_status") == "active"

        # 查 confirmed_keywords 数量
        kw_count = 0
        if quote_id:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM confirmed_keywords WHERE quote_id = %s",
                (quote_id,),
            )
            row = cur.fetchone()
            kw_count = int((row or {}).get("cnt") or 0)

        # 查 articles 数量
        article_total = 0
        article_published = 0
        if quote_id:
            try:
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM articles WHERE quote_id = %s",
                    (quote_id,),
                )
                article_total = int((cur.fetchone() or {}).get("cnt") or 0)
                # [WP7 cutover 2026-08-17] 原来读 `articles.first_published_at`。
                # 那是 P0.4 为 O(1) dashboard 加的**冗余列**,不是事实源:
                #   · 它只在人工登记链回写,代发/插件两条链不写 → 少算;
                #   · 它一旦写上就永不回退 → 撤稿之后仍然显示"已发布",多算。
                # 统一走 quote-scoped 投影(唯一 canonical 口径)。
                from services.publication_stage_adapters import quote_published_active

                _active = quote_published_active(quote_id, cursor=cur)
                # None = 投影不可用。不许糊成 0 —— 0 与"不知道"在界面上长得一样。
                article_published = int(_active) if _active is not None else 0
            except Exception:
                # 表 / 字段可能未 migration · 降级返 0(不阻断)
                pass

        # 查 monitoring(已发布 ≥ 1 且监测启动)
        monitoring_running = False
        try:
            cur.execute(
                "SELECT monitoring_enabled, monitoring_started_at FROM quotes WHERE id = %s",
                (quote_id,),
            )
            mrow = cur.fetchone()
            monitoring_running = bool(
                mrow and mrow.get("monitoring_enabled") and mrow.get("monitoring_started_at")
            )
        except Exception:
            pass

        # 查最近 monitoring 数据(7 天内)
        monitoring_recent = 0
        try:
            from services.monitoring_identity_review import aggregate_eligible_sql
            cur.execute(
                f"""SELECT COUNT(*) AS cnt FROM monitoring_results mr
                   JOIN confirmed_keywords ck ON ck.id = mr.keyword_id
                   WHERE ck.quote_id = %s
                     AND mr.tested_at > NOW() - INTERVAL '7 days'
                     AND {aggregate_eligible_sql('mr')}""",
                (quote_id,),
            )
            monitoring_recent = int((cur.fetchone() or {}).get("cnt") or 0)
        except Exception:
            pass

        cur.close()

        # 组装 checks
        checks = []
        next_blocker = None
        current_stage = "unknown"

        # stage 4 末:服务期激活前置
        if not quote_id:
            checks.append({"name": "已生成报价", "ok": False, "detail": "请先做诊断 + 出报价方案"})
            next_blocker = "请先做诊断 + 出报价方案"
            current_stage = "diagnosis"
        elif not service_active:
            checks.append({"name": "已生成报价", "ok": True, "detail": f"quote_id={quote_id}"})
            checks.append(
                {
                    "name": "服务期已激活",
                    "ok": False,
                    "detail": f"quote.status={quote.get('status')} · 客户已付款?点决策条主按钮激活",
                }
            )
            next_blocker = "服务期未激活 · 客户付款后点'激活服务期'按钮"
            current_stage = "quote_pending_activation"
        else:
            checks.append({"name": "服务期已激活", "ok": True, "detail": f"{quote.get('service_start_date')} ~ {quote.get('service_end_date')}"})

            # stage 5:写作前置
            if kw_count == 0:
                checks.append({"name": "关键词已入库", "ok": False, "detail": "0 个 · 客户在 /s/:token 选词后会自动同步"})
                next_blocker = "关键词未入库 · 发选词链给客户"
                current_stage = "writing_blocked_no_keywords"
            else:
                checks.append({"name": "关键词已入库", "ok": True, "detail": f"{kw_count} 个"})

                # stage 5 → 6:写作项目
                if article_total == 0:
                    checks.append(
                        {
                            "name": "写作项目已生成",
                            "ok": False,
                            "detail": "0 篇 · 进写作大厅触发批量生成",
                        }
                    )
                    next_blocker = "写作项目尚未生成 · 进 /m3/delivery/writing 触发"
                    current_stage = "writing"
                else:
                    checks.append(
                        {
                            "name": "写作项目已生成",
                            "ok": True,
                            "detail": f"{article_total} 篇 · 已发 {article_published}",
                        }
                    )

                    # stage 6 → 7:发布证据
                    if article_published == 0:
                        checks.append(
                            {
                                "name": "已发布证据 ≥ 1",
                                "ok": False,
                                "detail": "代理需在发布中心上传截图证据",
                            }
                        )
                        next_blocker = "已发布证据缺失 · 进 /m3/delivery/publish 上传"
                        current_stage = "publishing"
                    else:
                        checks.append(
                            {
                                "name": "已发布证据",
                                "ok": True,
                                "detail": f"{article_published} 篇已发",
                            }
                        )

                        # stage 7:监测启动
                        if not monitoring_running:
                            checks.append(
                                {
                                    "name": "监测已启动",
                                    "ok": False,
                                    "detail": "scheduler 未启动 · 联系管理员",
                                }
                            )
                            next_blocker = "监测未启动 · 自动 scheduler 应已触发 · 报告异常"
                            current_stage = "monitoring_blocked"
                        else:
                            checks.append({"name": "监测已启动", "ok": True})
                            if monitoring_recent == 0:
                                checks.append(
                                    {
                                        "name": "近 7 日监测数据",
                                        "ok": False,
                                        "detail": "0 条 · 4 引擎可能限流",
                                    }
                                )
                                next_blocker = "近 7 日监测数据缺失 · 检查 4 引擎 API"
                                current_stage = "monitoring_no_data"
                            else:
                                checks.append(
                                    {
                                        "name": "近 7 日监测数据",
                                        "ok": True,
                                        "detail": f"{monitoring_recent} 条",
                                    }
                                )
                                current_stage = "healthy_in_service"

        return {
            "success": True,
            "brand_id": brand_id,
            "quote_id": quote_id,
            "stage": current_stage,
            "checks": checks,
            "next_blocker": next_blocker,
            "stats": {
                "keyword_count": kw_count,
                "article_total": article_total,
                "article_published": article_published,
                "monitoring_running": monitoring_running,
                "monitoring_recent_7d": monitoring_recent,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[m3/pipeline-health] brand={brand_id} fail: {e}")
        raise HTTPException(status_code=500, detail="流转健康度查询失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# Phase E.7 · v2 §F2 · 代理记账区(CTO-15.10 2026-04-27)
# ============================================================
#
# 玩法 B:客户付款不走平台 · 代理需要在 OmniRank 后台私人记账
# 用途:
#   - 已收款多少 · 部分收款多少 · 没收款
#   - "已收款超 24h 未激活" 红标提醒(防漏激活)
#   - 不取代真实付款记录 · 只是代理私人备注
# ============================================================


# ============================================================
# Phase C · v2 §4 · 6 类客户链接聚合(CTO-15.10 2026-04-27)
# ============================================================
