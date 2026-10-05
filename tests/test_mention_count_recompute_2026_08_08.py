"""改写单元格后汇总计数必须跟着刷新 —— 真库真链路判据。

[WO_MENTION_COUNT_NOT_RECOMPUTED 2026-08-08 §5.A]

本 bug 的形态:``dimension_stats`` 和分数一直有人刷,``detected_count`` /
``overall_mention_rate`` 没人刷。客户版报告正文那句「品牌被提及 **N** 次」
读的正是 ``detected_count``,于是服务商人工确认 9 格、报告数字纹丝不动。

🔴 **§5.A③ 铁律:判据打在「落库后的 ai_visibility」上,不是打在 ``_aggregates()``
的返回值上。** 前两轮同类 bug(出现率分母明示 / 门户校准)全都是因为锁只验函数、
没验接线 —— 函数对、锁全绿、生产恒不生效。所以这里:

* 全部走**真 PostgreSQL**(``TEST_DATABASE_URL``),schema 由生产 ``init_db()`` +
  真 migration SQL 亲自建;
* 人工确认走**真 ``decide_brand_cell``**(真事务、真审计表),不是构造返回值;
* 断言一律**重新从库里 SELECT 出来**再看,不看内存里的那个 dict;
* 连"客户报告正文那句话"都真渲染一遍(``report_writer_v2``),
  证明修的是用户真看得见的那个数字。

每条判据都配了反向对照,写在各自 docstring 里。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="需要 TEST_DATABASE_URL 指向可写 throwaway PostgreSQL"
)

OWNER_UID = 770808

# 🔴 每次种一份**全新 id**,不做清场:
#   `monitoring_identity_decision_events` 是 append-only(触发器拒 DELETE ——
#   生产的真实保证),为了测试去绕它就等于把这条保证也一起测没了。
#   用新 id 天然隔离,顺带让用例之间不可能互相污染。
_SEQ = {"n": 0}


def _fresh_ids() -> tuple[int, int]:
    _SEQ["n"] += 1
    base = 900000 + _SEQ["n"] * 37
    return base, base + 500000  # (diagnosis_id, brand_id)


_CUR = {"diag": 0, "brand": 0, "name": ""}

# brands.name 有唯一约束 → 品牌名也要随种子唯一,
# 且答案正文/matched_text 必须用**同一个**名字(否则重判路径对不上)。
BRAND_NAME_BASE = "揭阳滨江南路雅栖酒店"
Q_LOCAL = "揭阳有哪些好的酒店推荐"
Q_SCENE = "商务出差住哪家酒店比较好"
ENGINES = ("kimi", "deepseek", "doubao", "qwen")


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════

def _cell(verdict: str, *, matched: str = "", reason: str = "trusted_exact_alias",
          answer: str = "", review_state: str | None = None) -> dict:
    cell = {
        "answer_summary": (answer or f"回答文本 {verdict}")[:150],
        "brand_detected": verdict == "YES",
        "brand_verdict": verdict,
        "detection_reason": reason,
        "full_response": answer or f"回答文本 {verdict}",
        "status": "success",
    }
    if matched:
        cell["matched_text"] = matched
    if review_state:
        cell["identity_review_state"] = review_state
    return cell


def _raw_payload(*, brand_name: str, detected_count: int, mention_rate: float,
                 total_tests: int = 8) -> dict:
    """两道题 × 四引擎 = 8 格。

    布局(YES 4 格 · NO 2 格 · PENDING 2 格):
      Q_LOCAL : YES  YES  NO   PENDING(待人工确认 · 就是它会被 confirm)
      Q_SCENE : YES  YES  NO   PENDING(**留着不确认**)

    🔴 第二格 PENDING 是**故意留的**:它让「确定分母 totals["total"]」(=YES+NO)
    与「记录自己的 total_tests」在数值上分开(确认后 7 vs 8)。
    第一版夹具两者恰好都等于 8,于是"把分母换成 totals[total]"这个变异是**空操作**,
    锁看着全绿其实什么都没管住 —— 变异验证 M07 存活才发现。
    """
    pending = _cell(
        "UNKNOWN",
        reason="local_evidence_requires_review",
        answer=f"推荐{brand_name}，位置便利。",
    )
    return {
        "data": {
            "ai_visibility": {
                "test_questions": [Q_LOCAL, Q_SCENE],
                "engines_tested": list(ENGINES),
                "question_types": {Q_LOCAL: "regional_industry", Q_SCENE: "super_tier1"},
                "total_tests": total_tests,
                "total_planned": 8,
                "detected_count": detected_count,
                "overall_mention_rate": mention_rate,
                "detail_table": [
                    {
                        "question": Q_LOCAL,
                        "results": {
                            "kimi": _cell("YES", matched=brand_name, answer=f"首推{brand_name}。"),
                            "deepseek": _cell("YES", matched=brand_name, answer=f"首推{brand_name}。"),
                            "doubao": _cell("NO"),
                            "qwen": pending,
                        },
                    },
                    {
                        "question": Q_SCENE,
                        "results": {
                            "kimi": _cell("YES", matched=brand_name, answer=f"首推{brand_name}。"),
                            "deepseek": _cell("YES", matched=brand_name, answer=f"首推{brand_name}。"),
                            "doubao": _cell("NO"),
                            "qwen": _cell(
                                "UNKNOWN", reason="local_evidence_requires_review",
                                answer=f"可以考虑{brand_name}。"),
                        },
                    },
                ],
            }
        },
        "scores": {},
    }


@pytest.fixture(scope="module")
def realdb():
    """全新真库 · schema 由生产 init_db()/init_monitoring_tables() + 真 migration 建。"""
    import psycopg2
    from psycopg2 import sql

    database_name = f"mentioncount_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    admin.close()

    parsed = urlsplit(PG_URL)
    target_url = urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment)
    )
    os.environ["DATABASE_URL"] = target_url

    from db import connection as connection_db

    previous_url = connection_db.DATABASE_URL
    connection_db.DATABASE_URL = target_url
    connection_db._pool = None

    from db.diagnosis_db import init_db
    from db.monitoring_db import init_monitoring_tables

    init_db()
    init_monitoring_tables()

    conn = connection_db.get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS client_profiles (
            id BIGSERIAL PRIMARY KEY, brand_id INTEGER,
            brand_display_names TEXT, is_deleted INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS brand_aliases (
            id BIGSERIAL PRIMARY KEY, canonical_name TEXT, alias TEXT,
            brand_id INTEGER, source TEXT DEFAULT 'manual',
            UNIQUE (canonical_name, alias))
    """)
    cur.execute("ALTER TABLE brands ADD COLUMN IF NOT EXISTS brand_display_names TEXT")
    cur.execute("ALTER TABLE brands ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE")
    cur.execute("ALTER TABLE brands ADD COLUMN IF NOT EXISTS company_name TEXT")
    # RBAC 侧表:归属判定要读它们(建空表即可 —— 本判据用的是"归属服务商",不是 admin)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS public.roles (
            id BIGINT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS public.user_roles (
            user_id BIGINT NOT NULL, role_id BIGINT NOT NULL,
            PRIMARY KEY (user_id, role_id));
        CREATE TABLE IF NOT EXISTS public.user_clients (
            user_id BIGINT NOT NULL, brand_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, brand_id));
    """)
    conn.commit()
    for migration in (
        "scripts/migration_monitoring_identity_review_2026_07_21.sql",
        "scripts/migration_diagnosis_identity_review_2026_07_22.sql",
    ):
        cur.execute((ROOT / migration).read_text(encoding="utf-8"))
        conn.commit()
    conn.close()

    yield target_url

    connection_db._pool = None
    connection_db.DATABASE_URL = previous_url
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(database_name))
        )
    admin.close()


def _seed(*, detected_count: int, mention_rate: float,
          col_detected: int | None = None, col_rate: float | None = None) -> int:
    from db.connection import get_connection

    diag_id, brand_id = _fresh_ids()
    brand_name = f"{BRAND_NAME_BASE}{_SEQ['n']}号店"
    _CUR["diag"], _CUR["brand"], _CUR["name"] = diag_id, brand_id, brand_name

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO brands (id, name, industry, owner_user_id, latest_score,"
        " latest_diagnosis_id) VALUES (%s,%s,%s,%s,%s,%s)",
        (brand_id, brand_name, "酒店", OWNER_UID, 58, diag_id),
    )
    cur.execute("INSERT INTO client_profiles (brand_id) VALUES (%s)", (brand_id,))
    cur.execute(
        "INSERT INTO diagnosis_records (id, session_id, brand_name, industry, brand_id,"
        " keywords, total_score, level, raw_data_json, result_visibility,"
        " ai_total_tests, ai_detected_count, ai_mention_rate)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'published',%s,%s,%s)",
        (diag_id, f"fixture-{uuid.uuid4().hex[:8]}", brand_name, "酒店", brand_id,
         "揭阳 酒店", 58, "成长型",
         json.dumps(_raw_payload(brand_name=brand_name, detected_count=detected_count,
                                mention_rate=mention_rate),
                    ensure_ascii=False),
         8,
         col_detected if col_detected is not None else detected_count,
         col_rate if col_rate is not None else mention_rate),
    )
    conn.commit()
    conn.close()
    return diag_id


@pytest.fixture()
def seeded(realdb):
    """基线一致的一份诊断:4 格 YES(PENDING 那格还没确认)· 8 有效测试 · 50.0%。"""
    return _seed(detected_count=4, mention_rate=50.0)


@pytest.fixture(autouse=True)
def _no_provider_calls(monkeypatch):
    """决策链禁止任何 provider/付费调用 —— 判据必须是本地确定性的。"""
    async def forbidden(**_kwargs):
        raise AssertionError("决策链禁止 provider 调用")

    monkeypatch.setattr(
        "services.brand_identity_resolver._default_structured_verifier", forbidden,
        raising=False,
    )


# ══════════════════════════════════════════════════════════════════════
# 读回工具:一律从库里重新 SELECT
# ══════════════════════════════════════════════════════════════════════

def _persisted() -> dict:
    """从库里重新读出落库后的 ai_visibility 与列镜像。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT raw_data_json, total_score, level, ai_detected_count, ai_mention_rate"
            "  FROM diagnosis_records WHERE id = %s",
            (_CUR["diag"],),
        )
        row = dict(cur.fetchone())
    finally:
        conn.close()
    raw = row["raw_data_json"]
    if isinstance(raw, str):
        raw = json.loads(raw)
    row["ai"] = raw["data"]["ai_visibility"]
    row["raw"] = raw
    return row


