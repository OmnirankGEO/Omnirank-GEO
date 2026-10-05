"""Shared seeding + fake-verifier helpers for geo-observation tests."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from services.brand_identity_resolver import BrandVerdict, VerificationResult


def now():
    return datetime.now(timezone.utc)


def seed_brand(conn, brand_id, name, owner_user_id, industry="装修", aliases=()):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO brands(id,name,company_name,brand_display_names,industry,industry_category,owner_user_id,status)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,'active')",
        (brand_id, name, name, None, industry, industry, owner_user_id),
    )
    for alias in aliases:
        cur.execute(
            "INSERT INTO brand_aliases(brand_id,canonical_name,alias,source) VALUES (%s,%s,%s,'manual')",
            (brand_id, name, alias),
        )
    conn.commit()


def diag_raw_json(question, engine, answer, detected=True, mentioned=None, section="primary"):
    """真实生产形状:data.ai_visibility.(detail_table|custom_visibility.detail_table)[].results{engine:{answer_summary,brand_detected,mentioned_brands}}"""
    item = {"question": question, "results": {engine: {
        "answer_summary": answer, "brand_detected": detected, "mentioned_brands": mentioned or []}}}
    av = {"engines_tested": [engine]}
    if section == "custom":
        av["custom_visibility"] = {"questions": [question], "detail_table": [item]}
    else:
        av["detail_table"] = [item]
    return json.dumps({"data": {"ai_visibility": av}}, ensure_ascii=False)


def seed_diagnosis(conn, run_token, owner, brand, run_status="committed", visibility="published",
                   total_score=72, raw_json=None, refund=False, question="深圳装修哪家好", engine="deepseek",
                   answer="推荐大昀装修,口碑好。", session_id=None):
    cur = conn.cursor()
    session_id = session_id or f"sess_{run_token}"
    cur.execute(
        "INSERT INTO diagnosis_runs(run_token,session_id,owner_user_id,brand_id,billing_mode,run_status,"
        "status_changed_at,finished_at) VALUES (%s,%s,%s,%s,'paid',%s,NOW(),NOW())",
        (run_token, session_id, owner, brand, run_status),
    )
    if raw_json is None:
        raw_json = diag_raw_json(question, engine, answer)
    cur.execute(
        "INSERT INTO diagnosis_records(session_id,brand_id,run_token,result_visibility,total_score,raw_data_json)"
        " VALUES (%s,%s,%s,%s,%s,%s)",
        (session_id, brand, run_token, visibility, total_score, raw_json),
    )
    if refund:
        cur.execute(
            "INSERT INTO diagnosis_refund_records(run_token,freeze_task_ref,freeze_id,freeze_backend,owner_user_id,"
            "points,refund_tx_ref,operator) VALUES (%s,%s,1,'legacy',%s,100,'txrefund1','ops')",
            (run_token, f"diag_{run_token}", owner),
        )
    conn.commit()


def seed_monitoring(conn, result_id_out, brand, owner, keyword="深圳装修", monitoring_query="深圳装修公司推荐",
                    platform="deepseek", answer="大昀装修值得推荐,口碑不错。", status="completed",
                    total_tests=4, completed_tests=4, quote_id="Q1", is_detected=1):
    cur = conn.cursor()
    if brand is not None:
        cur.execute("SELECT 1 FROM brands WHERE id=%s", (brand,))
        if cur.fetchone() is None:
            seed_brand(conn, brand, "大昀装修", owner)
    cur.execute(
        "INSERT INTO monitoring_tasks(quote_id,client_id,brand_id,status,total_tests,completed_tests,completed_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,NOW()) RETURNING id",
        (None, quote_id, brand, status, total_tests, completed_tests),
    )
    task_id = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO confirmed_keywords(quote_id,keyword,monitoring_query) VALUES (%s,%s,%s)",
        (quote_id, keyword, monitoring_query),
    )
    cur.execute(
        "INSERT INTO monitoring_results(task_id,keyword,platform,is_detected,full_response,tested_at)"
        " VALUES (%s,%s,%s,%s,%s,NOW()) RETURNING id",
        (task_id, keyword, platform, is_detected, answer),
    )
    rid = cur.fetchone()["id"]
    conn.commit()
    return rid


def seed_research(conn, raw_id_hint, industry="装修", query="深圳装修哪家好", engine="deepseek",
                  answer="推荐大昀装修。", round_status="completed", round_id="round_test_1"):
    cur = conn.cursor()
    batch_id = f"batch_{round_id}"
    cur.execute(
        "INSERT INTO geo_research_round(round_id,batch_id,status,finished_at) "
        "VALUES (%s,%s,%s,CASE WHEN %s='completed' THEN NOW() ELSE NULL END) "
        "ON CONFLICT (round_id) DO UPDATE SET status=EXCLUDED.status, "
        "batch_id=EXCLUDED.batch_id, finished_at=EXCLUDED.finished_at",
        (round_id, batch_id, round_status, round_status),
    )
    cur.execute(
        "INSERT INTO geo_research_raw(industry,query,engine,answer_text,is_answer_cited,adoption_rank,batch_id)"
        " VALUES (%s,%s,%s,%s,TRUE,1,%s) RETURNING id",
        (industry, query, engine, answer, batch_id),
    )
    rid = cur.fetchone()["id"]
    conn.commit()
    return rid


def const_verifier(verdict: BrandVerdict, *, matched_text=None, window_index=None, start=None, end=None, reason=""):
    """注入 resolver 的确定性 fake verifier(测试用)。"""
    async def _v(*, identity, evidence_windows):
        return VerificationResult(verdict, reason, matched_text=matched_text,
                                  window_index=window_index, matched_start=start, matched_end=end)
    return _v


def const_outcome(level="recommended"):
    """注入 promotion 的确定性 fake outcome classifier(gold-gate 通过路径测试用)。"""
    from services.geo_observation.contracts import TargetOutcome
    async def _clf(question, answer, identity):
        return TargetOutcome(level)
    return _clf


class FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def make_capturing_post(captured: list, verdict="NO", status_code=200, bad_json=False):
    """返回捕获 request body 的官方 post_fn(测试拦截最小化 body / 强类型校验)。"""
    async def _post(body):
        captured.append(body)
        if bad_json:
            content = "not-a-json-object-{{"
        else:
            content = json.dumps({"verdict": verdict, "reason": "t", "matched_text": "",
                                  "window_index": None, "matched_start": None, "matched_end": None})
        return FakeResp({"choices": [{"message": {"content": content}}]}, status_code=status_code)
    return _post
