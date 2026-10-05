"""#185 c2 · 防御型(公司词)方向 —— 判据包夹具。

库私有(`geo_c14_185_test`),从**生产 schema** 建表:
手写夹具会漏掉 CHECK / UNIQUE / NOT NULL,于是判据能证出生产上不可能的事。
"""
import io
import os

_DSN = "postgresql://geo_admin:testpw@localhost:55492/geo_c14_185_test"


def pytest_configure(config):
    os.environ.setdefault("TEST_DATABASE_URL", _DSN)
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]


import subprocess
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

PROD_SCHEMA = Path("C:/AI-Test/.deploy_toolkit/prod_schema_2026-09-05.sql")
PG_CONTAINER = "defgeo-c14-62-pg"
DB_NAME = "geo_c14_185_test"
ADMIN_DSN = "postgresql://geo_admin:testpw@localhost:55492/postgres"


def _admin(sql, args=None):
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    try:
        conn.cursor().execute(sql, args)
    finally:
        conn.close()


def conn():
    c = psycopg2.connect(_DSN)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    c.autocommit = True
    return c


def _db_exists():
    c = psycopg2.connect(ADMIN_DSN)
    c.autocommit = True
    try:
        cur = c.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,))
        return cur.fetchone() is not None
    finally:
        c.close()


def _build():
    _admin('CREATE DATABASE "%s"' % DB_NAME)
    sql = io.open(PROD_SCHEMA, encoding="utf-8").read()
    sql = "\n".join(l for l in sql.splitlines() if not l.startswith(chr(92)))
    proc = subprocess.run(
        ["docker", "exec", "-i", PG_CONTAINER, "psql", "-v", "ON_ERROR_STOP=0",
         "-U", "geo_admin", "-d", DB_NAME],
        input=sql.encode("utf-8"), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-1500:])


def _shape_ok():
    """库**在**不等于库是我要的那个世界。

    上一次建库若炸在恢复 schema 之前,库壳会留下、`_db_exists()` 照样为真,
    判据就红在 UndefinedTable —— 那看起来像被测对象坏了。
    """
    try:
        c = psycopg2.connect(_DSN)
    except Exception:
        return False
    try:
        cur = c.cursor()
        cur.execute("SELECT to_regclass('public.topics') AS t,"
                    "       to_regclass('public.client_profiles') AS p")
        row = cur.fetchone()
        return all(row[i] is not None for i in range(2)) if not isinstance(row, dict) \
            else all(row[k] is not None for k in ("t", "p"))
    finally:
        c.close()


#: [#185 c3] 本包新建的表 —— 生产 schema 快照(2026-09-05)里当然没有它。
#: 🔴 不补这一步的话:表不存在 → playbook 每次读写都失败 → fail-soft 一路绿,
#:    而「缓存命中不打 LLM」「sources_used 对得上」这两条判据**永远验不到**
#:    真实路径(本仓 `criterion-silently-ran-the-degraded-path`)。
_MIGRATIONS = (Path(__file__).resolve().parents[2]
               / "db" / "migration_060_defensive_playbook_2026_09_14.sql",)


#: [#185 c3'] **参照行**,夹具必须自己建出来。
#:
#: 🔴 `confirmed_keywords.monitoring_product_version` 默认 `'monitoring-unified5-v1'`
#:    且外键指向 `monitoring_product_platform_matrices(version)`。而
#:    `prod_schema_2026-09-05.sql` 是**纯 schema 快照**(有表有外键、**没有数据**),
#:    于是全新库里这张表是空的 ⇒ `_seed` 每一次插 `confirmed_keywords` 都违反外键。
#:    我的私库碰巧有这两行(早前跑过监测迁移),所以"在我机器上全绿" ——
#:    这正是「验了,但验对的是另一个对象」。Review 在他的新库上实测 27 红。
#:
#: 🔴 值不是我编的:逐字取自两支监测迁移(见
#:    `test_the_fixture_reference_rows_still_match_the_migrations`),
#:    迁移里的值改了那条判据就红,夹具不会静默漂走。
#:    不直接跑那两支迁移是因为 07-21 那支含大量 DML(ALTER/UPDATE/触发器),
#:    而 07-26 那支还要求 classic4 行先存在 —— 把它们拉进判据路径,
#:    代价和风险都比建两行参照数据大得多。
_PLATFORM_MATRIX_ROWS = (
    ("monitoring-classic4-v1", "dashscope,deepseek,kimi,doubao"),
    ("monitoring-unified5-v1", "dashscope,deepseek,doubao,kimi,yuanbao"),
)
_PLATFORM_MATRIX_MIGRATIONS = (
    "scripts/migration_monitoring_product_matrix_2026_07_21.sql",
    "scripts/migration_monitoring_unified5_2026_07_26.sql",
)