def _yes_cells_in_db() -> int:
    """按**分类器 SSOT** 数落库后判 YES 的格数。

    不直接数 ``brand_verdict == 'YES'``:``classify_cell_state`` 对没有
    ``brand_verdict`` 的老格子会回落到 ``brand_detected``,两者只在
    "格格都有 brand_verdict" 的记录上等价。用 SSOT 口径才不会在老记录上误判。
    """
    from services.diagnosis_identity_review import VERDICT_YES, classify_cell_state

    ai = _persisted()["ai"]
    return sum(
        1
        for item in ai["detail_table"]
        for cell in item["results"].values()
        if classify_cell_state(cell) == VERDICT_YES
    )


def _confirm_the_pending_cell():
    """走真 ``decide_brand_cell``:把 Q_LOCAL/qwen 那格人工确认成 YES。"""
    from db.connection import get_connection
    from services.diagnosis_identity_decision import decide_brand_cell

    return decide_brand_cell(
        diagnosis_id=_CUR["diag"],
        brand_id=_CUR["brand"],
        actor_user_id=OWNER_UID,
        question=Q_LOCAL,
        engine="qwen",
        action="confirm_yes",
        selected_name=_CUR["name"],
        reason="正文明确是本店",
        request_id=str(uuid.uuid4()),
        expected_version=0,
        ip="10.0.0.8",
        get_conn=get_connection,
    )


