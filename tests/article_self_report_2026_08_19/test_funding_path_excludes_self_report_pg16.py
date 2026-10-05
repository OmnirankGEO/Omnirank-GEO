"""判据⑥ · 资金路径:未核实自报**不得**进监测入池 / 严格投影(R2 §⑤)。

Review-CTO 2026-08-19 追加第 ⑤ 条,优先级最高 —— 因为**监测入池是扣费路径**,
不是展示口径。`db/monitoring_db.py::get_monitoring_enabled_clients` 上方那段
CTO-15.23 注释把代价写得很直白:加那道「已发文」闸就是因为老板反馈
「用户没发文但被自动监测扣费(每词 38 积分/次)」。

## 先证伪工单给的坐标(2026-08-19 显式路径实测)

Review 指的 `services/publication_stage_sources.py:138/183` 与
`db/monitoring_db.py:9563`,在**本包底(生产尖 `99492532`)上是这样的**:

  · `services/publication_stage_sources.py` —— **本仓 prod 尖不存在这个文件**。
    它只存在于**图文(geoimg)包**的 worktree(`feat/geo-image-note-2026-08-17`,
    尖 `b448480d6`,**尚未发车**)。
  · `db/monitoring_db.py`(10902 行,行号对得上)—— 但全文 **0 处** `publish_records`。
    9563 一带是 `get_monitoring_enabled_clients`,它的闸是
    `EXISTS articles.first_published_at IS NOT NULL`。
  · `articles.first_published_at` 全仓**只有一个写入方**:
    `services/publication_facts.record_manual_publication`(人工登记,operator 驱动)。
    `publish_records` **不写它**。

**结论:在本包底上,浏览器自报进不了扣费池** —— 所以本包不是"漏修",
而是那条路径当前不通。**但 Review 的实质判断完全成立**:在图文包里它是通的
(`monitoring_db.py:9563` 正是把闸换成 `published_occurrence_predicate("q.id")`,
而该谓词的 `publish_records` 臂是**裸** `rx.status='success'`;
`publication_stage_sources.py:138` 一带的 body proof 用的是
`public_url_reported_explicitly`——正是 R2 刚降级掉的**来源位**)。

## 所以这份锁做两件事

1. **钉住当前边界**(真库):伪造一条 raw success → 该报价**不进**
   `get_monitoring_enabled_clients()`;成对反向对照:人工登记 → 立刻进。
   没有反向臂的话,"不进"可能只是因为夹具根本凑不齐入池条件。
2. **跨包完备性闸**:全仓扫描,任何把 `publish_records` 当
   「发生过发布 / 正文证据」的 SQL 臂,**必须**带核实闸。
   今天全绿(本包底没有这种臂);图文包按现状合过来的**当天变红**,
   资金回归落不进去。
"""

from __future__ import annotations

import os
import ast
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get("TEST_DATABASE_URL")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
SCHEMA_SQL = Path(__file__).resolve().parent / "prod_schema_2026-08-19.sql"
MIGRATION_SQL = ROOT / "scripts" / "migration_publish_records_url_verification_2026_08_19.sql"
#: [并车 2026-08-20 · 36 班] 图文包迁移随树进列。schema dump 是 08-19 的快照,不含它们;
#: 而并树后的**代码**(如 monitoring_db 的 ix.source_geo_post_id)引用 034 的列 ——
#: 夹具必须像生产 prestart 一样重放缺的迁移,否则红的是夹具不是代码。
#: 三条全部 additive 零 DML(034/036)或幂等替换(035),重放安全。
GEOIMG_MIGRATIONS = (
    ROOT / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql",
    ROOT / "db" / "migration_035_geo_image_note_slot_channel_2026_08_18.sql",
    ROOT / "db" / "migration_036_publication_stage_strict_source_2026_08_18.sql",
)

ARTICLE_ID = 993101
TOPIC_ID = 993102
QUOTE_ID = 993103
BRAND_ID = 993104
OWNER_UID = 993105
UID = "993105"
KEYWORD_ID = 993106
TITLE = "资金路径验证专用标题一二三"
PUBLIC_URL = "https://www.toutiao.com/article/993101/"


def _skip_unless_throwaway_pg():
    if not PG_URL:
        pytest.skip("需要 TEST_DATABASE_URL(一次性 loopback 测试库)")
    parsed = urlsplit(PG_URL)
    if (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
        pytest.skip(f"只允许 loopback 一次性容器 DSN:{parsed.hostname}")
    base_db = (parsed.path or "").lstrip("/").lower()
    if "prod" in base_db or "test" not in base_db:
        pytest.skip(f"基础库名必须含 test 且不含 prod:{base_db}")


@pytest.fixture(scope="module")
def db():
    _skip_unless_throwaway_pg()
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    name = f"artfund_test_{uuid.uuid4().hex[:10]}"
    assert "test" in name and "prod" not in name
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parsed = urlsplit(PG_URL)
    url = urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))

    raw = SCHEMA_SQL.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in raw.splitlines()
        if not line.startswith("\\") and not line.startswith("CREATE SCHEMA public;")
    )
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    with conn.cursor() as cur:
        for _ext in ("vector", "pg_trgm"):
            cur.execute(f"CREATE EXTENSION IF NOT EXISTS {_ext}")
        cur.execute(body)
        cur.execute("SET search_path TO public")
        for _mig in GEOIMG_MIGRATIONS:
            cur.execute(_mig.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_SQL.read_text(encoding="utf-8"))
    try:
        yield type("DB", (), {"url": url, "conn": conn})
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture(autouse=True)
def seed(db):
    """凑齐**除「已发文」之外**的全部入池条件 —— 这样入不入池只由那一条决定。

    🔴 这一点是本锁的分母:如果别的条件也不满足,"没进池"就说明不了任何事。
    """
    with db.conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        cur.execute("DELETE FROM media_publications WHERE quote_id=%s", (QUOTE_ID,))
        cur.execute("DELETE FROM publish_records WHERE user_id=%s", (UID,))
        cur.execute("DELETE FROM confirmed_keywords WHERE quote_id=%s", (QUOTE_ID,))
        cur.execute("DELETE FROM articles WHERE id=%s", (ARTICLE_ID,))
        cur.execute("DELETE FROM quotes WHERE id=%s", (QUOTE_ID,))
        cur.execute("DELETE FROM brands WHERE id=%s", (BRAND_ID,))
        cur.execute("INSERT INTO brands (id,name,owner_user_id) VALUES (%s,%s,%s)",
                    (BRAND_ID, "资金路径验证品牌", OWNER_UID))
        cur.execute(
            """
            INSERT INTO quotes (id, brand_id, brand_name, service_days, owner_user_id,
                                status, monitoring_enabled, monitoring_frequency,
                                monitoring_start_hour, service_start_date, service_end_date,
                                service_status)
            VALUES (%s,%s,%s,365,%s,'paid',TRUE,1,0,CURRENT_DATE - 1,
                    CURRENT_DATE + 30,'active')
            """,
            (QUOTE_ID, BRAND_ID, "资金路径验证品牌", OWNER_UID),
        )
        # confirmed_keywords.monitoring_product_version 有 DEFAULT 且 FK 到矩阵表,
        # 生产库里那一行是既有数据;一次性库要自己补,否则 FK 直接拒。
        cur.execute(
            "INSERT INTO monitoring_product_platform_matrices (version, platforms) "
            "VALUES ('monitoring-unified5-v1','[]') ON CONFLICT DO NOTHING")
        cur.execute(
            "INSERT INTO confirmed_keywords (id, quote_id, keyword, is_core) "
            "VALUES (%s,%s,%s,TRUE)", (KEYWORD_ID, QUOTE_ID, "资金路径验证词"))
        cur.execute(
            "INSERT INTO articles (id,topic_id,quote_id,title,content,publication_profile) "
            "VALUES (%s,%s,%s,%s,%s,'standard')",
            (ARTICLE_ID, TOPIC_ID, QUOTE_ID, TITLE, "正文。" * 20))
    yield


