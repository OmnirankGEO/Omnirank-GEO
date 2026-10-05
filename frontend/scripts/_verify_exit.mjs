/**
 * #95 · verify-*.mjs 的**三态退出码**共享契约。
 *
 *     0  PASS         全部检查跑了且通过
 *     1  FAIL         跑了,有检查没过     ⇒ 处置:**改代码**
 *     3  UNEVALUATED  根本没跑成           ⇒ 处置:**去构建 / 起服务 / 装依赖**
 *
 * ## 为什么必须分开
 *
 * 「没评估」与「失败」压成同一个退出码时,读的人**分不出该做什么** ——
 * 而这两件事的处置正好相反。2026-09-05 实测 28 个 verify 脚本:
 *
 *     rc=0  18
 *     rc=1   5   ← 其中 **4 个其实是「未评估」**
 *     rc=3   5
 *
 * 那 4 个:
 *     verify-no-lookbehind         dist/assets ENOENT(没有产物可扫)
 *     verify-observation-bundle    未找到 dist-observation-preview
 *     verify-public-privacy-bundle scandir dist ENOENT —— 而且是**未捕获异常**崩退
 *     verify-safeimage-render      连不上 127.0.0.1:8793(要服务在跑)
 * 只有 `verify-organization-enum-labels` 是真失败(枚举映射缺 5 项)。
 *
 * ⇒ 在没构建的树上跑这批脚本,**80% 的「失败」是假的**,
 *   而它们与真失败在退出码上一模一样。
 *
 * ## 报文必须**列名**未评估项
 *
 * 只报「3 项未评估」没有用:不同形态处置不同(缺产物 / 缺服务 / 缺依赖)。
 * `unevaluated()` 因此要求传**具名条目数组**,不接受计数。
 */

export const EXIT = Object.freeze({ PASS: 0, FAIL: 1, UNEVALUATED: 3 });

/**
 * 未评估退出:必须给出**具名**条目(不是计数)。
 * @param {string[]} items 每条写清「什么没跑成 + 怎么才能跑」
 * @param {string} [what] 本脚本在验什么(用于报文抬头)
 */
export function unevaluated(items, what = '') {
    if (!Array.isArray(items) || items.length === 0) {
        console.error('🔴 unevaluated() 必须传**具名条目数组** —— 只报数量的话,');
        console.error('   读的人不知道该去构建、起服务还是装依赖(三者处置不同)。');
        process.exit(EXIT.FAIL);
    }
    console.log(`\n⚠️ 未评估${what ? '(' + what + ')' : ''}:${items.length} 项没跑成 —— ` +
        '这**不是**「通过」,也**不是**「失败」。');
    for (const it of items) console.log('   · ' + it);
    console.log('   处置:先让它能跑(构建 / 起服务 / 装依赖),再看结论。');
    process.exit(EXIT.UNEVALUATED);
}

/** 失败退出:有检查真的没过 ⇒ 去改代码。 */
export function fail(items, what = '') {
    const list = Array.isArray(items) ? items : [String(items)];
    console.error(`\n❌ 失败${what ? '(' + what + ')' : ''}:${list.length} 项没通过。`);
    for (const it of list) console.error('   · ' + it);
    process.exit(EXIT.FAIL);
}

/** 通过退出。 */
export function pass(msg = '') {
    if (msg) console.log('✅ ' + msg);
    process.exit(EXIT.PASS);
}

/**
 * 把「前置条件不满足」统一成未评估:路径不存在 ⇒ 直接 exit 3 并说明怎么补。
 * @param {Array<{path: string, how: string}>} needs
 */
export function requirePaths(needs, what = '') {
    const fs = require('node:fs');
    const missing = needs.filter((n) => !fs.existsSync(n.path))
        .map((n) => `${n.path} 不存在 —— ${n.how}`);
    if (missing.length) unevaluated(missing, what);
}
