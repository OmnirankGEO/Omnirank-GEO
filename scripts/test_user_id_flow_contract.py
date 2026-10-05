# -*- coding: utf-8 -*-
"""#101 · `request.state.user` 上不许读不存在的键。

## 缺陷是什么

`request.state.user` 是 **JWT payload**(或 perm_version 软刷新时的规范化 dict),
它的键是 `user_id / username / is_admin / roles / permissions / …` —— **没有 `id`**。
软刷新那条分支显式写 `"user_id": fresh_user["id"]`,把 DB 行的 `id` **改名**成了 `user_id`。

⇒ `user["id"]` 恒 KeyError(500);`user.get("id")` 恒 None(**静默**,更难发现)。

## 分母都是机械导出的,一处没手写

| 要回答 | 怎么来的 |
|---|---|
| 哪些键是合法的 | AST 取 `auth/jwt_utils.py` 与 `auth/middleware.py` 两处构造的 dict 字面量 + 后续 `payload["k"]=` 追加 |
| 哪些变量是 user | 从 `request.state.user` 出发**不动点推导**:所有 return 都追溯到 user 源的函数,自己也是 user 源 |
| 哪些键其实安全 | 源函数体内 `user["k"] = …` **注入**过的键(注入后读它没问题) |

C 试过四版按名字匹配的检测器:99 / 50 / 1 处,其中两版**漏掉已知真阳**
(手写 helper 名单静默漏项);第四版候选 225 处但假阳率未知。
本器一个 helper 名单都不维护。

## 🔴 我自己在这道门上量错了三次

| 版本 | 读数 | 错在哪 |
|---|---|---|
| v1 | 744 | `ID_KEYS` 手写成 `("id","user_id")` —— 而 `user_id` 是**合法**键 |
| v2 | 191 | 合法键集改成机械导出后,`user["id"] = …`(**写**)被当成读点;未判 `ctx` |
| v3 | **8** | 注入键做成**全局**集合 ⇒ 一个模块补了 `id` 就让全仓 `id` 都安全,**正样本反而抓不到** |
| v4 | 119 | 源函数与注入键改成**按文件**解析(它们几乎都是模块内私有 helper) |

v3 那次最险:读数从 191 掉到 8,看起来像「精度大幅提升」,
实际是**把要抓的东西一起滤掉了** —— 只有正样本臂把它戳穿。
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CENSUS = ROOT / "scripts" / "user_id_flow_census.py"

#: 🔴 已知未修的读点 —— **按文件计数**,不记行号(行号会漂)。
#:    某文件计数**增加**或**出现新文件** ⇒ 红。修好要同笔把计数改小/删条目。
#:    C 的 `3d3119fea`(#78)只覆盖 faq_api + research_monitor_industry_api 两个文件;
#:    本表是全量,等 Review 分派归属后按单修。
KNOWN_HITS: dict[str, int] = {
    # #78b 之后:114 处裸读 `id` 已全部收敛到 auth.user_ctx.current_user_id。
    # 剩下这 5 处读的是**别的**不存在键,各 1 处,未定性 —— 单独一卡处置。
    # [WO_273 · 2026-09-23] 原有插件后端 1 处('sub'),文件随插件后端整体删除 ⇒ 同笔删条目,
    #   TOTAL_CEILING 同笔 5 → 4(只降本单减掉的那 1 处;profile_api 那条基线就已是陈条目,不归本单)。
    "api/profile_api.py": 1,              # 'agent_level'
    "api/wallet_api.py": 1,               # 'nickname'
    "middleware/organization_guard.py": 1,  # 'portal'
}
#: 上表之外的文件按「总数上限」兜住 —— 逐个列 40 多个文件没有信息量,
#: 但总数不许涨:涨了说明有人新写了一处裸读。
# [E3a · 2026-09-28] 灵感模块随 E3 整删 ⇒ 同笔删条目,上限 4 → 3(只降本单减掉的那 1 处)
TOTAL_CEILING = 3


def _census():
    spec = importlib.util.spec_from_file_location("_uidflow101", CENSUS)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def r():
    m = _census()
    assert m.VALID_KEYS, (
        "合法键集为空 —— 这道门**没有分母**。先查 auth/jwt_utils.py 与 auth/middleware.py "
        "里那两处 dict 构造是否还在,别把「什么都没量到」读成「没有违规」。")
    return m.run(), m


# ══ ① 分母自证 ═════════════════════════════════════════════════════
def test_the_valid_key_set_is_derived_and_excludes_id(r):
    res, m = r
    assert "user_id" in m.VALID_KEYS, "合法键集里没有 user_id —— 导出坏了"
    assert "id" not in m.VALID_KEYS, (
        "合法键集里出现了 `id` —— 要么两处构造真加了这个键(那本门前提就变了,"
        "同笔重写 docstring 与登记表),要么导出把别的 dict 也吃进来了。")
    assert len(res["sources"]) >= 10, (
        f"只推导出 {len(res['sources'])} 个 user 源函数 —— 不动点没跑起来,"
        f"下面所有读数都会偏低。")


# ══ ② 正样本臂 = **登记表本身**,自维护 ═══════════════════════════════
def test_the_gate_detects_every_registered_hit(r):
    """🔴 这条是**整道门有没有牙**的判据。

    v3 那版把注入键做成全局集合,读数从 191 掉到 8 ——
    看起来像精度提升,其实把要抓的一起滤掉了。只有正样本臂戳穿了它。

    第一版把 `faq_api.py:403` 与 `research_monitor_industry_api.py:564` **写死**成正样本;
    #78b 把它们(连同另外 112 处)修好之后,这条臂当场红 ——
    「修复推翻了存在锁的前提」:锁没错,是它的前提被**正确的改动**作废了。
    今天这是第二次踩(#94 那把一模一样),所以改成自维护:
    门必须抓到登记表里的每一个文件,登记缩它跟着缩,不会假红。
    """
    res, _ = r
    seen = {h["f"] for h in res["hits"]}
    missed = sorted(set(KNOWN_HITS) - seen)
    assert not missed, (
        f"门没抓到这些**已登记确认**的文件:{missed} —— 先修门,别信它给出的任何绿。")
    assert KNOWN_HITS, (
        "🔴 登记表空了(裸读归零 = #78b 终态)。"
        " 此刻这道门**失去了正样本臂** —— 它还有没有牙,没有任何东西在证。"
        " 同笔改成**合成探针**:造一段读不存在键的源码喂给普查器,断言被抓到。")


def test_the_gate_does_not_flag_the_legitimate_key(r):
    """反臂:`user["user_id"]` 是**对的**,不许被判成缺陷。

    v1 正是把 user_id 一起算进去,得 744 —— 其中 550+ 是完全正确的代码。
    """
    res, _ = r
    bad = [h for h in res["hits"] if h["key"] == "user_id"]
    assert not bad, f"把合法键 user_id 判成了缺陷({len(bad)} 处):{bad[:3]}"


def test_injected_keys_are_not_flagged(r):
    """反臂:模块自己 `user["id"] = user.get("user_id") or …` 补过键之后,下游读它安全。

    实测 `api/admin_withdrawal_api.py` 的 `_get_admin` 就这么写。
    不认这一层会造出一整片假阳(v2 的 191 里有一批是它)。
    """
    res, _ = r
    inj = [h for h in res["hits"] if h["f"] == "api/admin_withdrawal_api.py"]
    assert not inj, (
        f"admin_withdrawal_api 里的读点被判成缺陷了({len(inj)} 处)—— "
        f"它的 `_get_admin` 明确注入了 id,注入识别坏了。")


# ══ ③ 主锁:不许新增 ═══════════════════════════════════════════════
def test_no_new_bare_id_read_is_introduced(r):
    res, _ = r
    by_file: dict[str, int] = {}
    for h in res["hits"]:
        by_file[h["f"]] = by_file.get(h["f"], 0) + 1
    # 🔴 对称的一侧:**有命中但不在登记里的文件**也要红。
    #    第一版只查「登记表里的文件」⇒ 把某文件从登记里删掉,它的命中就此无人管,
    #    而这件事不会让任何一条红(实测:登记 5→4,7 条判据全绿)。
    #    只有 TOTAL_CEILING 兜着,而它同样可以被一起改大。
    unregistered = sorted(set(by_file) - set(KNOWN_HITS))
    assert not unregistered, (
        f"这些文件有裸读命中,却**不在登记表里**:{unregistered}\n"
        f"    要么是新写的裸读(改用 `auth.user_ctx.current_user_id`),\n"
        f"    要么是有人把条目从登记里删了 —— 删掉不等于修好。")
    grown = {f: (KNOWN_HITS[f], n) for f, n in by_file.items()
             if f in KNOWN_HITS and n > KNOWN_HITS[f]}
    assert not grown, (
        f"这些文件里裸读不存在键的处数**变多了**(登记 → 实测):{grown}\n"
        f"    `request.state.user` 上没有 `id`:`user['id']` 恒 500,`user.get('id')` 恒 None。\n"
        f"    用 `user['user_id']`,或用 C 的 `_user_id(user)` helper。")
    assert len(res["hits"]) <= TOTAL_CEILING, (
        f"总处数 {len(res['hits'])} 超过登记上限 {TOTAL_CEILING} —— 有人新写了裸读。\n"
        f"    修好后请把 TOTAL_CEILING 同笔调低(它只许降不许升)。")


def test_the_registry_is_not_stale(r):
    """修好了要同笔调低计数 —— 登记表比实际大,说明它在说谎。"""
    res, _ = r
    by_file: dict[str, int] = {}
    for h in res["hits"]:
        by_file[h["f"]] = by_file.get(h["f"], 0) + 1
    stale = {f: (c, by_file.get(f, 0)) for f, c in KNOWN_HITS.items() if by_file.get(f, 0) < c}
    assert not stale, (
        f"这些文件的实测处数已经**少于**登记(登记 → 实测):{stale} —— 同笔把计数调低或删条目。")


# ══ ④ UNRESOLVED 单列 ═════════════════════════════════════════════
def test_unresolved_is_counted_not_folded(r):
    """名叫 user 但来源追不到的读点 —— 单列计数,不进 HIT 也不当无关。

    这个数就是这道门的覆盖率读数。
    """
    res, _ = r
    assert res["unresolved"], (
        "UNRESOLVED 为 0 —— 不可能:仓里有大量跨函数传 user 的写法。"
        "这说明它被折进别的档了,覆盖率读数就此消失。")
