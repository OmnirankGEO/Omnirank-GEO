# -*- coding: utf-8 -*-
"""WO_317 第四笔 · 个人路径同一请求编号不许扣两次(真 PG · 真 server.api_generate_titles · 真扣费)。

病:组织路径有 reserve_charge + claim(同编号 409),个人路径直接 deduct_points,title_batch_charges 只记第一笔。
    双击 / 两个标签页 / 网络重试(前端指纹不变 ⇒ 同一编号)⇒ 各扣一次,第二次整表保存还删掉第一次的草稿。
修:扣费前 claim_batch 抢占(charge_tx_id NULL),抢不到 ⇒ 409 不扣不删;扣完回填;整批失败退费后释放。

  E1 🔴 同一编号并发两次 ⇒ 恰一笔扣费、另一次 409、第一次的草稿一条不少
  E2 🔴 不同编号 ⇒ 各扣各的
  E3 🔴 中途失败(LLM 无产出 ⇒ 全额退)后同编号重试 ⇒ 能再扣一次、能出题(不是永远 409)
  E4 🔴 抢占行 charge_tx_id 为 NULL ⇒ authorize_recovery 认作没付过(不给免费补救);扣完回填后才认
  E5 🔴 对照:组织路径早有的两道 409(回放终态 / 正在执行)仍在
  E6–E10 (第五 / 六笔)被掐遗弃的抢占:没扣过的超 30 分钟可接管并扣一次;**已付未退的永不接管**(409,
        受理凭据里每个词走 WO_241 丙免费补救);未超时 / 已有选题 409;已退款的可接管重扣
  RV C2/R1–R3 (第六笔,Review 复现臂原样收入)已交付的已付批次被开写 / 整表重出改写编号后,同编号重放
        ⇒ 409、不退不扣(第五笔在此白给一次生成或退掉已交付那笔)
只测扣费语义:租户归属校验 require_quote_access 桩成放行(它有自己的锁);其余全真。
"""
from __future__ import annotations

import asyncio
import ast
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def server_mod():
    import db.diagnosis_db  # noqa: F401  先把 init_db 跑完,别和本文件的数据互等
    import server
    return server


