"""【fof8 P1-4】第三支不许把所有 `finished_at IS NULL` 都伪装成 collecting/0%。

被测缺陷(Codex fof8 §2 P1-4,坐标 `server.py:4696-4726`,SQL 在 `:4711`)
------------------------------------------------------------------------
那条 SQL 只 `SELECT 1`,**根本没读 `run_status`**;于是所有还没写 `finished_at`
的可达状态都被映射成"采集中 0%":正在结算的、已经转人工处理的用户,
都会被告知"刚开始采集"。这是「重新发起」相邻资金误导的**新形**
(门八第三发现治的是"未找到",这条治的是"假装刚开始")。

🔴 这一支是我上一单加的,而我当时**只造了 `running` 一种状态** ——
   正是我自己那堂 C1c 课(核覆盖按**状态空间**走,不按判据清单走)的复发。
   这次分母**机械枚举**,不手列。

分母怎么来的(不是抄 Codex 的六个词)
------------------------------------
`services/diagnosis_runs.py::_cas` 的调用点 AST 枚举:20 处 → 去重 5 组
`(to_status, finish)`,其中 `finish=False`(即**不写** `finished_at`)的是
`running` / `commit_pending` / `release_pending` / `settlement_manual`。
另外两个不经 `_cas`:`pending_freeze`(`INSERT … VALUES (…,'pending_freeze')`,
`services/diagnosis_runs.py:1258-1261`)与 `manual_resolving`
(`:412` 的直写 UPDATE,同样不碰 `finished_at`)。
⇒ 六态,与 Codex 点名的六个逐一对上。本文件把这个枚举**做成判据**(见
`test_denominator_matches_the_state_machine`),枚举漂了就红。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from .conftest import (
    DEAD_REDIS_PORT, TENANT, OTHER, insert_run, make_client, poll_status,
    redis_down, seed_owner_brand_pricing,
)

pytestmark = pytest.mark.integration

REPO = pathlib.Path(__file__).resolve().parents[2]

#: 六个"活着"的状态。**冻结分母** —— 与下面那条从 `_cas`/直写 UPDATE 机械枚举出来的
#: 集合必须相等;状态机加了新的非终态而这里没跟,判据当场红。
ALIVE_STATUSES = (
    "pending_freeze",
    "running",
    "commit_pending",
    "release_pending",
    "settlement_manual",
    "manual_resolving",
)

#: 每态期望的 run_state(来自现役统一投影 `run_status_projection`,不是我另编一套)。
EXPECTED_RUN_STATE = {
    "pending_freeze": "queued",
    "running": "running",
    "commit_pending": "settlement_pending",
    "release_pending": "settlement_pending",
    "settlement_manual": "needs_action",
    "manual_resolving": "needs_action",
}


@pytest.fixture()
def env(chain_db, live_redis):
    import psycopg2
    import psycopg2.extras

    import db.connection as dbconn

    dsn = chain_db("p14")
    dbconn.DATABASE_URL = dsn
    dbconn._pool = None
    conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    conn.cursor().execute("SET search_path = public")
    with conn.cursor() as cur:
        brand_id = seed_owner_brand_pricing(cur, TENANT, "p14")
        cur.execute(
            "INSERT INTO users (id, username, display_name, password_hash, email, is_active) "
            "VALUES (%s,%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
            (OTHER, "p14_other", "other", "p14o@example.com"))
    yield {"conn": conn, "brand_id": brand_id, "client": make_client(TENANT)}
    conn.close()


def test_denominator_matches_the_state_machine():
    """🔴 分母锁:`ALIVE_STATUSES` 必须等于从状态机机械枚举出来的那一组。

    手写的分母漏掉的那一项不会让任何判据变红 —— 所以这里不手列,现扫。
    """
    src = (REPO / "services/diagnosis_runs.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    scanned = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "_cas" and len(node.args) > 2:
            to = node.args[2]
            finish = [k.value for k in node.keywords if k.arg == "finish"]
            finished = bool(finish and isinstance(finish[0], ast.Constant)
                            and finish[0].value)
            if isinstance(to, ast.Constant) and not finished:
                scanned.add(to.value)
    # 不经 _cas 的两条直写(逐条带出处,不凭印象)
    assert "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending_freeze')" in src, \
        "INSERT 的初态锚点漂了 —— 分母可能已经不是 pending_freeze"
    assert "SET run_status='manual_resolving'" in src, \
        "manual_resolving 的直写锚点漂了"
    scanned |= {"pending_freeze", "manual_resolving"}
    assert scanned == set(ALIVE_STATUSES), (
        f"状态机枚举出来的活态 {sorted(scanned)} 与冻结分母 "
        f"{sorted(ALIVE_STATUSES)} 不等 —— 有新非终态没人管它在进度端点上长什么样")


def test_presentation_table_covers_every_run_state():
    """呈现表的键集必须**覆盖 RunState 全集**,且没有"其余按 collecting"的兜底行。

    分母从投影模块的 `RunState` Literal 现取,不手抄:
    投影加了第九个 run_state 而端点没跟,它会落进兜底 —— 而兜底若是 collecting,
    P1-4 就原样复发。
    """
    import typing

    from server import _ALIVE_RUN_STATE_FALLBACK, _ALIVE_RUN_STATE_VIEW
    from services.defensive_geo.run_status_projection import RunState

    all_states = set(typing.get_args(RunState))
    assert len(all_states) >= 8, f"RunState 分母塌了:{all_states}"
    assert set(_ALIVE_RUN_STATE_VIEW) == all_states, (
        f"呈现表键集 {sorted(_ALIVE_RUN_STATE_VIEW)} != RunState 全集 {sorted(all_states)}"
        " —— 漏的那一格会落进兜底,而没人验兜底长什么样")
    # 兜底本身也不许是 collecting/0(那就是 P1-4 的形状)
    assert _ALIVE_RUN_STATE_FALLBACK != ("collecting", 0), \
        "兜底落在了 collecting/0 —— 未知状态又会被伪装成刚开始采集"
    for state, (stage, progress) in _ALIVE_RUN_STATE_VIEW.items():
        if state != "running":
            assert (stage, progress) != ("collecting", 0), \
                f"{state} 被映射成 collecting/0 —— 只有 running 配得上这一格"


@pytest.mark.parametrize("run_status", ALIVE_STATUSES)
def test_each_alive_status_is_told_truthfully(env, run_status):
    """🔴 主锁:六态逐个,端点说的必须是**它自己**,不是一律"采集中 0%"。"""
    from services.defensive_geo.copy_registry import user_label
    from services.defensive_geo.run_status_projection import project

    sid = insert_run(env["conn"], env["brand_id"], run_status=run_status, alive=True)
    redis_down()

    out = poll_status(sid)
    expected_state = EXPECTED_RUN_STATE[run_status]
    assert project(run_status).run_state == expected_state, \
        f"投影表变了:{run_status} 现在是 {project(run_status).run_state}"

    assert out.get("found") is True, f"{run_status}:run 还活着却说找不到:{out}"
    if expected_state != "running":
        assert not (out.get("stage") == "collecting" and out.get("progress") == 0), \
            (f"{run_status} 被伪装成「刚开始采集」:{out} —— "
             "正在结算/已转人工的用户会被告知刚开始跑")
    assert out.get("message"), f"{run_status}:一句话都没给她:{out}"
    assert out["message"] == user_label("run_state", expected_state) \
        or out["message"] == user_label("reason", "progress_channel_degraded"), \
        f"{run_status} 的那句话不是 registry 里的(端点在现编第二套):{out['message']!r}"


def test_unknown_status_never_masquerades_as_collecting(env):
    """配对的必须不命中:**没见过**的状态值不许默认成 collecting。

    投影层的铁律是"未知双轴 quarantined,绝不默认 completed/released";
    端点这一层要接住同一条铁律 —— 未知必须进**可行动**的人工/核实态。
    """
    from services.defensive_geo.run_status_projection import project

    # 库里的 CHECK 约束不允许乱值,所以走投影层直接证:未知 ⇒ quarantined
    proj = project("some_status_nobody_has_seen")
    assert proj.run_state == "quarantined" and proj.known is False, \
        f"投影层对未知值的处置变了:{proj}"

    sid = insert_run(env["conn"], env["brand_id"], run_status="settlement_manual", alive=True)
    redis_down()
    out = poll_status(sid)
    # needs_action / quarantined 这一类必须是**可行动**的:不许 done=True 假装完成
    assert out.get("done") is not True, f"人工处理态被报成了完成:{out}"
    assert out.get("progress") != 0 or out.get("stage") != "collecting", \
        f'人工处理态仍然长得像「刚开始采集」:{out}'


def test_alive_branch_still_enforces_ownership(env):
    """新读了一列不等于可以少一道鉴权(配对的必须不命中)。"""
    from fastapi import HTTPException

    sid = insert_run(env["conn"], env["brand_id"], run_status="commit_pending", alive=True)
    redis_down()
    with pytest.raises(HTTPException) as exc:
        poll_status(sid, user_id=OTHER)
    assert exc.value.status_code == 403, f"别人也能读到这条进度:{exc.value!r}"
