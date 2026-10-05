"""变异 runner —— 证明本批两包的锁有判别力,不是恒真断言。

三层分流(同 P2 runner 口径):
  仍绿                   → **存活**(锁没抓到)
  退出码非 0 但零断言失败 → **假红**,同样计存活(可能是 collection error / 无库 ERROR)
  判别力断言不成立        → **存活**(变异根本没落到被断言的位置上)

每个变异跑完做**字节级还原**并校验 sha256 与变异前一致(newline='' 防 LF→CRLF 毁文件)。

用法:
    python tests/mutation_runner_event_loop_and_sweep_2026_08_01.py --selftest   # 冒烟:自证注入真的落到文件里
    python tests/mutation_runner_event_loop_and_sweep_2026_08_01.py              # 跑全部变异
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]

MEDIA_API = ROOT / "api" / "media_entity_flywheel_api.py"
WRITING_API = ROOT / "api" / "writing_style_flywheel_api.py"
BRIDGE = ROOT / "services" / "research_monitor" / "flywheel_bridge.py"
BRIDGE_DB = ROOT / "db" / "flywheel_bridge_db.py"

LOCKS_A = "tests/test_event_loop_blocking_2026_08_01.py"
LOCKS_B = "tests/test_flywheel_bridge_sweep_wiring_2026_08_01.py"


def read(path: Path) -> str:
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write(path: Path, text: str) -> None:
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_locks(locks: str, k: str | None = None) -> tuple[int, int, int, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-m", "pytest", locks, "-q", "--no-header", "-p", "no:cacheprovider"]
    if k:
        cmd += ["-k", k]
    proc = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", out)) else 0
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
    errors = len(re.findall(r"^ERROR ", out, flags=re.M))
    return failed, passed, errors, out


# ---------------------------------------------------------------------------
# 变异定义:(名字, 文件, old, new, 锁文件, -k 过滤, 说明)
# old 必须在文件中**唯一命中**,否则 runner 直接判"注入失败"(不是存活,是 runner 坏了)。
# ---------------------------------------------------------------------------
MUTATIONS = [
    (
        "M1 · 把 /bridge/health 改回 async def(还原一个已修端点)",
        MEDIA_API,
        "def flywheel_bridge_health_endpoint(request: Request):",
        "async def flywheel_bridge_health_endpoint(request: Request):",
        LOCKS_A,
        "converted",
        "改法锁必须抓到回退",
    ),
    (
        "M2 · 把 /writing/strategy-active 改回 async def",
        WRITING_API,
        "def writing_strategy_active(request: Request, industry_key: str = \"general\"):",
        "async def writing_strategy_active(request: Request, industry_key: str = \"general\"):",
        LOCKS_A,
        "converted",
        "改法锁必须抓到回退",
    ),
    (
        "M3 · 并发探针的重端点改回裸 async(反向对照本体)",
        ROOT / LOCKS_A.replace("/", os.sep),
        "        @app.get(\"/heavy\")\n        def heavy():                            # 本批修法:同步 def → run_in_threadpool",
        "        @app.get(\"/heavy\")\n        async def heavy():                      # 变异:裸同步塞回协程",
        LOCKS_A,
        "starved",
        "主锁(轻端点 <1s)必须转红 —— 证明探针不恒绿",
    ),
    (
        "M4 · 拆掉 sweep 接线(桥跑入口不再清扫)",
        BRIDGE,
        "                swept = await asyncio.to_thread(sweep_stale_orphan_runs, dry_run=False)",
        "                swept = {\"swept\": 0, \"threshold_hours\": 0, \"run_ids\": []}",
        LOCKS_B,
        None,
        "接线锁必须抓到死函数复发",
    ),
    (
        "M5 · sweep 去掉时间阈值(按 status 一刀切,会误杀正在跑的)",
        BRIDGE_DB,
        "             WHERE status = 'running'\n               AND started_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')\n            RETURNING id",
        "             WHERE status = 'running'\n               AND started_at < CURRENT_TIMESTAMP + (%s * INTERVAL '1 hour')\n            RETURNING id",
        LOCKS_B,
        None,
        "「1h 内不动」那条必须转红",
    ),
    (
        "M6 · 把 sweep 挪出 not dry_run(预览也写库)",
        BRIDGE,
        "        if not dry_run:\n            # [包B · 2026-08-01] 自愈式清扫",
        "        if True:\n            # [包B · 2026-08-01] 自愈式清扫",
        LOCKS_B,
        "dry_run",
        "dry_run 零写库那条必须转红",
    ),
    (
        # 🔴 不能用 `try:` → `if True:` 做这个变异:那会留下悬空的 except,整文件 SyntaxError,
        #    锁只会报 collection ERROR = 假红(计存活)。变异必须**语法合法、只改语义**。
        "M7 · 收窄 sweep 的 fail-open except(异常不再被吞,会穿到外层)",
        BRIDGE,
        "            except Exception as exc:  # fail-open:清扫失败不拖垮桥跑",
        "            except ZeroDivisionError as exc:  # 变异:RuntimeError 不再被吞",
        LOCKS_B,
        "failure",
        "fail-open 那条必须转红(且证明它不是恒真:外层 except 会把异常吞成 status=failed)",
    ),
    (
        "M8 · health 快照不带 stale_orphan 计数",
        BRIDGE_DB,
        "        \"stale_orphan_run_count\": int(stale_orphan_runs or 0),",
        "        \"_stale_orphan_run_count_removed\": int(stale_orphan_runs or 0),",
        LOCKS_B,
        "health",
        "§B3 那条必须转红",
    ),
    (
        # 真回归注入:把一个已经 to_thread 化的 async 端点拆回裸同步(= 新增一处阻塞)。
        # 上一版这里改的是扫描器的 THREADPOOL_WRAPPERS 常量 —— 那个变异**存活**,但不是锁弱:
        # 包裹器的实参是 ast.Name 不是 ast.Call,不收集包裹器也不会多出任何 finding,
        # 即"变异没落到被断言的位置上"。教训:变异要打在真行为上,不是打在看着相关的常量上。
        "M9 · 把 /writing/article-review-queue 的 to_thread 拆掉(真新增一处裸同步)",
        WRITING_API,
        "    rows = await asyncio.to_thread(list_review_queue, limit=max(1, min(limit, 500)))",
        "    rows = list_review_queue(limit=max(1, min(limit, 500)))",
        LOCKS_A,
        "no_new_async_endpoint_blocking",
        "AST 扫描锁必须抓到新增的裸同步重活",
    ),
    (
        "M10 · 扫描器不再跳过嵌套 def 体(误把线程池闭包算成 loop 上的活)",
        ROOT / "scripts" / "scan_async_endpoint_blocking_2026_08_01.py",
        "                node, skip_nested_defs=True, skip_wrapper_args=True\n            ):",
        "                node, skip_nested_defs=False, skip_wrapper_args=True\n            ):",
        LOCKS_A,
        "no_new_async_endpoint_blocking",
        "扫描锁必须对扫描口径变化敏感(否则锁跟扫描器脱钩)",
    ),
]


def apply_one(name, path, old, new, locks, kfilter, why, *, selftest: bool) -> str:
    src = read(path)
    before = sha(src)
    hits = src.count(old)
    if hits != 1:
        return f"注入失败(命中 {hits} 次,要求恰好 1 次)"
    mutated = src.replace(old, new)
    assert sha(mutated) != before, "替换后内容没变?"
    write(path, mutated)
    try:
        # 冒烟:证明注入真的落到磁盘上了(不是只在内存里改了个字符串)
        on_disk = read(path)
        if on_disk != mutated:
            return "注入失败(写盘内容与预期不符)"
        if selftest:
            return "注入生效(selftest 不跑锁)"
        failed, passed, errors, out = run_locks(locks, kfilter)
        if failed > 0:
            return f"KILLED(锁抓到:{failed} failed / {passed} passed)"
        if errors > 0:
            return f"存活·假红(0 failed 但 {errors} ERROR —— 不算抓到)"
        return f"🔴 存活(仍绿:{passed} passed)"
    finally:
        write(path, src)
        restored = read(path)
        assert sha(restored) == before, f"还原失败! {path}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="冒烟:只验注入能落盘 + 能字节级还原,不跑锁")
    args = ap.parse_args()

    if not args.selftest:
        print("== 基线(未变异)必须全绿 ==")
        for locks in (LOCKS_A, LOCKS_B):
            f, p, e, _ = run_locks(locks)
            print(f"  {locks}: {p} passed / {f} failed / {e} ERROR")
            if f or e:
                print("  基线就不绿,变异结果无意义。先修基线。")
                return 2

    print(f"\n== {'SELFTEST(只验注入)' if args.selftest else '变异'} · 共 {len(MUTATIONS)} 个 ==")
    survivors = []
    for m in MUTATIONS:
        name = m[0]
        verdict = apply_one(*m, selftest=args.selftest)
        print(f"  [{verdict}] {name}")
        if "存活" in verdict or "失败" in verdict:
            survivors.append((name, verdict))

    print()
    if args.selftest:
        bad = [s for s in survivors if "失败" in s[1]]
        print("selftest:", "PASS(全部变异都能注入 + 字节级还原)" if not bad else f"FAIL {bad}")
        return 0 if not bad else 1
    if survivors:
        print(f"🔴 {len(survivors)} 个变异存活 —— 对应的锁没有判别力:")
        for n, v in survivors:
            print(f"    - {n}  →  {v}")
        return 1
    print(f"✅ {len(MUTATIONS)}/{len(MUTATIONS)} 全杀。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
