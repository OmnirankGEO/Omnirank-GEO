"""E3-1 补洞 · 发车前置证据脚本(census + 一次性回填)**必须有读取者**。

为什么补这一条(外选 MUT-EXTE3-05 存活坐实的洞)
------------------------------------------------
`scripts/backfill_monitoring_cell_tenant_owner_2026_08_26.sql` 是发车三步走的
第①②步:Deploy 跑 §1 把四个数抄进发车记录,`attempts_tenant_zero > 0` 就停下报
Review。外选变异把 §1b 的探测口径从 ``<= 0`` 弱化成 ``< 0``,**全分母零红** ——
因为全仓(tests/ + scripts/)对这个文件**一个读取者都没有**。

它被刻意排除在 `migration_manifest` 之外(重放一次就等于拿当天的 owner 去补新
出现的 NULL,正是迁移 054 要消灭的漂移),于是也顺带被排除在所有迁移判据之外。
"不在清单里" ≠ "不用守":这个文件出错的后果是**发车判断依据是错的**——
编造租户 0 的存量行被报成 0 行,Deploy 放行,046 的 CHECK 在 prestart 才炸,
或者更糟:根本不炸,错归属永久留存。

本文件的分母纪律
----------------
🔴 SQL **不手抄**,从脚本文件里机械抽出来执行 —— 手抄的那份会在脚本改动那天
   静默漂移,而判据照绿。抽取规则自己也有一条自证(e5_01):抽不出恰两条
   可执行语句就当场红。
🔴 判据把库摆成**生产当前**的形态(046 未部署 ⇒ 账本表上没有禁 0 的 CHECK),
   否则"编造 0 的存量行"根本插不进来 —— 那样测的就不是脚本,是 CHECK。
   摆布全部在 SAVEPOINT 里,跑完逐字还原,不给后面的判据留残留。
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]

SCRIPT_REL = "scripts/backfill_monitoring_cell_tenant_owner_2026_08_26.sql"
LEDGER = "defgeo_monitoring_attempts"
ZERO_GUARD = "chk_defgeo_attempt_tenant_owner_positive"

BRAND = 7701
BRAND_NO_OWNER = 7702
OWNER = 4242
SETTLED_BY = 6060


# ══════════════════════════════════════════════════════════════════════
# 机械抽取 —— 判据执行的是脚本自己的 SQL,不是它的副本
# ══════════════════════════════════════════════════════════════════════

def _statements() -> list[str]:
    """把脚本切成可以直送 psycopg2 的语句。

    规则只有三条,都可机械复核:
      · 丢掉 psql 元命令(``\\echo``)—— psycopg2 直送会 syntax error;
      · 丢掉**整行**注释 —— §2 的回填被整体注掉,因此天然不在结果里
        (这正是脚本要的:§2 由 Deploy 手工放开,判据不许替它跑);
      · 按 ``;`` 切。
    """
    raw = (ROOT / SCRIPT_REL).read_text(encoding="utf-8")
    kept = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("\\") or stripped.startswith("--"):
            continue
        kept.append(line)
    return [s.strip() for s in "\n".join(kept).split(";") if s.strip()]


def _census_sql(tag: str) -> str:
    hits = [s for s in _statements() if tag in s]
    assert len(hits) == 1, f"脚本里含 {tag!r} 的语句有 {len(hits)} 条,抽取规则失效"
    return hits[0]


@contextlib.contextmanager
def _as_production_looks_today(cur):
    """把账本摆成 046 **还没部署**的样子:没有禁 0 的 CHECK。

    §1b 要回答的问题就是"046 部署之前,存量里有没有编造 0 的行" ——
    在一个已经装了 CHECK 的库上问这句话是问不出来的(插都插不进去)。
    """
    cur.execute("SAVEPOINT e5_no_guard")
    cur.execute(f"ALTER TABLE public.{LEDGER} DROP CONSTRAINT {ZERO_GUARD}")
    try:
        yield
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT e5_no_guard")
        cur.execute("RELEASE SAVEPOINT e5_no_guard")


def _seed_attempt(cur, *, tenant: int, seed: str) -> None:
    """插一条账本行。``tenant`` 允许 0 / 负数 —— 调用方负责先摘掉 CHECK。"""
    cur.execute(
        f"""
        INSERT INTO public.{LEDGER}
            (attempt_id, plan_cell_id, attempt_ordinal, run_authority_id,
             tenant_owner_user_id, brand_id, actual_provider, actual_model,
             actual_surface, actual_search_mode, request_hash, ledger_version)
        VALUES (%s, %s, 1, 'run', %s, %s,
                'dashscope', 'planned-model', 's', 'sm', 'rh', 'v')
        """,
        (seed * 64, seed * 64, tenant, BRAND))


def _seed_cell(cur, *, task_id: int, keyword_id: int, tenant: int | None,
               settlement_reference: str | None, brand_id: int = BRAND) -> str:
    """建一格。

    🔴 ``settlement_reference`` 必须在 INSERT 那一刻给定:
       ``trg_monitoring_run_cell_terminal_overwrite`` 把它编进"计划不可变"的
       ROW() 比对里,事后 UPDATE 会被 RAISE 掉。
    """
    import hashlib
    import uuid
    ph = hashlib.sha256(
        f"{task_id}:{keyword_id}:{tenant}:{settlement_reference}".encode()
    ).hexdigest()
    cur.execute(
        """
        INSERT INTO public.monitoring_run_cells
            (task_id, brand_id, keyword_id, keyword_source, keyword_snapshot,
             question_snapshot, target_brand_snapshot, platform, is_planned,
             state, entitlement_snapshot, order_snapshot, fulfillment_credential,
             fulfillment_state, plan_hash, tenant_owner_user_id,
             settlement_reference)
        VALUES (%s,%s,%s,'confirmed','kw','q','tb','dashscope',TRUE,
                'queued',
                '{"schema_version":"monitoring-entitlement-snapshot-v1"}'::jsonb,
                '{"schema_version":"monitoring-order-snapshot-v1"}'::jsonb,
                %s,'reserved',%s,%s,%s)
        RETURNING plan_hash
        """,
        (task_id, brand_id, keyword_id, str(uuid.uuid4()), ph, tenant,
         settlement_reference))
    return str(cur.fetchone()["plan_hash"])


def _seed_settlement(cur, *, reference: str, task_id: int, keyword_id: int,
                     billing_user_id: int | None, brand_id: int = BRAND) -> None:
    cur.execute(
        """
        INSERT INTO public.monitoring_keyword_settlements
            (settlement_reference, task_id, brand_id, keyword_id,
             keyword_source, billing_user_id, feature_code)
        VALUES (%s,%s,%s,%s,'confirmed',%s,'monitoring_run')
        """,
        (reference, task_id, brand_id, keyword_id, billing_user_id))


def _brand(cur, *, brand_id: int, owner: int | None) -> None:
    cur.execute(
        "INSERT INTO public.brands (id, name, owner_user_id) VALUES (%s,%s,%s) "
        "ON CONFLICT (id) DO UPDATE SET owner_user_id = EXCLUDED.owner_user_id",
        (brand_id, f"brand-{brand_id}", owner))


def _task(cur, *, brand_id: int = BRAND) -> int:
    cur.execute(
        "INSERT INTO public.monitoring_tasks (brand_id, total_tests) "
        "VALUES (%s,0) RETURNING id", (brand_id,))
    return int(cur.fetchone()["id"])


# ══════════════════════════════════════════════════════════════════════
# ① 抽取规则自证 —— 抽不出来就当场红,不许静默跑零条
# ══════════════════════════════════════════════════════════════════════

def test_e5_01_the_script_yields_exactly_two_executable_census_statements():
    """脚本里**可执行**的只有两条 census,§2 回填必须仍是注释。

    🔴 这条是上面所有判据的活性栓:抽取规则一旦失效(脚本换了写法、
       §2 被人取消注释),下面几条会在**零条语句**上假绿,或者更糟——
       判据自己把生产回填跑了。
    """
    stmts = _statements()
    assert len(stmts) == 2, (
        f"从 {SCRIPT_REL} 抽出 {len(stmts)} 条可执行语句(期望 2)。"
        f"§2 被取消注释了?抽取规则过时了?实得:{[s[:60] for s in stmts]}")
    # 🔴 [V4-C] §2 退役之后,脚本的可执行部分**全部**是只读的 ——
    #    不再有"由 Deploy 手工放开"的那一条。第一条是 CTE(WITH ... SELECT)。
    assert all(s.upper().startswith(("SELECT", "WITH")) for s in stmts), (
        "抽出了非只读语句 —— 本脚本已退役回填,可执行部分必须全是只读:"
        f"{[s[:60] for s in stmts]}")
    assert _census_sql("cells_total") and _census_sql("attempts_tenant_zero")


def test_e5_02_the_one_shot_script_is_not_in_the_migration_manifest():
    """🔴 它**绝不许**进 manifest —— 文件头把这一条写成红线,这里执行它。

    prestart 每次部署无条件重放全部迁移;这个脚本读的是**当前**
    ``brands.owner_user_id``,重放一次就等于拿当天的 owner 去补新出现的
    NULL 行,正好把迁移 054 要消灭的"归属随品牌转移漂移"以另一种形态请回来。
    """
    from db.migration_manifest import MIGRATIONS

    listed = [m for m in MIGRATIONS if "backfill_monitoring_cell_tenant_owner" in m]
    assert not listed, (
        f"一次性回填脚本被放进 migration_manifest 了:{listed} —— "
        "prestart 会每次部署重放它,拿当天的 owner 补 NULL,归属漂移复辟")


# ══════════════════════════════════════════════════════════════════════
# ② §1b:「账本里有没有编造租户 0 的存量行」—— 发车 stop-the-line 的那个数
# ══════════════════════════════════════════════════════════════════════

def test_e5_10_the_zero_census_sees_every_fabricated_tenant(cur):
    """0 与负数都是**编造**,都必须被数进来。

    🔴 这个数 > 0 时 Deploy 必须停下报 Review。数错了的后果不是"少报一条":
       046 的 CHECK 会让 prestart 非零退出(发车当场断),或者这批错归属的行
       永久留在账本里,而所有按租户对账/计价的口径都从它取数。
    """
    sql = _census_sql("attempts_tenant_zero")
    with _as_production_looks_today(cur):
        _seed_attempt(cur, tenant=0, seed="a")     # 编造:取不到租户时的 0
        _seed_attempt(cur, tenant=-1, seed="b")    # 编造:更明显的那一种
        _seed_attempt(cur, tenant=OWNER, seed="c")  # 健康行,不该被数
        cur.execute(sql)
        got = int(cur.fetchone()["attempts_tenant_zero"])
    assert got == 2, (
        f"§1b 报了 {got} 条编造租户,库里实际有 2 条(0 与 -1)。"
        "口径漏掉哪一种,发车前的 stop-the-line 判断就是错的")


def test_e5_11_a_bare_zero_alone_is_enough_to_stop_the_line(cur):
    """单独把「恰好是 0」那一批拎出来打一发。

    🔴 分开写是有原因的:``<= 0`` 弱化成 ``< 0`` 时,负数那一批照样被数到,
       只有 **0** 这一批变盲 —— 而 0 恰恰是"取不到租户就落 0"这个原病灶
       产出的形态,也就是这个 census 最该看见的那一批。
    """
    sql = _census_sql("attempts_tenant_zero")
    with _as_production_looks_today(cur):
        _seed_attempt(cur, tenant=0, seed="d")
        cur.execute(sql)
        got = int(cur.fetchone()["attempts_tenant_zero"])
    assert got == 1, (
        f"账本里有一条 tenant=0 的编造行,§1b 却报 {got} —— "
        "「取不到租户就落 0」的存量行对发车 census 变盲了")


def test_e5_12_a_healthy_ledger_reports_zero(cur):
    """判别力自证:全是健康行时必须报 0,否则上面两条只是"恒返正数"。"""
    sql = _census_sql("attempts_tenant_zero")
    with _as_production_looks_today(cur):
        _seed_attempt(cur, tenant=OWNER, seed="e")
        _seed_attempt(cur, tenant=1, seed="f")
        cur.execute(sql)
        got = int(cur.fetchone()["attempts_tenant_zero"])
    assert got == 0, f"健康账本被报成 {got} 条编造行 —— 发车会被假警报卡住"


def test_e5_13_the_missing_table_branch_returns_the_documented_sentinel(cur):
    """046 **还没部署**时,§1b 必须给 Deploy 一个数(-1),不是抛异常。

    🔴 这条判据 08-27 上午是**反着**写的("抛 UndefinedTable" 的已知缺陷钉子)。
       当时 criteria-only,只钉不改;Review 尾单批了修脚本,于是同笔把它翻成正向。

    为什么原来会抛:`to_regclass(...) IS NULL` 是**运行期**判断,而 CASE 两条腿里
    的表名要在**计划期**解析 —— 那一步在任何行被求值之前就发生了。
    修法:计数改走 ``query_to_xml`` 的动态执行(表名在字符串里,计划期不解析)。

    发车语义(两个值不许混):
      · **-1** = 表不存在 ⇒ "046 还没上,这一步什么都没证到";
      · **0**  = 表在、且账本上没有编造租户行(见 e5_12)。
    合成一个数会让"还没验"冒充"验过了没问题" —— 那正是发车前最贵的一种误读。
    """
    sql = _census_sql("attempts_tenant_zero")
    cur.execute("SAVEPOINT e5_drop")
    try:
        cur.execute(f"DROP TABLE public.{LEDGER} CASCADE")
        cur.execute(sql)
        got = int(cur.fetchone()["attempts_tenant_zero"])
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT e5_drop")
        cur.execute("RELEASE SAVEPOINT e5_drop")
    assert got == -1, (
        f"账本表不存在时 §1b 返 {got},脚本自陈的哨兵值是 -1 —— "
        "Deploy 拿不到数就没法执行发车三步走的第②步")


def test_e5_14_the_sentinel_and_a_clean_ledger_are_two_different_numbers(cur):
    """判别力自证:-1(表不存在)与 0(表在、账本干净)必须**不同**。

    没有这一条,上一条可以靠"这条 SQL 在任何情况下都返 -1"通过,
    而那会让「046 已部署且干净」被读成「046 还没上」—— 方向相反的两种误读。
    """
    sql = _census_sql("attempts_tenant_zero")
    with _as_production_looks_today(cur):          # 表在,账本干净
        cur.execute(sql)
        present = int(cur.fetchone()["attempts_tenant_zero"])
    cur.execute("SAVEPOINT e5_drop2")
    try:
        cur.execute(f"DROP TABLE public.{LEDGER} CASCADE")
        cur.execute(sql)
        absent = int(cur.fetchone()["attempts_tenant_zero"])
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT e5_drop2")
        cur.execute("RELEASE SAVEPOINT e5_drop2")
    assert (present, absent) == (0, -1), (
        f"表在时={present} / 表不在时={absent} —— 两种状态必须给出不同的数,"
        "否则「还没验」会冒充「验过了没问题」")


# ── 通用小工具(V4-C:§2 退役后,判据改为"跑整脚本 + 看分类")──────────────
def _tenant_of(cur, plan_hash: str):
    cur.execute("SELECT tenant_owner_user_id FROM public.monitoring_run_cells"
                " WHERE plan_hash = %s", (plan_hash,))
    return cur.fetchone()["tenant_owner_user_id"]


def _run_whole_script(cur) -> None:
    """把脚本里**全部可执行语句**跑一遍。

    🔴 「脚本不再改任何归属」这句话,只有把整脚本真跑一遍才算验过 ——
       只跑 census 那一条等于自己挑了个不会写的语句来证明"没写"。
       抽取规则与 :func:`_statements` 同源,所以将来有人往脚本里加了
       可执行语句,这里会自动把它也跑上。
    """
    for stmt in _statements():
        cur.execute(stmt)
        try:
            cur.fetchall()
        except psycopg2.ProgrammingError:
            pass          # 非 SELECT 语句没有结果集


# ══════════════════════════════════════════════════════════════════════
# ③ [工单 V4-C · C-1] §1 分档:**互斥且穷尽**,且全部通向人工裁定
# ══════════════════════════════════════════════════════════════════════
# 🔴 本节整段按 V4-C 重写。上一版的分档是「可回填 / 转人工」二分,
#    建立在 `payer == 今天的 owner ⇒ 可以安全回填` 这个**被证伪的前提**上:
#    历史 tenant 4242、payer 9999、品牌后来转给 9999 ⇒ 条件成立 ⇒ 写 9999。
#    我上一轮自己入档过这条病(判据把错误前提钉成正确行为),这一轮撞在
#    同一处 —— 所以这些判据现在钉的是**分类**,不是"敢不敢写":
#    脚本已经不写任何东西了。
BUCKETS = (
    "manual_no_owner_today",
    "manual_no_payer_evidence",
    "manual_payer_equals_owner_today",
    "manual_payer_differs_from_owner",
)

TRANSFERRED_TO = 9999      # 付款人;品牌后来转给了它
HISTORICAL_TENANT = 4242   # 当时真正的租户


def _assert_partition(row: dict) -> None:
    """四档必须是一个**划分**,而且脚本自报的 ``partition_ok`` 不许说谎。

    🔴 两件事分开验,缺一不可:
      ① **在判据里自己把四档加起来**与 ``cells_tenant_null`` 比 ——
         这一位不依赖脚本自报的任何东西。只信 ``partition_ok`` 的话,
         把它写成 ``... OR TRUE`` 就永远绿(我第一版就是只信它,
         自选变异当场证明那条断言零判别力)。
      ② ``partition_ok`` 必须**与①的结论一致** —— Deploy 读的是这一个布尔,
         它说谎(恒真或恒假)都要当场红。
    """
    total = sum(int(row[b]) for b in BUCKETS)
    assert total == int(row["cells_tenant_null"]), (
        f"四档之和 {total} != 待回填总数 {row['cells_tenant_null']} —— "
        f"分档有重叠或有漏,发车记录上的账闭不上:{row}")
    assert row["partition_ok"] is True, (
        f"脚本自报 partition_ok={row['partition_ok']},而判据自己加出来是闭合的 —— "
        f"Deploy 读的那个布尔在说谎:{row}")


def _census(cur) -> dict:
    cur.execute(_census_sql("cells_total"))
    return dict(cur.fetchone())


def test_e5_20_the_four_buckets_are_mutually_exclusive_and_exhaustive(cur):
    """四档必须是一个**划分**:互斥 + 穷尽,和恒等于待回填总数。

    🔴 上一版是四条各自带 WHERE 的子查询,于是 `owner IS NULL` +
       `payer IS NOT NULL` 的格**同时**命中两档 —— 四数之和 2、总数 1,
       发车记录上的账闭不上(Codex 真 PG16 输出)。
       现在是单一 CASE,一格只落进第一个命中的分支。
    """
    _brand(cur, brand_id=BRAND, owner=OWNER)
    _brand(cur, brand_id=BRAND_NO_OWNER, owner=None)
    tid = _task(cur)

    # ① 没有今天的 owner(**且**有付款人 —— 正是上一版双计的那一格)
    tid2 = _task(cur, brand_id=BRAND_NO_OWNER)
    _seed_cell(cur, task_id=tid2, keyword_id=51, tenant=None,
               settlement_reference="S-NOOWNER", brand_id=BRAND_NO_OWNER)
    _seed_settlement(cur, reference="S-NOOWNER", task_id=tid2, keyword_id=51,
                     billing_user_id=TRANSFERRED_TO, brand_id=BRAND_NO_OWNER)
    # ② 没有同期结算证据
    _seed_cell(cur, task_id=tid, keyword_id=52, tenant=None,
               settlement_reference=None)
    # ③ 付款人 == 今天的 owner(看起来像互证,其实不是)
    _seed_cell(cur, task_id=tid, keyword_id=53, tenant=None,
               settlement_reference="S-SAME")
    _seed_settlement(cur, reference="S-SAME", task_id=tid, keyword_id=53,
                     billing_user_id=OWNER)
    # ④ 付款人 != 今天的 owner
    _seed_cell(cur, task_id=tid, keyword_id=54, tenant=None,
               settlement_reference="S-DIFF")
    _seed_settlement(cur, reference="S-DIFF", task_id=tid, keyword_id=54,
                     billing_user_id=TRANSFERRED_TO)
    # ⑤ 已经有租户 ⇒ 完全不在范围里
    _seed_cell(cur, task_id=tid, keyword_id=55, tenant=OWNER,
               settlement_reference=None)

    row = _census(cur)
    assert int(row["cells_total"]) == 5, row
    assert int(row["cells_tenant_null"]) == 4, (
        f"待回填格数不对(已有租户的那一格被算进来了?):{row}")
    for bucket in BUCKETS:
        assert int(row[bucket]) == 1, f"{bucket} 数错了:{row}"
    # 分母自证:BUCKETS 与脚本实际产出的列**同集**,手抄漏一档会当场红
    assert set(BUCKETS) <= set(row), f"分档列名对不上:{sorted(row)}"
    _assert_partition(row)


def test_e5_21_owner_null_plus_a_payer_lands_in_exactly_one_bucket(cur):
    """🔴 Codex 真 PG16 反例②原样重放:`owner IS NULL` + `payer IS NOT NULL`。

    上一版这一格被 `manual_payer_conflict` 与 `unresolvable` **双计**:
    partition_sum=2 而 cells_tenant_null=1。
    这一条单独打它,因为上一条那种"四格各一"的摆法里,双计会被别的档
    的正确计数掩盖掉 —— 只有把它单独摆出来,和才会露出破绽。
    """
    _brand(cur, brand_id=BRAND_NO_OWNER, owner=None)
    tid = _task(cur, brand_id=BRAND_NO_OWNER)
    _seed_cell(cur, task_id=tid, keyword_id=56, tenant=None,
               settlement_reference="S-ONLY", brand_id=BRAND_NO_OWNER)
    _seed_settlement(cur, reference="S-ONLY", task_id=tid, keyword_id=56,
                     billing_user_id=TRANSFERRED_TO, brand_id=BRAND_NO_OWNER)

    row = _census(cur)
    assert int(row["cells_tenant_null"]) == 1, row
    hit = [b for b in BUCKETS if int(row[b]) > 0]
    assert hit == ["manual_no_owner_today"], (
        f"这一格落进了 {hit}(应当**只**落进 manual_no_owner_today):{row}")
    _assert_partition(row)


def test_e5_22_a_transferred_brand_is_not_treated_as_corroborated(cur):
    """🔴 Codex 真 PG16 反例①原样重放:历史 tenant 4242 / payer 9999 /
    品牌**后来转给** 9999。

    上一版:`payer == 今天的 owner` 成立 ⇒ 自动回填写入 9999 ⇒ 错归属。
    现在:脚本不回填任何东西;而且这一格被明确分到
    ``manual_payer_equals_owner_today`` —— 档名本身就在说
    "等于的是**今天的** owner",不是"当时的租户"。

    🔴 判据不能只验"没写" —— 那样把整个 census 删掉也全绿。所以同时验
       它被**归到哪一档**:分类错了,人工裁定的名单就是错的。
    """
    _brand(cur, brand_id=BRAND, owner=HISTORICAL_TENANT)   # 当时:owner=4242
    tid = _task(cur)
    ph = _seed_cell(cur, task_id=tid, keyword_id=57, tenant=None,
                    settlement_reference="S-XFER")
    _seed_settlement(cur, reference="S-XFER", task_id=tid, keyword_id=57,
                     billing_user_id=TRANSFERRED_TO)       # 付款人 9999
    _brand(cur, brand_id=BRAND, owner=TRANSFERRED_TO)      # 之后:转给 9999

    row = _census(cur)
    assert int(row["manual_payer_equals_owner_today"]) == 1, (
        f"转移后 payer==今天的 owner 这一格没被归进「看似互证」那一档:{row}")
    _assert_partition(row)

    # 最硬的一条:跑完整个脚本,这一格仍然是 NULL —— 脚本不再动任何归属
    _run_whole_script(cur)
    assert _tenant_of(cur, ph) is None, (
        f"脚本改了归属(实得 {_tenant_of(cur, ph)})—— §2 应当已经整段退役;"
        f"真实历史租户是 {HISTORICAL_TENANT},自动推断在这一形态下必然写错")


def test_e5_23_running_the_whole_script_changes_no_tenant_at_all(cur):
    """判别力自证 + 总闸:脚本跑完,**一格归属都不许动**。

    上一条只看那一格。这一条把四种形态一起摆上再跑整脚本,
    确认零写入 —— 否则"退役"可能只退役了我摆出来的那一种形态。
    """
    _brand(cur, brand_id=BRAND, owner=OWNER)
    _brand(cur, brand_id=BRAND_NO_OWNER, owner=None)
    tid = _task(cur)
    hashes = []
    hashes.append(_seed_cell(cur, task_id=tid, keyword_id=61, tenant=None,
                             settlement_reference="S-A"))
    _seed_settlement(cur, reference="S-A", task_id=tid, keyword_id=61,
                     billing_user_id=OWNER)
    hashes.append(_seed_cell(cur, task_id=tid, keyword_id=62, tenant=None,
                             settlement_reference="S-B"))
    _seed_settlement(cur, reference="S-B", task_id=tid, keyword_id=62,
                     billing_user_id=TRANSFERRED_TO)
    hashes.append(_seed_cell(cur, task_id=tid, keyword_id=63, tenant=None,
                             settlement_reference=None))
    tid2 = _task(cur, brand_id=BRAND_NO_OWNER)
    hashes.append(_seed_cell(cur, task_id=tid2, keyword_id=64, tenant=None,
                             settlement_reference=None, brand_id=BRAND_NO_OWNER))

    _run_whole_script(cur)

    still_null = [h for h in hashes if _tenant_of(cur, h) is None]
    assert len(still_null) == len(hashes), (
        f"{len(hashes) - len(still_null)} 格的归属被脚本改过 —— "
        "本脚本必须是**纯只读**的")


def test_e5_24_the_script_contains_no_tenant_write_even_in_comments(cur):
    """结构锁:全文**没有任何**写 ``tenant_owner_user_id`` 的语句,注释里也没有。

    🔴 为什么连注释都不许留:注释掉的 UPDATE 是一份"取消注释就能跑"的现成品,
       而它的错误恰恰是**看起来完全合理**(两版都过了我自己这一关)。
       留着它,下一个赶时间的人只会看到"这里本来有个回填,注掉了"。

    🔴 扫描剔除本文件对规则本身的引用之前,先自证正则**抓得住**目标形状 ——
       否则一条永远匹配不到东西的正则会以"全绿"的样子存在(本仓记过)。
    """
    import re

    raw = (ROOT / SCRIPT_REL).read_text(encoding="utf-8")
    pat = re.compile(r"(UPDATE|SET)\s+[^\n]*tenant_owner_user_id\s*=", re.I)
    assert not pat.search(raw), (
        f"脚本里还有写租户列的语句(注释里也算):{pat.search(raw).group(0)!r}")
    # 反向自证:同一条正则必须抓得住一条真的写语句,否则它证明不了任何事
    assert pat.search("UPDATE t SET tenant_owner_user_id = 1"), "正则失效"


def test_e5_25_the_deploy_procedure_no_longer_has_a_backfill_step(cur):
    """发车步骤必须**只剩两步**,且明说不再回填。

    Deploy 照着文件头做事。文件头还写着"③ §2 回填"的话,退役就只退在代码里,
    没退在流程里 —— 而流程才是人会照着做的那一份。
    """
    head = (ROOT / SCRIPT_REL).read_text(encoding="utf-8").split("-- ── §1")[0]
    assert "不再有回填步骤" in head, "文件头没有明说回填步骤已取消"
    assert "两步,没有第三步" in head, "文件头没有把步骤数收成两步"
