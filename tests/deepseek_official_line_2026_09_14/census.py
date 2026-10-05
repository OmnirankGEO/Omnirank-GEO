# -*- coding: utf-8 -*-
"""WO_206 · 官方线模型名普查(E5 的分母,也是改动前的地图)。

🔴 为什么不照工单那 11 条做:工单点名 11 条,而全仓碰 `api.deepseek.com` 的
   有 ~50 个文件。**别人给的份数不是分母** —— 先自己机械枚举,再按后果分域。

🔴 分域的第一刀是**供货线**,不是文件名:
   `deepseek-v4-flash` 在**百炼/DashScope** 上是另一家的模型 ID,
   DeepSeek 官方改名**不改**百炼那边的 ID。把两条线的名字一起改,
   等于把百炼那条链改成一个它不认识的名字 —— 而它只会在运行时报"模型不存在",
   或者更糟:静默落到别的档。

🔴 第二刀是**用途**:本单范围只有 (a) GEO 干活模型。
   (b) 被监测引擎(把 DeepSeek 当搜索引擎查)不动 —— 换它等于换被测对象;
   (c) 社媒 / IP 线不归本窗(板块边界,元指令 12)。

═══════════════════════════════════════════════════════════════════════
🔴🔴🔴 2026-09-14 **返工:分母锚重写**。第一版的 `_EMIT` 是**形状猜测** ——
   它只认 `model=` / `"model":` / `default_model=` / `DEFAULT_*MODEL=` 这几种写法。
   拿「引号里以 `deepseek-` 开头的字面量」当上界复量:命中 135 处,
   而**生产代码里另有 126 处它从没看见**,其中确有真发出点:

     · `services/placement_service.py:945`   api.deepseek.com + v4-flash(位置参数)
     · `tools/pricing_llm_assessor.py:229`   同上,走官方 key 池 failover
     · `writing/llm_providers.py:40`         `model_id="deepseek-chat"`,api_url 官方
     · `agents/provider_config.py:38`        `OpenAIChatModel("deepseek-chat", ...)`
     · `services/flywheel_judgment.py:39`    `("deepseek", "deepseek-v4-flash")`
     · `agents/report_enhancement_agent.py:175` `or "deepseek-chat"` 兜底

   漏掉的形状:`model_id=` `"model_id":` `model_name=` `MODEL_NAME=` `MODEL_MAIN=`
   `default_model_key=` `: str = "…"` `or "兜底"`、以及**位置参数**(46 处)。

   教训与本仓 09-13 那条同款:**先列形态,再写锚**。所以现在的做法是 ——
   枚举**所有像模型名的字面量**(不预设它左边长什么样),
   再由 `classified.py` 逐条人读,标 kind 说明它到底是不是"发出去的名字"。
   「这不是发出点」必须是一句**签了字的话**,不能是锚没覆盖造成的**沉默**。
═══════════════════════════════════════════════════════════════════════
"""
import ast
import io
import os
import re
import subprocess

#: 🔴 分母锚:**所有**像模型名的字面量。`deepseek` 后面必须跟 `-` 或 `/` ——
#:   这样排掉 provider 名 `"deepseek"` 与环境变量名 `"DEEPSEEK_API_KEY"`,
#:   但不排掉任何一种**写法**。左边长什么样一律不问,那是人读的事。
_MODEL_LITERAL = re.compile(
    r'["\'](deepseek[-/][A-Za-z0-9._/\-]*)["\']', re.IGNORECASE)

#: 🔴 分母的**第二半**:常量引用。
#:   本单的整个动作就是把字面量换成 `DEEPSEEK_OFFICIAL_FLASH` —— 如果分母只数字面量,
#:   那么每改一处分母就少一处,改到最后这张表会**自己空掉**,而"空表全绿"看起来
#:   和"全部覆盖"一模一样。所以常量引用也算一处「这里出现了一个模型名」。
#:   🔴 这里用**零宽断言**划词边界,不用那个反斜杠加 b 的写法:
#:      本仓有过改写脚本把它吃成退格 0x08、于是否定断言恒真恒绿的先例(#195 全仓四处)。
#:      写这条注释时我**当场又中了一次**(heredoc 把它变成了一个真的 0x08 字符),
#:      所以本包补了一条控制字符自检,见 test_no_control_chars_in_the_instrument。
_CONST_VALUES = {
    "DEEPSEEK_OFFICIAL_FLASH": "deepseek-flash",
    "DEEPSEEK_OFFICIAL_PRO": "deepseek-v4-pro",
}
_CONST_REF = re.compile(
    r"(?<![A-Za-z0-9_])(DEEPSEEK_OFFICIAL_FLASH|DEEPSEEK_OFFICIAL_PRO)(?![A-Za-z0-9_])")

