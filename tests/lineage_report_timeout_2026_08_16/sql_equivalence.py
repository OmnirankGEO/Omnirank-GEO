"""[P0 血缘三报 2026-08-16] 改写前/改写后 SQL **逐行同结果** 机械对照。

判据(成对):
  - 必须命中:同一夹具库、同一组参数下,旧 SQL 与新 SQL 的结果集
    **按查询返回顺序逐行、逐列**完全一致(rn 对齐 + 全列 IS DISTINCT FROM 比较)。
  - 必须不命中(反向对照):把新 SQL 注入一处**已知会改变结果**的扰动
    (GROUP BY 折叠时误用 MIN 而非 MAX),对照必须**转红**。
    单向断言证明不了判别力 —— 见工具箱 README「自验本身也会自造恒真」。

旧 SQL 取自生产尖 git 对象,不读工作树(工作树已被本包改过)。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

PROD_TIP = "72faac33c376e70c66bb4caf6eab0c90f7827d64"
def _repo_root() -> str:
    """向上找 `.git` 锚定仓根。

    🔴 原来写 `dirname(dirname(__file__))` —— 那是脚本还待在 `.probe/` 时的层数。
    入库到 `tests/lineage_report_timeout_2026_08_16/` 后只剥到 `tests/`,
    于是从**任何** cwd 跑都是 `No module named 'services'` / 打开 `tests/services/...`。
    交付单里的数字出自搬家**前**那份,交付树上这份根本跑不起来 —— Review 抓到的就是这个。
    锚定 `.git` 后再搬一次家也不会复发。
    注:worktree 里 `.git` 是**文件**不是目录,必须用 `exists` 不能用 `isdir`。
    """
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            raise SystemExit("找不到仓根(一路向上都没有 .git)")
        d = parent


REPO = _repo_root()
# 判据可用性第 0 关:REPO 错了后面全是「找不到模块」这类误导性失败,先响亮地炸掉。
if not os.path.isfile(os.path.join(REPO, "services", "article_structure_analysis.py")):
    raise SystemExit(f"仓根判定错误:{REPO} 下没有 services/article_structure_analysis.py")
TARGET = "services/article_structure_analysis.py"
CONTAINER = "lineage-prodshape-pg"


class SetupError(RuntimeError):
    """夹具/SQL 根本没跑起来 —— 与「跑起来了但结果不同」严格区分。"""


def _extract(src: str, name: str) -> str:
    m = re.search(rf'{name} = """(.*?)"""', src, re.S)
    if not m:
        raise SystemExit(f"抽不到 {name}")
    return m.group(1)


def old_source() -> str:
    return subprocess.run(
        ["git", "show", f"{PROD_TIP}:{TARGET}"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout


def new_source() -> str:
    with open(os.path.join(REPO, TARGET), encoding="utf-8") as fh:
        return fh.read()


def bind(raw: str, vals: list[str]) -> str:
    parts = raw.split("%s")
    if len(parts) - 1 != len(vals):
        raise SystemExit(f"参数个数不匹配: SQL 需要 {len(parts)-1},给了 {len(vals)}")
    out = []
    for i, part in enumerate(parts):
        out.append(part)
        if i < len(vals):
            out.append(vals[i])
    return "".join(out)


def psql(sql: str) -> tuple[int, str]:
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    p = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-U", "geo_admin",
         "-d", "geo_agentscope", "-v", "ON_ERROR_STOP=1", "-tAF|", "-P", "pager=off"],
        input=sql, capture_output=True, text=True, encoding="utf-8", env=env,
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture_multiurl.sql"),
          encoding="utf-8") as _fh:
    FIXTURE_SQL = _fh.read()


def compare(old_sql: str, new_sql: str, label: str, *, fixture: bool = False) -> tuple[bool, str]:
    """两侧各自按**查询返回顺序**编号,再逐行逐列比。任何差异 → 计数 > 0。

    fixture=True 时先注入构造夹具(见 fixture_multiurl.sql):真数据里跨 URL 的
    信号值恒相同,不注入夹具的话本对照对「折叠是否正确」零判别力。
    全程事务内 + ROLLBACK,不落库。
    """
    sql = f"""
