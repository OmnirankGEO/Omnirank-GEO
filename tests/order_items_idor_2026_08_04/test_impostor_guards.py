"""[FIND-MHZ-ORDERITEMS-2026-08-04 · S2] 同名冒牌守卫锁。

`material_api` 与另两个社媒 router(运营 / 人设,已随 E3 删)三处各有一份本地
`_get_profile_safe`,docstring 自写「宽松版,不做 brand 权限校验」——
只判档案存不存在,任何登录用户拿任意 profile_id 都过。

同一份假货 2026 年早些时候已在 `geo_assets_api.py` 被删过一次(GEO-R8-CAN-003),
`tests/regression/test_fix_geo_assets_api.py` 至今留着「不许它回来」的断言 ——
**但那次只修了一个文件,没有横扫**。本文件把那条先例扩到全部三处,
并加一条按"体内有没有真鉴权原语"扫描的通用闸,防止下一个同名冒牌货。
"""
import ast
import os
import pathlib
import re

import pytest

from impostor_guard_scan import AUTHZ_PRIMITIVES, scan

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
# [开源 E3 · B1b-2a] 运营 / 人设两个 router 随 E3 删,只剩素材 router 这一处要守
_SWEPT = ("api/material_api.py",)
# 每个文件里原本的假守卫调用点数(改后必须一个不少地转成 canonical 调用)
_EXPECTED_CALLS = {
    "api/material_api.py": 6,
}


def _tree(rel):
    return ast.parse(pathlib.Path(_ROOT, rel).read_text(encoding="utf-8"))


# ---------- 必须不命中:假守卫不许再存在 ----------

@pytest.mark.parametrize("rel", _SWEPT)
def test_local_impostor_helper_is_gone(rel):
    tree = _tree(rel)
    defs = [
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_get_profile_safe"
    ]
    assert not defs, f"{rel} 里本地 _get_profile_safe 又回来了(它不做 brand 校验)"
    refs = [
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == "_get_profile_safe"
    ]
    assert not refs, f"{rel} 里还有对已删假守卫的引用:{refs}"


# ---------- 必须命中:调用点一个不少地转成了 canonical ----------

@pytest.mark.parametrize("rel", _SWEPT)
def test_calls_migrated_to_canonical_guard(rel):
    tree = _tree(rel)
    calls = sum(
        1 for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "require_profile_access"
    )
    assert calls == _EXPECTED_CALLS[rel], (
        f"{rel} 的 require_profile_access 调用点 {calls},"
        f"预期 {_EXPECTED_CALLS[rel]} —— 迁移过程中漏了或多了"
    )
    imported = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "auth.brand_access"
        and any(a.name == "require_profile_access" for a in n.names)
        for n in ast.walk(tree)
    )
    assert imported, f"{rel} 没有 import canonical 守卫"


# ---------- 通用闸:全站不许再有冒牌守卫 ----------

def test_no_impostor_guards_in_api_tree():
    hits, checked = scan(_ROOT)
    assert checked > 20, f"只检查了 {checked} 个函数,扫描器多半没走到 api/ —— 断言恒真"
    assert not hits, "发现长得像守卫但体内没有任何鉴权原语的函数:\n" + "\n".join(
        f"  {r}:{l}  {n}" for r, l, n in sorted(hits)
    )


def test_scanner_actually_catches_an_impostor(tmp_path):
    """🔴 反向对照:上面那条"0 命中"必须有判别力。

    一个恒不命中的扫描器也能让"0 命中"通过。所以这里造一个**已知的**冒牌货,
    形状与真实那三个逐字同型,扫描器必须抓到它;
    同时放一个**真守卫**在同一个文件里,它必须不被抓 —— 双向都要对。
    """
    api = tmp_path / "api"
    api.mkdir()
    (api / "probe_api.py").write_text(
        "from fastapi import Request\n"
        "from auth.brand_access import require_profile_access\n"
        "\n"
        "def _get_profile_safe_impostor(request, profile_id):\n"
        '    """获取 profile（宽松版，不做 brand 权限校验）"""\n'
        "    profile = get_profile(profile_id)\n"
        "    if not profile:\n"
        "        raise HTTPException(status_code=404, detail='档案不存在')\n"
        "    return profile\n"
        "\n"
        "def _get_profile_real_access(request, profile_id):\n"
        "    return require_profile_access(request, profile_id)\n",
        encoding="utf-8",
    )
    hits, checked = scan(str(tmp_path))
    names = {n for _r, _l, n in hits}
    assert checked == 2, f"两个函数都该被纳入检查,实际 {checked}"
    assert "_get_profile_safe_impostor" in names, "扫描器抓不到已知冒牌货 → 判据作废"
    assert "_get_profile_real_access" not in names, "扫描器把真守卫也抓了 → 噪声太大不可用"


def test_scanner_name_gate_is_a_known_blind_spot(tmp_path):
    """🔴 明写扫描器的盲区,免得下一个人把"0 命中"读成"全站没有越权"。

    扫描器只检查**名字像守卫**的函数(含 safe/access/require/... 等词)。
    一个名字完全不像守卫、却被当守卫用的函数,它抓不到。
    这条用例不是在要求修,是把边界钉成可执行的事实 ——
    哪天扫描器改成不再名字门控了,它会红,提醒人来更新这段说明。
    """
    api = tmp_path / "api"
    api.mkdir()
    (api / "blind_api.py").write_text(
        "def _load_profile(request, profile_id):\n"
        "    return get_profile(profile_id)\n",
        encoding="utf-8",
    )
    hits, checked = scan(str(tmp_path))
    assert checked == 0 and not hits, (
        "扫描器现在能看到非守卫命名的函数了 —— 盲区说明该更新"
    )


# ---------- 原语名单不许落后于 auth/brand_access ----------

def test_primitive_list_covers_all_require_helpers():
    """`auth/brand_access.py` 里每个 `require_*` 都必须登记进 AUTHZ_PRIMITIVES。

    漏一个的后果不是漏报而是**误报**:用那个原语的真守卫会被判成冒牌货,
    然后人就开始不信这个扫描 —— 扫描一旦被无视,等于没有。
    (第一版就漏了 `require_quote_access`。)
    """
    src = pathlib.Path(_ROOT, "auth", "brand_access.py").read_text(encoding="utf-8")
    names = set(re.findall(r"^def (require_\w+)", src, re.M))
    assert names, "一个 require_* 都没解析出来 → 断言恒真"
    missing = names - AUTHZ_PRIMITIVES
    assert not missing, f"AUTHZ_PRIMITIVES 漏登记:{sorted(missing)}"


# ---------- 既有先例仍然有效(不许把 geo_assets 那条锁改坏) ----------

def test_geo_assets_precedent_still_holds():
    tree = _tree("api/geo_assets_api.py")
    defs = [
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_get_profile_safe"
    ]
    assert not defs, "geo_assets_api 的假守卫回来了(GEO-R8-CAN-003 的先例被推翻)"