def _insert_raw_self_report(db, *, state="pending"):
    with db.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO publish_records
            (user_id, article_title, platform, account_name, status, brand_id, article_id,
             submitted_title_snapshot, public_url, public_url_reported_explicitly,
             public_url_verification_state, created_at)
            VALUES (%s,%s,'今日头条','acct-1','success',%s,%s,%s,%s,TRUE,%s,CURRENT_TIMESTAMP)
            """,
            (UID, TITLE, BRAND_ID, ARTICLE_ID, TITLE, PUBLIC_URL, state),
        )


def _monitoring_pool(db, monkeypatch) -> set[int]:
    """跑**真实**的 `get_monitoring_enabled_clients()`,返回入池 quote_id 集合。"""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    import db.connection as dbconn

    opened = []

    def _get():
        c = psycopg2.connect(db.url, cursor_factory=RealDictCursor)
        with c.cursor() as cur:
            cur.execute("SET search_path TO public")
        opened.append(c)
        return c

    monkeypatch.setattr(dbconn, "get_connection", _get)
    try:
        from db.monitoring_db import get_monitoring_enabled_clients
        rows = get_monitoring_enabled_clients()
    finally:
        monkeypatch.undo()
        for c in opened:
            try:
                c.close()
            except Exception:
                pass
    return {int(r["quote_id"]) for r in rows}


# ===========================================================================
# 1 · 钉住当前边界(真库 · 成对)
# ===========================================================================

@pytest.mark.parametrize("state", ["pending", "content_matched", "needs_action", "unverified"])
def test_raw_self_report_never_enters_billing_pool(db, monkeypatch, state):
    """伪造 raw success(含探针给出的 content_matched 线索)→ **监测 occurrence 零新增**。

    四个未核实态逐个打 —— `content_matched` 尤其要打:它是 R2 新增的态,
    最容易被后来者当成"核实过了"顺手放行。
    """
    _insert_raw_self_report(db, state=state)
    assert QUOTE_ID not in _monitoring_pool(db, monkeypatch), (
        f"未核实自报(state={state})把客户放进了扣费池")


def test_manual_registration_does_enter_pool(db, monkeypatch):
    """🔴 反向对照(分母自证):真发布事实进得来。

    没有这一条,上面四条"进不来"可能只是夹具凑不齐入池条件 ——
    那种全绿什么也证明不了。
    """
    assert QUOTE_ID not in _monitoring_pool(db, monkeypatch), "起点必须是没进池"
    # [并车 2026-08-20 · 36 班] 池闸已换成统一 occurrence 谓词(WP7 cutover):
    # 「真发布事实」现在的最诚实形态就是**手工登记行本身**(media_publications),
    # 不再是直写 first_published_at 列(那是旧闸的输入,新闸不读它)。
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO media_publications (quote_id, article_id, brand_id,"
            " platform_name, article_url, publish_date)"
            " VALUES (%s, %s, %s, '搜狐', 'https://www.sohu.com/a/993101', NOW())",
            (QUOTE_ID, ARTICLE_ID, BRAND_ID))
    assert QUOTE_ID in _monitoring_pool(db, monkeypatch), (
        "真发布事实都进不来 → 这份锁的分母是空的,前面四条不算数")


def test_pool_gate_today_is_the_unified_occurrence_predicate(db, monkeypatch):
    """[并车 2026-08-20 · 36 班翻转] 池闸口径钉现役:统一 occurrence 谓词。

    旧版这条钉的是「入池只看 first_published_at」并预告"图文包正在换闸,
    换的那天要有人来看一眼新口径有没有把未核实自报放进来"。那一天就是并车日,
    看过了:新闸的 publish_records 臂带核实位(见 seam 套件的单一出处锁),
    上面四条 raw_self_report 判据 + 本文件的正对照就是行为层的守卫。
    这条判据的新职责:池闸必须**只**通过 published_occurrence_predicate 组合,
    不许在 monitoring_db 里内联第二份 publish_records 谓词(单一出处)。
    """
    src = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
    assert "published_occurrence_predicate" in src, (
        "池闸不再走统一 occurrence 谓词 —— 口径又换了,必须有人重判自报是否被挡")
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "FROM publish_records" not in body, (
        "monitoring_db 内联了自己的 publish_records 谓词 —— 单一出处被绕开,"
        "必须先确认带没带核实闸")


def test_first_published_at_has_exactly_one_writer():
    """`articles.first_published_at` 的写入方 census —— 它是扣费闸的**唯一**输入。

    多一个写入方 = 多一条能把客户放进扣费池的路。新增写入方必须先回答
    「它认的是不是权威事实」。
    """
    writers = []
    for path in ROOT.rglob("*.py"):
        parts = set(path.parts)
        if {".git", "node_modules", "__pycache__"} & parts or "tests" in parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        # 🔴 先剥掉 `#` 注释行再匹配:`db/diagnosis_db.py:902` 有一行**注释**写着
        #    "…UPDATE articles SET first_published_at",裸串锚会把它当成写入方。
        #    正确处置是收紧匹配面(注释不是代码),不是把那个文件加进白名单 ——
        #    加白名单等于以后它真写了也看不见。
        code = "\n".join(
            l for l in text.splitlines() if not l.lstrip().startswith("#"))
        if re.search(r"SET\s+first_published_at", code):
            writers.append(path.relative_to(ROOT).as_posix())
    assert sorted(writers) == ["services/publication_facts.py"], writers


# ===========================================================================
# 2 · 跨包完备性闸:任何 publish_records 的「发生过发布 / 正文证据」臂都要带核实
# ===========================================================================

#: 用得上 `publish_records` 却**不该**要求核实的场景 —— **函数级**豁免,逐条写理由。
#:
#: 🔴 R5 ④:原来是**整文件**豁免的一个集合,后果很直接 ——
#:    `api/meijiehezi_api.py` 4523 行整体免检,里面 5 条臂只有 1 条被真正论证过;
#:    插件后端(WO_273 已整体删除)10 条臂也是整体免检。这不是"豁免了几个文件",
#:    是**在闸上开了几个按文件计的洞**,以后往这些文件里新写的任何臂都自动免检。
#:
#:    收窄到 `(文件, 函数)` 之后,全仓 28 条臂里**只有 4 条**需要豁免(见下),
#:    其余 24 条一律受闸。并且下面配了三条自锁:
#:      · 每条豁免必须**真的对上一条违规臂**(否则就是白开的后门);
#:      · 豁免的函数必须**在文件里真实存在**(改名/删函数后名单不许烂着);
#:      · Python 文件**不许**再出现整文件豁免。
#:
#: key = (相对路径, 函数名);value = 理由(空理由不许过)。
OCCURRENCE_SCAN_EXEMPT: dict[tuple[str, str], str] = {
    # [WO_273 · 2026-09-23] 原有一条插件后端 `get_publish_counts_24h`(扩展面板的每账号 24h 提交次数徽标)。
    #   该函数连同所在文件随插件后端整体删除,豁免同步删 —— 下面两把自锁(豁免必须对上违规臂 /
    #   豁免的函数必须存在)本来就不许它烂着。删后分母:全仓臂 30 → 20,在断言发布的臂 9 → 8。
    ("api/meijiehezi_api.py", "api_published_articles"):
        "R2 §③ + Owner 拍板**明确保留**的防重复发布占位:回执成功就占位,"
        "否则用户会把同一篇再发一遍(换媒体重投合法且真扣费)。"
        "同一响应里另给 `verified_published_article_ids` / "
        "`reported_success_unverified_article_ids` 两个字段,已核实与未核实分得开。",
    ("api/meijiehezi_api.py", "api_article_publish_stats"):
        "双轴投影的**来源**本身:它要把自报行投成 `reported_success_unverified` 这一档,"
        "所以必须先把未核实的行取出来。取出来之后没有被当成发布事实 ——"
        "`published` 计数与 `published_media` 都不含它(有判据在钉)。",
}

#: 允许整文件豁免的**非 Python** 文件(SQL 没有函数边界)。当前为空 ——
#: R5 ④ 复查发现原先豁免的三个 .sql / 校验脚本里**一条臂都没有**,
#: 也就是说那几条豁免从来没起过作用,纯粹是白开的口子,直接删掉。
OCCURRENCE_SCAN_WHOLE_FILE_EXEMPT: dict[str, str] = {}

#: 「这条臂从哪开始」——只认起点,臂的**终点**由 `_arm_body` 按括号平衡扫出来。
#:
#: 🔴 R4 §E:原来这里是一条非贪婪正则 `(?P<body>.{0,900}?)(?:\)|\Z)`,
#:    **截断于第一个右括号**。于是一条这样的臂:
#:
#:        FROM publish_records rx WHERE rx.article_id = a.id
#:          AND COALESCE(rx.status, '') = 'success'
#:
#:    截出来的 body 只到 `COALESCE(rx.status, '` —— `= 'success'` 被切在外面,
#:    `_RAW_SUCCESS` 匹配不上 → `asserts_publication` 为假 → **这条臂被整个跳过**。
#:    也就是说:凡是写成 COALESCE 形状的臂,这道跨包闸一条都拦不住。
#:    而 R3 §⑤ 刚把本仓的同类谓词**全部**改成了 COALESCE 形状,图文包按现状合过来
#:    也会经过同一轮改写 —— 闸看着是绿的,实际早已不设防(零判别力的绿)。
#: 🔴 R4 §E 复查时又挖出两个同族的洞(都由真实代码暴露,不是假想):
#:
#:  (b) **无别名的臂**。`services/article_data_health.py:74` 写的是
#:        FROM publish_records
#:         WHERE status='success' AND public_url_verification_state = 'verified'
#:      原起点正则强制要一个别名 `(\w+)`,于是把 `WHERE` 当成了别名吃掉,
#:      臂身从 `WHERE` 之后开始;而两条探测正则又强制要 `<别名>.` 前缀,
#:      裸列名一个都匹配不上 → **这条臂对闸完全隐形**。
#:      今天它恰好是带闸的,所以没出事;换成不带闸的同样隐形 —— 那才是漏的形态。
#:      修法:别名整体可选(且用负向断言挡住 SQL 关键字),列前缀也可选。
#:
#:  (c) **注释里的分号截断臂身**。`services/strict_article_outcomes.py:364` 的
#:      `FROM publish_records p` 与它的 WHERE 之间隔着两行 `--` 注释,其中一行含
#:      一个 `;`。`_arm_body` 见 `;` 即停 → 臂身只剩注释,WHERE 整段没进来。
#:      修法:扫描前**剥掉行注释**。这同时堵住反方向的危险 ——
#:      一行 `-- p.public_url_verification_state = 'verified'` 的注释本来能让
#:      `_VERIFIED_GATE` 命中,把一条真正裸奔的臂判成"有闸"。
_SQL_KEYWORDS_NOT_ALIAS = (
    "WHERE|AND|OR|ON|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|OUTER|GROUP|ORDER|LIMIT|OFFSET"
    "|UNION|EXCEPT|INTERSECT|HAVING|WINDOW|RETURNING|SET|VALUES|USING|NATURAL"
)
#: 🔴 R5 ①:起点原来只认 `FROM`。两个现实形态漏在外面 ——
#:    · `JOIN publish_records p ON …`(以及 INNER/LEFT/RIGHT/FULL/CROSS JOIN):
#:      一条 JOIN 进来的臂照样能拿 `p.status='success'` 当发布事实,而闸看不见;
#:    · `public.publish_records`:schema 限定写法在迁移与部分脚本里很常见。
#:    两者都属于"闸带着盲区上岗"——今天没人这么写不代表明天没有,
#:    而图文包并车时这道闸是**执行锁**,盲区就是放行口。
_OCCURRENCE_START = re.compile(
    r"(?:FROM|(?:INNER\s+|LEFT\s+|RIGHT\s+|FULL\s+|CROSS\s+)?"
    r"(?:OUTER\s+)?JOIN)\s+(?:public\.)?publish_records\b"
    r"(?:\s+(?:AS\s+)?(?!(?:" + _SQL_KEYWORDS_NOT_ALIAS + r")\b)(\w+))?",
    re.IGNORECASE,
)

#: 注释:行注释(SQL `--` / Python `#`)**与 SQL 块注释 `/* … */`**。
#:
#: 🔴 R5 ②:原来只剥行注释。块注释里同样能伪造一条核实谓词 ——
#:        SELECT 1 FROM publish_records rx
#:         /* rx.public_url_verification_state = 'verified' */
#:         WHERE rx.status = 'success'
#:    `_VERIFIED_GATE` 会在块注释里命中,于是**一条真正裸奔的臂被判成有闸**。
#:    这与 R4 修的行注释假冒是同一个洞,只是换了一种注释语法。
#:    块注释跨行,所以要 DOTALL;替换时按原长填空格,保持行号不动。
_LINE_COMMENT = re.compile(r"/\*.*?\*/|(?:--|#)[^\n]*", re.DOTALL)


def _blank_out_comments(text: str) -> str:
    """把注释替换成**等长**的空白(换行保留),这样行号与偏移都不变。"""
    def _pad(mm: "re.Match[str]") -> str:
        return "".join("\n" if ch == "\n" else " " for ch in mm.group(0))
    return _LINE_COMMENT.sub(_pad, text)

#: 单条臂最多扫多远。够长以覆盖真实的多行 EXISTS,又不至于吞掉后面整段文件。
_ARM_MAX_SPAN = 1200

#: 「这个右括号只是派生表的收尾,臂还没完」的形状:`) x WHERE …` / `) AS x ON …`。
#:
#: 🔴 **承重的是后面那个 lookahead**:右括号之后必须是「一个标识符 + 一个子句关键字」。
#:    `EXISTS ( … ) AND EXISTS ( … )` 里,`)` 后面是 `AND EXISTS` ——
#:    `EXISTS` 不在子句关键字表里,所以不匹配,臂照旧停。**不能晚停**:
#:    晚停会把下一条臂的闸吃进来,把一条真正裸奔的臂判成"有闸"。
#:
#: 🔴 这里原本还写了一个 `(?!(?:AND|OR)\b)` 的负向断言。变异实验证明它**够不到** ——
#:    把它删掉,`test_derived_table_scan_still_stops_at_a_sibling_exists` 照样绿,
#:    因为上面那个 lookahead 已经把 `) AND EXISTS` 挡在外面了(`) AND WHERE`
#:    这种既要绕过 lookahead 又要靠它拦的组合根本不是合法 SQL)。
#:    留着它只会让人以为那道防线在这一层,所以删掉,把承重点说清楚。
_DERIVED_TABLE_TAIL = re.compile(
    r"\)\s*(?:AS\s+)?\w+\s*"
    r"(?=(?:WHERE|ON|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|GROUP|HAVING|ORDER|LIMIT"
    r"|UNION|EXCEPT|INTERSECT)\b)",
    re.IGNORECASE,
)


def _arm_body(text: str, start: int) -> str:
    """从臂的起点扫到**它自己的**边界:括号平衡 + 语句/字面量边界。

    终止条件(任一命中即止):
      · 遇到一个把**外层**括号关掉的 `)`(depth 已经是 0 时的右括号)
        —— 那正是包着这条 `EXISTS ( ... )` 的括号,臂到此为止;
      · `;` 语句结束;
      · Python 三引号字面量结束(SQL 常写在三引号串里);
      · 扫满 `_ARM_MAX_SPAN`。

    `COALESCE(...)` 这类**臂内部**的括号会让 depth 先 +1 再 -1,不会误判成边界 ——
    这正是原正则做不到的那件事。
    """
    depth = 0
    i = start
    limit = min(len(text), start + _ARM_MAX_SPAN)
    while i < limit:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                # 🔴 R5 ③:这个 `)` 未必是臂的终点。派生表形态
                #        FROM (SELECT … FROM publish_records) x WHERE x.status='success'
                #    里,臂的起点在**子查询内部**,而闸写在子查询**外面**;
                #    见 `)` 就停会把臂身截成空,整条臂对闸隐形。
                #    判别方式:`)` 后面若跟着「别名 + 同一个查询的子句关键字」,
                #    说明我们只是从派生表里爬出来,臂还在继续 → 继续扫。
                #    而 `EXISTS ( … )` 那种后面跟的是 `AND` / `OR` / 另一个谓词,
                #    不匹配这个形状 → 照旧停(不能晚停,晚停会把下一条臂的闸吃进来)。
                if _DERIVED_TABLE_TAIL.match(text, i):
                    i += 1
                    continue
                break
            depth -= 1
        elif ch == ";":
            break
        elif text[i:i + 3] in (chr(34) * 3, chr(39) * 3):
            break
        i += 1
    return text[start:i]


#: 允许裹在列名外面的函数 —— **白名单**,不是 `\w+\(`。
#: `count(status)` / `max(status)` 也是"列名外面裹了个函数",但它们不是**这一行的
#: 状态判断**;把它们当成比较会让闸对聚合查询乱报红,而报红的闸最后都会被人关掉。
_CMP_WRAPPERS = r"COALESCE|LOWER|UPPER|BTRIM|TRIM|NULLIF"


def _cmp_operand(column: str) -> str:
    r"""能指代 `<column>` 这一列的**表达式**形状(不含比较运算符)。

    覆盖 R6 ② 点名的几种包裹:`LOWER(...)` 等函数、裸括号 `(col)`、
    `::text` 显式转型、`COALESCE(col, '')` 的第二参数。
    """
    col = r"(?:\w+\.)?\b" + column + r"\b(?:\s*::\s*\w+)?"
    return (
        r"(?:"
        # ① 没有包裹:列名直接参与比较。**不允许**尾随右括号 —— 见下。
        + col
        + r"|"
        # ② 有包裹:开括号必须来自白名单函数,或是一个**前面不是标识符**的裸括号。
        #
        #    🔴 承重的是**开括号那一侧**的两件事,变异实验逐个证过:
        #      · `{1,3}`(必须真有开括号)—— 退成 `{0,3}` 就允许"没开却闭",
        #        `COUNT(rx.status) = 'success'` 当场命中(M73);
        #      · `(?<![\w.])`(裸括号前面不许是标识符)—— 去掉它同样让 COUNT 命中(M84)。
        #
        #    🔴 **闭括号那一侧的 `{1,3}` 不承重**:把它放成 `{0,3}`,LOWER / 裸括号 /
        #       COUNT 三种输入的判定一个都不变(第一轮 M73 就是这么打的,**存活**)。
        #       留着它是为了写清"成对"的意图,但别把它当防线 —— 判据够不到的装饰
        #       不该冒充防线(R5 M64 同一课)。
        + r"(?:(?:" + _CMP_WRAPPERS + r")\s*\(\s*|(?<![\w.])\(\s*){1,3}"
        + col
        + r"(?:\s*,\s*'[^']*')?"                                 # COALESCE 第二参
        + r"(?:\s*\)){1,3}"
        + r")"
    )


def _cmp_shape(column: str, literal: str) -> "re.Pattern[str]":
    r"""「这段 SQL 在断言 `<column>` 等于 `<literal>`」的形状。

    🔴 R4 §E 的第二半。把臂截准了还不够 —— 探测用的正则本身也只认裸列名:
       `COALESCE(rx.status, '') = 'success'` 里 `.status` 后面跟的是 `,` 不是 `=`,
       原来的 `\.status\s*=\s*'success'` 一个都匹配不上。
       两个方向都会坏,而且**坏法相反**:
         · `_RAW_SUCCESS` 认不出 → 臂被判成"没在断言发布" → **漏报**(闸失效);
         · `_VERIFIED_GATE` 认不出 → 带闸的臂被判成没闸 → **误报**(闸不可留)。
       所以两条必须共用同一个形状构造器,不能只修其中一条。

    🔴 R6 ②:SQL 里"等于某个字面量"有六七种等价写法,而闸原来只认最直白的那一种。
       一个盲区 = 一条**换个写法就隐形**的路 —— 对漏报方向尤其致命:
       写成 `rx.status IN ('success')` 的臂原本连分母都进不去。补齐的形态:

       | 写法 | 例 |
       |---|---|
       | 反写 | `'success' = rx.status` |
       | `IN` | `rx.status IN ('success', 'partial')` |
       | 转型 | `rx.status::text = 'success'` |
       | 裸括号 | `(rx.status) = 'success'` |
       | `ANY(ARRAY)` | `rx.status = ANY(ARRAY['success'])` |
       | 大小写归一 | `LOWER(rx.status) = 'success'` |

    🔴 **否定形态刻意不认**:`NOT IN` / `<>` / `!=` 说的是相反的话。
       把它们认成"在断言发布"是漏报方向的噪声,认成"带闸"则是**假绿** ——
       `state NOT IN ('verified')` 会变成一道反着开的闸。所以 `\bIN\b` 前面
       只允许空白:`status NOT IN (…)` 里 `\s*` 跨不过 `NOT`,自然不匹配。
    """
    operand = _cmp_operand(column)
    lit = r"'" + literal + r"'"
    return re.compile(
        r"(?:"
        + operand + r"\s*=\s*" + lit                                     # col = 'x'
        + r"|" + operand + r"\s*=\s*ANY\s*\(\s*ARRAY\s*\[[^\]]*" + lit   # = ANY(ARRAY[…])
        + r"|" + operand + r"\s*\bIN\s*\([^)]*" + lit                    # IN (…)
        + r"|" + lit + r"\s*=\s*" + operand                              # 'x' = col
        + r")",
        re.IGNORECASE,
    )


_VERIFIED_GATE = _cmp_shape("public_url_verification_state", "verified")
_RAW_SUCCESS = _cmp_shape("status", "success")
_DEMOTED_SOURCE_BIT = re.compile(
    r"public_url_reported_explicitly\s+IS\s+TRUE", re.IGNORECASE)


def _enclosing_function(path: Path, line: int) -> str:
    """行号 → 最内层函数名(仅 .py;其余回 `<file>`)。

    🔴 用 AST 而不是"往上找最近的 `def`":装饰器、嵌套函数、字符串里的 `def`
       都会让文本法给出错的归属,而归属错了豁免就会张冠李戴。
    """
    if path.suffix != ".py":
        return "<file>"
    key = str(path)
    spans = _FN_SPAN_CACHE.get(key)
    if spans is None:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            spans = []
        else:
            spans = [(n.lineno, getattr(n, "end_lineno", n.lineno), n.name)
                     for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            spans.sort(key=lambda sp: (sp[1] - sp[0]))   # 最内层优先
        _FN_SPAN_CACHE[key] = spans
    for lo, hi, name in spans:
        if lo <= line <= hi:
            return name
    return "<module>"


_FN_SPAN_CACHE: dict[str, list] = {}


def _scan_arms(root: Path) -> list[dict]:
    """扫描器**看见的全部臂**(不只是违规的那些),每条带上归属函数与豁免结论。

    🔴 R4:`_scan_unguarded_arms(...) == []` 只有在分母非空时才是个结论。
       分母为 0 的绿与"全都合规"的绿在断言里长得一模一样,而本仓已经栽过
       好几次零分母绿 —— 所以把分母单独暴露出来,让判据能直接钉它。

    🔴 R5 ④:豁免从"整文件跳过"改成**逐臂判定**。整文件跳过有两个坏处:
       ① 分母里根本看不见那些臂(4523 行的文件整体消失);
       ② 以后往那个文件新写的臂自动免检。现在所有臂都进分母,
          只是违规臂会带上 `exempt_reason`。
    """
    seen: list[dict] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in (".py", ".sql") or not path.is_file():
            continue
        parts = set(path.parts)
        if {".git", "node_modules", "__pycache__", "frontend"} & parts:
            continue
        if "prod_schema" in path.name:
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith("tests/"):
            continue
        raw_text = path.read_text(encoding="utf-8", errors="ignore")
        if "publish_records" not in raw_text:
            continue
        text = _blank_out_comments(raw_text)
        assert len(text) == len(raw_text)
        for m in _OCCURRENCE_START.finditer(text):
            arm = m.group(0) + _arm_body(text, m.end())
            line = text[:m.start()].count(chr(10)) + 1
            fn = _enclosing_function(path, line)
            reason = OCCURRENCE_SCAN_EXEMPT.get((rel, fn))
            if reason is None:
                reason = OCCURRENCE_SCAN_WHOLE_FILE_EXEMPT.get(rel)
            seen.append({
                "where": f"{rel}:{line}",
                "file": rel,
                "function": fn,
                "asserts_publication": bool(_RAW_SUCCESS.search(arm))
                or bool(_DEMOTED_SOURCE_BIT.search(arm)),
                "guarded": bool(_VERIFIED_GATE.search(arm)),
                "exempt_reason": reason,
            })
    return seen


def _violating_arms(root: Path) -> list[dict]:
    """在断言发布、没带闸、**且没有被逐条论证豁免**的臂。"""
    return [a for a in _scan_arms(root)
            if a["asserts_publication"] and not a["guarded"]
            and not a["exempt_reason"]]


def _scan_unguarded_arms(root: Path) -> list[str]:
    return [a["where"] for a in _violating_arms(root)]


def test_cross_package_gate_has_a_real_denominator():
    """🔴 分母自证:这道闸在本仓**确实审视到了**若干条真臂,并且其中有
    「在断言发布」的那一类 —— 否则 `unguarded == []` 只是零分母的绿。

    (R4 复查时这条差点白写:改进检测形状之前,5 条真臂里只有 1 条被认出
     `asserts_publication`,另外 3 条因为"无别名"或"注释里的分号截断臂身"而隐形。
     隐形的臂今天恰好都带闸,所以没出事 —— 但闸对它们本就没起作用。)
    """
    arms = _scan_arms(ROOT)
    # 🔴 R5 ④ 之后分母大了一截:豁免文件不再整体跳过,里面的臂也进分母了。
    # 🔴 [WO_273 · 2026-09-23] 地板 20 → 16。插件后端两个文件随退役删除,带走的臂逐文件对账:
    #    插件后端 10 条 + 插件死写入模块 4 条 = 14 条(基线 01cf3a9e2 实测 30 条 → 删后 16 条;
    #    其余 8 个文件的臂数一条没变,在断言发布的臂 9 → 8)。少的是**被扫的代码**,不是扫描器的牙。
    #    地板取删后的**精确值**:此后再少一条都得有人像这样逐文件对账,而不是悄悄变窄。
    assert len(arms) >= 16, f"扫描器只看见 {len(arms)} 条臂,分母太小,绿没有意义"
    asserting = [a for a in arms if a["asserts_publication"]]
    assert len(asserting) >= 3, (
        "「在断言发布」的臂少于 3 条 —— 检测形状可能又退化了(无别名/注释截断/"
        f"COALESCE/JOIN/派生表 都属这一类)。当前:{arms}")
    # 在断言发布的臂,要么带闸,要么**被逐条论证豁免** —— 没有第三种。
    for a in asserting:
        assert a["guarded"] or a["exempt_reason"], (
            f"这条臂在断言发布,既没带核实闸也没有豁免理由:{a}")


def test_no_python_file_is_exempt_as_a_whole():
    """🔴 R5 ④:Python 文件不许整文件豁免。

    整文件豁免的实际含义是"这个文件以后新写的任何臂都自动免检" ——
    `api/meijiehezi_api.py` 4523 行整体免检时,里面 5 条臂只有 1 条被真正论证过。
    """
    py_whole = [f for f in OCCURRENCE_SCAN_WHOLE_FILE_EXEMPT if f.endswith(".py")]
    assert py_whole == [], f"这些 Python 文件还在整文件豁免:{py_whole}"


def test_every_exemption_is_actually_used():
    """🔴 每条豁免必须**真的对上一条违规臂**,否则就是白开的后门。

    R5 ④ 复查时,原名单里 `services/publication_receipt_projection.py`、
    两个 .sql、一个校验脚本**一条臂都没有** —— 那几条豁免从来没起过作用,
    却给未来留着口子。这条锁让名单不许烂着。
    """
    arms = _scan_arms(ROOT)
    used = {(a["file"], a["function"]) for a in arms
            if a["asserts_publication"] and not a["guarded"]}
    declared = set(OCCURRENCE_SCAN_EXEMPT)
    dead = sorted(declared - used)
    assert not dead, (
        f"这些豁免没有对应任何违规臂(白开的后门,删掉):{dead}")


def test_every_exemption_names_a_function_that_exists():
    """豁免指的函数必须真实存在 —— 改名/删掉之后名单不许留着一条死条目。"""
    import ast as _ast

    for (rel, fn), reason in OCCURRENCE_SCAN_EXEMPT.items():
        path = ROOT / rel
        assert path.exists(), f"豁免指向的文件不存在:{rel}"
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        names = {n.name for n in _ast.walk(tree)
                 if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
        assert fn in names, f"{rel} 里没有函数 {fn}(豁免条目已过期)"
        assert len(reason) >= 30, f"({rel}, {fn}) 的豁免理由太短,不算论证:{reason!r}"


def test_exemption_is_scoped_to_the_function_not_the_file(tmp_path, monkeypatch):
    """🔴 "函数级"三个字的实质:同一文件里**没被豁免**的函数照样受闸。

    造一个只有两个函数的临时文件,把其中一个放进豁免名单,
    另一个的裸奔臂必须照样被抓到 —— 若豁免仍是整文件生效,这条当场红。
    """
    f = tmp_path / "services" / "two_funcs.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        "def blessed():\n"
        '    return """SELECT 1 FROM publish_records rx'
        " WHERE rx.status = 'success' )\"\"\"\n"
        "\n\n"
        "def not_blessed():\n"
        '    return """SELECT 1 FROM publish_records ry'
        " WHERE ry.status = 'success' )\"\"\"\n",
        encoding="utf-8")

    monkeypatch.setitem(OCCURRENCE_SCAN_EXEMPT,
                        ("services/two_funcs.py", "blessed"),
                        "临时树里的测试豁免:证明豁免只对这一个函数生效,理由字段够长。")
    hits = _scan_unguarded_arms(tmp_path)
    assert len(hits) == 1, f"期望只放行 blessed 那一条,实得:{hits}"
    assert hits[0].endswith(":6"), f"被抓到的应当是 not_blessed 里那条:{hits}"


