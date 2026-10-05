"""监测 SSE 事件必须能序列化(2026-08-03 · QZQZ 事故锁)

事故:2026-08-03 18:17,任务 1532(QZQZ木作美学定制)24/24 全部成功、结果已落库、
状态 completed,但最后那个 SSE 「complete」事件抛
``TypeError: Object of type datetime is not JSON serializable``
(``api/monitoring_api.py:1507``)。前端收不到完成信号,转而收到 error → 用户看到报错。

真因:``complete_event['cells'] = list_monitoring_run_cells(...)`` 的 SELECT 带
``started_at / completed_at / updated_at`` 三个 datetime 列(2026-07-21 `aed2ecc8` 加入),
而 ``json.dumps`` 没有 ``default=str``。实测该任务 24 个 cell 三个时间戳**全部有值**
→ 只要有 cell 真跑完就必炸。

影响面:``trigger_type='manual_sse'`` 共 150 单(界面手动发起,走 SSE);
``scheduled`` 走 cron **不经 SSE**,所以每日自动监测一直没暴露这个问题。

🔴 这个位置**上一轮被点过名却只修了一半**:``monitoring_api.py`` 里
工单 2026-07-29 T3 §3.4 的注释逐字写着「其中 list_monitoring_run_cells(...) 与
json.dumps 并没有各自的 try」,但那次只修了「已 completed 不再降级成 failed」
(所以本次任务状态是对的),**没修序列化本身**。本锁补的就是那一半。

本锁两条:
  1. 结构级 —— 该文件里每一个 SSE ``yield f"data: {json.dumps(...)}"`` 都必须带
     ``default=str``(事故当天 12 处里 0 处有);
  2. 行为级 —— 证明 ``default=str`` 确实能吃下 datetime,而不带它必抛。
"""
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
API = REPO / "api" / "monitoring_api.py"

# 只认【真的往 SSE 流里 yield】的那种:形如  yield f"data: {json.dumps(
SSE_DUMPS = re.compile(r'yield\s+f"data:\s*\{json\.dumps\(')


def _sse_dump_lines(text: str):
    """返回 [(行号, 整行)] —— 所有 SSE json.dumps 行。"""
    return [(i + 1, ln) for i, ln in enumerate(text.split("\n")) if SSE_DUMPS.search(ln)]


def test_file_exists_and_has_sse_dumps():
    """反向对照:先证明口径能抓到东西 —— 否则下面的断言是恒真。"""
    assert API.is_file(), f"{API} 不存在"
    found = _sse_dump_lines(API.read_text(encoding="utf-8"))
    assert len(found) >= 10, (
        f"只匹配到 {len(found)} 处 SSE json.dumps,事故当天实测 12 处 —— "
        f"正则大概率写废了,下面的断言会变成恒真"
    )


def test_every_sse_json_dumps_has_default_str():
    """正向:每一处 SSE json.dumps 都必须带 default=str。"""
    text = API.read_text(encoding="utf-8")
    bad = [(no, ln.strip()[:110]) for no, ln in _sse_dump_lines(text) if "default=str" not in ln]
    assert not bad, (
        "以下 SSE json.dumps 缺 default=str —— 载荷里一旦出现 datetime/Decimal 就会\n"
        "整条流断掉,前端收不到事件(2026-08-03 QZQZ 事故形态):\n"
        + "\n".join(f"  L{no}: {s}" for no, s in bad)
    )


def test_detector_would_catch_a_missing_one():
    """🔴 反向对照:构造一行【缺 default=str】的 SSE 行,检测口径必须抓到它。

    没有这条,上面那条断言"全都带了"可能只是因为正则谁都匹配不到。
    """
    synthetic = 'x = 1\nyield f"data: {json.dumps(evt, ensure_ascii=False)}\\n\\n"\ny = 2\n'
    found = _sse_dump_lines(synthetic)
    assert len(found) == 1, f"合成样本没被匹配到,检测口径失效:{found}"
    assert "default=str" not in found[0][1], "合成样本本就不该带 default=str"


def test_detector_accepts_a_good_one():
    """反向对照的另一半:带了 default=str 的行必须被判为合格(不能恒红)。"""
    synthetic = 'yield f"data: {json.dumps(evt, ensure_ascii=False, default=str)}\\n\\n"\n'
    found = _sse_dump_lines(synthetic)
    assert len(found) == 1
    assert "default=str" in found[0][1]


@pytest.mark.parametrize("payload", [
    {"type": "complete", "cells": [{"started_at": datetime(2026, 8, 3, 18, 14, 0)}]},
    {"type": "cell", "updated_at": datetime.now()},
])
def test_default_str_actually_swallows_datetime(payload):
    """行为级:证明 default=str 真能吃下 datetime,且不带它确实会炸。

    —— 断言"代码里有这个字符串"不等于"它有用";这条把因果补上。
    """
    with pytest.raises(TypeError):
        json.dumps(payload, ensure_ascii=False)          # 不带:必炸(事故原形)
    out = json.dumps(payload, ensure_ascii=False, default=str)   # 带:必通
    assert "2026-08-03" in out or "20" in out
