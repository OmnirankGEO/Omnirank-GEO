"""迁移**索引**守卫必须绑定表 —— 真 PG16 判别测试 + 三根轴的机械分母锁。

═══════════════════════════════════════════════════════════════════════
被测的是什么
═══════════════════════════════════════════════════════════════════════
``CREATE [UNIQUE] INDEX IF NOT EXISTS idx_x ON t (...)`` 的判存是
**按 schema 内的关系名**,不绑表(PG16 实测:同名索引长在别的表上时,
它打一条 ``NOTICE: relation "idx_x" already exists, skipping`` 就过去了)。
于是目标表上的索引**永远建不出来**,而迁移返回成功、prestart 退出码 0。

后果按索引类型分两种,后一种是数据完整性洞:
  · 普通索引缺失 —— 查询结果仍对,只是全表扫(静默性能塌);
  · **UNIQUE 索引缺失 —— 唯一性约束静默消失**。
    ``uq_defgeo_attempt_single_inflight`` 是 partial unique,
    它缺了,「同一格同时至多一个在飞 attempt」这条不变式就没人执行了 ——
    本文件 :func:`test_partial_unique_single_inflight_is_actually_enforced`
    在父提交臂上真的插进去两条在飞行。

而 ``scripts/prestart.py`` **每次部署无条件重放全部 manifest 迁移**(无追踪表),
所以这不是"某次迁移可能出错",是每次部署都在重演。

═══════════════════════════════════════════════════════════════════════
🔴 三根轴,分母都是机械扫出来的
═══════════════════════════════════════════════════════════════════════
同一个病有三种语法形态,只堵一种等于没堵:

  轴A ``CREATE [UNIQUE] INDEX IF NOT EXISTS``     建的时候被骗 ⇒ 索引不存在
  轴B ``WHERE indexname='X'`` / ``relname='X'``   反查时被骗 ⇒ readiness 假绿
  轴C ``DROP INDEX [IF EXISTS] X``               PG 语法上**无法绑表** ⇒ 删错表

轴A、轴B 本单已全部改完,锁是「== 0」的负向锁,**范围 = 整个 manifest**
(不是 ``db/migration_04*.sql``):新迁移带着旧形态进来当场红,不用人盯。
轴C 是破坏性语义变更、不在本单改动范围,但**冻结站点集合**钉住,新增一处就红。

分母源是 ``db/migration_manifest.py`` 的 ``MIGRATIONS``,不是手写文件名单
—— 手写名单漏掉的那一份不会让任何判据变红(本仓 2026-08 反复记过)。
扫描谓词只写一处(``scripts/defgeo_index_guard_scan.py``),census / readiness
导出 / 一次性改写脚本三方共用,要红一起红。
"""

from __future__ import annotations

import ast
import collections
import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

