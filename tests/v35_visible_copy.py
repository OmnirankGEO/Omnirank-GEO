# -*- coding: utf-8 -*-
"""#91 · 「用户能看到的文案」抽取器 —— 四条禁词判据**共用一份**。

## 为什么要有它

`tests/test_v35_ui_audit.py` 与 `tests/test_v35_followup.py` 里有四条判据在问同一个
问题:「这个工程英文词有没有露给用户看?」它们各自写了一套「跳过注释」的近似,
四套都漏,而且漏的层次不一样。2026-09-05 实测这四条**全部是假阳**:

| 判据 | 命中 | 实际是什么 |
|---|---|---|
| `r10` visible_no_active_jsx | `IndustriesPromptsPanel` L679/775/825 | 全在 `{/* JSX 注释 */}` 里 |
| `r11` no_completed_in_visible | `ArticlesPanel` L544/553/559 | `` `失败 ${failed}` `` —— **模板插值里的变量名** |
| `r13` no_dev_jargon | `ArticlesPanel` L203-214 | 「占位」在**代码注释**里(讲 placeholder-swap 算法) |
| `w5_4` customer_no_forbidden | `BuyCredit.tsx:148` | `): QuotedSKU {` —— **返回类型注解**,词是更长标识符的一部分 |

四层缺口:`{/* */}` JSX 注释 / `${...}` 插值 / 块注释 / 标识符片段。
与本班 `#94`(SQL 列普查:注释、具名参数、docstring)和 `#107`(写入方普查:出处
字符串、正则、docstring)是**同一个病的第三个家族** ——「提到」不等于「在用」。

## 口径

「可见」= **不在注释里、不在 `${}` 插值里、不是更长标识符的一部分**。
按 Review 裁定,中文场景用「去注释源 + 包含判定」即可,不上 TS 解析。
抽取是**保行保列**的:注释与插值内容替换成等长空格,所以报出来的行号仍对得上原文。

🔴 边界(写出来,免得下一个人以为这层管住了全部):
  · 不解析 TS,`'a' + b + 'c'` 这类拼接后才可见的文案看不出来;
  · 正则字面量 `/x/` 不识别,里面的 `//` 有可能被当成行注释起点;
  · 模板字符串里嵌套 `${ {a:1} }` 这种带花括号的插值只挖到第一个 `}`。
  这三档都会让判据**偏松**(漏报),不会偏严。要更严就得上 parser。
"""
from __future__ import annotations

import re

__all__ = ["strip_comments", "blank_interpolations", "visible_source", "visible_hits"]


def strip_comments(src: str) -> str:
    """把 `//` / `/* */` / `{/* */}` 的**内容**换成等长空格,保留行数与列位置。

    必须跟踪字符串状态 —— 否则 `toast.error('https://x')` 里的 `//` 会被当成注释起点,
    把整行后半截抹掉,判据就此对那一行失明(**偏松**,而且没人会发现)。
    """
    out = list(src)
    i, n = 0, len(src)
    state = None          # None | 'line' | 'block' | "'" | '"' | '`'
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state is None:
            if c == "/" and nxt == "/":
                state = "line"; out[i] = out[i + 1] = " "; i += 2; continue
            if c == "/" and nxt == "*":
                state = "block"; out[i] = out[i + 1] = " "; i += 2; continue
            if c in "'\"`":
                state = c; i += 1; continue
            i += 1; continue
        if state == "line":
            if c == "\n":
                state = None; i += 1; continue
            out[i] = " "; i += 1; continue
        if state == "block":
            if c == "*" and nxt == "/":
                out[i] = out[i + 1] = " "; state = None; i += 2; continue
            if c != "\n":
                out[i] = " "
            i += 1; continue
        # 字符串内
        if c == "\\":
            i += 2; continue
        if c == state:
            state = None
        i += 1
    return "".join(out)


_INTERP = re.compile(r"\$\{[^{}]*\}")


def blank_interpolations(src: str) -> str:
    """把 `${...}` 内部换成等长空格 —— 那是变量名,不是给人看的字。

    `` `跳过 ${skipped} 失败 ${failed}` `` 的可见文本只有中文;
    不挖空的话 `failed` 会被判成「文案里露了英文 status enum」(r11 实测假阳)。
    """
    return _INTERP.sub(lambda m: "${" + " " * (len(m.group(0)) - 3) + "}", src)


def visible_source(src: str) -> str:
    """保行保列的「可见文案近似源」。四条判据都从这里取源。"""
    return blank_interpolations(strip_comments(src))


_ASCII_WORD = re.compile(r"[A-Za-z0-9_]")


def _is_identifier_fragment(line: str, start: int, end: int) -> bool:
    """词紧邻 **ASCII** 词字符 ⇒ 它是更长标识符的一部分,不是文案。

    🔴 只认 ASCII:`\\w` 在 Python 里连中文也匹配,用它会把
    `'您的SKU额度'` 这种**真违规**一起滤掉(偏严,把真阳杀掉)。
    实测这条挡的是 `function toQuotedSKU(...): QuotedSKU {` 的**返回类型注解** ——
    原有规则挡了 `.X` / `X:` / `X(` / `<X` 四种位置,唯独漏了这一种。
    """
    if start > 0 and _ASCII_WORD.match(line[start - 1]):
        return True
    if end < len(line) and _ASCII_WORD.match(line[end]):
        return True
    return False


def visible_hits(src: str, word: str) -> list[tuple[int, str]]:
    """返回 `[(行号, 原文行)]` —— 该词出现在**可见**位置的行。

    行号与原文对齐(抽取保行保列),报文里给的是**原文**行,便于人直接去看。
    """
    raw_lines = src.split("\n")
    scan_lines = visible_source(src).split("\n")
    hits: list[tuple[int, str]] = []
    in_type_block = False
    depth = 0
    for i, line in enumerate(scan_lines, 1):
        # interface / type 体内全是字段名,不是文案
        if re.search(r"^\s*(export\s+)?(interface|type)\s+\w+", line):
            in_type_block = True
            depth = 0
        if in_type_block:
            depth += line.count("{") - line.count("}")
            if depth <= 0 and "}" in line:
                in_type_block = False
                depth = 0
            continue
        if word not in line:
            continue
        stripped = line.strip()
        if (stripped.startswith(("import ", "export type ", "type ", "interface "))
                or "v35Terminology" in line or "from '@/lib/v35" in line):
            continue
        for m in re.finditer(re.escape(word), line):
            s, e = m.start(), m.end()
            before, after = line[:s], line[e:]
            if _is_identifier_fragment(line, s, e):
                continue
            if re.search(r"[.?]$", before):            # foo.X / foo?.X 字段访问
                continue
            if re.match(r"\s*[?]?:", after):           # X: type 字段定义
                continue
            if re.match(r"[\[\]\.,;:)<>]", after) and not after.lstrip().startswith(">"):
                if not re.search(r">\s*$", before):    # 但 `>X<` 是 JSX 文本,算可见
                    continue
            if re.match(r"\w*\(", after):              # rechargeSKUs( 函数名/调用
                continue
            if re.search(r"<\s*$", before):            # <X 类型泛型
                continue
            hits.append((i, raw_lines[i - 1].rstrip()))
            break
    return hits
