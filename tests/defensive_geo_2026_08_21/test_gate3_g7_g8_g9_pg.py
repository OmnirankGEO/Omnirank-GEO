"""门三返修:G7 路由映射 · G8 进度归属键 · G9 执行器接线。

G9 的被测对象是**接线**,不是诊断质量
--------------------------------------
门库实证:confirm 把 run 推进 running、冻住 7800 算力,然后**没有任何东西接手**
(run_b6ad4e77… 在 running 挂了 23 分钟,心跳一次没动)。所以判据打的是:
「已确认的 run 会不会被领走、领走之后是不是拿**冻结那一版**题单去跑、
 跑不起来的会不会被放手让 sweeper 退钱」。

真 LLM 一次都不跑:被测代码是领取/还原/接线,派发的终点(现役
``run_diagnosis_task``)在判据里被打桩。**桩替的是被调方,不是被测代码。**
"""

from __future__ import annotations

import concurrent.futures
import uuid

import pytest

pytestmark = pytest.mark.integration

TENANT = 8401
OTHER = 8402


@pytest.fixture(autouse=True)
def _clean_denominator(db):
    """每条判据开跑前清掉本租户的 run。

    `uq_diag_active_per_brand` 是**生产真约束**(一个品牌同时只能有一个活跃诊断)。
    上一条判据留下的活跃 run 会让下一条的夹具 UniqueViolation —— 红因与被测行为
    毫无关系。分母不干净时,判据测的是"上一条留了什么"。
    """
    # 🔴 **只清 diagnosis_runs,不碰 preview**。migration 041 装了不可变触发器
    #    (「preview 是资金审计链的一环」),DELETE 会被它拒 —— 那是真不变式,
    #    夹具不该为了打扫卫生去跟它打架。残留 preview 无害:每条判据用全新
    #    run_token,反查 consumed_command_id 不会撞上。
    with db.cursor() as cur:
        cur.execute("DELETE FROM diagnosis_runs WHERE owner_user_id = ANY(%s)",
                    ([TENANT, OTHER],))
    db.commit()
    yield


# ══════════════════════════════════════════════════════════════════════════
# G7:三个 defensive router 全部映射到 diagnosis —— 分母从 router census 机械导出
# ══════════════════════════════════════════════════════════════════════════
def _defensive_routes() -> list[str]:
    """机械分母:**从 router 对象自己**枚举路径,不手抄清单。

    手抄的问题很具体:新加一个端点时清单不会自己长出来,判据照绿,
    而那个端点悄悄落进 `__unmapped__` → 只有 admin 能用。
    """
    from fastapi.routing import APIRoute

    paths: list[str] = []
    for module_name in ("api.defensive_geo_api", "api.defensive_geo_report_api",
                        "api.defensive_publish_api"):
        mod = __import__(module_name, fromlist=["router"])
        for route in mod.router.routes:
            if isinstance(route, APIRoute):
                paths.append(route.path)
    return sorted(set(paths))


def test_router_census_denominator_is_real():
    """反向对照:分母不能是空的,也不能只有一两条 —— 否则下面那条什么都没证明。"""
    routes = _defensive_routes()
    assert len(routes) >= 8, f"只扫到 {len(routes)} 条 defensive 路由,分母可疑:{routes}"
    assert all(r.startswith("/api/defensive-geo") for r in routes), routes


def test_no_defensive_route_is_unmapped():
    """🔴 存在未映射的 defensive 路由 = 红。

    未映射 → `resolve_permission` 返回 `__unmapped__` → 全局中间件默认拒 →
    **只有 admin 能用**。本仓记过这个形态:付费功能上线后服务商一个都进不去。
    """
    from auth.module_mapping import resolve_permission

    unmapped = {r: resolve_permission(r) for r in _defensive_routes()
                if resolve_permission(r) == "__unmapped__"}
    assert not unmapped, f"这些 defensive 路由没有映射,只有 admin 能用:{sorted(unmapped)}"


def test_every_defensive_route_maps_to_diagnosis():
    from auth.module_mapping import resolve_permission

    wrong = {r: resolve_permission(r) for r in _defensive_routes()
             if resolve_permission(r) != "diagnosis"}
    assert not wrong, f"这些 defensive 路由没映射到 diagnosis 模块:{wrong}"


def test_publish_subprefix_is_covered_too():
    """publish router 前缀更长,必须一起被覆盖(它被 /api/defensive-geo/ 包住)。"""
    from auth.module_mapping import resolve_permission

    assert resolve_permission("/api/defensive-geo/publish/anything") == "diagnosis"


