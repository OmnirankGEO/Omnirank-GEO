"""brand.is_test auto-detect helper · CTO-15.18 PM 干预 A.2

老板红线(2026-04-28):
- 测试客户(M3验收测试客户_20260427_深圳家装等)暴露在 C 端 5 公开链接 + 销售话术
- → SaaS 设计 ABC:加 is_test boolean 字段 · 默认 ON 隐藏测试客户

auto-detect 策略(白名单优先 · Q3 老板裁决):
1. 8 个真客户 ID 白名单 → 永远 false(不论名字)
2. 名字含 "测试|验收|压测|_demo|_test" 或 latin 词(test/e2e/stress/breakdown,
   两侧非字母数字)→ true(双保险)
3. 默认 false(代理建真客户)

admin 后台可通过 brands.is_test_locked=true 手动锁定 is_test 不让 auto-detect 改。
"""
from __future__ import annotations
import re

# 真客户白名单(老板 Q3 裁决 · 这 8 个 brand_id 永远不算 test · 即使名字含"测试"字也不行)
REAL_CLIENT_BRAND_IDS: frozenset[int] = frozenset({94, 96, 99, 105, 107, 140, 152, 154})

# 名字 substring 自动标记(双保险 · 未来新建 brand 名字含这些词自动 is_test=true)
#
# 🔴 [#99 · 2026-09-05] 与 CLAUDE.md 铁律 5 项对齐 + Deploy 只读实证补 4 项。
#    删掉的 `demo|debug|sandbox`:它们**不在铁律里**,而且是**裸词**——
#    「demo 家居」「Sandbox 咖啡」这类真品牌名会被误标成测试客户,
#    而测试客户 filter 默认 ON ⇒ 她的客户从列表里**消失**,且不报错。
#    补上的 `压测|stress|e2e|breakdown`:Deploy 只读实证存量 32 个未被 5 项覆盖的
#    is_test 品牌里 28 个是压测/E2E 合成物(「压测客户01」「stress-J-1778426622」
#    「breakdown_e2e_prod 的创作空间」)。
#
# 🔴 **词界为什么不是 `\b`**:`_` 是正则里的**词字符**,所以
#    `\bbreakdown\b` **接不住** `breakdown_e2e_prod` —— 实测「加 \b 漏标 1 个正样本 /
#    去 \b 误标 latest、Protest、e2enterprise」,**两边各在一个方向上坏掉**。
#    换的是**边界的定义**而不是宽度:两侧不是字母或数字 ⇒ `_ - 空格 CJK` 都算分隔符。
#
# 🔴 `_test` 这一支现在是**冗余**(新边界已能接住 `abc_test`)——
#    **故意留着,别删**:留一支冗余代价是零,删错的代价是漏标。
_LATIN_TEST_WORDS = ("test", "e2e", "stress", "breakdown")
TEST_NAME_PATTERNS = re.compile(
    r"测试|验收|压测|_demo|_test"
    r"|(?<![A-Za-z0-9])(?:" + "|".join(_LATIN_TEST_WORDS) + r")(?![A-Za-z0-9])",
    re.IGNORECASE)


def detect_is_test_for_new_brand(name: str | None) -> bool:
    """新建 brand 时根据名字 auto-detect is_test

    新建场景没 brand_id · 只能从名字判断
    """
    if not name:
        return False
    return bool(TEST_NAME_PATTERNS.search(name))


def detect_is_test_for_existing_brand(brand_id: int | None, name: str | None) -> bool:
    """已存在 brand 的 is_test 判定(migration backfill 用)

    优先级:
    1. brand_id 在白名单 → false(覆盖名字判断)
    2. 名字含测试 substring → true
    3. 默认 false
    """
    if brand_id is not None and brand_id in REAL_CLIENT_BRAND_IDS:
        return False
    return detect_is_test_for_new_brand(name)


def should_auto_apply_is_test(is_test_locked: bool) -> bool:
    """admin 锁定后 auto-detect 不再改"""
    return not bool(is_test_locked)
