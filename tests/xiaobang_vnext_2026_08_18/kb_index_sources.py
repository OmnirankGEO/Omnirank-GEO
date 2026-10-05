"""小榜 KB 术语锁的**分母**:从索引结构锚枚举,不再手写文件清单。

## 这个模块存在的理由(34 班 verify ④ 挂的账)

R3 的术语锁把扫描面写成一张**文件 glob 清单**::

    _SCAN_GLOBS = ("knowledge/system_kb/pages/*.md", "agents/xiaobang_canned_faq.py", ...)

清单里的文件全清干净了,锁全绿,包发了车。上线当天真问小榜「充值」,
它答**「充进来的是充值积分」**;问「席位」,它答**「靠老板设置额度」**。

线上 ``kb_chunks`` 实测:含「积分」4 条(``faq_1/2/8/9``)、含「额度」11 条。
它们一条都不在那张清单里 —— 因为**清单不是索引的分母**:

===============================  ==================================
真正进索引的                     R3 清单里有吗
===============================  ==================================
``api/help_docs_content.json``   ❌ 没有 → 线上 11 条「额度」
``faq_items`` 表(种子在 .py)    ❌ 没有 → 线上 4 条「积分」
``agents/xiaobang_presets.py``   ✅ 有
``knowledge/system_kb/pages/``   ✅ 有
===============================  ==================================

🔴 **34 班发车记录把根因记成「没清 agents/xiaobang_canned_faq.py(积分残留 96 处)」
—— 那条不成立**:该文件在生产镜像里实测 ``积分=0 额度=0``,而且它**根本不进索引**
(``xiaobang_kb_indexer`` 三个 builder 一个都不读它;它是 RAG 之前的确定性兜底层)。
真正的两个漏口是上表那两行。分母搞错时,把清单里的文件清得再干净也不会让线上变好。

## 所以分母改成什么

改成**从索引构建链的结构锚解析**:每一条 :data:`kb_index_sources` 的路径都
**从现役模块读出来**(``tools.xiaobang_kb_indexer.DOCS_FILE`` 这类模块常量、
``reindex_system`` 的 ``pages_glob`` 默认值),不在这里重打一遍字符串。
重打一遍 = 又造了一份会各自漂移的清单。

## 锁怎么在「新增一个入索引源」时转红

光有注册表不够 —— 注册表本身也会漏登记。所以配四道**完备性闸**
(:func:`completeness_violations`),全部从 builder 模块的 AST 机械取全集:

1. **source_type 全集**:builder 里出现的每个 ``source_type=`` 字面量都必须被登记;
2. **文件入参全集**:模块级路径常量 + 任何路径/glob 形状的字面量;
3. **SQL 表全集**:``FROM <表>`` —— faq 那条就是从表读的,只有这道闸看得见它;
4. **导入的数据常量全集**:``from agents.xiaobang_presets import PRESETS`` 这类。

四道闸各自有活性自证(喂一段带未登记源的假代码,必须报出来),
见 ``test_kb_index_denominator.py``。
"""

from __future__ import annotations

import ast
import glob
import importlib
import inspect
import re
from dataclasses import dataclass, field
from pathlib import Path


#: 🔴 builder 自身**不进内容分母**,理由必须写清楚:
#:    ``xiaobang_kb_indexer._DOMAIN_WORDS`` 里有「充值积分 / 佣金积分 / 写作大厅」——
#:    那是 **jieba 查询侧词典**,用来让还在说旧词的用户也能被召回,
#:    它不产出任何一个字给用户看。把 builder 塞进内容分母会逼下一个人
#:    删掉这些同义词,结果是说旧词的用户召回不到 —— 锁把功能改坏了。
BUILDER_NOT_CONTENT_REASON = (
    "builder 只含 jieba 查询侧同义词典(充值积分/佣金积分/写作大厅),"
    "零字节进 chunk content;删它会让说旧词的用户召回不到"
)


