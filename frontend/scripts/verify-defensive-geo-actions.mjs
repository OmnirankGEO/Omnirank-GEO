#!/usr/bin/env node
/**
 * UI-37 · provider 动作绑定静态闸(窗D · 门四微单后**纯前端**版)。
 *
 * ## 这里只剩纯前端的检查,跨层同源判据已搬走
 *
 * 原来这个脚本还查一条「前端动作集合 == 后端 `_STATE_ACTIONS`」。
 * 那条要读 `services/defensive_geo/presentation/registries.py` ——
 * 而 Dockerfile 的 `frontend-builder` 阶段只 `COPY frontend/ ./`,
 * **后端源码不在那一层**。build 链的越界闸(verify-no-backend-refs-in-build-chain)
 * 因此把它判红,判得对。
 *
 * 🔴 没有采用的两种"解法",以及为什么不采用:
 *   · 给它加 `try/catch → SKIP` —— 那就是**裸奔判据**:每次构建大声跳过、
 *     打印「这不是通过,是没跑」然后 exit 0。构建绿 ≠ 判据过,而大家只看得到构建绿。
 *     2026-08-17 清扫掉的两条(test-diagnosis-launch-ui / test-referral-wiring)就是这个下场;
 *   · 把本脚本从 build 链摘掉 —— 不接线的锁等于没有。
 *
 * 采用的是**把判据挪到够得着两层的那一侧**:后端 pytest 全仓可达,
 * 见 `tests/defensive_geo_w4_2026_08_22/test_frontend_backend_action_parity.py`。
 * 搬家时把原 `--selftest` 里的 `UI37/action-extra` 注毒一起搬了,
 * 并补齐了原来缺的反向(action-missing)——**判据走到哪,毒跟到哪**。
 *
 * ## 留在这里的:纯前端、零越界
 *
 * 每条「必须命中」都配一条「必须不命中」。`--selftest` 逐把注毒,
 * 证明它真能转红 —— 没有自证的负向锁与恒绿无法区分
 * (本仓 2026-08-20 记过:`0x08` 假 \b 负向锁恒绿顶了两轮复审)。
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');

const stripComments = (t) =>
    t
        .split('\n')
        .filter((l) => !l.trim().startsWith('*') && !l.trim().startsWith('//'))
        .join('\n');

const read = (rel) => stripComments(readFileSync(join(SRC, rel), 'utf8'));

const ACTIONS_FILE = 'lib/defensiveGeoActions.ts';
const VIEW_FILE = 'components/defensiveGeo/DefensiveReportView.tsx';

const failures = [];
const fail = (id, msg) => failures.push(`${id}: ${msg}`);

/** 每个绑定都必须有非空 route(POR-16:没有 dead CTA)。 */
function bindingsWithoutRoute(src) {
    const blocks = src.split('{').filter((b) => b.includes('key:'));
    return blocks.filter((b) => b.includes('key:') && !b.includes('route:')).length;
}

function run(actionsSrc, viewSrc) {
    failures.length = 0;

    // ── ① POR-16:零 dead CTA ────────────────────────────────────────────
    if (bindingsWithoutRoute(actionsSrc) > 0)
        fail('POR16/dead-cta', '有绑定缺 route —— 只有 label 没有 target 就是死按钮');

    if (/card\.actions\s*\.\s*map/.test(viewSrc))
        fail(
            'POR16/unfiltered-actions',
            '直接 map card.actions —— 未绑定的 key 会渲染成点不动的按钮;必须过 renderableActions',
        );

    // ── ② §9.8:触控目标 >= 44px ─────────────────────────────────────────
    if (!/min-h-\[44px\]/.test(viewSrc))
        fail('UI98/touch-target', '动作按钮没有 min-h-[44px] —— §9.8 要求常用触控目标 >=44');

    // ── ③ UI-37:provider 面禁「联系自己」───────────────────────────────
    if (/联系您的服务商|联系你的服务商/.test(viewSrc))
        fail(
            'UI37/contact-yourself',
            'provider 报告面出现「联系您的服务商」—— 服务商自己就是服务商',
        );

    // ── 附:CUR-03 前端不许自己按数字判档 ────────────────────────────────
    // 纯前端、零越界,与上面三查同层,故保留(窗B 在别的文件上已立同族锁,
    // 这一条覆盖窗D 新加的卡片渲染)。
    if (/card\.(valid|planned)\s*[><=]/.test(viewSrc))
        fail(
            'CUR03/client-threshold',
            '前端按 summary 数字自己判档 —— 等级/状态必须读服务端 state 与 level_meta',
        );

    return failures;
}

const actionsSrc = read(ACTIONS_FILE);
const viewSrc = read(VIEW_FILE);

if (process.argv.includes('--selftest')) {
    // 逐把注毒:每一发都必须让**对应那把锁**报出来。
    // 跨层同源那一发已随判据搬去后端 pytest,这里不再有它。
    const poisons = [
        // 🔴 毒必须**真的**把 route 键拿掉。第一版写的是
        //    `split('route:').join('notaroute:')` —— 而 'notaroute:' 仍然
        //    **包含**子串 'route:',探测器照旧命中,这一发当场 SURVIVED。
        //    子串陷阱,本仓记过(secret_string_cannot_be_substring_of_allowed_label)。
        ['POR16/dead-cta', () => [actionsSrc.split('route:').join('href_:'), viewSrc]],
        ['POR16/unfiltered-actions', () => [actionsSrc, viewSrc + '\n{card.actions.map(a => a)}']],
        ['UI98/touch-target', () => [actionsSrc, viewSrc.split('min-h-[44px]').join('h-8')]],
        ['UI37/contact-yourself', () => [actionsSrc, viewSrc + '\n<span>联系您的服务商</span>']],
        ['CUR03/client-threshold', () => [actionsSrc, viewSrc + '\n{card.valid > 3 && <b/>}']],
    ];
    let bad = 0;
    for (const [id, mutate] of poisons) {
        const [a, v] = mutate();
        const got = run(a, v);
        const hit = got.some((f) => f.startsWith(id));
        console.log(`${hit ? 'OK ' : '!! '}${id} ${hit ? 'KILLED' : 'SURVIVED'}`);
        if (!hit) bad++;
    }
    // 干净输入必须**零命中**(证明这些锁不是恒红)。
    const clean = run(actionsSrc, viewSrc);
    console.log(`${clean.length === 0 ? 'OK ' : '!! '}clean-input ${clean.length} 条`);
    if (clean.length) bad++;
    process.exit(bad ? 1 : 0);
}

const result = run(actionsSrc, viewSrc);
if (result.length) {
    console.error('❌ UI-37 前端动作闸不通过:');
    for (const f of result) console.error('  · ' + f);
    process.exit(1);
}
// 🔴 指向后端那条同源判据的坐标**只写在注释里**,不进 console.log。
//    越界闸剥注释后再扫,所以注释里的路径不命中;而 console.log 里的字符串
//    是**真代码**,带 .py 会被判红 —— 这正是本仓记过的
//    「引用裁决原文会让裸串结构锚判红」同一形态。第一版就踩了这一脚。
//    坐标见:tests/defensive_geo_w4_2026_08_22/test_frontend_backend_action_parity.py
console.log('✅ UI-37 前端动作闸通过(零 dead CTA / 触控 44px / provider 面无「联系自己」)');
console.log('   跨层同源判据在后端 pytest(坐标见本文件顶部注释)');
