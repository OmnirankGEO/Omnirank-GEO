"""E0b · 统一评分与视频链接取数上提出社媒包之后的锁(2026-09-26)。

守四件事([开源 E3 · B2 · 2026-09-28] 社媒包连同垫子整删;统一评分 services/content_scoring.py
与能力处理器随后成了不可达孤儿,也一并删了 ⇒ ①② 的名单去掉这两个文件,③ 与 AF 两格退役):
  ① 上提后的新位置(services/video_link.py · tools/agent_loop/adapters/video.py)
     不许 import 社媒改写代码(一键改写 / 内容工坊 / advisor_llm / 整个社媒工具包,名单见 FORBIDDEN_PARTS)——
     函数体内的延迟 import 也算;包已删,这类 import 一被调用就炸;
  ② 在役叶子文件对社媒旧包路径零引用;
  ③ (退役)旧位置的 re-export 垫随包删除,「垫子与新位置同一对象」无对象可比;
  ④ 视频 adapter 的逐平台行为(Review 09-26 定 (b+));视频链接附件不做转写 ⇒ 不扣 video_asr(Review 09-26 定)。
①② 的牙证与对照臂走与真判据同一个函数(_forbidden_imports / _old_path_refs),只换输入。
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

NEW_MODULES = (
    "services/video_link.py",
    "tools/agent_loop/adapters/video.py",
)
LEAF_FILES = (
    "api/agent_api.py",
    "services/chat_attachments.py",
    "tools/agent_loop/adapters/video.py",
)
FORBIDDEN_PARTS = ("one_click_rewrite", "content_workshop", "advisor_llm")  # 包已随开源 E3 B2 整删,仍留在禁止表里:防它被加回来
OLD_PACKAGE = "tools.social_operator"  # 包已随开源 E3 B2 整删,仍留在禁止表里:防它被加回来


def _read(rel: str) -> str:
    return (ROOT / rel).read_bytes().decode("utf-8")


def _forbidden_imports(src: str) -> list[str]:
    """整棵 AST 里(含函数体内)所有 import 目标,命中社媒改写代码的列出来。"""
    hits: list[str] = []
    for node in ast.walk(ast.parse(src)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            names = [base] + [f"{base}.{a.name}" for a in node.names]
        for nm in names:
            parts = [p for p in nm.split(".") if p]
            if nm.lstrip(".").startswith(OLD_PACKAGE) or any(p in FORBIDDEN_PARTS for p in parts):
                hits.append(f"L{node.lineno}: {nm}")
    return hits


def _old_path_refs(src: str) -> list[str]:
    """源码里(不论 import、字符串还是注释)出现社媒旧包路径的行。"""
    return [f"L{i}: {ln.strip()}" for i, ln in enumerate(src.split("\n"), 1)
            if OLD_PACKAGE in ln or OLD_PACKAGE.replace(".", "/") in ln]


# ---------- ① 新位置不许 import 社媒改写代码 ----------

def test_new_modules_do_not_import_rewrite_code() -> None:
    bad = {rel: _forbidden_imports(_read(rel)) for rel in NEW_MODULES}
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, f"上提后的新位置 import 了社媒改写代码:{bad}"


def test_forbidden_import_checker_has_teeth() -> None:
    """牙证:往真文件源码里注入四种写法(模块级 / 函数体内 / from 包 import 子模块 / 普通 import),每种都必须被抓到。"""
    base = _read("services/video_link.py")
    poisons = (
        "\nfrom tools.social_operator.one_click_rewrite import one_click_rewrite\n",  # 毒串:包已删,写法仍须被抓
        "\ndef _p():\n    from tools.social_operator.content_workshop import get_content_workshop\n",  # 毒串:包已删,写法仍须被抓
        "\nfrom tools.social_operator import advisor_llm\n",  # 毒串:包已删,写法仍须被抓
        "\nimport tools.social_operator.rewrite_tool\n",  # 毒串:包已删,写法仍须被抓
    )
    for p in poisons:
        assert _forbidden_imports(base + p), f"注毒没被抓到:{p!r}"


def test_forbidden_import_checker_control_arm() -> None:
    """对照臂:注入无关的在役 import(名字里带 content / rewrite 字样但不是那三个模块)必须仍然绿。"""
    base = _read("services/video_link.py")
    clean = (
        "\nfrom api.content_api import router\n"
        "\ndef _q():\n    from services.chat_attachments import parse_video_url\n"
        "\nimport tools.agent_loop.adapters.video\n"
    )
    assert _forbidden_imports(base + clean) == []


# ---------- ② 在役叶子文件对旧包路径零引用 ----------

def test_leaf_files_have_zero_old_package_refs() -> None:
    bad = {rel: _old_path_refs(_read(rel)) for rel in LEAF_FILES}
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, f"在役叶子文件仍引用社媒旧包路径:{bad}"


def test_old_path_ref_checker_has_teeth_and_control() -> None:
    base = _read("services/chat_attachments.py")
    assert _old_path_refs(base + "\n    from tools.social_operator.rewrite_tool import detect_platform\n")  # 毒串:包已删,写法仍须被抓
    assert _old_path_refs(base + "\n# 见 tools/social_operator/content_workshop.py\n")  # 毒串:包已删,写法仍须被抓
    assert _old_path_refs(base + "\nfrom services.video_link import detect_platform  # social operator\n") == []


# ---------- ③ 垫子与新位置是同一个对象 ----------

# [开源 E3 · B2] ③「垫子与新位置同一对象」退役:两个垫子随社媒包删除,无对象可比。


# ---------- ④ 逐平台行为 + 不扣费 ----------

URLS = {
    "douyin": "https://v.douyin.com/AbCdEf12/",
    "xiaohongshu": "https://www.xiaohongshu.com/explore/abc123def",
    "bilibili": "https://www.bilibili.com/video/BV1xx411c7mD",
    "weixin": "https://weixin.qq.com/sph/AbCdEf",
}
FAKE = {
    "extract_video_id_douyin": {"status": "success", "video_id": "7597023840262196495"},
    "fetch_video_detail_douyin": {"status": "success", "video_id": "7597023840262196495", "title": "抖音标题",
                                  "author": {"nickname": "抖音作者"}, "duration_ms": 65000,
                                  "stats": {"play": 1000, "digg": 50, "comment": 5, "share": 3}},
    "extract_xhs_note_id": {"status": "success", "note_id": "abc123def", "share_text": ""},
    "fetch_xhs_note_detail": {"status": "success", "note_id": "abc123def", "title": "小红书标题", "desc": "小红书正文",
                              "images": ["https://example.invalid/1.jpg"], "author": {"nickname": "小红书作者"},
                              "stats": {"likes": 10, "comments": 2, "shares": 1}},
    "extract_bilibili_bv_id": {"status": "success", "bv_id": "BV1xx411c7mD"},
    "fetch_bilibili_video_detail": {"status": "success", "bv_id": "BV1xx411c7mD", "title": "B站标题",
                                    "description": "B站简介", "subtitle_text": "B站字幕", "author": {"nickname": "UP主"},
                                    "stats": {"view": 900, "reply": 4, "share": 1, "like": 30}},
    "extract_wechat_video_id": {"status": "success", "video_id": "exp1", "id_type": "exportId"},
    "fetch_wechat_video_detail": {"status": "success", "video_id": "exp1", "title": "视频号标题",
                                  "description": "视频号描述", "author": {"nickname": "视频号作者"},
                                  "stats": {"like": 7, "comment": 1, "share": 1, "play": 300}},
}
EXPECT = {
    "douyin": ("抖音标题", "抖音作者", "", "", {"likes": 50, "views": 1000, "comments": 5, "shares": 3}),
    "xiaohongshu": ("小红书标题", "小红书作者", "https://example.invalid/1.jpg", "小红书正文",
                    {"likes": 10, "views": 0, "comments": 2, "shares": 1}),
    "bilibili": ("B站标题", "UP主", "", "B站字幕", {"likes": 30, "views": 900, "comments": 4, "shares": 1}),
    "weixin": ("视频号标题", "视频号作者", "", "视频号描述", {"likes": 7, "views": 300, "comments": 1, "shares": 1}),
}


def _patched():
    from tools.agent_loop.adapters import video as video_module

    def mk(val, sync=False):
        if sync:
            return lambda *_a, **_k: dict(val)

        async def f(*_a, **_k):
            return dict(val)
        return f

    return [patch.object(video_module, nm, mk(v, sync=(nm == "extract_bilibili_bv_id"))) for nm, v in FAKE.items()]


def _run_all():
    from services import chat_attachments
    from tools.agent_loop.adapters import video as video_module

    async def go():
        out = {}
        for plat, url in URLS.items():
            out[plat] = (await video_module.parse_video(url), await chat_attachments.parse_video_url(url))
        return out

    ps = _patched()
    for p in ps:
        p.start()
    try:
        return asyncio.run(go())
    finally:
        for p in ps:
            p.stop()


def test_per_platform_behavior_after_lift() -> None:
    res = _run_all()
    for plat, (title, author, thumb, transcript, metrics) in EXPECT.items():
        r, _c = res[plat]
        got = (r["title"], r["author"], r["thumbnail"], r["transcript"], r["metrics"])
        assert r["status"] == "ok" and r["source"] == "video_link", (plat, r)
        assert got == (title, author, thumb, transcript, metrics), (plat, got)


def test_video_link_attachment_carries_no_charge() -> None:
    """Review 09-26 定:视频链接附件只取基础信息、不做转写 ⇒ 不收转写费。
    四个平台的解析结果都不带任何扣费字段(incurred_cost=False · 无 feature · 0 分钟),
    调用方 content_api 见 incurred_cost=False 走「直接暴露」分支,不扣费。
    抖音夹具故意带 duration_ms=65000:有人把按时长扣 video_asr 接回来,这里一定红。"""
    res = _run_all()
    for plat, (_r, c) in res.items():
        assert c.ok, (plat, c.error)
        assert (c.incurred_cost, c.cost_feature_code, c.cost_video_minutes) == (False, None, 0), (
            plat, c.incurred_cost, c.cost_feature_code, c.cost_video_minutes)
        assert c.title == EXPECT[plat][0], (plat, c.title)


def test_adapter_raw_keeps_detail_one_level_down() -> None:
    """平台详情放在 raw['detail'] 下一层,raw 顶层不出现时长字段(防有人按顶层时长再算扣费分钟)。"""
    res = _run_all()
    for plat, (r, _c) in res.items():
        assert sorted(r["raw"]) == ["detail"], (plat, sorted(r["raw"]))


# ---------- ⑤ 入口准入口径 · [开源 E3 · B3b · 2026-09-28] 已退役 ----------
#   守的是 content_api 的 POST /api/content/chat-attachment/parse-url 端点(非管理员五平台能进、无 402/422、超限频 429)。
#   该端点随社媒对话一并删除,五格与其桩(_FakeRedis / _drive_endpoint / RL_ANCHOR)一起删;
#   「删掉的端点不许回来」由 tests/oss_e3a_routers_2026_09_28 的 retired_content_routes 格接管。


# ─── AF preflight F821 · 统一评分竞品字段(Review 09-27)· 已退役 ───────────────────

# [开源 E3 · B2] 统一评分 services/content_scoring.py 删除(E3 孤儿),AF 两格与其夹具退役。