def test_scanner_sees_arms_without_a_table_alias(tmp_path):
    """形状 (b):`FROM publish_records` 后**直接跟 WHERE**(生产里真有这种写法)。

    原实现把 `WHERE` 当成别名吃掉,再要求 `<别名>.列` 前缀 —— 裸列名一个都匹配不上,
    整条臂对闸隐形。这里给的是**不带闸**的版本,必须被抓到。
    """
    f = tmp_path / "services" / "no_alias.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT article_id FROM publish_records\n'
        " WHERE status='success' AND article_id IS NOT NULL )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) != [], "无别名的裸 success 臂没被抓到"


def test_scanner_accepts_a_guarded_arm_without_alias(tmp_path):
    """形状 (b) 的反向对照:同样无别名、但带闸的臂不许误报。"""
    f = tmp_path / "services" / "no_alias_ok.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT article_id FROM publish_records\n'
        " WHERE status='success'"
        " AND public_url_verification_state = 'verified' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) == []


def test_comment_with_semicolon_does_not_truncate_the_arm(tmp_path):
    """形状 (c):臂里夹一行含 `;` 的注释,臂身不许被截断。

    截断的后果是**漏报**:WHERE 整段没进臂身,闸看不到那条裸 success。
    """
    f = tmp_path / "services" / "commented_arm.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT 1 FROM publish_records p\n'
        "  -- 这行注释里有个分号;它不该把臂身切断\n"
        " WHERE p.status='success' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) != [], "注释里的分号把臂身截断了(漏报)"


