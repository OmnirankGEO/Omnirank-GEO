# -*- coding: utf-8 -*-
"""子集出题(per_keyword_plan)下游:09-19 起因 400 在生产上一次没跑过的分支,修通后逐个锁(Review 09-28 普查)。

  C7 🔴 失败放回的状态:子集请求失败 ⇒ 放回本次请求前的 writing_status;整表失败 ⇒ pending(老行为)。
        置 pending 会让前端只剩「批量生成标题」,一点就按整表对已付费的词再收一次。
  C8 🔴 组织成员的幂等锚点:子集请求带上每词条数,整表请求锚点逐字不变(否则 1 词请求与 N 词请求撞同一个 execution_id)
  C9 🔴 前端请求编号指纹带上子集:不同词的「立即生成标题」在同一局面下不共用编号(凭据按编号幂等)
"""
from __future__ import annotations

import ast
import functools
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


@functools.lru_cache(maxsize=1)
def _fn() -> str:
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_generate_titles")
    return ast.get_source_segment(src, node)


def failure_status_problems(fn: str) -> list[str]:
    out = []
    pending = fn.count('update_writing_status(request.quote_id, "pending")')
    restored = fn.count("update_writing_status(request.quote_id, _status_on_failure)")
    if pending != 1:
        out.append(f"写死 pending 的应只剩「没有关键词」那 1 处,实际 {pending}")
    if restored != 3:
        out.append(f"三个失败分支应都放回 _status_on_failure,实际 {restored}")
    if not re.search(r'if request\.per_keyword_plan:\n\s+_status_on_failure = detail\["quote"\]\.get\("writing_status"\) or "pending"', fn):
        out.append("_status_on_failure 没按「带 per_keyword_plan 才放回原状态」设")
    return out


def test_c7_subset_failure_restores_the_previous_status():
    fn = _fn()
    assert failure_status_problems(fn) == []
    bad = fn.replace("update_writing_status(request.quote_id, _status_on_failure)",
                     'update_writing_status(request.quote_id, "pending")', 1)
    assert failure_status_problems(bad)                                                   # 毒:一处改回 pending


def test_c8_org_anchor_includes_plan_only_for_subset_requests():
    from services.organization_contract import payload_hash
    fn = _fn()
    assert "if request.per_keyword_plan else {}" in fn and "**_title_anchor_extra," in fn
    base = {"keywords": [1, 2, 3], "topics": []}
    # 整表:extra 为空 ⇒ 与改前同一个哈希
    assert payload_hash({**{}, **base}) == payload_hash(base)
    # 子集:1 词计划 vs 3 词计划 ⇒ 不同哈希(不再撞 execution_id)
    one = payload_hash({"plan": [(1, 35), (2, 0), (3, 0)], **base})
    three = payload_hash({"plan": [(1, 35), (2, 35), (3, 35)], **base})
    assert len({one, three, payload_hash(base)}) == 3


def test_c9_request_fingerprint_includes_the_subset():
    tsx = (ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx").read_text(encoding="utf-8")
    m = re.search(r"const titleFingerprint = JSON\.stringify\(\{(.*?)\}\);", tsx, re.S)
    assert m, "指纹没找到"
    assert "onlyKeywordIds:" in m.group(1)
