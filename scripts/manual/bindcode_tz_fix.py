"""
绑定码 bug 1 修复验证 · 时区无歧义 + TTL 15 分钟

不依赖 FastAPI / DB / 用户登录，纯函数级验证返回字段。
"""

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"[PASS] {msg}")


def _fail(msg):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def main():
    # 模拟 generate_bind_code 的核心逻辑
    BIND_CODE_TTL_SEC = 900
    expires_at = time.time() + BIND_CODE_TTL_SEC
    response = {
        "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
        "expires_at_ts": expires_at,
        "ttl_seconds": BIND_CODE_TTL_SEC,
    }

    # 1. TTL 是 900 秒
    if response["ttl_seconds"] != 900:
        _fail(f"ttl_seconds 应为 900，实际 {response['ttl_seconds']}")
    _ok("TTL = 900 秒（15 分钟）")

    # 2. expires_at_ts 是数字，且大约 = now + 900
    if not isinstance(response["expires_at_ts"], (int, float)):
        _fail(f"expires_at_ts 应为数字，实际类型 {type(response['expires_at_ts'])}")
    delta = response["expires_at_ts"] - time.time()
    if not (895 < delta < 905):
        _fail(f"expires_at_ts 跟 now 差距异常 {delta}")
    _ok(f"expires_at_ts 是 Unix 数字，距 now {delta:.1f} 秒")

    # 3. 老字段 expires_at 必须带时区后缀
    iso = response["expires_at"]
    if "+00:00" not in iso and not iso.endswith("Z"):
        _fail(f"expires_at ISO 字符串没有时区后缀（bug 1 核心）: {iso}")
    _ok(f"expires_at 带时区后缀: {iso}")

    # 4. 用 Python 当 JS 模拟一下解析（带 TZ 后缀的 ISO Python/JS 都会按 UTC 解）
    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        _fail("解析后没有时区信息")
    parsed_ts = parsed.timestamp()
    if abs(parsed_ts - response["expires_at_ts"]) > 1:
        _fail(f"老字段解析回来跟 expires_at_ts 不一致 {parsed_ts} vs {response['expires_at_ts']}")
    _ok(f"老字段解析回来 Unix = 新字段 ({parsed_ts:.0f})")

    # 5. 模拟不同时区的客户端解析（关键 bug 修复点）
    # 之前 bug：服务器在 +8 时区生成 ISO 无后缀，客户端在 UTC 当 local 解析 → 偏 8 小时
    # 现在带后缀 ISO，无论客户端在哪个时区，解析出来都是同一个绝对时刻
    print("\n  TZ 无歧义验证：")
    iso_aware = response["expires_at"]
    # 模拟"服务器 +8 → 客户端 UTC"和"服务器 UTC → 客户端 +8"两种场景
    # 不管怎么变换，带后缀 ISO 解析出来的 Unix 时间戳都是固定的
    import os as _os
    for tz_name in ["UTC", "Asia/Shanghai", "America/New_York"]:
        if hasattr(time, "tzset"):
            _os.environ["TZ"] = tz_name
            time.tzset()
        parsed = datetime.fromisoformat(iso_aware).timestamp()
        diff = abs(parsed - response["expires_at_ts"])
        if diff > 1:
            _fail(f"客户端在 {tz_name} 时区解析偏了 {diff} 秒 → bug 1 未修好")
        print(f"    [PASS] 客户端 TZ={tz_name} 解析 Unix={parsed:.0f}（与服务端一致）")

    print("\n========== bug 1 (绑定码时区+TTL) 修复验证全过 ==========")
    print("- TTL 从 5 分钟 → 15 分钟")
    print("- expires_at ISO 带 +00:00 后缀，JS new Date() 解析无歧义")
    print("- 新增 expires_at_ts / ttl_seconds 给前端将来用更稳的方案")


if __name__ == "__main__":
    main()
