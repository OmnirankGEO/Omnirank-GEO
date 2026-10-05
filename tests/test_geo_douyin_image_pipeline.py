"""GEO 抖音图文管线 v1 · 图文生产两条红线的锁

红线 1(工单 §3.3):产物即传 OSS,**本地磁盘零滞留**(生产盘 71%)
红线 2(工单 §3.2):外部 API 真异步,**禁同步阻塞占事件循环**

锁的打法:用 AST 扫真实调用,不用正则扫源码串 ——
  正则会把注释/docstring 里的字样算成命中(整文件跑正则误报 18 处,踩过);
  而且断言打在源码串上会被"换个写法"绕过,双向脆。
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from services.geo_douyin.image_pipeline import (
    CardBatchResult,
    CardImageResult,
    build_card_oss_key,
    build_card_prompt,
    render_cards,
    render_one_card,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "services" / "geo_douyin" / "image_pipeline.py"


def _tree() -> ast.AST:
    return ast.parse(PIPELINE.read_text(encoding="utf-8"))


def _called_names(tree: ast.AST) -> set[str]:
    """收集所有被调用的名字(含属性调用的属性名)。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


# ─────────────────────────────────────────────────────────────
# 红线 1:本地磁盘零滞留
# ─────────────────────────────────────────────────────────────
_DISK_WRITE_CALLS = {
    "write_bytes", "write_text", "mkdtemp", "mkstemp",
    "NamedTemporaryFile", "TemporaryFile", "makedirs", "mkdir",
    "copyfile", "copy2", "urlretrieve", "savefig", "imwrite",
}


def test_no_local_disk_write():
    """红线 1:管线里不得出现任何落盘调用。"""
    called = _called_names(_tree())
    hits = sorted(called & _DISK_WRITE_CALLS)
    assert not hits, f"image_pipeline 出现落盘调用: {hits}(产物必须只在内存与 OSS)"


def test_no_builtin_open_for_writing():
    """`open(...)` 只要出现就可疑(读也不该有);单列一条,错误信息更直白。"""
    called = _called_names(_tree())
    assert "open" not in called, "image_pipeline 不得使用 open() 落盘"


def test_disk_write_lock_has_discriminating_power():
    """必须不命中面:锁不能是恒真的。

    构造一段【真的含落盘调用】的代码,同一套检测必须命中 —— 否则上面两条
    只是"这个文件恰好没写盘"而不是"检测器能抓到写盘"。
    """
    bad = ast.parse("import pathlib\npathlib.Path('x').write_bytes(b'1')\n")
    assert _called_names(bad) & _DISK_WRITE_CALLS, "检测器抓不到真实落盘调用 = 恒真锁"


# ─────────────────────────────────────────────────────────────
# 红线 2:同步 SDK 必须 to_thread,不占事件循环
# ─────────────────────────────────────────────────────────────
def test_oss_sync_sdk_wrapped_in_to_thread():
    """oss2 是同步 SDK,put_object / 签名都必须经 asyncio.to_thread。"""
    called = _called_names(_tree())
    assert "to_thread" in called, (
        "同步 oss2 调用必须包在 asyncio.to_thread 里,否则阻塞事件循环")


def _calls_within(func_name: str) -> set[str]:
    """取【指定函数体内】的调用名集合。"""
    for node in ast.walk(_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == func_name:
            out: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    if isinstance(f, ast.Name):
                        out.add(f.id)
                    elif isinstance(f, ast.Attribute):
                        out.add(f.attr)
            return out
    raise AssertionError(f"找不到函数 {func_name} —— 锚点失效")


@pytest.mark.parametrize("fn", ["_upload_bytes_to_oss", "signed_card_urls"])
def test_each_blocking_function_offloads_itself(fn):
    """🔴 逐函数锁:每个碰同步 oss2 的函数**各自**都要 to_thread。

    只断言"整个文件里出现过 to_thread"不够 —— 把某一个函数的 to_thread 拆掉,
    另一个函数里还留着,文件级断言照样绿(实测该变异存活)。
    """
    assert "to_thread" in _calls_within(fn), (
        f"{fn} 内没有 to_thread,同步 oss2 调用会直接阻塞事件循环")


def test_per_function_offload_detector_has_power():
    """必须不命中面:构造一个没有 to_thread 的函数,检测口径必须判它缺失。"""
    tree = ast.parse("async def f():\n    return _put()\n")
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    assert "to_thread" not in names


def _nearest_func_is_sync(tree: ast.AST, call_attr: str) -> list[bool]:
    """对每个 `X.call_attr(...)` 调用点,判断它【最近的函数祖先】是不是同步 def。

    同步 SDK 调用必须待在同步闭包里(再交给 to_thread);若最近祖先是 async def,
    说明它直接跑在事件循环上 = 红线 2 被破。
    """
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    verdicts: list[bool] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == call_attr):
            continue
        cur = parent.get(node)
        while cur is not None:
            if isinstance(cur, ast.AsyncFunctionDef):
                verdicts.append(False)
                break
            if isinstance(cur, ast.FunctionDef):
                verdicts.append(True)
                break
            cur = parent.get(cur)
    return verdicts


