# -*- coding: utf-8 -*-
"""WO_272 · 运行时按路径读取的非 ASCII 仓内路径:在树里、进得了镜像,且缺了会出声。

分母由 `_enumerate.py` **机械枚举**(规则与自曝盲区见该文件抬头),不是我挑的清单:
  修复前的树 `db862889a` 上它报出 6 条「树里只有 cp437 乱码孪生」(= WO_268 ⑤ 的 6 红)+ 1 条从未存在;
  本分支上乱码孪生 0 条。

五类判据(真判据、牙证、对照臂都走同一个 `enumerate_sources`):
  (a) 每条必需路径在 HEAD 的树里且非空(文件 blob 非空 / 目录下至少一个跟踪文件);
  (b) 每条必需路径进得了构建上下文(复用 WO_268 对拍过真 BuildKit 的匹配器),清单文件本身也要进;
  (c) 没有任何「代码写正确名、树里只有乱码名」—— 本次事故那一类;
  (d) 读不存在路径的,**恰好**是冻结的已知缺失表(多一条少一条都红),且表项可机检:
      正确名与乱码名都不在树里、`git log --all` 从未出现过(不是丢失的文件);
  (e) `config/runtime_required_paths.txt` == 枚举器输出(Deploy 镜像门读的就是它),格式合镜像门的约定;
  分母只许增:冻结的下限表必须仍在枚举结果里。
出声:两处原来静默给空的加载,缺文件时必出 ERROR 日志并计数。
"""
from __future__ import annotations

import logging
import subprocess

import pytest

from ._enumerate import (
    REQUIRED_LIST_REL,
    ROOT,
    Tree,
    cp437_twin,
    dockerignore_matcher,
    enumerate_head,
    enumerate_sources,
    parse_required_list,
    render_required_list,
    required_paths,
)

#: 分母下限(只许增不许减):2026-09-23 首跑的必需路径。枚举结果少了其中任何一条 ⇒ 红 ——
#: 要么代码真的不再读它(改这张表是一个显式决定),要么尺子坏了。
FLOOR_REQUIRED = (
    "docs/条款/",
    "docs/条款/代理合作协议_v2.0.md",
    "docs/条款/服务商申请协议_v2.3.md",
    "knowledge/2026年用户搜索意图与热门标题公式知识库.md",
    "knowledge/AI对话平台→内容平台映射（完整版）.md",
    "knowledge/geo_specific/GEO优化规则库（完整版）.md",
    "knowledge/案例库/",
    "knowledge/部分平台内容风格.md",
)

#: 读不存在路径的已知项(精确冻结)。(源文件, 代码里的串, 解析路径)。
#: `v9_ranking_template.md`:正确名、乱码名在全部引用的历史里**都从未出现过** —— 一条死读取,
#: 不是 07-12 那一类;是否删掉这段读取另开单(WO_272 边界:不改加载逻辑)。
KNOWN_MISSING = {
    ("writing/prompt_template_manager.py", "knowledge/案例库/v9_ranking_template.md",
     "knowledge/案例库/v9_ranking_template.md"),
}


@pytest.fixture(scope="module")
def head():
    files, dockerignore, tree, sources, findings = enumerate_head("HEAD")
    return {"files": files, "dockerignore": dockerignore, "tree": tree,
            "sources": sources, "findings": findings,
            "required": required_paths(findings, tree)}


def _git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True).stdout


# ══════════════════════════════════════════════════════════════════
# 对照臂:分母打印 + 下限(尺子没坏)
# ══════════════════════════════════════════════════════════════════

def test_denominator_is_printed_and_not_below_floor(head):
    print("\n运行时 .py %d 个 · 发现 %d 条 · 必需路径 %d 条" %
          (len(head["sources"]), len(head["findings"]), len(head["required"])))
    assert len(head["sources"]) > 500, "只枚举到 %d 个运行时 .py —— 尺子坏了(rc=3 语义)" % len(head["sources"])
    lost = [p for p in FLOOR_REQUIRED if p not in head["required"]]
    assert not lost, "分母只许增不许减,这些必需路径从枚举结果里消失了:%r" % lost


