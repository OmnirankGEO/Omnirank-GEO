"""
文章代发数据库模块
- mhz_media: 外部发布通道媒体列表（自动同步）
- publish_batches: 发布批次
- publish_orders: 代发订单（每篇文章一个）
- publish_order_items: 订单明细（每个媒体一条）
"""

import logging
import json
import math
from datetime import datetime
from db.connection import get_connection, get_db
from psycopg2.extras import Json

# [WO_VOCAB_CONVERGENCE 2026-08-09] 价格档识别(纯字符串映射 · 不查库不抛异常)
from tools.media_vocab_normalize import detect_listing_slot, LISTING_SLOTS

logger = logging.getLogger("GEO-Publish")

# 加价比例（管理员可调整）
DEFAULT_MARKUP_RATIO = 2.0
POINTS_PER_YUAN = 130


def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_name = %s
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

def init_publish_tables():
    """创建发布相关表（幂等）"""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mhz_media (
                id INTEGER PRIMARY KEY,
                media_name TEXT NOT NULL,
                category TEXT,
                media_type TEXT DEFAULT 'media',
                platform TEXT,
                price_normal NUMERIC(10,2),
                price_vip NUMERIC(10,2),
                price_svip NUMERIC(10,2),
                our_price_yuan NUMERIC(10,2),
                our_price_points BIGINT,
                inclusion_rate TEXT,
                avg_publish_time TEXT,
                publish_rate TEXT,
                pc_weight INTEGER DEFAULT 0,
                mobile_weight INTEGER DEFAULT 0,
                news_source TEXT,
                link_type TEXT,
                can_geo INTEGER DEFAULT 0,
                case_link TEXT,
                remark TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mhz_category ON mhz_media(category)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mhz_platform ON mhz_media(platform)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mhz_price ON mhz_media(our_price_points)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mhz_active ON mhz_media(is_active)")
        # [2026-06-02 GEO CTO] 联系方式发布策略(none/website_only/full_contact · 默认 none 保守)
        _safe_add_column(cursor, "mhz_media", "contact_policy", "TEXT DEFAULT 'none'")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS publish_batches (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                article_count INTEGER DEFAULT 0,
                total_publish_count INTEGER DEFAULT 0,
                total_cost_points BIGINT DEFAULT 0,
                status TEXT DEFAULT 'processing',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pb_user ON publish_batches(user_id, created_at DESC)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS publish_orders (
                id SERIAL PRIMARY KEY,
                batch_id TEXT REFERENCES publish_batches(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                article_id INTEGER,
                article_title TEXT NOT NULL,
                article_content TEXT,
                status TEXT DEFAULT 'pending',
                total_cost_points BIGINT DEFAULT 0,
                media_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_po_user ON publish_orders(user_id, created_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_po_batch ON publish_orders(batch_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS publish_order_items (
                id SERIAL PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES publish_orders(id) ON DELETE CASCADE,
                media_id INTEGER NOT NULL,
                media_name TEXT NOT NULL,
                cost_yuan NUMERIC(10,2),
                cost_points BIGINT,
                mhz_cost_yuan NUMERIC(10,2),
                mhz_order_id TEXT,
                status TEXT DEFAULT 'pending',
                publish_url TEXT,
                reject_reason TEXT,
                submitted_at TIMESTAMP,
                published_at TIMESTAMP,
                refunded_at TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_poi_order ON publish_order_items(order_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_poi_status ON publish_order_items(status)")

        # v4: publish_orders + publish_batches 加 brand_id
        try:
            cursor.execute("ALTER TABLE publish_orders ADD COLUMN IF NOT EXISTS brand_id INTEGER")
            cursor.execute("ALTER TABLE publish_batches ADD COLUMN IF NOT EXISTS brand_id INTEGER")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_po_brand ON publish_orders(brand_id) WHERE brand_id IS NOT NULL")
        except Exception:
            pass

        # 分层定价规则表（全局/分类/单个媒体）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mhz_pricing_rules (
                id SERIAL PRIMARY KEY,
                rule_type TEXT NOT NULL,
                match_field TEXT,
                match_value TEXT,
                media_id INTEGER,
                markup_ratio NUMERIC(4,2),
                fixed_price_yuan NUMERIC(10,2),
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT chk_rule_type CHECK (rule_type IN ('global', 'category', 'platform', 'media'))
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS media_effective_pool (
                id BIGSERIAL PRIMARY KEY,
                media_source TEXT NOT NULL,
                media_id BIGINT NOT NULL,
                platform_name TEXT,
                industry TEXT,
                quality_score NUMERIC(5,2),
                evidence_score NUMERIC(5,2),
                price_score NUMERIC(5,2),
                noise_score NUMERIC(5,2),
                effective_score NUMERIC(5,2),
                is_recommendable BOOLEAN DEFAULT FALSE,
                tier TEXT,
                tags JSONB,
                reasons JSONB,
                updated_at TIMESTAMP DEFAULT NOW(),
                UNIQUE (media_source, media_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mep_tier_recommendable ON media_effective_pool(tier, is_recommendable)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mep_industry ON media_effective_pool(industry)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mep_score ON media_effective_pool(effective_score DESC)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS article_publish_analysis (
                article_id INTEGER PRIMARY KEY,
                article_title TEXT,
                analysis JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        _safe_add_column(cursor, "article_publish_analysis", "article_type", "TEXT")
        _safe_add_column(cursor, "article_publish_analysis", "semantic_keywords", "JSONB")
        _safe_add_column(cursor, "article_publish_analysis", "publish_goal", "TEXT")
        _safe_add_column(cursor, "article_publish_analysis", "recommended_platforms", "JSONB")
        _safe_add_column(cursor, "article_publish_analysis", "risk_tags", "JSONB")
        _safe_add_column(cursor, "article_publish_analysis", "analysis_version", "TEXT DEFAULT 'v2.3'")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS publish_decision_snapshots (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                brand_id INTEGER NOT NULL,
                quote_id INTEGER,
                article_ids JSONB NOT NULL,
                recommendation_payload JSONB,
                evidence_payload JSONB,
                selected_media JSONB,
                selected_wemedia JSONB,
                estimated_points INTEGER,
                disclaimer_version TEXT,
                terms_version TEXT,
                analysis_version TEXT,
                recommendation_level SMALLINT,
                retention_until TIMESTAMP,
                request_id TEXT,
                confirmed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE (request_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pds_brand ON publish_decision_snapshots(brand_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pds_user ON publish_decision_snapshots(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pds_retention ON publish_decision_snapshots(retention_until)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS publish_outcome_records (
                id BIGSERIAL PRIMARY KEY,
                snapshot_id BIGINT REFERENCES publish_decision_snapshots(id),
                publish_status TEXT,
                publish_url TEXT,
                inclusion_status TEXT,
                ai_citations_delta_30d INTEGER,
                monitoring_brand_score_delta_30d NUMERIC(5,2),
                failed_reason TEXT,
                synced_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_por_snapshot ON publish_outcome_records(snapshot_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_por_synced ON publish_outcome_records(synced_at)")

        # [媒体平衡 T2 规则② · 2026-07-29] 缺货降位需求计数。
        # 采购按"被引强度 × 降位次数"的真实信号补货，不拍脑袋。
        # 每次因缺货把某个被引域降位替代，就在这里累加一笔。
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS geo_media_substitution_demand (
                id BIGSERIAL PRIMARY KEY,
                missing_domain VARCHAR(160) NOT NULL,
                missing_label VARCHAR(160) NOT NULL DEFAULT '',
                role VARCHAR(40) NOT NULL DEFAULT '',
                family_key VARCHAR(60) NOT NULL DEFAULT '',
                family_label VARCHAR(120) NOT NULL DEFAULT '',
                citation_count INTEGER NOT NULL DEFAULT 0,
                citation_share REAL NOT NULL DEFAULT 0,
                substituted_domain VARCHAR(160) NOT NULL DEFAULT '',
                substituted_media_name VARCHAR(200) NOT NULL DEFAULT '',
                self_serve BOOLEAN NOT NULL DEFAULT FALSE,
                industry VARCHAR(80) NOT NULL DEFAULT '',
                demand_count INTEGER NOT NULL DEFAULT 1,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_gmsd_domain_industry "
            "ON geo_media_substitution_demand(missing_domain, industry)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_gmsd_demand "
            "ON geo_media_substitution_demand((citation_count * demand_count) DESC)"
        )

        # [多供应商 2026-07-27] 媒体库从"只有媒介盒子"扩成多渠道(新增快易播)。
        #   provider          -- 'mhz' 媒介盒子 / 'kyb' 快易播。存量行默认 'mhz',零改动。
        #   provider_media_id -- 上游真实媒体 id。**下单必须用它**,不能用我们的主键
        #                        (两家 id 区间重叠:kyb 33493-673656 vs mhz_media 2096-128636,
        #                         所以 kyb 行的主键做了偏移,见 services/kuaiyibo/config.py)。
        #   source_domain     -- 从案例 URL 归一化出的真实发布域。补上媒体库一直缺的
        #                        domain 维度 —— 之前只能按名字匹配,实测「界面」141 条名字命中
        #                        里只有 9 条真发 jiemian.com,按名字算会严重虚高。
        # mhz_wemedia / mhz_short_video 由 db.meijiehezi_db.init_mhz_tables() 建，
        # 与本函数没有固定先后顺序 —— 全新库上本函数可能先跑。所以逐表判存在，
        # 不存在就跳过（那边建完后下一次启动补上），绝不因此让启动链崩掉。
        for _tbl in ("mhz_media", "mhz_wemedia", "mhz_short_video"):
            cursor.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (_tbl,))
            _row = cursor.fetchone()
            if not (_row and _row.get("ok")):
                logger.info("[Publish] %s 尚未建表,provider 列稍后补", _tbl)
                continue
            cursor.execute(
                f"ALTER TABLE {_tbl} ADD COLUMN IF NOT EXISTS provider VARCHAR(16) NOT NULL DEFAULT 'mhz'")
            cursor.execute(
                f"ALTER TABLE {_tbl} ADD COLUMN IF NOT EXISTS provider_media_id INTEGER")
            cursor.execute(
                f"ALTER TABLE {_tbl} ADD COLUMN IF NOT EXISTS source_domain VARCHAR(160)")
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_tbl}_provider ON {_tbl}(provider)")
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_tbl}_source_domain ON {_tbl}(source_domain)")

        # 供应商自记账:对方没有余额接口(2026-07-27 已确认「余额先不管」),
        # 运营要靠"我们自己花了多少"判断该不该充值(低于 1000 元补充)。
        # 只记我方视角的应付金额,不假装是对方账本。
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS provider_spend_ledger (
                id BIGSERIAL PRIMARY KEY,
                provider VARCHAR(16) NOT NULL,
                provider_order_id VARCHAR(64) NOT NULL,
                provider_media_id INTEGER,
                media_name TEXT NOT NULL DEFAULT '',
                cost_yuan NUMERIC(12,2) NOT NULL DEFAULT 0,
                outcome VARCHAR(20) NOT NULL DEFAULT 'pending',
                refunded BOOLEAN NOT NULL DEFAULT FALSE,
                order_item_id INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                settled_at TIMESTAMPTZ
            )
        """)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_spend_order "
            "ON provider_spend_ledger(provider, provider_order_id)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_spend_open "
            "ON provider_spend_ledger(provider, outcome)")

        logger.info("[Publish] 发布表初始化完成")


# ==================== 定价规则 ====================

def get_price_for_media(media: dict) -> dict:
    """
    根据分层定价规则计算媒体售价。
    优先级：单个媒体 > 分类/平台规则 > 全局规则 > 默认加价比例
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        vip_price = float(media.get("price_vip") or media.get("price") or 0)
        if vip_price <= 0:
            return {"yuan": 0, "points": 0}

        # 1. 查单个媒体规则
        cursor.execute(
            "SELECT * FROM mhz_pricing_rules WHERE rule_type='media' AND media_id=%s AND is_active=TRUE LIMIT 1",
            (media.get("id"),)
        )
        rule = cursor.fetchone()

        # 2. 查分类规则
        if not rule and media.get("category"):
            cursor.execute(
                "SELECT * FROM mhz_pricing_rules WHERE rule_type='category' AND match_value=%s AND is_active=TRUE LIMIT 1",
                (media.get("category"),)
            )
            rule = cursor.fetchone()

        # 3. 查平台规则
        if not rule and media.get("platform"):
            cursor.execute(
                "SELECT * FROM mhz_pricing_rules WHERE rule_type='platform' AND match_value=%s AND is_active=TRUE LIMIT 1",
                (media.get("platform"),)
            )
            rule = cursor.fetchone()

        # 4. 查全局规则
        if not rule:
            cursor.execute(
                "SELECT * FROM mhz_pricing_rules WHERE rule_type='global' AND is_active=TRUE LIMIT 1"
            )
            rule = cursor.fetchone()

        if rule:
            rule = dict(rule)
            if rule.get("fixed_price_yuan") and float(rule["fixed_price_yuan"]) > 0:
                our_yuan = float(rule["fixed_price_yuan"])
            elif rule.get("markup_ratio") and float(rule["markup_ratio"]) > 0:
                our_yuan = math.ceil(vip_price * float(rule["markup_ratio"]))
            else:
                our_yuan = math.ceil(vip_price * get_markup_ratio())
        else:
            our_yuan = math.ceil(vip_price * get_markup_ratio())

        return {"yuan": our_yuan, "points": int(our_yuan * POINTS_PER_YUAN)}
    finally:
        conn.close()


def get_pricing_rules() -> list:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM mhz_pricing_rules WHERE is_active=TRUE ORDER BY rule_type, id")
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def upsert_pricing_rule(rule_type: str, match_value: str = None,
                        media_id: int = None, markup_ratio: float = None,
                        fixed_price_yuan: float = None) -> dict:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO mhz_pricing_rules (rule_type, match_field, match_value, media_id, markup_ratio, fixed_price_yuan)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (rule_type,
              'category' if rule_type == 'category' else 'platform' if rule_type == 'platform' else None,
              match_value, media_id, markup_ratio, fixed_price_yuan))
        return dict(cursor.fetchone())


def delete_pricing_rule(rule_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE mhz_pricing_rules SET is_active=FALSE WHERE id=%s", (rule_id,))


# ==================== 系统配置 ====================

def get_markup_ratio() -> float:
    """获取当前媒体加价比例。

    [FIX·P2-A 媒体系数收口] 统一到 mhz_config.markup_ratio(与代发同源·admin 改 /api/meijiehezi/admin/config/markup
    一处即生效两链路);fallback 老 system_config.publish_markup_ratio(向后兼容)→ DEFAULT_MARKUP_RATIO。
    注:两源历史可能不一致(本为 pre-existing),收口后以 mhz_config 为准;green 需核对两源现值确认无突变。
    """
    # 优先 mhz_config(SSOT·代发真实生效·与自助发布统一)
    try:
        from db.meijiehezi_db import get_config
        v = get_config("markup_ratio")
        if v:
            return float(v)
    except Exception:
        pass
    # fallback 老 system_config(向后兼容)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_config WHERE key = 'publish_markup_ratio'")
        row = cursor.fetchone()
        if row and row["value"]:
            return float(row["value"])
    except Exception:
        pass
    finally:
        conn.close()
    return DEFAULT_MARKUP_RATIO


def calculate_our_price(vip_price: float, markup_ratio: float = None) -> dict:
    """计算我们的售价"""
    ratio = markup_ratio or get_markup_ratio()
    our_yuan = math.ceil(vip_price * ratio)
    our_points = our_yuan * POINTS_PER_YUAN
    return {"yuan": our_yuan, "points": our_points}


# ==================== mhz_media CRUD ====================

def upsert_media(media_data: dict):
    """新增或更新媒体（同步用）"""
    # [WO_VOCAB_CONVERGENCE 2026-08-09] 本入口写的是 category(不是 resource_type_name),
    #   而 category 里同样躺着价格档(2026-08-09 实测 548 行)。只在 meijiehezi_db 那一处落闸
    #   会漏掉这条路径 —— 四个写入点必须全接。本入口不写 area,故只需 listing_slot。
    listing_slot = detect_listing_slot(
        media_data.get("resource_type_name"), media_data.get("category")
    )
    ratio = get_markup_ratio()
    vip_price = media_data.get("price_vip") or 0
    our = calculate_our_price(vip_price, ratio) if vip_price > 0 else {"yuan": 0, "points": 0}

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO mhz_media (
                id, media_name, category, media_type, platform,
                price_normal, price_vip, price_svip,
                our_price_yuan, our_price_points,
                inclusion_rate, avg_publish_time, publish_rate,
                pc_weight, mobile_weight, news_source, link_type,
                can_geo, case_link, remark, listing_slot, is_active, synced_at
            ) VALUES (
                %(id)s, %(media_name)s, %(category)s, %(media_type)s, %(platform)s,
                %(price_normal)s, %(price_vip)s, %(price_svip)s,
                %(our_yuan)s, %(our_points)s,
                %(inclusion_rate)s, %(avg_publish_time)s, %(publish_rate)s,
                %(pc_weight)s, %(mobile_weight)s, %(news_source)s, %(link_type)s,
                %(can_geo)s, %(case_link)s, %(remark)s, %(listing_slot)s, TRUE, CURRENT_TIMESTAMP
            )
            ON CONFLICT (id) DO UPDATE SET
                media_name = EXCLUDED.media_name,
                category = EXCLUDED.category,
                listing_slot = EXCLUDED.listing_slot,
                price_normal = EXCLUDED.price_normal,
                price_vip = EXCLUDED.price_vip,
                price_svip = EXCLUDED.price_svip,
                our_price_yuan = EXCLUDED.our_price_yuan,
                our_price_points = EXCLUDED.our_price_points,
                inclusion_rate = EXCLUDED.inclusion_rate,
                avg_publish_time = EXCLUDED.avg_publish_time,
                publish_rate = EXCLUDED.publish_rate,
                pc_weight = EXCLUDED.pc_weight,
                mobile_weight = EXCLUDED.mobile_weight,
                news_source = EXCLUDED.news_source,
                link_type = EXCLUDED.link_type,
                can_geo = EXCLUDED.can_geo,
                case_link = EXCLUDED.case_link,
                remark = EXCLUDED.remark,
                is_active = TRUE,
                synced_at = CURRENT_TIMESTAMP
        """, {
            **media_data,
            "our_yuan": our["yuan"],
            "our_points": our["points"],
            "listing_slot": listing_slot,
        })
        conn.commit()
    finally:
        conn.close()


def bulk_upsert_media(media_list: list):
    """批量新增或更新媒体"""
    if not media_list:
        return
    ratio = get_markup_ratio()
    with get_db() as conn:
        cursor = conn.cursor()
        for m in media_list:
            vip_price = m.get("price_vip") or 0
            our = calculate_our_price(vip_price, ratio) if vip_price > 0 else {"yuan": 0, "points": 0}
            cursor.execute("""
                INSERT INTO mhz_media (
                    id, media_name, category, media_type, platform,
                    price_normal, price_vip, price_svip,
                    our_price_yuan, our_price_points,
                    inclusion_rate, avg_publish_time, publish_rate,
                    pc_weight, mobile_weight, news_source, link_type,
                    can_geo, case_link, remark, listing_slot, is_active, synced_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, CURRENT_TIMESTAMP
                )
                ON CONFLICT (id) DO UPDATE SET
                    media_name = EXCLUDED.media_name,
                    price_vip = EXCLUDED.price_vip,
                    our_price_yuan = EXCLUDED.our_price_yuan,
                    our_price_points = EXCLUDED.our_price_points,
                    listing_slot = EXCLUDED.listing_slot,
                    is_active = TRUE,
                    synced_at = CURRENT_TIMESTAMP
            """, (
                m.get("id"), m.get("media_name"), m.get("category"),
                m.get("media_type", "media"), m.get("platform"),
                m.get("price_normal"), m.get("price_vip"), m.get("price_svip"),
                our["yuan"], our["points"],
                m.get("inclusion_rate"), m.get("avg_publish_time"), m.get("publish_rate"),
                m.get("pc_weight", 0), m.get("mobile_weight", 0),
                m.get("news_source"), m.get("link_type"),
                m.get("can_geo", 0), m.get("case_link"), m.get("remark"),
                # [WO_VOCAB_CONVERGENCE 2026-08-09] 第 4 个写入点 · 同上,只落标记不动原值
                detect_listing_slot(m.get("resource_type_name"), m.get("category")),
            ))
        logger.info(f"[Publish] 批量同步 {len(media_list)} 条媒体")


def mark_media_inactive(active_ids: set):
    """将不在 active_ids 中的媒体标记为下架"""
    if not active_ids:
        return
    with get_db() as conn:
        cursor = conn.cursor()
        # 将所有不在列表中的标记为 inactive
        placeholders = ",".join(["%s"] * len(active_ids))
        cursor.execute(f"""
            UPDATE mhz_media SET is_active = FALSE
            WHERE id NOT IN ({placeholders}) AND is_active = TRUE
        """, tuple(active_ids))


def get_media_list(page: int = 1, limit: int = 50, category: str = None,
                   platform: str = None, news_source: str = None,
                   can_geo: bool = None, price_min: int = None,
                   price_max: int = None, search: str = None,
                   media_type: str = None, high_value: bool = None,
                   weight_min: int = None,
                   sort_by: str = "our_price_points", sort_dir: str = "asc") -> dict:
    """分页查询媒体列表（支持筛选）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        conditions = ["is_active = TRUE"]
        params = []

        if media_type:
            conditions.append("media_type = %s")
            params.append(media_type)
        if category:
            conditions.append("category = %s")
            params.append(category)
        if platform:
            conditions.append("platform = %s")
            params.append(platform)
        if news_source:
            conditions.append("news_source = %s")
            params.append(news_source)
        if can_geo is not None:
            conditions.append("can_geo = %s")
            params.append(1 if can_geo else 0)
        if price_min is not None:
            conditions.append("our_price_points >= %s")
            params.append(price_min)
        if price_max is not None:
            conditions.append("our_price_points <= %s")
            params.append(price_max)
        if weight_min is not None:
            conditions.append("pc_weight >= %s")
            params.append(weight_min)
        if high_value:
            # 高价值：权重>=4 或 百度新闻源
            conditions.append("(pc_weight >= 4 OR news_source = '百度新闻源')")
        if search:
            conditions.append("media_name ILIKE %s")
            params.append(f"%{search}%")

        where = " AND ".join(conditions)

        # 安全的排序字段
        allowed_sorts = {"our_price_points", "inclusion_rate", "pc_weight", "mobile_weight", "media_name"}
        if sort_by not in allowed_sorts:
            sort_by = "our_price_points"
        direction = "DESC" if sort_dir.upper() == "DESC" else "ASC"

        # 总数
        cursor.execute(f"SELECT COUNT(*) as cnt FROM mhz_media WHERE {where}", params)
        total = cursor.fetchone()["cnt"]

        # 分页数据
        offset = (page - 1) * limit
        cursor.execute(f"""
            SELECT * FROM mhz_media
            WHERE {where}
            ORDER BY {sort_by} {direction}
            LIMIT %s OFFSET %s
        """, params + [limit, offset])

        media = [dict(r) for r in cursor.fetchall()]
        return {
            "media": media,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": math.ceil(total / limit) if limit > 0 else 0,
        }
    finally:
        conn.close()


def get_media_by_ids(media_ids: list) -> list:
    """根据 ID 列表批量获取媒体"""
    if not media_ids:
        return []
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholders = ",".join(["%s"] * len(media_ids))
        cursor.execute(f"""
            SELECT * FROM mhz_media
            WHERE id IN ({placeholders}) AND is_active = TRUE
        """, tuple(media_ids))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def get_media_categories() -> list:
    """获取所有媒体分类（筛选用）

    P0 fix 2026-04-19: 原查 mhz_media.category 字段全为 NULL (历史遗留错位).
    改查 resource_type_name (真实有数据: "新闻资讯"/"女性时尚" 等).
    签名不变, 返回 list[str], 兼容历史调用方.

    [WO_VOCAB_CONVERGENCE 2026-08-09] 排除上架档位:「套餐系列 / 十元专区 / 最新秒杀」
      是价格档不是内容分类,却混在同一列里 → 分类下拉里冒出三个假分类。
      这是"不再污染体裁列"的**可验出口**:落 listing_slot 只是把档位识别出来,
      要让它真的不再当分类用,得在消费侧把它挡掉。
      🔴 不靠 listing_slot 列做过滤而是直接按值排除 —— 迁移漏跑 / 回填没跑时
      这条依然生效(不把正确性押在"另一步跑过了"上)。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT resource_type_name FROM mhz_media
            WHERE is_active = TRUE AND resource_type_name IS NOT NULL AND resource_type_name != ''
              AND resource_type_name <> ALL(%s)
            ORDER BY resource_type_name
        """, (sorted(LISTING_SLOTS),))
        return [r["resource_type_name"] for r in cursor.fetchall()]
    finally:
        conn.close()


def get_media_platforms() -> list:
    """获取所有平台（筛选用）

    P0 fix 2026-04-19: 原查 mhz_media.platform 字段全为 NULL.
    改查 portal_media (真实有数据: "垂直媒体"/"门户网站" 等).
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT portal_media FROM mhz_media
            WHERE is_active = TRUE AND portal_media IS NOT NULL AND portal_media != ''
            ORDER BY portal_media
        """)
        return [r["portal_media"] for r in cursor.fetchall()]
    finally:
        conn.close()


# ==================== publish_batches CRUD ====================

def create_batch(batch_id: str, user_id: int, article_count: int,
                 total_publish_count: int, total_cost_points: int) -> dict:
    """创建发布批次"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO publish_batches (id, user_id, article_count, total_publish_count, total_cost_points)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
        """, (batch_id, user_id, article_count, total_publish_count, total_cost_points))
        conn.commit()
        return dict(cursor.fetchone())
    finally:
        conn.close()


def get_batch(batch_id: str) -> dict:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM publish_batches WHERE id = %s", (batch_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_batches(user_id: int, limit: int = 20, offset: int = 0) -> list:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM publish_batches
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (user_id, limit, offset))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def update_batch_status(batch_id: str, status: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE publish_batches SET status = %s WHERE id = %s",
            (status, batch_id)
        )


# ==================== publish_orders CRUD ====================

def create_order(batch_id: str, user_id: int, article_id: int,
                 article_title: str, article_content: str,
                 media_count: int, total_cost_points: int) -> dict:
    """创建单篇代发订单"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        from services.article_review_gate import assert_publication_eligible
        assert_publication_eligible(article_id, cursor=cursor)
        cursor.execute("""
            INSERT INTO publish_orders
                (batch_id, user_id, article_id, article_title, article_content,
                 media_count, total_cost_points)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (batch_id, user_id, article_id, article_title, article_content,
              media_count, total_cost_points))
        conn.commit()
        return dict(cursor.fetchone())
    finally:
        conn.close()


def get_order(order_id: int) -> dict:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM publish_orders WHERE id = %s", (order_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_orders(user_id: int, batch_id: str = None,
                    limit: int = 20, offset: int = 0) -> list:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if batch_id:
            cursor.execute("""
                SELECT * FROM publish_orders
                WHERE user_id = %s AND batch_id = %s
                ORDER BY created_at DESC LIMIT %s OFFSET %s
            """, (user_id, batch_id, limit, offset))
        else:
            cursor.execute("""
                SELECT * FROM publish_orders
                WHERE user_id = %s
                ORDER BY created_at DESC LIMIT %s OFFSET %s
            """, (user_id, limit, offset))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def update_order_status(order_id: int, status: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE publish_orders
            SET status = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (status, order_id))


# ==================== publish_order_items CRUD ====================

def create_order_item(order_id: int, media_id: int, media_name: str,
                      cost_yuan: float, cost_points: int,
                      mhz_cost_yuan: float) -> dict:
    """创建订单明细"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO publish_order_items
                (order_id, media_id, media_name, cost_yuan, cost_points, mhz_cost_yuan)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (order_id, media_id, media_name, cost_yuan, cost_points, mhz_cost_yuan))
        conn.commit()
        return dict(cursor.fetchone())
    finally:
        conn.close()


def bulk_create_order_items(items: list):
    """批量创建订单明细（在一个事务中）"""
    if not items:
        return
    with get_db() as conn:
        cursor = conn.cursor()
        for item in items:
            cursor.execute("""
                INSERT INTO publish_order_items
                    (order_id, media_id, media_name, cost_yuan, cost_points, mhz_cost_yuan)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                item["order_id"], item["media_id"], item["media_name"],
                item["cost_yuan"], item["cost_points"], item["mhz_cost_yuan"],
            ))


def get_order_items(order_id: int) -> list:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM publish_order_items WHERE order_id = %s ORDER BY id",
            (order_id,)
        )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def get_items_by_batch(batch_id: str) -> list:
    """获取一个批次下所有订单的所有明细"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT poi.*, po.article_title, po.article_id
            FROM publish_order_items poi
            JOIN publish_orders po ON poi.order_id = po.id
            WHERE po.batch_id = %s
            ORDER BY po.id, poi.id
        """, (batch_id,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def update_item_submitted(item_id: int, mhz_order_id: str):
    """标记明细为已提交"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE publish_order_items
            SET status = 'submitted', mhz_order_id = %s, submitted_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (mhz_order_id, item_id))


def update_item_published(item_id: int, publish_url: str):
    """标记明细为已发布"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT po.user_id,po.batch_id,po.article_id,
                       po.article_title,po.article_content
                 FROM publish_order_items poi
                 JOIN publish_orders po ON po.id=poi.order_id
                WHERE poi.id=%s FOR UPDATE OF poi""",
            (item_id,),
        )
        item = cursor.fetchone()
        cursor.execute("""
            UPDATE publish_order_items
            SET status = 'published', publish_url = %s, published_at = COALESCE(published_at,CURRENT_TIMESTAMP)
            WHERE id = %s
            RETURNING published_at
        """, (publish_url, item_id))
        published_fact = cursor.fetchone()
        if item and item.get("article_id"):
            from services.article_publication_snapshot import capture_publication_snapshot_with_cursor
            _publication_snapshot = capture_publication_snapshot_with_cursor(
                cursor,
                article_id=int(item["article_id"]),
                source="publish_order_items",
                source_id=int(item_id),
                success_state="published",
                title_override=item.get("article_title"),
                content_override=item.get("article_content"),
                body_observation_level="submission_snapshot",
            )
            if _publication_snapshot.get("content_hash"):
                from services.article_delivery_plan import enqueue_publication_locked_in_transaction_if_enabled

                enqueue_publication_locked_in_transaction_if_enabled(
                    cursor,
                    publication_source="publish_order_items",
                    publication_source_id=int(item_id),
                    article_id=int(item["article_id"]),
                    platform="legacy_media_box",
                    provider_receipt=f"publish-item:{int(item_id)}",
                    public_url=str(publish_url or ""),
                    submitted_content_hash=str(_publication_snapshot["content_hash"]),
                    published_at=(published_fact or {}).get("published_at"),
                )
        if item and not item.get("batch_id"):
            from services.notification_events import NotificationEventType, RecipientKind
            from services.notification_outbox import enqueue_notification_event

            enqueue_notification_event(
                cursor,
                event_type=NotificationEventType.PUBLICATION_COMPLETED,
                business_id=f"item:{int(item_id)}",
                terminal_state="published",
                recipient_user_id=int(item["user_id"]),
                recipient_kind=RecipientKind.USER,
                facts={
                    "business_no": f"PUBLISH-{int(item_id)}",
                    "status": "已发布",
                    "occurred_at": datetime.now().isoformat(timespec="seconds"),
                    "summary": "请在发布中心查看发布结果。",
                },
            )


def update_item_rejected(item_id: int, reject_reason: str):
    """标记明细为已拒稿"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT po.user_id,po.batch_id
                 FROM publish_order_items poi
                 JOIN publish_orders po ON po.id=poi.order_id
                WHERE poi.id=%s FOR UPDATE OF poi""",
            (item_id,),
        )
        item = cursor.fetchone()
        cursor.execute("""
            UPDATE publish_order_items
            SET status = 'rejected', reject_reason = %s
            WHERE id = %s
        """, (reject_reason, item_id))
        if item and not item.get("batch_id"):
            from services.notification_events import NotificationEventType, RecipientKind
            from services.notification_outbox import enqueue_notification_event

            enqueue_notification_event(
                cursor,
                event_type=NotificationEventType.PUBLICATION_REJECTED,
                business_id=f"item:{int(item_id)}",
                terminal_state="rejected",
                recipient_user_id=int(item["user_id"]),
                recipient_kind=RecipientKind.USER,
                facts={
                    "business_no": f"PUBLISH-{int(item_id)}",
                    "status": "稿件未通过",
                    "occurred_at": datetime.now().isoformat(timespec="seconds"),
                    "summary": "请在发布中心查看处理结果；退款以真实退款终态通知为准。",
                },
            )


def update_item_refunded(item_id: int):
    """标记明细为已退款"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE publish_order_items
            SET refunded_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (item_id,))


def update_item_queued(item_id: int):
    """标记明细为排队中（Session 失效时）"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE publish_order_items
            SET status = 'queued'
            WHERE id = %s
        """, (item_id,))


def get_pending_items(batch_id: str = None) -> list:
    """获取待提交的明细（用于队列处理）。

    [防重复提交] 加 batch_id 过滤参数：
      - 传入 batch_id：只返回该批次内的待提交项（防 _submit_batch_to_mhz 跨批次串扰）
      - 不传：返回所有待提交项（兼容老的全局调度场景）
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if batch_id:
            cursor.execute("""
                SELECT poi.*, po.article_id, po.article_title, po.article_content, po.user_id
                FROM publish_order_items poi
                JOIN publish_orders po ON poi.order_id = po.id
                WHERE poi.status IN ('pending', 'queued')
                  AND po.batch_id = %s
                ORDER BY poi.id
            """, (batch_id,))
        else:
            cursor.execute("""
                SELECT poi.*, po.article_id, po.article_title, po.article_content, po.user_id
                FROM publish_order_items poi
                JOIN publish_orders po ON poi.order_id = po.id
                WHERE poi.status IN ('pending', 'queued')
                ORDER BY poi.id
            """)
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def get_submitted_items() -> list:
    """获取所有已提交待同步状态的明细"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT poi.*, po.user_id, po.article_title, po.brand_id
            FROM publish_order_items poi
            JOIN publish_orders po ON poi.order_id = po.id
            WHERE poi.status = 'submitted'
            ORDER BY poi.submitted_at
        """)
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def is_first_publish(user_id: int) -> bool:
    """检查用户是否从未代发过（首单优惠判断）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM publish_batches WHERE user_id = %s",
            (user_id,)
        )
        return cursor.fetchone()["cnt"] == 0
    finally:
        conn.close()


def recalculate_order_status(order_id: int):
    """根据订单下所有明细状态，重新计算订单整体状态"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM publish_order_items WHERE order_id = %s",
            (order_id,)
        )
        statuses = [r["status"] for r in cursor.fetchall()]
        if not statuses:
            return

        if all(s in ("published", "rejected") for s in statuses):
            new_status = "completed" if "rejected" not in statuses else "partial"
        elif any(s in ("pending", "queued", "submitted") for s in statuses):
            new_status = "processing"
        else:
            new_status = "completed"

        cursor.execute("""
            UPDATE publish_orders
            SET status = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (new_status, order_id))
        conn.commit()
    finally:
        conn.close()


def recalculate_batch_status(batch_id: str):
    """根据批次下所有订单状态，重新计算批次整体状态"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id FROM publish_batches WHERE id = %s FOR UPDATE",
            (batch_id,),
        )
        batch_row = cursor.fetchone()
        if not batch_row:
            return
        cursor.execute(
            "SELECT status FROM publish_orders WHERE batch_id = %s",
            (batch_id,)
        )
        statuses = [r["status"] for r in cursor.fetchall()]
        if not statuses:
            return

        if all(s == "completed" for s in statuses):
            new_status = "completed"
        elif any(s == "partial" for s in statuses) or (
            all(s in ("completed", "partial") for s in statuses)
        ):
            new_status = "partial"
        else:
            new_status = "processing"

        cursor.execute(
            "UPDATE publish_batches SET status = %s WHERE id = %s",
            (new_status, batch_id)
        )
        if new_status in ("completed", "partial"):
            from services.notification_events import NotificationEventType, RecipientKind
            from services.notification_outbox import enqueue_notification_event

            partial = new_status == "partial"
            enqueue_notification_event(
                cursor,
                event_type=(
                    NotificationEventType.PUBLICATION_PARTIAL
                    if partial else NotificationEventType.PUBLICATION_COMPLETED
                ),
                business_id=str(batch_id),
                terminal_state="partial_success" if partial else "completed",
                recipient_user_id=int(batch_row["user_id"]),
                recipient_kind=RecipientKind.USER,
                facts={
                    "business_no": str(batch_id),
                    "status": "部分稿件未通过" if partial else "发布任务已完成",
                    "occurred_at": datetime.now().isoformat(timespec="seconds"),
                    "summary": "请在发布中心查看各稿件结果。",
                },
            )
        conn.commit()
    finally:
        conn.close()


# ==================== V2.3 推荐与快照 ====================

def count_publish_order_samples() -> int:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS cnt FROM publish_orders")
        row = cursor.fetchone()
        return int(row["cnt"] or 0)
    except Exception:
        return 0
    finally:
        conn.close()


def upsert_media_effective_pool(rows: list[dict]) -> int:
    if not rows:
        return 0
    with get_db() as conn:
        cursor = conn.cursor()
        for row in rows:
            cursor.execute("""
                INSERT INTO media_effective_pool (
                    media_source, media_id, platform_name, industry,
                    quality_score, evidence_score, price_score, noise_score,
                    effective_score, is_recommendable, tier, tags, reasons,
                    updated_at
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    NOW()
                )
                ON CONFLICT (media_source, media_id) DO UPDATE SET
                    platform_name = EXCLUDED.platform_name,
                    industry = EXCLUDED.industry,
                    quality_score = EXCLUDED.quality_score,
                    evidence_score = EXCLUDED.evidence_score,
                    price_score = EXCLUDED.price_score,
                    noise_score = EXCLUDED.noise_score,
                    effective_score = EXCLUDED.effective_score,
                    is_recommendable = EXCLUDED.is_recommendable,
                    tier = EXCLUDED.tier,
                    tags = EXCLUDED.tags,
                    reasons = EXCLUDED.reasons,
                    updated_at = NOW()
            """, (
                row.get("media_source") or "media",
                int(row.get("media_id") or row.get("id") or 0),
                row.get("platform_name") or row.get("media_name"),
                row.get("industry") or "",
                row.get("quality_score"),
                row.get("evidence_score"),
                row.get("price_score"),
                row.get("noise_score"),
                row.get("effective_score"),
                bool(row.get("is_recommendable")),
                row.get("tier") or "L0",
                Json(row.get("tags") or {}),
                Json(row.get("reasons") or []),
            ))
    return len(rows)


#: 有效池候选的公共投影 + 连接。行业过滤那一段单独拼(见 _POOL_CANDIDATE_SQL 用法)。
_POOL_CANDIDATE_SQL = """
    SELECT
        p.*,
        {industry_match} AS industry_match,
        COALESCE(m.media_name, w.toutiao_name, p.platform_name) AS media_name,
        COALESCE(m.media_name, w.toutiao_name, p.platform_name) AS platform_name,
        COALESCE(m.our_price_yuan, w.our_price_yuan, w.price, 0) AS our_price_yuan,
        COALESCE(m.our_price_points, w.our_price_points, 0) AS our_price_points,
        COALESCE(m.price, w.price, m.our_price_yuan, w.our_price_yuan, 0) AS price,
        COALESCE(m.portal_media, w.platform, '') AS portal_media,
        COALESCE(m.resource_type_name, w.industry, p.industry, '') AS category,
        COALESCE(m.inclusion_rate::text, '') AS inclusion_rate,
        COALESCE(m.pc_weight, 0) AS pc_weight,
        COALESCE(m.m_weight, 0) AS m_weight,
        COALESCE(m.geo_rank, w.geo_rank, 0) AS geo_rank,
        COALESCE(m.authority_media, w.authority_media, 0) AS authority_media
    FROM media_effective_pool p
    LEFT JOIN mhz_media m
      ON p.media_source = 'media'
     AND p.media_id = m.id
     AND m.is_active = TRUE
    LEFT JOIN mhz_wemedia w
      ON p.media_source = 'wemedia'
     AND p.media_id = w.id
     AND w.is_active = TRUE
    WHERE p.is_recommendable = TRUE
      AND p.tier IN ('L1', 'L2')
      AND {industry_cond}
      {source_cond}
    ORDER BY
      CASE p.tier WHEN 'L2' THEN 2 WHEN 'L1' THEN 1 ELSE 0 END DESC,
      p.effective_score DESC NULLS LAST,
      p.updated_at DESC
    LIMIT %s
"""


def get_effective_pool_candidates(
    *,
    industry: str = "",
    limit: int = 60,
    include_wemedia: bool = True,
) -> list[dict]:
    """有效池候选。**空行业候选只构成无行业兜底档,不与精确行业档混排。**

    🔴 旧口径的漏洞(2026-08-08 B 单):行业条件写成
        `%s = '' OR COALESCE(p.industry,'') = '' OR p.industry ILIKE %s`
    ——中间那一支让**没有行业标注的候选无条件穿透行业过滤**,与精确命中的候选同档
    进入组合包。餐饮客户看到汽车号就是这么来的。

    🔴 为什么不能只靠"排在后面"解决:本函数的 ORDER BY **活不过下游**。
       services/publish_recommendation.build_recommendation_packages 会按
       effective_score 重新排序(:462),SQL 里给兜底档降权是**死排序**,
       用户看到的顺序一点不变。所以必须在集合层面分档,而不是在排序层面。

    分档规则:
      - `industry` 为空(调用方没给行业)→ 整池,行为与旧版逐字一致。
      - `industry` 非空 → **精确行业档**(`p.industry ILIKE %industry%`)。
        只有精确档**一条都没有**时,才回落到**无行业兜底档**
        (`COALESCE(p.industry,'') = ''`)—— 保证面板不至于空,又绝不把
        无行业候选混进有精确命中的结果里。
      - 每行带 `industry_match`:True=精确档/无行业查询,False=兜底档。
        消费方见 api/publish_api._build_media_package_response(据此出兜底提示)。

    ⚠️ 本函数只堵穿透漏洞,**不修第二层病**:`media_effective_pool.industry` 的词表
       (新闻资讯/汽车网站/综合…,来自 mhz_media.resource_type_name 与
       mhz_wemedia.industry,本质是**内容体裁**)与品牌侧业务行业词表(餐饮/全屋定制…)
       几乎不相交 —— 2026-08-08 生产实测 152 种品牌行业里只有 3 种能 ILIKE 命中池子。
       词表映射未经 Owner 拍板,不在本单范围。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        source_cond = "" if include_wemedia else "AND p.media_source = 'media'"
        industry = (industry or "").strip()

        if not industry:
            # 无行业查询:不过滤,整池都算"匹配"(没有行业就谈不上不匹配)。
            cursor.execute(
                _POOL_CANDIDATE_SQL.format(
                    industry_match="TRUE",
                    industry_cond="TRUE",
                    source_cond=source_cond,
                ),
                (limit,),
            )
            return [dict(r) for r in cursor.fetchall()]

        # 精确行业档
        cursor.execute(
            _POOL_CANDIDATE_SQL.format(
                industry_match="TRUE",
                industry_cond="p.industry ILIKE %s",
                source_cond=source_cond,
            ),
            (f"%{industry}%", limit),
        )
        rows = [dict(r) for r in cursor.fetchall()]
        if rows:
            return rows

        # 无行业兜底档(仅在精确档为空时启用)
        cursor.execute(
            _POOL_CANDIDATE_SQL.format(
                industry_match="FALSE",
                industry_cond="COALESCE(p.industry, '') = ''",
                source_cond=source_cond,
            ),
            (limit,),
        )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def get_article_for_publish_recommendation(article_id: int) -> dict | None:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                a.id,
                a.title,
                a.content,
                COALESCE(t.original_keyword, '') AS keyword,
                COALESCE(a.quote_id, t.quote_id) AS quote_id,
                q.brand_id AS brand_id,
                a.created_at
            FROM articles a
            LEFT JOIN topics t ON t.id = a.topic_id
            LEFT JOIN quotes q ON q.id = COALESCE(a.quote_id, t.quote_id)
            WHERE a.id = %s
        """, (article_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_article_publish_analysis(article_id: int) -> dict | None:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT article_id, article_title, analysis, article_type,
                   semantic_keywords, publish_goal, recommended_platforms,
                   risk_tags, analysis_version, created_at
            FROM article_publish_analysis
            WHERE article_id = %s
        """, (article_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_article_publish_analysis(
    *,
    article_id: int,
    article_title: str,
    analysis: dict,
    article_type: str = "",
    semantic_keywords: list | None = None,
    publish_goal: str = "",
    recommended_platforms: list | None = None,
    risk_tags: list | None = None,
    analysis_version: str = "v2.3",
) -> dict:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO article_publish_analysis (
                article_id, article_title, analysis, article_type,
                semantic_keywords, publish_goal, recommended_platforms,
                risk_tags, analysis_version, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (article_id) DO UPDATE SET
                article_title = EXCLUDED.article_title,
                analysis = EXCLUDED.analysis,
                article_type = EXCLUDED.article_type,
                semantic_keywords = EXCLUDED.semantic_keywords,
                publish_goal = EXCLUDED.publish_goal,
                recommended_platforms = EXCLUDED.recommended_platforms,
                risk_tags = EXCLUDED.risk_tags,
                analysis_version = EXCLUDED.analysis_version,
                created_at = NOW()
            RETURNING *
        """, (
            article_id,
            article_title,
            Json(analysis or {}),
            article_type or "",
            Json(semantic_keywords or []),
            publish_goal or "",
            Json(recommended_platforms or []),
            Json(risk_tags or []),
            analysis_version,
        ))
        return dict(cursor.fetchone())


def create_publish_decision_snapshot(payload: dict) -> dict:
    request_id = payload.get("request_id")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO publish_decision_snapshots (
                user_id, brand_id, quote_id, article_ids,
                recommendation_payload, evidence_payload,
                selected_media, selected_wemedia, estimated_points,
                disclaimer_version, terms_version, analysis_version,
                recommendation_level, retention_until, request_id,
                confirmed_at, created_at
            )
            VALUES (
                %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, NOW() + INTERVAL '5 years', %s,
                NOW(), NOW()
            )
            ON CONFLICT (request_id) DO UPDATE SET
                request_id = publish_decision_snapshots.request_id
            RETURNING *
        """, (
            payload["user_id"],
            payload["brand_id"],
            payload.get("quote_id"),
            Json(payload.get("article_ids") or []),
            Json(payload.get("recommendation_payload") or {}),
            Json(payload.get("evidence_payload") or {}),
            Json(payload.get("selected_media") or []),
            Json(payload.get("selected_wemedia") or []),
            int(payload.get("estimated_points") or 0),
            payload.get("disclaimer_version") or "",
            payload.get("terms_version") or "",
            payload.get("analysis_version") or "",
            int(payload.get("recommendation_level") or 3),
            request_id,
        ))
        return dict(cursor.fetchone())


def get_publish_decision_snapshot(snapshot_id: int, user_id: int | None = None) -> dict | None:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        params = [snapshot_id]
        user_filter = ""
        if user_id is not None:
            user_filter = "AND user_id = %s"
            params.append(user_id)
        cursor.execute(f"""
            SELECT *
            FROM publish_decision_snapshots
            WHERE id = %s
              {user_filter}
        """, params)
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def validate_publish_snapshot(snapshot_id: int, user_id: int) -> dict:
    snapshot = get_publish_decision_snapshot(snapshot_id, user_id=user_id)
    if not snapshot:
        return {"valid": False, "reason": "snapshot_not_found", "snapshot": None}
    if not snapshot.get("confirmed_at"):
        return {"valid": False, "reason": "snapshot_not_confirmed", "snapshot": snapshot}
    return {"valid": True, "reason": "", "snapshot": snapshot}


def list_publish_decision_snapshots(
    *,
    user_id: int,
    brand_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        where = ["user_id = %s"]
        params: list = [user_id]
        if brand_id:
            where.append("brand_id = %s")
            params.append(brand_id)
        params.extend([limit, offset])
        cursor.execute(f"""
            SELECT *
            FROM publish_decision_snapshots
            WHERE {' AND '.join(where)}
            ORDER BY confirmed_at DESC NULLS LAST, created_at DESC
            LIMIT %s OFFSET %s
        """, params)
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def _json_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _get_brand_keywords(brand_id: int) -> list[str]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT keyword
            FROM (
                SELECT ck.keyword
                  FROM confirmed_keywords ck
                  JOIN quotes q ON q.id = ck.quote_id
                 WHERE q.brand_id = %s
                UNION ALL
                SELECT keyword
                  FROM extra_keywords
                 WHERE brand_id = %s
                   AND COALESCE(status, 'active') = 'active'
                UNION ALL
                SELECT keyword
                  FROM client_keywords
                 WHERE brand_id = %s
                   AND COALESCE(status, 'active') = 'active'
            ) k
            WHERE COALESCE(keyword, '') != ''
        """, (brand_id, brand_id, brand_id))
        return [r["keyword"] for r in cursor.fetchall()]
    finally:
        conn.close()


def _fetch_strict_same_source_citations(quote_id, confirmed_at) -> dict:
    """[WP7 2026-08-17] same-source 严格归因命中数(**替代**品牌+时间窗的代理量)。

    规格 03 §10:「`publish_outcome_records/media_publish_success` 不得用
    brand+时间窗/累计 citation 冒充 same-source 因果;Q1 hit 不污染同品牌 Q2,
    **删除 strict source join 后红**。」

    🔴 旧口径 `ai_citations_delta_30d` 的问题不是"数偏大",是**它根本不是因果**:
       它数的是"这个品牌的关键词在这 30 天里被检出了多少次",和这次发布的
       URL 有没有被引用毫无关系。同品牌另一张报价的历史内容带来的检出,
       会被记成这次发布的成果。

    新口径 = 该 **quote** 的现役发布物的规范化 URL 真的出现在监测引用里。
    """
    if not quote_id or not confirmed_at:
        return {"strict_same_source_citations": 0, "strict_scope": "unavailable"}
    try:
        from services.publication_stage_adapters import quote_stage_tuple

        projection = quote_stage_tuple(int(quote_id))
        ledger = projection.get("strict_attribution_ledger") or []
        return {
            "strict_same_source_citations": len(ledger),
            "strict_scope": f"quote:{int(quote_id)}",
            "strict_metric_version": (projection.get("source_versions") or {}).get(
                "attribution_metric"),
        }
    except Exception as exc:
        logger.warning("[publish-outcome] strict same-source 归因不可用 quote=%s: %s",
                       quote_id, exc)
        return {"strict_same_source_citations": 0, "strict_scope": "unavailable"}


def _fetch_monitoring_outcome(brand_id: int, keywords: list[str], confirmed_at) -> dict:
    """品牌+时间窗的**背景量**,不是因果。

    🔴 保留它是因为运营确实要看"这段时间品牌整体检出如何";但它的键名已经
       改成自曝形态(`brand_window_*`),且调用方必须另取
       `_fetch_strict_same_source_citations` 才拿得到可归因的那个数。
       两个数并列摆着,谁也冒充不了谁。
    """
    if not keywords or not confirmed_at:
        return {"brand_window_detections_30d": 0, "brand_window_score_30d": 0,
                "brand_window_note": "no_keywords_or_time"}

    conn = get_connection()
    try:
        cursor = conn.cursor()
        try:
            from services.publish_recommendation import MONITORING_OUTCOME_SQL
            cursor.execute(MONITORING_OUTCOME_SQL, (brand_id, keywords, confirmed_at, confirmed_at))
            rows = cursor.fetchall()
        except Exception:
            conn.rollback()
            from services.monitoring_identity_review import aggregate_eligible_sql
            cursor.execute(f"""
                SELECT
                    platform AS engine,
                    SUM(CASE WHEN COALESCE(is_detected, 0) = 1 THEN 1 ELSE 0 END)::int AS ai_citations,
                    ROUND(AVG(CASE WHEN COALESCE(is_detected, 0) = 1 THEN 100 ELSE 0 END)::numeric, 2)
                        AS monitoring_brand_score
                  FROM monitoring_results
                 WHERE keyword = ANY(%s)
                   AND platform IN ('dashscope','deepseek','kimi','doubao')
                   AND tested_at BETWEEN %s AND %s + INTERVAL '30 days'
                   AND {aggregate_eligible_sql()}
                   AND (
                        task_id IN (SELECT id FROM monitoring_tasks WHERE brand_id = %s)
                        OR task_id IS NULL
                   )
                 GROUP BY platform
            """, (keywords, confirmed_at, confirmed_at, brand_id))
            rows = cursor.fetchall()

        citations = sum(int(r.get("ai_citations") or 0) for r in rows)
        scores = [float(r.get("monitoring_brand_score") or 0) for r in rows]
        score_delta = round(sum(scores) / len(scores), 2) if scores else 0
        return {
            # 名字即断言:这是"品牌 × 时间窗"的背景检出量,**不是**本次发布带来的。
            "brand_window_detections_30d": citations,
            "brand_window_score_30d": score_delta,
            "brand_window_note": "brand_time_window_context_not_causal",
        }
    finally:
        conn.close()


def _fetch_publish_status_for_snapshot(snapshot: dict) -> dict:
    article_ids = [int(x) for x in _json_list(snapshot.get("article_ids")) if str(x).isdigit()]
    confirmed_at = snapshot.get("confirmed_at") or snapshot.get("created_at")
    if not article_ids or not confirmed_at:
        return {"publish_status": "pending", "publish_url": "", "failed_reason": ""}

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT i.status, i.publish_url, i.reject_reason
              FROM mhz_publish_orders o
              LEFT JOIN mhz_publish_order_items i ON i.order_id = o.id
             WHERE o.user_id = %s
               AND o.article_id = ANY(%s)
               AND o.created_at BETWEEN %s - INTERVAL '30 minutes' AND %s + INTERVAL '30 days'
        """, (snapshot["user_id"], article_ids, confirmed_at, confirmed_at))
        rows = cursor.fetchall()
    finally:
        conn.close()

    statuses = [str(r.get("status") or "").lower() for r in rows if r.get("status")]
    publish_url = next((r.get("publish_url") for r in rows if r.get("publish_url")), "")
    failed_reason = "；".join(
        str(r.get("reject_reason"))
        for r in rows
        if r.get("reject_reason")
    )[:1000]

    if not statuses:
        publish_status = "pending"
    elif any(s in {"published", "success", "completed"} for s in statuses):
        publish_status = "published"
    elif all(s in {"rejected", "failed", "withdrawn", "cancelled"} for s in statuses):
        publish_status = "rejected" if any(s == "rejected" for s in statuses) else "failed"
    elif any(s in {"rejected", "failed"} for s in statuses):
        publish_status = "failed"
    else:
        publish_status = "reviewing"

    return {
        "publish_status": publish_status,
        "publish_url": publish_url or "",
        "failed_reason": failed_reason,
    }


def _upsert_publish_outcome_record(cursor, payload: dict) -> dict:
    cursor.execute("""
        UPDATE publish_outcome_records
           SET publish_status = %s,
               publish_url = %s,
               inclusion_status = %s,
               ai_citations_delta_30d = %s,
               monitoring_brand_score_delta_30d = %s,
               strict_same_source_citations = %s,
               strict_scope = %s,
               strict_metric_version = %s,
               failed_reason = %s,
               synced_at = NOW()
         WHERE snapshot_id = %s
        RETURNING *
    """, (
        payload.get("publish_status") or "pending",
        payload.get("publish_url") or "",
        payload.get("inclusion_status") or "unknown",
        # 🔴 老列继续存**老口径**(品牌 × 时间窗背景量)。不改它的语义 ——
        #    改语义会让历史数据无法解释。新口径去新列。
        int(payload.get("brand_window_detections_30d") or 0),
        payload.get("brand_window_score_30d") or 0,
        int(payload.get("strict_same_source_citations") or 0),
        payload.get("strict_scope") or "unavailable",
        payload.get("strict_metric_version"),
        payload.get("failed_reason") or "",
        payload["snapshot_id"],
    ))
    row = cursor.fetchone()
    if row:
        return dict(row)

    cursor.execute("""
        INSERT INTO publish_outcome_records (
            snapshot_id, publish_status, publish_url, inclusion_status,
            ai_citations_delta_30d, monitoring_brand_score_delta_30d,
            strict_same_source_citations, strict_scope, strict_metric_version,
            failed_reason, synced_at, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        RETURNING *
    """, (
        payload["snapshot_id"],
        payload.get("publish_status") or "pending",
        payload.get("publish_url") or "",
        payload.get("inclusion_status") or "unknown",
        int(payload.get("brand_window_detections_30d") or 0),
        payload.get("brand_window_score_30d") or 0,
        int(payload.get("strict_same_source_citations") or 0),
        payload.get("strict_scope") or "unavailable",
        payload.get("strict_metric_version"),
        payload.get("failed_reason") or "",
    ))
    return dict(cursor.fetchone())


def sync_publish_outcome_records(window_days: int = 30) -> dict:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT *
              FROM publish_decision_snapshots
             WHERE confirmed_at IS NOT NULL
               AND confirmed_at >= NOW() - (%s * INTERVAL '1 day')
             ORDER BY confirmed_at DESC
        """, (int(window_days),))
        snapshots = [dict(r) for r in cursor.fetchall()]

        synced = 0
        for snapshot in snapshots:
            keywords = _get_brand_keywords(int(snapshot["brand_id"]))
            monitoring = _fetch_monitoring_outcome(
                int(snapshot["brand_id"]),
                keywords,
                snapshot.get("confirmed_at"),
            )
            # [WP7] same-source 严格归因按 **quote** 取,不按 brand。
            strict = _fetch_strict_same_source_citations(
                snapshot.get("quote_id"), snapshot.get("confirmed_at"))
            publish_status = _fetch_publish_status_for_snapshot(snapshot)
            _upsert_publish_outcome_record(cursor, {
                "snapshot_id": snapshot["id"],
                "inclusion_status": "unknown",
                **publish_status,
                **monitoring,
                **strict,
            })
            synced += 1

    return {"status": "success", "synced": synced, "window_days": window_days}


def list_publish_outcomes(
    *,
    user_id: int,
    brand_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        where = ["s.user_id = %s"]
        params: list = [user_id]
        if brand_id:
            where.append("s.brand_id = %s")
            params.append(brand_id)
        params.extend([limit, offset])
        cursor.execute(f"""
            SELECT o.*, s.brand_id, s.quote_id, s.article_ids, s.selected_media, s.selected_wemedia,
                   s.estimated_points, s.confirmed_at
              FROM publish_outcome_records o
              JOIN publish_decision_snapshots s ON s.id = o.snapshot_id
             WHERE {' AND '.join(where)}
             ORDER BY COALESCE(o.synced_at, o.created_at) DESC
             LIMIT %s OFFSET %s
        """, params)
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


# ==================== Session 管理 ====================

def get_mhz_session() -> str:
    """获取外部发布通道会话（统一从 mhz_config 表读取，与前端配置页面一致）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM mhz_config WHERE key = 'phpsessid'")
        row = cursor.fetchone()
        return row["value"] if row else None
    except Exception:
        return None
    finally:
        conn.close()


def set_mhz_session(session_id: str):
    """更新外部发布通道会话（统一写入 mhz_config 表，与前端配置页面一致）"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO mhz_config (key, value)
            VALUES ('phpsessid', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (session_id,))
    logger.info("[Publish] 外部发布通道 Session 已更新")