def _seed_reference_rows():
    c = psycopg2.connect(_DSN)
    c.autocommit = True
    try:
        cur = c.cursor()
        for version, platforms in _PLATFORM_MATRIX_ROWS:
            cur.execute(
                "INSERT INTO monitoring_product_platform_matrices (version, platforms)"
                " VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                (version, platforms))
    finally:
        c.close()


def _apply_migrations():
    for path in _MIGRATIONS:
        sql = io.open(path, encoding="utf-8").read()
        proc = subprocess.run(
            ["docker", "exec", "-i", PG_CONTAINER, "psql", "-v", "ON_ERROR_STOP=1",
             "-U", "geo_admin", "-d", DB_NAME],
            input=sql.encode("utf-8"), capture_output=True)
        if proc.returncode != 0:
            # 🔴 这一条**不吞**:迁移没跑成而判据照跑,读到的全是降级路径。
            raise RuntimeError("迁移 %s 失败:\n%s"
                               % (path.name, proc.stderr.decode("utf-8", "replace")[-1500:]))


def pytest_sessionstart(session):
    if _db_exists() and not _shape_ok():
        _admin("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",
               (DB_NAME,))
        _admin('DROP DATABASE IF EXISTS "%s"' % DB_NAME)
    if not _db_exists():
        _build()
    # 每次 session 都跑:迁移本身就是幂等的(生产走 prestart、每次启动重跑),
    # 顺便让「它幂等吗」这件事在每一轮判据里都被真跑一遍。
    _apply_migrations()
    _seed_reference_rows()


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """判据**一律不许真出网**;要用 LLM 的请求 `llm_stub`。

    🔴 2026-09-15 一次挂死逼出来的:`test_defensive_endpoints.py` 单独跑挂了
       34 分钟,5 条 DB 连接空转。原因是 c3 之后防御题要过 LLM、还要蒸 playbook,
       而这个文件里几条判据没请求 `llm_stub` ⇒ `httpx.post` **真的出网**。
       慢和不稳只是表面;真正的问题是它们验的是 **fail-soft 降级路径**,
       而判据的名字说的是正常路径 —— 绿得毫无意义。
    🔴 出网就抛,不静默放过:静默的话下一个人只看到"这个包有时候很慢",
       而看不到"这几条判据从来没走过被测的那条路"。
    🔴 `llm_stub` 在测试体内再打自己的桩,会盖掉这一层 —— 那是**声明式**的:
       谁要用 LLM,谁就得写出来。
    """
    import httpx

    attempts = []

    def _blocked(*a, **kw):
        attempts.append(str(kw.get("url") or (a[0] if a else "?"))[:80])
        raise AssertionError("判据未打桩就试图真出网")

    monkeypatch.setattr(httpx, "post", _blocked)
    monkeypatch.setattr(httpx.AsyncClient, "post", _blocked)
    yield
    # 🔴 断言放在 **teardown**,不是靠抛异常。
    #    抛出去会被被测代码自己的 fail-soft 吞掉
    #    (`get_or_build_playbook` 是 `except Exception: return None, ...`),
    #    于是判据照样绿、跑得还很快 —— 比挂死更难发现:
    #    挂死至少还看得见,静默降级看不见。
    assert not attempts, (
        "这条判据没请求 `llm_stub` 就试图出网 %d 次(%s)。"
        "被测代码的 fail-soft 会把异常吞掉,于是它验的是**降级路径**,"
        "却绿得像验了正常路径 —— 要用 LLM 就把 `llm_stub` 写进参数里。"
        % (len(attempts), ", ".join(sorted(set(attempts))[:3])))


@pytest.fixture(autouse=True)
def clean_rows():
    """每条判据自己的世界。

    🔴 清理**不吞异常**:吞掉的话「我以为清干净了」会让下一条判据读到
       上一条的残留,而那种串味只表现为"偶尔红一条",最难查。
    """
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("DELETE FROM llm_call_log WHERE caller = 'defensive_playbook_distill'")
        cur.execute("DELETE FROM geo_article_citation_attributions WHERE brand_id >= 900000")
        cur.execute("DELETE FROM geo_defensive_playbooks WHERE brand_id >= 900000")
        cur.execute("DELETE FROM topics WHERE quote_id >= 900000")
        cur.execute("DELETE FROM confirmed_keywords WHERE quote_id >= 900000")
        cur.execute("DELETE FROM client_profiles WHERE brand_id >= 900000")
        cur.execute("DELETE FROM quotes WHERE id >= 900000")
        cur.execute("DELETE FROM brands WHERE id >= 900000")
    finally:
        c.close()
    yield


# ════════════════════════════════════════════════════════════════
# [#185 c3] LLM 桩
#
# c2 时防御题**不进 LLM**,所以那几条判据不需要桩。c3 之后它们进了 ——
# 没有桩的话端点会以 502「LLM 未产出有效标题」收场,而那种红读起来像
# "被测对象坏了",实际是**判据没跟上路径**。
#
# 🔴 桩要**记下它被怎么调的**:T1 要断言 prompt 里有品牌名/问名/角度,
#    T5 要断言"第二次没打 LLM"。只返回结果的桩证不了这两件事。
# ════════════════════════════════════════════════════════════════
class LLMCalls:
    """一次 stub 的账本:调了几次、每次**真正**发出去的 prompt 是什么。"""

    def __init__(self):
        self.count = 0
        self.prompts = []
        self.generators = []          # 被造出来的生成器实例(用来读它算出的 plan)
        # 🔴 蒸馏(playbook)走的是**另一条出网路**:`httpx.post` 同步调用,
        #    不是标题那条 `httpx.AsyncClient.post`。两条分开记 ——
        #    合成一个计数的话,T5「第二次没打 LLM」就分不出
        #    "没蒸"与"没出题",而那是两件事。
        self.distills = []            # 每次蒸馏发出去的 prompt

    @property
    def plans(self):
        return [list(g.defensive_plan or []) for g in self.generators]

    @property
    def distill_count(self):
        return len(self.distills)


@pytest.fixture
def llm_stub(monkeypatch):
    """桩住**出网那一步**,让真实的 `_generate_batch` 跑完并留下真 prompt。

    🔴 不桩 `generate()`:桩在那一层拿不到生成器**真正构造**的 user_message,
       判据只能"自己复现一遍同样的拼接再断言" —— 那是自洽,不是证据
       (本仓 `self-consistency-is-not-evidence-go-measure`)。
       桩在 httpx 这一层,`prompts[i]` 就是它**准备发出去的**那一份。

    🔴 `__init__` 只做**记录**(先调原始实现),不改任何参数 ——
       它不参与 prompt 的构造,所以拿到的 plan 与发出去的 prompt 同源。

    用法::

        calls = llm_stub(lambda d: BRAND + d["question"])   # 每槽怎么出题
        calls = llm_stub(drop_slots=[("词", 0)])            # 某槽 LLM 不产出
        calls = llm_stub(angles={"怎么样": ["角度A"]})       # 蒸馏产出的角度
        calls = llm_stub(distill_fails=True)                # 蒸馏那一路打挂
    """
    import json as _json

    import httpx

    from writing.defensive_questions import DEFENSIVE_QUESTIONS
    from writing.keyword_topic_generator import KeywordTopicGenerator

    def _install(title_fn=None, drop_slots=(), brand="", angles=None,
                 distill_fails=False):
        calls = LLMCalls()
        _orig_init = KeywordTopicGenerator.__init__

        def _recording_init(self, *a, **kw):
            _orig_init(self, *a, **kw)
            calls.generators.append(self)

        class _Resp:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        async def _fake_post(_self, url, headers=None, json=None, **kw):
            body = json or {}
            calls.count += 1
            calls.prompts.append(
                "".join(str(m.get("content") or "") for m in (body.get("messages") or [])))

            gen = calls.generators[-1] if calls.generators else None
            plan = list(getattr(gen, "defensive_plan", None) or [])
            drop = {(str(k), int(i)) for k, i in (drop_slots or ())}
            brand_name = brand or getattr(gen, "brand_name", "")

            topics = []
            plan_slots = {(d["keyword"], d["slot_index"]) for d in plan}
            for d in plan:
                if (d["keyword"], int(d["slot_index"])) in drop:
                    continue
                title = title_fn(d) if title_fn else "%s%s" % (brand_name, d["question"])
                topics.append({"original_keyword": d["keyword"],
                               "slot_index": d["slot_index"],
                               "optimized_title": title,
                               "article_style": "品牌软文"})
            # 非防御槽照常出普通题(否则"混批"那几条判据会以为普通篇也没产出)
            for kw in (getattr(gen, "keywords", None) or []):
                for slot in range(int(kw.get("required_articles") or 0)):
                    if (kw.get("keyword"), slot) in plan_slots:
                        continue
                    topics.append({"original_keyword": kw.get("keyword"),
                                   "slot_index": slot,
                                   "optimized_title": "普通题 %s slot%d" % (kw.get("keyword"), slot),
                                   "article_style": "品牌软文"})

            content = _json.dumps({"topics": topics}, ensure_ascii=False)
            return _Resp({"choices": [{"message": {"content": content}}],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

        # ── 蒸馏(playbook)那一路 ───────────────────────────────────
        # 🔴 不桩它的话 `_distill_with_llm` 会**真的出网**,90s 超时之后
        #    `get_or_build_playbook` fail-soft 返回 `distill_failed` ——
        #    于是每条判据都悄悄走了降级路径、还都是绿的:
        #    「角度进了提示块」「缓存第二次不打 LLM」这两件事根本没被验过。
        #    (本仓 `criterion-silently-ran-the-degraded-path`。)
        def _fake_sync_post(url, headers=None, json=None, **kw):
            body = json or {}
            calls.distills.append(
                "".join(str(m.get("content") or "") for m in (body.get("messages") or [])))
            if distill_fails:
                raise RuntimeError("蒸馏出网失败(判据故意的)")
            payload = {
                "title_angles": (angles if angles is not None
                                 else {q: ["角度·%s" % q] for q in DEFENSIVE_QUESTIONS}),
                "body_points": ["正文要点1", "正文要点2"],
                "evidence_rules": ["有素材才写"],
                "forbidden": ["不点名同行"],
            }
            # 🔴 带上 `usage` 与 `model`:蒸馏这一路要进 `llm_call_log`,
            #    没有 usage 的假回包会让「记了一行」与「记了一行但全是 0」
            #    分不出来 —— 而后者正是成本表上那条线消失的方式。
            return _Resp({"choices": [{"message": {
                "content": _json.dumps(payload, ensure_ascii=False)}}],
                "model": "deepseek-v4-flash",
                "usage": {"prompt_tokens": 1234, "completion_tokens": 567}})

        monkeypatch.setattr(KeywordTopicGenerator, "__init__", _recording_init)
        monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
        monkeypatch.setattr(httpx, "post", _fake_sync_post)
        return calls

    return _install
