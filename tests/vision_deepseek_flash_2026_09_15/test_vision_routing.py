# -*- coding: utf-8 -*-
"""WO_220-c1 · 图片识别换 DeepSeek flash。

Owner(2026-09-15 13:12 北京):「识别图片这里 deepseek-flash 现在升级成多模态了,
你测试一下行不行,如果可以的话就换成 deepseek」。
Review 探针取证:`C:\\AI-Test\\REVIEW_DEEPSEEK_VISION_PROBE_2026-09-15.md`。

判据分三层:
  · **行为层** —— 桩掉出网,抓住**真正发出去的那个请求体**,看端点/模型/thinking。
  · **结构层** —— 消费方不许自己拼端点(否则单点解析器形同虚设)。
  · **降级层** —— 失败仍走 `_fallback`,不抛错、不中断上传。
"""
from __future__ import annotations

import ast
import asyncio
import io
import json
import pathlib
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

CONSUMERS = ("tools/vision/image_describe.py", "services/geo_douyin/ocr_qa.py")

#: 探针实测的复杂图返回(结构化 JSON,8 键齐)
_GOOD_JSON = json.dumps({
    "image_type": "storefront", "title": "门店外观", "caption": "门头实拍",
    "alt_text": "某品牌门店门头", "vision_summary": "一张门店正面照,招牌清晰。",
    "ocr_text": "全域上榜 QIGE 4471", "suggested_placement": "brand_intro",
    "usage_scenarios": ["brand_intro"], "tags": ["门店", "门头"], "risk_flags": [],
}, ensure_ascii=False)

_EIGHT_KEYS = ("image_type", "title", "caption", "alt_text",
               "vision_summary", "ocr_text", "suggested_placement", "usage_scenarios")


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


def _chat_payload(model, content=_GOOD_JSON, finish="stop"):
    return {"model": model,
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1416, "completion_tokens": 900}}


class _Captor:
    """桩掉出网,把请求原样留下来 —— 判据看的是**真正发出去的那个**。"""

    def __init__(self, payload_fn):
        self.calls = []
        self._payload_fn = payload_fn

    def client(self, *a, **kw):
        captor = self

        class _C:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            async def post(self_inner, url, **kwargs):
                captor.calls.append({"url": url, **kwargs})
                return _Resp(captor._payload_fn(kwargs.get("json") or {}))

        return _C()


@pytest.fixture
def envkeys(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-ds")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dummy-ds-scope")


@pytest.fixture
def routing():
    import config.vision_routing as mod
    assert pathlib.Path(mod.__file__).resolve() == (REPO / "config" / "vision_routing.py").resolve()
    return mod


# ══════════════════════════════════════════════════════════════════
# 1. 行为层:真正发出去的那个请求
# ══════════════════════════════════════════════════════════════════
def _run_describe(monkeypatch, captor, model="deepseek-flash"):
    import tools.vision.image_describe as mod
    import config.vision_routing as vr
    monkeypatch.setattr(vr, "resolve_vision_model", lambda: model)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", captor.client)
    return asyncio.run(mod.describe_image_structured(b"\x89PNG fake", "shop.png"))


def test_image_describe_hits_deepseek_with_thinking_disabled(monkeypatch, envkeys, routing):
    """🔴 三项同锁:端点 = DeepSeek 官方 · 模型 == 常量 · thinking disabled。

    这三样是同一件事的三个面。分开锁的话,改了一样漏了另一样时判据仍绿:
    端点对了模型没换 ⇒ 400;模型对了 thinking 没关 ⇒ **200 但 content 为空**,
    静默落 `_fallback`,表现为「识别失败」(Review 探针 3b 实测)。
    """
    from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
    cap = _Captor(lambda body: _chat_payload(body.get("model")))
    out = _run_describe(monkeypatch, cap)
    assert len(cap.calls) == 1, "没发出请求或发了多次:%d" % len(cap.calls)
    call = cap.calls[0]
    assert call["url"] == routing.DEEPSEEK_ENDPOINT, "打去了 %s" % call["url"]
    body = call["json"]
    assert body["model"] == DEEPSEEK_OFFICIAL_FLASH
    assert body.get("thinking") == {"type": "disabled"}, (
        "请求体没带 thinking disabled —— 复杂图会 200 但 content 为空,静默落 fallback")
    assert out["vision_ok"] is True
    for k in _EIGHT_KEYS:
        assert k in out, "_normalize 少了 %s" % k


