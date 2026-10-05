# -*- coding: utf-8 -*-
"""BH-011 · `/settings` 保存不许静默丢字段。

## 缺陷是什么(实测,不是抄报告)

`PUT /api/settings` 每次「保存成功」都会把 **8 个** `SystemSettings` 字段静默重置为类默认值,
HTTP 仍返回 `{"success": true}`,前端照弹「✓ 设置已成功保存到 settings.json」。

丢的含 `media_balance_enabled` / `media_balance_whitelist` / `media_provider_priority`
—— `services/media_balance_gate.py`(**fail-closed 灰度闸**,决定投放平台路由)**唯一**读的开关;
以及 `kuaiyibo_api_token` / `publish_channel_callback_secret` 两个**凭据**,被抹成 `''`。
`settings.json` 是这 8 个字段的**唯一写入方** ⇒ 没有任何东西会把它们写回来。

## 🔴 机理不是 pydantic `extra='ignore'`(BUG_HUNT 报告的归因是错的)

真链路三步:
1. `update_settings()` **不改 `current`,而是从零重建** `SystemSettings(...)`,
   靠一份**手工维护的 68 个 kwarg 清单**;
2. 模型有 76 个字段 ⇒ 清单从没提到的 8 个取**类默认值**;
3. `save_settings()` 用 `model_dump()` + `json.dump` **整文件覆盖**(无 merge)⇒ 落盘。

把 `extra` 改成 `allow`/`forbid` **一个都救不了** —— 构造调用里根本没有它们。
`extra='ignore'` 只解释「为什么不报 422」,**不解释「为什么值没了」**。

## 🔴 这一族判据此前对它结构性不可见

`tests/defgeo_woc_closure_2026_08_25/test_c7_settings_ghost.py` 已经推理过这个
「平移一格就隐身」的性质,注释白纸黑字写「**两个方向各一条**」——
**但设置保存链上有三个方向,它只锁了两个,漏的正好是这一个(第三方向 = 保存 handler)。**
⇒ 本文件补的就是第三个方向,而且分母**机械取自模型**,不手写。
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: 🔴 冻结例外集 —— **大小钉死为 0**。
#:    任何「这个字段就是该被丢」的主张,必须在这里显式登记并写理由,
#:    而不是靠「构造清单里恰好没写它」这种**沉默**来表达。
DROPPED_BY_DESIGN: frozenset[str] = frozenset()


def _settings_model():
    spec = importlib.util.spec_from_file_location(
        "_bh011_sm", ROOT / "config" / "settings_manager.py")
    m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
    return m.SystemSettings


def _update_settings_fn() -> ast.AST:
    src = io.open(ROOT / "server.py", encoding="utf-8", newline="").read()
    return next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "update_settings")


# ══ ① 主锁 · 第三方向:保存 handler 必须覆盖**每一个**模型字段 ═══════
def test_bh011_every_settings_field_survives_the_save_handler():
    """分母**机械取自模型**(`model_fields`),不是手写清单 ——
    手写清单会漏,而漏的那格恰好就是没人想到、因而出事的那格。

    「覆盖」的两种合法方式:
      · 出现在 `model_copy(update={...})` 的键里(被显式设值),或
      · 根本没被提到 —— 在 `model_copy` 语义下**自动继承 `current`**,即不丢。
    所以本条真正要禁的是**第三种**:回到 `SystemSettings(...)` 那种从零重建。
    """
    fn = _update_settings_fn()
    rebuilt = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
               and isinstance(c.func, ast.Name) and c.func.id == "SystemSettings"]
    assert not rebuilt, (
        f"`update_settings` 里又出现了 `SystemSettings(...)` 从零重建"
        f"(L{[c.lineno for c in rebuilt]})。\n"
        f"    那意味着「清单里没提到的字段」= 取类默认值 = 静默丢失,\n"
        f"    而 `save_settings()` 的整文件 json.dump 会把它落盘。\n"
        f"    请改回 `current.model_copy(update={{...}})`:没提到 ⇒ 继承,不丢。")

    copies = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
              and isinstance(c.func, ast.Attribute) and c.func.attr == "model_copy"]
    assert len(copies) == 1, (
        f"`update_settings` 里 `model_copy` 调用 {len(copies)} 处(应恰 1 处)")


def test_bh011_the_eight_previously_dropped_fields_round_trip():
    """行为锁 · 拿真模型跑一次 round-trip:改一个可编辑字段,其余必须**原样存活**。

    这条打的是**缺陷现场**:这 8 个正是修复前会被重置的那些。
    """
    S = _settings_model()
    #: 探针值按**真实字段类型**取(第一版把 whitelist 写成 list,pydantic 当场 ValidationError
    #: —— 那是我的判据坏了,不是被测代码坏了)。每个值都与类默认**不同**,
    #: 否则「存活」与「被重置成默认」在读数上同形,这条就没有区分力。
    probe = {
        "media_balance_enabled": True,                       # 默认 False
        "media_balance_whitelist": {"brand_ids": ["b-1"]},   # 默认三个空 list
        "media_provider_priority": ["p1", "p2"],             # 默认 ['mhz','kyb']
        "kuaiyibo_api_token": "TOK-BH011",                   # 默认 ''
        "publish_channel_callback_secret": "SEC-BH011",      # 默认 ''
        "citation_domain_window_days": 99,                   # 默认 90
        "style_ratio_category_sets": {"probe": {"x": 1}},
        "industry_style_categories": {"probe": ["c"]},
    }
    cur = S(**probe)
    # 前置自证:每个探针值都必须**不等于**类默认,否则本条恒绿
    same_as_default = [k for k, v in probe.items()
                       if S.model_fields[k].default == v]
    assert not same_as_default, (
        f"这些探针值与类默认相同 {same_as_default} —— "
        f"「存活」与「被重置」读数同形,本条无区分力,换值")
    # 模拟修好后的 handler:只更新一个可编辑字段
    new = cur.model_copy(update={"llm_narrative_alert_yuan": 123.0})
    lost = {k: (v, getattr(new, k)) for k, v in probe.items() if getattr(new, k) != v}
    assert not lost, f"这些字段在 round-trip 里被改变了:{lost}"
    assert new.llm_narrative_alert_yuan == 123.0, "被编辑的那个字段没生效 —— 反臂:证明这不是「什么都没做」"


def test_bh011_the_probe_would_notice_a_reset():
    """🔴 反臂:上一条若换成**从零重建**的写法,那 8 个必须当场丢。

    没有这一条,`test_..._round_trip` 可能只是在断言「model_copy 不改东西」——
    那对**任何**实现都成立,证明不了修复有效。
    """
    S = _settings_model()
    cur = S(media_balance_enabled=True, kuaiyibo_api_token="TOK-BH011")
    # 旧写法:只把「可编辑清单」里的项传进去,其余取类默认
    rebuilt = S(llm_narrative_alert_yuan=123.0)
    assert rebuilt.media_balance_enabled != cur.media_balance_enabled or \
        rebuilt.kuaiyibo_api_token != cur.kuaiyibo_api_token, (
        "从零重建竟然保住了那些字段 —— 那说明它们的类默认值恰好等于探针值,"
        "**这个探针没有区分力**,换一组值重写本条。")


# ══ ② 冻结例外集:任何「该丢」的主张必须显式登记 ════════════════════
def test_bh011_no_field_is_dropped_by_design_without_a_written_reason():
    assert DROPPED_BY_DESIGN == frozenset(), (
        f"有人往 `DROPPED_BY_DESIGN` 里加了 {sorted(DROPPED_BY_DESIGN)} 却没改本条。\n"
        f"    「这个字段就是该被丢」是一个**需要理由的主张**,不许靠沉默表达。\n"
        f"    要加就同笔写清:谁读它、丢了之后谁把它写回来。")


# ══ ③ save_settings 整文件覆盖的**前提**必须成立 ═════════════════════
def test_bh011_whole_file_overwrite_is_safe_only_because_the_object_is_complete():
    """🔴 我没有改 `save_settings` 的整文件覆盖,而 Review 裁定里提到「save 走 merge」。

    理由:`model_copy` 之后对象已含**全部**模型字段的正确值 ⇒ 整文件覆盖是安全的,
    merge 是多余的第二处修改(少改一处就少欠一条判据)。

    **但那个安全性依赖一个前提**,这条就是钉住它的:
    写入方必须拿到**完整对象**。哪天有人改成「只 dump 一部分」或「从 dict 拼」,
    整文件覆盖就会重新变成数据毁灭器 —— 那时本条必须红。
    """
    src = io.open(ROOT / "config" / "settings_manager.py", encoding="utf-8",
                  newline="").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "save_settings")
    body = ast.unparse(fn)
    assert "model_dump()" in body, (
        "`save_settings` 不再对**整个对象**做 `model_dump()` —— "
        "整文件覆盖的安全前提没了。要么改回全量 dump,要么把写入改成 merge。")
    args = [a.arg for a in fn.args.args]
    assert args and args[0] == "settings", (
        f"`save_settings` 的第一个形参变成了 {args!r} —— "
        f"它必须收**完整的 SystemSettings 对象**,不是 dict 片段。")