# ══════════════════════════════════════════════════════════════════════
# 判别力前提:中间那层真的在跑
# ══════════════════════════════════════════════════════════════════════

def test_the_pending_cell_is_really_pending_before_confirm(seeded):
    """前提:那格确实是 PENDING、且基线 detected_count 与 YES 格数一致。

    没有这条,后面「+1」可能是从别的状态跳过来的,判据会失去指向性。
    """
    from services.diagnosis_identity_review import STATE_PENDING_IDENTITY, classify_cell_state

    ai = _persisted()["ai"]
    target = ai["detail_table"][0]["results"]["qwen"]
    assert classify_cell_state(target) == STATE_PENDING_IDENTITY
    assert ai["detected_count"] == 4
    assert _yes_cells_in_db() == 4


# ══════════════════════════════════════════════════════════════════════
# §5.A① 人工确认 1 格 → detected_count +1(落库后)
# ══════════════════════════════════════════════════════════════════════

def test_confirm_one_cell_raises_persisted_detected_count_by_one(seeded):
    """§5.A① · 反向对照 = 变异 M01(删掉接线)→ 本条必红。

    这就是 5 份真客户被少报的那条路径。断言读的是**重新 SELECT 出来的**值。
    """
    before = _persisted()["ai"]["detected_count"]
    result = _confirm_the_pending_cell()
    assert result["success"] is True

    after = _persisted()["ai"]
    assert after["detected_count"] == before + 1 == 5, (
        f"人工确认 1 格后落库的 detected_count 应为 {before + 1},实际 {after['detected_count']}"
    )


def test_confirm_also_refreshes_the_rate_with_the_record_s_own_denominator(seeded):
    """rate 必须同步,且**分母一个字不动**(仍是记录自己的 total_tests)。

    §3.1 红线:「待确认格算不算分母」挂 Owner,本单不碰。所以这里同时断言
    分子变了、分母没变、公式与产生点 ai_tester 逐字一致。
    反向对照 = 变异 M02(rate 沿用旧值)→ 本条必红。
    """
    before = _persisted()["ai"]
    assert before["total_tests"] == 8

    _confirm_the_pending_cell()

    after = _persisted()["ai"]
    assert after["total_tests"] == 8, "分母被改动了 —— §3.1 红线"
    assert after["overall_mention_rate"] == round(5 / 8 * 100, 1) == 62.5
    assert after["overall_mention_rate"] != before["overall_mention_rate"]

    # 🔴 分母必须是**记录自己的 total_tests**,不是聚合出来的「确定分母」。
    #    夹具特意让两者不等(确认后 YES5+NO2=7 ≠ total_tests 8),
    #    否则"换分母"这个变异在数值上是空操作,这条判据就管不住 §3.1 红线。
    from services.diagnosis_identity_decision import (
        _aggregates, _ai_visibility, _load_identity_for_review, _parse_raw_data,
    )

    row = _persisted()
    ai2 = _ai_visibility(_parse_raw_data(json.dumps(row["raw"], ensure_ascii=False)))
    totals = _aggregates(ai2, _load_identity_for_review(
        _CUR["brand"], fallback_name=_CUR["name"]))["totals"]
    assert totals["total"] == 7 != after["total_tests"]
    assert after["overall_mention_rate"] != round(5 / totals["total"] * 100, 1)


# ══════════════════════════════════════════════════════════════════════
# §5.A③ 落库后的计数 == 该诊断内判 YES 的格数(不是看函数返回值)
# ══════════════════════════════════════════════════════════════════════

def test_persisted_count_equals_actual_yes_cells_after_confirm(seeded):
    """§5.A③ 核心不变式。反向对照 = 变异 M01/M03 任一 → 本条必红。"""
    _confirm_the_pending_cell()
    assert _persisted()["ai"]["detected_count"] == _yes_cells_in_db() == 5


def test_persisted_count_equals_actual_yes_cells_even_when_seed_was_wrong(realdb):
    """种一份**本来就错**的(库里写 99,实际 4 格 YES)→ 确认后必须落到真值 5。

    证明修复不是"在旧值上 +1",而是**重新派生** —— 旧值有多离谱都不影响结果。
    反向对照:若实现写成 ``old + 1``,这条会得到 100,必红。
    """
    _seed(detected_count=99, mention_rate=99.9)
    _confirm_the_pending_cell()
    ai = _persisted()["ai"]
    assert ai["detected_count"] == 5 == _yes_cells_in_db()
    assert ai["overall_mention_rate"] == 62.5


