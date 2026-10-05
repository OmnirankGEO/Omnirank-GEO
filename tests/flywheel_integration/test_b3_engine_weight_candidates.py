"""[B3-1] 回归:引擎权重候选生成 → 审核通过 → UPSERT geo_engine_weights;
以及 _get_engine_weights 合并语义(部分审核不掉其余引擎)。"""
import db.diagnosis_db  # noqa: F401  建 geo_engine_stats / geo_engine_weights 表
import db.engine_weight_candidates_db as ewc
from services.placement_service import PlacementService, ENGINE_WEIGHTS


def _clean(conn):
    c = conn.cursor()
    c.execute("DELETE FROM geo_engine_weight_candidates")
    c.execute("DELETE FROM geo_engine_weights")
    conn.commit()


def _seed_stats(conn, rows):
    c = conn.cursor()
    for (industry, engine, platform, cites, samples) in rows:
        c.execute(
            """INSERT INTO geo_engine_stats
                   (industry, engine, platform, citation_count, total_queries, citation_rate, last_updated)
               VALUES (%s,%s,%s,%s,%s,%s, NOW())
               ON CONFLICT (industry, engine, platform) DO UPDATE SET citation_count=EXCLUDED.citation_count""",
            (industry, engine, platform, cites, samples, (cites / samples if samples else 0)),
        )
    conn.commit()


def test_generate_review_and_upsert(db_with_clean_research):
    conn = db_with_clean_research
    ewc.init_engine_weight_candidate_tables()
    _clean(conn)

    # 某行业:豆包引用远高于其他 → 引用份额 ~0.7,与硬编码 0.35 差异大 → 生成候选
    _seed_stats(conn, [
        ("测试行业", "豆包", "知乎", 700, 1000),
        ("测试行业", "Kimi", "知乎", 150, 1000),
        ("测试行业", "DeepSeek", "CSDN", 150, 1000),
    ])

    n = ewc.generate_engine_weight_candidates(["测试行业"], min_citations=30)
    assert n >= 1, "应至少生成 1 条候选"

    cands = ewc.list_engine_weight_candidates(status="candidate")
    doubao = [c for c in cands if c["engine"] == "豆包" and c["industry"] == "测试行业"]
    assert doubao, f"应有豆包候选: {cands}"
    # 建议权重 = 引用份额 700/1000 = 0.7
    assert abs(float(doubao[0]["suggested_weight"]) - 0.7) < 0.01

    # 审核通过 → geo_engine_weights 写入豆包=0.7
    res = ewc.review_engine_weight_candidate(doubao[0]["id"], reviewer_id=99, decision="approve", note="数据支持上调")
    assert res["status"] == "success" and abs(res["new_weight"] - 0.7) < 0.01

    # _get_engine_weights 合并:豆包=0.7(DB 覆盖),其余引擎仍是硬编码(不掉到 0.1)
    weights = PlacementService()._get_engine_weights()
    assert abs(weights["豆包"] - 0.7) < 0.01, "DB 审核值未覆盖"
    assert abs(weights["Kimi"] - ENGINE_WEIGHTS["Kimi"]) < 1e-9, "其余引擎被错误掉权重"
    assert abs(weights["DeepSeek"] - ENGINE_WEIGHTS["DeepSeek"]) < 1e-9

    # 候选转 approved,不再出现在待审核列表
    assert not any(c["engine"] == "豆包" for c in ewc.list_engine_weight_candidates(status="candidate")
                   if c["industry"] == "测试行业")


def test_small_diff_no_candidate(db_with_clean_research):
    conn = db_with_clean_research
    ewc.init_engine_weight_candidate_tables()
    _clean(conn)
    # 份额接近硬编码(豆包 0.35 ≈ 350/1000)→ 差异 < 10% → 不生成
    _seed_stats(conn, [
        ("平稳行业", "豆包", "知乎", 350, 1000),
        ("平稳行业", "Kimi", "知乎", 250, 1000),
        ("平稳行业", "DeepSeek", "CSDN", 220, 1000),
        ("平稳行业", "千问", "微博", 180, 1000),
    ])
    n = ewc.generate_engine_weight_candidates(["平稳行业"], min_citations=30)
    cands = [c for c in ewc.list_engine_weight_candidates(status="candidate") if c["industry"] == "平稳行业"]
    assert cands == [], f"份额≈现行权重不应生成候选: {cands}"


def test_reject_then_same_value_not_regenerated(db_with_clean_research):
    conn = db_with_clean_research
    ewc.init_engine_weight_candidate_tables()
    _clean(conn)
    _seed_stats(conn, [
        ("拒绝行业", "豆包", "知乎", 700, 1000),
        ("拒绝行业", "Kimi", "知乎", 300, 1000),
    ])
    ewc.generate_engine_weight_candidates(["拒绝行业"], min_citations=30)
    cand = [c for c in ewc.list_engine_weight_candidates(status="candidate")
            if c["industry"] == "拒绝行业" and c["engine"] == "豆包"][0]
    ewc.review_engine_weight_candidate(cand["id"], reviewer_id=1, decision="reject", note="暂不调整")
    # 再次生成:同值(0.7)已被 reject → 不重复生成
    ewc.generate_engine_weight_candidates(["拒绝行业"], min_citations=30)
    reopened = [c for c in ewc.list_engine_weight_candidates(status="candidate")
                if c["industry"] == "拒绝行业" and c["engine"] == "豆包"]
    assert reopened == [], "同值已拒绝的候选不应重复生成"
