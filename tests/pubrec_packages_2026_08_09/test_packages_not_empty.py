"""P0 锁:真实生产数据喂进去,推荐组合包必须非空。

病灶两条(2026-08-09 replica 实测,逐条量化过边际贡献):
  · `media_effective_pool` 无 `citation_rate` 列,投影也带不出 → 重算 `evidence_score`
    恒 0,而它占 0.45 权重 → 池内蒸馏好的证据分被整个丢弃;
  · `_price_yuan` 用 `_num(v, -1) >= 0` 判"取到了",于是 `our_price_yuan = 0`
    被当成合法价直接返回,**后面的 `price`/`price1` 回退永远走不到**,
    再被 `price < 5` 打成 `price_too_low` 一票否决(risk_tags 非空即拒)。

315 条 `our_price_yuan = 0` 的候选拆开看(replica 实测):
  · **252 条其实有价**(`price` 字段里躺着 15/20/25/30 元)—— 纯粹是回退 bug 误杀;
  · 63 条解析后确实无价 —— 它们是**投影 JOIN 落空**的行(池是快照,源表
    `is_active` 转 false 后没同步),portal/geo_rank/authority 全 0,
    本来就被 `missing_quality_signals` 正确拦下(实测也正好 63 条)。
    所以"未录价给中性分"这条改动在**当前真实数据上净影响为 0**,是防御性的,
    不许申报成它救了谁。

边际贡献(1290 条真实候选,可推荐数):
    现状基线              0
    只修 evidence       972
    只修 price           25      ← 单独修它远远不够
    两条一起           1223
所以两条都要修;只修一条组合包仍然接近空。这组数字本身就是"别只修一半"的判据。
"""

from __future__ import annotations

import pytest

from db.publish_db import get_effective_pool_candidates
from services.publish_recommendation import (
    build_recommendation_packages, classify_media_tier, score_media_candidate,
)

SUMMARY = {"industry": "", "article_type": "qa_solve", "semantic_keywords": [],
           "publish_goal": "提升 AI 搜索引用"}


def _packages(candidates):
    return build_recommendation_packages(
        article_summary=SUMMARY, candidates=candidates, recommendation_level=1,
    )["packages"]


# ══════════════════════════════════════════════════════════════
# 1. 验收本体
# ══════════════════════════════════════════════════════════════

def test_real_production_rows_produce_a_non_empty_package(real_pool):
    """🔴 本单验收本体:1290 条真实行进去,三个组合包都要有货。"""
    candidates = get_effective_pool_candidates(industry="", limit=2000)
    assert len(candidates) == 1290, f"候选数不对:{len(candidates)}(fixture 或投影被改过)"

    packages = _packages(candidates)
    assert packages, "组合包全空 —— 这正是本单要修的那个 P0"
    by_type = {p["package_type"]: len(p["items"]) for p in packages}
    assert by_type == {"trial": 3, "balanced": 5, "authority": 8}, by_type


def test_most_of_the_pool_survives_rescoring(real_pool):
    """池内已经是 173171 选 1290 的结果,请求时重算不该再刷掉九成。"""
    candidates = get_effective_pool_candidates(industry="", limit=2000)
    scored = [score_media_candidate(c, industry="") for c in candidates]
    ok = [s for s in scored if s["is_recommendable"]]
    assert len(ok) == 1223, len(ok)


def test_rescoring_is_still_a_filter_not_a_rubber_stamp(real_pool):
    """🔴 分布锁:不是"全放行"。仍有候选被刷掉,且理由是真风险标签。

    没有这一条,把 `is_recommendable` 写死成 True 也能让上面两条绿。
    """
    candidates = get_effective_pool_candidates(industry="", limit=2000)
    scored = [score_media_candidate(c, industry="") for c in candidates]
    verdicts = {s["is_recommendable"] for s in scored}
    assert verdicts == {True, False}, verdicts
    rejected = [s for s in scored if not s["is_recommendable"]]
    assert rejected, "一条都没刷掉 = 闸没在守"
    assert all(s["risk_tags"] or s["effective_score"] < 50 for s in rejected)


# ══════════════════════════════════════════════════════════════
# 2. 反向对照:证明修的就是这两条,不是别的
# ══════════════════════════════════════════════════════════════