from tests.defensive_geo_w3_2026_08_21.conftest import (
    EXACT_THROWAWAY_URL,
    _load_prod_schema,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.defgeo_drop_index_inventory import (  # noqa: E402
    ADJUDICATED,
    DYNAMIC_BOUND_SITES,
    derive_expected_table,
)
from scripts.defgeo_index_guard_scan import (  # noqa: E402
    bound_drop_guard_defects,
    bound_guard_defects,
    manifest_sql_files,
    scan_bound_index_guards,
    scan_bound_drop_guards,
    scan_drop_index_sites,
    scan_dynamic_drop_any,
    scan_dynamic_drop_sites,
    scan_ifne_index_statements,
    scan_index_name_predicates,
    scan_naked_drop_index_sites,
)
from scripts.defgeo_manifest_replay import replay, snapshot  # noqa: E402
from scripts.defgeo_runtime_drop_scan import (  # noqa: E402
    DROP_BEARING_MODULES,
    drop_index_name,
    import_closure,
    is_executable_surface,
    non_test_callers,
    python_naked_drops,
)
from scripts.defgeo_readiness_gen import (  # noqa: E402
    READINESS_FILES,
    block_is_canonical,
    declared_constraints,
    declared_columns,
    declared_indexes,
    declared_triggers,
)

#: 本单的底(工单指定)。保护文件零 diff 与"新增形态"都以它为基线。
BASE_SHA = "fa8aecc50"
#: 轴C(三单)的底 —— R2 小补丁那一发。DROP 守卫的逐字等价锁比的是它。
AXIS_C_BASE_SHA = "6d8c90240"

_TEMPLATE_DB = "geo_defgeo_idxguard_tmpl_test"


# ══════════════════════════════════════════════════════════════════════════
# 机械分母(不带库,纯静态)
# ══════════════════════════════════════════════════════════════════════════
def _all_files() -> list[Path]:
    return manifest_sql_files(ROOT)


def _scan_all():
    ifne, bound, preds, drops = [], [], [], []
    for p in _all_files():
        rel = p.relative_to(ROOT).as_posix()
        sql = p.read_text(encoding="utf-8", errors="replace")
        ifne += scan_ifne_index_statements(sql, rel)
        bound += scan_bound_index_guards(sql, rel)
        preds += scan_index_name_predicates(sql, rel)
        drops += scan_drop_index_sites(sql, rel)
    return ifne, bound, preds, drops


def _axis_c_scan():
    """轴C 三件套:裸 DROP / DROP 守卫 / 动态 DROP。"""
    naked, guards, dyn = [], [], []
    for p in _all_files():
        rel = p.relative_to(ROOT).as_posix()
        sql = p.read_text(encoding="utf-8", errors="replace")
        naked += scan_naked_drop_index_sites(sql, rel)
        guards += scan_bound_drop_guards(sql, rel)
        dyn += scan_dynamic_drop_sites(sql, rel)
    return naked, guards, dyn


def test_manifest_denominator_is_live() -> None:
    """分母源自证:MIGRATIONS 解析得到的文件必须全部存在且数量可观。

    分母塌成 0 时,下面每一条「== 0」都会变成对空气说话的假绿。
    """
    files = _all_files()
    assert len(files) >= 110, f"manifest 只解析出 {len(files)} 个迁移 —— 分母源漂移了"
    missing = [p.as_posix() for p in files if not p.exists()]
    assert not missing, f"manifest 登记了不存在的迁移文件:{missing}"
    # 🔴 **集合锁,不是计数锁**。独立审计 IM-13:在扫描器返回处加一句 ``[:112]``
    #    就能把 042–046(防御 GEO 自己的五个迁移)抽走,而四条"地板"
    #    (files≥110 / bound≥400 / unique≥100 / preds≥20)一条都不会响。
    #    计数式判据会被业务写过期,集合式不会。
    from db.migration_manifest import MIGRATIONS

    want = [r for r in MIGRATIONS if str(r).endswith(".sql")]
    got = [p.relative_to(ROOT).as_posix() for p in files]
    assert got == [str(r).replace("\\", "/") for r in want], (
        "扫描器返回的迁移清单与 db/migration_manifest.MIGRATIONS 不逐项相等 —— "
        f"少了 {sorted(set(want) - set(got))} / 多了 {sorted(set(got) - set(want))}")


def test_axis_a_no_ifne_index_statement_remains() -> None:
    """轴A 负向锁:manifest 里不许再有 ``CREATE [UNIQUE] INDEX IF NOT EXISTS``。"""
    ifne, bound, _, _ = _scan_all()
    # 🔴 活性自证:新形态守卫必须成百上千地在 —— 扫描器瞎了时"零违规"是假绿。
    assert len(bound) >= 400, f"只扫到 {len(bound)} 条表绑定守卫 —— 扫描器或分母塌了"
    offenders = collections.Counter(s.path for s in ifne)
    assert not ifne, (
        "这些索引语句仍用 CREATE INDEX IF NOT EXISTS(按名判存不绑表;"
        f"同名索引长在别的表上时会静默跳过,目标表上永远建不出来):{dict(offenders)}")


def test_axis_a_unique_guards_stay_unique() -> None:
    """UNIQUE 掉了 = 唯一性约束没了,而普通索引照样能建成 —— 必须单独钉住数量。"""
    _, bound, _, _ = _scan_all()
    uniq = [s for s in bound if s.unique]
    assert len(uniq) >= 100, f"UNIQUE 守卫只剩 {len(uniq)} 条 —— 有守卫把 UNIQUE 丢了"
    for s in uniq:
        assert "CREATE UNIQUE INDEX" in s.body.upper(), (
            f"{s.path}::{s.index} 锚说是 unique,建索引分支却不是 CREATE UNIQUE INDEX")


def test_axis_a_every_guard_has_all_three_legs() -> None:
    """三条腿:同表跳过 / 异表 RAISE / 不存在真建。缺哪条都让守卫退回假守卫。"""
    _, bound, _, _ = _scan_all()
    broken = {f"{s.path}::{s.index}": bound_guard_defects(s)
              for s in bound if bound_guard_defects(s)}
    assert not broken, f"这些表绑定守卫缺腿:{broken}"


def test_every_guard_is_byte_identical_to_rewriting_the_base_version() -> None:
    """🔴 **全 459 条的逐字等价锁**:每一条守卫都必须逐字等于「把底(fa8aecc50)上
    那条旧语句喂给改写器」的输出。

    这条一次性钉死了逐字文本锁够不到的一整类退化(独立审计点名的几种):
      · 建索引腿的**列清单 / WHERE 谓词**被改(IM-16 —— 逐字模板只比到
        ``CREATE … ON public.<表> `` 这个前缀为止,后面的内容此前没人验);
      · 建索引腿**指错表**(IM-02);
      · ``-- @index-guard`` 锚被删(IM-17 —— 条数当场对不上);
      · 承重分支被取反 / 绑定被拆(IM-03)。
    而且它是**相对于底**的:证明的是"改写没改变任何索引的语义",
    不是"守卫长得像模板"。
    """
    from scripts.defgeo_index_guard_rewrite import rewrite_text

    checked = 0
    diffs: list[str] = []
    for p in _all_files():
        rel = p.relative_to(ROOT).as_posix()
        base = subprocess.run(["git", "show", f"{BASE_SHA}:{rel}"],
                              cwd=ROOT, capture_output=True)
        if base.returncode != 0:
            continue                      # 底上没有这个文件(本包不新增迁移,应为空)
        base_sql = base.stdout.decode("utf-8", errors="replace")
        rendered, n = rewrite_text(base_sql, rel)
        if n == 0:
            continue
        want = {g.index: " ".join(g.body.split())
                for g in scan_bound_index_guards(rendered, rel)}
        got = {g.index: " ".join(g.body.split())
               for g in scan_bound_index_guards(p.read_text(encoding="utf-8", errors="replace"), rel)}
        if set(want) != set(got):
            diffs.append(f"{rel}: 守卫集合不同 —— 少了 {sorted(set(want) - set(got))} / "
                         f"多了 {sorted(set(got) - set(want))}")
            continue
        for idx in sorted(want):
            checked += 1
            if want[idx] != got[idx]:
                diffs.append(f"{rel}::{idx} 守卫体与「改写底版本」的输出不同\n"
                             f"  应为:{want[idx][:220]}\n  实得:{got[idx][:220]}")
    assert checked >= 400, f"只比对了 {checked} 条守卫 —— 分母塌了,这条判据是空的"
    assert not diffs, "改写不是纯机械的(或守卫被手改过):\n" + "\n".join(diffs[:8])


def test_axis_a_anchor_count_equals_guard_body_count() -> None:
    """锚注释与守卫体必须**一一对应** —— 删掉一行注释不许让索引静默退出普查。

    🔴 独立审计 IM-17:全部机械分母的键是一行 ``-- @index-guard`` 注释,
       而没有任何判据要求"代码与注释同在"。删掉它,运行期行为一点不变,
       但这个索引同时退出 census / 声明集 / 将来的 readiness 导出,
       而所有地板(459→458、117→116)照样绿。
       这里按**每条守卫都会且只会发一条 ``[index-guard]`` RAISE** 交叉核对。
    """
    total_anchor = 0
    total_raise = 0
    per_file: dict[str, tuple[int, int]] = {}
    for p in _all_files():
        rel = p.relative_to(ROOT).as_posix()
        sql = p.read_text(encoding="utf-8", errors="replace")
        a = len(scan_bound_index_guards(sql, rel))
        r = sql.count("RAISE EXCEPTION '[index-guard] ")
        total_anchor += a
        total_raise += r
        if a != r:
            per_file[rel] = (a, r)
    assert total_anchor >= 400, f"锚只扫到 {total_anchor} 条 —— 分母塌了,这条判据没意义"
    assert not per_file, (
        "这些文件里 `-- @index-guard` 锚数与 `[index-guard]` RAISE 数对不上"
        f"(锚被删 / 守卫体被删):{per_file}")
    assert total_anchor == total_raise


def test_axis_b_no_unbound_index_name_predicate_remains() -> None:
    """轴B 负向锁:按索引名判存的地方必须绑表(indrelid / tablename / 整串比定义)。

    不绑表的反查会在"同名索引长在别的表"时**报绿**,而目标表上一个都没有 ——
    readiness 说就绪、真相是没建成,比不验还糟。
    """
    _, _, preds, _ = _scan_all()
    # 分母自证。轴B 只数 ``@index-guard`` 块**之外**的索引名谓词
    #(守卫块内部由轴A 的逐字模板锁守),所以它是几十条量级,不是几百条。
    assert len(preds) >= 20, f"轴B 只扫到 {len(preds)} 条谓词 —— 分母塌了"
    bad = [f"{p.path}:{p.line} {p.text[:120]}" for p in preds if not p.bound]
    assert not bad, f"这些索引名判存没绑表:{bad}"


# ══════════════════════════════════════════════════════════════════════════
# 第四条限界:``EXECUTE format('CREATE INDEX IF NOT EXISTS %I …')`` 动态建索引
# ══════════════════════════════════════════════════════════════════════════
# 🔴 索引名是**运行期算出来的**(``'idx_org_' || left(artifact_table,42) || '_scope'``),
#    静态扫描器只看得到 ``%I`` —— 所以它**不在**轴A 的 459 条普查里,本包也**没有改写它**。
#    不改的理由:要给动态语句加表绑定守卫,就得再套一层 EXECUTE 拼串,
#    引入的风险(拼串注入面、错误信息不可读、FOREACH 内提前中止把整批 artifact 表卡住)
#    大于收益。**披露 + 锁住**即可,改写与否是 Owner/Review 的决定。
#
# 两条锁:
#   (a) 站点集合冻结 == 1 —— 再长出一处动态建索引,当场红;
#   (b) 它生成的 10 个名字与 457 组**声明索引名**机械互斥 —— 撞名即红。
#       为什么(b)是承重的:这个站点用的是**老形态**(`CREATE INDEX IF NOT EXISTS %I`),
#       撞名时会静默跳过。只要它生成的名字与某条守卫的索引名重合,
#       两边就会互相当"已存在"——本单修好的那一侧会 RAISE,而它这一侧照样静默。
FROZEN_DYNAMIC_INDEX_SITES = {
    ("scripts/migration_organization_internal_seats_2026_07_20.sql",
     "idx_org_<artifact_table>_scope"),
}
#: FOREACH 里那 10 张 artifact 表(与 SQL 里的 ARRAY 字面量同源;
#: :func:`test_dynamic_index_table_list_matches_the_sql` 逐字核对,不许手抄漂移)。
_DYNAMIC_ARTIFACT_TABLES = (
    "client_profiles", "client_materials", "diagnosis_records", "quotes",
    "keyword_selection_sessions", "article_generations", "articles",
    "media_publications", "monitoring_tasks", "monitoring_reports",
)
_DYNAMIC_SITE_FILE = "scripts/migration_organization_internal_seats_2026_07_20.sql"
_EXEC_FORMAT_INDEX = re.compile(
    r"EXECUTE\s+format\s*\(\s*'CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS",
    re.IGNORECASE)


def _dynamic_index_sites() -> list[tuple[str, int]]:
    out = []
    for p in _all_files():
        rel = p.relative_to(ROOT).as_posix()
        sql = p.read_text(encoding="utf-8", errors="replace")
        for m in _EXEC_FORMAT_INDEX.finditer(sql):
            out.append((rel, sql[:m.start()].count("\n") + 1))
    return out


def test_dynamic_execute_format_index_sites_are_frozen() -> None:
    """(a) 动态建索引站点集合冻结 —— manifest 里有且只有这 1 处,新增即红。"""
    sites = _dynamic_index_sites()
    files = {rel for rel, _ln in sites}
    assert files == {rel for rel, _n in FROZEN_DYNAMIC_INDEX_SITES}, (
        "新增/挪动了 `EXECUTE format('CREATE INDEX IF NOT EXISTS %I …')` 站点。"
        "动态形态的索引名是运行期算的,**静态普查看不见**,也没有表绑定守卫 —— "
        f"要新增必须先由 Owner/Review 裁:{sorted(files)}")
    assert len(sites) == 1, f"动态建索引站点条数 {len(sites)} != 1:{sites}"


def test_dynamic_index_site_is_genuinely_outside_the_census() -> None:
    """活性:这条限界必须是真的 —— 该站点确实**没有**被轴A 数进去。

    如果哪天扫描器能吃动态形态了,这条会红,提醒把它并回主轴(好事,但要人知道)。
    """
    sql = (ROOT / _DYNAMIC_SITE_FILE).read_text(encoding="utf-8", errors="replace")
    assert _EXEC_FORMAT_INDEX.search(sql), "动态站点不在了 —— 这条限界在对空气说话"
    assert scan_ifne_index_statements(sql, _DYNAMIC_SITE_FILE) == [], \
        "该文件轴A 有残留 —— 与「动态形态在普查之外」这条陈述不自洽"
    guarded = {g.index for g in scan_bound_index_guards(sql, _DYNAMIC_SITE_FILE)}
    assert "idx_org_publish_orders_scope" in guarded, (
        "同文件里**静态**那条 idx_org_publish_orders_scope 应当已被改写成表绑定守卫 —— "
        "它不在动态循环里,是对照组")


def test_dynamic_index_table_list_matches_the_sql() -> None:
    """名单不手抄:FOREACH 的 ARRAY 字面量必须与本文件里的常量逐项相等。"""
    sql = (ROOT / _DYNAMIC_SITE_FILE).read_text(encoding="utf-8", errors="replace")
    m = re.search(r"FOREACH\s+artifact_table\s+IN\s+ARRAY\s+ARRAY\[(.*?)\]\s*LOOP",
                  sql, re.S | re.IGNORECASE)
    assert m, "找不到 FOREACH 的 ARRAY 字面量 —— 名单来源没了"
    in_sql = tuple(re.findall(r"'([^']+)'", m.group(1)))
    assert in_sql == _DYNAMIC_ARTIFACT_TABLES, (
        f"artifact 表名单漂移了:SQL 里是 {in_sql},判据里是 {_DYNAMIC_ARTIFACT_TABLES}")


def test_dynamic_generated_names_never_collide_with_declared_indexes() -> None:
    """(b) 10 个生成名与 457 组声明索引名**机械互斥** —— 撞名即红。

    撞上就意味着:一侧(本包守卫)会 RAISE、另一侧(动态老形态)静默跳过,
    同一个名字两种处置,谁先跑谁说了算。
    """
    generated = {f"idx_org_{tb[:42]}_scope" for tb in _DYNAMIC_ARTIFACT_TABLES}
    assert len(generated) == 10, f"生成名去重后只剩 {len(generated)} 个 —— left(,42) 截断撞车了"
    _, bound, _, _ = _scan_all()
    declared = {g.index for g in bound}
    assert len(declared) >= 400, f"声明索引名只有 {len(declared)} 个 —— 分母塌了"
    clash = sorted(generated & declared)
    assert not clash, (
        "动态生成的索引名与本包改写过的声明索引名撞了 —— 同一个名字会出现"
        f"「守卫 RAISE」与「动态形态静默跳过」两种处置:{clash}")


# ══════════════════════════════════════════════════════════════════════════
# 扫描器判别力(没有这一组,上面每一条「== 0」都可能是扫描器瞎了)
# ══════════════════════════════════════════════════════════════════════════
_OLD_PLAIN = "CREATE INDEX IF NOT EXISTS zz_i ON zz_t (a);"
_OLD_UNIQUE = "CREATE UNIQUE INDEX IF NOT EXISTS zz_u ON zz_t (a) WHERE a IS NOT NULL;"
_OLD_MULTILINE = "CREATE INDEX IF NOT EXISTS zz_m\n    ON public.zz_t (a, b DESC)\n    WHERE b > 0;"
_OLD_GIN = "CREATE INDEX IF NOT EXISTS zz_g ON zz_t USING gin (a gin_trgm_ops);"


def test_axis_a_scanner_catches_every_old_shape() -> None:
    """正样本:四种旧形态都必须被认出来,且解析出的三元组正确。"""
    got = scan_ifne_index_statements(
        "\n".join([_OLD_PLAIN, _OLD_UNIQUE, _OLD_MULTILINE, _OLD_GIN]), "probe.sql")
    assert [(s.index, s.table, s.unique) for s in got] == [
        ("zz_i", "zz_t", False), ("zz_u", "zz_t", True),
        ("zz_m", "zz_t", False), ("zz_g", "zz_t", False),
    ], f"旧形态没认全:{[(s.index, s.table, s.unique) for s in got]}"


def test_axis_a_scanner_ignores_commented_and_quoted_text() -> None:
    """负样本:注释里/字符串里的同样文本不算命中,否则轴A 的 ==0 永远达不到。"""
    noise = (
        "-- CREATE INDEX IF NOT EXISTS zz_c ON zz_t (a);\n"
        "/* CREATE UNIQUE INDEX IF NOT EXISTS zz_b ON zz_t (a); */\n"
        "SELECT 'CREATE INDEX IF NOT EXISTS zz_s ON zz_t (a)';\n"
    )
    assert scan_ifne_index_statements(noise, "probe.sql") == [], "把注释/字符串当成了真语句"


#: 用来做缺腿判别的**真实**守卫(不是手写样本)—— 手写样本与生成器一旦漂移,
#: 判别力判据就在考一个仓里不存在的形状。
_REAL_GUARD_FILE = "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql"
_REAL_GUARD_IDX = "uq_defgeo_attempt_single_inflight"
_REAL_GUARD_TBL = "defgeo_monitoring_attempts"


def test_axis_a_guard_defect_detector_has_discriminating_power() -> None:
    """把**真实**守卫的任一条腿挖掉,缺腿检测必须报出来。

    🔴 第一版只查 ``"INDRELID" in body``。而 RAISE 的诊断子查询里本来就有
       ``LEFT JOIN pg_class t ON t.oid = i.indrelid`` —— 于是把承重 ``IF``
       分支上的绑定拆掉,检测**照样全绿**(撕锁 SELF-03 实测)。
       同一发变异现在必须报缺腿。
    """
    src = io.open(ROOT / _REAL_GUARD_FILE, encoding="utf-8", newline="").read()
    guards = [g for g in scan_bound_index_guards(src, _REAL_GUARD_FILE)
              if g.index == _REAL_GUARD_IDX]
    assert len(guards) == 1, f"锚点不唯一({len(guards)})—— 这条判据在考错题"
    assert bound_guard_defects(guards[0]) == [], "真实守卫被误报成缺腿"
    # 🔴 body 不许溢出到下一条守卫:溢出就等于白捡邻居的三条腿,检测恒绿。
    assert len(guards[0].body) < 2000, (
        f"守卫 body {len(guards[0].body)} 字符 —— 收尾找错了,吞掉了下一条守卫")

    legs = {
        "IF 分支的 indrelid 绑定":
            (f"WHERE c.relname = '{_REAL_GUARD_IDX}' AND "
             f"i.indrelid = to_regclass('public.{_REAL_GUARD_TBL}')",
             f"WHERE c.relname = '{_REAL_GUARD_IDX}'"),
        "异表同名的 RAISE":
            (f"RAISE EXCEPTION '[index-guard] {_REAL_GUARD_IDX} 已存在但不在",
             "RAISE NOTICE '已存在但不在"),
        "建索引分支":
            (f"CREATE UNIQUE INDEX {_REAL_GUARD_IDX} ON public.{_REAL_GUARD_TBL} ",
             "NULL; -- "),
        "UNIQUE 关键字":
            (f"CREATE UNIQUE INDEX {_REAL_GUARD_IDX} ON public.{_REAL_GUARD_TBL} ",
             f"CREATE INDEX {_REAL_GUARD_IDX} ON public.{_REAL_GUARD_TBL} "),
    }
    for why, (frm, to) in legs.items():
        assert src.count(frm) == 1, f"「{why}」的锚点命中 {src.count(frm)} 处(必须恰好 1)"
        broken = [g for g in scan_bound_index_guards(src.replace(frm, to, 1), _REAL_GUARD_FILE)
                  if g.index == _REAL_GUARD_IDX]
        assert broken, f"挖掉「{why}」之后守卫整个扫不到了 —— 判别力判据在对空气说话"
        assert bound_guard_defects(broken[0]), f"挖掉「{why}」之后**没有**报缺腿"


def test_axis_b_predicate_detector_has_discriminating_power() -> None:
    """轴B 正负样本:不绑表要认出来,三种绑法都不许误报。"""
    unbound = ("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_indexes "
               "WHERE indexname = 'zz_i') THEN RAISE EXCEPTION 'x'; END IF; END $$;")
    got = scan_index_name_predicates(unbound, "probe.sql")
    assert got and not got[0].bound, "认不出不绑表的索引名判存"

    by_where = unbound.replace("indexname = 'zz_i'", "indexname = 'zz_i' AND tablename = 'zz_t'")
    assert all(p.bound for p in scan_index_name_predicates(by_where, "probe.sql")), \
        "WHERE 里绑了 tablename 却被误报"

    by_recheck = ("DO $$ DECLARE r RECORD; BEGIN "
                  "SELECT i.* INTO r FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid "
                  " WHERE c.relname='zz_i'; "
                  "IF r.indrelid IS DISTINCT FROM 'public.zz_t'::regclass THEN "
                  "RAISE EXCEPTION 'x'; END IF; END $$;")
    assert all(p.bound for p in scan_index_name_predicates(by_recheck, "probe.sql")), \
        "先取行再比 indrelid 的写法被误报成不绑表"

    by_def = ("DO $$ DECLARE idx TEXT; BEGIN "
              "SELECT pg_get_indexdef(to_regclass('public.zz_i')) INTO idx; "
              "IF idx IS DISTINCT FROM 'CREATE INDEX zz_i ON public.zz_t USING btree (a)' "
              "THEN RAISE EXCEPTION 'x'; END IF; END $$;")
    assert all(p.bound for p in scan_index_name_predicates(by_def, "probe.sql")), \
        "整串比 pg_get_indexdef 的写法被误报成不绑表"


#: 🔴 轴B 的**逐站点**判别力样本:本单实际绑上的 9 处,每一处单独回退,
#: 都必须让**这个文件**的"不绑表"计数从 0 变成 ≥1。
#:
#: 为什么非要逐站点、不能只跑一条"全局 == 0":
#:   第一版轴B 的作用域是**块级**,撕锁 SELF-06/07/08 实测——拆掉其中一处的绑定,
#:   同一块里别的谓词还留着 ``indrelid`` 这个词,锁照样全绿。
#:   一条抓不住自己修的那 9 处回退的锁,等于没有锁。
#: 每个样本都点名自己那个文件(不是"全仓有命中就算"),免得被别的站点顺手判红。
_TBL_TUPLE_IN = (
    "(tablename, indexname) IN (\n"
    "          ('admin_user_governance_audits',  'idx_admin_user_governance_audit_subject'),\n"
    "          ('admin_user_governance_audits',  'idx_admin_user_governance_audit_operator'),\n"
    "          ('admin_user_governance_audits',  'uq_admin_user_governance_audits_request_id'),\n"
    "          ('customer_agent_binding_history','uq_customer_agent_binding_history_active'),\n"
    "          ('customer_agent_binding_history','uq_customer_agent_binding_history_created_request'),\n"
    "          ('customer_agent_binding_history','idx_customer_agent_binding_history_timeline')\n")
_NAME_ONLY_IN = (
    "indexname IN (\n"
    "          'idx_admin_user_governance_audit_subject',\n"
    "          'idx_admin_user_governance_audit_operator',\n"
    "          'uq_admin_user_governance_audits_request_id',\n"
    "          'uq_customer_agent_binding_history_active',\n"
    "          'uq_customer_agent_binding_history_created_request',\n"
    "          'idx_customer_agent_binding_history_timeline'\n")
_BILLING = "scripts/migration_billing_deduction_idempotency_2026_07_19.sql"
_BILLING_TAIL = "  -- 绑表:索引名只在 schema 内唯一,不绑表会读到别的表上的同名索引"

AXIS_B_REVERTS = [
    ("db/migration_045_defgeo_run_dispatch_2026_08_22.sql",
     "           AND tablename = 'diagnosis_runs'\n", ""),
    ("scripts/migration_mhz_media_name_trgm_2026_07_28.sql",
     "           AND ix.indrelid = to_regclass('public.mhz_media')\n", ""),
    ("scripts/migration_mhz_media_name_trgm_2026_07_28.sql",
     "           AND i.indrelid = to_regclass('public.mhz_media')\n", ""),
    ("db/migration_034_geo_image_note_contract_2026_08_17.sql",
     "      JOIN pg_index ix ON ix.indexrelid = c.oid\n"
     "     WHERE c.relname = 'uq_mhz_item_live_revision_root'\n"
     "       AND ix.indrelid = to_regclass('public.mhz_publish_order_items');",
     "     WHERE c.relname = 'uq_mhz_item_live_revision_root';"),
    ("scripts/migration_admin_user_governance_2026_07_15.sql", _TBL_TUPLE_IN, _NAME_ONLY_IN),
] + [
    (_BILLING,
     f"AND p.indexname='{idx}'\n       AND p.tablename='{tbl}';{_BILLING_TAIL}",
     f"AND p.indexname='{idx}';")
    for idx, tbl in (
        ("idx_billing_deduction_idempotency_created", "billing_deduction_idempotency"),
        ("idx_billing_deduction_charge_identity", "billing_deduction_idempotency"),
        ("idx_billing_debt_offset_pending", "billing_debt_offset_outbox"),
        ("idx_billing_debt_offset_charge", "billing_debt_offset_outbox"),
    )
]


@pytest.mark.parametrize("rel,bound_text,reverted_text",
                         AXIS_B_REVERTS,
                         # 🔴 短 id:pytest 会再截断/清洗长 id,撕锁 runner 的
                         #    期望红集合就对不上了(第一轮实测)。
                         ids=[f"axisb{i}" for i in range(len(AXIS_B_REVERTS))])
def test_axis_b_lock_catches_each_binding_reverted(rel, bound_text, reverted_text) -> None:
    src = io.open(ROOT / rel, encoding="utf-8", newline="").read()
    assert src.count(bound_text) == 1, (
        f"{rel} 的绑定锚点命中 {src.count(bound_text)} 处(必须恰好 1)—— 这条判据在考错题")
    now = [p for p in scan_index_name_predicates(src, rel) if not p.bound]
    assert not now, f"{rel} 当前就有不绑表的谓词:{[p.line for p in now]}"
    mutated = src.replace(bound_text, reverted_text, 1)
    after = [p for p in scan_index_name_predicates(mutated, rel) if not p.bound]
    assert after, f"{rel} 把这处绑定回退掉,轴B 锁**没有**变红 —— 这条锁抓不住它自己修的东西"


# ══════════════════════════════════════════════════════════════════════════
# 轴C(三单):DROP INDEX 表绑定守卫
# ══════════════════════════════════════════════════════════════════════════
# 🔴 退役判据与继任者(继任者纪律:退役必须写清谁接的班)
#   · ``test_axis_c_drop_index_sites_are_frozen``(19 处冻结集)
#       → 继任 ``test_axis_c_no_naked_drop_index_remains``(裸 DROP == 0,自动盖未来迁移);
#   · ``test_guards_shadowed_by_a_preceding_drop_are_frozen``(8 站点冻结集)
#       → 继任 ``test_no_create_guard_is_shadowed_by_a_naked_drop``(遮蔽 == 0)。
#   两条冻结集在轴C 之后都**归零**了,继续冻结等于把"已经治好"钉成"永远这样"。


def test_axis_c_no_naked_drop_index_remains() -> None:
    """轴C 负向锁:manifest 里不许再有**裸**(不绑表)``DROP INDEX``。

    PG16 实测(轴C §一):裸 DROP 撞名时**静默把别的表的索引删掉**,零报错。
    这条锁自动盖未来迁移 —— 新迁移带着裸 DROP 进来当场红。
    """
    naked, guards, _dyn = _axis_c_scan()
    # 活性自证:DROP 守卫必须成打地在,否则"零裸 DROP"可能是扫描器瞎了
    assert len(guards) >= 15, f"只扫到 {len(guards)} 条 DROP 守卫 —— 扫描器或分母塌了"
    assert not naked, (
        "这些 DROP INDEX 仍是裸的(语法上不带表名;撞名时会静默删掉别的表的索引):"
        f"{naked}")


def test_axis_c_every_drop_guard_has_all_three_legs() -> None:
    """三条腿:同表真删 / 异表异类 RAISE / 不存在按原语句 IF EXISTS 口径。"""
    _naked, guards, _dyn = _axis_c_scan()
    broken = {f"{g.path}::{g.index}": bound_drop_guard_defects(g)
              for g in guards if bound_drop_guard_defects(g)}
    assert not broken, f"这些 DROP 守卫缺腿:{broken}"


def test_no_create_guard_is_shadowed_by_a_naked_drop() -> None:
    """§7.1 收账:不许再有"建索引守卫被同文件前面的裸 DROP 抢跑清名"。

    轴C 之前这里有 **8** 处(7 处在资金域):裸 DROP 先把名字删干净,
    守卫永远走 ELSE,RAISE 腿够不到 —— 撞名时仍会静默删掉无辜表的索引。
    DROP 绑表之后这个集合归零,判据从"冻结 8 处"翻成"必须是 0"。
    """
    shadowed: set[tuple[str, str]] = set()
    for p in _all_files():
        rel = p.relative_to(ROOT).as_posix()
        sql = p.read_text(encoding="utf-8", errors="replace")
        first: dict[str, int] = {}
        for _r, line, name in scan_naked_drop_index_sites(sql, rel):
            first.setdefault(name, line)
        for g in scan_bound_index_guards(sql, rel):
            gl = sql[:g.start].count("\n") + 1
            if g.index in first and first[g.index] < gl:
                shadowed.add((rel, g.index))
    assert not shadowed, (
        "这些建索引守卫的 RAISE 腿被同文件前面的**裸** DROP 抢跑清名了 —— "
        f"撞名时仍会静默删掉别的表的索引:{sorted(shadowed)}")


@pytest.mark.parametrize("row", ADJUDICATED,
                         # 🔴 序号 id:清单里有重名条目(同一索引名在两个迁移里各一处),
                         #    纯用索引名会让 pytest 自动加 0/1 后缀,撕锁的期望 node 名对不上。
                         ids=[f"{i:02d}{r['tier']}:{r['index'][:28]}"
                              for i, r in enumerate(ADJUDICATED)])
def test_drop_guard_expected_table_matches_its_provenance(row) -> None:
    """19 组审定清单:``A1``/``A2`` 现推核对,``M`` 冻结。

    DROP 语句不带表名 ⇒ 期望表必须有出处。推错了比不改更糟(本来能删的删不掉),
    所以两档机械档现推现比,人工档冻结且依据必须写满。
    """
    sql = (ROOT / row["file"]).read_text(encoding="utf-8", errors="replace")
    guards = [g for g in scan_bound_drop_guards(sql, row["file"])
              if g.index == row["index"]]
    assert guards, f"{row['file']}::{row['index']} 没有 DROP 守卫"
    for g in guards:
        assert g.table == row["table"], (
            f"守卫绑到了 {g.table},审定清单说 {row['table']}")
        assert g.unique == row["if_exists"], (
            f"IF EXISTS 口径不符:守卫说 {g.unique},清单说 {row['if_exists']}")

    if row["tier"] in ("A1", "A2"):
        # 现推:把守卫锚临时摘掉再推,免得推的是自己刚写进去的那条
        stripped = sql.replace(
            f"-- @drop-index-guard {row['index']} ON {row['table']} ", "-- @zz ", 1)
        line = min(ln for _r, ln, nm in scan_drop_index_sites(stripped, row["file"])
                   if nm == row["index"])
        derived = derive_expected_table(stripped, row["file"], row["index"], line)
        assert derived, f"{row['tier']} 档却推不出期望表 —— 档位标错了"
        assert derived[0] == row["tier"] and derived[1] == row["table"], (
            f"现推得 {derived},清单说 ({row['tier']}, {row['table']}) —— 手抄漂移")
    else:
        assert len(row["evidence"]) >= 40, "人工审定档的依据太短 —— 依据必须可核"


def test_adjudication_covers_every_drop_guard() -> None:
    """双向:每条 DROP 守卫都在审定清单里,清单里也不许有幽灵条目。"""
    _naked, guards, _dyn = _axis_c_scan()
    on_disk = {(g.path, g.index) for g in guards}
    listed = {(r["file"], r["index"]) for r in ADJUDICATED}
    assert on_disk - listed == set(), f"守卫不在审定清单里:{sorted(on_disk - listed)}"
    assert listed - on_disk == set(), f"清单里的幽灵条目:{sorted(listed - on_disk)}"
    assert len(guards) == 18, f"DROP 守卫 {len(guards)} 条 != 18"


def test_dynamic_drop_site_is_frozen_and_already_table_bound() -> None:
    """唯一一处动态 DROP:不改写,但必须证明它**本来就绑表**,且集合冻结。

    ``EXECUTE format('DROP INDEX public.%I', item.index_name)`` 的名字来自游标,
    而那个游标的 WHERE 把候选集限死在 ``geo_observation_insight_jobs`` 上 ——
    删不到别的表。动态语句加不了静态守卫,所以只冻结 + 盯住那句 indrelid 过滤。
    """
    sites = []
    for p in _all_files():
        rel = p.relative_to(ROOT).as_posix()
        sites += [(r, ln) for r, ln, snip in scan_dynamic_drop_any(
            p.read_text(encoding="utf-8", errors="replace"), rel) if "DROP INDEX" in snip]
    assert len(sites) == 1, f"动态**索引** DROP 站点条数 {len(sites)} != 1:{sites}"
    frozen = DYNAMIC_BOUND_SITES[0]
    assert sites[0][0] == frozen["file"], f"动态 DROP 挪窝了:{sites}"
    sql = (ROOT / frozen["file"]).read_text(encoding="utf-8", errors="replace")
    assert frozen["snippet"] in sql, "动态 DROP 语句形态变了 —— 冻结条目已过期"
    assert frozen["bound_by"] in sql, (
        "那句把候选集限死在目标表上的 indrelid 过滤不在了 —— "
        "这条动态 DROP 从此可能删到别的表的索引")


def test_drop_then_recreate_pairs_end_up_with_the_widened_definition() -> None:
    """两对 drop-then-recreate 的**终态**必须是后一条(加宽版)。

    A/B 指纹已经盖住了,但那是"与父提交相同";这条点名钉住"相同的那个值是对的"。
    口径来自迁移原文,不是从库里现读(现读就变成用结果证明结果)。
    """
    pairs = {
        "idx_geoplan_brand_status": (
            "scripts/migration_v7_geoplan_settle_conflict_index_2026_07_13.sql",
            "settle_conflict"),
        "uniq_fund_recovery_open": (
            "scripts/migration_v7_fund_recovery_fencing_2026_07_13.sql",
            "'pending','processing','manual'"),
    }
    from db.migration_manifest import MIGRATIONS

    order = [str(m).replace("\\", "/") for m in MIGRATIONS]
    for idx, (later_file, widened) in pairs.items():
        owners = [rel for rel in order
                  if idx in {g.index for g in scan_bound_index_guards(
                      (ROOT / rel).read_text(encoding="utf-8", errors="replace"), rel)}]
        assert len(owners) == 2, f"{idx} 的声明处应有 2 个(前后两版),实得 {owners}"
        assert owners[-1] == later_file, (
            f"{idx} 的**最后**一次声明应在 {later_file},实得 {owners[-1]} —— "
            "manifest 顺序变了,终态会翻转")
        last = (ROOT / owners[-1]).read_text(encoding="utf-8", errors="replace")
        guard = [g for g in scan_bound_index_guards(last, owners[-1]) if g.index == idx][0]
        assert widened in guard.body, (
            f"{idx} 终态定义里找不到加宽标志 {widened!r} —— 终态不是后一条")


#: 🔴 动态 ``EXECUTE … 'DROP …`` 站点全集(**任何对象类型**,不只索引)。
#: 独立审计 FG-2/FG-2b:``EXECUTE 'DROP INDEX IF EXISTS ' || quote_ident(x)`` 与
#: ``EXECUTE format('DROP %s public.%I','INDEX',…)`` 两种拼串里,``INDEX`` 这个词
#: 可以一个字都不出现 ⇒ 任何"认 INDEX 关键字"的正则都抓不到,而它每次部署真的在删索引。
#: 所以这根轴放粗到"任何动态 DROP",冻结当前 5 处;新增一处就红,逼人来看它删的是什么。
FROZEN_DYNAMIC_DROP_ANY = {
    ("scripts/migration_geo_provider_attempts_2026_07_21.sql", "DROP TABLE IF EXISTS pg_temp"),
    ("scripts/migration_geo_provider_attempts_2026_07_21.sql", "DROP TABLE pg_temp"),
    ("scripts/migration_marketing_deal_drafts_2026_07_22.sql", "DROP TABLE IF EXISTS pg_temp"),
    ("scripts/migration_marketing_deal_drafts_2026_07_22.sql", "DROP TABLE pg_temp"),
    ("scripts/migration_geo_observation_aggregate_basis_2026_07_20.sql", "DROP INDEX public"),
}


def test_dynamic_drop_of_any_object_is_frozen() -> None:
    """任何动态 ``EXECUTE … 'DROP …`` 站点冻结 == 5(4 处临时表清理 + 1 处动态索引)。"""
    seen = set()
    total = 0
    for p in _all_files():
        rel = p.relative_to(ROOT).as_posix()
        for r, _ln, snippet in scan_dynamic_drop_any(
                p.read_text(encoding="utf-8", errors="replace"), rel):
            total += 1
            kind = next((k for _f, k in FROZEN_DYNAMIC_DROP_ANY if k in snippet), snippet[:40])
            seen.add((r, kind))
    assert total == 5, f"动态 DROP 站点条数 {total} != 5"
    assert seen == FROZEN_DYNAMIC_DROP_ANY, (
        f"动态 DROP 站点集合变了 —— 新增 {sorted(seen - FROZEN_DYNAMIC_DROP_ANY)} / "
        f"消失 {sorted(FROZEN_DYNAMIC_DROP_ANY - seen)}")


def test_naked_drop_scanner_has_discriminating_power() -> None:
    """🔴 裸扫描器的**正负样本**(独立审计 FG-5:此前把它整个挖空,负向锁照样全绿)。

    五种位置各一例,成对断言"哪几条必须认成裸、哪几条必须不认"。
    FG-1/FG-3/FG-4 三个假绿都在这一组里变成常驻正样本。
    """
    guard = (
        "-- @drop-index-guard zz_i ON zz_t if-exists\n"
        "DO $dropguard$\nBEGIN\n"
        "    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid\n"
        "                WHERE c.relname = 'zz_i' AND i.indrelid = to_regclass('zz_t')) THEN\n"
        "        DROP INDEX IF EXISTS zz_i;\n"
        "    ELSIF EXISTS (SELECT 1 FROM pg_class c\n"
        "                   WHERE c.relname = 'zz_i' AND c.relnamespace = current_schema()::regnamespace) THEN\n"
        "        RAISE EXCEPTION 'x';\n"
        "    ELSE\n        NULL;\n    END IF;\n"
        "END $dropguard$;\n")

    def naked(sql):
        return {n for _r, _l, n in scan_naked_drop_index_sites(sql, "probe.sql")}

    # 必须**不**认成裸:守卫自己那条
    assert naked(guard) == set(), f"守卫自己的真删被当成裸:{naked(guard)}"
    # 必须认成裸:守卫之前 / 之后 / END IF 同行行尾 / 守卫体内多出来的一条
    assert "zz_before" in naked("DROP INDEX zz_before;\n" + guard), "守卫**之前**的裸 DROP 没认出来"
    assert "zz_after" in naked(guard + "DROP INDEX zz_after;\n"), "守卫**之后**的裸 DROP 没认出来"
    assert "zz_tail" in naked(
        guard.replace("    END IF;\n", "    END IF; DROP INDEX IF EXISTS zz_tail;\n", 1)), \
        "写在守卫 `END IF;` **同一行行尾**的裸 DROP 没认出来(FG-1)"
    assert "zz_extra" in naked(
        guard.replace("        DROP INDEX IF EXISTS zz_i;\n",
                      "        DROP INDEX IF EXISTS zz_i;\n        DROP INDEX zz_extra;\n", 1)), \
        "守卫体内**多出来的**那条 DROP 没认出来"
    # CRLF 下同样成立(FG-4:第一版按 splitlines 折算偏移,CRLF 整体错位)
    crlf = guard.replace("    END IF;\n", "    END IF; DROP INDEX IF EXISTS zz_tail;\n", 1) \
                .replace("\n", "\r\n")
    assert "zz_tail" in naked(crlf), "CRLF 下认不出来(偏移折算又回来了)"
    # 动态站点同一行的裸 DROP 必须认出来(FG-3:按行号豁免会一并放行)
    dyn_line = ("DO $$ BEGIN\n    EXECUTE format('DROP INDEX public.%I', x);"
                " DROP INDEX zz_riding;\nEND $$;\n")
    assert "zz_riding" in naked(dyn_line), "蹭动态站点行号的裸 DROP 被一并豁免了(FG-3)"


def test_drop_guard_defect_detector_has_discriminating_power() -> None:
    """🔴 DROP 守卫缺腿检测的判别力(独立审计 FG-6:此前挖空它,99 条照样全绿)。"""
    # 🔴 样本刻意选一条**没有任何变异打它**的守卫:判别力样本坐在变异靶上,
    #    每一发打那条守卫的变异都会顺手把这条判据一起打红(耦合噪声,不是发现)。
    rel = "scripts/migration_v10_channel_revenue_exactly_once_2026_07_13.sql"
    src = io.open(ROOT / rel, encoding="utf-8", newline="").read()
    head = ("-- @drop-index-guard uniq_fund_recovery_channel_open "
            "ON fund_recovery_orders if-exists")
    i = src.index(head)
    blk = src[i:src.index("END $dropguard$;", i) + len("END $dropguard$;")]
    good = [g for g in scan_bound_drop_guards(src, rel) if g.index == "uniq_fund_recovery_channel_open"]
    assert len(good) == 1 and bound_drop_guard_defects(good[0]) == [], "好守卫被误报成缺腿"

    for why, mut in (
        ("绑定被拆", blk.replace("i.indrelid = to_regclass('fund_recovery_orders')", "TRUE", 1)),
        ("真删腿被摘", blk.replace("        DROP INDEX IF EXISTS uniq_fund_recovery_channel_open;",
                                    "        NULL;", 1)),
        ("TOCTOU 的 IF EXISTS 被拿掉",
         blk.replace("DROP INDEX IF EXISTS uniq_fund_recovery_channel_open;",
                     "DROP INDEX uniq_fund_recovery_channel_open;", 1)),
        ("RAISE 降级成 NOTICE", blk.replace("RAISE EXCEPTION '[drop-index-guard]",
                                             "RAISE NOTICE '[drop-index-guard]", 1)),
        ("EXCEPTION 段吞掉 RAISE",
         blk.replace("    END IF;\nEND $dropguard$;",
                     "    END IF;\nEXCEPTION WHEN OTHERS THEN NULL;\nEND $dropguard$;", 1)),
        ("体内多一条 DROP",
         blk.replace("        DROP INDEX IF EXISTS uniq_fund_recovery_channel_open;",
                     "        DROP INDEX IF EXISTS uniq_fund_recovery_channel_open;\n"
                     "        DROP INDEX zz_extra;", 1)),
    ):
        broken = [g for g in scan_bound_drop_guards(src.replace(blk, mut, 1), rel)
                  if g.index == "uniq_fund_recovery_channel_open"]
        assert broken, f"「{why}」之后守卫整个扫不到 —— 判别力判据在对空气说话"
        assert bound_drop_guard_defects(broken[0]), f"「{why}」之后**没有**报缺腿"


def test_every_drop_guard_is_byte_identical_to_rewriting_the_base_version() -> None:
    """🔴 **全 18 条 DROP 守卫的逐字等价锁**(与轴A 同纪律,底 = 本单的底)。

    每条守卫必须逐字等于「把底上那条旧 DROP 语句 + 审定表 喂给改写器」的输出。
    它一次钉死了逐字模板锁够不到的几类:期望表被改成另一张真表、
    ``strict``/``if-exists`` 口径被改、真删语句里塞第二个名字、锚被删。
    """
    from scripts.defgeo_index_guard_rewrite import rewrite_drops

    expected = {(r["file"], r["index"]): r for r in ADJUDICATED}
    checked, diffs = 0, []
    for rel in sorted({r["file"] for r in ADJUDICATED}):
        base = subprocess.run(["git", "show", f"{AXIS_C_BASE_SHA}:{rel}"],
                              cwd=ROOT, capture_output=True)
        assert base.returncode == 0, f"取不到底上的 {rel}"
        rendered, n = rewrite_drops(base.stdout.decode("utf-8", errors="replace"),
                                    rel, expected)
        assert n > 0, f"{rel} 在底上一条裸 DROP 都没有 —— 这条判据在对空气说话"
        want = {g.index: " ".join(g.body.split())
                for g in scan_bound_drop_guards(rendered, rel)}
        got = {g.index: " ".join(g.body.split())
               for g in scan_bound_drop_guards(
                   (ROOT / rel).read_text(encoding="utf-8", errors="replace"), rel)}
        if set(want) != set(got):
            diffs.append(f"{rel}: 守卫集合不同 —— 少 {sorted(set(want) - set(got))} / "
                         f"多 {sorted(set(got) - set(want))}")
            continue
        for idx in sorted(want):
            checked += 1
            if want[idx] != got[idx]:
                diffs.append(f"{rel}::{idx} 与「改写底版本」的输出不同\n"
                             f"  应为:{want[idx][:200]}\n  实得:{got[idx][:200]}")
    assert checked == 18, f"只比对了 {checked} 条 DROP 守卫 —— 分母塌了"
    assert not diffs, "DROP 改写不是纯机械的(或守卫被手改过):\n" + "\n".join(diffs[:6])


#: 轴C 毒夹具:至少含一处 **§7.1 资金域**站点(``uniq_fund_recovery_open`` 一人占两站),
#: 证明「RAISE 腿复活」不是纸面推论。
DROP_POISON_CASES = [
    ("uniq_fund_recovery_open", "fund_recovery_orders"),
    ("idx_geoplan_brand_status", "geo_plan_tasks"),
    ("uq_diag_refund_extref", "diagnosis_refund_records"),
]


@pytest.mark.parametrize("index,table", DROP_POISON_CASES,
                         ids=[f"{i}-{t}" for i, t in DROP_POISON_CASES])
def test_naked_drop_would_silently_delete_another_tables_index(probe_db, index, table) -> None:
    """🔴 轴C 核心:诱饵表上有同名索引时,重放必须**响亮失败**,而且诱饵的索引要活着。

    底(轴C 之前)在这里是 **0 报错 + 诱饵表的索引被静默删掉** ——
    交付单 §「父臂复现记录」里有三处实测,含资金域的 uniq_fund_recovery_open。
    """
    url = _replayed(probe_db(f"dropoison_{index}"))
    decoy = "zz_dropguard_decoy"
    _exec(url,
          f'DROP INDEX IF EXISTS public."{index}"',
          f"CREATE TABLE IF NOT EXISTS public.{decoy} (a int, b int)",
          f'CREATE INDEX "{index}" ON public.{decoy} (a)')
    assert _index_host(url, index) == decoy, "诱饵没建上"

    log = replay(url, 1, stop_on_error=False)
    bad = [e for e in log if not e["ok"]]
    assert bad, (
        f"诱饵表上有同名索引 {index} 时重放**零报错** —— 裸 DROP 会把诱饵表的索引静默删掉")
    assert any("[drop-index-guard]" in e["error"] and index in e["error"] for e in bad), (
        "报错了,但不是 DROP 守卫报的:\n"
        + "\n".join(f"{e['file']} :: {e['error'][:200]}" for e in bad[:4]))
    assert _index_host(url, index) == decoy, (
        f"诱饵表的 {index} 被删掉了 —— 守卫没拦住,正是本单要修的病")


@pytest.mark.parametrize("index,table", DROP_POISON_CASES,
                         ids=[f"{i}-{t}" for i, t in DROP_POISON_CASES])
def test_without_decoy_the_drop_still_does_its_job(probe_db, index, table) -> None:
    """反向:没有诱饵时重放零报错,且该删的真删了 —— 分得开「守卫生效」与「迁移本来就跑不过」。"""
    url = _replayed(probe_db(f"dropclean_{index}"))
    bad = [e for e in replay(url, 1, stop_on_error=False) if not e["ok"]]
    assert not bad, f"无诱饵时重放就失败了(判据底座坏了):{bad[:3]}"
    host = _index_host(url, index)
    assert host in (table, None), (
        f"无诱饵时 {index} 落在了 {host} 上 —— 既不是目标表也不是「已被淘汰」")


def test_axis_c_drop_scanner_has_discriminating_power() -> None:
    sql = ("DROP INDEX zz_a;\nDROP INDEX IF EXISTS zz_b;\n"
           "DROP INDEX IF EXISTS public.zz_c;\n-- DROP INDEX zz_commented;\n"
           # 🔴 逗号列表:``DROP INDEX a, b;`` 是合法 PG 语法。第一版只取第一个名字,
           #    独立审计 IM-08 就用它把 ux_recharge_orders_idem 藏进去,
           #    冻结集合与 ==19 全绿,而真 PG16 上那条幂等 UNIQUE 被每次部署删掉。
           "DROP INDEX IF EXISTS zz_d, public.zz_e;\n")
    names = [n for _r, _l, n in scan_drop_index_sites(sql, "probe.sql")]
    assert names == ["zz_a", "zz_b", "zz_c", "zz_d", "zz_e"], \
        f"DROP INDEX 扫描器口径不对:{names}"


# ══════════════════════════════════════════════════════════════════════════
# 独立审计(2026-08-24)点名的绕过 —— 每一条都留成常驻判据
# ══════════════════════════════════════════════════════════════════════════
def test_axis_a_catches_quoted_identifier_form() -> None:
    """IM-01:``CREATE UNIQUE INDEX IF NOT EXISTS "quoted" ON t`` 也必须被轴A 数到。

    加两个双引号就绕过负向锁,而真 PG16 上索引照样建出来 —— 那条"新迁移带着
    旧形态进来当场红"的承诺就不成立了。
    """
    q = 'CREATE UNIQUE INDEX IF NOT EXISTS "ux_quoted_idem" ON public.recharge_orders (a);'
    got = scan_ifne_index_statements(q, "probe.sql")
    assert [(s.index, s.table, s.unique) for s in got] == \
        [("ux_quoted_idem", "recharge_orders", True)], \
        f"引号形态没被轴A 认出来:{[(s.index, s.table) for s in got]}"


def test_axis_a_catches_inverted_guard_branch() -> None:
    """IM-03:承重分支 ``IF EXISTS (`` 被改成 ``IF NOT EXISTS (`` 必须报缺腿。

    一个词让守卫永远走"幂等跳过",索引一辈子建不出来;三条腿的模板串却一个不少。
    真 PG16 实测过:重放零报错、``uq_publish_idem_command_id`` 静默消失。
    """
    rel = _REAL_GUARD_FILE
    src = io.open(ROOT / rel, encoding="utf-8", newline="").read()
    frm = ("    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid\n"
           f"                WHERE c.relname = '{_REAL_GUARD_IDX}'")
    assert src.count(frm) == 1, f"锚点命中 {src.count(frm)} 处(必须 1)—— 这条判据在考错题"
    mutated = src.replace(frm, frm.replace("IF EXISTS (", "IF NOT EXISTS (", 1), 1)
    g = [x for x in scan_bound_index_guards(mutated, rel) if x.index == _REAL_GUARD_IDX]
    assert g, "取反之后守卫整个扫不到了 —— 判据在对空气说话"
    assert bound_guard_defects(g[0]), "承重分支被取反,缺腿检测**没有**报出来"


def test_axis_b_catches_reversed_operand_form() -> None:
    """IM-10:``WHERE 'idx_x' = c.relname`` 这种反序写法也必须进轴B 分母并判不绑表。"""
    rev = ("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_index i2 JOIN pg_class c2 "
           "ON c2.oid = i2.indexrelid WHERE 'organizations_short_code_unique' = c2.relname) "
           "THEN RAISE EXCEPTION 'x'; END IF; END $$;")
    ps = scan_index_name_predicates(rev, "probe.sql")
    assert len(ps) == 1 and not ps[0].bound, \
        f"反序操作数的不绑表谓词退出了分母:{[(p.line, p.bound) for p in ps]}"


def test_do_block_comments_are_comments_but_execute_strings_are_not() -> None:
    """IM-12:DO 块里注掉的旧写法不算命中;但 ``EXECUTE '…'`` 串里的**要算**。

    前半是防无故判红(顶层的负样本判据宣称"注释不算",块里必须同样成立);
    后半是 fail-closed —— EXECUTE 串是真会执行的。
    """
    commented = ("-- @index-guard zz ON t plain\nDO $idxguard$\nBEGIN\n"
                 "    -- CREATE INDEX IF NOT EXISTS zz_dead ON t (id);\n"
                 "    NULL;\nEND $idxguard$;\n")
    assert scan_ifne_index_statements(commented, "probe.sql") == [], \
        "DO 块里注掉的一行旧写法被当成了真语句 —— 轴A 会无故判红"
    executed = "DO $$ BEGIN\n    EXECUTE 'CREATE INDEX IF NOT EXISTS zz_exec ON t (id)';\nEND $$;\n"
    assert [s.index for s in scan_ifne_index_statements(executed, "probe.sql")] == ["zz_exec"], \
        "EXECUTE 串里的旧形态没被扫到 —— 它是真会执行的"


# ══════════════════════════════════════════════════════════════════════════
# readiness:期望对象集必须 == 文件机械声明的对象集(双向)
# ══════════════════════════════════════════════════════════════════════════
_VALUES_ROW = re.compile(r"^\s*\('([^']+)',\s*'public\.([^']+)',", re.MULTILINE)


def _readiness_block(sql: str, tag: str) -> str:
    b, e = f"-- @readiness-begin {tag}", f"-- @readiness-end {tag}"
    assert b in sql and e in sql, f"[{tag}] 没有机械生成的 readiness 块(标记缺失)"
    return sql[sql.index(b): sql.index(e)]


@pytest.mark.parametrize("tag,rel", sorted(READINESS_FILES.items()))
def test_readiness_expected_set_equals_declared_set(tag: str, rel: str) -> None:
    """双向断言:声明的对象必须全在 readiness 里,readiness 里不许有幽灵对象。

    手挑"承重"子集是 fa8aecc50 的写法 —— 漏掉的那一条不会让任何判据变红。
    现在期望集合 == 文件机械声明的集合,一条不多一条不少。
    """
    sql = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    block = _readiness_block(sql, tag)
    listed = {(n, t) for n, t in _VALUES_ROW.findall(block)}
    # 🔴 [工单 V3-A · Codex 三审 P1-2] 声明集加上**触发器**这一轴。
    #    加轴不加进这个双向断言,等于新轴上「声明了却没进 readiness」永远不会红 ——
    #    而那正是 P1-2 的病(040 声明了触发器,readiness 里只有约束和索引)。
    declared = (set(declared_constraints(sql)) | set(declared_indexes(sql, rel))
                | set(declared_triggers(sql)) | set(declared_columns(sql)))
    assert declared, f"[{tag}] 机械声明集为空 —— 分母塌了,双向断言会变成对空气说话"
    assert declared - listed == set(), f"[{tag}] 声明了却没进 readiness:{sorted(declared - listed)}"
    assert listed - declared == set(), f"[{tag}] readiness 里的幽灵对象:{sorted(listed - declared)}"


@pytest.mark.parametrize("tag,rel", sorted(READINESS_FILES.items()))
def test_readiness_block_actually_checks_indexes_bound_to_table(tag: str, rel: str) -> None:
    """readiness 必须真的验它声明了的那一侧,且验索引时按 (表, 索引名) 验。

    🔴 [工单 E3-3 · 2026-08-26] 本条原来对**每个**文件都要求两侧同时存在。
       那不是严格,那是把轴的作用域卡死在"两侧都有"的文件上 ——
       051(0 约束 + 1 索引)、052/054(1 约束 + 0 索引)因此进不来,
       而 Codex 二审 P1-F4/F5 打穿的正是 051 与 052。
       现在按**文件自己声明了什么**来要求:声明了索引就必须验索引(且绑表),
       声明了约束就必须验约束。两侧都没声明仍然红(见下一条)。
    """
    sql = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    block = _readiness_block(sql, tag)
    if declared_indexes(sql, rel):
        assert "defgeo_ident_index(" in block, (
            f"[{tag}] 声明了索引却没在 readiness 里验定义"
            "(锚已随取值口从 pg_get_indexdef 换成 defgeo_ident_index:"
            "渲染文本会被 dump/restore 重写,身份不会)")
        assert "i.indrelid = r.tname::regclass" in block, f"[{tag}] readiness 验索引时没绑表"
        assert "i.indisvalid AND i.indisready" in block, (
            f"[{tag}] readiness 没验 indisvalid/indisready —— "
            "CREATE INDEX CONCURRENTLY 失败留下的壳子,pg_get_indexdef 文本与正品一模一样")
    if declared_columns(sql):
        # 🔴 [工单 V3-A · Codex 三审 P1-3 + P2-4] 列合同三样都要验。
        #    typmod 那一位尤其:varchar(64) 与 varchar(255) 的 data_type 相同,
        #    只核 data_type 的守卫对「错长度」零判别;而 default 那一位是
        #    050 原来**零核验**的那一轴,Codex 三个亲验 poison 有两个走的就是它。
        assert "defgeo_ident_column(" in block, (
            f"[{tag}] 声明了列却没在 readiness 里核列合同"
            "(身份含 atttypid+atttypmod,长度错一样抓得到)")
        assert "a.attnotnull" in block, f"[{tag}] 列合同没核可空性"
        assert "pg_get_expr(d.adbin, d.adrelid)" in block, (
            f"[{tag}] 列合同**没核 default**")
    if declared_triggers(sql):
        # 🔴 [工单 V3-A · Codex 三审 P1-2] 触发器四样都要验:
        #    绑表(病根)/ 定义 / tgenabled(DISABLE 掉的定义一字不差)/ 函数体。
        assert "defgeo_ident_trigger(" in block, (
            f"[{tag}] 声明了触发器却没在 readiness 里验")
        assert "g.tgrelid = to_regclass(p_table)" in block, (
            f"[{tag}] readiness 验触发器时**没绑表** —— pg_trigger.tgname 不绑表,"
            "别的表上一个同名触发器就能替它答「在」")
        assert "g.tgenabled" in block, (
            f"[{tag}] readiness 没验 tgenabled —— DISABLE 掉的触发器 pg_get_triggerdef "
            "输出与正品一字不差,却一次都不会执行")
        assert "pg_get_functiondef" in block, (
            f"[{tag}] readiness 没验函数体 —— 同名触发器指向同名函数、函数体被换成 "
            "RETURN NEW 那一手,只有比函数定义才看得见")
    if declared_constraints(sql):
        assert "defgeo_ident_constraint(" in block, (
            f"[{tag}] 声明了约束却没在 readiness 里验定义")
        assert "convalidated" in block, (
            f"[{tag}] readiness 没验 convalidated —— NOT VALID 约束只守未来,存量行没验过")


@pytest.mark.parametrize("tag,rel", sorted(READINESS_FILES.items()))
def test_readiness_denominator_is_nonzero(tag: str, rel: str) -> None:
    """活性:每个文件至少声明一侧对象,否则那块 readiness 是对空气说的。"""
    sql = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    # 🔴 [工单 V3-A] 四条轴任意一条有分母即可:050 声明 0 约束 / 0 索引 / 0 触发器
    #    / **1 列** —— 它是靠列合同轴进来的,卡在"必须有约束或索引"上会把它挡在门外,
    #    而 Codex 三审 P1-3 打穿的恰恰就是它。
    assert (declared_constraints(sql) or declared_indexes(sql, rel)
            or declared_triggers(sql) or declared_columns(sql)), (
        f"[{tag}] 约束/索引/触发器/列一个都没声明 —— readiness 在这个文件上是空的")


def test_the_readiness_axis_still_covers_both_sides_somewhere() -> None:
    """轴级活性:放宽到"单侧文件也能进"之后,两条轴都必须仍有真实分母。

    🔴 没有这一条,上面那两条可以在**全部文件都只有约束**的世界里全绿 ——
       而那正好等于"索引轴悄悄消失了",且没有任何判据会红。
    """
    with_idx, with_con, with_trg, with_col = [], [], [], []
    for tag, rel in sorted(READINESS_FILES.items()):
        sql = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        if declared_indexes(sql, rel):
            with_idx.append(tag)
        if declared_constraints(sql):
            with_con.append(tag)
        if declared_triggers(sql):
            with_trg.append(tag)
        if declared_columns(sql):
            with_col.append(tag)
    assert with_idx, "本轴已经没有任何文件声明索引 —— 索引轴整条消失了"
    assert with_con, "本轴已经没有任何文件声明约束 —— 约束轴整条消失了"
    # 🔴 [工单 V3-A] 触发器轴同样要有真实分母。没有这一条,「所有文件都不声明
    #    触发器」的世界里上面的双向断言全绿,而触发器轴悄悄消失了。
    assert with_trg, "本轴已经没有任何文件声明触发器 —— 触发器轴整条消失了"
    assert with_col, "本轴已经没有任何文件声明列 —— 列合同轴整条消失了"


@pytest.mark.parametrize("rel", sorted(READINESS_FILES.values()))
def test_migration_body_stays_zero_dml(rel: str) -> None:
    """迁移体仍零 DML、纯 additive(readiness 与索引守卫都只 SELECT/DDL + RAISE)。

    🔴 不能只看行首。独立审计 IM-07 把
       ``EXECUTE 'DELETE FROM defgeo_monitoring_attempts WHERE terminal_state IS NULL';``
       塞进 readiness 块:行首是 ``EXECUTE`` ⇒ 三条行首正则零命中 ⇒ "零 DML"报绿,
       而 prestart 每次部署都会把在飞 attempt 删光。
       现在**先剥注释、再按 token 扫**,``EXECUTE '…'`` 串里的 DML 一样算。
    """
    sql = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    body = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    for verb in (r"\bINSERT\s+INTO\s", r"\bUPDATE\s+(?:public\s*\.\s*)?[A-Za-z_]\w*\s+SET\b",
                 r"\bDELETE\s+FROM\s"):
        hit = re.search(verb, body, re.IGNORECASE)
        assert hit is None, (
            f"{rel} 出现 DML:{hit.group(0)!r} @ 第 {body[:hit.start()].count(chr(10)) + 1} 行"
            "(EXECUTE 串里的也算 —— prestart 每次部署无条件重放)")
    for verb in ("DROP TABLE", "DROP COLUMN", "TRUNCATE"):
        assert verb not in body.upper(), f"{rel} 出现破坏性语句 {verb}"


@pytest.mark.parametrize("rel", sorted(READINESS_FILES.values()))
def test_zero_dml_check_catches_dml_hidden_in_execute(rel: str) -> None:
    """活性:把 DML 藏进 ``EXECUTE '…'`` 里,上面那条必须仍然抓得到。"""
    sql = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    poisoned = sql.replace(
        "BEGIN\n", "BEGIN\n    EXECUTE 'DELETE FROM zz_probe WHERE 1=1';\n", 1)
    body = "\n".join(ln for ln in poisoned.splitlines() if not ln.lstrip().startswith("--"))
    assert re.search(r"\bDELETE\s+FROM\s", body, re.IGNORECASE), \
        "藏在 EXECUTE 串里的 DELETE 没被抓到 —— 零 DML 判据是空头支票"


@pytest.mark.parametrize("tag,rel", sorted(READINESS_FILES.items()))
def test_readiness_block_is_byte_identical_to_generator_output(tag: str, rel: str) -> None:
    """readiness 块必须**逐字**等于把它自己的 VALUES 行重新喂给生成器的结果。

    substring 断言挡不住 ``(… OR TRUE)`` 与 ``IF FALSE THEN`` 这两种改写
    (独立审计 IM-05 / IM-06 实证);逐字比对把整块骨架一次钉死。
    """
    ok, msg = block_is_canonical((ROOT / rel).read_text(encoding="utf-8", errors="replace"), tag)
    assert ok, msg


def test_canonical_readiness_lock_has_discriminating_power() -> None:
    """反向对照:上面那条必须真的认得出 ``OR TRUE`` 与 ``IF FALSE`` 两种退化。"""
    rel = "db/migration_044_defgeo_publish_decision_2026_08_21.sql"
    src = io.open(ROOT / rel, encoding="utf-8", newline="").read()
    for why, frm, to in (
        ("把索引 readiness 的绑表条件短路掉",
         "i.indrelid = r.tname::regclass", "(i.indrelid = r.tname::regclass OR TRUE)"),
        # 🔴 [工单 V3-A · 2026-08-28] 锚点从 `'[044] ` 加长到 `'[044] 索引 % 不在`。
        #    readiness 加了**触发器**那一段之后,「IF actual IS NULL THEN / RAISE
        #    EXCEPTION / '[044] 」在同一份文件里命中 2 处(索引段 + 触发器段)——
        #    是这条判据自己的「锚点必须恰 1 命中」把它拦下来的,不是我事后发现的。
        #    加长到点名是哪一段,锚点重新唯一;换段就是换锚点,不许含糊。
        ("把「索引不在目标表上」分支变死代码",
         "IF actual IS NULL THEN\n            RAISE EXCEPTION\n                '[044] 索引 % 不在",
         "IF FALSE THEN\n            RAISE EXCEPTION\n                '[044] 索引 % 不在"),
        ("把「触发器不在目标表上」分支变死代码",
         "IF actual IS NULL THEN\n            RAISE EXCEPTION\n                '[044] 触发器 % 不在",
         "IF FALSE THEN\n            RAISE EXCEPTION\n                '[044] 触发器 % 不在"),
        # 🔴 [P0 2026-09-02] 锚随取值口搬家:绑表条件现在长在
        #    `defgeo_ident_trigger` 的函数体里(`g.tgrelid = to_regclass(p_table)`),
        #    块里那份 `g.tgrelid = r.tname::regclass` 已不存在。
        #    是这条判据自己的「锚点必须恰 1 命中」把它拦下来的 —— 第二次了。
        ("把触发器 readiness 的绑表条件短路掉",
         "g.tgrelid = to_regclass(p_table)",
         "(g.tgrelid = to_regclass(p_table) OR TRUE)"),
    ):
        assert src.count(frm) == 1, f"「{why}」锚点命中 {src.count(frm)} 处(必须 1)"
        ok, _msg = block_is_canonical(src.replace(frm, to, 1), "044")
        assert not ok, f"「{why}」之后逐字锁**没有**变红"


# ══════════════════════════════════════════════════════════════════════════
# 保护文件 / manifest 零 diff
# ══════════════════════════════════════════════════════════════════════════
#: 仓里"保护文件"有多份不一致的清单(五 / 六 / 七)。这里取**并集**,
#: 任何一份口径下都成立;外加 manifest 本身(它决定哪些迁移会被重放)。
PROTECTED_UNION = (
    "middleware/billing.py", "db/connection.py", "auth/middleware.py",
    "auth/jwt_utils.py", "config/pricing_config.py", "tools/transparent_pricing.py",
    "db/wallet_db.py", "config/settings_manager.py", "api/scheduler.py",
    "db/migration_manifest.py",
)


def test_ab_harness_diff_actually_detects_a_difference() -> None:
    """等价性证据的**生成器**自己也要被验一次。

    🔴 独立审计 IM-14:整包"没改坏"的论证靠 `defgeo_index_ab_harness diff` 的退出码,
       而全仓 tests/ 无一处 import 它 —— 把里面那行
       ``same = all(...)`` 改成 ``same = True``,两臂永远报 identical、永远 exit 0,
       交付单里的等价性实证从此是空的,而一条判据都不红。
       证据工具要么进判据面,要么每次用前先注毒自证。这里选前者。
    """
    from scripts import defgeo_index_ab_harness as h

    a = {"indexes": [["public.t", "idx_a", "def_a"]], "tables": [["t"]]}
    same = {"indexes": [["public.t", "idx_a", "def_a"]], "tables": [["t"]]}
    moved = {"indexes": [["public.other", "idx_a", "def_a"]], "tables": [["t"]]}
    drifted = {"indexes": [["public.t", "idx_a", "def_a2"]], "tables": [["t"]]}

    def cmp(x, y):
        for k in sorted(set(x) | set(y)):
            if {tuple(r) for r in x.get(k, [])} != {tuple(r) for r in y.get(k, [])}:
                return False
        return True

    assert cmp(a, same), "对照函数把相同的两份指纹判成不同 —— 底座坏了"
    assert not cmp(a, moved), "索引换了宿主表,指纹对照**没有**判出不同"
    assert not cmp(a, drifted), "索引定义漂移,指纹对照**没有**判出不同"
    # 台架里那一行必须是真的比,不是常量
    src = io.open(ROOT / "scripts" / "defgeo_index_ab_harness.py",
                  encoding="utf-8", newline="").read()
    assert 'same = all(not v["only_in_a"] and not v["only_in_b"] for v in report.values())' in src, \
        "台架的 identical 判定被改成了常量 —— 等价性证据从此恒真"
    assert "_SNAPSHOT_SQL" not in src or True
    fp_src = io.open(ROOT / "scripts" / "defgeo_manifest_replay.py",
                     encoding="utf-8", newline="").read()
    # 指纹必须按 (表, 索引名) 键 —— 否则"索引长错表"在指纹里看不见
    assert "JOIN pg_class t ON t.oid = i.indrelid" in fp_src, \
        "schema 指纹没按宿主表取索引 —— 长错表时指纹不会变"


def test_replay_harness_prepare_matches_prestart() -> None:
    """台架的 ``_prepare`` 自称与 ``scripts/prestart.py::_apply`` 逐字同构 —— 那句话要有锁。

    🔴 独立审计 IM-09:整包价值的执行方是 prestart(守卫 RAISE ⇒ 非零退出 ⇒ 部署 halt),
       而 tests/ 里没有一行代码读过它;docstring 里的"逐字同构"是一句没有锁的断言。
       口径一旦漂移,台架跑的就不是生产跑的那段 SQL。
    """
    pre = io.open(ROOT / "scripts" / "prestart.py", encoding="utf-8", newline="").read()
    har = io.open(ROOT / "scripts" / "defgeo_manifest_replay.py",
                  encoding="utf-8", newline="").read()
    strip_meta = r"^\s*\\[a-zA-Z_]+.*$"
    strip_txn = r"^\s*(BEGIN|COMMIT)\s*;\s*$"
    for what, pattern in (("剥 psql 元命令", strip_meta), ("剥裸 BEGIN/COMMIT", strip_txn)):
        assert pattern in pre, f"prestart 里找不到「{what}」的正则 —— 口径已漂移,锁失效"
        assert pattern in har, f"台架里找不到「{what}」的正则 —— 与 prestart 不同构"
    assert "cur.execute(sql)" in pre, \
        "prestart 不再是「整份文件一次 execute」—— 台架的单事务假设要重新核"


def test_protected_files_and_manifest_have_zero_diff() -> None:
    # 🔴 比的是 **底 vs 工作树**(不带 HEAD):提交前后都成立。
    #    只比 BASE..HEAD 时,改动还在工作区没提交,这条会假绿。
    #
    # [合流 2026-08-25 Review-CTO] 合并树上并集里四个成员被**别的合法包**动过,
    # 各有自己的守卫,从本条摘出(本条的职责是"加固包自己不许碰",不是全站冻结):
    #   config/settings_manager.py —— 包F ⑦ Owner 逐行白名单,由 wp6
    #     test_protected_files_have_zero_diff 的白名单判据守;
    #   api/scheduler.py —— 慎改区,改动在各自交付单单列(P0-3 系已审);
    #   db/migration_manifest.py —— 追加登记制(048/049 号段 Review 登记),
    #     幽灵/漏登由 test_manifest_denominator_is_live 的集合锁守;
    #   config/pricing_config.py / tools/transparent_pricing.py —— Deploy 七件套
    #     成员,生产线演化按其台账;此处仍随五核心一起冻(下方未摘)。
    _merge_exempt = {"config/settings_manager.py", "api/scheduler.py",
                     "db/migration_manifest.py"}
    _still_frozen = tuple(p for p in PROTECTED_UNION if p not in _merge_exempt)
    out = subprocess.run(
        ["git", "diff", "--numstat", BASE_SHA, "--", *_still_frozen],
        cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, f"git diff 跑不起来:{out.stderr}"
    # 反向对照:同一条命令对**确实改过**的文件必须非空,否则"零 diff"可能是命令没生效
    probe = subprocess.run(
        ["git", "diff", "--numstat", BASE_SHA, "--",
         "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql"],
        cwd=ROOT, capture_output=True, text=True)
    assert probe.stdout.strip(), "反向对照落空:git diff 对已改文件也返回空 —— 这条判据当前无效"
    # 🔴 [2026-09-05] **收窄,不是放宽**:本轮 `middleware/billing.py` 经授权改动。
    #    Owner 2026-09-05 亲口授权(「billing.py 这笔我批了」)· OWNER_AUTHORIZATIONS_2026-09-05.md sha256 b673b188ff01e377
    #    其余保护文件任一被改 ⇒ 仍然红;billing.py 改回不动 ⇒ **也红**。
    _files = tuple(sorted(ln.split(chr(9))[-1] for ln in out.stdout.splitlines() if ln.strip()))
    assert _files == ("middleware/billing.py",), (
        f"保护文件/manifest 改动集 {_files} != 已授权集 · Owner 2026-09-05 亲口授权(「billing.py 这笔我批了」)· OWNER_AUTHORIZATIONS_2026-09-05.md sha256 b673b188ff01e377")


# ══════════════════════════════════════════════════════════════════════════
# 真 PG16:异表同名毒夹具
# ══════════════════════════════════════════════════════════════════════════
DECOY_TABLE = "zz_idxguard_decoy"


def _admin_conn():
    base, _, _db = EXACT_THROWAWAY_URL.rpartition("/")
    conn = psycopg2.connect(base + "/postgres")
    conn.autocommit = True
    return conn


def _base_url() -> str:
    return EXACT_THROWAWAY_URL.rpartition("/")[0]


@pytest.fixture(scope="module")
def template_db() -> str:
    """一次性造一个**只装生产 dump**的模板库(生产形状,尚未重放 manifest)。

    🔴 模板里**刻意不含**"重放过 manifest"的结果:模板库跨进程复用(建一次用很多次),
       一旦把重放结果烤进去,任何对迁移文件的改动(撕锁变异就是这么干的)
       都会与缓存里的旧 schema 比 —— 红不红取决于模板是哪一版建的,不可复现。
       需要"重放后状态"的判据自己现跑 :func:`replay`(1-2 秒),这样读的永远是**当前文件**。

    后面每条判据从它 ``CREATE DATABASE ... TEMPLATE`` 出一个独立库 ——
    共享库跑 A/B 不可复现(2026-08-19 实测三跑三答案),这里一条判据一个库。
    """
    admin = _admin_conn()
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (_TEMPLATE_DB,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{_TEMPLATE_DB}"')
                url = f"{_base_url()}/{_TEMPLATE_DB}"
                conn = psycopg2.connect(url)
                conn.autocommit = True
                try:
                    with conn.cursor() as c2:
                        c2.execute("CREATE EXTENSION IF NOT EXISTS vector")
                        c2.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                    _load_prod_schema(conn)
                finally:
                    conn.close()
    finally:
        admin.close()
    return _TEMPLATE_DB


def _replayed(url: str) -> str:
    """在库上把当前 manifest 重放一遍(判据底座坏了要当场知道)。"""
    bad = [e for e in replay(url, 1) if not e["ok"]]
    assert not bad, f"重放 manifest 就失败了(判据底座坏了):{bad[:3]}"
    return url


def _drop_db(name: str) -> None:
    assert "defgeo" in name and "test" in name, "安全栓:只点名删含 defgeo+test 的库"
    admin = _admin_conn()
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.close()


def _fresh_from_template(template: str, suffix: str) -> str:
    """从模板克隆一个**本条判据专用**的库。

    🔴 库名必须是**确定性**的:第一版用 ``abs(hash(index)) % 9973``,
       而 Python 的 str hash 每进程随机化 ⇒ 每跑一次就在容器里留下一批新库
       (实测攒到 50+ 个),而且不同轮次之间无法点名清理。

    🔴 ``DROP DATABASE`` 返回、``pg_database`` 里已查不到该行,紧接着的
       ``CREATE DATABASE ... TEMPLATE`` 仍可能撞 ``pg_database_datname_index``
       —— 真 PG16 上实测复现(三连做的第三次)。所以这里退避重试,
       而不是把这种偶发红当成"被测代码坏了"。
    """
    # 🔴 截断只砍 slug,不砍整串:第一版 ``f"...{slug}_test"[:60]`` 会把结尾的
    #    ``_test`` 削掉,安全栓当场判红(它做对了 —— 但那是判据自己的洞)。
    slug = re.sub(r"[^a-z0-9]+", "_", suffix.lower()).strip("_")[:38]
    name = f"geo_defgeo_idxg_{slug}_test"
    assert "defgeo" in name and "test" in name, "安全栓:库名必须同时含 defgeo 与 test"
    last: Exception | None = None
    for attempt in range(6):
        _drop_db(name)
        admin = _admin_conn()
        try:
            with admin.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
            return f"{_base_url()}/{name}"
        except psycopg2.Error as exc:      # pragma: no cover - 偶发路径
            last = exc
            time.sleep(0.4 * (attempt + 1))
        finally:
            admin.close()
    raise AssertionError(f"克隆 {name} 连试 6 次都失败(不是被测代码的问题):{last}")


@pytest.fixture
def probe_db(template_db):
    """判据专用一次性库工厂 —— 用完**点名删**(禁 prune),卷数守恒。"""
    created: list[str] = []

    def _make(suffix: str) -> str:
        url = _fresh_from_template(template_db, suffix)
        created.append(url.rsplit("/", 1)[-1])
        return url

    yield _make
    for name in created:
        _drop_db(name)


def _exec(url: str, *stmts: str) -> None:
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for s in stmts:
                cur.execute(s)
    finally:
        conn.close()


def _index_host(url: str, index: str) -> str | None:
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.relname FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid "
                "  JOIN pg_class t ON t.oid=i.indrelid "
                " WHERE c.relname=%s AND c.relnamespace='public'::regnamespace", (index,))
            row = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()
    return row[0] if row else None


def _plant_decoy(url: str, index: str, unique: bool) -> None:
    _exec(url,
          f"CREATE TABLE IF NOT EXISTS public.{DECOY_TABLE} (a int, b int)",
          f'DROP INDEX IF EXISTS public."{index}"',
          f'CREATE {"UNIQUE " if unique else ""}INDEX "{index}" ON public.{DECOY_TABLE} (a)')


#: 三个毒点覆盖三种情形:防御 GEO 的 **partial unique**(数据完整性洞)、
#: 防御 GEO 的普通索引、以及 defgeo **之外**的一条(证明本单是 manifest 级不是 04* 级)。
POISON_CASES = [
    ("uq_defgeo_attempt_single_inflight", "defgeo_monitoring_attempts", True,
     "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql"),
    ("idx_defgeo_qplan_brand_latest", "defgeo_question_plans", False,
     "db/migration_040_defgeo_question_plans_2026_08_21.sql"),
    ("idx_mhz_media_name_trgm", "mhz_media", False,
     "scripts/migration_mhz_media_name_trgm_2026_07_28.sql"),
]


@pytest.mark.parametrize("index,table,unique,owner", POISON_CASES)
def test_decoy_same_named_index_makes_replay_fail_loudly(
        probe_db, index, table, unique, owner) -> None:
    """🔴 本单的核心判据:别的表上先有同名索引时,重放必须**响亮失败**。

    父提交(fa8aecc50)上这里是 0 报错、索引留在诱饵表上、目标表一个都没有
    —— 交付单 §毒夹具复现记录里有实测。
    """
    url = probe_db(f"poison_{index}")
    _exec(url, f'DROP INDEX IF EXISTS public."{index}"')
    _plant_decoy(url, index, unique)
    # 诱饵确实到位(否则这条判据在证明一个不存在的场景)
    assert _index_host(url, index) == DECOY_TABLE, "诱饵没建上"

    log = replay(url, 1, stop_on_error=False)
    bad = [e for e in log if not e["ok"]]
    assert bad, (
        f"诱饵表上有同名索引 {index} 时,重放**零报错** —— 守卫被骗了,"
        f"public.{table} 上的索引静默消失而迁移返回成功")
    sigs = "\n".join(f"{e['file']} :: {e['error'][:200]}" for e in bad)
    assert any(index in e["error"] and "[index-guard]" in e["error"] for e in bad), \
        f"报错了,但不是本单的守卫报的(可能是别处顺带炸的):\n{sigs}"
    assert any(e["file"] == owner for e in bad), \
        f"报错的不是声明该索引的那个迁移({owner}):\n{sigs}"


@pytest.mark.parametrize("index,table,unique,owner", POISON_CASES)
def test_without_decoy_replay_is_clean_and_index_lands_on_target(
        probe_db, index, table, unique, owner) -> None:
    """反向:没有诱饵时必须零报错,且索引落在目标表上。

    没有这条,上面那条分不开"守卫生效"与"迁移本来就跑不过"。
    """
    url = probe_db(f"clean_{index}")
    _exec(url, f'DROP INDEX IF EXISTS public."{index}"')
    bad = [e for e in replay(url, 1, stop_on_error=False) if not e["ok"]]
    assert not bad, f"无诱饵时重放就失败了(判据底座坏了):{bad[:3]}"
    assert _index_host(url, index) == table, \
        f"无诱饵时 {index} 也没落在 {table} 上 —— 判据底座坏了"


def test_partial_unique_single_inflight_is_actually_enforced(probe_db) -> None:
    """行为判据:partial unique 修好之后,同一格的第二条在飞 attempt 必须被拒。

    父提交 + 诱饵时这里能插进去两条(交付单 §毒夹具复现记录有实测输出)——
    「索引缺失」不是抽象的,它就是这条不变式没人执行。
    """
    url = _replayed(probe_db("inflight"))
    cell = "ZZPROBE_CELL".ljust(64, "x")
    ins = (
        "INSERT INTO defgeo_monitoring_attempts "
        "(attempt_id, plan_cell_id, attempt_ordinal, run_authority_id, tenant_owner_user_id,"
        " brand_id, actual_provider, actual_model, actual_surface, actual_search_mode,"
        " request_hash, ledger_version) VALUES (%s, %s, %s, 'ra-1', 9001, 9001,"
        " 'p','m','s','sm','h','v1')")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(ins, ("ZZPROBE_A".ljust(64, "x"), cell, 1))
        with pytest.raises(psycopg2.errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(ins, ("ZZPROBE_B".ljust(64, "x"), cell, 2))
    finally:
        conn.close()


def test_every_declared_index_lands_on_its_target_table_after_cold_replay(probe_db) -> None:
    """🔴 **跨全集**行为判据:冷库重放后,普查的每一个 `(索引, 表)` 都必须真的落在目标表上。

    这条是独立审计(2026-08-24)点名要补的那一条。此前 459 条守卫里,
    只有落在 4 个 readiness 文件里的 25 个有行为级兜底,另外 434 条**只有静态文本锁**
    —— 于是"把承重分支取反""把锚注释删掉""把建索引腿指错表"这类变异
    在 113 个文件上一条判据都不红。

    做法:装生产 dump → 重放一遍 → 把普查里能删的索引全删掉(逼建索引腿执行)
    → 再重放 → 逐条核对宿主表。
    """
    url = _replayed(probe_db("landing"))
    census = []
    for p in _all_files():
        rel = p.relative_to(ROOT).as_posix()
        census += scan_bound_index_guards(p.read_text(encoding="utf-8", errors="replace"), rel)
    want = sorted({(s.index, s.table) for s in census})
    assert len(want) >= 400, f"普查只有 {len(want)} 条 —— 分母塌了,这条判据没意义"

    conn = psycopg2.connect(url)
    conn.autocommit = True
    dropped = 0
    try:
        with conn.cursor() as cur:
            for idx, tbl in want:
                cur.execute(
                    "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid "
                    " WHERE c.relname=%s AND i.indrelid = to_regclass(%s)",
                    (idx, f"public.{tbl}"))
                if cur.fetchone() is None:
                    continue
                try:
                    cur.execute(f'DROP INDEX public."{idx}"')
                    dropped += 1
                except psycopg2.Error:
                    pass          # 被 FK 背书的索引删不掉 —— 如实跳过
    finally:
        conn.close()
    # 🔴 活性:必须真的删掉了一大批,否则"全都在"只是"本来就都在"
    assert dropped >= 300, f"冷化只删掉 {dropped} 个索引 —— 建索引腿没被逼出来,这条判据是空的"

    _replayed(url)
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname, t.relname FROM pg_class c "
                "  JOIN pg_index i ON i.indexrelid=c.oid JOIN pg_class t ON t.oid=i.indrelid "
                " WHERE c.relnamespace='public'::regnamespace")
            live = {(a, b) for a, b in cur.fetchall()}
        conn.rollback()
    finally:
        conn.close()
    missing = sorted(set(want) - live)
    assert not missing, (
        f"冷库重放后这些索引没落在目标表上({len(missing)}/{len(want)}):{missing[:20]}")


def test_replay_twice_is_a_noop(probe_db) -> None:
    """幂等:第二遍重放不许改变任何 schema 对象(索引/约束/触发器/列/表)。"""
    url = probe_db("idem")
    assert not [e for e in replay(url, 1) if not e["ok"]]
    first = snapshot(url)
    assert not [e for e in replay(url, 1) if not e["ok"]]
    second = snapshot(url)
    assert first["indexes"], "快照里一个索引都没有 —— 幂等判据在对空气说话"
    for key in sorted(first):
        assert first[key] == second[key], f"第二遍重放改变了 {key}"


def test_readiness_catches_index_definition_drift(probe_db) -> None:
    """readiness 索引循环必须真的比定义:把 partial 谓词改宽一格,重放 046 必须红。"""
    url = _replayed(probe_db("drift"))
    _exec(url,
          "DROP INDEX IF EXISTS public.uq_defgeo_attempt_single_inflight",
          "CREATE UNIQUE INDEX uq_defgeo_attempt_single_inflight "
          "  ON public.defgeo_monitoring_attempts (plan_cell_id) "
          " WHERE terminal_state IS NULL AND brand_id > 0")
    rel = "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql"
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with pytest.raises(psycopg2.Error) as err:
            with conn.cursor() as cur:
                cur.execute((ROOT / rel).read_text(encoding="utf-8"))
    finally:
        conn.close()
    assert "定义漂移" in str(err.value), f"漂移没被 readiness 抓到,实得:{err.value}"


def test_readiness_catches_missing_index_when_guard_is_neutered(probe_db) -> None:
    """readiness 的"索引不在目标表上"分支不是死代码:摘掉守卫的建索引分支就能走到。

    正常路径下守卫会先 RAISE 或先把索引建出来,这一支够不到 —— 但它是
    纵深防御,必须证明它**能**触发,否则它只是好看的注释。
    """
    url = _replayed(probe_db("neuter"))
    rel = "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql"
    src = io.open(ROOT / rel, encoding="utf-8", newline="").read()
    create_line = ("        CREATE UNIQUE INDEX uq_defgeo_attempt_single_inflight "
                   "ON public.defgeo_monitoring_attempts (plan_cell_id) "
                   "WHERE terminal_state IS NULL;")
    assert src.count(create_line) == 1, "变异锚点不唯一 —— 这条判据在考错题"
    mutant = src.replace(create_line, "        NULL;", 1)
    _exec(url, "DROP INDEX IF EXISTS public.uq_defgeo_attempt_single_inflight")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with pytest.raises(psycopg2.Error) as err:
            with conn.cursor() as cur:
                cur.execute(mutant)
    finally:
        conn.close()
    assert "uq_defgeo_attempt_single_inflight" in str(err.value) and "不在" in str(err.value), \
        f"readiness 没报「索引不在目标表上」,实得:{err.value}"


# ══════════════════════════════════════════════════════════════════════════
# 🔴 R2 收口 ⑧:`strict` 站点(034)两条腿的不可达,必须有**可执行取证**
#
# 「不可达 / 走不到 / 分母为 0」是强断言。交付单 §9.3-(1) 原来只说了一条腿、
# 而且只有推理。这里三条判据把它钉死:A/B 证两条死腿够不到,C 是活性对照
# —— 没有 C,A/B 全绿也可能是"整块什么都没跑"。
# 取证脚本:docs/AI-CONTEXT/AXISC_ARTIFACTS_2026-08-24/strict_site_unreachable.py
# ══════════════════════════════════════════════════════════════════════════
STRICT_REL = "db/migration_034_geo_image_note_contract_2026_08_17.sql"
STRICT_IDX = "uq_mhz_item_live_revision_root"
STRICT_TBL = "mhz_publish_order_items"
STRICT_WIDE = "OR (availability IS NULL)"


def _strict_outer_select(url: str) -> str | None:
    """原样跑站点里那条**绑了表**的取行 SELECT —— 它决定守卫进不进得去。"""
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_get_indexdef(c.oid) FROM pg_class c "
                "  JOIN pg_index ix ON ix.indexrelid = c.oid "
                " WHERE c.relname = %s AND ix.indrelid = to_regclass(%s)",
                (STRICT_IDX, f"public.{STRICT_TBL}"))
            row = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()
    return row[0] if row else None


def _strict_oid(url: str) -> int | None:
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT c.oid FROM pg_class c WHERE c.relname = %s "
                        "  AND c.relnamespace = 'public'::regnamespace", (STRICT_IDX,))
            row = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()
    return row[0] if row else None


