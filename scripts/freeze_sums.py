# -*- coding: utf-8 -*-
"""冻结证据目录的 `SHA256SUMS.txt` —— **只负责生成**。

🔴 [V7-B · Codex fof5 P1-2] 为什么要把它从核验器里拆出来:

   上一版 `verify_fof5.py` 自己 `unlink` 掉四份 SUMS 再重新生成一遍,然后宣布
   「非 OK 0 条」。**核验器改写了被核验物** —— 那句「全项通过」等于自问自答:
   无论证据被谁动过,它都会先按现状重算一遍再说「对得上」。
   Review 上一轮拿它的 rc=0 当亲核依据,同笔入账。

   现在:生成在这里,核验在 `verify_fof_evidence.py`,后者**一个字节都不写**。

用法::

    python scripts/freeze_sums.py <证据目录>            # 生成/刷新五份 SUMS
    python scripts/freeze_sums.py <证据目录> --check    # 只报会写什么,不写

每个子目录一份 SUMS,**排除它自己**(踩过:把自己列进去,列的是创建瞬间空文件的 hash)。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import pathlib
import sys

with contextlib.suppress(AttributeError):   # 调用方可能把 stdout 换成
    sys.stdout.reconfigure(encoding="utf-8")   # StringIO(判据加载本模块时)

SUMS_NAME = "SHA256SUMS.txt"
MANIFEST_NAME = "MANIFEST.json"


def sha256_of(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def lines_for(sub: pathlib.Path) -> list[str]:
    """该子目录下除 SUMS 自身外的全部文件,按相对路径排序。"""
    out = []
    for p in sorted(sub.rglob("*")):
        if p.is_file() and p.name != SUMS_NAME:
            out.append(f"{sha256_of(p)}  {p.relative_to(sub).as_posix()}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("evidence_dir")
    ap.add_argument("--check", action="store_true", help="只报不写")
    ap.add_argument("--tip", required=True, help="本轮尖(写进根锚)")
    ap.add_argument("--tree", required=True, help="本轮树(写进根锚)")
    a = ap.parse_args()
    root = pathlib.Path(a.evidence_dir)
    if not root.is_dir():
        print(f"🔴 证据目录不存在:{root}")
        return 1
    subs = sorted(d for d in root.iterdir() if d.is_dir())
    if not subs:
        print(f"🔴 {root} 下一个子目录都没有 —— 空分母不是通过")
        return 1
    scopes = {}
    for sub in subs:
        ls = lines_for(sub)
        if not ls:
            print(f"🔴 {sub.name} 里一个文件都没有 —— 半份产物不冻结")
            return 1
        body = ("\n".join(ls) + "\n").encode("utf-8")
        if not a.check:
            (sub / SUMS_NAME).write_bytes(body)
        scopes[sub.name] = {"files": len(ls),
                            "sums_sha256": hashlib.sha256(body).hexdigest()}
        print(f"  {sub.name:<12} {len(ls):>3} 份 · SUMS 自身 sha256 = "
              f"{scopes[sub.name]['sums_sha256']}"
              + ("   (--check,未写)" if a.check else ""))

    # 🔴 [V8-B · Codex fof6 P1-2] **根锚**。五份 SUMS 只证「每个 scope 内部自洽」——
    #    重冻一遍照样自洽,所以它证不了**原真**。根 manifest 把五份 SUMS 的 sha
    #    连同本轮尖/树一起钉在一处;它自己的 sha 由跑批端报进台账,
    #    由**证据包之外**的记录来锚。缺了它,「协同改内容 + 重冻 SUMS」无从分辨。
    man = {"tip": a.tip, "tree": a.tree, "scopes": scopes}
    body = (json.dumps(man, ensure_ascii=False, indent=1,
                       sort_keys=True) + chr(10)).encode("utf-8")
    if not a.check:
        (root / MANIFEST_NAME).write_bytes(body)
    print(f"  {MANIFEST_NAME:<12} 根锚 · 覆盖 {len(scopes)} 个 scope · "
          f"自身 sha256 = {hashlib.sha256(body).hexdigest()}"
          + ("   (--check,未写)" if a.check else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
