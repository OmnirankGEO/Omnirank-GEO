-- 文章域 · 浏览器自报终态收口(WO_ARTICLE_BROWSER_SELF_REPORT_2026-08-19)
-- R2(Review-CTO 2026-08-19 §①):**探针不许给自己签 verified**。
--
-- 病灶:`publish_records` 的唯一活写入方是浏览器扩展 WS 消息 PUBLISH_PROGRESS
-- (浏览器插件后端;WO_273 已删)。浏览器一句 {"success":true,"publicUrl":"…"} 就能让
-- 下游「严格事实」链(发布快照 → 交付槽投影 → 效果归因台账)当真。
--
-- R1 加了服务端探针,但 R2 复审指出探针本身**证明不了发布**:攻击者做一个同标题
-- 的页面就能骗过内容指纹。所以探针结果只是**线索**,不是权威。
--
-- 三个维度分开,谁也别冒充谁:
--   `public_url_reported_explicitly`   来源位 —— 浏览器**显式回报过** URL(语义一直诚实)
--   `public_url_verification_state`    核实态 —— 见下方闭集
--   `public_url_verification_source`   权威来源 —— 谁下的结论
--
-- 🔴 **本迁移的核心是那条 `verified ⇒ source ∈ (provider_receipt, human_attestation)`
--    的 CHECK**:它把"探针不能自签"钉在**库**上,而不是钉在代码的自觉上。
--    代码写错、有人绕过 service 直写 SQL,库都会拒绝。
--
-- 🔴 幂等:本仓 prestart 每次部署无条件重放全部迁移(无追踪表),重放必须无害。

ALTER TABLE publish_records
    ADD COLUMN IF NOT EXISTS public_url_verification_state VARCHAR(32) NOT NULL DEFAULT 'unverified';
ALTER TABLE publish_records
    ADD COLUMN IF NOT EXISTS public_url_verification_source VARCHAR(40);
ALTER TABLE publish_records
    ADD COLUMN IF NOT EXISTS public_url_verified_at TIMESTAMPTZ;
ALTER TABLE publish_records
    ADD COLUMN IF NOT EXISTS public_url_verification_method VARCHAR(80);
ALTER TABLE publish_records
    ADD COLUMN IF NOT EXISTS public_url_verification_detail JSONB;

-- 核实态闭集:
--   unverified       没有可核实的 URL 声明
--   pending          有声明,排队等服务端探针
--   content_matched  🔴 **线索态**:探针在平台域内取到页面且带我方指纹。
--                    它**不是**终态事实 —— 攻击者做个同标题页面就能造出这一态。
--   needs_action     探不动 / 无指纹 / 域不在平台清单 / 无可比对锚点 —— 等人处理
--   verified         唯一终态事实。只能来自 provider 回执或人工核实动作
-- 🔴 往这个 CHECK 加允许值**不是** additive 变更:每加一个值就是多一条能进
--    终态链的路。加值必须单独评审,不许顺手改。
-- 🔴 用 DROP IF EXISTS + ADD(不是"不存在才加"):R1 版本的闭集只有 4 个值,
--    已经跑过 R1 的测试库重放本文件时必须被**改定义**,而"不存在才加"会让它
--    永远停在旧闭集上(定义漂移且无声)。本表 58 行,重验成本可忽略。
ALTER TABLE publish_records
    DROP CONSTRAINT IF EXISTS publish_records_public_url_verification_state_check;
ALTER TABLE publish_records
    ADD CONSTRAINT publish_records_public_url_verification_state_check
    CHECK (public_url_verification_state IN
           ('unverified', 'pending', 'content_matched', 'needs_action', 'verified'));

-- 权威来源闭集。server_probe 在列内,但下面那条 CHECK 不让它配 verified。
ALTER TABLE publish_records
    DROP CONSTRAINT IF EXISTS publish_records_public_url_verification_source_check;
ALTER TABLE publish_records
    ADD CONSTRAINT publish_records_public_url_verification_source_check
    CHECK (public_url_verification_source IS NULL
           OR public_url_verification_source IN
              ('server_probe', 'provider_receipt', 'human_attestation'));

