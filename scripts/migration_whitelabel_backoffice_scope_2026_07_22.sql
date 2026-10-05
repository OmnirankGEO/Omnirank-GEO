-- ============================================================================
-- 板块 C · 白标作用域纠正（2026-07-22）
-- 合同: docs/AI-CONTEXT/BUILDER_CTO_NEXT_QUALITY_BATCH_DEBUG_PLAN_2026-07-22.md §4
-- 裁决: 同合同 §13.2 Review-CTO D1/D2/D3（Owner 2026-07-22 新裁决，优先级最高）
--
--   D1: 客户触达页面白标服务商自设免审批；必须校验+审计+版本化；
--       后台换肤需独立 backoffice_brand 授权。
--   D2: 「仅客户页面」只映射 customer-facing surfaces；「含后台品牌」或明确
--       backoffice entitlement 保留后台换肤；空值/旧脏值/无法证明档位 → 内部
--       fail-closed 显示 OmniRank；ID 132 固定 customer-only；禁止无差别收回
--       真正已授权的后台品牌（mode=oem + status=active + unlocked_by_admin=TRUE
--       的既有授权映射为 backoffice 有效，不得一律清零）。
--   D3: 正常到期/撤权 → 历史快照保留签发时冻结 Logo；emergency suspension
--       （whitelabel_status='suspended'）→ 展示层立即压制回落平台标识。
--
-- 幂等: 全部 IF NOT EXISTS / 可连跑两次；UPDATE 为重放安全（同值重写）。
-- 红线: 不改旧列定义、不 DROP、不 TRUNCATE、不动快照数据（agent_quotes.whitelabel）。
-- 对象引用: 全部 public.* 精确限定（R6 复审 P2）——诱饵 search_path 无法把
--   建表/加列/映射重定向到其他 schema。
--
-- [统一 R3 §七 · 2026-07-23]
--   锁: forward 与 rollback 使用同一事务级咨询锁（key=2026072201）+ 有界
--       lock_timeout（5s）。forward×rollback 并发被序列化；等锁超 5s 即以
--       55P03 lock_not_available 整体中止（本事务无任何对象落库，可安全重跑），
--       绝不无限等也不半态交错。
--   单一 readiness 合同: schema 校验口径只有一份 —— §6 创建的 DB 函数
--       public.whitelabel_backoffice_schema_blockers(boolean)。
--       §7 自验块 / scripts/prestart.py / api/referral_api.py 启动自检 /
--       scripts/verify_unified_release_readiness.py 四处调同一个函数
--       （薄调用方 services/whitelabel_backoffice_schema_contract.py 不含口径）。
-- ============================================================================

BEGIN;
SET LOCAL search_path = pg_catalog, public;
-- [统一 R3 §七] 有界锁等待：本事务内一切锁（含咨询锁）最多等 5s，超时整体中止。
SET LOCAL lock_timeout = '5000';
-- [统一 R3 §七] forward/rollback 同一事务级咨询锁：序列化 forward×rollback、
-- forward×forward、rollback×rollback；同事务重入为 no-op。
SELECT pg_catalog.pg_advisory_xact_lock(2026072201);

