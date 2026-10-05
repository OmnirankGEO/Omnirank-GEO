"""夹具自身的判别力自证 —— 「装载路径从没被真跑过」是一种假绿。

窗B 的同名 ``_load_prod_schema`` 里没有处理 pg_dump 17+ 的 psql 元命令
(``\\restrict`` / ``\\unrestrict``);它那边靠 ``to_regclass(...) is not None``
短路,库预装好时**根本走不到装载分支**,于是那条路径从没被真跑过 ——
直到窗C 在一个**冷库**上第一次真跑,当场 `syntax error at or near "\\"`。

窗C 的 conftest 同样有短路(否则同一个一次性库跑第二次必炸),所以
这条判据的作用是:**不依赖库**地证明两处剥离规则真的有判别力。
"""

from __future__ import annotations

import pytest

from tests.defensive_geo_w3_2026_08_21 import conftest as ct


def test_search_path_poison_constant_matches_real_dump():
    """毒行常量必须在真 dump 里存在 —— 常量写错了,中和就是空操作。"""
    if not ct.PROD_SCHEMA.exists():
        pytest.skip(f"缺生产 schema 快照:{ct.PROD_SCHEMA}")
    text = ct.PROD_SCHEMA.read_text(encoding="utf-8", errors="replace")
    assert ct._SEARCH_PATH_POISON in text, (
        "search_path 毒行常量与真 dump 对不上 —— 中和会变成空操作,"
        "而空操作与「已中和」在结果上长得一样(直到某条不带 schema 限定的语句炸)"
    )


def test_dump_really_contains_psql_meta_commands():
    """真 dump 里确实有 psql 元命令 —— 这是剥离规则存在的**前提**。

    如果哪天 dump 换成不写元命令的版本,这条会红,提示剥离规则已成死代码
    (conftest 里那句 `stripped == 0 → raise` 是同一件事的运行时形态)。
    """
    if not ct.PROD_SCHEMA.exists():
        pytest.skip(f"缺生产 schema 快照:{ct.PROD_SCHEMA}")
    lines = ct.PROD_SCHEMA.read_text(encoding="utf-8", errors="replace").splitlines()
    meta = [ln for ln in lines if ln.lstrip().startswith(("\\restrict", "\\unrestrict"))]
    assert meta, (
        "真 dump 里一条 psql 元命令都没有 —— 窗C 的剥离规则成了死代码。"
        "这不是失败,是提醒:去掉那段并同时去掉 conftest 里的 stripped==0 断言"
    )


def test_package_migrations_are_registered_in_manifest():
    """本包迁移必须在 manifest 里 —— 漏登记 = 上线后永远不会跑。

    这条**不依赖库**,所以即使判据库没起来它也会红。
    """
    paths = ct.package_migrations()
    assert paths, "本包迁移清单为空 —— 这条锁的分母是零"
    for p in paths:
        assert p.exists(), f"manifest 登记了但文件不在:{p}"


def test_manifest_registration_check_has_discriminating_power(monkeypatch):
    """判别力自证:把 manifest 换成一个不含本包迁移的列表,``package_migrations`` 必抛。

    没有这一条,上面那条在「manifest 检查被写成恒真」时也会绿。
    """
    import db.migration_manifest as mm

    monkeypatch.setattr(mm, "MIGRATIONS", ["db/some_other.sql"], raising=True)
    with pytest.raises(RuntimeError, match="没进 manifest"):
        ct.package_migrations()
