"""
tests/integration 目录的 conftest。

[开源 E3 · B2 · 2026-09-28] 原来这里是 Phase 4(CTO-15.5 · 2026-04-20)C 端 GEO 方案 D1–D15 集成测试的共享夹具:
两个测试用户、7 个测试品牌、L1 行业知识种子、按用户建 TestClient、dispatch_worker no-op 等。
消费者随 C 端 GEO 方案任务 API(B3c G4)整文件删除,worker 随 B2 删除;全仓 0 处再引用这些夹具
(本目录只剩 FAQ 反馈测试,它自带库连接,不用这里任何东西)⇒ 夹具整体删除,只留把仓库根放进 sys.path 这一步。

跑法:
  python -m pytest tests/integration -x -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