# ══════════════════════════════════════════════════════════════════════════
# G8:进度归属键 —— 创建者本人 200 / 别人 403 成对
# ══════════════════════════════════════════════════════════════════════════
def test_progress_session_id_is_the_key_legacy_actually_checks():
    """confirm 交出去的键必须等于 `diagnosis_runs.session_id`。

    `runId` 是 run_token,而 `auth.session_access.authorize_session` 查的是
    session_id —— 两者不相等。不把这把键交出去,前端只能拿 runId 去试,
    于是创建者查**自己的**进度被 403。
    """
    from services.defensive_geo.run_executor import progress_session_id

    assert progress_session_id("run_abc") == "defgeo_run_abc"


def test_creator_sees_own_progress_and_others_do_not(db):
    """🔴 成对判据:本人放行 / 别人拒。

    只验其中一半都是假的:只验"本人能看"会漏掉"所有人都能看";
    只验"别人不能看"会漏掉"本人也看不了"(那正是修之前的样子)。
    """
    from auth.session_access import authorize_session
    from services.defensive_geo.run_executor import progress_session_id

    run_token = "run_" + uuid.uuid4().hex
    sid = progress_session_id(run_token)
    with db.cursor() as cur:
        # freeze_id/backend 不是装饰:`chk_freeze_handle` 要求 paid 行进 running
        # 前必须持有句柄(库级不变式)。少给这两列夹具当场 CheckViolation。
        cur.execute(
            "INSERT INTO diagnosis_runs (run_token, session_id, owner_user_id, brand_id,"
            " client_request_id, billing_mode, freeze_task_ref, run_status, freeze_id,"
            " freeze_backend) VALUES (%s,%s,%s,901,%s,'paid',%s,'running',1,'legacy')",
            (run_token, sid, TENANT, "creq-" + uuid.uuid4().hex[:8], "diag_" + run_token))
    db.commit()
    try:
        assert authorize_session({"user_id": TENANT}, sid) is True, \
            "创建者看不到自己的进度 —— 归属键没对齐"
        assert authorize_session({"user_id": OTHER}, sid) is False, \
            "别人能看到这次进度 —— 越权面"
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM diagnosis_runs WHERE run_token=%s", (run_token,))
        db.commit()


def test_status_url_points_at_the_live_progress_endpoint():
    """statusUrl 必须指向真进度端点,不是 preview 的 GET。

    指向 preview 会让前端轮到一个**永远不动**的冻结快照 ——
    她看到的是一个卡在"待确认"的页面,而体检其实在跑。
    """
    import inspect

    import api.defensive_geo_api as mod

    src = inspect.getsource(mod._confirm_response)
    assert "/api/diagnosis/session/" in src, "statusUrl 没指向 legacy 进度端点"
    assert "progressSessionId=" in src or "progressSessionId" in src


# ══════════════════════════════════════════════════════════════════════════
# G9:执行器
# ══════════════════════════════════════════════════════════════════════════
def _seed_ghost(db, *, owner=TENANT, brand_id=901, session_prefix="defgeo_",
                with_preview=True, questions=("这个牌子靠谱吗", "它是做什么的")):
    """复刻门库那条幽灵 run 的形状:running + 冻结 + **从没派发过**。"""
    run_token = "run_" + uuid.uuid4().hex
    sid = session_prefix + run_token
    plan_id = str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO diagnosis_runs (run_token, session_id, owner_user_id, brand_id,"
            " client_request_id, billing_mode, freeze_task_ref, run_status, freeze_id,"
            " freeze_backend) VALUES (%s,%s,%s,%s,%s,'paid',%s,'running',1,'legacy')",
            (run_token, sid, owner, brand_id, "creq-" + uuid.uuid4().hex[:8],
             "diag_" + run_token))
        if with_preview:
            payload = {"questions": [
                {"text": t, "global_ordinal": i + 1, "mode_side": "defensive",
                 "family_key": "identity_scope", "brand_exposure": "named",
                 "origin": "customer", "question_revision": 1,
                 "question_identity_key": f"q_{i}"}
                for i, t in enumerate(questions)]}
            import json as _json
            # 🔴 列名/NOT NULL 不是猜的:由 information_schema 现查后逐列填。
            #    第一版凭印象写,当场 NotNullViolation(question_set_version)
            #    并且把 preview 的 idempotency_key / canonical_request_hash
            #    错写成了 client_request_id / request_content_hash。
            cur.execute(
                "INSERT INTO defgeo_question_plans (plan_id, plan_revision, tenant_owner_user_id,"
                " brand_id, created_by_user_id, mode, profile_revision_id, question_set_version,"
                " canonical_hash, frozen_payload, client_request_id, request_content_hash,"
                " total_count, defensive_count, offensive_count, expires_at)"
                " VALUES (%s,1,%s,%s,%s,'defensive','prof-1','qsv-1',%s,%s::jsonb,%s,%s,%s,%s,0,"
                " NOW() + INTERVAL '1 day')",
                (plan_id, owner, brand_id, owner, "a" * 64, _json.dumps(payload),
                 "creq-" + uuid.uuid4().hex[:8], "b" * 64, len(questions), len(questions)))
            cur.execute(
                "INSERT INTO defgeo_diagnosis_run_previews (preview_id, tenant_owner_user_id,"
                " brand_id, created_by_user_id, question_plan_id, question_plan_revision,"
                " question_plan_hash, profile_revision_id, campaign_mode, canonical_hash,"
                " frozen_payload, feature_code, pricing_catalog_version, base_points,"
                " extra_points, exact_total_points, funding_policy, principal_kind,"
                " approval_requirement, planned_cells, lifecycle, consumed_command_id,"
                " consumed_at, idempotency_key, canonical_request_hash, expires_at)"
                " VALUES (%s,%s,%s,%s,%s,1,%s,'prof-1','defensive',%s,'{}'::jsonb,"
                " 'geo_diagnosis','v1',7,0,7,'personal_wallet','personal','not_required',"
                " %s,'consumed',%s,NOW(),%s,%s, NOW() + INTERVAL '1 day')",
                (str(uuid.uuid4()), owner, brand_id, owner, plan_id, "a" * 64, "c" * 64,
                 len(questions), run_token, "idem-" + uuid.uuid4().hex[:8], "d" * 64))
    db.commit()
    return run_token, sid


