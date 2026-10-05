"""诊断关键词的**唯一**派生口(订正二十六 · Review §20 · 2026-09-05)。

背景:⑤b 删掉前端关键词框之后,前端一字不发。上一版我在 DTO 上补 `[brand_name]`,
但 `keywords` 还有**三个 LLM 输入**消费点(`diagnosis_workflow.py:652 analyze_client_business`
/ `:716 generate_diagnosis_keywords` / `:1729 视频相关性 prompt`)——
有档案词的品牌会从「策划过的词表」缩成一个品牌名。**这是 ⑤b 单独引入的质量回退。**

🔴 为什么派生不在 DTO 里:DTO 无库,加了「读档案」那一档就变成**两处派生** ——
   落库记空而 workflow 跑了派生值,事后说不清那次诊断按什么词跑的,**且不报错**。
   调用链 `start_diagnosis --create_task--> run_diagnosis_task --> _run_diagnosis_impl`
   传的是 request **本体**,后两者全仓各只有一处调用点 ⇒
   在端点派生一次并回写,两个消费点结构上看到同一份。

🔴 为什么上限常量在这里而不是各写一份:档案词是在 DTO 校验**之后**赋进去的,
   绕过 `validate_keywords`。两处各写一遍 20/50,漂了不会有任何东西报错 ——
   所以 `validate_keywords` 也从这里取。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("GEO-DiagnosisKeywordSource")

#: ≡ `DiagnosisRequest.validate_keywords` 的上限。**唯一定义处**。
MAX_KEYWORDS = 20
MAX_KEYWORD_CHARS = 50


class NoKeywordSource(ValueError):
    """三个来源都空 —— 放行等于收了钱跑一次零搜索词的诊断。"""


def clamp_keywords(raw, *, source: str = "档案") -> list[str]:
    """把一批词收进与 `validate_keywords` **同解**的上限内。

    🔴 超限**不 422**(Review §20):档案词不是这次提交填的,
       用户没法通过改这次提交来救 —— 拒了她只会卡死。
       超长的丢弃、条数截断,都**留 WARN**:
       被正确捕获的处理不留痕迹,最该诊断的就最查不到。
    """
    out: list[str] = []
    dropped: list[str] = []
    for item in (raw or []):
        text = (str(item) if item is not None else "").strip()
        if not text:
            continue
        if len(text) > MAX_KEYWORD_CHARS:
            dropped.append(text[:20])
            continue
        if text not in out:
            out.append(text)
    if dropped:
        logger.warning(
            "[诊断词源] %s 里 %d 个词超过 %d 字,已丢弃(前几个:%s)",
            source, len(dropped), MAX_KEYWORD_CHARS, dropped[:3])
    if len(out) > MAX_KEYWORDS:
        logger.warning(
            "[诊断词源] %s 有 %d 个词,超过上限 %d,截断到前 %d 个",
            source, len(out), MAX_KEYWORDS, MAX_KEYWORDS)
        out = out[:MAX_KEYWORDS]
    return out


#: ≡ `api_get_brand_latest_diagnosis_params` 用的那条谓词。**唯一定义处**。
LATEST_PUBLISHED_WHERE = (
    "brand_id = %s AND total_score IS NOT NULL "
    "AND (result_visibility IS NULL OR result_visibility = 'published')")


def latest_published_diagnosis_keywords(cur, *, brand_id) -> list[str]:
    """这个品牌**上一次已发布诊断**用的关键词 —— 旧关键词框的预填就是它。

    🔴 词源我连错两次,都长得很有道理:
      ① `client_profiles.seed_keywords` —— **那张表没有这列**。错法是我用 psql
         一次问了两张表、把输出一起 grep,只看到一行就归给了前者 ——
         **读数不携带它来自哪条命令**。
      ② `brands.seed_keywords` —— 列是真的,但它是**死列**:全仓唯一的 SELECT
         在 `db/monitoring_db.py:2481` 自身的回写例程里,消费侧从来没做。
         真品牌里非空的只有 4/363。
      ③ 真源 = `GET /api/brands/{id}/latest-diagnosis-params`(`server.py:6316`)。
    两次都因为 `try/except` 会把「列不存在 / 查不到」咽掉、静默回落品牌名,
    而假 cursor 的判据一条都不会红。所以这里配了打真库的锁。

    谓词与端点**共用同一份**(`LATEST_PUBLISHED_WHERE`):各写一份的话,
    哪天端点改了「算不算已发布」的口径,预填与派生就会给出两套词,且不报错。
    """
    if not brand_id:
        return []
    try:
        cur.execute(
            "SELECT keywords FROM diagnosis_records WHERE " + LATEST_PUBLISHED_WHERE
            + " ORDER BY created_at DESC LIMIT 1", (int(brand_id),))
        row = cur.fetchone() or {}
    except Exception as err:
        logger.warning("[诊断词源] 读品牌 %s 的上次诊断词失败(回落品牌名):%s",
                       brand_id, err)
        return []
    raw = row.get("keywords")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    return clamp_keywords(raw, source="品牌 %s 的上次已发布诊断" % brand_id)


def resolve_keywords_with_source(cur, *, given, brand_id, brand_name, industry):
    """**一处派生**,并报出**来自哪一级**。顺序:提交带的 → 上次已发布诊断 → 品牌名 → 行业 → 拒。

    返回 `(keywords, source)`,`source ∈ {given, last_diagnosis, brand_name, industry}`。

    🔴 阶梯只有这一份实现。`resolve_keywords` 是它的薄壳 ——
    预填端点要知道来源(前端据此说不同的话),提交路径只要词;
    两个「应该等价」的实现漂开那天不会有任何东西变红,而表现是
    **她看到 A、系统跑 B**(与 #62 同形)。
    """
    if given:
        return list(given), "given"
    from_last = latest_published_diagnosis_keywords(cur, brand_id=brand_id)
    if from_last:
        logger.info("[诊断词源] 品牌 %s 复用上次已发布诊断的 %d 个词", brand_id, len(from_last))
        return from_last, "last_diagnosis"
    for cand, src in (((brand_name or "").strip(), "brand_name"),
                      ((industry or "").strip(), "industry")):
        if cand:
            logger.info("[诊断词源] 品牌 %s 无上次诊断词,回落%s", brand_id, src)
            return clamp_keywords([cand], source=src), src
    raise NoKeywordSource(
        "没有可用的搜索词:请至少填写品牌名或行业,或直接给出关键词")


def resolve_keywords(cur, *, given, brand_id, brand_name, industry) -> list[str]:
    """薄壳:只要词、不要来源。**与预填端点走同一个阶梯**。"""
    return resolve_keywords_with_source(
        cur, given=given, brand_id=brand_id,
        brand_name=brand_name, industry=industry)[0]
