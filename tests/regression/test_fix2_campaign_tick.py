"""
判别性回归测试 · FIX2 · tools/geo_managed/campaign_tick.py

覆盖 GEO-R4-CAN-013:
  _publish_article 在发布失败时必须向上抛异常,不能吞掉后正常返回,
  否则调用方(approve 端点 / 24h 自动发布调度)会误标 review 为
  approved/auto_published,记录一次"假交付"。

采用 source-inspection 判别锁为主(不 import server.py / 不依赖 DB):
  断言修复标志存在于 _publish_article 的 except 块中(re-raise + cid 标记)。
若修复被回退(去掉 raise / 恢复吞异常),这些断言会失败。
"""

import ast
import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

TARGET = (
    Path(__file__).resolve().parents[2]
    / "tools" / "geo_managed" / "campaign_tick.py"
)


def _read_source() -> str:
    return TARGET.read_text(encoding="utf-8")


def _get_publish_article_source() -> str:
    """用 AST 精确切出 _publish_article 函数体源码(避免误匹配其它函数)。"""
    src = _read_source()
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "_publish_article":
            start = node.lineno - 1
            end = node.end_lineno  # 1-indexed inclusive
            return "\n".join(lines[start:end])
    raise AssertionError("未找到 _publish_article 函数定义")


def test_fix_marker_comment_present():
    """[GEO-R4-CAN-013] 修复注释标记存在。"""
    assert "GEO-R4-CAN-013" in _read_source(), "缺少 [GEO-R4-CAN-013] 修复标记注释"


def test_publish_failure_is_reraised_not_swallowed():
    """
    判别锁:_publish_article 的最后一个 except 块必须以 `raise` 结尾,
    即发布失败向上传播,而不是记完日志就正常返回(吞异常)。
    """
    src = _read_source()
    tree = ast.parse(src)
    target_fn = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "_publish_article":
            target_fn = node
            break
    assert target_fn is not None, "未找到 _publish_article"

    # 收集函数体内所有 ExceptHandler
    handlers = [n for n in ast.walk(target_fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "_publish_article 应包含 except 块"

    # 找到包裹 log_action('publish_failed', ...) 的那个 except 块
    def _handler_logs_publish_failed(h: ast.ExceptHandler) -> bool:
        for sub in ast.walk(h):
            if isinstance(sub, ast.Constant) and sub.value == "publish_failed":
                return True
        return False

    publish_fail_handlers = [h for h in handlers if _handler_logs_publish_failed(h)]
    assert publish_fail_handlers, "未找到记录 'publish_failed' 的 except 块"

    for h in publish_fail_handlers:
        has_bare_reraise = any(
            isinstance(sub, ast.Raise) for sub in ast.walk(h)
        )
        assert has_bare_reraise, (
            "发布失败的 except 块必须 re-raise 异常(不能吞异常后正常返回),"
            "否则调用方会误标 review 为 approved/auto_published"
        )


def test_publish_article_has_no_return_after_swallow():
    """
    回归防护:确保 except 块内不是"记日志 → 隐式正常返回"。
    通过源码级检查 except 块末尾含 `raise`。
    """
    fn_src = _get_publish_article_source()
    # 找到 publish_failed 之后到函数末尾的片段
    idx = fn_src.find("publish_failed")
    assert idx != -1, "源码应包含 publish_failed 日志"
    tail = fn_src[idx:]
    assert re.search(r"\braise\b", tail), (
        "publish_failed 日志之后必须有 raise,把失败信号抛给调用方"
    )


def test_syntax_compiles():
    """目标文件语法可编译(修复未引入语法错误)。"""
    compile(_read_source(), str(TARGET), "exec")