def test_comment_cannot_forge_a_verified_gate(tmp_path):
    """反方向同样危险:**注释里**写一句核实闸,不许被当成真的闸。

    不剥注释的话,一条真正裸奔的臂只要旁边写句
    `-- p.public_url_verification_state = 'verified'` 就能骗过这道闸。
    """
    f = tmp_path / "services" / "fake_gate.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT 1 FROM publish_records p\n'
        "  -- p.public_url_verification_state = 'verified'\n"
        " WHERE p.status='success' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) != [], "注释假冒的闸骗过了扫描器"


def test_occurrence_scanner_catches_a_forged_unguarded_arm(tmp_path):
    """判别力自证:塞一条裸 success 臂进临时树,扫描器必须抓到。"""
    f = tmp_path / "services" / "sneaky_occurrence.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT 1 FROM publish_records rx '
        "WHERE rx.article_id = a.id AND rx.status = 'success' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) != [], "裸 success 臂没被抓到"


def test_occurrence_scanner_accepts_a_guarded_arm(tmp_path):
    """反向对照:带 verified 闸的臂**不许**误报,否则这道闸没人敢留着。"""
    f = tmp_path / "services" / "ok_occurrence.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT 1 FROM publish_records rx '
        "WHERE rx.article_id = a.id AND rx.status = 'success' "
        "AND rx.public_url_verification_state = 'verified' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) == []