# ══════════════════════════════════════════════════════════════════
# (a)(b)(c)(d)(e)
# ══════════════════════════════════════════════════════════════════

def test_a_every_required_path_is_in_head_tree_and_nonempty(head):
    tree: Tree = head["tree"]
    bad = []
    for p in head["required"]:
        if p.endswith("/"):
            d = p[:-1]
            if tree.kind(d) != "dir" or not any(f.startswith(p) for f in tree.files):
                bad.append((p, "目录不在树里或为空"))
        elif tree.kind(p) != "file":
            bad.append((p, "文件不在树里"))
        elif int(_git("cat-file", "-s", "HEAD:" + p).strip()) <= 0:
            bad.append((p, "文件为空"))
    assert not bad, bad


def test_b_every_required_path_and_the_list_itself_reach_build_context(head):
    ctx = dockerignore_matcher().build_context_files(head["files"], head["dockerignore"])
    bad = []
    for p in head["required"]:
        if p.endswith("/"):
            if not any(f.startswith(p) for f in ctx):
                bad.append(p)
        elif p not in ctx:
            bad.append(p)
    assert not bad, "进不了镜像构建上下文:%r" % bad
    # 镜像门从镜像里读 /app/config/runtime_required_paths.txt —— 它自己也得进得去
    assert REQUIRED_LIST_REL in ctx, "%s 被 .dockerignore 排除了,镜像门读不到清单" % REQUIRED_LIST_REL


def test_c_no_correct_name_in_code_with_only_a_mojibake_twin_in_tree(head):
    twins = [(f.source, f.line, f.literal, f.resolved) for f in head["findings"] if f.mojibake_twin]
    assert not twins, "代码按正确名读、树里只有 cp437 乱码名(07-12 那一类):%r" % twins


def test_d_missing_reads_are_exactly_the_frozen_known_missing(head):
    missing = {(f.source, f.literal, f.resolved) for f in head["findings"]
               if f.kind == "missing" and not f.mojibake_twin}
    assert missing == KNOWN_MISSING, "多 %r 少 %r" % (sorted(missing - KNOWN_MISSING),
                                                   sorted(KNOWN_MISSING - missing))
    tree: Tree = head["tree"]
    for _src, _lit, path in KNOWN_MISSING:
        assert tree.kind(path) == "missing" and tree.kind(cp437_twin(path)) == "missing"
        for p in (path, cp437_twin(path)):
            assert not _git("log", "--all", "--format=%h", "--", p).strip(), (
                "%s 在历史里出现过 —— 它是**丢失的文件**,不许靠已知缺失表放行" % p)


def test_e_committed_required_list_equals_enumerator_and_fits_gate_format(head):
    # 读 HEAD 里**提交了的**那份(镜像按提交构建,镜像门读的就是它),不读工作区
    text = _git("show", "HEAD:" + REQUIRED_LIST_REL).decode("utf-8")
    committed = parse_required_list(text)
    assert committed == head["required"], (
        "清单与枚举器不一致(重新生成:python -m tests.runtime_nonascii_paths_2026_09_23._enumerate --write):"
        "多 %r 少 %r" % (sorted(set(committed) - set(head["required"])),
                        sorted(set(head["required"]) - set(committed))))
    for p in committed:     # 镜像门的格式约定:相对 /app、无 ..、目录以 / 结尾
        assert not p.startswith("/") and ".." not in p.split("/"), p
    assert text == render_required_list(head["required"]), "清单是手改过的(表头或顺序与生成器不一致)"


# ══════════════════════════════════════════════════════════════════
# 牙证 + 配对的对照臂(注入合成源码 / 合成树,走同一个 enumerate_sources)
# ══════════════════════════════════════════════════════════════════

