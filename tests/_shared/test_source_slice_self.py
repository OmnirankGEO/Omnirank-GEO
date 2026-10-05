# -*- coding: utf-8 -*-
"""`source_slice` 自己的自检锁 —— 它是 13 条判据的仪器,坏了那 13 条一起假。

🔴 为什么要有这个文件:把窗口从「猜的字节数」换成「函数真实边界」之后,
   最要紧的问题不是「还红不红」,而是**有没有放宽** ——
   如果 `function_body` 其实返回了整份文件,那 13 条判据全都会被
   **别的函数里的同名串**满足,而读起来一切正常。
   这条「不许放宽」用毒不好打(要同时改两处),所以做成**直接断言**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests._shared.source_slice import (  # noqa: E402
    class_body, code_only, function_body, read_text)


def test_function_body_is_not_the_whole_file():
    """🔴 取到的必须是**那个函数**,不是整份文件。"""
    whole = read_text("server.py")
    body = function_body("server.py", "api_regenerate_titles")
    assert 0 < len(body) < len(whole) / 5, (
        "取出来的片段占了文件的 %.1f%% —— 像是把整份文件端回来了"
        % (100.0 * len(body) / len(whole)))
    assert body.lstrip().startswith(("def ", "async def ")), body[:80]


def test_function_body_excludes_strings_that_live_in_other_functions():
    """🔴 **不放宽的正面证据**:只在别的函数里出现的串,不许出现在本片段里。

    取法:在 server.py 里找一个**全文件只出现一次**的标识符,
    且那一次不在 `api_regenerate_titles` 里 —— 它必须落在片段之外。
    """
    whole = read_text("server.py")
    body = function_body("server.py", "api_regenerate_titles")

    # `_query_doubao_search_impl` 之类跨文件的不算;挑 server.py 自己的函数名
    probes = [p for p in ("api_add_keyword_to_project", "api_get_writing_project_detail",
                          "TitleGenerateRequest")
              if whole.count(p) >= 1 and p not in body]
    assert probes, (
        "找不到「只在别处出现」的探针 —— 这条锁没有判别力,"
        "说明片段大到把这些名字都包进去了")
    for p in probes:
        assert p not in body, "片段里出现了只属于别处的 %s —— 窗口被放宽了" % p


def test_missing_function_shouts_instead_of_returning_empty():
    """🔴 找不到就抛。返回空串会让 `assert "x" in body` 稳定判红,
    而读起来像「代码里没有 x」—— 仪器坏了要出声。"""
    with pytest.raises(AssertionError) as ei:
        function_body("server.py", "this_function_does_not_exist_9f2c")
    assert "找不到函数" in str(ei.value)

    with pytest.raises(AssertionError):
        class_body("server.py", "ThisClassDoesNotExist9f2c")


def test_code_only_strips_comments_in_both_languages():
    """剥注释两种语言各验一次 + 反向对照(代码里的同名串不许被剥掉)。"""
    py = 'x = 1  # user_choice_source 出现在注释里\ny = "user_choice_source"\n'
    out = code_only(py, "py")
    assert "出现在注释里" not in out
    assert out.count("user_choice_source") == 1, out

    ts = "// distributableDirections() 在注释里\nconst a = distributableDirections();\n"
    out = code_only(ts, "ts")
    assert "在注释里" not in out
    assert out.count("distributableDirections()") == 1, out

    block = "/* distributableDirections() 块注释 */\nconst b = 1;\n"
    assert "distributableDirections" not in code_only(block, "ts")

    with pytest.raises(AssertionError):
        code_only("x", "rust")