-- ── 1. whitelabel_settings 新列（D1 版本化 + D2 backoffice 独立授权）─────────
-- [R3 fix-of-fix] fresh 空库守卫：prestart 迁移早于运行时 init_referral_tables 建表；
-- 基表缺失时跳过 §1/§3/§4，由运行时 init_referral_tables + _WHITELABEL_BACKOFFICE_COLUMNS
-- 兜底收敛（25 列契约一致）。生产库表已存在不受影响。
DO $$
BEGIN
    IF to_regclass('public.whitelabel_settings') IS NULL THEN
        RAISE NOTICE 'whitelabel_settings 不存在（fresh 环境）：跳过 §1 列级迁移，由运行时启动兜底收敛';
        RETURN;
    END IF;
    -- backoffice_brand_unlocked: 后台换肤独立授权位（只能 admin 写 · 与 mode 解耦）
    ALTER TABLE public.whitelabel_settings
        ADD COLUMN IF NOT EXISTS backoffice_brand_unlocked BOOLEAN NOT NULL DEFAULT FALSE;
    -- admin 授权痕迹（审计表另有全量历史；此处为当前态快照）
    ALTER TABLE public.whitelabel_settings
        ADD COLUMN IF NOT EXISTS backoffice_brand_granted_by INTEGER;
    ALTER TABLE public.whitelabel_settings
        ADD COLUMN IF NOT EXISTS backoffice_brand_granted_at TIMESTAMPTZ;
    -- brand_version: 品牌有效载荷版本号（D1 版本化）。
    -- 任何品牌字段/授权位变更 +1；缓存键 (owner, surface, brand_version) 的失效维度。
    ALTER TABLE public.whitelabel_settings
        ADD COLUMN IF NOT EXISTS brand_version INTEGER NOT NULL DEFAULT 1;
END $$;