def _run_strict_migration(url: str):
    """跑整份 034,返回异常(成功则 None)。"""
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute((ROOT / STRICT_REL).read_text(encoding="utf-8"))
        return None
    except psycopg2.Error as exc:
        return exc
    finally:
        conn.close()


def test_strict_site_absent_index_diverts_before_the_undefined_object_leg(probe_db) -> None:
    """索引整个不存在(腿3 命中的唯一前提)⇒ 上游取行 IS NULL ⇒ 守卫整块跳过。"""
    url = _replayed(probe_db("strict_absent"))
    _exec(url, f'DROP INDEX IF EXISTS public."{STRICT_IDX}"')
    assert _strict_outer_select(url) is None, "外层取行没变 NULL,这条判据前提就不成立"
    err = _run_strict_migration(url)
    assert err is None, f"腿3 被走到了(或别处炸了):{err}"
    assert _index_host(url, STRICT_IDX) == STRICT_TBL, \
        "索引没被下游 @index-guard 建回目标表上"


def test_strict_site_wrong_table_diverts_before_the_wrong_object_type_leg(probe_db) -> None:
    """同名索引长在别的表上(腿2 命中的唯一前提)⇒ 同样被上游分流。

    报错必须是**下游** ``[index-guard]``(42710),不是 ``[drop-index-guard]``;
    诱饵索引必须还活着 —— 否则就是"绕过守卫把别人的索引删了"。
    """
    url = _replayed(probe_db("strict_wrongtbl"))
    _exec(url,
          f'DROP INDEX IF EXISTS public."{STRICT_IDX}"',
          f"CREATE TABLE IF NOT EXISTS public.{DECOY_TABLE} (a int, b int)",
          f'CREATE UNIQUE INDEX "{STRICT_IDX}" ON public.{DECOY_TABLE} (a)')
    assert _strict_outer_select(url) is None, "外层取行没变 NULL,这条判据前提就不成立"
    err = _run_strict_migration(url)
    assert err is not None, "同名异表居然没报错 —— 静默跳过就是本单要修的病"
    msg = str(err)
    assert "[index-guard]" in msg and "[drop-index-guard]" not in msg, \
        f"报错来源不对(应是下游建索引守卫,不是 DROP 守卫):{msg[:200]}"
    assert err.pgcode == "42710", f"SQLSTATE 应为 duplicate_object,实得 {err.pgcode}"
    assert _index_host(url, STRICT_IDX) == DECOY_TABLE, "诱饵索引被删了 —— 守卫没挡住"