# ══════════════════════════════════════════════════════════════════════
# §5.A② 重建把 YES 改少 → detected_count 跟着减
# ══════════════════════════════════════════════════════════════════════

def test_rebuild_path_lowers_the_persisted_count(realdb, monkeypatch):
    """§5.A② · 存量重建方向相反(541/563 就是这样写成「被提及 17 次」的隐形级报告)。

    这里直接驱动**真的 rebuild_one**(apply=True 真提交),不是构造场景。
    反向对照 = 变异 M04(重建路径沿用旧值)→ 本条必红。
    """
    _seed(detected_count=4, mention_rate=50.0)

    # 让重建把两格 YES 判成 NO:把品牌身份换成一个与正文无关的名字。
    from services.brand_identity_resolver import BrandIdentity

    alien = BrandIdentity(
        brand_id=_CUR["brand"], canonical_names=("完全无关的另一个品牌",), industry="酒店",
    )
    monkeypatch.setattr(
        "services.diagnosis_identity_decision._load_identity_for_review",
        lambda brand_id, fallback_name="": alien,
    )

    from db.connection import get_connection
    from scripts.rebuild_parenthetical_false_mentions_2026_08_06 import (
        _preload_report_chain, rebuild_one,
    )

    _preload_report_chain()
    conn = get_connection()
    try:
        res = rebuild_one(conn, _CUR["diag"], apply=True)
        conn.commit()
    finally:
        conn.close()

    after = _persisted()["ai"]
    assert res["detected_before"] == 4
    assert after["detected_count"] == res["detected_after"] == _yes_cells_in_db()
    assert after["detected_count"] < 4, "重建把 YES 改少了,汇总必须跟着减"


# ══════════════════════════════════════════════════════════════════════
# §5.A④ 一致性回归:没有任何改写时,数字逐字不变
# ══════════════════════════════════════════════════════════════════════

def test_recompute_is_a_noop_on_an_already_consistent_record(seeded):
    """§5.A④ · 证明本改动不会顺带改动正常诊断的数字。

    反向对照见下一条:故意偏移 1 时本判据的等式必须能看出来(否则它是恒真的)。
    """
    from services.diagnosis_identity_decision import (
        _aggregates, _ai_visibility, _load_identity_for_review, _parse_raw_data,
        apply_recomputed_totals,
    )

    row = _persisted()
    ai = _ai_visibility(_parse_raw_data(json.dumps(row["raw"], ensure_ascii=False)))
    identity = _load_identity_for_review(_CUR["brand"], fallback_name=_CUR["name"])
    delta = apply_recomputed_totals(ai, _aggregates(ai, identity))

    assert delta["detected_count"]["before"] == delta["detected_count"]["after"] == 4
    assert delta["overall_mention_rate"]["before"] == delta["overall_mention_rate"]["after"] == 50.0


def test_the_noop_lock_can_actually_see_a_one_off_drift(realdb):
    """§5.A④ 的反向对照:把种子偏移 1 → 上一条那个等式必须不成立。

    没有这条,「逐字不变」可能只是因为判据本身恒真。
    """
    from services.diagnosis_identity_decision import (
        _aggregates, _ai_visibility, _load_identity_for_review, _parse_raw_data,
        apply_recomputed_totals,
    )

    _seed(detected_count=5, mention_rate=62.5)  # 实际只有 4 格 YES,故意偏 1
    row = _persisted()
    ai = _ai_visibility(_parse_raw_data(json.dumps(row["raw"], ensure_ascii=False)))
    identity = _load_identity_for_review(_CUR["brand"], fallback_name=_CUR["name"])
    delta = apply_recomputed_totals(ai, _aggregates(ai, identity))

    assert delta["detected_count"]["before"] == 5
    assert delta["detected_count"]["after"] == 4
    assert delta["detected_count"]["before"] != delta["detected_count"]["after"]


# ══════════════════════════════════════════════════════════════════════
# 扫出的第三处陈旧点:DB 列镜像
# ══════════════════════════════════════════════════════════════════════

def test_column_mirror_is_refreshed_in_the_same_transaction(seeded):
    """列 ``ai_detected_count`` / ``ai_mention_rate`` 必须跟着刷。

    工单没列到这一处。消费方两个:``/api/strategy/generate`` 拿它喂销售话术;
    ``scripts/regen_v2_reports.py:107`` 会拿列值**反向覆盖回 JSON** ——
    只修 JSON 不修列,跑一次 regen 就把修好的冲掉。
    反向对照 = 变异 M05(删掉列 UPDATE)→ 本条必红。
    """
    _confirm_the_pending_cell()
    row = _persisted()
    assert row["ai_detected_count"] == 5
    assert float(row["ai_mention_rate"]) == 62.5
    # 列与 JSON 不许分家
    assert row["ai_detected_count"] == row["ai"]["detected_count"]


def test_rebuild_path_also_refreshes_the_column_mirror(realdb, monkeypatch):
    """重建路径同样要刷列 —— 两条路径不许只修一条(「两道墙只拆一道=白修」)。"""
    _seed(detected_count=4, mention_rate=50.0, col_detected=4, col_rate=50.0)

    from services.brand_identity_resolver import BrandIdentity

    alien = BrandIdentity(
        brand_id=_CUR["brand"], canonical_names=("完全无关的另一个品牌",), industry="酒店",
    )
    monkeypatch.setattr(
        "services.diagnosis_identity_decision._load_identity_for_review",
        lambda brand_id, fallback_name="": alien,
    )

    from db.connection import get_connection
    from scripts.rebuild_parenthetical_false_mentions_2026_08_06 import (
        _preload_report_chain, rebuild_one,
    )

    _preload_report_chain()
    conn = get_connection()
    try:
        rebuild_one(conn, _CUR["diag"], apply=True)
        conn.commit()
    finally:
        conn.close()

    row = _persisted()
    assert row["ai_detected_count"] == row["ai"]["detected_count"] == _yes_cells_in_db()


