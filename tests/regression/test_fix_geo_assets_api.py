"""
判别性回归锁 — api/geo_assets_api.py 的 2 条 GEO-R8 修复。

主形态 = source-inspection：读源码文本断言修复标志存在，回退修复则断言失败。
不依赖 DB / 不 import server.py。

covered candidates:
  - GEO-R8-CAN-003 (geoassets-authz-owner-only): 用 brand_access.require_profile_access
    替换基于不存在的 client_profiles.user_id 的旧 _get_profile_safe（旧逻辑对所有非 admin 恒 403）。
  - GEO-R8-CAN-005 (geoassets-lost-update-jsonb): update_geo_assets 的 read-modify-write
    放进同一事务并对目标行加 SELECT ... FOR UPDATE 悲观锁，消除并发丢更新。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "api" / "geo_assets_api.py").read_text(encoding="utf-8")


# ----------------------------------------------------------------------------
# GEO-R8-CAN-003 — 归属校验改用 brand_access（owner/分配代理/admin）
# ----------------------------------------------------------------------------

def test_can003_imports_require_profile_access():
    assert "from auth.brand_access import require_profile_access" in SRC


def test_can003_both_handlers_use_require_profile_access():
    # get + patch 两个 handler 都必须经 require_profile_access 校验
    assert SRC.count("require_profile_access(request, str(profile_id))") >= 2


def test_can003_broken_userid_check_removed():
    # 旧的 profile.get("user_id") != user_id 判定（依赖不存在的列）必须彻底移除
    assert 'profile.get("user_id") != user_id' not in SRC
    assert "profile.get('user_id') != user_id" not in SRC


def test_can003_broken_helper_removed():
    # 旧 helper 定义已删除，避免被误复用
    assert "def _get_profile_safe(" not in SRC
    assert "_get_profile_safe(" not in SRC


# ----------------------------------------------------------------------------
# GEO-R8-CAN-005 — read-modify-write 单事务 + FOR UPDATE 行锁
# ----------------------------------------------------------------------------

def _update_handler_src() -> str:
    # 截取 update_geo_assets 函数体（到文件尾）
    idx = SRC.index("async def update_geo_assets(")
    return SRC[idx:]


def test_can005_for_update_lock_present():
    body = _update_handler_src()
    assert "FOR UPDATE" in body
    # 锁的对象是目标 profile 行
    assert re.search(
        r"SELECT\s+industry_brief\s+FROM\s+client_profiles\s+WHERE\s+id\s*=\s*%s\s+FOR\s+UPDATE",
        body,
    ), "缺少对目标行的 SELECT ... FOR UPDATE 悲观锁"


def test_can005_read_and_write_in_same_transaction():
    body = _update_handler_src()
    # SELECT FOR UPDATE 与 UPDATE 必须在同一个 with get_db() 块内（先锁后写）。
    # 锚定 SQL 语句本身（不是注释里的字样）。
    lock_pos = body.index("SELECT industry_brief FROM client_profiles")
    update_pos = body.index("UPDATE client_profiles SET industry_brief")
    with_pos = body.index("with get_db() as conn:")
    assert with_pos < lock_pos < update_pos, "读锁与写回必须在同一事务且锁在写之前"


def test_can005_no_stale_prelock_read_of_brief():
    # 修复后不再在事务外先读一份 industry_brief 再覆盖（旧的 lost-update 源头）。
    # 事务内读取用的是锁定后的 row.get("industry_brief")。
    assert 'row.get("industry_brief")' in _update_handler_src()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