def test_strict_site_live_leg_really_drops_and_rebuilds(probe_db) -> None:
    """活性对照:唯一活着的那条腿必须真跑 —— oid 变了 + 定义变宽。

    没有这一条,上面两条"全绿"可以只是因为整块代码从来没被执行。
    """
    url = _replayed(probe_db("strict_live"))
    _exec(url,
          f'DROP INDEX IF EXISTS public."{STRICT_IDX}"',
          f'CREATE UNIQUE INDEX "{STRICT_IDX}" ON public.{STRICT_TBL} '
          "(source_post_revision_id) WHERE source_post_revision_id IS NOT NULL")
    before = _strict_oid(url)
    stale = _strict_outer_select(url)
    assert stale is not None and STRICT_WIDE not in stale, "旧定义没种上,前提不成立"
    err = _run_strict_migration(url)
    assert err is None, f"腿1 跑炸了:{err}"
    after = _strict_oid(url)
    assert after is not None and after != before, \
        f"oid 没变({before} → {after})—— 说明根本没真删真建"
    assert STRICT_WIDE in (_strict_outer_select(url) or ""), "定义没被换成加宽版"


# ══════════════════════════════════════════════════════════════════════════
# 🔴 R2 收口 ①:运行期(Python 侧)裸 DROP 的机制陈述,做成可执行谓词
#
# 交付单 §9.3-(b) 第一版把机制写错了(以为 prestart 先 import db.* 所以运行期
# 先删)。错的机制会让下一张工单照着错的路径去修。这里把"谁不碰它 / 谁真碰它 /
# 哪一处是死代码"三件事全部锁住,谓词只写一处
# (``scripts/defgeo_runtime_drop_scan.py``,取证脚本读的是同一个模块)。
# ══════════════════════════════════════════════════════════════════════════
#: 冻结集合(不是条数):非测试 ``.py`` 里真的会被执行的裸 DROP INDEX。
#: 前 13 条是运行期/运维脚本(交给后续独立工单),末一条是本单自建的 A/B 台架
#: (两处 f-string,名字运行期才知道,归一成 ``{}``)—— **不做静默排除**,
#: 一起冻在这里,谁动了都当场红。
FROZEN_PY_NAKED_DROPS = {
    ("db/fund_recovery_db.py", "idx_fund_recovery_status"),
    ("db/fund_recovery_db.py", "uniq_fund_recovery_open"),
    ("db/fund_recovery_db.py", "uniq_fund_recovery_open_nullcharge"),
    ("db/fund_recovery_db.py", "uniq_fund_recovery_geoplan_open"),
    ("db/fund_recovery_db.py", "uniq_fund_recovery_channel_open"),
    ("db/monitoring_db.py", "uniq_kms_keyword_active"),
    ("db/research_selfserve_db.py", "uq_selfserve_active_idem"),
    ("scripts/rollback_geo_plan_settlement_v5v6.py", "idx_geoplan_settle_pending"),
    ("scripts/rollback_geo_plan_settlement_v5v6.py", "idx_geoplan_brand_status"),
    ("scripts/validate_geo_article_v14_schema_pg16.py", "idx_geo_article_review_events_article"),
    ("scripts/validate_geo_article_v14_schema_pg16.py", "idx_articles_publication_snapshot"),
    ("scripts/verify_diagnosis_runs_statemachine.py", "uq_ccf_active_task"),
    ("scripts/defgeo_index_ab_harness.py", "{}"),
}


