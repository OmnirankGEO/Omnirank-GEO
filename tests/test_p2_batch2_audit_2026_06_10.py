# -*- coding: utf-8 -*-
"""P2 批2(audit · 2026-06-10):#11 漏斗 0 样本层权重重归一 + #18 delete_client 热路径 DDL 移除。"""
from pathlib import Path

from tools.scoring.funnel_score import calculate_funnel_score

ROOT = Path(__file__).resolve().parents[1]


# ---------- #11 漏斗权重重归一 ----------

def test_11_normal_three_layers_unchanged():
    """三层都有样本 → 重归一 = 原始权重,分数与旧逻辑一致(正常诊断零影响)。"""
    r = calculate_funnel_score(
        brand_detected=10, brand_total=10,
        local_detected=10, local_total=10,
        scenario_detected=10, scenario_total=10,
    )
    assert r["total_score"] == 100  # 全命中 = 满分
    assert r["level_meta"]["partial_sample"] is False
    for ly in r["layers"]:
        assert ly["effective_weight"] == ly["weight"]  # 无 0 样本层 → 不放大


def test_11_empty_layer_reweights_not_capped():
    """scenario 层 0 样本(LLM 分类失败)→ 旧版封顶 60;新版按 brand+local 折算到 100。
    brand(w=20)+local(w=40) 都满命中 → 重归一后两层 eff_weight=33.3/66.7 → 满分 100(不再被砍到 60)。"""
    r = calculate_funnel_score(
        brand_detected=10, brand_total=10,
        local_detected=10, local_total=10,
        scenario_detected=0, scenario_total=0,
    )
    assert r["level_meta"]["partial_sample"] is True
    assert r["total_score"] == 100, "有样本层全命中 → 重归一后应满分(旧版被砍到 60)"
    sc = next(l for l in r["layers"] if l["key"] == "scenario")
    assert sc["effective_weight"] == 0.0 and sc["score"] == 0.0  # 0 样本层不计分不占权重


def test_11_empty_layer_partial_rate():
    """有样本层半命中 → 折算后约半分(不被 0 样本层拖累成更低)。"""
    r = calculate_funnel_score(
        brand_detected=5, brand_total=10,      # 50%
        local_detected=5, local_total=10,      # 50%
        scenario_detected=0, scenario_total=0, # 无样本
    )
    assert r["total_score"] == 50  # (0.5×33.3)+(0.5×66.7)=50
    assert r["level_meta"]["partial_sample"] is True


def test_11_all_empty_degenerates_zero():
    """全无样本 → 退化 0 分(不除零崩)。"""
    r = calculate_funnel_score()
    assert r["total_score"] == 0
    assert r["level_meta"]["partial_sample"] is True


def test_11_weights_sum_still_100():
    """原始权重和仍为 100(展示层不变)。"""
    r = calculate_funnel_score(brand_detected=1, brand_total=2,
                               local_detected=1, local_total=2,
                               scenario_detected=1, scenario_total=2)
    assert sum(l["weight"] for l in r["layers"]) == 100


# ---------- #18 delete_client 热路径 DDL ----------

def test_18_no_ddl_in_delete_client():
    src = (ROOT / "api" / "brand_api.py").read_text(encoding="utf-8")
    i = src.find("def delete_client") if "def delete_client" in src else src.find("级联软删除关联数据")
    blk = src[i:i + 1500]
    assert "ALTER TABLE diagnosis_records" not in blk, "delete_client 热路径不得再跑 DDL(ACCESS EXCLUSIVE 锁阻塞全表)"
    assert "UPDATE diagnosis_records SET is_deleted = TRUE" in blk  # 直接 UPDATE 既有列


# ---------- #7 Fable 返修(2026-06-10)----------

def test_7_init_db_has_soft_delete_columns():
    """[#7 返修] is_deleted/deleted_at 必须进 init_db 的 _safe_add_column —— 全新 init / 灾备库
    delete_client 不再撞 UndefinedColumn(原靠'历史某次 delete 自建'的运气)。"""
    src = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    i = src.find("def init_db")
    blk = src[i:i + 25000]
    assert '_safe_add_column(cursor, "diagnosis_records", "is_deleted"' in blk, "is_deleted 须进 init_db"
    assert '_safe_add_column(cursor, "diagnosis_records", "deleted_at"' in blk, "deleted_at 须进 init_db"


def test_7_brand_only_capped_not_dominant():
    """[#7 返修 行为] 只测品牌名(brand 满命中,local/scenario 0 样本)→ 重归一把 brand 20 权重放大到
    100 → 单层满分。封顶:最高只到成长级,不冒充主导/健康级(从一个数据点宣称市场主导)。"""
    r = calculate_funnel_score(
        brand_detected=10, brand_total=10,
        local_detected=0, local_total=0,
        scenario_detected=0, scenario_total=0,
    )
    assert r["total_score"] == 100, "单层满分重归一后仍 100(分数本身不变)"
    assert r["level"] not in ("主导级", "健康级"), "单层覆盖被封顶,不得冒充主导/健康级"
    assert r["level"] == "成长级"
    assert r["level_meta"]["partial_sample"] is True
    assert r["level_meta"]["level_capped"] is True


def test_7_two_layers_not_capped():
    """[#7 返修 行为] 双层覆盖(brand+local 满命中,effective_weight_sum=60)→ 不封顶(合理折算)。"""
    r = calculate_funnel_score(
        brand_detected=10, brand_total=10,
        local_detected=10, local_total=10,
        scenario_detected=0, scenario_total=0,
    )
    assert r["level_meta"]["level_capped"] is False, "双层覆盖(60)不应封顶"
    assert r["total_score"] == 100


def test_7_renderer_uses_effective_weight_denominator():
    """[#7 返修] 渲染层分母改 effective_weight(消除 score/原始weight 的 '100/20' 矛盾数)+ 空层'无样本'。"""
    html = (ROOT / "services" / "report_html_renderer.py").read_text(encoding="utf-8")
    assert "effective_weight" in html, "HTML 渲染须用 effective_weight 分母"
    assert "无样本" in html, "空样本层须显示无样本(不印 0/0)"
    wv2 = (ROOT / "services" / "report_writer_v2.py").read_text(encoding="utf-8")
    assert "effective_weight" in wv2, "writer_v2 须用 effective_weight 分母"
