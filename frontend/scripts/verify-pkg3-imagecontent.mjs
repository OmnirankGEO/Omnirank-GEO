#!/usr/bin/env node
/**
 * 判据 · 包三 §G「图文板块」收口。三态退出码:0 / 1 / 3。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const rd = (p) => strip(readFileSync(join(ROOT, p), 'utf8'));
const SHARED = rd('src/pages/geoChannelShared.tsx');
const PAGE = rd('src/pages/GeoContentCenter.tsx');

let bad = 0; let notEvaluated = 0; const skipped = [];
const ENV_SKIPS = ['E0', 'F0'];
const ok = (c, l) => { console.log(`${c ? '  ✅' : '  🔴'} ${l}`); if (!c) bad += 1; };

// ── A 🔴 成图两键:按**她所在设备**说话 ─────────────────────────────
/**
 * 🔴 手机上「下载」这个词没有意义 —— 文件是进相册的。
 *    用在她那台设备上成立的词,而不是一个对一半用户为假的词。
 */
ok(/data-testid="gcc-save-image"/.test(SHARED), 'A1 保存图片那颗键可被判据定位');
const aIdx = SHARED.indexOf('gcc-save-image');
const aBlock = SHARED.slice(aIdx, aIdx + 700);
ok(aBlock.length > 200, 'A0 正样本臂:切到了那颗键');
ok(/isPhone \? '保存到手机' : '下载图片'/.test(aBlock),
    'A2 🔴 手机说「保存到手机」、桌面说「下载图片」—— 两端各自为真');
ok(/const isPhone = useIsMobile\(\)/.test(SHARED),
    'A3 判定来自全站 `useIsMobile`,不是这一页自己 sniff UA');
ok(/复制文案/.test(SHARED), 'A4 另一颗键「复制文案」仍在(两键是一对,少一个就不成对)');

// ── B 🔴 等待期给人话,且**不编进度** ───────────────────────────────
/**
 * 🔴 她在等待时要判断的只有两件:还要多久、能不能走开。
 *    不说,她就会守着屏幕,或者以为卡住了反复点 —— 而点一次就是一次扣费确认。
 *    🔴 没有真实进度可报时**不编百分比**:编出来的进度条比没有更糟,
 *       它会在 90% 停住,而她会认为系统坏了。
 */
ok(/data-testid="gcc-generating-wait"/.test(PAGE), 'B1 生成中有一句等待说明');
const bIdx = PAGE.indexOf('gcc-generating-wait');
const bBlock = PAGE.slice(bIdx, bIdx + 500);
ok(bBlock.length > 150, 'B0 正样本臂:切到了那一句');
ok(/分钟|秒/.test(bBlock), 'B2 🔴 说了大概要多久');
ok(/可以先去|不用等|自己更新/.test(bBlock), 'B3 🔴 说了能不能走开(她最想知道的第二件事)');
ok(/不用重复点|不要重复点/.test(bBlock),
    'B4 🔴 明说别重复点 —— 重复点一次就是重复扣费确认一次');
/**
 * 🔴 第一版写成 `/\{n\}%|进度 \d+%/` —— 我按**想象中的形状**列模式,
 *    而毒写的是「已完成 90%」,两个都不命中。今天第二次栽在同一件事上
 *    (上一次是绝对化承诺的词表按完整短语列)。
 *    规则:**禁的是那一类事实,不是我想到的那几种写法** —— 任何百分数都算编进度。
 */
const FAKE_PROGRESS = /\d+\s*%/;
ok(!FAKE_PROGRESS.test(bBlock),
    'B5 🔴 **不编百分比**:没有真实进度就别报数字,它会停在 90% 而她认为系统坏了');
ok(FAKE_PROGRESS.test('已完成 90%,大概要一两分钟'),
    'B5a 🔴 注入正样本:同一个谓词对那句毒的原文**必须命中**');

// ── C 🔴 对客文案不说内部机制 ───────────────────────────────────────
/**
 * 🔴 先剥掉模板插值 `${...}` —— 里面是**代码**不是给人看的字。
 *    不剥的话,`推广包 #${item.job_id}` 会让文案普查命中标识符 job_id,
 *    而屏幕上她看到的其实是「推广包 #123」。**把代码算进文案分母 = 假红**,
 *    而假红逼人放宽词表,最后把真的问题一起放过去。
 */
