"""Read-only R6 analysis for structures in answer-adopted GEO articles."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Final, Iterator

import psycopg2

from db.connection import get_connection
from services.article_structure_features import extract_article_structure_features
from services.media_entity_flywheel import industry_filter_values, normalize_domain, normalize_industry_key
from services.writing_style_feature_extractor import infer_style_family

logger = logging.getLogger("GEO-ArticleStructure")


BOOLEAN_FEATURES: dict[str, str] = {
    "title_has_year": "标题包含年份",
    "title_has_city_or_region": "标题包含城市或区域",
    "title_has_ranking_signal": "标题带榜单/推荐信号",
    "title_has_comparison_signal": "标题带对比/测评信号",
    "title_has_guide_signal": "标题带攻略/避坑信号",
    "title_question_form": "标题是问题句",
    "lead_answers_question": "开头先回答问题",
    "lead_has_summary_judgement": "开头先给判断",
    "lead_has_selection_criteria": "开头列选择标准",
    "lead_has_scope_boundary": "开头说明适用范围",
    "lead_is_generic_intro": "开头泛泛铺垫",
    "has_ranked_list": "正文有榜单/分层推荐",
    "has_checklist": "正文有选择清单",
    "has_faq_block": "正文有问答块",
    "has_case_block": "正文有案例块",
    "has_data_block": "正文有数据/报告",
    "has_price_or_budget": "正文有价格或预算",
    "has_customer_case": "正文有客户案例",
    "has_risk_or_pitfall": "正文有避坑/风险提示",
    "conclusion_has_decision_advice": "结尾给选择建议",
    "conclusion_is_salesy": "结尾销售感过强",
}

RULE_BY_FEATURE = {
    "lead_answers_question": "开头 200 字内先直接回答问题，再解释选择标准。",
    "lead_has_selection_criteria": "先列筛选维度，让 AI 更容易摘取判断框架。",
    "has_checklist": "正文使用清单或步骤组织，不要写成大段泛论。",
    "has_price_or_budget": "补充价格、预算或费用边界，提升可摘取证据密度。",
    "has_customer_case": "加入客户案例或真实场景，避免只有通用观点。",
    "has_risk_or_pitfall": "加入避坑和风险提示，保持中肯而非硬广。",
    "conclusion_has_decision_advice": "结尾给清晰选择建议，但不要承诺固定效果。",
}

ARTICLE_STRUCTURE_SQL = """
WITH signal_scope AS (
    -- [血缘三态 2026-08-01] 门从 WHERE 挪到条件聚合。
    -- 原写法 `WHERE lineage_status='complete' AND label_provenance_type='direct_observation'`
    -- 把「压根没标血缘(legacy_unknown)」和「标了但不合格」写进了同一个条件 —— 与 P1 发布门
    -- 同型病。生产 71,403/71,403 行全 legacy_unknown → 采纳组恒空 → 面板显示"0 篇 / 0.00 倍",
    -- 被当成"真没有"排查了两天。
    -- 现在:新口径(is_new_lineage)照旧独占效果对比;legacy 单独计数、单独成组,
    -- **不进 source_weight、不进 engine_count、不进 _feature_lift** —— 只做"旧口径样本"展示。
    SELECT *,
           (lineage_status = 'complete'
            AND label_provenance_type = 'direct_observation') AS is_new_lineage
      FROM geo_research_source_signals
     WHERE industry_key = %s
),
source_signal_by_url AS (
    SELECT source_url,
           industry_key,
           MAX(CASE WHEN is_new_lineage AND signal_tier = 'answer_adopted' THEN 1 ELSE 0 END) AS is_adopted,
           MAX(CASE WHEN is_new_lineage AND signal_tier = 'cited_source' THEN 1 ELSE 0 END) AS is_cited,
           MAX(CASE WHEN is_new_lineage AND signal_tier = 'search_result_only' THEN 1 ELSE 0 END) AS is_search_only,
           MAX(CASE WHEN is_new_lineage THEN COALESCE(balanced_weight, 0) ELSE 0 END) AS source_weight,
           -- [B1-1] engine 归一(小写别名/中文混存 → COUNT(DISTINCT) 会把同一引擎算多次)
           -- 非新口径行归 NULL,COUNT(DISTINCT) 自动忽略 → legacy 不污染引擎数
           COUNT(DISTINCT CASE WHEN is_new_lineage THEN (CASE
               WHEN LOWER(engine) IN ('doubao', '豆包') THEN '豆包'
               WHEN LOWER(engine) IN ('kimi') THEN 'Kimi'
               WHEN LOWER(engine) IN ('deepseek') THEN 'DeepSeek'
               WHEN LOWER(engine) IN ('qwen', '千问') THEN '千问'
               ELSE engine
           END) END) AS engine_count,
           MAX(CASE WHEN NOT is_new_lineage AND signal_tier = 'answer_adopted' THEN 1 ELSE 0 END) AS is_adopted_legacy,
           MAX(CASE WHEN NOT is_new_lineage AND signal_tier = 'cited_source' THEN 1 ELSE 0 END) AS is_cited_legacy,
           MAX(CASE WHEN NOT is_new_lineage AND signal_tier = 'search_result_only' THEN 1 ELSE 0 END) AS is_search_only_legacy
      FROM signal_scope
     GROUP BY source_url, industry_key
),
-- [P0 血缘三报超时根治 2026-08-16] 原为两级聚合:
--   article_source_signal  GROUP BY (article_id, cite_url)  → 8 列全 MAX
--   article_signal         GROUP BY article_id              → 对上面 8 列再 MAX
-- 八列**全部**是 MAX,而 MAX(MAX(x) over 细分组) ≡ MAX(x) over 粗分组
--   ⇒ 折叠成一级 GROUP BY article_id 是**恒等变换**,逐行同结果(见 tests/
--     test_lineage_report_timeout_2026_08_16.py 的 SQL 等价对照)。
--   中间层唯一多出的 raw.cite_url 只被 GROUP BY 消费,下游无人读 → 去掉无影响。
--
-- 🔴 AS MATERIALIZED 是**性能正确性**要求,不是优化偏好:
--   本聚合与外层 geo_research_articles 无任何相关性(不含外层列),但 PG 对
--   article 那 8 个 AND 谓词的选择率连乘后估成 rows=1(实测 781),于是选
--   Nested Loop 把本子树当内表**逐行重扫 781 次**:
--     Nested Loop Left Join … Rows Removed by Join Filter: 12,965,496
--   实测 10,641ms,且随 corpus_grade 命中数线性劣化(生产 JC5=1605 ≈ 2 倍)。
--   MATERIALIZED 强制只算一次,重扫退化为读 tuplestore。
article_signal AS MATERIALIZED (
    SELECT citation.article_id,
           MAX(COALESCE(sig.is_adopted, 0)) AS is_adopted,
           MAX(COALESCE(sig.is_cited, 0)) AS is_cited,
           MAX(COALESCE(sig.is_search_only, 0)) AS is_search_only,
           MAX(COALESCE(sig.source_weight, 0)) AS source_weight,
           MAX(COALESCE(sig.engine_count, 0)) AS engine_count,
           MAX(COALESCE(sig.is_adopted_legacy, 0)) AS is_adopted_legacy,
           MAX(COALESCE(sig.is_cited_legacy, 0)) AS is_cited_legacy,
           MAX(COALESCE(sig.is_search_only_legacy, 0)) AS is_search_only_legacy
      FROM geo_research_article_citations citation
      JOIN geo_research_raw raw ON raw.id = citation.raw_id
      LEFT JOIN source_signal_by_url sig ON sig.source_url = raw.cite_url
     WHERE raw.industry = ANY(%s)
     GROUP BY citation.article_id
),
scored AS (
SELECT article.id,
       article.url,
       article.domain,
       article.title,
       article.primary_industry,
       article.content_type,
       article.intent_type,
       article.inline_cleaned_content,
       article.oss_key_cleaned,
       article.cleaned_char_count,
       article.corpus_grade,
       article.canonical_body_hash,
       article.content_cluster_id,
       article.label_provenance_version,
       COALESCE(article_signal.is_adopted, 0) AS is_adopted,
       COALESCE(article_signal.is_cited, 0) AS is_cited,
       COALESCE(article_signal.is_search_only, 0) AS is_search_only,
       COALESCE(article_signal.source_weight, 0) AS source_weight,
       COALESCE(article_signal.engine_count, 0) AS engine_count,
       COALESCE(article_signal.is_adopted_legacy, 0) AS is_adopted_legacy,
       COALESCE(article_signal.is_cited_legacy, 0) AS is_cited_legacy,
       COALESCE(article_signal.is_search_only_legacy, 0) AS is_search_only_legacy,
       -- [包④ §3H-1] 本段读的是调研语料,信号是**实测**的 → TRUE。
       -- 与回退段的 FALSE 成对,让前端能区分「真没有」和「不适用」。
       TRUE AS signal_measured
  FROM geo_research_articles article
  LEFT JOIN article_signal ON article_signal.article_id = article.id
 WHERE article.primary_industry = ANY(%s)
   AND COALESCE(article.expired, FALSE) = FALSE
   AND COALESCE(article.review_status, '') <> 'rejected'
   AND COALESCE(article.clean_status, '') <> 'failed'
   AND article.corpus_grade = %s
   AND article.canonical_body_hash IS NOT NULL
   AND article.content_cluster_id IS NOT NULL
   AND article.label_provenance_version IS NOT NULL
   AND COALESCE(article.cleaned_char_count, CHAR_LENGTH(COALESCE(article.inline_cleaned_content, ''))) >= %s
)
, ranked AS (
    SELECT scored.*,
           ROW_NUMBER() OVER (
               -- [血缘三态] legacy 单列 partition,否则旧口径样本挤在 'reference' 里
               -- 被 top-N 截断 →「旧口径 N 篇」报不准。顺序必须与 _group_key 一致。
               PARTITION BY CASE WHEN is_adopted > 0 THEN 'adopted'
                                 WHEN is_cited > 0 THEN 'cited'
                                 WHEN is_search_only > 0 THEN 'search'
                                 WHEN is_adopted_legacy > 0
                                   OR is_cited_legacy > 0
                                   OR is_search_only_legacy > 0 THEN 'legacy'
                                 ELSE 'reference' END
               ORDER BY source_weight DESC, cleaned_char_count DESC NULLS LAST, id DESC
           ) AS group_rank
      FROM scored
)
SELECT * FROM ranked
 WHERE group_rank <= %s
 ORDER BY is_adopted DESC,
          is_cited DESC,
          source_weight DESC,
          cleaned_char_count DESC NULLS LAST,
          id DESC