# ══════════════════════════════════════════════════════════════════════
# 接线终点:客户报告正文那句话真的变了
# ══════════════════════════════════════════════════════════════════════

def test_the_customer_facing_sentence_really_changes(seeded):
    """🔴 最要紧的一条:修的是**用户看得见的那个数字**,不是内存里的字段。

    直接把落库后的 ai_visibility 交给真渲染器 ``report_writer_v2``,
    grep 正文里那句「品牌被提及 **N** 次」。
    反向对照:确认前必须是「4 次」,确认后必须是「5 次」——
    两侧都断言,单侧断言会被恒真骗过。
    """
    from services.report_writer_v2 import build_module_3_raw_ai_appendix

    def sentence() -> str:
        ai = _persisted()["ai"]
        # 喂法与生产一致:装配层把 ai_visibility 放在 diagnosis_data.ai_visibility_data
        return build_module_3_raw_ai_appendix(
            {"diagnosis_data": {"ai_visibility_data": ai}}
        )["rendered_md"]

    before_md = sentence()
    assert "品牌被提及 **4 次**" in before_md

    _confirm_the_pending_cell()

    after_md = sentence()
    assert "品牌被提及 **5 次**" in after_md
    assert "品牌被提及 **4 次**" not in after_md


# ══════════════════════════════════════════════════════════════════════
# §3 红线
# ══════════════════════════════════════════════════════════════════════

def test_headline_score_and_level_do_not_move_because_of_this_fix(seeded):
    """头牌总分/等级来自 dimension_stats,**不来自计数** —— 本次改动不许动它。

    §6 要求量化评分影响,不许静默改分。这条把"不影响"钉死:
    同一次确认里,分数该怎么变还怎么变(由 dimension_stats 决定),
    但把种子的 detected_count 换成一个离谱值,分数**一模一样**。
    """
    _seed(detected_count=4, mention_rate=50.0)
    _confirm_the_pending_cell()
    sane = _persisted()

    _seed(detected_count=99, mention_rate=99.9)
    _confirm_the_pending_cell()
    absurd = _persisted()

    assert sane["total_score"] == absurd["total_score"]
    assert sane["level"] == absurd["level"]
    # 而计数确实跟着真值走(证明上面那个"相等"不是因为两边都没跑)
    assert sane["ai"]["detected_count"] == absurd["ai"]["detected_count"] == 5


def test_human_decided_cells_are_not_overturned(seeded):
    """§3.3 人工已决格不许被机器推翻 —— 确认后那格必须仍是 confirmed/YES。"""
    _confirm_the_pending_cell()
    ai = _persisted()["ai"]
    target = ai["detail_table"][0]["results"]["qwen"]
    assert target["brand_verdict"] == "YES"
    assert target.get("identity_review_state") == "confirmed"


def test_full_response_is_never_touched(seeded):
    """原始回答一字节不改(与上一单同口径,防"顺手改文案")。"""
    def digest() -> str:
        ai = _persisted()["ai"]
        answers = [
            cell.get("full_response")
            for item in ai["detail_table"]
            for cell in item["results"].values()
        ]
        return hashlib.sha256(
            json.dumps(answers, ensure_ascii=False).encode("utf-8")).hexdigest()

    before = digest()
    _confirm_the_pending_cell()
    assert digest() == before


# ══════════════════════════════════════════════════════════════════════
# 回填脚本
# ══════════════════════════════════════════════════════════════════════

def test_backfill_dry_run_writes_nothing(realdb, capsys):
    """§4.2 dry-run 必须只看不写。反向对照 = 下一条 apply 真的写了。"""
    _seed(detected_count=99, mention_rate=99.9)
    from db.connection import get_connection
    from scripts.backfill_mention_count_2026_08_08 import backfill_one

    conn = get_connection()
    try:
        res = backfill_one(conn, _CUR["diag"], apply=False)
        # 🔴 这里**必须 commit 而不是 rollback**:第一版写的是 rollback,
        #    等于判据自己把"dry-run 偷偷写库"给盖住了(变异 M10 因此存活)。
        #    真 dry-run 压根没有东西可提交,commit 是无害的;
        #    真写了库,commit 就会把它暴露出来。
        conn.commit()
    finally:
        conn.close()

    assert res["status"] == "dry_run"
    assert res["detected_before"] == 99 and res["detected_after"] == 4
    assert _persisted()["ai"]["detected_count"] == 99, "dry-run 不许落库"
    assert _persisted()["ai_detected_count"] == 99, "dry-run 不许动列镜像"


