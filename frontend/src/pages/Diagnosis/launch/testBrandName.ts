/**
 * 「这个名字会被当成测试客户吗」—— 前端复述**今天真在跑的**那条规则。
 *
 * 🔴 权威源:`utils/is_test_brand.py:23` `TEST_NAME_PATTERNS`,
 *    由 `detect_is_test_for_new_brand()` 提供,三个写入点(`auth_api.py:579`、
 *    `brand_api.py:658`、`brand_api.py:1168`)都调它 —— 它才是真正决定 `brands.is_test` 的那份。
 *
 * 🔴 **不要按文档写。** CLAUDE.md M3 铁律 5 与三处代码注释写的是 5 项
 *    (测试|test|_demo|_test|验收),而真正在跑的有 **8 项** —— 多出
 *    `demo` / `debug` / `sandbox`,从没进过任何文档。
 *    另有第三份 `operation_packages_api.py:43`(公开链接过滤)对拉丁词加了词边界,
 *    与本份不同 —— 12 个样本里三者答案不同的有 8 个(见 #99 / WO_A_TEST_BRAND_RULE_DIVERGENCE)。
 *    这里复述的是**打标那一份**:提示要说今天真会发生什么,不说大家以为会发生什么。
 *    说错一次比不说更糟(同 #64「取不到价就不显示」的口径)。
 *
 * 🔴 这是同一条规则的第二份 ⇒ 判据配**跨层锁**:本文件的模式集合必须逐项等于
 *    `utils/is_test_brand.py:23` 的交替项集合,不一致即红
 *    (`verify-pkg1-subject-summary.mjs` A 段)。Owner 若裁定统一规则(#99),
 *    这条锁会红一次 —— 那是**预期的换锚**,不是缺陷。
 */

/** 逐项对应 Python 那条正则的交替项(顺序无关,判据按集合比)。 */
export const TEST_NAME_PATTERNS: readonly string[] = [
    '测试', 'test', '_demo', '_test', '验收', 'demo', 'debug', 'sandbox',
];

/**
 * 名字是否会被自动打成测试客户。
 * 与 Python 侧一致:**子串匹配、不加词边界、大小写不敏感**。
 * ⚠️ 因此 `Contest 传媒` / `Latest 潮流` 这类真客户名也会命中 —— 这正是要提示她的原因。
 */
export function looksLikeTestBrand(name: string | null | undefined): boolean {
    if (!name) return false;
    const lower = name.toLowerCase();
    return TEST_NAME_PATTERNS.some((p) => lower.includes(p.toLowerCase()));
}

/** 命中了哪几项 —— 提示里要指名道姓,否则她不知道该改哪个字。 */
export function matchedTestPatterns(name: string | null | undefined): string[] {
    if (!name) return [];
    const lower = name.toLowerCase();
    return TEST_NAME_PATTERNS.filter((p) => lower.includes(p.toLowerCase()));
}