@pytest.fixture
def world(server_mod, monkeypatch):
    from db.auth_db import create_user
    from db.connection import get_connection
    from db.wallet_db import grant_trial_bonus
    from writing import keyword_topic_generator as ktg
    import auth.brand_access as ba

    uname = "replay_%s_test" % uuid.uuid4().hex[:8]
    uid = create_user(uname, "x-" + uuid.uuid4().hex, "重放判据", role_ids=[], must_change_password=0)
    grant_trial_bonus(uid)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO brands (name, owner_user_id, is_test) VALUES (%s, %s, true) RETURNING id",
                (uname, uid))
    bid = cur.fetchone()["id"]
    cur.execute("INSERT INTO quotes (brand_id, brand_name, industry, status, writing_status, owner_user_id) "
                "VALUES (%s, %s, '测试行业', 'paid', 'in_progress', %s) RETURNING id", (bid, uname, uid))
    qid = cur.fetchone()["id"]
    for kw in ("重放甲", "重放乙"):
        cur.execute("INSERT INTO confirmed_keywords (quote_id, brand_id, keyword, required_articles, is_core, status) "
                    "VALUES (%s, %s, %s, 1, true, 'confirmed')", (qid, bid, kw))
    conn.commit()

    monkeypatch.setattr(ba, "require_quote_access", lambda *a, **k: {"id": qid, "brand_id": bid})
    state = {"gen": None, "calls": 0, "empty": False}
    orig_init = ktg.KeywordTopicGenerator.__init__

    def _init(self, *a, **k):
        orig_init(self, *a, **k)
        state["gen"] = self

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload
            self.text = json.dumps(payload, ensure_ascii=False)

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    async def _post(_self, url, *a, json=None, **k):
        if "chat/completions" not in str(url):
            raise RuntimeError("判据拒绝外呼 %s" % url)
        state["calls"] += 1
        gen = state["gen"]
        # 让出事件循环:E1 的第二个请求要在第一个**生成途中**真正插进来(不然两个协程是先后跑完的)
        await asyncio.sleep(0.05)
        prompt = "".join(str(m.get("content") or "") for m in ((json or {}).get("messages") or []))
        names = {kw.get("id"): kw.get("keyword") for kw in gen.keywords}
        topics = [] if state["empty"] else [
            {"original_keyword": names[p["keyword_id"]], "keyword_id": p["keyword_id"], "slot_index": p["slot_index"],
             "optimized_title": "%s怎么选?第%d个要看的地方" % (names[p["keyword_id"]], p["slot_index"] + 1),
             "article_style": p.get("user_choice") or ""}
            for p in (gen.style_plan or []) if names[p["keyword_id"]] in prompt]
        return _Resp({"choices": [{"message": {"content": __import__("json").dumps({"topics": topics}, ensure_ascii=False)}}],
                      "model": "fake", "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    monkeypatch.setattr(ktg.KeywordTopicGenerator, "__init__", _init)
    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("判据拒绝同步外呼")))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-not-a-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-not-a-key")

    def call(rid):
        req = SimpleNamespace(
            state=SimpleNamespace(user={"user_id": uid, "is_admin": False}, organization_identity=None),
            headers={"x-request-id": rid})
        return server_mod.api_generate_titles(server_mod.TitleGenerateRequest(quote_id=qid), req)

    def balance():
        c = conn.cursor()
        c.execute("SELECT COALESCE(paid_points,0)+COALESCE(bonus_points,0)+COALESCE(commission_points,0) AS b "
                  "FROM user_wallets WHERE user_id=%s", (uid,))
        row = c.fetchone()
        conn.commit()
        return int(row["b"]) if row else 0

    def drafts():
        c = conn.cursor()
        c.execute("SELECT count(*) AS n FROM topics WHERE quote_id=%s AND status IN ('draft','pending') "
                  "AND optimized_title IS NOT NULL", (qid,))
        n = int(c.fetchone()["n"])
        conn.commit()
        return n

    try:
        yield SimpleNamespace(uid=uid, qid=qid, call=call, balance=balance, drafts=drafts, state=state, conn=conn)
    finally:
        c = conn.cursor()
        conn.rollback()
        c.execute("DELETE FROM topics WHERE quote_id=%s", (qid,))
        c.execute("DELETE FROM title_batch_charges WHERE quote_id=%s", (qid,))
        c.execute("DELETE FROM confirmed_keywords WHERE quote_id=%s", (qid,))
        c.execute("DELETE FROM quotes WHERE id=%s", (qid,))
        c.execute("DELETE FROM brands WHERE id=%s", (bid,))
        conn.commit()
        conn.close()


def _run(*coros):
    async def _go():
        return await asyncio.gather(*coros, return_exceptions=True)
    return asyncio.run(_go())


def _status(r):
    from fastapi import HTTPException
    if isinstance(r, HTTPException):
        return r.status_code
    if isinstance(r, BaseException):
        raise r
    return 200 if r.get("success") else "fail"


def test_e1_same_request_twice_charges_once(world):
    b0 = world.balance()
    r1, r2 = _run(world.call("replay-e1"), world.call("replay-e1"))
    assert sorted([_status(r1), _status(r2)], key=str) == [200, 409], (r1, r2)
    assert b0 - world.balance() == 160, "同一编号扣了不止一笔"                  # 2 词 × 80
    assert world.drafts() == 2, "第一次的草稿被动了"
    r3, = _run(world.call("replay-e1"))                                          # 事后再来一次也 409
    assert _status(r3) == 409 and world.drafts() == 2 and b0 - world.balance() == 160


def test_e2_different_requests_are_charged_separately(world):
    b0 = world.balance()
    ra, = _run(world.call("replay-e2-a"))
    rb, = _run(world.call("replay-e2-b"))
    assert _status(ra) == 200 and _status(rb) == 200
    assert b0 - world.balance() == 320


