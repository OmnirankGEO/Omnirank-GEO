"""出现率口径修(原始检出率头条 + 过期显当期值 + 每引擎)· 2026-05-29 · 测试

老板拍板 Q1/Q2:
  Q1 头条 = 原始检出率(全链单一口径含达标)· 市占率加权另存代理端 · 每引擎本期检出展开
  Q2 过期服务 = 显当期真实检出(detection_rate)· 不冻结被污染的 effective_rate
回归红线(不破):倒反天罡(max 保护)/ 懒政(连续不达标切瞬时)/ AI 洞察打架(非过期单一 effective)。

打分链 SQL 嵌在大查询里 · 用源码断言锁口径;v3 算法行为另由 test_compliance_algorithm_v3 覆盖(本次不改算法)。
"""
from __future__ import annotations

import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _read(rel):
    with open(os.path.join(_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


def _func_block(src, name, span=9000):
    i = src.find(f"def {name}(")
    assert i > 0, f"未找到 {name}"
    j = src.find("\ndef ", i + 1)
    return src[i: j if 0 < j < i + span else i + span]


# ============================================================
# Q1 · detection_rate 头条 = 原始检出率(非加权)
# ============================================================
def test_detection_rate_headline_uses_raw_not_weighted():
    blk = _func_block(_read("db/monitoring_db.py"), "get_client_keywords", span=12000)
    # 新增 raw_rate(原始检出 detected/total)
    assert "as raw_rate" in blk, "未新增 raw_rate(原始检出率)"
    # detection_rate 头条改用 raw_rate(不是 weighted_rate)
    assert "ROUND(CAST(ks.raw_rate AS NUMERIC), 1)\n                    ELSE 0\n                END as detection_rate" in blk, \
        "detection_rate 头条未切到 raw_rate"
    # 市占率加权仍另存(代理端深度分析)
    assert "END as weighted_rate" in blk, "weighted_rate 未保留(代理端深度分析)"
    # 每引擎本期检出
    assert "as per_engine_detection" in blk and (
        "jsonb_object_agg(platform, is_detected)" in blk
        or "jsonb_object_agg(platform_key, is_detected)" in blk
    ), \
        "未暴露 per_engine_detection 每引擎检出"


def test_weighted_rate_no_longer_drives_headline():
    """防回退:detection_rate 头条不能再直接用 weighted_rate。"""
    blk = _func_block(_read("db/monitoring_db.py"), "get_client_keywords", span=12000)
    # 不允许 weighted_rate 直接作 detection_rate
    assert "ROUND(CAST(ks.weighted_rate AS NUMERIC), 1)\n                    ELSE 0\n                END as detection_rate" not in blk, \
        "回退:detection_rate 又用了 weighted_rate"


# ============================================================
# [v12 item8] display_rate 履约口径 · 不由自然日 service_expired 决定
# ============================================================
def test_display_rate_ignores_natural_day_expiry():
    """🔴 [v12 item8] display_rate 统一履约滚动平滑 effective_rate(空则 detection_rate)· 【不得】由自然日
    service_expired 决定 —— 旧"过期→display=detection"会把未达标(compliant_days=0)但付款超服务天数的词
    误判过期 → 掉成实时 0%,与"未达标不消耗服务天数"打架。重新引入该分支 → 转红。"""
    src = _read("api/monitoring_api.py")
    assert 'kw["display_rate"] = kw.get("effective_rate") if (kw.get("effective_rate") is not None) else kw.get("detection_rate", 0)' in src, \
        "display_rate 必须统一 effective_rate ?? detection_rate(履约平滑·防 AI 洞察 0% vs 详情 75% 打架)"
    assert 'kw["display_rate"] = kw.get("detection_rate", 0)' not in src, \
        "🔴 display_rate 不得再有 service_expired→detection_rate 分支(未达标词误判过期掉成 0%)"


def test_keyword_table_expiry_uses_compliance_not_natural_day():
    """🔴 [v12 item8] 前端 KeywordTable 到期/交付由履约 compliant_days 决定 · 移除自然日 service_expired 的"已到期"分支
    (未达标 compliant_days=0 但自然日过期的词不得再显"已到期 N 天")。"""
    src = _read("frontend/src/pages/Monitoring/components/KeywordTable.tsx")
    assert "已到期{kw.service_overdue_days" not in src, \
        "🔴 前端不得再用自然日 service_expired 显'已到期'(未达标词误显已到期)"
    assert "kw.service_expired ?" not in src and "&& kw.service_expired ?" not in src, \
        "🔴 到期/倒计时分支不得由自然日 service_expired 门控"
    assert '(kw.compliant_days || 0) >= (kw.service_days || 0)' in src, \
        "服务完成必须由履约 compliant_days>=service_days 判定(非自然日)"


# ============================================================
# P0 回归(源码层确认未破 · 行为由 test_compliance_algorithm_v3 覆盖)
# ============================================================
def test_v3_max_protection_and_lazy_preserved():
    blk = _func_block(_read("db/monitoring_db.py"), "_compute_effective_rate_v3", span=4000)
    # 倒反天罡:max(detection, rolling) 保护
    assert "max(detection_rate, rolling" in blk, "倒反天罡破:max 保护丢失"
    # 懒政:连续不达标 ≥ lazy → 暴露瞬时
    assert "consec_below >= lazy_threshold_days" in blk, "懒政破:连续不达标切瞬时丢失"