"""


ARTICLE_STRUCTURE_ALL_SQL = """
WITH signal_scope AS (
    -- [血缘三态 2026-08-01] 口径与 ARTICLE_STRUCTURE_SQL 逐字一致,见那边注释。
    SELECT *,
           (lineage_status = 'complete'
            AND label_provenance_type = 'direct_observation') AS is_new_lineage
      FROM geo_research_source_signals
),
source_signal_by_url AS (
    SELECT source_url,
           MAX(CASE WHEN is_new_lineage AND signal_tier = 'answer_adopted' THEN 1 ELSE 0 END) AS is_adopted,
           MAX(CASE WHEN is_new_lineage AND signal_tier = 'cited_source' THEN 1 ELSE 0 END) AS is_cited,
           MAX(CASE WHEN is_new_lineage AND signal_tier = 'search_result_only' THEN 1 ELSE 0 END) AS is_search_only,
           MAX(CASE WHEN is_new_lineage THEN COALESCE(balanced_weight, 0) ELSE 0 END) AS source_weight,
           -- [B1-1] engine 归一(同 ARTICLE_STRUCTURE_SQL)
           COUNT(DISTINCT CASE WHEN is_new_lineage THEN (CASE
               WHEN LOWER(engine) IN ('doubao', '豆包') THEN '豆包'
               WHEN LOWER(engine) IN ('kimi') THEN 'Kimi'
               WHEN LOWER(engine) IN ('deepseek') THEN 'DeepSeek'
               WHEN LOWER(engine) IN ('qwen', '千问') THEN '千问'
               ELSE engine
           END) END) AS engine_count,
           MAX(CASE WHEN NOT is_new_lineage AND signal_tier = 'answer_adopted' THEN 1 ELSE 0 END) AS is_adopted_legacy,
           MAX(CASE WHEN NOT is_new_lineage AND signal_tier = 'cited_source' THEN 1 ELSE 0 END) AS is_cited_legacy,
           MAX(CASE WHEN NOT is_new_lineage AND signal_tier = 'search_result_only' THEN 1 ELSE 0 END) AS is_search_only_legacy
      FROM signal_scope
     GROUP BY source_url
),
-- [P0 血缘三报超时根治 2026-08-16] 两级 MAX 折叠成一级 + MATERIALIZED。
-- 口径与理由与 ARTICLE_STRUCTURE_SQL 逐字一致,见那边注释。
article_signal AS MATERIALIZED (
    SELECT citation.article_id,
           MAX(COALESCE(sig.is_adopted, 0)) AS is_adopted,
           MAX(COALESCE(sig.is_cited, 0)) AS is_cited,
           MAX(COALESCE(sig.is_search_only, 0)) AS is_search_only,
           MAX(COALESCE(sig.source_weight, 0)) AS source_weight,
           MAX(COALESCE(sig.engine_count, 0)) AS engine_count,
           MAX(COALESCE(sig.is_adopted_legacy, 0)) AS is_adopted_legacy,
           MAX(COALESCE(sig.is_cited_legacy, 0)) AS is_cited_legacy,
           MAX(COALESCE(sig.is_search_only_legacy, 0)) AS is_search_only_legacy
      FROM geo_research_article_citations citation
      JOIN geo_research_raw raw ON raw.id = citation.raw_id
      LEFT JOIN source_signal_by_url sig ON sig.source_url = raw.cite_url
     GROUP BY citation.article_id
),
scored AS (
SELECT article.id,
       article.url,
       article.domain,
       article.title,
       article.primary_industry,
       article.content_type,
       article.intent_type,
       article.inline_cleaned_content,
       article.oss_key_cleaned,
       article.cleaned_char_count,
       article.corpus_grade,
       article.canonical_body_hash,
       article.content_cluster_id,
       article.label_provenance_version,
       COALESCE(article_signal.is_adopted, 0) AS is_adopted,
       COALESCE(article_signal.is_cited, 0) AS is_cited,
       COALESCE(article_signal.is_search_only, 0) AS is_search_only,
       COALESCE(article_signal.source_weight, 0) AS source_weight,
       COALESCE(article_signal.engine_count, 0) AS engine_count,
       COALESCE(article_signal.is_adopted_legacy, 0) AS is_adopted_legacy,
       COALESCE(article_signal.is_cited_legacy, 0) AS is_cited_legacy,
       COALESCE(article_signal.is_search_only_legacy, 0) AS is_search_only_legacy,
       -- [包④ §3H-1] 本段读的是调研语料,信号是**实测**的 → TRUE。
       -- 与回退段的 FALSE 成对,让前端能区分「真没有」和「不适用」。
       TRUE AS signal_measured
  FROM geo_research_articles article
  LEFT JOIN article_signal ON article_signal.article_id = article.id
 WHERE COALESCE(article.expired, FALSE) = FALSE
   AND COALESCE(article.review_status, '') <> 'rejected'
   AND COALESCE(article.clean_status, '') <> 'failed'
   AND article.corpus_grade = %s
   AND article.canonical_body_hash IS NOT NULL
   AND article.content_cluster_id IS NOT NULL
   AND article.label_provenance_version IS NOT NULL
   AND COALESCE(article.cleaned_char_count, CHAR_LENGTH(COALESCE(article.inline_cleaned_content, ''))) >= %s
)
, ranked AS (
    SELECT scored.*,
           ROW_NUMBER() OVER (
               -- [血缘三态] legacy 单列 partition,否则旧口径样本挤在 'reference' 里
               -- 被 top-N 截断 →「旧口径 N 篇」报不准。顺序必须与 _group_key 一致。
               PARTITION BY CASE WHEN is_adopted > 0 THEN 'adopted'
                                 WHEN is_cited > 0 THEN 'cited'
                                 WHEN is_search_only > 0 THEN 'search'
                                 WHEN is_adopted_legacy > 0
                                   OR is_cited_legacy > 0
                                   OR is_search_only_legacy > 0 THEN 'legacy'
                                 ELSE 'reference' END
               ORDER BY source_weight DESC, cleaned_char_count DESC NULLS LAST, id DESC
           ) AS group_rank
      FROM scored
)
SELECT * FROM ranked
 WHERE group_rank <= %s
 ORDER BY is_adopted DESC,
          is_cited DESC,
          source_weight DESC,
          cleaned_char_count DESC NULLS LAST,
          id DESC