def test_e3_after_a_refunded_failure_the_same_request_can_retry(world):
    b0 = world.balance()
    world.state["empty"] = True
    r1, = _run(world.call("replay-e3"))
    assert _status(r1) == "fail" and world.balance() == b0                       # 无产出 ⇒ 全额退
    world.state["empty"] = False
    r2, = _run(world.call("replay-e3"))
    assert _status(r2) == 200, r2
    assert b0 - world.balance() == 160 and world.drafts() == 2


def test_e4_a_claim_without_a_charge_is_not_a_paid_batch(world):
    from services.title_batch_charge_registry import authorize_recovery, claim_batch, record_batch_charge
    c = world.conn.cursor()
    assert claim_batch(c, generation_request_id="replay-e4", user_id=world.uid, quote_id=world.qid)
    assert not claim_batch(c, generation_request_id="replay-e4", user_id=world.uid, quote_id=world.qid)
    ok, why = authorize_recovery(c, generation_request_id="replay-e4", user_id=world.uid,
                                 quote_id=world.qid, keyword_id=0)
    assert not ok and why == "batch_was_never_charged"
    record_batch_charge(c, generation_request_id="replay-e4", user_id=world.uid, quote_id=world.qid,
                        charge_tx_id=987654321)
    c.execute("SELECT charge_tx_id FROM title_batch_charges WHERE generation_request_id='replay-e4'")
    assert int(c.fetchone()["charge_tx_id"]) == 987654321                        # 回填成功
    record_batch_charge(c, generation_request_id="replay-e4", user_id=world.uid, quote_id=world.qid,
                        charge_tx_id=111)
    c.execute("SELECT charge_tx_id FROM title_batch_charges WHERE generation_request_id='replay-e4'")
    assert int(c.fetchone()["charge_tx_id"]) == 987654321                        # 不覆盖第一笔
    world.conn.rollback()


# ─────────────────── 第五笔:进程被掐后遗弃的抢占行(Review 09-28) ───────────────────

def _claim_row(world, rid, charge_tx_id=None, minutes_old=31):
    c = world.conn.cursor()
    c.execute("INSERT INTO title_batch_charges (generation_request_id, user_id, quote_id, charge_tx_id, created_at) "
              "VALUES (%s, %s, %s, %s, LOCALTIMESTAMP - make_interval(mins => %s))",
              (rid, world.uid, world.qid, charge_tx_id, minutes_old))
    world.conn.commit()


def _real_charge(world, points=160):
    """真扣一笔 topic_gen(模拟「扣费后、生成中被掐」留下的那一笔),返回 charge_tx_id。"""
    from middleware.billing import deduct_points
    res = asyncio.run(deduct_points(world.uid, "topic_gen", extra_cost=points - 80))
    return int(res["charge_tx_id"])


def test_e6_abandoned_claim_without_charge_is_taken_over_and_charged_once(world):
    _claim_row(world, "replay-e6", charge_tx_id=None)                         # 抢占后、扣费前被掐
    b0 = world.balance()
    r, = _run(world.call("replay-e6"))
    assert _status(r) == 200, r
    assert b0 - world.balance() == 160 and world.drafts() == 2


def test_e7_abandoned_paid_claim_is_never_taken_over_but_every_keyword_can_be_recovered_free(world):
    """[WO_317 第六笔] 已付未退的行永不接管(409、不扣);扣费后、生成中被掐的批次走 WO_241 丙逐词免费补救。"""
    from services.title_batch_charge_registry import authorize_recovery
    ctx = _real_charge(world)                                                  # 扣费后、生成中被掐
    _claim_row(world, "replay-e7", charge_tx_id=ctx)
    c = world.conn.cursor()                                                    # 被掐前已落的受理凭据(pending、带原编号)
    c.execute("SELECT id, keyword FROM confirmed_keywords WHERE quote_id=%s ORDER BY id", (world.qid,))
    kws = [(r["id"], r["keyword"]) for r in c.fetchall()]
    for kid, kw in kws:
        c.execute("INSERT INTO topics (quote_id, keyword_id, original_keyword, status, generation_request_id, "
                  "generation_operation) VALUES (%s, %s, %s, 'pending', 'replay-e7', 'title_batch')",
                  (world.qid, kid, kw))
    world.conn.commit()
    b0 = world.balance()
    r, = _run(world.call("replay-e7"))
    assert _status(r) == 409, r
    assert world.balance() == b0, "已付未退的遗弃行被接管"
    for kid, _ in kws:
        ok, why = authorize_recovery(c, generation_request_id="replay-e7", user_id=world.uid,
                                     quote_id=world.qid, keyword_id=kid)
        assert ok, (kid, why)
    world.conn.rollback()