-- ── 2. whitelabel_audit（D1 审计 · append-only）─────────────────────────────
-- ⚠️ APPEND-ONLY：本表禁止 UPDATE / DELETE（无 updated_at 列；任何更正以新行追加）。
--    部署侧如需强制，可 REVOKE UPDATE, DELETE ON whitelabel_audit FROM <app_role>;
--    应用代码只允许 INSERT / SELECT。
CREATE TABLE IF NOT EXISTS public.whitelabel_audit (
    id             BIGSERIAL PRIMARY KEY,
    user_id        INTEGER NOT NULL,                 -- 被变更的白标属主（代理）
    actor_user_id  INTEGER,                          -- 操作者（代理本人或 admin）
    actor_role     TEXT NOT NULL DEFAULT 'agent',    -- agent | admin | system
    field          TEXT NOT NULL,                    -- 变更字段名（company_name / whitelabel_status / backoffice_brand_unlocked ...）
    old_value      TEXT,                             -- 变更前（统一转 TEXT；NULL 保持 NULL）
    new_value      TEXT,                             -- 变更后
    request_id     TEXT,                             -- X-Request-ID 或服务端生成
    ip             TEXT,                             -- X-Forwarded-For 首跳 / request.client.host
    reason         TEXT,                             -- 操作理由（admin 授权/暂停时填写）
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- @index-guard idx_whitelabel_audit_user_created ON whitelabel_audit plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_whitelabel_audit_user_created' AND i.indrelid = to_regclass('public.whitelabel_audit')) THEN
        NULL;  -- 已在 public.whitelabel_audit 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_whitelabel_audit_user_created' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_whitelabel_audit_user_created 已存在但不在 public.whitelabel_audit 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_whitelabel_audit_user_created' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_whitelabel_audit_user_created ON public.whitelabel_audit (user_id, created_at DESC);
    END IF;
END $idxguard$;

-- [R7 · 复审 P2 → R8 补强 → 统一 R3 §七 收敛] 索引契约 fail-closed：
-- CREATE INDEX IF NOT EXISTS 遇其他表占用同名索引时会静默跳过（"假成功"），
-- 且仅比较定义文字无法识别 indisvalid/indisready/indislive=false 的半成品索引。
-- 原内联自验 DO 块已收敛进 §6 单一合同函数（归属/列序/定义/三状态同一口径），
-- 由 §7 自验块统一调用；本处不再保留第二份口径。

-- ── 3/4. D2 既有 oem 授权映射 + ID 132 固定 customer-only（fresh 守卫同 §1）───
-- 只映射"可证明档位"的三条件同时成立者；空值/旧脏值（mode 非 oem、status 非 active、
-- unlocked_by_admin 非 TRUE）一律保持 fail-closed 默认 FALSE，绝不猜测授权。
-- fresh 空库无 oem 用户，跳过无害；基表由运行时兜底建齐。
DO $$
BEGIN
    IF to_regclass('public.whitelabel_settings') IS NULL THEN
        RAISE NOTICE 'whitelabel_settings 不存在（fresh 环境）：跳过 §3/§4 数据映射，由运行时启动兜底收敛';
        RETURN;
    END IF;
    -- §3 D2 既有 oem 授权映射（保留真正已授权者 · 不无差别收回）
    UPDATE public.whitelabel_settings
    SET backoffice_brand_unlocked  = TRUE,
        backoffice_brand_granted_by = COALESCE(backoffice_brand_granted_by, approved_by),
        backoffice_brand_granted_at = COALESCE(backoffice_brand_granted_at, approved_at, updated_at, NOW())
    WHERE whitelabel_mode = 'oem'
      AND whitelabel_status = 'active'
      AND unlocked_by_admin IS TRUE;

    -- §4 D2 · ID 132 固定 customer-only
    -- admin-desktop.har 132 处实证账号：无论既有档位如何，后台换肤强制关闭
    -- （其客户触达页面白标不受本行影响 · D1 自设免审批保持）。
    --
    --
    -- ══════════════════════════════════════════════════════════════════
    -- 🔴 [客户反馈④ 2026-08-09] 这一行**故意保持原样**,读之前先看这段
    -- ══════════════════════════════════════════════════════════════════
    -- 取证(生产快照只读):
    --   · `whitelabel_audit` id=21 —— 2026-07-31 19:57:43,admin(user 1) 把 132 的
    --     `backoffice_brand_unlocked` 从 False 改成 **True**(append-only,改不了);
    --   · 今天库里 132 仍是 FALSE,而 `backoffice_brand_granted_by=1` /
    --     `backoffice_brand_granted_at=2026-07-31 19:57:43` 两个授权戳**还留着**;
    --   · 审计里**没有**把它改回 False 的记录 —— 应用侧每次写都留痕,所以不是人改的。
    --   → 改回去的就是本行:它在 `db/migration_manifest.py` 里,而 `scripts/prestart.py`
    --     每次部署全量重跑清单。**工单说的"管理员漏点第三个开关"不成立 —— 他点了。**
    --
    -- 那要不要给本行加"别覆盖 admin 决定"的守卫?**不要**,理由是同一份合同的另一半:
    --   §6 的 `whitelabel_backoffice_schema_blockers()` 把
    --   "132 的 backoffice_brand_unlocked 为 TRUE" 定义成一条 **blocker**,
    --   而 prestart / 运行时启动自检 / release readiness 三处都读它、fail-closed。
    --   → 132 固定 customer-only 是 D2 裁决的**不变式**,不是忘了拨的开关;
    --     真放开它,下一次部署会被自己的 readiness 合同挡住(比静默重置更糟)。
    --
    -- 所以本次只做**不改变授权语义**的那一半:堵住"管理员能点、点了没用、还没人告诉他"
    -- 这条静默路径 —— 见 `api/referral_api.py` 的 PIN 守卫(写入侧当场 400 说清楚)
    -- 与管理页对该账号的置灰。要不要真放开 132,是裁决变更,归 Owner。
    UPDATE public.whitelabel_settings
    SET backoffice_brand_unlocked = FALSE
    WHERE user_id = 132;
END $$;

-- ── 5. whitelabel_audit append-only DB 层强制（R3 · 对标 monitoring 既有模式）─────
-- 应用层约定（只 INSERT/SELECT）之外，DB 触发器硬强制：任何 UPDATE/DELETE 直接
-- RAISE EXCEPTION。镜像 migration_monitoring_identity_review_2026_07_21.sql 的
-- reject_monitoring_identity_event_mutation 模式；CREATE OR REPLACE + DROP IF EXISTS
-- 幂等，可连跑。
CREATE OR REPLACE FUNCTION public.trg_whitelabel_audit_append_only()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'whitelabel_audit is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_whitelabel_audit_append_only ON public.whitelabel_audit;
CREATE TRIGGER trg_whitelabel_audit_append_only
BEFORE UPDATE OR DELETE ON public.whitelabel_audit
FOR EACH ROW EXECUTE FUNCTION public.trg_whitelabel_audit_append_only();

-- ── 6. 单一 readiness 合同函数（统一 R3 §七 · 全仓唯一口径）────────────────────
-- 返回全部 drift blocker（空结果 = 就绪）。覆盖：
--   · whitelabel_settings 4 新列 type/null/default 精确核验（表存在即必查；
--     表缺失仅当 p_require_settings=TRUE 时报 missing_table —— fresh 空库守卫
--     场景由迁移自验/prestart 传 FALSE 容忍，运行时兜底收敛后以 TRUE 全量核验）；
--   · ID 132 customer-only 不变式（D2：任何时刻不得 backoffice_brand_unlocked=TRUE）；
--   · whitelabel_audit 完整 11 列 type/null/default + PK(id) + 永久普通表属性；
--   · idx_whitelabel_audit_user_created 存在、归属 public.whitelabel_audit、
--     非唯一、列序 (user_id, created_at DESC)、定义精确一致、
--     indisvalid/indisready/indislive 三状态全 TRUE（半成品 fail-closed）；
--   · append-only 函数存在、plpgsql、返回 trigger、定义含 RAISE append-only；
--   · 触发器挂 public.whitelabel_audit、非 internal、tgtype=27
--     （BEFORE UPDATE OR DELETE FOR EACH ROW）、tgfoid 归属同一函数。
-- 免疫诱饵：函数级 SET search_path = pg_catalog, public + 全限定 public.* +
-- relpersistence='p' 核验（pg_temp 影子表/视图/诱饵 schema 均无法顶替）。
CREATE OR REPLACE FUNCTION public.whitelabel_backoffice_schema_blockers(
    p_require_settings boolean DEFAULT true
)
RETURNS TABLE(blocker text)
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, public
AS $func$
DECLARE
    settings_oid OID := pg_catalog.to_regclass('public.whitelabel_settings');
    audit_oid    OID := pg_catalog.to_regclass('public.whitelabel_audit');
    idx_rec      RECORD;
    trg_rec      RECORD;
    fn_rec       RECORD;
BEGIN
    -- ── A. whitelabel_settings 4 新列 + ID 132 不变式 ─────────────────────
    IF settings_oid IS NULL THEN
        IF p_require_settings THEN
            blocker := 'missing_table:public.whitelabel_settings';
            RETURN NEXT;
        END IF;
    ELSE
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_class c
             WHERE c.oid = settings_oid AND c.relkind = 'r' AND c.relpersistence = 'p'
        ) THEN
            blocker := 'wrong_relation_kind:public.whitelabel_settings 必须是永久普通表';
            RETURN NEXT;
        END IF;

        RETURN QUERY
        WITH expected(name, sql_type, not_null, default_probe) AS (
            VALUES
                ('backoffice_brand_unlocked'::text, 'boolean'::text, true, 'false'::text),
                ('backoffice_brand_granted_by', 'integer', false, NULL),
                ('backoffice_brand_granted_at', 'timestamp with time zone', false, NULL),
                ('brand_version', 'integer', true, '1')
        ), actual AS (
            SELECT a.attname::text AS name,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS sql_type,
                   a.attnotnull AS not_null,
                   pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS default_expr
              FROM pg_catalog.pg_attribute a
              LEFT JOIN pg_catalog.pg_attrdef d
                ON d.adrelid = a.attrelid AND d.adnum = a.attnum
             WHERE a.attrelid = settings_oid AND a.attnum > 0 AND NOT a.attisdropped
        )
        SELECT 'missing_column:public.whitelabel_settings.' || e.name
          FROM expected e LEFT JOIN actual ac USING (name)
         WHERE ac.name IS NULL
        UNION ALL
        SELECT 'wrong_type:public.whitelabel_settings.' || e.name
               || ':' || ac.sql_type || '!=' || e.sql_type
          FROM expected e JOIN actual ac USING (name)
         WHERE ac.sql_type <> e.sql_type
        UNION ALL
        SELECT 'wrong_nullability:public.whitelabel_settings.' || e.name
               || ':' || ac.not_null::text || '!=' || e.not_null::text
          FROM expected e JOIN actual ac USING (name)
         WHERE ac.not_null <> e.not_null
        UNION ALL
        SELECT CASE WHEN e.default_probe IS NULL
                    THEN 'unexpected_default:public.whitelabel_settings.' || e.name
                         || ':' || ac.default_expr
                    ELSE 'wrong_default:public.whitelabel_settings.' || e.name
                         || ':' || COALESCE(ac.default_expr, '<null>')
                END
          FROM expected e JOIN actual ac USING (name)
         WHERE (e.default_probe IS NULL AND ac.default_expr IS NOT NULL)
            OR (e.default_probe IS NOT NULL AND (
                   ac.default_expr IS NULL
                   OR position(lower(e.default_probe) in lower(ac.default_expr)) = 0
               ));

        -- ID 132 customer-only 不变式（D2 裁决 · 仅当授权列已就位时核验）
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_attribute
             WHERE attrelid = settings_oid AND attname = 'backoffice_brand_unlocked'
               AND attnum > 0 AND NOT attisdropped
        ) AND EXISTS (
            SELECT 1 FROM public.whitelabel_settings
             WHERE user_id = 132 AND backoffice_brand_unlocked IS TRUE
        ) THEN
            blocker := 'id132_backoffice_unlocked:public.whitelabel_settings user_id=132 必须固定 customer-only（backoffice_brand_unlocked 不得为 TRUE）';
            RETURN NEXT;
        END IF;
    END IF;

    -- ── B. whitelabel_audit 完整 11 列 + PK + 永久普通表属性 ───────────────
    IF audit_oid IS NULL THEN
        blocker := 'missing_table:public.whitelabel_audit';
        RETURN NEXT;
    ELSE
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_class c
             WHERE c.oid = audit_oid AND c.relkind = 'r' AND c.relpersistence = 'p'
        ) THEN
            blocker := 'wrong_relation_kind:public.whitelabel_audit 必须是永久普通表';
            RETURN NEXT;
        END IF;

        RETURN QUERY
        WITH expected(name, sql_type, not_null, default_probe) AS (
            VALUES
                ('id'::text, 'bigint'::text, true, 'nextval'::text),
                ('user_id', 'integer', true, NULL),
                ('actor_user_id', 'integer', false, NULL),
                ('actor_role', 'text', true, 'agent'),
                ('field', 'text', true, NULL),
                ('old_value', 'text', false, NULL),
                ('new_value', 'text', false, NULL),
                ('request_id', 'text', false, NULL),
                ('ip', 'text', false, NULL),
                ('reason', 'text', false, NULL),
                ('created_at', 'timestamp with time zone', true, 'now()')
        ), actual AS (
            SELECT a.attname::text AS name,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS sql_type,
                   a.attnotnull AS not_null,
                   pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS default_expr
              FROM pg_catalog.pg_attribute a
              LEFT JOIN pg_catalog.pg_attrdef d
                ON d.adrelid = a.attrelid AND d.adnum = a.attnum
             WHERE a.attrelid = audit_oid AND a.attnum > 0 AND NOT a.attisdropped
        )
        SELECT 'missing_column:public.whitelabel_audit.' || e.name
          FROM expected e LEFT JOIN actual ac USING (name)
         WHERE ac.name IS NULL
        UNION ALL
        SELECT 'wrong_type:public.whitelabel_audit.' || e.name
               || ':' || ac.sql_type || '!=' || e.sql_type
          FROM expected e JOIN actual ac USING (name)
         WHERE ac.sql_type <> e.sql_type
        UNION ALL
        SELECT 'wrong_nullability:public.whitelabel_audit.' || e.name
               || ':' || ac.not_null::text || '!=' || e.not_null::text
          FROM expected e JOIN actual ac USING (name)
         WHERE ac.not_null <> e.not_null
        UNION ALL
        SELECT CASE WHEN e.default_probe IS NULL
                    THEN 'unexpected_default:public.whitelabel_audit.' || e.name
                         || ':' || ac.default_expr
                    ELSE 'wrong_default:public.whitelabel_audit.' || e.name
                         || ':' || COALESCE(ac.default_expr, '<null>')
                END
          FROM expected e JOIN actual ac USING (name)
         WHERE (e.default_probe IS NULL AND ac.default_expr IS NOT NULL)
            OR (e.default_probe IS NOT NULL AND (
                   ac.default_expr IS NULL
                   OR position(lower(e.default_probe) in lower(ac.default_expr)) = 0
               ));

        IF NOT EXISTS (
            SELECT 1
              FROM pg_catalog.pg_constraint c
             WHERE c.conrelid = audit_oid
               AND c.contype = 'p'
               AND c.convalidated
               AND (
                   SELECT array_agg(a.attname ORDER BY k.ord)
                     FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                     JOIN pg_catalog.pg_attribute a
                       ON a.attrelid = c.conrelid AND a.attnum = k.attnum
               ) = ARRAY['id']::name[]
        ) THEN
            blocker := 'missing_pk:public.whitelabel_audit 必须有 PRIMARY KEY(id)';
            RETURN NEXT;
        END IF;
    END IF;

    -- ── C. 索引归属 + 列序 + 定义 + 三状态（半成品 fail-closed）─────────────
    SELECT idx.oid AS index_oid, i.indrelid, i.indisvalid, i.indisready, i.indislive,
           i.indisunique,
           pg_catalog.pg_get_indexdef(i.indexrelid) AS indexdef,
           ARRAY(
               SELECT pg_catalog.pg_get_indexdef(i.indexrelid, g.ord, true)
                      -- indoption::int2[] 保持 int2vector 的 0 基下标（与 v14 合同
                      -- 引擎 Python 侧 0 基枚举同口径），故取 g.ord-1；bit0=DESC。
                      || CASE WHEN ((i.indoption::int2[])[g.ord - 1] & 1) = 1
                              THEN ' DESC' ELSE '' END
                 FROM generate_series(1, i.indnkeyatts) AS g(ord)
                 ORDER BY g.ord
           ) AS key_cols
      INTO idx_rec
      FROM pg_catalog.pg_class idx
      JOIN pg_catalog.pg_namespace n ON n.oid = idx.relnamespace
      JOIN pg_catalog.pg_index i ON i.indexrelid = idx.oid
     WHERE n.nspname = 'public'
       AND idx.relname = 'idx_whitelabel_audit_user_created'
       AND idx.relkind = 'i';
    IF NOT FOUND THEN
        blocker := 'missing_index:public.idx_whitelabel_audit_user_created';
        RETURN NEXT;
    ELSE
        IF audit_oid IS NULL OR idx_rec.indrelid <> audit_oid THEN
            blocker := 'wrong_index_ownership:idx_whitelabel_audit_user_created 必须归属 public.whitelabel_audit';
            RETURN NEXT;
        END IF;
        IF NOT (idx_rec.indisvalid AND idx_rec.indisready AND idx_rec.indislive) THEN
            blocker := 'half_built_index:idx_whitelabel_audit_user_created 状态不可用'
                       || '（indisvalid=' || idx_rec.indisvalid::text
                       || ' indisready=' || idx_rec.indisready::text
                       || ' indislive=' || idx_rec.indislive::text || '，必须全 TRUE）';
            RETURN NEXT;
        END IF;
        IF idx_rec.indisunique THEN
            blocker := 'wrong_index_unique:idx_whitelabel_audit_user_created 不得为唯一索引';
            RETURN NEXT;
        END IF;
        IF idx_rec.key_cols <> ARRAY['user_id', 'created_at DESC']::text[] THEN
            blocker := 'wrong_index_columns:idx_whitelabel_audit_user_created 列序必须为'
                       || ' (user_id, created_at DESC)，实际 (' || array_to_string(idx_rec.key_cols, ', ') || ')';
            RETURN NEXT;
        END IF;
        IF idx_rec.indexdef <>
           'CREATE INDEX idx_whitelabel_audit_user_created ON public.whitelabel_audit USING btree (user_id, created_at DESC)' THEN
            blocker := 'wrong_index_definition:' || idx_rec.indexdef;
            RETURN NEXT;
        END IF;
    END IF;

    -- ── D. append-only 函数与触发器归属 ──────────────────────────────────
    SELECT p.oid
      INTO fn_rec
      FROM pg_catalog.pg_proc p
      JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public'
       AND p.proname = 'trg_whitelabel_audit_append_only'
       AND p.pronargs = 0;
    IF NOT FOUND THEN
        blocker := 'missing_function:public.trg_whitelabel_audit_append_only()';
        RETURN NEXT;
    ELSE
        IF NOT EXISTS (
            SELECT 1
              FROM pg_catalog.pg_proc p
              JOIN pg_catalog.pg_language l ON l.oid = p.prolang
             WHERE p.oid = fn_rec.oid
               AND l.lanname = 'plpgsql'
               AND p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype
               AND position('whitelabel_audit is append-only'
                            in pg_catalog.pg_get_functiondef(p.oid)) > 0
        ) THEN
            blocker := 'wrong_function_definition:public.trg_whitelabel_audit_append_only()'
                       || ' 必须为 plpgsql、返回 trigger、且 RAISE append-only';
            RETURN NEXT;
        END IF;
    END IF;

    IF audit_oid IS NOT NULL THEN
        SELECT t.tgtype, t.tgfoid
          INTO trg_rec
          FROM pg_catalog.pg_trigger t
         WHERE t.tgrelid = audit_oid
           AND t.tgname = 'trg_whitelabel_audit_append_only'
           AND NOT t.tgisinternal;
        IF NOT FOUND THEN
            blocker := 'missing_trigger:public.whitelabel_audit 缺 trg_whitelabel_audit_append_only';
            RETURN NEXT;
        ELSE
            -- tgtype 位掩码：BEFORE(2) | ROW(1) | DELETE(8) | UPDATE(16) = 27
            IF trg_rec.tgtype <> 27 THEN
                blocker := 'wrong_trigger_timing:trg_whitelabel_audit_append_only tgtype='
                           || trg_rec.tgtype::text
                           || '（期望 27 = BEFORE UPDATE OR DELETE FOR EACH ROW）';
                RETURN NEXT;
            END IF;
            IF fn_rec.oid IS NULL OR trg_rec.tgfoid <> fn_rec.oid THEN
                blocker := 'wrong_trigger_function:触发器必须 EXECUTE public.trg_whitelabel_audit_append_only()';
                RETURN NEXT;
            END IF;
        END IF;
    END IF;

    RETURN;
