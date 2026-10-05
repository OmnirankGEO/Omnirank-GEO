"""
积分钱包数据库模块
- user_wallets: 用户钱包（paid + bonus 双积分）
- point_transactions: 积分流水
- recharge_orders: 充值订单
- feature_pricing: 功能定价配置
- referral_links: 推荐关系
"""

import logging
from typing import Optional
from db.connection import get_connection, get_db

logger = logging.getLogger("GEO-Wallet")

INTERNAL_CUSTOMER_HIDDEN_TRANSACTION_TYPES = ("v35_migrated_to_customer_credit",)


def _format_yuan_from_cents(amount_cents: int) -> str:
    """Format cents for user-facing descriptions without dropping cent precision."""
    cents = int(amount_cents or 0)
    if cents % 100 == 0:
        return str(cents // 100)
    return f"{cents / 100:.2f}"


# ==================== 初始化 ====================

def _ensure_check_constraint(cursor, table, name, check_def):
    """幂等加 CHECK 约束:先查 pg_constraint,不存在才 ALTER。
    [2026-06-10 audit P1] 原 try/except pass 包 ADD CONSTRAINT(PG 不支持 IF NOT EXISTS),在
    非 autocommit 单事务里约束已存在会抛 duplicate_object → 事务 aborted → 后续语句全
    InFailedSqlTransaction → 整个 init 失败被 server.py 吞成一行 warning,wallet schema 静默跳过。
    它现在能跑通唯一靠 P0-5 的 autocommit 污染(每条独立提交)。P0-5 复位后单事务生效,必须
    改成"先查再加"才不炸。"""
    cursor.execute(
        "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass",
        (name, table),
    )
    if not cursor.fetchone():
        cursor.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check_def})")


