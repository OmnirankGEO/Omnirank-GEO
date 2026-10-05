"""[Deploy-CTO NO-GO v3 finding 2] 文章归属 canonical 反查 · 抗 ../ 穿越 · 判别性 DB 行为测试。

复现 Deploy-CTO 反证:`diagnosis_本人/../diagnosis_他人/secret.md` 旧实现正则识别"本人"、realpath 却读"他人"。
v3 修复:归属按【规范化真实路径 realpath】反查(realpath 严格相等),穿越后落到"他人"的真实归属。

判别性:恢复 caller 原始字符串正则(按 diagnosis_ 数字提取)→ test_traversal_resolves_to_real_target
会返回"本人"而非"他人",断言失败。
需 throwaway PG(article_generations + diagnosis_records)。
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

D_SELF = 772001    # "本人" diagnosis(attacker 拥有)
D_OTHER = 772002   # "他人" diagnosis(victim 拥有)
ARTICLES_ROOT = os.path.realpath(os.path.join(str(ROOT), "output", "articles"))
OTHER_REAL = os.path.join(ARTICLES_ROOT, f"diagnosis_{D_OTHER}", "nogo_secret.md")           # 他人真实文件
TRAVERSAL = os.path.join(ARTICLES_ROOT, f"diagnosis_{D_SELF}", "..", f"diagnosis_{D_OTHER}", "nogo_secret.md")  # 穿越串


def _conn():
    c = psycopg2.connect(DB)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(autouse=True)
def _seed():
    with _conn() as c:
        cur = c.cursor()
        for d in (D_SELF, D_OTHER):
            cur.execute("DELETE FROM article_generations WHERE diagnosis_id=%s", (d,))
        # FK 依赖:先建 diagnosis_records(D_OTHER)(brand owner=victim),再登记文章
        cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s) "
                    "ON CONFLICT (id) DO UPDATE SET owner_user_id=EXCLUDED.owner_user_id", (772102, "victim", 772902))
        cur.execute("INSERT INTO diagnosis_records (id, session_id, brand_id, brand_name, industry) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (id) DO UPDATE SET brand_id=EXCLUDED.brand_id",
                    (D_OTHER, f"s{D_OTHER}", 772102, "victim", "ind"))
        # 只登记【他人】的文章(其真实规范化路径)
        cur.execute("INSERT INTO article_generations (diagnosis_id, file_path, title, status) VALUES (%s,%s,%s,'success')",
                    (D_OTHER, OTHER_REAL, "victim secret"))
        c.commit()
    yield
    with _conn() as c:
        cur = c.cursor()
        for d in (D_SELF, D_OTHER):
            cur.execute("DELETE FROM article_generations WHERE diagnosis_id=%s", (d,))
        c.commit()


def test_traversal_resolves_to_real_target_not_first_segment():
    """核心:穿越串 realpath 落到【他人 D_OTHER】的文件 → 归属必须判为 D_OTHER,不是字面靠前的 D_SELF。"""
    from db.diagnosis_db import get_diagnosis_id_by_article_path
    # 前置:确认本平台 realpath 会折叠 ..(否则用例前提不成立)
    assert os.path.realpath(TRAVERSAL) == os.path.realpath(OTHER_REAL), "realpath 应把穿越串折叠到他人真实路径"
    diag = get_diagnosis_id_by_article_path(TRAVERSAL)
    assert diag == D_OTHER, f"🔴 穿越串归属必须是真实目标 D_OTHER={D_OTHER},实际 {diag}"
    assert diag != D_SELF, "🔴 绝不能被字面靠前的 diagnosis_本人 骗过(旧正则 bug)"


def test_unregistered_real_target_is_failclosed():
    """穿越到【未登记】的真实文件(无 article_generations 行)→ None(fail-closed 拒绝)。"""
    from db.diagnosis_db import get_diagnosis_id_by_article_path
    bogus = os.path.join(ARTICLES_ROOT, f"diagnosis_{D_SELF}", "..", "diagnosis_999999", "ghost.md")
    assert get_diagnosis_id_by_article_path(bogus) is None


def test_attacker_denied_on_resolved_owner():
    """归属链末端:穿越串解析到他人 → require_diagnosis_access(attacker) 必 403。"""
    from db.diagnosis_db import get_diagnosis_id_by_article_path
    from auth.brand_access import require_diagnosis_access
    from fastapi import HTTPException
    diag = get_diagnosis_id_by_article_path(TRAVERSAL)
    assert diag == D_OTHER
    # 构造他人 diagnosis 归属(brand owner = victim);attacker 非 owner → 403
    with _conn() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s) "
                    "ON CONFLICT (id) DO UPDATE SET owner_user_id=EXCLUDED.owner_user_id", (772102, "victim", 772902))
        cur.execute("INSERT INTO diagnosis_records (id, session_id, brand_id, brand_name, industry) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (id) DO UPDATE SET brand_id=EXCLUDED.brand_id",
                    (D_OTHER, f"s{D_OTHER}", 772102, "victim", "ind"))
        c.commit()
    attacker_req = SimpleNamespace(state=SimpleNamespace(user={"is_admin": False, "user_id": 772901, "client_brand_ids": []}))
    with pytest.raises(HTTPException) as ei:
        require_diagnosis_access(attacker_req, diag, allow_null=False)
    assert ei.value.status_code == 403