END;
$func$;

-- ── 7. 迁移自验（统一 R3 §七 · 调 §6 同一合同函数）───────────────────────────
-- 本块 / prestart / runtime 启动自检 / 统一 release readiness 四处同一口径。
-- 任何 drift（含 canonical 索引名被异物占用的"假成功"、indisvalid/indisready/
-- indislive=false 半成品索引）→ 同事务 RAISE，整迁移中止（deploy abort）。
-- fresh 空库基表缺失属预期守卫场景 → p_require_settings 按实际存在性传入。
DO $$
DECLARE
    drift text;
BEGIN
    SELECT string_agg(b.blocker, ' | ')
      INTO drift
      FROM public.whitelabel_backoffice_schema_blockers(
              pg_catalog.to_regclass('public.whitelabel_settings') IS NOT NULL) AS b;
    IF drift IS NOT NULL THEN
        RAISE EXCEPTION 'WHITELABEL_BACKOFFICE_FORWARD_CONTRACT_DRIFT: %', drift;
    END IF;
END $$;

COMMIT;

-- 复跑说明: 本文件可连续执行两次以上；3/4 两节 UPDATE 为同值重写，无副作用；
--   §6 合同函数 CREATE OR REPLACE 幂等，§7 自验只读。
-- 回滚: scripts/rollback_whitelabel_backoffice_scope_2026_07_22.sql
--   （同一咨询锁 key=2026072201；仅 DROP 新列/合同函数/append-only 对象 + 归档审计表）。