def test_ocr_qa_hits_the_same_target(monkeypatch, envkeys, routing):
    """第二个 GEO 消费方走同一条线 —— 两个消费方只切一个等于没切。"""
    from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
    import services.geo_douyin.ocr_qa as mod
    import config.vision_routing as vr
    monkeypatch.setattr(vr, "resolve_vision_model", lambda: DEEPSEEK_OFFICIAL_FLASH)
    cap = _Captor(lambda body: _chat_payload(body.get("model"), content="全域上榜"))
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", cap.client)
    got = asyncio.run(mod.ocr_image_url("https://example.com/card.png"))
    assert got == "全域上榜"
    assert cap.calls[0]["url"] == routing.DEEPSEEK_ENDPOINT
    assert cap.calls[0]["json"]["model"] == DEEPSEEK_OFFICIAL_FLASH
    assert cap.calls[0]["json"].get("thinking") == {"type": "disabled"}


class _TrackCaptor:
    """桩 `llm_track`,把它**真正收到的 platform** 留下来。

    🔴 这条是注毒补出来的。第一版我让解析器返回了 platform,却没有任何判据断言
       它真的传进了 `llm_track` —— 毒「platform 记成 dashscope」当场读绿。
       **锁没牙**,不是毒够不着:四样一起返回只保证它们被算出来,
       保证不了每一样都被用上。「A 调 B 那行没人验」
       (本仓 nobody-verified-the-line-where-a-calls-b)。
    """

    def __init__(self):
        self.seen = []

    def __call__(self, purpose, platform, model=None, **kw):
        self.seen.append({"purpose": purpose, "platform": platform, "model": model})
        captor = self

        class _T:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            def record(self_inner, **kw2):
                captor.seen[-1].setdefault("records", []).append(kw2)

        return _T()


@pytest.mark.parametrize("model,want_platform", [
    ("deepseek-flash", "deepseek"),
    ("qwen3.6-flash", "dashscope"),
])
def test_cost_is_recorded_against_the_provider_actually_called(
        monkeypatch, envkeys, routing, model, want_platform):
    """🔴 platform 必须**跟着端点走**,并且真的传进 `llm_track`。

    记错行的后果不是报错,是**成本落到另一个供应商名下** —— 账单总额不变、
    没有任何异常,而按 caller 分层对账时会得出反过来的结论(WO_214 同族)。
    两个模型各跑一次:只钉 deepseek 那一档的话,「恒返 deepseek」也能读绿。
    """
    import tools.llm_call_tracker as tracker_mod
    tc = _TrackCaptor()
    monkeypatch.setattr(tracker_mod, "llm_track", tc)
    cap = _Captor(lambda body: _chat_payload(body.get("model")))
    _run_describe(monkeypatch, cap, model=model)
    assert tc.seen, "llm_track 根本没被调用 —— 这次调用不会进成本表"
    assert tc.seen[0]["platform"] == want_platform, (
        "模型 %s 打到 %s,成本却记成 %s"
        % (model, want_platform, tc.seen[0]["platform"]))
    assert tc.seen[0]["model"] == model