def test_put_object_only_called_from_sync_closure():
    """机制锁:每个 put_object 调用点的最近函数祖先必须是同步 def。"""
    verdicts = _nearest_func_is_sync(_tree(), "put_object")
    assert verdicts, "没找到 put_object 调用点 —— 锚点失效,锁形同虚设"
    assert all(verdicts), (
        "存在直接写在 async def 里的 put_object,会阻塞事件循环")


def test_sync_closure_detector_has_discriminating_power():
    """必须不命中面:把 put_object 写进 async def,检测器必须判 False。"""
    bad = ast.parse(
        "import x\n"
        "async def f():\n"
        "    bucket.put_object('k', b'')\n"
    )
    assert _nearest_func_is_sync(bad, "put_object") == [False], (
        "检测器认不出 async def 里的同步调用 = 恒真锁")


# ─────────────────────────────────────────────────────────────
# OSS key 规范
# ─────────────────────────────────────────────────────────────
def test_oss_key_shape_and_uniqueness():
    k1 = build_card_oss_key(42, 1)
    k2 = build_card_oss_key(42, 1)
    assert k1.startswith("geo_douyin/42/card_1_")
    assert k1.endswith(".png")
    assert k1 != k2, "同 post 同 idx 两次生成必须不撞号(带时间戳+随机)"


def test_oss_key_rejects_bad_ext():
    with pytest.raises(ValueError):
        build_card_oss_key(1, 1, "exe")


def test_oss_key_normalizes_jpeg():
    assert build_card_oss_key(1, 1, "jpeg").endswith(".jpg")


# ─────────────────────────────────────────────────────────────
# 失败面:单张失败不拖垮整批;整批未全成功要能被识别
# ─────────────────────────────────────────────────────────────
def test_render_one_card_failsoft_on_gen_error(monkeypatch):
    async def fake_generate(*_a, **_k):
        return {"ok": False, "error": "poll_failed", "cost_usd": 0.0}

    monkeypatch.setattr("services.marketing.image_client.generate_image", fake_generate)
    res = asyncio.run(render_one_card(1, 1, "标题"))
    assert res.ok is False and res.error == "poll_failed"


def test_render_cards_partial_failure_is_visible(monkeypatch):
    calls = {"n": 0}

    async def fake_generate(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"ok": False, "error": "poll_failed", "cost_usd": 0.0}
        return {"ok": True, "image_url": "https://x/i.png", "cost_usd": 0.006}

    async def fake_download(_url):
        return b"\x89PNG fake"

    async def fake_upload(key, _data, _ct="image/png"):
        return key

    monkeypatch.setattr("services.marketing.image_client.generate_image", fake_generate)
    monkeypatch.setattr("services.marketing.image_client.download_image", fake_download)
    monkeypatch.setattr("services.geo_douyin.image_pipeline._upload_bytes_to_oss",
                        fake_upload)

    cards = [{"headline": f"卡{i}"} for i in range(3)]
    batch = asyncio.run(render_cards(7, cards))
    assert batch.ok_count == 2
    assert batch.all_ok is False, "有失败时 all_ok 必须为 False(否则会当成功去发布)"
    assert len(batch.oss_keys()) == 2


def test_all_ok_true_when_every_card_succeeds(monkeypatch):
    """必须不命中面:全成功时 all_ok 必须是 True,否则这个判据恒假也能过上一条。"""
    async def fake_generate(*_a, **_k):
        return {"ok": True, "image_url": "https://x/i.png", "cost_usd": 0.006}

    async def fake_download(_url):
        return b"\x89PNG fake"

    async def fake_upload(key, _data, _ct="image/png"):
        return key

    monkeypatch.setattr("services.marketing.image_client.generate_image", fake_generate)
    monkeypatch.setattr("services.marketing.image_client.download_image", fake_download)
    monkeypatch.setattr("services.geo_douyin.image_pipeline._upload_bytes_to_oss",
                        fake_upload)

    batch = asyncio.run(render_cards(7, [{"headline": "卡1"}, {"headline": "卡2"}]))
    assert batch.all_ok is True and batch.ok_count == 2


