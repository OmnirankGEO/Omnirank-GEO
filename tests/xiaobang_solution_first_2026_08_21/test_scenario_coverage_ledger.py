"""工单 §8 十六场景的**覆盖台账**(补验 §8 弱点 3)。

交付单第一批如实记过「16 场景只覆盖 S02/S03/S09/S10/S12,其余未逐条实测」。
这一份把台账**变成可执行的**:

  · 声称覆盖的,必须指得出**具体哪条判据**在守,且那条判据真的存在(反射校验);
  · 没覆盖的,必须显式登记为 NOT_VERIFIED,**并说清为什么**。

🔴 台账本身不是覆盖。它防的是另一件事:
   「交付单里写着覆盖了 S07,但那条判据后来被删了/改名了」——
   台账指向一个不存在的判据时,这里会红。
   本仓记过「白名单替一个已经不存在的情况开口子」这种烂法。
"""

from __future__ import annotations

import importlib
import inspect
import pathlib

import pytest

_PKG = "tests.xiaobang_solution_first_2026_08_21"
_FRONTEND_TESTS = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "tests"


#: 场景 → (覆盖它的判据坐标, 一句话说明)
#: 判据坐标两种形态:
#:   · "模块::函数"        —— 本包的 Python 判据(反射校验它真的存在)
#:   · "spec:<相对路径>::<用例名片段>" —— 浏览器判据(校验文件里真有这段文字)
COVERED: dict[str, tuple[str, str]] = {
    "S02": ("test_help_center_shortcut_root_cause::"
            "test_how_to_questions_no_longer_route_to_the_help_center",
            "「员工席位这个该怎么用」不再被路由到帮助中心(截图第 2 幕)"),
    "S03": ("test_current_page_gate::"
            "test_what_can_you_do_hits_the_preset_even_from_the_real_drawer",
            "抽屉形态下「你除了帮助中心还能做什么」命中能力答案(截图第 3 幕)"),
    "S05": ("test_answer_exits::test_route_only_fallback_carries_exits",
            "知识不足时给出口(含一键接管),不是只推文档"),
    "S06": ("test_answer_exits::test_the_second_identical_fallback_escalates",
            "同一条兜底第二次改口 + 升级 should_escalate"),
    "S09": ("test_dynamic_facts_adapter::"
            "test_source_failure_never_falls_back_to_a_constant",
            "问价走动态真值;源不可用给 O1,绝不回退常量"),
    "S10": ("test_corpus_lint_rules::"
            "test_target_engines_and_the_prohibition_text_are_not_flagged",
            "对外能力名与目标 AI 引擎合法;内部供应商穿透判红"),
    "S12": ("spec:xiaobang-session-isolation/session-isolation.spec.ts::"
            "同浏览器 A 退出 → B 登录",
            "双账号本地会话隔离(H0,已单独 PASS)"),
    "S14": ("test_telemetry::test_the_emitted_event_carries_no_user_text",
            "问答只读:观测事件不落正文;问答链路零扣费/零建单(见下方 §无副作用)"),
    "S15": ("spec:xiaobang-solution-first/exit-bar-handoff.spec.ts::"
            "接管 API 挂了",
            "接管失败保留已填内容 + 给重试,不显示裸错误码"),
    "S16": ("spec:xiaobang-solution-first/handoff-and-source.spec.ts::"
            "系统知识库来源的链接不再拼成",
            "来源作依据,不再生成 /help/docs//route 这种假地址"),
}

#: 场景 → 没验的**原因**。写不出原因的不许进这张表。
NOT_VERIFIED: dict[str, str] = {
    "S01": "「GEO 图文无法使用」要给排错步骤 —— 需要真 LLM + 真 KB 才能判答案质量,"
           "mock 后端下只能验形态不能验内容。本包不声称。",
    "S04": "「上一轮谈按钮,追问这个按钮点不了」——包 A③ 已把 recent_turns 送进 LLM"
           "(接线锁在 test_recent_turns_wiring),但**答案是否真的用上了上文**"
           "要真 LLM 才判得了。接线验了,效果没验。",
    "S07": "无权限用户的引导话术 —— 需要三身份真库 + 真权限元数据;"
           "本包没有搭这套夹具。",
    "S08": "route 下线/动态页缺参数的兜底 —— 漂移锁(test_route_registry_and_social_lock)"
           "守的是注册表与 App.tsx 一致,**运行时缺参数**那一支没验。",
    "S11": "三身份知识池与动作隔离 —— 既有判据在 tests/system_kb/"
           "test_two_kb_identity_isolation.py,需要 KB 真库;本包没跑,不计入通过数。",
    "S13": "带截图转人工 —— 截图上传走 /api/faq/feedback/screenshot,"
           "本包的接管 hook 预留了 screenshotUrl 但**抽屉里还没有截图入口**,未接。",
}


def test_the_ledger_covers_all_sixteen_scenarios():
    """🔴 分母机械枚举:S01..S16 一个都不许漏登记。"""
    expected = {"S%02d" % i for i in range(1, 17)}
    got = set(COVERED) | set(NOT_VERIFIED)
    assert got == expected, "漏登记:%s;多出来:%s" % (
        sorted(expected - got), sorted(got - expected))
    overlap = set(COVERED) & set(NOT_VERIFIED)
    assert not overlap, "同一场景既声称覆盖又声称没验:%s" % sorted(overlap)


@pytest.mark.parametrize("sid", sorted(COVERED))
def test_every_claimed_coverage_points_at_a_criterion_that_really_exists(sid):
    """🔴 台账指向的判据必须**真的存在**。

    「交付单说覆盖了 SXX,但那条判据后来被删了/改名了」——
    这条就是防它的。台账不是覆盖,但**指错的台账比没台账更坏**。
    """
    coord, _why = COVERED[sid]
    if coord.startswith("spec:"):
        rel, _, needle = coord[len("spec:"):].partition("::")
        path = _FRONTEND_TESTS / rel
        assert path.exists(), "%s:浏览器判据文件不存在 %s" % (sid, path)
        body = path.read_text(encoding="utf-8")
        assert needle in body, "%s:%s 里找不到用例 %r" % (sid, rel, needle)
        return
    mod_name, _, func_name = coord.partition("::")
    mod = importlib.import_module("%s.%s" % (_PKG, mod_name))
    assert hasattr(mod, func_name), "%s:%s 里没有判据 %s" % (sid, mod_name, func_name)
    assert inspect.isfunction(getattr(mod, func_name)), coord


@pytest.mark.parametrize("sid", sorted(NOT_VERIFIED))
def test_every_unverified_scenario_states_a_real_reason(sid):
    """没验的必须写清原因 —— 「未覆盖」三个字不算原因。"""
    why = NOT_VERIFIED[sid]
    assert len(why) >= 20, (sid, why)
    assert "未覆盖" != why.strip(), sid


def test_the_ledger_does_not_overclaim():
    """🔴 诚实度自证:声称覆盖的**不许超过**一半以上还没验的场景数量级。

    这条不是在限制质量,是在防「把台账写成公关稿」——
    真实覆盖 10/16 就写 10,不许把 NOT_VERIFIED 那栏悄悄清空。
    本包当前:覆盖 10、未验 6。
    """
    assert len(COVERED) == 10, len(COVERED)
    assert len(NOT_VERIFIED) == 6, len(NOT_VERIFIED)
