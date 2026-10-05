-- 存量影响面普查 · 「admin 代跑他人品牌复制出同名副本」(生产实证 brands.id=936)
-- ============================================================================
-- 🔴 只读。全文只有 SELECT,零 DML/DDL。执行方式(Deploy):
--     docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope <<'SQL'
--     BEGIN; SET TRANSACTION READ ONLY;
--     <本文件内容>
--     -- 自证:下一句必须失败,失败才证明只读栓真的生效
--     UPDATE public.brands SET name = name WHERE false;
--     ROLLBACK;
--     SQL
--   自证语句**不依赖任何列名之外的东西**:`name` 是 brands 上必有的列;
--   若写成不存在的列,它会在解析期就挂并报 column does not exist,
--   那是"自证静默失效"而不是"只读拦住了"。
--
-- 🔴 口径警告(先读再看数):**跨 owner 同名是合法的**。
--   生产实查一个品牌名最多 9 个 owner(不同服务商服务同一家企业)。
--   所以"同名不同 owner"本身**不是**证据,只有配上下面两条才是本缺陷的指纹:
--     ① 副本在**旧的那条之后**创建,且两者创建时间相隔很近(诊断那一跑之内);
--     ② 诊断产物(pipeline_stage_log 的 diagnosis/complete)落在**副本**上,
--        而同名的旧品牌**有史以来一次都没有**。
--   只用①会把正常的多服务商同名一起算进来(假阳)。
--
-- 🔴 ② 那半句「有史以来」是干跑改出来的,不是想出来的:第一版写的是
--   「旧品牌在**同一窗口**里没有」,喂进一个合法样本(两个服务商各自都跑过、
--   老的那次在 17 天前)之后,Q1 照样把它命中 —— 因为窗口锚在副本的创建时刻,
--   而合法老品牌的诊断早就跑完了。**必须不命中的那一半不喂进去,就发不现这个假阳。**
--
-- 列已核(不是照记忆写):
--   brands(id, name, owner_user_id, is_deleted, brand_type, created_at)
--     —— db/brands_schema.py 建表段 + get_or_create_brand 的 INSERT 列
--   pipeline_stage_log(id, brand_id, stage_name, event, meta JSONB,
--                      actor_user_id, created_at)
--     —— db/pipeline_stage_log_db.py:67-75 建表段
-- ============================================================================

-- ── Q0 · 分母与底噪:先知道这张表在这个窗口里有多大,再看下面的数 ──────────
SELECT 'Q0_denominator'                                   AS q,
       COUNT(*)                                           AS brands_alive,
       COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days')
                                                          AS brands_created_30d
  FROM public.brands
 WHERE COALESCE(is_deleted, false) = false;

-- ── Q1 · 疑似副本(指纹①+②都成立)────────────────────────────────────────
WITH alive AS (
    SELECT id, name, owner_user_id, brand_type, created_at
      FROM public.brands
     WHERE COALESCE(is_deleted, false) = false
),
pair AS (
    SELECT dup.id            AS dup_brand_id,
           dup.name          AS brand_name,
           dup.owner_user_id AS dup_owner,
           dup.created_at    AS dup_created_at,
           orig.id           AS orig_brand_id,
           orig.owner_user_id AS orig_owner,
           orig.created_at   AS orig_created_at
      FROM alive dup
      JOIN alive orig
        ON orig.name = dup.name
       AND orig.id <> dup.id
       AND orig.owner_user_id IS DISTINCT FROM dup.owner_user_id
       AND orig.created_at < dup.created_at
     WHERE dup.created_at >= NOW() - INTERVAL '30 days'
)
SELECT 'Q1_suspected_duplicates' AS q,
       p.*,
       (SELECT COUNT(*) FROM public.pipeline_stage_log l
         WHERE l.brand_id = p.dup_brand_id
           AND l.stage_name = 'diagnosis' AND l.event = 'complete'
           AND l.created_at BETWEEN p.dup_created_at - INTERVAL '10 minutes'
                               AND p.dup_created_at + INTERVAL '30 minutes')
                                          AS dup_diag_events_in_window,
       -- 🔴 这一侧**不设窗**:合法的多服务商同名,原品牌的诊断可能发生在
       --    很久以前,用窗口去问它会得到 0,于是合法样本被误判成缺陷
       --    (实测:干跑时 `合法同名_B` 正是这么被 Q1 误命中的)。
       (SELECT COUNT(*) FROM public.pipeline_stage_log l
         WHERE l.brand_id = p.orig_brand_id
           AND l.stage_name = 'diagnosis' AND l.event = 'complete')
                                          AS orig_diag_events_total
  FROM pair p
 ORDER BY p.dup_created_at DESC;
