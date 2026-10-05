"""锁:SQL 里禁止拿 PostgreSQL 保留字当未加引号的表别名。

为什么要这把锁(2026-08-01 真实事故):
    37c49950(2026-07-19)在 flywheel_bridge._load_round_raw_page 里把 LATERAL 子查询
    的别名写成了 `) fetch ON TRUE`。FETCH 是 PostgreSQL 保留字,未加引号做别名会让
    **整条 SQL 在解析期就炸**(syntax error at or near "fetch")。
    该 stage 外层是 _safe_stage fail-soft,只把 status 记成 failed 就放过 → 轮照样
    completed、面板照常 200 → geo_research_source_signals 断供 14 天无人察觉。

    注意:tests/flywheel_integration/test_flywheel_bridge.py 本来就能红(它真连库跑
    _stage_source_signals)。那次上线是**没跑测试**。本锁的增量价值是:不连库、不依赖
    执行到那一行,纯静态全仓扫,commit 期即可拦。

覆盖边界(诚实声明,不吹):
    只扫 **AST 抽出来的字符串字面量**里的三种别名位 ——
      1) `) alias ON TRUE`(LATERAL,事故原样)
      2) `) AS alias`
      3) `FROM/JOIN <表> <alias>`
    动态拼接的 SQL(f-string 变量段、% 拼接、ORM 生成)扫不到;这是已知缺口,
    不是"已全覆盖"。第一版曾对整文件正文跑正则 → 18 处误报(把散文和 Python 语句
    当成 SQL),改 AST 抽取后归零。

判别力:见 test_scanner_has_discriminating_power —— 「必须命中」与「必须不命中」
成对断言,防止扫描器退化成恒真/恒假。
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# 只扫真正会跑 SQL 的目录;tests/ 与 docs/ 排除(它们会「描述」这个模式本身)
SCAN_DIRS = ("services", "db", "api", "tools", "writing", "workflows", "agents", "scripts")

# PostgreSQL 16 保留字(pg_get_keywords() 里 catcode='R')。
# test_reserved_list_matches_postgres 会在测试库可用时与真库互校,防快照漂移。
PG_RESERVED: frozenset[str] = frozenset("""
all analyse analyze and any array as asc asymmetric both case cast check collate column
constraint create current_catalog current_date current_role current_time current_timestamp
current_user default deferrable desc distinct do else end except false fetch for foreign
from grant group having in initially intersect into lateral leading limit localtime
localtimestamp not null offset on only or order placing primary references returning select
session_user some symmetric system_user table then to trailing true union unique user using
variadic when where window with
""".split())

# 别名位三种形态
_LATERAL_ALIAS = re.compile(r"\)\s*([A-Za-z_][A-Za-z0-9_]*)\s+ON\s+TRUE", re.IGNORECASE)
_AS_ALIAS = re.compile(r"\)\s*AS\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_TABLE_ALIAS = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.]*)\s+(?!AS\b)([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)

# 出现在「别名位」但其实是子句续写的词 —— 不是别名,跳过避免误报。
# LATERAL 形态由 _LATERAL_ALIAS 单独精确覆盖,不受此豁免影响。
_CLAUSE_FOLLOWERS = frozenset("""
where order group having limit on using left right inner outer full cross natural join
union except intersect returning set values window for offset fetch as and or not
""".split())


def _looks_like_sql(text: str) -> bool:
    if len(text) < 20:
        return False
    has_select = re.search(r"\bSELECT\b", text, re.IGNORECASE)
    has_source = re.search(r"\b(FROM|JOIN)\b", text, re.IGNORECASE)
    return bool(has_select and has_source)


def iter_sql_literals(source: str):
    """用 AST 抽出所有「看起来是 SQL」的字符串字面量,产出 (lineno, sql)。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _looks_like_sql(node.value):
                yield node.lineno, node.value


def find_reserved_aliases(sql_text: str) -> list[str]:
    """返回 sql_text 里被当成未加引号别名使用的保留字(小写去重,按出现序)。"""
    hits: list[str] = []

    def _add(word: str) -> None:
        w = word.lower()
        if w in PG_RESERVED and w not in hits:
            hits.append(w)

    for m in _LATERAL_ALIAS.finditer(sql_text):
        _add(m.group(1))
    for m in _AS_ALIAS.finditer(sql_text):
        _add(m.group(1))
    for m in _TABLE_ALIAS.finditer(sql_text):
        alias = m.group(2).lower()
        if alias in _CLAUSE_FOLLOWERS:
            continue
        _add(alias)
    return hits


