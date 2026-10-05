"""WO-B ①:三个业务页面的 intent 接线**结构锁**(规格 §12.3)。

浏览器判据(`frontend/tests/xiaobang-prefill/`)证明"这三页现在是通的";
本文件证明的是**别的事**:

* 页面 plan 里写的字段,是后端**真会产出**的那几个;
* 用了 hook 的页面,都真的把预填区**渲染出来**了;
* 后端冻结的每一个字段,**至少有一页**在消费它。

这三条都是浏览器判据看不见的失效方式:

* 写一个后端从不产出的键 → 不报错,只是安静地什么都不填。三页里两页填上了、
  第三页的某个键写错了,PW 用例只要没专门断言那个参数就是绿的;
* 取了数不渲染 → 页面上什么都不显示,而"没有预填条"与"没有 intent"在
  PW 里长得一样(除非每页都写反向对照 —— 那正是分母会漏的地方);
* 服务端冻结了一个字段却没有任何页面读它 → 那个字段是死的。
  R3-P11 抓到的 `quote_id` **半截链**(服务端冻了、页面也认、就是没人接上)
  就是这个形态,当时是靠人肉发现的。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "frontend" / "src" / "hooks" / "useXiaobangPrefill.ts"
FRONTEND_SRC = REPO / "frontend" / "src"


def _pages_using_the_hook() -> dict:
    """机械枚举:哪些页面 import 了这个 hook。

    🔴 分母不是我手写的三页 —— 第四页接进来时它自动进分母,
       而不是"新加的那页碰巧没人验"。
    """
    out = {}
    for path in FRONTEND_SRC.rglob("*.tsx"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "useXiaobangPrefill" in text and "hooks/useXiaobangPrefill" in text:
            out[str(path.relative_to(REPO)).replace("\\", "/")] = text
    return out


def _plan_fields(text: str) -> set:
    """抠出这一页 plan 里的 `form_prefill 字段名`(numbers / lists 两块的**键**)。

    🔴 先剥掉 `//` 行注释再扫。本仓的结构锚一贯"注释也算命中",在别处那是**特意**
       如此(免得锚被注释绕过);但这一条判的是「代码里写了哪些键」,
       而一条解释"我为什么删掉了 lists: { article_ids: … }"的注释会被误判成
       "这一页还在引用它" —— 引用裁决原文把锁打红,本仓记过这个形态。
       所以这一格按语义收窄:只扫**可执行的那部分**。
    """
    code = chr(10).join(line for line in text.splitlines()
                        if not line.strip().startswith("//"))
    fields: set = set()
    for block in re.findall(r"(?:numbers|lists):\s*\{(.*?)\}", code, re.S):
        fields |= set(re.findall(r"(?:^|,)\s*(\w+)\s*:", block))
    return fields


def test_pages_using_the_hook_is_a_real_denominator():
    """分母自证:hook 真的有页面在用(零命中 = 下面几条恒真)。"""
    pages = _pages_using_the_hook()
    assert len(pages) >= 3, sorted(pages)
    joined = " ".join(pages)
    for expected in ("PublishCenter.tsx", "WritingWorkspace.tsx", "GeoContentCenter.tsx"):
        assert expected in joined, (expected, sorted(pages))


def test_page_param_plans_only_reference_producer_fields():
    """🔴 plan 的键必须落在后端 `_FORM_PREFILL_FIELDS` 里 —— 逐页逐键。

    分母是**后端源码**里的那个 allowlist,不是我记得几个。
    """
    import api.xiaobang_operations_api as ops

    allowed = set(ops._FORM_PREFILL_FIELDS)
    assert allowed, "producer allowlist 是空的 —— 这条判据没有分母"

    for page, text in _pages_using_the_hook().items():
        fields = _plan_fields(text)
        assert fields, page + ":plan 是空的,这一页取了数却什么都不填"
        extra = fields - allowed
        assert not extra, (
            page + " 的 plan 引用了后端从不产出的字段 " + str(sorted(extra))
            + ";它不会报错,只会安静地什么都不填")


def test_every_producer_field_is_consumed_by_at_least_one_page():
    """🔁 反方向:服务端冻结的每个字段,至少有一页在读。

    只做"页面别写多余键"这一个方向的话,**半截链**照样活着:
    服务端冻了一个字段、没有任何页面消费它 —— R3-P11 的 `quote_id` 就是这样。
    """
    import api.xiaobang_operations_api as ops

    consumed: set = set()
    for text in _pages_using_the_hook().values():
        consumed |= _plan_fields(text)
    dead = set(ops._FORM_PREFILL_FIELDS) - consumed
    assert not dead, (
        "服务端冻结了这些字段但没有任何页面消费:" + str(sorted(dead))
        + ";这就是「冻结了、页面也认、就是没人把两头接上」那种半截链")


def test_every_page_that_reads_the_prefill_also_renders_it():
    """🔴 接线锁:取了数就必须**渲染**出来。

    只调 hook 不渲染 = §12.3 那张同屏清单一项都不显示,而页面看起来"没坏"。
    """
    for page, text in _pages_using_the_hook().items():
        assert "XiaobangPrefillRegion" in text, (
            page + ":用了 useXiaobangPrefill 却没有渲染 XiaobangPrefillRegion —— "
            "取了数不上屏,页面上什么都看不见")


def test_the_hook_keeps_the_intent_param_on_the_url():
    """§12.2:intent 必须留在 URL 上,否则刷新/后退就恢复不了。

    打的是 hook 源码里那一行(唯一实现),不是每页各打一次。
    """
    text = HOOK.read_text(encoding="utf-8")
    assert "next.set(XIAOBANG_INTENT_PARAM, intentId)" in text, text[:200]
    # 🔁 反向对照:形状不对的 id 不发请求这条口径仍在 lib 里(hook 复用它)
    assert "readIntentId" in text