def test_backfill_apply_fixes_json_and_columns(realdb):
    """§4.2 真回填:JSON 与列一起修好,且**一个单元格都没动**。"""
    _seed(detected_count=99, mention_rate=99.9, col_detected=99, col_rate=99.9)

    from db.connection import get_connection
    from scripts.backfill_mention_count_2026_08_08 import backfill_one
    from scripts.rebuild_parenthetical_false_mentions_2026_08_06 import _preload_report_chain

    cells_before = json.dumps(_persisted()["ai"]["detail_table"], ensure_ascii=False, sort_keys=True)

    _preload_report_chain()
    conn = get_connection()
    try:
        res = backfill_one(conn, _CUR["diag"], apply=True)
        conn.commit()
    finally:
        conn.close()

    assert res["status"] == "backfilled"
    row = _persisted()
    assert row["ai"]["detected_count"] == 4 == _yes_cells_in_db()
    assert row["ai"]["overall_mention_rate"] == 50.0
    assert row["ai_detected_count"] == 4
    assert float(row["ai_mention_rate"]) == 50.0
    cells_after = json.dumps(row["ai"]["detail_table"], ensure_ascii=False, sort_keys=True)
    assert cells_after == cells_before, "回填脚本不许改单元格"


def test_backfill_does_not_change_the_funnel_derived_score(realdb):
    """回填不改**漏斗派生的**分数 —— 它由 dimension_stats 决定,而回填不动单元格。

    🔴 措辞和做法都较真过两轮:
      · 回填脚本自己不写 total_score/level,但它触发的 v2 重生
        (``update_diagnosis_v2_in_db``)在 funnel 有效时**会**把这两列同步成评分 SSOT 值。
      · 所以不变式不是"这两列字节不变"(第一版这么写,被 `成长型→成长级` 打红,
        那是我的种子写了非规范等级被纠正),也不是"等于种子值"(第二版这么写,
        被 `58 → 67` 打红,那是我的种子分数本来就与载荷对不上)。
      · 正确的不变式是**幂等**:同步过一次之后再跑一次,分数/等级一个字不动。
        这条不受种子任意性影响,且真能抓住"回填顺手改分"。

    反向对照见下一条(非规范等级会被纠正 → 证明这两列确实在被写,本条不是恒真)。
    """
    from tools.scoring.funnel_score import get_funnel_meta
    from db.connection import get_connection
    from scripts.backfill_mention_count_2026_08_08 import backfill_one
    from scripts.rebuild_parenthetical_false_mentions_2026_08_06 import _preload_report_chain

    _seed(detected_count=99, mention_rate=99.9)
    _preload_report_chain()

    def run_backfill():
        conn = get_connection()
        try:
            backfill_one(conn, _CUR["diag"], apply=True)
            conn.commit()
        finally:
            conn.close()

    run_backfill()          # 第 1 次:把列同步到 SSOT
    first = _persisted()
    run_backfill()          # 第 2 次:必须什么都不动
    second = _persisted()

    assert second["total_score"] == first["total_score"]
    assert second["level"] == first["level"] == get_funnel_meta(first["total_score"])["level"]
    # 计数也必须幂等(顺带证明第二次确实跑了、不是被跳过)
    assert second["ai"]["detected_count"] == first["ai"]["detected_count"] == 4


def test_backfill_syncs_a_non_canonical_level_to_the_scoring_ssot(realdb):
    """上一条的反向对照:种一个非规范等级 → 回填后必须被纠正成 SSOT 值。

    这条同时证明上一条那个"相等"不是恒真(v2 重生确实在写这两列)。
    """
    from tools.scoring.funnel_score import get_funnel_meta
    from db.connection import get_connection

    _seed(detected_count=99, mention_rate=99.9)
    conn = get_connection()
    conn.cursor().execute(
        "UPDATE diagnosis_records SET level = %s WHERE id = %s",
        ("胡编的等级", _CUR["diag"]),
    )
    conn.commit()
    conn.close()
    assert _persisted()["level"] == "胡编的等级"

    from scripts.backfill_mention_count_2026_08_08 import backfill_one
    from scripts.rebuild_parenthetical_false_mentions_2026_08_06 import _preload_report_chain

    _preload_report_chain()
    conn = get_connection()
    try:
        backfill_one(conn, _CUR["diag"], apply=True)
        conn.commit()
    finally:
        conn.close()

    after = _persisted()
    assert after["level"] == get_funnel_meta(after["total_score"])["level"] != "胡编的等级"


def test_backfill_has_no_default_id_list(realdb):
    """§4.2:没有默认名单,必须显式传 —— 防"顺手全跑"。"""
    import scripts.backfill_mention_count_2026_08_08 as mod

    source = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    assert "DEFAULT_IDS" not in source
    assert 'required=True' in source


# ══════════════════════════════════════════════════════════════════════
# 元判据:不许再写第二套统计
# ══════════════════════════════════════════════════════════════════════

def _strip_comments_and_strings(source: str) -> str:
    """剥注释**和字符串字面量** —— 否则会撞上本文件/被测文件自己讲这件事的话。"""
    import io
    import tokenize

    out: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return "\n".join(out)


def test_both_rewrite_paths_go_through_the_single_derivation_point():
    """§3.2:两条改写路径都必须调 ``apply_recomputed_totals``,不许各写一套。

    先自证判据抓得住:在剥掉注释和字符串的正文里找调用点;
    然后反向对照 —— 同样的扫法在一个没接线的文件里必须找不到。
    """
    decision = _strip_comments_and_strings(
        (ROOT / "services" / "diagnosis_identity_decision.py").read_text(encoding="utf-8"))
    rebuild = _strip_comments_and_strings(
        (ROOT / "scripts" / "rebuild_parenthetical_false_mentions_2026_08_06.py").read_text(
            encoding="utf-8"))
    backfill = _strip_comments_and_strings(
        (ROOT / "scripts" / "backfill_mention_count_2026_08_08.py").read_text(encoding="utf-8"))

    for name, src in (("decide", decision), ("rebuild", rebuild), ("backfill", backfill)):
        assert "apply_recomputed_totals" in src, f"{name} 没走单点派生"

    # 反向对照:随便找一个不该出现它的模块,同样扫法必须落空
    unrelated = _strip_comments_and_strings(
        (ROOT / "services" / "diagnosis_identity_review.py").read_text(encoding="utf-8"))
    assert "apply_recomputed_totals" not in unrelated


