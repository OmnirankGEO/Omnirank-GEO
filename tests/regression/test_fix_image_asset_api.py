"""回归锁: api/image_asset_api.py 的 GEO 修复标志(source-inspection)。

判别锁:读源码文本,断言修复标志存在。回退修复则断言失败。
不依赖 DB / 不 import server.py。
"""
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "api" / "image_asset_api.py").read_text(encoding="utf-8")


def _handler_body():
    """截取 article_image_action handler 到下一个 @router 之间的源码。"""
    start = SRC.index("async def article_image_action(")
    tail = SRC[start:]
    nxt = tail.find("\n@router.", 1)
    return tail if nxt == -1 else tail[:nxt]


def test_image_action_write_is_fail_closed():
    """[GEO-R3-CAN-003] image-action 写操作必须 fail-closed:
    require_brand_access 传 allow_null=False,不再对 null-brand 文章放行。
    """
    body = _handler_body()
    # 必须调用 require_brand_access
    assert "require_brand_access(" in body, "handler 应保留 require_brand_access 归属校验"
    # 关键:不能再用 allow_null=True 静默放行 null-brand 文章
    assert not re.search(
        r"require_brand_access\([^)]*allow_null\s*=\s*True", body
    ), "回退!image-action 写操作重新用了 allow_null=True(IDOR)"
    # 正向:显式 fail-closed
    assert re.search(
        r"require_brand_access\([^)]*allow_null\s*=\s*False", body
    ), "image-action 应显式 allow_null=False fail-closed"


def test_fix_marker_present():
    """修复注释标记存在。"""
    assert "[GEO-R3-CAN-003]" in SRC


if __name__ == "__main__":
    test_image_action_write_is_fail_closed()
    test_fix_marker_present()
    print("OK")