@dataclass(frozen=True)
class KBIndexSource:
    """一个**会把文字送到用户眼前的**内容源。"""

    key: str
    #: 这个源产出的 chunk ``source_type``(与 builder 里的字面量对齐);
    #: 空 tuple = 不进 kb_chunks,但仍是用户可见答案(见 canned_faq)。
    source_types: tuple[str, ...]
    #: ``file`` | ``glob`` | ``db_table``
    kind: str
    #: 现役模块里解析出来的路径/glob/表名 —— **不在本文件里重打字符串**。
    locator: str
    #: 文本真正的出处(``db_table`` 类的种子文本在 ``.py`` 里)。
    text_origin: str
    #: 登记理由 + 结构锚是怎么解析到的。
    why: str
    #: 供完备性闸 ③/④ 对账。
    db_tables: tuple[str, ...] = ()
    imported_constants: tuple[str, ...] = ()


def _module_attr(module_name: str, attr: str) -> str:
    return str(getattr(importlib.import_module(module_name), attr))


def _default_arg(module_name: str, func: str, param: str) -> str:
    sig = inspect.signature(getattr(importlib.import_module(module_name), func))
    return str(sig.parameters[param].default)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _rel(path: str) -> str:
    """builder 里的路径常量是绝对路径(``os.path.join(_ROOT, ...)``)。

    统一成仓库相对路径,判据才能在任何 worktree 里跑。
    """
    try:
        return Path(path).resolve().relative_to(repo_root()).as_posix()
    except ValueError:
        return Path(path).as_posix()


#: kb_chunks 的落地表名 —— 下面两级枚举的结构锚起点。
KB_TABLE = "kb_chunks"

#: 「往 kb_chunks 写」的字面判据。用一个编译好的常量,不在两处各写一遍正则 ——
#: 两处各写就会有一处先漂。
_INSERT_RE = re.compile("INSERT\\s+INTO\\s+" + KB_TABLE, re.IGNORECASE)

#: writer 所在模块。它是 sink,不是 builder。
KB_WRITER_MODULE_PATH = "db/kb_db.py"

#: 扫描 builder 时跳过的目录(判据自己 / 归档 / 前端依赖)。
_SCAN_SKIP_DIRS = ("tests", "node_modules", ".git", "docs", "frontend", "qa", "_archive")


def kb_chunks_writers(writer_source: str | None = None) -> set[str]:
    """**第一级结构锚**:``db/kb_db.py`` 里哪些函数真的往 ``kb_chunks`` 写。

    判据 = 函数体里出现 ``INSERT INTO kb_chunks`` 的字符串字面量。
    不按函数名猜(``clear_chunks_by_type`` 名字像 writer,其实只 DELETE),
    也不手写名单 —— 手写名单就是 R3 那张手写文件清单的同一种病。
    """
    if writer_source is None:
        writer_source = (repo_root() / KB_WRITER_MODULE_PATH).read_text(encoding="utf-8")
    out: set[str] = set()
    tree = ast.parse(writer_source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Constant) and isinstance(inner.value, str)
                    and _INSERT_RE.search(inner.value)):
                out.add(node.name)
                break
    return out


def builder_modules_from(
    sources_by_relpath: dict[str, str], writers: set[str] | None = None,
) -> tuple[str, ...]:
    """**第二级结构锚**:谁调用了那些 writer —— 那就是 builder 全集。

    ``sources_by_relpath`` 做成入参(而不是内部走文件系统),是为了能喂一份
    **虚构的第三个 writer 调用方**做活性自证:闸自己得先被证明会响。
    """
    writers = writers if writers is not None else kb_chunks_writers()
    assert writers, "第一级锚取到空集合 —— 后面的枚举会恒空,且看起来像'没有 builder'"
    found: list[str] = []
    for relpath, source in sorted(sources_by_relpath.items()):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        hit = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in writers:
                    hit = True
                    break
            # 绕过函数调用直接写表的路径也算 writer 调用方。
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and _INSERT_RE.search(node.value)):
                hit = True
                break
        if hit:
            found.append(relpath.replace("/", ".").removesuffix(".py"))
    return tuple(found)


def _repo_python_sources() -> dict[str, str]:
    root = repo_root()
    out: dict[str, str] = {}
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel == KB_WRITER_MODULE_PATH:
            continue                       # writer 自身是 sink,不是 builder
        if any(rel.startswith(skip + "/") or rel == skip for skip in _SCAN_SKIP_DIRS):
            continue
        try:
            out[rel] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return out