#: 常量的**定义处**与 import 行不算引用 —— 它们不是"这里要发一个模型名",
#: 是"把这个名字搬过来"。定义处单独在 classified.py 里签了字。
_CONST_HOME = "config/deepseek_models.py"


#: 只作**给人看的提示**:这一处长得像不像"经典的发出写法"。
#: 🔴 它**不再**参与筛选 —— 第一版正是拿它当筛子,才把分母缩掉了 126 处。
_CLASSIC_EMIT_SHAPE = re.compile(
    r'(?:"model"\s*:\s*|model\s*=\s*|MODEL\s*=\s*|default_model\s*=\s*'
    r'|model_name"\s*:\s*|DEFAULT_\w*MODEL\s*=\s*)["\']([a-zA-Z0-9._\-]+)["\']')

#: 供货线判别:按**同文件里出现的网关/客户端**认,不按模型名猜。
_OFFICIAL_MARKS = ("api.deepseek.com", "deepseek_key_pool",
                   "adeepseek_post_with_failover", "DEEPSEEK_API_KEY")
_DASHSCOPE_MARKS = ("dashscope", "DASHSCOPE", "百炼",
                    "aliyuncs.com", "compatible-mode")
_OPENROUTER_MARKS = ("openrouter", "OPENROUTER")

#: 用途分域(按路径)。本单只动 `geo_work`。
_SOCIAL = ("api/social_", "scripts/product/social_",  # 社媒工具包目录已随开源 E3 B2 整删,从分域表去掉
           "api/advisor_api.py", "advisors/")  # 人设 router 随 E3 删,从分域表去掉
_MONITORED_ENGINE = ("tools/monitoring/batch_monitor.py",)
_NOT_PRODUCTION = ("scripts/", "tests/")


def _classify_line(path: str) -> str:
    p = path.replace("\\", "/")
    if any(p.startswith(x) or ("/" + x) in p for x in _NOT_PRODUCTION):
        return "not_production"
    if any(x in p for x in _MONITORED_ENGINE):
        return "monitored_engine"
    if any(x in p for x in _SOCIAL):
        return "social"
    return "geo_work"


def _provider_of(text: str) -> str:
    has_official = any(m in text for m in _OFFICIAL_MARKS)
    has_dash = any(m in text for m in _DASHSCOPE_MARKS)
    has_or = any(m in text for m in _OPENROUTER_MARKS)
    if has_official and not (has_dash or has_or):
        return "official"
    if has_official:
        return "mixed"          # 同一个文件里两条线都在 —— 要逐行看
    if has_dash:
        return "dashscope"
    if has_or:
        return "openrouter"
    return "unknown"


def _is_import(line: str) -> bool:
    """[已弃用] 按行首判 import —— **看不见多行 import 的续行**。

    保留定义是因为它曾是唯一的判别法;真正在用的是 `import_line_numbers()`。
    """
    t = line.strip()
    return t.startswith("from ") or t.startswith("import ")


def import_line_numbers(text: str) -> set:
    """这个文件里**属于 import 语句**的全部行号(含多行 import 的每一行)。

    🔴 [WO_221-c1' 移植] 原来按行首判 `from ` / `import `,于是

            from config.deepseek_models import (
                DEEPSEEK_OFFICIAL_FLASH,      <- 这一行不以 from 开头
            )

        的续行被当成常量**引用**数进分母,表现为「这些模型名没签过字」。
        实测:220/221 两单新加的多行 import 让本包凭空多出 2 条待分类行。

    🔴 同一个盲区**两个仪器都有**:`tests/model_line_flash_census_2026_09_15/census.py`
        先在 WO_221-c1 被两侧冻结逼出来,这里是移植同一个修法。
        仪器是抄出来的,**缺陷也跟着抄**;修一个要顺手查另一个。
    """
    try:
        tree = ast.parse(text)
    except Exception:
        return set()
    lines = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for i in range(n.lineno, (n.end_lineno or n.lineno) + 1):
                lines.add(i)
    return lines


def is_comment_line(line: str) -> bool:
    """这一行是不是**纯注释**。

    🔴 这是本文件里唯一被允许的"自动判断",因为它是**语法**问题不是**语义**问题:
       一行 strip 完以 `#` 开头,它就不会被执行。供货线那种需要读懂上下文的判断,
       本仓实测会产出自信的错答案(见 classified.py 顶部),一律人读。
       注意:它判不了三引号 docstring 的**中间行** —— 那几行仍由人读兜底。
    """
    return line.strip().startswith("#")