def test_detected_count_is_assigned_in_exactly_one_place():
    """§3.2 单点写入:全仓只允许 ``apply_recomputed_totals`` 给 ``detected_count`` 赋值。

    这条比"禁止出现 brand_detected"精确 —— 后者会误伤合法用法
    (回填脚本的单元格指纹就要读 ``brand_detected``)。
    这里用 AST 找**赋值**形态 ``ai["detected_count"] = ...``,只认写入不认读取。

    先自证判据有判别力:在派生函数自己身上必须**能**找到那处赋值。
    """
    import ast

    def assignment_sites(rel: str) -> list[tuple[str, int]]:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        hits: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in ("detected_count", "overall_mention_rate")
                ):
                    hits.append((rel, node.lineno))
        return hits

    derivation = assignment_sites("services/diagnosis_identity_decision.py")
    assert derivation, "派生函数里找不到赋值 —— 判据自己坏了(零判别力)"

    # 那些赋值必须全部落在 apply_recomputed_totals 函数体内
    src = (ROOT / "services" / "diagnosis_identity_decision.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "apply_recomputed_totals"
    )
    lo, hi = fn.lineno, (fn.end_lineno or fn.lineno)
    outside = [(rel, line) for rel, line in derivation if not (lo <= line <= hi)]
    assert not outside, f"派生点之外还有人写这两个字段:{outside}"

    # 另外两条改写路径一处都不许自己写
    for rel in (
        "scripts/rebuild_parenthetical_false_mentions_2026_08_06.py",
        "scripts/backfill_mention_count_2026_08_08.py",
    ):
        assert not assignment_sites(rel), f"{rel} 自己写了这两个字段 —— 应当只调派生点"


# ══════════════════════════════════════════════════════════════════════
# §6 评分影响量化 —— 不许静默改分
# ══════════════════════════════════════════════════════════════════════

def test_quantify_the_only_scoring_path_that_reads_detected_count(realdb):
    """§6:把「修正 detected_count 会不会连带改分」当场算两遍给出数字。

    全仓读 ``detected_count`` 的评分器有四个,但**改写路径上真正会跑到的只有一条**:
    ``services/diagnosis_report_v2.py:98`` —— 当记录**没有** 5 维 SSOT 时,
    用 ``calculate_geo_scope_score`` 重算 5 维,而它的 ``baseline_score``
    正比于 ``detected_count / total_tests``。

    结论(本判据当场算,不是文案):
      · **头牌 total_score / level 不受影响** —— 报告返回的 total_score 取自
        ``funnel_result``(来自 dimension_stats),另有专门判据钉死;
      · 受影响的只有 5 维旧表(``report["scores"]``,Module 2 向后兼容那张),
        且只发生在无 5 维 SSOT 的老记录上。方向与修正一致:
        少报的记录分数**上升**,多报的记录分数**下降**。

    反向对照:若把两次输入喂成同一个 detected_count,差值必须为 0
    (证明这个判据确实在量它,而不是恒真)。
    """
    from tools.scoring.geo_scope_scorer import calculate_geo_scope_score

    _seed(detected_count=4, mention_rate=50.0)
    ai_before = _persisted()["ai"]

    _confirm_the_pending_cell()
    ai_after = _persisted()["ai"]
    assert ai_before["detected_count"] == 4 and ai_after["detected_count"] == 5

    before = calculate_geo_scope_score(ai_visibility_data=ai_before, web_search_data={})
    after = calculate_geo_scope_score(ai_visibility_data=ai_after, web_search_data={})

    b_sum = sum(int(v or 0) for v in (before.get("dimension_scores") or {}).values())
    a_sum = sum(int(v or 0) for v in (after.get("dimension_scores") or {}).values())

    # 少报被修正 → 5 维旧表分数只会持平或上升,绝不下降
    assert a_sum >= b_sum, f"修正少报后 5 维旧表分数反而降了:{b_sum} → {a_sum}"

    # 反向对照:同一份输入喂两遍,差值必须为 0(否则本判据是恒真的)
    same = calculate_geo_scope_score(ai_visibility_data=ai_before, web_search_data={})
    s_sum = sum(int(v or 0) for v in (same.get("dimension_scores") or {}).values())
    assert s_sum == b_sum

    # 把数字打出来,交付单里那两行就是从这里抄的
    print(f"[§6 量化] 5 维旧表 sum: {b_sum} → {a_sum}(delta={a_sum - b_sum})")


