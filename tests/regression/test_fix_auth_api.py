"""
判别性回归锁 · api/auth_api.py 两条 GEO finding 修复标志
- GEO-R1-CAN-117: 启用短信注册时缺码 fail-closed
- GEO-R1-CAN-022: 头像上传 大小闸 + magic-byte 真实类型嗅探(忽略攻击者可控后缀)

主形态 = source-inspection(读源码文本断言修复标志存在;回退修复则断言失败)。
另加纯函数行为单测(_sniff_image_ext 不依赖 DB/app)。
"""
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

AUTH_API = ROOT / "api" / "auth_api.py"
SRC = AUTH_API.read_text(encoding="utf-8")


# ========== GEO-R1-CAN-117 短信注册 fail-closed ==========

def test_can117_marker_present():
    assert "GEO-R1-CAN-117" in SRC, "缺少 GEO-R1-CAN-117 修复标记"


def test_can117_require_sms_flag_gate():
    # 有 SIGNUP_REQUIRE_SMS 开关读取
    assert "SIGNUP_REQUIRE_SMS" in SRC, "缺少 SIGNUP_REQUIRE_SMS 开关"


def test_can117_fail_closed_on_missing_code():
    # 开关为真且无 sms_code 时必须 raise(fail-closed),不能静默放行
    # 断言存在 `_require_sms and not req.sms_code` 形态的守卫且紧跟 raise
    m = re.search(
        r"_require_sms\s+and\s+not\s+req\.sms_code\s*:\s*\n\s*raise\s+HTTPException",
        SRC,
    )
    assert m, "缺少 '启用短信注册且缺码 -> raise HTTPException' 的 fail-closed 守卫"


# ========== GEO-R1-CAN-022 头像上传硬化 ==========

def test_can022_marker_present():
    assert "GEO-R1-CAN-022" in SRC, "缺少 GEO-R1-CAN-022 修复标记"


def test_can022_size_cap():
    assert "AVATAR_MAX_BYTES" in SRC, "缺少头像大小上限常量"
    # 上传处对超限抛 413
    assert re.search(r"raise\s+HTTPException\(\s*413", SRC), "缺少 413 大小拒绝"


def test_can022_magic_byte_sniff_used_in_handler():
    # handler 里调用类型嗅探,并对非图片拒绝
    assert "_sniff_image_ext(content)" in SRC, "上传 handler 未调用 magic-byte 嗅探"
    # 后缀不再来自攻击者可控的 filename
    assert "os.path.splitext(file.filename)" not in SRC, (
        "仍从攻击者可控 filename 派生后缀(未修复)"
    )


def test_can022_helper_defined():
    assert "def _sniff_image_ext(" in SRC, "缺少 _sniff_image_ext 辅助函数"


# ========== 纯函数行为单测(_sniff_image_ext) ==========

def _load_sniff():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_auth_api_under_test", AUTH_API)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # 若因外部依赖 import 失败,退回文本抽取该纯函数单独 exec
        src = SRC
        start = src.index("def _sniff_image_ext(")
        # 截到下一个顶层 def/@ 之前
        rest = src[start:]
        end = re.search(r"\n(def |@router|# ==========)", rest[3:])
        body = rest[: end.start() + 3] if end else rest
        ns = {"Optional": __import__("typing").Optional}
        exec(body, ns)
        return ns["_sniff_image_ext"]
    return mod._sniff_image_ext


def test_sniff_accepts_real_images():
    sniff = _load_sniff()
    assert sniff(b"\xff\xd8\xff\xe0" + b"\x00" * 16) == ".jpg"
    assert sniff(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16) == ".png"
    assert sniff(b"GIF89a" + b"\x00" * 16) == ".gif"
    assert sniff(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8) == ".webp"


def test_sniff_rejects_html_svg_php():
    sniff = _load_sniff()
    assert sniff(b"<html><script>alert(1)</script></html>") is None
    assert sniff(b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>") is None
    assert sniff(b"<?php system($_GET['c']); ?>") is None
    assert sniff(b"") is None
    assert sniff(b"short") is None