/**
 * 🔴 文案普查的**已知边界**,写在这儿而不是靠记忆:
 *  ① 引号串那半可靠:剥掉 `${...}` 之后,里面剩的就是屏幕上的字。
 *     不剥的话 `推广包 #${item.job_id}` 会让普查命中标识符 job_id,
 *     而她看到的其实是「推广包 #123」——**把代码算进文案分母 = 假红**,
 *     而假红逼人放宽词表,最后把真问题一起放过去。
 *  ② JSX 那半**不完全可靠**:一次匹配可能跨越「文本节点 + 兄弟表达式」。
 *     这里用「代码形状」过滤掉明显是代码的片(真文案里不会有 `||`/`=>`/`throw`)。
 *     ⚠️ 仍可能漏掉与代码贴在一起的文案 —— 正则做不到可靠切分 JSX 文本节点,
 *     今天在同一根轴上调了五次松紧,每次都从一端滑到另一端。
 *     真正的解是换仪器(TS AST,仓里 test-diagnosis-launch-ui.mjs 已有先例),
 *     Review 已排在收官后。**这条边界是声明,不是门** —— 别把它当成覆盖。
 */
const stripInterp = (t) => t.replace(/\$\{[^{}]*\}/g, '~');
const looksLikeCode = (t) => /\|\||&&|=>|!==|return |throw /.test(t);
const cs = [
    ...(stripInterp(PAGE).match(/['"`][^'"`\n]*[一-龥][^'"`\n]*['"`]/g) || []),
    ...(PAGE.replace(/\{[^{}]*\}/g, '~').match(/(?<![=-])>[^<>;]*[一-龥][^<>;]*</g) || [])
        .filter((x) => !looksLikeCode(x)),
];
const JARGON = ['token', 'SOV', 'asset_id', 'undefined', 'NaN', '[object Object]'];
ok(cs.length >= 40, `C0 正样本臂:抽到 ${cs.length} 条对客文案`);
const hit = JARGON.filter((j) => cs.some((c) => c.includes(j)));
ok(hit.length === 0, `C1 🔴 零工程术语(实得 ${JSON.stringify(hit)})`);
ok(JARGON.some((j) => '生成失败 undefined'.includes(j)),
    'C2 反向对照:词表对样例串确实命中(否则 C1 的"零"只是词表坏了)');

// ── E 🔴 红臂 ───────────────────────────────────────────────────────
const BASE = '2b35f21fd';
try {
    const baseShared = execFileSync('git', ['show', `${BASE}:frontend/src/pages/geoChannelShared.tsx`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
    const basePage = execFileSync('git', ['show', `${BASE}:frontend/src/pages/GeoContentCenter.tsx`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
    ok(!/保存到手机/.test(baseShared), 'E1 红臂:改动前只有「下载图片」一种说法');
    ok(!/gcc-generating-wait/.test(basePage), 'E2 红臂:改动前等待期没有那一句');
    ok(/下载图片/.test(baseShared), 'E3 配对臂:基线里本来就有的锚确实在');
} catch (e) {
    notEvaluated += 1; skipped.push('E0');
    console.log(`  ⚠️ E0 **未评估**:取不到基线 ${BASE}(${String(e.message).slice(0, 50)})`);
}

// ── F dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1; skipped.push('F0');
    console.log('  ⚠️ F0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    ok(js.includes('保存到手机'), 'F1 新说法进了产物');
    ok(js.includes('gcc-generating-wait'), 'F2 等待说明也进了产物');
}

const unexpected = skipped.filter((t) => !ENV_SKIPS.includes(t));
if (unexpected.length) { bad += 1; console.log(`  🔴 SKIP-GUARD 清单外未评估:${JSON.stringify(unexpected)}`); }
if (bad > 0) { console.log(`\n🔴 ${bad} 项不通过`); process.exit(1); }
if (notEvaluated > 0) { console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估`); process.exit(3); }
console.log('\n✅ 全部通过');
process.exit(0);
