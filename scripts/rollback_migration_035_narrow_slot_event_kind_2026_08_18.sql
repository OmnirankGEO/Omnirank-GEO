-- ============================================================================
-- 回滚工件 · 与 db/migration_035_geo_image_note_slot_channel_2026_08_18.sql 绑定
--   作用:把 ck_geo_article_slot_event_kind 从 035 的 **14 值**收窄回旧 **9 值**。
--
-- 🔴🔴 **这不是迁移。它不进 db/migration_manifest.py。**
--   prestart 每次部署**无条件重放**全部 manifest 迁移(本仓无迁移追踪表)。
--   把收窄放进 manifest = 每次部署先把 035 撤掉,而 035 就在同一份 manifest 里
--   往回扩 —— 两条在同一次 prestart 里互相拆台,谁排在后面谁赢,
--   而顺序是列表位置决定的,不是意图决定的。
--   它是**人工回滚工件**:只在「代码要退回 035 之前那一版」的那一刻,由人手动跑一次。
--
-- 为什么需要它(回滚纵深):
--   36 班已把 035 上生产 ⇒ 库里是 14 值。要把版本退回 `c2ad8825` 时,
--   那一版的 `services/article_closed_loop_schema_contract.py:273` 把 9 值定义
--   **逐字钉死**(该文件注释自己写着:substring 检查会接受被削弱的谓词,所以钉全串)。
--   ⇒ 库 14 值 ≠ 契约 9 值 ⇒ `assert_schema_ready()` 抛
--     `GEO_ARTICLE_CLOSED_LOOP_SCHEMA_NOT_READY:wrong_constraint_definition:…`
--   ⇒ **版本回退这条腿被我们自己的守卫挡住**。先跑本文件,回退腿才通。
--
-- 只动这一个 CHECK:035 的另外几项(delivery_channel / media_mix_bucket /
--   fulfillment_state 三列、三个新 CHECK、新索引)是**纯 additive**,
--   旧契约按「期望项逐个核对」跑,多出来的列/约束一个都不看(实证见判据文件)。
--   收窄它们既无必要,又会真的丢数据。
--
-- ----------------------------------------------------------------------------
-- 跑法(生产 · 务必先 dry-run)
--   dry-run:
--     docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope -v ON_ERROR_STOP=1 \
--       -c "BEGIN;" -f scripts/rollback_migration_035_narrow_slot_event_kind_2026_08_18.sql -c "ROLLBACK;"
--   真跑:
--     docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope -v ON_ERROR_STOP=1 \
--       -f scripts/rollback_migration_035_narrow_slot_event_kind_2026_08_18.sql
--   🔴 `--single-transaction` 是 COMMIT 不是 dry-run,别拿它当演练。
--
-- 幂等:重复跑无害(已经是 9 值就什么都不做)。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 安全闸 + 收窄,同一个 DO 块、同一个事务:
--   闸必须在 DDL **之前**,而且必须跟 DDL 同事务 —— 分开跑的话,
--   「查完 0 行」与「收窄」之间的那道缝里写进来的新枚举行会被 ALTER 拒绝,
--   报出来的是一句 PostgreSQL 原文,不是我们这句人话。
--
-- 🔴 为什么必须有这道闸:Deploy 今天(2026-08-20)实测新 5 枚举 0 行在用,
--   但**回滚发生那天的数据不是今天的数据**。图文链一旦真开始跑,
--   `slot_claimed` / `generation_started` 这些就会有真行。那时候硬收窄 =
--   ALTER 直接失败(PG 会校验既有行)⇒ 一句难懂的报错 + 一次失败的回滚。
--   闸把它变成:**明确拒收窄 + 指名是哪几种、各多少行、样例 id**。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    _new_kinds  text[] := ARRAY['slot_claimed', 'slot_released',
                                'generation_started', 'post_linked', 'ready'];
    _breakdown  text;
    _total      bigint;
    _samples    text;
    _cur        text;
    _is_old     boolean;
