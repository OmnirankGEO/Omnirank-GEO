"""[§2 verify-only] recommend_for_publish(v1)历史 NameError 已在 base(29b0109e/77971296)修好。

本文件不改函数逻辑,只回归:
- flag 关走原 v1 路径,matched_media 来自真实库存池(get_media_list),非空池 stub(禁止回退 mhz_media=[])。
- 无库存 fail-soft:返回 list、不 500、不裸崩。
"""
import db.diagnosis_db  # noqa: F401  bootstrap geo_research_raw 等

import pytest


def _media(mid, name, platform, price):
    return {"id": mid, "media_name": name, "platform": platform, "price": price,
            "our_price_points": int(price * 130), "our_price_yuan": price,
            "category": "news", "inclusion_rate": "90%", "avg_publish_time": "2h"}


def test_matched_media_comes_from_real_inventory_pool(monkeypatch):
    """flag 关 → 原 v1 路径。调研平台命中库存池媒体 → 推荐来自 cross-match(非空池 stub)。"""
    import db.publish_db as pub
    from services import placement_service as ps

    monkeypatch.setattr("writing.feature_switches.is_feature_enabled", lambda k: False)

    # 库存池:含一个能被"搜狐"命中的媒体(证明 mhz_media 真从 get_media_list 载入并参与交叉匹配)
    pool = {"media": [_media(42, "搜狐网资讯", "搜狐", 100.0), _media(43, "某小站", "小站", 20.0)]}
    monkeypatch.setattr(pub, "get_media_list", lambda **kw: pool)

    # 让调研匹配返回一个平台(搜狐),命中 RESEARCH_TO_MHZ_KEYWORDS['搜狐']
    svc = ps.get_placement_service()
    monkeypatch.setattr(svc, "_match_research_industry",
                        lambda industry: ({"搜狐": {"score": 90, "engines": ["DeepSeek"]}}, "教育培训"))

    out = ps.recommend_for_publish(industry="教育培训", limit=8)
    assert isinstance(out, list) and out, "cross-match 应从库存池产出推荐(非空)"
    # 命中 cross-match 的媒体 id=42,且理由是调研 cross-match 分支("调研引用率高"),非 supplement 兜底
    matched = [r for r in out if r.get("media_id") == 42]
    assert matched, "库存池里被调研平台命中的媒体应出现在推荐中(证明 mhz_media 非空池 stub)"
    assert "调研" in (matched[0].get("reason") or ""), "应走调研×库存 cross-match 分支,不是纯兜底"


def test_no_inventory_fail_soft_returns_list_no_500(monkeypatch):
    """空库存(get_media_list 返空)→ fail-soft 返回 list,不抛、不 500。"""
    import db.publish_db as pub
    from services import placement_service as ps

    monkeypatch.setattr("writing.feature_switches.is_feature_enabled", lambda k: False)
    monkeypatch.setattr(pub, "get_media_list", lambda **kw: {"media": []})
    svc = ps.get_placement_service()
    monkeypatch.setattr(svc, "_match_research_industry", lambda industry: ({}, ""))

    out = ps.recommend_for_publish(industry="教育培训", limit=8)
    assert isinstance(out, list)  # 空库存 → 空推荐,但绝不 NameError / 500