BEGIN;
SET LOCAL statement_timeout = '20min';
{FIXTURE_SQL if fixture else ''}
CREATE TEMP TABLE t_old AS SELECT row_number() OVER () AS __rn, * FROM ({old_sql}) s;
CREATE TEMP TABLE t_new AS SELECT row_number() OVER () AS __rn, * FROM ({new_sql}) s;
SELECT 'rows_old=' || (SELECT count(*) FROM t_old)
    || ' rows_new=' || (SELECT count(*) FROM t_new)
    || ' old_minus_new=' || (SELECT count(*) FROM (SELECT * FROM t_old EXCEPT ALL SELECT * FROM t_new) z)
    || ' new_minus_old=' || (SELECT count(*) FROM (SELECT * FROM t_new EXCEPT ALL SELECT * FROM t_old) z)
    || ' order_mismatch=' || (
        SELECT count(*) FROM t_old o FULL JOIN t_new n USING (__rn)
         WHERE (o.*) IS DISTINCT FROM (n.*)
       );
ROLLBACK;
"""
    rc, out = psql(sql)
    line = ""
    for ln in out.splitlines():
        if ln.startswith("rows_old="):
            line = ln.strip()
    if rc != 0 or not line:
        # 🔴 SQL 没跑起来 ≠ 判据看见了差异。第一版把这两件事都当"红",
        #    于是夹具 INSERT 撞 CHECK 约束时,5 条反向对照全部"按预期转红" ——
        #    红得完全不是因为毒被抓到。必须先分「跑没跑起来」再分「过没过」。
        raise SetupError(f"{label}: psql rc={rc} :: {out.strip()[:500]}")
    ok = (" old_minus_new=0 " in line + " "
          and " new_minus_old=0 " in line + " "
          and line.endswith("order_mismatch=0"))
    nums = dict(kv.split("=") for kv in line.split())
    ok = (nums["old_minus_new"] == "0" and nums["new_minus_old"] == "0"
          and nums["order_mismatch"] == "0" and int(nums["rows_old"]) == int(nums["rows_new"]))
    return ok, f"{label}: {line}"


def main() -> int:
    o_src, n_src = old_source(), new_source()
    o_all, n_all = _extract(o_src, "ARTICLE_STRUCTURE_ALL_SQL"), _extract(n_src, "ARTICLE_STRUCTURE_ALL_SQL")
    o_ind, n_ind = _extract(o_src, "ARTICLE_STRUCTURE_SQL"), _extract(n_src, "ARTICLE_STRUCTURE_SQL")

    if o_all == n_all and o_ind == n_ind:
        print("🔴 旧新 SQL 逐字相同 —— 对照零判别力(包没改到东西?)")
        return 3

    grades = ["'JC5'", "'JC3'", "'JC0'"]
    limits = ["50", "300", "1000"]
    minchars = ["0", "500"]
    industries = ["'装修建材'", "'金融理财'", "'医疗健康'", "'GEO 优化服务'", "'不存在的行业xyz'"]

    results: list[tuple[bool, str]] = []

    def guarded(*a, **kw):
        try:
            return compare(*a, **kw)
        except SetupError as exc:
            return False, f"🔴 SETUP FAILED :: {exc}"

    for g in grades:
        for lim in limits:
            for mc in minchars:
                results.append(guarded(bind(o_all, [g, mc, lim]),
                                       bind(n_all, [g, mc, lim]),
                                       f"ALL grade={g} limit={lim} min_chars={mc}"))
    for ind in industries:
        for g in grades:
            for lim in ["50", "300"]:
                arr = f"ARRAY[{ind}]"
                results.append(guarded(bind(o_ind, [ind, arr, arr, g, "500", lim]),
                                       bind(n_ind, [ind, arr, arr, g, "500", lim]),
                                       f"IND ind={ind} grade={g} limit={lim}"))

    # ---- 正向:构造夹具上(跨 URL 值真的不同)也必须逐行同结果 ----
    fixture_rows: list[tuple[bool, str]] = []
    for lim in ["50", "300", "1000"]:
        fixture_rows.append(guarded(bind(o_all, ["'JC5'", "500", lim]),
                                    bind(n_all, ["'JC5'", "500", lim]),
                                    f"[夹具] ALL grade='JC5' limit={lim}", fixture=True))
    arr = "ARRAY['装修建材']"
    for lim in ["50", "300"]:
        fixture_rows.append(guarded(bind(o_ind, ["'装修建材'", arr, arr, "'JC5'", "500", lim]),
                                    bind(n_ind, ["'装修建材'", arr, arr, "'JC5'", "500", lim]),
                                    f"[夹具] IND ind='装修建材' limit={lim}", fixture=True))
    results.extend(fixture_rows)

    # ---- 反向对照:三种**错误折叠**,在构造夹具上必须全部转红 ----
    # 锚点必须真的命中,否则「注入了个寂寞」→ 恒绿。
    poisons = [
        ("MIN 替 MAX(is_adopted)",
         "MAX(COALESCE(sig.is_adopted, 0)) AS is_adopted",
         "MIN(COALESCE(sig.is_adopted, 0)) AS is_adopted"),
        ("MIN 替 MAX(source_weight)",
         "MAX(COALESCE(sig.source_weight, 0)) AS source_weight",
         "MIN(COALESCE(sig.source_weight, 0)) AS source_weight"),
        ("SUM 替 MAX(source_weight)",
         "MAX(COALESCE(sig.source_weight, 0)) AS source_weight",
         "SUM(COALESCE(sig.source_weight, 0)) AS source_weight"),
        ("MIN 替 MAX(engine_count)",
         "MAX(COALESCE(sig.engine_count, 0)) AS engine_count",
         "MIN(COALESCE(sig.engine_count, 0)) AS engine_count"),
        ("MIN 替 MAX(is_adopted_legacy)",
         "MAX(COALESCE(sig.is_adopted_legacy, 0)) AS is_adopted_legacy",
         "MIN(COALESCE(sig.is_adopted_legacy, 0)) AS is_adopted_legacy"),
    ]
    neg_results: list[tuple[bool, str]] = []
    for name, anchor, repl in poisons:
        if anchor not in n_all:
            neg_results.append((False, f"NEG {name}: 🔴 锚点未命中,注入失败"))
            continue
        poisoned = n_all.replace(anchor, repl)
        try:
            same, msg = compare(bind(o_all, ["'JC5'", "500", "300"]),
                                bind(poisoned, ["'JC5'", "500", "300"]),
                                f"NEG {name}", fixture=True)
        except SetupError as exc:
            # 跑不起来 = 判别力未证明,绝不算"抓到了"
            neg_results.append((False, f"NEG {name}: 🔴 SQL 未跑起来,判别力未证明 :: {exc}"))
            continue
        # same==True 表示「毒了还一样」= 判据没看见 = 坏
        neg_results.append((not same, msg))

    passed = sum(1 for ok, _ in results if ok)
    for ok, msg in results:
        print(("  ✅ " if ok else "  ❌ ") + msg)
    print(f"\n正向对照 {passed}/{len(results)} 组逐行同结果(含 {len(fixture_rows)} 组构造夹具)")
    print("\n反向对照(每条都必须转红,否则判据恒真):")
    neg_pass = sum(1 for ok, _ in neg_results if ok)
    for ok, msg in neg_results:
        print(("  ✅ 转红 " if ok else "  🔴 仍绿 ") + msg)
    print(f"  → {neg_pass}/{len(neg_results)} 条反向对照被判据抓到")

    if passed != len(results):
        print("\n🔴 等价对照未全绿 → 改写改变了报表口径,不可上车")
        return 1
    if neg_pass != len(neg_results):
        print("\n🔴 有反向对照没转红 → 判据零判别力,本次等价证明作废")
        return 2
    print("\n✅ 等价成立(真数据 + 构造夹具)+ 判据有判别力(5/5 错误折叠全被抓)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
