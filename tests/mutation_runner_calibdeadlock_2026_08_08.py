"""变异验证 · calibdeadlock-20260808。

规矩:每个变异都必须**只**能被指定的那几条锁抓到,且必须真的改到了源码
(替换没命中就当场失败 —— 惰性变异 = 虚报覆盖)。变异后跑指定用例,
**必须红**;红不了说明那条锁没有判别力,不算数。

    TEST_DATABASE_URL=postgresql://... python tests/mutation_runner_calibdeadlock_2026_08_08.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARE_API = ROOT / "api" / "share_api.py"
REBUILD = ROOT / "scripts" / "rebuild_parenthetical_false_mentions_2026_08_06.py"
WIRING = "tests/test_portal_calib_wiring_2026_08_08.py"
LOCKWIN = "tests/test_rebuild_lock_window_2026_08_08.py"


@dataclass
class Mutation:
    mid: str
    what: str
    path: Path
    old: str
    new: str
    must_fail: list[str]
    extra: list[tuple[Path, str, str]] = field(default_factory=list)


MUTATIONS = [
    # ── ① 门户校准接线 ────────────────────────────────────────────────
    Mutation(
        "M01", "删掉端点内 token 解析 —— **本 bug 的原形态**(生产就是这个样子)",
        SHARE_API,
        "    _viewer_identity = _optional_bearer_identity(request)",
        "    _viewer_identity = None",
        [f"{WIRING}::test_owner_bearer_does_get_calibration_through_real_asgi",
         f"{WIRING}::test_admin_bearer_gets_calibration"],
    ),
    Mutation(
        "M02", "归属判据放宽成「登录了就行」(红线④ 的反面)",
        SHARE_API,
        "        if brand_owner_user_id is None:\n            return False\n"
        "        return int(viewer_id) == int(brand_owner_user_id)",
        "        return True",
        [f"{WIRING}::test_stranger_bearer_gets_nothing"],
    ),
    # 🔴 M03 变异必须打在**调用点**上,不能打在 `_optional_bearer_identity` 内部:
    #    它整个函数体裹在 `try/except Exception: return None` 里,内部抛什么都会被
    #    自己吞掉 —— 打在里面是**空操作变异**,存活不代表锁弱(第一版就这么误判过)。
    Mutation(
        "M03", "带了 Authorization 但解析不出身份就拒绝请求(再造一次客户面 P0)",
        SHARE_API,
        "    _viewer_identity = _optional_bearer_identity(request)\n"
        "    if _viewer_identity is not None:",
        "    _viewer_identity = _optional_bearer_identity(request)\n"
        "    if _viewer_identity is None and request.headers.get('Authorization'):\n"
        "        raise HTTPException(401, detail='bad token')\n"
        "    if _viewer_identity is not None:",
        [f"{WIRING}::test_broken_authorization_is_treated_as_anonymous_not_rejected",
         f"{WIRING}::test_expired_jwt_is_anonymous",
         f"{WIRING}::test_portal_short_token_is_not_an_agent_identity"],
    ),
    # 🔴 M04 同理:把门户分支改成 `if False:` 也是空操作 —— 短 token 本来就过不了
    #    `decode_jwt`(不是三段 JWT),两条路殊途同归。要有判别力就得让它**真的**
    #    认下这个身份,那才是"方向反了"的形态。
    Mutation(
        "M04", "把门户短 token 直接认成服务商身份(客户拿到校准入口)",
        SHARE_API,
        "        if len(token) <= 20 and not token.startswith(\"eyJ\"):\n            return None",
        "        if len(token) <= 20 and not token.startswith(\"eyJ\"):\n"
        "            return {\"user_id\": 112, \"is_admin\": False, \"roles\": []}",
        [f"{WIRING}::test_portal_short_token_is_not_an_agent_identity"],
    ),
    Mutation(
        "M05", "偷偷给中间件的公共前缀开软鉴权(红线① · 受保护文件)",
        ROOT / "auth" / "middleware.py",
        '    "/api/public/",    # 公开页面（报告分享等）',
        '    # "/api/public/",  # 公开页面（报告分享等）',
        [f"{WIRING}::test_auth_middleware_untouched_public_prefix_still_early_returns"],
    ),

    # ── ② 重建脚本写窗口 ─────────────────────────────────────────────
    Mutation(
        "M06", "写窗口内开一次新连接(= 旧顺序的等价效应)· 守卫应当当场炸",
        REBUILD,
        '    with _forbid_new_connections("rebuild_one 写窗口"):\n        cur.execute(\n'
        '            "UPDATE diagnosis_records SET raw_data_json = %s WHERE id = %s",',
        '    with _forbid_new_connections("rebuild_one 写窗口"):\n'
        '        from db.connection import get_connection as _mut_gc\n'
        '        _mut_gc().close()\n        cur.execute(\n'
        '            "UPDATE diagnosis_records SET raw_data_json = %s WHERE id = %s",',
        [f"{LOCKWIN}::test_no_new_connection_is_opened_inside_the_write_window"],
    ),
    Mutation(
        "M07", "拆掉守卫 + 写窗口内开新连接 —— 证明**探针**独立于守卫也抓得到",
        REBUILD,
        '    with _forbid_new_connections("rebuild_one 写窗口"):\n        cur.execute(\n'
        '            "UPDATE diagnosis_records SET raw_data_json = %s WHERE id = %s",',
        '    with contextlib.nullcontext():\n        cur.execute(\n'
        '            "UPDATE diagnosis_records SET raw_data_json = %s WHERE id = %s",',
        [f"{LOCKWIN}::test_no_new_connection_is_opened_inside_the_write_window"],
        extra=[(
            REBUILD,
            '        if v2 is not None and not report_error:\n'
            '            from services.diagnosis_report_v2 import update_diagnosis_v2_in_db',
            '        if v2 is not None and not report_error:\n'
            '            from db.connection import get_connection as _mut_gc\n'
            '            _mut_gc().close()\n'
            '            from services.diagnosis_report_v2 import update_diagnosis_v2_in_db',
        )],
    ),
    Mutation(
        "M08", "守卫改成 patch 模块属性(拦不住 import 期绑定的调用方)",
        REBUILD,
        "    pool = _connection._get_pool()\n    original = pool.getconn",
        "    pool = _connection\n    original = _connection.get_connection",
        [f"{LOCKWIN}::test_forbid_guard_denies_even_import_bound_get_connection"],
        extra=[(
            REBUILD,
            "    pool.getconn = _deny\n    try:\n        yield\n    finally:\n"
            "        pool.getconn = original",
            "    pool.get_connection = _deny\n    try:\n        yield\n    finally:\n"
            "        pool.get_connection = original",
        )],
    ),
    Mutation(
        "M09", "预加载清单里去掉真凶 db.diagnosis_db",
        REBUILD,
        '        "db.diagnosis_db",              # 🔴 模块级 init_db() —— 死锁真凶,必须最先',
        '        "json",',
        [f"{LOCKWIN}::test_preload_covers_the_module_that_actually_deadlocks"],
    ),
    Mutation(
        "M10", "跑完不提交(报告一件没发生的事)",
        REBUILD,
        "                if result.get(\"status\") == \"rebuilt\":\n                    conn.commit()",
        "                if result.get(\"status\") == \"rebuilt\":\n                    pass",
        [f"{LOCKWIN}::test_apply_end_to_end_really_commits_and_never_touches_full_response",
         f"{LOCKWIN}::test_one_diagnosis_failing_does_not_take_down_the_others"],
    ),
    # 🔴 dry-run 安全是**两道**闸(rebuild_one 早返回 + main 逐份 rollback)。
    #    只拆一道是空操作变异 —— 必须两道一起拆,才验得到"dry-run 一个字不写"。
    Mutation(
        "M11", "dry-run 也写库(两道闸一起拆:早返回 + 逐份 rollback)",
        REBUILD,
        "    if not apply:\n        return result\n\n    cur = conn.cursor()",
        "    if False:\n        return result\n\n    cur = conn.cursor()",
        [f"{LOCKWIN}::test_dry_run_writes_nothing"],
        extra=[(
            REBUILD,
            "                elif result.get(\"status\") == \"dry_run\":\n"
            "                    conn.rollback()",
            "                elif result.get(\"status\") == \"dry_run\":\n"
            "                    conn.commit()",
        )],
    ),
    Mutation(
        "M12", "改写原始回答 full_response(红线⑤)",
        REBUILD,
        "            cell[\"matched_text\"] = decision.matched_alias or None",
        "            cell[\"matched_text\"] = decision.matched_alias or None\n"
        "            cell[\"full_response\"] = answer + \"·\"",
        [f"{LOCKWIN}::test_apply_end_to_end_really_commits_and_never_touches_full_response"],
    ),
    Mutation(
        "M13", "不清 matched_text —— 真凶「深圳」继续挂在报告上",
        REBUILD,
        "            cell[\"matched_text\"] = decision.matched_alias or None",
        "            pass",
        [f"{LOCKWIN}::test_apply_end_to_end_really_commits_and_never_touches_full_response"],
    ),
]


def _run(node_ids: list[str]) -> tuple[bool, str]:
    # 🔴 显式 utf-8 + errors=replace:Windows 默认 GBK 解子进程输出会直接抛
    #    UnicodeDecodeError,把 stdout 变成 None —— 报错被吃掉,结论全废。
    # 🔴 PYTHONDONTWRITEBYTECODE=1:本轮变异有一条**改前改后字节数完全相同**,
    #    __pycache__ 的失效判据是 (mtime, size) —— 同秒内改回去会让子进程
    #    继续跑**变异版的 .pyc**,表现为"复原后基线仍然红"。踩过一次,写死。
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", *node_ids],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or ""))[-1500:]


def _read(path: Path) -> str:
    """按**字节**读再解码 —— 不走 universal newlines。

    🔴 `Path.read_text()/write_text()` 在 Windows 上是 LF→CRLF 的单向阀:
    读进来变 LF、写回去变 CRLF,于是"原样复原"会把整个文件的行尾翻一遍
    (受保护文件 `auth/middleware.py` 被这么翻过一次)。踩过一次,写死。
    """
    return path.read_bytes().decode("utf-8")


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def main() -> int:
    if not os.environ.get("TEST_DATABASE_URL"):
        print("🔴 需要 TEST_DATABASE_URL(真 PostgreSQL)")
        return 2

    all_nodes = sorted({n for m in MUTATIONS for n in m.must_fail})
    green, output = _run(all_nodes)
    if not green:
        print("🔴 基线就不是绿的,变异结论无意义:\n" + output)
        return 1
    print(f"✅ 基线绿({len(all_nodes)} 条被变异覆盖的用例)\n")

    killed = survived = 0
    for mutation in MUTATIONS:
        edits = [(mutation.path, mutation.old, mutation.new), *mutation.extra]
        originals = {path: _read(path) for path, _, _ in edits}
        try:
            for path, old, new in edits:
                text = _read(path)
                if old not in text:
                    raise AssertionError(f"{mutation.mid} 锚点没命中 {path.name} —— 变异是惰性的")
                _write(path, text.replace(old, new, 1))
                assert _read(path) != originals[path], "文件没真改"

            passed, tail = _run(mutation.must_fail)
            if passed:
                survived += 1
                print(f"🔴 {mutation.mid} 存活 —— 锁没有判别力:{mutation.what}\n{tail}\n")
            else:
                killed += 1
                print(f"✅ {mutation.mid} 被杀:{mutation.what}")
        finally:
            for path, text in originals.items():
                _write(path, text)
                assert _read(path) == text, f"{path} 没复原"

    再绿, _ = _run(all_nodes)
    print(f"\n变异 {killed} 杀 / {survived} 存活 · 复原后基线{'绿' if 再绿 else '🔴 红'}")
    return 0 if (survived == 0 and 再绿) else 1


if __name__ == "__main__":
    sys.exit(main())