def builder_modules() -> tuple[str, ...]:
    """现役 builder 全集 —— 机械枚举,**手写清单已作废**(R3-P7 ②)。

    R3-P6 这里是一行 tuple 字面量。它当时是对的,但它和分母那张手写清单
    是同一种东西:**没有任何机制保证新增一个 writer 调用方会出现在里面**。
    现在改成从「kb_chunks writer 的调用方」两级机械枚举。
    """
    return builder_modules_from(_repo_python_sources())


#: 向后兼容的名字。**不再是字面量** —— 它现在是枚举结果。
BUILDER_MODULES: tuple[str, ...] = builder_modules()


def kb_index_sources() -> tuple[KBIndexSource, ...]:
    """现役索引链的内容源全集(路径全部从现役模块解析)。"""
    docs_nav = _rel(_module_attr("tools.xiaobang_kb_indexer", "DOCS_FILE"))
    docs_body = _rel(_module_attr("tools.xiaobang_kb_indexer", "HELP_DOCS_CONTENT_FILE"))
    pages_glob = _default_arg("tools.xiaobang_system_kb", "reindex_system", "pages_glob")
    return (
        KBIndexSource(
            key="help_docs_nav",
            source_types=("doc",),
            kind="file",
            locator=docs_nav,
            text_origin=docs_nav,
            why="parse_docs_ts() 读它取 slug/title;锚 = 模块常量 DOCS_FILE",
        ),
        KBIndexSource(
            key="help_docs_body",
            source_types=("doc",),
            kind="file",
            locator=docs_body,
            text_origin=docs_body,
            why=("doc chunk 的**正文**在这里(2026-06-03 从 docs-data.ts 迁出);"
                 "R3 清单漏的就是它 —— 线上 11 条「额度」全出自此。"
                 "锚 = 模块常量 HELP_DOCS_CONTENT_FILE"),
        ),
        KBIndexSource(
            key="faq_items",
            source_types=("faq",),
            kind="db_table",
            locator="faq_items",
            text_origin="db/faq_db.py",
            why=("build_faq_chunks() 从 faq_items 表 SELECT;表里的文本由 "
                 "db/faq_db.py::_INITIAL_SEED 灌入,且 seed 同步路径对 "
                 "updated_by IS NULL 的行**会覆盖回 seed 版本**,"
                 "所以改 seed 就等于改线上索引。R3 清单漏的第二处 —— "
                 "线上 4 条「积分」(faq_1/2/8/9)全出自此。锚 = SQL FROM 表名"),
            db_tables=("faq_items",),
        ),
        KBIndexSource(
            key="presets",
            source_types=("preset",),
            kind="file",
            locator="agents/xiaobang_presets.py",
            text_origin="agents/xiaobang_presets.py",
            why="build_preset_chunks() 遍历 PRESETS;锚 = 模块级 import 的数据常量",
            imported_constants=("PRESETS",),
        ),
        KBIndexSource(
            key="system_pages",
            source_types=("sys_page", "sys_field", "sys_button", "sys_error", "sys_qa"),
            kind="glob",
            locator=pages_glob,
            text_origin=pages_glob,
            why="reindex_system(pages_glob=...) 的默认值就是结构锚,不重打字符串",
        ),
        KBIndexSource(
            key="canned_faq",
            source_types=(),
            kind="file",
            locator="agents/xiaobang_canned_faq.py",
            text_origin="agents/xiaobang_canned_faq.py",
            why=("确定性兜底层:命中即直接返回硬答案(preset 之后、RAG 之前),"
                 "**不经过 kb_chunks**。规格 §19.1#13 的扫描面明写含 "
                 "「canned/RAG 输出」,所以它在分母里 —— 但它不是索引源,"
                 "34 班把线上残留归因到它是错的"),
        ),
        KBIndexSource(
            key="page_prefill_copy",
            source_types=(),
            kind="file",
            locator="services/xiaobang_page_prefill.py",
            text_origin="services/xiaobang_page_prefill.py",
            why=("[R3-P7 ②] 它**产出用户可见文案**:主按钮「确认并执行 · 使用 X 算力」、"
                 "取消「取消本次操作 · 不使用算力」、改绑提示「内容已改,算力需要更新」。"
                 "文案在服务端定、前端只渲染 ⇒ 这里写错就是用户读到错词,"
                 "和 KB 是同一类风险,所以进同一个分母"),
        ),
        KBIndexSource(
            key="release_manifest",
            source_types=(),
            kind="file",
            locator="knowledge/system_kb/manifest.json",
            text_origin="knowledge/system_kb/manifest.json",
            why="system page release 的清单,随页面一起发布",
        ),
    )


