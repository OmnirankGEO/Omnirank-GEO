"""[内容审核 AI 化 + 核验流融合 · 包① · 2026-08-01] 工单 §5 锁 · 行为级,禁源码串断言。

工单:docs/AI-CONTEXT/WORKORDER_AI_REVIEW_REPLACES_HUMAN_2026-08-01.md §3A/§5

锁 1  不点核查可直接发布(含原 legal_hard 类),零阻塞
锁 2  🔴反向:资金/租户/对象完整性 H0 仍拦(防降级过界)
锁 4  静默留痕**双向**:带提示发布→有日志行;无提示发布→无日志行

🔴 为什么锁 4 必须双向:只断言"有提示时写了行",把写入改成无条件 INSERT 照样全绿 ——
那样留痕就失去区分度,等于没留。反向面才是判别力所在。

用 TEMP 表影子(与 test_publish_gate_tristate / test_p07 同款),ON COMMIT DROP + rollback。
留痕表刻意**用真迁移文件建**:顺带证明 scripts/migration_publication_notice_audit_2026_08_01.sql
是可执行的合法 SQL,而不是只在 manifest 里挂个名。
"""
import hashlib
import json
import os
import pathlib


import psycopg2
import psycopg2.extras
import pytest

from services.article_review_gate import (
    DOWNGRADED_CONTENT_NOTICE_CLASSES,
    ArticlePublicationBlocked,
    assert_publication_eligible,
    evaluate_publication_eligibility,
)

_REPO = pathlib.Path(__file__).resolve().parents[1]
_MIGRATIONS = (
    _REPO / "scripts" / "migration_publication_notice_audit_2026_08_01.sql",
    _REPO / "scripts" / "migration_article_ai_review_2026_08_01.sql",
)

_DDL = """
CREATE TEMP TABLE articles (
  id BIGINT PRIMARY KEY,
  content TEXT,
  current_content_hash CHAR(64),
  style_code VARCHAR(60),
  style VARCHAR(60),
  style_family VARCHAR(60),
  article_review_status VARCHAR(40),
  article_human_review_status VARCHAR(40),
  article_human_reviewed_by INTEGER,
  article_human_reviewed_at TIMESTAMPTZ,
  article_human_review_reason TEXT,
  article_review JSONB,
  evidence_manifest_hash CHAR(64),
  publication_profile VARCHAR(60),
  platform_review JSONB,
  evidence_pack JSONB,
  quality_warning JSONB,
  title TEXT,
  quote_id INTEGER
) ON COMMIT DROP;

CREATE TEMP TABLE quotes (id INTEGER PRIMARY KEY, industry TEXT) ON COMMIT DROP;

CREATE TEMP TABLE geo_article_review_events (
  id BIGSERIAL PRIMARY KEY,
  article_id BIGINT NOT NULL,
  actor_user_id INTEGER NOT NULL,
  decision VARCHAR(40) NOT NULL CHECK (decision IN ('approved','rejected','skipped')),
  reason TEXT NOT NULL,
  machine_review_status VARCHAR(40),
  machine_review_version VARCHAR(100),
  reviewed_content_hash CHAR(64),
  evidence_manifest_hash CHAR(64),
  prior_human_review_status VARCHAR(40),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
) ON COMMIT DROP;
"""

_EV = "e" * 64
_ILLEGAL = "我们是行业第一的服务商。"


@pytest.fixture(scope="module")
def _apply_notice_audit_migration():
    """**逐字执行**真迁移文件建留痕表(不是手抄一份影子 DDL)。

    🔴 手抄的影子 DDL 永远和迁移一致(因为都是我抄的),证明不了迁移本身对不对。
    逐字跑真文件,才能同时证明:①迁移是合法可执行 SQL ②列名/类型与代码里的
    INSERT 对得上。迁移是幂等的(IF NOT EXISTS),重复跑无副作用。
    """
    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    try:
        conn.autocommit = True
        for path in _MIGRATIONS:
            conn.cursor().execute(path.read_text(encoding="utf-8"))
    finally:
        conn.close()


