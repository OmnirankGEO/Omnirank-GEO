"""[R5 ⑤] 本目录夹具的 brands 保真锁 —— 换回手搓即红。

原夹具在 conftest.BASE_DDL 里手搓了 3 列的 brands
(id BIGSERIAL / owner_user_id NOT NULL REFERENCES users(id) / name),生产 32 列。

🔴 本目录的 DSN 安全栓(EXACT_THROWAWAY_URL + 每进程私有 schema)**一个字没动**:
   改的是 brands 的 DDL 来源,不是连哪个库。本锁也走那把栓开出来的私有 schema。
"""
from tests.brands_fixture_lock import (
    assert_brands_has_production_unique_index,
    assert_brands_is_production_shaped,
)


def test_fixture_brands_is_built_by_the_production_ssot_export(
        isolated_pricing_schema, raw_conn):
    assert_brands_is_production_shaped(raw_conn)
    assert_brands_has_production_unique_index(raw_conn)