#: 归档/内部件:显式豁免,必须带理由且路径真实存在(规格 §19.1#13)。
ARCHIVE_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("knowledge/system_kb/REVIEW_ALL_26_PAGES.md",
     "逐页人工复核记录 · 历史归档;34 班实测线上 0 chunk,不进索引"),
    ("knowledge/system_kb/SCHEMA.md",
     "内部 schema 说明,非用户可见文案"),
)


def scan_paths() -> list[Path]:
    """分母:把注册表展开成真实文件列表。

    ``glob`` 类按 builder 的口径排除 ``_*.md`` 草稿页
    (``reindex_system`` 的 docstring 明写这条)。
    """
    out: list[Path] = []
    for src in kb_index_sources():
        if src.kind == "glob":
            for hit in sorted(glob.glob(src.locator)):
                if Path(hit).name.startswith("_"):
                    continue
                out.append(Path(hit))
        elif src.kind == "file":
            out.append(Path(src.locator))
        elif src.kind == "db_table":
            out.append(Path(src.text_origin))
        else:                          # pragma: no cover
            raise ValueError("未知 kind: {0}".format(src.kind))
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = p.as_posix()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


# ══════════════════════════════════════════════════════════════════════════
# 完备性闸:从 builder 的 AST 机械取全集,与注册表对账
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class BuilderFacts:
    """从一份 builder 源码里机械取出的四类结构锚。"""

    source_types: set[str] = field(default_factory=set)
    file_inputs: set[str] = field(default_factory=set)
    sql_tables: set[str] = field(default_factory=set)
    imported_constants: set[str] = field(default_factory=set)