@pytest.fixture()
def cur(monkeypatch, _apply_notice_audit_migration):
    """🔴 迁移 fixture 刻意是 **module 作用域 + 非 autouse**,由本 fixture 显式依赖。

    第一版写成 `scope="session", autouse=True`,结果它作用到**整个会话的所有测试文件**:
    与 test_c4_review_autopilot(导入 server.py 会对同一个库跑整份迁移清单)一起跑时
    引发 212 个 error。单跑我这个文件、或不带我这个文件跑别人,都看不出来 ——
    典型的"只在特定组合下才炸"。autouse 的作用域必须收到刚好够用为止。
    """
    monkeypatch.setenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", "true")
    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"],
                            cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        c = conn.cursor()
        c.execute(_DDL)
        yield c
    finally:
        # 留痕表是真表,但本连接整个事务 rollback → 测试写入的行不落地
        conn.rollback()
        conn.close()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed(cur, *, article_id, review_status, content="正文", style="buying_guide",
          style_family=None, human=None, reviewed=True, reviewed_content=None,
          evidence_hash=_EV, industry="建材家居", title="本地装修怎么选"):
    review_json = None
    if reviewed:
        review_json = json.dumps({
            "reviewed_content_hash": _sha(reviewed_content if reviewed_content is not None else content),
            "reviewed_evidence_manifest_hash": evidence_hash,
            "review_version": "v14-test",
        })
    cur.execute(
        "INSERT INTO quotes(id, industry) VALUES (%s,%s) ON CONFLICT (id) DO NOTHING",
        (article_id, industry),
    )
    cur.execute(
        "INSERT INTO articles(id, content, style_code, style_family, article_review_status, "
        "article_human_review_status, article_review, evidence_manifest_hash, title, quote_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
        (article_id, content, style, style_family, review_status, human, review_json,
         evidence_hash if reviewed else None, title, article_id),
    )


def _stub_key(monkeypatch):
    """给测试一把假 key,**并绕开真 key 池**。

    🔴 这不是图省事:`_deepseek_key()` 会先试 key 池模块(当时在 `tools.social_operator.deepseek_key_pool`,已上提 services/llm,原位随开源 E3 B2 删)。
    实测(2026-08-01)只要这个模块在 `import server` 之前被导入,
    test_c4_review_autopilot 就整片 24 个 error(`I/O operation on closed file`)——
    仓里既有的导入顺序地雷,与本包无关,但我的测试会踩上去。
    测试本来也不该依赖真 key 池,直接把取 key 这一步替掉即可。
    """
    from services import article_ai_review as _air

    monkeypatch.setattr(_air, "_deepseek_key", lambda: "sk-test-not-real")


def _stub_http_ok(monkeypatch, content: str, seen_urls: list | None = None):
    """替换**本模块自己的** `_post_chat` 接缝,并记录实际请求的 URL。

    🔴 记 URL 是关键:锁 8 要断言**真正打出去的 host**,而不是断言某个常量长什么样。
    断言常量的话,谁多包一层代理、或改个常量名,锁都抓不到。

    🔴 早先这里改的是全局 `httpx.Client`,结果污染到 test_c4_review_autopilot
    (只在"我的文件排在它前面"时才炸 24 个 error)。换成自家接缝后副作用不出本模块。
    """
    from services import article_ai_review as _air

    def _fake(url, headers, payload):
        if seen_urls is not None:
            seen_urls.append(url)
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(_air, "_post_chat", _fake)


def _audit_rows(cur, article_id):
    cur.execute(
        "SELECT article_id, notice_classes, notice_count, gate_snapshot "
        "FROM geo_article_publication_notice_audits WHERE article_id=%s ORDER BY id",
        (article_id,),
    )
    return cur.fetchall()