def tracked_python_files(root="."):
    """分母 = `git ls-files`,**不是**我手写的清单。"""
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=root,
                         capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode("utf-8", "replace"))
    return [l for l in out.stdout.decode("utf-8").splitlines() if l.strip()]


def nearest_provider(lines, idx, window=120):
    """从第 idx 行**往上找最近的网关标记**,给出这一行到底发给谁。

    🔴 只是**提示**。分类以 `classified.py` 的人读为准 —— 这个启发式
       抽核时被骗过两次(注释里提到另一条线 / 三条线的代码彼此挨着)。
    """
    for j in range(idx - 1, max(-1, idx - window) - 1, -1):
        line = lines[j]
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""') \
                or stripped.startswith("'''"):
            continue
        code = line.split("#", 1)[0]     # 行尾注释也不算
        if any(m in code for m in _OFFICIAL_MARKS):
            return "official"
        if any(m in code for m in _DASHSCOPE_MARKS):
            return "dashscope"
        if any(m in code for m in _OPENROUTER_MARKS):
            return "openrouter"
    return "unresolved"


def left_shape(src: str, name: str) -> str:
    """字面量左边那一小段,归成一个可读的"写法"标签(给人读表用)。"""
    k = -1
    for q in ('"', "'"):
        k = src.find(q + name + q)
        if k >= 0:
            break
    if k < 0:
        return "?"
    left = src[:k].rstrip()
    m = re.search(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*$', left)
    if m:
        return m.group(1) + "="
    m = re.search(r'["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*:\s*$', left)
    if m:
        return '"' + m.group(1) + '":'
    m = re.search(r'([A-Za-z_][A-Za-z0-9_.]*)\s*\(\s*$', left)
    if m:
        return m.group(1) + "()"
    if left.endswith("or"):
        return "or 兜底"
    if left.endswith(("(", ",", "[")):
        return "位置参数/元素"
    return "其它"


def census(root="."):
    """返回 `[{path, line, model, shape, comment, provider, scope, classic}]`。

    🔴 每一条都是**一个像模型名的字面量**,不是"我认为的发出点" ——
       是不是发出点由 `classified.py` 逐条签字。
    """
    rows = []
    for rel in tracked_python_files(root):
        full = os.path.join(root, rel)
        try:
            text = io.open(full, encoding="utf-8").read()
        except Exception:
            continue
        if "deepseek" not in text.lower():
            continue
        provider = _provider_of(text)
        scope = _classify_line(rel)
        lines = text.splitlines()
        _import_lines = import_line_numbers(text)
        for i, line in enumerate(lines, 1):
            classic = {m.group(1) for m in _CLASSIC_EMIT_SHAPE.finditer(line)}
            found = [(m.start(), m.group(1)) for m in _MODEL_LITERAL.finditer(line)]
            if rel.replace("\\", "/") != _CONST_HOME and i not in _import_lines:
                found += [(m.start(), _CONST_VALUES[m.group(1)])
                          for m in _CONST_REF.finditer(line)]
            for _pos, name in sorted(found):
                per_line = provider
                if provider == "mixed":
                    per_line = nearest_provider(lines, i - 1)
                rows.append({"path": rel.replace("\\", "/"), "line": i,
                             "model": name, "provider": per_line,
                             "file_provider": provider, "scope": scope,
                             "shape": left_shape(line, name),
                             "comment": is_comment_line(line),
                             "classic": name in classic,
                             "src": line.strip()[:120]})
    # 🔴 [0913e 集成] 每行加一个**不随行号漂**的身份号:
    #    `occ` = 该 (文件, 模型名) 在该文件内按行序的第几次出现。
    #    分类表按 (path, model, occ) 查 —— 合车时别人在靠前位置加几行,
    #    行号全平移而 occ 不动;这一行的名字被改了,occ 照样对不上。
    _cnt = {}
    for r in sorted(rows, key=lambda r: (r["path"], r["line"])):
        k = (r["path"], r["model"])
        _cnt[k] = _cnt.get(k, 0) + 1
        r["occ"] = _cnt[k]
    return rows


def print_census(rows):
    by = {}
    for r in rows:
        by.setdefault((r["scope"], r["provider"]), []).append(r)
    print("模型名字面量总数: %d(其中经典发出写法 %d)"
          % (len(rows), len([r for r in rows if r["classic"]])))
    for key in sorted(by):
        scope, provider = key
        print("\n── scope=%s · provider=%s (%d 条) ──" % (scope, provider, len(by[key])))
        for r in sorted(by[key], key=lambda x: (x["path"], x["line"])):
            print("   %-58s :%-5d %-28s %s" % (r["path"], r["line"], r["model"],
                                               r["shape"]))


if __name__ == "__main__":
    print_census(census("."))