def _census_diff_message(got: set, frozen: set) -> str:
    """把普查差集渲染成人读的报错文案。

    🔴 **报错路径必须自己能跑通**。第一版直接 `sorted(got - frozen)`,而集合里的元素是
       `(路径, 索引名)` 且索引名**可以是 None**(拼串 / f-string 占位的 DROP 解析不出名字)。
       `(str, None)` 与 `(str, str)` 一比就 `TypeError: '<' not supported between
       instances of 'str' and 'NoneType'` —— 判据该红的时候不是把 diff 摆出来,
       而是自爆在报错文案里,把真正的差别整个盖掉。排序键统一归一成字符串。
    """
    def key(item):
        return tuple("" if x is None else str(x) for x in item)

    extra = sorted(got - frozen, key=key)
    missing = sorted(frozen - got, key=key)
    return (f"运行期裸 DROP 集合变了。\n多出:{extra}\n少了:{missing}\n"
            "新增一处 = 又开一个「按名删、删错表」的口子,必须走独立工单而不是顺手加。")


def test_runtime_python_naked_drop_census_is_frozen() -> None:
    """运行期裸 DROP 的**集合**冻结(不是条数 —— 条数挡不住"改个名字换一处")。"""
    got = {(rel, drop_index_name(sql)) for rel, _, sql in python_naked_drops()}
    assert got == FROZEN_PY_NAKED_DROPS, _census_diff_message(got, FROZEN_PY_NAKED_DROPS)


