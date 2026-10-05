"""监测问题复用合同 · SSOT geo-commercial-intent-governance-v1.0 §4.3。

- 复用客户确认的不可变问题(monitoring_query 逐字优先);
- 老数据无确认问题时按购买关键词**原样**发送;
- 运行时不得再拼接「关键词 + 哪家好」生成另一套问题(归档索引 C)。
"""
from tools.monitoring.batch_monitor import build_question, resolve_monitoring_query


def test_confirmed_question_is_reused_verbatim():
    kw = {
        "keyword": "深圳电梯厂家",
        "monitoring_query": "深圳做旧楼加装电梯的厂家哪家靠谱",
    }
    assert resolve_monitoring_query(kw) == "深圳做旧楼加装电梯的厂家哪家靠谱"


def test_legacy_fallback_is_verbatim_keyword_not_synthetic():
    kw = {"keyword": "深圳电梯厂家", "monitoring_query": ""}
    sent = resolve_monitoring_query(kw)
    assert sent == "深圳电梯厂家"
    assert "哪家好" not in sent
    assert "推荐一下" not in sent


def test_build_question_no_longer_appends_intent():
    sent = build_question("创客教室激光设备采购")
    assert sent == "创客教室激光设备采购"
    assert "哪家好" not in sent


def test_monitoring_api_has_no_synthetic_fallback_string():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    api_src = (root / "api" / "monitoring_api.py").read_text(encoding="utf-8")
    bm_src = (root / "tools" / "monitoring" / "batch_monitor.py").read_text(encoding="utf-8")
    scheduler_src = (root / "api" / "scheduler.py").read_text(encoding="utf-8")
    for src, name in ((api_src, "monitoring_api"), (bm_src, "batch_monitor"), (scheduler_src, "scheduler")):
        assert 'f"{keyword}哪家好' not in src, f"{name} 仍存在运行时拼接问题模板"
        assert "哪家好？推荐一下" not in src, f"{name} 仍存在合成 fallback 字符串"
        assert "哪家好？请推荐几家" not in src, f"{name} 仍存在合成 fallback 字符串"