@pytest.mark.parametrize("sql,raw_hit,gate_hit", [
    # 裸形状
    ("rx.status = 'success'", True, False),
    ("rx.public_url_verification_state = 'verified'", False, True),
    # COALESCE 形状(R3 §⑤ 之后的现役写法)
    ("COALESCE(rx.status, '') = 'success'", True, False),
    ("COALESCE(rx.public_url_verification_state, '') = 'verified'", False, True),
    # 🔴 反向对照:形状像但语义不同的,一个都不许命中 —— 否则"能匹配"只是因为
    #    这两条正则太宽,那样的闸会把好代码也报红,最终被人关掉。
    ("rx.status = 'failed'", False, False),
    ("rx.public_url_verification_state = 'pending'", False, False),
    ("rx.substatus = 'success'", False, False),
    # 🔴 R6 ② 的反向对照,这一条是本文件自己抓到的实伤:放宽包裹时先写成
    #    「开括号 0-3 个、闭括号 0-3 个」,于是"没开却闭"的 `COUNT(rx.status)`
    #    也命中了。聚合不是这一行的状态判断,认它 = 对聚合查询乱报红。
    ("COUNT(rx.status) = 'success'", False, False),
    ("MAX(rx.public_url_verification_state) = 'verified'", False, False),
    # 否定形态:两个方向都不许认(详见 test_negated_predicates_are_not_read_as_assertions)
    ("rx.status NOT IN ('success')", False, False),
    ("rx.status <> 'success'", False, False),
    ("rx.status != 'success'", False, False),
    ("rx.public_url_verification_state <> 'verified'", False, False),
])
def test_cmp_shape_matches_both_bare_and_coalesce(sql, raw_hit, gate_hit):
    assert bool(_RAW_SUCCESS.search(sql)) is raw_hit, sql
    assert bool(_VERIFIED_GATE.search(sql)) is gate_hit, sql


