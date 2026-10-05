"""
外部发布通道代发 - 数据库操作模块
管理配置、媒体列表、代发订单、定价规则
"""

import json
import logging
from typing import Optional, List, Dict, Any, Set
from datetime import datetime, timedelta, timezone

# [WO_VOCAB_CONVERGENCE 2026-08-09] 写入侧词表规范化(纯字符串映射 · 不查库不抛异常)
from tools.media_vocab_normalize import (
    normalize_media_row, normalize_wemedia_row, LISTING_SLOTS,
)

logger = logging.getLogger("GEO-MHZ-DB")

# ============================================================================
# [短视频 lane · 2026-07-04] media_type SSOT 常量
# 发布中心代发有三类资源：软文 / 自媒体 / 短视频。三类的 media_type 字符串在
#   扣费归一化 / 桶划分 / DB 落库 / 前端 四层必须一致，禁止散落裸字符串。
#   历史上软文这一类在不同入口写过 'mhz' / 'article' / ''，都当软文（见 sync 兜底）。
#   短视频 media_type 用 'svideo'（6 字符）而不是 'short_video'（11 字符）：
#   mhz_publish_order_items.media_type 与 mhz_synced_orders.media_type 都是
#   VARCHAR(10)，'short_video' 会插入报错。远端媒介盒子接口叫 short_video，
#   本地一律存/判 'svideo'。
# ============================================================================
MEDIA_TYPE_MHZ = "mhz"          # 软文（也兼容历史 'article' / ''）
MEDIA_TYPE_WEMEDIA = "wemedia"  # 自媒体
MEDIA_TYPE_SVIDEO = "svideo"    # 短视频

# ============================================================================
# [WO_KYB_CATALOG_GOVERNANCE 2026-08-10] provider（供货渠道）常量
# 🔴 与上面的 MEDIA_TYPE_* 是**两个维度**，字面量恰好都有 "mhz" 但含义无关：
#     media_type = 这条货是软文/自媒体/短视频（板块）
#     provider   = 这条货从哪家进的（mhz_media.provider 列，取值 mhz / kyb）
#   混用会写出 `WHERE provider = MEDIA_TYPE_WEMEDIA` 这种恒空条件，故分开命名。
# 值必须与 services/kuaiyibo/config.py 的 LEGACY_PROVIDER_KEY / PROVIDER_KEY 一致，
#   已在 tests/kyb_catalog_governance_2026_08_10 里配了逐字断言锁（改一边会转红）。
# ============================================================================
PROVIDER_LEGACY = "mhz"   # 媒介盒子（历史唯一供货渠道，全部老行都是它）
PROVIDER_KUAIYIBO = "kyb"  # 快易播（2026-07-27 接入）
# 软文侧判定集合（历史脏值兼容）：凡不是 wemedia / svideo 的都按软文
_SOFTTEXT_MEDIA_TYPES = ("mhz", "article", "")


def canonical_media_type(value) -> str:
    """[WO-ACCEPTANCE-3FIX-2026-08-05 项1] 请求侧写法 → **落库口径**的唯一归一点。

    请求体里软文这一类历史上写过 'article' / '' / 'mhz' 三种（``PublishOrderRequest``
    的默认值就是 ``'article'``），而 DB 里一直只有 'mhz'（生产实测 467/467）。
    调用方要往 ``mhz_publish_order_items.media_type`` 落值，就得先过这里，
    否则就会往那一列里塞进第 4 个值 —— 发布历史的筛选下拉（``publish-history``
    的 ``SELECT DISTINCT media_type``）会因此多出一个同样显示"软文"的重复选项。

    🔴 **这不是给 ``create_order`` 用的**（工单 §1.4 明令禁止）：那边 ``it.get("media_type")``
       拿到的是 ``None``，"不知道是什么类型"和"知道是软文"是两回事，
       在那里兜底成 'mhz' 会把「自媒体单被当软文」固化成正确行为。
       本函数只在**调用方手里有真值**（``req.media_type``）时把写法归一，
       并且 ``None`` 一律原样返回 ``None``，绝不代替调用方猜。

    不认识的写法（``media_type`` 在请求模型里是裸 ``str``，客户端能塞任意值）归 'mhz'，
    因为**同一个请求的其余三层已经按软文办了**：``_recompute_publish_charge`` 查
    ``mhz_media`` 定价、``_is_wm = (req.media_type == "wemedia")`` 走软文投递、
    消费侧 ``(oi.get("media_type") or "mhz") not in ("wemedia","svideo")`` 也算软文。
    这里落 'mhz' 是**记录已经发生的事**，不是替 ``None`` 猜。
    （顺带挡住 VARCHAR(10) 溢出：'short_video' 这类 11 字符的写法原样落库会 INSERT 报错。）

    >>> canonical_media_type("wemedia"), canonical_media_type("article")
    ('wemedia', 'mhz')
    >>> canonical_media_type(""), canonical_media_type(None)
    ('mhz', None)
    """
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw == MEDIA_TYPE_WEMEDIA:
        return MEDIA_TYPE_WEMEDIA
    if raw == MEDIA_TYPE_SVIDEO:
        return MEDIA_TYPE_SVIDEO
    return MEDIA_TYPE_MHZ


def _get_conn():
    from db.connection import get_connection
    return get_connection()


# ========================================
# 表初始化
# ========================================