-- 🔴🔴 R2 §① 的库级锁:verified 只有两个合法来源。
--      探针(server_probe)想写 verified → 23514 当场拒绝,不看调用方是谁。
ALTER TABLE publish_records
    DROP CONSTRAINT IF EXISTS publish_records_verified_requires_authority_source;
ALTER TABLE publish_records
    ADD CONSTRAINT publish_records_verified_requires_authority_source
    CHECK (public_url_verification_state <> 'verified'
           OR public_url_verification_source IN ('provider_receipt', 'human_attestation'));

-- 待核实队列走这条偏索引(整表扫描没必要 · 绝大多数行是 unverified 终态)
-- @index-guard idx_pr_url_verification_pending ON publish_records plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pr_url_verification_pending' AND i.indrelid = to_regclass('public.publish_records')) THEN
        NULL;  -- 已在 public.publish_records 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pr_url_verification_pending' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pr_url_verification_pending 已存在但不在 public.publish_records 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pr_url_verification_pending' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pr_url_verification_pending ON public.publish_records (created_at) WHERE public_url_verification_state = 'pending';
    END IF;
END $idxguard$;
-- needs_action / content_matched 是**人要处理的队列**,也得能快速捞
-- @index-guard idx_pr_url_verification_actionable ON publish_records plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pr_url_verification_actionable' AND i.indrelid = to_regclass('public.publish_records')) THEN
        NULL;  -- 已在 public.publish_records 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pr_url_verification_actionable' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pr_url_verification_actionable 已存在但不在 public.publish_records 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pr_url_verification_actionable' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pr_url_verification_actionable ON public.publish_records (created_at) WHERE public_url_verification_state IN ('needs_action', 'content_matched');
    END IF;
END $idxguard$;

-- 人工核实动作的审计流水(**纯新表 · 只追加**)。
-- R2 §① 要求人工核实带 actor + 证据 hash + 审计 —— 没有留痕的人工放行,
-- 等于把刚堵上的洞开在后台。同时它是 needs_action / content_matched 的**出路**。
-- 🔴 不加 FK 到 publish_records:审计不该随业务行删除而消失,
--    归属校验每请求现做(FK 假装做过,而且是部署期地雷)。
CREATE TABLE IF NOT EXISTS publish_record_verification_events (
    id                BIGSERIAL PRIMARY KEY,
    publish_record_id INTEGER      NOT NULL,
    actor_user_id     INTEGER,
    actor_kind        VARCHAR(24)  NOT NULL DEFAULT 'human',
    action            VARCHAR(40)  NOT NULL,
    from_state        VARCHAR(32),
    to_state          VARCHAR(32)  NOT NULL,
    verification_source VARCHAR(40) NOT NULL,
    evidence_sha256   CHAR(64),
    evidence_note     TEXT,
    detail            JSONB,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- @index-guard idx_prve_record ON publish_record_verification_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_prve_record' AND i.indrelid = to_regclass('public.publish_record_verification_events')) THEN
        NULL;  -- 已在 public.publish_record_verification_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_prve_record' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_prve_record 已存在但不在 public.publish_record_verification_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_prve_record' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_prve_record ON public.publish_record_verification_events (publish_record_id, created_at DESC);
    END IF;
END $idxguard$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'publish_record_verification_events'::regclass
           AND conname  = 'prve_human_attestation_needs_evidence'
    ) THEN
        -- 人工核实必须留下 actor 与证据 hash;缺一样就不是"核实过",是"有人点了个按钮"。
        ALTER TABLE publish_record_verification_events
            ADD CONSTRAINT prve_human_attestation_needs_evidence
            CHECK (verification_source <> 'human_attestation'
                   OR (actor_user_id IS NOT NULL AND evidence_sha256 IS NOT NULL));
    END IF;
END $$;

-- 回填:历史上「浏览器显式回报过 URL」的行从来没有被任何权威核实过。
-- 它们的诚实归类是 needs_action(等人处理),**不是** verified。
-- 🔴 生产实证(2026-08-19 只读探针):公网库 58 行 publish_records 中
--    public_url_reported_explicitly IS TRUE 的有 **0** 行 → 本条在生产是 0 行改动。
--    写在这里是为测试库/未来库同样收口,不是因为生产有存量。
UPDATE publish_records
   SET public_url_verification_state = 'needs_action',
       public_url_verification_method = 'legacy_backfill_never_server_verified'
 WHERE public_url_reported_explicitly IS TRUE
   AND public_url_verification_state = 'unverified';

