"""发布链 SSOT §9 缺陷② · 短视频 item 的 media_name 存成空串

生产实证:`mhz_publish_order_items` 里唯一一条 svideo item(id=458)的
media_name = ''(空串),订单列表显示缺失。

根因:名字只来自客户端 `req.media_names`,客户端没传/传短了就落 ''。
修法:服务端按 media_id 查权威真名,客户端值只作兜底
      (与"价格已经服务端权威重算"同一个道理 —— 展示字段也不该只信客户端)。

锁双向:
  - 客户端没传 → 必须用服务端名;
  - 服务端查不到 → 必须回退客户端名(fail-soft,绝不因取名失败挡住下单)。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MHZ_API = ROOT / "api" / "meijiehezi_api.py"


def _load_symbol(name: str):
    """只取目标函数的源码执行,避免 import 整个 meijiehezi_api(会拉起一堆三方依赖)。"""
    tree = ast.parse(MHZ_API.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_resolver_function_exists():
    assert _load_symbol("_resolve_svideo_media_names") is not None, (
        "缺 _resolve_svideo_media_names —— 服务端权威取名没实现")


def test_resolver_queries_authoritative_table():
    """必须查 mhz_short_video(短视频账号的权威表),不是别的表。"""
    node = _load_symbol("_resolve_svideo_media_names")
    src = ast.unparse(node)
    assert "mhz_short_video" in src, "必须从 mhz_short_video 取权威账号名"
    assert "media_name" in src


def _svideo_publish_src() -> str:
    """只取短视频发布函数的源码。

    🔴 断言必须限定在【短视频】这条 lane:软文/自媒体 lane(api_publish)同一行写法
       还在,那是另一条 lane、不在本包范围 —— 全文件扫会把它误判成"没修"。
    """
    tree = ast.parse(MHZ_API.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_publish_short_video":
            return ast.unparse(node)
    raise AssertionError("找不到 api_publish_short_video —— 锚点失效")


def test_order_item_prefers_server_name_over_client():
    """短视频下单组装处:必须是「服务端名 or 客户端名」,不能只用客户端名。"""
    src = _svideo_publish_src()
    # 🔴 断言打在【真机制】上 = item 字典里 media_name 的取值表达式。
    #    不能断言"文件里不出现 req.media_names[i]" —— 那个表达式作为 **兜底变量**
    #    是正确且必须保留的(_client_name),按串扫会把正确写法误判成未修。
    assert "'media_name': _name_map.get(int(mid)) or _client_name" in src, (
        "svideo item 的 media_name 没有优先取服务端权威名 —— 缺陷②未修")
    assert "_client_name" in src, "必须保留客户端兜底(服务端查不到时不能落空)"


def test_article_lane_untouched():
    """范围锁:软文/自媒体 lane 不属本包,必须逐字未动(防误伤别人的 lane)。"""
    tree = ast.parse(MHZ_API.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_publish":
            src = ast.unparse(node)
            assert "_name_map" not in src, "改到了软文 lane —— 超出本包范围"
            return
    raise AssertionError("找不到 api_publish —— 锚点失效")


def test_resolver_is_fail_soft():
    """取名失败必须回退,不能抛异常挡住下单(把「低」缺陷升级成阻断)。"""
    node = _load_symbol("_resolve_svideo_media_names")
    src = ast.unparse(node)
    assert "except" in src and "return {}" in src, (
        "取名失败必须 fail-soft 返回空 dict")


def test_resolver_filters_blank_names():
    """服务端查到空名时不能覆盖客户端值 —— 否则等于没修。"""
    node = _load_symbol("_resolve_svideo_media_names")
    src = ast.unparse(node)
    assert "strip()" in src, "必须过滤空白名,空名不入 map"


# ── 行为面:直接跑函数逻辑(用假 cursor,不连真库)──
def test_resolver_behavior_with_fake_db(monkeypatch):
    import types

    mod = types.ModuleType("_probe")
    src = MHZ_API.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_resolve_svideo_media_names")
    ns: dict = {"logger": types.SimpleNamespace(warning=lambda *a, **k: None)}

    class _Cur:
        def execute(self, *_a):
            pass

        def fetchall(self):
            return [{"id": 11, "media_name": "商业逻辑局"},
                    {"id": 12, "media_name": "   "}]  # 空白名必须被过滤

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    fake_db = types.ModuleType("db.connection")
    fake_db.get_connection = lambda: _Conn()
    monkeypatch.setitem(__import__("sys").modules, "db.connection", fake_db)

    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<probe>", "exec"), ns)
    out = ns["_resolve_svideo_media_names"]([11, 12])
    assert out == {11: "商业逻辑局"}, f"空白名应被过滤,实得 {out}"
    assert ns["_resolve_svideo_media_names"]([]) == {}
    del mod