def test_echo_mismatch_is_not_trusted(monkeypatch, envkeys, routing):
    """🔴 回显 != 请求名 ⇒ 不采信。

    供应商静默把旧名映射到新模型:HTTP 200、按新价计费、账单不异常 ——
    上一次两个多月无人发现。对图片标注来说,采信一个不知道是谁给出的描述,
    等于把错的资产标注写进库(本仓 a-200-can-hide-a-silently-substituted-model)。
    """
    cap = _Captor(lambda body: _chat_payload("deepseek-someone-else"))
    out = _run_describe(monkeypatch, cap)
    assert out["vision_ok"] is False, "回显对不上还当成功 —— 静默换模型就此通过"
    assert out["publish_allowed"] == 0, "识别没成还默认可外发"


def test_admin_choosing_qwen_routes_back_to_dashscope(monkeypatch, envkeys, routing):
    """管理员显式配 Qwen ⇒ **走回百炼**,而不是把 qwen 的名字发给 DeepSeek。

    共享 admin 键 `vision_model` 有四个消费方,另外两个仍只打百炼,
    所以它的默认值不能动;那就必须容得下「这里配着一个 Qwen 名字」。
    走错线的表现是 400 → 静默 fallback ⇒ 用户看到「识别失败」,而真因是路由。
    """
    cap = _Captor(lambda body: _chat_payload(body.get("model")))
    out = _run_describe(monkeypatch, cap, model="qwen3.6-flash")
    call = cap.calls[0]
    assert call["url"] == routing.DASHSCOPE_ENDPOINT, "qwen 被发去了 %s" % call["url"]
    assert call["json"]["model"] == "qwen3.6-flash"
    assert "thinking" not in call["json"], "给百炼发了它不认的 thinking 字段"
    assert out["vision_ok"] is True


def test_deepseek_pro_is_pulled_back_to_flash(routing, monkeypatch):
    """🔴 官方线上只有 flash 能识图。

    探针实测:`deepseek-v4-pro` 同一张图返 **200**,prompt 只计 108 token
    (图根本没进去),答「无法查看该图片」—— 一个**会返 200 的错答案**。
    配成 pro 必须被拉回 flash 并出声,不能让它安静地每张图都识别不准。
    """
    from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH, DEEPSEEK_OFFICIAL_PRO
    import db.social_preferences_db as prefs
    monkeypatch.setattr(prefs, "get_admin_setting",
                        lambda k, t, d=None: DEEPSEEK_OFFICIAL_PRO, raising=False)
    assert routing.resolve_vision_model() == DEEPSEEK_OFFICIAL_FLASH


def test_failure_still_falls_back_and_does_not_raise(monkeypatch, envkeys, routing):
    """出网炸了仍走保守降级:图已经传上来了,不能因为识别失败就丢。"""
    import tools.vision.image_describe as mod
    import config.vision_routing as vr
    monkeypatch.setattr(vr, "resolve_vision_model", lambda: "deepseek-flash")

    class _Boom:
        def __call__(self, *a, **kw):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            raise RuntimeError("网络炸了")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Boom())
    out = asyncio.run(mod.describe_image_structured(b"x", "a.png"))
    assert out["vision_ok"] is False and out["publish_allowed"] == 0
    for k in _EIGHT_KEYS:
        assert k in out, "降级形状也必须 8 键齐,否则调用方要写两套解析"


