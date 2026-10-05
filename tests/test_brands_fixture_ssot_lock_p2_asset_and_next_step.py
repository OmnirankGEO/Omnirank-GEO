"""[R5 ⑤ 微单] tests/test_p2_asset_and_next_step_2026_08_14.py 的 brands 保真锁 —— 换回手搓即红。

原夹具手搓的 brands：3 列（id/name/owner_user_id），生产 32 列。
已改调 db.brands_schema.ensure_brands_schema()（窄出口，批 1 建），
并仍登记进 `_created`，保持该文件「建过必拆」的清理契约不变。

这是分母里的**第 8 个**单文件夹具：批 2 工单范围写死 7 个，我在交付单里把它单列报回，
Review 裁定「收」。按「顺手多修一处 = 顺手多欠一条判据」，它配同款正反两把锁。

两条一正一反，谓词写在 tests/brands_fixture_lock.py 一处：
  · 正：AST 里必须有一次**真正的调用**（import / 注释 / 字符串里提一嘴都不算）；
  · 反：源码里不许再有手搓的 `CREATE TABLE [IF NOT EXISTS] brands (`。
改回手搓必然同时踩这两条。brands 的**形状**由 tests/db_bootstrap_r5 的运行时判据保证。
"""
from tests.brands_fixture_lock import (
    assert_fixture_calls_the_ssot_export,
    assert_fixture_does_not_hand_author_brands,
)

TARGET = "tests/test_p2_asset_and_next_step_2026_08_14.py"


def test_fixture_calls_the_production_ssot_export():
    assert_fixture_calls_the_ssot_export(TARGET)


def test_fixture_no_longer_hand_authors_brands():
    assert_fixture_does_not_hand_author_brands(TARGET)