def test_clearing_pooled_evidence_collapses_the_pass_rate(real_pool, mutable_pool):
    """🔴 反向对照 A:清空池内 evidence_score → 通过数从 1223 塌到 **25**。

    🔴 这条锁第一版写的是"塌到组合包全空",**跑出来是红的,而且它错得有道理** ——
       我自己量过的边际贡献就写着"只修 price 也能救 25 条"。
       清空 evidence 之后 price 那条修复仍在生效,所以还剩 25 条,组合包当然非空。
       把断言改成"塌到 25"而不是"塌到 0":既证明证据分这条真在起作用
       (1223 → 25,少了 98%),又没有把另一条修复的功劳算到它头上。
    """
    cur = mutable_pool.cursor()
    cur.execute("UPDATE media_effective_pool SET evidence_score = NULL")
    mutable_pool.commit()

    candidates = get_effective_pool_candidates(industry="", limit=2000)
    assert all(c["evidence_score"] is None for c in candidates)
    scored = [score_media_candidate(c, industry="") for c in candidates]
    survivors = sum(1 for s in scored if s["is_recommendable"])
    assert survivors == 25, survivors


def test_forcing_the_rescued_rows_to_a_real_cheap_price_gets_them_rejected(
        real_pool, mutable_pool):
    """🔴 反向对照 B:把那 252 条"被回退救回来"的行的真实价格改成 3 元 → 必须重新被拦。

    证明本包修的是"回退走不到 + 把缺失当取值",**不是**"取消低价拦截"。

    🔴 这条锁第一版把 UPDATE 打在 `our_price_yuan=0 AND price=0` 上,一条都没命中 ——
       那批(63 条)是 JOIN 落空的行,源表 `is_active=false`,改源表根本传不到投影。
       真正能验到低价拦截的是**有价被救回来**的那 252 条。
       第一版红得有道理:它逼我把 315 拆成 252 + 63。
    """
    before = sum(1 for c in get_effective_pool_candidates(industry="", limit=2000)
                 if score_media_candidate(c, industry="")["is_recommendable"])

    cur = mutable_pool.cursor()
    cur.execute("""
        UPDATE mhz_media SET price = 3
         WHERE is_active AND COALESCE(our_price_yuan, 0) = 0 AND COALESCE(price, 0) > 0
    """)
    assert cur.rowcount > 0, "一行都没改到 —— 反向对照零判别力"
    mutable_pool.commit()

    scored = [score_media_candidate(c, industry="")
              for c in get_effective_pool_candidates(industry="", limit=2000)]
    after = sum(1 for s in scored if s["is_recommendable"])
    cheap = [s for s in scored if "price_too_low" in s["risk_tags"]]
    assert len(cheap) == 252, len(cheap)
    assert after < before, f"改成真便宜之后通过数没降({before} → {after})"


def test_a_genuinely_cheap_medium_is_still_rejected():
    """🔴 反向对照 B:真取到 3 元 → `price_too_low` 照样拦。

    修的是"把没录价当成太便宜",不是"取消低价拦截"。
    """
    row = {"media_id": 1, "media_source": "media", "media_name": "三元小站",
           "our_price_yuan": 3, "portal_media": "门户网站", "geo_rank": 5,
           "inclusion_rate": "80", "pc_weight": 10, "m_weight": 10,
           "authority_media": 1, "evidence_score": 90}
    scored = score_media_candidate(row, industry="")
    assert "price_too_low" in scored["risk_tags"]
    assert scored["is_recommendable"] is False


def test_an_unpriced_medium_is_not_treated_as_cheap():
    """同一条行,价格字段全缺 → 不打 `price_too_low`(信息缺失不是风险)。"""
    row = {"media_id": 1, "media_source": "media", "media_name": "没录价的站",
           "our_price_yuan": 0, "price": 0, "portal_media": "门户网站", "geo_rank": 5,
           "inclusion_rate": "80", "pc_weight": 10, "m_weight": 10,
           "authority_media": 1, "evidence_score": 90}
    scored = score_media_candidate(row, industry="")
    assert "price_too_low" not in scored["risk_tags"]
    assert scored["is_recommendable"] is True


def test_missing_price_gets_a_neutral_score_not_a_perfect_one():
    """🔴 没录价拿**中性分**,不是满分 —— 否则等于奖励数据缺失。"""
    unpriced = score_media_candidate(
        {"media_name": "没录价", "our_price_yuan": 0, "portal_media": "门户"}, industry="")
    assert unpriced["price_score"] == 50.0, unpriced["price_score"]
    # 反向对照:真便宜的价格拿到的是高分(公式还在算,不是被写死成 50)
    cheap = score_media_candidate(
        {"media_name": "便宜站", "our_price_yuan": 20, "portal_media": "门户"}, industry="")
    assert cheap["price_score"] == 99.0, cheap["price_score"]


