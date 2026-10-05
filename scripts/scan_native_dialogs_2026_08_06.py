#!/usr/bin/env python3
"""[工单 2026-08-06 §1] 原生 confirm()/alert() 全前端扫描器。

🔴 存在的理由 —— 这是**第三次**修同一个 bug(2026-05-09 / 2026-07-01 / 2026-08-06)。
2026-07-01 那次全扫除用的 grep 是:

    window\\.(confirm|alert)\\(

**裸写法 `confirm(...)` / `alert(...)` 整批漏网** —— 修了一半,漏的那一半是靠
grep 写法区分的,不是靠重要性。本扫描器的第一职责就是让「裸写法」不可能再漏。

判别力的三个难点(用 grep 做不到,所以这里是个真扫描器不是正则):

  1. **字符串/注释里的字面量**必须剥掉 —— `fixtures/longContent.ts` 里
     markdown 正文出现 "alert(" 不是调用点。
  2. **模板串里的 `${...}` 是代码**,不能连着一起剥 —— 否则真调用点会被吞掉。
  3. **局部同名绑定必须放行**:
     - `const [confirmDialog, confirm] = useConfirmDialog()` → `await confirm({...})`
       是**应用内**弹窗,正是我们要的目标形态;
     - `ProviderDowngradeWizard.tsx` 里 `const confirm = async () => {...}` 是自己的
       业务函数,跟原生弹窗无关。
     局部绑定会 shadow 全局,所以「文件里绑定了 confirm」⇒ 该文件的裸 `confirm(`
     在运行时不可能是原生的。🔴 但 `window.confirm(` 不受 shadow 影响,永远算原生。

用法:
    python scripts/scan_native_dialogs_2026_08_06.py            # 列出违规点
    python scripts/scan_native_dialogs_2026_08_06.py --json     # 机读
退出码:0 = 干净(仅剩豁免清单),1 = 有违规。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Iterator, NamedTuple

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "frontend" / "src"

# ---------------------------------------------------------------- 豁免清单
# 🔴 只有一条,而且必须写清为什么。任何新增都要在工单里论证。
#   versionPoll.ts —— 前端检测到新版本、且自动刷新已失败时的**灾难兜底**:
#   此时 React 树可能已经加载不出新 chunk,应用内弹窗本身就不可靠,
#   只有原生 confirm 一定能弹。工单 §1.6「必须不命中」明确要求保留。
_EXEMPT: dict[str, str] = {
    "frontend/src/lib/versionPoll.ts": "版本刷新灾难兜底:应用内组件此时不可靠,原生弹窗是最后一道",
}


class Hit(NamedTuple):
    path: str
    line: int
    kind: str  # 'window.confirm' / 'confirm' / 'window.alert' / 'alert'
    snippet: str


# `/` 前面出现下列 token 时,它是**正则字面量**的开头而不是除号。
# 🔴 这条不是可选优化:`WritingHall.tsx:516` 的
#     anchor.replace(/[#*`>\\[\\]()|_~-]/g, '')
# 正则里含一个反引号。不识别正则 → 剥离器把它当模板串开头 → 一口吞掉 50 行 →
# 3402 行那个真的裸 `confirm(` 静默消失。这正是「判据自己坏」的形态:
# 扫描器报绿,而 bug 还在。
_REGEX_PRECEDERS = {
    "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";", "+", "-",
    "*", "%", "~", "^", "<", ">", "\n",
}
_REGEX_PRECEDING_KEYWORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "do", "else", "case", "yield", "await", "throw",
}


def _slash_starts_regex(out_so_far: str, nxt: str) -> bool:
    """看 `/` 前后的 token,判断这是正则、除法,还是 **JSX**。

    🔴 JSX 是这里最大的坑,踩过两次:
      - `</Card>` —— `/` 前面是 `<`。把 `<` 当「正则前导符」会从这里一路吞到下一个 `/`,
        `OrganizationCenter.tsx` 的两处 `window.confirm` 就是这么消失的。
      - `<Trash2 className="h-4" />` —— `/` 前面是字符串收尾的引号、后面是 `>`。
        同样不是正则。
    所以先按 JSX 排除,再谈正则。
    """
    if nxt == ">":
        return False  # JSX 自闭合 `/>`
    j = len(out_so_far) - 1
    while j >= 0 and out_so_far[j] in " \t":
        j -= 1
    if j < 0:
        return True
    prev = out_so_far[j]
    if prev == "<":
        return False  # JSX 闭合标签 `</Foo>`
    if prev in ("'", '"', "`"):
        return False  # 字符串之后只可能是除法或 JSX,不可能是正则
    if prev in _REGEX_PRECEDERS:
        return True
    if prev.isalnum() or prev in "_$":
        # 取出完整标识符,看是不是关键字(return / typeof / ...)
        k = j
        while k >= 0 and (out_so_far[k].isalnum() or out_so_far[k] in "_$"):
            k -= 1
        return out_so_far[k + 1 : j + 1] in _REGEX_PRECEDING_KEYWORDS
    # `)` `]` 之后是除法(如 (a+b)/2、arr[0]/2);其余保守当正则
    return prev not in (")", "]")


def strip_noncode(text: str) -> str:
    """把注释、字符串**内容**、正则字面量清成空格,保留字节偏移与换行。

    🔴 保留长度是硬要求:行号必须能用 text[:idx].count('\\n') 直接算出来。
    🔴 模板串内的 `${ ... }` 视作代码保留 —— 那里面真的会有函数调用。
    🔴 正则字面量必须整体剥掉,否则里面的引号/反引号会让状态机失步(见上)。
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        # 行注释
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        # 块注释
        if ch == "/" and nxt == "*":
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            for k in range(i, min(i + 2, n)):
                out[k] = " "
            i += 2
            continue
        # 正则字面量 /.../flags —— 必须在字符串分支之前处理
        if ch == "/" and _slash_starts_regex("".join(out[max(0, i - 64) : i]), nxt):
            j = i + 1
            in_class = False
            closed = False
            while j < n:
                c = text[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "\n":
                    break  # 正则不能跨行 → 判错了,当除号处理
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    closed = True
                    break
                j += 1
            if closed:
                for k in range(i + 1, j):
                    out[k] = " "
                i = j + 1
                continue
            # 没闭合 → 它其实是除号,按普通字符继续
            i += 1
            continue
        # 单/双引号字符串
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\":
                    out[i] = " "
                    if i + 1 < n and text[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if text[i] == quote or text[i] == "\n":
                    break
                out[i] = " "
                i += 1
            i += 1
            continue
        # 模板串
        if ch == "`":
            i += 1
            while i < n:
                if text[i] == "\\":
                    out[i] = " "
                    if i + 1 < n and text[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if text[i] == "`":
                    break
                if text[i] == "$" and i + 1 < n and text[i + 1] == "{":
                    # ${...} 内是代码:整段原样保留,只做括号配对跳过
                    depth = 0
                    j = i + 1
                    while j < n:
                        if text[j] == "{":
                            depth += 1
                        elif text[j] == "}":
                            depth -= 1
                            if depth == 0:
                                break
                        j += 1
                    i = j + 1
                    continue
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            i += 1
            continue
        i += 1
    return "".join(out)


_CALL = re.compile(r"(?<![.\w$])(window\s*\.\s*)?(confirm|alert)\s*\(")

# 局部绑定的三种形态(全部在**已剥注释字符串**的代码上匹配)
def _locally_bound(code: str, name: str) -> bool:
    patterns = [
        # const [dialog, confirm] = useConfirmDialog()  /  解构里出现该标识符
        rf"(?:const|let|var)\s*\[[^\]]*\b{name}\b[^\]]*\]\s*=",
        # const confirm = ... / let alert = ...
        rf"(?:const|let|var)\s+{name}\s*[=:]",
        # function confirm(...) / async function confirm(...)
        rf"function\s+{name}\s*\(",
        # import { confirm } from ...  /  import confirm from ...
        rf"import\s+(?:\{{[^}}]*\b{name}\b[^}}]*\}}|{name})\s+from",
        # 对象解构:const { confirm } = ...
        rf"(?:const|let|var)\s*\{{[^}}]*\b{name}\b[^}}]*\}}\s*=",
        # 形参:(confirm: ...) 或 ({ confirm })  —— 保守放行
        rf"\(\s*{name}\s*:",
    ]
    return any(re.search(p, code) for p in patterns)


def scan(src_dir: Path = _SRC) -> list[Hit]:
    hits: list[Hit] = []
    for path in sorted(src_dir.rglob("*")):
        if path.suffix not in (".ts", ".tsx") or not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8")
        code = strip_noncode(raw)
        try:
            rel = path.relative_to(_ROOT).as_posix()
        except ValueError:
            # 自证会把样本写到临时目录里扫,那里算不出仓库相对路径
            rel = path.as_posix()
        bound = {n: _locally_bound(code, n) for n in ("confirm", "alert")}
        for m in _CALL.finditer(code):
            is_window = bool(m.group(1))
            name = m.group(2)
            # 🔴 window.X 不受局部 shadow 影响,永远算原生
            if not is_window and bound[name]:
                continue
            line = code[: m.start()].count("\n") + 1
            snippet = raw.splitlines()[line - 1].strip()[:120] if line - 1 < len(raw.splitlines()) else ""
            hits.append(Hit(rel, line, ("window." if is_window else "") + name, snippet))
    return hits


def offenders(src_dir: Path = _SRC) -> list[Hit]:
    return [h for h in scan(src_dir) if h.path not in _EXEMPT]


def main() -> int:
    all_hits = scan()
    bad = [h for h in all_hits if h.path not in _EXEMPT]
    exempted = [h for h in all_hits if h.path in _EXEMPT]
    if "--json" in sys.argv:
        print(json.dumps({"offenders": [h._asdict() for h in bad],
                          "exempted": [h._asdict() for h in exempted]}, ensure_ascii=False, indent=2))
    else:
        files = sorted({h.path for h in bad})
        for h in bad:
            print(f"{h.path}:{h.line}  [{h.kind}]  {h.snippet}")
        print()
        print(f"违规 {len(bad)} 处 / {len(files)} 文件;豁免 {len(exempted)} 处")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
