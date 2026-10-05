# -*- coding: utf-8 -*-
""".dockerignore 匹配器 —— 照 moby/patternmatcher(Docker / BuildKit 用的那一份)的语义移植。

为什么不用 `git check-ignore`:gitignore 与 dockerignore 语义不同 —— 例如本仓
`.dockerignore` 里的 `*.txt` 在 Docker 下**只匹配上下文根目录**的 txt(模式恒锚定在根),
gitignore 下却匹配任意层级。拿 gitignore 的答案当 Docker 的答案,尺子本身就是歪的。

移植的三段(对应上游):
  1. `ignorefile.ReadAll`:跳过 `#` 开头的行(判在 TrimSpace **之前**)→ TrimSpace →
     `!` 取反 → `filepath.Clean` → 去掉开头的 `/`。
  2. `Pattern.compile`:`**` 在末尾 ⇒ `.*`;`**` 在中间 ⇒ `(.*/)?`(`**/` 吃掉斜杠);
     `*` ⇒ `[^/]*`;`?` ⇒ `[^/]`;`\\` 转义下一个字符;其余字面量。整串锚定 `^…$`。
  3. `MatchesOrParentMatches`:按顺序走全部模式,**最后一条命中者说了算**;
     一条模式没命中文件本身时,再试它的每一级父目录(`docs/**` 会命中 `docs/x/y.md`,
     `!docs/条款` 也会通过父目录 `docs/条款` 命中 `docs/条款/a.md`)。
     「已排除时只看取反模式、未排除时只看排除模式」这条短路也照搬。

判据用法见 `build_context_files`:**构建上下文 = 树里的文件 ∩ 未被排除**。
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Iterable, List, Set


@dataclass(frozen=True)
class _Pattern:
    exclusion: bool
    cleaned: str
    regex: "re.Pattern[str]"


def _go_clean(p: str) -> str:
    """Go `filepath.Clean` 在 `/` 分隔符下的等价物(Linux 构建机语义)。"""
    if p == "":
        return "."
    rooted = p.startswith("/")
    out = posixpath.normpath(p)
    # posixpath 会保留开头的 `//`,Go 会折成一个
    if rooted and out.startswith("//"):
        out = "/" + out.lstrip("/")
    return out


def read_patterns(text: str) -> List[str]:
    """`ignorefile.ReadAll` 的移植:返回清洗后的模式串(取反的带 `!` 前缀)。"""
    out: List[str] = []
    for i, line in enumerate(text.splitlines()):
        if i == 0:
            line = line.lstrip("﻿")
        if line.startswith("#"):
            continue
        pattern = line.strip()
        if not pattern:
            continue
        invert = pattern[0] == "!"
        if invert:
            pattern = pattern[1:].strip()
        if pattern:
            pattern = _go_clean(pattern)
            if len(pattern) > 1 and pattern[0] == "/":
                pattern = pattern[1:]
        out.append(("!" + pattern) if invert else pattern)
    return out


def _compile(pattern: str) -> "re.Pattern[str]":
    reg = "^"
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                if i >= n:
                    reg += ".*"
                else:
                    reg += "(.*/)?"
                continue
            reg += "[^/]*"
        elif ch == "?":
            reg += "[^/]"
        elif ch == "\\":
            if i + 1 < n:
                i += 1
                reg += re.escape(pattern[i])
            else:
                reg += re.escape("\\")
        elif ch in "[]":
            reg += ch
        else:
            reg += re.escape(ch)
        i += 1
    return re.compile(reg + "$")


def compile_patterns(patterns: Iterable[str]) -> List[_Pattern]:
    """`patternmatcher.New` 的移植(它对每条模式**再做一次** Clean,再剥 `!`)。"""
    out: List[_Pattern] = []
    for p in patterns:
        p = p.strip()
        if not p:
            continue
        p = _go_clean(p)
        exclusion = p.startswith("!")
        if exclusion:
            if len(p) == 1:
                raise ValueError('illegal exclusion pattern: "!"')
            p = p[1:]
        out.append(_Pattern(exclusion, p, _compile(p)))
    return out


def is_excluded(rel_path: str, patterns: List[_Pattern]) -> bool:
    """`MatchesOrParentMatches` 的移植。`rel_path` 用 `/` 分隔、相对上下文根。"""
    matched = False
    parent = posixpath.dirname(rel_path) or "."
    parent_dirs = parent.split("/")
    for pat in patterns:
        if pat.exclusion != matched:
            continue
        match = bool(pat.regex.match(rel_path))
        if not match and parent != ".":
            for i in range(len(parent_dirs)):
                if pat.regex.match("/".join(parent_dirs[: i + 1])):
                    match = True
                    break
        if match:
            matched = not pat.exclusion
    return matched


def build_context_files(tracked: Iterable[str], dockerignore_text: str) -> Set[str]:
    """构建上下文 = 树里的文件 ∩ 未被 `.dockerignore` 排除。"""
    pats = compile_patterns(read_patterns(dockerignore_text))
    return {p for p in tracked if not is_excluded(p, pats)}