def test_empty_cards_is_not_all_ok():
    """空批次不能算"全部成功"(否则零卡片也会被放去发布)。"""
    assert CardBatchResult(cards=[]).all_ok is False


def test_card_prompt_mentions_headline_and_forbids_watermark():
    p = build_card_prompt("深圳全屋定制怎么选", "看完不踩坑", keyword="全屋定制", city="深圳")
    assert "深圳全屋定制怎么选" in p
    assert "水印" in p


# ═════════════════════════════════════════════════════════════
# 垫图锚(Owner 2026-08-02 裁定② · 与第 0 步 A/B 共用渗漏判据)
# ═════════════════════════════════════════════════════════════
from services.geo_douyin import image_pipeline as _ip  # noqa: E402
from services.geo_douyin.card_templates import (  # noqa: E402
    REFERENCE_NO_COPY_MARKER, with_reference_guard)


class _GenSpy:
    """把 generate_image / download_image / OSS 上传全打桩,记录每次调用的入参。"""

    def __init__(self):
        self.calls = []

    def install(self, monkeypatch, ok=True):
        spy = self

        async def fake_generate(prompt, *, size="3:4", resolution="1k",
                                n=1, image_urls=None):
            spy.calls.append({"prompt": prompt, "image_urls": image_urls})
            if not ok:
                return {"ok": False, "error": "gen_failed", "cost_usd": 0.0}
            return {"ok": True, "image_url": "https://cdn.apimart.ai/x.png",
                    "cost_usd": 0.0085}

        async def fake_download(_url):
            return b"\x89PNG\r\n\x1a\n" + b"0" * 32

        async def fake_upload(oss_key, _data, _ct="image/png"):
            return oss_key

        async def fake_sign(keys, expires_seconds=900):
            # 测试环境没有 OSS 凭据;真实现会抛 OSSConfigError 并降级为无锚 ——
            # 那条降级路径由 test_group_falls_back_* 单独覆盖,这里要测的是有锚的主路径。
            return [f"https://signed.example/{k}" for k in keys]

        import services.marketing.image_client as ic
        monkeypatch.setattr(ic, "generate_image", fake_generate)
        monkeypatch.setattr(ic, "download_image", fake_download)
        monkeypatch.setattr(_ip, "_upload_bytes_to_oss", fake_upload)
        monkeypatch.setattr(_ip, "signed_card_urls", fake_sign)
        return spy


def _specs():
    return [
        {"kind": "cover", "idx": 1, "headline": "封面", "prompt": "COVER_PROMPT"},
        {"kind": "content", "idx": 2, "headline": "内容1", "prompt": "C1"},
        {"kind": "closing", "idx": 3, "headline": "收尾", "prompt": "CL"},
    ]


def test_reference_urls_force_the_no_copy_guard(monkeypatch):
    """🔴🔴 传参考图 = 必须带禁抄段,调用方绕不过去。

    第 0 步 A/B 实测:垫图不带禁抄段 → 参考图上的文字被抄进产物 **3/3**
    (含母版角标「模板示例」逐字印出);带上 → 0/3。
    所以这两件事必须是同一个动作。
    """
    spy = _GenSpy().install(monkeypatch)
    asyncio.run(_ip.render_one_card(1, 1, "标题", prompt_override="P",
                                    reference_urls=["https://x/ref.png"]))
    assert len(spy.calls) == 1
    assert spy.calls[0]["image_urls"] == ["https://x/ref.png"]
    assert REFERENCE_NO_COPY_MARKER in spy.calls[0]["prompt"], (
        "传了参考图却没带禁抄段 —— 这条路径会把参考图上的文字抄进客户成图")


def test_no_reference_means_no_guard(monkeypatch):
    """反向面:没传参考图时不该塞禁抄段(无参考图时那段是纯噪声)。"""
    spy = _GenSpy().install(monkeypatch)
    asyncio.run(_ip.render_one_card(1, 1, "标题", prompt_override="P"))
    assert spy.calls[0]["image_urls"] is None
    assert REFERENCE_NO_COPY_MARKER not in spy.calls[0]["prompt"]


def test_guard_is_idempotent():
    """已带禁抄段的 prompt 不该被加第二遍。"""
    once = with_reference_guard("P")
    assert with_reference_guard(once) == once
    assert once.count(REFERENCE_NO_COPY_MARKER) == 1


