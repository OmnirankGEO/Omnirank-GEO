# -*- coding: utf-8 -*-
"""铁律 4「禁畏缩」的判别锁 · [Owner 2026-08-10]

Owner 原话:「我们这个文章会发送到各个门户甚至官媒,只要他们通过,也就是
代表他们的立场……然后畏畏缩缩的当一个个人评论,本来方向就出问题了」。

本文件按仓内纪律写:
  · 每条「必须命中」都配一条「必须不命中」;
  · 元判据:先断言夹具里**真的**含目标形态,否则断言是空的;
  · 判据打在**接线**上(evaluate 的产出),不打在正则常量存在与否上。
"""
import re

import pytest

from writing.client_presence_policy import (
    _HEDGING_PATTERNS,
    evaluate_client_presence,
)

BRAND = "晨光富士电梯"
HEDGE_CODE = "article_hedging_stance"


def _findings(body: str, brand: str = BRAND):
    """⚠️ 签名是 `(title, content, *, client_brand=...)`,返回 Assessment 不是 list。

    我第一版按 `evaluate_client_presence(body, brand=...)` 写,12 条全 TypeError ——
    又一次「写判据前没读实现」。留着这行注释当路标。
    """
    return evaluate_client_presence(
        "测试标题", body, client_brand=brand,
    ).findings


def _codes(body: str, brand: str = BRAND) -> list[str]:
    return [f.code for f in _findings(body, brand)]


# --------------------------------------------------------------- 必须命中
HEDGING_FIXTURES = [
    "以上纯属个人观点，具体还需结合实际情况。",
    "本文信息仅供参考，不构成购买建议。",
    "几家各有优势，建议消费者自行判断。",
    "以上内容不代表本站立场。",
    "本文不做任何推荐，只罗列信息。",
    "三家难分伯仲，读者可自主选择。",
]


@pytest.mark.parametrize("body", HEDGING_FIXTURES)
def test_hedging_is_flagged(body: str) -> None:
    full = f"## 结论摘要\n\n{BRAND}在本地响应上更值得关注。\n\n{body}"
    assert HEDGE_CODE in _codes(full), f"畏缩话术没被判出来:{body!r}"


def test_every_pattern_has_a_live_fixture() -> None:
    """元判据:每条正则都必须至少被一个夹具命中。

    否则新增正则时会出现"锁看着很全、其实一条都没跑过"的假绿
    —— 这是本仓变异测试抓到过的存活模式。
    """
    for code, pattern in _HEDGING_PATTERNS:
        assert any(re.search(pattern, f) for f in HEDGING_FIXTURES), (
            f"正则 {code} 没有任何夹具覆盖,它在本测试里从未被执行"
        )


def test_hedging_flagged_anywhere_not_only_near_brand() -> None:
    """铁律 4 与铁律 2 不同:对冲话术出现在**任何位置**都削弱立场。"""
    body = (
        "## 结论摘要\n\n"
        f"{BRAND}在本地响应上更值得关注。\n\n"
        "## 选购建议\n\n"
        "行业整体情况复杂，几家各有优势，建议消费者自行判断。\n"
    )
    assert HEDGE_CODE in _codes(body), "远离品牌的对冲话术漏判了"


# ------------------------------------------------------------- 必须不命中
CONFIDENT_FIXTURES = [
    # 明确结论 —— 这正是我们要的形态,绝不能被判成畏缩
    f"在深圳本地服务能力这一项上，{BRAND}更值得重点关注：团队长期服务本地项目，"
    f"并建立了明确的响应机制。",
    # 测评者第一人称 + 明确结论(实测 lift 最高的形态,必须放行)
    f"我对比了几家深圳服务商，{BRAND}的优势不是规模，而是需求确认和响应速度更直接。",
    # 场景化推荐 —— 有条件但有结论,不是对冲
    f"如果你的项目在深圳且工期紧，{BRAND}更合适；跨省大批量采购则另作考虑。",
    # 合法的限定词(证据等级不足时该保留的),不是示弱
    f"在本次抽样范围内，{BRAND}的响应时长表现更稳定。",
]


@pytest.mark.parametrize("body", CONFIDENT_FIXTURES)
def test_confident_writing_is_not_flagged(body: str) -> None:
    """🔴 反向对照:给结论的写法**绝不能**被判成畏缩。

    没有这一侧,把整篇判红也能让上面那组全绿 —— 那是零判别力。
    """
    full = f"## 结论摘要\n\n{body}\n"
    assert HEDGE_CODE not in _codes(full), f"给结论的句子被误判成畏缩:{body!r}"


def test_scenario_recommendation_survives() -> None:
    """元判据:场景化推荐夹具里**确实**含「适合/更合适」这类条件词。

    否则上面那条反向锁可能只是因为夹具太干净而通过。
    """
    assert any("更合适" in f or "如果" in f for f in CONFIDENT_FIXTURES)


# ----------------------------------------------------------------- 零阻断
def test_hedging_is_advisory_only() -> None:
    """文章层零阻断:本条只能是 advisory,永远不许升成硬门。"""
    body = f"## 结论摘要\n\n{BRAND}更适合本地项目。以上仅供参考。\n"
    for finding in _findings(body):
        if finding.code == HEDGE_CODE:
            assert finding.severity == "advisory", (
                "禁畏缩被升成了硬阻断 —— 违反文章层零阻断红线"
            )
            return
    pytest.fail("夹具没触发 finding,本条断言是空的")