--   判读:``dup_diag_events_in_window > 0 AND orig_diag_events_total = 0``
--   的那些行 = 本缺陷的高置信命中(产物落在副本、真品牌同窗零产物)。
--   两个都 > 0 或两个都 = 0 的行**不要**直接算进影响面 —— 前者更像两个服务商
--   各自真的跑过,后者是产物还没写或窗口取窄了。

-- ── Q2 · 必须不命中的那一半(反向对照)──────────────────────────────────
--   同名跨 owner、但**两边都有**自己的诊断产物 = 正常的多服务商同名。
--   这一组的存在证明 Q1 的指纹**有区分力**;若这一组为 0,说明库里根本没有
--   合法同名样本,那 Q1 的"命中"就不能归因于指纹,只能归因于"同名很少见"。
WITH alive AS (
    SELECT id, name, owner_user_id FROM public.brands
     WHERE COALESCE(is_deleted, false) = false
)
SELECT 'Q2_reverse_control_legit_same_name' AS q,
       a.name,
       COUNT(DISTINCT a.owner_user_id) AS owners,
       COUNT(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM public.pipeline_stage_log l
            WHERE l.brand_id = a.id
              AND l.stage_name = 'diagnosis' AND l.event = 'complete'))
                                       AS brands_with_own_diagnosis
  FROM alive a
 GROUP BY a.name
HAVING COUNT(DISTINCT a.owner_user_id) > 1
 ORDER BY owners DESC
 LIMIT 50;

-- ── Q3 · 真品牌因此缺了什么(逐条,给处置用)──────────────────────────────
--   注意:这里只回答"诊断阶段事件"这一项。v2 报告与信任资产落在别的表上,
--   我没有生产只读权限去核那两张表的**列名**,所以**不在本文件里替它们写 SQL**
--   —— 照记忆写列名正是本仓 SQL 四维核验禁止的第一条。
--   需要那两项时请把 `\d` 结果给我,我补第二份。
WITH alive AS (
    SELECT id, name, owner_user_id, created_at FROM public.brands
     WHERE COALESCE(is_deleted, false) = false
)
SELECT 'Q3_original_brand_missing_diagnosis_event' AS q,
       orig.id            AS orig_brand_id,
       orig.name,
       orig.owner_user_id AS orig_owner,
       dup.id             AS dup_brand_id,
       dup.owner_user_id  AS dup_owner,
       dup.created_at      AS dup_created_at,
       (SELECT MAX(l.created_at) FROM public.pipeline_stage_log l
         WHERE l.brand_id = orig.id
           AND l.stage_name = 'diagnosis' AND l.event = 'complete')
                          AS orig_last_diagnosis_complete
  FROM alive dup
  JOIN alive orig
    ON orig.name = dup.name AND orig.id <> dup.id
   AND orig.owner_user_id IS DISTINCT FROM dup.owner_user_id
   AND orig.created_at < dup.created_at
 WHERE dup.created_at >= NOW() - INTERVAL '30 days'
   AND EXISTS (SELECT 1 FROM public.pipeline_stage_log l
                WHERE l.brand_id = dup.id
                  AND l.stage_name = 'diagnosis' AND l.event = 'complete')
   -- 🔴 原品牌**有史以来**一次诊断产物都没有,才算"因此缺了"。
   --    加窗会把「原品牌很久以前诊断过」的合法同名一起圈进来(干跑实测)。
   AND NOT EXISTS (SELECT 1 FROM public.pipeline_stage_log l
                    WHERE l.brand_id = orig.id
                      AND l.stage_name = 'diagnosis' AND l.event = 'complete')
 ORDER BY dup.created_at DESC;