"""


#: [飞轮收尾包④ §3H-1 · 2026-08-01] 语料等级由高到低的**动态回退阶梯**。
#:
#: 🔴 根因订正(工单假设不成立,已实测):JC5 门的判定 SQL **完全正常** ——
#: 生产 dry-run 实跑 **781 篇合格**、`geo_research_articles` 里 JC3 有 982 篇且
#: body_hash / cluster_id 三门全通。JC5 全表为 0 的真因在**执行侧**:
#: 升级动作 `promote_jc5_from_direct_signals` 被埋在「每月 1/16 日 05:10 + flag 门控」
#: 的半月进化审计 job 里,而 `geo_research_corpus_label_events` **0 行**
#: —— 它从来没有成功产出过任何一次。
#: 所以**绝不能"照面值放宽门"**(那会把没经过血缘核验的样本当成 JC5)。
#: 正确做法是:判据一字不改,只在**查询端**按"现存最高等级"取样,并把实际取到的
#: 等级如实告诉页面,让用户看到「当前样本等级 JC3(JC5 尚无)」而不是一片空白。
CORPUS_GRADE_LADDER: Final = ("JC5", "JC3", "JC0")


def resolve_effective_corpus_grade(cur) -> str:
    """返回**现存**样本里的最高语料等级。全空时回落阶梯末级(行为与旧版一致:取不到就空)。

    🔴 只看"有没有行",不改任何判定条件 —— 降级取样 ≠ 放宽门。
    """
    cur.execute(
        "SELECT corpus_grade, COUNT(*) AS n FROM geo_research_articles "
        "WHERE corpus_grade = ANY(%s) GROUP BY corpus_grade",
        (list(CORPUS_GRADE_LADDER),),
    )
    present = {str(r["corpus_grade"]): int(r["n"]) for r in cur.fetchall() if r["n"]}
    for grade in CORPUS_GRADE_LADDER:
        if present.get(grade):
            return grade
    return CORPUS_GRADE_LADDER[-1]


GENERATED_ARTICLES_ALL_SQL = """
SELECT ('live-' || article.id::text) AS id,
       '' AS url,
       'generated-article' AS domain,
       COALESCE(article.title, '') AS title,
       'all_articles' AS primary_industry,
       COALESCE(article.style, 'generated') AS content_type,
       'generated_article' AS intent_type,
       COALESCE(article.content, '') AS inline_cleaned_content,
       GREATEST(COALESCE(article.word_count, 0), CHAR_LENGTH(COALESCE(article.content, ''))) AS cleaned_char_count,
       -- [飞轮收尾包④ §3H-1 · 2026-08-01] 这几个 0 **不是测出来的 0,是"不适用"**。
       -- 本段读的是 `articles`(我方生成稿),而 `geo_research_source_signals.article_id`
       -- 指向的是 `geo_research_articles`(外部调研语料)—— 两张表压根不是同一批对象,
       -- 我方草稿身上不可能有调研信号。
       -- 🔴 原样保留 0(下游按 0 做排序/分组,改成 NULL 会连锁),但**必须显式标出
       --    "这批没测过"**,否则页面把"不适用"渲染成"实测采纳率 0",
       --    看起来像我们写的文章一篇都没被引用 —— 与事实完全相反的结论。
       --    §3H-2 的前端空态字段消费的就是这一列。
       0 AS is_adopted,
       0 AS is_cited,
       0 AS is_search_only,
       0 AS source_weight,
       0 AS engine_count,
       FALSE AS signal_measured,
       1 AS is_generated_article
  FROM articles article
 WHERE COALESCE(article.content, '') <> ''
   AND GREATEST(COALESCE(article.word_count, 0), CHAR_LENGTH(COALESCE(article.content, ''))) >= %s
 ORDER BY article.id DESC
 LIMIT %s