# ══════════════════════════════════════════════════════════════════
# 1b. 🔴 不桩模型解析 —— 上面每一条都 monkeypatch 掉了 resolve_vision_model,
#     于是它们**按定义**看不见「解析出来的到底是哪个模型」。
# ══════════════════════════════════════════════════════════════════
def test_the_seeded_social_key_does_not_pin_the_geo_line(live_db):
    """🔴 这条是**真厂商实证**补出来的,不是我想出来的。

    13 条桩判据全绿时,走真实代码路径打真厂商,解析器返回的是
    `qwen3.6-flash / dashscope` —— 因为共享键 `vision_model` 被
    `db.social_preferences_db.ensure_schema()` **播种成 qwen3.6-flash 写进库**,
    而 `get_admin_setting` 有库里的行就不会用调用方给的 default。
    ⇒ 我改的「模块兜底默认」在生产里**永远轮不到**。

    这正是 WO_217-c1a §2 那条「活的开关面是运行期配置,不是代码默认值」——
    我自己在同一天又踩了一次。修法是给 GEO 线**另起一个键**
    (共享键有四个消费方,社媒那两个仍只打百炼,改它的值会把它们弄坏)。

    本条钉的就是这件事:库里共享键是 qwen 的情况下,GEO 线仍解析出 flash。
    """
    from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
    from db.social_preferences_db import get_admin_setting
    import config.vision_routing as vr

    shared = get_admin_setting("vision_model", str)
    assert shared == "qwen3.6-flash", (
        "前提没成立:库里共享键是 %r,这条判据就测不到它该测的东西 —— "
        "不是通过,是**没测到**" % shared)
    assert vr.resolve_vision_model() == DEEPSEEK_OFFICIAL_FLASH, (
        "GEO 线被共享键钉住了 —— 代码默认改了也不生效")


def test_the_resolver_reads_the_geo_key_not_the_shared_one():
    """结构臂:解析器读的键必须是 GEO 专用那个。

    行为臂(上一条)要打库;这条不打库也能红,合车时先死的是它。
    """
    import config.vision_routing as vr
    assert vr._ADMIN_SETTING_KEY == "geo_vision_model"
    assert vr._SHARED_SOCIAL_SETTING_KEY == "vision_model"
    src = io.open(REPO / "config" / "vision_routing.py", encoding="utf-8").read()
    body = src[src.index("def resolve_vision_model"):src.index("def resolve_vision_target")]
    assert "_SHARED_SOCIAL_SETTING_KEY" not in body, (
        "解析器又去读共享键了 —— 它被 ensure_schema 播种成 qwen,读它等于不切")