_SYNTH = "services/_wo272_synthetic_reader.py"


def _synthetic(tail: str) -> str:
    return ("from pathlib import Path\n"
            "D = Path(__file__).resolve().parent.parent / 'knowledge'\n"
            "def f():\n"
            "    return (D / %r).read_text()\n" % tail)


def _subset(head) -> dict:
    """当前有发现的那几个模块。枚举逐模块独立(发现只取决于该模块源码 + 树),
    所以牙证在这个子集上跑与在全量上跑结论相同 —— 全量每跑一遍要几秒,牙证要跑好几遍。"""
    return {p: head["sources"][p] for p in sorted({f.source for f in head["findings"]})}


def test_tooth_subset_reproduces_full_findings(head):
    """上面那句「逐模块独立」本身也要验:子集上的发现 == 全量发现。"""
    assert enumerate_sources(_subset(head), head["tree"]) == head["findings"]


def test_tooth_code_reading_a_nonexistent_path_adds_one_red(head):
    sub = _subset(head)
    base = enumerate_sources(sub, head["tree"])
    poisoned = enumerate_sources({**sub, _SYNTH: _synthetic("不存在的知识库.md")}, head["tree"])
    assert len(poisoned) == len(base) + 1, "分母没 +1:%d → %d" % (len(base), len(poisoned))
    new = [f for f in poisoned if f.source == _SYNTH]
    assert [(f.kind, f.resolved) for f in new] == [("missing", "knowledge/不存在的知识库.md")]
    # 对照臂:同一段合成代码读一个真存在的目录 ⇒ +1 且不红
    control = enumerate_sources({**sub, _SYNTH: _synthetic("案例库")}, head["tree"])
    assert len(control) == len(base) + 1
    assert [(f.kind, f.resolved) for f in control if f.source == _SYNTH] == [("dir", "knowledge/案例库")]


def test_tooth_mojibake_twin_in_tree_turns_c_red(head):
    sub = _subset(head)
    victim = "knowledge/部分平台内容风格.md"
    twin_tree = Tree([cp437_twin(p) if p == victim else p for p in head["tree"].files])
    hits = [f for f in enumerate_sources(sub, twin_tree) if f.mojibake_twin]
    assert [(f.source, f.resolved) for f in hits] == [("utils/knowledge_manager.py", victim)]
    # 对照臂:树不动 ⇒ 没有孪生
    assert not [f for f in enumerate_sources(sub, head["tree"]) if f.mojibake_twin]


def test_tooth_agreement_version_drift_turns_red(head):
    src = head["sources"]["api/partner_api.py"]
    assert src.count('CURRENT_AGREEMENT_VERSION = "v2.3"') == 1, "毒锚没找到 —— 版本常量改了写法?"
    drifted = {"api/partner_api.py":
               src.replace('CURRENT_AGREEMENT_VERSION = "v2.3"', 'CURRENT_AGREEMENT_VERSION = "v9.9"')}
    red = [f for f in enumerate_sources(drifted, head["tree"]) if f.shape == "fstring"]
    assert [(f.kind, f.alternatives) for f in red] == [
        ("missing", ("docs/条款/服务商申请协议_v9.9.md", "docs/条款/代理合作协议_v9.9.md"))]
    # 对照臂:版本不动 ⇒ 当前版本那份在
    ok = [f for f in enumerate_sources({"api/partner_api.py": src}, head["tree"]) if f.shape == "fstring"]
    assert [(f.kind, f.resolved) for f in ok] == [("file", "docs/条款/服务商申请协议_v2.3.md")]


