"""判据① · `publish_records` 写入方 census 完备性闸(WO §3 第 1 条)。

本工单的整条推论都建立在一句话上:**`publish_records` 的活写入方只有浏览器扩展
那一条**。如果哪天多出一条写入方(尤其是又一条"客户端自报"),而这份 census 没有
当场变红,那么"自报已收口"的结论就自动过期,却没人知道。

🔴 [WO_273 · 2026-09-23] 浏览器插件后端整体退役:那条自报写入方与两个死写入函数
   (连同它们所在的两个模块)一并删除。此后**自报写入方为零**,生产事实写入方只剩服务端核实器;
   表与存量行保留,读者照旧读。台账与下面两格按此同步(见各处说明)。

所以这份锁不是"跑一遍看看",它是**分母自证**:
  · `test_declared_writers_match_repo` —— 仓里的写入方集合必须与下表逐条相等;
  · `test_census_scanner_catches_a_forged_writer` —— 往临时树里塞一条伪造写入方,
    扫描器必须抓到。没有这一条,上一条全绿也可能只是因为扫描器什么都扫不到
    (本仓栽过"锁全绿是因为夹具是空的")。

🔴 结构锚取全集,不用函数名/符号名:`INSERT INTO` / `UPDATE` / `DELETE FROM` +
   表名。用符号名会漏掉直接写裸 SQL 的路径 —— 而裸 SQL 恰恰是本表的常态。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import ast
import warnings

ROOT = Path(__file__).resolve().parents[2]
TABLE = "publish_records"

#: 结构锚。动词与表名之间用 `\s+`,因为本仓的 SQL 常把列清单换行写在表名之后。
#: 🔴 本文件内一律不出现「动词 + 表名」的裸串(注释里也不行) —— 否则这份锁会把
#:    自己扫成写入方。想举例请像下面 `test_census_scanner_catches_a_forged_writer`
#:    那样运行时拼接。
WRITE_ANCHOR = re.compile(
    r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+" + TABLE + r"\b",
    re.IGNORECASE,
)

SCAN_SUFFIXES = (".py", ".sql")
#: 排除项逐条写明理由 —— 不许出现"顺手多排一个"。
EXCLUDED_DIR_NAMES = {
    ".git",           # 版本库内部对象,不是源码
    "node_modules",   # 第三方
    "frontend",       # 前端不直连 DB
    "__pycache__",
}
#: 生产库 schema 快照(pg_dump 产物)只是夹具素材,不是写入方。
EXCLUDED_FILE_MARKERS = ("prod_schema",)


def _iter_scan_files(root: Path):
    for path in root.rglob("*"):
        if path.suffix.lower() not in SCAN_SUFFIXES or not path.is_file():
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        if any(marker in path.name for marker in EXCLUDED_FILE_MARKERS):
            continue
        yield path


#: 真正会把 SQL 送进库的被调方 —— 「写入方」的判定落在**被调方是谁**,不是「文中有没有这串」。
_EXECUTORS = {"execute", "executemany", "executescript", "execute_values", "execute_batch"}


def _py_writer_hits(text: str) -> int:
    """`.py` 文件里的**真**写入点:DML 字符串常量**流向 execute 家族**。

    🔴 [#107] 原来这里是 `WRITE_ANCHOR.findall(text)` —— 扫原文。
       它把三种「**提到**」当成了「**在写**」,实测三处全是假阳:
         · `services/defensive_geo/publish/publish_settlement.py:207`
           是一个**出处字符串**:`RawStateRow("<插件死写入模块>:217 INSERT INTO …", …)`(那个模块 WO_273 已删,出处串也已改写)
           —— 它如实记录了真写点在哪,于是被当成了嫌疑人;
         · `scripts/test_self_publish_in_published_list.py:90` 是**搜这条 INSERT 的正则**;
         · `scripts/defgeo_census/migration_dml_census.py:17` 是 **docstring**。

       换成 AST 也**不够**:那个出处字符串**确实**在一个 `ast.Call` 里
       (`RawStateRow(...)`),按「Call 里的 SQL 常量」判照样命中。
       区分点是**被调方**:`RawStateRow` 是数据结构构造器,`cursor.execute` 才写库。

    同族三层假阳(#94 实测,改这里时照这三层查):
       自由 tokenize / 具名参数 `x => v` / 注释(Python docstring 与 SQL `--` 两种)。
    """
    try:
        # 仓里有文件带非法转义序列,ast.parse 会喷 SyntaxWarning ——
        # 那是被扫文件的事,不是本普查器的读数,别让它污染判据输出。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except SyntaxError:
        return 0
    # 排除 docstring:模块/类/函数的首个字符串表达式是**说明**不是代码
    docs = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and fn.body:
            b0 = fn.body[0]
            if isinstance(b0, ast.Expr) and isinstance(b0.value, ast.Constant) \
                    and isinstance(b0.value.value, str):
                docs.add(id(b0.value))

    def _dml_strings(node: ast.AST) -> int:
        n = 0
        for x in ast.walk(node):
            if id(x) in docs:
                continue
            if isinstance(x, ast.Constant) and isinstance(x.value, str):
                # SQL 注释里的动词不算代码(`--` 行注释 / `/* */` 块注释)
                s = re.sub(r"/\*.*?\*/", " ", x.value, flags=re.S)
                s = re.sub(r"--[^\n]*", " ", s)
                if WRITE_ANCHOR.search(s):
                    n += 1
            elif isinstance(x, ast.JoinedStr) and WRITE_ANCHOR.search(ast.unparse(x)):
                n += 1
        return n

    hits = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func.attr if isinstance(node.func, ast.Attribute) else (
            node.func.id if isinstance(node.func, ast.Name) else None)
        if callee not in _EXECUTORS:
            continue
        for arg in list(node.args) + [k.value for k in node.keywords]:
            hits += _dml_strings(arg)
    return hits


def census_writers(root: Path) -> dict[str, int]:
    """返回 {仓内相对路径: 该文件里的**真**写入点数}。

    `.py` 走 AST + 被调方判定;`.sql` 文件里出现 DML 就是写入方(它本身就是被执行的东西)。
    """
    found: dict[str, int] = {}
    for path in _iter_scan_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = (_py_writer_hits(text) if path.suffix.lower() == ".py"
                else len(WRITE_ANCHOR.findall(text)))
        if hits:
            found[path.relative_to(root).as_posix()] = hits
    return found


# ---------------------------------------------------------------------------
# 已知写入方台账 —— 每条都标可信级(WO §1 第 1 问)
# ---------------------------------------------------------------------------
# 可信级口径:
#   browser_self_report  被测方(浏览器扩展)自己签发,**不可直接当终态事实**
#   server_verified      服务端探针核实后写入,唯一能升终态事实的一档
#   dead_code            仓内零调用方(census 时实证),留着但不构成事实来源
#   migration            一次性迁移 SQL
#   test_or_validation   夹具 / 校验脚本,不进生产事实链
DECLARED_WRITERS: dict[str, str] = {
    # 🔴 [WO_273 · 2026-09-23] 这里原有两条:插件后端(browser_self_report)与插件的死写入模块
    #    (dead_code)。两个文件随插件后端整体删除 —— 台账同步删,不是改了可信级。
    #    它们哪天回来,`test_declared_writers_match_repo` 会把它当「未登记写入方」拦下。
    "services/publication_url_verifier.py": "server_verified",
    "scripts/migration_geo_article_v14_2026_07_19.sql": "migration",
    "scripts/migration_publish_records_url_verification_2026_08_19.sql": "migration",
    "scripts/validate_geo_article_v14_pg16.py": "test_or_validation",
    "tests/publish_center_p1/test_publish_history_integration.py": "test_or_validation",
    "tests/publish_dispatch_industry_2026_08_17/test_publish_stats_third_source.py":
        "test_or_validation",
    "tests/article_self_report_2026_08_19/test_self_report_not_terminal_pg16.py":
        "test_or_validation",
    "tests/article_self_report_2026_08_19/test_dual_axis_user_surfaces_pg16.py":
        "test_or_validation",
    "tests/article_self_report_2026_08_19/test_funding_path_excludes_self_report_pg16.py":
        "test_or_validation",
    # [并车 2026-08-20 · 36 班] 图文包随树并入的**测试夹具**写入方:
    # 自建一次性 PG16 库里造行来驱动投影判据,不触生产事实链。
    "tests/geo_image_note_2026_08_17/test_wp7_projection_pg16.py": "test_or_validation",
    # 🔴 [#107 · 2026-09-05] 这里原本还有一条
    #    `tests/geo_image_note_2026_08_17/test_r3_p07_fences_pg16.py` —— **删掉了**。
    #    它根本不写这张表:文件里那串是**读结构用的正则**
    #    `r"(?i)\b(from|into|update|join)\s+publish_records\b"`,
    #    被旧的**文本版**扫描器当成了写入方,还就此**登进了台账**。
    #    AST 版实测该文件 `execute(...)` 里带 DML 的 **0 处**。
    #    ⇒ 一个检测器的假阳,一旦被登记,就变成了「已知事实」;
    #      「补一行白名单」正是把假阳固化成事实的动作。
}
# 🔴 本文件(census 锁自身)**不在**台账里,也不许进 —— 它靠「锚点全部运行时拼接」
#    做到自己不触发扫描器。哪天有人在这里写下裸串,test_declared_writers_match_repo
#    会当场变红,那是**对的**:锁自身混进分母就等于给白名单开后门。

#: 唯一允许的"生产事实写入方"集合。多一个就必须重做整份调查。
PRODUCTION_FACT_WRITERS = {
    path for path, trust in DECLARED_WRITERS.items()
    if trust in {"browser_self_report", "server_verified"}
}


def test_declared_writers_match_repo():
    """仓内写入方集合 == 台账。多一条、少一条都红。"""
    actual = set(census_writers(ROOT))
    declared = set(DECLARED_WRITERS)
    missing = declared - actual
    unexpected = actual - declared
    assert not unexpected, (
        f"出现未登记的 {TABLE} 写入方:{sorted(unexpected)}。\n"
        "这不是「补一行白名单」就完事的事 —— 新写入方必须先定可信级,"
        "再确认它会不会绕过服务端核实直接产生终态事实。"
    )
    assert not missing, (
        f"台账里这些写入方在仓里找不着了:{sorted(missing)}。"
        "被删/被改名的话,台账要同步,否则 census 会变成一份过期清单。"
    )


def test_no_browser_self_report_writer_remains():
    """自报写入方的分母 —— [WO_273 起] 为零。

    原格 `test_only_one_browser_self_report_writer` 断言「有且仅有插件后端一条」;插件后端
    整体退役后这一档清零。分母变了,断言跟着改成「零」,守的命题不变:
    自报入口是否存在、有几条,必须是台账里的一个确定值。
    与 `test_declared_writers_match_repo` 合起来:仓里冒出任何新写入方都红,冒出的若是自报,
    要先登记成 browser_self_report 才能过台账那一格 —— 而这一格会接着红。
    """
    self_reported = [p for p, t in DECLARED_WRITERS.items() if t == "browser_self_report"]
    assert self_reported == [], self_reported
    assert PRODUCTION_FACT_WRITERS == {"services/publication_url_verifier.py"}, PRODUCTION_FACT_WRITERS


# [WO_273 · 2026-09-23 肯定式退役] 原格 `test_dead_writers_still_have_zero_callers`:钉住插件那个死写入模块里
#   两个写入函数零调用方(一旦被接上就是第二条自报入口)。两个函数连同所在模块随插件后端整体删除 ——
#   已经没有可被「接上」的东西。接替:它们(或任何同形写入)回到仓里,`test_declared_writers_match_repo`
#   当场当成未登记写入方拦下。


def test_census_scanner_catches_a_forged_writer(tmp_path):
    """🔴 判别力自证:伪造一条写入方,扫描器必须抓到。

    没有这一条,上面的"集合相等"可能只是因为扫描器压根没扫到东西 ——
    恒真的判据和恒假的一样废。
    """
    forged = tmp_path / "services" / "sneaky_writer.py"
    forged.parent.mkdir(parents=True)
    # 🔴 锚点在这里**拼**出来,不写字面量:本文件自己若含裸串「INSERT+INTO+表名」,
    #    census 会把这份锁自身当成写入方。正确处置是收紧匹配面(让自己不触发),
    #    **不是**把自己加进白名单 —— 那等于给「提一嘴表名就能进名单」开后门。
    for verb in ("INSERT INTO", "UPDATE", "DELETE FROM"):
        forged.write_text(f'cur.execute("{verb} {TABLE} ...")\n', encoding="utf-8")
        assert "services/sneaky_writer.py" in census_writers(tmp_path), verb


def test_scanner_does_not_fire_on_mere_mention(tmp_path):
    """反向对照:只是**提到**表名(读取 / 注释 / 文档)不算写入方。

    没有这条,扫描器可能是"见 publish_records 就报",那样集合相等纯属巧合。
    """
    innocent = tmp_path / "services" / "reader.py"
    innocent.parent.mkdir(parents=True)
    innocent.write_text(
        f'# {TABLE} 是自助发布记录表\n'
        f'cur.execute("SELECT id FROM {TABLE} WHERE user_id=%s")\n',
        encoding="utf-8",
    )
    assert census_writers(tmp_path) == {}, "读取/提及被误判成写入方"


@pytest.mark.parametrize("trust", sorted(set(DECLARED_WRITERS.values())))
def test_every_trust_level_is_from_the_closed_set(trust):
    assert trust in {
        "browser_self_report", "server_verified", "dead_code",
        "migration", "test_or_validation",
    }


def test_the_discriminator_is_the_callee_not_the_text(tmp_path):
    """🔴 [#107] 判别点是**被调方**,不是「文里有没有这串」。

    实测三处假阳,都是「提到」不是「在写」:
      · 出处字符串塞进数据结构构造器(`publish_settlement.py:207` 的 `RawStateRow(...)`);
      · 搜这条 SQL 的**正则**(`test_self_publish_in_published_list.py:90`);
      · **docstring**(`scripts/defgeo_census/migration_dml_census.py:17`)。

    ⇒ 只改成 AST **不够**:那个出处字符串确实在一个 `ast.Call` 里,
      按「Call 里有 SQL 常量」判照样命中。区分它俩的只有**被调方是谁**。
    """
    d = tmp_path / "services"
    d.mkdir(parents=True)
    stmt = f"INSERT INTO {TABLE} (a) VALUES (1)"  # 运行时拼 —— 理由同上面的伪造臂

    # ① 同一串交给**非** execute 家族的构造器 ⇒ 不是写入方
    (d / "provenance.py").write_text(
        f'RawStateRow("retired/dead_writer.py:217 {stmt}", "dead_code")\n', encoding="utf-8")
    # ② 搜这条 SQL 的正则 ⇒ 不是写入方
    (d / "searcher.py").write_text(f're.compile("(?i){stmt}")\n', encoding="utf-8")
    # ③ docstring ⇒ 不是写入方
    (d / "doc.py").write_text(f'"""本脚本普查 {stmt} 一类的 DML。"""\n', encoding="utf-8")
    assert census_writers(tmp_path) == {}, (
        "「提到」被判成了「在写」—— 判别点必须落在被调方(execute 家族),"
        "不是文本里有没有这串。")

    # ④ 同一串走 execute ⇒ 必须抓到。
    #    🔴 没有这一格,上面那个空集可能只是因为扫描器什么都扫不到 ——
    #       「滤得准」与「瞎了」在读数上同形,只有这一格能把两者分开。
    (d / "real.py").write_text(f'cur.execute("{stmt}")\n', encoding="utf-8")
    assert set(census_writers(tmp_path)) == {"services/real.py"}, (
        "同一串走 execute 却没被抓到 —— 收紧判别面时把真阳一起杀掉了。")


def test_sql_files_still_scan_by_text(tmp_path):
    """`.sql` 文件本身就是被送进库执行的东西,没有「被调方」可判 —— 保持文本判定。

    钉住这一格,是因为上面那次收紧只该作用在 `.py` 上;
    顺手把 `.sql` 也改成 AST 判定的话,两条 migration 写入方会当场消失而没人发现。
    """
    d = tmp_path / "scripts"
    d.mkdir(parents=True)
    (d / "m.sql").write_text(f"UPDATE {TABLE} SET url_verified = true;\n", encoding="utf-8")
    assert set(census_writers(tmp_path)) == {"scripts/m.sql"}
