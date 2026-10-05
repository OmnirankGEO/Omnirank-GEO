"""E3-4 补洞 · 报告五卡「按采样窗取格」要有**窗内正样本**。

为什么补这一条(外选 MUT-EXTE3-15 存活坐实的洞)
------------------------------------------------
`api/defensive_geo_report_api.py` 里取格那一跳是两步:
① 从 ``defgeo_report_snapshots`` 取这份报告的采样窗;② 按 ``brand_id`` +
``created_at`` 落在窗内取 ``plan_hash``。外选变异把②的两个绑定参数**对调**
(``created_at >= 窗尾 AND created_at <= 窗头``)⇒ 结果恒空 ⇒ 每一份报告的
五卡从此**永远静默隐藏**,而代码把"取不到格"当合法留白处理(不告警)。

全分母零红。既有的 `test_e4_33` 是**在场锁**:``"created_at" in q`` /
``defgeo_report_snapshots`` / ``_CardsUnbound`` 三个子串在变异后一个不少。
全仓没有任何判据在真库里放一格**窗内**数据并断言取得到 ——
与本仓记过的「加宽 pattern 没加正样本 = 没判据在守」同形。

本文件怎么避免变成"另一份手抄的 SQL"
------------------------------------
🔴 SQL 与**参数绑定顺序**都从 `api/defensive_geo_report_api.py` 里机械抽出来,
   然后原样执行。判据自己不写查询、也不写"start 在前 end 在后"这个顺序 ——
   顺序是被测对象,判据只负责给它一个真库和一格**确实落在窗内**的数据。
   (本仓记过:判据自己构造被测的中间值 ⇒ 变异存活。)
🔴 抽取规则有自证(e6_01):抽不出恰一条快照查询 + 恰一条取格查询就当场红。
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
API_REL = "api/defensive_geo_report_api.py"

BRAND = 7801
OWNER = 4242


# ══════════════════════════════════════════════════════════════════════
# 机械抽取:生产的 SQL + 生产的绑定顺序
# ══════════════════════════════════════════════════════════════════════

def _sql_of(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return ""


def _executes() -> tuple[str, list[tuple[str, list[str]]]]:
    """枚举 ``cur.execute(sql, params)`` —— 返回 (SQL 原文, 每个绑定参数的源码片段)。"""
    src = (ROOT / API_REL).read_text(encoding="utf-8")
    tree = ast.parse(src)
    found: list[tuple[str, list[str]]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "execute"
                and node.args):
            continue
        sql = _sql_of(node.args[0])
        if not sql:
            continue
        params: list[str] = []
        if len(node.args) > 1 and isinstance(node.args[1], (ast.Tuple, ast.List)):
            params = [ast.get_source_segment(src, e) or ""
                      for e in node.args[1].elts]
        found.append((sql, params))
    return src, found


def _one(pred, what: str) -> tuple[str, list[str]]:
    _, calls = _executes()
    hits = [c for c in calls if pred(c[0])]
    assert len(hits) == 1, (
        f"{API_REL} 里符合「{what}」的 execute 有 {len(hits)} 条(期望恰 1)——"
        "抽取规则失效,下面的判据会对着空气说话")
    return hits[0]


def _snapshot_query() -> tuple[str, list[str]]:
    return _one(lambda s: "defgeo_report_snapshots" in s and "sampling_window_start" in s,
                "取报告采样窗")


def _cells_query() -> tuple[str, list[str]]:
    return _one(lambda s: "monitoring_run_cells" in s and "plan_hash" in s,
                "按窗取格")


def _bind(exprs: list[str], ns: dict) -> tuple:
    """按**生产写的顺序**求值每个绑定参数。判据不重排、不改写。"""
    return tuple(eval(expr, {"int": int, "str": str}, ns) for expr in exprs)


# ══════════════════════════════════════════════════════════════════════
# 夹具 —— 这些是**输入**,不是被测对象
# ══════════════════════════════════════════════════════════════════════

def _seed_brand(cur) -> None:
    cur.execute(
        "INSERT INTO public.brands (id, name, owner_user_id) VALUES (%s,%s,%s) "
        "ON CONFLICT (id) DO UPDATE SET owner_user_id = EXCLUDED.owner_user_id",
        (BRAND, "报告品牌", OWNER))


def _seed_task(cur) -> int:
    cur.execute(
        "INSERT INTO public.monitoring_tasks (brand_id, total_tests) "
        "VALUES (%s,0) RETURNING id", (BRAND,))
    return int(cur.fetchone()["id"])


def _seed_cell_at(cur, *, task_id: int, keyword_id: int, created_at: str) -> str:
    """建一格并把 ``created_at`` 摆在指定时刻(窗内 / 窗外由调用方决定)。"""
    import hashlib
    ph = hashlib.sha256(f"{task_id}:{keyword_id}:{created_at}".encode()).hexdigest()
    cur.execute(
        """
        INSERT INTO public.monitoring_run_cells
            (task_id, brand_id, keyword_id, keyword_source, keyword_snapshot,
             question_snapshot, target_brand_snapshot, platform, is_planned,
             state, entitlement_snapshot, order_snapshot, fulfillment_credential,
             fulfillment_state, plan_hash, tenant_owner_user_id, created_at)
        VALUES (%s,%s,%s,'confirmed','kw','q','tb','dashscope',TRUE,'queued',
                '{"schema_version":"monitoring-entitlement-snapshot-v1"}'::jsonb,
                '{"schema_version":"monitoring-order-snapshot-v1"}'::jsonb,
                %s,'reserved',%s,%s,%s::timestamptz)
        RETURNING plan_hash
        """,
        (task_id, BRAND, keyword_id, str(uuid.uuid4()), ph, OWNER, created_at))
    return str(cur.fetchone()["plan_hash"])


def _seed_snapshot(cur, *, diagnosis_id: int, revision: int,
                   window_start: str, window_end: str) -> None:
    cur.execute(
        """
        INSERT INTO public.defgeo_report_snapshots
            (report_snapshot_id, revision, tenant_owner_user_id, brand_id,
             diagnosis_id, state, content_hash, plan_snapshot_id,
             plan_snapshot_hash, sampling_window_start, sampling_window_end,
             cutoff_at, input_watermark, raw_result_ids,
             entity_resolver_version, outcome_classifier_version,
             evidence_extractor_version, metric_definition_version,
             snapshot_version)
        VALUES (%s,%s,%s,%s,%s,'ready',%s,'plan-1',%s,
                %s::timestamptz,%s::timestamptz,%s::timestamptz,'wm','{}',
                'er-1','oc-1','ee-1','md-1','snap-v1')
        """,
        (f"rs-{diagnosis_id}", revision, OWNER, BRAND, diagnosis_id,
         "a" * 64, "b" * 64, window_start, window_end, window_end))


def _clear_snapshots(cur, diagnosis_id: int) -> None:
    """``defgeo_report_snapshots`` 不在 conftest 的 TRUNCATE 名单里 ——
    自己清自己那几行,免得跨判据串味(本仓记过:残留让判据依赖执行顺序)。"""
    cur.execute("DELETE FROM public.defgeo_report_snapshots WHERE diagnosis_id=%s",
                (diagnosis_id,))


# ══════════════════════════════════════════════════════════════════════
# ① 抽取自证
# ══════════════════════════════════════════════════════════════════════

def test_e6_01_the_two_report_queries_are_uniquely_locatable():
    """抽取规则的活性栓:两条查询各恰一条,取格那条绑齐了该绑的冻结键。

    🔴 [工单 V3-C · C-4 · 2026-08-28] 本条上一版断言的是
       ``len(cells_params) == 3``。C-4 给取格查询补上 ``cutoff_at`` 之后它
       立刻变红 —— 红的不是缺陷,是**判据自己过期了**:计数式判据会随任何
       正当的增补而过期,集合式不会(本仓记过)。改成集合断言。
    """
    snap_sql, snap_params = _snapshot_query()
    cells_sql, cells_params = _cells_query()

    assert "revision DESC" in snap_sql, (
        f"快照查询不再按 revision 取最新那一版:{snap_sql}")
    assert len(snap_params) == 2, (
        f"快照查询的绑定参数不是 2 个:{snap_params}")
    _need = {'_brand_id',
             '_window["sampling_window_start"]',
             '_window["sampling_window_end"]',
             '_window["cutoff_at"]'}
    assert _need <= set(cells_params), (
        f"取格查询漏绑了 {sorted(_need - set(cells_params))} —— "
        f"实绑:{cells_params}")
    assert "created_at >=" in cells_sql and "created_at <=" in cells_sql, (
        f"取格查询不再是闭区间取窗:{cells_sql}")


def test_e6_02_the_window_is_bound_lower_bound_first():
    """结构臂:``>= %s`` 那一位必须绑**窗头**,``<= %s`` 那一位必须绑**窗尾**。

    🔴 这是"在场锁"抓不到的那一格:两个参数对调之后,SQL 原文一个字没变
       (``created_at`` 还在、``sampling_window_start/end`` 也都还在),
       只有**绑定顺序**是反的。所以判的是顺序,不是在场。
    """
    cells_sql, params = _cells_query()
    lower = cells_sql.index("created_at >=")
    upper = cells_sql.index("created_at <=")
    assert lower < upper, f"SQL 里下界不在上界之前,本判据的前提不成立:{cells_sql}"

    window_params = [p for p in params if "sampling_window" in p]
    assert len(window_params) == 2, f"两个窗参数没找齐:{params}"
    assert "sampling_window_start" in window_params[0], (
        f"``created_at >= %s`` 绑的不是窗头:{params} —— "
        "窗上下界对调 ⇒ 条件恒不成立 ⇒ 每一份报告的五卡永远静默隐藏")
    assert "sampling_window_end" in window_params[1], (
        f"``created_at <= %s`` 绑的不是窗尾:{params}")


# ══════════════════════════════════════════════════════════════════════
# ② 行为臂:真库 · 窗内正样本
# ══════════════════════════════════════════════════════════════════════

def test_e6_10_a_cell_inside_the_sampling_window_is_really_picked_up(cur):
    """🔴 **窗内正样本** —— 缺的就是这一条。

    两步都跑生产自己的 SQL、按生产自己的绑定顺序:
    先取采样窗,再按窗取格。窗内那一格必须回来。

    拆红方式(亲毒验过):把 ``api/defensive_geo_report_api.py`` 里
    ``_window["sampling_window_start"]`` 与 ``..._end`` 两个实参对调 ——
    条件变成 ``created_at >= 窗尾 AND created_at <= 窗头``,恒空 ⇒ 本条红。
    """
    diagnosis_id = 88101
    _seed_brand(cur)
    tid = _seed_task(cur)
    _clear_snapshots(cur, diagnosis_id)
    _seed_snapshot(cur, diagnosis_id=diagnosis_id, revision=1,
                   window_start="2026-08-01 00:00:00+00",
                   window_end="2026-08-31 23:59:59+00")

    inside = _seed_cell_at(cur, task_id=tid, keyword_id=1,
                           created_at="2026-08-15 12:00:00+00")
    _seed_cell_at(cur, task_id=tid, keyword_id=2,
                  created_at="2026-07-01 12:00:00+00")   # 窗前
    _seed_cell_at(cur, task_id=tid, keyword_id=3,
                  created_at="2026-09-20 12:00:00+00")   # 窗后

    snap_sql, snap_params = _snapshot_query()
    cur.execute(snap_sql, _bind(snap_params,
                                {"diagnosis_id": diagnosis_id, "_brand_id": BRAND}))
    window = cur.fetchone()
    assert window is not None, "快照查询没取到窗 —— 后面的取格无从谈起"

    cells_sql, cells_params = _cells_query()
    cur.execute(cells_sql, _bind(cells_params,
                                 {"_brand_id": BRAND, "_window": window}))
    got = {r["plan_hash"] for r in (cur.fetchall() or [])}

    assert got == {inside}, (
        f"按采样窗取格取到 {got},期望恰好是窗内那一格 {inside}。"
        "空集 = 五卡从此在每一份报告上静默隐藏,而代码把它当合法留白,不告警")


def test_e6_11_the_window_excludes_what_lies_outside_it(cur):
    """判别力自证(窗方向之外的另一半):窗外的格不许混进来。

    没有这一条,上面那条也可能只是"这条 SQL 把所有格都返回了"。
    """
    diagnosis_id = 88102
    _seed_brand(cur)
    tid = _seed_task(cur)
    _clear_snapshots(cur, diagnosis_id)
    _seed_snapshot(cur, diagnosis_id=diagnosis_id, revision=1,
                   window_start="2026-08-01 00:00:00+00",
                   window_end="2026-08-31 23:59:59+00")
    _seed_cell_at(cur, task_id=tid, keyword_id=4,
                  created_at="2026-07-01 12:00:00+00")

    snap_sql, snap_params = _snapshot_query()
    cur.execute(snap_sql, _bind(snap_params,
                                {"diagnosis_id": diagnosis_id, "_brand_id": BRAND}))
    window = cur.fetchone()
    cells_sql, cells_params = _cells_query()
    cur.execute(cells_sql, _bind(cells_params,
                                 {"_brand_id": BRAND, "_window": window}))
    got = [r["plan_hash"] for r in (cur.fetchall() or [])]
    assert got == [], (
        f"窗外的格被算进这份**冻结**报告了:{got} —— "
        "卡片会跟着这个品牌未来每一次监测漂,报告却还是同一份")


def test_e6_12_the_latest_revision_wins(cur):
    """采样窗取的是**最新那一版** revision —— 报告改版后旧窗不许赢。

    只跑第①步,所以它对"窗上下界对调"这个变异是绿的:
    分开写是为了让红集读得出"红在哪一步"。
    """
    diagnosis_id = 88103
    _seed_brand(cur)
    _clear_snapshots(cur, diagnosis_id)
    _seed_snapshot(cur, diagnosis_id=diagnosis_id, revision=1,
                   window_start="2026-01-01 00:00:00+00",
                   window_end="2026-01-31 00:00:00+00")
    _seed_snapshot(cur, diagnosis_id=diagnosis_id, revision=2,
                   window_start="2026-08-01 00:00:00+00",
                   window_end="2026-08-31 00:00:00+00")

    snap_sql, snap_params = _snapshot_query()
    cur.execute(snap_sql, _bind(snap_params,
                                {"diagnosis_id": diagnosis_id, "_brand_id": BRAND}))
    window = cur.fetchone()
    assert window["sampling_window_start"].month == 8, (
        f"取到的是旧 revision 的窗:{window} —— 报告改版后五卡会按老窗取格")


def test_e6_13_no_snapshot_means_no_window_to_bind(cur):
    """"绑不上就隐藏"的前半段:没有快照时第①步必须返空。

    这一格既有判据只锁了 ``_CardsUnbound`` 这个名字在源码里 ——
    名字在场证明不了查询真的取不到东西。
    """
    _seed_brand(cur)
    _clear_snapshots(cur, 88104)
    snap_sql, snap_params = _snapshot_query()
    cur.execute(snap_sql, _bind(snap_params,
                                {"diagnosis_id": 88104, "_brand_id": BRAND}))
    assert cur.fetchone() is None, (
        "这份诊断没有报告快照,第①步却取到了窗 —— "
        "「绑不上就隐藏」那条出口永远不会被走到")
