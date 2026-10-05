"""
回归判别锁 · tools/sentiment_classifier.py 上线前 GEO 缺陷修复

覆盖:
  · GEO-R10-CAN-006 舆情降级/不可用状态被伪装成"中性" → consensus='unknown'
  · GEO-R10-CAN-005 引擎回复首部截断漏掉尾部负面证据 → head+tail 覆盖采样 + truncated 标志

主形态为 source-inspection 判别锁(读源码文本断言修复标志存在,回退即 fail),
另附 _coverage_sample 纯函数行为单测(不依赖 DB / 不 import server.py)。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "tools" / "sentiment_classifier.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# GEO-R10-CAN-006 · 降级 != 中性
# ---------------------------------------------------------------------------

def test_can006_degraded_uses_unknown_not_neutral():
    """全失败 / LLM 分类失败 / 缺输入 三条错误路径必须用 consensus='unknown',
    不得再返回 consensus='neutral' 把降级伪装成真实中性共识。"""
    # 修复后源码不应再出现任何 "consensus": "neutral"(neutral 只应作为单引擎 sentiment)
    assert '"consensus": "neutral"' not in SRC, (
        "错误路径仍把 consensus 设成 neutral —— 降级状态被伪装成真实中性(CAN-006 回退)"
    )
    # 至少 3 处错误路径改用 unknown
    assert SRC.count('"consensus": "unknown"') >= 3, (
        "缺输入 / 全失败 / LLM 分类失败 三条降级路径应各返回 consensus='unknown'"
    )
    assert "GEO-R10-CAN-006" in SRC


def test_can006_error_paths_still_carry_error_field():
    """降级路径必须保留 error 字段(renderer 据此弹不完整横幅),不能吞错。"""
    assert '"error": "all engines failed"' in SRC
    assert '"error": "brand_name required"' in SRC
    assert '"error": cls["_error"]' in SRC


# ---------------------------------------------------------------------------
# GEO-R10-CAN-005 · 尾部负面证据不再被首部截断吃掉
# ---------------------------------------------------------------------------

def test_can005_source_has_coverage_sample_and_flag():
    assert "def _coverage_sample(" in SRC, "缺 head+tail 覆盖采样 helper(CAN-005 回退)"
    # 分类 bundle 不得再用裸 [:1500] 二次截断
    assert "r.get('raw_response', '')[:1500]" not in SRC, (
        "分类 bundle 仍用裸 [:1500] 截断 —— char 1500 后的负面证据进不了分类器(CAN-005 回退)"
    )
    assert 'r.get("raw_response", "")[:1500]' not in SRC
    # 保留窗口应改由 _RAW_CAP 统一控制,查询阶段做覆盖采样并带 truncated 标志
    assert "_RAW_CAP" in SRC and "_coverage_sample(raw, _RAW_CAP)" in SRC
    assert '"truncated"' in SRC, "缺对外 truncated 降级标志"
    assert "GEO-R10-CAN-005" in SRC


def test_can005_evidence_after_1500_survives_into_classifier_window():
    """核心场景:唯一负面证据落在 char 2000(> 旧 1500 截断点)但在保留窗口(_RAW_CAP)内,
    覆盖采样必须完整保留它。"""
    from tools.sentiment_classifier import _coverage_sample, _RAW_CAP

    assert _RAW_CAP >= 2000
    # 负面证据放在 char ~2000,整段仍在保留窗口内 → 不截断,原样保留
    prefix = "该品牌服务不错口碑良好。" * 100  # ~1300 字
    text = prefix[:2000] + "有大量差评投诉负面新闻"
    assert len(text) <= _RAW_CAP
    sampled, truncated = _coverage_sample(text, _RAW_CAP)
    assert truncated is False
    assert "差评投诉负面新闻" in sampled, "保留窗口内的负面证据不应被丢弃(CAN-005 未修复)"


def test_can005_coverage_sample_preserves_tail_when_over_cap():
    """超过保留窗口时,尾部(负面新闻常在此)仍必须被首尾覆盖采样保住,并标记 truncated=True。"""
    from tools.sentiment_classifier import _coverage_sample

    filler = "口碑良好服务不错。" * 500  # 远超 budget
    text = filler + "结尾出现差评投诉负面新闻"
    sampled, truncated = _coverage_sample(text, 1500)
    assert truncated is True
    assert "差评投诉负面新闻" in sampled, "尾部负面证据被采样丢弃 —— CAN-005 未真正修复"
    assert len(sampled) <= 1500 + 40  # 采样文本受预算约束(含省略标记)


def test_can005_coverage_sample_shortcircuits_when_within_budget():
    from tools.sentiment_classifier import _coverage_sample

    short = "口碑一般"
    sampled, truncated = _coverage_sample(short, 1500)
    assert truncated is False
    assert sampled == short

    empty, tflag = _coverage_sample("", 1500)
    assert empty == "" and tflag is False
