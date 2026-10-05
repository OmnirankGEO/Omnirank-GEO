"""[WP9-P0-2] 竞品名称自动核验桥判别。

锁定:AI 真实答案(监测蒸馏)出现 ≥3 次的**已有**竞品名 → name_verified=True + 血缘;
<3 次不核验;蒸馏无数据 → 零改动(行为不变);禁虚构(只标已存在项,绝不新增名);
已 human_verified 的项不被覆盖来源。
"""
import services.writing_competitor_auto_verify as av


def _radar(brands):
    return {"brands": brands}


def test_ge3_mentions_become_verified_with_lineage(monkeypatch):
    monkeypatch.setattr(av, "_norm", lambda n: str(n or "").strip())
    monkeypatch.setattr(
        "db.distillation_db.get_competitor_radar",
        lambda bid, days=30: _radar([
            {"brand_name": "甲公司", "mention_count": 5},
            {"brand_name": "乙公司", "mention_count": 3},
            {"brand_name": "丙公司", "mention_count": 2},  # <3 不核验
        ]),
        raising=False,
    )
    m = av.auto_verified_competitor_map(101)
    assert set(m) == {"甲公司", "乙公司"}  # 丙公司(2 次)被排除
    assert m["甲公司"]["mention_count"] == 5
    assert m["甲公司"]["method"] == av.AUTO_VERIFY_METHOD
    assert m["甲公司"]["source"] == av.AUTO_VERIFY_SOURCE


def test_annotate_marks_only_existing_hits(monkeypatch):
    monkeypatch.setattr(av, "_norm", lambda n: str(n or "").strip())
    comps = [{"name": "甲公司"}, {"name": "未上榜公司"}]
    vmap = {"甲公司": {"mention_count": 4, "method": av.AUTO_VERIFY_METHOD, "source": av.AUTO_VERIFY_SOURCE}}
    newly = av.annotate_competitor_verification(comps, vmap)
    assert newly == 1
    assert comps[0]["name_verified"] is True
    assert comps[0]["name_verified_mention_count"] == 4
    assert comps[0]["verify_source"] == av.AUTO_VERIFY_SOURCE
    # 未命中的项绝不被标(禁虚构/不误核验)
    assert "name_verified" not in comps[1]


def test_empty_map_is_noop_behavior_unchanged():
    comps = [{"name": "甲公司"}, {"name": "乙公司"}]
    assert av.annotate_competitor_verification(comps, {}) == 0
    assert all("name_verified" not in c for c in comps)


def test_human_verified_source_not_overwritten(monkeypatch):
    monkeypatch.setattr(av, "_norm", lambda n: str(n or "").strip())
    comps = [{"name": "甲公司", "human_verified_name": True, "verify_source": "human"}]
    vmap = {"甲公司": {"mention_count": 9, "method": av.AUTO_VERIFY_METHOD, "source": av.AUTO_VERIFY_SOURCE}}
    assert av.annotate_competitor_verification(comps, vmap) == 0
    assert comps[0]["verify_source"] == "human"  # 人工核验来源不被自动桥覆盖


def test_no_distillation_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "db.distillation_db.get_competitor_radar",
        lambda bid, days=30: {"brands": []},
        raising=False,
    )
    assert av.auto_verified_competitor_map(101) == {}
    # brand_id 不可用 → 空(行为不变)
    assert av.auto_verified_competitor_map(None) == {}
    assert av.auto_verified_competitor_map(0) == {}


def test_radar_exception_fails_soft_to_empty(monkeypatch):
    def _boom(bid, days=30):
        raise RuntimeError("distillation down")
    monkeypatch.setattr("db.distillation_db.get_competitor_radar", _boom, raising=False)
    assert av.auto_verified_competitor_map(101) == {}  # fail-soft → 空 → 行为不变
