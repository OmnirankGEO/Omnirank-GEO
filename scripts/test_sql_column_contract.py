# -*- coding: utf-8 -*-
"""#94 · 代码里 SQL 字面量引用的列,必须在生产 schema 里存在。

## 缺陷是什么

`SELECT profile_id FROM ai_suggestions`(当时在社媒智能建议模块里,该模块已随 E3 删除)—— 生产
`ai_suggestions` 13 列里**没有** `profile_id`。这类语句在被走到时 `UndefinedColumn`,
而它在代码里长得完全正常:有表、有列名、有占位符、有 try/except。
**只有拿生产 schema 对一遍才戳得穿。**

分母 = Deploy 当天的 `pg_dump --schema-only`(513 张表),不是 08-17 那份旧快照。

## 四态,不是两态

    PASS        列在生产 schema 里
    STALE       列不在快照、但树内有 ADD COLUMN / _safe_add_column 加过它
                ⇒ **尺子过期**(快照没跟上迁移),报出来但不判红
    FAIL        列不在快照、树里也没人加过 ⇒ 真缺陷
    UNRESOLVED  解析不了(f-string / JOIN / 子查询 / SELECT 列表含函数别名 / 表不在 dump)
                ⇒ **单列计数**,不进 PASS 也不进 FAIL。
                这个数**本身就是这道门的覆盖率读数** —— 压进 PASS,覆盖率永远看起来是 100%。

## 🔴 精度是这道门的全部价值,而我第一版砸了

v1 用自由 tokenizer 得 **6397 FAIL**,基本全假阳:
字符串值 `'running'`、函数名 `EXTRACT`/`ABS`、别名 `AS count`、占位符 `%s`→`s` 全被当列名。
v2 改成只取**语法位置确定**的标识符(比较运算符左侧 / SET 左侧 / INSERT 显式列清单;
SELECT 列表仅在「纯裸标识符逗号分隔」时才取),再修三类:

| 假阳类 | 数量 | 修法 |
|---|---|---|
| 自由 tokenize 值/函数/别名 | ~6340 | 只取语法位置确定的标识符 |
| `make_interval(secs => %s)` 具名参数 | 21 | 比较符改 `=(?!>)` |
| docstring 里引用 SQL(散文 `markup = …`) | 3 | 排除模块/类/函数首个字符串表达式 |

6397 → **25**。三次都是同一个病:**检测器在「提到」与「在用」之间不分**(#107 同族)。
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CENSUS = ROOT / "scripts" / "sql_column_census.py"

#: 🔴 已知未修的 FAIL —— **冻结登记,不是白名单**。
#:    每条带坐标;新增违规不在此表 ⇒ 红。修好一条要同笔从这里删。
#:    这些是**真缺陷**(已逐个核过生产列清单与近似名),等 Review 分派归属后修。
KNOWN_FAILURES: dict[str, str] = {
    # [WO_273 · 2026-09-23] 原有一条 `publish_records.account_id`(#114 定性为插件死写入模块里
    #   两个零调用函数引用的不存在列,「删掉还是接活交 Review 定」)。Review 的答案随 WO_273 落地:
    #   连同所在模块整体删除。违规消失,按本表规矩同笔删条目(下面「无陈条目」那格本来就不许它留着)。
    # [开源 E3 · B2 · 2026-09-28] 原有一条 `brands.is_grandfather`(#114 定性为假阳:那条 SELECT 在社媒输出模式模块里,
    #   被运行时查列守卫罩着)。该模块随社媒工具包整包删除,违规连同出处消失,按本表「无陈条目」规矩同笔删条目。
}


def _census():
    spec = importlib.util.spec_from_file_location("_sqlcol94", CENSUS)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def result():
    m = _census()
    assert m.DUMP.exists(), (
        f"生产 schema dump 不在:{m.DUMP}\n"
        f"    🔴 这不是「没有违规」,是这道门**没有分母**。用 PROD_SCHEMA_DUMP 环境变量指定。")
    return m.run()


# ══ ① 分母自证:先证尺子在量东西 ═══════════════════════════════════
def test_the_denominator_is_not_empty(result):
    assert result["pass"] > 5000, (
        f"只判定出 {result['pass']} 个 PASS —— 分母塌了,先查 dump 解析与扫描目录,"
        f"别把「什么都没量到」读成「没有违规」。")


# ══ ② 主锁:没有**未登记**的 FAIL ═════════════════════════════════
def test_no_unregistered_column_reference_is_missing_from_production(result):
    seen = {f"{r['t']}.{r['c']}" for r in result["fail"]}
    new = sorted(seen - set(KNOWN_FAILURES))
    detail = {k: [f"{r['f']}:{r['l']}" for r in result["fail"] if f"{r['t']}.{r['c']}" == k] for k in new}
    assert not new, (
        "这些 SQL 引用的列**在生产 schema 里不存在**,且没有登记:\n    "
        + "\n    ".join(f"{k}  @ {detail[k]}" for k in new)
        + "\n    走到即 UndefinedColumn。二选一:①改对列名;②若是尚未上线的迁移,"
          "把 ADD COLUMN 写进树里(它会被判成 STALE 而非 FAIL);"
          "③确属已知未修 ⇒ 登记进 `KNOWN_FAILURES` 并写清生产实际列名与影响。")


def test_the_known_failure_registry_has_no_stale_entries(result):
    """🔴 反向:修好了就要从登记表里删。

    留一条指向「其实已经对了」的登记 ⇒ 登记表在说谎,
    而且下一个人会以为还有一处待修。
    """
    seen = {f"{r['t']}.{r['c']}" for r in result["fail"]}
    fixed = sorted(set(KNOWN_FAILURES) - seen)
    assert not fixed, (
        f"这些已经不再违规,但还留在 `KNOWN_FAILURES` 里:{fixed} —— 同笔删掉。")
    for k, why in KNOWN_FAILURES.items():
        assert len(why) > 20, f"{k} 的登记理由太短,等于没写"


# ══ ③ 正样本臂:门必须真的抓到已知那两处 ═══════════════════════════
def test_the_gate_actually_detects_every_registered_defect(result):
    """🔴 正样本臂 = **登记表本身**,它自维护。

    第一版把 `ai_suggestions.profile_id` 与 `brands.brand_name` 写死成正样本 ——
    然后 #114 把这两条**修好了**,这条臂当场红。
    「修复推翻了存在锁的前提」:锁本身没错,是它的前提被正确的改动作废了。
    改成「门必须抓到登记表里的每一条」之后,修一条删一条,臂跟着缩,不会假红。
    """
    seen = {f"{r['t']}.{r['c']}" for r in result["fail"]}
    missed = sorted(set(KNOWN_FAILURES) - seen)
    assert not missed, (
        f"门没抓到这些**已登记确认**的缺陷:{missed} —— 先修门,别信它给出的任何绿。")
    assert KNOWN_FAILURES, (
        "🔴 登记表空了(FAIL 归零,#114 终态)。\n"
        "    此刻这道门**失去了正样本臂** —— 它是不是还有牙,没有任何东西在证。\n"
        "    同笔改成**合成探针**:造一段引用不存在列的 SQL 喂给普查器,断言被抓到。")


# ══ ④ UNRESOLVED 必须被计数,且不许压进 PASS ═══════════════════════
def test_unresolved_is_counted_separately_and_is_the_coverage_readout(result):
    """UNRESOLVED 的数量**就是**这道门的覆盖率读数。

    把它压进 PASS,覆盖率永远看起来是 100%;压进 FAIL,清单会长而且脏。
    本条钉住:它被单独计数,且形态分档拿得到(不同形态处置不同)。
    """
    assert result["unres"] > 0, (
        "UNRESOLVED 为 0 —— 不可能:仓里有 f-string SQL 与 JOIN。"
        "这说明它被并进别的档了,覆盖率读数就此消失。")
    assert result["kinds"], "UNRESOLVED 没有形态分档 —— 只有总数的话没人知道该先治哪一类"
    total = result["pass"] + len(result["fail"]) + len(result["stale"]) + result["unres"]
    assert result["unres"] / total > 0.05, (
        f"UNRESOLVED 占比 {result['unres']/total:.1%} 低得可疑 —— "
        f"先查是不是有一类解析不了的东西被静默判成了 PASS")


# ══ ⑤ 🔴 前缀匹配当词匹配 —— 正样本臂,不是注释 ═══════════════════
@pytest.mark.parametrize("col", [
    "check_date", "check_frequency_per_day",   # 实际栽过的两个
    "unique_key", "constraint_name", "primary_contact", "foreign_ref", "exclude_flag",
])
def test_columns_whose_name_starts_with_a_constraint_keyword_are_parsed(col):
    """🔴 `startswith("CHECK")` 会把 `check_date date NOT NULL` 整列当成 CHECK 约束丢掉。

    2026-09-05 实测代价:残留 FAIL 16 条里 **13 条**是这一个前缀 bug 造的假阳,
    而我已经在给 Deploy 准备**加性迁移工单** —— 生产明明有 `check_date date NOT NULL`。
    **给已存在的列再 ADD COLUMN 一次**,是一次本可以发生的生产事故。

    这条把那次教训做成**会红的东西**:只写进 docstring 的警示,下一个人不会读。
    """
    m = _census()
    synthetic = (
        "CREATE TABLE public.probe_t (\n"
        "    id integer NOT NULL,\n"
        f"    {col} date NOT NULL,\n"
        "    CONSTRAINT probe_t_ck CHECK ((id > 0)),\n"
        "    CHECK ((id < 100)),\n"
        "    UNIQUE (id),\n"
        "    PRIMARY KEY (id),\n"
        "    FOREIGN KEY (id) REFERENCES public.other(id)\n"
        ");\n"
    )
    cols = m.parse_schema(synthetic).get("probe_t", set())
    assert col in cols, (
        f"列 `{col}` 没被解析出来 —— 约束行判定又变回**前缀匹配**了。\n"
        f"    后果不是少一列:引用它的 SQL 会全被判 FAIL,\n"
        f"    而处置是「给生产加列」——**给已存在的列再加一次**。\n"
        f"    实测解析出的列:{sorted(cols)}")
    # 反臂:真正的约束行**不许**被当成列
    assert not ({"constraint", "check", "unique", "primary", "foreign"} & cols), (
        f"约束行被当成列了:{sorted(cols)} —— 词边界放得太松")


def test_stale_is_a_state_of_its_own_not_a_failure(result):
    """STALE = 树内加过列、快照没跟上 ⇒ **尺子过期**,不是代码缺陷。

    压进 FAIL 会让每次快照落后都变成一堆假红,然后有人去「修」正确的代码。
    """
    for r in result["stale"]:
        assert f"{r['t']}.{r['c']}" not in {f"{x['t']}.{x['c']}" for x in result["fail"]}, \
            f"{r['t']}.{r['c']} 同时出现在 STALE 与 FAIL 里 —— 分档逻辑有重叠"
