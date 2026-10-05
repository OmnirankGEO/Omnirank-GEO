"""E0c · 小帮识图依赖的看图转文字上提出社媒 router 之后的锁(2026-09-27)。

背景:`POST /api/xiaobang/parse-image`(在役:XiaobangChatInput、FeedbackPage)→ `services/chat_attachments.parse_image`
→ 原来函数体内 import 社媒主路径 router 里的 `_vision_image_to_text`;而那个 router 顶层 import 社媒包(已随 E3 B1b-2b 整文件删)
(E3_DELETION_MAP §6-2)。E0c 把 `_VISION_MIME_MAP` + `_vision_image_to_text` 逐字搬到 `services/image_to_text.py`。

守四件事:
  ① 新模块(含函数体内的延迟 import)不许 import 社媒代码(社媒工具包 / 社媒主路径 router / 社媒 agent,见 SOCIAL);
  ② `parse_image` 的函数体不许再引社媒主路径 router;
  ③ 旧位置的 re-export 垫与新位置是同一个对象;
  ④ **模拟 E3 删包**:社媒三个模块在 sys.modules 里被屏蔽(一 import 就 ImportError),parse_image 照样跑通 ——
     只在 HTTP 与记账两层打桩,搬过去的 `_vision_image_to_text` 本体真跑。
①② 的牙证与对照臂走与真判据同一个函数,只换输入。
"""

from __future__ import annotations

import ast
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SOCIAL = ("tools.social_operator", "api.social_mainpath_api", "agents.social_agent")  # 三件均已删(router 随 B1b-2b,包与 agent 随 B2),仍留在禁止表里:防它被加回来


def _read(rel: str) -> str:
    return (ROOT / rel).read_bytes().decode("utf-8")


def _social_imports(src: str, only_func: str | None = None) -> list[str]:
    """整棵 AST(或只看某个函数体)里命中社媒模块的 import。"""
    tree = ast.parse(src)
    roots = [tree]
    if only_func:
        roots = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == only_func]
        assert roots, f"找不到函数 {only_func}"
    hits = []
    for root in roots:
        for n in ast.walk(root):
            names = []
            if isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                # `from api import <模块>` 的 module 只是 `api` —— 要把导入名拼上才看得见
                names = [n.module or ""] + [f"{n.module}.{a.name}" for a in n.names if n.module]
            for nm in names:
                if any(nm == s or nm.startswith(s + ".") for s in SOCIAL):
                    hits.append(f"L{n.lineno}: {nm}")
    return hits


# ---------- ① 新模块不许 import 社媒代码 ----------

def test_new_module_has_no_social_imports() -> None:
    assert _social_imports(_read("services/image_to_text.py")) == []


def test_social_import_checker_teeth_and_control() -> None:
    base = _read("services/image_to_text.py")
    for poison in ("\ndef _p():\n    from api.social_mainpath_api import _extract_upload_text\n",  # 已随 E3 B1b-2b 删除,仍留在禁止表里:防它被加回来
                   "\nimport tools.social_operator.file_analyzer\n",  # 毒串:包已删,写法仍须被抓
                   "\nfrom agents.social_agent import get_social_agent\n",  # 毒串:包已删,写法仍须被抓
                   "\ndef _q():\n    from api import social_mainpath_api as _legacy\n"):  # 已随 E3 B1b-2b 删除,仍留在禁止表里:防它被加回来
        assert _social_imports(base + poison), f"注毒没被抓到:{poison!r}"
    # 对照臂:名字带 social 字样但不是这三个模块(真实在役模块)⇒ 绿
    assert _social_imports(base + "\nfrom db.social_preferences_db import get_admin_setting\n") == []


# ---------- ② parse_image 不再引社媒主路径 router ----------

def test_parse_image_body_has_no_social_imports() -> None:
    assert _social_imports(_read("services/chat_attachments.py"), only_func="parse_image") == []


# ---------- ③ 旧位置随文件删除(原「垫子同一性」格;垫子在 E3 B1b-2b 随社媒主路径 router 一起删了)----------

def test_old_location_is_gone_with_its_shim() -> None:
    assert not (ROOT / "api" / "social_mainpath_api.py").exists()  # 已删;加回来 ⇒ 红
    from services import image_to_text

    assert callable(image_to_text._vision_image_to_text) and image_to_text._VISION_MIME_MAP


# ---------- ④ 模拟 E3 删包:parse_image 照样跑通 ----------

class _FakeResp:
    status_code = 200
    text = "ok"

    def json(self):
        return {"choices": [{"message": {"content": "  图里写着:开业大吉  "}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class _FakeClient:
    sent = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.sent.append((url, json))
        return _FakeResp()


class _Tracker:
    def record(self, **_k):
        pass


@asynccontextmanager
async def _fake_track(*_a, **_k):
    yield _Tracker()


def test_parse_image_works_with_social_modules_blocked(monkeypatch) -> None:
    import httpx
    from services import chat_attachments

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-not-a-real-key")
    _FakeClient.sent.clear()
    blocked = {m: None for m in SOCIAL}  # sys.modules 里置 None ⇒ 再 import 就 ImportError(= 删包后的样子)
    # 只置 None 不够:若本进程先 import 过,子模块已挂成父包属性,`from api import <模块>`
    # 会直接取属性、不查 sys.modules(09-27 注毒第 4 发实测漏过)⇒ 同时摘掉父包上的属性
    import importlib
    for m in SOCIAL:
        parent, _, child = m.rpartition(".")
        try:
            monkeypatch.delattr(importlib.import_module(parent), child, raising=False)
        except ImportError:
            pass
    with patch.dict(sys.modules, blocked), \
         patch.object(httpx, "AsyncClient", _FakeClient), \
         patch("tools.llm_call_tracker.llm_track", _fake_track):
        res = asyncio.run(chat_attachments.parse_image(b"\x89PNG fake", "shop.png"))
    assert res.ok and res.status == "parsed", (res.status, res.error)
    assert "开业大吉" in res.markdown_summary
    assert len(_FakeClient.sent) == 1 and "dashscope" in _FakeClient.sent[0][0]
    assert _FakeClient.sent[0][1]["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
