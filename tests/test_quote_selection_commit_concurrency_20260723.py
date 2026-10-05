"""PostgreSQL discrimination tests for customer selection commit semantics."""

import asyncio
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
import pytest
from fastapi import HTTPException

from api import selection_api


TEST_DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@pytest.fixture
def selection_store(monkeypatch):
    schema = f"quote_selection_{uuid.uuid4().hex}"
    with psycopg2.connect(TEST_DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cursor.execute(
                sql.SQL(
                    """
                    CREATE TABLE {}.keyword_selection_sessions (
                        id BIGSERIAL PRIMARY KEY,
                        token TEXT UNIQUE NOT NULL,
                        quote_id BIGINT NOT NULL,
                        brand_id BIGINT NOT NULL,
                        status TEXT NOT NULL,
                        expires_at TEXT,
                        business_lines TEXT,
                        keywords_snapshot TEXT NOT NULL,
                        selected_keyword_ids TEXT,
                        custom_keywords TEXT,
                        keywords_submitted_at TEXT,
                        updated_at TEXT
                    )
                    """
                ).format(sql.Identifier(schema))
            )

    def connect():
        connection = psycopg2.connect(
            TEST_DATABASE_URL,
            cursor_factory=RealDictCursor,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
            )
        connection.commit()
        return connection

    notices: list[dict] = []
    notice_lock = threading.Lock()

    def record_notice(**kwargs):
        with notice_lock:
            notices.append(kwargs)

    monkeypatch.setattr(selection_api, "get_connection", connect)
    monkeypatch.setattr(selection_api, "_durable_selection_notice", record_notice)

    def insert(token: str):
        business_lines = [
            {"id": line_id, "name": f"业务方向{line_id}", "is_selected": False}
            for line_id in range(1, 7)
        ]
        # [Review-CTO 2026-07-23 P1] 唯一引擎按文本判定:每 3 个用知识题
        # (会被排除),其余用真实商业题(保留);验证提交分区在并发下的
        # 分类 + 幂等,而非占位词。
        keywords = [
            {
                "id": keyword_id,
                "keyword": (
                    "GEO和SEO有什么区别"
                    if keyword_id % 3 == 0
                    else f"深圳{keyword_id}区装修公司哪家好推荐几家"
                ),
                "business_line_id": ((keyword_id - 1) % 6) + 1,
                "intent": "informational" if keyword_id % 3 == 0 else "commercial",
            }
            for keyword_id in range(1, 21)
        ]
        with connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO keyword_selection_sessions(
                        token, quote_id, brand_id, status, expires_at,
                        business_lines, keywords_snapshot, updated_at
                    ) VALUES (%s, %s, %s, 'selecting', %s, %s, %s, %s)
                    """,
                    (
                        token,
                        1001,
                        2001,
                        (datetime.now() + timedelta(days=1)).isoformat(),
                        json.dumps(business_lines, ensure_ascii=False),
                        json.dumps(keywords, ensure_ascii=False),
                        datetime.now().isoformat(),
                    ),
                )

    def read(token: str) -> dict:
        with connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM keyword_selection_sessions WHERE token=%s",
                    (token,),
                )
                return dict(cursor.fetchone())

    try:
        yield insert, read, notices
    finally:
        with psycopg2.connect(TEST_DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )


def _submit_business_lines(token: str, line_ids: list[int]):
    try:
        return asyncio.run(
            selection_api.submit_business_lines(
                token,
                selection_api.SubmitBusinessLinesRequest(
                    selected_business_line_ids=line_ids
                ),
            )
        )
    except HTTPException as error:
        return error


def _submit_keywords(token: str, keyword_ids: list[int]):
    try:
        return asyncio.run(
            selection_api.submit_keywords(
                token,
                selection_api.SubmitKeywordsRequest(
                    selected_ids=keyword_ids,
                    custom_keywords=[],
                ),
            )
        )
    except HTTPException as error:
        return error


def test_same_business_line_submission_is_single_commit_and_idempotent(selection_store):
    insert, read, notices = selection_store
    token = "same-selection"
    insert(token)

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(
            executor.map(
                lambda _: _submit_business_lines(token, [1, 2, 3, 4, 5, 6]),
                range(20),
            )
        )

    assert all(isinstance(result, dict) and result["success"] for result in results)
    assert sum(result["replayed"] is False for result in results) == 1
    assert sum(result["replayed"] is True for result in results) == 19
    row = read(token)
    # [SSOT geo-commercial-intent-governance-v1.0 §3.2/§3.3 · 2026-07-23]
    # fixture 里每 3 个词有 1 个 intent=informational(知识词):
    #   - 合格商业词全部保留(14/14 · 不静默缩减);
    #   - 知识词不进付费交付,且**逐条**带原因返回(不是静默消失)。
    commercial_ids = [kid for kid in range(1, 21) if kid % 3 != 0]
    knowledge_ids = [kid for kid in range(1, 21) if kid % 3 == 0]
    assert json.loads(row["selected_keyword_ids"]) == commercial_ids
    for result in results:
        excluded = result["delivery_excluded_keywords"]
        assert sorted(item["id"] for item in excluded) == knowledge_ids
        assert all(item["reason"] for item in excluded)
        assert all(item["policy_version"] for item in excluded)
    assert len(notices) == 1


def test_late_different_selection_cannot_overwrite_committed_scope(selection_store):
    insert, read, notices = selection_store
    token = "conflicting-selection"
    insert(token)

    requests = [[1, 2, 3], [4, 5, 6]] * 10
    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(
            executor.map(lambda ids: _submit_business_lines(token, ids), requests)
        )

    successes = [result for result in results if isinstance(result, dict)]
    conflicts = [result for result in results if isinstance(result, HTTPException)]
    assert successes
    assert conflicts
    assert all(error.status_code == 409 for error in conflicts)
    assert all(
        error.detail["code"] == "BUSINESS_LINE_SELECTION_ALREADY_COMMITTED"
        for error in conflicts
    )
    row = read(token)
    stored_lines = selection_api._selected_business_line_ids(
        json.loads(row["business_lines"])
    )
    assert stored_lines in ([1, 2, 3], [4, 5, 6])
    assert len(notices) == 1


def test_unknown_or_duplicate_ids_fail_before_write(selection_store):
    insert, read, notices = selection_store
    insert("unknown-lines")
    result = _submit_business_lines("unknown-lines", [1, 999])
    assert isinstance(result, HTTPException)
    assert result.status_code == 409
    assert result.detail["code"] == "BUSINESS_LINE_SELECTION_IDS_STALE"
    assert read("unknown-lines")["status"] == "selecting"

    insert("duplicate-lines")
    result = _submit_business_lines("duplicate-lines", [1, 1])
    assert isinstance(result, HTTPException)
    assert result.status_code == 409
    assert result.detail["code"] == "BUSINESS_LINE_SELECTION_IDS_INVALID"
    assert read("duplicate-lines")["status"] == "selecting"
    assert notices == []


def test_direct_keyword_submit_rejects_unknown_and_duplicate_ids(selection_store):
    insert, read, notices = selection_store
    insert("unknown-keywords")
    result = _submit_keywords("unknown-keywords", [1, 999])
    assert isinstance(result, HTTPException)
    assert result.status_code == 409
    assert result.detail["code"] == "KEYWORD_SELECTION_IDS_STALE"
    assert read("unknown-keywords")["status"] == "selecting"

    insert("duplicate-keywords")
    result = _submit_keywords("duplicate-keywords", [1, 1])
    assert isinstance(result, HTTPException)
    assert result.status_code == 409
    assert result.detail["code"] == "KEYWORD_SELECTION_IDS_INVALID"
    assert read("duplicate-keywords")["status"] == "selecting"
    assert notices == []


def test_frontend_uses_synchronous_submit_fence():
    source = (
        Path(selection_api.__file__).parents[1]
        / "frontend"
        / "src"
        / "pages"
        / "Selection"
        / "SelectionPage.tsx"
    )
    contents = source.read_text(encoding="utf-8")
    assert "const submitInFlightRef = useRef(false)" in contents
    assert contents.count("submitInFlightRef.current = true") >= 2
    assert contents.count("submitInFlightRef.current = false") >= 2
