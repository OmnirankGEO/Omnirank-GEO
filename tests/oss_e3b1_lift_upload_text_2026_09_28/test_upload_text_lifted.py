"""开源 E3 · B1b-1 · 小帮「解析文件」依赖链上提出社媒代码之后的锁(2026-09-28)。

背景:在役 `POST /api/xiaobang/parse-file` → `services/chat_attachments.parse_document` → 原来函数体内
从社媒主路径 router 里取 `_extract_upload_text`,而后者又用社媒包里的 `file_analyzer`;两者都随 E3 删(router 已在 B1b-2b 删)。
B1b-1 把 `_extract_upload_text` 及其 PDF / 解码依赖逐字搬到 `services/upload_text.py`,把 `FileAnalyzer` 那套逐字搬到
`services/file_analyzer.py`,旧位置留 re-export 垫(标 E3 删)。

守三件事:
  ① 两个新模块(含函数体内延迟导入)不许 import 社媒代码,也不许按字面量写这三个模块名(防字符串动态导入);
  ② `parse_document` 不再引社媒主路径 router;旧位置(社媒包里分析器的垫子、router)都已不在
     ([开源 E3 · B2 · 2026-09-28] 社媒包连同垫子整删:原「垫子与新位置同一对象」改为「旧位置不在、新位置可调」);
  ③ **模拟删包**:干净子进程里把三个社媒模块屏蔽(一 import 就 ImportError),`parse_document` 照样解析
     `.txt`(解码路径)与 `.log`(先走上提的 FileAnalyzer,读不出才退到解码),并直接调上提的 `analyze_file`
     读一个 .txt(`.log` 路径咬不到 FileAnalyzer 本体,所以单独调)—— 对照臂:不屏蔽时读数相同;
     B2 起三件都已真删,对照臂里它们同样导入不了(= 真删,不是拦截器的效果)。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOCIAL = ("tools.social_operator", "api.social_mainpath_api", "agents.social_agent")  # 已随 E3 B1b-2b 删除,仍留在禁止表里:防它被加回来
NEW_MODULES = ("services/upload_text.py", "services/file_analyzer.py")


def _social_imports(src: str) -> list[str]:
    bad = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            bad += [a.name for a in n.names if a.name.startswith(SOCIAL)]
        elif isinstance(n, ast.ImportFrom) and n.module:
            full = [n.module] + [f"{n.module}.{a.name}" for a in n.names]
            bad += [x for x in full if x.startswith(SOCIAL)]
    return bad


def test_new_modules_do_not_import_or_name_social_code():
    for rel in NEW_MODULES:
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert _social_imports(src) == [], (rel, _social_imports(src))
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        # 模块 docstring 里点名旧位置是说明,不算;字面量检查只看 docstring 之外
        body = code.split('"""', 2)[-1] if code.lstrip().startswith(('"""', "# -*-")) else code
        for m in SOCIAL:
            assert m not in body, (rel, m)


def test_import_checker_has_teeth():
    # 牙证:同一个检查器对一段延迟导入社媒代码的源码必须报出来
    probe = "def f():\n    from api.social_mainpath_api import _extract_upload_text\n    return _extract_upload_text\n"  # 已随 E3 B1b-2b 删除,仍留在禁止表里:防它被加回来
    assert _social_imports(probe) == ["api.social_mainpath_api", "api.social_mainpath_api._extract_upload_text"]  # 已随 E3 B1b-2b 删除,仍留在禁止表里:防它被加回来


def test_parse_document_no_longer_reaches_into_the_social_router():
    tree = ast.parse((ROOT / "services/chat_attachments.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "parse_document")
    assert _social_imports(ast.unparse(fn)) == []


def test_old_locations_are_gone_and_new_ones_callable():
    """[开源 E3 · B2] 旧位置两处都已删(router 随 B1b-2b,社媒包里的分析器垫子随 B2 整包);新位置的函数都在。"""
    import services.file_analyzer as nfa
    import services.upload_text as nut

    assert not (ROOT / "api" / "social_mainpath_api.py").exists()
    assert not (ROOT / "tools" / "social_operator").exists()
    for name in ("FileAnalyzer", "ExtractedInfo", "analyze_file", "analyze_text", "get_file_analyzer"):
        assert getattr(nfa, name, None) is not None, name
    for name in ("_extract_upload_text", "_decode_uploaded_bytes", "_extract_pdf_with_vision",
                 "_pdf_pages_to_images_via_vision", "_extract_pdf_text_fallback"):
        assert callable(getattr(nut, name)), name


_CHILD = r'''
import asyncio, json, sys
BLOCK = __BLOCK__
SOCIAL = ("tools.social_operator", "api.social_mainpath_api", "agents.social_agent")  # 已随 E3 B1b-2b 删除,仍留在禁止表里:防它被加回来
class _Block:
    def find_spec(self, name, path=None, target=None):
        if BLOCK and any(name == p or name.startswith(p + ".") for p in SOCIAL):
            raise ImportError("E3 删包模拟: " + name)
sys.meta_path.insert(0, _Block())
out = {}
for p in SOCIAL:
    try:
        __import__(p); out[p] = "importable"
    except ImportError:
        out[p] = "blocked"
from services.chat_attachments import parse_document
txt = "第一行 hello\n第二行 world\n".encode("utf-8")
r1 = asyncio.run(parse_document(txt, "note.txt", "text/plain"))
r2 = asyncio.run(parse_document(txt, "note.log", "text/plain"))
out["txt"] = [r1.ok, r1.status, r1.char_count, "hello" in (r1.markdown_summary or "")]
out["log"] = [r2.ok, r2.status, r2.char_count, "world" in (r2.markdown_summary or "")]
import tempfile, os
from services.file_analyzer import analyze_file
with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as t:
    t.write(txt); tp = t.name
try:
    fa = analyze_file(tp)
finally:
    os.remove(tp)
out["fa"] = [bool(fa.get("success")), "world" in (fa.get("raw_text") or "")]
out["social_loaded"] = sorted(m for m in sys.modules if m.startswith(SOCIAL))
print("RESULT=" + json.dumps(out, ensure_ascii=False))
'''


def _child(block: bool) -> dict:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT)}
    if env.get("TEST_DATABASE_URL"):
        env["DATABASE_URL"] = env["TEST_DATABASE_URL"]
    r = subprocess.run([sys.executable, "-c", _CHILD.replace("__BLOCK__", "True" if block else "False")],
                       cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=180)
    lines = [l for l in r.stdout.splitlines() if l.startswith("RESULT=")]
    assert r.returncode == 0 and lines, f"rc={r.returncode}\n{r.stdout[-1500:]}\n{r.stderr[-2500:]}"
    return json.loads(lines[-1][len("RESULT="):])


def test_parse_document_works_with_the_social_packages_gone():
    out = _child(block=True)
    assert all(out[p] == "blocked" for p in SOCIAL), out
    assert out["txt"][:2] == [True, "parsed"] and out["txt"][3], out
    assert out["log"][:2] == [True, "parsed"] and out["log"][3], out
    # 上提的 FileAnalyzer 本体真跑(.log 路径在它读不出时会退到解码,咬不到它;这里直接调)
    assert out["fa"] == [True, True], out
    assert out["social_loaded"] == [], out
    # 对照臂:不屏蔽时读数一样 —— 证明上面的绿不是拦截器没生效、也不是解析行为变了
    ctrl = _child(block=False)
    # [开源 E3 · B2] 三件都已真删:不屏蔽时同样导入不了 —— 读数相同靠的是真删,不是拦截器
    assert all(ctrl[p] == "blocked" for p in SOCIAL), ctrl
    assert (ctrl["txt"], ctrl["log"], ctrl["fa"]) == (out["txt"], out["log"], out["fa"]), (ctrl, out)