def test_group_anchors_the_rest_on_the_cover(monkeypatch):
    """🔴 裁定②:封面先出,成图当参考图传给内容卡/收尾卡。"""
    spy = _GenSpy().install(monkeypatch)
    batch = asyncio.run(_ip.render_prompt_group(7, _specs()))
    assert batch.all_ok
    assert len(spy.calls) == 3
    # 第一次调用必须是封面,且**不带**参考图(它自己就是锚)
    assert spy.calls[0]["prompt"].startswith("COVER_PROMPT")
    assert spy.calls[0]["image_urls"] is None, "封面自己不该被垫图"
    # 其余两张必须带同一个锚 + 禁抄段
    for c in spy.calls[1:]:
        assert c["image_urls"], "内容/收尾卡没有拿到封面锚"
        assert REFERENCE_NO_COPY_MARKER in c["prompt"]
    assert spy.calls[1]["image_urls"] == spy.calls[2]["image_urls"], (
        "两张卡锚到了不同的图 —— 那就锁不住组内风格")


def test_group_keeps_original_card_order(monkeypatch):
    """两阶段渲染不得打乱卡序 —— 乱序会让落库的 oss_keys 与 cards 对不上。"""
    _GenSpy().install(monkeypatch)
    batch = asyncio.run(_ip.render_prompt_group(7, _specs()))
    assert [c.idx for c in batch.cards] == [1, 2, 3]


def test_group_order_preserved_when_cover_is_not_first(monkeypatch):
    """🔴 判别力样本:封面**不在第 0 位**时才测得出"有没有按原序还原"。

    上一版只用"封面在第 0 位"的样本,那时 `[封面] + 其余` 与正确顺序恰好相同 ——
    把还原逻辑换成那种写法,锁照样绿(实测该变异存活)。判据没错,是样本没判别力。
    """
    spy = _GenSpy().install(monkeypatch)
    specs = [
        {"kind": "content", "idx": 1, "headline": "内容1", "prompt": "C1"},
        {"kind": "cover", "idx": 2, "headline": "封面", "prompt": "COVER_PROMPT"},
        {"kind": "closing", "idx": 3, "headline": "收尾", "prompt": "CL"},
    ]
    batch = asyncio.run(_ip.render_prompt_group(7, specs))
    # 封面仍然先渲染(它是锚),但**返回顺序必须回到原序**
    assert spy.calls[0]["prompt"].startswith("COVER_PROMPT"), "封面没有先渲染"
    kinds = []
    for c in batch.cards:
        kinds.append(specs[c.idx - 1]["kind"])
    assert kinds == ["content", "cover", "closing"], (
        f"两阶段把卡序打乱了:{kinds} —— 落库时 oss_keys 会和 cards 对不上")


def test_group_falls_back_when_cover_fails(monkeypatch):
    """封面没出来时其余卡仍要跑完(拿到完整错误),而不是整组一起消失。"""
    spy = _GenSpy().install(monkeypatch, ok=False)
    batch = asyncio.run(_ip.render_prompt_group(7, _specs()))
    assert not batch.all_ok
    assert len(spy.calls) == 3, "封面失败后其余卡没被渲染,错误信息会不完整"
    for c in spy.calls[1:]:
        assert not c["image_urls"], "封面都没出来,哪来的锚"


def test_single_card_group_skips_the_two_phase(monkeypatch):
    """只有一张卡时没有"锚"这回事,不该白白串行一次。"""
    spy = _GenSpy().install(monkeypatch)
    asyncio.run(_ip.render_prompt_group(7, [_specs()[0]]))
    assert len(spy.calls) == 1
    assert spy.calls[0]["image_urls"] is None


def test_anchor_can_be_disabled(monkeypatch):
    """反向面:关掉锚时必须退回一次性并发,不能还偷偷传参考图。"""
    spy = _GenSpy().install(monkeypatch)
    asyncio.run(_ip.render_prompt_group(7, _specs(), anchor_on_cover=False))
    assert len(spy.calls) == 3
    assert all(c["image_urls"] is None for c in spy.calls)


def test_reference_urls_only_enter_through_render_one_card():
    """🔴 结构锁:全仓只有 render_one_card 能把 image_urls 传给生图。

    多一个入口 = 多一条"传了图忘了带禁抄段"的路径。
    """
    src = pathlib.Path(_ip.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.unparse(node)
        if "image_urls=" in body and node.name != "render_one_card":
            offenders.append(node.name)
    assert not offenders, f"这些函数也在直接传 image_urls:{offenders}"


def test_render_one_card_pairs_guard_with_image_urls():
    """同一函数里必须同时出现"附加禁抄段"和"传 image_urls",不能只剩一半。"""
    tree = ast.parse(pathlib.Path(_ip.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "render_one_card")
    body = ast.unparse(fn)
    assert "with_reference_guard" in body, "唯一入口没有附加禁抄段"
    assert "image_urls=" in body