def test_tooth_prose_punctuation_alone_keeps_a_string_out(head):
    """标点过滤**单独**起决定作用的一组输入(注毒实测:没有这格时删掉标点过滤一格都不红 ——
    下面那格的散文都没有扩展名,先被容器规则的扩展名要求挡掉了)。"""
    assert enumerate_sources({_SYNTH: 'X = "knowledge/附录：说明.md"\n'}, head["tree"]) == []
    # 对照臂:同一串去掉全角冒号 ⇒ 仓根相对规则收它,且判 missing(证明决定结果的就是那个冒号)
    got = enumerate_sources({_SYNTH: 'X = "knowledge/附录说明.md"\n'}, head["tree"])
    assert [(f.shape, f.kind) for f in got] == [("repo_relative", "missing")]


def test_tooth_prose_with_slash_is_not_a_path(head):
    """首跑时容器规则把「你还记得第一个客户/第一份工作吗？」这类散文当成了路径 —— 锁住它不再回来。
    这格守的是容器规则的**扩展名要求**(`AI/SaaS软件`、`500-50000元/年` 无标点、只能靠它挡)。"""
    src = ("from pathlib import Path\n"
           "D = Path(__file__).parent.parent / 'data'\n"
           "QUESTIONS = ['你还记得第一个客户/第一份工作吗？', 'AI/SaaS软件', '500-50000元/年']\n"
           "def f(q):\n"
           "    return D / q\n")
    found = enumerate_sources({_SYNTH: src}, head["tree"])
    assert found == [], found


# ══════════════════════════════════════════════════════════════════
# 出声:两处原来静默给空的加载,缺文件必出 ERROR 并计数(行为不变)
# ══════════════════════════════════════════════════════════════════

def test_knowledge_manager_missing_files_log_error_and_count(tmp_path, monkeypatch, caplog):
    import asyncio

    import utils.knowledge_manager as km
    from services.runtime_file_alarm import missing_counts

    monkeypatch.setattr(km, "KNOWLEDGE_DIR", tmp_path)
    before = missing_counts()
    mgr = km.GEOKnowledgeManager()
    with caplog.at_level(logging.ERROR, logger="GEO-KnowledgeManager"):
        asyncio.run(mgr.initialize())
    errs = [r for r in caplog.records if r.levelno == logging.ERROR and "Knowledge file not found" in r.getMessage()]
    assert len(errs) == len(km.KNOWLEDGE_BASES), [r.getMessage() for r in caplog.records]
    assert mgr.knowledge_contents == {}, "行为变了:缺文件时应照旧降级为空,而不是抛或造内容"
    after = missing_counts()
    for cfg in km.KNOWLEDGE_BASES.values():
        key = str(tmp_path / cfg["file"])
        assert after.get(key, 0) == before.get(key, 0) + 1, key


def test_knowledge_manager_real_dir_loads_all_without_error(caplog):
    import asyncio

    import utils.knowledge_manager as km

    mgr = km.GEOKnowledgeManager()
    with caplog.at_level(logging.ERROR, logger="GEO-KnowledgeManager"):
        asyncio.run(mgr.initialize())
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], [r.getMessage() for r in caplog.records]
    assert sorted(mgr.knowledge_contents) == sorted(km.KNOWLEDGE_BASES)
    assert all(v["content"].strip() for v in mgr.knowledge_contents.values())


def test_distiller_missing_case_library_logs_error_and_real_dir_loads(tmp_path, monkeypatch, caplog):
    import writing.distiller as dist

    pipe = dist.DistillerPipeline({"industry": "GEO 优化服务", "brand_name": "x"})
    monkeypatch.setattr(dist, "KNOWLEDGE_BASE_DIR", str(tmp_path))
    with caplog.at_level(logging.ERROR, logger="GEO-Distiller"):
        assert pipe._load_case_library() == ""
    assert [r for r in caplog.records if "Case library dir not found" in r.getMessage()]
    caplog.clear()
    monkeypatch.undo()           # 对照臂:真目录 ⇒ 读得到范文、不出 ERROR
    with caplog.at_level(logging.ERROR, logger="GEO-Distiller"):
        loaded = pipe._load_case_library()
    assert loaded.strip(), "改名后案例库仍读不到"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
