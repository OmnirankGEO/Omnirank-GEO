"""#78 · `request.state.user` 上**没有** `id` 键 —— 裸读它恒 None / 恒 KeyError。

鉴权中间件三条路径放的都是 `user_id`:
  · `auth/jwt_utils.py` 的 JWT payload(`"user_id": user["id"]`)
  · `auth/middleware.py` soft-refresh 支(`"user_id": fresh_user["id"]`)
  · `auth/middleware.py` 门户支(`"user_id": f"portal_{...}"`)
**没有一条**放 `id`。所以:
  · `user.get("id")` **恒 None** —— 不报错,只是把 None 写进库/日志(最难发现的那种);
  · `user["id"]` **恒 KeyError → 500**(FAQ 投票就是它,`faq_votes` 因此一直 0)。

🔴 两种后果差别很大而根因同一个:一个响亮、一个静默。修的时候要一起修,
   否则只修了响亮那个,静默那个继续把 None 写进 `reviewed_by`。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: 本单收编的两个模块。🔴 **不是全仓**:全仓「零裸读」的分母我量过 —— 按
#: 「所在函数带 request 参数」这个近似有 225 处候选,假阳率未知(很多 `user`
#: 是端点内部的数据库行,那种确实有 `id` 列)。硬按 225 处改是另一件事,
#: 已单独报回;这里只锁**已证同源**的两个模块,不假装覆盖了全仓。
_MODULES = ["api/faq_api.py", "api/research_monitor_industry_api.py"]


def _dicts_with_user_id(path: str):
    src = (ROOT / path).read_text(encoding="utf-8")
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, ast.Dict):
            continue
        keys = {k.value for k in n.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if "user_id" in keys:
            yield n.lineno, keys


@pytest.mark.parametrize("path", ["auth/jwt_utils.py", "auth/middleware.py"])
def test_no_auth_path_puts_an_id_key_on_the_user_dict(path):
    """🔴 根因臂:凡是造 user dict 的地方,有 `user_id` 就不许有 `id`。

    这条一红有两种含义,都必须人来看:
      · 真的加了 `id` 键 ⇒ 本单的前提没了,那些 `_user_id()` 可以简化;
      · 或者有人把两个键都放了 ⇒ 出现**两个真相源**,更糟。
    """
    found = list(_dicts_with_user_id(path))
    assert found, f"{path} 里没找到任何带 user_id 的 dict —— 分母为空,这条什么都没验"
    bad = [(ln, sorted(keys)) for ln, keys in found if "id" in keys]
    assert not bad, f"{path} 的 user dict 同时带了 id 与 user_id:{bad}"


@pytest.mark.parametrize("path", _MODULES)
def test_module_has_zero_naked_user_id_reads(path):
    """🔁 模块门:收编的两个模块里,零裸读。

    `_user_id()` 自己那一行(`user.get("id") or user.get("user_id") or 0`)是**兜底**,
    不算裸读 —— 它正是修法本体。判定时先把带 `or` 兜底的排掉,
    否则门会把药也判成病(#72 刚踩过这个,记着)。
    """
    # 🔴 用 AST 不用正则:裸串锁会被**文档里提到这个写法**触发
    #    (本判据第一版就被 helper 自己的 docstring 打红了)。
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    naked = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)            and n.value.id == "user" and isinstance(n.slice, ast.Constant)            and n.slice.value == "id":
            naked.append((n.lineno, 'user["id"] 恒 KeyError'))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)            and n.func.attr == "get" and isinstance(n.func.value, ast.Name)            and n.func.value.id == "user" and n.args            and isinstance(n.args[0], ast.Constant) and n.args[0].value == "id":
            # helper 自己那行是 `user.get("id") or user.get("user_id") or 0` ——
            # 它在 BoolOp 里,是**兜底**不是裸读。门要能分辨药和病(#72 的教训)。
            parents = [q for q in ast.walk(tree)
                       if isinstance(q, ast.BoolOp) and any(c is n for c in ast.walk(q))]
            if not parents:
                naked.append((n.lineno, 'user.get("id") 恒 None'))
    assert not naked, f"{path} 还有裸读:{naked}"


@pytest.mark.parametrize("path", _MODULES)
def test_module_actually_has_the_helper_and_uses_it(path):
    """🔁 正样本臂:没有它,上面那条全绿也可能是因为**这两个模块根本不读用户 id 了**
    (比如被谁顺手删干净)—— 那不是修好,是功能没了。"""
    src = (ROOT / path).read_text(encoding="utf-8")
    assert "def _user_id(" in src, f"{path} 没有 _user_id helper"
    assert src.count("_user_id(user)") >= 1, f"{path} 定义了 helper 却没人用"


@pytest.mark.parametrize("shape,expect", [
    ({"user_id": 7}, 7),            # 生产实际形状
    ({"id": 7}, 7),                 # 万一将来加了 id 也认
    ({"id": 0, "user_id": 9}, 9),   # id 为假值时回落 user_id
    ({}, 0),                        # 都没有 ⇒ 0,不抛
])
def test_helper_tolerates_both_key_shapes(shape, expect):
    from api.faq_api import _user_id
    assert _user_id(shape) == expect


def test_the_vote_failure_toast_does_not_invent_a_cause():
    """#78 附带项:失败提示不许说「网络问题」——那是我们并不知道的原因。

    真因是服务端 KeyError 恒 500(确定性失败),而「网络问题 · 稍后再试」会让她
    去查自己的网、并相信等一会儿就好。🔴 **一句具体但错误的话比笼统的更糟**:
    笼统让她继续找/来问,错的具体让她停止追查并走错方向。

    🔴 判据打在 **toast 调用**上而不是文件文本上:按文件查串会被
    「解释这次改动的注释」触发(散文提及 ≠ 代码里还在用),本文件上一条刚踩过。
    """
    src = (ROOT / "frontend/src/pages/Help/HelpFAQ.tsx").read_text(encoding="utf-8")
    calls = re.findall(r"toast\.(?:error|warning)\(\s*'([^']*)'", src)
    assert calls, "一个 toast 调用都没抓到 —— 分母为空,这条什么都没验"
    bad = [c for c in calls if "网络" in c]
    assert not bad, f"失败提示里编了原因:{bad}"
