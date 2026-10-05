"""开源 E3 · B2 · 社媒工具包整删 + 社媒 agent + E3 孤儿之后的锁(2026-09-28)。

B2 删了三类:① `tools/social_operator/**` 整包(54 个文件,含 E0 上提时留下的兼容垫);② `agents/social_agent.py`;
③ 32 个 E3 孤儿(前几片删掉入口后,从 server / prestart / main / scheduler 四个根都不可达的模块)+ 局部重写工具。
E2 的 RETIRED_PATHS 只登记了 ①②;本包把 ③ 连同 ①② 一起按**完整模块路径**钉住:

守三件事(全是静态扫描,不起应用):
1. 文件都不在;全仓已跟踪 .py 没有任何一处 import 它们(含函数体内延迟导入、`from pkg import mod` 形、相对导入);
2. 非测试的代码 / 配置里没有它们的点分或斜杠完整路径字面量(按字符串动态导入的也拦;与普查门同一张白名单);
3. 普查门 ⑦ 对「文件名太泛」的键不搜(trace / chat_db / document / meeting_room / soul 等只提示人工看)——
   本包按完整路径逐个钉,补上那一块。
对照臂:同一套扫描器对在役模块(services.llm.advisor_llm / services.chat_attachments)必须扫得到。
"""
import ast
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

RETIRED_PACKAGES = ["tools.social_operator"]
RETIRED_MODULES = [
    "agents.social_agent",
    # E3 孤儿(不可达:在 E3 前的基线上可达、现在从四个根都不可达)
    "agents.omni_router", "agents.page_knowledge", "agents.sse_protocol",
    "config.agent_loop_kill_switch",
    "db.author_db", "db.benchmark_db", "db.chat_attachment_billing_db", "db.chat_db", "db.competitor_db",
    "db.corpus_db", "db.interview_db", "db.method_selection_log", "db.social_match_db",
    "db.social_pending_framework_db", "db.social_product_db", "db.topic_similarity",
    "meeting", "meeting.meeting_room",
    "services.capability_handler", "services.capability_pre_router", "services.content_scoring",
    "services.geo_plan_worker", "services.profile_completeness_v2", "services.research_follow_up",
    "services.sse_event_mux", "services.sse_progress_backbone", "services.sse_progress_dynamic",
    "tools.agent_loop.confirm_callback", "tools.agent_loop.trace", "tools.agent_loop.write_tools.regenerate_part",
    "tools.document", "tools.document.document_parser",
    "utils.m3_stage_validator",
]
ALL = RETIRED_PACKAGES + RETIRED_MODULES
LIVE_CONTROL = {"services.llm.advisor_llm": "agents/brand_field_suggester.py",
                "services.chat_attachments": "api/xiaobang_api.py"}


def _path_of(mod: str) -> Path:
    return REPO.joinpath(*mod.split("."))


def _resolve(rel: str, node: ast.ImportFrom) -> str:
    """相对导入解析成绝对模块名(以文件所在包为锚)。"""
    if node.level == 0:
        return node.module or ""
    pkg = rel.rsplit("/", 1)[0].split("/") if "/" in rel else []
    base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
    return ".".join(base + ([node.module] if node.module else []))


def _imported_names(rel: str, src: str) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            names |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mod = _resolve(rel, n)
            names.add(mod)
            names |= {f"{mod}.{a.name}" for a in n.names}
    return names


