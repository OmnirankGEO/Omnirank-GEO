"""#104 · 发布事实源 `media_publications` 必须有**生产写入方**,而且**够得着**。

根因(全部仓内证据):真实发布走 mhz 代发 → `article_publication_snapshot` 只更新
`articles.first_published_at`(加速列),**不写事实源**;唯一会写事实源的
`publication_facts.record_manual_publication` 只被「代理手动确认」那个 API 调,
而自动写入的两处在 2026-05-25 被有意移除(注释逐字:「取消旧 add_publication
垃圾写入(quote_id=0 + 空字段)」「是双写」)。
于是 schema 注释说「加速列派生自事实源」,实际**恰好相反**:
加速列是唯一的真实记录,被声明为事实源的那张表建表以来零插入。

🔴 本文件的**主交付物是两把锁,不是数据修好了**:
  ① 存在锁:事实源必须有生产写入方;
  ② **可达性锁**:那个写入方必须被真实发布链够得着。
     #104 修之前**通得过①**(`db/monitoring_db.py:8181 add_publication` 一直在),
     却仍然是空的 —— 因为它被绕开了。**只锁「存在」会给出绿灯而问题照旧。**
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
FACT_TABLE = "media_publications"

#: 不算数的写入点:判据自己的夹具、一次性校验脚本。
#: 🔴 分母排除项**冻结在这里**,新增要人显式改 —— 不让门自动放行。
_NOT_PRODUCTION = ("tests/", "scripts/validate_")


def _production_insert_sites() -> list[tuple[str, int]]:
    """机械枚举全仓写事实源的生产代码点(不手写清单)。"""
    out = []
    skip = {"node_modules", ".git", "frontend", ".venv", "_qa_frontend_nogo_artifacts"}
    for p in ROOT.rglob("*.py"):
        if any(s in p.parts for s in skip):
            continue
        rel = p.relative_to(ROOT).as_posix()
        if any(rel.startswith(x) or ("/" + x) in rel for x in _NOT_PRODUCTION):
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if "INSERT INTO " + FACT_TABLE not in src:
            continue
        for i, ln in enumerate(src.splitlines(), 1):
            if "INSERT INTO " + FACT_TABLE in ln:
                out.append((rel, i))
    return out


def test_fact_source_has_at_least_one_production_writer():
    """① 存在锁。"""
    sites = _production_insert_sites()
    assert sites, (
        FACT_TABLE + " 没有任何生产写入方 —— 读取方在等一张没人写的表。"
        "这正是 #63/#104 两次事故的形状。")


def test_the_real_publishing_path_reaches_a_writer():
    """② 🔴 可达性锁 —— 本文件的核心。

    真实发布的必经点是 `article_publication_snapshot.capture_publication_snapshot_with_cursor`
    (mhz 两处 / publish_db / publication_url_verifier 都汇到它)。
    它**必须**在捕获成功后调到写事实源的那一步。

    只断言「某处存在 INSERT」不够:#104 修之前那个 INSERT 一直在
    (`db/monitoring_db.py:8181`),而真实发布链根本走不到它,表照样是空的。
    """
    src = (ROOT / "services" / "article_publication_snapshot.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "capture_publication_snapshot_with_cursor"), None)
    assert fn is not None, "必经点函数不见了 —— 分母塌了"
    called = {getattr(c.func, "id", getattr(c.func, "attr", None))
              for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "_record_publication_fact" in called, (
        "真实发布的必经点没有调用写事实源那一步 —— 表会继续是空的,"
        "而**不会有任何东西报错**(这正是它躺了这么久没被发现的原因)")


def test_the_writer_uses_the_caller_cursor_not_its_own_connection():
    """🔴 同一事务:写事实源必须用调用方的 cursor,不许自开连接。

    本函数在别人的事务里跑(装配/发布链深处),自开连接 = 第二个事务 ——
    08-10 那次把生产打成 503 就是这么来的。
    """
    src = (ROOT / "services" / "article_publication_snapshot.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_record_publication_fact")
    seg = ast.get_source_segment(src, fn) or ""
    for forbidden in ("get_connection(", "get_db_conn(", "psycopg2.connect("):
        assert forbidden not in seg, "写事实源时自开了连接:" + forbidden


def test_incomplete_fields_write_nothing():
    """🔁 反臂:字段不齐**不写**。

    当年移除自动写入的理由是「垃圾写入(quote_id=0 + 空字段)」。
    修回来时若不守这条,等于把当年那批垃圾行重新造出来 ——
    而那会让事实源"有行了"却全是废数据,比空着更难发现。
    """
    from services.article_publication_snapshot import _record_publication_fact

    class _Boom:
        def execute(self, *a, **k):
            raise AssertionError("字段不齐却发了 SQL")
        def fetchone(self):
            raise AssertionError("字段不齐却查了库")

    for kw in (
        dict(quote_id=None, platform_name="X", platform_url="u"),
        dict(quote_id=0, platform_name="X", platform_url="u"),
        dict(quote_id=7, platform_name="", platform_url="u"),
        dict(quote_id=7, platform_name="X", platform_url=""),
    ):
        assert _record_publication_fact(
            _Boom(), article_id=1, article_title="t",
            publish_date="2026-09-05T00:00:00", operator_id="src", **kw) is False, kw


def test_complete_fields_insert_once_and_dedupe_on_retry():
    """🔁 正样本臂 + 幂等:字段齐 ⇒ 插一行;重放 ⇒ 不再插。

    没有正样本臂,上面那条反臂全绿也可能是因为**这个函数永远返回 False**
    (功能整个没接上)—— 两者在反臂读数上完全同形。
    """
    from services.article_publication_snapshot import _record_publication_fact

    class _Cur:
        def __init__(self, existing):
            self.existing = existing
            self.inserts = 0
            self._last = None
        def execute(self, sql, params=None):
            self._last = sql
            if "INSERT INTO media_publications" in sql:
                self.inserts += 1
        def fetchone(self):
            return {"id": 1} if self.existing else None

    fresh = _Cur(existing=False)
    assert _record_publication_fact(
        fresh, quote_id=7, article_id=3, article_title="t",
        platform_name="小红书", platform_url="https://x/1",
        publish_date="2026-09-05T00:00:00", operator_id="mhz") is True
    assert fresh.inserts == 1

    again = _Cur(existing=True)
    assert _record_publication_fact(
        again, quote_id=7, article_id=3, article_title="t",
        platform_name="小红书", platform_url="https://x/1",
        publish_date="2026-09-05T00:00:00", operator_id="mhz") is False
    assert again.inserts == 0, "重放又插了一行 —— 去重没生效"


def test_mhz_path_supplies_platform_and_url():
    """🔁 mhz 那一层必须把平台与 URL 传下来。

    被委派的函数是**以文章为中心**的,它不知道发到了哪儿;不传就等于字段不齐,
    helper 会静默跳过 —— 表继续是空的,而所有断言都还是绿的。
    """
    src = (ROOT / "services" / "article_publication_snapshot.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef)
              and n.name == "capture_mhz_publication_snapshot_with_cursor")
    seg = ast.get_source_segment(src, fn) or ""
    assert "platform_name=" in seg and "platform_url=" in seg, "mhz 层没透传平台/URL"
    assert "media_name" in seg and "publish_url" in seg, "mhz 层没从 item 取平台/URL"
