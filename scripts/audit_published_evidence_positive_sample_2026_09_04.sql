-- #54/#55 前置门 · 「已发布证据」这个数据源**有没有正样本**
-- ============================================================================
-- 工单要求「先只读复核 first_published_at 当前全表计数」。本文件比那多问三件,
-- 因为只数一个列答不了真正要回答的问题。
--
-- 🔴 为什么这是**前置门**,不是顺手看看:
--   上一单(#58)的教训 —— 我把「读一个不存在的列」改成「读一个正确但恒为空的列」,
--   **修是对的,对客可见效果可能是零**。本单如果「真实已发布 ≥ 1」这一态在生产上
--   一个样本都没有,那么三态里就只有两态可达,「满足」那一格
--   **永远不会被观测到**,判据里它只能靠夹具绿,而生产上它是死的。
--   ⇒ **先证正样本存在,再谈改法。** 若 Q2 与 Q3 都为 0,本单要先回 Review/Owner。
--
-- 🔴 还要问「事实源 vs 加速列」:
--   schema 注释写着 `articles.first_published_at` 是
--   「P0.4 denormalized · 首次发布时间(任一平台) · **事实源 media_publications** · 仅查询加速用」。
--   ⇒ 只数加速列,读到的可能是**缓存漂移后的数**。Q4 专门量这个漂移。
--
-- 执行(Deploy · 只读):
--     docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope <<'SQL'
--     BEGIN; SET TRANSACTION READ ONLY;
--     <本文件内容>
--     ROLLBACK;
--     SQL
--   🔴 只读自证的那条必失败写**必须另起一个事务**:同事务内它会把事务打进 abort 态,
--      之后每条 SELECT 只回表头 —— 与「真的全是 0」读数完全同形。
--        BEGIN; SET TRANSACTION READ ONLY;
--          UPDATE public.articles SET status = status WHERE false;   -- 必须报 read-only
--        ROLLBACK;
-- ============================================================================

-- ── Q1 · 分母自证:先证明连对了库、这两张表非空 ─────────────────────────────
--   🔴 若 articles_total = 0,下面每一个 0 的含义都是「库/表选错了」,
--      **不是**「没有已发布文章」。这两件事在读数上完全同形,必须先排除。
SELECT 'Q1_denominator'                                        AS q,
       (SELECT COUNT(*) FROM public.articles)                  AS articles_total,
       (SELECT COUNT(*) FROM public.media_publications)        AS media_publications_total,
       (SELECT COUNT(*) FROM public.quotes)                    AS quotes_total,
       (SELECT COUNT(DISTINCT quote_id) FROM public.articles
         WHERE quote_id IS NOT NULL)                           AS quotes_with_articles;

-- ── Q2 · 加速列口径:first_published_at 非空的篇数 ─────────────────────────
--   工单点名要的那个数。三档分开数,别把 NULL 与 0 压成一个读数。
SELECT 'Q2_first_published_at'                                 AS q,
       COUNT(*)                                                AS articles_total,
       COUNT(*) FILTER (WHERE first_published_at IS NOT NULL)  AS with_first_published,
       COUNT(*) FILTER (WHERE first_published_at IS NULL)      AS without_first_published,
       COUNT(DISTINCT quote_id) FILTER (WHERE first_published_at IS NOT NULL)
                                                               AS quotes_with_published,
       MIN(first_published_at)                                 AS earliest,
       MAX(first_published_at)                                 AS latest
  FROM public.articles;

-- ── Q3 · 事实源口径:media_publications 里有几条、覆盖几个 quote ───────────
--   🔴 与 Q2 分开问。若两者对不上,说明加速列漂了(见 Q4)。
SELECT 'Q3_media_publications'                                 AS q,
       COUNT(*)                                                AS rows_total,
       COUNT(DISTINCT quote_id)                                AS distinct_quotes,
       COUNT(DISTINCT article_id)                              AS distinct_articles,
       COUNT(*) FILTER (WHERE publish_date IS NOT NULL)        AS with_publish_date,
       COUNT(*) FILTER (WHERE publish_timestamp IS NOT NULL)   AS with_publish_timestamp,
       MIN(created_at)                                         AS earliest_row,
       MAX(created_at)                                         AS latest_row
  FROM public.media_publications;

-- ── Q4 · 🔴 加速列 vs 事实源的**漂移**:两个方向都要数 ────────────────────
--   一个方向为 0 不能推另一个方向也为 0 —— 漂移是有方向的。
--   · 有发布记录却没写 first_published_at ⇒ 用加速列会**漏判**(该满足的判成缺失);
--   · 写了 first_published_at 却没有发布记录 ⇒ 用加速列会**误判**(该缺失的判成满足)。
SELECT 'Q4_drift_between_cache_and_fact'                       AS q,
       (SELECT COUNT(*) FROM public.articles a
         WHERE a.first_published_at IS NULL
           AND EXISTS (SELECT 1 FROM public.media_publications m
                        WHERE m.article_id = a.id))            AS has_publication_but_no_cache,
       (SELECT COUNT(*) FROM public.articles a
         WHERE a.first_published_at IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM public.media_publications m
                            WHERE m.article_id = a.id))        AS has_cache_but_no_publication,
       (SELECT COUNT(*) FROM public.articles a
         WHERE a.first_published_at IS NOT NULL
           AND EXISTS (SELECT 1 FROM public.media_publications m
                        WHERE m.article_id = a.id))            AS both_agree;

-- ── Q5 · 三态在生产上各有多少个 quote(这才是本单真正要的分布)──────────────
--   报告的完整度是按**该次诊断关联的那张报价**算的,所以按 quote 分档。
--   🔴 三档必须互斥且穷尽:三个数之和 must == quotes_total(Q1)。
--      对不上说明我的分档谓词有洞,而不是「数据就是这样」。
WITH q AS (
    SELECT qt.id AS quote_id,
           (SELECT COUNT(*) FROM public.articles a
             WHERE a.quote_id = qt.id AND a.first_published_at IS NOT NULL)
                                                               AS pub_by_cache,
           (SELECT COUNT(*) FROM public.media_publications m
             WHERE m.quote_id = qt.id)                         AS pub_by_fact
      FROM public.quotes qt
)
SELECT 'Q5_tri_state_distribution_by_quote'                    AS q,
       COUNT(*)                                                AS quotes_total,
       COUNT(*) FILTER (WHERE pub_by_cache = 0)                AS cache_says_zero,
       COUNT(*) FILTER (WHERE pub_by_cache > 0)                AS cache_says_satisfied,
       COUNT(*) FILTER (WHERE pub_by_fact = 0)                 AS fact_says_zero,
       COUNT(*) FILTER (WHERE pub_by_fact > 0)                 AS fact_says_satisfied,
       COUNT(*) FILTER (WHERE (pub_by_cache > 0) <> (pub_by_fact > 0))
                                                               AS two_sources_disagree
  FROM q;

-- ── Q6 · 若有正样本,给出前 20 个,供判据挑真实夹具 ────────────────────────
--   🔴 若这一支 0 行:**「满足」那一态在生产上不可达**,
--      此时本单的判据只能靠合成夹具绿,而生产上那一格是死的 —— 必须先报回。
SELECT 'Q6_positive_samples'                                   AS q,
       a.quote_id, COUNT(*) AS published_articles,
       MIN(a.first_published_at) AS earliest_pub
  FROM public.articles a
 WHERE a.first_published_at IS NOT NULL AND a.quote_id IS NOT NULL
 GROUP BY a.quote_id
 ORDER BY published_articles DESC, a.quote_id
 LIMIT 20;
