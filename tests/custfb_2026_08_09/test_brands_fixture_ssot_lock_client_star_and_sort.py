"""[R5 ⑤ 批2] tests/custfb_2026_08_09/test_client_star_and_sort_2026_08_09.py 的 brands 保真锁 —— 换回手搓即红。

原夹具手搓的 brands：12 列（id/name/industry/company_name/cities/brand_type/owner_user_id/is_deleted/is_test/status/created_at/updated_at）
生产 32 列。已改调 db.brands_schema.ensure_brands_schema()（窄出口，批 1 建）。

两条一正一反，谓词写在 tests/brands_fixture_lock.py 一处：
  · 正：AST 里必须有一次**真正的调用**（import / 注释 / 字符串里提一嘴都不算）；
  · 反：源码里不许再有手搓的 `CREATE TABLE [IF NOT EXISTS] brands (`。
改回手搓必然同时踩这两条。brands 的**形状**由 tests/db_bootstrap_r5 的运行时判据保证。
"""
from tests.brands_fixture_lock import (
    assert_fixture_calls_the_ssot_export,
    assert_fixture_does_not_hand_author_brands,
)

TARGET = "tests/custfb_2026_08_09/test_client_star_and_sort_2026_08_09.py"


def test_fixture_calls_the_production_ssot_export():
    assert_fixture_calls_the_ssot_export(TARGET)


def test_fixture_no_longer_hand_authors_brands():
    assert_fixture_does_not_hand_author_brands(TARGET)
