"""分母的**完备性闸**判据(R3-P6 制度修复)。

术语锁改了分母之后,新的失败模式是:**注册表本身漏登记**。
所以四道闸从 builder 的 AST 机械取全集,与注册表对账。

这个文件里最重要的不是「现役 builder 全绿」那几条 —— 全绿也可能是闸根本
不会响。真正的判据是**活性自证**:给每道闸喂一段带未登记源的假 builder 源码,
它必须报出来。喂假源码而不是改真文件,是因为
:func:`kb_index_sources.builder_facts` 只解析源码字符串、不 import 被测模块。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from . import kb_index_sources as idx

# ══════════════════════════════════════════════════════════════════════════
# 一、注册表本身自证
# ══════════════════════════════════════════════════════════════════════════

def test_every_source_resolves_to_a_real_target():
    """每条登记的路径都必须真实存在(``db_table`` 类核 text_origin)。"""
    for src in idx.kb_index_sources():
        if src.kind == "glob":
            assert list(Path().glob(src.locator)), src.key
        else:
            assert Path(src.text_origin).exists(), (src.key, src.text_origin)
        assert src.why.strip(), src.key


def test_paths_come_from_live_modules_not_retyped_strings():
    """🔴 锚必须解析自现役模块,不是在判据里重打一遍字符串。

    重打一遍就是又造了一份会各自漂移的清单 —— 那正是 R3 踩的坑。
    这条直接拿模块常量比。
    """
    from tools import xiaobang_kb_indexer as indexer

    by_key = {s.key: s for s in idx.kb_index_sources()}
    assert by_key["help_docs_nav"].locator == idx._rel(indexer.DOCS_FILE)
    assert by_key["help_docs_body"].locator == idx._rel(indexer.HELP_DOCS_CONTENT_FILE)


def test_archive_allowlist_entries_have_reasons_and_exist():
    for path, reason in idx.ARCHIVE_ALLOWLIST:
        assert Path(path).exists(), path
        assert reason.strip(), path
    scanned = {p.as_posix() for p in idx.scan_paths()}
    for path, _reason in idx.ARCHIVE_ALLOWLIST:
        assert path not in scanned, "{0} 既在白名单又在分母里".format(path)


def test_non_content_exemptions_have_reasons():
    """三张豁免表的每一条都必须写清「为什么它不产字给用户看」。"""
    for table in (idx.NON_CONTENT_EXEMPT_PATHS,
                  idx.NON_CONTENT_EXEMPT_TABLES,
                  idx.NON_CONTENT_EXEMPT_CONSTS):
        assert table, table
        for item, reason in table:
            assert item.strip() and reason.strip(), item


def test_builder_itself_is_not_in_the_content_denominator():
    """builder **不进**内容分母,理由必须是可解析的、且当下仍然成立。

    ``_DOMAIN_WORDS`` 里确实有「充值积分」这类旧词 —— 那是 jieba **查询侧**
    同义词典,删了会让还在说旧词的用户召回不到。把 builder 塞进内容分母
    等于逼下一个人删功能。
    """
    scanned = {p.as_posix() for p in idx.scan_paths()}
    for module_path in ("tools/xiaobang_kb_indexer.py", "tools/xiaobang_system_kb.py"):
        assert module_path not in scanned, module_path
    assert idx.BUILDER_NOT_CONTENT_REASON.strip()
    # 反向对照:理由声称的那批旧词**确实**还在 builder 里 —— 否则这条豁免是空的。
    src = idx.builder_sources()["tools.xiaobang_kb_indexer"]
    assert "充值积分" in src and "写作大厅" in src


# ══════════════════════════════════════════════════════════════════════════
# 二、现役 builder 全绿(先证明取到的全集非空,再证明它对得上)
# ══════════════════════════════════════════════════════════════════════════

def test_live_builders_are_fully_registered():
    problems = idx.completeness_violations(idx.builder_sources())
    assert not problems, problems


@pytest.mark.parametrize("module_name", idx.BUILDER_MODULES)
def test_extracted_facts_are_non_empty(module_name: str):
    """🔴 分母自证:闸取到的全集必须非空。

    空集合与「全部已登记」在对账结果里长得一模一样(都是零违规)。
    这条先把「闸跑起来了」和「闸过了」分开。
    """
    facts = idx.builder_facts(idx.builder_sources()[module_name])
    assert facts.source_types, module_name
    assert facts.file_inputs or facts.sql_tables or facts.imported_constants, module_name


def test_sql_table_gate_actually_sees_the_faq_table():
    """闸③ 的存在理由:faq 那条源**只有**从 SQL 看得见。

    它不是文件常量、不是 import 的数据常量 —— R3 的文件清单看不见它,
    正是线上 4 条「积分」的来路。
    """
    facts = idx.builder_facts(idx.builder_sources()["tools.xiaobang_kb_indexer"])
    assert "faq_items" in facts.sql_tables


# ══════════════════════════════════════════════════════════════════════════
# 三、活性自证:每道闸各喂一段带未登记源的假 builder,必须转红
# ══════════════════════════════════════════════════════════════════════════

_FAKE_NEW_SOURCE_TYPE = '''
def build_glossary_chunks():
    return [dict(source_type="glossary", content="新增术语表")]
'''

_FAKE_NEW_FILE_INPUT = '''
NEW_DOC_FILE = "knowledge/marketing/talking_points.md"

def build_extra_chunks():
    with open(NEW_DOC_FILE) as handle:
        return handle.read()
'''

_FAKE_NEW_SQL_TABLE = '''
def build_notice_chunks(cur):
    cur.execute("SELECT id, body FROM product_notices WHERE is_published = TRUE")
    return list(cur.fetchall())
'''

_FAKE_NEW_IMPORT_CONST = '''
from agents.xiaobang_scripts import SALES_SCRIPTS

def build_script_chunks():
    return [dict(content=s) for s in SALES_SCRIPTS]
'''


@pytest.mark.parametrize(
    "gate, fake, needle",
    [
        ("闸①source_type", _FAKE_NEW_SOURCE_TYPE, "glossary"),
        ("闸②文件入参", _FAKE_NEW_FILE_INPUT, "talking_points.md"),
        ("闸③SQL 表", _FAKE_NEW_SQL_TABLE, "product_notices"),
        ("闸④数据常量", _FAKE_NEW_IMPORT_CONST, "SALES_SCRIPTS"),
    ],
    ids=lambda v: v if isinstance(v, str) and len(v) < 20 else "",
)
def test_each_gate_turns_red_on_an_unregistered_source(gate, fake, needle):
    """🔴 WO 的原话:「锁要能在『新增一个入索引源含旧术语』时转红」。

    这四条就是那句话的判据 —— 每道闸各自被证明会响,而不是四道一起看总数。
    """
    problems = idx.completeness_violations({"fake_builder": fake})
    assert any(needle in p for p in problems), (gate, problems)


def test_gates_do_not_fire_on_a_fully_registered_fake():
    """反向对照:同一套闸对**已登记**的源必须放行,否则它是恒红的。

    恒红和恒真一样废 —— 恒红的门第二天就会被人关掉。
    """
    registered_fake = '''
from agents.xiaobang_presets import PRESETS

DOCS_FILE = "frontend/src/pages/Help/docs-data.ts"

def build(cur):
    cur.execute("SELECT question FROM faq_items")
    return [dict(source_type="faq", content=p) for p in PRESETS]
'''
    assert idx.completeness_violations({"fake_builder": registered_fake}) == []


def test_path_gate_ignores_prose_that_merely_mentions_a_path():
    """闸② 的**形状**判据:整串是路径才算入参。

    第一版用 ``search`` + 只看后缀,把 docstring 与日志格式串
    (「从 final 目录(knowledge/system_kb/pages/*.md)读取…」)全判成新增源。
    一噪就会被关掉 —— 那比没有闸更坏。
    """
    prose = '''
"""从 final 目录(knowledge/system_kb/pages/*.md)读取人审真值。"""
def f():
    logger.warning("[system-kb] glob '%s' 无匹配 final 页(已排除 _*.md)", g)
'''
    assert idx.completeness_violations({"fake_builder": prose}) == []
    # 反向对照:同一道闸对真的新增路径字面量仍然会响。
    assert idx.completeness_violations(
        {"fake_builder": 'NEW = "knowledge/other/page.md"'}
    )


# ══════════════════════════════════════════════════════════════════════════
# 四、builder 全集**结构锚化**(R3-P7 ②)· 手写清单已作废
# ══════════════════════════════════════════════════════════════════════════

def test_builder_modules_is_not_a_hand_written_literal():
    """🔴 R3-P6 这里是一行 tuple 字面量。

    它当时**是对的**,但它和被作废的那张手写文件清单是同一种东西:
    没有任何机制保证「新增一个 writer 调用方」会出现在里面。
    这条钉住它现在是枚举结果 —— 判据打的是「源码里那一行不是字面量」。
    """
    source = (idx.repo_root() / "tests/xiaobang_vnext_2026_08_18/kb_index_sources.py"
              ).read_text(encoding="utf-8")
    assert "BUILDER_MODULES: tuple[str, ...] = builder_modules()" in source
    # 反向对照:确实枚举出了东西,不是空 tuple 混过去。
    assert idx.BUILDER_MODULES, idx.BUILDER_MODULES


def test_first_anchor_finds_writers_by_what_they_do_not_by_their_name():
    """第一级锚:``db/kb_db.py`` 里**函数体含 INSERT INTO kb_chunks** 的才算 writer。

    不按名字猜 —— ``clear_chunks_by_type`` 名字像 writer,其实只 DELETE。
    这条同时是反向对照:那两个 clear 函数必须**不在**集合里。
    """
    writers = idx.kb_chunks_writers()
    assert writers == {"insert_chunk", "replace_chunks_transactionally"}, writers
    assert "clear_chunks_by_type" not in writers
    assert "clear_system_chunks" not in writers


def test_second_anchor_reproduces_the_live_builder_set():
    """第二级锚:writer 的调用方 = builder 全集。

    钉的是**集合**,不是个数 —— 换掉一个、总数不变的改动,数个数看不出来。
    """
    assert set(idx.builder_modules()) == {
        "tools.xiaobang_kb_indexer", "tools.xiaobang_system_kb",
    }, idx.builder_modules()


def test_the_writer_module_itself_is_not_counted_as_a_builder():
    """``db/kb_db.py`` 是 sink,不是 builder。把它算进去会让"谁在造内容"失焦。"""
    assert "db.kb_db" not in idx.builder_modules()


#: 🔴 工单点名的反向对照:**虚构第三个 writer 调用方**。
_FABRICATED_THIRD_WRITER = '''
from db.kb_db import replace_chunks_transactionally

def build_glossary_chunks():
    return [dict(source_type="glossary", source_slug="g1", content="术语表")]

def reindex_glossary():
    replace_chunks_transactionally(source_types=("glossary",), chunks=build_glossary_chunks())
'''

_FABRICATED_RAW_SQL_WRITER = '''
def push_notice(cur):
    cur.execute("INSERT INTO kb_chunks (source_type, content) VALUES ('notice', %s)", ("x",))
'''


@pytest.mark.parametrize(
    "label, fake",
    [("走 writer 函数", _FABRICATED_THIRD_WRITER), ("裸 SQL 绕过函数", _FABRICATED_RAW_SQL_WRITER)],
    ids=["via_writer_call", "via_raw_sql"],
)
def test_a_fabricated_third_writer_is_enumerated(label, fake):
    """🔴 工单原话:「虚构第三个 writer → 完备性闸红」。

    两种形态都要打到:走 writer 函数的,和**绕过函数直接写表**的
    (后者是前一版枚举方式看不见的那类)。
    """
    modules = idx.builder_modules_from({"tools/fake_glossary.py": fake})
    assert modules == ("tools.fake_glossary",), (label, modules)


def test_the_fabricated_third_writer_turns_the_completeness_gate_red():
    """枚举出来只是第一步 —— 它必须真的把完备性闸打红。

    (枚举到了但闸不看它,等于没枚举。)
    """
    problems = idx.completeness_violations({"tools.fake_glossary": _FABRICATED_THIRD_WRITER})
    assert any("glossary" in p for p in problems), problems


def test_enumeration_does_not_swallow_a_module_that_never_writes():
    """反向对照:不写 kb_chunks 的模块**不许**被算成 builder。

    恒真的枚举和恒假的枚举一样废 —— 它会把全仓都登记进分母然后一起豁免。
    """
    innocent = '''
def render_report(rows):
    return [dict(title=r["title"]) for r in rows]
'''
    assert idx.builder_modules_from({"services/innocent.py": innocent}) == ()


def test_prefill_copy_module_is_in_the_terminology_denominator():
    """[R3-P7 ②] ``services/xiaobang_page_prefill.py`` 产出用户可见文案 ⇒ 进分母。"""
    scanned = {p.as_posix() for p in idx.scan_paths()}
    assert "services/xiaobang_page_prefill.py" in scanned, sorted(scanned)


def test_terminology_module_is_a_reexport_not_a_second_copy():
    """[R3-P7 ①] 判据侧与运行时侧必须是**同一个函数对象**。

    比"行为看起来一样"没用 —— 两份实现的漂移一定是线上那份更松。
    """
    from services import kb_terminology_gate as runtime
    from . import terminology_domains as shim

    for name in ("kb_index_residue", "cash_semantic_violations",
                 "dead_scheme_violations", "domain12_violations"):
        assert getattr(shim, name) is getattr(runtime, name), name
    shim_source = (idx.repo_root()
                   / "tests/xiaobang_vnext_2026_08_18/terminology_domains.py"
                   ).read_text(encoding="utf-8")
    # shim 里不许有第二份实现(出现 def 就是又抄了一份)。
    assert "def domain12_violations" not in shim_source
    assert "from services.kb_terminology_gate import" in shim_source
