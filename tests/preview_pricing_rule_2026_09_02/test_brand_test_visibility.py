"""测试客户可见性:两处口径必须出自**同一份**规则(#116 · 2026-09-05)。

🔴 缺陷形态:`api/brand_api.py` 的「我的客户」列表按 #67 已**不再**隔离服务商
   自己名下的 is_test 品牌,而 `api/dashboard_api.py` 的 `brand_count` 仍在排除 ——
   两处都在**她自己的范围内**,却给出不同答案。
   现象:首页写 8 个客户、点进列表看到 9 个,**没有任何东西报错**。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from services.brand_test_visibility import (
    OWN_SCOPE_TEST_EXCLUSION,
    PLATFORM_TEST_EXCLUSION,
    own_scope_test_clause,
    platform_test_clause,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONSUMERS = ("api/brand_api.py", "api/dashboard_api.py")


# ══════════════════════════════════════════════════════════════════════════
# 规则本身
# ══════════════════════════════════════════════════════════════════════════

def test_own_scope_never_hides_her_own_test_brands():
    """🔴 自己名下**不隔离** —— 空串是这条规则的**内容**,不是「还没实现」。

    她自己建的「测试科技有限公司」被自动打上 is_test 后就此消失,
    她不知道它去哪了,也没有任何提示(#67 的原始报障)。
    """
    assert own_scope_test_clause() == "" == OWN_SCOPE_TEST_EXCLUSION


@pytest.mark.parametrize("include_test,is_admin,hides", [
    (False, False, True),
    (True, False, True),        # 🔴 非 admin 传 include_test 不生效:防 query 参数绕过
    (False, True, True),
    (True, True, False),        # 只有 admin 显式要求时才放开
])
def test_platform_scope_hides_test_brands_unless_admin_asks(include_test, is_admin, hides):
    got = platform_test_clause(include_test=include_test, is_admin=is_admin)
    assert (got == PLATFORM_TEST_EXCLUSION) is hides, (
        "include_test=%s is_admin=%s 时子句为 %r" % (include_test, is_admin, got))


def test_the_two_scopes_are_not_one_boolean():
    """🔴 两支必须**各有各的名字**,不能压成一个布尔开关。

    压成开关后调用方要在 platform / own-scope 之间猜,**而猜错不报错** ——
    这正是本缺陷的成因(dashboard 那处以为自己是平台视图)。
    """
    assert platform_test_clause() != own_scope_test_clause()


# ══════════════════════════════════════════════════════════════════════════
# 接线:两处都必须**用**这份规则,不许各写一份
# ══════════════════════════════════════════════════════════════════════════

def _calls_shared_rule(src: str) -> bool:
    """是否**调用**了共享规则(不是「文件里提到过这个名字」)。

    🔴 判调用不判文本:只 import 不调用,包含判定照绿 ——
    今天这个病已经出现六次(短路/旁路/常量右值/换赋值目标/判据自构/import 顶名字)。
    """
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call):
            name = getattr(n.func, "id", getattr(n.func, "attr", None))
            if name in ("own_scope_test_clause", "platform_test_clause",
                        "_own_scope_test_clause", "_own_scope_clause", "_platform_clause"):
                return True
    return False


@pytest.mark.parametrize("rel", CONSUMERS)
def test_both_consumers_call_the_shared_rule(rel):
    assert _calls_shared_rule((ROOT / rel).read_text(encoding="utf-8")), (
        "%s 没有调用共享的可见性规则 —— 它在自己写一份" % rel)


def test_the_call_detector_is_not_fooled_by_an_import():
    """正样本自证:光 import 不算接线。"""
    only_import = "from services.brand_test_visibility import own_scope_test_clause\nx = 1\n"
    real = "from services.brand_test_visibility import own_scope_test_clause\nx = own_scope_test_clause()\n"
    assert _calls_shared_rule(only_import) is False, "只有 import 却算成接线了"
    assert _calls_shared_rule(real) is True, "真调用却没认出来 —— 尺子坏了"


@pytest.mark.parametrize("rel", CONSUMERS)
def test_no_consumer_writes_its_own_is_test_clause(rel):
    """🔴 两处都不许再出现**手写的** is_test 子句 —— 那就是"各写一份"本身。

    分母是 CONSUMERS 两个文件;规则模块自己当然要有那一份,所以不在分母里。
    """
    src = (ROOT / rel).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
    assert "b.is_test = FALSE" not in code, (
        "%s 里还留着手写的 is_test 子句 —— 与共享规则会漂开" % rel)


#: 🔴 存量手写拷贝(平台级视图,写法没错、只是各写一份)。**显式冻结,等单独一单。**
FROZEN_INLINE_COPIES = frozenset({
    'auth_api.py', 'm3_api.py', 'm3_export_endpoints.py',
})


def _api_modules_with_inline_clause():
    out = set()
    for p in (ROOT / 'api').glob('*.py'):
        code = chr(10).join(l for l in p.read_text(encoding='utf-8').split(chr(10))
                            if not l.strip().startswith('#'))
        if 'b.is_test = FALSE' in code:
            out.add(p.name)
    return out


def test_no_new_inline_copies_of_the_rule():
    """规则原文不许出现**新的**手写拷贝(注释不算)。

    🔴 存量三处**显式冻结**,不是「没看见」:
      `auth_api.py` / `m3_api.py` / `m3_export_endpoints.py` —— 它们都是**平台级**视图,
      排除 is_test 本身没写错,只是各写了一份。本单的范围是 dashboard 与 brand_api
      的**口径打架**,把这三处一起改属于扩范围;但也不能假装它们不存在。

      冻结的代价与收益:**新拷贝仍然会红**,存量摆在明面上等单独一单。
      配对臂钉住豁免不许变成死条目 —— 名单一旦过期,它就成了
      「看起来在管、其实什么都不管」的东西,比没有名单更坏。
    """
    extra = _api_modules_with_inline_clause() - FROZEN_INLINE_COPIES
    assert not extra, '出现了新的手写 is_test 子句:%s' % sorted(extra)


def test_the_frozen_copies_are_not_dead_entries():
    """🔴 配对臂:豁免名单里每一项都必须**仍然**在手写那份子句。

    某一项被修好了却留在名单里 ⇒ 名单过期 ⇒ 它变成一个不管事的摆设。
    """
    still = _api_modules_with_inline_clause()
    stale = sorted(FROZEN_INLINE_COPIES - still)
    assert not stale, '这些豁免项已经不再手写子句了,应当移出名单:%s' % stale
