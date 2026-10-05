"""DOM target census:前后端两份 `helpTargetForRoute` 的**真实**一致性(WP0 补面)。

## census 发现

小榜的「高亮页面入口」靠 `data-help-target`。这个 target 由**两处各自实现**的同名
算法产生:

* 后端 `services/gap_operation_map.py::help_target_for_route`(签发进动作卡);
* 前端 `frontend/src/components/xiaobang/XiaobangDrawer.tsx::helpTargetForRoute`
  (运行时往 `<main>` 上注入)。

两处一旦漂移,后果是**静默的**:动作卡带着后端算的 target 跳过去,前端注入的是另一个
值,`querySelectorAll` 查不到 → 高亮悄悄不发生,没有任何报错。既有的
`test_all_route_targets_follow_frontend_binding_contract` 只验了后端**自洽**,
并没有把前端那份拿来比。

## 判据怎么打

不重写一份 TS 算法来"对照"(那只会变成第三处实现)。而是把**前端源文件里的那段
函数原文**抠出来丢给 node 真跑一遍,拿它的输出和后端逐条比。
反向对照:把抠出来的源文本改一个字符,同一套比对必须转红。
"""

from __future__ import annotations

import json
import re
import subprocess
import shutil
from pathlib import Path

import pytest

from services import gap_operation_map as omap

_DRAWER = Path("frontend/src/components/xiaobang/XiaobangDrawer.tsx")
_FN = "helpTargetForRoute"

#: 除注册表路由外,再喂一批**边界**输入。两份实现真正会分叉的地方在这里,
#: 不在 /help 这种规矩路径上。
_EDGE_ROUTES = (
    "/", "", "/My-Clients", "/pricing?tab=1", "/publish#anchor",
    "/a//b", "/-leading", "/trailing-", "/中文路径", "/x_y.z",
    "/dashboard/today", "/organization/team",
)


def _extract_frontend_fn() -> str:
    source = _DRAWER.read_text(encoding="utf-8")
    match = re.search(
        r"export function " + _FN + r"\(route: string\): string \{(.*?)\n\}",
        source, re.S,
    )
    assert match, "前端 {0} 抠不出来 —— 判据的输入没了".format(_FN)
    body = match.group(1)
    # 去掉 TS 类型标注,让它能直接在 node 里跑(只动签名,函数体本身是纯 JS)。
    return "function " + _FN + "(route) {" + body + "\n}"


def _run_frontend(fn_source: str, routes: list[str]) -> list[str]:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("环境里没有 node,跑不了真前端算法")
    script = (
        fn_source
        + "\nconst routes = " + json.dumps(routes, ensure_ascii=False) + ";"
        + "\nconsole.log(JSON.stringify(routes.map(" + _FN + ")));"
    )
    proc = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _all_routes() -> list[str]:
    registry = sorted({e.route_template for e in omap.all_operations()})
    return registry + list(_EDGE_ROUTES)


def test_denominator_is_not_empty():
    routes = _all_routes()
    assert len(routes) >= 20, routes


def test_frontend_and_backend_help_targets_agree():
    """逐条比对真前端源码的输出与后端的输出。"""
    routes = _all_routes()
    frontend = _run_frontend(_extract_frontend_fn(), routes)
    backend = [omap.help_target_for_route(r) for r in routes]
    mismatched = [
        (r, b, f) for r, b, f in zip(routes, backend, frontend) if b != f
    ]
    assert not mismatched, "前后端 help_target 漂移:{0}".format(mismatched[:8])


def test_the_parity_criterion_is_alive():
    """🔴 反向对照:改一个字符,同一套比对必须转红。

    没有这一条,「全部一致」与「两边都跑了个空数组」完全同形。
    """
    mutated = _extract_frontend_fn().replace("`route-${slug}`", "`ROUTE-${slug}`")
    assert "ROUTE-" in mutated, "变异没生效,这条自检本身失效了"
    routes = _all_routes()
    frontend = _run_frontend(mutated, routes)
    backend = [omap.help_target_for_route(r) for r in routes]
    assert frontend != backend, "变异后仍然一致 —— 比对是瞎的"


def test_registry_targets_are_either_route_derived_or_a_real_dom_anchor():
    """注册表里的 help_target 只有两种合法来源:

    ① 由 route 推导(前端运行时往 `<main>` 注入,恒能命中);
    ② 页面里**真的存在**一个 `data-help-target="X"` 静态锚点。

    第三种情况 —— 既不是推导值、页面里也没有这个锚点 —— 就是"点了没反应"。
    """
    static_anchors = set()
    for path in Path("frontend/src").rglob("*.tsx"):
        for m in re.finditer(r'data-help-target="([^"]+)"', path.read_text(encoding="utf-8")):
            static_anchors.add(m.group(1))
    assert static_anchors, "全仓一个静态锚点都没扫到,判据的分母是空的"

    orphans = []
    for entry in omap.all_operations():
        derived = omap.help_target_for_route(entry.route_template)
        if entry.help_target == derived or entry.help_target in static_anchors:
            continue
        orphans.append((entry.operation_id, entry.help_target))
    assert not orphans, "help_target 既非 route 推导值、页面里也没有该锚点:{0}".format(orphans)


def test_static_anchor_inventory_is_reported():
    """把静态锚点清单钉住 —— 它是 census 的一部分,少一个就说明有页面改了没同步。"""
    static_anchors = set()
    for path in Path("frontend/src").rglob("*.tsx"):
        for m in re.finditer(r'data-help-target="([^"]+)"', path.read_text(encoding="utf-8")):
            static_anchors.add(m.group(1))
    # 现役三个:交付计划区块 / 单张任务卡 / 写作中心预填锚点。
    assert {"gap-plan-section", "gap-plan-item", "gap-plan-prefill"} <= static_anchors, (
        sorted(static_anchors)
    )
