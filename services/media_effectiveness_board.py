"""E1 · 行业媒体有效性榜(L3 答案实体主榜 + shadow/绑定/L2 附加证据)。

设计(老板拍板 · 答案实体消费接线 SPEC E1):
- 每行主键实体 = **L3 答案实体**(geo_research_answer_entities · AI 点名推荐的媒体/品牌)为主榜。
- 媒体 shadow_score / 可投放绑定 / L2 采纳 作**附加证据**(LEFT JOIN 不改主榜集合)。
- 两表共用同一 entity_key 归一(build_entity_key),可 join。

铁律:数字全真实、零 LLM、可溯源、fail-soft、无数据不显示(不显假 0)。
本模块纯只读聚合,不写库、不调 LLM、不碰报价/发布/扣费。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from services.media_entity_flywheel import (
    is_all_industry_scope,
    normalize_domain,
    normalize_industry_key,
    outcome_score_from_rollup,
)

logger = logging.getLogger("GEO-MediaEffectivenessBoard")

# ============================================================
# [E1 SSOT · 综合有效分权重] 命名单点常量(改这里 = 全局改 E1 综合有效分口径,禁第二份漂移)。
# 说明:此权重是「L3 答案实体点名有效性」口径(mention/rank/engine/confidence 四维),
# 与 services.media_entity_flywheel 的媒体 shadow_score 口径(evidence/quality/inventory/outcome)
# 是两个正交视角 —— E1 主榜用点名口径,shadow_score 作附加证据列并存,不混算。
# ============================================================
EFF_MENTION_WEIGHT = 40.0        # 点名次数(答案数)
EFF_RANK_WEIGHT = 30.0           # 排名靠前
EFF_ENGINE_WEIGHT = 20.0         # 引擎覆盖
EFF_CONFIDENCE_WEIGHT = 10.0     # 置信度
EFF_MENTION_SATURATION = 10      # 点名 >= 10 记满分
EFF_RANK_SPAN = 5                # avg_rank=1 满分 / >=6 记 0

# ============================================================
# [E5 SSOT · 效果校准] 自家发布 outcome(哪家媒体发的我们文章真被引了)作 E1 有效分的**后置校准权重**。
# additive · dormant · 绝不阻塞 E1-E4。当前 0 发布 → 无 per-entity outcome → 校准恒 no-op + 标「依据:调研数据」。
# 有真实发布 outcome 时:bonus 复用 media_entity_flywheel.outcome_score_from_rollup(SSOT · 禁另写 outcome 公式),
# 归一到最多 OUTCOME_CALIBRATION_MAX_BONUS 分,不喧宾夺主(E1 有效分主体仍是 L1/L2/L3 调研数据)。
# ============================================================
OUTCOME_CALIBRATION_MAX_BONUS = 15.0     # 校准最多加 15 分(命名常量单点)
CALIBRATION_BASIS_RESEARCH = "调研数据"                   # 0 发布 / 无 outcome:全靠调研数据
CALIBRATION_BASIS_CALIBRATED = "调研数据 + 发布效果校准"   # 有真实发布 outcome:调研数据 + 校准


def _clamp(value: float, low: float, high: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, v))


def compute_answer_entity_effectiveness(row: dict[str, Any]) -> float:
    """L3 答案实体综合有效分(0-100)· 纯函数零 IO。

    mention = min(1, mention_count/SATURATION) * MENTION_WEIGHT
    rank    = (avg_rank None → 0;否则 clamp(1-(avg_rank-1)/SPAN, 0, 1)) * RANK_WEIGHT
    engine  = min(1, engine_count/4) * ENGINE_WEIGHT
    conf    = clamp(avg_confidence, 0, 1) * CONFIDENCE_WEIGHT
    """
    row = row or {}

    try:
        mention_count = float(row.get("mention_count") or 0)
    except (TypeError, ValueError):
        mention_count = 0.0
    mention = min(1.0, mention_count / EFF_MENTION_SATURATION) * EFF_MENTION_WEIGHT if EFF_MENTION_SATURATION else 0.0

    avg_rank = row.get("avg_recommendation_rank")
    if avg_rank is None:
        rank = 0.0
    else:
        try:
            ar = float(avg_rank)
        except (TypeError, ValueError):
            ar = None
        if ar is None:
            rank = 0.0
        else:
            rank_norm = _clamp(1.0 - (ar - 1.0) / EFF_RANK_SPAN, 0.0, 1.0) if EFF_RANK_SPAN else 0.0
            rank = rank_norm * EFF_RANK_WEIGHT

    try:
        engine_count = float(row.get("engine_count") or 0)
    except (TypeError, ValueError):
        engine_count = 0.0
    engine = min(1.0, engine_count / 4.0) * EFF_ENGINE_WEIGHT

    conf = _clamp(row.get("avg_confidence") or 0, 0.0, 1.0) * EFF_CONFIDENCE_WEIGHT

    return round(mention + rank + engine + conf, 2)


def apply_outcome_calibration(
    effectiveness_score: float, outcome_rollup: dict[str, Any] | None
) -> dict[str, Any]:
    """[E5] 用自家发布 outcome 校准 L3 有效分 · 纯函数零 IO · additive · dormant。

    - 无 outcome(None / 非 dict / 空 dict / published_count<=0)→ **no-op**:
      返回原分 + calibrated=False + basis='调研数据'(0 发布时全走此支,证明 dormant)。
    - 有真实 outcome(published_count>0)→ bonus = min(MAX_BONUS, outcome_score_from_rollup 的归一小额),
      score = min(100, 原分 + bonus),calibrated=True + basis 含「发布效果校准」+ outcome_bonus。
      🔴 复用 outcome_score_from_rollup SSOT,绝不另写 outcome 公式,绝不伪造 outcome。

    fail-soft:任何异常(含畸形 rollup)→ 退化为 no-op 原分,绝不抛。
    """
    try:
        base = round(float(effectiveness_score), 2)
    except (TypeError, ValueError):
        base = 0.0

    rollup = outcome_rollup if isinstance(outcome_rollup, dict) else None

    published = 0.0
    if rollup:
        try:
            published = float(rollup.get("published_count") or 0)
        except (TypeError, ValueError):
            published = 0.0

    # dormant no-op:无 outcome 源 / 0 发布 → 有效分完全由调研数据构成,如实标注。
    if not rollup or published <= 0:
        return {"score": base, "calibrated": False, "basis": CALIBRATION_BASIS_RESEARCH}

    # 有真实发布 outcome → 计算归一小额校准 bonus(复用 SSOT 公式)。
    try:
        outcome_score = float(outcome_score_from_rollup(rollup))  # 0-100
    except Exception:
        # rollup 畸形导致 SSOT 计算失败 → 保守退化为 no-op(绝不伪造)。
        return {"score": base, "calibrated": False, "basis": CALIBRATION_BASIS_RESEARCH}

    # 归一:outcome_score(0-100)→ [0, MAX_BONUS] 小额;min 再兜一层上限防漂移。
    bonus = round(
        min(OUTCOME_CALIBRATION_MAX_BONUS,
            max(0.0, outcome_score) / 100.0 * OUTCOME_CALIBRATION_MAX_BONUS),
        2,
    )
    score = round(min(100.0, base + bonus), 2)
    # [review fix] 返回**实际生效增量**(触顶 min(100) 钳制后 = score-base),而非钳前 bonus:
    # 否则实体调研分已满(base=100)时前端仍显「(含发布效果校准 +10.8)」夸大边际贡献(实际为 0)。
    # 前端 outcome_bonus>0 守卫据此自然抑制虚假 caption。
    effective_bonus = round(max(0.0, score - base), 2)
    return {
        "score": score,
        "calibrated": True,
        "basis": CALIBRATION_BASIS_CALIBRATED,
        "outcome_bonus": effective_bonus,
    }


def get_industry_media_effectiveness_board(
    industry: str = "", weeks: int = 4, limit: int = 30,
    allow_all_industry_fallback: bool = False,
) -> dict[str, Any]:
    """E1 行业媒体有效性榜聚合(L3 主榜 + shadow/绑定/L2 附加证据)· 纯只读零 LLM · fail-soft。

    返回顶层 key:industry_key / requested_industry_key / scope / weeks / generated_rows / rows / source / has_data。

    allow_all_industry_fallback(发布流可见性修复 · 2026-07-05):
    - 答案实体的行业覆盖来自调研批次,长尾行业(归一后)常无点名数据 → 榜恒隐藏,代理端完全看不到。
    - True(仅发布流端点传):本行业无点名数据时退**全行业榜**,scope='all_industry_fallback' 如实标注,
      三条附加证据源(shadow/绑定/outcome)同步切全行业口径,防「装修的 shadow 分贴在全行业榜上」串味。
    - False(admin 端点默认):保持原语义 —— 该行业没数据就是没数据,不掩盖数据缺口(运营审计视角)。
    - 🔴 E2 排序 boost 不走本 fallback(_prefetch_entity_rank_boost 直查本行业):
      用全行业数据 boost 本行业排序 = 跨行业污染,恰是 blend 死因防线;榜只做展示情报,可以退。
    """
    # 惰性 import 让 db 层不在模块 import 期强连(import 冒烟无 DATABASE_URL 也能过)。
    from db.research_answer_entity_db import list_answer_entity_summary
    from db.media_entity_flywheel_db import (
        get_shadow_scores_by_entity_keys,
        get_outcome_rollups_by_entity_keys,
        list_approved_media_binding_candidates,
    )

    # ① 归一入参(all-scope → '',与端点 _query_industry_key 对齐)+ clamp weeks。
    if not industry or is_all_industry_scope(industry):
        industry_key = ""
    else:
        industry_key = normalize_industry_key(industry)
        if is_all_industry_scope(industry_key):
            industry_key = ""
    try:
        weeks = int(weeks)
    except (TypeError, ValueError):
        weeks = 4
    weeks = max(1, min(52, weeks))
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(200, limit))
    since_days = weeks * 7

    requested_industry_key = industry_key
    scope = "industry" if industry_key else "all_industry"


    empty = {
        "industry_key": industry_key,
        "requested_industry_key": requested_industry_key,
        "scope": scope,
        "weeks": weeks,
        "generated_rows": 0,
        "rows": [],
        "source": "依据:AI 调研数据聚合(答案点名 + 媒体证据)· 只读",  # [review fix] 该字段随代理端响应出网,内部术语人话化
        "has_data": False,
    }

    # ② L3 主榜数据源(点名答案聚合)。主查询失败 → 整榜空(fail-soft 不 500)。
    try:
        # [review fix] 池取 limit×3:池按点名次数排序,综合有效分重排后 top-N 的构成可能来自
        # 点名排位 50+ 的高质量实体(点名 ≥10 即饱和满分),池太小会静默漏行。
        rows = list_answer_entity_summary(
            industry_key=industry_key, limit=max(limit * 3, 150), since_days=since_days,
        ) or []
    except Exception as exc:
        logger.warning("[E1] list_answer_entity_summary 失败(返空榜): %s", str(exc)[:200])
        return empty

    # ②b [publish-visibility fix 2026-07-05] 本行业无点名数据 → 退全行业榜(仅发布流启用)。
    #    scope 如实标注,前端明示「本行业数据积累中 · 全行业参考」,绝不冒充本行业数据。
    if not rows and industry_key and allow_all_industry_fallback:
        try:
            rows = list_answer_entity_summary(
                industry_key="", limit=max(limit * 3, 150), since_days=since_days,
            ) or []
        except Exception as exc:
            logger.warning("[E1] 全行业 fallback 查询失败(返空榜): %s", str(exc)[:200])
            rows = []
        if rows:
            industry_key = ""  # 后续 shadow/绑定/outcome 证据源全部切全行业口径,防跨行业串味
            scope = "all_industry_fallback"

    if not rows:
        return empty

    # ③ 附加证据源(各自 fail-soft;任一失败不影响主榜)。
    keys = [r.get("entity_key") for r in rows if r.get("entity_key")]
    try:
        # [review fix] 传 industry_key:行业榜的「媒体有效分」限本行业或 general 快照,防跨行业串分
        shadow = get_shadow_scores_by_entity_keys(keys, industry_key=industry_key) or {}
    except Exception as exc:
        logger.warning("[E1] get_shadow_scores_by_entity_keys 失败(shadow 归 None): %s", str(exc)[:200])
        shadow = {}
    try:
        # [review fix] limit=None:全量 membership 判定,LIMIT 100 会把排位后的已绑定实体错标「参考」
        approved = list_approved_media_binding_candidates(
            industry_key=industry_key, include_general=True, limit=None,
        ) or []
        # [publish-visibility fix v2 · Codex P1/P2] entity_key → 各库绑定 {source: 媒体库真名}。
        # 绑定表 media_source 值域 'mhz_media'/'mhz_wemedia'(软文库/自媒体库两张库存表),去 mhz_
        # 前缀规范为 'media'/'wemedia'(与组合包候选、前端 tab 口径对齐,同 publish_recommendation._boost_key)。
        # 每库取首见(approved 已按 match_confidence DESC,首见即最优);带来源才能让前端
        # ①点击引导搜对库(软文绑定在自媒体 tab 点击 → 自动切软文 tab)②「可投放」标注库别不误导。
        # membership 判定(key in bound)语义不变:任一库有绑定 = 已绑定。
        bound: dict[str, dict[str, str]] = {}
        for a in approved:
            k = a.get("entity_key")
            if not k:
                continue
            src = str(a.get("media_source") or "media")
            if src.startswith("mhz_"):
                src = src[4:]
            if src not in ("media", "wemedia"):
                src = "media"
            per_source = bound.setdefault(k, {})
            if src not in per_source:
                per_source[src] = str(a.get("media_name") or "").strip()
    except Exception as exc:
        logger.warning("[E1] list_approved_media_binding_candidates 失败(可投放归'参考'): %s", str(exc)[:200])
        bound = {}

    # [E5] 自家发布 outcome per-entity 源(fail-soft:失败/0 发布 → 恒 {} → 校准全 dormant no-op)。
    #      绝不阻断主榜 —— E1-E4 上线不依赖 E5;查询挂掉只降级为「依据:调研数据」。
    try:
        outcome_rollups = get_outcome_rollups_by_entity_keys(keys, industry_key=industry_key) or {}
    except Exception as exc:
        logger.warning("[E5] get_outcome_rollups_by_entity_keys 失败(校准降级为调研数据): %s", str(exc)[:200])
        outcome_rollups = {}
    if not isinstance(outcome_rollups, dict):
        outcome_rollups = {}

    # ④ 逐行组装。
    board_rows: list[dict[str, Any]] = []
    for r in rows:
        key = r.get("entity_key")
        try:
            score = compute_answer_entity_effectiveness(r)
        except Exception:
            score = 0.0
        # get_shadow_scores_by_entity_keys 返 {entity_key: float}(非嵌套 dict)→ 直接取值。
        # 🔴 无匹配 → None(不显假 0)。
        shadow_score = shadow.get(key) if isinstance(shadow, dict) else None
        # [E5] additive 校准:取该实体自家发布 outcome(无则 None)→ 过 apply_outcome_calibration。
        #      0 发布 → no-op(校准分=原分)+ basis='调研数据';有真实 outcome → +bonus(≤15)。
        calib = apply_outcome_calibration(score, outcome_rollups.get(key))
        board_rows.append({
            "entity_key": key,
            "entity_name": r.get("entity_name"),
            "entity_type": r.get("entity_type"),
            "effectiveness_score": calib["score"],  # 综合有效分(0-100 · E5 校准后 · 0 发布=原分)
            "mention_count": r.get("mention_count"),
            # [publish-visibility fix v2 · Codex P1] 已绑定实体带**各库**绑定明细(additive):
            # [{source: 'media'|'wemedia', name: 媒体库真名}],未绑定 → []。前端据此搜对库/标注库别。
            "bound_media": [
                {"source": s, "name": n}
                for s, n in (bound.get(key) or {}).items()
            ],
            "evidence": {
                "点名推荐次数": r.get("mention_count"),
                "平均排名": r.get("avg_recommendation_rank"),  # 可空 → 前端不显示
                "引擎分布": r.get("engines") or [],
                "媒体有效分": shadow_score,  # 无则 None,不显 0
                "可投放": ("已绑定" if key in bound else "参考/待拓展"),
                # v1 经 shadow 内含 L2;精确 per-entity 域名级采纳待 v2。不得伪造 L2 数字。
                "l2_adopted": None,
            },
            # [E5] 校准溯源:数据到位自动增强,0 发布如实标注「依据:调研数据」。
            "calibration": {
                "basis": calib["basis"],
                "calibrated": calib["calibrated"],
                "outcome_bonus": calib.get("outcome_bonus", 0.0),
            },
            # 溯源=前端调 /effectiveness-board/trace?entity_key= 端点带 entity_key 点开。
            "traceable": bool(key),
        })

    # ⑤ 排序 by 综合有效分 desc, mention_count desc;截 limit。
    def _sort_key(x: dict[str, Any]) -> tuple[float, float]:
        try:
            mc = float(x.get("mention_count") or 0)
        except (TypeError, ValueError):
            mc = 0.0
        return (float(x.get("effectiveness_score") or 0), mc)

    board_rows.sort(key=_sort_key, reverse=True)
    board_rows = board_rows[:limit]

    # ⑥ 返回。
    return {
        "industry_key": industry_key,
        "requested_industry_key": requested_industry_key,
        "scope": scope,
        "weeks": weeks,
        "generated_rows": len(board_rows),
        "rows": board_rows,
        "source": "依据:AI 调研数据聚合(答案点名 + 媒体证据)· 只读",  # [review fix] 该字段随代理端响应出网,内部术语人话化
        "has_data": bool(board_rows),
    }


# ============================================================
# [发布榜垂类特异性 · 2026-07-05 老板拍板「垂类媒体权重调高」] 纯展示层排序,shadow SSOT 0 碰。
# lift = (该域名本行业引用份额) / (该域名全网引用份额)。通用大站(搜狐/知乎)lift≈1,
# 垂类媒体本行业 lift 显著 >1 → 上浮 + 理由「本行业引用集中度是全网基线的 X 倍」。
# 数据稀疏防御(防纸面加权空转):本行业信号总权重 / 单域名权重不达下限 → 全部 no-op,
# 榜自动退化为现状排序,绝不因数据不足而变差;数据积累到位自动生效。
# ============================================================
VERTICAL_LIFT_THRESHOLD = 2.0        # 本行业引用集中度 ≥ 全网基线 2 倍 = 垂类特荐
VERTICAL_MIN_INDUSTRY_WEIGHT = 10.0  # 本行业信号总权重下限(不足 = 行业数据太稀,不算 lift)
VERTICAL_MIN_DOMAIN_WEIGHT = 1.0     # 单域名本行业权重下限(不足 = 样本太小,不标)

# ============================================================
# [发布榜展示层] 常见媒体域名 → 中文名(只收录确定无歧义的大站;不确定的宁缺毋滥,
# 兜底显示域名)。绑定行优先用媒体库真名,此表只服务未绑定行的可读性。纯展示,不进评分。
# ============================================================
_DOMAIN_DISPLAY_NAMES: dict[str, str] = {
    "maimai.cn": "脉脉",
    "cnblogs.com": "博客园",
    "zhuanlan.zhihu.com": "知乎专栏",
    "zhihu.com": "知乎",
    "csdn.net": "CSDN",
    "blog.csdn.net": "CSDN博客",
    "mbd.baidu.com": "百度百家号",
    "baijiahao.baidu.com": "百家号",
    "baidu.com": "百度",
    "ifeng.com": "凤凰网",
    "finance.ifeng.com": "凤凰网财经",
    "tidenews.com.cn": "潮新闻",
    "jiemian.com": "界面新闻",
    "yoojia.com": "百度有驾",
    "pcauto.com.cn": "太平洋汽车网",
    "sohu.com": "搜狐",
    "news.sohu.com": "搜狐新闻",
    "smzdm.com": "什么值得买",
    "post.smzdm.com": "什么值得买",
    "ithome.com": "IT之家",
    "maigoo.com": "买购网",
    "yiche.com": "易车网",
    "zgswcn.com": "中国商务新闻网",
    "163.com": "网易",
    "news.163.com": "网易新闻",
    "qq.com": "腾讯网",
    "new.qq.com": "腾讯新闻",
    "toutiao.com": "今日头条",
    "bilibili.com": "哔哩哔哩",
    "weibo.com": "微博",
    "xinhuanet.com": "新华网",
    "people.com.cn": "人民网",
    "thepaper.cn": "澎湃新闻",
    "36kr.com": "36氪",
    "sina.com.cn": "新浪",
    "finance.sina.com.cn": "新浪财经",
    "china.com": "中华网",
    "cctv.com": "央视网",
}


# ============================================================
# [UGC 号生态平台域名] 与 _DOMAIN_DISPLAY_NAMES(纯显示名)是两回事,不许混用
# (老板 07-05 实测抓到:中国商务新闻网/界面新闻是**媒体自营站**,被冤枉成"平台内容"):
# - 集合内 = 引用大概率来自第三方账号内容(号生态)→ 理由如实标「平台内容被…」+
#   不参选「本行业特荐」(平台 ≠ 垂类媒体;平台行业集中度再高也只是号生态密度)。
# - 集合外的独立媒体 = 被采纳的就是它自己的内容 → 理由原文,lift 达标照标垂类。
# 边界取保守:只收明确的号生态平台,宁漏勿冤。
# ============================================================
_UGC_PLATFORM_DOMAINS: frozenset[str] = frozenset({
    "mbd.baidu.com", "baijiahao.baidu.com", "baidu.com", "yoojia.com",
    "zhihu.com", "zhuanlan.zhihu.com",
    "csdn.net", "blog.csdn.net", "cnblogs.com", "maimai.cn",
    "smzdm.com", "post.smzdm.com",
    "toutiao.com", "bilibili.com", "weibo.com",
    "sohu.com", "news.sohu.com",
    "163.com", "news.163.com",
    "qq.com", "new.qq.com",
    "sina.com.cn", "finance.sina.com.cn",
})


def _match_domain_map(domain: str, table) -> str:
    """域名在映射/集合里的命中值(精确 → 逐级剥子域);dict 返映射值,set 返命中键;查无返 ''。"""
    d = (domain or "").strip().lower().removeprefix("www.")
    parts = d.split(".")
    for i in range(len(parts)):
        key = ".".join(parts[i:])
        if isinstance(table, dict):
            hit = table.get(key)
            if hit:
                return hit
        elif key in table:
            return key
    return ""


def _platform_display_name(domain: str) -> str:
    """已知平台/大站域名 → 公认中文名(精确 → 逐级剥子域);查无返 ''。"""
    return _match_domain_map(domain, _DOMAIN_DISPLAY_NAMES)


def _is_ugc_platform_domain(domain: str) -> bool:
    """是否 UGC 号生态平台域名(触发「平台内容被…」理由改写 + 垂类特荐排除)。"""
    return bool(_match_domain_map(domain, _UGC_PLATFORM_DOMAINS))


def _is_bare_domain_label(label: str, domain: str) -> bool:
    """展示名是不是"其实就是域名"(= 前面几档全落空、兜底到域名那一档)。

    只有这一档才交给域名目录补人话名 —— 平台公认名 / 媒体库绑定真名 / canonical
    都是比 LLM 蒸馏更硬的信源,不许被目录覆盖(尤其 `_display_name_for` 里那套
    UGC 平台归属纠偏,被覆盖就退回老 bug)。
    """
    text = (label or "").strip().lower().removeprefix("www.")
    dom = (domain or "").strip().lower().removeprefix("www.")
    return (not text) or (bool(dom) and text == dom)


def _apply_domain_directory(
    rows: list[dict[str, Any]], general_rows: list[dict[str, Any]],
) -> None:
    """[§1 · 域名人话化] 出榜时 join 域名目录:裸域名 → 中文名 + 一句话简介。**原地改**。

    - SSOT 在后端(`services.media_domain_directory` / 表 `media_domain_directory`),
      前端补丁表 `GENERAL_MEDIA_LABELS` 降为最后兜底。
    - 只补"展示名 == 域名"那一档(见 `_is_bare_domain_label`),不动更硬的信源。
    - `one_liner` 只要目录里有就带上(hover 用),与是否替换展示名无关。
    - 仍缺名的域名丢进**后台**蒸馏,本次请求照常返回(不阻断主链)。
    - fail-soft:目录模块 / 查询 / 蒸馏任何异常 → 榜完全退化为裸域名现状。
    """
    all_rows = [r for r in (list(rows) + list(general_rows)) if isinstance(r, dict)]
    domains = [str(r.get("domain") or "") for r in all_rows]
    if not any(domains):
        return
    try:
        from services.media_domain_directory import (
            get_directory_entries,
            normalize_directory_domain,
            request_distillation,
        )
    except Exception as exc:
        logger.warning("[publish-board] 域名目录不可用(退化为域名展示): %s", str(exc)[:200])
        return

    try:
        entries = get_directory_entries(domains)
    except Exception as exc:
        logger.warning("[publish-board] 域名目录查询失败(退化为域名展示): %s", str(exc)[:200])
        return

    missing: list[str] = []
    for r in all_rows:
        dom = str(r.get("domain") or "")
        key = normalize_directory_domain(dom)
        hit = entries.get(key) if key else None
        label = str(r.get("display_name") or "")
        if hit and hit.get("zh_name") and _is_bare_domain_label(label, dom):
            r["display_name"] = hit["zh_name"]
        if hit and hit.get("one_liner"):
            r["one_liner"] = hit["one_liner"]
        # 仍是裸域名 = 目录没覆盖到 → 排队蒸馏(下一次出榜就有名字)。
        if key and _is_bare_domain_label(str(r.get("display_name") or ""), dom):
            missing.append(key)

    if missing:
        try:
            request_distillation(missing)
        except Exception as exc:
            logger.warning("[publish-board] 域名蒸馏排队失败(不影响本次出榜): %s", str(exc)[:200])


def _display_name_for(domain: str, canonical_name: str, bound_names: dict[str, str]) -> str:
    """展示名:平台/大站公认名 > 绑定库真名 > canonical_name > 域名。

    [归属纠偏 · 老板 07-05 实测「全屋定制行业采纳第二是全网车事」] mbd.baidu.com 这类
    UGC 平台域名的引用证据是**平台级聚合**(百家号整体被采纳 N 次),库里绑定的具体账号名
    (「全网车事」= 一个汽车号)当主名会造成归属幻觉。平台名做主名如实表达证据粒度;
    绑定账号名保留给点击引导(搜库用它才搜得到可买资源)。独立媒体站(不在映射表)仍用绑定名。
    """
    platform = _platform_display_name(domain)
    if platform:
        return platform
    for n in bound_names.values():
        if n:
            return n
    d = (domain or "").strip().lower().removeprefix("www.")
    return (canonical_name or "").strip() or d


def _citation_volume(snapshot_evidence: Any) -> float:
    """快照 evidence.citation_rollup 的原始引用量(采纳 + 明确引用),做同分 tiebreak。
    头部媒体 evidence_score 触 100 封顶后 shadow 分塌缩同值(如全 82),
    原始量级仍有区分度 —— 只影响同分段内先后,不改分数口径。fail-soft 归 0。"""
    try:
        rollup = (snapshot_evidence or {}).get("citation_rollup") or {}
        adopted = float(rollup.get("answer_adopted_count") or 0)
        cited = float(rollup.get("cited_count") or 0)
        return adopted + cited
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _snapshot_is_newer(row: dict[str, Any], other: dict[str, Any]) -> bool:
    """created_at 安全比较(TIMESTAMPTZ aware;缺失当旧,绝不因 naive/aware 混比抛)。"""
    a, b = row.get("created_at"), other.get("created_at")
    if a is None:
        return False
    if b is None:
        return True
    try:
        return a > b
    except TypeError:
        return False



# ============================================================
# [R5 · 近似行业 fallback 2026-07-06] 品牌行业细分(长尾中文 slug)无数据时,先找"相近行业"
# 再退全行业(老板:"总是全行业,没主动找离客户近的行业")。
# 🔴高精度铁律(出口对抗审核抓 P0:overlap>=1 会误连"建筑装饰↔服装饰品",仅共享尾部连接词):
# 相近 = 一方的**行业主名词(首 2 字)出现在另一方名字里**,而非"共享任意 bigram"。
# ============================================================
_GENERIC_HEAD_WORDS = frozenset({
    "专业", "综合", "高端", "大型", "知名", "优质", "正规", "新型", "全国", "中国",
    "连锁", "智能", "绿色", "环保", "现代", "传统", "国际", "本地", "高档", "一站",
    "制造", "服务", "行业", "领域", "产业", "管理", "系统", "公司", "集团", "业务",
    "有限", "责任", "中心", "平台", "贸易", "销售", "生产", "批发", "零售", "咨询",
    "发展", "实业", "经营", "商贸", "加工", "定制", "方案", "解决", "科技",
})


def _industry_bigrams(text: str) -> set[str]:
    """中文 2-gram 全集(用于"主名词是否出现在对方名字里"的成员判定)。非中文 → 空集。"""
    s = re.sub(r"[^一-鿿]", "", str(text or ""))
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _head_noun_bigram(text: str) -> str:
    """行业主名词 bigram = 前两个中文字符;落在修饰/泛商业词则视为无有效主名词(返 '')。"""
    s = re.sub(r"[^一-鿿]", "", str(text or ""))
    if len(s) < 2:
        return ""
    head = s[:2]
    return "" if head in _GENERIC_HEAD_WORDS else head


def _resolve_near_industry(raw_industry: str, requested_key: str) -> str | None:
    """在"有 shadow 数据的行业"里找相近行业:要求一方的主名词(首 bigram)出现在另一方名字里;
    多命中取 bigram 总重叠最高者。fail-soft:任何异常 → None(退全行业,绝不抛)。"""
    try:
        from db.media_entity_flywheel_db import list_shadow_industry_keys
        candidates = list_shadow_industry_keys() or []
        req_text = str(raw_industry or requested_key or "")
        req_head = _head_noun_bigram(req_text)
        req_bg = _industry_bigrams(req_text)
        if not req_bg:
            return None
        best_key, best_overlap = None, 0
        for ck in candidates:
            if not ck or ck == requested_key or is_all_industry_scope(ck):
                continue
            ck_bg = _industry_bigrams(ck)
            ck_head = _head_noun_bigram(ck)
            matched = (req_head and req_head in ck_bg) or (ck_head and ck_head in req_bg)
            if not matched:
                continue
            overlap = len(req_bg & ck_bg)
            if overlap > best_overlap:
                best_overlap, best_key = overlap, ck
        return best_key
    except Exception as exc:
        logger.warning("[publish-board] 近似行业解析失败(退全行业): %s", str(exc)[:200])
        return None


# ============================================================
# [缺陷 #3 修复 · 读榜归一同源 · 2026-07-06] 让「前端传品牌原文查榜」与「自助轮写库 / 验收传规范名」
# 读到同一 industry_key。根因:前端 PublishCenter 传品牌行业原文(如「电梯维保」),而付费自助轮
# 把 media shadow 写在 resolver 规范名键(如「电梯行业」)。对 resolver 真实归并过的长尾行业,
# normalize_industry_key('电梯维保')='电梯维保' ≠ normalize_industry_key('电梯行业')='电梯行业'
# → 前端本行业榜永远退 all_industry_fallback + 7 天新鲜度闸锁死重跑 = 付费黑洞。
# 修法:读榜前先经 resolver 别名缓存(zero-LLM)把入参归并到规范名,再交给 normalize_industry_key。
# 🔴 不碰 normalize_industry_key(飞轮 SSOT),只在读路径入口加一步别名归并;fail-soft 绝不让榜崩。
# ============================================================


# 🔴 `_industry_name_by_id` 2026-08-08 **删除** —— 归并收编进
#    `services.industry_canonical` 后本函数零调用方。留着就是"死函数"家族又一例:
#    读代码的人会以为读榜路径还自己查一次行业表,而它永远不会再被执行。


def _resolve_to_canonical(industry: str, brand: Optional[dict] = None) -> str:
    """[缺陷 #3] 把读榜入参行业(可能是品牌原文长尾)经 resolver 别名缓存归并到平台规范行业名。

    - 空 / 全行业 scope:原样返回(交给下游 all-scope 判定)。
    - resolve_alias(zero-LLM · 只查已沉淀别名)命中 → 取该行业规范名(active);未命中(头部行业 /
      从没自助过)→ **原样返回,行为不变**。
    - 🔴 归一同源不变式:resolve_alias(规范名) 命中自己或返 None,都不改变规范名的 normalize —
      故验收路径(worker 直传规范名 · allow_all_industry_fallback=False)行为完全不受影响。
    - fail-soft:任何异常 → 退回原 industry,绝不因归并失败让榜崩。

    🔴 2026-08-08 **收编**:本函数原来是本模块的**本地实现**(当时刻意不 import
       `industry_resolver`,因为那条路径带 httpx / llm_track / intent 分类器的重依赖)。
       现在归并收敛到 `services.industry_canonical` —— 那个模块本身就是零 LLM、只读、
       轻依赖的,当初避重依赖的理由在它身上不成立,所以没有理由再留一份副本。
       (工单 `WO_INDUSTRY_KEY_RESOLVER_WIRING` §4-A1:同一失败模式已在三条链上
        各长出一种行为,再抄一遍就是第三份。)
       **行为逐字不变**:命中别名 → 规范名;未命中 / 软删 / 全行业 / 异常 → 原样返回。
    """
    raw = (industry or "").strip()
    if not raw or is_all_industry_scope(raw):
        return industry
    from services.industry_canonical import resolve_readonly

    # [WO_267] 带品牌上下文(调用方已鉴权)⇒ 走行业路由前段,与付费写路径同一份顺序;不带 ⇒ v1.0 逐字不变
    ci = resolve_readonly(raw, taxonomy=brand is not None, brand=brand)
    # 🔴 只认**真归并**(别名命中且行业 active)。原样透传时返回**入参本身**而不是
    #    `ci.canonical_name` —— 后者是 strip 过的,会把"归一 no-op"悄悄变成"顺手清洗",
    #    那是行为改变,不在本次收编范围内。
    return ci.canonical_name if ci.merged else industry


def get_publish_media_board(
    industry: str = "", limit: int = 30,
    allow_all_industry_fallback: bool = True,
    brand: Optional[dict] = None,
) -> dict[str, Any]:
    """发布流「AI 真实引用媒体榜」(媒体实体口径 · 2026-07-05 数据源纠偏)。

    为什么不是 L3 答案实体(admin E1 主榜):抽取器是**品牌实体级**(entity_type 恒 'brand',
    AI 答案里点名的品牌/公司图谱 = 竞品情报),与媒体库 key 空间几乎零重叠 → 发布流榜上全是
    「工商银行/天天基金」,无一可投放、点击引导必空搜(老板实测截图坐实)。
    发布流要的是**可投放的媒体**(老板基石:调研数据判媒体真实有效,自动推荐):
    - 主榜 = geo_media_entities × 最新 shadow 快照(shadow_score 四维:引用证据/质量/库存/outcome,
      引用证据来自 L2 调研引用信号 —— 这才是「被 AI 真实引用」的媒体口径)。
    - 可投放 = 快照 is_purchasable 或有人工审批绑定;绑定带库别真名(点击搜对库/切对 tab)。
    - Row 契约与 E1 对齐(entity_key/entity_name/effectiveness_score/evidence/bound_media),
      前端 MediaEffectivenessPanel 无需换契约。admin E1(L3 品牌点名 + 溯源)保持不动。
    纯只读、零 LLM、fail-soft;无数据不显示;本行业无快照 → 退全行业榜(scope 如实标注)。
    """
    from db.media_entity_flywheel_db import (
        list_shadow_media_entities,
        list_approved_media_binding_candidates,
    )

    if not industry or is_all_industry_scope(industry):
        industry_key = ""
    else:
        # [缺陷 #3 修复] 先经 resolver 别名缓存把品牌原文归并到规范名,再 normalize —— 让前端
        # (品牌原文)与后端写库/验收(规范名)读到同一 industry_key(详见 _resolve_to_canonical)。
        canonical = _resolve_to_canonical(industry, brand=brand)
        industry_key = normalize_industry_key(canonical)
        if is_all_industry_scope(industry_key):
            industry_key = ""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(200, limit))

    requested_industry_key = industry_key
    scope = "industry" if industry_key else "all_industry"

    near_industry_key = ""

    empty = {
        "industry_key": industry_key,
        "requested_industry_key": requested_industry_key,
        "near_industry_key": "",
        "near_industry_label": "",
        "scope": scope,
        "generated_rows": 0,
        "rows": [],
        "general_rows": [],
        "source": "依据:AI 调研引用证据聚合 · 只读",
        "has_data": False,
    }

    def _load(key: str) -> list[dict[str, Any]]:
        # 池放大:同实体多行业/多版本快照未在 SQL 去重,服务层按最新快照收敛后才截 limit。
        return list_shadow_media_entities(industry_key=key, limit=max(limit * 4, 200)) or []

    try:
        pool = _load(industry_key)
    except Exception as exc:
        logger.warning("[publish-board] list_shadow_media_entities 失败(返空榜): %s", str(exc)[:200])
        return empty

    if not pool and industry_key and allow_all_industry_fallback:
        # [R5] 三级 fallback:精确 → 近似行业 → 全行业。细分行业不再一步退全行业。
        near = _resolve_near_industry(industry, industry_key)
        if near:
            try:
                pool = _load(near)
            except Exception as exc:
                logger.warning("[publish-board] 近似行业查询失败: %s", str(exc)[:200])
                pool = []
            if pool:
                # 后续 shadow/绑定/lift 全部按近似行业口径(→ 近似行业也能拿垂类扶持)。
                industry_key = near
                near_industry_key = near
                scope = "near_industry"
        if not pool:
            try:
                pool = _load("")
            except Exception as exc:
                logger.warning("[publish-board] 全行业 fallback 查询失败(返空榜): %s", str(exc)[:200])
                pool = []
            if pool:
                industry_key = ""
                scope = "all_industry_fallback"

    if not pool:
        return empty

    # 同实体收敛到最新快照(SQL 按 purchasable/分数排序,首见 ≠ 最新)。
    latest: dict[str, dict[str, Any]] = {}
    for r in pool:
        k = r.get("entity_key")
        if not k:
            continue
        prev = latest.get(k)
        if prev is None or _snapshot_is_newer(r, prev):
            latest[k] = r

    # 绑定明细(与 E1 v2 同口径:去 mhz_ 前缀规范 media/wemedia,每库首见最优)。fail-soft。
    bound: dict[str, dict[str, str]] = {}
    try:
        approved = list_approved_media_binding_candidates(
            industry_key=industry_key, include_general=True, limit=None,
        ) or []
        for a in approved:
            k = a.get("entity_key")
            if not k:
                continue
            src = str(a.get("media_source") or "media")
            if src.startswith("mhz_"):
                src = src[4:]
            if src not in ("media", "wemedia"):
                src = "media"
            per_source = bound.setdefault(k, {})
            if src not in per_source:
                per_source[src] = str(a.get("media_name") or "").strip()
    except Exception as exc:
        logger.warning("[publish-board] 绑定查询失败(可投放退快照口径): %s", str(exc)[:200])
        bound = {}

    # [垂类特异性] 展示层 lift(fail-soft:任何失败/数据稀疏 → 空 dict = 全部 no-op 退化现状)。
    # 全行业 fallback 后 industry_key='' → 跳过(全行业谈不上行业特异性);\n    # [R6] 近似行业命中时 industry_key 已置近似 key → lift 按近似行业算(细分行业也拿垂类扶持)。
    lift_by_domain: dict[str, float] = {}
    if industry_key:
        try:
            from db.geo_source_signals_db import sum_signal_weight_by_domain

            def _norm_shares(raw: dict[str, float]) -> dict[str, float]:
                out: dict[str, float] = {}
                for d, w in (raw or {}).items():
                    nd = normalize_domain(d) or str(d or "").strip().lower()
                    if nd:
                        out[nd] = out.get(nd, 0.0) + float(w or 0)
                return out

            ind_share = _norm_shares(sum_signal_weight_by_domain(industry_key))
            all_share = _norm_shares(sum_signal_weight_by_domain(""))
            ind_total = sum(ind_share.values())
            all_total = sum(all_share.values())
            if ind_total >= VERTICAL_MIN_INDUSTRY_WEIGHT and all_total > 0:
                for d, w in ind_share.items():
                    if w < VERTICAL_MIN_DOMAIN_WEIGHT:
                        continue
                    base = all_share.get(d) or 0.0
                    if base <= 0:
                        continue
                    lift_by_domain[d] = (w / ind_total) / (base / all_total)
        except Exception as exc:
            logger.warning("[publish-board] 垂类特异性聚合失败(退化现状排序): %s", str(exc)[:200])
            lift_by_domain = {}

    rows: list[dict[str, Any]] = []
    for k, r in latest.items():
        try:
            score = round(float(r.get("shadow_score") or 0), 2)
        except (TypeError, ValueError):
            score = 0.0
        purchasable = bool(r.get("is_purchasable")) or k in bound
        per_bound = bound.get(k) or {}
        # 理由(数据说话):快照 reasons 现成人话(「答案采纳 N 次」「被 AI 明确引用 N 次」
        # 「覆盖 N 个引擎」),取前 3 条;「暂无可采购资源…」类提示行对代理无增量,过滤。
        raw_reasons = r.get("reasons") or []
        reasons = [
            str(x).strip() for x in raw_reasons
            if str(x).strip() and "暂无可采购" not in str(x)
        ][:3]
        # [归属纠偏 · 仅 UGC 号生态平台] 采纳/引用是平台级聚合(来自任意第三方账号),
        # 理由如实标「平台内容被…」,不让「答案采纳 41 次」被误读为绑定的具体账号被采纳。
        # 独立媒体自营站(中国商务新闻网/界面新闻等)被采纳的就是它自己的内容,理由保持原文。
        is_ugc_platform = _is_ugc_platform_domain(str(r.get("domain") or ""))
        if is_ugc_platform:
            reasons = [
                x.replace("答案采纳", "平台内容被答案采纳", 1)
                 .replace("被 AI 明确引用", "平台内容被 AI 明确引用", 1)
                for x in reasons
            ]
        # [垂类特异性] lift 达阈值 → 标垂类 + 数据说话理由(排序在 ⑤ 上浮)。
        # UGC 平台不参选:平台 ≠ 垂类媒体,行业集中度再高也只是号生态密度,标「本行业特荐」
        # 会让代理误读「百度百家号是建筑装饰垂类媒体」(老板 07-05 实测抓到)。
        nd = normalize_domain(str(r.get("domain") or "")) or str(r.get("domain") or "").strip().lower()
        lift = float(lift_by_domain.get(nd) or 0.0)
        vertical = (not is_ugc_platform) and lift >= VERTICAL_LIFT_THRESHOLD
        if vertical:
            reasons.append(f"本行业引用集中度是全网基线的 {round(lift, 1)} 倍")
        rows.append({
            "entity_key": k,
            "entity_name": r.get("canonical_name") or r.get("domain") or k,
            "display_name": _display_name_for(str(r.get("domain") or ""), str(r.get("canonical_name") or ""), per_bound),
            "domain": r.get("domain") or "",
            "entity_type": "media",
            "effectiveness_score": score,
            "mention_count": None,  # 媒体口径无点名次数(点名是品牌图谱),不伪造
            "vertical": vertical,
            # UGC 号生态平台标记:前端点击引导用平台名搜索(出平台下全部可买账号),
            # 不用绑定的单账号名(账号有自己的垂直属性,可能与当前行业不对口——
            # 老板 07-05 实测:建筑装饰行业点「什么值得买」定向到「化妆品推荐官」号)。
            "is_platform": is_ugc_platform,
            "reasons": reasons,
            "bound_media": [
                {"source": s, "name": n}
                for s, n in per_bound.items()
            ],
            "evidence": {
                "媒体有效分": score,
                "可投放": ("已绑定" if purchasable else "参考/待拓展"),
            },
            "traceable": False,  # L3 溯源端点是品牌点名口径,媒体口径溯源后续接 L2 引用样本
            "_citation_volume": _citation_volume(r.get("evidence")),  # 内部 tiebreak,出网前剥离
        })

    # 排序:可投放 → 垂类特荐 → 有效分 → 原始引用量。
    # 垂类在可投放层内也上浮(老板拍板「垂类权重调高」);头部 evidence 封顶后 shadow
    # 同分塌缩(如全 82)—— 引用量 tiebreak 让同分段内先后有真实依据,不改分数口径。
    rows.sort(
        key=lambda x: (
            1 if x["evidence"]["可投放"] == "已绑定" else 0,
            1 if x.get("vertical") else 0,
            float(x.get("effectiveness_score") or 0),
            float(x.get("_citation_volume") or 0),
        ),
        reverse=True,
    )
    rows = rows[:limit]
    for x in rows:
        x.pop("_citation_volume", None)

    # [两段式 · 老板 07-05 拍板] 本行业榜(scope='industry')下附「全网通用头部」小段:
    # 搜狐/知乎/百度系等任何行业都常被 AI 引用的大站,从本行业榜里"消失"会让代理以为
    # 数据不全 —— 单独一段既承认通用头部真有效,又不淹没本行业垂类。
    # 轻量只读:排除本行业池已有实体;不带可投放/绑定(跨行业绑定信息会误导);top 8。
    general_rows: list[dict[str, Any]] = []
    if scope in ("industry", "near_industry", "all_industry_fallback", "all_industry"):
        try:
            g_pool = _load("")
        except Exception as exc:
            logger.warning("[publish-board] 通用头部段查询失败(段留空): %s", str(exc)[:200])
            g_pool = []
        g_latest: dict[str, dict[str, Any]] = {}
        for r in g_pool:
            k = r.get("entity_key")
            if not k or (scope in ("industry", "near_industry") and k in latest):
                continue
            prev = g_latest.get(k)
            if prev is None or _snapshot_is_newer(r, prev):
                g_latest[k] = r
        g_sorted = sorted(
            g_latest.values(),
            key=lambda r: (
                float(r.get("shadow_score") or 0) if isinstance(r.get("shadow_score"), (int, float)) else 0.0,
                _citation_volume(r.get("evidence")),
            ),
            reverse=True,
        )[:8]
        for r in g_sorted:
            dom = str(r.get("domain") or "")
            try:
                g_score = round(float(r.get("shadow_score") or 0), 2)
            except (TypeError, ValueError):
                g_score = 0.0
            general_rows.append({
                "entity_key": r.get("entity_key"),
                "display_name": _platform_display_name(dom) or str(r.get("canonical_name") or "").strip() or dom,
                "domain": dom,
                "effectiveness_score": g_score,
            })

    # [§1 · 域名人话化] 出榜最后一步 join 域名目录(裸域名 → 中文名 + 一句话简介)。
    # 放在这里而不是行组装里,是因为主榜与通用头部段要一次查完(一次 SQL,不是两次)。
    _apply_domain_directory(rows, general_rows)

    return {
        "industry_key": industry_key,
        "requested_industry_key": requested_industry_key,
        "near_industry_key": near_industry_key,
        "near_industry_label": near_industry_key.replace("_", "/") if near_industry_key else "",
        "scope": scope,
        "generated_rows": len(rows),
        "rows": rows,
        "general_rows": general_rows,
        "source": "依据:AI 调研引用证据聚合 · 只读",
        "has_data": bool(rows),
    }