def test_occurrence_scanner_catches_a_coalesce_shaped_unguarded_arm(tmp_path):
    """🔴 R4 §E 的正样本:**COALESCE 开头的臂**必须被抓到。

    这是原实现的盲区 —— 非贪婪 body 截断在 `COALESCE(rx.status, '` 的右括号处,
    `= 'success'` 落在臂外,于是整条臂被当成"没在断言发布"而跳过。
    R3 §⑤ 把本仓同类谓词全改成了这个形状,所以这不是假想的形状,是**现役**形状。
    """
    f = tmp_path / "services" / "coalesce_occurrence.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT 1 FROM publish_records rx '
        "WHERE rx.article_id = a.id "
        "AND COALESCE(rx.status, '') = 'success' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) != [], (
        "COALESCE 形状的裸 success 臂没被抓到 —— 闸对现役写法零判别力")


def test_occurrence_scanner_accepts_a_guarded_coalesce_arm(tmp_path):
    """反向对照:同为 COALESCE 形状、但**带核实闸**的臂不许误报。

    没有这一臂,上面那条可能只是"凡见 COALESCE 就报",那种闸没人敢留。
    """
    f = tmp_path / "services" / "ok_coalesce.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT 1 FROM publish_records rx '
        "WHERE rx.article_id = a.id "
        "AND COALESCE(rx.status, '') = 'success' "
        "AND COALESCE(rx.public_url_verification_state, '') = 'verified' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) == []