def _iter_python_files():
    for d in SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            yield path


def scan_repo() -> list[str]:
    offenders: list[str] = []
    for path in _iter_python_files():
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, sql in iter_sql_literals(source):
            for word in find_reserved_aliases(sql):
                offenders.append(f"{rel}:{lineno} 别名 `{word}` 是 PostgreSQL 保留字")
    return offenders


def test_no_reserved_word_used_as_sql_alias():
    """全仓扫:任何 SQL 字面量都不许拿保留字当未加引号别名。"""
    offenders = scan_repo()
    assert not offenders, (
        "发现拿 PostgreSQL 保留字当未加引号 SQL 别名(解析期即报 syntax error):\n  "
        + "\n  ".join(offenders)
        + "\n改法:换个非保留字别名(如 af / fx),不要用双引号硬撑。"
    )


def test_scanner_has_discriminating_power():
    """判别力自证:必须命中 + 必须不命中 成对断言。

    只断言「坏样本被命中」→ 恒真扫描器也能过;
    只断言「好样本不被命中」→ 恒假扫描器也能过。两条都要。
    """
    # —— 必须命中:事故原样(37c49950 引入的那段)
    bad = """
        SELECT art.canonical_body_hash, fetch.id AS article_fetch_id
          FROM geo_research_raw raw
          LEFT JOIN LATERAL (
                SELECT f.id FROM geo_research_article_fetches f LIMIT 1
          ) fetch ON TRUE
    """
    assert "fetch" in find_reserved_aliases(bad), "扫描器漏掉真事故样本 = 恒假,无判别力"

    # —— 必须不命中:修复后的写法
    good = """
        SELECT art.canonical_body_hash, af.id AS article_fetch_id
          FROM geo_research_raw raw
          LEFT JOIN LATERAL (
                SELECT f.id FROM geo_research_article_fetches f LIMIT 1
          ) af ON TRUE
    """
    assert find_reserved_aliases(good) == [], "扫描器对合法写法误报 = 恒真,无判别力"

    # —— 不是只硬编码了 fetch
    assert "user" in find_reserved_aliases(") user ON TRUE")
    assert "window" in find_reserved_aliases(") window ON TRUE")
    # —— 非保留字别名不许误报
    assert find_reserved_aliases(") arc ON TRUE") == []
    assert find_reserved_aliases(") af ON TRUE") == []


def test_ast_extraction_filters_prose_not_sql():
    """AST 抽取必须只拿到 SQL 字面量 —— 反向对照第一版的 18 处误报。"""
    source = '''
from db.connection import get_connection   # 这行含 from,但不是 SQL

DOC = """业务说明:先 FROM articles 再 JOIN orders,然后 or 一下"""  # 散文,不含 SELECT

QUERY = """
    SELECT a.id FROM articles a
      LEFT JOIN LATERAL (SELECT 1) fetch ON TRUE
"""
'''
    literals = list(iter_sql_literals(source))
    # 必须命中:只抽到 1 条真 SQL
    assert len(literals) == 1, f"应只抽到 1 条 SQL,实际 {len(literals)} 条: {literals}"
    assert "fetch" in find_reserved_aliases(literals[0][1])
    # 必须不命中:散文那条没被当成 SQL
    assert all("业务说明" not in sql for _, sql in literals)


def test_reserved_list_matches_postgres():
    """与真库 pg_get_keywords() 互校:防内嵌快照与实际 PG 版本漂移。"""
    psycopg2 = pytest.importorskip("psycopg2")

    url = os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("无测试库连接串")
    try:
        conn = psycopg2.connect(url)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"测试库不可达: {type(exc).__name__}: {exc}")
    try:
        cur = conn.cursor()
        cur.execute("SELECT word FROM pg_get_keywords() WHERE catcode = 'R'")
        live = {str(r[0]).lower() for r in cur.fetchall()}
    finally:
        conn.close()

    # 反向对照:先证判据本身有效(真库确实返回非空且含 fetch),再断言无缺口
    assert live, "pg_get_keywords() 返回空集 = 判据失效"
    assert "fetch" in live, "pg_get_keywords() 未返回 fetch = 判据失效"
    missing = sorted(live - PG_RESERVED)
    assert not missing, f"PG_RESERVED 快照缺少真库保留字: {missing}"
