-- ============================================================
-- #185 c3 · 防御型写法(playbook)按品牌持久化
--
-- Owner 2026-09-13 原话:「防御型公司词的标题生成也需要经过 AI,
--   要蒸馏一下研究一下防御型的这些文章怎么写。」
-- 「蒸馏研究」的产物就是这张表的一行:研究「X 怎么样 / 靠谱吗 / 投诉 /
--   差异 / 资质 / 适合谁 / 价格 / 团队」这类公司词问法下,AI 引擎会引用
--   什么样的标题与正文。标题与正文两处都读它。
--
-- 🔴 为什么单独一张表,不挂 jsonb 列在 brands 上:
--    它有**生命周期**(TTL 7 天 + 知识库更新即失效)与**版本**,
--    而一个挂在品牌行上的 jsonb 列只有"当前值",旧版本会被覆盖 ——
--    产物出错时没法回看"上一版是什么、它是用什么素材蒸的"。
--    素材计数(sources_used)也要能逐版本回查,否则「今天为 0」与
--    「这张表根本答不了这个问题」就分不出来。
--
-- 🔴 SQL 四维核验。**权威读数 = Deploy 2026-09-14 的生产只读**(geo_agentscope/public):
--    ① 列名 · ② data_type:`brands.id` = **integer(int4)NOT NULL**,
--      default `nextval('brands_id_seq')` ⇒ 本表 brand_id 用 INTEGER;
--      时间列一律 timestamptz(与本仓既有表同款)。
--    ③ 字段归属:`brands_pkey` 存在、`contype='p'`、列 = id ⇒ 外键指得住。
--      **不 grep dump** —— pg_dump 把 ALTER TABLE 与 FOREIGN KEY 写在两行,
--      同一行 grep 会得出"这族没有外键"的自信错答案(本仓 09-13 栽过)。
--      (查 pg_constraint 时注意:`contype` 是 "char",拼接要 `::text`。)
--    ④ dry-run:本迁移在私库 BEGIN/ROLLBACK 跑过、**连跑两遍**约束数 3→3 零报错。
--
-- 🔴 私库那份读数**不作为 schema 权威**:它是夹具用 prod_schema_2026-09-05.sql
--    恢复出来的,conftest 还用 ON_ERROR_STOP=0(部分对象恢复失败也算建成功)。
--    它证的是「这段 SQL 跑得通」,证不了「生产长这样」。两件事分开说。
--
-- 🔴 本文件**不含任何 DML**(仓规:迁移体内禁 DML)。
-- ============================================================

CREATE TABLE IF NOT EXISTS geo_defensive_playbooks (
    id              BIGSERIAL PRIMARY KEY,

    --: 属于哪个品牌。防御型写法是**按品牌**蒸的:它要用这家的事实与竞品。
    brand_id        INTEGER NOT NULL,

    --: 产物版本号(`v<prompt 版本>-<日期>`)。换 prompt 必须换它 ——
    --: 否则"这条角度是哪一版蒸出来的"永远查不清。
    version         TEXT NOT NULL,

    --: 产物本体:{title_angles{问名: [角度…]}, body_points[], evidence_rules[], forbidden[]}
    payload         JSONB NOT NULL,

    --: 🔴 素材计数,**逐项写清**,不许假装有。
    --:    形如 {"client_profile_facts": 9, "competitors": 4,
    --:          "cited_samples": {"count": 0, "table": "geo_article_citation_attributions"},
    --:          "cited_samples_unavailable": {...}}
    --:    「今天为 0」与「这张表根本答不了这个问题」是**两件事**:
    --:    前者会随时间变,后者不会。混成一个 0,下一个人会一直等它长出来。
    sources_used    JSONB NOT NULL DEFAULT '{}'::jsonb,

    --: 知识库指纹。品牌事实一变,这个值变,旧 playbook 即失效 ——
    --: 比"存一个更新时间再比大小"稳:时间可能因为无关字段的 touch 而前进。
    kb_fingerprint  TEXT NOT NULL DEFAULT '',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    --: TTL 到点(创建 + 7 天)。过期不删行(留痕可回看),只是不再被取用。
    expires_at      TIMESTAMPTZ NOT NULL,

    CONSTRAINT fk_defensive_playbook_brand
        FOREIGN KEY (brand_id) REFERENCES brands (id),

    --: 过期时间必须在创建之后 —— 写反了就等于"生下来就过期",
    --: 而那会表现成"缓存永远不命中、每次都打 LLM",没人会去看这一列。
    CONSTRAINT ck_defensive_playbook_ttl CHECK (expires_at > created_at)
);

-- ============================================================
-- 🔴 幂等补齐:上面那段 `CREATE TABLE IF NOT EXISTS` 在**表已存在**时整段跳过,
--    连同里面的两条约束一起跳过。于是"表是上一次半截建出来的"这种情况下,
--    约束会永远补不上 —— 而**生产迁移走 prestart、每次启动都重跑、不写
--    `_migrations`**(本仓 2026-09-13 实测),正是最容易遇到半截状态的跑法。
--
--    所以约束再用 `pg_constraint` 查一次、缺了才加。`DO $$` 块里先查再加,
--    比 `ADD CONSTRAINT IF NOT EXISTS`(PostgreSQL 没有这个语法)可靠。
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.geo_defensive_playbooks'::regclass
           AND conname  = 'fk_defensive_playbook_brand'
    ) THEN
        ALTER TABLE geo_defensive_playbooks
            ADD CONSTRAINT fk_defensive_playbook_brand
            FOREIGN KEY (brand_id) REFERENCES brands (id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.geo_defensive_playbooks'::regclass
           AND conname  = 'ck_defensive_playbook_ttl'
    ) THEN
        ALTER TABLE geo_defensive_playbooks
            ADD CONSTRAINT ck_defensive_playbook_ttl
            CHECK (expires_at > created_at);
    END IF;
END $$;

--: 取用路径就是这一条:某品牌、还没过期、最新的那一版。
CREATE INDEX IF NOT EXISTS idx_defensive_playbook_live
    ON geo_defensive_playbooks (brand_id, expires_at DESC, id DESC);