def _cleanup(db, run_token):
    with db.cursor() as cur:
        cur.execute("DELETE FROM diagnosis_runs WHERE run_token=%s", (run_token,))
    db.commit()


def test_executor_is_registered_in_the_scheduler():
    """🔴 接线锁。工单点名的那一发变异「从调度里摘掉执行器」必须让它变红。

    这条**只**证明"调度里有这条 job",不证明它跑得对 —— 后者由下面几条打。
    分开写是因为它们会各自独立地坏:接线在但逻辑错 / 逻辑对但没人调。
    """
    import ast
    import inspect

    import api.scheduler as sched

    src = inspect.getsource(sched)
    assert "defgeo_run_execute" in src, "调度里没有防御型 GEO 执行器这条 job"
    assert "consume_confirmed_runs_sync" in src, "调度没有引用执行器的入口函数"

    # 结构:必须真的走 add_job 注册,而不是只在注释里提一句
    tree = ast.parse(src)
    registered = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name != "add_job":
                continue
            for kw in node.keywords:
                if kw.arg == "id" and isinstance(kw.value, ast.Constant) \
                        and kw.value.value == "defgeo_run_execute":
                    registered = True
    assert registered, "执行器没有通过 add_job 真正注册(只是文本里出现过)"


def test_claim_picks_up_a_ghost_run(db):
    """幽灵 run(running + 冻结 + 从没派发)必须被领走。"""
    from services.defensive_geo.run_executor import claim_next_unstarted_run

    run_token, _ = _seed_ghost(db)
    try:
        with db.cursor() as cur:
            claimed = claim_next_unstarted_run(cur)
        db.commit()
        assert claimed is not None, "幽灵 run 没被领走 —— 它会一直冻着钱"
        assert claimed["run_token"] == run_token
        assert claimed["defgeo_dispatch_attempts"] == 1

        with db.cursor() as cur:
            cur.execute("SELECT defgeo_dispatched_at FROM diagnosis_runs WHERE run_token=%s",
                        (run_token,))
            assert cur.fetchone()["defgeo_dispatched_at"] is not None, \
                "领取没有落下 external-start marker —— 崩溃恢复会二次外调"
    finally:
        _cleanup(db, run_token)


def test_claim_is_atomic_under_concurrency(db, schema_url=None):
    """20 并发领取恰一个。承重的是那条 UPDATE…RETURNING,不是 Python 的 if。"""
    import psycopg2
    from psycopg2.extras import RealDictCursor

    from services.defensive_geo.run_executor import claim_next_unstarted_run
    from tests.defensive_geo_2026_08_21.conftest import EXACT_THROWAWAY_URL

    run_token, _ = _seed_ghost(db)
    try:
        def _one(_i):
            c = psycopg2.connect(EXACT_THROWAWAY_URL, cursor_factory=RealDictCursor)
            try:
                cur = c.cursor()
                got = claim_next_unstarted_run(cur)
                c.commit()
                return got["run_token"] if got else None
            finally:
                c.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            results = list(ex.map(_one, range(20)))
        winners = [r for r in results if r == run_token]
        assert len(winners) == 1, f"20 并发领到了 {len(winners)} 次 —— 会重复外调"
    finally:
        _cleanup(db, run_token)


