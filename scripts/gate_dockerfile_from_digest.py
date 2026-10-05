#!/usr/bin/env python3
"""WO_307 · Dockerfile FROM 锁摘要门(preflight 5-f · 静态 · 三态)。

## 为什么要这一把
0913AE bake 时 `python:3.12-slim` 上游更新,两个 FROM 都没锁摘要 ⇒ 依赖层整层重建(apt 92s、pip 298s),
盘 77% → 83%,而且**基础镜像换了没经任何评审**。锁成 `名字@sha256:<64 位十六进制>` 之后,
基础镜像只会在「单独一小单、附包清单差异」的升级里变。

## 判据(只读 git 对象,不读工作树)
  · 待发树里每个 Dockerfile 形状的文件(`Dockerfile`、`Dockerfile.*`、`*.dockerfile`、`Containerfile`)的每条 FROM:
    镜像必须带 `@sha256:` + 64 位十六进制(前面带不带 tag 都行)⇒ 否则红;
  · 不要求摘要的两种:引用本文件**前面阶段名**的 FROM(`FROM builder`,阶段名不分大小写)、`FROM scratch`;
  · 镜像名里有 `$`(ARG 变量)⇒ 红:看不出最后拉的是什么;
  · `--platform=…` 之类的参数先剥掉再判;续行(行尾 `\\`)先接上。
  · 分类函数带牙证:先用固定样本自检,自检不过 ⇒ rc 3(尺子坏了不量)。

## 退出码
  0 = 每条 FROM 都锁了摘要或属于免检两种
  1 = 有 FROM 没锁 / 摘要形状不对 / 用了变量
  3 = 没跑成(取不到树 / 一个 Dockerfile 都没找到 / 自检不过)
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DOCKERFILE_RE = re.compile(r"(^|/)(Dockerfile|Containerfile)([.-][^/]*)?$|\.dockerfile$", re.I)
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
FROM_RE = re.compile(r"^\s*FROM\s+(.*)$", re.I)


def classify(image: str, stages: set[str]) -> tuple[str, str]:
    """返回 (类别, 说明)。类别:pinned / stage / scratch / var / unpinned。"""
    if "$" in image:
        return "var", "镜像名里有变量,看不出最后拉的是什么"
    if image.lower() == "scratch":
        return "scratch", "scratch 空镜像,无摘要可锁"
    if image.lower() in stages:
        return "stage", "引用本文件前面的阶段"
    if DIGEST_RE.search(image):
        return "pinned", "已锁摘要"
    if "@sha256:" in image:
        return "unpinned", "有 @sha256: 但不是 64 位小写十六进制"
    return "unpinned", "没锁摘要"


def selftest() -> bool:
    d = "@sha256:" + "a" * 64
    cases = [
        ("python:3.12-slim", set(), "unpinned"),
        ("python:3.12-slim" + d, set(), "pinned"),
        ("python" + d, set(), "pinned"),
        ("python:3.12-slim@sha256:" + "a" * 63, set(), "unpinned"),
        ("frontend-builder", {"frontend-builder"}, "stage"),
        ("Frontend-Builder", {"frontend-builder"}, "stage"),
        ("frontend-builder", set(), "unpinned"),
        ("scratch", set(), "scratch"),
        ("${BASE}", set(), "var"),
    ]
    if not all(classify(img, st)[0] == want for img, st, want in cases):
        return False
    # [Review 09-25 补] 解析也要自检:Dockerfile 指令不分大小写,小写 from 必须被认出来(否则整条被当成非 FROM 漏掉)
    return [a for _, a in from_lines("from python:3.12-slim AS base\nFrom scratch\nRUN from x\n")] == \
        ["python:3.12-slim AS base", "scratch"]


def from_lines(text: str) -> list[tuple[int, str]]:
    """(行号, FROM 之后的参数串);续行接上,注释行跳过。"""
    out: list[tuple[int, str]] = []
    buf, start = "", 0
    for i, raw in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        line = raw.rstrip()
        if not buf and line.lstrip().startswith("#"):
            continue
        if not buf:
            start = i
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        full = buf + line
        buf = ""
        m = FROM_RE.match(full)
        if m:
            out.append((start, m.group(1).strip()))
    return out


def parse_from(args: str) -> tuple[str, str | None]:
    """返回 (镜像, 阶段名 | None);剥掉 --xxx=… 参数。"""
    toks = [t for t in args.split() if not t.startswith("--")]
    if not toks:
        return "", None
    image = toks[0]
    name = toks[2] if len(toks) >= 3 and toks[1].lower() == "as" else None
    return image, name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--ref", required=True)
    a = ap.parse_args(argv)

    def git(*args: str) -> tuple[int, str]:
        r = subprocess.run(["git", "-C", a.repo, *args], capture_output=True)
        return r.returncode, r.stdout.decode("utf-8", errors="replace")

    if not selftest():
        print("  🔴 本门【没跑成·rc=3】:分类函数自检不过(未锁必红 / 已锁必绿 / 阶段引用与 scratch 免检 / 变量红)—— 尺子坏了不量")
        return 3
    rc, files = git("ls-tree", "-r", "--name-only", a.ref)
    if rc:
        print(f"  🔴 本门【没跑成·rc=3】:{a.ref} 的树取不到")
        return 3
    dfs = [f for f in files.splitlines() if DOCKERFILE_RE.search(f)]
    if not dfs:
        print("  🔴 本门【没跑成·rc=3】:待发树里一个 Dockerfile 都没找到 —— 分母塌了(仓里本来有)")
        return 3
    red: list[str] = []
    n = 0
    for f in dfs:
        rc, text = git("show", f"{a.ref}:{f}")
        if rc:
            print(f"  🔴 本门【没跑成·rc=3】:{a.ref}:{f} 读不到")
            return 3
        stages: set[str] = set()
        for ln, args in from_lines(text):
            n += 1
            image, name = parse_from(args)
            kind, why = classify(image, stages)
            mark = {"pinned": "✅", "stage": "⚪", "scratch": "⚪"}.get(kind, "🔴")
            print(f"    {mark} {f}:{ln} FROM {image}{' AS ' + name if name else ''} —— {why}")
            if mark == "🔴":
                red.append(f"{f}:{ln} FROM {image}:{why}")
            if name:
                stages.add(name.lower())
    if n == 0:
        print(f"  🔴 本门【没跑成·rc=3】:{len(dfs)} 个 Dockerfile 里一条 FROM 都没解析到 —— 解析坏了")
        return 3
    for r_ in red:
        print(f"  🔴 {r_} —— 改成 名字@sha256:<64 位十六进制>;升基础镜像走单独一小单(见 DEPLOY_CTO_SSOT)")
    if red:
        return 1
    print(f"  ✅ {len(dfs)} 个 Dockerfile · {n} 条 FROM 都锁了摘要或属于免检两种")
    return 0


if __name__ == "__main__":
    sys.exit(main())