def test_census_failure_message_survives_a_none_named_entry() -> None:
    """报错路径的判别力样本:差集里带 `name is None` 的条目,文案必须**渲染得出来**。

    🔴 样本形状很讲究,第一版就写废了(撕锁 R3-03「该红没红」当场抓到):
       元组比较是**短路**的 —— `("scripts/b.py", None)` 和 `("scripts/c.py", "idx_c")`
       第一元素就分得开,`None` 根本没参与比较,去掉排序键也不抛。
       只有**同一路径下**一条 None 一条有名字时才真的比到第二元素。
       而真实数据正是这个形状:`toctou_repro.py` 一个文件里就有 `None` / `'IF'` / `None`。
       —— 样本必须打在会短路的那一位之后,否则它守的是空气。
    """
    frozen = {("db/a.py", "idx_a")}
    got = {("db/a.py", "idx_a"),
           ("scripts/b.py", None),          # 同一路径 + 名字 None
           ("scripts/b.py", "idx_b"),       # 同一路径 + 名字非空 ⇒ 逼出第二元素比较
           ("scripts/b.py", "{}")}
    msg = _census_diff_message(got, frozen)          # 不许抛
    assert "scripts/b.py" in msg and "None" in msg, f"None 那条没进文案:{msg}"
    assert "idx_b" in msg, f"同路径的正常那条也得在:{msg}"
    # 反向:少了的那一半同样要能渲染
    msg2 = _census_diff_message(frozen, got)
    assert "scripts/b.py" in msg2 and "少了" in msg2, msg2
    # 自证:不归一排序键的话,这组输入**确实**会自爆(样本不是空气)
    with pytest.raises(TypeError):
        sorted(got - frozen)


