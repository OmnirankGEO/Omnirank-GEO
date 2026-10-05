"""#156 底线判据 · prestart 的 fail-fast **不许退化**。

## 为什么这是底线,而不是「干净库能建起来」

实测(2026-09-08 census,`WO156_CLEAN_DB_MIGRATION_CENSUS_C_2026-09-08.txt`):
干净库上 manifest **首跑 63 支失败 / 二跑 28 支**。所以「新库能建起来」今天做不到,
写成断言就是锁住一个坏状态,还会在它被修好那天反过来变红。

真正要守住的是**性质差**:

  · `server.py::_run_sql_migrations` 记完日志**继续走** ⇒ 半迁移的库,**静默**;
  · `scripts/prestart.py`(生产/灾备唯一入口)**任何一支失败即非零退出** ⇒
    deploy 在切流之前 abort。

⇒ 生产今天拿不到半迁移的库,靠的**就是** prestart 这条 fail-fast。
   哪天有人为了「让新库先跑起来」把它改成 try/except 继续走,
   那一刻起坏 schema 会被放到线上,而**屏幕上一切正常**。
   本文件就是拦这一下。
"""

from __future__ import annotations

import ast
import io
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
PRESTART = REPO / "scripts" / "prestart.py"


def _tree():
    return ast.parse(io.open(PRESTART, encoding="utf-8").read())


def test_applying_a_migration_never_swallows_its_error():
    """🔴 `_apply` 里不许有 try/except 把 SQL 错误吞掉。

    毒:给 `cur.execute(sql)` 套一层 `except Exception: log; return` ⇒ 本条红。
    吞掉之后 prestart 会**报成功**,deploy 照常切流,而库是半迁移的。
    """
    fn = next(n for n in ast.walk(_tree())
              if isinstance(n, ast.FunctionDef) and n.name == "_apply")
    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    for h in handlers:
        body = ast.unparse(ast.Module(body=h.body, type_ignores=[]))
        assert ("raise" in body or "sys.exit" in body), (
            "_apply 里有个 except 没有再抛也没退出 —— 迁移失败会被吞掉:\n%s"
            % body[:300])


def test_the_migration_loop_is_not_wrapped_in_a_continue_on_error():
    """🔴 manifest 循环外层不许出现「记日志然后 continue」。

    这正是 `server.py` 那条路的写法 —— 它是 dev 的权宜,
    **不能**被搬进 prestart:那会把「生产不会拿到半迁移的库」这条性质删掉。
    """
    src = io.open(PRESTART, encoding="utf-8").read()
    tree = ast.parse(src)
    loops = [n for n in ast.walk(tree) if isinstance(n, ast.For)
             and "MIGRATIONS" in ast.unparse(n.iter)]
    assert loops, "找不到 manifest 循环 —— 分母塌了,不是通过"
    for loop in loops:
        for node in ast.walk(loop):
            if isinstance(node, ast.Try):
                for h in node.handlers:
                    body = ast.unparse(ast.Module(body=h.body, type_ignores=[]))
                    assert "continue" not in body, (
                        "manifest 循环里出现了 continue-on-error —— "
                        "prestart 会带着坏 schema 报成功:\n%s" % body[:300])


def test_prestart_exits_non_zero_on_failure():
    """🔴 顶层失败处置必须是**非零退出**,不是 return。

    `return` 会让容器编排以为 prestart 成功。
    """
    src = io.open(PRESTART, encoding="utf-8").read()
    assert "sys.exit(" in src, "prestart 没有任何非零退出路径"
    tree = ast.parse(src)
    # 顶层 except 里必须有 sys.exit
    tops = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
            and any((getattr(h.type, "id", None) or "") in ("Exception", "BaseException")
                    for h in n.handlers)]
    assert tops, "prestart 没有顶层异常处置 —— 分母塌了"
    assert any("sys.exit" in ast.unparse(ast.Module(body=h.body, type_ignores=[]))
               for n in tops for h in n.handlers), (
        "顶层 except 没有非零退出 —— 编排会以为 prestart 成功")


def test_the_bootstrap_runs_before_the_loop_and_is_not_a_substitute_for_it():
    """引导排在循环之前(#156 那笔),且**没有**因此把循环的 fail-fast 放松。

    两件事要一起成立:引导让 034 有机会跑;fail-fast 保证跑不过就别上线。
    """
    src = io.open(PRESTART, encoding="utf-8").read()
    boot = src.find("init_mhz_tables()")
    loop = src.find("for rel in MIGRATIONS")
    assert boot != -1 and loop != -1 and boot < loop, (boot, loop)


def test_the_census_file_is_recorded():
    """债务台账在,且记的是**实测**不是估计。

    ⚠️ 只断言「文件在 + 有关键数字」;它是外部证据文件,
       内容由那次实测产出,不由本判据生成 —— 否则就是自己给自己作证。
    """
    census = pathlib.Path(r"C:/AI-Test/WO156_CLEAN_DB_MIGRATION_CENSUS_C_2026-09-08.txt")
    if not census.is_file():
        pytest.skip("census 文件不在本机(它是交付物,不在仓内)")
    text = census.read_text(encoding="utf-8", errors="ignore")
    assert "首跑失败 63 支" in text and "二跑仍失败 28 支" in text, text[:300]
