"""R3 §②④⑥ 判据:缓存命中路径 / 新列进 readiness / 相对 Location。

这三条不需要库,但都有一个共同的形状:**判据必须打在真正会被走到的那条路上**。

  · §② [WO_273 已退役:该端点随插件后端删除,见下方该节]
        生产上发布完立刻轮询 `/publish-result`,命中的几乎全是 Redis 缓存。
        R2 只给 DB fallback 加了双轴 —— 也就是说用户面真正走的那条路没改。
        所以这里禁止 mock 成 cache miss:必须**真写缓存、真命中**,
        并且把 DB 那条路封死(一碰就炸),否则"过了"可能只是又走了 fallback。
  · §④ 新列必须同时进 startup safe-add 与 schema contract(本仓既有规则)。
        判据用 AST 取真实列清单,不 grep —— 注释里出现列名不算数。
  · §⑥ `Location: /a/123` 是合法的相对重定向。不 urljoin 的话下一跳没有 scheme,
        安全校验直接判失败 → 一次**正常的站内 302** 被记成「探不动」。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ===========================================================================
# §② `/publish-result` 的 Redis 命中路径 —— [WO_273 · 2026-09-23 肯定式退役]
# ===========================================================================
# 这里原有 4 格 + 夹具:缓存命中带双轴 / 上一版缓存条目命中后重投影 / 回执失败对照 /
# WS 写入方把核实态塞进缓存。它们守的是插件后端 `/publish-result` 的缓存命中路径;
# 该端点、WS 写入方与缓存模块随插件后端**整体删除** —— 被守的那条路不存在了,不是换了实现。
# 接替:
#   · 「这条路不许悄悄回来」→ tests/extension_retirement_2026_09_23 的路由缺席锁
#     (app.routes 里不许有 /api/extension* 与 /ws/extension)与模块缺席锁;
#   · 「回执成功 ≠ 已发布」在**仍在役**的用户面上照旧有判据:
#     test_dual_axis_user_surfaces_pg16.py 的已分发聚合 / 发布统计 / 统一记录列表各格与
#     SQL 轴 = Python 轴那一格(投影本身 services/publication_receipt_projection.py 未动)。
# 对应变异 M36 / M37 一并从 run_mutations.py 退役。


# ===========================================================================
# §④ 新列同时进 startup safe-add 与 schema contract
# ===========================================================================

R3_NEW_COLUMNS = (
    "public_url_verification_source",
    "public_url_availability_state",
    "public_url_availability_checked_at",
    "public_url_availability_detail",
)


def _safe_add_columns() -> set[str]:
    """从 startup safe-add 函数里用 AST 取真实的 safe-add 列清单。

    🔴 不用 grep:注释里写了列名不代表那一列真的会被 ADD。本仓的原话 ——
       census 用裸符号名会把 import 当调用。
    🔴 [WO_273 · 改指向] 这段 safe-add 原在插件后端的 `_init_extension_tables`;插件后端退役时
       **按字节原样**搬到 `db/publish_records_schema.py::init_publish_records_table`
       (逐字相同由 `tests/extension_retirement_2026_09_23` 锁)。本锁守的规则不变,只换址。
    """
    tree = ast.parse((ROOT / "db" / "publish_records_schema.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "init_publish_records_table")
    found: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Tuple) and len(node.elts) == 2:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
    return found


@pytest.mark.parametrize("column", R3_NEW_COLUMNS)
def test_new_column_is_in_startup_safe_add(column):
    assert column in _safe_add_columns(), (
        f"{column} 不在 startup safe-add 里 —— 冷启动/新库那条路上没有这一列")


@pytest.mark.parametrize("column", R3_NEW_COLUMNS)
def test_new_column_is_in_schema_contract(column):
    from services.geo_article_v14_schema_contract import COLUMNS

    assert column in COLUMNS["publish_records"], (
        f"{column} 不在 readiness 契约里 —— 少这一列 readiness 会漏报")


def test_safe_add_extractor_can_actually_fail():
    """分母自证:上面那个 AST 提取器**不是**恒返回全集。

    不加这条,`assert column in _safe_add_columns()` 可能只是因为提取器把
    整个文件里所有字符串都收进来了 —— 那种绿一个变异都杀不动。
    """
    cols = _safe_add_columns()
    assert "public_url_verification_state" in cols
    assert "brand_id" not in cols, "brand_id 是单独 ALTER 的,不在这张 safe-add 表里"
    assert "definitely_not_a_column_2026" not in cols


# ===========================================================================
# §⑥ 相对 Location 的 302
# ===========================================================================

def _fake_transport(monkeypatch, script):
    """把安全传输层换成脚本化假件,并**记录每一跳收到的 URL**。

    判据要的正是"第二跳收到了什么" —— 只断言最终 200 的话,
    urljoin 有没有生效看不出来(不 urljoin 时第二跳会在安全校验里就炸)。
    """
    import services.publication_url_verifier as verifier

    hops: list[str] = []

    def _hop(url):
        hops.append(url)
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        # 逐字复刻真实防线的形状:没有 scheme/host 的相对路径在这里就该炸。
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(f"unsafe hop: {url}")
        return parts.hostname, ["1.2.3.4"], (parts.path or "/")

    def _get(host, ips, target):
        return script.pop(0)

    monkeypatch.setattr(verifier, "_assert_safe_probe_hop", _hop)
    monkeypatch.setattr(verifier, "pinned_https_get_body", _get)
    return hops


def test_relative_location_is_resolved_against_current_url(monkeypatch):
    """正样本:`302 → /article/990101/` 必须被解析成绝对 URL 后继续跳。"""
    from services.publication_url_verifier import _default_fetcher

    hops = _fake_transport(monkeypatch, [
        (302, {"location": "/article/990101/"}, ""),
        (200, {}, "<html>正文</html>"),
    ])
    out = _default_fetcher("https://www.toutiao.com/a/old-path")

    assert out["status"] == 200
    assert hops == ["https://www.toutiao.com/a/old-path",
                    "https://www.toutiao.com/article/990101/"]
    assert out["final_url"] == "https://www.toutiao.com/article/990101/"


def test_protocol_relative_location_is_resolved(monkeypatch):
    """`//host/path` 同样是合法 Location(继承当前 scheme)。"""
    from services.publication_url_verifier import _default_fetcher

    hops = _fake_transport(monkeypatch, [
        (301, {"location": "//www.toutiao.com/x/1"}, ""),
        (200, {}, "<html>正文</html>"),
    ])
    out = _default_fetcher("https://m.toutiao.com/a/1")
    assert out["status"] == 200
    assert hops[1] == "https://www.toutiao.com/x/1"


def test_absolute_location_still_goes_through_the_same_guard(monkeypatch):
    """反向对照:绝对 Location 行为不变,而且**照样逐跳过安全校验**。

    urljoin 不是"放宽" —— 它只负责把相对路径补全,补全后那一跳的 https/公网 IP
    校验一格没少。这里用一个跳到 http 的 Location 证明防线还在。
    """
    from services.publication_url_verifier import _default_fetcher

    _fake_transport(monkeypatch, [
        (302, {"location": "http://insecure.example.com/x"}, ""),
        (200, {}, "<html>正文</html>"),
    ])
    with pytest.raises(ValueError):
        _default_fetcher("https://www.toutiao.com/a/1")