def test_claim_never_touches_a_non_defgeo_run(db):
    """🔴 越界锁:legacy run 崩了也满足「running + 从没派发」,但绝不能去跑它。

    它的原始请求体拿不到,硬跑等于凭空编一次诊断 —— 比不跑更坏。
    """
    from services.defensive_geo.run_executor import claim_next_unstarted_run

    legacy_token, _ = _seed_ghost(db, session_prefix="legacy_", with_preview=False)
    try:
        with db.cursor() as cur:
            claimed = claim_next_unstarted_run(cur)
        db.commit()
        assert claimed is None or claimed["run_token"] != legacy_token, \
            f"执行器领走了一条 legacy run {legacy_token} —— 越界"
    finally:
        _cleanup(db, legacy_token)


def test_frozen_plan_drives_the_request_verbatim(db):
    """跑的必须是**客户签的那一版**题面,逐字。"""
    from services.defensive_geo.run_executor import (
        build_diagnosis_request, load_brand_context, load_frozen_questions,
    )

    texts = ("安泰电梯靠谱吗?", "安泰电梯是做什么的?")
    run_token, _ = _seed_ghost(db, questions=texts)

    # 🔴 再插一版**更新的** revision。没有它,"取冻结那版"和"取最新那版"
    #    落在同一行上,判据零区分力 —— 实测变异「改成取最新」当场存活。
    #    单变体夹具让整条链错了也全绿,必须给它一个能分辨的对照。
    with db.cursor() as cur:
        cur.execute(
            "SELECT plan_id, tenant_owner_user_id, brand_id, created_by_user_id"
            " FROM defgeo_question_plans WHERE plan_id = ("
            "  SELECT question_plan_id FROM defgeo_diagnosis_run_previews"
            "   WHERE consumed_command_id=%s)", (run_token,))
        base = cur.fetchone()
        import json as _json
        later = {"questions": [{"text": "改过的第二版题面(客户没签过)", "global_ordinal": 1,
                                "mode_side": "defensive", "family_key": "identity_scope",
                                "brand_exposure": "named", "origin": "agent",
                                "question_revision": 2, "question_identity_key": "q_v2"}]}
        cur.execute(
            "INSERT INTO defgeo_question_plans (plan_id, plan_revision, tenant_owner_user_id,"
            " brand_id, created_by_user_id, mode, profile_revision_id, question_set_version,"
            " canonical_hash, frozen_payload, client_request_id, request_content_hash,"
            " total_count, defensive_count, offensive_count, expires_at)"
            " VALUES (%s,2,%s,%s,%s,'defensive','prof-1','qsv-1',%s,%s::jsonb,%s,%s,1,1,0,"
            " NOW() + INTERVAL '1 day')",
            (base["plan_id"], base["tenant_owner_user_id"], base["brand_id"],
             base["created_by_user_id"], "e" * 64, _json.dumps(later),
             "creq-" + uuid.uuid4().hex[:8], "f" * 64))
    db.commit()

    try:
        with db.cursor() as cur:
            questions = load_frozen_questions(cur, run_token)
            brand = load_brand_context(cur, 901)
        # 🔴 读完必须结事务。上一版没结,连接停在 `idle in transaction` 上持着
        #    `brands` 的锁,把**下一个 pytest 会话**的迁移重放整个挡死
        #    (实测:pg_blocking_pids 指回这一行)。只读也要结账。
        db.commit()
        assert questions == list(texts), (
            f"题面不是客户签的那一版:{questions} —— "
            "库里还有一份更新的 rev2,取到它就等于「她确认的是 A、系统跑了 B」"
        )

        req = build_diagnosis_request(
            {"brand_id": 901, "owner_user_id": TENANT}, questions, brand)
        assert req.custom_questions == list(texts), "冻结题面没进 custom_questions"
        assert req.brand_id == 901
        assert req.keywords, "keywords 为空会被 DiagnosisRequest 的 min_length=1 拒掉"
    finally:
        _cleanup(db, run_token)


