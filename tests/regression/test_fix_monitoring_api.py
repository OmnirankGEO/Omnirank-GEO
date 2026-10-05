"""
回归判别锁 · api/monitoring_api.py 缺陷修复

GEO-R7-CAN-009 (P3 · freeze-commit-failure-logged-only):
  SSE 监测收尾 _settle 中 commit_freeze 抛错原本只 log → 冻结悬挂(frozen 既未消费也未释放)。
  修复:commit_freeze 有界重试(幂等·3 次)尽力完成消费;仍失败则冻结保持 frozen,
  由 services/freeze_sweeper.py(每小时·12h+ 兜底 release)确定性回收——非只留一行 log。

主形态=source-inspection 判别锁:读源码文本断言修复标志存在。回退修复则断言失败。
不依赖 DB / 不 import server.py。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "api" / "monitoring_api.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _settle_region() -> str:
    """截取 SSE 收尾 _settle 相关代码段(commit 分支)用于定位断言。"""
    idx = SRC.find("async def _settle")
    assert idx != -1, "未找到 SSE 收尾 _settle 定义"
    return SRC[idx: idx + 3000]


def test_marker_present():
    # 修复标记必须存在(回退则缺失)
    assert "GEO-R7-CAN-009" in SRC, "缺少 GEO-R7-CAN-009 修复标记 · 疑似修复被回退"


def test_commit_freeze_has_bounded_retry():
    region = _settle_region()
    # commit_freeze 调用被有界重试循环包裹(回退到单次调用则该循环消失)
    assert "for _c_attempt in range(3)" in region, "commit_freeze 未加有界重试循环"
    # 重试内确实再次调用 commit_freeze
    assert region.count("commit_freeze(") >= 1, "重试块内未调用 commit_freeze"
    # 递增退避(尽力而非硬打)
    assert "asyncio.sleep" in region, "重试缺退避 asyncio.sleep"


def test_commit_failure_not_silently_logged_only():
    region = _settle_region()
    # 重试用尽后必须显式落到 freeze_sweeper 兜底态(可恢复),而不是只 print 一行就完事
    assert "重试用尽" in region, "缺少重试用尽后的兜底处理"
    assert "freeze_sweeper" in region, "未引用 freeze_sweeper 作为确定性回收兜底"
    # 兜底须描述冻结保持 frozen(交 sweeper 回收),证明不是静默悬挂
    assert re.search(r"冻结保持\s*frozen", region), "未声明冻结保持 frozen 待 sweeper 兜底"


def test_freeze_sweeper_backstop_exists():
    # 兜底所依赖的 sweeper 模块真实存在(修复方向落地的前提)
    sweeper = ROOT / "services" / "freeze_sweeper.py"
    assert sweeper.exists(), "freeze_sweeper.py 不存在 · 兜底承诺落空"
    body = sweeper.read_text(encoding="utf-8")
    assert "sweep_zombie_freezes" in body and "release_freeze" in body, \
        "freeze_sweeper 未提供 zombie 冻结回收能力"