def test_arm_body_stops_at_its_own_closing_paren(tmp_path):
    """臂的边界要**准**:既不能早停(漏掉后半条),也不能晚停(吃进下一条臂)。

    早停 = R4 §E 那个洞;晚停同样危险 —— 把**下一条**臂的 verified 闸吃进来,
    就会把一条真正裸奔的臂判成"有闸",方向反过来的同一个假绿。
    """
    text = ("SELECT 1 FROM publish_records rx WHERE COALESCE(rx.status, '') = 'success')"
            " AND EXISTS (SELECT 1 FROM publish_records ry "
            "WHERE COALESCE(ry.public_url_verification_state, '') = 'verified')")
    starts = [m for m in _OCCURRENCE_START.finditer(text)]
    assert len(starts) == 2
    first = _arm_body(text, starts[0].end())
    assert "= 'success'" in first, "早停:后半条被切掉了(R4 §E 的洞)"
    assert "verified" not in first, "晚停:把下一条臂的闸吃进来了"


def test_scanner_catches_an_unguarded_join_arm(tmp_path):
    """🔴 R5 ① 正样本:`JOIN publish_records p ON …` 的裸奔臂必须被抓到。

    起点只认 `FROM` 时,一条 JOIN 进来的臂照样能拿 `p.status='success'`
    当发布事实,而闸**完全看不见它**。
    """
    f = tmp_path / "services" / "join_arm.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT a.id FROM articles a'
        " JOIN publish_records p ON p.article_id = a.id"
        " WHERE p.status = 'success' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) != [], "JOIN 形态的裸奔臂没被抓到"


@pytest.mark.parametrize("join_sql", [
    "LEFT JOIN publish_records p ON p.article_id = a.id",
    "INNER JOIN publish_records p ON p.article_id = a.id",
    "LEFT OUTER JOIN publish_records p ON p.article_id = a.id",
])
def test_scanner_catches_every_join_flavour(tmp_path, join_sql):
    """各种 JOIN 写法逐个钉 —— 只测裸 `JOIN` 会让 `LEFT JOIN` 继续隐形。"""
    f = tmp_path / "services" / "join_flavour.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT a.id FROM articles a ' + join_sql
        + " WHERE p.status = 'success' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) != [], f"{join_sql} 没被抓到"


def test_scanner_accepts_a_guarded_join_arm(tmp_path):
    """反向对照:带闸的 JOIN 臂不许误报,否则这道闸没人敢留。"""
    f = tmp_path / "services" / "join_ok.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT a.id FROM articles a'
        " JOIN publish_records p ON p.article_id = a.id"
        " WHERE p.status = 'success'"
        " AND p.public_url_verification_state = 'verified' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) == []


def test_scanner_catches_a_schema_qualified_arm(tmp_path):
    """🔴 R5 ① 正样本之二:`public.publish_records` 的裸奔臂必须被抓到。"""
    f = tmp_path / "services" / "schema_qualified.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT 1 FROM public.publish_records rx'
        " WHERE rx.status = 'success' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) != [], "schema 限定写法没被抓到"


def test_block_comment_cannot_forge_a_verified_gate(tmp_path):
    """🔴 R5 ② 正样本:**块注释**里伪造核实谓词,不许骗过闸。

    与 R4 修的行注释假冒是同一个洞,只是换了 `/* … */` 这种语法。
    """
    f = tmp_path / "services" / "block_comment.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT 1 FROM publish_records rx'
        " /* rx.public_url_verification_state = 'verified' */"
        " WHERE rx.status = 'success' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) != [], "块注释假冒的闸骗过了扫描器"


def test_block_comment_does_not_truncate_a_real_arm(tmp_path):
    """反方向:块注释里若含 `;` 或 `)`,不许把真臂截断(那是漏报)。"""
    f = tmp_path / "services" / "block_comment_trunc.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT 1 FROM publish_records rx'
        " /* 说明:这段注释里有分号;还有右括号) */"
        " WHERE rx.status = 'success' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) != [], "块注释里的分号/括号把臂身截断了"


def test_scanner_catches_an_unguarded_derived_table_arm(tmp_path):
    """🔴 R5 ③ 正样本:派生表形态 —— 臂的起点在子查询里,闸在子查询外。

    见第一个 `)` 就停会把臂身截成空,整条臂对闸隐形。
    """
    f = tmp_path / "services" / "derived.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT x.article_id FROM'
        " (SELECT article_id, status FROM publish_records) x"
        " WHERE x.status = 'success' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) != [], "派生表形态的裸奔臂没被抓到"


def test_scanner_accepts_a_guarded_derived_table_arm(tmp_path):
    """反向对照:派生表外面带闸的,不许误报。"""
    f = tmp_path / "services" / "derived_ok.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT x.article_id FROM'
        " (SELECT article_id, status, public_url_verification_state"
        "  FROM publish_records) x"
        " WHERE x.status = 'success'"
        " AND x.public_url_verification_state = 'verified' )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) == []


