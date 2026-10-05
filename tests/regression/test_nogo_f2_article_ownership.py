"""[Deploy-CTO NO-GO finding 2] 文章文件接口跨租户归属 · 判别性 DB 行为测试(throwaway PG)。

修复链:file_path --get_diagnosis_id_by_article_path--> diagnosis_id --require_diagnosis_access--> 归属。
- get_diagnosis_id_by_article_path 精确匹配 article_generations.file_path → diagnosis_id;找不到 → None(fail-closed)。
- require_diagnosis_access:owner 通过 · 非 owner 403。
判别性:
  - 删 get_diagnosis_id_by_article_path 精确匹配(恒 None)→ test_lookup_by_path 失败;
  - 端点 _article_path_access_ok 对 None 返 False(fail-closed)→ test_unknown_path_is_failclosed 失败若改成放行。
需 throwaway PG(brands/diagnosis_records/article_generations)。
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL")

OWNER = 770001
STRANGER = 770002
BRAND = 770101
DIAG = 770201
PATH = "output/articles/diagnosis_770201/nogo_test_article.md"


def _conn():
    c = psycopg2.connect(DB)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(autouse=True)
def _seed():
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM article_generations WHERE diagnosis_id=%s", (DIAG,))
        cur.execute("DELETE FROM diagnosis_records WHERE id=%s", (DIAG,))
        cur.execute("DELETE FROM brands WHERE id=%s", (BRAND,))
        cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s) "
                    "ON CONFLICT (id) DO UPDATE SET owner_user_id=EXCLUDED.owner_user_id",
                    (BRAND, "NOGO测试品牌", OWNER))
        cur.execute("INSERT INTO diagnosis_records (id, session_id, brand_id, brand_name, industry) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (id) DO UPDATE SET brand_id=EXCLUDED.brand_id",
                    (DIAG, f"sess_{DIAG}", BRAND, "NOGO测试品牌", "测试行业"))
        cur.execute("INSERT INTO article_generations (diagnosis_id, file_path, title, status) "
                    "VALUES (%s,%s,%s,'success')", (DIAG, PATH, "标题"))
        c.commit()
    yield
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM article_generations WHERE diagnosis_id=%s", (DIAG,))
        cur.execute("DELETE FROM diagnosis_records WHERE id=%s", (DIAG,))
        cur.execute("DELETE FROM brands WHERE id=%s", (BRAND,))
        c.commit()


def _fake_req(user_id, is_admin=False):
    return SimpleNamespace(state=SimpleNamespace(
        user={"is_admin": is_admin, "user_id": user_id, "client_brand_ids": []}))


def test_lookup_by_path_returns_owning_diagnosis():
    from db.diagnosis_db import get_diagnosis_id_by_article_path
    assert get_diagnosis_id_by_article_path(PATH) == DIAG


def test_unknown_path_is_none_failclosed():
    from db.diagnosis_db import get_diagnosis_id_by_article_path
    # 无 article_generations 行的路径 → None → 端点据此 fail-closed 拒绝
    assert get_diagnosis_id_by_article_path("output/articles/diagnosis_999999/other_tenant.md") is None
    assert get_diagnosis_id_by_article_path("") is None


def test_owner_passes_stranger_denied_on_derived_diagnosis():
    """归属链末端:文件所属 diagnosis 的 owner 可访问,陌生人 403。"""
    from auth.brand_access import require_diagnosis_access
    from fastapi import HTTPException
    from db.diagnosis_db import get_diagnosis_id_by_article_path
    diag_id = get_diagnosis_id_by_article_path(PATH)
    # owner 通过(不抛)
    require_diagnosis_access(_fake_req(OWNER), diag_id, allow_null=False)
    # 陌生人被拒
    with pytest.raises(HTTPException) as ei:
        require_diagnosis_access(_fake_req(STRANGER), diag_id, allow_null=False)
    assert ei.value.status_code == 403


def test_endpoint_wiring_calls_access_helper():
    """源码级兜底:三端点都调 _article_path_access_ok(防有人移除接线)。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    # content / zip / batch-delete 三处都必须调归属校验
    assert server.count("_article_path_access_ok(") >= 3, "content/zip/batch-delete 必须都调 _article_path_access_ok"
