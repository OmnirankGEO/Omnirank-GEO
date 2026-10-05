#!/usr/bin/env node
/**
 * WO_262 反臂 —— 没毒过的锁不能当证据。
 *
 * 🔴 写本文件时**这支臂的基线有 19 格红**(Codex 图文包把选题面板搬走,
 *    臂的锚停在旧形态,而它不在 build 链、也不在 arms,两班没人跑)。
 *    [WO_258 后] 那 19 格已逐格定性改锚,红格数改成**现场数**,不再写死;
 *    但下面的读法不改 —— 基线里只要还有一格红,rc 就又失去分辨力。
 *    红基线会让**每一发毒都像命中**,所以本文件:
 *      · **不看 rc**(基线有红格时它恒为 1,毒不毒都一样);
 *      · 只看**我这几格自己**的 OK→FAIL,并且要求**其余格一个都不许变**。
 *    「其余不变」这条同样重要:少了它,一发把整页打崩的毒也会让我的格变红,
 *    而那证明的是"页面坏了",不是"这一格守得住"。
 *
 * 毒:把弹窗顶部那段原因块拿掉(`data-testid="redraw-reason"` 整块)。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const ARM = join(ROOT, 'scripts', 'test-image-note-detail-render.mjs');
const DETAIL = join(ROOT, 'src', 'pages', 'Writing', 'DouyinPostDetail.tsx');
const MINE = /^W262-/;

const run = () => {
    let out = '';
    try {
        out = execFileSync(process.execPath, [ARM], { cwd: ROOT, encoding: 'utf8', timeout: 20 * 60 * 1000 });
    } catch (e) {
        out = String(e.stdout || '') + String(e.stderr || '');
    }
    /* 逐格读数:id → OK/FAIL。装饰(emoji)可能在名字里,所以先跳过非字母数字。
       🔴 [WO_258] 格行缩进两格,顶格的「FAIL 19 项不通过」是汇总行,不是一格。
          用 `[ \t]+` 不用 `\s*`:后者让汇总行(连同它前面那一空行)被收成一格「19」;
          跳装饰那段也不许跨行,否则「FAIL 判据不可用…」会顺到下一行去抓个词当格名。 */
    const cells = {};
    for (const m of out.matchAll(/^[ \t]+(OK|FAIL)[ \t]+[^A-Za-z0-9\n]*([A-Za-z0-9][^\s]*)/gm)) {
        cells[m[2]] = m[1];
    }
    return cells;
};

console.log('=== 基线(逐格读数;不看 rc)===');
const base = run();
const mineBase = Object.entries(base).filter(([k]) => MINE.test(k));
console.log(`  我的格 ${mineBase.length} 个:${mineBase.map(([k, v]) => `${k}=${v}`).join(' ')}`);
if (mineBase.length === 0 || mineBase.some(([, v]) => v !== 'OK')) {
    console.log('🔴 基线里我的格没有全绿 —— 注毒结果不作数');
    process.exit(3);
}
const othersBase = Object.entries(base).filter(([k]) => !MINE.test(k));
const redBase = othersBase.filter(([, v]) => v === 'FAIL').map(([k]) => k);
console.log(`  其余 ${othersBase.length} 格作为对照冻下来(其中基线就红的 ${redBase.length} 格:${redBase.join(',') || '无'})`);

const original = readFileSync(DETAIL, 'utf8');
const START = '                        {activeMissing.length > 0 && (';
const END = '                        )}\n';
const i = original.indexOf(START);
if (i < 0) {
    console.log('🔴 锚没命中,这一发**没下成**(不是"锁没牙")');
    process.exit(3);
}
const j = original.indexOf(END, i);
if (j < 0) {
    console.log('🔴 找不到原因块的收口,这一发没下成');
    process.exit(3);
}
const poisoned = original.slice(0, i) + original.slice(j + END.length);

let rc = 0;
try {
    writeFileSync(DETAIL, poisoned, 'utf8');
    console.log('\n=== 毒:拿掉弹窗顶部那段原因块 ===');
    const after = run();
    const mineNow = Object.entries(after).filter(([k]) => MINE.test(k));
    const flipped = mineNow.filter(([k, v]) => v === 'FAIL' && base[k] === 'OK').map(([k]) => k);
    console.log(`  我的格:${mineNow.map(([k, v]) => `${k}=${v}`).join(' ')}`);
    console.log(`  翻红:${flipped.join(',') || '(一个都没翻)'}`);

    /* 🔴 对照:其余格一个都不许变 —— 否则"我的格红了"可能只是整页崩了。 */
    const drift = othersBase.filter(([k, v]) => after[k] !== undefined && after[k] !== v).map(([k]) => k);
    console.log(`  其余格漂移:${drift.join(',') || '无(基线红格原样,其余原样)'}`);

    const good = flipped.includes('W262-2') && drift.length === 0;
    if (!good) rc = 1;
    console.log(`\n${good ? '✅ 有牙:拿掉原因块 ⇒ W262-2 翻红,且其余格一个没动'
        : '🔴 出乎预料:要么 W262-2 没翻红(锁没牙),要么其余格跟着动了(毒打崩了页面)'}`);
} finally {
    writeFileSync(DETAIL, original, 'utf8');
}
console.log('\n=== 复原自证 ===');
console.log(readFileSync(DETAIL, 'utf8') === original ? '源文件逐字回位' : '🔴 没回位');
process.exit(rc);