def init_mhz_tables():
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS mhz_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW(),
                updated_by INTEGER
            )
        """)
        # [FIX·P1-4] 既有表补 updated_by(媒体加价系数改动影响全员扣费·需修改者审计·幂等)
        c.execute("ALTER TABLE mhz_config ADD COLUMN IF NOT EXISTS updated_by INTEGER")
        c.execute("""
            CREATE TABLE IF NOT EXISTS mhz_media (
                id INTEGER PRIMARY KEY,
                media_name TEXT NOT NULL DEFAULT '',
                price NUMERIC(10,2) DEFAULT 0,
                price1 NUMERIC(10,2) DEFAULT 0,
                price2 NUMERIC(10,2) DEFAULT 0,
                area TEXT DEFAULT '',
                portal_media TEXT DEFAULT '',
                resource_type_name TEXT DEFAULT '',
                resource_type TEXT DEFAULT '',
                inclusion_rate INTEGER DEFAULT 0,
                publish_rate TEXT DEFAULT '',
                avg_time INTEGER DEFAULT 0,
                pc_weight INTEGER DEFAULT 0,
                m_weight INTEGER DEFAULT 0,
                news_resource INTEGER DEFAULT 0,
                link_type INTEGER DEFAULT 0,
                remark TEXT DEFAULT '',
                case_link TEXT DEFAULT '',
                geo_rank INTEGER DEFAULT 0,
                geo_rank_platform TEXT DEFAULT '',
                entrance_level INTEGER DEFAULT 0,
                entrance_link TEXT DEFAULT '',
                weekend_publish INTEGER DEFAULT 0,
                authority_media INTEGER DEFAULT 0,
                special_industry INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                synced_at TIMESTAMP DEFAULT NOW(),
                our_price_yuan NUMERIC(10,2) DEFAULT 0,
                our_price_points INTEGER DEFAULT 0,
                -- [WO_KYB_CATALOG_GOVERNANCE 2026-08-10] provider 三件原本只由
                -- db/publish_db.py 启动时 ALTER 补，本函数建表时不含它们 ——
                -- 全新库存在一个「表已建、列还没补」的窗口，任何按 provider 归口
                -- 的查询在那个窗口会 UndefinedColumn。这里随建表一次给全，
                -- 存量库不受影响（那边的 ADD COLUMN IF NOT EXISTS 仍是幂等 no-op）。
                provider VARCHAR(16) NOT NULL DEFAULT 'mhz',
                provider_media_id INTEGER,
                source_domain VARCHAR(160)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_media_active ON mhz_media(is_active)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_media_name ON mhz_media(media_name)")

        # 自媒体表
        c.execute("""
            CREATE TABLE IF NOT EXISTS mhz_wemedia (
                id INTEGER PRIMARY KEY,
                toutiao_name TEXT NOT NULL DEFAULT '',
                platform TEXT DEFAULT '',
                industry TEXT DEFAULT '',
                province TEXT DEFAULT '',
                fans_num INTEGER DEFAULT 0,
                read_num INTEGER DEFAULT 0,
                price NUMERIC(10,2) DEFAULT 0,
                price1 NUMERIC(10,2) DEFAULT 0,
                price2 NUMERIC(10,2) DEFAULT 0,
                video_price NUMERIC(10,2) DEFAULT 0,
                weitoutiao_price NUMERIC(10,2) DEFAULT 0,
                case_link TEXT DEFAULT '',
                entrance_link TEXT DEFAULT '',
                remark TEXT DEFAULT '',
                avg_time INTEGER DEFAULT 0,
                p_rate TEXT DEFAULT '',
                geo_rank INTEGER DEFAULT 0,
                geo_rank_platform TEXT DEFAULT '',
                quota INTEGER DEFAULT 0,
                authority_media INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                synced_at TIMESTAMP DEFAULT NOW(),
                our_price_yuan NUMERIC(10,2) DEFAULT 0,
                our_price_points INTEGER DEFAULT 0,
                -- [WO_KYB_CATALOG_GOVERNANCE 2026-08-10] provider 三件原本只由
                -- db/publish_db.py 启动时 ALTER 补，本函数建表时不含它们 ——
                -- 全新库存在一个「表已建、列还没补」的窗口，任何按 provider 归口
                -- 的查询在那个窗口会 UndefinedColumn。这里随建表一次给全，
                -- 存量库不受影响（那边的 ADD COLUMN IF NOT EXISTS 仍是幂等 no-op）。
                provider VARCHAR(16) NOT NULL DEFAULT 'mhz',
                provider_media_id INTEGER,
                source_domain VARCHAR(160)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_wemedia_active ON mhz_wemedia(is_active)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_wemedia_name ON mhz_wemedia(toutiao_name)")

        # [短视频 lane · 2026-07-04] 短视频资源表（渠道短视频资源列表的本地副本）
        # 不复用 mhz_wemedia：短视频是独立资源池 + 独立上传 + 独立下单/回流接口，业务语义与
        #   自媒体·头条号不同（mhz_wemedia 虽有 video_price 字段，但同步/发布/回流全是自媒体口径）。
        # 类型说明：远端字段类型未完全确认，从宽存储（计数用 BIGINT、价格 NUMERIC、含糊串用 TEXT），
        #   由 mapper 负责强制转型，避免拉取时因单条脏值整批失败。
        c.execute("""
            CREATE TABLE IF NOT EXISTS mhz_short_video (
                id INTEGER PRIMARY KEY,
                media_name TEXT NOT NULL DEFAULT '',
                platform TEXT DEFAULT '',
                location TEXT DEFAULT '',
                occupation TEXT DEFAULT '',
                industry TEXT DEFAULT '',
                fans_num BIGINT DEFAULT 0,
                fans_num_text TEXT DEFAULT '',
                avg_likes_num BIGINT DEFAULT 0,
                total_likes_num BIGINT DEFAULT 0,
                price NUMERIC(10,2) DEFAULT 0,
                price1 NUMERIC(10,2) DEFAULT 0,
                price2 NUMERIC(10,2) DEFAULT 0,
                hepai_price NUMERIC(10,2) DEFAULT 0,
                hepai_price1 NUMERIC(10,2) DEFAULT 0,
                hepai_price2 NUMERIC(10,2) DEFAULT 0,
                avg_publish_time TEXT DEFAULT '',
                can_modify INTEGER DEFAULT 0,
                can_hepai INTEGER DEFAULT 0,
                can_tuwen INTEGER DEFAULT 0,
                authority_media INTEGER DEFAULT 0,
                account_auth TEXT DEFAULT '',
                remark TEXT DEFAULT '',
                case_link TEXT DEFAULT '',
                entrance_link TEXT DEFAULT '',
                status INTEGER DEFAULT 0,
                reason TEXT DEFAULT '',
                blacklist INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                synced_at TIMESTAMP DEFAULT NOW(),
                our_price_yuan NUMERIC(10,2) DEFAULT 0,
                our_price_points INTEGER DEFAULT 0,
                -- [WO_KYB_CATALOG_GOVERNANCE 2026-08-10] provider 三件原本只由
                -- db/publish_db.py 启动时 ALTER 补，本函数建表时不含它们 ——
                -- 全新库存在一个「表已建、列还没补」的窗口，任何按 provider 归口
                -- 的查询在那个窗口会 UndefinedColumn。这里随建表一次给全，
                -- 存量库不受影响（那边的 ADD COLUMN IF NOT EXISTS 仍是幂等 no-op）。
                provider VARCHAR(16) NOT NULL DEFAULT 'mhz',
                provider_media_id INTEGER,
                source_domain VARCHAR(160)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_short_video_active ON mhz_short_video(is_active)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_short_video_name ON mhz_short_video(media_name)")

        # [短视频 lane · 2026-07-04] 短视频"视频稿"占位表（P0-1）
        # 短视频没有软文那种"文章"，但整条发布/去重/回流链以 article_id(int) 为骨架。
        # 每次短视频提交铸一条 draft，用 -draft.id 作为 mhz_publish_orders.article_id：
        #   - 负号命名空间与真实文章 id(正数)、彼此之间都不会撞号 → 三个按 article_id 去重的函数
        #     天然对短视频类型安全，无需改它们的签名（零回归）。
        #   - draft 同时存 video_url/cover/keyword/content，供重投/回流重建 publish_short_video 载荷
        #     （P0-4：一次提交多账号时各 item 共享同一 draft）。
        # 不写进 articles 表 → 不污染面向用户的文章列表（大姐看不到幽灵"文章"）。
        c.execute("""
            CREATE TABLE IF NOT EXISTS mhz_short_video_drafts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                brand_id INTEGER,
                title TEXT NOT NULL DEFAULT '',
                content TEXT DEFAULT '',
                keyword TEXT DEFAULT '',
                video_url TEXT NOT NULL DEFAULT '',
                cover_image TEXT DEFAULT '',
                customer_name TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT NOW(),
                -- [T5 · 2026-07-30] 图文笔记模式：article_type 1 视频直发 / 3 图文笔记，
                --   image_urls 存英文逗号拼接的图片地址(与 save.html 的 image_urls 同形态)。
                --   ⚠️ 这里的建表只对**新库**生效(CREATE TABLE IF NOT EXISTS 对存量表是 no-op)，
                --   存量库靠 scripts/migration_svideo_image_mode_2026_07_30.sql 补列 —— 两处都要有，
                --   只写这里等于"存量库永远没有这两列"。
                article_type INTEGER NOT NULL DEFAULT 1,
                image_urls TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_svideo_drafts_user ON mhz_short_video_drafts(user_id, created_at DESC)")

        # [2026-06-02 GEO CTO] 联系方式发布策略 contact_policy:
        #   none(默认·不允许联系方式) / website_only(只允许官网) / full_contact(允许完整)
        #   admin 后台逐个标记 · 发布时按此软化联系方式(防媒体拒稿)
        for _ct in ("mhz_media", "mhz_wemedia", "mhz_short_video"):
            try:
                c.execute(f"ALTER TABLE {_ct} ADD COLUMN IF NOT EXISTS contact_policy TEXT DEFAULT 'none'")
            except Exception:
                pass
        c.execute("""
            CREATE TABLE IF NOT EXISTS mhz_publish_orders (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                article_id INTEGER,
                article_title TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                total_items INTEGER DEFAULT 0,
                total_cost_points INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_orders_user ON mhz_publish_orders(user_id, created_at DESC)")
        # [BUG-P0-1] 退款对账字段(自愈 · 老库无需迁移脚本):
        #   admin_exempt: 下单时是否 admin 免扣(免扣订单 deduct=0,失败退款须退 0,防凭空注入)
        #   actually_deducted_points: 该订单真实扣的积分(退款 cap = 真实扣费 - 已退 · admin 免扣 = 0)
        try:
            c.execute("ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS admin_exempt BOOLEAN DEFAULT FALSE")
            c.execute("ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS actually_deducted_points INTEGER")
            c.execute("ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS article_content_snapshot TEXT")
            c.execute("ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS article_content_snapshot_hash CHAR(64)")
            c.execute("ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS article_content_snapshot_at TIMESTAMPTZ")
            c.execute("ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS article_content_snapshot_source VARCHAR(40)")
        except Exception:
            pass
        c.execute("""
            CREATE TABLE IF NOT EXISTS mhz_publish_order_items (
                id SERIAL PRIMARY KEY,
                order_id INTEGER REFERENCES mhz_publish_orders(id),
                user_id INTEGER NOT NULL,
                media_id INTEGER,
                media_name TEXT NOT NULL,
                mhz_order_id TEXT,
                status TEXT DEFAULT 'pending',
                cost_points INTEGER DEFAULT 0,
                cost_yuan NUMERIC(10,2) DEFAULT 0,
                publish_url TEXT,
                reject_reason TEXT,
                submitted_at TIMESTAMP,
                published_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_items_order ON mhz_publish_order_items(order_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_items_status ON mhz_publish_order_items(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_items_mhz_id ON mhz_publish_order_items(mhz_order_id)")
        # Snapshot fields must be added after the base table exists. Keeping
        # these in the publish_orders block made a truly fresh database fail
        # before CREATE TABLE could run, while established production schemas
        # silently hid the ordering defect.
        # [P1 拒绝原因分层] reject_reason 是一坨给人看不懂的内部文本。拆成对齐 §13 的三列:
        #   reject_code         机器码(前端据此分支 / 监控据此聚合)
        #   reject_user_message 讲人话的一句(直接展示给用户)
        #   reject_contract     完整七字段合同(含 actions —— 红码必须有下一步)
        # reject_reason 保留不动:它是内部诊断文本,历史数据与日志都依赖它。
        c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS reject_code TEXT")
        c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS reject_user_message TEXT")
        c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS reject_contract JSONB")
        c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS submitted_title_snapshot TEXT")
        c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS submitted_content_snapshot TEXT")
        c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS submitted_content_snapshot_hash CHAR(64)")
        c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS submitted_content_snapshot_at TIMESTAMPTZ")
        c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS submitted_content_snapshot_source VARCHAR(80)")
        c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS submitted_legal_catalog_version VARCHAR(64)")

        # 🔴 [WO-D-R2 ① 2026-08-20] `billing_mode` 由迁移 034 加(该文件 409 行),
        #   但**没有进这份自举兜底** —— 于是:
        #     · 生产没事(prestart 每次部署无条件重放全部迁移,034 早跑过);
        #     · 任何**只靠 init_mhz_tables() 自举**的新库(CI / 新 staging / 重建预演 /
        #       一次性测试库)拿到的表**没有这一列**,而生产代码在读它:
        #         services/publish_orphan_settlement.py:226  AND (i.billing_mode IS NULL OR …)
        #         db/meijiehezi_db.py:2164 / 4620            同一个谓词的两处
        #       → tests/publish_zombie_2026_08_04 6 条判据 **双臂恒红**
        #         `column i.billing_mode does not exist`,红因与被测代码零关系。
        #   DDL 与 034 **逐字对齐**(TEXT · 无 DEFAULT · 不 backfill):034 顶部写明
        #   「不给 DEFAULT 也不 backfill」是刻意的 —— 存量行 NULL 天然匹配不上新链的
        #   `billing_mode = 'freeze_per_item'`,给了 DEFAULT 反而会让老单穿透进新链。
        #   CHECK 也一并补,否则自举库能写进生产写不进的值(夹具比生产宽 = 假绿)。
        c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS billing_mode TEXT")
        c.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mhz_item_billing_mode') THEN
                    ALTER TABLE mhz_publish_order_items ADD CONSTRAINT ck_mhz_item_billing_mode
                        CHECK (billing_mode IS NULL OR billing_mode IN ('deduct_upfront', 'freeze_per_item'));
                END IF;
            END $$;
        """)

        # 防重复提交：提交次数 + 最后提交时间（自愈，老库无需迁移脚本）
        try:
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS submit_attempts INT DEFAULT 0")
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS last_submit_at TIMESTAMP")
        except Exception as _e:
            pass

        # 待用户确认（mhz 返回 203/204/205 时挂起 item，不绕过 mhz 查重防线）
        # 字段语义：
        #   pending_confirm_codes: JSONB 数组 [203, 204]，记录所有触发过的 mhz 码
        #   pending_confirm_msg:   mhz 返回的原始 msg（前端原样展示给用户）
        #   pending_confirm_fields:JSONB 已被用户确认的 field 集合 ["confirm_content"]
        #   awaiting_since:        进入 awaiting_confirmation 状态的时间（24h 自动取消用）
        try:
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS pending_confirm_codes JSONB DEFAULT '[]'::jsonb")
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS pending_confirm_msg TEXT")
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS pending_confirm_fields JSONB DEFAULT '[]'::jsonb")
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS awaiting_since TIMESTAMP")
            c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_items_awaiting ON mhz_publish_order_items(status, awaiting_since) WHERE status = 'awaiting_confirmation'")
        except Exception as _e:
            pass
        c.execute("""
            CREATE TABLE IF NOT EXISTS mhz_pricing_rules (
                id SERIAL PRIMARY KEY,
                rule_type TEXT NOT NULL DEFAULT 'global',
                target_id INTEGER,
                target_name TEXT,
                markup_rate NUMERIC(5,2) DEFAULT 1.5,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # 同步订单表（从外部发布通道拉取的文章订单）
        c.execute("""
            CREATE TABLE IF NOT EXISTS mhz_synced_orders (
                id TEXT PRIMARY KEY,
                order_sn TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                media_name TEXT DEFAULT '',
                resource_id INTEGER DEFAULT 0,
                price NUMERIC(10,2) DEFAULT 0,
                status INTEGER DEFAULT 0,
                url TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                published_at TIMESTAMP,
                order_remark TEXT DEFAULT '',
                customer_name TEXT DEFAULT '',
                file_url TEXT DEFAULT '',
                subtitle TEXT DEFAULT '',
                user_id INTEGER DEFAULT 0,
                synced_at TIMESTAMP DEFAULT NOW()
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_synced_orders_user ON mhz_synced_orders(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_synced_orders_status ON mhz_synced_orders(status)")

        # 退款申请表
        c.execute("""
            CREATE TABLE IF NOT EXISTS mhz_refund_requests (
                id SERIAL PRIMARY KEY,
                order_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                admin_note TEXT DEFAULT '',
                refund_points INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                reviewed_at TIMESTAMP,
                reviewed_by INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_refund_user ON mhz_refund_requests(user_id)")

        # 迁移：确保旧表有新增的列（兼容旧版表结构）
        migration_columns = [
            ("mhz_media", "price", "NUMERIC(10,2) DEFAULT 0"),
            ("mhz_media", "area", "TEXT DEFAULT ''"),
            ("mhz_media", "m_weight", "INTEGER DEFAULT 0"),
            ("mhz_media", "resource_type", "TEXT DEFAULT ''"),
            ("mhz_media", "resource_type_name", "TEXT DEFAULT ''"),
            ("mhz_media", "inclusion_rate", "INTEGER DEFAULT 0"),
            ("mhz_media", "publish_rate", "TEXT DEFAULT ''"),
            ("mhz_media", "avg_time", "INTEGER DEFAULT 0"),
            ("mhz_media", "news_resource", "INTEGER DEFAULT 0"),
            ("mhz_media", "link_type", "INTEGER DEFAULT 0"),
            ("mhz_media", "remark", "TEXT DEFAULT ''"),
            ("mhz_media", "case_link", "TEXT DEFAULT ''"),
            ("mhz_media", "entrance_level", "INTEGER DEFAULT 0"),
            ("mhz_media", "entrance_link", "TEXT DEFAULT ''"),
            ("mhz_media", "portal_media", "TEXT DEFAULT ''"),
            ("mhz_media", "price1", "NUMERIC(10,2) DEFAULT 0"),
            ("mhz_media", "price2", "NUMERIC(10,2) DEFAULT 0"),
            ("mhz_media", "geo_rank", "INTEGER DEFAULT 0"),
            ("mhz_media", "geo_rank_platform", "TEXT DEFAULT ''"),
            ("mhz_media", "weekend_publish", "INTEGER DEFAULT 0"),
            ("mhz_media", "authority_media", "INTEGER DEFAULT 0"),
            ("mhz_media", "special_industry", "INTEGER DEFAULT 0"),
            ("mhz_media", "our_price_yuan", "NUMERIC(10,2) DEFAULT 0"),
            ("mhz_media", "our_price_points", "INTEGER DEFAULT 0"),
            ("mhz_media", "pc_weight", "INTEGER DEFAULT 0"),
            # [WO_VOCAB_CONVERGENCE 2026-08-09] 上架档位(migration_031 同款列)。
            #   这里补一遍是给**新库 / 旧表**兜底:migration_031 走部署清单,而本函数是
            #   运行时自愈路径,两条路都得能把列建出来 —— 否则新库上 _upsert_media 的
            #   INSERT 带 listing_slot 会直接 UndefinedColumn 把整批同步打挂。
            #   🔴 只补列不补 CHECK/索引:那两样归 migration_031(本函数吞异常,
            #   在这里加约束会静默失败,反而造出"以为有约束"的假象)。
            ("mhz_media", "listing_slot", "TEXT"),
        ]
        for table, col, col_type in migration_columns:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}")
            except Exception:
                pass  # 列已存在或其他非致命错误

        # mhz_synced_orders 加 brand_id（用于精准同步到监测中心投放记录）
        try:
            c.execute("ALTER TABLE mhz_synced_orders ADD COLUMN IF NOT EXISTS brand_id INTEGER")
            c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_synced_brand ON mhz_synced_orders(brand_id) WHERE brand_id IS NOT NULL")
        except Exception:
            pass
        # mhz_publish_order_items 加 brand_id
        try:
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS brand_id INTEGER")
        except Exception:
            pass
        # mhz_publish_order_items 加 media_type（区分软文/自媒体）
        try:
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS media_type VARCHAR(10)")
        except Exception:
            pass
        # mhz_synced_orders 加 media_type（区分软文/自媒体同步订单）
        try:
            c.execute("ALTER TABLE mhz_synced_orders ADD COLUMN IF NOT EXISTS media_type VARCHAR(10)")
        except Exception:
            pass
        # mhz_synced_orders 加 article_id（关联原始文章，支持重发）
        try:
            c.execute("ALTER TABLE mhz_synced_orders ADD COLUMN IF NOT EXISTS article_id INTEGER")
        except Exception:
            pass
        # mhz_synced_orders 加 republished_order_sn（重发后的新订单号，NULL=未重发）
        try:
            c.execute("ALTER TABLE mhz_synced_orders ADD COLUMN IF NOT EXISTS republished_order_sn TEXT")
        except Exception:
            pass

        # [2026-04-30 防重复发布] mhz_publish_order_items 加 mhz_raw_response 列
        # 每次调用 mhz publish/publish_wemedia 后立即写入返回的原始 data，便于事后排查
        # awaiting_sync_overdue_at: awaiting_sync 状态超 30 分钟反查不到 → 进人工审核队列
        try:
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS mhz_raw_response JSONB")
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS awaiting_sync_since TIMESTAMP")
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS manual_review_required BOOLEAN DEFAULT FALSE")
            c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_items_awaiting_sync ON mhz_publish_order_items(status, awaiting_sync_since) WHERE status = 'awaiting_sync'")
            c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_items_manual_review ON mhz_publish_order_items(manual_review_required) WHERE manual_review_required = TRUE")
        except Exception:
            pass

        # [P0 代发卡单出口 2026-07-26] 权威源是 scripts/migration_mhz_awaiting_sync_exit_2026_07_26.sql
        # (已登记 db/migration_manifest.py)。这里是**运行时兜底** —— 迁移漏跑时,
        # 72 小时出口扫描会 UndefinedColumn 并被 job 的 except 静默吞掉,
        # 表现成"任务在跑但永远不出口",正是本次要修的故障形态,所以两层都留。
        try:
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS awaiting_sync_probe_attempts INTEGER NOT NULL DEFAULT 0")
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS user_exit_claim VARCHAR(32)")
            c.execute("ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS user_exit_claim_at TIMESTAMP")
            c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_items_awaiting_sync_exit ON mhz_publish_order_items(awaiting_sync_since, awaiting_sync_probe_attempts) WHERE status = 'awaiting_sync'")
        except Exception:
            pass

        # [2026-04-30 数据修复] mhz_synced_orders.media_type 历史空值回填
        # 281 条远古同步数据没有标 media_type，靠订单号前缀兜底判断：
        #   订单号前缀 11... = 软文（mhz）
        #   订单号前缀 12... = 自媒体（wemedia）
        # 回填后撤单接口分流逻辑能直接读 media_type 字段，不再依赖前缀判断
        try:
            c.execute("""
                UPDATE mhz_synced_orders
                SET media_type = CASE
                    WHEN order_sn LIKE '12%' THEN 'wemedia'
                    WHEN order_sn LIKE '11%' THEN 'mhz'
                    ELSE media_type  -- 不认识的前缀保持原值
                END
                WHERE (media_type IS NULL OR media_type = '')
                  AND order_sn IS NOT NULL
                  AND (order_sn LIKE '11%' OR order_sn LIKE '12%')
            """)
        except Exception:
            pass

        # [2026-04-30 运维] mhz 接口调用日志表 —— 每次调 mhz 接口都打点
        # 出问题不用 ssh 服务器查日志，直接 SELECT 这张表就能看到：
        #   - 哪条接口、什么时间、用什么参数调的
        #   - 响应 code、是否成功、耗时
        #   - 关联的 item_id（如果是发文调用）
        # 自动 30 天 TTL 清理避免无限增长
        c.execute("""
            CREATE TABLE IF NOT EXISTS mhz_call_log (
                id BIGSERIAL PRIMARY KEY,
                endpoint TEXT NOT NULL,
                method VARCHAR(10) NOT NULL DEFAULT 'POST',
                item_id INTEGER,
                user_id INTEGER,
                params_summary JSONB,
                response_code INTEGER,
                response_summary JSONB,
                elapsed_ms INTEGER,
                ok BOOLEAN NOT NULL DEFAULT TRUE,
                error_msg TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_call_log_created ON mhz_call_log(created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_call_log_item ON mhz_call_log(item_id) WHERE item_id IS NOT NULL")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_call_log_endpoint ON mhz_call_log(endpoint, created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mhz_call_log_failed ON mhz_call_log(created_at DESC) WHERE ok = FALSE")

        # 回填 article_id：从 mhz_publish_order_items 反查
        try:
            c.execute("""
                UPDATE mhz_synced_orders s
                SET article_id = sub.article_id
                FROM (
                    SELECT DISTINCT ON (i.mhz_order_id) i.mhz_order_id, o.article_id
                    FROM mhz_publish_order_items i
                    JOIN mhz_publish_orders o ON o.id = i.order_id
                    WHERE i.mhz_order_id IS NOT NULL
                ) sub
                WHERE s.order_sn = sub.mhz_order_id AND s.article_id IS NULL
            """)
        except Exception:
            pass

        conn.commit()
        logger.info("外部发布通道表初始化完成")
    except Exception as e:
        conn.rollback()
        logger.error(f"外部发布通道表初始化失败: {e}")
    finally:
        conn.close()

    # 幂等键表（独立 try 防止主表初始化连环失败）
    try:
        _init_idempotency_table()
        logger.info("代发幂等键表初始化完成")
    except Exception as e:
        logger.warning(f"代发幂等键表初始化失败（不影响主流程）: {e}")


# ========================================
# 配置管理
# ========================================

def get_config(key: str) -> Optional[str]:
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT value FROM mhz_config WHERE key = %s", (key,))
        row = c.fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_config(key: str, value: str, updated_by: int = None):
    """[FIX·P1-4/H] 加 updated_by 审计 + before/after 日志(媒体系数改动影响全员扣费·需可追溯)"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT value FROM mhz_config WHERE key = %s", (key,))
        _old = c.fetchone()
        _before = (_old["value"] if isinstance(_old, dict) else _old[0]) if _old else None
        c.execute("""
            INSERT INTO mhz_config (key, value, updated_at, updated_by) VALUES (%s, %s, NOW(), %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW(), updated_by = EXCLUDED.updated_by
        """, (key, value, updated_by))
        conn.commit()
        import logging as _lg
        _lg.getLogger("GEO-Meijiehezi").info(
            "[mhz_config 审计] key=%s before=%s after=%s updated_by=%s", key, _before, value, updated_by)
    finally:
        conn.close()


# ========================================
# 媒体列表
# ========================================

def save_media(media: Dict):
    conn = _get_conn()
    try:
        c = conn.cursor()
        _upsert_media(c, media)
        conn.commit()
    finally:
        conn.close()


def save_media_batch(media_list: List[Dict]):
    """批量保存媒体，单个连接内完成，每 500 条 commit 一次"""
    if not media_list:
        return
    conn = _get_conn()
    try:
        c = conn.cursor()
        for i, media in enumerate(media_list):
            _upsert_media(c, media)
            if (i + 1) % 500 == 0:
                conn.commit()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _upsert_media(cursor, media: Dict):
    # [WO_VOCAB_CONVERGENCE 2026-08-09] 写入侧规范化闸 · 顺序不能反(先落闸再回填,
    #   否则回填完下一次同步立刻把「综合全国」「十元专区」写回来)。
    #   area 归一(综合全国→全国 / 海外→全球)· listing_slot 从类目列剥出(原值不动)。
    normalize_media_row(media)
    cursor.execute("""
        INSERT INTO mhz_media (
            id, media_name, price, price1, price2, area, portal_media,
            resource_type_name, resource_type, inclusion_rate, publish_rate, avg_time,
            pc_weight, m_weight, news_resource, link_type, remark, case_link,
            geo_rank, geo_rank_platform, entrance_level, entrance_link,
            weekend_publish, authority_media, special_industry, listing_slot, is_active, synced_at
        ) VALUES (
            %(id)s, %(media_name)s, %(price)s, %(price1)s, %(price2)s, %(area)s, %(portal_media)s,
            %(resource_type_name)s, %(resource_type)s, %(inclusion_rate)s, %(publish_rate)s, %(avg_time)s,
            %(pc_weight)s, %(m_weight)s, %(news_resource)s, %(link_type)s, %(remark)s, %(case_link)s,
            %(geo_rank)s, %(geo_rank_platform)s, %(entrance_level)s, %(entrance_link)s,
            %(weekend_publish)s, %(authority_media)s, %(special_industry)s, %(listing_slot)s, TRUE, NOW()
        )
        ON CONFLICT (id) DO UPDATE SET
            listing_slot = EXCLUDED.listing_slot,
            media_name = EXCLUDED.media_name, price = EXCLUDED.price,
            price1 = EXCLUDED.price1, price2 = EXCLUDED.price2,
            area = EXCLUDED.area, portal_media = EXCLUDED.portal_media,
            resource_type_name = EXCLUDED.resource_type_name, resource_type = EXCLUDED.resource_type,
            inclusion_rate = EXCLUDED.inclusion_rate, publish_rate = EXCLUDED.publish_rate,
            avg_time = EXCLUDED.avg_time, pc_weight = EXCLUDED.pc_weight, m_weight = EXCLUDED.m_weight,
            news_resource = EXCLUDED.news_resource, link_type = EXCLUDED.link_type,
            remark = EXCLUDED.remark, case_link = EXCLUDED.case_link,
            geo_rank = EXCLUDED.geo_rank, geo_rank_platform = EXCLUDED.geo_rank_platform,
            entrance_level = EXCLUDED.entrance_level, entrance_link = EXCLUDED.entrance_link,
            weekend_publish = EXCLUDED.weekend_publish, authority_media = EXCLUDED.authority_media,
            special_industry = EXCLUDED.special_industry, is_active = TRUE, synced_at = NOW()
    """, media)


def get_all_media_ids(provider: Optional[str] = PROVIDER_LEGACY) -> Set[int]:
    """在售软文媒体的本地 id 集合。

    [WO_KYB_CATALOG_GOVERNANCE 2026-08-10] **默认按 provider 归口**。
    调用方全是「拿本地集合 − 上游快照 = 待下架」的同步链：不归口就会把另一家
    供应商的库存整批算成 stale。2026-08-10 实测后果 —— 34,747 条快易播软文
    进了媒介盒子同步的 stale 集，把部分快照守卫的阈值永久撑破，媒介盒子的下架
    分支自快易播接入起**一次都没执行过**（``last_media_sync_result`` 恒「下架0」，
    而无跨供应商污染的短视频同一段代码同期「下架23」）。
    ``provider=None`` = 老口径（全供应商），只有确实要全表 id 时才传。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        if provider is None:
            c.execute("SELECT id FROM mhz_media WHERE is_active = TRUE")
        else:
            c.execute("SELECT id FROM mhz_media WHERE is_active = TRUE AND provider = %s",
                      (provider,))
        return {r["id"] for r in c.fetchall()}
    finally:
        conn.close()


def deactivate_media(ids: List[int]):
    if not ids:
        return
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE mhz_media SET is_active = FALSE WHERE id = ANY(%s)", (ids,))
        conn.commit()
    finally:
        conn.close()


# [CTO-15.23 2026-05-18 v2-F] 原设计:媒介盒子 <¥5 区 616 家 SSH 实证 0 家
#   authority+GEO+一线门户 → 默认硬隐藏,用户切价格升序或勾 toggle 才露出。
#
# [2026-07-28 Owner 拍板] 阈值降为 0 = **不再按价格硬隐藏任何媒体**。
#   下线理由:那组「616 家全是冒名小站」是**只有媒介盒子一家供货商**时量出来的。
#   2026-07-27 快易播接入后新增 3.4 万条媒体,定价档与媒介盒子完全不同 ——
#   例:博客园(cnblogs.com)在快易播是 ¥4,备注为「ai GEO 收录好、直编在线收稿、
#   审核松、出稿快、收录好、GEO 有排名」,却被这条 ¥5 阈值连搜索一起盖掉
#   (search 与价格条件是 AND,搜名字也搜不出来)。
#   用老供应商的价格-质量关系去过滤新供应商的目录会大面积误伤。
#
#   媒体优劣改由**飞轮真实被引数据**驱动推荐,不再把价格当质量的代理变量。
#   保留常量与那段 where 分支(而不是删掉):将来若要按"飞轮实测成功率"重做隐藏规则,
#   改这一个数即可,不必再动查询结构。
_MIN_PRICE_HIDE_THRESHOLD = 0.0  # 0 = 不按价格隐藏(2026-07-28 起)

# 一线门户白名单(GEO 真权威排序加成)
_TOP_PORTAL_MEDIAS = (
    '网易网', '新华网', '人民网', '光明网', '凤凰网', '央视网',
    '新浪网', '中华网', '环球网', '国际在线', '中国广播网', '中国网',
)


def get_user_published_media_ids(user_id: int, days: int = 90) -> Set[int]:
    """[v2-F] 该用户/代理过去 N 天发过的 media_id 集合 · 用于推荐 90 天去重惩罚

    覆盖 status:已发布(published)/进行中(pending/submitted) · 排除已结束状态(发不出去不算占用)。
    days 用 Python 端算 cutoff datetime 传参 · 避免 psycopg2 字符串字面量内 placeholder 不替换的坑。

    [BUG 6 修 2026-05-18] dead status 与业务代码 db/meijiehezi_db.py:1667 对齐:
      - rejected/failed/cancelled/withdrawn 都是"已结束 · 不锁占用" · 该媒体可再推荐
      - paused_admin_dedupe 是 admin 暂停去重的脏数据(53 历史条)· 同算 dead
    """
    if not user_id or days <= 0:
        return set()
    cutoff = datetime.now() - timedelta(days=days)
    conn = _get_conn()
    try:
        c = conn.cursor()
        # [svideo lane · 2026-07-04] 排除短视频 item：短视频与软文/自媒体来自不同远端资源池，
        #   media_id 可能撞号；本函数只服务软文/自媒体的 90 天推荐去重（短视频 v1 不做自动推荐）。
        #   IS DISTINCT FROM 正确处理 NULL（老数据 media_type 为空的软文/自媒体行保留，与旧行为一致）。
        c.execute(
            """
            SELECT DISTINCT media_id FROM mhz_publish_order_items
            WHERE user_id = %s
              AND created_at >= %s
              AND status NOT IN ('cancelled', 'failed', 'rejected', 'withdrawn', 'paused_admin_dedupe')
              AND media_id IS NOT NULL
              AND media_type IS DISTINCT FROM 'svideo'
            """,
            (user_id, cutoff),
        )
        return {r['media_id'] for r in c.fetchall()}
    except Exception as e:
        logger.warning(f"get_user_published_media_ids 失败 user={user_id} days={days}: {e}")
        return set()
    finally:
        conn.close()


def get_media_contact_policies(media_ids, is_wemedia: bool = False, media_type: str = None) -> dict:
    """批量查媒体的 contact_policy(发布时按渠道软化联系方式用)。
    返回 {media_id(int): policy}· 查不到 / 字段未建 → 不在结果里(调用方默认 'none' 最保守)。

    [svideo lane · 2026-07-04] 新增 media_type 显式分流（优先于旧 is_wemedia 布尔）：
      'wemedia'->mhz_wemedia / 'svideo'->mhz_short_video / 其余(mhz/article/'')->mhz_media。
      media_type=None 时回退旧 is_wemedia 逻辑，老调用方零改动。"""
    if not media_ids:
        return {}
    if media_type == MEDIA_TYPE_SVIDEO:
        table = "mhz_short_video"
    elif media_type == MEDIA_TYPE_WEMEDIA or (media_type is None and is_wemedia):
        table = "mhz_wemedia"
    else:
        table = "mhz_media"
    try:
        ids = [int(m) for m in media_ids if m is not None]
    except (TypeError, ValueError):
        ids = []
    if not ids:
        return {}
    conn = _get_conn()
    try:
        c = conn.cursor()
        placeholders = ",".join(["%s"] * len(ids))
        try:
            c.execute(f"SELECT id, contact_policy FROM {table} WHERE id IN ({placeholders})", ids)
        except Exception:
            return {}  # 字段未建(老库)→ 全默认 none
        out = {}
        for r in c.fetchall():
            pol = str(r.get("contact_policy") or "none").strip().lower()
            out[int(r["id"])] = pol if pol in ("none", "website_only", "full_contact") else "none"
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_media_source_domains(media_ids, is_wemedia: bool = False, media_type: str = None) -> dict:
    """[P1-3 2026-08-14] 批量查媒体真实发布域(发布时按域名取 media_form 分档用)。

    返回 {media_id(int): source_domain(str)}。表路由与 get_media_contact_policies
    同一套;查不到 / 字段未建 / 任何异常 → 不在结果里(调用方按"未分档"走,
    O1:零行为变化,绝不阻断发布)。
    🔴 source_domain 是内部列(供应商信息纪律),本函数只供服务端渲染层消费,
    绝不进任何用户可见响应。"""
    if not media_ids:
        return {}
    if media_type == MEDIA_TYPE_SVIDEO:
        table = "mhz_short_video"
    elif media_type == MEDIA_TYPE_WEMEDIA or (media_type is None and is_wemedia):
        table = "mhz_wemedia"
    else:
        table = "mhz_media"
    try:
        ids = [int(m) for m in media_ids if m is not None]
    except (TypeError, ValueError):
        ids = []
    if not ids:
        return {}
    conn = _get_conn()
    try:
        c = conn.cursor()
        placeholders = ",".join(["%s"] * len(ids))
        try:
            c.execute(f"SELECT id, source_domain FROM {table} WHERE id IN ({placeholders})", ids)
        except Exception:
            return {}  # 字段未建(老库)→ 全按未分档
        out = {}
        for r in c.fetchall():
            dom = str(r.get("source_domain") or "").strip().lower()
            if dom:
                out[int(r["id"])] = dom
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ========== [WO-KYB-ROUTING D0-a · 2026-08-04] 目录接口字段白名单 ==========
# 三个用户端目录接口(/media /wemedia /short-video)此前都是 `SELECT *`,把内部列
# 原样吐给了任何登录用户:供应商归属(provider)、上游主键(provider_media_id)、
# 真实发布域(source_domain)、去重内部态(hidden_by_dedupe)、成本位(wholesale_*/
# platform_cost_*)。其中 provider 直接违反「不给用户看供应商名」铁律,而且
# 快易播独有媒体一上架(工单 D4)就等于对外宣布我们有两家供货商。
#
# 🔴 为什么是白名单不是"pop 掉几个字段"的黑名单:黑名单在下一次
#    `ALTER TABLE ADD COLUMN` 时会静默失效 —— 那正是本 bug 的成因型
#    (`list_media` 函数体内 `provider` 字面出现 0 次,靠读代码根本搜不到)。
#
# 🔴 为什么白名单要跟库内真实列求交,而不是把列名直接写死进 SQL:
#    本仓对 mhz_media 存在【两份不同的 CREATE TABLE IF NOT EXISTS】——
#    db/meijiehezi_db.py:54(price/area/portal_media/…)与 db/publish_db.py:51
#    (category/platform/price_normal/…)。谁先跑谁建表,另一份整个变 no-op。
#    生产是历史上两份都落过地的并集(实测 47 列),但**全新库只会有其中一份**。
#    把 40 个列名写死进 SQL,在全新库上就是 `column "xxx" does not exist` →
#    整个媒体列表 500。求交后:库里没有的列自动不选(不炸),库里多出来的列
#    自动不选(不漏)——两个方向都安全。
#
# 求交结果按表缓存(进程级)。启动期 ALTER 都跑完才会有第一个请求,故缓存安全;
# 真要热加列也只需重启,与 mhz_config 那些缓存同级别。

# 只在服务端内部用、绝不出接口的列(仅用于自检断言,不参与 SQL 构造)
_INTERNAL_ONLY_MEDIA_COLUMNS = frozenset({
    "provider",             # 供应商归属 mhz/kyb —— 铁律:不给用户看供应商名
    "provider_media_id",    # 上游主键 —— 能反查到我们从谁那儿进的货
    "source_domain",        # 归一化真实发布域 —— 同上
    "hidden_by_dedupe",     # 双供应商去重内部态
    "platform_cost_cents",  # 成本位(当前生产 0 行有值,但不能等有值了才堵)
    "wholesale_cents",
    "wholesale_points",
})

# 软文 · mhz_media(生产实测共 47 列 · 白名单 40 = 47 - 上面 7 个)
MEDIA_PUBLIC_COLUMNS = (
    "id", "media_name", "price", "price1", "price2", "area", "portal_media",
    "resource_type_name", "resource_type", "inclusion_rate", "publish_rate",
    "avg_time", "pc_weight", "m_weight", "news_resource", "link_type", "remark",
    "case_link", "geo_rank", "geo_rank_platform", "entrance_level", "entrance_link",
    "weekend_publish", "authority_media", "special_industry", "is_active", "synced_at",
    "our_price_yuan", "our_price_points", "contact_policy",
    # 以下 10 列来自 db/publish_db.py:51 那份 DDL(生产并集里有,全新库可能没有 →
    # 求交会自动跳过,不会炸)
    "category", "media_type", "platform", "price_normal", "price_vip", "price_svip",
    "avg_publish_time", "mobile_weight", "news_source", "can_geo",
)

# 自媒体 · mhz_wemedia(生产实测共 30 列 · 白名单 26 = 30 - provider 三件 - hidden_by_dedupe)
WEMEDIA_PUBLIC_COLUMNS = (
    "id", "toutiao_name", "platform", "industry", "province", "fans_num", "read_num",
    "price", "price1", "price2", "video_price", "weitoutiao_price", "case_link",
    "entrance_link", "remark", "avg_time", "p_rate", "geo_rank", "geo_rank_platform",
    "quota", "authority_media", "is_active", "synced_at",
    "our_price_yuan", "our_price_points", "contact_policy",
)

# 短视频 · mhz_short_video(推导共 36 列 · 白名单 33 = 36 - provider 三件)
# ⚠️ 工单只点名了 /media 与 /wemedia,但 db/publish_db.py:286 那个循环把
#    provider/provider_media_id/source_domain 同样加到了 mhz_short_video,
#    而 list_short_video 也是 `SELECT *` —— 同一个 bug 的第三个出口,一并堵。
#    (该表没有 hidden_by_dedupe:migration 只 ALTER 了 media 与 wemedia。)
SHORT_VIDEO_PUBLIC_COLUMNS = (
    "id", "media_name", "platform", "location", "occupation", "industry",
    "fans_num", "fans_num_text", "avg_likes_num", "total_likes_num",
    "price", "price1", "price2", "hepai_price", "hepai_price1", "hepai_price2",
    "avg_publish_time", "can_modify", "can_hepai", "can_tuwen", "authority_media",
    "account_auth", "remark", "case_link", "entrance_link", "status", "reason",
    "blacklist", "is_active", "synced_at",
    "our_price_yuan", "our_price_points", "contact_policy",
)

_PUBLIC_COLUMN_CACHE: Dict[str, tuple] = {}


def _public_select_list(cursor, table: str, declared: tuple) -> str:
    """把声明白名单与库里真实列求交,返回可直接拼进 SELECT 的列清单。

    交集为空 = 表不存在 / 库结构完全对不上。这时候**宁可炸**也不能退回
    `SELECT *` —— 退回去就是把本次修的漏洞原样放回来(fail-open),
    而炸是响一声、看得见、修得掉。
    """
    cached = _PUBLIC_COLUMN_CACHE.get(table)
    if cached is None:
        # 🔴 用 to_regclass 而不是 information_schema + current_schema():
        #    current_schema() 返回的是 search_path 的第一项。若连接串把 search_path
        #    设成 `"$user", public`(PG 默认),而表在 public,current_schema() 会
        #    返回那个同名 user schema → 一列都查不到 → 本函数 fail-closed 抛异常
        #    → 媒体列表整个 500。to_regclass 走的是和下面主查询【完全同一套】
        #    search_path 解析,查到的列就是主查询会看到的列,不会错位。
        cursor.execute(
            "SELECT a.attname AS column_name FROM pg_attribute a "
            "WHERE a.attrelid = to_regclass(%s) "
            "AND a.attnum > 0 AND NOT a.attisdropped",
            (table,),
        )
        actual = {r["column_name"] for r in cursor.fetchall()}
        cached = tuple(col for col in declared if col in actual)
        if not cached:
            raise RuntimeError(
                f"[D0-a] {table} 的公开列白名单与库内实际列交集为空"
                f"(库内 {len(actual)} 列)。拒绝退回 SELECT * ——"
                f"那会把内部字段重新泄漏出去。"
            )
        _PUBLIC_COLUMN_CACHE[table] = cached
    return ", ".join(f'"{col}"' for col in cached)


def list_media(page: int = 1, limit: int = 20, search: str = "", area: str = "",
               resource_type: str = "", news_resource: str = "",
               sort_by: str = "price", sort_dir: str = "asc",
               price_min: float = 0, price_max: float = 0,
               portal_media: str = "", resource_type_name: str = "",
               geo_rank: int = 0, authority_media: int = -1,
               geo_platform: str = "", special_industry: int = -1,
               hide_low_quality: bool = True) -> Dict:
    conn = _get_conn()
    try:
        c = conn.cursor()
        # [双供应商去重 2026-08-02] 同名同域两家都在架时目录只出 1 条(留 mhz 那条,
        # 售价不变,成本走更便宜的一家)。🔴 用独立的 hidden_by_dedupe,**不动 is_active**
        # —— 后者是上游同步字段,动它会被下一次同步覆盖回来(工单 §3.3)。
        where = ["is_active = TRUE", "NOT hidden_by_dedupe"]
        params: list = []
        if search:
            where.append("media_name ILIKE %s")
            params.append(f"%{search}%")
        if area:
            where.append("area = %s")
            params.append(area)
        if resource_type:
            where.append("resource_type = %s")
            params.append(resource_type)
        if news_resource:
            where.append("news_resource::text = %s")
            params.append(news_resource)
        if portal_media:
            where.append("portal_media = %s")
            params.append(portal_media)
        if resource_type_name:
            where.append("resource_type_name = %s")
            params.append(resource_type_name)
        if geo_rank > 0:
            where.append("geo_rank > 0")
        if authority_media >= 0:
            where.append("authority_media = %s")
            params.append(authority_media)
        if price_min > 0:
            where.append("price >= %s")
            params.append(price_min)
        if price_max > 0:
            where.append("price <= %s")
            params.append(price_max)
        if geo_platform:
            # geo_rank_platform is comma-separated, check if it contains the code
            where.append("geo_rank_platform LIKE %s")
            params.append(f"%{geo_platform}%")
        if special_industry >= 0:
            where.append("special_industry = %s")
            params.append(special_industry)
        # [v2-F] <¥5 默认硬隐藏 · price_min 显式 > 0 或 sort_by=price ASC 时不隐藏(用户主动想看)
        if hide_low_quality and price_min <= 0 and not (sort_by == 'price' and sort_dir.lower() == 'asc'):
            where.append("price >= %s")
            params.append(_MIN_PRICE_HIDE_THRESHOLD)

        where_sql = " AND ".join(where)
        allowed_sorts = {
            "id", "price", "pc_weight", "m_weight", "media_name", "inclusion_rate",
            "avg_time", "publish_rate", "news_resource",
            # [v2-F] GEO 引擎覆盖数(a-f 字母 · z 不算)+ 真权威综合分
            "geo_engine_coverage", "geo_authority_score",
        }
        # [CTO-15.23 2026-05-17 老板报"收录率排序字典序"]
        # mhz_media.inclusion_rate / publish_rate 是 text 列 · 直接 ORDER BY 走字典序
        # "9" > "89" > "88" → 看起来 desc 但 9 排在 89 前 · 视觉混乱
        # 修法:这两列用 NULLIF::int 转数字再排 · NULLS LAST 防空值塞顶
        TEXT_NUMERIC_SORTS = {"inclusion_rate", "publish_rate"}
        # GEO 引擎覆盖数表达式(逗号分隔 a-f 字母数 · z 不算 · 用 LIKE 检测 6 个字母位)
        # [BUG 7 修 2026-05-18] cur.execute(sql, params) 走 psycopg2 解析 ·
        # 单 '%a%' 字面会让 psycopg2 把 %a 当非法占位符 → IndexError ·
        # 必须用 '%%a%%' 转义(psycopg2 把 %% 转 % 再发 PG)· SSH 已实证修前 BOOM。
        _GEO_ENGINE_COVERAGE_EXPR = (
            "(CASE WHEN geo_rank_platform LIKE '%%a%%' THEN 1 ELSE 0 END +"
            " CASE WHEN geo_rank_platform LIKE '%%b%%' THEN 1 ELSE 0 END +"
            " CASE WHEN geo_rank_platform LIKE '%%c%%' THEN 1 ELSE 0 END +"
            " CASE WHEN geo_rank_platform LIKE '%%d%%' THEN 1 ELSE 0 END +"
            " CASE WHEN geo_rank_platform LIKE '%%e%%' THEN 1 ELSE 0 END +"
            " CASE WHEN geo_rank_platform LIKE '%%f%%' THEN 1 ELSE 0 END)"
        )
        # GEO 真权威综合分(默认 chip "GEO 真权威↓"用 · 多 tier 排)
        _GEO_AUTHORITY_SCORE_EXPR = (
            f"({_GEO_ENGINE_COVERAGE_EXPR} * 10"
            f" + CASE WHEN authority_media = 1 THEN 5 ELSE 0 END"
            f" + CASE WHEN portal_media IN ('网易网','新华网','人民网','光明网','凤凰网','央视网','新浪网','中华网','环球网','国际在线','中国广播网','中国网') THEN 4 ELSE 0 END"
            f" + CASE WHEN price BETWEEN 30 AND 60 THEN 3 WHEN price BETWEEN 15 AND 30 THEN 1 ELSE 0 END)"
        )
        sb = sort_by if sort_by in allowed_sorts else "id"
        sd = "DESC" if sort_dir.lower() == "desc" else "ASC"
        if sb in TEXT_NUMERIC_SORTS:
            order_clause = f"NULLIF({sb}, '')::int {sd} NULLS LAST"
        elif sb == "geo_engine_coverage":
            order_clause = f"{_GEO_ENGINE_COVERAGE_EXPR} {sd} NULLS LAST, price ASC"
        elif sb == "geo_authority_score":
            order_clause = f"{_GEO_AUTHORITY_SCORE_EXPR} {sd} NULLS LAST, price ASC"
        else:
            order_clause = f"{sb} {sd}"

        c.execute(f"SELECT COUNT(*) AS total FROM mhz_media WHERE {where_sql}", params)
        total = c.fetchone()["total"]

        offset = (page - 1) * limit
        _cols = _public_select_list(c, "mhz_media", MEDIA_PUBLIC_COLUMNS)
        c.execute(
            f"SELECT {_cols} FROM mhz_media WHERE {where_sql} ORDER BY {order_clause} LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        media = [dict(r) for r in c.fetchall()]
        return {"media": media, "total": total, "page": page, "pages": (total + limit - 1) // limit}
    finally:
        conn.close()


# [地区筛选只留中国 2026-07-28 老板拍板] 上游 mhz_media.area 有 271 个不同值,其中
# ~230 个是国家/大洲组合(全球、海外、北美,美国、东南亚,泰国,马来西亚,新加坡,印度尼西亚…),
# 在筛选栏里铺满 20 多行,把真正常用的省份挤到看不见。当前业务只做中国大陆 + 港澳台,
# 所以**筛选项**只留中国区。
#
# 注意这里过滤的是"筛选选项",不是媒体本身 —— 国际媒体仍在库里、仍可被搜索到,
# 只是不再给一个几乎没人点的入口。要连媒体一起隐藏是另一个决定(会藏掉约 1.3k 家),
# 需要单独拍板,不在本次范围。
_CHINA_AREA_TOKENS = frozenset({
    "中国", "全国", "综合全国", "港澳台",
    # 34 个省级行政区(直辖市/省/自治区/特别行政区)
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
    "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州",
    "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
    "香港", "澳门", "台湾",
})


def _china_only_areas(areas: List[str]) -> List[str]:
    """只保留纯中国区的 area 值。

    area 是逗号分隔的集合(如 ``中国,台湾`` / ``全球,中国,香港``)。判定规则是
    **逐 token 全中才留**:
      - ``中国,台湾``     → 两个 token 都在白名单 → 保留
      - ``全球,中国``     → ``全球`` 不在白名单   → 丢弃(它本质是国际盘)
    用"全中才留"而不是"含中就留",是因为后者会把「全球,中国,东南亚」这类国际投放
    也当成中国区,筛出来的结果会让代理困惑。
    """
    kept = []
    for raw in areas:
        tokens = [t.strip() for t in (raw or "").split(",") if t.strip()]
        if tokens and all(t in _CHINA_AREA_TOKENS for t in tokens):
            kept.append(raw)
    return kept


def get_media_filters() -> Dict:
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT DISTINCT area FROM mhz_media WHERE is_active = TRUE AND area != '' ORDER BY area")
        areas = _china_only_areas([r["area"] for r in c.fetchall()])
        c.execute("SELECT DISTINCT resource_type FROM mhz_media WHERE is_active = TRUE AND resource_type != '' ORDER BY resource_type")
        types = [r["resource_type"] for r in c.fetchall()]
        c.execute("SELECT DISTINCT news_resource::text FROM mhz_media WHERE is_active = TRUE ORDER BY news_resource::text")
        news = [r["news_resource"] for r in c.fetchall()]
        c.execute("SELECT DISTINCT portal_media FROM mhz_media WHERE is_active = TRUE AND portal_media != '' ORDER BY portal_media")
        portal_medias = [r["portal_media"] for r in c.fetchall()]
        # [WO_VOCAB_CONVERGENCE 2026-08-09] 同 publish_db.get_media_categories:
        #   「套餐系列/十元专区/最新秒杀」是上架档位不是内容分类,不进分类筛选项。
        #   按值排除而非按 listing_slot 列过滤 —— 迁移/回填没跑时这条也生效。
        c.execute(
            "SELECT DISTINCT resource_type_name FROM mhz_media "
            "WHERE is_active = TRUE AND resource_type_name != '' "
            "  AND resource_type_name <> ALL(%s) ORDER BY resource_type_name",
            (sorted(LISTING_SLOTS),),
        )
        resource_type_names = [r["resource_type_name"] for r in c.fetchall()]

        # GEO排名平台（拆分逗号分隔的值去重）
        c.execute("SELECT DISTINCT geo_rank_platform FROM mhz_media WHERE is_active = TRUE AND geo_rank_platform != '' ORDER BY geo_rank_platform")
        raw_platforms = [r["geo_rank_platform"] for r in c.fetchall()]
        geo_platforms_set = set()
        for p in raw_platforms:
            for code in p.split(','):
                code = code.strip()
                if code:
                    geo_platforms_set.add(code)
        # 映射字母到平台名
        GEO_PLATFORM_MAP = {'a': 'DeepSeek', 'b': '豆包', 'c': '通义千问', 'd': '腾讯元宝', 'e': '文心一言', 'f': 'Kimi', 'z': '其他'}
        geo_platforms = [{"code": k, "name": GEO_PLATFORM_MAP.get(k, k)} for k in sorted(geo_platforms_set) if k in GEO_PLATFORM_MAP]

        # 特别行业
        SPECIAL_INDUSTRY_MAP = {1: '金融区块链', 3: '党政加分', 4: '健康', 6: '白名单来源', 7: '移动端媒体', 8: '需要来源媒体', 9: '首页焦点图/首页文字链'}
        c.execute("SELECT DISTINCT special_industry FROM mhz_media WHERE is_active = TRUE AND special_industry > 0 ORDER BY special_industry")
        special_industries = [{"code": r["special_industry"], "name": SPECIAL_INDUSTRY_MAP.get(r["special_industry"], f"类型{r['special_industry']}")} for r in c.fetchall()]

        return {
            "areas": areas,
            "resource_types": types,
            "news_resources": news,
            "portal_medias": portal_medias,
            "resource_type_names": resource_type_names,
            "geo_platforms": geo_platforms,
            "special_industries": special_industries,
        }
    finally:
        conn.close()


# ========================================
# 自媒体列表
# ========================================

def save_wemedia_batch(media_list: List[Dict]):
    """批量保存自媒体，每 500 条 commit 一次"""
    if not media_list:
        return
    conn = _get_conn()
    try:
        c = conn.cursor()
        for i, media in enumerate(media_list):
            _upsert_wemedia(c, media)
            if (i + 1) % 500 == 0:
                conn.commit()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _upsert_wemedia(cursor, media: Dict):
    # [WO_VOCAB_CONVERGENCE 2026-08-09] 自媒体侧地域列叫 province,同一套同义归一。
    #   2026-08-09 实测:province 里「综合全国」71087 行 /「全国」12347 /「海外」92(无「全球」)。
    #   本表没有价格档问题(industry 列存的是内容体裁,那是另一层病,本单不动)。
    normalize_wemedia_row(media)
    cursor.execute("""
        INSERT INTO mhz_wemedia (
            id, toutiao_name, platform, industry, province, fans_num, read_num,
            price, price1, price2, video_price, weitoutiao_price,
            case_link, entrance_link, remark, avg_time, p_rate,
            geo_rank, geo_rank_platform, quota, authority_media, is_active, synced_at
        ) VALUES (
            %(id)s, %(toutiao_name)s, %(platform)s, %(industry)s, %(province)s,
            %(fans_num)s, %(read_num)s, %(price)s, %(price1)s, %(price2)s,
            %(video_price)s, %(weitoutiao_price)s, %(case_link)s, %(entrance_link)s,
            %(remark)s, %(avg_time)s, %(p_rate)s, %(geo_rank)s, %(geo_rank_platform)s,
            %(quota)s, %(authority_media)s, TRUE, NOW()
        )
        ON CONFLICT (id) DO UPDATE SET
            toutiao_name = EXCLUDED.toutiao_name, platform = EXCLUDED.platform,
            industry = EXCLUDED.industry, province = EXCLUDED.province,
            fans_num = EXCLUDED.fans_num, read_num = EXCLUDED.read_num,
            price = EXCLUDED.price, price1 = EXCLUDED.price1, price2 = EXCLUDED.price2,
            video_price = EXCLUDED.video_price, weitoutiao_price = EXCLUDED.weitoutiao_price,
            case_link = EXCLUDED.case_link, entrance_link = EXCLUDED.entrance_link,
            remark = EXCLUDED.remark, avg_time = EXCLUDED.avg_time, p_rate = EXCLUDED.p_rate,
            geo_rank = EXCLUDED.geo_rank, geo_rank_platform = EXCLUDED.geo_rank_platform,
            quota = EXCLUDED.quota, authority_media = EXCLUDED.authority_media,
            is_active = TRUE, synced_at = NOW()
    """, media)


def get_all_wemedia_ids(provider: Optional[str] = PROVIDER_LEGACY) -> Set[int]:
    """在售自媒体的本地 id 集合。归口理由同 :func:`get_all_media_ids`
    （实测污染面更大：97,982 条快易播自媒体）。``provider=None`` = 全供应商。"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        if provider is None:
            c.execute("SELECT id FROM mhz_wemedia WHERE is_active = TRUE")
        else:
            c.execute("SELECT id FROM mhz_wemedia WHERE is_active = TRUE AND provider = %s",
                      (provider,))
        return {r["id"] for r in c.fetchall()}
    finally:
        conn.close()


def deactivate_wemedia(ids: List[int]):
    if not ids:
        return
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE mhz_wemedia SET is_active = FALSE WHERE id = ANY(%s)", (ids,))
        conn.commit()
    finally:
        conn.close()


# ── [WO_PUBLISH_DISPATCH Part② 2026-08-17] 行业筛选归一化 ───────────────────
# 旧口径 `industry = %s` 是**逐字精确匹配**目录原始串,而目录里全是
# `母婴亲子/亲子/教育培训/知识/健康医疗` 这种斜杠组合(生产实测 243 个 distinct)。
# 新口径:前端传回来的是 L1 大类键,服务端展开成「归属该大类的全部原始串」再
# `= ANY(...)`。非 L1 值(老链接 / 老客户端传的原始串)**原样走精确匹配**,
# 不改行为 —— 这是不缩水的那一半。
def _industry_predicate(c, table: str, industry: str) -> tuple[str, list]:
    """返回 (SQL 片段, 参数列表)。`table` 恒为本模块内的字面量,不来自外部输入。"""
    from services.media_industry_taxonomy import is_l1_key, raw_values_for_l1

    if not is_l1_key(industry):
        return "industry = %s", [industry]
    c.execute(
        f"SELECT DISTINCT industry FROM {table} WHERE is_active = TRUE AND industry <> ''"
    )
    raws = raw_values_for_l1([r["industry"] for r in c.fetchall()], industry)
    # 该大类当前一个媒体都没有 → 给一个空数组,`= ANY('{}')` 恒假,返回空列表。
    # (facet 已经把 0 家的 chip 隐藏了,这里只是防手工拼 URL。)
    return "industry = ANY(%s)", [raws]


def _industry_facets(c, table: str, where_sql: str, params: list) -> List[Dict]:
    """在**当前筛选结果集**上算每个 L1 大类的媒体数(0 家的不返回)。

    🔴 `where_sql` 必须是列表查询用的**同一份**谓词去掉 industry 那条 —— 数量与
       点进去看到的条数才对得上。所以两边都由 `_wemedia_where` / `_short_video_where`
       同一个 builder 产出,不各写一遍。
    """
    from services.media_industry_taxonomy import facet_counts

    c.execute(
        f"SELECT industry, COUNT(*) AS n FROM {table} WHERE {where_sql} "
        f"AND industry <> '' GROUP BY industry",
        params,
    )
    return facet_counts([(r["industry"], r["n"]) for r in c.fetchall()])


def _wemedia_where(c, search: str = "", platform: str = "", industry: str = "",
                   province: str = "", price_min: float = 0, price_max: float = 0,
                   geo_platform: str = "", fans_min: int = 0, fans_max: int = 0,
                   authority_media: int = -1) -> tuple[str, list]:
    """自媒体目录的筛选谓词 —— 列表查询与 facet 计数**共用**这一份。"""
    # [双供应商去重 2026-08-02] 同上:只出 1 条,不动 is_active。
    where = ["is_active = TRUE", "NOT hidden_by_dedupe"]
    params: list = []
    if search:
        where.append("toutiao_name ILIKE %s")
        params.append(f"%{search}%")
    if platform:
        where.append("platform = %s")
        params.append(platform)
    if industry:
        _sql, _p = _industry_predicate(c, "mhz_wemedia", industry)
        where.append(_sql)
        params.extend(_p)
    if province:
        where.append("province = %s")
        params.append(province)
    if price_min > 0:
        where.append("price >= %s")
        params.append(price_min)
    if price_max > 0:
        where.append("price <= %s")
        params.append(price_max)
    if geo_platform:
        where.append("geo_rank_platform LIKE %s")
        params.append(f"%{geo_platform}%")
    if fans_min > 0:
        where.append("fans_num >= %s")
        params.append(fans_min)
    if fans_max > 0:
        where.append("fans_num <= %s")
        params.append(fans_max)
    if authority_media >= 0:
        where.append("authority_media = %s")
        params.append(authority_media)
    return " AND ".join(where), params


def list_wemedia(page: int = 1, limit: int = 20, search: str = "",
                 platform: str = "", industry: str = "", province: str = "",
                 sort_by: str = "price", sort_dir: str = "asc",
                 price_min: float = 0, price_max: float = 0,
                 geo_platform: str = "", fans_min: int = 0, fans_max: int = 0,
                 authority_media: int = -1) -> Dict:
    conn = _get_conn()
    try:
        c = conn.cursor()
        where_sql, params = _wemedia_where(
            c, search=search, platform=platform, industry=industry, province=province,
            price_min=price_min, price_max=price_max, geo_platform=geo_platform,
            fans_min=fans_min, fans_max=fans_max, authority_media=authority_media,
        )
        allowed_sorts = {
            "id", "price", "toutiao_name", "fans_num", "read_num", "avg_time", "p_rate",
        }
        sb = sort_by if sort_by in allowed_sorts else "id"
        sd = "DESC" if sort_dir.lower() == "desc" else "ASC"

        c.execute(f"SELECT COUNT(*) AS total FROM mhz_wemedia WHERE {where_sql}", params)
        total = c.fetchone()["total"]

        offset = (page - 1) * limit
        _cols = _public_select_list(c, "mhz_wemedia", WEMEDIA_PUBLIC_COLUMNS)
        c.execute(
            f"SELECT {_cols} FROM mhz_wemedia WHERE {where_sql} ORDER BY {sb} {sd} LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        media = [dict(r) for r in c.fetchall()]
        return {"media": media, "total": total, "page": page, "pages": (total + limit - 1) // limit}
    finally:
        conn.close()


def get_wemedia_filters(search: str = "", platform: str = "", province: str = "",
                        price_min: float = 0, price_max: float = 0,
                        geo_platform: str = "", fans_min: int = 0, fans_max: int = 0,
                        authority_media: int = -1) -> Dict:
    """自媒体筛选项。

    [WO_PUBLISH_DISPATCH Part② 2026-08-17] `industries` 从「243 个原始串平铺」
    改成「L1 大类 + facet 计数」,并且**按当前其余筛选条件动态算** ——
    切平台后重算,0 家的大类不返回(选了就是空结果的 chip 不该渲染)。
    industry 自己**不进 facet 的 where**:否则选了一个大类,其余大类全变 0,就没法换了。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT DISTINCT platform FROM mhz_wemedia WHERE is_active = TRUE AND platform != '' ORDER BY platform")
        platforms = [r["platform"] for r in c.fetchall()]
        _facet_where, _facet_params = _wemedia_where(
            c, search=search, platform=platform, province=province,
            price_min=price_min, price_max=price_max, geo_platform=geo_platform,
            fans_min=fans_min, fans_max=fans_max, authority_media=authority_media,
        )
        industries = _industry_facets(c, "mhz_wemedia", _facet_where, _facet_params)
        c.execute("SELECT DISTINCT province FROM mhz_wemedia WHERE is_active = TRUE AND province != '' ORDER BY province")
        provinces = [r["province"] for r in c.fetchall()]
        # GEO排名平台
        GEO_PLATFORM_MAP = {'a': 'DeepSeek', 'b': '豆包', 'c': '通义千问', 'd': '腾讯元宝', 'e': '文心一言', 'f': 'Kimi', 'z': '其他'}
        c.execute("SELECT DISTINCT geo_rank_platform FROM mhz_wemedia WHERE is_active = TRUE AND geo_rank_platform != '' ORDER BY geo_rank_platform")
        raw_gp = [r["geo_rank_platform"] for r in c.fetchall()]
        geo_set = set()
        for p in raw_gp:
            for code in p.split(','):
                code = code.strip()
                if code and code in GEO_PLATFORM_MAP:
                    geo_set.add(code)
        geo_platforms = [{"code": k, "name": GEO_PLATFORM_MAP[k]} for k in sorted(geo_set)]

        return {
            "platforms": platforms, "industries": industries, "provinces": provinces,
            "geo_platforms": geo_platforms,
        }
    finally:
        conn.close()


# ========================================
# 短视频资源（svideo lane · 2026-07-04）
# 全部对称 mhz_wemedia 的同步/查询模式，只是表和字段换成短视频专属。
# ========================================

def save_short_video_batch(media_list: List[Dict]):
    """批量保存短视频资源，每 500 条 commit 一次。入参为 mapper 已转型好的 dict。"""
    if not media_list:
        return
    conn = _get_conn()
    try:
        c = conn.cursor()
        for i, media in enumerate(media_list):
            _upsert_short_video(c, media)
            if (i + 1) % 500 == 0:
                conn.commit()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _upsert_short_video(cursor, media: Dict):
    cursor.execute("""
        INSERT INTO mhz_short_video (
            id, media_name, platform, location, occupation, industry,
            fans_num, fans_num_text, avg_likes_num, total_likes_num,
            price, price1, price2, hepai_price, hepai_price1, hepai_price2,
            avg_publish_time, can_modify, can_hepai, can_tuwen,
            authority_media, account_auth, remark, case_link, entrance_link,
            status, reason, blacklist, is_active, synced_at
        ) VALUES (
            %(id)s, %(media_name)s, %(platform)s, %(location)s, %(occupation)s, %(industry)s,
            %(fans_num)s, %(fans_num_text)s, %(avg_likes_num)s, %(total_likes_num)s,
            %(price)s, %(price1)s, %(price2)s, %(hepai_price)s, %(hepai_price1)s, %(hepai_price2)s,
            %(avg_publish_time)s, %(can_modify)s, %(can_hepai)s, %(can_tuwen)s,
            %(authority_media)s, %(account_auth)s, %(remark)s, %(case_link)s, %(entrance_link)s,
            %(status)s, %(reason)s, %(blacklist)s, TRUE, NOW()
        )
        ON CONFLICT (id) DO UPDATE SET
            media_name = EXCLUDED.media_name, platform = EXCLUDED.platform,
            location = EXCLUDED.location, occupation = EXCLUDED.occupation,
            industry = EXCLUDED.industry, fans_num = EXCLUDED.fans_num,
            fans_num_text = EXCLUDED.fans_num_text, avg_likes_num = EXCLUDED.avg_likes_num,
            total_likes_num = EXCLUDED.total_likes_num, price = EXCLUDED.price,
            price1 = EXCLUDED.price1, price2 = EXCLUDED.price2,
            hepai_price = EXCLUDED.hepai_price, hepai_price1 = EXCLUDED.hepai_price1,
            hepai_price2 = EXCLUDED.hepai_price2, avg_publish_time = EXCLUDED.avg_publish_time,
            can_modify = EXCLUDED.can_modify, can_hepai = EXCLUDED.can_hepai,
            can_tuwen = EXCLUDED.can_tuwen, authority_media = EXCLUDED.authority_media,
            account_auth = EXCLUDED.account_auth, remark = EXCLUDED.remark,
            case_link = EXCLUDED.case_link, entrance_link = EXCLUDED.entrance_link,
            status = EXCLUDED.status, reason = EXCLUDED.reason, blacklist = EXCLUDED.blacklist,
            is_active = TRUE, synced_at = NOW()
    """, media)


def get_all_short_video_ids(provider: Optional[str] = PROVIDER_LEGACY) -> Set[int]:
    """在售短视频资源的本地 id 集合。归口理由同 :func:`get_all_media_ids`。

    短视频这一板块目前只有媒介盒子在供货（快易播全目录抖音 4 条 / 快手 4 条，
    没接），所以**当下**归不归口结果一样 —— 正因为一样，它是本次修复的干净
    反向对照：同一段守卫代码在这里从未被撑破（实测「下架23」）。归口是为了
    将来这块接第二家时不再重演。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        if provider is None:
            c.execute("SELECT id FROM mhz_short_video WHERE is_active = TRUE")
        else:
            c.execute("SELECT id FROM mhz_short_video WHERE is_active = TRUE AND provider = %s",
                      (provider,))
        return {r["id"] for r in c.fetchall()}
    finally:
        conn.close()


def deactivate_short_video(ids: List[int]):
    if not ids:
        return
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE mhz_short_video SET is_active = FALSE WHERE id = ANY(%s)", (ids,))
        conn.commit()
    finally:
        conn.close()


def _short_video_where(c, search: str = "", platform: str = "", location: str = "",
                       industry: str = "", price_min: float = 0, price_max: float = 0,
                       fans_min: int = 0, fans_max: int = 0, can_modify: int = -1,
                       authority_media: int = -1, account_auth: str = "",
                       can_tuwen: int = -1) -> tuple[str, list]:
    """短视频目录的筛选谓词 —— 列表查询与 facet 计数**共用**这一份。"""
    where = ["is_active = TRUE"]
    params: list = []
    if search:
        where.append("media_name ILIKE %s")
        params.append(f"%{search}%")
    if platform:
        where.append("platform = %s")
        params.append(platform)
    if location:
        where.append("location = %s")
        params.append(location)
    if industry:
        _sql, _p = _industry_predicate(c, "mhz_short_video", industry)
        where.append(_sql)
        params.extend(_p)
    if price_min > 0:
        where.append("price >= %s")
        params.append(price_min)
    if price_max > 0:
        where.append("price <= %s")
        params.append(price_max)
    if fans_min > 0:
        where.append("fans_num >= %s")
        params.append(fans_min)
    if fans_max > 0:
        where.append("fans_num <= %s")
        params.append(fans_max)
    if can_modify >= 0:
        where.append("can_modify = %s")
        params.append(can_modify)
    if authority_media >= 0:
        where.append("authority_media = %s")
        params.append(authority_media)
    if account_auth:
        where.append("account_auth = %s")
        params.append(account_auth)
    # [#192 c1] image-note capable filter; shape copied from can_modify/authority_media.
    #   -1 = no filter (legacy calls stay byte-identical), 0/1 = exact match.
    #   Server-side is mandatory: this table is the vendor catalogue (20k+ rows in prod)
    #   and the panel only fetches page 1 of 50, so it would never find a capable row.
    if can_tuwen >= 0:
        where.append("can_tuwen = %s")
        params.append(can_tuwen)
    if can_tuwen == 1:
        # Same static image-note pool as account_eligibility; quota stays in preview.
        where.append("COALESCE(blacklist, 0) != 1")
    return " AND ".join(where), params


def list_short_video(page: int = 1, limit: int = 20, search: str = "",
                     platform: str = "", location: str = "", industry: str = "",
                     sort_by: str = "price", sort_dir: str = "asc",
                     price_min: float = 0, price_max: float = 0,
                     fans_min: int = 0, fans_max: int = 0,
                     can_modify: int = -1, authority_media: int = -1,
                     account_auth: str = "", can_tuwen: int = -1) -> Dict:
    """短视频资源列表 · 只按本地已同步字段筛选（gender 等仅远端支持的筛选不在本地做）。"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        where_sql, params = _short_video_where(
            c, search=search, platform=platform, location=location, industry=industry,
            price_min=price_min, price_max=price_max, fans_min=fans_min, fans_max=fans_max,
            can_modify=can_modify, authority_media=authority_media, account_auth=account_auth,
            can_tuwen=can_tuwen,
        )
        allowed_sorts = {
            "id", "price", "media_name", "fans_num", "avg_likes_num", "total_likes_num",
        }
        sb = sort_by if sort_by in allowed_sorts else "price"
        sd = "DESC" if sort_dir.lower() == "desc" else "ASC"

        c.execute(f"SELECT COUNT(*) AS total FROM mhz_short_video WHERE {where_sql}", params)
        total = c.fetchone()["total"]

        offset = (page - 1) * limit
        _cols = _public_select_list(c, "mhz_short_video", SHORT_VIDEO_PUBLIC_COLUMNS)
        c.execute(
            f"SELECT {_cols} FROM mhz_short_video WHERE {where_sql} ORDER BY {sb} {sd}, id ASC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        media = [dict(r) for r in c.fetchall()]
        return {"media": media, "total": total, "page": page, "pages": (total + limit - 1) // limit}
    finally:
        conn.close()


def get_short_video_filters(search: str = "", platform: str = "", location: str = "",
                            price_min: float = 0, price_max: float = 0,
                            fans_min: int = 0, fans_max: int = 0, can_modify: int = -1,
                            authority_media: int = -1, account_auth: str = "", can_tuwen: int = -1) -> Dict:
    """短视频筛选项。行业与自媒体同治(同一套 L1 大类 + facet 计数 + 0 家隐藏)。

    短视频目录的行业本来就是 20 个单值词(`汽车交通` / `IT科技` …),没有斜杠组合病;
    但它与自媒体**各说各话**,同一个概念两个 tab 名字不同。归一到同一套 L1 之后
    两个 tab 的行业筛选口径才一致,同时也顺带拿到「0 家不渲染」。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        # Static option vocabulary comes from the same available content/platform pool.
        # Do not narrow a dimension by itself: users must be able to switch back.
        _pool_where, _pool_params = _short_video_where(c, platform=platform if can_tuwen >= 0 else "", can_tuwen=can_tuwen)
        c.execute(f"SELECT DISTINCT platform FROM mhz_short_video WHERE {_pool_where} AND platform != '' ORDER BY platform", _pool_params)
        platforms = [r["platform"] for r in c.fetchall()]
        c.execute(f"SELECT DISTINCT location FROM mhz_short_video WHERE {_pool_where} AND location != '' ORDER BY location", _pool_params)
        locations = [r["location"] for r in c.fetchall()]
        _facet_where, _facet_params = _short_video_where(
            c, search=search, platform=platform, location=location,
            price_min=price_min, price_max=price_max, fans_min=fans_min, fans_max=fans_max,
            can_modify=can_modify, authority_media=authority_media, account_auth=account_auth, can_tuwen=can_tuwen,
        )
        industries = _industry_facets(c, "mhz_short_video", _facet_where, _facet_params)
        c.execute(f"SELECT DISTINCT account_auth FROM mhz_short_video WHERE {_pool_where} AND account_auth != '' ORDER BY account_auth", _pool_params)
        account_auths = [r["account_auth"] for r in c.fetchall()]
        return {
            "platforms": platforms, "locations": locations,
            "industries": industries, "account_auths": account_auths,
        }
    finally:
        conn.close()


# ============================================================================
# 🔴 服务端**权威归属**:这份内容到底属于哪个品牌(2026-08-10 · P1 张冠李戴)
# ============================================================================
#
# ## 病根一句话
#
# `mhz_publish_order_items.brand_id` 一直是**客户端说了算**的。
# 唯一的服务端兜底(2026-05-29 加的)只覆盖软文,而且是 `it.get("brand_id") or _fallback`
# —— `or` 短路 = **客户端值优先**,兜底只在客户端没给值时才生效。
#
# svideo 更彻底:它用 `-draft_id` 当 `article_id`,而兜底查的是 `articles.id = <负数>`,
# **结构上永远查不到** → 兜底恒 None → 客户端值原样落库。
# 生产实证:svideo 全库 3 条,**2 条错记到别的真实客户名下**,两个受害方还分属不同服务商。
#
# ## 为什么是一个函数而不是各路径各修
#
# 「这份内容属于谁」是**同一个问题**,软文和 svideo 只是来源不同。分两处写,
# 下次加第三种来源(比如长文/播客)就会再漏一次 —— 本仓已经因为"同一件事写两处"
# 吃过好几次亏。所以收成单点,新增来源只在这里加一个分支。
#
# ## 与"算价"对齐的范式
#
# 同一个下单端点里,**价格早就不信客户端**了(`_recompute_publish_charge`:服务端重算 →
# 与客户端不一致 → 以服务端为准 + WARNING)。归属照抄这个形状,不另立范式。


def resolve_authoritative_brand_id(*, article_id=None, geo_post_id=None,
                                   cur=None) -> tuple:
    """服务端反查这份内容的**权威**归属品牌。返回 `(brand_id, 依据)`。

    依据取值:`geo_post` / `svideo_draft_source` / `article_quote` / `""`(查不到)。
    查不到返回 `(None, "")` —— 那是"没有权威来源"(比如用户自己上传的视频),
    **不是**"归属为空",调用方此时才允许退回客户端值(且仍要过 RBAC)。

    🔴 `cur` 传进来时会在**调用方的事务里**执行,所以必须包 SAVEPOINT ——
       否则这里任何一句 SQL 抛错都会把调用方**整个事务打废**(而本函数是
       fail-soft 的,失败只该退化成"查不到",不该连累人家下单)。
       既有那段 `_brand_fallback` 就是裸 try/except,踩的是同一个坑,已一并收进来。

    🔴 **不过滤 `deleted_at`**:作品被软删不改变它曾经属于谁。归属是历史事实,
       不是当前可见性。
    """
    own = cur is None
    conn = _get_conn() if own else None
    c = conn.cursor() if own else cur
    sp = None
    try:
        if not own:
            sp = "sp_auth_brand"
            c.execute("SAVEPOINT %s" % sp)

        gid = int(geo_post_id) if geo_post_id else None
        basis = "geo_post"

        # ② svideo:草稿自己记着来源(migration 033 加的列)。
        #    这一条正是"svideo 永远没有服务端校验通道"的补丁 —— 没有它,
        #    下面那条 articles 查询对 svideo 恒空。
        if gid is None and article_id is not None and int(article_id) < 0:
            c.execute("SELECT geo_post_id FROM mhz_short_video_drafts WHERE id = %s",
                      (-int(article_id),))
            row = c.fetchone()
            if row and row.get("geo_post_id"):
                gid = int(row["geo_post_id"])
                basis = "svideo_draft_source"

        # ①② 归到同一条:有来源作品 id → 作品的 brand_id 就是权威值
        if gid is not None:
            c.execute("SELECT brand_id FROM geo_douyin_posts WHERE id = %s", (gid,))
            row = c.fetchone()
            if row and row.get("brand_id"):
                return int(row["brand_id"]), basis

        # ③ 软文:既有的 article → quote 链(口径逐字不变,只是搬进来)
        if article_id is not None and int(article_id) > 0:
            c.execute("""
                SELECT q.brand_id FROM articles a
                JOIN quotes q ON q.id = a.quote_id
                WHERE a.id = %s AND q.brand_id IS NOT NULL
                LIMIT 1
            """, (int(article_id),))
            row = c.fetchone()
            if row and row.get("brand_id"):
                return int(row["brand_id"]), "article_quote"

        return None, ""
    except Exception as exc:  # noqa: BLE001 — 查不到权威值不该挡住下单
        logger.warning("[归属] 权威值反查失败 article_id=%s geo_post_id=%s: %s",
                       article_id, geo_post_id, str(exc)[:200])
        if sp is not None:
            try:
                c.execute("ROLLBACK TO SAVEPOINT %s" % sp)
            except Exception:
                pass
        return None, ""
    finally:
        if sp is not None:
            try:
                c.execute("RELEASE SAVEPOINT %s" % sp)
            except Exception:
                pass
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def create_short_video_draft(user_id: int, brand_id, title: str, content: str = "",
                             keyword: str = "", video_url: str = "",
                             cover_image: str = "", customer_name: str = "",
                             article_type: int = 1, image_urls: str = "",
                             geo_post_id=None) -> int:
    """铸一条短视频"视频稿"占位并返回 draft.id。订单以 -draft.id 作 article_id。

    [T5 · 2026-07-30] article_type/image_urls 供图文笔记模式使用(默认 1/'' → 老调用方零感知)。

    🔴 `geo_post_id`(2026-08-10):内容来源。**草稿必须记住自己从哪来** ——
       服务端事后要靠它反查权威归属,不然 svideo 这条路永远只能信客户端。
       INSERT **显式带这一列**:迁移 033 漏跑时当场 UndefinedColumn 抛出,
       而不是静默少写一列(见迁移文件头「漏跑后果响亮」那段)。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO mhz_short_video_drafts
                (user_id, brand_id, title, content, keyword, video_url, cover_image, customer_name,
                 article_type, image_urls, geo_post_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (user_id, brand_id, title or "", content or "", keyword or "",
              video_url or "", cover_image or "", customer_name or "",
              int(article_type or 1), image_urls or "",
              int(geo_post_id) if geo_post_id else None))
        draft_id = c.fetchone()["id"]
        conn.commit()
        return int(draft_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_short_video_draft(draft_id: int) -> Optional[Dict]:
    """按 draft.id 取视频稿（重投/回流重建 publish 载荷用）。"""
    if not draft_id:
        return None
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM mhz_short_video_drafts WHERE id = %s", (int(draft_id),))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_recent_svideo_duplicates(user_id: int, media_ids: List[int], video_url: str,
                                  window_minutes: int = 30, image_urls: str = "") -> List[int]:
    """[svideo lane · Codex 复审 2026-07-04] 短视频专用重复提交拦截，扣费前调用。

    短视频每次提交都新铸 draft(article_id 全新负数)，老去重(article_id+media_id)
    对短视频永不命中 → 同视频同账号连点几次会各自成单各自扣费。本函数按
    (user_id + media_id + 素材 + 时间窗) 查未终态订单，命中返回冲突 media_id。
    终态(withdrawn/rejected/failed/cancelled)不算占用，与软文 find_active_orders_for_media 口径一致。

    [T5 · 2026-07-30] 图文笔记模式 video_url 为空、素材是 image_urls → 老实现
      `if not video_url: return []` 会**直接放行**，等于图文模式完全没有重复提交拦截
      (同一组图连点几次各自成单各自扣费)。故按传入的素材切匹配列：
      video_url 非空 → 匹配 d.video_url；否则用 image_urls 匹配 d.image_urls。
      两者都空仍返 [](与老行为一致：没有素材就没有可比对的键)。
    """
    _video = (video_url or "").strip()
    _images = (image_urls or "").strip()
    if not (user_id and media_ids and (_video or _images)):
        return []
    if _video:
        _match_col, _match_val = "d.video_url", _video
    else:
        _match_col, _match_val = "d.image_urls", _images
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(f"""
            SELECT DISTINCT i.media_id
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            JOIN mhz_short_video_drafts d ON d.id = -o.article_id
            WHERE o.user_id = %s
              AND o.article_id < 0
              AND i.media_type = 'svideo'
              AND i.media_id = ANY(%s)
              AND {_match_col} = %s
              AND i.status NOT IN ('withdrawn', 'rejected', 'failed', 'cancelled')
              AND i.created_at >= NOW() - INTERVAL '{int(window_minutes)} minutes'
        """, (user_id, media_ids, _match_val))
        return [r["media_id"] for r in c.fetchall()]
    finally:
        conn.close()


# ========================================
# 代发订单
# ========================================

def create_order(user_id: int, article_id: int, article_title: str,
                 items: List[Dict], admin_exempt: bool = False) -> Dict:
    conn = _get_conn()
    try:
        c = conn.cursor()
        # A6/A8: opt-in strict publication gate.  It is default-off until the
        # staged migration is verified, then blocks unreviewed/failed articles.
        from services.article_review_gate import assert_publication_eligible
        assert_publication_eligible(article_id, cursor=c)
        _submission_title = article_title
        _submission_content = None
        _submission_hash = None
        _submission_at = None
        _submission_source = None
        if int(article_id) > 0:
            c.execute("SELECT title, content FROM articles WHERE id=%s", (article_id,))
            _article_row = c.fetchone()
            if not _article_row:
                raise ValueError("article_not_found")
            import hashlib as _hashlib
            _submission_title = str(_article_row.get("title") or article_title or "")
            _submission_content = str(_article_row.get("content") or "")
            _submission_hash = _hashlib.sha256(_submission_content.encode("utf-8")).hexdigest()
            _submission_at = datetime.now(timezone.utc)
            _submission_source = "article_at_order_submission"
        total_points = sum(it.get("cost_points", 0) for it in items)
        # [BUG-P0-1] 真实扣费额:admin 免扣订单 = 0(退款据此 cap,免扣订单失败退 0 不凭空);
        # 非免扣订单 = 订单总价(扣费 == 订单 == 退款一致 · A-8-1 服务端权威价)
        actually_deducted = 0 if admin_exempt else total_points
        c.execute("""
            INSERT INTO mhz_publish_orders (
                user_id, article_id, article_title, status, total_items,
                total_cost_points, admin_exempt, actually_deducted_points,
                article_content_snapshot, article_content_snapshot_hash,
                article_content_snapshot_at, article_content_snapshot_source
            ) VALUES (%s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            user_id, article_id, _submission_title, len(items), total_points,
            admin_exempt, actually_deducted, _submission_content, _submission_hash,
            _submission_at, _submission_source,
        ))
        order_id = c.fetchone()["id"]

        # [CTO-15.23 2026-05-29 P2] brand_id 兜底:item 未带 brand_id 时经 article→quote.brand_id 反填。
        # 防 mhz_publish_order_items.brand_id 历史 108/108 全 NULL(只靠 article→quote 间接链 · article 删即断)。
        #
        # 🔴🔴 2026-08-10 P1 返工:这里原来是 `it.get("brand_id") or _brand_fallback`
        #    —— `or` 短路 = **客户端值优先**,服务端算出来的权威值只在客户端没给时才用上。
        #    于是客户端传错一个 brand_id,内容就被记到别的真实客户名下,服务端全程不吭声。
        #    生产实证:svideo 3 条里 2 条错记(66.7%),两个受害方分属不同服务商。
        #    **改成服务端优先**;客户端值只在"查不到权威来源"时才用(比如用户自己上传的视频)。
        #
        # 🔴 并且反查收进 `resolve_authoritative_brand_id` 单点:
        #    原来这段只查 `articles.id = article_id`,而 svideo 的 article_id 是**负数**,
        #    结构上永远查不到 → svideo 一条服务端校验通道都没有。
        #
        # 🔴 SAVEPOINT:原来是裸 `try/except` 在**调用方的事务里**跑 SQL,
        #    抛一次就把整笔下单的事务打废(fail-soft 反而变成 fail-loud)。已收进解析器内部。
        _brand_fallback, _brand_basis = resolve_authoritative_brand_id(
            article_id=article_id, cur=c)

        # 🔴 [2026-08-10] 收集 item id:上游要把它们回写进 `geo_douyin_posts.publish_item_ids`,
        #    让作品的主人在创作中心看得见"这条发到哪几个账号去了"。
        #    additive —— 老调用方只读 order_id/total_* 三个键,不受影响。
        _item_ids: list = []
        for it in items:
            _client_bid = it.get("brand_id")
            _bid = _brand_fallback or _client_bid
            if _brand_fallback and _client_bid and int(_client_bid) != int(_brand_fallback):
                # 不一致时**以服务端为准**,并留痕 —— 与同端点的算价不一致告警同形状。
                logger.warning(
                    "[归属] 客户端 brand_id=%s 与服务端权威值 %s(依据 %s)不一致,"
                    "以服务端为准 · order article_id=%s media_id=%s",
                    _client_bid, _brand_fallback, _brand_basis, article_id,
                    it.get("media_id"))
            c.execute("""
                INSERT INTO mhz_publish_order_items
                    (order_id, user_id, media_id, media_name, cost_points, cost_yuan, status, brand_id, media_type)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                RETURNING id
            """, (order_id, user_id, it["media_id"], it["media_name"],
                  it.get("cost_points", 0), it.get("cost_yuan", 0), _bid,
                  it.get("media_type")))
            _row = c.fetchone()
            if _row and _row.get("id") is not None:
                _item_ids.append(int(_row["id"]))
        conn.commit()
        return {"order_id": order_id, "total_items": len(items),
                "total_cost_points": total_points, "item_ids": _item_ids}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


#: 单号里**不该**出现的分隔符 —— 出现即说明这是一串而不是一个。
#: 🔴 只列真正的分隔符,**不含** `-`:单号本身可能带连字符,
#:    把它算进来会把合法单号误判成批。
_ORDER_SN_SEPARATORS = (",", ";", "|", " ", "\t", "\n", "\r")


def _looks_like_multi_order_sn(value) -> bool:
    """这个值像不像**一串**单号(而不是一个)。

    判据是「含分隔符」而不是「长度超过 N」:长度是猜的,分隔符是事实。
    """
    text = str(value or "")
    return any(sep in text for sep in _ORDER_SN_SEPARATORS)


def order_sn_segment_count(value) -> int:
    """一个 `mhz_order_id` 里有几段。普查与告警用;1 = 正常,>1 = 存了整批。"""
    import re as _re

    text = str(value or "").strip()
    if not text:
        return 0
    return len([p for p in _re.split(r"[,;|\s]+", text) if p])


def update_order_item_submitted(item_id: int, mhz_order_id: str) -> bool:
    """把 item 标为已提交并落外部单号。

    🔴 [WO-PUB-ZOMBIE-2026-08-04] fail-closed:``mhz_order_id`` 为空时**什么都不写**
    并返回 False。空单号的 ``submitted`` 是个自相矛盾的状态 —— 状态同步任务靠单号
    回写,够不着它;去重判据又把它算作"在发",于是那篇文章对那家媒体永久锁死。
    全仓有 6 处调用点把 ``sn_map.get(...) or result.order_sn`` 直接传进来,任何一处
    两边都取空就会造出僵尸,所以闸设在这里(唯一写入点)而不是逐个调用点。

    Returns: True 已落库；False 单号为空,未做任何修改(调用方必须另行收口)。
    """
    if not (mhz_order_id and str(mhz_order_id).strip()):
        logger.error(
            f"[WO-PUB-ZOMBIE] 拒绝把 item={item_id} 标为 submitted:外部单号为空。"
            f"调用方需改走失败收口/awaiting_sync,不能留下无单号的在途条目"
        )
        return False
    # 🔴 [#151-B 2026-09-08] 一条 item 只能存**它自己那一个**单号。
    #    实证:批量提交时 `order_sn_map = {m: inline_sn for m in media_ids}`
    #    把**批级回执**扇给了每个 media —— 22 条 item 各存了整批 5 或 7 个单号
    #    (正常 651 条段数=1;段数>1 的恰好就是卡死全集)。
    #    后果是回写 `WHERE mhz_order_id = %s` **永远对不上**:
    #    供应商那 22 个单号早已终态(17 已发布带 url / 5 被拒),
    #    而本地 27 天一直显示「在发」—— 两边各自看都正常。
    #    闸设在这里(与上面空单号那道同处)是因为**这是唯一写入点**;
    #    逐个调用点设闸,漏一处就再造一批。
    if _looks_like_multi_order_sn(mhz_order_id):
        logger.error(
            "[#151-B] 拒绝把 item=%s 标为 submitted:单号像**整批回执**而不是单条"
            "(%r)。批级值扇给每条会让状态回写永远匹配不上;"
            "调用方应按 media 逐条反查单号,拿不到就走 awaiting_sync。",
            item_id, str(mhz_order_id)[:120])
        return False
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE mhz_publish_order_items SET status = 'submitted', mhz_order_id = %s, submitted_at = NOW()
            WHERE id = %s
        """, (str(mhz_order_id).strip(), item_id))
        conn.commit()
        return True
    finally:
        conn.close()


def set_item_submission_snapshot(
    item_ids: List[int], *, title: str, content: str, source: str,
    legal_catalog_version: str | None = None,
) -> int:
    """Freeze the exact channel payload immediately before one submit attempt.

    A retry may replace the pending snapshot.  Once an item reaches a terminal
    published state, the article-level immutable snapshot is captured from this
    value and no later edit can change it.

    [统一 R3 · 2026-07-23 §五] ``legal_catalog_version`` 为 additive 血缘字段:
    提供时一并冻结外发判定所用的法律禁止清单版本;缺省(旧调用方)不动该列,
    历史行保持 NULL=版本未知,不回填。
    """
    ids = sorted({int(item_id) for item_id in item_ids if int(item_id) > 0})
    if not ids:
        return 0
    import hashlib as _hashlib

    body = str(content or "")
    digest = _hashlib.sha256(body.encode("utf-8")).hexdigest()
    version_sql = ""
    params: list = [str(title or ""), body, digest, str(source or "channel_submit")[:80]]
    if legal_catalog_version is not None:
        version_sql = ",\n                   submitted_legal_catalog_version=%s"
        params.append(str(legal_catalog_version)[:64])
    params.append(ids)
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(
            f"""
            UPDATE mhz_publish_order_items
               SET submitted_title_snapshot=%s,
                   submitted_content_snapshot=%s,
                   submitted_content_snapshot_hash=%s,
                   submitted_content_snapshot_at=NOW(),
                   submitted_content_snapshot_source=%s{version_sql}
             WHERE id = ANY(%s)
               AND status IN ('pending','submitting','awaiting_confirmation','awaiting_sync')
            """,
            params,
        )
        changed = c.rowcount
        conn.commit()
        return int(changed or 0)
    finally:
        conn.close()


#: 提交重试上限的唯一权威值(挂起逻辑与锁逻辑必须同源,否则一边停一边还在转)
DEFAULT_MAX_SUBMIT_ATTEMPTS = 3


def try_lock_for_submit(item_id: int, max_attempts: int = DEFAULT_MAX_SUBMIT_ATTEMPTS) -> bool:
    """提交前置乐观锁 — 用 SQL 条件更新原子化把 'pending' 改成 'submitting'。

    目的：堵死所有并发重复提交的可能性。任何调用方（API/调度器/重试）
    在调外部发布通道 publish 前都必须先拿到这个锁。

    成功返回 True：拿到锁，调用方可以放心提交
    失败返回 False：item 已不在 pending 状态，或重试次数耗尽 → 跳过

    [CTO-15.23 2026-05-19 P0 BUG 修 · 老板报"今日看板状态没同步 · 用户拒稿了但显示准备中"]
    根因:旧逻辑 attempts >= 3 时返 False 但不标 failed · 孤儿永远卡 pending + 数据不同步
    SSH 实证 5 个孤儿(全表 submit_attempts=3 + status=pending + mhz_order_id NULL):
      - order 172/173 items 用户付费但永远显示"准备中" · 实际媒介盒子早已 reject
    修法:lock 前先 fail-safe scan · attempts 用完 + 仍 pending → 主动标 failed + 触发退款
          (跟 release_submit_lock 一致 · 但兜底死分支)

    Args:
        item_id: mhz_publish_order_items 行 ID
        max_attempts: 最大重试次数（默认 3 次后不再重试）
    """
    conn = _get_conn()
    needs_refund = False
    refund_user_id: Optional[int] = None
    refund_cost: int = 0
    refund_reason: str = ""

    try:
        c = conn.cursor()
        # [P0 BUG 修] fail-safe:进入时若 attempts 已耗尽但 status 仍 pending(孤儿)→ 标 failed 并触发退款
        # WHERE 用 attempts >= max_attempts 严格匹配 · 正常 attempts<max 不会命中(下面 lock 走正路)
        c.execute("""
            UPDATE mhz_publish_order_items
            SET status = 'failed',
                reject_reason = COALESCE(NULLIF(reject_reason, ''), %s)
            WHERE id = %s
              AND status = 'pending'
              AND COALESCE(submit_attempts, 0) >= %s
            RETURNING user_id, cost_points, reject_reason
        """, ("提交次数耗尽 · 系统自动标失败(lock fail-safe)", item_id, max_attempts))
        fail_row = c.fetchone()
        if fail_row:
            needs_refund = True
            refund_user_id = int(fail_row["user_id"])
            refund_cost = int(fail_row["cost_points"] or 0)
            refund_reason = fail_row["reject_reason"] or "提交次数耗尽"
            logger.warning(
                f"[P0 BUG 修 · try_lock_for_submit] item={item_id} 孤儿 pending 强标 failed · "
                f"user={refund_user_id} cost={refund_cost} 触发退款"
            )

        # 然后尝试拿锁:status='pending' AND 重试次数未达上限
        c.execute("""
            UPDATE mhz_publish_order_items
            SET status = 'submitting',
                submit_attempts = COALESCE(submit_attempts, 0) + 1,
                last_submit_at = NOW()
            WHERE id = %s
              AND status = 'pending'
              AND COALESCE(submit_attempts, 0) < %s
            RETURNING id
        """, (item_id, max_attempts))
        row = c.fetchone()
        conn.commit()
    finally:
        conn.close()

    # [P0 BUG 修] 标 failed 后按 item 退款(独立 connection 避免长事务)
    if needs_refund and refund_cost > 0 and refund_user_id is not None:
        try:
            refund_for_publish_order(
                user_id=refund_user_id,
                amount=refund_cost,
                refund_key=f"item:{item_id}",
                reason=f"提交次数耗尽自动退款: {refund_reason[:200]}",
            )
        except Exception as _e:
            logger.error(f"[P0 BUG 修 · try_lock_for_submit] item={item_id} 自动退款异常: {_e}")

    return row is not None


def _publish_refund_cap(requested_amount: int, actually_deducted, already_refunded: int) -> int:
    """[BUG-P0-1] 代发退款 cap:实退 = min(请求额, max(0, 真实扣费 - 已退))。

    - actually_deducted=0(admin 免扣订单)→ 返回 0(不退,防凭空注入)
    - actually_deducted=None(老订单无对账字段)→ 返回请求额(维持原行为·老凭空靠回收 SQL 清)
    - 否则按"真实扣费 - 已退"夹紧,绝不退超订单真实扣费。
    """
    if actually_deducted is None:
        return int(requested_amount)
    cap = max(0, int(actually_deducted) - int(already_refunded or 0))
    return min(int(requested_amount), cap)


# [BUG-1 2026-07-27] 代发退款账本路由 · 退款必须回到"客户真正花得出去"的钱包
# 根因:refund_for_publish_order 无条件退到 user_wallets.paid_points,完全不判扣费来源。
# V3.5 客户(customer_agent_credit_wallets 有 row)消费一律走 consume_credit 扣信用钱包,
# 其 user_wallets 余额已被 settlement 代收/revoke 清空,退到那里客户【花不出去】=资产被劈成两半。
# 生产实证 user 149:信用钱包扣 32,955(全 tool 池),退款 12 笔共 22,425 却进了平台 paid。
_PUBLISH_REFUND_LEDGER_V35 = "v35_credit"
_PUBLISH_REFUND_LEDGER_PLATFORM = "platform"
# V3.5 侧退款流水的 source 标记 · 与 billing 的 'tool_fail_refund' 区分,便于幂等与对账
_PUBLISH_REFUND_CREDIT_SOURCE = "publish_proxy_refund"


def _publish_refund_ledger(cursor, user_id: int) -> str:
    """判定这个用户的代发退款该回哪个账本 —— 单账本后恒为平台钱包。

    [单账本接线 2026-08-17 · C-1] **不再查 `customer_agent_credit_wallets` 有没有 row**。

    原判据「表里有 row = V3.5 客户 → 退信用钱包」在 2026-07-29 单账本收敛后**结论反了**:
    收敛把 5 户的余额并进 `user_wallets` 并清零三池,但**保留了那 5 行 row** →
    判据恒命中 → 退款写进停写表;而消费侧(`middleware/billing.py` 的
    `check_balance_only` / `deduct_points`)已统一只读 `user_wallets` →
    退回去的算力客户**花不出去**,展示层(`db/wallet_db.py` 的 `paid_points_total`)
    却把三池加进总额 → 余额虚高、一花就 402。

    单账本后正确口径只有一条:**客户的算力只有 `user_wallets` 一处,退款一律落那里**。
    这与 `services/customer_entitlement.py` 的 grant/revoke 落点、以及迁移脚本
    `wallet_credit_merge_migrate_2026_07_27.sql` 的映射完全同源。

    函数与常量保留(不改调用方签名),恒返平台账本;`_PUBLISH_REFUND_LEDGER_V35`
    仅供 `_publish_already_refunded` 读**历史**信用侧退款流水时作 source 标记使用。
    """
    return _PUBLISH_REFUND_LEDGER_PLATFORM


def _publish_already_refunded(cursor, user_id: int, order_id: int) -> int:
    """该订单已退累计(跨两个账本合并)。

    退款上限 cap 必须跨账本统一,否则"平台退过一次 + 信用再退一次"会双退凭空注入。
    """
    cursor.execute("""
        SELECT COALESCE(SUM(amount), 0)::bigint AS refunded
        FROM point_transactions
        WHERE type = 'refund' AND feature_code = 'media_proxy_publish' AND user_id = %s
          AND (order_id IN (SELECT 'item:' || id FROM mhz_publish_order_items WHERE order_id = %s)
               OR order_id = 'order:' || %s)
    """, (user_id, order_id, order_id))
    _platform = int((cursor.fetchone() or {}).get("refunded") or 0)

    cursor.execute("""
        SELECT COALESCE(SUM(points), 0)::bigint AS refunded
        FROM customer_credit_transactions
        WHERE type = 'refund' AND source = %s AND customer_user_id = %s
          AND (related_order_id IN (SELECT 'item:' || id FROM mhz_publish_order_items WHERE order_id = %s)
               OR related_order_id = 'order:' || %s)
    """, (_PUBLISH_REFUND_CREDIT_SOURCE, user_id, order_id, order_id))
    _credit = int((cursor.fetchone() or {}).get("refunded") or 0)
    return _platform + _credit


# [单账本接线 2026-08-17 · C-1] `_v35_publish_refund_pool_split` 已删除。
# 它的唯一职责是把代发退款额按 tool/publish 占比拆回停写表三池;退款落点改为
# user_wallets.paid_points(单账本无 tool/publish 之分)后,该函数随其唯一调用方
# 一起失去意义。删除时全仓 grep 实证 0 调用方(详见交付单 R2 §孤儿清理)。


def _sanitize_publish_refund_reason(reason) -> str:
    """清洗退款原因 · 去供应商代号(mhz=媒介盒子)+ 英文术语(大小写不敏感)。

    [2026-05-30 bug2/P2] memory feedback_no_supplier_names_to_users
    [BUG-1 2026-07-27] 从 refund_for_publish_order 内联抽出 · 两个账本分支共用
    """
    import re as _re
    _clean = str(reason or "媒体发布未成功")
    for _pat, _cn in ((r"mhz[ _]?rejected", "媒体审核未通过"), (r"\bmhz\b", "媒体平台"),
                      (r"item\s*提交失败", "内容提交失败"), (r"\bitem\b", "内容"),
                      (r"\brejected\b", "审核未通过"), (r"reject_reason", "审核原因")):
        _clean = _re.sub(_pat, _cn, _clean, flags=_re.IGNORECASE)
    return _clean


def _enqueue_publish_notification(
    cursor,
    *,
    user_id: int,
    refund_key: str,
    event_type: str,
    terminal_state: str,
    status: str,
    points: Optional[int] = None,
    include_admins: bool = False,
) -> None:
    from services.notification_events import RecipientKind
    from services.notification_outbox import (
        enqueue_admin_notification_events,
        enqueue_notification_event,
    )

    facts = {
        "business_no": str(refund_key),
        "status": status,
        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": "平台已记录该发布任务，请在发布中心查看处理结果。",
    }
    if points is not None:
        facts["points"] = str(int(points))
    enqueue_notification_event(
        cursor,
        event_type=event_type,
        business_id=str(refund_key),
        terminal_state=terminal_state,
        recipient_user_id=int(user_id),
        recipient_kind=RecipientKind.USER,
        facts=facts,
    )
    if include_admins:
        enqueue_admin_notification_events(
            cursor,
            event_type=event_type,
            business_id=str(refund_key),
            terminal_state=terminal_state,
            facts=facts,
        )


def refund_for_publish_order(
    user_id: int,
    amount: int,
    refund_key: str,
    reason: str,
) -> dict:
    """代发场景统一退款入口（按订单/项粒度，幂等）。

    所有代发链路上的退款（内容空、提交失败、拒稿、超时、批量逐项失败）
    都走这一个函数，确保：
      1. 幂等：同 refund_key 不重复退款（**仍跨两个账本都查** —— 历史上退到信用侧的
         那批必须能被认出来，否则会在平台侧重退一次）
      2. 一致：**一律退回 `user_wallets.paid_points`** —— [单账本接线 2026-08-17 · C-1]
         · 2026-07-29 单账本收敛后，客户算力只有 user_wallets 一处
         · 原 [BUG-1 2026-07-27] 的「V3.5 客户退回信用钱包」分支**已删除**：
           它按「停写表有没有 row」判身份，而收敛保留了 5 行残行 → 判据恒命中 →
           退款写进已停写的表；消费侧只读 user_wallets → 客户**看得见花不出去**
         · 详见 `_publish_refund_ledger` 的注释
      3. 审计：写 point_transactions（order_id=refund_key）。
         信用侧 customer_credit_transactions **只读不写**（历史账本）

    Args:
        user_id: 用户 ID
        amount: 退款积分（正整数）
        refund_key: 幂等键，建议用 f"order:{order_id}" 或 f"item:{item_id}"
                   同一 refund_key 二次调用会被幂等拦截
        reason: 退款原因（写入 transaction.description）

    Returns:
        {"success": bool, "refunded": int, "skipped": bool, "reason": str}
    """
    if amount <= 0:
        return {"success": False, "refunded": 0, "skipped": True, "reason": "金额非正"}

    conn = _get_conn()
    try:
        c = conn.cursor()

        # ── [返工 2026-08-18 · Codex P0-06] 新链隔离:freeze_per_item 不走老退款 ──
        #
        # 🔴 GEO 图文合同链的 item 是 **freeze → commit / release** 口径:
        #    钱还冻在 `point_freezes` 里,从没被 `deduct_points` 扣走过。
        #    老链这条 `refund_for_publish_order` 是 `deduct_upfront` 的反向操作 ——
        #    它会往钱包里**加**一笔。两条链叠在一起 = 冻结照样 release 一次、
        #    钱包又凭空多一笔 = **双补偿**。
        #
        # 🔴 闸放在这里(唯一退款入口)而不是逐个调用点:实测调用点 12 处,
        #    分散在 meijiehezi_api / publish_api / scheduler 三个文件。
        #    逐点加判断必然漏,而漏掉的那一处是资金面的。
        #    「资金修复要枚举全 sink」—— 收口成一个 sink 是更强的做法。
        #
        # 🔴 判据用**显式相等**:`billing_mode = 'freeze_per_item'`。
        #    034 零 DML ⇒ 存量行 billing_mode 为 NULL,NULL 天然匹配不上,
        #    老行的退款行为**一个字不变**(反过来写 `<> 'freeze_per_item'`
        #    对 NULL 恒 UNKNOWN,会把所有老行一起挡掉 —— 那才是回归)。
        if str(refund_key or "").startswith("item:"):
            try:
                _item_id = int(str(refund_key).split(":", 1)[1])
            except (ValueError, IndexError):
                _item_id = None
            if _item_id is not None:
                c.execute(
                    "SELECT billing_mode FROM mhz_publish_order_items WHERE id = %s",
                    (_item_id,),
                )
                _row = c.fetchone()
                if _row and str(dict(_row).get("billing_mode") or "") == "freeze_per_item":
                    conn.rollback()
                    return {"success": False, "refunded": 0, "skipped": True,
                            "reason": "freeze_per_item 由冻结链 release,不走老退款"}

        # [单账本接线 2026-08-17 · C-1] 账本恒 = 平台钱包(见 _publish_refund_ledger 注释)
        # 返回值里仍带 ledger 字段(调用方/日志在读),但已不再有分支语义。
        # (并车注 2026-08-20:图文侧的「V3.5 客户退信用钱包」旧注释已被单账本口径取代,并车时删。)
        ledger = _publish_refund_ledger(c, user_id)

        # [关键] 先拿钱包行锁，再做幂等检查 — 防 TOCTOU race
        # 之前的实现：先查幂等再拿锁，两个并发请求都能通过幂等检查后阻塞拿锁，会双退款
        # 现在：行锁串行化两个请求，第一个完成后第二个才查幂等，能正确拦住
        # [BUG-1] 锁必须锁"要改的那张表"的行 —— 单账本后要改的恒是 user_wallets,
        #   所以锁的也恒是 user_wallets 行(原按 is_v35 分支锁停写表行的写法已随判据一起删)。
        c.execute(
            "SELECT paid_points FROM user_wallets WHERE user_id = %s FOR UPDATE",
            (user_id,)
        )
        wallet = c.fetchone()
        if not wallet:
            conn.rollback()
            return {"success": False, "refunded": 0, "skipped": False, "reason": "钱包不存在"}

        # 幂等检查（在行锁内）：refund_key 已存在则跳过
        # [BUG-1] 两个账本都查 —— 历史上退到平台的那批,再次调用不得在信用侧重退。
        c.execute("""
            SELECT id, amount FROM point_transactions
            WHERE user_id = %s
              AND type = 'refund'
              AND feature_code = 'media_proxy_publish'
              AND order_id = %s
            LIMIT 1
        """, (user_id, refund_key))
        existing_refund = c.fetchone()
        if not existing_refund:
            c.execute("""
                SELECT MIN(id) AS id, COALESCE(SUM(points), 0)::bigint AS amount
                FROM customer_credit_transactions
                WHERE customer_user_id = %s
                  AND type = 'refund'
                  AND source = %s
                  AND related_order_id = %s
            """, (user_id, _PUBLISH_REFUND_CREDIT_SOURCE, refund_key))
            _credit_refund = c.fetchone()
            if _credit_refund and _credit_refund.get("id") is not None:
                existing_refund = _credit_refund
        if existing_refund:
            _enqueue_publish_notification(
                c,
                user_id=user_id,
                refund_key=refund_key,
                event_type="publication.refunded",
                terminal_state="refunded",
                status="已退回",
                points=int(existing_refund.get("amount") or 0),
            )
            conn.commit()
            return {"success": True, "refunded": 0, "skipped": True, "reason": "已退过（幂等）"}

        # [BUG-P0-1] 退款对账 cap:admin 免扣订单不退;退款额 ≤ 订单真实扣费 - 该订单已退。
        # 根因:admin 免扣 deduct=0,旧逻辑仍按 cost_points 全额退 → 钱包凭空注入(~22 万)。
        # 仅 refund_key='item:{id}' 主路径 cap(所有自动退款均走此键);老订单 actually_deducted
        # IS NULL → 跳过维持原行为(老凭空靠回收 SQL 清)。
        import re as _re_cap
        _m = _re_cap.match(r'^item:(\d+)$', refund_key or "")
        if _m:
            c.execute("""
                SELECT o.id AS oid, o.actually_deducted_points
                FROM mhz_publish_order_items it
                JOIN mhz_publish_orders o ON o.id = it.order_id
                WHERE it.id = %s
            """, (int(_m.group(1)),))
            _row = c.fetchone()
            if _row and _row["actually_deducted_points"] is not None:
                # [BUG-1] 已退累计跨两账本合并 · 防"平台退一次 + 信用退一次"绕过 cap
                _already = _publish_already_refunded(c, user_id, _row["oid"])
                amount = _publish_refund_cap(amount, _row["actually_deducted_points"], _already)
                if amount <= 0:
                    conn.rollback()
                    return {"success": True, "refunded": 0, "skipped": True,
                            "reason": "admin 免扣订单或已达订单真实扣费上限,不退"}

        _clean_reason = _sanitize_publish_refund_reason(reason)

        # [单账本接线 2026-08-17 · C-1] 原 if is_v35 分支(退回停写表三池)已删除 ——
        # 客户的算力只有 user_wallets 一处,退款一律落这里。
        # 退还到 paid_points（平台钱包 · 单账本唯一落点）
        c.execute("""
            UPDATE user_wallets
            SET paid_points = paid_points + %s, updated_at = NOW()
            WHERE user_id = %s
            RETURNING paid_points
        """, (amount, user_id))
        new_balance = c.fetchone()["paid_points"]

        # 写 transaction（refund_key 存在 order_id 字段，作为幂等键）
        from db.wallet_db import insert_transaction
        insert_transaction(
            c, user_id, "refund", "paid", amount, new_balance,
            feature_code="media_proxy_publish",
            description=f"代发退款: {_clean_reason}",
            order_id=refund_key,
        )
        _enqueue_publish_notification(
            c,
            user_id=user_id,
            refund_key=refund_key,
            event_type="publication.refunded",
            terminal_state="refunded",
            status="已退回",
            points=amount,
        )
        conn.commit()
        return {"success": True, "refunded": amount, "skipped": False,
                "reason": reason, "ledger": ledger}
    except Exception as e:
        conn.rollback()
        return {"success": False, "refunded": 0, "skipped": False, "reason": f"退款异常: {e}"}
    finally:
        conn.close()


def cleanup_orphan_submitting_items(timeout_minutes: int = 5) -> int:
    """清理 submitting 孤儿状态（提交期间崩溃留下的 zombie）。

    判定：status='submitting' AND last_submit_at < NOW() - timeout_minutes
    动作：
      - submit_attempts < 3 → 回 pending（等下次正常重试）
      - submit_attempts >= 3 → 标 failed（之后由 scheduler 退款）

    🔴 [第 3 棒 · Codex R2 P0-5 · 规格 §8.3] **排除 `freeze_per_item` 新链**。
       规格原文:「现有 `cleanup_orphan_submitting_items`、`release_submit_lock`、
       cancel/refund scheduler 通过 lane/billing_mode WHERE 明确排除
       `freeze_per_item` 新链,不能超时盲退或走 legacy refund」。
       两条链对同一行的收敛动作是**相反**的:
         · 老链:超时 5 分钟 → 回 pending 重投 / 标 failed 走老退款;
         · 新链:已 external-start 的**永远不重投**(重复外调 + 重复扣费),
           走 `needs_action + manual` 人工核对(`contract_worker._reconcile_stuck_publish_items`)。
       两个 sweeper 同时盯着同一行时,先跑到的那个说了算 —— 而老的那个跑得更勤。
       谓词显式列老值(NULL 与 `deduct_upfront`):写 `<> 'freeze_per_item'`
       对 NULL 恒 UNKNOWN,会把**全部老行**一起漏掉,那才是回归。

    Returns: 清理的 item 数量
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(f"""
            UPDATE mhz_publish_order_items
            SET status = CASE
                    WHEN COALESCE(submit_attempts, 0) >= 3 THEN 'failed'
                    ELSE 'pending'
                END,
                reject_reason = CASE
                    WHEN COALESCE(submit_attempts, 0) >= 3 THEN '提交超时未完成（孤儿状态自动清理）'
                    ELSE reject_reason
                END
            WHERE status = 'submitting'
              AND (billing_mode IS NULL OR billing_mode = 'deduct_upfront')
              AND last_submit_at < NOW() - INTERVAL '{timeout_minutes} minutes'
            RETURNING id,user_id,status
        """)
        rows = c.fetchall()
        for row in rows:
            if row.get("status") == "failed":
                _enqueue_publish_notification(
                    c,
                    user_id=int(row["user_id"]),
                    refund_key=f"item:{int(row['id'])}",
                    event_type="publication.rejected",
                    terminal_state="failed",
                    status="未完成",
                )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        return 0
    finally:
        conn.close()


def get_recent_pending_orders(within_minutes: int = 60) -> list:
    """获取最近 within_minutes 分钟内创建但仍 pending 的订单（启动 sweep 用）。

    用途：服务启动时立即扫描"扣费了但 mhz 还没收到"的订单，
    避免上一次进程死亡后这些订单要等 10 分钟才被正常调度器接走。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(f"""
            SELECT DISTINCT o.id AS order_id, o.user_id, o.article_id,
                   o.article_title, o.total_cost_points, o.created_at
            FROM mhz_publish_orders o
            JOIN mhz_publish_order_items i ON i.order_id = o.id
            WHERE i.status = 'pending'
              AND i.mhz_order_id IS NULL
              AND COALESCE(i.submit_attempts, 0) < 3
              AND o.created_at > NOW() - INTERVAL '{within_minutes} minutes'
            ORDER BY o.created_at
        """)
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


# ============== 幂等键（防双击/重试导致重复扣费+下单） ==============

def _init_idempotency_table():
    """初始化代发幂等键表（首次启动建表，已存在跳过）"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS publish_idempotency_keys (
                request_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL,
                response_json JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_idempotency_user_created
            ON publish_idempotency_keys(user_id, created_at DESC)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_idempotency_cleanup
            ON publish_idempotency_keys(created_at)
        """)
        conn.commit()
    finally:
        conn.close()


def check_idempotency(request_id: str, user_id: int, endpoint: str) -> dict:
    """查幂等命中。命中返回原响应（dict），未命中返回 None。

    严格匹配 (request_id, user_id, endpoint) 防止 request_id 跨用户/跨端点泄露。
    """
    if not request_id:
        return None
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT response_json FROM publish_idempotency_keys
            WHERE request_id = %s AND user_id = %s AND endpoint = %s
        """, (request_id, user_id, endpoint))
        row = c.fetchone()
        if row:
            return row["response_json"]
        return None
    finally:
        conn.close()


def save_idempotency(request_id: str, user_id: int, endpoint: str, response: dict) -> bool:
    """保存幂等响应。如已存在则忽略（不抛错）"""
    if not request_id:
        return False
    import json as _json
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO publish_idempotency_keys (request_id, user_id, endpoint, response_json)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (request_id) DO NOTHING
        """, (request_id, user_id, endpoint, _json.dumps(response, ensure_ascii=False, default=str)))
        conn.commit()
        return True
    except Exception as _e:
        conn.rollback()
        return False
    finally:
        conn.close()


def cleanup_old_idempotency_keys(retain_hours: int = 24) -> int:
    """清理超过 retain_hours 小时的旧幂等键（每天跑一次即可）"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(f"""
            DELETE FROM publish_idempotency_keys
            WHERE created_at < NOW() - INTERVAL '{retain_hours} hours'
            RETURNING request_id
        """)
        rows = c.fetchall()
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        return 0
    finally:
        conn.close()


def release_submit_lock(item_id: int, success: bool, reject_reason: str = None):
    """释放提交锁 — 在 try_lock_for_submit 之后调用。

    成功（success=True）：本应由 update_order_item_submitted 处理，这里只是兜底
    失败（success=False）：
        - submit_attempts < 3 → 回 pending（等下次重试）
        - submit_attempts >= 3 → 标 failed + [P1-F] 自动按 item 维度退款
                                 （幂等键 item:{id}，不会和 scheduler 全单退冲突）
    """
    conn = _get_conn()
    needs_refund = False
    user_id = None
    cost_points = 0

    try:
        c = conn.cursor()
        if success:
            # 兜底：如果调用方没单独调 update_order_item_submitted
            c.execute("""
                UPDATE mhz_publish_order_items
                SET status = 'submitted', submitted_at = NOW()
                WHERE id = %s AND status = 'submitting'
            """, (item_id,))
        else:
            # 先查现状决定要不要退款
            c.execute("""
                SELECT user_id, cost_points, COALESCE(submit_attempts, 0) AS attempts
                FROM mhz_publish_order_items
                WHERE id = %s AND status = 'submitting'
            """, (item_id,))
            row = c.fetchone()
            if row and row["attempts"] >= 3:
                needs_refund = True
                user_id = int(row["user_id"])
                cost_points = int(row["cost_points"] or 0)

            # 失败：根据 attempts 判断是回 pending 等下次重试，还是直接标 failed
            c.execute("""
                UPDATE mhz_publish_order_items
                SET status = CASE
                    WHEN COALESCE(submit_attempts, 0) >= 3 THEN 'failed'
                    ELSE 'pending'
                END,
                reject_reason = CASE
                    WHEN COALESCE(submit_attempts, 0) >= 3 THEN %s
                    ELSE reject_reason
                END
                WHERE id = %s AND status = 'submitting'
                RETURNING user_id,status
            """, (reject_reason or "提交失败次数过多，已自动放弃", item_id))
            terminal_row = c.fetchone()
            if terminal_row and terminal_row.get("status") == "failed":
                _enqueue_publish_notification(
                    c,
                    user_id=int(terminal_row["user_id"]),
                    refund_key=f"item:{int(item_id)}",
                    event_type="publication.rejected",
                    terminal_state="failed",
                    status="未完成",
                )
        conn.commit()
    finally:
        conn.close()

    # [P1-F] 标 failed 后按 item 退款（独立 connection 避免长事务）
    if needs_refund and cost_points > 0:
        try:
            refund_for_publish_order(
                user_id=user_id,
                amount=cost_points,
                refund_key=f"item:{item_id}",
                reason=f"item 提交失败次数耗尽，自动退款: {reject_reason or '未知'}",
            )
        except Exception as _e:
            import logging
            logging.getLogger("GEO-MHZ-DB").error(f"[P1-F] item={item_id} 失败退款异常: {_e}")


# ============== 待用户确认（203/204/205 confirm code 流程） ==============

def mark_item_awaiting_confirmation(
    item_id: int,
    code: int,
    msg: str,
    field: str,
) -> bool:
    """把 submitting 状态的 item 改为 awaiting_confirmation，记录 mhz 返回的 code/msg/field。

    多次进入（mhz 一次提交可能依次返回 203 → 204 → 205）会累积 codes/msg，不覆盖。

    Returns: True 成功标记；False item 不在 submitting 状态（被并发改了）。
    """
    import json as _json
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE mhz_publish_order_items
            SET status = 'awaiting_confirmation',
                pending_confirm_codes = COALESCE(pending_confirm_codes, '[]'::jsonb) ||
                    CASE WHEN pending_confirm_codes ? %s THEN '[]'::jsonb
                         ELSE %s::jsonb END,
                pending_confirm_msg = %s,
                awaiting_since = COALESCE(awaiting_since, NOW())
            WHERE id = %s
              AND status = 'submitting'
            RETURNING id
        """, (str(code), _json.dumps([code]), msg, item_id))
        row = c.fetchone()
        conn.commit()
        return row is not None
    except Exception as e:
        conn.rollback()
        logger.error(f"mark_item_awaiting_confirmation 失败 item={item_id}: {e}")
        return False
    finally:
        conn.close()


def get_awaiting_items_by_user(user_id: int) -> List[Dict]:
    """拉取用户名下所有待确认的 item（用于前端列表/弹框）"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT i.id, i.order_id, i.user_id, i.media_id, i.media_name, i.media_type,
                   i.cost_points, i.cost_yuan, i.status,
                   i.pending_confirm_codes, i.pending_confirm_msg,
                   i.pending_confirm_fields, i.awaiting_since,
                   i.brand_id,
                   o.article_id, o.article_title
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            WHERE i.user_id = %s AND i.status = 'awaiting_confirmation'
            ORDER BY i.awaiting_since DESC
        """, (user_id,))
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def get_awaiting_item(item_id: int, user_id: int) -> Optional[Dict]:
    """拿单个 item（带 user_id 校验，防越权）"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT i.*, o.article_id, o.article_title, o.user_id AS order_user_id
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            WHERE i.id = %s AND i.user_id = %s
        """, (item_id, user_id))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def reset_awaiting_to_pending_for_confirm(item_id: int, confirmed_field: str) -> bool:
    """用户点"仍然发布"：把 awaiting → pending，把 confirmed_field 加入 pending_confirm_fields。

    保留 submit_attempts 计数（不重置）—— 已经消耗的尝试次数不能白拿。
    Returns: True 成功；False item 不在 awaiting 状态。
    """
    import json as _json
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE mhz_publish_order_items
            SET status = 'pending',
                pending_confirm_fields = COALESCE(pending_confirm_fields, '[]'::jsonb) ||
                    CASE WHEN pending_confirm_fields ? %s THEN '[]'::jsonb
                         ELSE %s::jsonb END,
                awaiting_since = NULL
            WHERE id = %s
              AND status = 'awaiting_confirmation'
            RETURNING id
        """, (confirmed_field, _json.dumps([confirmed_field]), item_id))
        row = c.fetchone()
        conn.commit()
        return row is not None
    except Exception as e:
        conn.rollback()
        logger.error(f"reset_awaiting_to_pending_for_confirm 失败 item={item_id}: {e}")
        return False
    finally:
        conn.close()


def cancel_awaiting_item(item_id: int, reject_reason: str = "用户取消发布") -> Optional[Dict]:
    """用户点"取消发布"：把 awaiting → cancelled，并按 item 退款。

    返回 dict {user_id, cost_points, refunded} 用于上层日志；item 不存在/状态不对返 None。
    """
    conn = _get_conn()
    user_id = None
    cost_points = 0
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE mhz_publish_order_items
            SET status = 'cancelled',
                reject_reason = %s,
                awaiting_since = NULL
            WHERE id = %s
              AND status = 'awaiting_confirmation'
            RETURNING user_id, COALESCE(cost_points, 0) AS cost_points
        """, (reject_reason, item_id))
        row = c.fetchone()
        if row:
            _enqueue_publish_notification(
                c,
                user_id=int(row["user_id"]),
                refund_key=f"item:{int(item_id)}",
                event_type="publication.withdrawn",
                terminal_state="cancelled",
                status="已取消",
            )
        conn.commit()
        if not row:
            return None
        user_id = int(row["user_id"])
        cost_points = int(row["cost_points"])
    finally:
        conn.close()

    refund_result = {"refunded": 0, "skipped": True}
    if cost_points > 0 and user_id:
        try:
            refund_result = refund_for_publish_order(
                user_id=user_id,
                amount=cost_points,
                refund_key=f"item:{item_id}",
                reason=reject_reason,
            )
        except Exception as e:
            logger.error(f"cancel_awaiting_item 退款异常 item={item_id}: {e}")
            refund_result = {"refunded": 0, "skipped": False, "error": str(e)}

    return {
        "user_id": user_id,
        "cost_points": cost_points,
        "refunded": refund_result.get("refunded", 0),
        "skipped": refund_result.get("skipped", False),
    }


def auto_cancel_stale_awaiting(stale_hours: int = 24) -> int:
    """超过 stale_hours 小时未决的 awaiting_confirmation item 自动走"取消并退款"。

    供 scheduler 周期调用。返回处理的 item 数量。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(f"""
            SELECT id FROM mhz_publish_order_items
            WHERE status = 'awaiting_confirmation'
              AND awaiting_since < NOW() - INTERVAL '{stale_hours} hours'
        """)
        rows = c.fetchall()
    finally:
        conn.close()

    cancelled = 0
    for row in rows:
        item_id = int(row["id"])
        try:
            result = cancel_awaiting_item(item_id, reject_reason=f"超过 {stale_hours} 小时未确认，自动取消")
            if result:
                cancelled += 1
        except Exception as e:
            logger.error(f"auto_cancel_stale_awaiting item={item_id} 异常: {e}")
    return cancelled


def suspend_item_for_user_action(item_id: int, contract: dict, cursor=None) -> bool:
    """[P0 软失败] 把 item 挂起等人工动作 —— **不走** failed,因此不触发退款往返。

    漂移是可修复的:退了钱用户还得重新下单、重新走一遍,体验更差、对账也更脏。
    这里只挂起并写下 §13 合同(带 actions),让前端能渲染出「重新准备并审核」。

    同时把 submit_attempts 顶到上限,**彻底停掉自动重试** —— 沿用旧快照重试是
    保证失败的空转,还白走一次退款往返(Owner 点名的第 2 条)。
    """
    from services.publication_content_drift import ITEM_STATUS_AWAITING_ACTION, contract_to_columns

    code, user_message, contract_json = contract_to_columns(contract)
    own = cursor is None
    conn = _get_conn() if own else None
    c = conn.cursor() if own else cursor
    try:
        c.execute(
            """
            UPDATE mhz_publish_order_items
               SET status = %s,
                   reject_code = %s,
                   reject_user_message = %s,
                   reject_contract = %s::jsonb,
                   reject_reason = COALESCE(NULLIF(reject_reason, ''), %s),
                   submit_attempts = GREATEST(COALESCE(submit_attempts, 0), %s)
             WHERE id = %s
               AND status NOT IN ('published', 'success', 'cancelled', 'withdrawn')
         RETURNING order_id
            """,
            (ITEM_STATUS_AWAITING_ACTION, code, user_message, contract_json,
             f"内容漂移挂起 · {code}", DEFAULT_MAX_SUBMIT_ATTEMPTS, int(item_id)),
        )
        row = c.fetchone()
        if row and row.get("order_id"):
            recompute_order_status(int(row["order_id"]), cursor=c)
        if own:
            conn.commit()
        return bool(row)
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _terminal_cas_predicate(expect_status_in, expect_settlement):
    """把可选的**前置态限定**编成一段追加谓词。

    🔴 写成"追加",不是把原谓词改写成三元式:两个参数都不传时返回 `("", [])`,
       于是老调用方执行的 SQL **逐字节不变**。改写默认路径的写法本仓已经踩过 ——
       "默认参数=旧行为不变"必须落到字节,不是落到语义。
    """
    sql, params = "", []
    if expect_status_in is not None:
        sql += " AND status = ANY(%s)"
        params.append(list(expect_status_in))
    if expect_settlement is not None:
        sql += " AND settlement_status = %s"
        params.append(str(expect_settlement))
    return sql, params


def update_order_item_status(item_id: int, status: str, publish_url: str = None,
                             reject_reason: str = None, reject_contract: dict = None,
                             *, cursor=None, expect_status_in=None,
                             expect_settlement=None, published_at=None):
    """[P1 拒绝原因分层] reject_contract 给了就同时落 code / user_message / 合同三列。

    没给合同时(渠道回调等老调用方)按既有行为写 reject_reason,并补一个**兜底合同**:
    机器码 PUBLISH_REJECTED + 原因原文 + 一个"查看发布任务"的下一步 —— §13 铁律是
    红码必须有下一步,不能因为调用方没传就退化成一句没出口的话。

    🔴 [第 7 棒 · Codex R7 P0-B] 这个函数是"这一项发出去了"的**唯一权威落点**:
       它写终态三列**并且**重算订单头、抓发布快照/lineage、入交付与通知 outbox。
       任何重放型 writer(供应商回执回填)都必须走它,不能自己写一句 UPDATE ——
       自己写的那句只改三列,订单头会永远停在 `pending`、快照与 outbox 一个都没有。

       `cursor` 传进来就**并入调用方事务**(不 commit 不 close),这样
       「item 权威转移」与「post 投影」才是同一个事务里的一笔。
       `expect_status_in` / `expect_settlement` 是重放型调用需要的 **CAS 谓词**
       (本函数原本没有 CAS —— 重放会把任何状态的项推成 published)。

    """
    from services.publication_content_drift import contract_to_columns

    if status in ("rejected", "failed") and not reject_contract:
        reject_contract = {
            "code": "PUBLISH_REJECTED",
            "message": (reject_reason or "这条发布任务没有成功。"),
            "reason": (reject_reason or "渠道未接受本次提交。"),
            "impact": "该条发布未完成;若已扣费会按原路退回。",
            "repair_hint": "可在发布中心换一家媒体重新发布。",
            "actions": [
                {"id": "view_orders", "label": "查看发布任务", "type": "nav", "target": "/publish"},
            ],
            "rule_version": "publish-reject-v1",
        }

    own = cursor is None
    conn = _get_conn() if own else None
    try:
        c = conn.cursor() if own else cursor
        if reject_contract:
            _code, _user_message, _payload = contract_to_columns(reject_contract)
            c.execute(
                """UPDATE mhz_publish_order_items
                      SET reject_code = %s, reject_user_message = %s, reject_contract = %s::jsonb
                    WHERE id = %s""",
                (_code, _user_message, _payload, int(item_id)),
            )
        if status == "published":
            _cas_sql, _cas_params = _terminal_cas_predicate(expect_status_in,
                                                            expect_settlement)
            if published_at is None:
                c.execute("UPDATE mhz_publish_order_items SET status = %s, publish_url = %s, published_at = COALESCE(published_at,NOW()) WHERE id = %s"
                          + _cas_sql + " RETURNING user_id,order_id,published_at",
                          [status, publish_url, item_id] + _cas_params)
            else:
                # 供应商回执自带发布时刻时优先用它(回填是补记历史,不是"现在发的")。
                c.execute("UPDATE mhz_publish_order_items SET status = %s, publish_url = %s, published_at = COALESCE(published_at,%s,NOW()) WHERE id = %s"
                          + _cas_sql + " RETURNING user_id,order_id,published_at",
                          [status, publish_url, published_at, item_id] + _cas_params)
        elif status in ("rejected", "failed") and reject_reason is not None:
            # 🔴 [下单备注 P0 · 2026-08-10] 改前这里只有 `status == "rejected"`,
            #    `failed` 掉进最后那个 else,**调用方传了 reject_reason 也被静默丢掉**。
            #    生产实证(2026-08-10 只读取证):84 条 failed 里 **51 条 reject_reason 为空**,
            #    真因只躺在 mhz_raw_response 的 jsonb 里 —— 连内部排障都得手翻原始回包。
            #    典型:订单 479/480 的 item 531/532,供应商明说「备注字段内容不合法」,
            #    reject_reason 却是空的。
            #    ⚠️ 只在 reject_reason **显式传了值**时才写:其余状态(awaiting_sync 等)
            #    调用方不传原因,不能把已有的 reject_reason 抹成 NULL。
            c.execute("UPDATE mhz_publish_order_items SET status = %s, reject_reason = %s WHERE id = %s"
                      " RETURNING user_id,order_id",
                      (status, reject_reason, item_id))
        else:
            c.execute("UPDATE mhz_publish_order_items SET status = %s WHERE id = %s"
                      " RETURNING user_id,order_id", (status, item_id))
        changed = c.fetchone()
        # [P2] item 终态一变就重算订单状态 —— 不重算的话 status 还是那个恒 pending 的死字段
        if changed and changed.get("order_id"):
            try:
                recompute_order_status(int(changed["order_id"]), cursor=c)
            except Exception as _roll_err:
                logger.warning(f"[order-status] 订单 {changed.get('order_id')} 状态重算失败: {_roll_err}")
        if changed and status == "published":
            from services.article_publication_snapshot import (
                capture_mhz_publication_snapshot_with_cursor,
            )
            _publication_snapshot = capture_mhz_publication_snapshot_with_cursor(
                c, item_id=int(item_id), success_state=status,
            )
            if _publication_snapshot.get("article_id") and _publication_snapshot.get("content_hash"):
                from services.article_delivery_plan import enqueue_publication_locked_in_transaction_if_enabled

                enqueue_publication_locked_in_transaction_if_enabled(
                    c,
                    publication_source="mhz_publish_order_items",
                    publication_source_id=int(item_id),
                    article_id=int(_publication_snapshot["article_id"]),
                    platform="media_box",
                    provider_receipt=f"mhz-item:{int(item_id)}",
                    public_url=str(publish_url or ""),
                    submitted_content_hash=str(_publication_snapshot["content_hash"]),
                    published_at=changed.get("published_at"),
                )
        event_type = {
            "published": "publication.completed",
            "rejected": "publication.rejected",
            "withdrawn": "publication.withdrawn",
            "cancelled": "publication.withdrawn",
        }.get(status)
        if changed and event_type:
            _enqueue_publish_notification(
                c,
                user_id=int(changed["user_id"]),
                refund_key=f"item:{int(item_id)}",
                event_type=event_type,
                terminal_state=status,
                status={
                    "published": "已发布",
                    "rejected": "未通过",
                    "withdrawn": "已撤回",
                    "cancelled": "已取消",
                }[status],
            )
        if own:
            conn.commit()
        return {"changed": bool(changed),
                "order_id": (changed or {}).get("order_id"),
                "published_at": (changed or {}).get("published_at")}
    finally:
        if own:
            conn.close()


def get_pending_items() -> List[Dict]:
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT i.*, o.article_title FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON i.order_id = o.id
            WHERE i.status = 'submitted'
        """)
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def list_orders(user_id: int = None, page: int = 1, limit: int = 20) -> Dict:
    conn = _get_conn()
    try:
        c = conn.cursor()
        where = "TRUE"
        params: list = []
        if user_id:
            where = "o.user_id = %s"
            params = [user_id]

        c.execute(f"SELECT COUNT(*) AS total FROM mhz_publish_orders o WHERE {where}", params)
        total = c.fetchone()["total"]
        offset = (page - 1) * limit
        c.execute(f"""
            SELECT o.*, array_agg(json_build_object(
                'id', i.id, 'media_name', i.media_name, 'status', i.status,
                'publish_url', i.publish_url, 'reject_reason', i.reject_reason,
                'cost_points', i.cost_points
            )) AS items
            FROM mhz_publish_orders o
            LEFT JOIN mhz_publish_order_items i ON o.id = i.order_id
            WHERE {where}
            GROUP BY o.id
            ORDER BY o.created_at DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset])
        orders = [dict(r) for r in c.fetchall()]
        return {"orders": orders, "total": total, "page": page, "pages": (total + limit - 1) // limit}
    finally:
        conn.close()


def get_order_items_by_order(order_id: int) -> List[Dict]:
    """🔴 无归属过滤 —— 这是【系统内部】用的取数函数,调用方必须自己先确认归属。

    [FIND-MHZ-ORDERITEMS-2026-08-04] 之所以特意写这句:本函数有 11 个调用点,
    其中 10 个是内部流程(刚由本请求创建的订单 / 调度器,没有"当前用户"这个概念),
    第 11 个 `api/meijiehezi_api.py::api_order_items` 是把结果**原样返给用户**的端点 ——
    它当初没做归属判定,任何登录代理把 order_id 换成别人的就能读到别人的
    投放明细 + 稿件正文 + 我方成本(已被 Deploy 真调实证:113 读 admin 的 order 441 → 200)。

    修法是在【端点层】用 `get_publish_order_owner()` 先判归属,不是给本函数加
    `user_id` 参数 —— 加参数就必须给一个默认值,而默认值只能是"不过滤",
    那 10 个内部调用点会把这个 fail-open 的默认值一直带着走,
    下一个新调用点照样漏。**宁可让调用方显式判,也不要一个可以省略的安全参数。**
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM mhz_publish_order_items WHERE order_id = %s ORDER BY id", (order_id,))
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def get_publish_order_owner(order_id: int) -> Optional[int]:
    """返回该代发订单的归属 user_id;订单不存在返 None。

    [FIND-MHZ-ORDERITEMS-2026-08-04] 归属只认 `mhz_publish_orders.user_id`
    这一个权威源。`mhz_publish_order_items` 自己也有 user_id 列,但那是冗余副本,
    判归属要认订单主表(与 `api/meijiehezi_api.py:1131` / `:1256` 既有守卫同源)。
    """
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return None
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT user_id FROM mhz_publish_orders WHERE id = %s", (oid,))
        row = c.fetchone()
        if not row or row.get("user_id") is None:
            return None
        return int(row["user_id"])
    finally:
        conn.close()


def get_admin_stats() -> Dict:
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) AS total FROM mhz_media WHERE is_active = TRUE")
        media_count = c.fetchone()["total"]
        # 自媒体计数（表可能尚不存在）
        try:
            c.execute("SELECT COUNT(*) AS total FROM mhz_wemedia WHERE is_active = TRUE")
            wemedia_count = c.fetchone()["total"]
        except Exception:
            wemedia_count = 0
        c.execute("""
            SELECT
                COUNT(*) AS total_orders,
                COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                COUNT(*) FILTER (WHERE status = 'submitted') AS submitted,
                COUNT(*) FILTER (WHERE status = 'published') AS published,
                COUNT(*) FILTER (WHERE status = 'rejected') AS rejected
            FROM mhz_publish_order_items
        """)
        stats = dict(c.fetchone())
        c.execute("""
            SELECT COUNT(*) AS today_orders
            FROM mhz_publish_order_items
            WHERE created_at::date = (NOW() AT TIME ZONE 'Asia/Shanghai')::date
        """)
        stats["today_orders"] = c.fetchone()["today_orders"]
        stats["media_count"] = media_count
        stats["wemedia_count"] = wemedia_count
        # Session 状态
        session = get_config("phpsessid")
        stats["session_configured"] = bool(session)
        # 上次同步时间
        stats["last_media_sync"] = get_config("last_media_sync") or ""
        stats["last_wemedia_sync"] = get_config("last_wemedia_sync") or ""
        return stats
    finally:
        conn.close()


# ========================================
# 同步订单（从外部发布通道拉取的文章订单）
# ========================================

def sync_mhz_orders(fallback_user_id: int, orders: List[Dict], media_type_hint: str = None) -> Dict:
    """
    从外部发布通道同步订单到本地 mhz_synced_orders 表（管理员全局视图）。
    同时回写 mhz_publish_order_items 表的状态（用户各自的发布记录）。

    新订单通过 order_sn 匹配 mhz_publish_order_items 找到原始 user_id，
    匹配不到时使用 fallback_user_id（管理员直接在 MHZ 后台下的单）。

    [svideo lane · 2026-07-04] media_type_hint：短视频订单同步 job 必须传 'svideo'。
      短视频订单列表既无 wemedia_platform 也无 toutiao_id，若不传 hint 会被下方
      is_wemedia 推断成 'mhz' 错标，连锁破坏状态回流/去重/扣费取价。软文/自媒体
      job 不传（None）时保持原 is_wemedia 推断，零回归。
    """
    if not orders:
        return {"added": 0, "updated": 0, "items_updated": 0}

    # 🔴 [#151-B] 段数>1 的 item 永远匹配不上本函数的 `WHERE mhz_order_id = %s` ——
    #    它们存的是**整批回执**而不是自己那一个单号。写入侧已加闸(不再产生新的),
    #    这里把**存量**喊出来:修完历史数据后这个数应当是 0,
    #    再次非 0 就说明有人绕过了写入侧那道闸。
    try:
        _c = _get_conn()
        try:
            _cur = _c.cursor()
            _cur.execute(
                "SELECT count(*) AS n FROM mhz_publish_order_items"
                " WHERE mhz_order_id ~ '[,;|[:space:]]'")
            _bad = int(dict(_cur.fetchone() or {}).get("n") or 0)
        finally:
            _c.close()
        if _bad:
            logger.warning(
                "[#151-B] 有 %s 条 item 的 mhz_order_id 存了**整批**单号,"
                "本次同步无论如何都匹配不上它们。修完存量后此数应为 0;"
                "再次非 0 = 有人绕过了 update_order_item_submitted 那道闸。", _bad)
    except Exception as _seg_err:  # noqa: BLE001 观测面失败不该拖垮同步
        logger.warning("[#151-B] 段数普查失败(不影响同步): %s", str(_seg_err)[:160])
    conn = _get_conn()
    try:
        c = conn.cursor()
        added = 0
        updated = 0
        items_updated = 0
        for o in orders:
            order_id = str(o.get("id", "") or o.get("ordernum", ""))
            if not order_id:
                continue
            order_sn = str(o.get("ordernum", ""))
            mhz_status = o.get("status", 0)
            # 自媒体字段兼容：toutiao_name → media_name, toutiao_id → resource_id
            is_wemedia = "wemedia_platform" in o or "toutiao_id" in o
            media_name = o.get("media_name") or o.get("toutiao_name", "")
            # [T1 · 2026-07-30 真环境采样订正] 短视频订单列表**没有 resource_id 也没有 toutiao_id**，
            #   资源 ID 字段真名是 media_id → 老表达式对 svideo 恒落 0。
            #   这里才是生产库 mhz_synced_orders.resource_id = 0 的真产地：
            #   落 0 之后 find_synced_order_by_signature 的 `AND resource_id = %s`
            #   永不命中 → awaiting_sync 的降级反查也一起断（与 scheduler 那处是同一个键名错的两个落点，
            #   只修一处仍然回流不了）。
            #   ⚠️ 严格按 media_type_hint 分支：软文/自媒体(hint=None)走原表达式，逐字未动，零回归。
            if media_type_hint == MEDIA_TYPE_SVIDEO:
                resource_id = o.get("media_id") or 0
                if not resource_id:
                    # [P2-A · 2026-07-30 Review 建议] 远端一旦改字段名,这里落 0,
                    #   而下方 UPDATE 自愈条件 `%s <> 0` 对 0 为假 → 本行 resource_id
                    #   保持原值(selfheal=skipped),回流按 resource_id 匹配也就一直对不上。
                    #   fail-safe 方向对(不写坏值),但必须留可 grep 的痕。
                    #   remote_keys 打的是远端**本次实际返回的键名全集**:下次改名一眼
                    #   看出新名,不用再抓包。
                    #   ⚠️ 措辞刻意只用方括号标签 + 数据,不写否定句 ——
                    #   运维 grep 标签时会把含否定词的句子一起命中。
                    logger.warning(
                        "[SvideoMediaIdMissing] order=%s order_sn=%s "
                        "selfheal=skipped remote_keys=%s",
                        order_id, order_sn, sorted(str(k) for k in o.keys()),
                    )
            else:
                resource_id = o.get("resource_id") or o.get("toutiao_id", 0)
            # media_type_hint 显式优先（短视频 job 传 'svideo'）；否则回退老的软文/自媒体推断
            media_type = media_type_hint or ("wemedia" if is_wemedia else "mhz")
            # 自媒体显示：账号名 + 平台
            if is_wemedia and o.get("wemedia_platform"):
                media_name = f"{media_name}（{o['wemedia_platform']}）"

            # [CTO-15.23 2026-05-13 全量隔离修] 同步前先解 owner 三件套(user_id/brand_id/article_id)
            # 老板报"一文多投只看到拒的不看到成功"真因:
            #   首次 INSERT 时 items 还没回填 mhz_order_id → JOIN 失败 → 三字段 NULL
            #   后续 UPDATE 分支不补 → 永远 NULL
            #   SSH prod 实证:73% article_id / 55% brand_id NULL · 用户看不到关联文章
            # 修法:每次 sync 都解一次 · INSERT/UPDATE 都用 COALESCE 兜底
            resolved_user_id = None
            resolved_brand_id = None
            resolved_article_id = None
            if order_sn:
                c.execute("""
                    SELECT i.user_id, i.brand_id, o.article_id
                    FROM mhz_publish_order_items i
                    JOIN mhz_publish_orders o ON o.id = i.order_id
                    WHERE i.mhz_order_id = %s LIMIT 1
                """, (order_sn,))
                row = c.fetchone()
                if row:
                    resolved_user_id = row["user_id"]
                    resolved_brand_id = row.get("brand_id")
                    resolved_article_id = row.get("article_id")
            _cw_owner = None   # 开源版的同步单都没有保留主人:下面三处用法一律走「没有主人」那条路

            # 1. 更新 mhz_synced_orders（管理员全局视图）
            c.execute("SELECT id, user_id, brand_id, article_id, status FROM mhz_synced_orders WHERE id = %s", (order_id,))
            exists = c.fetchone()
            if exists:
                # 完结且状态没变 + 三关键字段都已填 → skip
                # 三字段任一 NULL 必须走 UPDATE 分支 COALESCE 补回(修历史脏数据 + 防新订单 race 漏)
                terminal_status = mhz_status in (2, -1, -2, 3)
                fields_all_filled = (
                    exists["user_id"] is not None
                    and exists["brand_id"] is not None
                    and exists["article_id"] is not None
                )
                if (exists["status"] == mhz_status and terminal_status and fields_all_filled
                        and (_cw_owner is None or exists["user_id"] == _cw_owner)):
                    continue
                # UPDATE 分支用 COALESCE 不覆盖已有值 · 仅补 NULL → 安全幂等
                # [GEO-R9-CAN-005] 未匹配订单归属回收：首次 INSERT 时 items 尚未回填
                #   mhz_order_id → 只能落 fallback_user_id(管理员手工下单兜底)。后续
                #   sync 解出真实 user_id 后，旧的 COALESCE(user_id, ...) 会保留非空的
                #   admin fallback → 真实客户永远在 list_mhz_synced_orders(user_id) 里
                #   看不到自己的订单(状态/URL/拒稿原因)。
                # 修法：当解出真实用户(resolved_user_id)且现有 owner 为 NULL 或正是 admin
                #   fallback 时，事务内改写为真实用户;若已是其它真实用户则保持不动(防误覆盖)。
                #   brand_id/article_id 首插即 NULL，仍用 COALESCE 补回即可。
                #   ⚠️ 退款不受影响:退款按 mhz_order_id / item 维度结算，不读 synced_orders.user_id。
                _reassign_owner = (
                    resolved_user_id is not None
                    and exists["user_id"] is not None
                    and exists["user_id"] == fallback_user_id
                    and resolved_user_id != fallback_user_id
                ) or (_cw_owner is not None and exists["user_id"] != _cw_owner)
                # [T1 · 2026-07-30] resource_id 自愈：老 UPDATE 分支**完全不碰 resource_id**，
                #   于是键名错期间落进去的存量脏行(生产那笔 07-27 采样单 resource_id=0)
                #   即使改对了映射也**永远修不回来** —— 首次 INSERT 之后只走 UPDATE，
                #   而 find_synced_order_by_signature 按 resource_id 匹配，那笔单的回流一直是断的。
                #   修法与 brand_id/article_id 同形态：只补 NULL/0，已有非零值一律不覆盖(不误改)。
                c.execute("""
                    UPDATE mhz_synced_orders SET
                        status = %s, url = %s, reason = %s, updated_at = %s, published_at = %s, synced_at = NOW(),
                        user_id = CASE
                            WHEN %s IS NOT NULL AND (user_id IS NULL OR user_id = %s OR %s)
                            THEN %s ELSE user_id END,
                        brand_id   = COALESCE(brand_id, %s),
                        article_id = COALESCE(article_id, %s),
                        resource_id = CASE
                            WHEN (resource_id IS NULL OR resource_id = 0) AND %s IS NOT NULL AND %s <> 0
                            THEN %s ELSE resource_id END
                    WHERE id = %s
                """, (
                    mhz_status, o.get("url", ""), o.get("reason", ""),
                    o.get("updated_at"), o.get("published_at"),
                    resolved_user_id, fallback_user_id, _cw_owner is not None, resolved_user_id,
                    resolved_brand_id, resolved_article_id,
                    resource_id, resource_id, resource_id,
                    order_id
                ))
                if _reassign_owner:
                    # [GEO-R9-CAN-005] 归属回收审计:admin fallback → 真实客户
                    logger.warning(
                        "[GEO-R9-CAN-005] 同步订单归属回收 order=%s: admin_fallback=%s → real_user=%s",
                        order_id, fallback_user_id, resolved_user_id,
                    )
                updated += 1
            else:
                # INSERT 分支:items 没解出来 → 用 fallback_user_id(管理员手工下单)
                real_user_id = resolved_user_id if resolved_user_id is not None else fallback_user_id
                c.execute("""
                    INSERT INTO mhz_synced_orders (id, order_sn, title, media_name, resource_id, price, status, url, reason,
                        created_at, updated_at, published_at, order_remark, customer_name, file_url, subtitle, user_id, brand_id, article_id, media_type, synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                """, (
                    order_id, order_sn, o.get("title", ""), media_name,
                    resource_id, o.get("price", 0), mhz_status, o.get("url", ""),
                    o.get("reason", ""), o.get("created_at"), o.get("updated_at"), o.get("published_at"),
                    o.get("order_remark", "") or o.get("article_remark", ""), o.get("customer_name", ""),
                    o.get("file_url", ""), o.get("subtitle", ""), real_user_id, resolved_brand_id, resolved_article_id,
                    media_type
                ))
                added += 1

            # 2. 回写 mhz_publish_order_items（用户各自的发布记录）
            if order_sn:
                # 将 MHZ 数字状态映射为 order_items 的文本状态
                item_status = None
                if mhz_status == 2:
                    item_status = "published"
                elif mhz_status == -1:
                    item_status = "rejected"
                elif mhz_status == -2:
                    item_status = "withdrawn"

                if item_status:
                    # [P1-E + 第四轮 race 修] 拒稿/撤稿按 item 维度退款
                    # 用 UPDATE ... RETURNING 一次拿到所有变更的 item，避免：
                    #   旧实现 SELECT 后 UPDATE → 退款循环中崩溃 → 部分 item 永远不会再被退款
                    #   （因为下次 sync 时 SELECT 用相同 NOT IN 条件，已变 rejected 的 item 不再返回）
                    # 用 item_id 作幂等键防止重复跑导致双退
                    needs_refund = item_status in ("rejected", "withdrawn")

                    # [2026-04-30] 终态白名单完整版：published/rejected/withdrawn/failed/cancelled
                    # 之前老白名单缺 failed/cancelled 两项，会导致：
                    # - 管理员人工标 failed 的 item 被 sync 任务改回 published（前后不一致 + 多发通知）
                    # - 用户主动取消（cancelled）的 item 被 sync 改成其他状态（重复退款）
                    # 这次补全
                    if needs_refund:
                        # [2026-04-30] 多带 media_name 字段出来供拒稿通知用
                        c.execute("""
                            UPDATE mhz_publish_order_items SET
                                status = %s, publish_url = %s, reject_reason = %s, published_at = NOW(),
                                -- [2026-08-02] media_name 自愈：只补空值，不覆盖已有名字。
                                -- 老实现完全不碰这一列，一旦下单时落成空串就永远是空的
                                -- （实测 order 409 的 item 空名）。远端同步回来的记录本就带名字。
                                media_name = COALESCE(NULLIF(media_name, ''), %s)
                            WHERE mhz_order_id = %s
                              AND status NOT IN ('published', 'rejected', 'withdrawn', 'failed', 'cancelled')
                            RETURNING id, user_id, cost_points, media_name,
                                      (SELECT article_title FROM mhz_publish_orders WHERE id = mhz_publish_order_items.order_id) AS article_title
                        """, (item_status, o.get("url", ""), o.get("reason", ""), media_name, order_sn))
                        items_to_refund = list(c.fetchall())
                        if items_to_refund:
                            items_updated += len(items_to_refund)
                    else:
                        # published 路径：仅 UPDATE 不退款，但发"已发布"通知给用户
                        c.execute("""
                            UPDATE mhz_publish_order_items SET
                                status = %s, publish_url = %s, reject_reason = %s, published_at = NOW(),
                                -- [2026-08-02] media_name 自愈，同上：只补空值不覆盖
                                media_name = COALESCE(NULLIF(media_name, ''), %s)
                            WHERE mhz_order_id = %s
                              AND status NOT IN ('published', 'rejected', 'withdrawn', 'failed', 'cancelled')
                            RETURNING id, user_id, media_name,
                                      (SELECT article_title FROM mhz_publish_orders WHERE id = mhz_publish_order_items.order_id) AS article_title
                        """, (item_status, o.get("url", ""), o.get("reason", ""), media_name, order_sn))
                        items_published = list(c.fetchall())
                        if items_published:
                            items_updated += len(items_published)
                            from services.article_publication_snapshot import (
                                capture_mhz_publication_snapshot_with_cursor,
                            )
                            for _published_item in items_published:
                                capture_mhz_publication_snapshot_with_cursor(
                                    c,
                                    item_id=int(_published_item["id"]),
                                    success_state="published",
                                )
                        items_to_refund = []

                    # 拒稿/撤稿后按 item 退款 + 通知用户
                    if items_to_refund:
                        for it in items_to_refund:
                            _enqueue_publish_notification(
                                c,
                                user_id=int(it["user_id"]),
                                refund_key=f"item:{int(it['id'])}",
                                event_type=(
                                    "publication.rejected"
                                    if item_status == "rejected"
                                    else "publication.withdrawn"
                                ),
                                terminal_state=item_status,
                                status=("未通过" if item_status == "rejected" else "已撤回"),
                            )
                        # 先 commit 状态变更，避免长事务持锁；refund helper 用独立 connection
                        conn.commit()
                        for it in items_to_refund:
                            item_cost = int(it.get("cost_points") or 0)
                            if item_cost <= 0:
                                continue
                            refund_result = refund_for_publish_order(
                                user_id=int(it["user_id"]),
                                amount=item_cost,
                                refund_key=f"item:{it['id']}",
                                reason=f"mhz {item_status}: {o.get('reason', '')[:80]}",
                            )

                    # 已发布也通知用户
                    if not needs_refund and 'items_published' in locals() and items_published:
                        for it in items_published:
                            _enqueue_publish_notification(
                                c,
                                user_id=int(it["user_id"]),
                                refund_key=f"item:{int(it['id'])}",
                                event_type="publication.completed",
                                terminal_state="published",
                                status="已发布",
                            )

                        # CTO-15.23 2026-05-25 · 老板报漏斗 publish 0 · 补 mhz 自动同步发布主路径埋点
                        # · prod 真实发布走 mhz 代发(此处)· 不走 publication_facts.record_manual_publication
                        # · publication_facts 加的埋点(commit 18fa22df)只覆盖手动确认 · 0 调用
                        # · 反查 brand_id 链:item → mhz_publish_orders.article_id → articles.quote_id → quotes.brand_id
                        # · funnel 按 (brand_id, stage_name) 去重 · 一笔订单多 item 同 brand 仍算 1 次
                        for it in items_published:
                            try:
                                c.execute("""
                                    SELECT q.brand_id
                                    FROM mhz_publish_order_items i
                                    JOIN mhz_publish_orders mo ON mo.id = i.order_id
                                    LEFT JOIN articles a ON a.id = mo.article_id
                                    LEFT JOIN quotes q ON q.id = a.quote_id
                                    WHERE i.id = %s
                                    LIMIT 1
                                """, (it["id"],))
                                _brand_row = c.fetchone()
                                _brand_id_for_log = _brand_row.get("brand_id") if _brand_row else None
                                if _brand_id_for_log:
                                    from db.pipeline_stage_log_db import log_stage_event
                                    log_stage_event(
                                        brand_id=_brand_id_for_log,
                                        stage_name="publish",
                                        event="complete",
                                        meta={
                                            "source": "mhz_auto_sync",
                                            "item_id": it.get("id"),
                                            "media_name": it.get("media_name"),
                                            "mhz_order_sn": order_sn,
                                        },
                                        actor_user_id=it.get("user_id"),
                                    )
                            except Exception as _se:
                                logger.warning(f"[mhz publish stage_log] 失败(非阻塞) item={it.get('id')}: {_se}")

            if (added + updated) % 100 == 0:
                conn.commit()
        conn.commit()
        return {"added": added, "updated": updated, "items_updated": items_updated}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def count_user_orders(user_id: int) -> int:
    """统计指定用户的同步订单数"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM mhz_synced_orders WHERE user_id = %s", (user_id,))
        return c.fetchone()["cnt"]
    finally:
        conn.close()


def backfill_orphan_fields() -> Dict:
    """[CTO-15.23 2026-05-13] 一次性数据救援 · 给历史 mhz_synced_orders 回填三关键字段

    背景:同步流程历史 BUG 导致 73% article_id / 55% brand_id NULL · 用户看不到关联文章
    救援路径:通过 order_sn → mhz_publish_order_items.mhz_order_id JOIN mhz_publish_orders
    安全:COALESCE 不覆盖已填值 · 幂等可多次跑
    返回:实际补回的字段数(按维度统计)
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        # 先统计待救援的量
        c.execute("""
            SELECT
                COUNT(*) FILTER (WHERE s.user_id IS NULL) AS need_user,
                COUNT(*) FILTER (WHERE s.brand_id IS NULL) AS need_brand,
                COUNT(*) FILTER (WHERE s.article_id IS NULL) AS need_article,
                COUNT(*) AS total
            FROM mhz_synced_orders s
        """)
        before = dict(c.fetchone())

        # 主救援 SQL · COALESCE 保护已有值 · LIMIT 1 防 items 重复 join
        c.execute("""
            UPDATE mhz_synced_orders s SET
                user_id    = COALESCE(s.user_id, sub.user_id),
                brand_id   = COALESCE(s.brand_id, sub.brand_id),
                article_id = COALESCE(s.article_id, sub.article_id)
            FROM (
                SELECT DISTINCT ON (i.mhz_order_id)
                    i.mhz_order_id, i.user_id, i.brand_id, po.article_id
                FROM mhz_publish_order_items i
                JOIN mhz_publish_orders po ON po.id = i.order_id
                WHERE i.mhz_order_id IS NOT NULL
                ORDER BY i.mhz_order_id, i.created_at ASC
            ) sub
            WHERE s.id = sub.mhz_order_id
              AND (s.user_id IS NULL OR s.brand_id IS NULL OR s.article_id IS NULL)
        """)
        affected = c.rowcount

        # 再统计救援后剩余 NULL
        c.execute("""
            SELECT
                COUNT(*) FILTER (WHERE user_id IS NULL) AS still_null_user,
                COUNT(*) FILTER (WHERE brand_id IS NULL) AS still_null_brand,
                COUNT(*) FILTER (WHERE article_id IS NULL) AS still_null_article
            FROM mhz_synced_orders
            WHERE COALESCE(user_id, 0) >= 0   -- [WO_301b ②c] 同上
        """)
        after = dict(c.fetchone())
        conn.commit()
        return {
            "rows_affected": affected,
            "before": before,
            "after": after,
            "recovered_user": before["need_user"] - after["still_null_user"],
            "recovered_brand": before["need_brand"] - after["still_null_brand"],
            "recovered_article": before["need_article"] - after["still_null_article"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_synced_orders_grouped_by_article(user_id: Optional[int] = None,
                                          brand_id: Optional[int] = None,
                                          limit: int = 50) -> List[Dict]:
    """[CTO-15.23 2026-05-13] 一文多投聚合视图 · 老板"一篇帖子发两平台一拒一成功"场景

    返回每个 article_id 关联的所有平台订单 · 每篇文章一组
    管理员 user_id=None 看全部;普通用户传 user_id 隔离

    返回结构:
    [
      {
        article_id, article_title, brand_id, brand_name,
        total: N, completed: N, rejected: N, withdrawn: N, in_progress: N, refunded: N,
        latest_at: timestamp,
        orders: [{order_sn, media_name, status, reason, url, created_at, published_at}, ...]
      }, ...
    ]
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        where = ["o.article_id IS NOT NULL"]
        params: list = []
        if user_id is not None:
            where.append("o.user_id = %s")
            params.append(user_id)
        if brand_id is not None:
            where.append("o.brand_id = %s")
            params.append(brand_id)
        where_sql = " AND ".join(where)

        # 先按 article_id 分组拿汇总
        c.execute(f"""
            SELECT
                o.article_id,
                MAX(o.title) AS article_title,
                MAX(o.brand_id) AS brand_id,
                MAX(b.name) AS brand_name,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE o.status = 2) AS completed,
                COUNT(*) FILTER (WHERE o.status = -1) AS rejected,
                COUNT(*) FILTER (WHERE o.status = -2) AS withdrawn,
                COUNT(*) FILTER (WHERE o.status IN (0, 1)) AS in_progress,
                COUNT(*) FILTER (WHERE o.status = 3) AS refunded,
                MAX(o.updated_at) AS latest_at
            FROM mhz_synced_orders o
            LEFT JOIN brands b ON b.id = o.brand_id
            WHERE {where_sql}
            GROUP BY o.article_id
            ORDER BY MAX(o.created_at) DESC
            LIMIT %s
        """, params + [limit])
        groups = [dict(r) for r in c.fetchall()]
        if not groups:
            return []

        article_ids = [g["article_id"] for g in groups]
        # 拿每个 article 的所有 orders 明细
        c.execute(f"""
            SELECT
                o.article_id, o.id AS order_sn, o.media_name, o.media_type, o.status,
                o.reason, o.url, o.price, o.created_at, o.published_at, o.user_id
            FROM mhz_synced_orders o
            WHERE o.article_id = ANY(%s) AND {where_sql.replace('o.article_id IS NOT NULL', '1=1')}
            ORDER BY o.article_id, o.created_at DESC
        """, [article_ids] + params)
        orders_by_art: Dict[int, List[Dict]] = {}
        for r in c.fetchall():
            d = dict(r)
            for k in ("created_at", "published_at"):
                if d.get(k) and hasattr(d[k], "isoformat"):
                    d[k] = d[k].isoformat()
            orders_by_art.setdefault(d["article_id"], []).append(d)

        for g in groups:
            if g.get("latest_at") and hasattr(g["latest_at"], "isoformat"):
                g["latest_at"] = g["latest_at"].isoformat()
            g["orders"] = orders_by_art.get(g["article_id"], [])
        return groups
    finally:
        conn.close()


def list_mhz_synced_orders(user_id: int = None, page: int = 1, limit: int = 20,
                           status: str = "", brand_id: int = None, search: str = "",
                           media_type: str = "") -> Dict:
    """查询已同步的外部发布通道订单，JOIN users 带出提交者用户名"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        where = ["TRUE"]
        params: list = []
        if user_id:
            where.append("o.user_id = %s")
            params.append(user_id)
        if status:
            where.append("o.status = %s")
            params.append(int(status))
        if brand_id:
            where.append("o.brand_id = %s")
            params.append(brand_id)
        if media_type:
            where.append("o.media_type = %s")
            params.append(media_type)
        if search:
            where.append("(o.title ILIKE %s OR o.order_sn ILIKE %s OR u.display_name ILIKE %s OR u.username ILIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like, like])
        where_sql = " AND ".join(where)

        count_sql = f"""
            SELECT COUNT(*) AS total FROM mhz_synced_orders o
            LEFT JOIN users u ON o.user_id = u.id
            WHERE {where_sql}
        """
        c.execute(count_sql, params)
        total = c.fetchone()["total"]
        offset = (page - 1) * limit
        # [CTO-15.23 2026-05-13 全量隔离修] 带出 article_title(JOIN mhz_publish_orders)
        # 让用户能看到"这单是哪篇文章的"· 老板报"成功那篇没出现在已分发内"真相之一
        c.execute(f"""
            SELECT o.*, u.display_name AS user_display_name, u.username AS user_login,
                   po.article_title AS article_title
            FROM mhz_synced_orders o
            LEFT JOIN users u ON o.user_id = u.id
            LEFT JOIN mhz_publish_orders po ON po.id = (
                SELECT i.order_id FROM mhz_publish_order_items i
                WHERE i.mhz_order_id = o.id LIMIT 1
            )
            WHERE {where_sql} ORDER BY o.created_at DESC LIMIT %s OFFSET %s
        """, params + [limit, offset])
        orders = []
        for r in c.fetchall():
            d = dict(r)
            for k in ("created_at", "updated_at", "published_at", "synced_at"):
                if d.get(k) and hasattr(d[k], "isoformat"):
                    d[k] = d[k].isoformat()
            orders.append(d)
        return {"orders": orders, "total": total, "page": page, "pages": (total + limit - 1) // limit}
    finally:
        conn.close()


def get_mhz_synced_order_stats(user_id: int = None) -> Dict[str, int]:
    """Return the complete status breakdown for one user's synced orders.

    Keep this aggregate independent from list filters so one page request can
    render every counter without issuing seven additional HTTP requests.
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        where_sql = "WHERE user_id = %s" if user_id is not None else ""
        params = [user_id] if user_id is not None else []
        c.execute(f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 2) AS completed,
                COUNT(*) FILTER (WHERE status = 1) AS in_progress,
                COUNT(*) FILTER (WHERE status = 0) AS pending,
                COUNT(*) FILTER (WHERE status = -1) AS rejected,
                COUNT(*) FILTER (WHERE status = -2) AS withdrawn,
                COUNT(*) FILTER (WHERE status = 3) AS refunded
            FROM mhz_synced_orders
            {where_sql}
        """, params)
        row = c.fetchone() or {}
        return {
            key: int(row.get(key) or 0)
            for key in (
                "total", "completed", "in_progress", "pending",
                "rejected", "withdrawn", "refunded",
            )
        }
    finally:
        conn.close()


def update_synced_order_status(order_id: str, status: int):
    """更新同步订单的状态"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE mhz_synced_orders SET status = %s, synced_at = NOW() WHERE id = %s", (status, order_id))
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# [2026-04-30] 订单管理后台衔接补丁
# ============================================================================
# 问题：订单管理界面读 mhz_synced_orders（被定时任务从 mhz 拉过来的），
#       看不到 mhz_publish_order_items 里"还没同步""awaiting_sync""人工审核"
#       这些本地特有状态的订单。
# 补丁：给管理员加 2 个独立 endpoint 看本地待处理项 + 人工审核队列。

# 本地特有"在途"状态（mhz_synced_orders 里没有对应记录的）
_LOCAL_INFLIGHT_STATUSES = (
    'pending', 'submitting', 'submitted',
    'awaiting_confirmation', 'awaiting_sync',
)

_LOCAL_CUSTOMER_HISTORY_STATUSES = _LOCAL_INFLIGHT_STATUSES + (
    'published', 'failed', 'rejected', 'withdrawn', 'cancelled',
    'paused_admin_dedupe',
)

# ── [WO_PUBLISH_DISPATCH Part① 2026-08-17] item 状态 → 镜像 status 码 ──────────
#
# `/article-publish-stats` 的分母原来只有两源(`mhz_synced_orders` 镜像 + `publish_records`
# 自助)。**快易播(kyb)那条渠道从来不写镜像表** —— 它的状态回流是
# `services/kuaiyibo/status_sync.py` 直接 UPDATE `mhz_publish_order_items.status`。
# 于是 2026-08-17 生产实测:11 条已 published 的 item(单号 `26…` 族 · 媒体「列举网(可
# 指定地区)」)在镜像表 **0 命中**,发布中心 tab 恒显示「未分发」。
# 这张表把 item 的本地状态翻译成镜像那套 status 码,让第三源能与前两源同口径 UNION。
#
# 🔴 **闭集**:生产实测 item.status 只有 7 个取值(见 `ITEM_STATUS_ALL`),这里必须
#    逐个有条目。`None` = **不计入统计**(cancelled 是工单明确要求不计的)。
#    新增状态忘了登记 → `tests/publish_stats_third_source_2026_08_17` 的枚举锁当场红,
#    而不是静默从统计里消失。
ITEM_STATUS_TO_SYNCED_CODE: dict = {
    'published': 2,             # 已发布
    'pending': 0,               # 待接单(还没投出去)
    'submitting': 1,
    'submitted': 1,
    'awaiting_confirmation': 1,  # 等用户确认补充字段,单子还在
    'awaiting_sync': 1,          # 投出去了没拿到单号,等反查回填
    'awaiting_action': 1,        # 等人工动作,仍在管道里
    'paused_admin_dedupe': 1,    # 管理员暂停去重,单子没结束
    'rejected': -1,
    'failed': -1,
    'withdrawn': -2,
    'cancelled': None,           # 工单明确:不计
}

#: item.status 的闭集 = 代码里出现过的全部取值 ∪ 生产实测取值。枚举锁的分母。
ITEM_STATUS_ALL: tuple = tuple(sorted(
    set(_LOCAL_CUSTOMER_HISTORY_STATUSES) | {'awaiting_action'}
))


def item_countable_statuses() -> list:
    """会计入统计的 item 状态(值为 None 的不算)。"""
    return [k for k, v in ITEM_STATUS_TO_SYNCED_CODE.items() if v is not None]


def item_status_case_sql(col: str = "i.status") -> str:
    """由 `ITEM_STATUS_TO_SYNCED_CODE` **机械生成** CASE 片段。

    手抄一份 CASE 到 SQL 里,映射表改了 SQL 不改就是两套真相 —— 所以这里生成,
    不手写。键全是本模块自己的字面量,不来自外部输入。
    """
    parts = " ".join(
        f"WHEN '{k}' THEN {v}"
        for k, v in ITEM_STATUS_TO_SYNCED_CODE.items() if v is not None
    )
    return f"CASE {col} {parts} ELSE NULL END"


def list_user_publish_history(
    user_id: int,
    page: int = 1,
    limit: int = 20,
    source: str = "proxy",
    view: str = "order",
    status_filter: str = "",
    brand_id: Optional[int] = None,
    media_type: str = "",
    search: str = "",
    markup_ratio: str = "1.5",
    is_admin: bool = False,
) -> Dict[str, Any]:
    """Return the current user's publish history as one stable DB projection.

    ``is_admin`` used to gate **which actions a row advertises** (R3 §③): the
    「补人工证据」action was administrator-only.  [WO_273] Both self-report
    actions (re-verify / attest) were retired with the browser-extension
    backend, so the flag no longer changes the output; it is kept only so
    callers need not change.  It never widened row scope — this projection
    stays user-scoped for administrators too.

    The projection deliberately merges three durable sources inside PostgreSQL:
    synced proxy orders, local in-flight proxy items, and self-publish records.
    Local items disappear from the local arm once the corresponding synced row
    exists, while the synced arm keeps the local ``publish:proxy:<id>`` key.  That
    makes scheduler reconciliation an in-place update from the client's point
    of view instead of a remove/add flash.

    This is a customer-facing projection.  It never returns provider prices,
    raw provider payloads, internal table names, or internal status codes.
    Pagination, article grouping, filters and statistics all use the same SQL
    snapshot so concurrent scheduler sync cannot create split-query flicker.
    """
    if source not in {"all", "proxy", "self"}:
        raise ValueError("invalid source")
    if view not in {"order", "article"}:
        raise ValueError("invalid view")
    # 🔴 R2 新增的 `reported_unverified` 必须在这个白名单里 —— 前端筛选器给了这个值,
    #    而 R2 只加了前端那一半,后端这里会 ValueError → 「待核实」筛选一点就报错。
    #    (加一档状态要同步 4 处:SQL CASE / 这个白名单 / API 的 Literal / 前端。)
    if status_filter not in {"", "pending", "in_progress", "completed",
                             "reported_unverified", "rejected", "withdrawn", "refunded"}:
        raise ValueError("invalid status filter")

    page = max(1, int(page))
    limit = max(1, min(int(limit), 50))
    offset = (page - 1) * limit
    safe_search = (search or "").strip()
    safe_media_type = (media_type or "").strip()

    source_where = "TRUE" if source == "all" else "source = %s"
    source_params: List[Any] = [] if source == "all" else [source]
    base_where = [source_where]
    base_params: List[Any] = list(source_params)
    if brand_id is not None:
        base_where.append("brand_id = %s")
        base_params.append(int(brand_id))
    if safe_media_type:
        base_where.append("media_type = %s")
        base_params.append(safe_media_type)
    if safe_search:
        base_where.append("(article_title ILIKE %s OR channel_name ILIKE %s)")
        like = f"%{safe_search}%"
        base_params.extend([like, like])
    base_where_sql = " AND ".join(base_where)

    visible_where = ["TRUE"]
    visible_params: List[Any] = []
    if status_filter:
        visible_where.append("status_family = %s")
        visible_params.append(status_filter)
    visible_where_sql = " AND ".join(visible_where)

    # [WO_273 · 2026-09-23] 原来这里按身份分叉两件事:管理员行上多一个「补人工证据」动作,
    #   提示文案也按「这个人点得动什么」分两句。两个动作(重新核实 / 补人工证据)的后端端点只存在于
    #   浏览器插件后端,随它整体删除;前端按钮由 A 同单去掉。于是动作不再下发,提示只说状态本身、
    #   不再指向任何点不动的动作 —— 身份不再影响输出(`is_admin` 参数保留,调用方不用改)。
    attest_hint = "'这条只有浏览器回报，服务端还没核实过'"

    common_ctes = f"""
        WITH synced_rows AS (
            SELECT
                CASE WHEN li.id IS NOT NULL
                     THEN 'publish:proxy:' || li.id::text
                     ELSE 'publish:external:' || s.id END AS record_key,
                'proxy'::text AS source,
                s.id::text AS action_id,
                s.order_sn,
                COALESCE(s.article_id, li.article_id) AS article_id,
                COALESCE(NULLIF(s.title, ''), li.article_title, '') AS article_title,
                COALESCE(s.brand_id, li.brand_id) AS brand_id,
                b.name AS brand_name,
                COALESCE(NULLIF(s.media_name, ''), li.media_name, '') AS channel_name,
                COALESCE(NULLIF(s.media_type, ''), NULLIF(li.media_type, ''), 'mhz') AS media_type,
                CASE s.status
                    WHEN 0 THEN 'processing'
                    WHEN 1 THEN 'processing'
                    WHEN 2 THEN 'completed'
                    WHEN -1 THEN 'rejected'
                    WHEN -2 THEN 'withdrawn'
                    WHEN 3 THEN 'refunded'
                    ELSE 'processing'
                END AS status_key,
                CASE s.status
                    WHEN 0 THEN '待平台接单'
                    WHEN 1 THEN '处理中'
                    WHEN 2 THEN '已完成'
                    WHEN -1 THEN '未通过'
                    WHEN -2 THEN '已撤回'
                    WHEN 3 THEN '已退款'
                    ELSE '处理中'
                END AS status_label,
                CASE s.status
                    WHEN 0 THEN 'in_progress'
                    WHEN 1 THEN 'in_progress'
                    WHEN 2 THEN 'completed'
                    WHEN -1 THEN 'rejected'
                    WHEN -2 THEN 'withdrawn'
                    WHEN 3 THEN 'refunded'
                    ELSE 'in_progress'
                END AS status_family,
                COALESCE(
                    li.cost_points::bigint,
                    CEIL(COALESCE(s.price, 0) * 130 * %s::numeric)::bigint,
                    0
                ) AS points,
                -- [R2 §②] 代发是**供应商回执** lane:完成即已核实发布。
                CASE WHEN s.status = 2 THEN 'verified_published' ELSE 'not_published' END
                    AS publication_axis,
                NULLIF(s.url, '') AS public_url,
                CASE WHEN s.status = -1 THEN NULLIF(s.reason, '') ELSE NULL END AS status_detail,
                COALESCE(li.created_at, s.created_at, s.synced_at) AS created_at,
                COALESCE(s.updated_at, s.synced_at, li.created_at) AS updated_at,
                s.published_at,
                rr.status AS refund_status,
                (s.status = 0) AS can_withdraw,
                (s.status = 2 AND rr.status IS NULL) AS can_refund,
                (s.status = -1) AS can_republish,
                COALESCE(li.created_at, s.created_at, s.synced_at) AS sort_at
            FROM mhz_synced_orders s
            LEFT JOIN LATERAL (
                SELECT
                    i.id, i.cost_points, i.brand_id, i.media_name, i.media_type,
                    i.created_at, o.article_id, o.article_title
                FROM mhz_publish_order_items i
                JOIN mhz_publish_orders o ON o.id = i.order_id
                WHERE i.user_id = %s AND i.mhz_order_id IN (s.order_sn, s.id)
                ORDER BY i.id DESC
                LIMIT 1
            ) li ON TRUE
            LEFT JOIN brands b ON b.id = COALESCE(s.brand_id, li.brand_id)
            LEFT JOIN LATERAL (
                SELECT r.status
                FROM mhz_refund_requests r
                WHERE r.order_id = s.id AND r.user_id = %s
                ORDER BY r.created_at DESC, r.id DESC
                LIMIT 1
            ) rr ON TRUE
            WHERE s.user_id = %s
        ),
        local_rows AS (
            SELECT
                'publish:proxy:' || i.id::text AS record_key,
                'proxy'::text AS source,
                o.id::text AS action_id,
                NULL::text AS order_sn,
                o.article_id,
                o.article_title,
                i.brand_id,
                b.name AS brand_name,
                i.media_name AS channel_name,
                COALESCE(NULLIF(i.media_type, ''), 'mhz') AS media_type,
                CASE
                    WHEN i.status = 'pending' THEN 'preparing'
                    WHEN i.status = 'submitting' THEN 'submitting'
                    WHEN i.status = 'submitted' THEN 'submitted'
                    WHEN i.status = 'awaiting_confirmation' THEN 'awaiting_confirmation'
                    WHEN i.status = 'published' THEN 'completed'
                    WHEN i.status IN ('failed', 'rejected', 'cancelled') THEN 'failed'
                    WHEN i.status = 'withdrawn' THEN 'withdrawn'
                    ELSE 'processing'
                END AS status_key,
                CASE
                    WHEN i.status = 'pending' THEN '准备提交'
                    WHEN i.status = 'submitting' THEN '正在提交'
                    WHEN i.status = 'submitted' THEN '已提交，等待平台同步'
                    WHEN i.status = 'awaiting_confirmation' THEN '等待确认'
                    WHEN i.status = 'published' THEN '已完成'
                    WHEN i.status IN ('failed', 'rejected', 'cancelled') THEN '未完成'
                    WHEN i.status = 'withdrawn' THEN '已撤回'
                    ELSE '处理中'
                END AS status_label,
                CASE
                    WHEN i.status = 'pending' THEN 'pending'
                    WHEN i.status = 'published' THEN 'completed'
                    WHEN i.status IN ('failed', 'rejected', 'cancelled') THEN 'rejected'
                    WHEN i.status = 'withdrawn' THEN 'withdrawn'
                    ELSE 'in_progress'
                END AS status_family,
                COALESCE(i.cost_points, 0)::bigint AS points,
                -- [R2 §②] 本地下单同为供应商回执 lane。
                CASE WHEN i.status = 'published' THEN 'verified_published'
                     ELSE 'not_published' END AS publication_axis,
                CASE WHEN i.status = 'published' THEN NULLIF(i.publish_url, '') ELSE NULL END AS public_url,
                NULL::text AS status_detail,
                i.created_at,
                COALESCE(i.last_submit_at, i.submitted_at, i.created_at) AS updated_at,
                i.published_at,
                NULL::text AS refund_status,
                (i.status = 'pending' AND i.mhz_order_id IS NULL) AS can_withdraw,
                FALSE AS can_refund,
                FALSE AS can_republish,
                i.created_at AS sort_at
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            LEFT JOIN brands b ON b.id = i.brand_id
            WHERE i.user_id = %s
              AND (i.status = ANY(%s) OR i.manual_review_required = TRUE)
              AND NOT EXISTS (
                  SELECT 1 FROM mhz_synced_orders s
                  WHERE s.user_id = %s AND i.mhz_order_id IN (s.order_sn, s.id)
              )
        ),
        self_rows AS (
            SELECT
                'publish:self:' || pr.id::text AS record_key,
                'self'::text AS source,
                pr.id::text AS action_id,
                NULL::text AS order_sn,
                pr.article_id,
                COALESCE(pr.article_title, '') AS article_title,
                pr.brand_id,
                b.name AS brand_name,
                COALESCE(NULLIF(pr.platform, ''), '自助发布') AS channel_name,
                'self'::text AS media_type,
                -- [R2 §②③] 自助发布行的 status='success' 只是**操作回执**成功。
                -- 没有权威核实过之前,列表里不许写「已发布」。
                -- 🔴 R3 §⑤ 全段 COALESCE:`NULL = 'verified'` 求值为 NULL 而不是
                -- FALSE,三值逻辑下这些 CASE 会静默掉进下一格。列虽有 NOT NULL
                -- DEFAULT,但外连接/子查询补出来的仍是 NULL。
                CASE
                    WHEN COALESCE(pr.status, '') = 'success'
                         AND COALESCE(pr.public_url_verification_state, '') = 'verified'
                        THEN 'completed'
                    WHEN COALESCE(pr.status, '') = 'success' THEN 'reported_success_unverified'
                    WHEN COALESCE(pr.status, '') = 'failed' THEN 'failed'
                    ELSE 'processing'
                END AS status_key,
                CASE
                    WHEN COALESCE(pr.status, '') = 'success'
                         AND COALESCE(pr.public_url_verification_state, '') = 'verified'
                        THEN '已核实发布'
                    WHEN COALESCE(pr.status, '') = 'success' THEN '浏览器曾回报成功 · 尚未核实'
                    WHEN COALESCE(pr.status, '') = 'failed' THEN '未完成'
                    ELSE '处理中'
                END AS status_label,
                CASE
                    WHEN COALESCE(pr.status, '') = 'success'
                         AND COALESCE(pr.public_url_verification_state, '') = 'verified'
                        THEN 'completed'
                    WHEN COALESCE(pr.status, '') = 'success' THEN 'reported_unverified'
                    WHEN COALESCE(pr.status, '') = 'failed' THEN 'rejected'
                    ELSE 'in_progress'
                END AS status_family,
                0::bigint AS points,
                -- 🔴 位置必须与 synced_rows / local_rows 的同名列对齐:
                --    UNION ALL 按**位置**对齐,不按名字。错位不报错,只是悄悄串列。
                -- 🔴 这一格与 `services.publication_receipt_projection.PUBLICATION_AXIS_SQL`
                --    必须逐字同义(那边有逐格比对的判据在守,含 NULL 档)。
                CASE
                    WHEN COALESCE(pr.status, '') <> 'success' THEN 'not_published'
                    WHEN COALESCE(pr.public_url_verification_state, '') = 'verified'
                        THEN 'verified_published'
                    ELSE 'reported_success_unverified'
                END AS publication_axis,
                -- 查看:优先给自报的公开 URL,没有再退回草稿链接
                CASE WHEN COALESCE(pr.status, '') = 'success'
                     THEN COALESCE(NULLIF(pr.public_url, ''), NULLIF(pr.draft_url, ''))
                     ELSE NULL END AS public_url,
                -- [WO_273] 未核实行只说状态,不再指向动作(两个动作随插件后端删除)。
                CASE
                    WHEN COALESCE(pr.status, '') = 'failed' THEN '发布未完成，请稍后重试'
                    WHEN COALESCE(pr.status, '') = 'success'
                         AND COALESCE(pr.public_url_verification_state, '') <> 'verified'
                        THEN {attest_hint}
                    ELSE NULL
                END AS status_detail,
                pr.created_at,
                pr.created_at AS updated_at,
                -- 🔴 published_at 只在**核实过**之后才给 —— 它是"发布时间"不是"点击时间"
                CASE WHEN COALESCE(pr.status, '') = 'success'
                          AND COALESCE(pr.public_url_verification_state, '') = 'verified'
                     THEN pr.created_at ELSE NULL END AS published_at,
                NULL::text AS refund_status,
                FALSE AS can_withdraw,
                FALSE AS can_refund,
                FALSE AS can_republish,
                pr.created_at AS sort_at
            FROM publish_records pr
            LEFT JOIN brands b ON b.id = pr.brand_id
            WHERE pr.user_id = %s::text
        ),
        canonical_raw AS (
            SELECT * FROM synced_rows
            UNION ALL SELECT * FROM local_rows
            UNION ALL SELECT * FROM self_rows
        ),
        canonical AS (
            SELECT DISTINCT ON (record_key) *
            FROM canonical_raw
            ORDER BY record_key, updated_at DESC NULLS LAST, sort_at DESC NULLS LAST
        ),
        base_filtered AS (
            SELECT * FROM canonical WHERE {base_where_sql}
        ),
        visible AS (
            SELECT * FROM base_filtered WHERE {visible_where_sql}
        ),
        stats AS (
            SELECT jsonb_build_object(
                'total', COUNT(*),
                'completed', COUNT(*) FILTER (WHERE status_family = 'completed'),
                'in_progress', COUNT(*) FILTER (WHERE status_family = 'in_progress'),
                'pending', COUNT(*) FILTER (WHERE status_family = 'pending'),
                'rejected', COUNT(*) FILTER (WHERE status_family = 'rejected'),
                'withdrawn', COUNT(*) FILTER (WHERE status_family = 'withdrawn'),
                'refunded', COUNT(*) FILTER (WHERE status_family = 'refunded'),
                -- [R2 §②③] 回执成功但没核实过 —— 独立一档,**不并进 completed**
                'reported_unverified', COUNT(*) FILTER (WHERE status_family = 'reported_unverified')
            ) AS value
            FROM base_filtered
        ),
        filters AS (
            SELECT jsonb_build_object(
                'brands', COALESCE((
                    SELECT jsonb_agg(jsonb_build_object('id', x.brand_id, 'name', x.brand_name) ORDER BY x.brand_name)
                    FROM (
                        SELECT DISTINCT brand_id, COALESCE(NULLIF(brand_name, ''), '客户 #' || brand_id::text) AS brand_name
                        FROM canonical
                        WHERE ({source_where}) AND brand_id IS NOT NULL
                    ) x
                ), '[]'::jsonb),
                'media_types', COALESCE((
                    SELECT jsonb_agg(x.media_type ORDER BY x.media_type)
                    FROM (
                        SELECT DISTINCT media_type FROM canonical
                        WHERE ({source_where}) AND media_type IS NOT NULL AND media_type <> ''
                    ) x
                ), '[]'::jsonb)
            ) AS value
        )
    """

    row_json = """
        jsonb_strip_nulls(jsonb_build_object(
            'record_key', record_key,
            'source', source,
            'action_id', action_id,
            'order_sn', order_sn,
            'article_id', article_id,
            'article_title', article_title,
            'brand_id', brand_id,
            'brand_name', brand_name,
            'channel_name', channel_name,
            'media_type', media_type,
            'status_key', status_key,
            'status_label', status_label,
            'status_family', status_family,
            -- [R2 §②] 发布轴:前端要说「已发布」只能看这个,不能看 status_key
            'publication_axis', publication_axis,
            'points', points,
            'public_url', public_url,
            'status_detail', status_detail,
            'created_at', created_at,
            'updated_at', updated_at,
            'published_at', published_at,
            'refund_status', refund_status,
            'can_withdraw', can_withdraw,
            'can_refund', can_refund,
            'can_republish', can_republish,
            -- [R2 §③] 未核实回执行原有三动作:查看 / 重新核实 / 补人工证据。
            -- [WO_273] 后两个的后端端点随插件后端删除,不再下发;只剩「查看」。
            -- 🔴 `jsonb_strip_nulls` 会把 false 保留、把 null 丢掉,所以这里给的是
            --    布尔而不是 null —— 前端拿不到字段与拿到 false 是两回事。
            -- 🔴 SQL 注释里也不许出现百分号占位符 —— psycopg2 照样把它当参数数。
            'can_view', (public_url IS NOT NULL AND public_url <> '')
        ))
    """

    if view == "article":
        result_sql = f"""
            , article_groups AS (
                SELECT
                    source || ':' || group_identity AS record_key,
                    source,
                    article_id,
                    MAX(article_title) AS article_title,
                    MAX(brand_id) AS brand_id,
                    MAX(brand_name) AS brand_name,
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status_family = 'completed') AS completed,
                    COUNT(*) FILTER (WHERE status_family IN ('in_progress', 'pending')) AS in_progress,
                    -- [R2 §②③] 不并进 completed 也不并进 in_progress:它既不是已核实
                    -- 发布,也不是"还在跑" —— 它是"有人回报过,没人核实过"。
                    COUNT(*) FILTER (WHERE status_family = 'reported_unverified') AS reported_unverified,
                    COUNT(*) FILTER (WHERE status_family = 'rejected') AS rejected,
                    COUNT(*) FILTER (WHERE status_family = 'withdrawn') AS withdrawn,
                    COUNT(*) FILTER (WHERE status_family = 'refunded') AS refunded,
                    MAX(sort_at) AS sort_at,
                    jsonb_agg({row_json} ORDER BY sort_at DESC NULLS LAST, record_key DESC) AS records
                FROM (
                    SELECT *, COALESCE(article_id::text, 'record:' || record_key) AS group_identity
                    FROM visible
                ) grouped_visible
                GROUP BY source, group_identity, article_id
            ),
            result_total AS (SELECT COUNT(*)::int AS value FROM article_groups),
            page_rows AS (
                SELECT * FROM article_groups
                ORDER BY sort_at DESC NULLS LAST, record_key DESC
                LIMIT %s OFFSET %s
            )
            SELECT
                COALESCE(jsonb_agg(
                    jsonb_strip_nulls(to_jsonb(page_rows) - 'sort_at')
                    ORDER BY sort_at DESC NULLS LAST, record_key DESC
                ), '[]'::jsonb) AS records,
                COALESCE((SELECT value FROM result_total), 0) AS total,
                COALESCE((SELECT value FROM stats), '{{}}'::jsonb) AS stats,
                COALESCE((SELECT value FROM filters), '{{}}'::jsonb) AS filters
            FROM page_rows
        """
    else:
        result_sql = f"""
            , result_total AS (SELECT COUNT(*)::int AS value FROM visible),
            page_rows AS (
                SELECT * FROM visible
                ORDER BY sort_at DESC NULLS LAST, record_key DESC
                LIMIT %s OFFSET %s
            )
            SELECT
                COALESCE(jsonb_agg({row_json} ORDER BY sort_at DESC NULLS LAST, record_key DESC), '[]'::jsonb) AS records,
                COALESCE((SELECT value FROM result_total), 0) AS total,
                COALESCE((SELECT value FROM stats), '{{}}'::jsonb) AS stats,
                COALESCE((SELECT value FROM filters), '{{}}'::jsonb) AS filters
            FROM page_rows
        """

    # Parameter order follows the CTEs and then the two repeated source filters
    # used by metadata.  Keeping the whole read in one statement gives a single
    # PostgreSQL READ COMMITTED statement snapshot during scheduler sync.
    params: List[Any] = [
        str(markup_ratio), user_id, user_id, user_id,
        user_id, list(_LOCAL_CUSTOMER_HISTORY_STATUSES), user_id,
        user_id,
    ]
    params.extend(base_params)
    params.extend(visible_params)
    params.extend(source_params)
    params.extend(source_params)
    params.extend([limit, offset])

    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(common_ctes + result_sql, params)
        row = c.fetchone() or {}
        total = int(row.get("total") or 0)
        return {
            "records": row.get("records") or [],
            "total": total,
            "page": page,
            "pages": (total + limit - 1) // limit,
            "stats": row.get("stats") or {},
            "filters": row.get("filters") or {"brands": [], "media_types": []},
        }
    finally:
        conn.close()


def list_local_pending_items(page: int = 1, limit: int = 50,
                             status_filter: str = "", brand_id: int = None,
                             search: str = "") -> Dict:
    """[2026-04-30 admin 衔接] 列出 mhz_publish_order_items 里还没同步进
    mhz_synced_orders 的 item（管理员视图用）。

    包含的状态：
      - 在途状态（pending/submitting/submitted/awaiting_*）
      - 这些状态下 mhz_order_id 通常为空或没在 synced 表里出现
      - 也包含 manual_review_required=TRUE 的（本地标记需人工干预的）

    返回结构兼容 list_mhz_synced_orders，前端能复用同一套展示。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        where = [
            "(i.status = ANY(%s) OR i.manual_review_required = TRUE)",
            # 排除已经在 mhz_synced_orders 里的（避免重复展示）
            "NOT EXISTS (SELECT 1 FROM mhz_synced_orders s WHERE s.order_sn = i.mhz_order_id)",
        ]
        params: list = [list(_LOCAL_INFLIGHT_STATUSES)]

        if status_filter:
            # 支持按 item 的文字 status 筛选（admin 看待处理的子集）
            where.append("i.status = %s")
            params.append(status_filter)
        if brand_id:
            where.append("i.brand_id = %s")
            params.append(brand_id)
        if search:
            where.append("(o.article_title ILIKE %s OR i.media_name ILIKE %s OR u.display_name ILIKE %s OR u.username ILIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like, like])
        where_sql = " AND ".join(where)

        count_sql = f"""
            SELECT COUNT(*) AS total
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            LEFT JOIN users u ON i.user_id = u.id
            WHERE {where_sql}
        """
        c.execute(count_sql, params)
        total = c.fetchone()["total"]

        offset = (page - 1) * limit
        c.execute(f"""
            SELECT
                i.id, i.user_id, i.media_id, i.media_name, i.media_type,
                i.status AS local_status,
                i.mhz_order_id, i.cost_points, i.brand_id,
                i.last_submit_at, i.awaiting_sync_since, i.manual_review_required,
                i.reject_reason, i.created_at AS item_created_at,
                o.article_id, o.article_title AS title, o.created_at AS order_created_at,
                u.display_name AS user_display_name, u.username AS user_login
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            LEFT JOIN users u ON i.user_id = u.id
            WHERE {where_sql}
            ORDER BY i.manual_review_required DESC, i.last_submit_at DESC NULLS LAST, i.created_at DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset])

        items = []
        for r in c.fetchall():
            d = dict(r)
            for k in ("last_submit_at", "awaiting_sync_since", "item_created_at", "order_created_at"):
                if d.get(k) and hasattr(d[k], "isoformat"):
                    d[k] = d[k].isoformat()
            # 加一个 hint 字段，告诉前端这条订单为啥在这（让用户/管理员能直接看懂）
            if d.get("manual_review_required"):
                d["hint"] = "需人工审核（mhz 接单 12 小时未拿到凭证号自愈）"
            elif d.get("local_status") == "awaiting_sync":
                d["hint"] = "等待反查同步（mhz 已接单，正在补凭证号）"
            elif d.get("local_status") == "awaiting_confirmation":
                d["hint"] = "等待用户确认（mhz 提示内容/标题需确认后提交）"
            elif d.get("local_status") == "submitting":
                d["hint"] = "提交中（已锁定，正在调外部发布通道接口）"
            elif d.get("local_status") == "submitted":
                d["hint"] = "已提交外部发布通道，等待 mhz 列表同步过来"
            else:
                d["hint"] = f"本地状态 {d.get('local_status')}"
            items.append(d)
        return {"items": items, "total": total, "page": page, "pages": (total + limit - 1) // limit}
    finally:
        conn.close()


def list_manual_review_items(page: int = 1, limit: int = 50,
                              brand_id: int = None, search: str = "") -> Dict:
    """[2026-04-30 admin 衔接] 列出所有需要人工审核的 item（manual_review_required=TRUE）。

    这些是 awaiting_sync 状态超 12 小时仍反查不到 mhz 订单号的特殊情况：
      - mhz 那边可能真发了但凭证号丢失
      - 或 mhz 那边压根没接单但响应给了 200
      - 任何一种情况下都**不能自动退款也不能自动重发**，必须管理员人工核实
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        where = ["i.manual_review_required = TRUE"]
        params: list = []
        if brand_id:
            where.append("i.brand_id = %s")
            params.append(brand_id)
        if search:
            where.append("(o.article_title ILIKE %s OR i.media_name ILIKE %s OR u.display_name ILIKE %s OR u.username ILIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like, like])
        where_sql = " AND ".join(where)

        c.execute(f"""
            SELECT COUNT(*) AS total
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            LEFT JOIN users u ON i.user_id = u.id
            WHERE {where_sql}
        """, params)
        total = c.fetchone()["total"]

        offset = (page - 1) * limit
        c.execute(f"""
            SELECT
                i.id, i.user_id, i.media_id, i.media_name, i.media_type,
                i.status AS local_status,
                i.cost_points, i.brand_id,
                i.last_submit_at, i.awaiting_sync_since,
                i.reject_reason, i.mhz_raw_response,
                o.article_id, o.article_title AS title,
                u.display_name AS user_display_name, u.username AS user_login
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            LEFT JOIN users u ON i.user_id = u.id
            WHERE {where_sql}
            ORDER BY i.awaiting_sync_since ASC NULLS LAST
            LIMIT %s OFFSET %s
        """, params + [limit, offset])

        items = []
        for r in c.fetchall():
            d = dict(r)
            for k in ("last_submit_at", "awaiting_sync_since"):
                if d.get(k) and hasattr(d[k], "isoformat"):
                    d[k] = d[k].isoformat()
            items.append(d)
        return {"items": items, "total": total, "page": page, "pages": (total + limit - 1) // limit}
    finally:
        conn.close()


def count_local_pending_and_review() -> Dict:
    """[2026-04-30] admin 概览统计补充：本地未同步项数 + 人工审核队列数"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT COUNT(*) AS cnt
            FROM mhz_publish_order_items i
            WHERE (i.status = ANY(%s) OR i.manual_review_required = TRUE)
              AND NOT EXISTS (SELECT 1 FROM mhz_synced_orders s WHERE s.order_sn = i.mhz_order_id)
        """, (list(_LOCAL_INFLIGHT_STATUSES),))
        pending = int(c.fetchone()["cnt"] or 0)

        c.execute("SELECT COUNT(*) AS cnt FROM mhz_publish_order_items WHERE manual_review_required = TRUE")
        manual = int(c.fetchone()["cnt"] or 0)

        return {"pre_sync_count": pending, "manual_review_count": manual}
    finally:
        conn.close()


def admin_manual_review_resolve(item_id: int, action: str, admin_id: int, admin_note: str = "") -> Dict:
    """[2026-04-30] 管理员处理人工审核队列的 item。

    Args:
        action:
          'mark_published' = 已确认 mhz 那边发出去了 → 标 published（不退款）
          'mark_failed'    = 已确认没发出去 → 标 failed + 按 item 退款
        admin_id, admin_note: 操作者和备注

    Returns: {success: bool, refunded_amount: int, message: str}
    """
    if action not in ("mark_published", "mark_failed"):
        return {"success": False, "refunded_amount": 0, "message": "action 不合法"}

    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, user_id, cost_points, status, manual_review_required
            FROM mhz_publish_order_items WHERE id = %s
        """, (item_id,))
        row = c.fetchone()
        if not row:
            return {"success": False, "refunded_amount": 0, "message": "item 不存在"}
        if not row["manual_review_required"]:
            return {"success": False, "refunded_amount": 0, "message": "该 item 不在人工审核队列"}

        new_status = "published" if action == "mark_published" else "failed"
        admin_reason = f"管理员人工核对：{action} (admin={admin_id}) {admin_note}".strip()
        c.execute("""
            UPDATE mhz_publish_order_items
            SET status = %s, reject_reason = %s, manual_review_required = FALSE
            WHERE id = %s AND manual_review_required = TRUE
            RETURNING id,user_id
        """, (new_status, admin_reason, item_id))
        terminal_row = c.fetchone()
        if not terminal_row:
            conn.rollback()
            return {"success": False, "refunded_amount": 0, "message": "并发处理冲突"}
        _enqueue_publish_notification(
            c,
            user_id=int(terminal_row["user_id"]),
            refund_key=f"item:{int(item_id)}",
            event_type=("publication.completed" if action == "mark_published" else "publication.rejected"),
            terminal_state=new_status,
            status=("已发布" if action == "mark_published" else "未完成"),
        )
        conn.commit()
    finally:
        conn.close()

    refunded = 0
    if action == "mark_failed":
        cost = int(row["cost_points"] or 0)
        if cost > 0:
            r = refund_for_publish_order(
                user_id=int(row["user_id"]),
                amount=cost,
                refund_key=f"item:{item_id}",
                reason=f"管理员人工核对未接单退款 (admin={admin_id})",
            )
            if r.get("success") and not r.get("skipped"):
                refunded = cost
    return {"success": True, "refunded_amount": refunded,
            "message": f"已标 {new_status}" + (f"，退款 {refunded} 积分" if refunded else "")}


# ========================================
# 退款申请
# ========================================

class RefundNeedsManualReview(Exception):
    """[WO_310] 这笔退款拿不到原扣费(找不到本平台的下单条目)⇒ 不能自动批准,转人工。"""


def resolve_refund_payer(synced_order_id, cursor=None) -> Optional[Dict[str, int]]:
    """[WO_310] 同步订单 → order_sn → 本平台下单条目 → 发布订单,返回 {payer_user_id, item_id};找不到返回 None。

    🔴 受益人一律是**付款人** `mhz_publish_orders.user_id`。`mhz_synced_orders.user_id` 不用于钱:
       定时同步给它填 1,管理员手动同步填操作管理员自己的 id(Review 09-27 据 C 普查定)。
    找不到条目 = 这笔单没有本平台的扣费记录(原扣费只挂在「条目 → 发布订单」这条链上),
    调用方据此拒绝 / 转人工,不许按 price × markup × 130 现算着退。
    """
    own = cursor is None
    conn = _get_conn() if own else None
    c = conn.cursor() if own else cursor
    try:
        c.execute("SELECT order_sn FROM mhz_synced_orders WHERE id = %s", (synced_order_id,))
        sr = c.fetchone()
        if not sr or not sr.get("order_sn"):
            return None
        c.execute("""
            SELECT it.id AS item_id, o.user_id AS payer_user_id
            FROM mhz_publish_order_items it
            JOIN mhz_publish_orders o ON o.id = it.order_id
            WHERE it.mhz_order_id = %s
            ORDER BY it.id
            LIMIT 1
        """, (sr["order_sn"],))
        r = c.fetchone()
        if not r or r.get("payer_user_id") is None:
            return None
        return {"payer_user_id": int(r["payer_user_id"]), "item_id": int(r["item_id"])}
    finally:
        if own:
            conn.close()


def create_refund_request(order_id: str, user_id: int, reason: str, refund_points: int,
                          requested_by: Optional[int] = None) -> int:
    """创建退款申请，返回申请 ID

    [WO_310] user_id = 受益人(付款人);requested_by = 提出申请的人(管理员代申请时是管理员)。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO mhz_refund_requests (order_id, user_id, reason, refund_points, status, requested_by)
            VALUES (%s, %s, %s, %s, 'pending', %s) RETURNING id
        """, (order_id, user_id, reason, refund_points, requested_by))
        rid = c.fetchone()["id"]
        conn.commit()
        return rid
    finally:
        conn.close()


def list_refund_requests(user_id: int = None, status: str = "", page: int = 1,
                         limit: int = 20, search: str = "", brand_id: int = None) -> Dict:
    """查询退款申请列表，支持按用户和状态过滤"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        where = ["1=1"]
        params: list = []
        if user_id:
            where.append("r.user_id = %s")
            params.append(user_id)
        if status:
            where.append("r.status = %s")
            params.append(status)
        if search:
            where.append("(o.title ILIKE %s OR r.order_id ILIKE %s OR u.display_name ILIKE %s OR u.username ILIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like, like])
        if brand_id:
            where.append("o.brand_id = %s")
            params.append(brand_id)
        where_sql = " AND ".join(where)

        c.execute(f"""
            SELECT COUNT(*) AS total FROM mhz_refund_requests r
            LEFT JOIN mhz_synced_orders o ON r.order_id = o.id
            LEFT JOIN users u ON r.user_id = u.id
            WHERE {where_sql}
        """, params)
        total = c.fetchone()["total"]
        offset = (page - 1) * limit
        c.execute(f"""
            SELECT r.*, o.title, o.media_name, o.price, o.url,
                   u.display_name AS user_name, u.username AS user_login
            FROM mhz_refund_requests r
            LEFT JOIN mhz_synced_orders o ON r.order_id = o.id
            LEFT JOIN users u ON r.user_id = u.id
            WHERE {where_sql}
            ORDER BY r.created_at DESC LIMIT %s OFFSET %s
        """, params + [limit, offset])
        items = []
        for row in c.fetchall():
            d = dict(row)
            for k in ("created_at", "reviewed_at"):
                if d.get(k) and hasattr(d[k], "isoformat"):
                    d[k] = d[k].isoformat()
            items.append(d)
        return {"requests": items, "total": total, "page": page, "pages": (total + limit - 1) // limit}
    finally:
        conn.close()


def review_refund_request(request_id: int, approved: bool, admin_id: int, admin_note: str = "") -> bool:
    """管理员审核退款申请，通过时退还积分到用户钱包并写流水。

    [第四轮审计修] 之前用手动 SQL 退款 + str(request_id) 作 transaction.order_id，
    会和 sync 自动退款（key=item:{id}）出现双键不一致 → 同笔钱可能被退两次。
    现在改走 refund_for_publish_order 统一 helper：
      - 优先用 item:{item_id} 作幂等键（如果能查到对应的 item）
      - 否则用 refund_request:{request_id} 兜底（防止 item_id 关联不到时退款失败）—— WO_310 起不再用于批准,见下段

    [WO_310 · 2026-09-27] 兜底键 refund_request:{id} **不再用于批准**:它触发的条件就是找不到本平台
    下单条目,而原扣费只挂在条目上(统一退款 helper 的封顶只认 item:{id})⇒ 兜底路径等于不封顶地
    按 price × markup × 130 现算着退。现在批准时拿不到条目 ⇒ 抛 RefundNeedsManualReview,
    **申请保持 pending、不动钱**,由人工处理;拒绝(approved=False)照旧可以做。
    受益人按付款人 `mhz_publish_orders.user_id` 现取,不用申请行上存的 user_id —— 历史上管理员
    代申请记在管理员名下、仍 pending 的那些,批准后也退给付款人(不改历史数据)。
    """
    conn = _get_conn()
    review_ok = False
    refund_user_id = None
    refund_amount = 0
    refund_key = None

    try:
        c = conn.cursor()
        payer = None
        if approved:
            c.execute("SELECT order_id FROM mhz_refund_requests WHERE id = %s AND status = 'pending'", (request_id,))
            pending = c.fetchone()
            if pending:
                payer = resolve_refund_payer(pending["order_id"], cursor=c)
                if payer is None:
                    conn.rollback()
                    raise RefundNeedsManualReview(
                        "这笔订单找不到本平台的下单条目,拿不到原扣费,不能自动退款;请转人工核实后处理"
                    )
                if int(payer["payer_user_id"]) <= 0:
                    # 付款人 id ≤ 0 是系统账户,不走应用退款通道
                    conn.rollback()
                    raise RefundNeedsManualReview(
                        "这笔订单的付款方是保留账号,不走应用退款通道;请转人工按其自有账本处理"
                    )
        # 先 review 状态变更（同事务，防并发审核）
        c.execute("""
            UPDATE mhz_refund_requests SET status = %s, admin_note = %s, reviewed_at = NOW(), reviewed_by = %s
            WHERE id = %s AND status = 'pending'
            RETURNING user_id, refund_points, order_id
        """, ('approved' if approved else 'rejected', admin_note, admin_id, request_id))
        row = c.fetchone()
        review_ok = row is not None
        conn.commit()

        if not (approved and review_ok):
            return review_ok

        # [WO_310] 受益人 = 付款人(上面批准前已解析);幂等键 = item:{id}(封顶只认这个键)
        refund_user_id = int(payer["payer_user_id"])
        refund_amount = int(row["refund_points"] or 0)
        refund_key = f"item:{payer['item_id']}"
        synced_order_id = row["order_id"]

        # [WO_310] 原来这里按 order_sn 找条目、找不到回落兜底键 refund_request:{id};现在批准前已由
        #   resolve_refund_payer 解析(同一条链,按 item.id 排序取首条),找不到直接转人工 ⇒ 这段删掉,只留一处取键逻辑。
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 走统一 helper（独立 connection，不受外部事务影响）
    if approved and review_ok and refund_user_id and refund_amount > 0:
        result = refund_for_publish_order(
            user_id=refund_user_id,
            amount=refund_amount,
            refund_key=refund_key,
            reason=f"管理员审核退款 (request_id={request_id})",
        )
        if not result.get("success") and not result.get("skipped"):
            import logging
            logging.getLogger("GEO-MHZ-DB").error(
                f"[review_refund_request] helper 退款失败 request={request_id}: {result.get('reason')}"
            )

    return review_ok


def get_order_refund_status(order_ids: List[str], user_id: int) -> Dict[str, str]:
    """批量查询订单的退款状态，返回 {order_id: status}"""
    if not order_ids:
        return {}
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT DISTINCT ON (order_id) order_id, status
            FROM mhz_refund_requests
            WHERE order_id = ANY(%s) AND user_id = %s
            ORDER BY order_id, created_at DESC
        """, (order_ids, user_id))
        return {row["order_id"]: row["status"] for row in c.fetchall()}
    finally:
        conn.close()


#: 理由码 —— 这篇已经在这家媒体发出去了,别重复发。
BLOCK_REASON_ALREADY_PUBLISHED = "ALREADY_PUBLISHED"
#: 理由码 —— 这篇正在这家媒体的发布流程里(已拿到外部单号或在人工/确认环节)。
BLOCK_REASON_IN_FLIGHT = "IN_FLIGHT"


def find_active_orders_for_media(article_id: int, media_ids: List[int]) -> List[dict]:
    """返回 media_ids 中**真该被拦**的记录,每条带理由码。

    用于提交前拦截：在扣费/建单之前调用，有结果则直接返回 409，不扣用户的钱。

    🔴 [WO-PUB-ZOMBIE-2026-08-04] 老实现用「非失败终态」反向定义"活跃",把三种
    完全不同的东西混成一句"进行中":

      · ``published``(95 天前就发完了)      → 该拦,但说成"进行中"是错的
      · ``pending`` **有**外部单号(真在跑)   → 该拦
      · ``pending`` **无**外部单号            → 🔴 **根本没提交成功,不该拦**

    第三种是生产事故本体:批量下单部分 item 没拿到 ``mhz_order_id``,既没标失败
    也没退款,永远停在 pending,于是那篇文章对那家媒体**永久锁死**,用户怎么点都
    发不出去。

    现在改成正向列举:
      · ``published``                                   → 拦 · ALREADY_PUBLISHED
      · 其余非终态 **且有外部单号**                       → 拦 · IN_FLIGHT
      · 其余非终态 **无外部单号但也不是 pending**         → 拦 · IN_FLIGHT
        (awaiting_confirmation / awaiting_action 等等着用户或人工处置的态,
         它们本来就没有外部单号,放行会让用户重复下单重复扣费 —— 保持老行为)
      · ``pending`` **无外部单号**                        → 🔴 不拦(本次修复点)
      · 终态 withdrawn/rejected/failed/cancelled          → 不拦(老行为不变)

    Returns: ``[{"media_id": int, "reason_code": str}, ...]``,按 media_id 稳定排序。
    """
    if not article_id or not media_ids:
        return []
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT i.media_id,
                   BOOL_OR(i.status = 'published') AS has_published
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            WHERE o.article_id = %s
              AND i.media_id = ANY(%s)
              AND i.status NOT IN ('withdrawn', 'rejected', 'failed', 'cancelled')
              AND (
                    i.status = 'published'
                 OR i.mhz_order_id IS NOT NULL AND i.mhz_order_id <> ''
                 OR i.status <> 'pending'
              )
            GROUP BY i.media_id
            ORDER BY i.media_id
        """, (article_id, media_ids))
        return [
            {
                "media_id": r["media_id"],
                "reason_code": (BLOCK_REASON_ALREADY_PUBLISHED if r["has_published"]
                                else BLOCK_REASON_IN_FLIGHT),
            }
            for r in c.fetchall()
        ]
    finally:
        conn.close()


def count_mirror_hits_for_known_orders(sample_size: int = 6) -> tuple:
    """[WO-PUB-ZOMBIE] 判别力自检：拿**已知有外部单号**的条目按 order_sn 回查镜像。

    返回 ``(checked, hits)``。``hits < checked`` 说明 ``mhz_synced_orders`` 不全
    或者查法失效 —— 这时"僵尸在外部查不到"是**假结论**,调用方必须整轮跳过。
    """
    n = max(1, int(sample_size))
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(f"""
            SELECT (SELECT count(*) FROM mhz_synced_orders s
                     WHERE s.order_sn = i.mhz_order_id) AS hit
            FROM mhz_publish_order_items i
            WHERE i.mhz_order_id IS NOT NULL AND i.mhz_order_id <> ''
            ORDER BY i.id DESC
            LIMIT {n}
        """)
        rows = c.fetchall()
        return len(rows), sum(1 for r in rows if int(r["hit"] or 0) > 0)
    finally:
        conn.close()


def find_mirror_rows_for_orphan(*, article_title: str, media_id, media_name: str) -> List[dict]:
    """[WO-PUB-ZOMBIE] 在外部镜像里找这条孤儿单的"同名同媒体"记录,并标出它是否已被认领。

    ``claimed_by`` = 已经拿着这个 ``order_sn`` 的本地 item_id;``None`` 表示
    **没有任何本地条目认领** —— 那就是"对方收了、我们丢了回执"的形状,禁止自动退款。

    媒体侧刻意用 ``resource_id OR media_name`` 两个口径求并集(而不是只认一个):
    匹配得越宽,越容易落进"查到且未被认领"的禁退分支 = 越不容易退错钱。
    """
    if not article_title:
        return []
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT s.order_sn,
                   (SELECT MIN(i2.id) FROM mhz_publish_order_items i2
                     WHERE i2.mhz_order_id = s.order_sn) AS claimed_by
            FROM mhz_synced_orders s
            WHERE TRIM(s.title) = TRIM(%s)
              AND (s.resource_id = %s OR (%s <> '' AND s.media_name = %s))
        """, (article_title, int(media_id or 0), media_name or "", media_name or ""))
        return [{"order_sn": r["order_sn"], "claimed_by": r["claimed_by"]} for r in c.fetchall()]
    finally:
        conn.close()


def find_orphan_publish_items(*, min_age_hours: int, statuses: List[str],
                              limit: int = 50) -> List[dict]:
    """[WO-PUB-ZOMBIE] 取超时仍未拿到外部单号的孤儿条目。

    🔴 ``mhz_order_id`` 非空的一条都不返回 —— 那些是真在外部通道跑的单,
    清了等于凭空退款。这条约束写死在 WHERE 里,不靠调用方自觉。
    🔴 短视频(``media_type='svideo'``)与快易播改道单排除:它们各有自己的
    超时退款出口(``_mhz_svideo_stuck_refund_sweep`` / ``refund_stale_orders``),
    在这里重复处置等于两个主人抢同一条 item。
    🔴 **合同链(``billing_mode='freeze_per_item'``)另行显式排除**:它的钱是
    *冻着*的不是扣掉的,在这里退等于凭空送钱。不靠 ``media_type`` 顺手挡 ——
    那是等价而脆的巧合,见 WHERE 里的注释。
    """
    from services.kuaiyibo.config import ID_OFFSET

    hours = max(1, int(min_age_hours))
    n = max(1, int(limit))
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(f"""
            SELECT i.id, i.user_id, i.media_id, i.media_name, i.media_type,
                   i.cost_points, i.status, o.id AS order_id, o.article_title
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            WHERE i.status = ANY(%s)
              AND (i.mhz_order_id IS NULL OR i.mhz_order_id = '')
              AND COALESCE(i.media_type, 'mhz') <> 'svideo'
              -- [第 9 棒 · 并车前置件 ③] 显式排除**合同链**(freeze_per_item)。
              -- 上面那句 `media_type <> 'svideo'` 今天**恰好**也把图文合同项挡在外面,
              -- 但那是**等价而脆**:它挡的是"短视频"这个渠道名,不是"钱是冻着的"
              -- 这个计费口径。图文 lane 哪天换个 media_type、或者再来一条新的
              -- freeze_per_item 链,这道墙就没了 —— 而后果是**凭空退款**
              -- (freeze 链的钱是冻着的,不是扣掉的,退它等于白送)。
              -- 与三个兄弟(_retry_stuck_publish_orders / 补退 / svideo 兜底)口径对齐:
              -- 显式列出老值,不用 `<> 新值`(那个写法对 NULL 恒 UNKNOWN)。
              AND (i.billing_mode IS NULL OR i.billing_mode = 'deduct_upfront')
              AND COALESCE(i.routed_media_id, i.media_id, 0) <= {int(ID_OFFSET)}
              AND o.created_at < NOW() - INTERVAL '{hours} hours'
            ORDER BY o.created_at
            LIMIT {n}
        """, (list(statuses),))
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def mark_orphan_needs_manual_review(item_id: int, *, reason: str) -> bool:
    """[WO-PUB-ZOMBIE] 孤儿条目转人工:进 ``awaiting_sync`` + 打人工审核标记。

    刻意复用既有 ``awaiting_sync`` 队列而不是新造一个状态:
      · ``_mhz_awaiting_sync_resolver`` 会继续实时反查,很可能**直接把真单号回填**回来
        (自愈优于人工);
      · 反查不到时它 12 小时后自动转人工、72 小时后开用户出口 —— 整条终态链已经建好;
      · 那条链**明文拒绝自动退款**("mhz 已回执成功提交,稿件可能真发出去了"),
        与本模块的禁退区口径完全一致。

    🔴 绝不在这里退款,也绝不置终态。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE mhz_publish_order_items
            SET status = 'awaiting_sync',
                awaiting_sync_since = COALESCE(awaiting_sync_since, NOW()),
                manual_review_required = TRUE,
                reject_reason = %s
            WHERE id = %s
              AND status = ANY(%s)
              AND (mhz_order_id IS NULL OR mhz_order_id = '')
            RETURNING id, user_id
        """, (reason, item_id, ["pending", "submitted", "paused_admin_dedupe"]))
        row = c.fetchone()
        if row:
            _enqueue_publish_notification(
                c,
                user_id=int(row["user_id"]),
                refund_key=f"item:{int(item_id)}",
                event_type="publication.manual_required",
                terminal_state="manual_required",
                status="需要人工同步",
                include_admins=True,
            )
        conn.commit()
        return row is not None
    except Exception as e:
        conn.rollback()
        logger.error(f"mark_orphan_needs_manual_review 失败 item={item_id}: {e}")
        return False
    finally:
        conn.close()


#: [WO-PUB-ZOMBIE] 「超时仍没拿到外部单号的 pending」= 僵尸,不算占用。
#: 与 `ORPHAN_MIN_AGE_HOURS_DEFAULT` 对齐:小于这个岁数的无单号 pending 仍算在途
#: (同一批同秒下的两条就靠这个拦 —— 生产 386 那种本地重复下单不能放过),
#: 超过了才判僵尸。单位小时,内联进 SQL 前必须 int()。
ORPHAN_AGE_HOURS_FOR_DEDUPE = 3


def check_duplicate_submission(article_id: int, media_id: int, exclude_item_id: int) -> Optional[int]:
    """检查同文章+媒体是否已有活跃订单项（排除自身）。

    活跃状态 = NOT IN (withdrawn, rejected, failed, cancelled)。

    🔴 [WO-PUB-ZOMBIE-2026-08-04] 再排除一类:**超过 3 小时仍没拿到外部单号的
    ``pending``**。那是根本没提交成功的僵尸,不是在途订单。

    不加这条的话,扣费前那道闸(`find_active_orders_for_media`)虽然放行了,
    这道提交时的闸仍会把新 item 判成重复 → `mark_item_failed_duplicate` + 退款,
    用户点了不报错、钱也退了,**但文章还是发不出去** —— 锁只挪了个位置。

    时间下限是刻意的:同一批同秒下的两条彼此都是无单号 pending,那是真在途,
    必须继续拦(生产 386 就是同批下了两次单,外部去重后只回一个单号)。

    返回冲突的 item_id；无冲突返回 None。
    """
    if not article_id or not media_id:
        return None
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(f"""
            SELECT i.id
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            WHERE o.article_id = %s
              AND i.media_id = %s
              AND i.id != %s
              AND i.status NOT IN ('withdrawn', 'rejected', 'failed', 'cancelled')
              AND NOT (
                    i.status = 'pending'
                AND (i.mhz_order_id IS NULL OR i.mhz_order_id = '')
                AND o.created_at < NOW() - INTERVAL '{int(ORPHAN_AGE_HOURS_FOR_DEDUPE)} hours'
              )
            LIMIT 1
        """, (article_id, media_id, exclude_item_id))
        row = c.fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def mark_item_failed_duplicate(item_id: int):
    """将重复订单项直接标为 failed（跳过重试逻辑，不触发三次上限检查）。"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE mhz_publish_order_items
            SET status = 'failed',
                reject_reason = '重复投稿：已有同文章同媒体的活跃订单，自动取消'
            WHERE id = %s AND status = 'submitting'
        """, (item_id,))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


# ============================================================================
# [防重复发布契约 · 2026-04-30] 详见 services/meijiehezi/client.py 顶部说明
# ============================================================================

# 活跃状态闭集：除终态外的所有状态都视为"在途订单"，30 分钟内不允许同 user+article+
# media 再次提交。注意 awaiting_sync 也算活跃（订单已发到 mhz、等反查回填）。
_ACTIVE_STATUSES = (
    'pending', 'submitting', 'submitted',
    'awaiting_confirmation', 'awaiting_sync',
)


def check_recent_active_for_user_media(
    user_id: int,
    article_id: int,
    media_id: int,
    window_minutes: int = 30,
    exclude_item_id: int = None,
) -> Optional[int]:
    """[防线 1] 30 分钟幂等保护 —— try_lock_for_submit 之前必须调用。

    判断同一 (user, article, media) 在过去 window_minutes 内是否已有活跃订单。
    无论触发方是用户双击、调度器自我重试、还是其他任何路径，只要查到活跃订单
    就**直接拒**，不下发 mhz。

    Args:
        user_id: 提交用户
        article_id: 文章 ID
        media_id: 媒体 ID
        window_minutes: 时间窗口（默认 30 分钟，跟调度器 3×10min 重试期对齐）
        exclude_item_id: 可选，排除这个 item 自身（用于"用户重投"场景，
                         resubmit_item 重新提交同一个 awaiting_confirmation
                         的 item 时不应被自己拦住）

    Returns:
        冲突的 item_id；无冲突返回 None。
    """
    if not (user_id and article_id and media_id):
        return None
    conn = _get_conn()
    try:
        c = conn.cursor()
        # 用 created_at（订单建单时间）作为时间窗 anchor —— 避免长时间在途订单
        # 因为 last_submit_at 抖动而过期。同时把 awaiting_sync 也算活跃。
        params = [user_id, article_id, media_id]
        sql = f"""
            SELECT i.id
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            WHERE o.user_id = %s
              AND o.article_id = %s
              AND i.media_id = %s
              AND i.status = ANY(%s)
              AND COALESCE(i.last_submit_at, i.created_at, o.created_at)
                  >= NOW() - INTERVAL '{int(window_minutes)} minutes'
        """
        params.append(list(_ACTIVE_STATUSES))
        if exclude_item_id is not None:
            sql += "  AND i.id != %s\n"
            params.append(exclude_item_id)
        sql += "LIMIT 1"
        c.execute(sql, tuple(params))
        row = c.fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def save_mhz_raw_response(item_id: int, raw_data) -> None:
    """[审计] 保存 mhz 接口原始响应到 mhz_publish_order_items.mhz_raw_response。

    所有调用 mhz publish/publish_wemedia 之后必须调用本函数（不管成功失败）。
    raw_data 可以是 dict / list / str / None；统一序列化为 JSONB。
    """
    if item_id is None:
        return
    import json as _json
    conn = _get_conn()
    try:
        c = conn.cursor()
        try:
            payload = _json.dumps(raw_data, ensure_ascii=False, default=str) if raw_data is not None else None
        except Exception:
            payload = _json.dumps({"_serialize_error": str(raw_data)[:500]}, ensure_ascii=False)
        c.execute(
            "UPDATE mhz_publish_order_items SET mhz_raw_response = %s::jsonb WHERE id = %s",
            (payload, item_id),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning(f"save_mhz_raw_response 失败 item={item_id}: {e}")
    finally:
        conn.close()


def set_item_awaiting_sync(item_id: int, reason: str = "") -> bool:
    """[防线 4] 把 submitting 状态的 item 改为 awaiting_sync。

    场景：mhz 接口返回 code=200 但 order_sn 为空（AmbiguousResponseError）。
    我们假设 mhz 已经接单了（响应 200 是 mhz 收到请求的强证据），把 item 标
    awaiting_sync 等定时任务通过 find_synced_order_by_signature 反查回填。

    禁止：在这条路径上调度重试（这是 04-29 故障的根源）。

    Returns: True 成功；False item 已不在 submitting 状态（被并发改了）。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE mhz_publish_order_items
            SET status = 'awaiting_sync',
                awaiting_sync_since = NOW(),
                reject_reason = %s
            WHERE id = %s AND status = 'submitting'
            RETURNING id
        """, (reason or "mhz 响应模糊（code=200 但 order_sn 为空），等定时反查回填", item_id))
        row = c.fetchone()
        conn.commit()
        return row is not None
    except Exception as e:
        conn.rollback()
        logger.error(f"set_item_awaiting_sync 失败 item={item_id}: {e}")
        return False
    finally:
        conn.close()


def find_synced_order_by_signature(
    user_id: int,
    article_title: str,
    resource_id: int,
    submit_after,  # datetime or ISO string
    window_minutes: int = 30,
) -> Optional[str]:
    """[防线 2] 跨表反查 —— 在 mhz_synced_orders 里按签名找匹配订单。

    用于：
    - awaiting_sync 状态的 item 定时反查回填 mhz_order_id
    - 调度器准备重试 pending+空 mhz_order_id 的 item 之前的"二次确认"

    匹配规则：同 user_id、同 title（去前后空格）、同 resource_id（媒体 ID），
    且 created_at 在 [submit_after - 5min, submit_after + window_minutes] 区间。
    时间窗加 5min 缓冲是因为我方与 mhz 时钟可能有偏移。

    Returns: 匹配到的 mhz_synced_orders.order_sn；无匹配返回 None。
    优先返回 status=0/1（在途）的订单，其次 -1/-2（终态）。
    """
    if not (user_id and article_title and resource_id and submit_after):
        return None
    conn = _get_conn()
    try:
        c = conn.cursor()
        # 注意：mhz_synced_orders 里 user_id 可能是 0 或 1（定时全量同步用 admin
        # 归属），所以 user_id 不作硬过滤；改用 (title + resource_id + 时间窗) 做
        # 主键约束已经足够区分（同一秒内同 title+media 重复提交本身就该被防线 1
        # 拦下来了）。
        # [CTO-15.23 2026-05-13] f-string 展开 INTERVAL · 防累犯 #5b psycopg2 % operator 混用
        _wm = int(window_minutes)
        c.execute(f"""
            SELECT order_sn, status
            FROM mhz_synced_orders
            WHERE TRIM(title) = TRIM(%s)
              AND resource_id = %s
              AND created_at >= %s::timestamp - INTERVAL '5 minutes'
              AND created_at <= %s::timestamp + INTERVAL '{_wm} minutes'
            ORDER BY
                CASE WHEN status IN (0, 1) THEN 0 ELSE 1 END,
                created_at ASC
            LIMIT 1
        """, (article_title, resource_id, submit_after, submit_after))
        row = c.fetchone()
        return row["order_sn"] if row else None
    finally:
        conn.close()


def link_awaiting_sync_to_order_sn(item_id: int, mhz_order_id: str) -> bool:
    """[防线 4 兜底] awaiting_sync 反查到了 → 回填 mhz_order_id 并恢复到 submitted。

    幂等：如果 item 已被并发改成其他状态，安静返回 False 不报错。

    Returns: True 成功回填；False item 不在 awaiting_sync 状态。
    """
    if not item_id or not mhz_order_id:
        return False
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE mhz_publish_order_items
            SET status = 'submitted',
                mhz_order_id = %s,
                submitted_at = COALESCE(submitted_at, NOW()),
                reject_reason = NULL
            WHERE id = %s AND status = 'awaiting_sync'
            RETURNING id
        """, (mhz_order_id, item_id))
        row = c.fetchone()
        conn.commit()
        return row is not None
    except Exception as e:
        conn.rollback()
        logger.error(f"link_awaiting_sync_to_order_sn 失败 item={item_id}: {e}")
        return False
    finally:
        conn.close()


def find_awaiting_sync_items_to_check(min_age_minutes: int = 2, max_age_minutes: int = 720) -> List[Dict]:
    """[防线 4 配套] 找到需要反查的 awaiting_sync items。

    时间窗：进入状态超过 min_age_minutes（让 mhz 同步任务有时间拉到）但还没到
    max_age_minutes（超时就该转人工）。
    默认 12 小时（720 分钟）—— mhz 同步任务每 10 分钟全量拉一次，且偶尔有
    1.5 小时级别的同步延迟。给 12 小时是为了让自愈机会最大化，避免误转人工。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(f"""
            SELECT i.id, i.user_id, i.media_id, i.media_name, i.last_submit_at, i.awaiting_sync_since,
                   o.article_title
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            WHERE i.status = 'awaiting_sync'
              AND i.awaiting_sync_since <= NOW() - INTERVAL '{int(min_age_minutes)} minutes'
              AND i.awaiting_sync_since >= NOW() - INTERVAL '{int(max_age_minutes)} minutes'
              AND i.manual_review_required = FALSE
            ORDER BY i.awaiting_sync_since
            LIMIT 50
        """)
        return list(c.fetchall())
    finally:
        conn.close()


def find_awaiting_sync_items_overdue(max_age_minutes: int = 720) -> List[Dict]:
    """[防线 4 兜底] 找到超过 max_age_minutes 仍未反查到的 awaiting_sync items。

    这些进入"人工审核队列"——绝不自动退款（因为 mhz 那边可能真发出去了），
    也绝不自动重发（避免重复扣费），由管理员人工核实后处理。
    默认 12 小时（720 分钟），给 mhz 同步任务足够多次自愈机会。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(f"""
            SELECT i.id, i.user_id, i.media_id, i.media_name, i.cost_points,
                   i.awaiting_sync_since, o.article_title, o.article_id
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            WHERE i.status = 'awaiting_sync'
              AND i.awaiting_sync_since < NOW() - INTERVAL '{int(max_age_minutes)} minutes'
              AND i.manual_review_required = FALSE
            ORDER BY i.awaiting_sync_since
            LIMIT 50
        """)
        return list(c.fetchall())
    finally:
        conn.close()


def log_mhz_call(
    endpoint: str,
    method: str = "POST",
    item_id: int = None,
    user_id: int = None,
    params_summary=None,
    response_code: int = None,
    response_summary=None,
    elapsed_ms: int = None,
    ok: bool = True,
    error_msg: str = None,
) -> None:
    """[2026-04-30] 记录一次 mhz 接口调用 —— 异步打点，永远不抛异常影响主流程"""
    import json as _json
    try:
        conn = _get_conn()
        try:
            c = conn.cursor()
            try:
                params_json = _json.dumps(params_summary, ensure_ascii=False, default=str) if params_summary is not None else None
            except Exception:
                params_json = None
            try:
                resp_json = _json.dumps(response_summary, ensure_ascii=False, default=str) if response_summary is not None else None
            except Exception:
                resp_json = None
            c.execute("""
                INSERT INTO mhz_call_log
                  (endpoint, method, item_id, user_id, params_summary, response_code,
                   response_summary, elapsed_ms, ok, error_msg)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, %s)
            """, (
                endpoint, method or "POST", item_id, user_id, params_json,
                response_code, resp_json, elapsed_ms, bool(ok), (error_msg or None),
            ))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # 打点失败永远不影响业务
        try:
            logger.warning(f"log_mhz_call 失败: {e}")
        except Exception:
            pass


def cleanup_mhz_call_log(retention_days: int = 30) -> int:
    """清理 N 天前的调用日志（防表无限增长）"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        # [CTO-15.23 2026-05-13] f-string 展开 INTERVAL · 防累犯 #5b psycopg2 % operator 混用
        _rd = int(retention_days)
        c.execute(
            f"DELETE FROM mhz_call_log WHERE created_at < NOW() - INTERVAL '{_rd} days'"
        )
        deleted = c.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


def mark_item_manual_review(item_id: int) -> bool:
    """[防线 4 兜底] 标 manual_review_required=TRUE，进人工审核队列。

    不改 status，不退款。状态保持 awaiting_sync，等管理员人工处置。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE mhz_publish_order_items
            SET manual_review_required = TRUE
            WHERE id = %s AND status = 'awaiting_sync' AND manual_review_required = FALSE
            RETURNING id,user_id
        """, (item_id,))
        row = c.fetchone()
        if row:
            _enqueue_publish_notification(
                c,
                user_id=int(row["user_id"]),
                refund_key=f"item:{int(item_id)}",
                event_type="publication.manual_required",
                terminal_state="manual_required",
                status="需要人工同步",
                include_admins=True,
            )
        conn.commit()
        return row is not None
    except Exception as e:
        conn.rollback()
        logger.error(f"mark_item_manual_review 失败 item={item_id}: {e}")
        return False
    finally:
        conn.close()


# ============================================================
# [P0 代发卡单出口 2026-07-26] awaiting_sync 死胡同 —— 计次 + 人工出口
#
# 既有 12 小时链路(find_awaiting_sync_items_overdue → mark_item_manual_review)保持不动:
# 它负责"进 admin 队列"。本节负责它之后的**第二级升级**:72 小时 + 反查失败 3 次仍无解,
# 就把 item 推到 awaiting_action 并挂 §13 合同,让用户侧看得见、点得动。
#
# 为什么必须**不**过滤 manual_review_required:
#   历史卡单早在 12 小时那一关就被打了 TRUE,而两个既有扫描器的 WHERE 都是
#   `manual_review_required = FALSE` —— 打完标记那一刻它们从所有自动扫描里消失,
#   这正是"最老一条滞留 58 天"的直接原因。出口扫描若沿用同一过滤,等于新建一个死胡同。
# ============================================================


def find_awaiting_sync_items_for_exit(min_age_hours: int = 72, limit: int = 50) -> List[Dict]:
    """卡在 awaiting_sync 超 min_age_hours 的 item —— 人工出口候选。

    只认 status='awaiting_sync':终态(published/success/cancelled/withdrawn/rejected/failed)
    与已挂起(awaiting_action)天然被排除,不会把已结案的单重新捡起来。

    awaiting_sync_since 为 NULL 的行也纳入(退回用 created_at 计龄)——
    该列是后加的,早期行可能为空;两个既有扫描器都要求它非空,那些行对自动化
    **完全不可见**,是另一类永久卡死。
    """
    _h = int(min_age_hours)
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(f"""
            SELECT i.id, i.user_id, i.media_id, i.media_name, i.media_type,
                   i.cost_points, i.awaiting_sync_since, i.last_submit_at,
                   COALESCE(i.awaiting_sync_probe_attempts, 0) AS probe_attempts,
                   i.manual_review_required,
                   EXTRACT(EPOCH FROM (NOW() - COALESCE(i.awaiting_sync_since, i.created_at))) / 3600.0
                       AS awaiting_hours,
                   o.id AS order_id, o.article_title, o.article_id
            FROM mhz_publish_order_items i
            JOIN mhz_publish_orders o ON o.id = i.order_id
            WHERE i.status = 'awaiting_sync'
              AND COALESCE(i.awaiting_sync_since, i.created_at) < NOW() - INTERVAL '{_h} hours'
            ORDER BY COALESCE(i.awaiting_sync_since, i.created_at) ASC
            LIMIT {int(limit)}
        """)
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def bump_awaiting_sync_probe_attempt(item_id: int) -> int:
    """反查又失败一次 → 计数 +1,返回**新**的累计次数。

    只在 status 仍是 awaiting_sync 时计数;item 已被并发改走就返回 0(视作不再需要计次)。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE mhz_publish_order_items
               SET awaiting_sync_probe_attempts = COALESCE(awaiting_sync_probe_attempts, 0) + 1
             WHERE id = %s AND status = 'awaiting_sync'
         RETURNING awaiting_sync_probe_attempts
        """, (int(item_id),))
        row = c.fetchone()
        conn.commit()
        return int(row["awaiting_sync_probe_attempts"]) if row else 0
    except Exception as e:
        conn.rollback()
        logger.error(f"bump_awaiting_sync_probe_attempt 失败 item={item_id}: {e}")
        return 0
    finally:
        conn.close()


def open_awaiting_sync_user_exit(item_id: int, contract: dict) -> bool:
    """把救不回来的 awaiting_sync item 推到 awaiting_action + §13 合同。**绝不退款**。

    mhz 已回执"成功提交,正在执行发布",稿件有可能真发出去了 —— 自动退款 =
    既退钱又发稿,双重损失且不可追回。这里只挂起 + 给出口,动钱那一步交给
    管理员在 /admin/manual-review-queue/resolve 执行(那条路径的幂等键是唯一的 item:{id})。

    同时保留 manual_review_required=TRUE,让它在 admin 队列里仍然可见 ——
    用户和管理员任一侧动手都能结案,不依赖单一方出现。
    """
    from services.publication_awaiting_sync_exit import EXIT_CODE

    conn = _get_conn()
    try:
        c = conn.cursor()
        # 行锁内复核状态:防和 link_awaiting_sync_to_order_sn(反查刚好命中)抢同一行
        c.execute("""
            SELECT id, user_id, status FROM mhz_publish_order_items
             WHERE id = %s FOR UPDATE
        """, (int(item_id),))
        row = c.fetchone()
        if not row or row["status"] != "awaiting_sync":
            conn.rollback()
            return False

        ok = suspend_item_for_user_action(int(item_id), contract, cursor=c)
        if not ok:
            conn.rollback()
            return False

        c.execute(
            "UPDATE mhz_publish_order_items SET manual_review_required = TRUE WHERE id = %s",
            (int(item_id),),
        )
        try:
            _enqueue_publish_notification(
                c,
                user_id=int(row["user_id"]),
                refund_key=f"item:{int(item_id)}",
                event_type="publication.manual_required",
                terminal_state="awaiting_action",
                status="待你确认",
                include_admins=True,
            )
        except Exception as _notify_err:
            # 通知失败不能把出口本身回滚掉 —— 没出口比没通知严重得多
            logger.warning(f"[awaiting_sync_exit] item={item_id} 出口通知入队失败(状态已挂起): {_notify_err}")
        conn.commit()
        logger.warning(f"[awaiting_sync_exit] item={item_id} 已挂起等人工动作 code={EXIT_CODE}")
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"open_awaiting_sync_user_exit 失败 item={item_id}: {e}")
        return False
    finally:
        conn.close()


def list_user_pending_action_items(user_id: int, limit: int = 50) -> List[Dict]:
    """当前用户所有"挂起等你动一下"的发布任务，连 §13 合同一起给出去。

    没有这个读接口，`reject_contract` 就是**只写不读**的 —— 全仓没有任何用户侧
    API 会 SELECT 它，合同再完整用户也看不到，出口等于不存在。
    """
    from services.publication_content_drift import ITEM_STATUS_AWAITING_ACTION

    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT i.id AS item_id, i.order_id, i.media_name, i.media_type,
                   i.cost_points, i.status, i.reject_code, i.reject_user_message,
                   i.reject_contract, i.user_exit_claim, i.user_exit_claim_at,
                   i.awaiting_sync_since,
                   o.article_id, o.article_title
              FROM mhz_publish_order_items i
              JOIN mhz_publish_orders o ON o.id = i.order_id
             WHERE i.user_id = %s
               AND i.status = %s
             ORDER BY i.id DESC
             LIMIT %s
        """, (int(user_id), ITEM_STATUS_AWAITING_ACTION, int(limit)))
        items = []
        for r in c.fetchall():
            d = dict(r)
            for k in ("awaiting_sync_since", "user_exit_claim_at"):
                if d.get(k) and hasattr(d[k], "isoformat"):
                    d[k] = d[k].isoformat()
            items.append(d)
        return items
    finally:
        conn.close()


def record_awaiting_sync_user_claim(
    item_id: int,
    claim: str,
    publish_url: str = "",
    note: str = "",
) -> Dict:
    """记录用户在 §13 出口上的声明。**本函数不产生任何退款流水。**

    - claim='not_published' → 只落声明 + 留在 awaiting_action + 保持 admin 队列可见。
      真正退款由管理员走 admin_manual_review_resolve('mark_failed'),幂等键 item:{id}。
    - claim='published'     → 补录链接并结案(published)。用户等于主动放弃退款,
      不动钱,因此可以自助闭环。

    只接受 status='awaiting_action' 且 reject_code 是本出口码的 item ——
    否则任何人都能拿这个端点把别的状态的单子改成已发布。
    """
    from services.publication_awaiting_sync_exit import (
        EXIT_CODE, USER_CLAIM_NOT_PUBLISHED, USER_CLAIM_PUBLISHED, USER_CLAIMS,
    )
    from services.publication_content_drift import ITEM_STATUS_AWAITING_ACTION

    if claim not in USER_CLAIMS:
        return {"success": False, "message": "claim 不合法"}
    if claim == USER_CLAIM_PUBLISHED and not (publish_url or "").strip():
        return {"success": False, "message": "补录已发布必须提供稿件链接"}

    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, user_id, status, reject_code FROM mhz_publish_order_items
             WHERE id = %s FOR UPDATE
        """, (int(item_id),))
        row = c.fetchone()
        if not row:
            conn.rollback()
            return {"success": False, "message": "发布任务不存在"}
        if row["status"] != ITEM_STATUS_AWAITING_ACTION or row["reject_code"] != EXIT_CODE:
            conn.rollback()
            return {"success": False, "message": "这条发布任务当前不在待确认状态"}

        c.execute("""
            UPDATE mhz_publish_order_items
               SET user_exit_claim = %s,
                   user_exit_claim_at = NOW()
             WHERE id = %s
        """, (claim, int(item_id)))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"record_awaiting_sync_user_claim 失败 item={item_id}: {e}")
        return {"success": False, "message": "记录失败,请稍后重试"}
    finally:
        conn.close()

    if claim == USER_CLAIM_NOT_PUBLISHED:
        # 刻意什么都不动:不改 status、不退款。等管理员核实 mhz 后台后处置。
        return {
            "success": True,
            "claim": claim,
            "refunded": 0,
            "message": "已收到你的确认,我们会人工核实媒体侧是否真的没有发布,核实后按原路退回算力。",
        }

    # claim == published:补录链接 + 结案。走既有 update_order_item_status —— 它会
    # 同时落发布快照 / 交付计划 / 通知 / 订单状态重算,自己写 UPDATE 会漏掉这一整串。
    update_order_item_status(int(item_id), "published", publish_url=(publish_url or "").strip())
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE mhz_publish_order_items
               SET manual_review_required = FALSE,
                   reject_reason = %s
             WHERE id = %s
        """, (f"用户补录已发布链接结案{(' · ' + note) if note else ''}", int(item_id)))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning(f"record_awaiting_sync_user_claim 清人工标记失败 item={item_id}: {e}")
    finally:
        conn.close()

    return {
        "success": True,
        "claim": claim,
        "refunded": 0,
        "message": "已按「已发布」结案,链接已补录。",
    }


def get_publish_external_cost_statistics(
    days: Optional[int] = None,
    since=None,
) -> Dict[str, Any]:
    """媒体发布外采成本统计 · 给 Dashboard "媒体发布支出" 卡片用

    [CTO-15.23 2026-05-17 老板拍方案 A]
      Dashboard "LLM 总成本"只统计 LLM API token 成本(token_usage + llm_call_log) ·
      不含给 mhz 媒体平台付的"代发"外采成本。新增独立卡片显示 mhz 外采。

    [v1.1 2026-05-29 老板复审 P0]
      加 since 参数(支持 month_start 精确口径)· 防 dashboard_api 用 naive now() 抛 TypeError
      since 优先 · 兼容 days 参数(向后兼容老调用)

    数据源:mhz_publish_order_items
      - cost_yuan numeric · 媒体原价(我们付给 mhz 的钱)= 外采成本
      - cost_points integer · 代理付的 markup 后金额(收入侧 · 不在本统计)

    统计口径:
      - 全状态都算(published/rejected/failed/withdrawn/cancelled/...) 因为 mhz 实际是按提交收费 ·
        rejected/failed 也产生过外采尝试(可能后续退款 · 但当下确实占用了成本)
      - submitted/pending 状态算"将发生"成本

    Returns:
        {
            "total_external_cost_yuan": float,      # 外采总额(元)
            "total_external_cost_points": int,      # 折算积分(× 130)
            "total_orders": int,                    # 订单数(distinct order_id)
            "total_items": int,                     # 单品 item 数
            "status_breakdown": {status: {count, cost_yuan}},  # 按状态分布
        }
    """
    conn = _get_conn()
    try:
        c = conn.cursor()

        date_filter = "WHERE TRUE"
        params: list = []
        # v1.1 since 优先(精确口径)· 兼容老 days 参数
        if since is not None:
            # 接受 datetime(aware 或 naive 都行 · psycopg2 会处理时区)或 ISO 字符串
            if hasattr(since, "isoformat"):
                cutoff = since.isoformat()
            else:
                cutoff = str(since)
            date_filter += " AND created_at >= %s"
            params.append(cutoff)
        elif days:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            date_filter += " AND created_at >= %s"
            params.append(cutoff)

        # 总量
        c.execute(f"""
            SELECT
                COALESCE(SUM(cost_yuan), 0) AS total_yuan,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(*) AS items
            FROM mhz_publish_order_items {date_filter}
        """, params)
        row = c.fetchone()
        total_yuan = float(row["total_yuan"] or 0)

        # 状态分布
        c.execute(f"""
            SELECT status,
                   COUNT(*) AS cnt,
                   COALESCE(SUM(cost_yuan), 0) AS cost_yuan
            FROM mhz_publish_order_items {date_filter}
            GROUP BY status
            ORDER BY cnt DESC
        """, params)
        status_breakdown = {
            r["status"] or "unknown": {
                "count": int(r["cnt"] or 0),
                "cost_yuan": round(float(r["cost_yuan"] or 0), 2),
            }
            for r in c.fetchall()
        }

        return {
            "total_external_cost_yuan": round(total_yuan, 2),
            "total_external_cost_points": int(round(total_yuan * 130)),
            "total_orders": int(row["orders"] or 0),
            "total_items": int(row["items"] or 0),
            "status_breakdown": status_breakdown,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# [P2] mhz_publish_orders.status 死字段治理
# ---------------------------------------------------------------------------
# 生产实测 346/346 恒为 'pending' —— 从来没人写过它,却还挂在 JOIN 和看板上,
# 谁读谁被误导(Deploy 的资金审计就差点栽在这)。
#
# 选 roll-up 而不是"标 deprecated 再全网移除":订单级状态本身是有用的
# (列表页、审计、对账都要它),缺的只是有人去算。算法按 item 终态归并。

#: item 终态 → 订单态的归并优先级(越靠前越"未完成")
_ORDER_ACTIVE_ITEM_STATUSES = (
    "pending", "submitting", "submitted", "awaiting_sync",
    "awaiting_confirmation", "awaiting_action",
)


def recompute_order_status(order_id: int, cursor=None) -> str:
    """按 item 终态重算订单状态并落库,返回新状态。

    规则(先到先得):
      - 任一 item 仍在途            → 'processing'(有 awaiting_action 则 'awaiting_action')
      - 全部 item 成功/已发布       → 'completed'
      - 全部 item 取消/撤回         → 'cancelled'
      - 否则(存在失败/拒绝)        → 'partial' 或 'failed'
      - 没有 item                   → 'pending'(保持原语义)
    """
    own = cursor is None
    conn = _get_conn() if own else None
    c = conn.cursor() if own else cursor
    try:
        c.execute(
            "SELECT status, COUNT(*) AS n FROM mhz_publish_order_items "
            "WHERE order_id = %s GROUP BY status",
            (int(order_id),),
        )
        counts = {str(r["status"] or ""): int(r["n"]) for r in (c.fetchall() or [])}
        total = sum(counts.values())

        if total == 0:
            new_status = "pending"
        elif counts.get("awaiting_action"):
            # 挂起等人工优先暴露 —— 这正是用户需要动手的那种
            new_status = "awaiting_action"
        elif any(counts.get(s) for s in _ORDER_ACTIVE_ITEM_STATUSES):
            new_status = "processing"
        else:
            done = counts.get("success", 0) + counts.get("published", 0)
            dropped = counts.get("cancelled", 0) + counts.get("withdrawn", 0)
            if done == total:
                new_status = "completed"
            elif dropped == total:
                new_status = "cancelled"
            elif done > 0:
                new_status = "partial"
            else:
                new_status = "failed"

        c.execute(
            "UPDATE mhz_publish_orders SET status = %s, updated_at = NOW() "
            "WHERE id = %s AND status IS DISTINCT FROM %s",
            (new_status, int(order_id), new_status),
        )
        if own:
            conn.commit()
        return new_status
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
