"""`report_metrics.py` 读 quote 的**键名合同**
(工单 `WO_REPORT_METRICS_QUOTE_KEYS_2026-09-03.md` §3)。

## 这条锁在防什么

`compute_data_completeness` 读 `quote.get("keywords")` / `quote.get("publications_count")`,
而 `quotes` 表**没有这两列**(真实列是 `total_keywords` / `total_articles`)。
⇒ 「用 quote 兜底」那条分支**从来没执行过**,而它读起来像在工作:
`or 0` / `or []` 把「这个键不存在」和「这个值就是空」压成同一个结果,
**不报错、不告警、结果永远"合理"** —— 这是它活到今天的原因。

## 🔴 为什么不能只把键名改成 `total_keywords`

`total_keywords` 是 **integer**(计数),而 :449 那段拿到值之后要**逐个关键词分层**
(`classify_keyword_to_stratum` → brand / local / scenario)。给它一个整数:
`isinstance(5, str)` 与 `isinstance(5, list)` 都是 False ⇒ 三个计数仍然全 0。
**分支照样死,只是键名看起来合理了,下一个人不会再怀疑它。**
所以本文件除了"键必须存在"之外,还锁"**不许把 `total_keywords` 接到那个需要列表的位置**"。

(关键词列表的真身在 `confirmed_keywords` 表,接入是卡 #47,本单不做。)

## 分母来源

列名取自仓内 `tests/article_self_report_2026_08_19/prod_schema_2026-08-19.sql`
—— **它是快照,可能落后于生产**。所以:
  · 「键在不在列里」这类**存在性**判断用它足够(列只增不减是常态);
  · 「类型对不对」已由 Deploy 用真库 psql 的反斜杠-d quotes 复核过一次,本文件只把结论钉住。
"""

from __future__ import annotations

import ast
import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
METRICS = ROOT / "services" / "report_metrics.py"
DUMP = ROOT / "tests" / "article_self_report_2026_08_19" / "prod_schema_2026-08-19.sql"

def _quotes_columns() -> dict[str, str]:
    """从 dump 里取 `quotes` 的 列名 → 类型。"""
    src = io.open(DUMP, encoding="utf-8").read()
    m = re.search(r"^CREATE TABLE public\.quotes \(\n(.*?)^\);", src, re.S | re.M)
    assert m, "dump 里找不到 CREATE TABLE public.quotes —— 分母取不到,不是'表没列'"
    cols: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith(("CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK")):
            continue
        parts = line.split()
        if len(parts) >= 2:
            cols[parts[0]] = parts[1]
    assert len(cols) > 20, f"只解析出 {len(cols)} 列 —— 解析器坏了,不是'表就这么小'"
    return cols


def _quote_get_keys() -> list[tuple[str, int]]:
    """`quote.get("X")` 里的 X(走 AST,不 grep)。

    🔴 不用 grep:注释、docstring、错误文案里出现同一个词会被算进来
       （散文提及 ≠ 使用,这一条我在别的锁上栽过)。
    """
    tree = ast.parse(io.open(METRICS, encoding="utf-8").read())
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "get"):
            continue
        if not (isinstance(f.value, ast.Name) and f.value.id == "quote"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            out.append((node.args[0].value, node.lineno))
    return out


def test_positive_sample_extractors_work():
    """🔴 没有这条,「一个坏键都没有」与「提取器没抽到东西」读数相同。"""
    cols = _quotes_columns()
    assert "competitor_list" in cols and "total_keywords" in cols, (
        f"列提取器可疑:competitor_list/total_keywords 不在解析结果里(共 {len(cols)} 列)")
    keys = _quote_get_keys()
    assert keys, "AST 在 report_metrics.py 里一个 quote.get(...) 都没抽到 —— 提取器坏了"


def test_every_quote_key_exists_as_a_quotes_column():
    """`quote.get(` 用到的每个键都必须是 `quotes` 的真实列。"""
    cols = _quotes_columns()
    bad = [(k, ln) for k, ln in _quote_get_keys() if k not in cols]
    assert not bad, (
        "report_metrics.py 在读 quotes 上**不存在**的键:"
        + "、".join(f"{k}(:{ln})" for k, ln in bad)
        + "。这类读法不会报错 —— `or 0` / `or []` 把「键不存在」与「值为空」压成同一个结果,"
          "于是分支静默地永远走兜底。")


def test_integer_column_is_not_wired_into_a_list_shaped_use():
    """不许把 **integer** 列接到需要"关键词列表"的那个位置。

    这条锁的是**下一版修复本身**:把 `keywords` 改名成 `total_keywords` 看起来
    合理(键存在了、上面那条锁会变绿),但 `total_keywords` 是计数,
    分层循环拿到整数后三个计数仍然全 0 —— **bug 从可见变成不可见。**
    """
    cols = _quotes_columns()
    src = io.open(METRICS, encoding="utf-8").read()
    tree = ast.parse(src)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "raw_keywords" not in targets:
            continue
        for sub in ast.walk(node.value):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "get"
                    and isinstance(sub.func.value, ast.Name) and sub.func.value.id == "quote"
                    and sub.args and isinstance(sub.args[0], ast.Constant)):
                key = sub.args[0].value
                if cols.get(key) == "integer":
                    offenders.append(f"{key}(integer)@:{node.lineno}")
    assert not offenders, (
        f"把整数列接到了需要列表的 `raw_keywords` 上:{offenders}。"
        "分层循环会因为 isinstance 全不匹配而静默返回 0/0/0 —— "
        "键名合法了,分支依然是死的。关键词列表的真身在 confirmed_keywords(卡 #47)。")