-- ===========================================================================
-- R3 §① · verified 单调不可降级 + 独立的「可达性 / 撤回」轴
-- ===========================================================================
-- Codex 复现打出来的 P0:一条已被**人工核实**过的记录,管理员再点一次「重核」,
-- 探针探不动(页面临时 502 / 平台改版 / 文章后来被下架)就把它写回 needs_action,
-- 连 `public_url_verified_at` 都被清成 NULL —— **历史发布事实被一次探测抹掉了**。
--
-- 这在语义上就是错的:探针**从来不是**权威(R2 §① 已定),它凭什么撤销权威的结论?
-- 「页面现在打不开」与「这篇当初发布过」是两个互不蕴含的命题,必须分两根轴记:
--
--   核实轴 `public_url_verification_state`   —— 当初到底发没发出去(**单调**,只进不退)
--   可达轴 `public_url_availability_state`   —— 那个页面**此刻**还在不在
--
-- 于是「文章被平台下架了」这类事实有地方落,而且落下去**不动**核实轴。
ALTER TABLE publish_records
    ADD COLUMN IF NOT EXISTS public_url_availability_state VARCHAR(32);
ALTER TABLE publish_records
    ADD COLUMN IF NOT EXISTS public_url_availability_checked_at TIMESTAMPTZ;
ALTER TABLE publish_records
    ADD COLUMN IF NOT EXISTS public_url_availability_detail JSONB;

-- 可达轴闭集:
--   available        探针此刻取到页面且带我方指纹
--   unreachable      探不动 / 非 200
--   content_missing  取到了页面但我方指纹没了(可能被改稿/被替换)
--   domain_mismatch  落地域已不在该平台清单内(站内跳转跳走了)
--   retracted        🔴 人工登记的撤回/下架(只有人能下这个结论,探针给不出)
ALTER TABLE publish_records
    DROP CONSTRAINT IF EXISTS publish_records_public_url_availability_state_check;
ALTER TABLE publish_records
    ADD CONSTRAINT publish_records_public_url_availability_state_check
    CHECK (public_url_availability_state IS NULL
           OR public_url_availability_state IN
              ('available', 'unreachable', 'content_missing', 'domain_mismatch', 'retracted'));

-- 🔴🔴 单调性的**库级**锁:verified 是终态,任何把它写回低态的 UPDATE 直接拒绝。
--      Python 侧 `_write_state` 的守卫是第一道,这道是"代码写错也拦得住"的那道。
--      (与 `verified ⇒ 权威来源` 那条一样的思路:钉在库上,不钉在自觉上。)
CREATE OR REPLACE FUNCTION publish_records_verification_is_monotonic()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.public_url_verification_state = 'verified'
       AND NEW.public_url_verification_state <> 'verified' THEN
        RAISE EXCEPTION
            'publish_records.% verified 是终态,不许降级为 %(页面事后失效请写 public_url_availability_state)',
            OLD.id, NEW.public_url_verification_state
            USING ERRCODE = 'check_violation';
    END IF;
    -- 已经 verified 的行,verified_at 也不许被后续写入抹掉/改写:
    -- 它是"当初什么时候被核实的",不是"最近一次有人点过按钮"。
    IF OLD.public_url_verified_at IS NOT NULL THEN
        NEW.public_url_verified_at := OLD.public_url_verified_at;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 🔴 触发器的 CREATE 挪到了**本文件末尾**(R4 §A 的清洗 UPDATE 必须在它不存在时跑,
--    否则那句 `NEW.verified_at := OLD.verified_at` 会把清洗原地改回去)。
--    这里只保留函数定义;建触发器见文件最后一段。