BEGIN
    -- 表不在就没什么可收窄的(全新库 / 未跑过闭环迁移),直接跳过,不报错。
    IF to_regclass('geo_article_delivery_slot_events') IS NULL THEN
        RAISE NOTICE '[rollback-035] geo_article_delivery_slot_events 不存在 · 跳过';
        RETURN;
    END IF;

    -- ---- 闸:新 5 枚举有没有真行在用 ----
    SELECT coalesce(sum(cnt), 0),
           string_agg(event_kind || '=' || cnt, ' / ' ORDER BY event_kind)
      INTO _total, _breakdown
      FROM (
            SELECT event_kind::text AS event_kind, count(*) AS cnt
              FROM geo_article_delivery_slot_events
             WHERE event_kind::text = ANY (_new_kinds)
             GROUP BY 1
           ) t;

    IF _total > 0 THEN
        SELECT string_agg(sample, ' / ')
          INTO _samples
          FROM (
                SELECT format('id=%s kind=%s slot=%s', id, event_kind, delivery_slot_key) AS sample
                  FROM geo_article_delivery_slot_events
                 WHERE event_kind::text = ANY (_new_kinds)
                 ORDER BY id
                 LIMIT 5
               ) s;

        RAISE EXCEPTION
            '[rollback-035] 拒绝收窄:035 新增的事件种类已经有 % 行真数据在用(%)。'
            '前 5 条样例:%。'
            '收窄这个 CHECK 会让这些行变成违反约束的存量,ALTER 也过不去。'
            '处置:先决定这些行怎么办(留着就别收窄 / 要收窄就先把它们迁走或删掉),'
            '再重跑本文件。',
            _total, _breakdown, coalesce(_samples, '(取样失败)');
    END IF;

    RAISE NOTICE '[rollback-035] 安全闸通过:新 5 种事件 0 行在用';

    -- ---- 收窄:幂等 ----
    -- 判「是不是已经是旧 9 值版」用**新枚举在不在定义里**,不做全字符串比对 ——
    -- 全串比对跨 PG 版本脆(渲染细节会变),而这里真正要回答的问题就是
    -- 「那 5 个新种类还在不在允许集合里」。这与 035 自己的判法同构(方向相反)。
    SELECT pg_get_constraintdef(oid, true) INTO _cur
      FROM pg_constraint
     WHERE conname = 'ck_geo_article_slot_event_kind'
       AND connamespace = (SELECT oid FROM pg_namespace WHERE nspname = current_schema());

    _is_old := _cur IS NOT NULL
           AND _cur NOT LIKE '%slot_claimed%'
           AND _cur NOT LIKE '%slot_released%'
           AND _cur NOT LIKE '%generation_started%'
           AND _cur NOT LIKE '%post_linked%'
           AND _cur NOT LIKE '%''ready''%';

    IF _is_old THEN
        RAISE NOTICE '[rollback-035] CHECK 已经是旧 9 值版 · 无需改动(幂等)';
        RETURN;
    END IF;

    IF _cur IS NOT NULL THEN
        ALTER TABLE geo_article_delivery_slot_events
            DROP CONSTRAINT ck_geo_article_slot_event_kind;
    END IF;

    -- 🔴 这一串必须**逐字**渲染成 c2ad8825 契约钉死的那一版,否则收窄了守卫照样红。
    --    写法(`'x'::character varying` 列表 + 外层 `::text[]`)与生产快照里
    --    pg_get_constraintdef 的输出同形;判据里有一条直接比对。
    ALTER TABLE geo_article_delivery_slot_events
        ADD CONSTRAINT ck_geo_article_slot_event_kind
        CHECK (event_kind::text = ANY (ARRAY[
            'created'::character varying, 'blocked'::character varying,
            'unblocked'::character varying, 'cancelled'::character varying,
            'superseded'::character varying, 'reassigned'::character varying,
            'topic_linked'::character varying, 'article_linked'::character varying,
            'publication_locked'::character varying]::text[]));

    RAISE NOTICE '[rollback-035] 已收窄回旧 9 值版';
END $$;
