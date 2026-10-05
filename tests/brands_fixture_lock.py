"""[R5 ⑤] 「换回手搓即红」保真锁 —— 各目录共用的**同一个**谓词。

⑤ 要治的病:夹具自己 author `brands`,搓出来的表比生产窄(常见 2~8 列 vs 生产 32 列)。
比生产窄 ⇒ 针对那张表的断言测的是一张**不存在的表**,绿了也不算数;
比生产严(手搓的 NOT NULL / FK 生产没有)则会测出生产根本没有的约束。

治法:夹具改调 `db.brands_schema.ensure_brands_schema(cursor)`(生产 SSOT 出口),
或用它的纯 SQL 形态 `brands_schema_sql()`。本文件是配套的锁:谁改回手搓,当场红。

🔴 为什么锁写在一处、各目录只调用:
   六个目录抄六份「期望列清单」= 又造了六个会各自漂移的 builder,
   正是 ⑤ 自己要治的病。清单只有一个来源 —— `_BRANDS_SELF_HEAL_COLUMNS`。

🔴 为什么按 search_path 解析而不是写死 `table_schema='public'`:
   tests/pricing_quote_wiring 每个 pytest 进程用**自己的私有 schema**(安全栓的一部分,
   一个字不许动)。写死 public 会让那个目录的锁查到 0 行 —— 零分母、恒绿。
"""
from db.brands_schema import _BRANDS_SELF_HEAL_COLUMNS


def _scalar(row, key, idx=0):
    return row[key] if isinstance(row, dict) else row[idx]


def brands_columns(conn) -> set:
    """按 **search_path 实际解析到的** brands 取列名(不写死 public)。"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT a.attname AS column_name
          FROM pg_attribute a
         WHERE a.attrelid = to_regclass('brands')
           AND a.attnum > 0
           AND NOT a.attisdropped
        """
    )
    return {_scalar(r, "column_name") for r in cur.fetchall()}


def assert_brands_is_production_shaped(conn):
    """brands 必须是 SSOT 出口建出来的形状(不是夹具手搓的窄表)。

    手搓版典型 2~8 列 ⇒ 缺几十列 ⇒ 本断言当场红,并把缺的列名全打出来。
    """
    want = {c for c, _ in _BRANDS_SELF_HEAL_COLUMNS} | {"id", "name"}
    have = brands_columns(conn)
    assert have, "search_path 里根本没有 brands 表(或锁查错了 schema —— 零分母)"
    missing = sorted(want - have)
    assert not missing, (
        "brands 比生产**窄** %d 列:%s\n"
        "→ 这把锁是给「有人把夹具改回手搓 CREATE TABLE brands」准备的。"
        "夹具应当调 db.brands_schema.ensure_brands_schema(cursor)。"
        % (len(missing), missing))


def assert_brands_has_production_unique_index(conn):
    """连唯一性口径一起锁:生产是 (name, owner_user_id) WHERE is_deleted=false。

    手搓夹具要么没有唯一约束、要么长回早已废弃的全局 `name UNIQUE`;
    两种都会让「跨 owner 同名品牌」这类真实场景在测试里表现得和生产不一样。
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT pg_get_indexdef(i.indexrelid) AS indexdef
          FROM pg_index i
         WHERE i.indrelid = to_regclass('brands')
        """
    )
    defs = [_scalar(r, "indexdef") for r in cur.fetchall()]
    assert defs, "brands 一个索引都没有(连主键都没有?锁大概率查错了 schema)"
    hit = [d for d in defs if "brands_name_owner_key" in d]
    assert hit, "brands 缺生产那条 partial unique 索引 brands_name_owner_key;现有:%s" % defs
    assert "is_deleted = false" in hit[0], "唯一索引口径与生产不同:%s" % hit[0]


# ==================== [R5 ⑤ 批2] 单文件夹具的结构型保真锁 ====================
# 批 1 那六个是 conftest 级,锁可以直接连上目录自己的会话库做**运行时**列比。
# 批 2 这七个是单文件夹具,各自的建库方式互不相同(有的每跑现开一个随机名新库、
# 有的走应用连接池、有的只吃 TEST_DATABASE_URL),没有一个统一的运行时挂载点。
# ⇒ 这七把锁打**源码结构**,但**不是** grep 裸符号名:
#     · 用 AST 找真正的**调用**(Call 节点) —— import 一下、注释里提一句、
#       变量名里出现,统统不算数(我记过这个坑:裸符号名结构锚会被 import/注释满足);
#     · 配一条反向:源码里不许再有手搓的 `CREATE TABLE [IF NOT EXISTS] brands (`。
#   两条合起来 = 「换回手搓即红」:改回去必然要删调用 + 加回 DDL,两条各红一条。
#   brands 的**形状**由 tests/db_bootstrap_r5 那 13 条运行时判据保证,不在这里重复造轮子。

import ast as _ast
import re as _re
from pathlib import Path as _Path


def _target_source(rel_path: str) -> tuple:
    p = _Path(__file__).resolve().parents[1] / rel_path
    assert p.exists(), "锁指向的文件不存在:%s(坐标写错 = 零分母恒绿)" % rel_path
    return p, p.read_text(encoding="utf-8")


def assert_fixture_calls_the_ssot_export(rel_path: str):
    """目标夹具里必须有一次**真正的** ensure_brands_schema(...) 调用。"""
    p, src = _target_source(rel_path)
    tree = _ast.parse(src, filename=str(p))
    calls = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == "ensure_brands_schema":
                calls.append(getattr(node, "lineno", "?"))
    assert calls, (
        "%s 里没有 ensure_brands_schema(...) 的**调用**(AST Call 节点)。\n"
        "→ 夹具大概率被改回手搓 brands 了。注意:import 它、注释里提它、"
        "字符串里写它,都不算 —— 这条锁只认真调用。" % rel_path)


def assert_fixture_does_not_hand_author_brands(rel_path: str):
    """反向:目标夹具源码里不许再有手搓的 brands 建表语句。"""
    p, src = _target_source(rel_path)
    hits = []
    pat = _re.compile(r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?(public\.)?brands\s*\(", _re.I)
    for i, line in enumerate(src.split("\n"), 1):
        # 注释行不算(转换时留下的说明里会复述这句话 —— 不排除的话恒红)
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("--"):
            continue
        if pat.search(line):
            hits.append("%s:%d: %s" % (rel_path, i, stripped[:90]))
    assert not hits, (
        "%s 又手搓 brands 了:\n  %s\n→ 应当调 db.brands_schema.ensure_brands_schema(cursor)。"
        % (rel_path, "\n  ".join(hits)))