def test_derived_table_scan_still_stops_at_a_sibling_exists(tmp_path):
    """🔴 ③ 的**晚停**方向:爬出派生表不等于可以一直扫下去。

    两条并列的 `EXISTS ( … )`,第一条裸奔、第二条带闸 —— 若臂身越界吃进第二条的闸,
    第一条就会被判成"有闸"(方向相反的同一种假绿)。
    """
    text = ("SELECT 1 FROM publish_records rx WHERE rx.status = 'success')"
            " AND EXISTS (SELECT 1 FROM publish_records ry"
            " WHERE ry.public_url_verification_state = 'verified')")
    starts = list(_OCCURRENCE_START.finditer(text))
    assert len(starts) == 2
    first = starts[0].group(0) + _arm_body(text, starts[0].end())
    assert "= 'success'" in first
    assert "verified" not in first, "臂身越界吃进了下一条臂的闸"


def test_occurrence_scanner_catches_demoted_source_bit(tmp_path):
    """来源位当证据也要抓 —— 图文包 body proof 正是这个形状。"""
    f = tmp_path / "services" / "sneaky_proof.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'SQL = """SELECT r.id FROM publish_records r '
        "WHERE r.submitted_content_snapshot_hash IS NOT NULL "
        "AND r.public_url_reported_explicitly IS TRUE )\"\"\"\n",
        encoding="utf-8")
    assert _scan_unguarded_arms(tmp_path) != [], "来源位当证据没被抓到"


def test_exported_gates_are_the_one_line_fix():
    """导出的口径必须真能直接拼进 SQL(不是摆设)。"""
    from services.publication_receipt_projection import (
        SELF_REPORT_BODY_PROOF_SQL, SELF_REPORT_OCCURRENCE_SQL,
    )

    occ = SELF_REPORT_OCCURRENCE_SQL.format(r="rx")
    proof = SELF_REPORT_BODY_PROOF_SQL.format(r="rx")
    # 🔴 R3 改断言(不是退役):R3 §⑤ 给两条谓词加了 COALESCE(NULL 档),原来那句
    #    逐字匹配失效。守的东西没变 —— 「核实态必须出现在这条谓词里」——
    #    所以放宽到"引用了那一列且比的是 verified",而不是把这条锁删掉。
    for frag in (occ, proof):
        assert "rx.public_url_verification_state" in frag
        assert "= 'verified'" in frag
        # NULL 档不许再退回三值逻辑(R3 §⑤ 的洞)
        assert "COALESCE(rx.public_url_verification_state, '')" in frag
    assert "public_url_reported_explicitly" not in proof


# ===========================================================================
# R6 ② · 等价谓词形态:换个写法不许让臂隐形
# ===========================================================================
#
# 🔴 这一组守的是**漏报**方向。闸原来只认 `col = 'lit'` 一种写法,而 SQL 里
#    "等于某个字面量"至少有六种等价说法。任何一种没被认出来 =
#    那条臂 `asserts_publication` 判成 False → **整条臂被跳过**,连分母都进不去。
#    闸对着一条隐形的臂报绿,比没有闸更坏 —— 没有闸至少没人以为被守住了。
#
# 每种形态两条判据:裸奔的必须红(正样本)、带闸的不许误报(反向对照)。
# 反向对照不是凑数:只有正样本时,"凡见 IN 就报红"这种零判别力实现也能全绿,
# 而那样的闸上线第一天就会被人关掉。

#: (id, 断言"发布成功"的谓词, 同形态写出来的核实闸)
_EQUIVALENT_PREDICATE_FORMS = [
    ("reversed",  "'success' = rx.status",
                  "'verified' = rx.public_url_verification_state"),
    ("in_list",   "rx.status IN ('success', 'partial')",
                  "rx.public_url_verification_state IN ('verified')"),
    ("cast",      "rx.status::text = 'success'",
                  "rx.public_url_verification_state::text = 'verified'"),
    ("parens",    "(rx.status) = 'success'",
                  "(rx.public_url_verification_state) = 'verified'"),
    ("any_array", "rx.status = ANY(ARRAY['success'])",
                  "rx.public_url_verification_state = ANY(ARRAY['verified'])"),
    ("lower",     "LOWER(rx.status) = 'success'",
                  "LOWER(rx.public_url_verification_state) = 'verified'"),
]


def _write_arm(tmp_path, name: str, where_sql: str):
    f = tmp_path / "services" / f"{name}.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        'SQL = """SELECT 1 FROM publish_records rx '
        "WHERE rx.article_id = a.id "
        f"AND {where_sql} )\"\"\"\n",
        encoding="utf-8")
    return f


@pytest.mark.parametrize(
    "form_id,raw_pred,_gate_pred", _EQUIVALENT_PREDICATE_FORMS,
    ids=[f[0] for f in _EQUIVALENT_PREDICATE_FORMS])
def test_scanner_catches_every_equivalent_success_form(
        tmp_path, form_id, raw_pred, _gate_pred):
    """正样本:六种等价写法的**裸奔**臂,每一种都必须被抓到。"""
    _write_arm(tmp_path, f"raw_{form_id}", raw_pred)
    assert _scan_unguarded_arms(tmp_path) != [], (
        f"`{raw_pred}` 这种写法的裸奔臂没被抓到 —— 换个写法就能绕过闸")


@pytest.mark.parametrize(
    "form_id,raw_pred,gate_pred", _EQUIVALENT_PREDICATE_FORMS,
    ids=[f[0] for f in _EQUIVALENT_PREDICATE_FORMS])
def test_scanner_accepts_every_equivalent_verified_gate(
        tmp_path, form_id, raw_pred, gate_pred):
    """反向对照:同一种写法**带闸**时不许误报。

    闸也可能被写成这六种形态中的任意一种。认不出来 = 把守规矩的代码判红,
    而报假红的闸的下场只有一个:被人关掉。
    """
    _write_arm(tmp_path, f"ok_{form_id}", f"{raw_pred} AND {gate_pred}")
    assert _scan_unguarded_arms(tmp_path) == [], (
        f"带闸(`{gate_pred}`)的臂被误报成裸奔")


def test_negated_predicates_are_not_read_as_assertions(tmp_path):
    """🔴 否定形态说的是相反的话,两个方向都不许认。

    · `status NOT IN ('success')` 认成"在断言发布" = 噪声;
    · `state NOT IN ('verified')` 认成"带闸" = **假绿**,一道反着开的闸。
    第二个方向才是真正贵的,所以这条判据同时钉两边。
    """
    assert not _RAW_SUCCESS.search("rx.status NOT IN ('success')")
    assert not _VERIFIED_GATE.search(
        "rx.public_url_verification_state NOT IN ('verified')")
    # 实质面:一条裸奔臂,后面跟一个**否定**的核实谓词,仍然必须被抓到
    _write_arm(tmp_path, "negated_gate",
               "rx.status = 'success' "
               "AND rx.public_url_verification_state NOT IN ('verified')")
    assert _scan_unguarded_arms(tmp_path) != [], (
        "反着开的闸被当成了闸")