def _importers(targets: list[str], files: list[str] | None = None, extra_src: dict[str, str] | None = None) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {t: [] for t in targets}
    srcs = dict(extra_src or {})
    last = {t: t.rsplit(".", 1)[-1] for t in targets}
    if files is None:  # 先按末段名字一次 git grep 粗筛(只为提速),粗筛命中的再逐个 AST 判
        args = ["git", "grep", "-l", "-z", "-F", "-w"]
        for n in sorted(set(last.values())):
            args += ["-e", n]
        out = subprocess.run(args + ["--", "*.py"], cwd=str(REPO), capture_output=True).stdout
        files = [x for x in out.decode("utf-8").split("\0") if x]
    for rel in files:
        if rel in srcs:
            continue
        try:
            srcs[rel] = (REPO / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    for rel, src in srcs.items():
        if not any(last[t] in src for t in targets):  # 只为提速:名字都不出现的文件不可能导入它
            continue
        try:
            names = _imported_names(rel, src)
        except SyntaxError:
            continue
        for t in targets:
            if any(nm == t or nm.startswith(t + ".") for nm in names):
                hits[t].append(rel)
    return hits


def test_retired_files_are_gone():
    for m in ALL:
        p = _path_of(m)
        assert not p.with_suffix(".py").exists() and not p.is_dir(), m


def test_nothing_imports_a_retired_module():
    bad = {k: v for k, v in _importers(ALL).items() if v}
    assert not bad, bad


def test_import_scanner_teeth_and_control_arm():
    # 牙证:四种写法(绝对 / 函数体内延迟 / from 包 import 子模块 / 相对导入)注进一个假文件,每种都必须抓到
    probes = {
        "services/_probe_a.py": "from services.geo_plan_worker import dispatch_worker\n",
        "services/_probe_b.py": "def f():\n    import tools.agent_loop.trace as t\n    return t\n",
        "db/_probe_c.py": "from db import chat_db\n",
        "services/_probe_d.py": "from .content_scoring import _calculate_unified_score\n",
    }
    got = _importers(["services.geo_plan_worker", "tools.agent_loop.trace", "db.chat_db", "services.content_scoring"],
                     files=[], extra_src=probes)
    assert all(got.values()), got
    # 对照臂:在役模块在真仓里扫得到(扫描器没瞎)
    live = _importers(list(LIVE_CONTROL))
    for mod, importer in LIVE_CONTROL.items():
        assert importer in live[mod], (mod, live[mod])


LITERAL_GLOBS = ("*.py", "*.json", "*.ts", "*.tsx", "*.mjs", "*.sh", "*.conf", "*.yml", "*.yaml", "*.toml", "*.ini")


def _comment_allowlist() -> set[tuple[str, str]]:
    p = REPO / "tests" / "REFERENCER_COMMENT_ALLOW.txt"
    out = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                cols = line.split("\t")
                if len(cols) == 4:  # 文件 · 类型 · 行原文 · 理由(普查门同一张表;类型合规由普查门核)
                    out.add((cols[0].strip(), cols[2].strip()))
    return out


def _literal_hits(needles: list[str], word: bool) -> dict[str, list[str]]:
    """一次 git grep 查一批字面量(-F;点分形加 -w,斜杠形不加),按命中的是哪条归并;白名单行不算。"""
    hits: dict[str, set[str]] = {n: set() for n in needles}
    if not needles:
        return {}
    args = ["git", "grep", "-n", "-z", "-F"] + (["-w"] if word else [])
    for n in needles:
        args += ["-e", n]
    r = subprocess.run(args + ["--", *LITERAL_GLOBS, ":(exclude)tests", ":(exclude)docs",
                               ":(exclude,glob)agent-test-artifacts*/**"],  # 只读历史证据目录:与普查门 ⑦ 同口径(运行者保险由普查门核)
                       cwd=str(REPO), capture_output=True)
    allowed = _comment_allowlist()
    for rec in r.stdout.decode("utf-8").splitlines():
        parts = rec.split("\0")
        if len(parts) < 3:
            continue
        path, text = parts[0], parts[2]
        if (path, text.strip()) in allowed:
            continue
        for n in needles:
            if n in text:
                hits[n].add(path)
    return {n: sorted(v) for n, v in hits.items() if v}


def _literal_mentions(needle: str) -> list[str]:
    word = not needle.endswith(("/", ".py"))
    return _literal_hits([needle], word).get(needle, [])


def test_no_full_path_literal_survives_in_code_or_config():
    """完整路径两种写法都查(点分拦字符串动态导入,斜杠拦脚本 / 配置里的文件路径)。
    顶层裸包名(meeting)两种形都是普通词 / 前端路由片段(`/employees/meeting/`),由包内模块的完整路径钉。"""
    pkgs = set(RETIRED_PACKAGES) | {"tools.document"}
    mods = [m for m in ALL if "." in m]
    dotted = _literal_hits(mods, word=True)
    slashed = _literal_hits([m.replace(".", "/") + ("/" if m in pkgs else ".py") for m in mods], word=False)
    bad = {**dotted, **slashed}
    assert not bad, bad


def test_literal_scanner_control_arm_sees_a_live_module():
    assert "agents/brand_field_suggester.py" in _literal_mentions("services.llm.advisor_llm")