def test_e8_a_live_claim_is_still_409(world):
    _claim_row(world, "replay-e8", charge_tx_id=None, minutes_old=5)          # 未超时 = 可能还在跑
    b0 = world.balance()
    r, = _run(world.call("replay-e8"))
    assert _status(r) == 409 and world.balance() == b0


def test_e9_a_claim_whose_batch_saved_titles_is_never_taken_over(world):
    r1, = _run(world.call("replay-e9"))
    assert _status(r1) == 200
    c = world.conn.cursor()
    c.execute("SELECT count(*) AS n FROM topics WHERE quote_id=%s AND generation_request_id='replay-e9' "
              "AND optimized_title IS NOT NULL", (world.qid,))
    assert int(c.fetchone()["n"]) == 2, "成功存下的选题没盖上请求编号 —— 接管判据会把成功批次当成被遗弃"
    c.execute("UPDATE title_batch_charges SET created_at = LOCALTIMESTAMP - interval '2 hours' "
              "WHERE generation_request_id='replay-e9'")
    world.conn.commit()
    b0 = world.balance()
    r2, = _run(world.call("replay-e9"))
    assert _status(r2) == 409 and world.balance() == b0 and world.drafts() == 2


def test_e9b_saved_titles_block_takeover_even_when_the_charge_id_was_never_backfilled(world):
    """[WO_317 第七笔 · Review 09-28] 扣了费但回填失败(record_batch_charge 出错只记日志)⇒ 抢占行 charge_tx_id 为空。
    这时「已付未退永不接管」那道门认不出它是付过的;唯一挡住同编号二次扣费的是「这个编号下已存下带题选题」。"""
    r1, = _run(world.call("replay-e9b"))
    assert _status(r1) == 200
    c = world.conn.cursor()
    c.execute("UPDATE title_batch_charges SET charge_tx_id = NULL, created_at = LOCALTIMESTAMP - interval '2 hours' "
              "WHERE generation_request_id='replay-e9b'")
    assert c.rowcount == 1
    world.conn.commit()
    b0, d0 = world.balance(), world.drafts()
    r2, = _run(world.call("replay-e9b"))
    assert _status(r2) == 409, r2
    assert world.balance() == b0, "回填失败的已付批次被同编号再扣一次"
    assert world.drafts() == d0 == 2


def test_e10_abandoned_claim_whose_charge_was_refunded_is_charged_again(world):
    from middleware.billing import refund_points
    ctx = _real_charge(world)
    asyncio.run(refund_points(world.uid, "topic_gen", charge_tx_id=ctx, reason="判据:退费后进程被掐"))
    _claim_row(world, "replay-e10", charge_tx_id=ctx)
    b0 = world.balance()
    r, = _run(world.call("replay-e10"))
    assert _status(r) == 200, r
    assert b0 - world.balance() == 160, "已退费的遗弃批次被当成已付,白送了一次生成"


def test_e5_the_organization_path_still_has_its_own_409s():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_generate_titles")
    fn = ast.get_source_segment(src, node)
    for code in ("ORG_TITLES_REPLAY_TERMINAL", "ORG_WORK_ALREADY_RUNNING", "TITLE_REQUEST_DUPLICATE"):
        assert '"code": "%s"' % code in fn, code
    assert fn.index("claim_batch(") < fn.index("await deduct_points(")