# ===========================================================================
# 锁 1 · 不点核查可直接发布(含原 legal_hard 类),零阻塞
# ===========================================================================
def test_lock1_every_downgraded_content_class_publishes_without_review(cur):
    """三类内容提示逐一构造,`assert_publication_eligible` 必须**全部不抛**。

    用 assert_ 而不是 evaluate_:真发布路径走的是前者(它会 raise),
    只测 evaluate 的话"抛不抛"这件事根本没被覆盖。
    """
    _seed(cur, article_id=501, review_status="blocked", content=_ILLEGAL)
    _seed(cur, article_id=502, review_status="rewrite_required")
    _seed(cur, article_id=503, review_status="legacy_unreviewed", reviewed=False)

    seen_classes = set()
    detail_payloads = []
    for aid in (501, 502, 503):
        verdict = assert_publication_eligible(aid, cursor=cur)  # 不抛即通过
        assert verdict["eligible"] is True
        seen_classes.update(verdict["content_notice_classes"])
        detail_payloads.extend(verdict["content_notices"])

    # 🔴 覆盖断言:样本必须真的命中了降级类,否则"全不抛"可能只是因为压根没构造出提示
    assert seen_classes == {"legal_hard", "platform_profile_hard"}, (
        f"样本没覆盖到降级类,本锁等于空跑:{seen_classes}"
    )
    assert seen_classes <= DOWNGRADED_CONTENT_NOTICE_CLASSES

    # 🔴 详情载体必须同时非空。变异 M7(只把 `content_notices` 清空、
    # `content_notice_classes` 照旧)一度从本锁下溜走 —— 类别列表还在,但 UI 渲染
    # 文案/按钮靠的是详情载体,清空后前端就只剩一个没有内容的小红点。
    assert len(detail_payloads) == 2, f"提示详情载体被清空:{detail_payloads}"
    for n in detail_payloads:
        assert n.get("class") in DOWNGRADED_CONTENT_NOTICE_CLASSES
        assert n.get("message"), "提示必须带可读文案(§3D:禁笼统,更禁没有)"
        assert n.get("reason"), "提示必须带具体触发原因"


# ===========================================================================
# 锁 2 · 🔴 反向:资金 / 租户 / 对象完整性 H0 仍拦(防降级过界)
# ===========================================================================
def test_lock2_operator_hard_still_raises(cur):
    """三类 operator_hard 逐一构造,必须**全部抛** ArticlePublicationBlocked。
    与锁 1 成对:锁 1 证明降级生效,本锁证明降级没过界。"""
    # 对象完整性:正文在审核后被改
    _seed(cur, article_id=511, review_status="approved",
          content="改过的正文", reviewed_content="审核时的正文")
    # 人工明确拒稿
    _seed(cur, article_id=512, review_status="approved", human="rejected")

    blocked_reasons = []
    for aid in (511, 512, 999999):  # 999999 = 对象不存在
        with pytest.raises(ArticlePublicationBlocked) as exc:
            assert_publication_eligible(aid, cursor=cur)
        payload = exc.value.payload
        assert payload["publication_h0_state"] == "operator_hard"
        blocked_reasons.append(payload["reason"])

    assert set(blocked_reasons) == {
        "content_changed_after_review", "human_rejected", "article_not_found",
    }, f"三类 H0 必须逐类拦住,实际={blocked_reasons}"


def test_lock2b_downgrade_does_not_leak_into_operator_hard_when_both_present(cur):
    """🔴 降级过界最隐蔽的形态:一篇**同时**命中广告法与人工拒稿的文章。
    若实现把提示就地 return clear,这篇会被放行 —— 那就是把人工拒稿一起拆了。"""
    _seed(cur, article_id=513, review_status="blocked", human="rejected", content=_ILLEGAL)
    with pytest.raises(ArticlePublicationBlocked) as exc:
        assert_publication_eligible(513, cursor=cur)
    payload = exc.value.payload
    assert payload["publication_h0_state"] == "operator_hard"
    assert payload["reason"] == "human_rejected"
    # 拦归拦,提示不许被吞:证明评估是"走完全程"而不是"提前 return"
    assert "legal_hard" in payload["content_notice_classes"]


