"""R4 §C§D 判据:用户面不许再有"裸 status 绿勾",且后端每个主状态都得有前端桶。

[WO_273 · 2026-09-23] §C 三格随「自发记录」子分页一起肯定式退役(见 §C 段注释);§D 照旧在守。

两条都在守同一件事:**后端把话说清楚了,前端得真的听**。
R2/R3 反复出现的形态是"后端返齐了字段、前端没接" —— 那种缺陷在 UI 上是**静默**的:
没有报错、没有空态,只是绿勾还在、或者文章不见了。肉眼看不出来,只能由锁发现。

🔴 为什么打源码而不是端到端渲染:这两条命题很窄(某个分支存在 / 某个键有对应),
   拉起浏览器只会让判据更脆。但源码锁必须**紧邻匹配**,不能子串包含 ——
   R3 那发 M41 就是栽在"变异体是原文的超串"上。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCOPE_LOGIC = ROOT / "frontend/src/pages/Publishing/publishCenterScopeLogic.ts"
PUBLISH_CENTER = ROOT / "frontend/src/pages/Publishing/PublishCenter.tsx"
STATUS_FILTER = ROOT / "frontend/src/components/publishing/ArticleStatusFilter.tsx"
MHZ_API = ROOT / "api/meijiehezi_api.py"


# ===========================================================================
# §C · OrderManagementPage 的「自发记录」不许再用裸 status 判绿勾 —— [WO_273 · 2026-09-23 肯定式退役]
# ===========================================================================
# 原来三格(`test_order_page_has_no_bare_status_success_check_icon` /
# `test_order_page_reserves_the_green_check_for_verified_only` /
# `test_order_page_shows_the_publication_label`)守的是:订单管理「自发记录」子分页里,
# 回执成功不许画成绿勾、绿勾只跟 `verified_published`、得把发布轴那句话写出来。
# 被测对象**整块删了**:WO_273-A(21afc6084)随浏览器插件退役删掉了 `PublishSelfRecords`
# 子分页 —— 它的数据只来自插件端点 `/api/extension/publish-records`(WO_273-C 删)。
# 不是换了实现,是这一页不存在了;绿勾那句话已无处可说。
# 接替(「它不许回来」):
#   · build 链第 69 步 `frontend/scripts/test-self-publish-retired.mjs` 的 S2:
#     frontend/src 全部文件 `/api/extension` = 0 行 —— 自发记录子分页的唯一数据源请求不许回来(每次 bake 跑);
#   · 行为臂 A3(`frontend/scripts/test-self-publish-retired-render.mjs`,`browser:arms`):
#     订单管理没有「自发记录」,老链 `?tab=self` 落回「概览」。
# 登记:tests/RETIRED_TESTS.txt 按格各一行。对应变异 M53 / M54(run_mutations.py)随同退役。


# ===========================================================================
# §D · 后端每个 primary 值都必须有前端桶(奇偶锁)
# ===========================================================================

def _backend_primary_values() -> set[str]:
    """从 `api_article_publish_stats` 里机械取出所有 `primary = "..."` 的取值。

    🔴 用 AST 不用 grep:注释里出现的状态名不算数,只有真赋给 `primary` 的才算。
    """
    tree = ast.parse(MHZ_API.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "api_article_publish_stats")
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "primary":
                    if isinstance(node.value, ast.Constant) and isinstance(
                            node.value.value, str):
                        out.add(node.value.value)
    return out


def _frontend_bucket_map() -> dict[str, str]:
    """解析 `SCOPE_BUCKET_BY_STATUS` 那张表。"""
    src = SCOPE_LOGIC.read_text(encoding="utf-8")
    m = re.search(
        r"export const SCOPE_BUCKET_BY_STATUS[^=]*=\s*\{(?P<body>.*?)\n\};",
        src, re.DOTALL)
    assert m, "SCOPE_BUCKET_BY_STATUS 找不到了 —— 奇偶锁的锚点没了"
    return dict(re.findall(r"(\w+):\s*'([\w]+)'", m.group("body")))


def test_extractors_are_not_vacuous():
    """🔴 分母自证:两个提取器都得真取到东西,而且取的是**已知的那几个**。

    不加这条,下面的"逐个都有桶"可能只是因为后端集合是空的。
    """
    backend = _backend_primary_values()
    frontend = _frontend_bucket_map()
    assert len(backend) >= 4, backend
    assert {"published", "in_progress", "rejected", "none"} <= backend, backend
    assert len(frontend) >= 4, frontend
    assert frontend.get("published") == "published", frontend


def test_every_backend_primary_has_a_frontend_bucket():
    """🔴🔴 奇偶锁:后端 primary 的每个取值都必须在前端有桶。

    少一个 → 该状态的文章**从四个 tab 里同时消失**(四个分组都是等值过滤,
    没有任何一支收容"未知状态")。R2 给后端加了
    `reported_success_unverified` 却没加前端那一格,那 26 篇就是这么不见的。
    """
    backend = _backend_primary_values()
    frontend = _frontend_bucket_map()
    missing = sorted(backend - set(frontend))
    assert not missing, (
        f"后端会返回这些主状态,但前端没有对应的桶:{missing}\n"
        "这些文章会从发布中心的所有 tab 里同时消失(静默,无报错、无空态)。")


def test_no_frontend_bucket_is_orphaned():
    """反方向:前端也不许有后端永远不会发的桶 —— 那是个永远空的 tab。"""
    backend = _backend_primary_values()
    frontend = _frontend_bucket_map()
    orphan = sorted(set(frontend) - backend)
    assert not orphan, f"这些前端桶后端永远不会触发(永远空的 tab):{orphan}"


def test_reported_unverified_bucket_is_wired_all_the_way_to_a_tab():
    """光有映射表不够 —— 桶得真的接到一个 tab 上,否则文章照样看不见。

    三处逐个钉:分组数组 / tab key / tab 的渲染分支。
    """
    logic = SCOPE_LOGIC.read_text(encoding="utf-8")
    center = PUBLISH_CENTER.read_text(encoding="utf-8")
    filt = STATUS_FILTER.read_text(encoding="utf-8")

    assert "reportedUnverified: T[];" in logic, "PartitionResult 上没有这个分组"
    assert "const reportedUnverified = availableArticles.filter" in logic, "分组没被算出来"
    assert "'reportedUnverified'" in filt, "ArticleStatusKey 少这个 key"
    center_lines = [ln.strip() for ln in center.splitlines()]
    assert "key: 'reportedUnverified'," in center_lines, "筛选器没有这个 tab"
    assert "{articleStatusFilter === 'reportedUnverified' && (" in center_lines, (
        "没有这个 tab 的渲染分支 —— 点得到但点开是空的")


@pytest.mark.parametrize("status", ["published", "in_progress",
                                    "reported_success_unverified", "rejected", "none"])
def test_bucket_map_is_total_over_the_known_domain(status):
    """逐格钉,失败信息直接说是哪一格 —— 集合断言失败时那句话不够具体。"""
    assert status in _frontend_bucket_map()
