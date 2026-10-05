"""#72 · f-string 里的 SQL 注释含裸花括号 ⇒ 被当表达式求值 ⇒ 非 admin 恒 500。

`api/m3_api.py` 的 `get_monitoring_anomaly` 里那条 SQL 是 **f-string**,
注释行写了 `{confirmed, contract}` —— Python 把它当集合字面量**求值**,
两个名字都不存在 ⇒ NameError ⇒ 整条 500。
而这一支只有 `brand_filter` 非空(= **非 admin**)才走,所以 admin 一切正常、
非 admin 恒 500 —— 两种角色读数完全相反,最容易被记成「偶发」。

🔴 它就紧挨在上一行「注释里的裸百分号也会被 psycopg2 扫」那条警告下面:
   同一族的坑,换了个符号又踩一次。**在 f-string 里,注释不是注释。**
"""
from __future__ import annotations

import ast
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[2]

#: 冻结豁免:`{len(cons)}` 是**有意插值**(生成器把数字写进产物注释),能求值、不是缺陷。
#: 🔴 名单是冻结的:新增一条要人显式改这里,而不是让门自动放行。
_ALLOWED = {("scripts/defgeo_readiness_gen.py",)}


def test_repo_wide_no_unescaped_braces_in_fstring_sql_comments():
    """🔁 全仓门 + 冻结豁免:防第三次,而不是只修这一处。

    分母 = 全仓 .py(排除 tests/frontend/node_modules)里**每一个**含 SQL 的 f-string
    的注释行,机械枚举,不手写清单。
    """
    skip = {"node_modules", ".git", "tests", "frontend", "_qa_frontend_nogo_artifacts", ".venv"}
    hits, scanned = [], 0
    for p in ROOT.rglob("*.py"):
        if any(s in p.parts for s in skip):
            continue
        scanned += 1
        try:
            src = p.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except Exception:
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.JoinedStr):
                continue
            seg = ast.get_source_segment(src, n) or ""
            up = seg.upper()
            if "SELECT" not in up and "INSERT" not in up:
                continue
            for off, ln in enumerate(seg.splitlines()):
                if "--" not in ln:
                    continue
                # 🔴 先去掉**成对转义** `{{` / `}}` 再判:`{{` 正是本单的修法,
                #    把它当违规就是分不清病和药 —— 本判据第一版就这么红过一次。
                comment = ln.split("--", 1)[1].replace("{{", "").replace("}}", "")
                if "{" in comment:
                    rel = p.relative_to(ROOT).as_posix()
                    if (rel,) in _ALLOWED:
                        continue
                    hits.append(f"{rel}:{n.lineno + off}  {ln.strip()[:70]}")
    assert scanned > 500, f"只扫到 {scanned} 个文件 —— 分母塌了,这条判据没在验东西"
    assert not hits, "f-string SQL 注释里有裸花括号(会被求值):\n  " + "\n  ".join(hits)