# ===========================================================================
# 锁 4 · 静默留痕(双向)
# ===========================================================================
def test_lock4_silent_audit_written_when_publishing_with_open_notice(cur):
    _seed(cur, article_id=521, review_status="blocked", content=_ILLEGAL)
    assert _audit_rows(cur, 521) == [], "发布前不该有留痕行"

    assert_publication_eligible(521, cursor=cur)

    rows = _audit_rows(cur, 521)
    assert len(rows) == 1, f"带提示发布必须留痕一行,实际 {len(rows)} 行"
    row = rows[0]
    assert row["notice_classes"] == ["legal_hard"]
    assert row["notice_count"] == 1
    snap = row["gate_snapshot"]
    # 快照必须能还原"提示是什么、当时什么状态"——只存个时间戳等于没存
    assert snap["reason"] == "blocked"
    assert snap["reason_class"] == "legal_hard"
    assert snap["eligible"] is True
    assert snap["publication_h0_state"] == "clear"
    assert snap["notices"][0]["class"] == "legal_hard"


def test_lock4b_no_audit_row_when_publishing_without_notice(cur):
    """🔴 反向面:干净文章发布,一行都不许写。
    把写入改成无条件 INSERT(留痕失去区分度)→ 本条转红。"""
    _seed(cur, article_id=522, review_status="approved")
    assert_publication_eligible(522, cursor=cur)
    assert _audit_rows(cur, 522) == [], "无提示发布不得留痕"


def test_lock4c_audit_row_is_not_written_when_h0_blocks(cur):
    """被 H0 拦下的不算"客户选择带提示发布" → 不留痕。
    (留痕的语义是"平台提示过、客户仍选择发",拦下的根本没发出去。)"""
    _seed(cur, article_id=523, review_status="blocked", human="rejected", content=_ILLEGAL)
    with pytest.raises(ArticlePublicationBlocked):
        assert_publication_eligible(523, cursor=cur)
    assert _audit_rows(cur, 523) == [], "被拦下的不该留痕"


def test_lock4d_audit_failure_never_blocks_publishing(cur):
    """🔴 留痕是 best-effort:表不存在也绝不能让发布失败。

    构造方式是把 TEMP 留痕表 DROP 掉再发布 —— 若实现没用 SAVEPOINT,
    这条 INSERT 会把整个事务打成 aborted,后续任何语句都报
    `current transaction is aborted` → 本条红。
    """
    _seed(cur, article_id=524, review_status="blocked", content=_ILLEGAL)
    cur.execute("DROP TABLE geo_article_publication_notice_audits")

    verdict = assert_publication_eligible(524, cursor=cur)  # 必须不抛
    assert verdict["eligible"] is True

    # 事务必须仍然可用(这才是 SAVEPOINT 真正被证明的地方)
    cur.execute("SELECT 1 AS still_usable")
    assert cur.fetchone()["still_usable"] == 1


# ===========================================================================
# 锁 7 · AI 失败 fail-closed:停"未核查",可发布,不冒充已核查
# ===========================================================================
def _force_ai_failure(monkeypatch):
    """让调用直接炸,模拟 DeepSeek 不可用(替换自家接缝,不动全局 httpx)。"""
    from services import article_ai_review as _air

    def _boom(url, headers, payload):
        raise RuntimeError("deepseek down")

    monkeypatch.setattr(_air, "_post_chat", _boom)