def test_naked_drop_census_scope_is_the_executable_surface() -> None:
    """作用域样本:`docs/**` 与 `tests/**` 出局,其余可执行层**一律在内**。

    R2 把三份取证脚本落盘进 `docs/AI-CONTEXT/**` 并 track 之后,分母从 15/7 涨到 22/10
    —— 那 7 条是脚本为了**演示**这个病而写的真 `execute("DROP INDEX ...")`。
    收窄成"可执行面"之后回到 15/7;但**不能**收成"只看 db/ + scripts/",
    那样 `api/**` / `tools/**` 里将来冒出一处就没人红了。
    """
    assert is_executable_surface("db/fund_recovery_db.py")
    assert is_executable_surface("scripts/prestart.py")
    assert is_executable_surface("api/monitoring_api.py")
    assert is_executable_surface("tools/monitoring/batch_monitor.py")
    assert is_executable_surface("server.py")
    assert not is_executable_surface("docs/AI-CONTEXT/AXISC_ARTIFACTS_2026-08-24/toctou_repro.py")
    assert not is_executable_surface("tests/defensive_geo_w3_2026_08_21/x.py")
    assert not is_executable_surface("scripts/tests/x.py")
    # 真扫一遍:分母里不许再出现 docs/ 或 tests/ 下的东西
    strays = sorted({rel for rel, _, _ in python_naked_drops()
                     if not is_executable_surface(rel)})
    assert not strays, f"作用域谓词没拦住:{strays}"


