"""Source-inspection 判别锁 for api/admin_api.py GEO 修复。

回退任一修复标志 → 对应断言失败。不依赖 DB / 不 import server.py。
覆盖:
  - GEO-R1-CAN-028: batch-toggle-active 同步递增 permission_version
  - GEO-R1-CAN-046: PUT /users/{user_id} 改 is_active 时递增 permission_version
  - GEO-R1-CAN-047: GET /users/{user_id} 加 :int 转换器防止抢匹配 /users/export
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "api" / "admin_api.py").read_text(encoding="utf-8")


def test_can028_batch_toggle_bumps_permission_version():
    # 批量禁用的 UPDATE 必须同时递增 permission_version
    m = re.search(
        r"UPDATE users SET is_active = %s[^\n]*WHERE id = ANY\(%s\)",
        SRC,
    )
    assert m, "batch-toggle-active 的 UPDATE 语句未找到(结构已变)"
    assert "permission_version = permission_version + 1" in m.group(0), (
        "GEO-R1-CAN-028 回退: 批量禁用未递增 permission_version"
    )


def test_can046_put_update_bumps_permission_version_on_is_active():
    # admin_update_user handler 区域必须在 is_active 变更时递增 permission_version
    idx = SRC.find("async def admin_update_user")
    assert idx != -1, "admin_update_user handler 未找到"
    region = SRC[idx: idx + 1500]
    assert "'is_active' in updates" in region or '"is_active" in updates' in region, (
        "GEO-R1-CAN-046 回退: 未针对 is_active 变更做处理"
    )
    assert "permission_version = permission_version + 1" in region, (
        "GEO-R1-CAN-046 回退: PUT 编辑路径改 is_active 未递增 permission_version"
    )


def test_can047_get_user_route_has_int_converter():
    # GET /users/{user_id} 必须用 :int 转换器,避免抢匹配静态 /users/export
    assert '@router.get("/users/{user_id:int}")' in SRC, (
        "GEO-R1-CAN-047 回退: GET /users/{user_id} 未加 :int 路径转换器"
    )
    # 且 export 静态路由仍存在
    assert '@router.get("/users/export")' in SRC, "/users/export 路由缺失"


def test_export_not_shadowed_by_bare_dynamic_get():
    # 不应再存在裸 GET /users/{user_id}(无 :int)抢匹配
    assert '@router.get("/users/{user_id}")' not in SRC, (
        "GEO-R1-CAN-047 回退: 存在裸 GET /users/{user_id} 会抢匹配 /users/export"
    )