def init_wallet_tables():
    """创建钱包相关表（幂等，可重复调用）"""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_wallets (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                paid_points BIGINT NOT NULL DEFAULT 0,
                bonus_points BIGINT NOT NULL DEFAULT 0,
                total_recharged BIGINT NOT NULL DEFAULT 0,
                agent_level INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS point_transactions (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                type TEXT NOT NULL,
                point_type TEXT NOT NULL,
                amount BIGINT NOT NULL,
                balance_after BIGINT NOT NULL,
                feature_code TEXT,
                description TEXT,
                order_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_pt_user_time
            ON point_transactions(user_id, created_at DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_pt_type
            ON point_transactions(type)
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recharge_orders (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                amount_cents INTEGER NOT NULL,
                base_points BIGINT NOT NULL,
                bonus_points BIGINT NOT NULL,
                payment_method TEXT,
                payment_status TEXT DEFAULT 'pending',
                payment_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feature_pricing (
                feature_code TEXT PRIMARY KEY,
                feature_name TEXT NOT NULL,
                cost_points INTEGER NOT NULL,
                cost_compute NUMERIC(6,2) NOT NULL DEFAULT 0,
                requires_paid_points BOOLEAN DEFAULT FALSE,
                is_active BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS referral_links (
                referrer_id INTEGER NOT NULL REFERENCES users(id),
                referred_id INTEGER NOT NULL REFERENCES users(id),
                level INTEGER NOT NULL DEFAULT 1,
                commission_rate NUMERIC(4,3) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (referrer_id, referred_id)
            )
        """)

        # v4: point_transactions 加 brand_id（按客户拆账）
        try:
            cursor.execute("ALTER TABLE point_transactions ADD COLUMN IF NOT EXISTS brand_id INTEGER")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pt_brand ON point_transactions(brand_id) WHERE brand_id IS NOT NULL")
        except Exception:
            pass

        # BUG-2-003: Prevent negative balances at DB level
        # [2026-06-10 audit P1] 改先查 pg_constraint 再 ALTER(P0-5 复位 autocommit 后单事务里
        # 重复 ADD CONSTRAINT 会 abort 整个 init,不能再靠污染续命)
        _ensure_check_constraint(cursor, 'user_wallets', 'check_paid_non_negative', 'paid_points >= 0')
        _ensure_check_constraint(cursor, 'user_wallets', 'check_bonus_non_negative', 'bonus_points >= 0')

        # 2026-04-18: 冻结机制（异步长任务"未完成不真扣"）
        try:
            cursor.execute("ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS frozen_points BIGINT NOT NULL DEFAULT 0")
        except Exception:
            pass
        # [2026-06-10 audit P1] 先查再加(同上 · 防 P0-5 复位后单事务 abort)
        _ensure_check_constraint(cursor, 'user_wallets', 'check_frozen_non_negative', 'frozen_points >= 0')

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS point_freezes (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                feature_code TEXT NOT NULL,
                amount_total BIGINT NOT NULL,
                amount_bonus BIGINT NOT NULL DEFAULT 0,
                amount_paid BIGINT NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'frozen',   -- frozen | committed | released
                task_ref TEXT,                             -- diagnosis_id / campaign_id / monitor_id ...
                brand_id INTEGER,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                committed_at TIMESTAMP,
                released_at TIMESTAMP,
                CONSTRAINT check_freeze_status CHECK (status IN ('frozen','committed','released'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pf_user_status ON point_freezes(user_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pf_task_ref ON point_freezes(task_ref) WHERE task_ref IS NOT NULL")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pf_feature ON point_freezes(feature_code, status)")

        # 2026-04-18 v3.4: 三轨积分分离 (commission_points + 扣费偏好)
        try:
            cursor.execute("ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS commission_points BIGINT NOT NULL DEFAULT 0")
        except Exception:
            pass
        # [2026-06-10 audit P1] 先查再加(防 P0-5 复位后单事务 abort)
        _ensure_check_constraint(cursor, 'user_wallets', 'check_commission_non_negative', 'commission_points >= 0')
        # 扣费偏好（仅 agent_level>=1 的代理可切换）
        try:
            cursor.execute("ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS deduction_preference VARCHAR(20) NOT NULL DEFAULT 'default'")
        except Exception:
            pass
        # A.9-B (CTO-15.9 session 3 · 2026-04-25 · 老板拍板"默认关 · 代理同意才扣费")
        # 自动 monitor 24h after publish 开关 · 默认 FALSE · 代理在 /wallet 设置里手动开
        # 开启后 · 代理任一文章首发 24h 后自动跑 1 次监测(扣 ¥0.29 · charge_on_success)
        try:
            cursor.execute("ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS auto_monitor_after_publish BOOLEAN NOT NULL DEFAULT FALSE")
        except Exception:
            pass
        # 全站扣费提醒偏好（P5a · 2026-06-03 · 静默扣费 + 扣完通知 + 用户可配提醒强度）
        # NULL = 用户还没设置（前端首次引导用 · 区分"默认 quiet"与"未表态"）
        # 合法值: 'each'(每次都提醒) / 'quiet'(安静一点) / 'off'(不主动提醒)
        # 仅偏好存储 · 不改任何扣费逻辑(billing.py 不动)
        try:
            cursor.execute("ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS charge_notify_level VARCHAR(20) DEFAULT NULL")
        except Exception:
            pass
        # point_freezes 加 amount_commission（与 billing.freeze_points 对齐）
        try:
            cursor.execute("ALTER TABLE point_freezes ADD COLUMN IF NOT EXISTS amount_commission BIGINT NOT NULL DEFAULT 0")
        except Exception:
            pass

        # 历史佣金搬运：把过去 type='commission' point_type='paid' 的流水金额
        # 从 paid_points 挪到 commission_points（只搬能搬的，防止负值）
        # 用幂等标记表 + 防止重跑误动
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS _migration_markers (
                marker TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                note TEXT
            )
        """)
        cursor.execute("SELECT 1 FROM _migration_markers WHERE marker = %s", ('v3_4_commission_split',))
        if not cursor.fetchone():
            # 备份表（仅保留 1 次迁移前的快照）
            try:
                cursor.execute("DROP TABLE IF EXISTS _migration_v3_4_backup")
                cursor.execute("""
                    CREATE TABLE _migration_v3_4_backup AS
                    SELECT user_id, paid_points, bonus_points,
                           (paid_points + bonus_points) AS old_total,
                           updated_at
                    FROM user_wallets
                """)
                # 搬运：每个用户累计佣金（type=commission point_type=paid amount>0）
                cursor.execute("""
                    SELECT user_id, COALESCE(SUM(amount), 0)::bigint AS total
                    FROM point_transactions
                    WHERE type = 'commission' AND point_type = 'paid' AND amount > 0
                    GROUP BY user_id
                """)
                moves = cursor.fetchall()
                for row in moves:
                    uid = row["user_id"]
                    total = int(row["total"])
                    if total <= 0:
                        continue
                    # 只搬能搬的（GREATEST 防负数）
                    cursor.execute("""
                        UPDATE user_wallets
                        SET commission_points = commission_points + LEAST(paid_points, %s),
                            paid_points = paid_points - LEAST(paid_points, %s),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = %s
                    """, (total, total, uid))

                # 守恒校验（容差 130 积分 ≈ ¥1，允许业务运行期间小差异）
                cursor.execute("""
                    SELECT COUNT(*) AS mismatched FROM user_wallets w
                    JOIN _migration_v3_4_backup b USING (user_id)
                    WHERE ABS((w.paid_points + w.commission_points + w.bonus_points)
                            - (b.paid_points + b.bonus_points)) > 130
                """)
                mis = cursor.fetchone()["mismatched"]
                if mis > 0:
                    raise RuntimeError(
                        f"[v3.4 migration] 守恒校验失败: {mis} 个用户总积分不守恒 (>130pt)，"
                        f"已回滚该迁移步骤，请人工审查 _migration_v3_4_backup 表"
                    )

                # 标记已应用（下次启动跳过）
                cursor.execute(
                    "INSERT INTO _migration_markers (marker, note) VALUES (%s, %s)",
                    ('v3_4_commission_split', f'moved {len(moves)} users, conservation OK (tolerance 130pt)'),
                )
                logger.info(f"[Wallet] v3.4 佣金搬运完成: {len(moves)} 用户，守恒校验通过")
            except RuntimeError:
                raise
            except Exception as e:
                logger.warning(f"[Wallet] v3.4 佣金搬运跳过（非关键路径）: {e}")

        try:
            from db.refund_work_order_db import init_refund_work_order_tables
            init_refund_work_order_tables(cursor)
        except Exception as e:
            logger.warning(f"[Wallet] 退款工单表初始化跳过: {e}")

        # [v5 req4] 系统内部资金操作耐久补偿工单表(article_gen 线程窗口退款失败等)
        try:
            from db.fund_recovery_db import init_fund_recovery_tables
            init_fund_recovery_tables(cursor)
        except Exception as e:
            logger.warning(f"[Wallet] 资金补偿工单表初始化跳过: {e}")

        # [v6 req2] dispute_hold 订单托管(escrow)表
        try:
            from db.dispute_escrow_db import init_dispute_escrow_tables
            init_dispute_escrow_tables(cursor)
        except Exception as e:
            logger.warning(f"[Wallet] dispute_escrow 表初始化跳过: {e}")

        logger.info("[Wallet] 钱包表初始化完成")


def seed_feature_pricing():
    """初始化功能定价（幂等，已存在的不覆盖；关键调价用下方 upsert）"""
    PRICING_DATA = [
        # GEO 板块 (12)
        # ⚠️ seed 值需和数据库实际值一致（ON CONFLICT DO NOTHING，只影响新环境初始化）
        # 最后同步时间：2026-04-11，来源：SELECT * FROM feature_pricing
        ('geo_diagnosis',     'GEO专项诊断',         650,  5.0,  False),
        ('social_diagnosis',  '社媒专项诊断',         650,  5.0,  False),
        ('full_diagnosis',    '全面诊断',           1040,  8.0,  False),
        ('report_regen',      '诊断报告重新生成',     130,  2.0,  False),
        ('quote_generate',    'GEO方案书生成',       400,  3.0,  False),
        ('brand_fill',        '品牌信息AI填充',        40,  0.31, False),  # v1.7.6.3 SSOT (2026-05-24): 30→40 (专业档)
        # CTO-15.16 round2 P0(老板拍板 方案 A)·
        # /api/profiles/ai-fill 真实扣费用此 code · 130 积分对应产品心智 "AI 一键补齐 · ¥1.0/次"
        # 不复用 brand_fill(那是 40 积分 · 品牌信息联网填充 · 见上行 SSOT · 旧注释误写 30)
        ('autofill_brand',    '客户资料 AI 补齐',     130,  1.0,  False),
        ('deep_analyze',      '品牌深度行业解析',       500,  4.5,  False),  # v3.6 CTO-15.2 2026-04-19: 260→500（5 类 raw 素材+多 AI 投票+L1/L2/L3 飞轮）成本 ¥3.85/次拓荒，复用平摊后 ¥0.3/次
        ('article_gen',       'GEO文章生成',          390,  3.0,  False),
        ('article_rewrite',   'GEO文章补发/重写',     260,  3.0,  False),
        # 🔴 [#106b · 2026-09-06] `keyword_expand` 的种子行**已删**(Owner 亲口批准)。
        #    #106 已于 2026-09-05 在生产把该行置 `is_active=FALSE`(active 66→65);
        #    删掉种子是为了让**冷建库**与生产一致 —— 否则新环境会长出一条
        #    生产上已经下架的收费项,而 `ON CONFLICT DO NOTHING` 不会把它改回去,
        #    差异会一直存在且没有任何东西报错。
        #
        #    🔴 已知副作用(不在本笔修,已单独上报):`get_feature_pricing` 对
        #    查不到的 code `raise ValueError`,而 `api/research_api.py` 的 `_bill_ctx`
        #    非社媒分支(:517-519)**没有** try/except ⇒ `POST /api/research/keyword-scan`
        #    返裸 500。这条自 #106 置 is_active=FALSE 起在生产已经成立,不是本笔造成的;
        #    先例修法见 `api/wallet_api.py:2354`(ValueError → 400),但它在另一条调用链上。
        ('report_export',     '报告导出PDF/PPTX',     260,  3.0,  False),
        ('monitor_single',    '监测单次检测',         130,  1.0,  False),
        # 🔴 [商业边界裁决 2026-08-10] `monitor_month_10`(监测月包10词条)与
        #    `rank_alert`(排名跌落预警包)**已下架,种子行删除**。
        #    Owner 裁决:OmniRank 不再出售面向终端客户的营销打包商品 ——
        #    平台只提供能力与算力,打包/组合/定价是服务商自己的生意。
        #
        #    🔴 为什么必须从这里删,光把 DB 改成 is_active=false 不够:
        #      `seed_feature_pricing()` 由 `server.py` 模块顶层调用,**每次进程启动
        #      (每次部署/重启/蓝绿切换)都重放**;而本函数的 INSERT
        #      **不写 is_active**,吃的是列默认值 —— 生产实测
        #      `feature_pricing.is_active` 的 `column_default` = **true**。
        #      所以只要种子还在,行一旦被删就会以 **is_active=true 复活**。
        #      (行存在时 `ON CONFLICT DO NOTHING` 不覆盖任何字段,故生产上
        #       改 is_active=false 不会被种子改回来 —— 两道各管一头,缺一不可。)
        #
        #    生产取证(2026-08-10 只读):两个 code 在 `point_transactions` 与
        #    `point_freezes` 里**各 0 笔**(分母 2,478 / 318 非空);按中文
        #    「月包」「预警」模糊查同样 0 笔 —— 从未卖出过。
        #    监测真实计费走 `monitoring_keyword_daily`(766 笔,最新 2026-08-10 09:04
        #    仍在跑)/ `monitor_single` / `scheduled_monitoring` 三条独立 code,零交叉。
        #
        #    ⚠️ 生产上那两行**保留不删**(软下架 is_active=false,留历史与审计)。
        #    要恢复:此处加回两行 + DB 置 is_active=true。
        # R 批 (GEO 调研自助) · 代理自助单行业调研 · 占位 0 分,实际总额走 freeze_points extra_cost 动态传
        ('geo_research_selfserve', 'GEO 单行业自助调研', 0,  0.0,  False),
        # 社媒 IP 板块
        # v1.7.6.3 P0 SSOT align (2026-05-24 老板拍板 · migration_015)· 见 PRICING_DECISIONS.md 9 档
        # 配套 migration: db/migration_015_social_pricing_ssot_align.sql
        # 之前误用 GEO 老表 04-11 align 旧值 · script_gen 实扣 ¥5 vs 用户预期 ¥0.31 = 差 16 倍
        ('topic_gen',         '选题生成',              80,  0.62, False),  # 超级/组合 80
        ('hook_gen',          '开篇钩子生成',          40,  0.31, False),  # 专业 40
        ('script_gen',        '完整脚本生成',          40,  0.31, False),  # 专业 40
        ('hook_script_combo', '开篇+文案一套',         80,  0.62, False),  # 超级/组合 80
        ('rewrite_gen',       '爆款仿写',             650,  5.0,  False),  # 保持(已对 SSOT · 长任务)
        ('profile_polish',    '档案字段润色',           5,  0.04, False),  # 轻量 5
        ('learn_viral',       '学爆款',               390,  3.0,  False),  # 拆视频短 390(原 390 不变 · 注释对齐)
        ('find_trending',     '找热点',                 0,  0.0,  False),
        ('author_breakdown',  '博主拆解',            1950, 15.0,  False),  # 拆博主短 1950(原 1040)
        ('single_video',      '单视频采集+分析',      390,  3.0,  False),  # 拆视频短 390(原 260)
        ('content_review',    '链接内容复盘',         390,  3.0,  False),  # 拆视频短 390(原 260 · 下方 upsert 同步)
        ('video_framework',   '视频框架提取',         390,  3.0,  False),  # 拆视频短 390(原 130)
        ('video_asr',         '视频ASR转录(按分钟)',  40,  0.31, False),  # 视频分钟 40/分钟(新 seed · 配套 BILLING_FALLBACK_CODE_MAP 修)
        ('web_search',        '联网搜索',              10,  0.08, False),  # v1.7.6.3.1 P0 SSOT (2026-05-24): 联网档 · Codex 审核抓到 之前漏
        ('interview_full',    'AI面试建档',             0,  0.0,  False),
        ('interview_single',  'AI面试单轮',             0,  0.0,  False),
        ('persona_card',      'AI画像卡',               0,  0.0,  False),
        ('compatibility',     '契合度报告',             0,  0.0,  False),
        ('team_portrait',     '团队画像',            1950, 10.0,  False),  # 拆博主短 1950(原 1040)
        ('corpus_text',       '语料文本录入+特征提取',   5,  0.04, False),  # 轻量 5
        ('ai_coach',          'AI教练/顾问对话',        5,  0.04, False),  # 轻量 5
        ('comment_gen',       '评论生成',               5,  0.04, False),  # 轻量 5
        ('dm_gen',            '私信话术生成',           5,  0.04, False),  # 轻量 5
        ('scenario_gen',      '场景话术生成',           5,  0.04, False),  # 轻量 5
        ('corpus_upload',     '语料上传+特征提取',      5,  0.04, False),  # 轻量 5
        ('personality_refresh', '刷新性格画像',         40,  0.31, False),  # 专业 40
        ('scheduled_monitoring', '定时监测（按次扣费）', 130, 1.0, True),  # [BUG-P2] requires_paid_points=true(监测付费服务·赠送积分不跑·与 prod 一致·口径老板拍)
        # CTO-15.23 2026-05-09 · 关键词每日监测(代理自助开通 · daily charge_on_success)
        # 定价依据: 4 引擎全跑成本 ¥0.29/次(memory feedback_use_existing_system) × 30 天 = ¥8.7/月
        # 售价 130 积分/天 = ¥1/天 = ¥30/月 · 毛利 ¥21.3/月/keyword
        ('monitoring_keyword_daily', '关键词每日监测', 130, 1.0, False),
        # AI 员工/会议
        ('meeting_start',     'AI会议启动',          1040, 10.0,  False),
        ('meeting_continue',  'AI会议继续',           650,  5.0,  False),
        ('task_route',        'AI任务路由',             5,  0.04, False),  # v1.7.6.3 SSOT (2026-05-24): 13→5 (轻量档)
        # 媒体发布 (requires_paid_points)
        ('media_publish',     '媒体一键发布',           0,  0.0,  True),
        ('media_proxy_publish', '媒体代发',             0,  0.0,  True),
    ]

    with get_db() as conn:
        cursor = conn.cursor()
        for code, name, points, compute, requires_paid in PRICING_DATA:
            cursor.execute("""
                INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (feature_code) DO NOTHING
            """, (code, name, points, compute, requires_paid))

        # v3.7 CTO-15.0 知识库审核工作流 · 强制 upsert 保证生产环境拿到最新 flat rate
        # 用 DO UPDATE 而非 DO NOTHING：后续调价只需改这一行重部署即可（PRICING_DATA seed 改价不会生效）
        # v1.7.6.3 P0 SSOT (2026-05-24 老板拍板 · migration_015): 130 → 40 (专业档)
        cursor.execute("""
            INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
            VALUES ('industry_brief_rerun', '知识库字段重跑（AI）', 40, 0.31, FALSE)
            ON CONFLICT (feature_code) DO UPDATE
            SET cost_points = EXCLUDED.cost_points,
                feature_name = EXCLUDED.feature_name,
                cost_compute = EXCLUDED.cost_compute
        """)
        # v3.6 CTO-15.2 2026-04-19: deep_analyze 调价 260→500（升级到 v3.6 5 类 raw 素材 + 多 AI 投票 + L1/L2/L3 飞轮架构）
        # 拓荒成本 ¥3.85/次（含 metaso ¥2.31 + qwen3-max+deepseek 投票 ¥1.4 + max 整合 ¥0.15）
        # 复用平摊（同行业第 N 用户命中 L1/L2 缓存）边际成本降到 ¥0.3/次
        # 100 用户分布模拟（1 拓荒+60 同品类复用+30 同行业新品类+9 新行业拓荒）平均成本 ¥2.04/次
        # 500 分定价 = ¥3.85，单次毛利 ¥1.81，月利润 ¥181/100 单
        cursor.execute("""
            INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
            VALUES ('deep_analyze', '品牌深度行业解析', 500, 4.5, FALSE)
            ON CONFLICT (feature_code) DO UPDATE
            SET cost_points = EXCLUDED.cost_points,
                feature_name = EXCLUDED.feature_name,
                cost_compute = EXCLUDED.cost_compute
        """)
        # Phase A.2 / A.3 (CTO-15.11 2026-04-28): quote 内单点改造定价
        # 客户已签 quote 后加 / 换关键词 — 不重新跑诊断 + 不重新出报价
        # 5 pts base + 5 pts × 多余词数 (append) · 5 pts flat (replace)
        # 定价依据:仅落库 + 6 层分类 + audit_log,LLM 成本 ~0.04 元/词
        cursor.execute("""
            INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
            VALUES ('quote_keyword_append', 'Quote 加关键词', 5, 0.04, FALSE)
            ON CONFLICT (feature_code) DO UPDATE
            SET cost_points = EXCLUDED.cost_points,
                feature_name = EXCLUDED.feature_name,
                cost_compute = EXCLUDED.cost_compute
        """)
        cursor.execute("""
            INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
            VALUES ('quote_keyword_replace', 'Quote 换关键词', 5, 0.04, FALSE)
            ON CONFLICT (feature_code) DO UPDATE
            SET cost_points = EXCLUDED.cost_points,
                feature_name = EXCLUDED.feature_name,
                cost_compute = EXCLUDED.cost_compute
        """)
        # SocialStudio 数据复盘 · /api/social/review/by-link 真实扣费用此 code。
        # v1.7.6.3 P0 SSOT (2026-05-24 老板拍板 · migration_015): 260 → 390 (拆视频短档)
        cursor.execute("""
            INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
            VALUES ('content_review', '链接内容复盘', 390, 3.0, FALSE)
            ON CONFLICT (feature_code) DO UPDATE
            SET cost_points = EXCLUDED.cost_points,
                feature_name = EXCLUDED.feature_name,
                cost_compute = EXCLUDED.cost_compute
        """)
        # CTO-15.23 2026-05-09 · 关键词每日监测 · 代理自助开通 + daily charge_on_success
        # upsert 防 prod 老库 ON CONFLICT DO NOTHING 跳过(seed 加的不会落 prod)
        cursor.execute("""
            INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
            VALUES ('monitoring_keyword_daily', '关键词每日监测', 130, 1.0, FALSE)
            ON CONFLICT (feature_code) DO UPDATE
            SET cost_points = EXCLUDED.cost_points,
                feature_name = EXCLUDED.feature_name,
                cost_compute = EXCLUDED.cost_compute
        """)
        # [CTO-15.23 2026-05-19] P2 silent-fail feature_code 补 seed · Agent 复查发现
        # 老代码 deduct_points("ai_suggestion") / "team_analysis" / "keyword_price" 抛 ValueError 被 try/except 吞 → 实际 0 扣
        # 补 seed 防 silent fail · 价格按现有同档 feature 估算 · 老板若要调价单独 PR
        cursor.execute("""
            INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
            VALUES ('ai_suggestion', 'AI 建议', 130, 1.0, FALSE)
            ON CONFLICT (feature_code) DO UPDATE
            SET cost_points = EXCLUDED.cost_points,
                feature_name = EXCLUDED.feature_name,
                cost_compute = EXCLUDED.cost_compute
        """)
        cursor.execute("""
            INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
            VALUES ('team_analysis', '团队分析', 260, 2.0, FALSE)
            ON CONFLICT (feature_code) DO UPDATE
            SET cost_points = EXCLUDED.cost_points,
                feature_name = EXCLUDED.feature_name,
                cost_compute = EXCLUDED.cost_compute
        """)
        cursor.execute("""
            INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
            VALUES ('keyword_price', '关键词价格查询', 13, 0.1, FALSE)
            ON CONFLICT (feature_code) DO UPDATE
            SET cost_points = EXCLUDED.cost_points,
                feature_name = EXCLUDED.feature_name,
                cost_compute = EXCLUDED.cost_compute
        """)
        logger.info(f"[Wallet] 功能定价初始化完成 ({len(PRICING_DATA)} 项 + 9 upsert)")


# ==================== 钱包 CRUD ====================

def get_or_create_wallet(user_id: int) -> dict:
    """获取用户钱包，不存在则创建"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user_wallets WHERE user_id = %s", (user_id,))
        wallet = cursor.fetchone()
        if wallet:
            return dict(wallet)

        cursor.execute("""
            INSERT INTO user_wallets (user_id)
            VALUES (%s)
            ON CONFLICT (user_id) DO NOTHING
            RETURNING *
        """, (user_id,))
        conn.commit()
        wallet = cursor.fetchone()
        if wallet:
            return dict(wallet)

        cursor.execute("SELECT * FROM user_wallets WHERE user_id = %s", (user_id,))
        return dict(cursor.fetchone())
    finally:
        conn.close()


def get_wallet_balance(user_id: int) -> dict:
    """获取余额摘要（钱包三轨 + V3.1 订阅摘要 + V3.3.1 旁路服务费）"""
    wallet = get_or_create_wallet(user_id)
    paid = wallet.get("paid_points", 0) or 0
    bonus = wallet.get("bonus_points", 0) or 0
    commission = wallet.get("commission_points", 0) or 0
    frozen = wallet.get("frozen_points", 0) or 0

    active_sub_id = None
    active_plan_id = None
    sub_expires_at = None
    sub_auto_renew = None
    pending_clawback = 0
    # V3.5 客户授权额度池:钱在 customer_agent_credit_wallets · 不在 user_wallets
    # 前端"充值算力"必须合并这里 · 否则客户看顶部=0 反馈"没到账"(2026-06-08 P0)
    empty_customer_credit = {
        "tool_credit_points": 0,
        "publish_credit_points": 0,
        "bonus_credit_points": 0,
        "total_purchased_points": 0,
        "total_consumed_points": 0,
        "agent_user_id": None,
    }
    customer_credit = None
    customer_credit_status = "unavailable"
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, plan_id, expires_at, auto_renew
                FROM user_social_subscriptions
                WHERE user_id = %s
                  AND status = 'active'
                  AND expires_at > CURRENT_TIMESTAMP
                ORDER BY expires_at DESC
                LIMIT 1
            """, (user_id,))
            row = cursor.fetchone()
            if row:
                active_sub_id = int(row["id"])
                active_plan_id = row["plan_id"]
                sub_expires_at = row["expires_at"].isoformat() if row["expires_at"] else None
                sub_auto_renew = bool(row["auto_renew"])

            cursor.execute("""
                SELECT COALESCE(SUM(amount_points), 0) AS pending
                FROM commission_clawback_pending
                WHERE beneficiary_user_id = %s
                  AND status = 'pending'
            """, (user_id,))
            cb_row = cursor.fetchone()
            pending_clawback = int(cb_row["pending"] or 0) if cb_row else 0

        finally:
            conn.close()
    except Exception as e:
        # V3.1 订阅表可能尚未 migration,余额接口必须保持可用。
        try:
            logger.warning(
                "get_wallet_balance subscription/clawback subquery failed user_id=%s: %s",
                user_id,
                e,
            )
        except Exception:
            pass

    # V3.5 客户授权额度是当前功能可用额度的 SSOT，必须和可选订阅摘要分开读取。
    # 查询成功但无绑定记录才是可信 0；查询失败必须显式 unavailable，禁止拿默认 0 冒充真实值。
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT tool_credit_points, publish_credit_points, bonus_credit_points,
                       total_purchased_points, total_consumed_points, agent_user_id
                FROM customer_agent_credit_wallets
                WHERE customer_user_id = %s
            """, (user_id,))
            cc_row = cursor.fetchone()
            customer_credit = dict(empty_customer_credit)
            if cc_row:
                customer_credit = {
                    "tool_credit_points": int(cc_row["tool_credit_points"] or 0),
                    "publish_credit_points": int(cc_row["publish_credit_points"] or 0),
                    "bonus_credit_points": int(cc_row["bonus_credit_points"] or 0),
                    "total_purchased_points": int(cc_row["total_purchased_points"] or 0),
                    "total_consumed_points": int(cc_row["total_consumed_points"] or 0),
                    "agent_user_id": int(cc_row["agent_user_id"]) if cc_row.get("agent_user_id") is not None else None,
                }
            customer_credit_status = "ready"
        finally:
            conn.close()
    except Exception as cc_exc:
        logger.warning(
            "customer_agent_credit_wallets query failed user_id=%s: %s",
            user_id,
            cc_exc,
        )

    # [V3.5 v8 P0 · 2026-06-08 老板 + Codex 复审] 算力二轨铁律:
    #   充值算力(paid + tool_credit + publish_credit + commission)→ 可换所有功能含发布
    #   赠送算力(bonus + bonus_credit)→ 只工具 · 禁发布
    # 内部拆 tool_credit_total / publish_credit_total 给前端按 feature 类型选预检池 ·
    # 不并入 paid_points_total 误用预检 → 防 V3.5 客户 publish>>tool 时"前端说够后端拒"投诉
    credit_for_totals = customer_credit or empty_customer_credit
    credit_tool = credit_for_totals["tool_credit_points"]
    credit_publish = credit_for_totals["publish_credit_points"]
    credit_bonus = credit_for_totals["bonus_credit_points"]

    return {
        "paid_points": paid,
        "commission_points": commission,
        "bonus_points": bonus,
        "frozen_points": frozen,
        "total": paid + commission + bonus,                           # 可用合计(老语义 · 不破坏)
        "total_with_frozen": paid + commission + bonus + frozen,      # 加冻结
        "agent_level": wallet.get("agent_level", 0),
        "total_recharged": wallet.get("total_recharged", 0) or 0,
        "deduction_preference": wallet.get("deduction_preference") or 'default',
        # 全站扣费提醒偏好(P5a)· 保留 NULL = 未设置(前端首次引导)· 不强制 default
        "charge_notify_level": wallet.get("charge_notify_level"),
        "active_subscription_id": active_sub_id,
        "active_plan_id": active_plan_id,
        "subscription_expires_at": sub_expires_at,
        "subscription_auto_renew": sub_auto_renew,
        "pending_clawback_points": pending_clawback,
        # ===== V3.5 客户授权额度池(2026-06-08 P0 fix · 前端合并显示)=====
        "customer_credit_status": customer_credit_status,
        "customer_credit": customer_credit,
        # ===== V3.5 v8 三池语义拆分(2026-06-08 P0 老板 + Codex 复审) =====
        # 老板拍板:充值算力(paid + tool + publish + commission)可换全功能含发布;赠送只工具
        # 前端按 feature 类型选预检池(工具 vs 发布)· 不并入 paid_points_total 误用预检
        "tool_credit_total": paid + credit_tool,         # 工具可消费子池(paid + tool_credit)
        "publish_credit_total": credit_publish,          # 发布专用子池(publish_credit · 优先用于发布)
        "bonus_points_total": bonus + credit_bonus,      # 赠送算力(工具用 · bonus 优先)
        # 钱包顶部"充值算力"展示用(=tool+publish 总和 · 仅展示 · 不可预检)
        # 老接口字段保留向后兼容 · 语义改为"充值算力总展示"
        "paid_points_total": paid + credit_tool + credit_publish,
    }


def get_feature_pricing(feature_code: str, cursor=None) -> dict:
    """查询功能定价；可复用 caller-owned transaction（旧调用保持不变）。"""
    if cursor is not None:
        cursor.execute(
            "SELECT * FROM feature_pricing WHERE feature_code = %s AND is_active = TRUE",
            (feature_code,),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"未知的功能编码: {feature_code}")
        return dict(row)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM feature_pricing WHERE feature_code = %s AND is_active = TRUE",
            (feature_code,)
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"未知的功能编码: {feature_code}")
        return dict(row)
    finally:
        conn.close()


def get_all_pricing() -> list:
    """获取全部功能定价"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM feature_pricing WHERE is_active = TRUE ORDER BY feature_code"
        )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


# V3.3.1:source 字段由 migration_007 添加 · 模块级缓存检测一次
# Codex 二审反馈:migration 未上时无条件写 source 会爆 UndefinedColumn → 钱包流水坏
_POINT_TX_HAS_SOURCE = None  # None=未检测 / True=有列 / False=无列


def _point_tx_has_source(cursor) -> bool:
    """检测 point_transactions 是否有 source 列(migration_007 后才有)· 缓存"""
    global _POINT_TX_HAS_SOURCE
    if _POINT_TX_HAS_SOURCE is not None:
        return _POINT_TX_HAS_SOURCE
    try:
        cursor.execute(
            """
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'point_transactions' AND column_name = 'source'
            """
        )
        _POINT_TX_HAS_SOURCE = cursor.fetchone() is not None
    except Exception:
        _POINT_TX_HAS_SOURCE = False
    return _POINT_TX_HAS_SOURCE


def insert_transaction(cursor, user_id: int, tx_type: str, point_type: str,
                       amount: int, balance_after: int, feature_code: str = None,
                       description: str = None, order_id: str = None, brand_id: int = None,
                       source: str = None):
    """插入一条流水记录(在事务内调用)

    V3.3.1(2026-05-12 · Codex 反馈):加 source 参数 · 默认 None(向后兼容)
    - 充值回调入口应传 source='external_cash_payment'
    - 消费扣费 source='balance_deduction'
    - 媒体外采 source='external_media_purchase'
    - 服务费转积分 source='service_fee_conversion'

    Codex 二审修复:
    - source 字段由 migration_007 添加 · 未上 prod 时无条件写 source 会爆 UndefinedColumn
    - 改:动态检测 schema · 没 source 列就走老 INSERT(不写 source)· 兼容 migration 未跑

    [GEO-R2-CAN-039 返工 2026-07-12 · additive] 追加 RETURNING id → 返回本笔流水的不可变主键。
      老调用忽略返回值零感知;deduct_points 用它产出 charge_tx_id 供精确退款按 order_id 组定位。
    """
    if _point_tx_has_source(cursor):
        cursor.execute("""
            INSERT INTO point_transactions
                (user_id, type, point_type, amount, balance_after, feature_code, description, order_id, brand_id, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (user_id, tx_type, point_type, amount, balance_after, feature_code, description, order_id, brand_id, source))
    else:
        cursor.execute("""
            INSERT INTO point_transactions
                (user_id, type, point_type, amount, balance_after, feature_code, description, order_id, brand_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (user_id, tx_type, point_type, amount, balance_after, feature_code, description, order_id, brand_id))
    _row = cursor.fetchone()
    if _row is None:
        return None
    # RealDictCursor → dict{'id':..}; 普通 cursor → tuple
    return _row["id"] if isinstance(_row, dict) else _row[0]


def _build_transaction_where(
    user_id: int,
    tx_type: str = None,
    *,
    include_internal_migrations: bool = True,
    table_alias: str = "pt",
) -> tuple[str, list]:
    prefix = f"{table_alias}." if table_alias else ""
    where = [f"{prefix}user_id = %s"]
    params = [user_id]
    if tx_type:
        where.append(f"{prefix}type = %s")
        params.append(tx_type)
    if not include_internal_migrations:
        for hidden_type in INTERNAL_CUSTOMER_HIDDEN_TRANSACTION_TYPES:
            where.append(f"{prefix}type <> %s")
            params.append(hidden_type)
    return " AND ".join(where), params


def get_transactions(
    user_id: int,
    tx_type: str = None,
    limit: int = 50,
    offset: int = 0,
    *,
    include_internal_migrations: bool = True,
) -> list:
    """查询积分流水

    [WJ-25 2026-05-31] 流水补来源:LEFT JOIN brands 取品牌名(王姐看不懂「-650 谁扣的」)。
    - brands 表客户名列实际是 `name`(非 brand_name)· 主键 `id` · 实证 db/diagnosis_db.py CREATE TABLE
    - 沿用全库惯例 `b.name AS brand_name`(profile_db/team_db/monitoring_db 等同款别名)
    - 只读 LEFT JOIN · 不改 schema · 不动扣费/金额逻辑 · brand_id 为 NULL 时 brand_name 返回 NULL(前端优雅降级)
    - include_internal_migrations 默认 True,保留后台审计全量;客户钱包 API 显式传 False 隐藏内部迁移展示。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        where_sql, params = _build_transaction_where(
            user_id,
            tx_type,
            include_internal_migrations=include_internal_migrations,
            table_alias="pt",
        )
        cursor.execute(f"""
            SELECT pt.*, b.name AS brand_name, ro.amount_cents AS recharge_amount_cents
            FROM point_transactions pt
            LEFT JOIN brands b ON pt.brand_id = b.id
            LEFT JOIN recharge_orders ro ON ro.id = pt.order_id AND pt.type = 'recharge'
            WHERE {where_sql}
            ORDER BY pt.created_at DESC LIMIT %s OFFSET %s
        """, tuple(params + [limit, offset]))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def count_transactions(
    user_id: int,
    tx_type: str = None,
    *,
    include_internal_migrations: bool = True,
) -> int:
    """Count point transactions with the same visibility filter as get_transactions."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        where_sql, params = _build_transaction_where(
            user_id,
            tx_type,
            include_internal_migrations=include_internal_migrations,
            table_alias="pt",
        )
        cursor.execute(f"""
            SELECT COUNT(*) AS cnt
            FROM point_transactions pt
            WHERE {where_sql}
        """, tuple(params))
        row = cursor.fetchone()
        return int((row or {}).get("cnt") or 0)
    finally:
        conn.close()


def get_last_consume_transaction(user_id: int, feature_code: str) -> dict | None:
    """查询最近一笔某功能的扣费记录（退费用）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM point_transactions
            WHERE user_id = %s AND feature_code = %s AND type = 'consume'
            ORDER BY created_at DESC LIMIT 1
        """, (user_id, feature_code))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ==================== 充值订单 ====================

def get_recharge_order(order_id: str) -> dict | None:
    """查询充值订单"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM recharge_orders WHERE id = %s", (order_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_recharge_order(user_id: int, order_id: str, amount_cents: int,
                          base_points: int, bonus_points: int, payment_method: str,
                          *,
                          order_type: Optional[str] = None,
                          agent_user_id: Optional[int] = None,
                          sku_template_id: Optional[int] = None,
                          override_id: Optional[int] = None,
                          binding_source: Optional[str] = None,
                          source_token: Optional[str] = None,
                          pricing_snapshot: Optional[dict] = None,
                          price_quote_id: Optional[str] = None,
                          pricing_catalog_version: Optional[str] = None,
                          idempotency_key: Optional[str] = None,
                          quote_type: Optional[str] = None,
                          expected_product_code: Optional[str] = None) -> dict:
    """创建充值订单

    [V3.5 W3 2026-05-26] 加 V3.5 路径字段(全部可选 · 不传 = 直营/legacy 兼容):
      - order_type: 'customer_recharge' / 'agent_inventory_purchase' / 'customer_recharge_direct'(未绑定)
      - agent_user_id: 客户购买代理白标 SKU 时传 · 触发 V3.5 路径
      - sku_template_id: 具体 SKU
      - override_id: [1:N 白标包 2026-06-05] 客户买代理某个具体白标算力包时传(老订单 NULL · 向后兼容)
      - binding_source / source_token: 推广来源(写 customer_agent_bindings 时用)
      - pricing_snapshot: 价格快照 JSON · 防 SKU 价格变动影响已下单

    [双价目表 SSOT 切流 2026-07-12] price_quote_id 非空时:同一事务内 lock+校验+consume 报价
    (§9.2 一报价一单 · 防重放 · expected_final == amount_cents),报价与订单原子共存亡。
    quote_type 用于报价用途绑定校验;传入即视为已切流路径(默认 None = 完全 legacy 兼容)。
    """
    import json as _json
    conn = get_connection()
    try:
        cursor = conn.cursor()
        locked_quote = None
        # Lock and validate before INSERT. The order and quote still share this one
        # transaction, while concurrent replays queue on the quote row and fail before
        # they can create a second order.
        if price_quote_id:
            from services import price_quote as _pq
            resolved_product_code = expected_product_code
            if resolved_product_code is None and sku_template_id:
                cursor.execute("SELECT template_code FROM sku_templates WHERE id = %s", (sku_template_id,))
                product_row = cursor.fetchone()
                resolved_product_code = (
                    product_row["template_code"] if isinstance(product_row, dict)
                    else (product_row[0] if product_row else None)
                )
            locked_quote = _pq.lock_and_validate(
                cursor,
                price_quote_id,
                buyer_user_id=user_id,
                quote_type=quote_type or "retail",
                expected_final_cents=amount_cents,
                expected_points=base_points,
                expected_bonus_points=bonus_points,
                expected_product_code=resolved_product_code,
                expected_catalog_version=pricing_catalog_version,
            )
            source_ref = _pq.quote_source_ref(locked_quote)
            pinned_snapshot = _pq.quote_order_pricing_snapshot(locked_quote)
            effective_route = None
            if (
                (quote_type or "retail") == "retail"
                and source_ref.get("agent_user_id") is not None
            ):
                from services.commercial_service_routing import (
                    RelationshipError,
                    RelationshipResolution,
                    resolve_commercial_relationship,
                )

                try:
                    relationship = resolve_commercial_relationship(
                        cursor, int(user_id), for_update=True,
                    )
                except RelationshipError as exc:
                    raise _pq.QuoteError("retail quote relationship unavailable") from exc
                if int(relationship.service_user_id) != int(source_ref["agent_user_id"]):
                    raise _pq.QuoteError("retail quote relationship changed")
                pinned_snapshot = dict(pinned_snapshot or {})
                resolution = relationship.resolution.value
                existing_resolution = pinned_snapshot.get("commercial_resolution")
                if existing_resolution is not None and existing_resolution != resolution:
                    raise _pq.QuoteError("retail quote commercial resolution changed")
                pinned_snapshot["commercial_resolution"] = resolution
                effective_route = {
                    "provider_user_id": relationship.service_user_id,
                    "source": (
                        "explicit_binding"
                        if relationship.resolution is RelationshipResolution.BOUND
                        else "platform_direct"
                    ),
                }
            for key, supplied in (
                ("agent_user_id", agent_user_id),
                ("sku_template_id", sku_template_id),
                ("override_id", override_id),
            ):
                pinned = source_ref.get(key)
                if (supplied is None) != (pinned is None):
                    raise _pq.QuoteError(f"报价来源 {key} 与订单不一致 · 拒绝")
                if supplied is not None and int(supplied) != int(pinned):
                    raise _pq.QuoteError(f"报价来源 {key} 与订单不一致 · 拒绝")
            if pinned_snapshot:
                if int(pinned_snapshot.get("buyer_user_id", -1)) != int(user_id):
                    raise _pq.QuoteError("报价订单快照买方与订单不一致 · 拒绝")
                pinned_agent = pinned_snapshot.get("agent_user_id")
                if (agent_user_id is None) != (pinned_agent is None):
                    raise _pq.QuoteError("报价订单快照服务商与订单不一致 · 拒绝")
                if agent_user_id is not None and int(agent_user_id) != int(pinned_agent):
                    raise _pq.QuoteError("报价订单快照服务商与订单不一致 · 拒绝")
                if (
                    (quote_type or "retail") == "retail"
                    and pinned_agent is not None
                ):
                    if effective_route is None:
                        raise _pq.QuoteError("零售报价缺少可验证商业服务路由 · 拒绝")
                    pinned_route_source = pinned_snapshot.get("commercial_service_source")
                    if pinned_route_source is None:
                        # Compatibility is one-way: old explicit-binding quotes remain
                        # payable; platform direct is never inferred for an old quote.
                        if effective_route["source"] != "explicit_binding":
                            raise _pq.QuoteError("历史报价缺少商业服务路由 · 请重新报价")
                    else:
                        if pinned_route_source not in ("explicit_binding", "platform_direct"):
                            raise _pq.QuoteError("报价订单快照商业服务路由非法 · 拒绝")
                        if pinned_route_source != effective_route["source"]:
                            raise _pq.QuoteError("报价商业服务路由已变化 · 请重新报价")
                        if binding_source != pinned_route_source:
                            raise _pq.QuoteError("订单商业服务路由与报价不一致 · 拒绝")
            if pinned_snapshot:
                pricing_snapshot = pinned_snapshot
            pricing_catalog_version = str(locked_quote["catalog_version"])

        cursor.execute("""
            INSERT INTO recharge_orders
                (id, user_id, amount_cents, base_points, bonus_points, payment_method,
                 order_type, agent_user_id, sku_template_id, override_id, binding_source, source_token,
                 pricing_snapshot_jsonb, price_quote_id, pricing_catalog_version, idempotency_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING *
        """, (
            order_id, user_id, amount_cents, base_points, bonus_points, payment_method,
            order_type, agent_user_id, sku_template_id, override_id, binding_source, source_token,
            _json.dumps(pricing_snapshot) if pricing_snapshot else None,
            price_quote_id, pricing_catalog_version, idempotency_key,
        ))
        row = dict(cursor.fetchone())
        # [SSOT 切流] 同事务原子消费报价:条件 UPDATE 失败 → 整单回滚。
        if price_quote_id:
            if not _pq.consume_quote(cursor, price_quote_id, order_id):
                raise RuntimeError("报价消费失败(可能已被使用)· 订单回滚")
        conn.commit()
        return row
    finally:
        conn.close()


def _do_record_channel_revenue(cursor, order: dict) -> None:
    """[v9 P1-2 抽取] 渠道收益记账核心(适用性判定 + resolve + record)· 【失败即抛】。

    不适用(flag 已在上层判 / 无 procurement 直属渠道报价)→ 静默 return(非失败);
    真失败(报价查询 / resolve / record 抛错)→ 向上抛,由 _record_channel_revenue_if_applicable
    的 SAVEPOINT + 耐久补偿工单兜底(不再就地 fail-open 只记日志)。
    渠道收益 = 下级实付(报价 final) − 直属上游有效成本(resolve 受益人链)· record 幂等 UNIQUE(order_id)。
    """
    pq_id = order.get("price_quote_id")
    if not pq_id:
        return
    import json as _json
    raw_snapshot = order.get("pricing_snapshot_jsonb")
    if isinstance(raw_snapshot, str):
        try:
            raw_snapshot = _json.loads(raw_snapshot)
        except (TypeError, ValueError):
            raw_snapshot = None
    snapshot = dict(raw_snapshot) if isinstance(raw_snapshot, dict) else {}

    # New orders settle exclusively from the immutable order snapshot. No catalog,
    # coefficient, flag or relationship lookup is permitted in a payment callback.
    if snapshot:
        if snapshot.get("quote_type") != "procurement" or not snapshot.get("channel_beneficiary_user_id"):
            return
        if snapshot.get("price_quote_id") != pq_id:
            raise ValueError("渠道收益快照 quote_id 与订单不一致")
        beneficiary = int(snapshot["channel_beneficiary_user_id"])
        upstream_cost_raw = snapshot.get("upstream_cost_basis_cents")
        if upstream_cost_raw is None:
            raise ValueError("渠道收益订单快照缺少上游成本")
        upstream_cost = int(upstream_cost_raw)
        buyer_paid = int(snapshot.get("buyer_paid_cents", -1))
        buyer_id = int(snapshot.get("buyer_user_id", order.get("user_id") or 0))
        if order.get("amount_cents") is not None and buyer_paid != int(order["amount_cents"]):
            raise ValueError("渠道收益快照实付与订单金额不一致")
        if order.get("user_id") is not None and buyer_id != int(order["user_id"]):
            raise ValueError("渠道收益快照买方与订单不一致")
        relationship_version = snapshot.get("channel_relationship_version")
        catalog_version = snapshot.get("catalog_version")
    else:
        # Backward compatibility for the pre-cutover quote rows already covered by
        # the existing regression suite. Even here only immutable quote scalars are
        # accepted; the removed live relationship fallback must never return.
        from services import price_quote as _pq
        legacy_quote = _pq.get_quote(pq_id, cur=cursor)
        if (
            not legacy_quote
            or legacy_quote.get("quote_type") != "procurement"
            or not legacy_quote.get("channel_beneficiary_user_id")
        ):
            return
        if legacy_quote.get("upstream_cost_basis_cents") is None:
            raise ValueError("历史渠道报价缺少已锁定上游成本")
        beneficiary = int(legacy_quote["channel_beneficiary_user_id"])
        upstream_cost = int(legacy_quote["upstream_cost_basis_cents"])
        buyer_paid = int(legacy_quote["final_price_cents"])
        buyer_id = int(legacy_quote["buyer_user_id"])
        relationship_version = legacy_quote.get("channel_relationship_version")
        catalog_version = legacy_quote.get("catalog_version")

    from services import channel_pricing as _ch
    _ch.record_channel_revenue(
        cursor, recharge_order_id=order["id"], buyer_dealer_id=buyer_id,
        beneficiary_user_id=beneficiary, upstream_cost_basis_cents=upstream_cost,
        buyer_paid_cents=buyer_paid,
        relationship_version=relationship_version,
        price_quote_id=pq_id, catalog_version=catalog_version,
    )
    logger.info("[Wallet] 渠道收益已记账 order=%s beneficiary=%s", order["id"], beneficiary)


def _record_channel_revenue_if_applicable(cursor, order: dict) -> None:
    """[审核 #3 · v9 P1-2 耐久补偿 · v10 item4 去实时 flag 门控] 下级进货订单结算时接线渠道收益台账(SPEC P0-7 闭环)。

    当订单绑定含直属渠道受益人的 procurement 报价快照时记账(同事务·幂等 UNIQUE(order_id))。
    [v10 item4] 不再按回调时刻实时 CHANNEL_PRICING_ENABLED 门控 —— 该 flag 只控新报价生成,
      已生成订单的结算义务由不可变报价快照决定,flag 后续关闭/缓存失效/读取失败均不得抹掉。

    🔴 [v9 · Deploy-CTO NO-GO P1-2] 原实现 fail-open:记账异常仅记"高危·需人工对账"日志然后【继续完成订单】,
       回调重试见订单已 paid 也不补记 → 客户/代理资金已完成但渠道收益【永久漏记】。
    修:记账失败 → SAVEPOINT 回滚(不污染主充值事务)→ 【同事务】登记耐久补偿工单(exactly-once),
       主充值仍完成(不阻断进货 · house 风格);补偿工单登记本身若失败 → 不吞,由主事务回滚重试(fail-closed 兜底)。
       scheduler fund_recovery_processor 每 1min 幂等重试 record(见 services/fund_recovery_processor._retry_channel_revenue)。
    """
    # [v10 item4] 🔴 不再按【回调时刻实时 flag】门控:CHANNEL_PRICING_ENABLED 只控【新渠道报价生成】,
    #   已生成订单(绑定不可变 procurement 报价快照)的结算义务【不受 flag 后续关闭/缓存失效/读取失败影响】。
    #   适用性完全由订单报价快照决定(_do_record_channel_revenue 内判 procurement + channel_beneficiary_user_id):
    #   无 price_quote_id / 非 procurement / 无渠道受益人 → no-op;有则必按快照结算。
    if not order.get("price_quote_id"):
        return  # 无报价绑定 → 普通零售/legacy 订单 → 本就无渠道收益
    order_id = order.get("id")
    cursor.execute("SAVEPOINT sp_chan_rev")
    try:
        _do_record_channel_revenue(cursor, order)
        cursor.execute("RELEASE SAVEPOINT sp_chan_rev")
    except Exception as _e:  # noqa: BLE001
        try:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_chan_rev")
        except Exception:
            pass
        # [v9 P1-2] 同事务耐久补偿(与订单原子共存亡)· insert 若再失败则不吞 → 主事务回滚重试(fail-closed)
        from db.fund_recovery_db import insert_recovery_order_cursor
        insert_recovery_order_cursor(
            cursor, "channel_revenue", "record", ref_key=str(order_id),
            reason="渠道收益记账失败·耐久 exactly-once 补偿", last_error=repr(_e),
            payload={"order_id": order_id},
        )
        logger.error("[Wallet][channel_revenue] 记账失败→已登记耐久补偿工单(不阻断进货) order=%s: %s", order_id, _e)


def complete_recharge(order_id: str, payment_id: str) -> dict | None:
    """
    完成充值(支付回调后调用)
    幂等:已完成的订单不重复入账

    [V3.5 工厂模式 2026-05-26] SettlementOrchestrator SSOT 主事务编排
    - 主事务内 atomic:订单 paid + user_wallets 入账 + 流水 + 代理升级 +
                      SettlementOrchestrator 三互斥路由(v35/v32/direct)
    - 老 process_recharge_commission_v3 事务外调用改为按 settlement_mode 互斥
    - settlement_mode 写回 recharge_orders 防双写
    """
    def _enqueue_recharge_terminal(
        cursor,
        order_row,
        mode: str | None,
        *,
        credited_points: int | None = None,
    ) -> None:
        """Only enqueue from a proven settlement terminal, using this transaction."""
        from datetime import datetime, timezone

        from services.notification_events import NotificationEventType, RecipientKind
        from services.notification_outbox import (
            enqueue_admin_notification_events,
            enqueue_notification_event,
        )

        terminal_at = order_row.get("paid_at") or datetime.now(timezone.utc)
        terminal_text = (
            terminal_at.isoformat(timespec="seconds")
            if hasattr(terminal_at, "isoformat")
            else str(terminal_at)
        )
        business_id = str(order_row["id"])
        user_id = int(order_row["user_id"])
        amount = f"{_format_yuan_from_cents(order_row.get('amount_cents') or 0)} 元"
        points = int(
            credited_points
            if credited_points is not None
            else int(order_row.get("base_points") or 0) + int(order_row.get("bonus_points") or 0)
        )

        if mode in {"agent_inventory_prepay", "agent_inventory_prepay_channel_tier"}:
            enqueue_notification_event(
                cursor,
                event_type=NotificationEventType.AGENT_INVENTORY_CREDITED,
                business_id=business_id,
                terminal_state="credited",
                recipient_user_id=user_id,
                recipient_kind=RecipientKind.AGENT,
                facts={
                    "business_no": business_id,
                    "amount": amount,
                    "points": f"{points:,}",
                    "status": "进货库存已到账",
                    "occurred_at": terminal_text,
                },
            )
            return

        if mode == "dispute_hold":
            facts = {
                "business_no": business_id,
                "amount": amount,
                "status": "等待人工核验",
                "occurred_at": terminal_text,
            }
            enqueue_notification_event(
                cursor,
                event_type=NotificationEventType.RECHARGE_REVIEW_REQUIRED,
                business_id=business_id,
                terminal_state="review_required",
                recipient_user_id=user_id,
                recipient_kind=RecipientKind.CUSTOMER,
                facts=facts,
            )
            enqueue_admin_notification_events(
                cursor,
                event_type=NotificationEventType.RECHARGE_REVIEW_REQUIRED,
                business_id=business_id,
                terminal_state="review_required",
                facts=facts,
            )
            return

        if mode in {
            "dealer_consumer_resale",
            "v35_inventory_settlement",
            "v35_platform_direct_settlement",
            "v32_legacy",
            "direct",
        }:
            enqueue_notification_event(
                cursor,
                event_type=NotificationEventType.RECHARGE_CREDITED,
                business_id=business_id,
                terminal_state="credited",
                recipient_user_id=user_id,
                recipient_kind=RecipientKind.CUSTOMER,
                facts={
                    "business_no": business_id,
                    "amount": amount,
                    "points": f"{points:,}",
                    "status": "充值算力已到账",
                    "occurred_at": terminal_text,
                },
            )
            return

        logger.warning(
            "[Wallet][notification] 已支付订单结算态未知，禁止推测到账 order=%s mode=%r",
            business_id,
            mode,
        )

    settlement_mode = "direct"  # 默认值 · 由 Orchestrator 写回
    with get_db() as conn:
        cursor = conn.cursor()

        # 查订单
        cursor.execute("SELECT * FROM recharge_orders WHERE id = %s FOR UPDATE", (order_id,))
        order = cursor.fetchone()
        if not order:
            return None
        if order["payment_status"] == "paid":
            # Blue/green or callback retry may encounter a committed terminal whose
            # outbox write was absent before this migration.  The catalog event key
            # makes the repair idempotent.  Unknown/NULL modes deliberately emit no
            # success claim.
            _enqueue_recharge_terminal(cursor, order, order.get("settlement_mode"))
            return dict(order)  # 幂等

        # 更新订单状态
        cursor.execute("""
            UPDATE recharge_orders
            SET payment_status = 'paid', payment_id = %s, paid_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (payment_id, order_id))

        user_id = order["user_id"]
        base = order["base_points"]
        bonus = order["bonus_points"]
        amount_cents = order["amount_cents"]

        # ============================================================
        # [V3.5 W2] 早分支:代理预付进货(agent_inventory_purchase)
        # 锁单 + 订单 paid 后立刻分流 · 不写 user_wallets / 不写 customer_credit /
        # 不写 agent_revenue_ledger / 不写 point_transactions / 不写 bindings /
        # 不进 SettlementOrchestrator · 仅写代理库存 + 流水
        # ============================================================
        order_type = order.get("order_type")
        if order_type == "agent_inventory_purchase":
            try:
                from services.agent_inventory import purchase_inventory_prepay
                from services.channel_tier import (
                    compute_purchase_bonus_projection,
                    evaluate_and_apply_tier,
                    grant_founder_first_order_bonus,
                    grant_tier_bonus,
                    grab_founder_seat,
                    is_channel_tier_enabled,
                    is_first_order,
                )
                # 与订单创建共用 per-agent 事务锁，串行化 rolling-tier 输入和支付入账。
                cursor.execute("SELECT pg_advisory_xact_lock(920714, %s)", (int(user_id),))

                # 上线后创建的订单必须消费不可变快照。只有部署前真实存在且两个快照列都为空的
                # pending 订单才继续走下方原有 legacy 语义；禁止新订单伪装 legacy。
                snapshot_raw = order.get("pricing_snapshot_jsonb")
                snapshot_version = order.get("pricing_catalog_version")
                if snapshot_raw is not None or snapshot_version is not None:
                    import json
                    snapshot = json.loads(snapshot_raw) if isinstance(snapshot_raw, str) else snapshot_raw
                    if not isinstance(snapshot, dict) or not snapshot_version:
                        raise ValueError("代理进货订单快照不完整")
                    required = {
                        "catalog_version", "option_id", "amount_cents", "base_points",
                        "discount_source", "discount_numer", "discount_denom",
                        "channel_tier_enabled", "tier_at_order", "tier_source",
                        "tier_bonus_rate_bps", "tier_bonus_points",
                        "founder_eligibility_source", "founder_seat_policy",
                        "founder_cap_snapshot", "founder_bonus_rate_bps_snapshot",
                        "founder_min_first_order_yuan_snapshot", "option_source", "reward_eligible",
                        "founder_bonus_points_if_eligible", "bonus_rate_bps", "bonus_points", "total_points",
                        "bonus_validity_months",
                        "rolling_before_yuan_snapshot", "projected_rolling_12m_yuan_snapshot",
                        "tier_override_until_snapshot", "quote_schema_version", "calculator_version",
                        "quote_fingerprint",
                    }
                    missing = sorted(required.difference(snapshot))
                    if missing:
                        raise ValueError(f"代理进货订单快照缺字段: {','.join(missing)}")
                    snap_amount = int(snapshot["amount_cents"])
                    snap_base = int(snapshot["base_points"])
                    snap_bonus = int(snapshot["bonus_points"])
                    snap_total = int(snapshot["total_points"])
                    tier_bonus_points = int(snapshot["tier_bonus_points"])
                    tier_rate_bps = int(snapshot["tier_bonus_rate_bps"])
                    founder_points = int(snapshot["founder_bonus_points_if_eligible"])
                    founder_rate_bps = int(snapshot["founder_bonus_rate_bps_snapshot"])
                    founder_cap_snapshot = int(snapshot["founder_cap_snapshot"])
                    bonus_validity_months = int(snapshot["bonus_validity_months"])
                    reward_rate_bps = int(snapshot["bonus_rate_bps"])
                    discount_numer = int(snapshot["discount_numer"])
                    discount_denom = int(snapshot["discount_denom"])
                    from decimal import Decimal, InvalidOperation
                    try:
                        founder_min_yuan = Decimal(str(snapshot["founder_min_first_order_yuan_snapshot"]))
                    except (InvalidOperation, TypeError, ValueError):
                        raise ValueError("代理进货订单创始席门槛快照非法")
                    if not founder_min_yuan.is_finite() or founder_min_yuan < 0:
                        raise ValueError("代理进货订单创始席门槛快照非法")
                    if str(snapshot["catalog_version"]) != str(snapshot_version):
                        raise ValueError("代理进货订单目录版本与快照不一致")
                    if snap_amount != int(amount_cents) or snap_base != int(base) or snap_bonus != int(bonus):
                        raise ValueError("代理进货订单金额/算力与快照不一致")
                    if snap_total != snap_base + snap_bonus:
                        raise ValueError("代理进货订单总算力快照不守恒")
                    if min(snap_amount, snap_base, snap_bonus, tier_bonus_points, founder_points) < 0:
                        raise ValueError("代理进货订单快照含负数")
                    if founder_cap_snapshot < 0:
                        raise ValueError("代理进货订单创始席上限快照非法")
                    if discount_numer <= 0 or discount_denom <= 0:
                        raise ValueError("代理进货订单折扣快照非法")
                    if snap_base != snap_amount * discount_denom // discount_numer:
                        raise ValueError("代理进货订单基础算力快照不可复算")
                    if not (0 <= tier_rate_bps <= 10000 and 0 <= founder_rate_bps <= 10000 and 0 <= reward_rate_bps <= 10000):
                        raise ValueError("代理进货订单奖励比例越界")
                    if not 1 <= bonus_validity_months <= 120:
                        raise ValueError("代理进货订单奖励有效期快照非法")
                    expected_bonus = (snap_base * reward_rate_bps + 5000) // 10000
                    if snap_bonus != expected_bonus:
                        raise ValueError("代理进货订单奖励算力快照不可复算")
                    expected_founder = (snap_base * founder_rate_bps + 5000) // 10000
                    if founder_points != expected_founder:
                        raise ValueError("代理进货订单创始席奖励快照不可复算")
                    if type(snapshot["channel_tier_enabled"]) is not bool:
                        raise ValueError("代理进货订单渠道开关快照非法")
                    channel_tier_enabled_snapshot = bool(snapshot["channel_tier_enabled"])
                    if channel_tier_enabled_snapshot and tier_bonus_points != snap_bonus:
                        raise ValueError("代理进货渠道奖励快照不一致")
                    if not channel_tier_enabled_snapshot and tier_bonus_points != 0:
                        raise ValueError("关闭渠道奖励的订单不应含渠道奖励")
                    from services.agent_inventory_pricing import compute_quote_fingerprint
                    if str(snapshot["quote_fingerprint"]) != compute_quote_fingerprint(
                        snapshot, agent_user_id=int(user_id)
                    ):
                        raise ValueError("代理进货订单报价指纹不一致")

                    _record_channel_revenue_if_applicable(cursor, dict(order))
                    purchase_inventory_prepay(
                        cursor,
                        agent_user_id=user_id,
                        paid_points=snap_base,
                        bonus_points=0 if channel_tier_enabled_snapshot else snap_bonus,
                        related_order_id=order_id,
                        description=f"代理预付进货 ¥{_format_yuan_from_cents(snap_amount)}",
                    )
                    if channel_tier_enabled_snapshot:
                        tier_state = evaluate_and_apply_tier(
                            cursor, user_id, trigger_source="purchase_event", related_order_id=order_id,
                        )
                        tier = str(snapshot["tier_at_order"] or "none")
                        tier_bonus = grant_tier_bonus(
                            cursor,
                            agent_user_id=user_id,
                            amount_cents=snap_amount,
                            tier=tier,
                            related_order_id=order_id,
                            bonus_points=tier_bonus_points,
                            bonus_rate_bps=tier_rate_bps,
                            expires_months=bonus_validity_months,
                        )
                        founder_bonus = {"granted_points": 0}
                        if is_first_order(
                            cursor, user_id, order_id,
                            amount_yuan=Decimal(snap_amount) / Decimal(100),
                            min_first_order_yuan=founder_min_yuan,
                        ):
                            seat = grab_founder_seat(
                                cursor, user_id, cap_snapshot=founder_cap_snapshot
                            )
                            if seat.get("is_founder"):
                                founder_bonus = grant_founder_first_order_bonus(
                                    cursor,
                                    agent_user_id=user_id,
                                    base_points=snap_base,
                                    related_order_id=order_id,
                                    bonus_points=founder_points,
                                    bonus_rate_bps=founder_rate_bps,
                                    expires_months=bonus_validity_months,
                                )
                        cursor.execute(
                            "UPDATE recharge_orders SET settlement_mode='agent_inventory_prepay_channel_tier' WHERE id=%s",
                            (order_id,),
                        )
                        logger.info(
                            "[Wallet] agent_inventory_purchase snapshot 完成 agent=%s paid=%s tier=%s "
                            "tier_bonus=%s founder_bonus=%s order=%s catalog=%s current_tier=%s",
                            user_id, snap_base, tier, tier_bonus.get("granted_points", 0),
                            founder_bonus.get("granted_points", 0), order_id, snapshot_version,
                            tier_state.get("new_tier"),
                        )
                        credited_points = snap_total + int(founder_bonus.get("granted_points", 0) or 0)
                    else:
                        cursor.execute(
                            "UPDATE recharge_orders SET settlement_mode='agent_inventory_prepay' WHERE id=%s",
                            (order_id,),
                        )
                        logger.info(
                            "[Wallet] agent_inventory_purchase snapshot 完成 agent=%s paid=%s bonus=%s "
                            "order=%s catalog=%s",
                            user_id, snap_base, snap_bonus, order_id, snapshot_version,
                        )
                        credited_points = snap_total
                    _enqueue_recharge_terminal(
                        cursor,
                        dict(order),
                        "agent_inventory_prepay_channel_tier" if channel_tier_enabled_snapshot else "agent_inventory_prepay",
                        credited_points=credited_points,
                    )
                    return dict(order)

                # legacy 只认 activation 持锁时固化的显式 allowlist。不得再用 created_at
                # 比 marker：CURRENT_TIMESTAMP 是事务开始时间，无法证明 INSERT 发生代际。
                cursor.execute(
                    "SELECT value FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT' LIMIT 1"
                )
                cutover_row = cursor.fetchone()
                cutover_value = (
                    cutover_row.get("value") if isinstance(cutover_row, dict)
                    else (cutover_row[0] if cutover_row else None)
                )
                writer_generation = order.get("agent_inventory_writer_generation")
                if not cutover_value:
                    # Phase A 兼容双读：新 binary 已切流、旧 writer 尚在排空时，
                    # 允许 expansion 前/旧 writer 产生的“无代际”空快照订单继续 legacy 结算。
                    # 新 writer 必须显式写 generation=2；若它却缺快照，即使 marker
                    # 尚未激活也 fail-closed，不得伪装 legacy。
                    if writer_generation is not None or order.get("agent_inventory_legacy_eligible") is True:
                        raise ValueError("代理进货 Phase A 异常订单缺少不可变快照")
                elif (
                    order.get("agent_inventory_legacy_eligible") is not True
                    or int(writer_generation or 0) != 1
                ):
                    raise ValueError("部署后代理进货订单缺少不可变快照")

                logger.warning(
                    "[Wallet] legacy agent_inventory_purchase 无快照结算 · order=%s agent=%s phase=%s",
                    order_id, user_id, "post_activation_allowlist" if cutover_value else "phase_a_dual_read",
                )

                # [审核 #3] 渠道分级收益接线(CHANNEL 开启且订单绑定直属渠道进货报价时)· 同事务·幂等
                _record_channel_revenue_if_applicable(cursor, dict(order))

                channel_tier_enabled = is_channel_tier_enabled(cursor)
                if channel_tier_enabled:
                    purchase_inventory_prepay(
                        cursor,
                        agent_user_id=user_id,
                        paid_points=base,
                        bonus_points=0,
                        related_order_id=order_id,
                        description=f"代理预付进货 ¥{_format_yuan_from_cents(amount_cents)}",
                    )
                    projection = compute_purchase_bonus_projection(
                        cursor,
                        int(user_id),
                        int(amount_cents or 0),
                        exclude_order_id=order_id,
                    )
                    tier_state = evaluate_and_apply_tier(
                        cursor,
                        user_id,
                        trigger_source="purchase_event",
                        related_order_id=order_id,
                    )
                    tier = projection.get("projected_tier") or tier_state.get("new_tier") or tier_state.get("channel_tier") or "none"
                    actual_tier = tier_state.get("new_tier") or tier_state.get("channel_tier") or "none"
                    if actual_tier != tier:
                        logger.warning(
                            "[Wallet] channel tier projection mismatch · agent=%s order=%s projected=%s actual=%s",
                            user_id,
                            order_id,
                            tier,
                            actual_tier,
                        )
                    tier_bonus = grant_tier_bonus(
                        cursor,
                        agent_user_id=user_id,
                        amount_cents=amount_cents,
                        tier=tier,
                        related_order_id=order_id,
                        bonus_points=int(projection.get("bonus_points") or 0),
                    )
                    founder_bonus = {"granted_points": 0}
                    if is_first_order(cursor, user_id, order_id, amount_cents / 100):
                        seat = grab_founder_seat(cursor, user_id)
                        if seat.get("is_founder"):
                            founder_bonus = grant_founder_first_order_bonus(
                                cursor,
                                agent_user_id=user_id,
                                base_points=base,
                                related_order_id=order_id,
                            )
                    cursor.execute("""
                        UPDATE recharge_orders
                        SET settlement_mode = 'agent_inventory_prepay_channel_tier'
                        WHERE id = %s
                    """, (order_id,))
                    logger.info(
                        f"[Wallet] agent_inventory_purchase 渠道激励分支完成 · agent={user_id} "
                        f"paid={base} tier={tier} tier_bonus={tier_bonus.get('granted_points', 0)} "
                        f"founder_bonus={founder_bonus.get('granted_points', 0)} order={order_id}"
                    )
                    _enqueue_recharge_terminal(
                        cursor,
                        dict(order),
                        "agent_inventory_prepay_channel_tier",
                        credited_points=(
                            int(base or 0)
                            + int(tier_bonus.get("granted_points", 0) or 0)
                            + int(founder_bonus.get("granted_points", 0) or 0)
                        ),
                    )
                    return dict(order)

                purchase_inventory_prepay(
                    cursor,
                    agent_user_id=user_id,
                    paid_points=base,
                    bonus_points=bonus,
                    related_order_id=order_id,
                    description=f"代理预付进货 ¥{_format_yuan_from_cents(amount_cents)}",
                )
                cursor.execute("""
                    UPDATE recharge_orders
                    SET settlement_mode = 'agent_inventory_prepay'
                    WHERE id = %s
                """, (order_id,))
                logger.info(
                    f"[Wallet] agent_inventory_purchase 早分支完成 · agent={user_id} "
                    f"paid={base} bonus={bonus} order={order_id}"
                )
                _enqueue_recharge_terminal(
                    cursor,
                    dict(order),
                    "agent_inventory_prepay",
                    credited_points=int(base or 0) + int(bonus or 0),
                )
                return dict(order)
            except Exception:
                logger.exception(
                    f"[Wallet] 代理预付进货失败 agent={user_id} order={order_id}"
                )
                raise

        # 入账(user_wallets 平台 SSOT 不变 · 底层积分仍按此扣)
        cursor.execute("""
            UPDATE user_wallets
            SET paid_points = paid_points + %s,
                bonus_points = bonus_points + %s,
                total_recharged = total_recharged + %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
            RETURNING paid_points, bonus_points, total_recharged
        """, (base, bonus, amount_cents, user_id))
        wallet = cursor.fetchone()

        # 写流水(V3.3.1:充值入账 source='external_cash_payment' · §3.6 防套利核心)
        insert_transaction(cursor, user_id, "recharge", "paid", base,
                          wallet["paid_points"], order_id=order_id,
                          description=f"充值 ¥{_format_yuan_from_cents(amount_cents)}",
                          source="external_cash_payment")
        if bonus > 0:
            insert_transaction(cursor, user_id, "recharge", "bonus", bonus,
                              wallet["bonus_points"], order_id=order_id,
                              description=f"充值赠送 {bonus} 积分",
                              source="external_cash_payment")

        # 检查代理升级
        _check_agent_upgrade(cursor, user_id, wallet["total_recharged"])

        # Commercial relationships are administrator-managed facts. A payment
        # callback must never create/rebuild one from order, referral, or invite
        # metadata. The signed pricing snapshot and current binding are checked
        # fail-closed by SettlementOrchestrator below.
        pricing_snapshot = None
        if order.get("pricing_snapshot_jsonb"):
            import json as _json
            snap = order["pricing_snapshot_jsonb"]
            pricing_snapshot = _json.loads(snap) if isinstance(snap, str) else snap

        # ====== V3.5 SettlementOrchestrator 主事务内路由(Codex r1 P0)======
        # 互斥:v35_inventory_settlement / v35_platform_direct_settlement /
        # dispute_hold / direct
        # 防双写老分润 · settlement_mode 写回 recharge_orders
        try:
            from services.settlement_orchestrator import SettlementOrchestrator
            orchestrator = SettlementOrchestrator()
            order_dict = dict(order)
            order_dict["amount_cents"] = amount_cents
            order_dict["base_points"] = base
            order_dict["bonus_points"] = bonus
            settlement_mode = orchestrator.route(
                cursor=cursor,
                order=order_dict,
                user={"user_id": user_id, "id": user_id},
                pricing_snapshot=pricing_snapshot,
            )
        except Exception as e:
            # Orchestrator 失败必须冒泡 · 触发主事务 rollback · 防 race
            # 因为 settlement 跟 wallet 入账同事务 · 任一失败回滚保 atomic
            logger.exception(
                f"[Wallet] SettlementOrchestrator 失败 order={order_id} user={user_id}: {e}"
            )
            raise

        _enqueue_recharge_terminal(
            cursor,
            dict(order),
            settlement_mode,
        )

    # ====== 事务外 · V3.2 老分润 fallback(仅 settlement_mode='v32_legacy')======
    # Codex r1 P0:同一订单只能走旧分润或工厂差价 · 不双写
    # Orchestrator 已写 settlement_mode='v32_legacy' · 此处仅在该模式才调老逻辑
    # 老 process_recharge_commission_v3 自开 conn + 自 commit · 保持原 race 兼容
    if settlement_mode == "v32_legacy":
        try:
            from api.referral_api import process_recharge_commission_v3
            process_recharge_commission_v3(
                user_id, amount_cents, order_id,
                order_extras={
                    "source": "external_cash_payment",
                    "order_type": "recharge",
                },
            )
        except Exception as e:
            logger.error(
                f"[Wallet] v3.2 老分润计算失败(过渡期 fallback)user={user_id} order={order_id}: {e}",
                exc_info=True
            )

    return dict(order)


# ==================== 代理升级 ====================

# v3.1 legacy: 充值额阈值（保留向下兼容，新用户不再触发）
AGENT_THRESHOLDS = {
    1: 5000,    # ¥50 → L1 分销权
    2: 200000,  # ¥2000 → L2 交付权
}

# v3.2: 消费额阈值（3 选 1 触发升级）
AGENT_THRESHOLDS_V32_CONSUMED = {
    1: 50000,     # L1: 累计消费 ≥ ¥500 (50000 分 = 50000/130 约 ¥384，调整为积分数 65000≈¥500)
    2: 200000,    # L2: 累计消费 ≥ ¥2000 (200000 分 ≈ ¥1538；若按 ¥2000 = 260000 分)
}
# 精确值：
#   L1 消费 ¥500  = 500 × 130 = 65,000 积分
#   L2 消费 ¥2000 = 2000 × 130 = 260,000 积分
AGENT_THRESHOLDS_V32_POINTS = {
    1: 130000,    # L1: 累计消费 ≥ ¥1000 (1000 × 130 = 130,000 积分)
    2: 260000,    # L2: 累计消费 ≥ ¥2000 (不变)
}

# v3.2: 单次充值阈值（快捷升级路径）
# [CTO-15.3 2026-04-20] 老板拍板: 从原注释¥2000(实际¥1538)对齐为 ¥1999
#   1999 × 130 = 259,870 积分
AGENT_SINGLE_CHARGE_THRESHOLDS = {
    1: 259870,    # L1: 单次充值 ≥ ¥1999
}

# v3.2: 推荐付费人数阈值
AGENT_PAID_REFERRAL_THRESHOLDS = {
    1: 10,        # L1: 推荐 10 个付费用户
}


def _check_agent_upgrade(cursor, user_id: int, total_recharged: int):
    """充值后检查是否达到代理升级条件（v3.1 legacy + v3.2 规则混合 + v1.1 审核制 flag 双轨）

    v1.1 审核制双轨:
      - PARTNER_APPLY_ENABLED=true（flag on）→ L0 不再自动升级 L1，必须走 agent_applications.status='approved'
      - PARTNER_APPLY_ENABLED=false（flag off）→ 保留下述老逻辑（部署零感知）

    升级触发（flag off 时）:
      L1:
        - v3.1: 累计充值 ≥ ¥50（保留）
        - v3.2: 累计消费 ≥ ¥1000（原 ¥500 → 用户要求改 ¥1000）
        - v3.2: 单次充值 ≥ ¥1999（CTO-15.3 2026-04-20 老板拍板 · 原 ¥2000 实际¥1538 → 对齐 ¥1999）
        - v3.2: 直推付费用户 ≥ 10（原 5 → 用户要求改 10）
      L2（flag 无关，已实名代理内部分级）:
        - v3.1: 累计充值 ≥ ¥2000（保留）
        - v3.2: 累计消费 ≥ ¥2000
    """
    import os
    partner_apply_enabled = os.getenv("PARTNER_APPLY_ENABLED", "false").lower() == "true"

    # 2026-04-26 P1-2 修复(Codex 复验补丁):
    # 1) 只统计 paid 真金白银消耗 · 不把 bonus / commission 消耗计入升级阈值
    #    防止 admin 注入 bonus / 营销活动赠送被薅羊毛者刷成代理
    # 2) consume 类型 amount 存负数 · 改为 SUM(ABS(amount)) 让正阈值比较真生效
    #    (原 SUM(amount) 永远负数 vs 阈值 130000 正数 → 旧 v3.2 升级路径从未触发)
    cursor.execute("""
        SELECT agent_level,
               COALESCE((
                 SELECT SUM(ABS(amount))::bigint FROM point_transactions
                 WHERE user_id = %s AND type = 'consume' AND point_type = 'paid'
               ), 0) AS total_consumed_points
        FROM user_wallets WHERE user_id = %s
    """, (user_id, user_id))
    row = cursor.fetchone()
    if not row:
        return

    current_level = row["agent_level"]
    total_consumed_points = row["total_consumed_points"] or 0

    # [D4/B1/C1 拍板 2026-06-04] 消费自动升级 L0→L1 已【取消】(老板:被邀请客户保持 agent_level=0·看价随上级服务商系数)
    #   原"累计充值≥¥50 / 累计消费≥¥1000 / 直推10付费 自动升 L1"三条路径对 L0 全部失效·不依赖 PARTNER_APPLY_ENABLED
    #   全局 kill-switch:AGENT_AUTO_UPGRADE_L0_ENABLED=true 可回滚恢复老逻辑(C2·默认 false=取消·现注册都是自己人)
    #   连带价值:关闭自动升级后 agent_level 重新成为"客户(0) vs 服务商(>=1)"可靠信号(供 D4 系数继承判定)
    #   L1→L2 已实名代理内部分级不受影响(current_level>=1 继续走下方升级计算)
    auto_upgrade_l0_enabled = os.getenv("AGENT_AUTO_UPGRADE_L0_ENABLED", "false").lower() == "true"
    if current_level == 0 and not auto_upgrade_l0_enabled:
        logger.info(f"[Wallet] 用户 {user_id} L0 消费自动升级已取消(D4/C1·保持 L0 随上级系数)·跳过")
        return

    # (仅当 kill-switch 回滚开启 L0 升级时)v1.1 审核制保护仍生效:flag on 且 L0 → 走审核制
    if partner_apply_enabled and current_level == 0:
        logger.info(
            f"[Wallet] 用户 {user_id} L0 - PARTNER_APPLY_ENABLED=true，跳过自动升级 L0→L1 (走审核制)"
        )
        return

    new_level = current_level

    # v3.1 legacy: 按充值额（保留向下兼容）
    for level, threshold in sorted(AGENT_THRESHOLDS.items()):
        if total_recharged >= threshold and level > new_level:
            new_level = level

    # v3.2: 按消费额
    for level, threshold in sorted(AGENT_THRESHOLDS_V32_POINTS.items()):
        if total_consumed_points >= threshold and level > new_level:
            new_level = level

    # v3.2: 按直推付费用户数（L1 专用）
    if current_level < 1:
        cursor.execute("""
            SELECT COUNT(DISTINCT rl.referred_id) AS paid_referrals
            FROM referral_links rl
            JOIN recharge_orders ro ON ro.user_id = rl.referred_id
            WHERE rl.referrer_id = %s
              AND rl.level = 1
              AND ro.payment_status = 'paid'
        """, (user_id,))
        refs_row = cursor.fetchone()
        if refs_row and (refs_row["paid_referrals"] or 0) >= AGENT_PAID_REFERRAL_THRESHOLDS[1]:
            new_level = max(new_level, 1)

    if new_level > current_level:
        if current_level == 0:
            cursor.execute("""
                UPDATE user_wallets
                SET agent_level = %s, deduction_preference = 'agent_friendly', updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s
            """, (new_level, user_id))
        else:
            cursor.execute("""
                UPDATE user_wallets SET agent_level = %s, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s
            """, (new_level, user_id))
        logger.info(f"[Wallet] 用户 {user_id} 代理升级: L{current_level} → L{new_level}")


# ==================== 体验包 ====================

def grant_trial_bonus(user_id: int, amount: int = 3888):
    """注册时发放体验包（幂等）"""
    with get_db() as conn:
        cursor = conn.cursor()

        # 检查是否已发放
        cursor.execute("""
            SELECT 1 FROM point_transactions
            WHERE user_id = %s AND type = 'bonus' AND description = '注册体验包'
            LIMIT 1
        """, (user_id,))
        if cursor.fetchone():
            return  # 已发放

        cursor.execute("""
            UPDATE user_wallets
            SET bonus_points = bonus_points + %s, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
            RETURNING bonus_points
        """, (amount, user_id))
        result = cursor.fetchone()
        if result:
            insert_transaction(cursor, user_id, "bonus", "bonus", amount,
                              result["bonus_points"], description="注册体验包")
            logger.info(f"[Wallet] 用户 {user_id} 获得体验包 {amount} 积分")
