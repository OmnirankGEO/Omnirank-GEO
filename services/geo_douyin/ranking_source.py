"""榜单候选取数 · 只读 `geo_research_answer_entities`

## 为什么是这张表,不是 `keyword_insights`

| | `keyword_insights` | `geo_research_answer_entities` |
|---|---|---|
| 覆盖 | **仅 18 个品牌**(客户私有) | **17 个行业 / 20,305 实体**(行业级公共资产) |
| 位次 | `brands_found[].ranks`(TEXT 存 JSON) | `recommendation_rank`(列,99.9% 填充) |
| 证据 | `recommendation_reasons` | `recommendation_reasons` + **`evidence_phrases`** |
| 血缘 | 无 | **`extractor_version` + `llm_model` + `confidence`** |
| 覆盖代价 | 289 个真客户里 **273 个(94.5%)为 0** | 最小行业(电梯)也有 61 家 ≥2 引擎共识 |

`competitors` / `competitor_snapshots` 两张老表**生产 0 行**,不接。

## 🔴 这里只出"候选",不出"榜单"

本模块**不判名次、不聚合、不写文案**。它交出来的每一条都带原始
`recommendation_rank`(某引擎对某问题的回答里列第几)与 `engine`,
是否能说成"综合排名"由 `ranking_payload.AggregationContract` 的七要素判定 ——
七要素不齐就只能说「某引擎对某问题的回答中列第 N」(工单 P1-2)。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("GEO-Douyin-Ranking")

#: 候选新鲜度窗口(天)。**超窗不是拒绝** —— 上层按 R5 降级切版式 + 提示补齐(工单 §5)。
DEFAULT_WINDOW_DAYS: int = 90

#: 一次最多取多少候选进池(池 → `select_entities` 再挑 3-6 家)。
CANDIDATE_POOL_LIMIT: int = 40


# ── 引擎名:**复用既有 SSOT,本模块不建第二份映射**(P1-2)────────────────
#
# 🔴 这是一个会**静默把功能变成恒降级**的地雷,必须写清楚:
#    同一件事在两张表里存法不同 ——
#      `geo_research_answer_entities.engine`  = 豆包 / Kimi / DeepSeek / 千问(中文)
#      `geo_research_source_signals.engine`   = doubao / deepseek / qwen / kimi(小写拉丁)
#    写 `engine = 'doubao'` 查前者 → **0 行**;写 `engine = '豆包'` 查后者 → **0 行**。
#    两边都不报错,只是候选恒空 → 榜单恒降级。
#
# 归一口径的 SSOT 已经存在(**不要再写一份**):
#    写侧 SQL   `db/geo_source_signals_db._ENGINE_NORMALIZE_SQL`
#    读侧 Python `services/research_monitor/answer_entity_extractor._ENGINE_CANONICAL`
# 本模块从后者**反查**某个规范名对应的全部原始写法,当参数传进 SQL。


def engine_raw_spellings(canonical: str) -> tuple[str, ...]:
    """规范名 → 库里可能出现的**全部**原始写法(小写)。

    取自既有 `_ENGINE_CANONICAL`,**不在本模块另立字面量表** ——
    上游加一种写法(比如 `doubao_app`)时这里自动跟上。
    """
    from services.research_monitor.answer_entity_extractor import _ENGINE_CANONICAL

    want = str(canonical or "").strip()
    out = tuple(sorted({str(k).strip().lower()
                        for k, v in _ENGINE_CANONICAL.items()
                        if str(v).strip() == want and str(k).strip()}))
    # 规范名自己也算一种写法(库里就是这么存的)
    return tuple(sorted(set(out) | ({want.lower()} if want else set())))


def fetch_ranking_candidates(industry_key: str, *,
                             window_days: int = DEFAULT_WINDOW_DAYS,
                             limit: int = CANDIDATE_POOL_LIMIT,
                             exclude_names: Optional[list[str]] = None,
                             prefer_engine: str = "",
                             ) -> list[dict[str, Any]]:
    """按行业取榜单候选。**只读,不写,不扣费。**

    返回每条:`entity_name` / `entity_key` / `mention_count` / `engine_count` /
    `engines` / `best_rank` / `evidence_phrases` / `reasons` / `source`(举证链快照)。

    `exclude_names` 用于把**客户本人**排除出"竞品"语义的场景(蒸馏侧);
    榜单侧**不传** —— 有据分支要让客户进榜给实名次(治理 SSOT D12④)。

    `prefer_engine` 传规范名(如 `豆包`)时,**四元组优先从该引擎那一行取**;
    它**不过滤候选**,共识计数仍是四引擎全量。传空 = 不偏好(老行为)。
    """
    ind = str(industry_key or "").strip()
    if not ind:
        return []
    # 🔴 2026-08-08 P0:**键空间不一致** —— 和上面引擎名那条是同一类地雷,后果一模一样。
    #
    #    调用方传进来的是 `geo_douyin_posts.industry_key`,那一列存的是品牌档案里的
    #    **自由文本**(生产实例:「家居制造业 / 高端整木全屋定制/木作高定行业(住宅室内
    #    木作整装、实木定制家具设计生产安装一体化服务)」);而本表存的是**受控枚举**
    #    (`home_improvement` / `电梯行业` … 生产实取 17 个)。
    #
    #    直传 → `WHERE e.industry_key = '<自由文本>'` 命中 **0 行**,**不报错**,
    #    上层照着空池子说「这个行业还没攒够可以点名的同行数据」—— 而家装恰恰是池子里
    #    货**最多**的行业(5593 条 / 1851 去重实体 / 4 引擎 / 全部在窗内)。
    #    说错比不说更坏:用户会去补数据,而数据一直都在。
    #
    #    生产实证(2026-08-08 只读取证):213 个自由文本品牌 + 65 个空 = **一个都对不上**
    #    → 榜单必然 100% 降级。上线后第一张真实榜单请求(post 20)就是这么死的。
    #
    #    归一 SSOT 早就存在,**不在本模块另立第二份**;实测它对生产池那 17 个键
    #    **全部幂等**(所以这一刀只会修好现在捞不到的,不会打坏现在捞得到的)。
    from services.media_entity_flywheel import normalize_industry_key

    ind = normalize_industry_key(ind)
    excl = {str(x).strip() for x in (exclude_names or []) if str(x or "").strip()}
    # 空偏好时给一个不可能命中的写法,让 `<> ALL(...)` 恒为 true → 排序键退化成常量,
    # 行为与加这个参数之前**逐字相同**。
    # 🔴 哨兵**不能含 NUL**:`"\x00..."` 会让 psycopg2 直接抛
    #    「A string literal cannot contain NUL (0x00) characters」,
    #    而本函数把异常吞成"取不到候选" → 表现是榜单恒降级、日志里一句 warning。
    #    (写这条时我自己就是这么写的,被真库测试当场抓到。)
    prefer = list(engine_raw_spellings(prefer_engine)) if str(prefer_engine or "").strip() \
        else ["__no_engine_preference__"]

    # 🔴 2026-08-06 返工:**engine / rank / query / observed_at 必须取自同一行**。
    #
    #    上一版把 `ARRAY_AGG(DISTINCT engine)` 与 `MIN(recommendation_rank)` **各自独立
    #    聚合**,再在 Python 里拿 `engines[0]` 与 `best_rank` 配对 —— 两者**可能来自
    #    不同行**。于是「在 DeepSeek 回答『X』时列第 2」这句可以是**假的**:
    #    真实情况可能是 DeepSeek 第 5、Kimi 第 2,而 `engines[0]` 恰好按字典序是 DeepSeek。
    #    这一句正是 P1-2 允许在七要素不齐时对外说的**唯一一句**,它假了整条就废了。
    #
    #    改法:`DISTINCT ON (entity_key)` 取"最优的那一行",四元组全部从这一行读;
    #    统计量(提及数/引擎数/证据词)照旧另算聚合,两者按 entity_key 拼。
    #    `query` 不在实体表上 —— 它在 `geo_research_answer_facts.query`,按
    #    `answer_fact_id` 关联,所以四元组的取数也必须带上这个 join。
    sql = """
        WITH best AS (
            SELECT DISTINCT ON (e.entity_key)
                   e.entity_key,
                   e.entity_name,
                   e.engine                AS best_engine,
                   e.recommendation_rank   AS best_rank_raw,
                   e.extractor_version,
                   e.llm_model,
                   e.confidence,
                   e.updated_at            AS observed_at,
                   e.id                    AS best_row_id,
                   f.query                 AS best_query
              FROM geo_research_answer_entities e
              LEFT JOIN geo_research_answer_facts f ON f.id = e.answer_fact_id
             WHERE e.industry_key = %s
               AND e.entity_type = 'brand'
               AND e.created_at >= NOW() - (%s || ' days')::interval
             ORDER BY e.entity_key,
                      -- 🔴 P1-2:**目标引擎那一行优先**(false 排前)。
                      --    发布面是豆包(只有它引抖音),所以「第几名」这句话
                      --    优先引豆包自己的回答 —— 对读者和对豆包都最相关。
                      --    没有豆包行时自然落到别的引擎,`rank_statement` 会
                      --    **如实写出是哪个引擎**,不冒充。
                      --    ⚠️ 这只改「引谁的名次」,**不筛候选** ——
                      --       共识计数(下面的 agg)仍是四引擎全量。
                      (LOWER(e.engine) <> ALL(%s)),
                      -- 有正名次的行优先(false 排前),再按名次升序,同名次取最新
                      (e.recommendation_rank IS NULL OR e.recommendation_rank <= 0),
                      e.recommendation_rank ASC,
                      e.updated_at DESC,
                      -- 🔴 2026-08-09:末位必须是**唯一列**,否则 `DISTINCT ON` 挑哪一行
                      --    由 SQL **未规定**(同 updated_at 的行可以任意排)。挑中哪行决定
                      --    对外那句「在 X 的回答里列第 N」引的是谁 —— 未规定 = 同一单
                      --    重跑可能换一句话。`e.id` 唯一(PK),补在最后不改前面任何优先级。
                      --
                      -- 🔴 这一处与最终 ORDER BY 那处**性质不同**,别混为一谈:
                      --    这里选的是**同一家企业的哪一条证据行**(引哪个引擎的哪句回答),
                      --    不决定**哪家企业上榜** —— 前者是数据稳定性,后者是商业排名策略。
                      --    我上一版把两者打包成"一件技术修复"提交,Review 拆开后
                      --    只批了这一处;最终 ORDER BY 那处已撤。
                      e.id DESC
        ), agg AS (
            SELECT e.entity_key,
                   COUNT(*)                     AS mention_count,
                   COUNT(DISTINCT e.engine)     AS engine_count,
                   ARRAY_AGG(DISTINCT e.engine) AS engines,
                   ARRAY_AGG(e.id ORDER BY e.id) AS entity_row_ids,
                   JSONB_AGG(COALESCE(e.evidence_phrases, '[]'::jsonb))       AS evidence_bags,
                   JSONB_AGG(COALESCE(e.recommendation_reasons, '[]'::jsonb)) AS reason_bags
              FROM geo_research_answer_entities e
             WHERE e.industry_key = %s
               AND e.entity_type = 'brand'
               AND e.created_at >= NOW() - (%s || ' days')::interval
             GROUP BY e.entity_key
        )
        SELECT b.entity_key, b.entity_name,
               b.best_engine, b.best_rank_raw, b.best_query, b.observed_at,
               b.extractor_version, b.llm_model, b.confidence, b.best_row_id,
               a.mention_count, a.engine_count, a.engines,
               a.entity_row_ids, a.evidence_bags, a.reason_bags
          FROM best b JOIN agg a ON a.entity_key = b.entity_key
         -- 🔴🔴 **已知未修缺陷:第 40 名边界的并列,SQL 没有规定取谁。**(2026-08-09)
         --
         --    实测(replica 生产快照 · 90 天窗):**17 个行业里 14 个**在第 40 名处存在
         --    并列 —— `food` 有 11 家同 (engine_count, mention_count) 争最后一个坑。
         --    (实证边界:强制换执行计划 `enable_hashagg=off` + `enable_seqscan=off`
         --     两次结果一致 —— **没有观测到漂移**,歧义是规范层面的,不是已出事。)
         --
         --    🔴 我一度在这里加了 `b.entity_key ASC` 去歧义,**已按 Review 裁定撤销**。
         --       那不是"技术去随机",是**用字符序决定哪家企业进榜** ——
         --       实测它换掉了 `business_service` 榜上的一家公司(华为云 → Worktile)。
         --       同分时谁上榜属于**商业排名策略,必须有数据依据并由 Owner 签发**,
         --       不能由字母/中文码位顺便决定。
         --
         --    → 这里**故意保持与生产尖一致的现状(未规定)**,直到有签发过的同分策略。
         --      候选策略(都需要数据依据,不许执行方自己拍):最好名次优先 /
         --      最近观测优先 / 首次入库时间优先。见交付单 §3。
         ORDER BY a.engine_count DESC, a.mention_count DESC
         LIMIT %s
    """
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, (ind, str(int(window_days)), prefer,
                              ind, str(int(window_days)), int(limit)))
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — 取不到候选不该挡住整条内容(永不中断)
        logger.warning("[douyin-ranking] 候选取数失败 industry=%s: %s", ind, str(e)[:200])
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        name = str(r.get("entity_name") or "").strip()
        if not name or name in excl:
            continue
        raw_rank = r.get("best_rank_raw")
        best_rank = int(raw_rank) if raw_rank is not None and int(raw_rank) > 0 else None
        out.append({
            "entity_key": r.get("entity_key"),
            "entity_name": name,
            "mention_count": int(r.get("mention_count") or 0),
            "engine_count": int(r.get("engine_count") or 0),
            "engines": [x for x in (r.get("engines") or []) if x],
            "best_rank": best_rank,
            "evidence_phrases": _flatten(r.get("evidence_bags")),
            "reasons": _flatten(r.get("reason_bags")),
            # 举证链**自带快照** —— 不存指针(工单 P1-4 要素 7)。
            # 🔴 `engine` / `recommendation_rank` / `query` / `observed_at` **同行取数**
            #    (SQL 的 DISTINCT ON 那一行),不是各自聚合后拼起来的。
            "source": {
                "engine": str(r.get("best_engine") or ""),
                "recommendation_rank": best_rank,
                "query": str(r.get("best_query") or ""),
                "observed_at": (r["observed_at"].isoformat()
                                if r.get("observed_at") is not None else None),
                "extractor_version": r.get("extractor_version"),
                "llm_model": r.get("llm_model"),
                "confidence": (round(float(r["confidence"]), 3)
                               if r.get("confidence") is not None else None),
                "row_id": r.get("best_row_id"),
                # 下面是**聚合面**,与上面的同行四元组分开放,防再次被误当成同一行的事实
                "engines": [x for x in (r.get("engines") or []) if x],
                "entity_row_ids": list(r.get("entity_row_ids") or [])[:20],
                "industry_key": ind,
                "window_days": int(window_days),
                # 这一行的四元组是不是来自目标引擎 —— 留痕,便于事后区分
                # 「引的是豆包」还是「豆包没提到、退到了别的引擎」。
                "prefer_engine": str(prefer_engine or ""),
                "from_preferred_engine": (
                    str(r.get("best_engine") or "").strip().lower() in set(prefer)),
            },
        })
    return out


def _flatten(bags: Any) -> list[str]:
    """把 JSONB_AGG 出来的「数组的数组」摊平去重,保序。"""
    out: list[str] = []
    seen: set[str] = set()
    for bag in (bags or []):
        for item in (bag or []):
            s = str(item or "").strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out