# ══════════════════════════════════════════════════════════════
# 3. 边界:别把蒸馏侧弄坏
# ══════════════════════════════════════════════════════════════

def test_distill_side_behaviour_is_byte_for_byte_unchanged():
    """🔴 蒸馏脚本喂的是**原始 mhz 行**(没有 evidence_score)→ 必须仍走 citation_rate。

    这条守的是"别拿自己的输出喂自己":若 evidence 分支写成无条件读行内值,
    蒸馏侧会读到 None 走原路径没错,但一旦有人给原始行加了同名字段就会自我强化。
    """
    raw = {"media_name": "原始行", "our_price_yuan": 50, "portal_media": "门户网站",
           "geo_rank": 3, "citation_rate": 0.42}
    scored = score_media_candidate(raw, industry="")
    assert scored["evidence_score"] == 42.0, scored["evidence_score"]

    # 同一条行带上池内证据分 → 以池内为准(两条路径都真的走得到)
    pooled = score_media_candidate({**raw, "evidence_score": 77}, industry="")
    assert pooled["evidence_score"] == 77.0


def test_price_falls_back_past_a_zero_our_price():
    """顺带修好的展示 bug:our_price_yuan=0 但 price=300 → 应取 300,不是 0。"""
    from services.publish_recommendation import _price_yuan, _resolve_price_yuan
    row = {"our_price_yuan": 0, "price": 300}
    assert _resolve_price_yuan(row) == 300
    assert _price_yuan(row) == 300
    # 反向对照:全都没有时才是 None / 0
    assert _resolve_price_yuan({"our_price_yuan": 0, "price": 0}) is None
    assert _price_yuan({"our_price_yuan": 0, "price": 0}) == 0.0


def test_the_fixture_really_contains_both_price_shapes(real_pool):
    """🔴 判据可用性:fixture 里 252(有价被救回)与 63(真无价)两种形态都要在。

    少了任一种,上面的 price 锁就有一半是恒真的。
    """
    from services.publish_recommendation import _resolve_price_yuan
    candidates = get_effective_pool_candidates(industry="", limit=2000)
    zero_field = [c for c in candidates if float(c["our_price_yuan"] or 0) == 0]
    assert len(zero_field) == 315, len(zero_field)

    rescued = [c for c in zero_field if _resolve_price_yuan(c) is not None]
    truly_unpriced = [c for c in zero_field if _resolve_price_yuan(c) is None]
    assert len(rescued) == 252, len(rescued)
    assert len(truly_unpriced) == 63, len(truly_unpriced)

    # 被救回来的:拿到了真实价格,不再是 price_too_low,且真能过闸
    rs = [score_media_candidate(c, industry="") for c in rescued]
    assert not any("price_too_low" in s["risk_tags"] for s in rs)
    assert sum(1 for s in rs if s["is_recommendable"]) > 0

    # 真无价的 63 条:本来就该被 missing_quality_signals 拦(JOIN 落空,信号全 0)
    us = [score_media_candidate(c, industry="") for c in truly_unpriced]
    assert all("missing_quality_signals" in s["risk_tags"] for s in us)
    assert not any(s["is_recommendable"] for s in us)


def test_a_pooled_evidence_of_zero_means_zero_not_missing():
    """🔴 构造用例:池内证据分**恰好是 0** 时,必须当成"证据分 0",不是"没有值"。

    变异分诊留痕(2026-08-09):把 `is not None` 写成真值判断(P6),真实数据上
    **一条都测不出来** —— replica 里 evidence_score 最小 0.8(L1)/68(wemedia),
    永远走不到 0 这一格。那是**空操作变异**,不是锁弱。
    真值判断的后果:池子明确算出"这家没有行业证据"时,反而悄悄回退去读
    citation_rate —— 一个投影里根本不存在的列,于是又变成 0,看起来一样,
    但语义从"池子说 0"变成了"池子没说"。将来补了 citation_rate 列就会爆。
    """
    row = {"media_name": "池内证据为零的站", "our_price_yuan": 50,
           "portal_media": "门户网站", "geo_rank": 3,
           "evidence_score": 0, "citation_rate": 0.9}
    scored = score_media_candidate(row, industry="")
    assert scored["evidence_score"] == 0.0, (
        f"池内明写 0 却回退去读 citation_rate,算成了 {scored['evidence_score']}"
    )
    # 反向对照:池内没有这个键时,才该去读 citation_rate
    fallback = score_media_candidate(
        {k: v for k, v in row.items() if k != "evidence_score"}, industry="")
    assert fallback["evidence_score"] == 90.0, fallback["evidence_score"]
