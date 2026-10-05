# -*- coding: utf-8 -*-
"""#39 树守卫:证明「我为了量分母而做的动作,没有改树」。

🔴 为什么需要:2026-08-31 我并行扫 80 个前端脚本取分母,其中 8 个是**变异 runner**,
   超时被杀后在 `frontend/src/lib/reportLoadFailure.ts` 留下 `return true;` 残留,
   gate2 凭空红 2 条,我差点记到别人头上。**取分母的动作本身会改树。**

用法:
    python scripts/_wob39_tree_guard.py snap  > .wob39_guard_before.json
    ...做事...
    python scripts/_wob39_tree_guard.py check .wob39_guard_before.json

判据不是「git status 干净」——那漏掉整个 ignored 空间,而且我**本来就会**改文件。
判据是「**除我声明要改的路径外**,一个字节都没动」。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = pathlib.Path(__file__).resolve().parent.parent

# 我这一轮**声明会改**的路径;除此之外任何变动都算污染
DECLARED = ("scripts/manual/", "scripts/_wob39_", "pytest.ini", ".wob39_")


def _run(cmd: list[str]) -> str:
    """🔴 不用 text=True —— 它按 locale(本机 GBK)解码,仓里有非 GBK 文件名时
    抛 UnicodeDecodeError,`.stdout` 变 None,守卫**在量之前就死了**。
    死法还很坏:退出码非 0,与「检测到污染」同码 —— 我第一版就靠这个假通过了一次自证。"""
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} 退出 {r.returncode}: "
                           f"{r.stderr.decode('utf-8', 'replace')[:200]}")
    return r.stdout.decode("utf-8", "surrogateescape")


def _tracked_hashes() -> dict[str, str]:
    """所有 git 跟踪文件的内容哈希 —— 比 `git status` 强:
    status 只说「变没变」,哈希能说「变成了什么」,而且不受 mtime/CRLF 影响。"""
    out = _run(["git", "ls-files", "-z"])
    h = {}
    for name in out.split("\0"):
        if not name:
            continue
        p = ROOT / name
        if not p.is_file():
            continue
        h[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return h


def _ignored_and_untracked() -> list[str]:
    out = _run(["git", "status", "--porcelain", "--ignored=matching"])
    return sorted(l[3:] for l in out.splitlines() if l[:2] in ("!!", "??", " M", "M ", "MM"))


def snap() -> dict:
    return {"tracked": _tracked_hashes(), "loose": _ignored_and_untracked()}


def main() -> int:
    if sys.argv[1] == "snap":
        s = snap()
        pathlib.Path(sys.argv[2]).write_text(json.dumps(s), encoding="utf-8")
        print(f"  基线已存:跟踪文件 {len(s['tracked'])} 个 · 游离项 {len(s['loose'])} 个")
        return 0

    before = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
    after = snap()
    declared = lambda n: any(n.startswith(d) for d in DECLARED)

    changed = [n for n, h in after["tracked"].items()
               if before["tracked"].get(n) != h and not declared(n)]
    gone = [n for n in before["tracked"] if n not in after["tracked"] and not declared(n)]
    new_loose = [n for n in after["loose"] if n not in before["loose"] and not declared(n)]

    print(f"  跟踪文件内容变动(未声明): {len(changed)}")
    for n in changed[:10]:
        print(f"     🔴 {n}")
    print(f"  跟踪文件消失(未声明):     {len(gone)}")
    for n in gone[:10]:
        print(f"     🔴 {n}")
    print(f"  新增游离项(未声明):       {len(new_loose)}")
    for n in new_loose[:10]:
        print(f"     🟡 {n}")

    clean = not (changed or gone or new_loose)
    print(f"\n  {'✅ 树未被我的动作污染' if clean else '🔴 树被改了 —— 先查归属,别继续量'}")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