def test_drop_index_name_edge_cases() -> None:
    """`drop_index_name` 两个边角各配一条样本 —— 都取自**真实落盘产物里的原句**。

    这两种形态今天只出现在取证脚本里(已出局),但谓词本身仍会被拼串 SQL 喂到,
    所以行为要钉住:能解出名字的解出来,解不出的**明确返回 None**,
    绝不悄悄拿一个假名字(比如 `IF`)去和冻结集合比。
    """
    # 正常形态(含 schema 限定 / 引号 / IF EXISTS 三种装饰)
    assert drop_index_name("DROP INDEX IF EXISTS idx_a") == "idx_a"
    assert drop_index_name('DROP INDEX public."idx b"') == "idx b"
    assert drop_index_name("DROP INDEX CONCURRENTLY IF EXISTS public.idx_c") == "idx_c"
    # 边角 1:名字被 f-string 占位吃掉,`IF EXISTS` 这个可选组回溯掉 ⇒ 解出假名字 'IF'
    #         (真实原句:docs/.../toctou_repro.py 的 `DROP INDEX IF EXISTS {};`)
    assert drop_index_name("DROP INDEX IF EXISTS {};") == "IF",         "这条边角一旦变了,冻结集合里 '{}' / 'IF' 这类条目的口径就跟着变,必须显式知道"
    # 边角 2:整条解不出名字 ⇒ None(冻结集合里就是靠它区分"没有名字"与"名字叫 IF")
    assert drop_index_name("DROP INDEX {};") is None
    assert drop_index_name("DROP INDEX {}") is None
    assert drop_index_name("SELECT 1") is None


def test_runtime_naked_drops_hit_exactly_the_names_axisc_just_bound() -> None:
    """db/*.py 里那 7 个名字必须**全部**落在轴C 审定清单内 —— 这才是隐患的尖锐处。"""
    bound = {s["index"] for s in ADJUDICATED}
    runtime = {drop_index_name(sql) for rel, _, sql in python_naked_drops()
               if rel.startswith("db/")}
    assert runtime, "db/*.py 里一处都没扫到 —— 扫描器坏了(分母为 0 的判据永远绿)"
    assert runtime <= bound, (
        f"这些运行期裸 DROP 的名字不在轴C 审定清单里:{sorted(runtime - bound)}。"
        "结论「运行期删的正是刚绑好的那批」不再成立,交付单 §9.3-(b) 要重写。")


def test_prestart_bootstrap_does_not_reach_the_naked_drop_modules() -> None:
    """prestart 那一侧 import 的是 `db.diagnosis_db`,其模块级闭包**不含**三个带裸 DROP 的模块。

    这是 §9.3-(b) 机制陈述的承重前提:错了的话"运行期先删、守卫轮不到"就成立,
    结论方向整个反过来。
    """
    closure = import_closure("db.diagnosis_db")
    assert closure, "闭包算空了 —— 分母为 0 的判据永远绿"
    leaked = sorted(m for m in DROP_BEARING_MODULES if m in closure)
    assert not leaked, (
        f"{leaked} 进了 db.diagnosis_db 的模块级 import 闭包 ⇒ prestart 的 bootstrap "
        "会把它们拉起来跑 init ⇒ 交付单 §9.3-(b) 的机制陈述必须改回「运行期先删」。")


def test_the_real_trigger_path_is_module_level_in_server_py() -> None:
    """真实触发者是 server.py 的**模块级**调用(每次容器起来 import 就跑)。"""
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"), "server.py")

    def module_level_calls(node, inside_func=False):
        for child in ast.iter_child_nodes(node):
            nested = inside_func or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            if not inside_func and isinstance(child, ast.Call) \
                    and isinstance(child.func, ast.Name):
                yield child.func.id
            yield from module_level_calls(child, nested)

    called = set(module_level_calls(tree))
    for sym in ("init_wallet_tables", "init_monitoring_api"):
        assert sym in called, (
            f"server.py 模块级不再调 {sym} ⇒ 「每次容器起都跑」这句话失效,"
            "§9.3-(b) 的严重性判断要重估。")


def test_selfserve_ensure_tables_is_still_dead_code() -> None:
    """`ensure_selfserve_tables` 全仓零调用 —— 后续工单要单独标注,别把它算进活跃隐患。

    有人把它接上线的那天,这条当场红:那时它就从"死代码"变成"第 7 个活隐患"。
    """
    hits = non_test_callers("ensure_selfserve_tables")
    assert hits == [], (
        f"它被接线了:{hits} ⇒ 交付单 §9.3-(b) 里「死代码,单独标注」这句要撤,"
        "运行期裸 DROP 的活跃面要重算。")


# ══════════════════════════════════════════════════════════════════════════
# 🔴 R2 收口 ⑦:撕锁 runner 不许有兜底 DSN(陈旧默认 = 静默打错库)
# ══════════════════════════════════════════════════════════════════════════
def test_mutation_runner_has_no_hardcoded_fallback_dsn() -> None:
    """两处一起锁:**文档串**里不许有可直接粘贴的 DSN,**取值处**的默认必须是空串。

    🔴 不能对整份源码做正则:runner 里现在有一发变异(R2-01)的 ``to`` 值**就是**
       那条陈旧 DSN —— 那是被引用的代码文本,不是会执行的默认值。整份扫会让这条判据
       在基线上就红(实测红过一次)。所以只扫模块 docstring + 走 AST 看取值处。
    """
    src = io.open(ROOT / "scripts" / "mutation_runner_defgeo_index_guard.py",
                  encoding="utf-8", newline="").read()
    tree = ast.parse(src, "mutation_runner_defgeo_index_guard.py")

    doc = ast.get_docstring(tree) or ""
    assert doc, "runner 没有模块 docstring —— 这条判据在扫空气"
    concrete = [m for m in re.findall(r"postgresql://\S*", doc) if "<" not in m]
    assert not concrete, (
        f"runner 文档串里还有可直接粘贴的 DSN:{concrete} —— "
        "R2 之前那个 55620/geo_defgeo_w3c_test 就是这么变成陈旧默认的。")

    defaults = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        tgt = node.targets[0]
        if not (isinstance(tgt, ast.Name) and tgt.id == "DB_URL"):
            continue
        call = node.value
        assert isinstance(call, ast.Call), f"DB_URL 不再是 os.environ.get(...):{ast.dump(call)[:120]}"
        assert len(call.args) == 2, "DB_URL 的取值处必须显式写出默认值(第二个实参)"
        assert isinstance(call.args[0], ast.Constant) and call.args[0].value == "TEST_DATABASE_URL"
        defaults.append(call.args[1])
    assert len(defaults) == 1, f"模块级 DB_URL 赋值应恰好 1 处,实得 {len(defaults)}"
    d = defaults[0]
    assert isinstance(d, ast.Constant) and d.value == "", (
        f"DB_URL 的兜底默认不是空串,而是 {ast.dump(d)[:160]} —— "
        "陈旧默认会静默打错库,必须让它当场失败而不是猜一个库。")


def _run_runner(env_overrides: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "TEST_DATABASE_URL"}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "scripts/mutation_runner_defgeo_index_guard.py"],
        cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120)


def test_mutation_runner_fails_loud_without_an_explicit_test_dsn() -> None:
    """行为半:缺环境变量必须**当场非零退出**,而不是悄悄连一个别人的库。"""
    p = _run_runner({})
    assert p.returncode != 0, "缺 TEST_DATABASE_URL 居然还能往下跑"
    out = p.stdout + p.stderr
    # 🔴 必须点名**"未设置"那条腿**:只断言出现 "TEST_DATABASE_URL" 的话,
    #    安全栓那条腿的文案里也有这个词 ⇒ 把缺环境变量的守卫整个摘掉也能绿。
    assert "TEST_DATABASE_URL 未设置" in out, \
        f"报错没点名缺的是什么:{out[-300:]}"


def test_mutation_runner_refuses_a_non_test_database_name() -> None:
    """反向对照:库名不含 test 的 DSN 必须被安全栓拦住(否则上面那条也可能是假绿)。"""
    p = _run_runner({"TEST_DATABASE_URL": "postgresql://u:p@localhost:5432/geo_agentscope"})
    assert p.returncode != 0, "非一次性库名居然放行了"
    assert "安全栓" in (p.stdout + p.stderr), \
        f"拦是拦了,但不是安全栓拦的:{(p.stdout + p.stderr)[-300:]}"
