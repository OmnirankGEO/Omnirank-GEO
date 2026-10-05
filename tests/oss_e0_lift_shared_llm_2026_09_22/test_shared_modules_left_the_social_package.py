# -*- coding: utf-8 -*-
"""WO_259 · 开源 E0:GEO 主链的地基不再长在「E3 要整包删」的社媒包里。

事实:社媒包的 `__init__.py` 原来是**包顶层 eager import**
(改写工具 / 调研中心 / 内容工坊)⇒ 任何一句从包里引 key 池的 import 都会把**整个社媒包**拉起来。
[开源 E3 · B2 · 2026-09-28] 社媒包连同两个兼容垫已整删(路径登记在 E2 的 RETIRED_PATHS):
  「包 __init__ 懒加载」「垫子存在且标了删」两格退役;①④ 改为「旧路径**找不到**」—— 不再需要旧模块计数臂。
而 GEO 主链(诊断 / 写作 / 报价 / 元指令 13 的 `ensure_brand_fields`)
有 **33 个在役文件 · 61 处引用**落在那两个公共模块上。

不先上提就删:server 仍然起得来(注册 fail-open),**功能悄悄没了**。

本包四把锁,各带反臂(反臂由注毒跑,见交付单):
  ① 导入图锁 —— 新模块导入得起来,旧包路径 find_spec 为空(包已删)
  ② 引用方归零锁 —— 在役目录旧路径计数 = 0,**并打印基线计数作正控**
  ③ 空语料启动锁 —— `data/advisors` 不在时,注册与一次 `advisor_generate` 不抛
  ④ 行为不变 —— 两条真链各断言「调用真的到了新模块」(新模块计数 ≥1;旧模块已删)
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: 本单的基线 —— 正控从这个 sha 上取"改前计数"。
BASE_SHA = "37a8f82e1"

#: 在役目录(社媒包自身不算 —— 它与兼容垫同批被 E3 删)
IN_SERVICE_PREFIXES = (
    "api/", "services/", "agents/", "workflows/", "writing/",
    "config/", "tools/", "db/", "advisors/",
)
#: 点分 / 斜杠两种写法都数(包已随开源 E3 B2 整删;基线上它们都在,正控照样 > 0)
OLD_PATH_RE = re.compile(rb"tools[./]social_operator[./](deepseek_key_pool|advisor_llm)")


def _in_service_files():
    for p in sorted(REPO.rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if rel.startswith(IN_SERVICE_PREFIXES) or rel == "server.py":
            yield rel, p


# ══════════════════════════════════════════════════════════════════
# ① 导入图锁
# ══════════════════════════════════════════════════════════════════

def test_importing_the_lifted_modules_does_not_drag_in_the_social_package():
    """🔴 本单的全部意义:引一个公共模块,不该把整个社媒包拉起来。

    在**子进程**里跑 —— 同进程里别的用例可能已经 import 过社媒包,
    那样这把锁量到的是"本轮测试的历史",不是"引这几个模块的后果"。
    """
    # [开源 E3 · B2] 包已删:判据改为「新模块导入得起来 + 旧包路径 find_spec 为空」;
    #   对照臂:同一个 find_spec 对在役的 services.llm 必须非空(防一把恒返 None 的尺子)。
    code = (
        "import sys, importlib.util as u;"
        "import services.llm.deepseek_key_pool, services.llm.advisor_llm;"
        "import advisors;"
        "gone=u.find_spec('tools.' + 'social_operator') is None;"
        "live=u.find_spec('services.llm') is not None;"
        "print('GONE=%s LIVE=%s' % (gone, live));"
        "sys.exit(0 if (gone and live) else 1)"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, "新模块导入失败,或旧包路径又能找到了:%s" % (r.stdout or r.stderr)[-400:]


def test_the_lifted_modules_do_not_reference_the_social_package_at_all():
    """🔴 静态面:新家里一个引社媒包(包已随开源 E3 B2 删)的 import 都不许有。

    (运行时那把锁只看"这次有没有被拉起来";一行藏在冷分支里的 import
     运行时可能不触发,静态这一格把它也挡住。)

    🔴 查的是 **import 语句**不是文本:本包的 docstring 里就写着
       「本包不 import 任何社媒包模块」—— 按文本数会被自己那句话命中
       (本仓记过:文本计数分不出代码与注释)。
    """
    import ast

    bad = []
    for p in sorted((REPO / "services" / "llm").rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"), p.name)
        for n in ast.walk(tree):
            mod = None
            if isinstance(n, ast.ImportFrom):
                mod = n.module or ""
            elif isinstance(n, ast.Import):
                mod = ",".join(a.name for a in n.names)
            if mod and "tools.social_operator" in mod:  # 包已随开源 E3 B2 整删,仍按此名拦:防它被加回来
                bad.append("%s:%d" % (p.relative_to(REPO).as_posix(), n.lineno))
    assert not bad, "新模块里还引着社媒包:%s" % (bad,)


# [开源 E3 · B2] 「包 __init__ 懒加载」退役:包已整删,无 __init__ 可测。


# ══════════════════════════════════════════════════════════════════
# ② 引用方归零锁(带正控)
# ══════════════════════════════════════════════════════════════════

def _blobs_at(sha: str, rels: list[str]) -> dict[str, bytes]:
    """[ONESHOT 提速] 一个 `git cat-file --batch` 进程读完全部 `sha:rel`(原先每个文件起一次 `git show`,
    ~1300 次子进程 ≈ 23s)。基线上不存在的文件回 `missing` ⇒ 按空内容算,与原 `git show` 失败时 stdout 为空一致。"""
    req = b"".join(("%s:%s\n" % (sha, rel)).encode("utf-8") for rel in rels)
    out = subprocess.run(["git", "cat-file", "--batch"], cwd=REPO, input=req,
                         capture_output=True, check=True).stdout
    blobs, pos = {}, 0
    for rel in rels:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].split(b" ")
        pos = nl + 1
        if header[-1] == b"missing" or header[1] != b"blob":
            blobs[rel] = b""
            continue
        size = int(header[2])
        blobs[rel] = out[pos:pos + size]
        pos += size + 1  # 内容后跟一个 LF
    assert pos == len(out), "cat-file --batch 输出没读完 —— 解析与请求不对齐"
    return blobs


def _count_at(sha: str | None) -> tuple[int, int]:
    """(文件数, 引用行数)。`sha=None` 取工作树。"""
    files = 0
    lines = 0
    entries = list(_in_service_files())
    at_sha = _blobs_at(sha, [rel for rel, _ in entries]) if sha is not None else {}
    for rel, p in entries:
        raw = p.read_bytes() if sha is None else at_sha[rel]
        n = sum(1 for line in raw.split(b"\n") if OLD_PATH_RE.search(line))
        if n:
            files += 1
            lines += n
    return files, lines


def test_no_in_service_file_still_imports_the_old_path():
    """🔴 在役目录旧路径计数 = 0,**并打印基线计数作正控**。

    只断言"现在是 0"是买不到负控的:一把恒返 0 的尺子也能满分。
    所以同一把尺子去基线 `%s` 上量一遍 —— 那里必须**大于 0**,
    证明它确实数得出东西来。
    """ % BASE_SHA
    now_files, now_lines = _count_at(None)
    base_files, base_lines = _count_at(BASE_SHA)
    print("[正控] 基线 %s:文件 %d · 引用行 %d → 现在:文件 %d · 引用行 %d"
          % (BASE_SHA, base_files, base_lines, now_files, now_lines))
    assert base_lines > 0, (
        "尺子在基线上也量到 0 —— 它坏了,不是我改干净了(正控不成立)")
    assert now_lines == 0, "还有 %d 个文件 %d 行引着旧路径" % (now_files, now_lines)


# [开源 E3 · B2] 「兼容垫存在且标 E3 删」退役:两个垫子已按标记随包删除。


# ══════════════════════════════════════════════════════════════════
# ③ 空语料启动锁
# ══════════════════════════════════════════════════════════════════

def test_the_advisor_core_starts_with_no_corpus_directory(tmp_path, monkeypatch):
    """🔴 E4 会删 `data/advisors/**` 的课程语料,**执行内核要留**。

    这一格在一个**没有 data/advisors 的空目录**里构造 advisor 并走一次生成:
    不抛、拿到 LLM 结果(LLM 已 mock —— 本格验的是"没有语料也能活",不是 LLM)。

    ⚠️ 这条**今天就成立**(`base_advisor.__init__` 会 mkdir,`:679` 有 exists 守卫)。
       本单不改它,只把它钉住 —— 免得 E4 删语料时才发现它其实不成立。
    """
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "data" / "advisors").exists()

    from advisors.base_advisor import BaseAdvisor
    a = BaseAdvisor(advisor_id="probe", name="探针", base_prompt="你是探针")
    assert a.get_profile()["document_count"] == 0

    # 🔴 真正走一遍**读语料**那条路(`load_documents` 里的
    #    `if not self.knowledge_path.exists(): return` 就是那道守卫)。
    #    第一版这一格只 new 了个对象就完事 —— 注毒把守卫改成 `if False:` 时它**照样绿**,
    #    因为那条路根本没被走到。锁必须**碰到**它声称在保护的那一行。
    #    ⚠️ 构造器自己会 `mkdir(parents=True, exist_ok=True)` —— 也就是说
    #       E4 删掉语料目录后,它会被**悄悄重建成空目录**(不崩,但也别以为它还在)。
    #       所以要验"目录不存在"这条路,得指到一个它没建过的地方。
    a.knowledge_path = pathlib.Path(tmp_path) / "never-created"
    assert not a.knowledge_path.exists(), "前提:这个目录必须不存在"
    a.load_documents()          # 守卫在 ⇒ 直接 return;守卫没了 ⇒ glob 一个不存在的目录
    assert a.documents == []


def test_advisor_generate_degrades_to_plain_llm_without_corpus(tmp_path, monkeypatch):
    """🔴 空语料下 `advisor_generate` **退化为纯 LLM**,不抛。"""
    monkeypatch.chdir(tmp_path)
    import services.llm.advisor_llm as mod

    async def _fake(prompt):
        return "LLM-OK"

    monkeypatch.setattr(mod, "_fallback_llm", _fake)
    monkeypatch.setattr(mod, "_get_advisor", lambda advisor_id=None: None)
    out = asyncio.run(mod.advisor_generate("你好"))
    assert out == "LLM-OK", "空语料下没有退化到纯 LLM:%r" % out


# ══════════════════════════════════════════════════════════════════
# ④ 行为不变:调用真的到了新模块
# ══════════════════════════════════════════════════════════════════

def test_llm_utils_takes_its_key_from_the_new_module(monkeypatch):
    """🔴 `writing/llm_utils` 取 key 走**新模块**(新模块计数 ≥1)。

    原先还要「旧模块计数 = 0」—— 兼容垫会把旧路径转发到新模块,只看新计数分不出走哪条路。
    [开源 E3 · B2] 垫子随社媒包整删,旧路径已不存在(① 的 find_spec 臂钉住),旧计数臂随之退役。
    """
    import services.llm.deepseek_key_pool as new_mod

    hits = {"new": 0}
    monkeypatch.setattr(new_mod, "pick_deepseek_api_key",
                        lambda *a, **k: (hits.__setitem__("new", hits["new"] + 1), "sk-new")[1])

    # 写作模块默认 provider 就是 deepseek(`writing/llm_utils.py:51`),
    # 所以走一次真的 `get_llm_config` 就会命中取 key 那一段。
    import writing.llm_utils as lu
    api_url, api_key, model, provider = lu.get_llm_config("geo_article", module="writing")
    assert provider == "deepseek", "写作默认 provider 不再是 deepseek —— 判据前提变了"
    assert hits["new"] >= 1, "没有走到新模块(新 %d)" % hits["new"]


def test_brand_field_suggester_calls_the_new_advisor_module(monkeypatch):
    """🔴 元指令 13「永远不中断对话」的实现点(`_llm_infer` → `advisor_flash`)
    必须落在新模块上。它断了,补齐链就退化成"返回 None",而**页面上什么都不会说**。
    [开源 E3 · B2] 旧垫子随包删除,旧计数臂退役(旧路径不存在由 ① 钉住)。
    """
    import services.llm.advisor_llm as new_mod

    hits = {"new": 0}

    async def _new(prompt, history=None):
        hits["new"] += 1
        return '{"industry": "酒店住宿"}'

    monkeypatch.setattr(new_mod, "advisor_flash", _new)

    from agents.brand_field_suggester import _llm_infer
    got = asyncio.run(_llm_infer("猜一下行业"))
    assert got == {"industry": "酒店住宿"}, got
    assert hits["new"] == 1, "新 %d" % hits["new"]