# ─────────────────── 第六笔:Review 的四条复现臂(C:/AI-Test/WO_317_RV_ARMS_2026-09-28.py,原样收入) ───────────────────
# 第五笔的接管判据查 topics.generation_request_id,正常流程会改写它(开写文章改成文章编号、整表重出删旧草稿),
# 已交付的已付批次 30 分钟后同编号重放会被当成遗弃。第六笔:已付未退的行永不接管 ⇒ 四条都 409、不退不扣。
# copied from server.py article start (UPDATE topics SET status='writing', generation_request_id=...)
START_WRITING_SQL = """
UPDATE topics
SET status='writing', writing_started_at=NOW(),
    generation_request_id=%s,
    generation_operation=COALESCE(generation_operation,'initial'),
    generation_error_code=NULL,generation_error_message=NULL,
    generation_retryable=NULL,generation_failure_phase=NULL,
    generation_refund_status='not_charged',fail_reason=NULL
WHERE quote_id=%s
  AND article_id IS NULL
  AND NULLIF(BTRIM(optimized_title),'') IS NOT NULL
  AND (status IS NULL OR status IN ('draft', 'pending', 'titles_ready', 'failed'))
"""


def _exec(world, sql, args):
    c = world.conn.cursor()
    c.execute(sql, args)
    n = c.rowcount
    world.conn.commit()
    return n


def _age(world, rid):
    return _exec(world, "UPDATE title_batch_charges SET created_at = LOCALTIMESTAMP - interval '2 hours' "
                        "WHERE generation_request_id=%s", (rid,))


def _call_with(server_mod, world, rid, **body):
    req = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": world.uid, "is_admin": False}, organization_identity=None),
        headers={"x-request-id": rid})
    return server_mod.api_generate_titles(server_mod.TitleGenerateRequest(quote_id=world.qid, **body), req)


def _paid_batch(world, rid):
    b0 = world.balance()
    r, = _run(world.call(rid))
    assert _status(r) == 200, r
    b1 = world.balance()
    assert b0 - b1 == 160
    return b1


def test_rv_c2_control_untouched_batch_replayed_with_bad_style_is_409_no_refund(server_mod, world):
    b1 = _paid_batch(world, "rvarm-c2-00001")
    assert _age(world, "rvarm-c2-00001") == 1
    r, = _run(_call_with(server_mod, world, "rvarm-c2-00001", user_style="__not_a_style__"))
    assert _status(r) == 409, r
    assert world.balance() == b1


def test_rv_r1_titles_sent_to_writing_then_replay_with_bad_style_refunds_the_delivered_batch(server_mod, world):
    b1 = _paid_batch(world, "rvarm-r1-00001")
    assert _exec(world, START_WRITING_SQL, ("writing-articles-rv-r1", world.qid)) == 2
    assert _age(world, "rvarm-r1-00001") == 1
    r, = _run(_call_with(server_mod, world, "rvarm-r1-00001", user_style="__not_a_style__"))
    assert world.balance() == b1, "delivered batch refunded: balance %d -> %d (status %s)" % (
        b1, world.balance(), getattr(r, "status_code", r))
    assert _status(r) == 409, r


def test_rv_r2_titles_sent_to_writing_then_plain_replay_regenerates_for_free(server_mod, world):
    b1 = _paid_batch(world, "rvarm-r2-00001")
    assert _exec(world, START_WRITING_SQL, ("writing-articles-rv-r2", world.qid)) == 2
    assert _age(world, "rvarm-r2-00001") == 1
    r, = _run(world.call("rvarm-r2-00001"))
    assert _status(r) == 409, "replay of a delivered batch was taken over (status %s, drafts now %d)" % (
        _status(r), world.drafts())
    assert world.balance() == b1


def test_rv_r3_regenerated_under_a_new_request_then_old_request_replayed_with_bad_style(server_mod, world):
    _paid_batch(world, "rvarm-r3x-00001")
    b2 = _paid_batch(world, "rvarm-r3y-00001")
    assert _age(world, "rvarm-r3x-00001") == 1
    r, = _run(_call_with(server_mod, world, "rvarm-r3x-00001", user_style="__not_a_style__"))
    assert world.balance() == b2, "first batch refunded after being replaced: balance %d -> %d" % (
        b2, world.balance())
    assert _status(r) == 409, r