def test_undispatchable_run_stops_being_claimed_and_falls_to_the_sweeper(db):
    """🔴 收敛的另一半:跑不起来的 run 必须**放手**,让状态机把钱退掉。

    没有这一条,一个坏 run 会被无限重投、永远冻着客户的钱 ——
    那只是把"卡住"从"没人跑"换成了"一直在重跑"。
    """
    from services.defensive_geo.run_executor import (
        MAX_DISPATCH_ATTEMPTS, claim_next_unstarted_run, release_claim,
    )

    # 没有 preview → 还原不出题单 → 每次派发都会失败
    run_token, _ = _seed_ghost(db, with_preview=False)
    try:
        for i in range(MAX_DISPATCH_ATTEMPTS):
            with db.cursor() as cur:
                claimed = claim_next_unstarted_run(cur)
                assert claimed is not None, f"第 {i+1} 次就领不到了(应能领 {MAX_DISPATCH_ATTEMPTS} 次)"
                release_claim(cur, claimed["run_token"])
            db.commit()

        with db.cursor() as cur:
            assert claim_next_unstarted_run(cur) is None, (
                f"派发失败 {MAX_DISPATCH_ATTEMPTS} 次后仍在领取 —— "
                "这条 run 会被无限重投,钱永远退不回去"
            )
        db.commit()

        # 放手之后,现役 sweeper 的判死窗口必须能接住它(心跳推回 6 分钟前)
        with db.cursor() as cur:
            cur.execute(
                "UPDATE diagnosis_runs SET heartbeat_at = NOW() - INTERVAL '6 minutes'"
                " WHERE run_token=%s", (run_token,))
            cur.execute(
                "SELECT count(*) AS c FROM diagnosis_runs"
                " WHERE run_token=%s AND billing_mode='paid' AND run_status='running'"
                " AND heartbeat_at < NOW() - INTERVAL '300 seconds'", (run_token,))
            assert cur.fetchone()["c"] == 1, "sweeper 的判死谓词接不住这条 run"
        db.commit()
    finally:
        _cleanup(db, run_token)


def test_release_claim_keeps_the_attempt_count(db):
    """归还 marker 时**不许**清零 attempts —— 清零 = 无限重试 = 钱永远冻着。"""
    from services.defensive_geo.run_executor import claim_next_unstarted_run, release_claim

    run_token, _ = _seed_ghost(db, with_preview=False)
    try:
        with db.cursor() as cur:
            claim_next_unstarted_run(cur)
            release_claim(cur, run_token)
            cur.execute("SELECT defgeo_dispatched_at, defgeo_dispatch_attempts"
                        " FROM diagnosis_runs WHERE run_token=%s", (run_token,))
            row = cur.fetchone()
        db.commit()
        assert row["defgeo_dispatched_at"] is None, "marker 没归还,下一轮领不到"
        assert row["defgeo_dispatch_attempts"] == 1, "attempts 被清零了 —— 无限重试"
    finally:
        _cleanup(db, run_token)


def test_executor_actually_registers_at_runtime(monkeypatch):
    """🔴 **运行期接线锁** —— 上一条(结构锚)拦不住的那一半。

    实测:变异「把整块 `if not scheduler.get_job(...)` 改成 `if False:`」**存活**了。
    因为 `add_job(...)` 那行代码**文字上还在**,只是永远走不到 —— grep 源码和 AST
    都看得见它,而调度里其实一条 job 都没有。这正是本仓记过的
    「验『标记清零』≠验『接线』」。

    所以这条不看源码,**真的去跑一遍注册**,拿桩 scheduler 收作业:
    走不到 add_job 就收不到,判据就红。
    """
    import api.scheduler as sched

    recorded: dict = {}

    class _StubScheduler:
        def get_job(self, job_id):
            return None                 # 一律当"还没注册",让每个分支都尝试注册

        def add_job(self, func, **kwargs):
            recorded[kwargs.get("id")] = {"func": func, **kwargs}
            return None

        def __getattr__(self, _name):   # 其它调用一概吞掉,本判据只关心 add_job
            return lambda *a, **k: None

    monkeypatch.setattr(sched, "get_scheduler", lambda: _StubScheduler())
    sched.register_v32_core_tasks()

    # 反向对照:桩确实收到了作业(否则"没收到我的"只是因为整个注册没跑)
    assert len(recorded) >= 5, (
        f"桩只收到 {len(recorded)} 个作业 —— 注册流程本身没跑起来,这条判据分母是空的"
    )
    assert "defgeo_run_execute" in recorded, (
        f"注册跑完却没有防御型 GEO 执行器。已注册:{sorted(k for k in recorded if k)[:10]}… "
        "—— 已确认的 run 会永远卡在 running,钱一直冻着"
    )
    assert recorded["defgeo_run_execute"]["func"].__name__ == "consume_confirmed_runs_sync"
