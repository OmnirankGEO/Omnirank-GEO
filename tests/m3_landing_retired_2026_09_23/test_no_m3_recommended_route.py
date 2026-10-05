# -*- coding: utf-8 -*-
"""WO_281 · 登录后的推荐路由不许再指向已废弃的 M3(`/m3/...`)—— m3_enabled 与非 m3_enabled 的用户都一样。

病灶:`api/c_end_api.py::get_user_mode`(GET /api/c-end/settings/mode)对 m3_enabled
(全体管理员 + 灰度白名单服务商)回 `recommended_route = "/m3/sales/today"`,而且不论 preferred_mode 是什么
(agent / c / 没选)。前端 `determineTargetRoute` 原样跟随这个字段;App.tsx 目前把 /m3 与 /m3/* 一律
Navigate 到 "/",所以浏览器最终也落 "/",但要多绕一跳,等 M3 路由按 E3 删除名单删掉,就是死路。

判据(不连库、不起服务;被调用的端点函数本体是真的):
  · `_load_user_mode_sync`(读钱包等级 + 偏好)用 monkeypatch 喂定值 —— 被锁的是**路由决策**,不是那次查询;
  · 管理员身份取自 request.state.user(生产中间件写的就是这个字段);灰度名单取自 env M3_USER_WHITELIST
    (生产读的就是这个变量);
  · 用户矩阵:管理员(L0 / L1)、白名单服务商、白名单里的普通账号(不该开 M3)、普通服务商、普通账号,
    × preferred_mode {没选, agent, c};
  · 断言:recommended_route 不以 /m3 开头,且就是 "/";该开 M3 的那几臂,回包里 m3_enabled 必须真是 True
    —— 样本必须落在原来那条分流上,否则这条锁在改前代码上也是绿的。
反臂:把改前那段分流原样放回(对本文件源码做文本替换,在临时模块里执行)⇒ 该开 M3 的 3 类用户 × 3 种偏好
    = 9 臂拿到 /m3/sales/today ⇒ 锁必红;其余 9 臂仍是 "/"。
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "api" / "c_end_api.py"

WL_PROVIDER, WL_L0, PLAIN_PROVIDER, PLAIN_L0, ADMIN_L0, ADMIN_L1 = 992811, 992812, 992813, 992814, 992815, 992816
WHITELIST = f"{WL_PROVIDER},{WL_L0}"

# (名字, user_id, is_admin, agent_level, 按原规则该不该 m3_enabled)
USERS = [
    ("管理员·L0", ADMIN_L0, True, 0, True),
    ("管理员·L1", ADMIN_L1, True, 1, True),
    ("白名单服务商", WL_PROVIDER, False, 1, True),
    ("白名单里的普通账号", WL_L0, False, 0, False),
    ("普通服务商", PLAIN_PROVIDER, False, 1, False),
    ("普通账号", PLAIN_L0, False, 0, False),
]
MODES = [None, "agent", "c"]

# 改前的分流(WO_281 撤掉的那一段,逐字);FIXED 是改后那一行。反臂靠它们做文本替换。
OLD_BRANCH = (
    '    if preferred_mode == "agent":\n'
    '        recommended = "/m3/sales/today" if m3_enabled else "/"\n'
    '    elif m3_enabled:\n'
    '        recommended = "/m3/sales/today"\n'
    '    else:\n'
    '        recommended = "/"\n'
)
FIXED = '    recommended = "/"\n'


def _real_module():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import api.c_end_api as mod
    assert pathlib.Path(mod.__file__).resolve() == SRC.resolve(), f"import 到的不是本工作树的文件:{mod.__file__}"
    return mod


def _module_from_source(source: str, name: str):
    """在临时模块里执行一份(改过的)源码;执行期挂进 sys.modules,完事即摘。"""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(SRC)
    sys.modules[name] = mod
    try:
        exec(compile(source, str(SRC), "exec"), mod.__dict__)
    finally:
        sys.modules.pop(name, None)
    return mod


def _routes(mod, monkeypatch) -> dict:
    """{(名字, 偏好): 回包} —— 端点函数是真的,只把「读钱包与偏好」那次查询换成定值。"""
    monkeypatch.setenv("M3_USER_WHITELIST", WHITELIST)
    out = {}
    for name, uid, is_admin, level, _ in USERS:
        for mode in MODES:
            monkeypatch.setattr(mod, "_load_user_mode_sync",
                                lambda _uid, _l=level, _m=mode: {"wallet": {"agent_level": _l}, "preferred_mode": _m})
            req = types.SimpleNamespace(state=types.SimpleNamespace(user={"user_id": uid, "is_admin": is_admin}))
            out[(name, mode)] = asyncio.run(mod.get_user_mode(req))
    return out


def _violations(routes: dict) -> list:
    bad = []
    for (name, mode), resp in routes.items():
        route = resp["recommended_route"]
        if route.startswith("/m3") or route != "/":
            bad.append(f"{name} · preferred_mode={mode!r} ⇒ recommended_route={route!r}")
    return bad


def test_no_user_is_sent_to_m3(monkeypatch):
    routes = _routes(_real_module(), monkeypatch)
    # 样本自证:该开 M3 的几臂回包里 m3_enabled 真是 True,其余真是 False(否则没踩在原来那条分流上)
    flags = {(name, mode): resp["m3_enabled"] for (name, mode), resp in routes.items()}
    want = {(name, mode): expect for name, _, _, _, expect in USERS for mode in MODES}
    assert flags == want, f"m3_enabled 与预期不符 —— 矩阵没落在原分流上:{flags}"
    assert _violations(routes) == []


def test_arm_restoring_the_old_branch_turns_red(monkeypatch):
    source = SRC.read_text(encoding="utf-8")
    assert source.count(FIXED) == 1, "反臂没下成:改后那一行在源码里不是恰好 1 处"
    old = _module_from_source(source.replace(FIXED, OLD_BRANCH, 1), "wo281_old_branch_arm")
    bad = _violations(_routes(old, monkeypatch))
    m3_users = [name for name, _, _, _, expect in USERS if expect]
    expected = sorted(f"{name} · preferred_mode={mode!r} ⇒ recommended_route='/m3/sales/today'"
                      for name in m3_users for mode in MODES)
    assert sorted(bad) == expected, f"改回旧分流后,该红的应恰好是 {len(expected)} 臂:{bad}"
    # 对照:同一套执行方式跑改后的源码 ⇒ 全绿(证明红是旧分流带来的,不是临时模块本身)
    same = _module_from_source(source, "wo281_fixed_control")
    assert _violations(_routes(same, monkeypatch)) == []