# ══════════════════════════════════════════════════════════════════
# 1c. 🔴 [c1prime 2026-09-15] Review 10 发毒存活 6 —— 补锁这 5 个面。
#
#     存活的那 6 发不是「毒够不着」,是**我一个也没钉过**:
#     P2/P3 key、P4 ocr_qa 的回显锁、P8 归一、P10 record 的 success 字段、
#     P11 后台行有没有被读到。
#
#     🔴 根因值得写下来:**作者的毒来自作者的判据**。我写的 8 发全红,
#     因为每一发都打在我已经钉过的面上;注毒台是我写的,它反映的是
#     「我锁了什么」,不是「被测对象有几个面」。模块抬头我自己写着
#     「端点/key/platform/thinking 四样一起给」,判据锁了三样 —— key 那一样
#     从头到尾没人验。同一个病(算出来了没人验它被接上)在这个文件里犯了两次:
#     platform 是 Review 上一轮点出来的,key 是这一轮。
#     ⇒ 自己注毒时,先把**抬头里承诺的每一样**列成毒的清单,
#     而不是把已有判据倒过来当清单。
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("model,want_env,want_key", [
    ("deepseek-flash", "DEEPSEEK_API_KEY", "ds-fake-AAAA"),
    ("qwen3.6-flash", "DASHSCOPE_API_KEY", "dsc-fake-BBBB"),
])
def test_the_authorization_header_carries_the_right_vendor_key(
        monkeypatch, routing, model, want_env, want_key):
    """🔴 [钉 P2+P3] key 必须跟着端点走 —— 这一样此前**完全没锁**。

    后果不是报错是 401:拿百炼的 key 打 DeepSeek ⇒ 每张图「识别失败」,零报错。
    两个假值**互不为前缀**且用 `==` 不用 `startswith` ——
    前缀关系会让「两档取到同一个 key」也读绿。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-fake-AAAA")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dsc-fake-BBBB")
    assert not "ds-fake-AAAA".startswith("dsc-fake-BBBB")
    assert not "dsc-fake-BBBB".startswith("ds-fake-AAAA")
    import config.vision_routing as vr
    assert vr.resolve_vision_target(model).api_key_env == want_env
    cap = _Captor(lambda body: _chat_payload(body.get("model")))
    _run_describe(monkeypatch, cap, model=model)
    got = cap.calls[0]["headers"]["Authorization"]
    assert got == "Bearer " + want_key, (
        "%s 档带的是 %r,应为 Bearer %s" % (model, got, want_key))


def test_ocr_qa_also_refuses_a_mismatched_echo(monkeypatch, routing):
    """🔴 [钉 P4] `ocr_qa` 那一侧的回显锁 —— 代码里有,判据没钉,毒摘掉它读绿。

    「返回值对不对,和它有没有被接上,是两件事」这句是我自己上一轮写的,
    而它在 ocr_qa 这一侧又发生了一次。
    返 None = **没核成**,不是「图上一个字都没有」—— 两者混成一个值,
    视觉服务一挂就会把每张卡报成「文字全丢」。
    """
    from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
    import services.geo_douyin.ocr_qa as mod
    import config.vision_routing as vr
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-fake-AAAA")
    monkeypatch.setattr(vr, "resolve_vision_model", lambda: DEEPSEEK_OFFICIAL_FLASH)
    cap = _Captor(lambda body: _chat_payload("deepseek-someone-else", content="读到的字"))
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", cap.client)
    assert asyncio.run(mod.ocr_image_url("https://example.com/c.png")) is None, (
        "回显对不上还把内容当核验结果 —— 核验器被换掉却照常出报告")


@pytest.mark.parametrize("stale", ["deepseek-v4-flash", "deepseek-chat"])
def test_stale_official_names_are_normalised_before_emit(monkeypatch, routing, stale):
    """🔴 [钉 P8] 库里配着官方**旧名**时必须先归一再发。

    不归一的后果极隐蔽:旧名原样发出 → 官方**回显 deepseek-flash** →
    我自己的回显锁把**每一次**调用都判为不符 ⇒ **100% 降级、零报错**。
    闸的参照物变了,于是它全拒而看起来什么都没发生
    (本仓 a-gate-whose-referent-changed-rejects-100-percent)。
    """
    from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
    import db.social_preferences_db as prefs
    monkeypatch.setattr(prefs, "get_admin_setting",
                        lambda k, c=str, d=None: stale, raising=False)
    import config.vision_routing as vr
    assert vr.resolve_vision_model() == DEEPSEEK_OFFICIAL_FLASH, (
        "旧名 %s 没被归一 —— 发出去后回显 flash,回显锁会把每一次都拒掉" % stale)


def test_mismatched_echo_is_recorded_as_failure_with_both_names(monkeypatch, routing):
    """🔴 [钉 P10] 回显不符时 `tracker.record` 必须收到 success=False,
    且 error_msg **同时含两个名字**。

    否则成本表里看不出供应商静默换了模型 —— 而 WO_215 的心跳判据正是靠这个字段。
    只钉 success=False 不够:没有两个名字,拿到告警的人不知道被换成了什么。
    """
    import tools.llm_call_tracker as tracker_mod
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-fake-AAAA")
    tc = _TrackCaptor()
    monkeypatch.setattr(tracker_mod, "llm_track", tc)
    cap = _Captor(lambda body: _chat_payload("deepseek-someone-else"))
    _run_describe(monkeypatch, cap)
    recs = tc.seen[0].get("records") or []
    assert recs, "回显不符时一条 record 都没发 —— 成本表上这次调用不存在"
    bad = [r for r in recs if r.get("success") is False]
    assert bad, "回显不符却记成功 %s" % recs
    msg = str(bad[0].get("error_msg") or "")
    assert "deepseek-someone-else" in msg and "deepseek-flash" in msg, (
        "error_msg 没同时写清「请求的」与「回显的」:%r" % msg)


def test_a_real_admin_row_actually_steers_the_route(live_db):
    """🔴 [钉 P11] 后台那一行**真的被读到**。

    此前的行为臂只钉了「共享键是 qwen 时仍得 flash」—— 一个**恒返默认**的解析器
    也能满足它。这条反过来验:把 GEO 键真的写成 qwen,解析器必须跟着走,
    并且路由到百炼。写真库、finally 还原。
    """
    from db.social_preferences_db import get_admin_setting, set_admin_setting
    import config.vision_routing as vr
    before = get_admin_setting("geo_vision_model", str)
    try:
        assert set_admin_setting("geo_vision_model", "qwen3.6-flash"), "写不进去,这条测不到"
        assert vr.resolve_vision_model() == "qwen3.6-flash", (
            "后台行改了解析器却没跟着走 —— 它多半在恒返默认")
        t = vr.resolve_vision_target()
        assert t.endpoint == vr.DASHSCOPE_ENDPOINT and t.platform == "dashscope"
        assert t.extra_body == {}, "给百炼发了它不认的 thinking 字段"
    finally:
        set_admin_setting("geo_vision_model", before or "deepseek-flash")
    assert get_admin_setting("geo_vision_model", str) == (before or "deepseek-flash"), (
        "没还原 —— 下一条判据会读到我留下的值")


# ══════════════════════════════════════════════════════════════════
# 2. 结构层:解析器必须是单点
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("rel", CONSUMERS)
def test_consumers_do_not_build_endpoints_themselves(rel):
    """🔴 消费方里不许出现端点字面量 —— 否则单点解析器形同虚设。

    「注释说要走解析器」传不出去,门才传得出去
    (本仓 in-repo-prior-art-needs-a-gate-not-a-comment)。
    用 AST 只看字符串字面量:注释里提到端点是允许的。
    """
    tree = ast.parse(io.open(REPO / rel, encoding="utf-8").read())
    docs = {id(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    bad = [(getattr(n, "lineno", "?"), n.value) for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and id(n) not in docs
           and ("aliyuncs.com" in n.value or "api.deepseek.com" in n.value)]
    assert not bad, ("%s 自己拼了端点:%s —— 请改用 config.vision_routing" % (rel, bad))


@pytest.mark.parametrize("rel", CONSUMERS)
def test_consumers_actually_import_the_resolver(rel):
    """光是「没有端点字面量」还不够 —— 也可能是它根本不发请求了。

    正面钉:两个消费方都必须真的从单点解析器取目标。
    """
    src = io.open(REPO / rel, encoding="utf-8").read()
    tree = ast.parse(src)
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("vision_routing"):
            names |= {a.name for a in n.names}
    assert "resolve_vision_target" in names, (
        "%s 没有从 config.vision_routing 取路由" % rel)


def test_the_resolver_itself_carries_both_endpoints():
    """解析器必须**自己**知道两条线的端点 —— 否则上面那条「消费方没有端点」
    可以被「谁都不发请求了」满足。

    🔴 第一版我写成「端点字面量全仓只许出现在解析器一处」,两处错:
       ① 不真:百炼端点在 advisors / publish_api / 社媒等**非视觉**文件里也合法出现,
          这条判据的域被我写大了;顺带 `allowed` 白名单变量写了却没用上 —— 
          一个看起来在把关、实际什么都没做的形状。
       ② 用 `git grep` 找自己刚建的文件:**它只搜已跟踪文件**,新文件对它隐形,
          于是判据红在「解析器没有端点」,而真因是 `git add` 还没跑。
          读文件就读文件,别借道索引。

    消费方不许自己拼端点,由 `test_consumers_do_not_build_endpoints_themselves` 按 AST 钉。
    """
    src = io.open(REPO / "config" / "vision_routing.py", encoding="utf-8").read()
    assert "https://api.deepseek.com" in src, "解析器不知道 DeepSeek 端点"
    assert "dashscope.aliyuncs.com" in src, "解析器不知道百炼端点(管理员配 Qwen 时会无处可去)"