"""


# [B1-3 · review fix] 单次 analyze 的 OSS 回读上限:该分析被 dashboard GET 自动加载,
# 每条 OSS 行都是一次未缓存网络下载,跑在共享线程池上。不设上限时大文章集会打满线程池 + 反复 OSS 流量。
# 取样分析只需有限样本即可稳态,故封顶 N 条(超出的 OSS 行按原空正文处理,不参与,不报错)。
_MAX_OSS_BACKFILL = 40


def _backfill_oss_bodies(
    rows: list[dict[str, Any]],
    oss_backfill_cap: int | None = None,
) -> int:
    """[B1-3] 正文卸载到 OSS 的文章(inline_cleaned_content 为空但有 oss_key_cleaned):
    复用既有 helper 回读正文并写回 row["cleaned_content"],让结构特征提取器读到真实正文,
    避免空正文行稀释 adopted_group 的 feature_share。返回回读成功行数。禁自造 OSS 客户端。
    [review fix] OSS 回读封顶,防 dashboard 自动加载打满共享线程池。
    [W1.3] 上限参数化:dashboard 默认 40;后台蒸馏/rebuild 路径可传更高值(≥1000)分页批读,
    分析不再被 40 条稀释。默认 None → 调用时读 _MAX_OSS_BACKFILL,保持 dashboard 字节一致
    (常量必须运行时解析,def 时绑定会让封顶护栏单测的 monkeypatch 失效)。"""
    cap = max(0, int(oss_backfill_cap if oss_backfill_cap is not None else _MAX_OSS_BACKFILL))
    loaded = 0
    skipped_over_cap = 0
    for row in rows:
        inline = str(row.get("inline_cleaned_content") or row.get("cleaned_content") or "")
        oss_key = row.get("oss_key_cleaned")
        if inline or not oss_key:
            continue
        if loaded >= cap:
            skipped_over_cap += 1
            continue
        try:
            from services.research_monitor.oss_helper import download_markdown
            body = download_markdown(oss_key) or ""
            if body:
                row["cleaned_content"] = body
                loaded += 1
        except Exception as e:
            logger.warning("[article-structure] OSS 正文回读失败 article_id=%s: %s", row.get("id"), e)
    if skipped_over_cap:
        logger.info("[article-structure] OSS 回读封顶 %s 条,跳过 %s 条(未参与取样)",
                    cap, skipped_over_cap)
    return loaded


class ReportComputeTimeout(RuntimeError):
    """血缘三报计算超时。

    🔴 **绝不允许被 fail-soft 吞成 0**。飞轮 panorama 的 fail-soft 把 2026-08-16
    那场事故伪装成「运行正常 · 0 篇」,排查方按「真没有数据」查了两天。
    超时 ≠ 没有数据,两者处置完全相反,必须在响应里分得清。
    """

    def __init__(self, scope: str, timeout_ms: int):
        self.scope = scope
        self.timeout_ms = timeout_ms
        super().__init__(f"血缘三报计算超时({scope} · 上限 {timeout_ms}ms)")


def _timeout_ms(env_name: str, default_ms: int) -> int:
    """可配置超时(毫秒)。非法值回落默认,并夹在 [1s, 10min] 内防手滑写成 0=无限。"""
    raw = (os.getenv(env_name) or "").strip()
    try:
        value = int(raw) if raw else default_ms
    except ValueError:
        logger.warning("[article-structure] %s=%r 不是整数,回落 %sms", env_name, raw, default_ms)
        value = default_ms
    return max(1000, min(value, 600_000))


#: 工单 §1 止血层。生产 statement_timeout 全局为 0(= 不限),一条报表 SQL 能跑到天荒地老。
REPORT_STATEMENT_TIMEOUT_ENV: Final = "LINEAGE_REPORT_STATEMENT_TIMEOUT_MS"
#: 🔴 本次事故的**真形态**:SQL 早就跑完了,事务却因为应用侧在做 OSS 回读而一直不关。
#: statement_timeout 对此**完全无效**(实证 .probe/lock_chain.sh A 态:加了 5s
#: statement_timeout,ALTER 照堵、后续 SELECT 照排死)。所以必须成对再加一条。
REPORT_IDLE_IN_TXN_TIMEOUT_ENV: Final = "LINEAGE_REPORT_IDLE_TXN_TIMEOUT_MS"


@contextmanager
def _bounded_report_txn(scope: str) -> Iterator[Any]:
    """在**有界事务**里跑血缘三报查询,退出即结束事务、归还连接。

    两道闸成对,缺一不可(见 .probe/lock_chain.sh 三态实证):
      - statement_timeout          → 掐住跑飞的 SQL
      - idle_in_transaction_...    → 掐住「查完了但事务不关」(本次事故形态)
    两者都用 set_config(..., is_local := true):只作用于本事务,不污染连接池里
    别的会话(SET LOCAL 不能带绑定参数,set_config 可以)。
    """
    stmt_ms = _timeout_ms(REPORT_STATEMENT_TIMEOUT_ENV, 30_000)
    idle_ms = _timeout_ms(REPORT_IDLE_IN_TXN_TIMEOUT_ENV, 30_000)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(stmt_ms),))
        cur.execute("SELECT set_config('idle_in_transaction_session_timeout', %s, true)", (str(idle_ms),))
        try:
            yield cur
            conn.commit()
        except psycopg2.errors.QueryCanceled as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error("[article-structure] %s 计算超时(>%sms),返回 degraded 不吞成 0", scope, stmt_ms)
            raise ReportComputeTimeout(scope, stmt_ms) from exc
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


def _load_rows(
    industry: str,
    limit: int,
    min_chars: int,
    oss_backfill_cap: int | None = None,
) -> tuple[str, list[str], list[dict[str, Any]], str]:
    """[包④ §3H-2] 第 4 个返回值 = **实际取样用的语料等级**。

    页面必须能区分「真没有数据」和「被门滤掉了」—— 只回 rows 的话这两种情况
    在前端长得一模一样(都是空列表),而它们的处置完全相反:
    前者要等数据,后者要说明"当前样本等级 JC3(JC5 尚无)"。
    """
    # 🔴 [P0 2026-08-16] `_backfill_oss_bodies` **必须留在事务外**。
    # 它对每篇正文卸载到 OSS 的文章做一次**未缓存网络下载**,后台路径 cap 可达 1000
    # (corpus-export 传 limit=1000)。原实现把它写在 `cur.execute` 之后、`conn.close()`
    # 之前 —— 于是这上千次网络往返全程扛着该事务对 geo_research_* 的 ACCESS SHARE 锁。
    # prestart 的 ALTER 一来就排在后面(granted=f),PG 锁队列 FIFO 把全站 SELECT 排死。
    # 这正是 2026-08-16 那 5 条 xact 3831–3865s 的形态:**事务老 64 分钟,不是某条 SQL 慢**。
    # 所以取完数就结束事务,网络 I/O 挪到事务之外做(实证见 .probe/lock_chain.sh C 态)。
    industry_key = normalize_industry_key(industry)
    if industry_key in {"", "general", "all", "all_articles"}:
        safe_min_chars = max(0, int(min_chars or 0))
        safe_limit = max(1, int(limit or 300))
        with _bounded_report_txn("article_structure_all") as cur:
            # [包④ §3H-1] 按现存最高等级取样(JC5 尚无 → 自动落到 JC3),判据一字未改。
            effective_grade = resolve_effective_corpus_grade(cur)
            cur.execute(
                ARTICLE_STRUCTURE_ALL_SQL,
                (effective_grade, safe_min_chars, safe_limit),
            )
            pool = [dict(row) for row in cur.fetchall()]
            rows = _stratified_sample(pool, safe_limit)  # [T2] 四组配额,防对照组被采纳挤空
            remaining = max(0, safe_limit - len(rows))
            if remaining:
                cur.execute(
                    GENERATED_ARTICLES_ALL_SQL,
                    (safe_min_chars, remaining),
                )
                rows.extend(dict(row) for row in cur.fetchall())
        # ↑ 事务已结束、连接已归还 ↓ 下面是网络 I/O,不再持任何表锁
        _backfill_oss_bodies(rows, oss_backfill_cap)
        return "general", [], rows, effective_grade

    industry_values = industry_filter_values(industry or industry_key) or [industry_key]
    safe_limit = max(1, int(limit or 300))
    with _bounded_report_txn("article_structure_industry") as cur:
        # [包④ §3H-1] 同上:动态取现存最高等级,判据不变。
        effective_grade = resolve_effective_corpus_grade(cur)
        cur.execute(
            ARTICLE_STRUCTURE_SQL,
            (industry_key, industry_values, industry_values, effective_grade,
             max(0, int(min_chars or 0)), safe_limit),
        )
        pool = [dict(row) for row in cur.fetchall()]
        rows = _stratified_sample(pool, safe_limit)  # [T2] 四组配额,防对照组被采纳挤空
    # ↑ 同上:事务先关,OSS 回读在事务外
    # [review fix] 行业分支同样贯穿 cap,后台蒸馏不被 40 条稀释
    _backfill_oss_bodies(rows, oss_backfill_cap)
    return industry_key, industry_values, rows, effective_grade


def _group_key(row: dict[str, Any]) -> str:
    # 新口径优先:只要有 direct_observation 血缘的信号,就按它归组(与改造前逐字同义)。
    if int(row.get("is_adopted") or 0) > 0:
        return "adopted_group"
    if int(row.get("is_cited") or 0) > 0:
        return "cited_group"
    if int(row.get("is_search_only") or 0) > 0:
        return "search_only_control_group"
    # [血缘三态 2026-08-01] 只有旧口径信号(legacy_unknown)的文章单列一组。
    # 该组**不参与** _feature_lift / _SAMPLE_STRATA / status_label 判定 —— 只做展示,
    # 避免把"血缘未溯源"的样本混进效果对比的权重里。
    if (int(row.get("is_adopted_legacy") or 0) > 0
            or int(row.get("is_cited_legacy") or 0) > 0
            or int(row.get("is_search_only_legacy") or 0) > 0):
        return "legacy_lineage_group"
    return "reference_group"


# [T2 分层采样 2026-07-03] 四组配额:采纳/引用/搜索对照/参考。SQL 侧 window 已取组内 top-N,
#   这里按配额挑;某组 universe 不足配额时余额让给其他组。防 adopted 占满 LIMIT 挤光对照组
#   (原 ORDER BY is_adopted DESC LIMIT 300 → 1896 采纳把 300 名额占满,对照组全 0 无法算 lift)。
_SAMPLE_STRATA: tuple[tuple[str, float], ...] = (
    ("adopted_group", 0.40),
    ("cited_group", 0.20),
    ("search_only_control_group", 0.30),
    ("reference_group", 0.10),
)

# [血缘三态 2026-08-01] legacy 组走**独立预算**,不写进 _SAMPLE_STRATA。
# 理由:塞进 strata 会按比例挤占原四组配额 → 改变 _feature_lift 的输入 → 效果对比口径变了。
# 「不混权重」要求 legacy 对分析零影响,所以它在 limit 之外单独取,只用于展示计数。
_LEGACY_GROUP_KEY = "legacy_lineage_group"
_LEGACY_SAMPLE_CAP = 100


def _stratified_sample(pool: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    limit = max(1, int(limit or 300))
    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k, _ in _SAMPLE_STRATA}
    buckets[_LEGACY_GROUP_KEY] = []
    for row in pool:
        # pool 已按 group_rank(组内 source_weight/char/id 降序)排序,直接顺序取即最优先。
        buckets[_group_key(row)].append(row)
    quotas = {k: int(limit * frac) for k, frac in _SAMPLE_STRATA}
    quotas["adopted_group"] += limit - sum(quotas.values())  # 取整余额归采纳组
    selected: dict[str, list[dict[str, Any]]] = {}
    for k, _ in _SAMPLE_STRATA:
        selected[k] = buckets[k][: min(quotas[k], len(buckets[k]))]
    remaining = limit - sum(len(v) for v in selected.values())
    if remaining > 0:  # 溢出:未填满的配额让给还有存量的组
        for k, _ in _SAMPLE_STRATA:
            if remaining <= 0:
                break
            extra = len(buckets[k]) - len(selected[k])
            if extra > 0:
                take = min(extra, remaining)
                selected[k] = buckets[k][: len(selected[k]) + take]
                remaining -= take
    out: list[dict[str, Any]] = []
    for k, _ in _SAMPLE_STRATA:
        out.extend(selected[k])
    # legacy 在 limit 之外追加(见 _LEGACY_SAMPLE_CAP 注释):上面四组的取样结果
    # 与改造前逐字一致,legacy 只是额外挂在后面供计数/展示。
    out.extend(buckets[_LEGACY_GROUP_KEY][:_LEGACY_SAMPLE_CAP])
    return out


def _feature_share(items: list[dict[str, Any]]) -> dict[str, float]:
    if not items:
        return {}
    return {
        feature: round(sum(1 for item in items if item["features"].get(feature)) / len(items), 4)
        for feature in BOOLEAN_FEATURES
    }


def _group_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    domains = {
        normalize_domain(item.get("domain") or item.get("url") or "")
        for item in items
        if item.get("domain") or item.get("url")
    }
    engine_count = max([int(item.get("engine_count") or 0) for item in items] or [0])
    return {
        "count": len(items),
        "domain_count": len([d for d in domains if d]),
        "engine_count": engine_count,
        "feature_share": _feature_share(items),
        "examples": [
            {
                "title": item.get("title") or "",
                "domain": normalize_domain(item.get("domain") or item.get("url") or ""),
                "url": item.get("url") or "",
                "source_weight": float(item.get("source_weight") or 0),
            }
            for item in items[:8]
        ],
    }


def _feature_lift(adopted: dict[str, Any], control: dict[str, Any]) -> list[dict[str, Any]]:
    adopted_share = adopted.get("feature_share") or {}
    control_share = control.get("feature_share") or {}
    rows: list[dict[str, Any]] = []
    for feature, label in BOOLEAN_FEATURES.items():
        adopted_value = float(adopted_share.get(feature) or 0)
        control_value = float(control_share.get(feature) or 0)
        lift = round(adopted_value / max(control_value, 0.05), 3)
        rows.append({
            "feature": feature,
            "label": label,
            "adopted_share": adopted_value,
            "control_share": control_value,
            "lift": lift,
            "recommended": bool(lift >= 1.5 and adopted_value >= 0.2 and feature != "conclusion_is_salesy"),
        })
    return sorted(rows, key=lambda row: (row["recommended"], row["lift"], row["adopted_share"]), reverse=True)


def _recommended_rules(feature_lift: list[dict[str, Any]]) -> list[str]:
    rules: list[str] = []
    for row in feature_lift:
        rule = RULE_BY_FEATURE.get(row["feature"])
        if row.get("recommended") and rule and rule not in rules:
            rules.append(rule)
    return rules[:6]


# [V4] 结构规则「行业化」LLM 建议进程缓存(按 industry_key + recommended lift 指纹)。
_V4_RULES_CACHE: dict[str, list[str]] = {}


def _v4_rules_fingerprint(industry_key: str, feature_lift: list[dict[str, Any]]) -> str:
    """按 (行业 + 全部 recommended 行的 feature:lift:采纳占比) 做指纹,数据不变不重跑。"""
    from writing.flywheel_llm import fingerprint

    rec = [f"{r['feature']}:{r['lift']}:{r['adopted_share']}" for r in feature_lift if r.get("recommended")]
    return fingerprint(industry_key or "", *sorted(rec))


def generate_industry_rules_llm(
    feature_lift: list[dict[str, Any]],
    industry_key: str,
    adopted_count: int,
    control_count: int,
    *,
    generate: bool = True,
) -> list[str] | None:
    """[V4] 把「该行业真实 recommended 结构规律」说成人话的行业化写作建议(3-6 条,给运营看)。

    🔴 只改建议文案的**生成方式**,绝不碰 recommended 判定 / lift 数字(纯统计阈值 lift≥1.5 保持)。
    - generate=False(analyze GET 自动加载路径):**只取缓存,绝不内联调 LLM**(否则每次开看板慢 + LLM
      失败会阻断整块看板返回);默认 None → 前端回退静态 RULE_BY_FEATURE。
    - generate=True(手动[刷新行业化建议]POST):miss 才调 LLM 一次,按指纹缓存;守卫拦截/失败 → None 回退静态。
    """
    recommended = [r for r in feature_lift if r.get("recommended")]
    if not recommended:
        return None
    fp = _v4_rules_fingerprint(industry_key, feature_lift)
    cached = _V4_RULES_CACHE.get(fp)
    if cached is not None:
        return cached
    if not generate:
        return None
    try:
        from writing.flywheel_llm import call_flywheel_llm_sync, guard_output

        lines = "\n".join(
            f"- 「{r['label']}」:被采纳文章 {round(r['adopted_share'] * 100)}% 具备,"
            f"对照组仅 {round(r['control_share'] * 100)}%(lift {r['lift']})"
            for r in recommended[:8]
        )
        system = (
            "你是 GEO 内容策略顾问。根据给定的「某行业真实被采纳文章的结构规律」,给运营 3-6 条可执行的"
            "行业化写作建议(人话,每条一句、以「- 」开头)。只依据给定规律,禁止编造新数字或新规律;"
            "禁止提及任何 AI 模型或服务商名称;禁止承诺、保证类话术。只返回建议列表。"
        )
        user = (
            f"行业:{industry_key or '通用'}\n"
            f"样本:被采纳 {adopted_count} 篇 / 对照 {control_count} 篇\n"
            f"真实结构规律(lift 越高越显著):\n{lines}\n"
            f"请给 3-6 条行业化写作建议。"
        )
        text = call_flywheel_llm_sync("structure_rules", system, user, max_tokens=400)
        cleaned, blocked = guard_output(text or "")
        if not (text or "").strip() or blocked:
            return None
        rules = [ln.strip().lstrip("-•·*0123456789. ").strip() for ln in cleaned.splitlines()]
        rules = [r for r in rules if len(r) >= 4][:6]
        if not rules:
            return None
        _V4_RULES_CACHE[fp] = rules
        return rules
    except Exception as exc:  # LLM 建议失败绝不阻断看板,回退静态
        logger.warning("[structure-rules-llm] 生成失败(忽略): %s", str(exc)[:200])
        return None


def analyze_article_structure_patterns(
    industry: str,
    limit: int = 300,
    min_chars: int = 500,
    oss_backfill_cap: int | None = None,
    llm_rules: bool = False,
) -> dict[str, Any]:
    # [W1.3] oss_backfill_cap 默认 40(dashboard GET 路径不变、字节一致);后台蒸馏/rebuild
    #        传更高值(如 = limit)让 OSS 正文分页批读 ≥1000 篇,分析不被 40 条稀释。
    industry_key, industry_values, rows, effective_grade = _load_rows(
        industry, limit=limit, min_chars=min_chars, oss_backfill_cap=oss_backfill_cap
    )
    grouped: dict[str, list[dict[str, Any]]] = {
        "adopted_group": [],
        "cited_group": [],
        "search_only_control_group": [],
        "reference_group": [],
        _LEGACY_GROUP_KEY: [],
    }
    for row in rows:
        features = extract_article_structure_features(row)
        grouped[_group_key(row)].append({**row, "features": features})

    groups = {key: _group_payload(value) for key, value in grouped.items()}
    # [血缘三态] lift 只吃新口径两组 —— legacy 组绝不进这里(不混权重)。
    lift = _feature_lift(groups["adopted_group"], groups["search_only_control_group"])
    adopted_count = groups["adopted_group"]["count"]
    control_count = groups["search_only_control_group"]["count"]
    legacy_count = groups[_LEGACY_GROUP_KEY]["count"]
    engine_count = max(group.get("engine_count") or 0 for group in groups.values())
    status_label = "ready" if adopted_count >= 30 and control_count >= 30 else "observing"
    # [血缘三态] legacy 走 limit 之外的独立预算,不能算进「分析样本数」,
    # 否则 loaded 会虚高、且随旧数据量漂移。统计口径只数进分析的那四组。
    analysis_rows = [r for r in rows if _group_key(r) != _LEGACY_GROUP_KEY]
    generated_article_count = sum(
        1 for row in analysis_rows if int(row.get("is_generated_article") or 0) > 0
    )
    research_article_count = len(analysis_rows) - generated_article_count
    # [V4] 行业化 LLM 建议:GET 自动加载(llm_rules=False)只取缓存不调 LLM;手动刷新 POST 才生成。
    # 静态 recommended_structure_rules(下方)逐字不变;LLM 版是 additive 字段,前端有则用、无则回退静态。
    rules_llm = (
        generate_industry_rules_llm(lift, industry_key, adopted_count, control_count, generate=llm_rules)
        if status_label == "ready" else None
    )

    # [包④ §3H-2] 空态字段:让前端能区分「真没有」和「被门滤掉了」。
    # 两种情况在旧接口里长得一模一样(都是空 groups),而处置完全相反 ——
    # 前者要等数据,后者要告诉用户"当前看的是 JC3 样本,JC5 还没有"。
    measured_rows = sum(1 for r in analysis_rows if r.get("signal_measured"))
    return {
        "status": "success",
        "industry_key": industry_key,
        "industry_values": industry_values,
        # 实际取样等级 + 是否已降级(JC5 尚无 → 自动落 JC3)
        "effective_corpus_grade": effective_grade,
        "corpus_grade_degraded": effective_grade != CORPUS_GRADE_LADDER[0],
        "corpus_grade_notice": (
            "" if effective_grade == CORPUS_GRADE_LADDER[0]
            else f"当前样本等级 {effective_grade}({CORPUS_GRADE_LADDER[0]} 尚无)"
        ),
        # 🔴 有多少行的信号是**实测**的。我方生成稿没有调研信号,它们的 0 是
        # "不适用"而不是"实测为 0" —— 前端拿这个数决定能不能说"采纳率 0%"。
        "signal_measured_rows": measured_rows,
        "signal_unmeasured_rows": len(analysis_rows) - measured_rows,
        "dry_run": True,
        "shadow_only": True,
        "production_takeover": False,
        "loaded": len(analysis_rows),
        "research_article_count": research_article_count,
        "generated_article_count": generated_article_count,
        "sample_status": status_label,
        "engine_count": engine_count,
        "groups": groups,
        "feature_lift": lift,
        "recommended_structure_rules": _recommended_rules(lift) if status_label == "ready" else [],
        "recommended_structure_rules_llm": rules_llm,  # [V4] additive:行业化 LLM 建议(未生成/未 ready → None)
        "warnings": [] if status_label == "ready" else ["样本观察中：答案采纳或搜索曝光对照样本不足，不能生成强结论。"],
        # [血缘三态 2026-08-01] legacy 单列 + 空态区分。
        # 事故教训:采纳组 0 篇时面板只显示"0 篇",Owner 与排查方都读成"真没有",
        # 实际是被血缘门滤掉的旧口径样本。空态必须自己说清是哪一种。
        "legacy_lineage": {
            "count": legacy_count,
            "label": "旧口径样本（血缘未溯源）",
            "note": "这些样本产生于 research-source-lineage-v1.0 之前，无法回溯到具体"
                    "文章正文快照，因此不参与效果对比与权重计算，仅作历史参考。",
            "counts_toward_analysis": False,
        },
        "empty_state_reason": (
            None if adopted_count > 0
            else ("filtered_by_lineage_gate" if legacy_count > 0 else "no_data")
        ),
        "empty_state_message": (
            "" if adopted_count > 0
            else (f"新口径采纳样本 0 篇，但另有 {legacy_count} 篇旧口径样本被血缘门排除"
                  "——这不是“没有数据”，是“数据无法溯源”。"
                  if legacy_count > 0
                  else "该行业暂无任何采纳样本（新旧口径都没有）。")
        ),
        "source": "geo_research_articles + geo_research_source_signals + articles",
    }


def load_labeled_article_rows(
    industry: str,
    limit: int = 1000,
    min_chars: int = 500,
    oss_backfill_cap: int | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """[W2] 供蒸馏器复用同一套加载 + 21 特征提取 + 分组口径,返回逐行数据(不做聚合)。

    每行附加:`features`(21 布尔特征 · 与 analyze 完全同口径)、`group_key`
    (adopted_group/cited_group/search_only_control_group/reference_group)、`body`(回读正文)。
    后台路径默认放开 OSS 回读上限(= max(40, limit)),分析不被 40 条稀释。禁重造加载/提取逻辑。
    """
    cap = oss_backfill_cap if oss_backfill_cap is not None else max(_MAX_OSS_BACKFILL, int(limit or 0))
    industry_key, _industry_values, rows, _effective_grade = _load_rows(
        industry, limit=limit, min_chars=min_chars, oss_backfill_cap=cap
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        features = extract_article_structure_features(row)
        body = str(row.get("cleaned_content") or row.get("inline_cleaned_content") or row.get("content") or "")
        style_family = str(row.get("style_family") or "").strip() or infer_style_family(
            str(row.get("title") or ""),
            body,
            str(row.get("intent_type") or ""),
        )
        out.append({
            **row,
            "style_family": style_family,
            "features": features,
            "group_key": _group_key(row),
            "body": body,
        })
    return industry_key, out