_SQL_FROM = re.compile(r"\bFROM\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)
#: 🔴 必须**整串**是路径形状(fullmatch、不含空白)。
#:    第一版写成 ``search`` + 只看后缀,结果把 docstring 与日志格式串
#:    (「从 final 目录(knowledge/system_kb/pages/*.md)读取…」)全判成文件入参 ——
#:    闸一噪就会被关掉,那比没有闸更坏。收紧后仍打得到真正的新增源:
#:    新增一个内容源必然写成 ``"knowledge/xxx/yyy.md"`` 这种无空白字面量。
_LOOKS_LIKE_PATH = re.compile(r"[\w./*\-]+\.(md|json|ts|tsx|py)\Z")
_CONST_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def builder_facts(source: str) -> BuilderFacts:
    """AST 取全集。

    **不 import 被测模块**,只解析源码字符串 —— 所以对任意假代码同样成立,
    完备性闸的活性自证靠的就是这一点。
    """
    facts = BuilderFacts()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        # ① source_type= 关键字实参 / dict 里的 "source_type" 键
        if isinstance(node, ast.keyword) and node.arg == "source_type":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                facts.source_types.add(node.value.value)
        if isinstance(node, ast.Dict):
            for key_node, val_node in zip(node.keys, node.values):
                if (isinstance(key_node, ast.Constant) and key_node.value == "source_type"
                        and isinstance(val_node, ast.Constant)
                        and isinstance(val_node.value, str)):
                    facts.source_types.add(val_node.value)

        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # ② 文件入参:任何路径/glob 形状的字符串字面量
            if _LOOKS_LIKE_PATH.fullmatch(node.value):
                facts.file_inputs.add(node.value)
            # ③ SQL 表:FROM <表>
            for match in _SQL_FROM.finditer(node.value):
                facts.sql_tables.add(match.group(1).lower())

        # ④ 模块级 import 进来的**数据常量**(全大写名)
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if _CONST_NAME.match(alias.name):
                    facts.imported_constants.add(alias.name)
    return facts


def _registered() -> tuple[set[str], set[str], set[str], set[str]]:
    types: set[str] = set()
    paths: set[str] = set()
    tables: set[str] = set()
    consts: set[str] = set()
    for src in kb_index_sources():
        types.update(src.source_types)
        paths.add(Path(src.locator).as_posix())
        paths.add(Path(src.text_origin).as_posix())
        tables.update(src.db_tables)
        consts.update(src.imported_constants)
    return types, paths, tables, consts


#: 闸 ②/③/④ 的**已知非内容项**豁免。每条必须写清「为什么它不产字给用户看」。
NON_CONTENT_EXEMPT_PATHS: tuple[tuple[str, str], ...] = (
    ("/app/RELEASE_SHA", "部署合同写的版本文件,不是文案"),
)
NON_CONTENT_EXEMPT_TABLES: tuple[tuple[str, str], ...] = (
    ("kb_chunks", "索引自己的落地表,是 sink 不是 source"),
)
NON_CONTENT_EXEMPT_CONSTS: tuple[tuple[str, str], ...] = (
    ("OPERATION_REGISTRY_VERSION", "版本号字符串,不进 chunk content"),
    ("SYS_SOURCE_TYPES", "source_type 名单本身,不是文本源"),
    # [xbsolve 包 C④ 2026-08-21] manifest 现在记术语规则版本,供 health 判 stale。
    # 与上面 OPERATION_REGISTRY_VERSION 同类:**只进 manifest,不进 chunk content**。
    # 🔴 这道闸拦住了它们、逼我在这里写理由 —— 这正是它该有的行为,
    #    不是我绕开它:它问的是「这个源产不产给用户看的字」,答案是不产。
    ("RULING_RULE_ID", "术语规则的 id,只写进 release manifest 供 health 对账,不进 chunk content"),
    ("RULING_VERSION", "术语规则的版本号,同上;health 靠它判 stale"),
)


def completeness_violations(sources_by_module: dict[str, str]) -> list[str]:
    """四道闸。

    ``sources_by_module`` = ``{模块名: 源码}``。做成入参而不是内部读文件,
    是为了能喂一段**假的 builder 源码**做活性自证 —— 闸自己得先被证明会响。
    """
    reg_types, reg_paths, reg_tables, reg_consts = _registered()
    exempt_paths = {p for p, _ in NON_CONTENT_EXEMPT_PATHS}
    exempt_tables = {t for t, _ in NON_CONTENT_EXEMPT_TABLES}
    exempt_consts = {c for c, _ in NON_CONTENT_EXEMPT_CONSTS}

    out: list[str] = []
    for mod, source in sources_by_module.items():
        facts = builder_facts(source)

        for stype in sorted(facts.source_types - reg_types):
            out.append("闸①未登记 source_type『{0}』({1})".format(stype, mod))

        for raw in sorted(facts.file_inputs):
            norm = Path(raw).as_posix()
            if norm in exempt_paths:
                continue
            # 注册表登记的是仓库相对路径;builder 里的字面量常是相对片段,
            # 两边都归一成 posix 后做后缀匹配。
            if any(rp.endswith(norm) or norm.endswith(rp) for rp in reg_paths):
                continue
            out.append("闸②未登记文件入参『{0}』({1})".format(raw, mod))

        for tbl in sorted(facts.sql_tables - reg_tables - exempt_tables):
            out.append("闸③未登记 SQL 表『{0}』({1})".format(tbl, mod))

        for const in sorted(facts.imported_constants - reg_consts - exempt_consts):
            out.append("闸④未登记数据常量『{0}』({1})".format(const, mod))
    return out


def builder_sources() -> dict[str, str]:
    """现役 builder 的源码(给完备性闸用)。"""
    out: dict[str, str] = {}
    for name in BUILDER_MODULES:
        mod = importlib.import_module(name)
        path = inspect.getsourcefile(mod)
        out[name] = Path(path).read_text(encoding="utf-8")
    return out
