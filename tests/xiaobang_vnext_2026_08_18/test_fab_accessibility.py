"""小榜 FAB 的可达性与遮挡(规格 §18 WP1 · §21 验收门)。

验收门原话:「同一 intent 任何时刻只有一个确认 CTA;**FAB 不遮挡页面主按钮**」
「键盘可达、焦点清晰」。

## 🔴 本文件的第一等公民不是几何,是「挂载性前置」

同一个病我犯了**三次**,形态一次比一次隐蔽:

1. R2 · **锚不存在** —— 说 FAB 压住 ``MobileTabBar``,而那个组件全仓
   ``<MobileTabBar`` **零挂载**;
2. R3 · **锚的上游不存在** —— 拿 M3 销售客户页当共存对象,
   它在 ``App.tsx`` 里**零路由**;同一类还有 M3 副驾的悬浮钮 / 面板两件套,
   它们**有**使用者,但使用链的根(M3 布局壳)全仓零引用;
   ([开源 E3 · 前端 · 2026-10-01 · WO_322] 这四个组件随 pages/M3 · components/m3 整删;链式死亡的牙证改用合成夹具,
   见 :func:`test_the_recursion_is_what_catches_the_chain_deaths`)
3. R3-P3 · **锚存在但不共时** —— 把 ``OnboardingChecklist`` 圆球算进共存 CTA,
   而 ``Layout.tsx`` 用 ``!sandboxUI`` 关掉小榜 FAB、用 ``sandboxUI`` 打开圆球,
   两者永不同屏。

共同形态:**几何算得没错,锚是死的(或不共时)**。区间不相交对"不存在的东西"
恒成立,所以判据全绿而现实什么都没验证。

因此:**任何进入几何分母的锚,必须先拿出挂载证据;拿不出就报红,不是跳过。**
"跳过"和"通过"在退出码上一样 —— 那正是前几次没被发现的原因。

🔴 **一处旧结论已作废(R3-P3 订正)**:本 docstring 早先写过
「``/m3`` 与 ``/m3/*`` 的 catch-all 把 M3 销售客户页全量吞掉」。**那是错的** ——
react-router v6 ``<Routes>`` 按具体度排名挑分支,splat ``*`` 排最低,
任何静态段路由都赢它。那一页的死因是**连路由都没有**;
catch-all 只决定"那个 URL 最后落到哪",不决定"组件为什么不渲染"。
细节见下方第 42 行起的订正说明与 :func:`url_fate`。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

_FRONTEND = Path("frontend/src")
_APP = _FRONTEND / "App.tsx"
_LAYOUT = _FRONTEND / "components/layout/Layout.tsx"
_FLOATING_STACK = _FRONTEND / "lib/floating-stack.ts"
_AGENT_FAB = _FRONTEND / "components/agent/AgentFAB.tsx"

# ── 挂载性前置(R3-P3:递归到真正的 Route 根)──────────────────────────────
#
# R3-P2 的版本只看**直接**使用者:只要全仓有人写过 `<Name`,就判"挂载了"。
# 这个口径挡不住**链式死亡** —— 当年 M3 副驾悬浮钮有使用者(M3 布局壳),
# 但布局壳自己全仓零引用,整条链吊在空中。所以要一路递归到 `App.tsx`
# 里真实的 `<Route>`,走不到就是死的。
#
# 🔴 同时**订正** R3-P2 的一句错话。我当时写「M3 销售客户页被 `/m3/*` 的
#    catch-all Navigate 吞掉」。react-router v6 的 `<Routes>` 按**具体度排名**
#    挑分支,不是先到先得:splat `*` 排最低,任何静态段路由都赢它。
#    所以只要那一页还有自己的 `<Route path="/m3/sales/clients">`,
#    它就赢过 `/m3/*`,不会被吞。它真正的死因是**连路由都没有**
#    (28 个 M3 lazy import 在 2026-05-05 一并删了)。
#    catch-all 决定的只是"那个 URL 最后落到哪",不是"组件为什么不渲染" ——
#    这两件事我上一轮混为一谈了。因此:
#      · 死法只保留两种:NO_USAGE / DEAD_CHAIN(各有真实实例,逐条精确命中);
#      · catch-all 降级成信息性的 :func:`url_fate`,只回答 URL 去向,不再当死因。

LIVE = "LIVE"
NO_USAGE = "NO_USAGE"          # 死法一:全仓没人写 <Name
DEAD_CHAIN = "DEAD_CHAIN"      # 死法二:有人写,但那条链走不到任何 <Route>

_TSX = tuple(_FRONTEND.rglob("*.tsx"))
_SRC_CACHE: dict[Path, str] = {}


def _src(path: Path) -> str:
    if path not in _SRC_CACHE:
        _SRC_CACHE[path] = path.read_text(encoding="utf-8")
    return _SRC_CACHE[path]


def _declared_in(path: Path) -> set[str]:
    """一个文件对外提供哪些组件名(供递归时"换名字继续往上找")。"""
    source = _src(path)
    names = set(re.findall(r"export\s+(?:default\s+)?function\s+([A-Z]\w*)", source))
    names |= set(re.findall(r"export\s+const\s+([A-Z]\w*)\s*[:=]", source))
    names.add(path.stem)          # 默认导出常与文件同名(页面组件几乎都是)
    return names


def _jsx_usages(component: str) -> list[Path]:
    """组件被**别的文件**以 ``<Name`` 用到的地方。"""
    pattern = re.compile(r"<" + re.escape(component) + r"[\s/>]")
    return [p for p in _TSX if p.stem != component and pattern.search(_src(p))]


def catch_all_navigate_prefixes(app_src: str | None = None) -> set[str]:
    """``<Route path="X/*" element={<Navigate…>}`` 的前缀集合。"""
    source = app_src if app_src is not None else _src(_APP)
    out: set[str] = set()
    for match in re.finditer(r'<Route\s+path="([^"]+)"\s+element=\{\s*<Navigate\b', source):
        raw = match.group(1)
        if raw.endswith("*"):
            out.add("/" + raw.rstrip("*").strip("/"))
    return out


def _static_route_paths(app_src: str | None = None) -> set[str]:
    """带真实 element(非 Navigate)的路由路径。"""
    source = app_src if app_src is not None else _src(_APP)
    out: set[str] = set()
    for match in re.finditer(r'<Route\s+path="([^"]+)"\s+element=\{\s*(?!<Navigate\b)', source):
        out.add("/" + match.group(1).strip("/"))
    return out


def url_fate(url: str, app_src: str | None = None) -> dict:
    """一个 URL 最终落到哪。

    **信息性**判据,不参与"组件为什么不渲染"的定罪 —— 见本节顶部订正说明。
    只有在没有更具体的静态路由能匹配时,splat 重定向才真的吃到这个 URL。
    """
    url = "/" + url.strip("/")
    for path in _static_route_paths(app_src):
        if path == url:
            return {"swallowed": False, "why": "命中静态路由 {0}".format(path)}
    for prefix in catch_all_navigate_prefixes(app_src):
        if prefix != "/" and (url == prefix or url.startswith(prefix + "/")):
            return {"swallowed": True,
                    "why": "无更具体路由,落到 {0}/* 的 Navigate 重定向".format(prefix)}
    return {"swallowed": False, "why": "既无静态路由也无 catch-all 覆盖(404)"}


def mount_evidence(component: str, _seen: frozenset = frozenset()) -> dict:
    """递归出具挂载证据:能不能从 ``App.tsx`` 的 ``<Route>`` 一路渲染到它。

    刻意返回 ``mode`` + ``chain`` 而不是布尔:报红时要能说清是"没人用"还是
    "用它的人自己也是死的",两种死法处置不同。链一并给出,免得只知道结论
    不知道断在哪一节。
    """
    if component in _seen:                       # 环:A 用 B、B 用 A
        return {"mounted": False, "mode": DEAD_CHAIN,
                "why": "循环引用,未接到路由", "chain": [component], "usages": []}
    seen = _seen | {component}

    app_src = _src(_APP)
    if re.search(r"<" + re.escape(component) + r"[\s/>]", app_src):
        return {"mounted": True, "mode": LIVE, "why": "App.tsx 直接渲染",
                "chain": [component, "App.tsx"], "usages": ["frontend/src/App.tsx"]}

    usages = _jsx_usages(component)
    if not usages:
        return {"mounted": False, "mode": NO_USAGE,
                "why": "全仓无 <{0} 挂载点".format(component),
                "chain": [component], "usages": []}

    dead_chains: list[list[str]] = []
    for path in usages:
        for parent in sorted(_declared_in(path)):
            if parent == component:
                continue
            up = mount_evidence(parent, seen)
            if up["mounted"]:
                return {"mounted": True, "mode": LIVE,
                        "why": "经 <{0}> 接到路由".format(parent),
                        "chain": [component] + up["chain"],
                        "usages": [str(p).replace("\\", "/") for p in usages]}
            dead_chains.append([component] + up["chain"])

    longest = max(dead_chains, key=len) if dead_chains else [component]
    return {"mounted": False, "mode": DEAD_CHAIN,
            "why": "使用链 {0} 走不到任何 <Route>".format(" ← ".join(longest)),
            "chain": longest,
            "usages": [str(p).replace("\\", "/") for p in usages]}


#: Layout 里**真的会渲染**的三个浮层(仅用作 mount 正向对照)。
_LAYOUT_MOUNTED = ("XiaobangDrawer", "NotificationBanner", "OnboardingChecklist")

#: 会与小榜 FAB **同屏**争底部的真实 CTA —— 只有这些能进几何分母。
#:
#: 🔴 R3-P3:`OnboardingChecklist` 被**移出**分母。它确实会渲染(mount 正向对照
#:    里仍是 LIVE),但 `Layout.tsx` 用 `!sandboxUI` 关掉小榜 FAB / 抽屉、
#:    用 `sandboxUI` 打开圆球 —— **永不同屏**。
#:    这是"锚不对"的第三种形态,比前两次死锚更隐蔽:组件活着,但和被测对象
#:    互斥,几何再准也验不到东西。互斥由 :func:`test_checklist_is_mutually_exclusive_with_the_fab`
#:    从源码推出来,不靠我在文档里声明。
_LAYOUT_COEXISTING = ("XiaobangDrawer", "NotificationBanner")

#: 🔴 死锚回归负样本。**每条都写死期望的死法**,不许用 "A 或 B" 的松断言 ——
#:    R3-P2 我就是拿 `("Navigate" in why) or ("无" in why)` 蒙过去的,
#:    结果 Navigate 那条分支其实从没 fire 过,判据里躺着一段死代码没人发现。
_DEAD_ANCHORS = (
    # (组件, 期望死法, 链上必须出现的一环)
    ("MobileTabBar", NO_USAGE, None),
    # [开源 E3 · 前端 · 2026-10-01 · WO_322] 另外四条(M3 销售客户页 NO_USAGE · M3 副驾两件套 DEAD_CHAIN · M3 布局壳 NO_USAGE)
    #   随 pages/M3 · components/m3 整删;DEAD_CHAIN 的牙证由下面的合成夹具接替。
)


@pytest.mark.parametrize("component,expected_mode,must_appear", _DEAD_ANCHORS)
def test_each_dead_anchor_is_hit_by_its_exact_death_mode(component, expected_mode, must_appear):
    """🔴 防复发锁:每个死锚**精确**命中它那一种死法,不接受"两种之一"。"""
    evidence = mount_evidence(component)
    assert evidence["mounted"] is False, evidence
    assert evidence["mode"] == expected_mode, (component, evidence)
    if must_appear:
        assert must_appear in evidence["chain"], (component, evidence)


def test_the_recursion_is_what_catches_the_chain_deaths():
    """反向对照:证明 DEAD_CHAIN 是**递归**抓到的,不是碰巧没人用。

    没有这条,链式死亡看起来跟 NO_USAGE 没区别。

    [开源 E3 · 前端 · 2026-10-01 · WO_322] 真实的链式死锚(M3 副驾两件套)随 components/m3 整删,现树里再没有真实实例;
    改用合成夹具:往扫描集里临时放两个假组件 —— 叶子被中间件用、中间件全仓没人用 ——
    叶子必须判 DEAD_CHAIN 且链上有中间件;再把中间件挂进 App.tsx 的源码副本,叶子必须翻成 LIVE。
    后一半证明夹具走的是真递归(不是恒返回 DEAD_CHAIN)。
    """
    global _TSX
    leaf = _FRONTEND / "__e3f_fixture__/ZzFixtureLeaf.tsx"
    mid = _FRONTEND / "__e3f_fixture__/ZzFixtureMid.tsx"
    saved_tsx, saved_app = _TSX, _SRC_CACHE.get(_APP)
    try:
        _SRC_CACHE[leaf] = "export function ZzFixtureLeaf() { return null }\n"
        _SRC_CACHE[mid] = "export function ZzFixtureMid() { return <ZzFixtureLeaf /> }\n"
        _TSX = saved_tsx + (leaf, mid)
        assert _jsx_usages("ZzFixtureLeaf") == [mid], "夹具叶子本来就有使用者,否则测的不是链"
        dead = mount_evidence("ZzFixtureLeaf")
        assert dead["mode"] == DEAD_CHAIN, dead
        assert "ZzFixtureMid" in dead["chain"], dead
        _SRC_CACHE[_APP] = _src(_APP) + "\n<ZzFixtureMid />\n"
        live = mount_evidence("ZzFixtureLeaf")
        assert live["mode"] == LIVE and "App.tsx" in live["chain"], live
    finally:
        _TSX = saved_tsx
        _SRC_CACHE.pop(leaf, None)
        _SRC_CACHE.pop(mid, None)
        if saved_app is None:
            _SRC_CACHE.pop(_APP, None)
        else:
            _SRC_CACHE[_APP] = saved_app


def test_mount_evidence_accepts_something_that_really_renders():
    """正向对照 —— 否则上面几条只证明它恒返回 False。"""
    for component in _LAYOUT_MOUNTED:
        evidence = mount_evidence(component)
        assert evidence["mounted"] is True, (component, evidence)
        assert evidence["mode"] == LIVE, (component, evidence)
        assert "App.tsx" in evidence["chain"], (component, evidence)


def test_layout_coexisting_ctas_are_enumerated_and_all_mounted():
    """枚举 Layout 内真实共存 CTA(Codex R2 会签要求)。分母非空且逐条有证据。"""
    assert len(_LAYOUT_COEXISTING) >= 2
    layout = _src(_LAYOUT)
    for component in _LAYOUT_COEXISTING:
        assert "<" + component in layout, "{0} 不在 Layout 里".format(component)
        assert mount_evidence(component)["mounted"], component


def test_url_fate_is_informational_and_has_both_directions():
    """catch-all 只回答 URL 去向 —— 正反两向都要能分开(否则它恒真/恒假)。"""
    swallowed = url_fate("/m3/sales/clients")
    assert swallowed["swallowed"] is True, swallowed
    assert "/m3" in swallowed["why"], swallowed
    # 反向:有静态路由的 URL 不许被判成被吞。
    alive = url_fate("/login")
    assert alive["swallowed"] is False, alive
    # [开源 E3 · 前端 · 2026-10-01 · WO_322] 原订正锁「URL 被吞 ≠ 组件死于被吞」的组件那一半随 M3 销售客户页整删;URL 那一半上面照查。


# ── FloatingStack 常量:不许有"没人消费"的锚静默进分母 ────────────────────
def _consts() -> dict[str, int]:
    source = _FLOATING_STACK.read_text(encoding="utf-8")
    block = re.search(r"export const FLOATING_FAB = \{(.*?)\} as const", source, re.S)
    assert block, "FLOATING_FAB 块没找到 —— 判据的输入没了"
    out = {k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+)", block.group(1))}
    for name in ("MOBILE_BOTTOM_BAR_HEIGHT", "LG_BREAKPOINT"):
        extra = re.search(r"export const " + name + r" = (\d+)", source)
        if extra:
            out[name] = int(extra.group(1))
    return out


def _consumers(constant: str) -> list[str]:
    out: list[str] = []
    for path in list(_FRONTEND.rglob("*.tsx")) + list(_FRONTEND.rglob("*.ts")):
        if path.name == "floating-stack.ts":
            continue
        if constant in path.read_text(encoding="utf-8"):
            out.append(str(path))
    return out


def test_the_m3_dead_anchor_is_gone_from_the_constants():
    """🔴 死锚必须从常量表里**删掉**,不是留着加注释 —— 留着,下一个人照样算它。"""
    source = _FLOATING_STACK.read_text(encoding="utf-8")
    block = re.search(r"export const FLOATING_FAB = \{(.*?)\} as const", source, re.S)
    keys = set(re.findall(r"(\w+):\s*\d+", block.group(1)))
    assert not {k for k in keys if k.startswith("M3_ADD_CLIENT")}, keys


def test_unconsumed_constants_are_marked_and_excluded():
    """零消费方的常量(TUTORIAL_*)不许静默当共存分母:必须被标注,且不进几何判据。"""
    unconsumed = [c for c in ("TUTORIAL_BOTTOM", "TUTORIAL_RIGHT") if not _consumers(c)]
    source = _FLOATING_STACK.read_text(encoding="utf-8")
    for name in unconsumed:
        assert "零消费方" in source, "{0} 无人消费却没被标注".format(name)
    assert _consumers("XIAOBANG_DEFAULT_BOTTOM"), "小榜常量也没人用?判据输入不对"


# ── 几何 ──────────────────────────────────────────────────────────────────
_REAL_BOTTOM_BARS = (
    "frontend/src/pages/Selection/components/BottomActionBar.tsx",
    "frontend/src/pages/Agent/QuotePreview.tsx",
    "frontend/src/pages/Intake/IntakeFillPage.tsx",
    "frontend/src/pages/MaterialConfirm/MaterialConfirmPage.tsx",
)


def test_bottom_bar_sources_really_render_and_are_mounted():
    """常量依据必须落在真渲染的东西上,且**逐个过挂载性前置**。"""
    for relpath in _REAL_BOTTOM_BARS:
        path = Path(relpath)
        assert path.exists(), relpath
        assert "fixed bottom-0" in path.read_text(encoding="utf-8"), relpath
        evidence = mount_evidence(path.stem)
        assert evidence["mounted"], (relpath, evidence)


def test_xiaobang_fab_clears_page_level_bottom_bars():
    c = _consts()
    bar = c["MOBILE_BOTTOM_BAR_HEIGHT"]
    assert c["XIAOBANG_DEFAULT_BOTTOM"] >= bar + 8, (
        "小榜 FAB bottom={0} 没让开底部操作条(保守高度 {1})".format(
            c["XIAOBANG_DEFAULT_BOTTOM"], bar)
    )


def test_the_geometry_criterion_is_alive():
    """反向对照:修复前的 bottom=20 必须被判成压住 64px 底部条。

    20 不是随手挑的 —— 它是 R2 修复前 `Layout.tsx` 写死的 `bottom-5`。
    """
    c = _consts()
    bar = c["MOBILE_BOTTOM_BAR_HEIGHT"]
    assert 20 < bar, "旧值本就该被判压住,判据失效"
    assert c["XIAOBANG_DEFAULT_BOTTOM"] >= bar + 8


def test_drag_floor_clamps_above_the_bottom_bar():
    """默认位置让开了、**手一拖又能盖回去**就等于没让开。"""
    src = _AGENT_FAB.read_text(encoding="utf-8")
    assert "MOBILE_BOTTOM_BAR_HEIGHT" in src, "拖拽钳制没有引用底栏高度常量"
    assert "LG_BREAKPOINT" in src, "没有按断点区分「有没有底部条」"
    floor = re.search(r"const floor = .*MOBILE_BOTTOM_BAR_HEIGHT \+ (\d+)", src)
    assert floor, "找不到拖拽下限的钳制表达式"
    assert int(floor.group(1)) >= 8
    newy = re.search(r"const newY = .*", src)
    assert newy and "Math.max(floor" in newy.group(0), newy.group(0) if newy else ""


# ── 键盘与读屏 ────────────────────────────────────────────────────────────
def test_agent_fab_is_keyboard_operable():
    """🔴 最要紧的一条:回车/空格必须能打开小榜(修复前是死的)。"""
    src = _AGENT_FAB.read_text(encoding="utf-8")
    assert "onKeyDown" in src, "FAB 没有键盘处理 —— 键盘用户打不开小榜"
    assert "'Enter'" in src and ("' '" in src or "'Spacebar'" in src)
    assert "preventDefault()" in src, "空格不 preventDefault 会滚页"


def test_agent_fab_has_an_accessible_name_and_visible_focus():
    src = _AGENT_FAB.read_text(encoding="utf-8")
    assert 'type="button"' in src, "缺 type=button:落在 form 里会变成提交"
    assert "aria-label=" in src, "图标按钮没有无障碍名,读屏只报「按钮」"
    assert 'aria-haspopup="dialog"' in src
    assert "focus-visible:ring" in src, "焦点不可见"


def test_layout_fab_uses_the_floating_stack_ssot_not_magic_numbers():
    src = _LAYOUT.read_text(encoding="utf-8")
    assert "FLOATING_FAB.XIAOBANG_DEFAULT_BOTTOM" in src
    assert "FLOATING_Z.FAB" in src
    fab_block = src[src.index('aria-label="打开小榜 GEO 助手"'):][:1200]
    for magic in ("bottom-5", "right-5", "z-50", "sm:bottom-6"):
        assert magic not in fab_block, "FAB 仍在用魔法数字 {0!r}".format(magic)


def test_both_entry_points_sit_at_the_same_place():
    layout = _LAYOUT.read_text(encoding="utf-8")
    fab = _AGENT_FAB.read_text(encoding="utf-8")
    for token in ("XIAOBANG_DEFAULT_BOTTOM", "XIAOBANG_DEFAULT_RIGHT"):
        assert token in layout, token
        assert token in fab, token


def test_both_entry_points_honor_the_safe_area():
    for path in (_LAYOUT, _AGENT_FAB):
        assert "env(safe-area-inset-bottom" in path.read_text(encoding="utf-8"), path


def test_dead_components_are_not_used_as_live_justification():
    """🔴 零挂载组件不许再作为任何判据/注释的**活依据**。"""
    for component in ("MobileTabBar",):  # 另一个死锚(M3 销售客户页)随开源 E3 整删
        assert mount_evidence(component)["mounted"] is False, component
    for path in (_FLOATING_STACK, _AGENT_FAB, _LAYOUT):
        src = path.read_text(encoding="utf-8")
        for component in ("MobileTabBar",):
            if component not in src:
                continue
            assert any(k in src for k in ("零挂载", "零引用", "零路由", "是错的", "死锚")), (
                "{0} 仍把 {1} 当活依据".format(path, component)
            )


@pytest.mark.parametrize("path", [_FLOATING_STACK, _AGENT_FAB, _LAYOUT, _APP])
def test_sources_exist(path: Path):
    assert path.exists(), path
    assert path.stat().st_size > 0, path


# [开源 E3 · 前端 · 2026-10-01 · WO_322] census 勘误锁(R3-P3 ③)退役:它守的 M3 副驾两件套随 components/m3 整删;
#   开源导出里删这一格的那条改写规则同笔撤掉。


def _guard_of(anchor: str) -> str:
    """取 Layout.tsx 里包住某个挂载点的那段 JSX 条件(`{cond && (`)。"""
    layout = _src(_LAYOUT)
    index = layout.index(anchor)
    head = layout[:index]
    # 往回找最近一个 `{<条件> && (`
    guards = re.findall(r"\{([^{}\n]{0,160}?)\s*&&\s*\(", head)
    assert guards, anchor
    return guards[-1]


def test_checklist_is_mutually_exclusive_with_the_fab():
    """🔴 圆球为什么不进共存分母 —— 从**源码条件**推,不是我说了算。

    第三种"锚不对":组件活着(mount 正向对照里是 LIVE),但和被测对象永不同屏。
    前两次是锚不存在,这次是锚存在但不共时 —— 几何一样验不到东西。
    """
    fab_guard = _guard_of('aria-label="打开小榜 GEO 助手"')
    ball_guard = _guard_of("<OnboardingChecklist")
    assert "!sandboxUI" in fab_guard, fab_guard
    assert "sandboxUI" in ball_guard and "!sandboxUI" not in ball_guard, ball_guard
    assert "OnboardingChecklist" not in _LAYOUT_COEXISTING
    # 反向:真正同屏的那个不许被同一条理由误伤。
    assert "NotificationBanner" in _LAYOUT_COEXISTING
    banner_at = _src(_LAYOUT).index("<NotificationBanner />")
    line = _src(_LAYOUT)[:banner_at].count(chr(10)) + 1
    assert line > 0


# ── 主门入口锁(R3-P4 ①)──────────────────────────────────────────────────
_PACKAGE_JSON = Path("frontend/package.json")
_PW_CONFIG_NAME = "playwright.xiaobang-unified.config.ts"


def _xiaobang_pw_scripts() -> dict[str, str]:
    """所有跑 xiaobang-unified 那套 Playwright 的 npm 脚本(按**规则**取,不写死名字)。

    写死名字的锁只能挡住我今天想到的那个入口;新加一条门脚本它就漏了。
    """
    data = json.loads(_PACKAGE_JSON.read_text(encoding="utf-8"))
    return {name: cmd for name, cmd in data.get("scripts", {}).items()
            if _PW_CONFIG_NAME in cmd and not name.startswith("//")}


def test_the_main_gate_script_runs_the_whole_matrix():
    """🔴 主门脚本里出现 ``--project=`` 即红 —— **锁的是入口,不是承诺**。

    R3-P2 我按 `test:xiaobang-fab` 的写法跑,它自带 `--project=agent-mobile`,
    于是把单 project 的 11 条当「全 project」上报;`/agent/preview` 对
    normal / member 必红的事实因此整整一轮没被发现。
    口头约定"下次记得跑全矩阵"挡不住这个 —— 挡得住的是这条锁。
    """
    scripts = _xiaobang_pw_scripts()
    assert scripts, "一条 xiaobang Playwright 脚本都没有?锁的输入没了"
    gates = {n: c for n, c in scripts.items() if not n.endswith(":smoke")}
    assert gates, scripts
    for name, cmd in gates.items():
        assert "--project" not in cmd, (
            "主门脚本 {0} 带了 project 过滤,跑出来的不是全矩阵:{1}".format(name, cmd)
        )


def test_the_smoke_escape_hatch_exists_and_is_the_only_filtered_one():
    """反向对照:过滤版必须存在**且确实带过滤**。

    否则上一条有个廉价的绿法 —— 把过滤版一并删掉,规则就恒真了。
    """
    scripts = _xiaobang_pw_scripts()
    smoke = {n: c for n, c in scripts.items() if n.endswith(":smoke")}
    assert smoke, "没有 :smoke 快速入口 —— 主门锁会逼人把过滤偷偷写回主门"
    for name, cmd in smoke.items():
        assert "--project=" in cmd, (name, cmd)


# ── 分母锁(R3-P5 ③)────────────────────────────────────────────────────────
#
# 🔴 **双分母,不许用一个数代替另一个**:
#      · npm 主门 `test:xiaobang-fab`(只跑 fab-collision 一个文件)= 12 project × 12 条 = **144**
#      · 整份配置(fab-collision + xiaobang-unified 两个文件)        = 144 + 12       = **156**
#    R3-P2 的教训就是拿一个数冒充另一个(那次是拿单 project 的 11 当"全 project"),
#    所以这里把两个数分别写死,并且**都靠 `--list` 真数**,不靠我心算。
_PW_CONFIG = Path("frontend/playwright.xiaobang-unified.config.ts")

_PROJECT_IDENTITIES = ("admin", "agent", "normal", "member")
_PROJECT_SIZES = ("desktop", "fourk", "mobile")
_EXPECTED_PROJECTS = frozenset(
    "{0}-{1}".format(i, s) for i in _PROJECT_IDENTITIES for s in _PROJECT_SIZES
)
_MAIN_GATE_TOTAL = 144      # npm 主门:fab-collision 一个文件
_WHOLE_CONFIG_TOTAL = 156   # 整份配置:两个文件


def test_project_matrix_is_the_exact_expected_set():
    """project 名**精确集合**(4 身份 × 3 尺寸 = 12),多一个少一个都红。

    只断言"数量 == 12"挡不住"换了一档尺寸但总数没变"这种改动。
    """
    config = _PW_CONFIG.read_text(encoding="utf-8")
    identities = set(re.findall(r"'(\w+)'", re.search(
        r"const identities = \[(.*?)\]", config, re.S).group(1)))
    sizes = set(re.findall(r"^\s*(\w+):\s*\{ width", re.search(
        r"const sizes = \{(.*?)\n\} as const", config, re.S).group(1), re.M))
    assert identities == set(_PROJECT_IDENTITIES), identities
    assert sizes == set(_PROJECT_SIZES), sizes
    assert {"{0}-{1}".format(i, s) for i in identities for s in sizes} == _EXPECTED_PROJECTS
    assert len(_EXPECTED_PROJECTS) == 12


def _playwright_list(extra: list[str]) -> tuple[int, set[str], str]:
    """跑 `playwright test --list` 真数用例(`--list` 不起 webServer)。"""
    result = subprocess.run(
        ["npx", "playwright", "test", "--config=playwright.xiaobang-unified.config.ts",
         *extra, "--list"],
        cwd="frontend", capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300, shell=(os.name == "nt"),
    )
    out = (result.stdout or "") + (result.stderr or "")
    if "[基础设施" in out:
        raise AssertionError("这是**基础设施红**,不是产品红,别记到分母头上:\n" + out[-800:])
    assert result.returncode == 0, out[-1200:]
    total = re.search(r"Total:\s*(\d+)\s*tests?\s*in\s*(\d+)\s*files?", out)
    assert total, out[-800:]
    projects = set(re.findall(r"^\s*\[([\w-]+)\]\s+›", out, re.M))
    return int(total.group(1)), projects, out


@pytest.mark.slow
def test_main_gate_collects_exactly_the_fab_matrix():
    """主门 `--list` 真数:必须恰好 144,且 12 个 project 一个不缺。"""
    total, projects, out = _playwright_list(["fab-collision"])
    assert total == _MAIN_GATE_TOTAL, "主门收集数 {0} ≠ {1}\n{2}".format(
        total, _MAIN_GATE_TOTAL, out[-600:])
    assert projects == _EXPECTED_PROJECTS, projects
    assert "in 1 file" in out, out[-300:]


@pytest.mark.slow
def test_whole_config_collects_both_files():
    """整份配置 = 156(= 144 + 12)。两个分母分开验,不许互相冒充。"""
    total, projects, out = _playwright_list([])
    assert total == _WHOLE_CONFIG_TOTAL, "整份配置收集数 {0} ≠ {1}\n{2}".format(
        total, _WHOLE_CONFIG_TOTAL, out[-600:])
    assert projects == _EXPECTED_PROJECTS, projects
    assert total - _MAIN_GATE_TOTAL == len(_EXPECTED_PROJECTS), (
        "两个分母的差必须正好是另一个 spec 的 1 条 × 12 project")


# ── 基础设施红与产品红分离(R3-P4 建立 · R3-P5 重做身份判据)──────────────
_SERVER_GUARD = Path("frontend/tests/xiaobang-unified/server-guard.ts")

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)


def _strip_ts_comments(source: str) -> str:
    """去掉注释,只留代码。判"码里还有没有某个写法"必须扫代码,不扫说明文字。"""
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))


def test_webserver_keeps_stderr_and_starts_without_a_shell():
    """webServer 侧:少一层壳 + 保留 stderr + 端口被占当场退出。"""
    config = _PW_CONFIG.read_text(encoding="utf-8")
    assert "stderr: 'pipe'" in config
    command_line = next(line for line in config.splitlines()
                        if line.strip().startswith("command:"))
    assert "npm run" not in command_line and "npx " not in command_line, command_line
    assert "node node_modules/vite/bin/vite.js" in command_line, command_line
    assert "--strictPort" in command_line, command_line
    # 端口空闲检查必须在 config 求值期(早于 webServer 启动);
    # globalSetup 现在负责的是**采集身份**,不是查空闲(两次踩坑的产物)。
    assert "assertPortFreeBeforeServerStarts()" in config
    assert "globalSetup:" in config and "globalTeardown:" in config


def test_teardown_kills_by_identity_not_by_timing():
    """🔴 R3-P5 ①:时序不是身份。

    R3-P4 用"开跑后才出现在端口上"当身份,并发下会**杀掉别人的进程** ——
    别人的服务恰好在我们开跑后启动并抢到端口,就被我们当成自己的残留清掉了。
    现在只认 `pid + 创建时间 + 命令行 hash` 三元组。
    """
    guard = _SERVER_GUARD.read_text(encoding="utf-8")
    assert "[基础设施" in guard, "报错不自报家门,就会混进产品红"
    # 身份三元组齐全(缺创建时间 → pid 复用会误杀)
    for field in ("pid", "createdAt", "cmdlineHash"):
        assert field in guard, field
    assert "sameIdentity" in guard
    assert "a.pid === b.pid && a.createdAt === b.createdAt && a.cmdlineHash === b.cmdlineHash" in guard
    # 作废的时序判据不许还在
    assert "preexisting" not in _strip_ts_comments(guard), "时序判据(开跑前/后)已作废,不许残留在码里"
    # 身份不匹配 → 禁杀,只报红
    assert "禁杀" in guard
    assert "foreign.push" in guard and "killTree" in guard
    foreign_branch = guard[guard.index("for (const pid of probe.pids)"):guard.index("const after =")]
    # 不匹配那一支必须 continue 掉,不能落到 killTree
    assert foreign_branch.count("continue") >= 2, foreign_branch


def test_probe_is_fail_closed_not_fail_silent():
    """🔴 R3-P5 ②:探测器失效 ≠ 端口没人。

    R3-P4 的 `listenersOn` 出任何错都 `return []`,两件事在返回值上同形 ——
    本仓为这种假绿反复付过费。现在返回 `{ok:false, reason}`,调用方一律报红。
    """
    guard = _SERVER_GUARD.read_text(encoding="utf-8")
    code = _strip_ts_comments(guard)
    assert "ProbeResult" in guard
    assert "{ ok: false; reason: string }" in guard
    # 🔴 扫**探测器那一段的代码**:
    #    · 只扫代码 —— 注释里会原样引用旧写法(讲它为什么被废),连注释一起禁,
    #      下一个人就把说明删掉,门自己变哑;
    #    · 只扫探测段 —— `readState()` 的 `return []` 是另一回事(读不到状态就
    #      认作"本轮没登记任何自己的进程"),那条路径反而是 fail-closed:
    #      没登记 → 端口上的东西全被判成身份不匹配 → 禁杀 + 报红。
    probe_section = code[code.index("export function listenersOn"):code.index("function hashCmdline")]
    assert "return []" not in probe_section, probe_section
    # findstr 无匹配也退 1,和 netstat 自己失败同形 —— 不许再用这种管道(扫代码)
    assert "| findstr" not in code
    # 三个调用点都要处理 ok:false
    assert guard.count("if (!probe.ok)") >= 2
    assert "if (!after.ok)" in guard
    # 杀失败 / 杀完还占着 → 非零退出
    assert "failedKills" in guard and "stillOurs" in guard
    assert "throw new InfraError(problems.join" in guard
    # 反向对照的开关必须在(下面 §两向实测靠它)
    assert "XBVNEXT_FORCE_PROBE_FAILURE" in guard


def test_the_drawer_close_race_is_waited_on_not_slept_on():
    """收起动画的时序红用**等 DOM 事实**解决,不许用 sleep 糊过去。"""
    spec = Path("frontend/tests/xiaobang-unified/fab-collision.spec.ts").read_text(encoding="utf-8")
    assert "waitForDrawerOverlayGone" in spec
    assert "div.fixed.inset-0.z-50" in spec, "遮罩选择器变了?那这条等待就是空的"
    assert "waitForTimeout" not in spec, "出现了 sleep —— 时序问题要等确定事实,不是等运气"
    # 遮罩选择器必须真的对得上组件源码,否则等的是一个不存在的东西(又一个死锚)
    sheet = Path("frontend/src/components/ui/sheet.tsx").read_text(encoding="utf-8")
    assert "fixed inset-0 z-50" in sheet, "sheet.tsx 的遮罩类名变了,等待选择器已失效"


def _run_guard_selfcheck(mode: str) -> str:
    result = subprocess.run(
        ["node", "--experimental-strip-types",
         "tests/xiaobang-unified/guard-selfcheck.mts", mode],
        cwd="frontend", capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300,
    )
    out = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, "反向对照 {0} 没通过:\n{1}".format(mode, out[-1500:])
    assert "反向对照通过" in out, out[-800:]
    return out


@pytest.mark.slow
def test_reverse_control_foreign_process_is_reported_not_killed():
    """🔴 R3-P5 ① 的反向对照:端口上放一个**身份不匹配**的进程。

    伪造的"本轮身份"故意用**同一个 pid、不同的创建时间与命令行 hash** ——
    这正是 pid 复用的形态。只认 pid 的实现会在这里把别人的进程杀掉。
    要求:teardown 报红、说明"禁杀",且那个进程**仍然活着**。
    """
    out = _run_guard_selfcheck("foreign-no-kill")
    assert "禁杀" in out, out[-600:]
    assert "没被误杀" in out, out[-600:]


@pytest.mark.slow
def test_reverse_control_probe_failure_is_red_not_green():
    """🔴 R3-P5 ② 的反向对照:探测器失效必须红,两个入口都要红。

    同时验正向 —— 探测器正常且端口空闲时**不**红,否则上面的红只说明它恒红。
    """
    out = _run_guard_selfcheck("probe-failclosed")
    assert "前置检查 报红" in out and "teardown 报红" in out, out[-800:]
    assert "不红(证明上面的红来自失效,不是恒红)" in out, out[-800:]