def test_detected_count_only_enters_scoring_through_a_floor():
    """§6 精确边界:``detected_count`` 只经由一个**地板**影响 5 维旧表。

    ``geo_scope_scorer._score_ai_recommendation`` 在有 ``dimension_stats`` 时返回
    ``max(dimension_score, baseline_score // 2)``:
      · ``dimension_score`` 完全来自 ``dimension_stats`` —— **与 detected_count 无关**;
      · ``baseline_score = round(30 * detected/total)`` —— 只有它受影响,且要先 ``// 2``
        再跟 dimension_score 取大。
    ⇒ **只有当地板高过 dimension_score 时,修正 detected_count 才会改分**;
      其余情况影响精确为 0。这条把边界钉死,交付单里的数字从这里来。

    用受害最重那份(529:23 有效测试 · 8 → 17)当算例。
    """
    from tools.scoring.geo_scope_scorer import _score_ai_recommendation

    stats_high = {  # dimension_score 高于两个地板 → 修正前后必须一模一样
        "super_tier1": {"total": 10, "detected": 6},
        "regional_industry": {"total": 8, "detected": 5},
        "brand_awareness": {"total": 5, "detected": 4},
    }
    stats_low = {  # dimension_score 压到 0 → 地板生效,差值最大化
        "super_tier1": {"total": 10, "detected": 0},
        "regional_industry": {"total": 8, "detected": 0},
        "brand_awareness": {"total": 5, "detected": 0},
    }

    def score(stats, detected):
        return _score_ai_recommendation(
            {"dimension_stats": stats, "detected_count": detected, "total_tests": 23})

    # ① dimension_score 够高 → 影响精确为 0
    assert score(stats_high, 8) == score(stats_high, 17)

    # ② dimension_score 为 0 → 地板生效,这就是**最坏情况**的量
    worst_before, worst_after = score(stats_low, 8), score(stats_low, 17)
    assert worst_after > worst_before
    assert worst_before == round(30 * 8 / 23) // 2 == 5
    assert worst_after == round(30 * 17 / 23) // 2 == 11
    print(f"[§6 边界] 529 形状最坏情况:ai_recommendation {worst_before} → {worst_after}"
          f"(+{worst_after - worst_before} 分 / 满分 30);dimension_score 足够高时为 0")


def test_records_with_5dim_ssot_are_not_rescored_at_all(realdb):
    """有 5 维 SSOT 的记录压根不走重算分支 → 本次修正对它们**零**评分影响。

    这条把 §6 的影响面**收窄并证明**:只有"无 5 维 SSOT 的老记录"会被波及。
    做法:同一份 ai_visibility,一次带 5 维 SSOT、一次不带,看装配层给出的
    ``scores`` 是不是同一个 —— 带 SSOT 那次必须原样保留传入值。
    """
    from services.diagnosis_report_v2 import _shape_report_data

    _seed(detected_count=4, mention_rate=50.0)
    raw = _persisted()["raw"]

    ssot = {
        "ai_recommendation_score": 21,
        "web_content_score": 17,
        "authority_score": 13,
        "structured_content_score": 11,
        "brand_foundation_score": 9,
    }
    with_ssot = _shape_report_data(
        diagnosis_results=raw, score_data={"dimension_scores": dict(ssot)},
        brand={"name": _CUR["name"]}, profile={}, completeness={},
    )
    assert with_ssot["scores"] == ssot, "带 5 维 SSOT 的记录不该被重算"

    without_ssot = _shape_report_data(
        diagnosis_results=raw, score_data={},
        brand={"name": _CUR["name"]}, profile={}, completeness={},
    )
    # 反向对照:不带 SSOT 那次确实走了重算(否则上面那个"没变"没有信息量)
    assert without_ssot["scores"] != ssot

    # 且两者的头牌总分都来自 funnel,与 5 维无关 —— 这就是"不静默改分"的凭据
    assert with_ssot["total_score"] == without_ssot["total_score"]


def test_missing_denominator_never_fabricates_a_zero_rate(realdb):
    """没有 ``total_tests`` 的老记录:刷新分子,但**绝不**把 rate 写成 0。

    这条是 A/B 回归打出来的:既有假库夹具的 ai_visibility 压根没有 ``total_tests``,
    第一版实现在这种情况下写 `rate = 0` —— 等于凭空把客户推荐率抹成 0%,
    正是本单要消灭的「把客户成绩说低」那个方向,比不刷新更坏。

    反向对照:有分母时必须真的算(见 refreshes_the_rate 那条);
    这里额外断言列镜像也保留原值(SQL 的 COALESCE 真在起作用)。
    """
    from db.connection import get_connection
    from services.diagnosis_identity_decision import (
        _aggregates, _ai_visibility, _load_identity_for_review, _parse_raw_data,
        apply_recomputed_totals,
    )

    _seed(detected_count=4, mention_rate=50.0, col_rate=50.0)
    # 把 total_tests 抹掉,模拟老记录
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT raw_data_json FROM diagnosis_records WHERE id = %s", (_CUR["diag"],))
    raw = cur.fetchone()["raw_data_json"]
    if isinstance(raw, str):
        raw = json.loads(raw)
    raw["data"]["ai_visibility"].pop("total_tests", None)
    cur.execute("UPDATE diagnosis_records SET raw_data_json = %s WHERE id = %s",
                (json.dumps(raw, ensure_ascii=False), _CUR["diag"]))
    conn.commit()
    conn.close()

    ai = _ai_visibility(_parse_raw_data(json.dumps(raw, ensure_ascii=False)))
    identity = _load_identity_for_review(_CUR["brand"], fallback_name=_CUR["name"])
    delta = apply_recomputed_totals(ai, _aggregates(ai, identity))

    assert delta["rate_recomputed"] is False
    assert ai["detected_count"] == 4          # 分子照常刷新(它不需要分母)
    assert ai["overall_mention_rate"] == 50.0  # 原值原封不动,**不是 0**

    # 走真链路确认列镜像也保留原值(COALESCE 生效)
    _confirm_the_pending_cell()
    row = _persisted()
    assert row["ai"]["detected_count"] == 5
    assert float(row["ai_mention_rate"]) == 50.0, "无分母时列里的 rate 不许被写 0"