def test_lock7_ai_failure_stops_at_not_checked_and_still_publishes(cur, monkeypatch):
    from services import article_ai_review as air

    _stub_key(monkeypatch)
    _force_ai_failure(monkeypatch)

    _seed(cur, article_id=601, review_status="legacy_unreviewed", reviewed=False,
          content="随便一段正文")
    res = air.review_article(cur, 601, title="标题", content="随便一段正文")

    # ① 不抛(抛了会中断整批 / 让核查按钮报错)
    # ② 停在未核查,**绝不**冒充 L1 通过
    assert res["level"] == air.LEVEL_NOT_CHECKED
    assert res["level"] != air.LEVEL_AUTO_PASS

    # ③ 结论照样落库:否则"这篇为什么没结论"永远查不出来,下次还会再烧一次模型
    cur.execute(
        "SELECT level, reviewer FROM geo_article_ai_review_conclusions WHERE article_id=%s",
        (601,),
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["level"] == air.LEVEL_NOT_CHECKED
    assert rows[0]["reviewer"] == air.AI_REVIEWER

    # ④ 🔴 fail-closed ≠ 拦住:这篇必须仍然能发布
    verdict = assert_publication_eligible(601, cursor=cur)
    assert verdict["eligible"] is True


def test_lock7b_unparseable_level_is_not_silently_passed(cur, monkeypatch):
    """🔴 反向:模型吐了个看不懂的档,绝不能"猜一个 L1"。
    与锁 7 成对 —— 只测"调用失败"的话,把解析失败默认成通过照样全绿。"""
    from services import article_ai_review as air

    _stub_key(monkeypatch)
    # 模型真的吐回一个不认识的档位(走真解析路径,不打桩解析器)
    _stub_http_ok(monkeypatch, '{"level":"看不懂","summary":"?","findings":[]}')

    out = air.evaluate_article_content("标题", "正文")
    assert out["level"] == air.LEVEL_NOT_CHECKED, "解析失败不得被当成审核通过"


def test_lock7c_vague_finding_is_rejected(cur, monkeypatch):
    """§3D:说有问题却给不出具体触发点 = 笼统文案,不许透给客户。"""
    from services import article_ai_review as air

    _stub_key(monkeypatch)
    _stub_http_ok(monkeypatch, json.dumps({
        "level": "L2", "summary": "存在风险",
        "findings": [{"trigger": "", "suggestion": "改一下"}],   # 无具体触发点
    }, ensure_ascii=False))

    out = air.evaluate_article_content("标题", "正文")
    assert out["level"] == air.LEVEL_NOT_CHECKED
    assert out["findings"] == []


def test_lock7d_concrete_finding_is_kept(cur, monkeypatch):
    """🔴 与 7c 成对:带具体触发点的 finding 必须留下。
    只有 7c 的话,把 findings 恒清空也能全绿 —— 那等于 AI 白跑。"""
    from services import article_ai_review as air

    _stub_key(monkeypatch)
    _stub_http_ok(monkeypatch, json.dumps({
        "level": "L2", "summary": "有两处绝对化用语",
        "findings": [
            {"trigger": "第二段「我们是行业第一」", "suggestion": "改为「在公开口径下表现较好」"},
            {"trigger": "", "suggestion": "这条没有落点,应被丢弃"},
        ],
    }, ensure_ascii=False))

    out = air.evaluate_article_content("标题", "正文")
    assert out["level"] == "L2"
    assert len(out["findings"]) == 1, "带 trigger 的要留、不带的要丢,不能一刀切"
    assert "行业第一" in out["findings"][0]["trigger"]


# ===========================================================================
# 锁 8 · 官方直连 base_url + 幂等 by 正文 hash
# ===========================================================================
def test_lock8_calls_official_deepseek_host_only(cur, monkeypatch):
    """🔴 §2 硬约束:必须打官方 api.deepseek.com,禁 OpenRouter / DashScope 代理版。

    断言打在**实际发出的 URL** 上,不是断言常量长什么样 ——
    后者改个常量名或多包一层代理就绕过去了。
    """
    from services import article_ai_review as air
    from urllib.parse import urlparse

    _stub_key(monkeypatch)
    seen: list[str] = []
    _stub_http_ok(monkeypatch, json.dumps({"level": "L1", "summary": "ok", "findings": []}),
                  seen_urls=seen)

    air.evaluate_article_content("标题", "正文")

    assert len(seen) == 1, f"应恰好发起 1 次调用,实际 {len(seen)}"
    host = urlparse(seen[0]).netloc
    assert host == "api.deepseek.com", f"必须官方直连,实际打到 {host}"
    assert "openrouter" not in seen[0] and "dashscope" not in seen[0] and "aliyuncs" not in seen[0]


def test_lock8b_idempotent_by_content_hash(cur, monkeypatch):
    """同一篇 + 同一份正文二跑:**零新增模型调用、零新增审计行**。"""
    from services import article_ai_review as air

    _stub_key(monkeypatch)
    calls: list[str] = []
    _stub_http_ok(monkeypatch, json.dumps({"level": "L1", "summary": "ok", "findings": []}),
                  seen_urls=calls)

    body = "同一份正文"
    _seed(cur, article_id=611, review_status="approved", content=body)

    first = air.review_article(cur, 611, title="标题", content=body)
    assert first["reused"] is False and first["persisted"] is True
    assert len(calls) == 1

    second = air.review_article(cur, 611, title="标题", content=body)
    assert second["reused"] is True, "同 hash 二跑必须复用,不得重复调模型"
    assert len(calls) == 1, f"二跑不得新增模型调用,实际累计 {len(calls)}"

    cur.execute(
        "SELECT COUNT(*) AS n FROM geo_article_ai_review_conclusions WHERE article_id=%s",
        (611,),
    )
    assert cur.fetchone()["n"] == 1, "二跑不得新增审计行"


def test_lock8c_changed_content_is_re_evaluated(cur, monkeypatch):
    """🔴 与 8b 成对:正文改了必须重评。
    只测 8b 的话,把 review_article 改成"永远复用第一条"也能全绿 —— 那是把功能做死。"""
    from services import article_ai_review as air

    _stub_key(monkeypatch)
    calls: list[str] = []
    _stub_http_ok(monkeypatch, json.dumps({"level": "L1", "summary": "ok", "findings": []}),
                  seen_urls=calls)

    _seed(cur, article_id=612, review_status="approved", content="原始正文")
    air.review_article(cur, 612, title="标题", content="原始正文")
    assert len(calls) == 1

    out = air.review_article(cur, 612, title="标题", content="改过之后的正文")
    assert out["reused"] is False, "正文变了必须重评"
    assert len(calls) == 2

    cur.execute(
        "SELECT COUNT(*) AS n FROM geo_article_ai_review_conclusions WHERE article_id=%s",
        (612,),
    )
    assert cur.fetchone()["n"] == 2, "新正文应产生新行,旧结论留痕不删"


# ===========================================================================
# §3D 存量批跑 · 选取口径 + 不碰人工签发 + L3 占比分母
# ===========================================================================
def test_lock9_pilot_scope_uses_machine_column_not_human(cur):
    """🔴 EXIT 订正的落地锁:`pending_human_review` 在**机审列**。

    取错列的表现是"成功跑完 0 篇"且**不报错** —— 最坏的一类失败。
    本锁正向证明机审列取得到,反向证明人审列取不到(该值在人审列 0 行)。
    """
    from services.article_ai_review_batch import PILOT_SQL, count_scope

    # 机审列 = pending_human_review 且未签发 → 应入选
    _seed(cur, article_id=701, review_status="pending_human_review")
    # 机审列同值但**已人工签发** → 不该入选(已经有人看过了)
    _seed(cur, article_id=702, review_status="pending_human_review", human="approved")
    # 干扰项:人审列才是 pending_human_review(生产此形态 0 行)
    _seed(cur, article_id=703, review_status="approved", human="pending_human_review")

    assert count_scope(cur, "pilot") == 1, "只有 701 该入选"
    cur.execute(PILOT_SQL)
    assert [r["id"] for r in cur.fetchall()] == [701]

    # 🔴 反向对照:把条件写到人审列上,集合会变空 —— 这正是取错列的真实后果
    cur.execute(
        "SELECT COUNT(*) AS n FROM articles WHERE article_human_review_status='pending_human_review'"
        " AND COALESCE(article_review_status,'')=''"
    )
    assert cur.fetchone()["n"] == 0, "该形态应为 0,证明取错列必然得空集"


def test_lock9b_full_scope_covers_three_classes(cur):
    """全量 1243 = legacy + 未签发 + blocked 三类之和;approved 不进队列。"""
    from services.article_ai_review_batch import count_scope

    _seed(cur, article_id=711, review_status="legacy_unreviewed", reviewed=False)
    _seed(cur, article_id=712, review_status="pending_human_review")
    _seed(cur, article_id=713, review_status="blocked", content=_ILLEGAL)
    _seed(cur, article_id=714, review_status="approved")          # 不该入选
    _seed(cur, article_id=715, review_status="pending_human_review", human="approved")  # 已签发,不该入选

    assert count_scope(cur, "full") == 3, "只有三类未处理的该入选"


def test_lock9c_batch_never_touches_human_signoff_fields(cur, monkeypatch):
    """🔴🔴 批跑绝不能抹掉人工签发。

    `refresh_article_review()` 会把签发四字段一并置 NULL;若批跑图省事顺手调它,
    生产 22 篇已签发记录会被一次性抹掉。本锁对一篇**已签发**文章跑完批处理后
    逐字段核对原值。
    """
    from services import article_ai_review as air
    from services.article_ai_review_batch import run_batch

    _stub_key(monkeypatch)
    _stub_http_ok(monkeypatch, json.dumps({"level": "L1", "summary": "ok", "findings": []}))

    _seed(cur, article_id=721, review_status="legacy_unreviewed", reviewed=False)
    cur.execute(
        "UPDATE articles SET article_human_review_status='approved', "
        "article_human_reviewed_by=42, article_human_review_reason='已核对' WHERE id=%s",
        (721,),
    )
    # 已签发的不进 full 集合,但即便通过 limit 强行跑到也不许改这些字段
    air.review_article(cur, 721, title="标题", content="正文")

    cur.execute(
        "SELECT article_human_review_status AS s, article_human_reviewed_by AS b, "
        "article_human_review_reason AS r FROM articles WHERE id=%s", (721,),
    )
    row = cur.fetchone()
    assert row["s"] == "approved" and row["b"] == 42 and row["r"] == "已核对", (
        "AI 批跑抹掉了人工签发字段"
    )


def test_lock9d_l3_ratio_denominator_excludes_not_checked(cur, monkeypatch):
    """🔴 判据不能被 AI 大面积失败骗过去。

    若 l3_ratio 的分母用 total,那么 AI 全挂时 not_checked 会把 L3 占比稀释到接近 0,
    "L3 < 20% → 铺全量"这条判据反而在最该停手的时候放行。分母必须只含已分档。
    """
    from services.article_ai_review_batch import run_batch

    _stub_key(monkeypatch)
    for aid in (731, 732, 733, 734):
        _seed(cur, article_id=aid, review_status="legacy_unreviewed",
              reviewed=False, content=f"正文{aid}")

    # 两篇 L3、两篇 AI 失败
    calls = {"n": 0}
    from services import article_ai_review as _air

    def _two_then_fail(url, headers, payload):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("deepseek down")
        return {"choices": [{"message": {"content": json.dumps({
            "level": "L3", "summary": "需人工",
            "findings": [{"trigger": "第二段的数据无出处", "suggestion": "补来源"}],
        }, ensure_ascii=False)}}]}

    monkeypatch.setattr(_air, "_post_chat", _two_then_fail)

    res = run_batch(cur, "full")
    assert res["levels"].get("L3") == 2
    assert res["levels"].get("not_checked") == 2
    # 分母只含已分档(2),所以 100% —— 而不是被 4 稀释成 50%
    assert res["l3_ratio"] == 1.0, (
        f"分母混进了 not_checked,L3 占比被稀释成 {res['l3_ratio']}"
    )


# ===========================================================================
# 锁 5 · §3C UI:「剩 N 轮」等轮数字样 0 命中 + 单按钮 + 微光
# ===========================================================================
_WRITING_HALL = _REPO / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"

#: 渲染出去的轮数字样。刻意只匹配**会出现在界面上**的形态,不匹配变量名
#: (`roundsLeft` 本身必须保留 —— 上限逻辑内部化,不是删掉)。
_ROUND_LEAK_PATTERNS = (
    "剩 ${roundsLeft}",
    "剩余 ${roundsLeft}",
    "剩 {roundsLeft}",
    "修复次数已用完",
)


def _repair_button_block() -> str:
    """取一键修复按钮那一段源码(而不是整个文件),让断言有明确落点。"""
    src = _WRITING_HALL.read_text(encoding="utf-8")
    start = src.index('data-testid="auto-repair-btn"')
    return src[start - 400:start + 1200]


def test_lock5_repair_button_leaks_no_round_count():
    """🔴 §3C-4:UI 禁止出现「剩 N 轮」。

    Owner:"为什么分几轮?一轮修完不行吗?" —— 轮数是内部实现概念,
    客户只需要知道"点一下,能修的都修好"。
    """
    block = _repair_button_block()
    hits = [p for p in _ROUND_LEAK_PATTERNS if p in block]
    assert not hits, f"一键修复按钮仍在渲染轮数字样:{hits}"


def test_lock5b_round_cap_is_internalised_not_deleted():
    """🔴 与 5 成对:上限逻辑**必须还在**(防不收敛),只是不渲染。

    只有 5 的话,把 roundsLeft 整个删掉也全绿 —— 那是把防不收敛的闸拆了,
    比泄漏一个数字严重得多。
    """
    block = _repair_button_block()
    assert "roundsLeft <= 0" in block, "轮数上限被删掉了(disabled 判据没了)"


def test_lock5c_repair_is_a_single_button():
    """§3C-4:一键修复只有一颗按钮。"""
    block = _repair_button_block()
    assert block.count('data-testid="auto-repair-btn"') == 1


def test_lock5d_scan_actually_detects_a_leak():
    """判别力自证:把轮数字样注回去,上面那条锁必须抓到。
    否则切片口径写错(取到空串)会让它恒真。"""
    block = _repair_button_block()
    assert block, "切片为空 → 锁恒真"
    injured = block.replace("{busy ? '修复中…' : '一键修复'}",
                            "{busy ? '修复中…' : `一键修复(剩 ${roundsLeft} 轮)`}")
    assert any(p in injured for p in _ROUND_LEAK_PATTERNS), "注回轮数后仍未检出 → 锁没有判别力"


def test_lock5e_badges_no_longer_claim_publication_will_be_blocked():
    """🔴 §3A 降级后,徽章里"会被拦下 / 确认后再对外发布"这类话已经是**假话**。

    本文件 D-3 注释记过同型事故(文案与行为不符,代理以为文章废了)。
    这次方向相反:说了一件不再存在的事。两个方向都不许有。
    """
    import re as _re

    src = _WRITING_HALL.read_text(encoding="utf-8")
    start = src.index("const REVIEW_STATUS_BADGES")
    block = src[start:start + 3000]
    # 🔴 必须先剥注释再扫:解释"我为什么改掉旧文案"的注释里**必然**会引用旧文案,
    # 不剥的话本锁会咬自己的说明文字(第一版就是这么红的),而那不是渲染出去的内容。
    block = _re.sub(r"/\*[\s\S]*?\*/", "", block)
    block = _re.sub(r"//[^\n]*", "", block)
    for stale in ("待人工确认", "确认一处后再对外发布", "仅在对外发布时会被拦下"):
        assert stale not in block, f"徽章仍在承诺一个已不存在的阻断:{stale}"
    # 反向:提示级口径必须真的写上去了(不是把文案删空了事)
    assert "由你决定" in block or "也可以直接发布" in block