-- ===========================================================================
-- R4 §A · verified_at 的库级 CHECK(防"首设直写")
-- ===========================================================================
-- R3 把「已 verified 的行不许被降级、verified_at 不许被改写」钉住了,但留了一个
-- 方向没堵:**一条从来没被核实过的行,被直接写上一个 verified_at**。
-- 触发器只在 `OLD.public_url_verified_at IS NOT NULL` 时保值 —— 从 NULL 到有值
-- 那一跳它是放行的,而 R3 的单调守卫看的是 state 不是这一列。
-- 于是「state 还是 needs_action,verified_at 已经有值」这种自相矛盾的行写得进去,
-- 而下游若有任何一处按 `verified_at IS NOT NULL` 判"核实过"(这是很自然的写法),
-- 它就成了绕过整条权威链的后门。
--
-- 口径:**verified_at 有值 ⇔ state = 'verified'**。两列互为对方的冗余表达,
-- 不允许它们各说各话。
-- 🔴🔴 清洗必须在**触发器摘掉之后**跑。
--    R3 那个 BEFORE UPDATE 触发器里有一句
--      `IF OLD.public_url_verified_at IS NOT NULL THEN NEW.… := OLD.…`
--    —— 它会把下面这条 `SET … = NULL` **原地改回去**,清洗变成空转。
--    首次部署时触发器还没建,看不出问题;而 prestart **每次部署无条件重放全部迁移**,
--    第二次起触发器已经在了 → 清洗空转 → 若真有矛盾行,下面的 ADD CONSTRAINT
--    当场失败 → **prestart 挂,整次部署挂**。
--    (这条是判据 test_migration_nulls_out_preexisting_contradictory_verified_at
--     跑出来的,不是看出来的。)
DROP TRIGGER IF EXISTS trg_publish_records_verification_monotonic ON publish_records;

UPDATE publish_records
   SET public_url_verified_at = NULL
 WHERE public_url_verification_state <> 'verified'
   AND public_url_verified_at IS NOT NULL;

ALTER TABLE publish_records
    DROP CONSTRAINT IF EXISTS publish_records_verified_at_requires_verified_state;
ALTER TABLE publish_records
    ADD CONSTRAINT publish_records_verified_at_requires_verified_state
    CHECK (public_url_verified_at IS NULL
           OR public_url_verification_state = 'verified');

-- ===========================================================================
-- R4 §B · 可达轴的权威来源列 + retracted 的库级后盾
-- ===========================================================================
-- 核实轴有 `verified ⇒ source ∈ (provider_receipt, human_attestation)` 这条 CHECK,
-- 可达轴却只有 `_write_availability` 里一句 Python 断言 —— 也就是说
-- 「探针不能自己判 retracted」这条**只靠调用方自觉**。
-- 而 R3 自己写下的理由是:调用点会新增,靠自觉必漏一个。可达轴照镜子补齐。
ALTER TABLE publish_records
    ADD COLUMN IF NOT EXISTS public_url_availability_source VARCHAR(40);

ALTER TABLE publish_records
    DROP CONSTRAINT IF EXISTS publish_records_availability_source_check;
ALTER TABLE publish_records
    ADD CONSTRAINT publish_records_availability_source_check
    CHECK (public_url_availability_source IS NULL
           OR public_url_availability_source IN
              ('server_probe', 'provider_receipt', 'human_attestation'));

-- 🔴🔴 与核实轴那条完全同构:`retracted` 是**人**才能下的结论
--      (探针看到 404 只能说"探不动"),所以它只认人工来源。
ALTER TABLE publish_records
    DROP CONSTRAINT IF EXISTS publish_records_retracted_requires_human_source;
ALTER TABLE publish_records
    ADD CONSTRAINT publish_records_retracted_requires_human_source
    CHECK (public_url_availability_state <> 'retracted'
           OR public_url_availability_source = 'human_attestation');

-- 有可达结论就必须说清是谁下的(与核实轴 source 的存在性口径一致)。
ALTER TABLE publish_records
    DROP CONSTRAINT IF EXISTS publish_records_availability_state_needs_source;
ALTER TABLE publish_records
    ADD CONSTRAINT publish_records_availability_state_needs_source
    CHECK (public_url_availability_state IS NULL
           OR public_url_availability_source IS NOT NULL);

-- 触发器在本文件**末尾**重建(§A 的清洗需要先把它摘掉,见上)。
-- 放在最后一行是刻意的:它之前的所有 UPDATE 都属于"迁移自己的清洗",
-- 不该被这道运行时守卫拦住;它之后的一切写入才归它管。
CREATE TRIGGER trg_publish_records_verification_monotonic
    BEFORE UPDATE ON publish_records
    FOR EACH ROW EXECUTE FUNCTION publish_records_verification_is_monotonic();
