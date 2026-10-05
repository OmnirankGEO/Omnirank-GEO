#!/usr/bin/env node
/**
 * WO_276 反臂 —— 没毒过的锁不能当证据。
 *
 * 读两把尺子,格名带前缀防撞名:
 *   c: = test-chunk-heal.mjs(单元 / 结构,build 链)
 *   r: = test-chunk-heal-render.mjs(行为臂,真 chromium + 真 HTTP 缓存)—— 每发毒都**重打一份产物**
 *        (vite build 到临时目录,A276_DIST 指过去),量的是带毒的真产物,不是旧 dist
 *
 * 读法(不看 rc):每一发都要「**点名的那几格 OK→FAIL ∧ 其余格一个都不许动 ∧ 源文件逐字回位**」。
 *   · 下毒前先过语法尺子对照臂(原文必须判过),下毒后再判 —— 写成语法错 = 「毒没下成」,不算咬住;
 *   · 一发把整页打崩的毒也会让点名格变红 —— 「其余不动」挡的就是这种假咬。
 *
 * 七发(工单点名的一发 = K1「去掉 cache:'reload' ⇒ 红」):
 *   K1 请求参数去掉 cache:'reload'                  ⇒ c:U1 c:S3 · r:H301.1/.2 H404.1/.2 HB.1/.2 HM.1/.2/.4/.5
 *   K2 自动出口不再起自愈(退回老行为:直接刷新)    ⇒ c:S1 · r:H301.1/.2 H404.1/.2 HM.1/.2/.4/.5(按钮那条路不受影响)
 *   K3 「立即刷新」退回裸 reload                     ⇒ c:S2 · r:HB.1/.2
 *   K4 不读 chunk 源文本(不跟进清单与块间引用)      ⇒ c:U1 c:U2 · r:HM.4/.5(依赖块经 modulepreload 种子照样刷到,
 *                                                     只有「清单全刷」那一格救不了 —— 正是它存在的理由)
 *   K5 并发常量 6 → 12                               ⇒ c:U3 c:S4
 *   K6 拿掉总时限                                    ⇒ c:U4
 *   K7 「已自愈」标记不再挡自动路径                    ⇒ c:U6
 *
 * 三态退出码:0 全部有牙 / 1 有一发不过 / 3 基线不干净或尺子自己坏了。
 */
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const F = (rel) => join(ROOT, 'src', ...rel.split('/'));
const VITE = join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js');
const CACHE = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE, { recursive: true });
const DIST = mkdtempSync(join(CACHE, 'a276-poison-dist-'));
process.on('exit', () => { try { rmSync(DIST, { recursive: true, force: true }); } catch { /* 尽力 */ } });

const R_ALL = ['r:H301.1', 'r:H301.2', 'r:H404.1', 'r:H404.2', 'r:HM.1', 'r:HM.2', 'r:HM.4', 'r:HM.5'];
const POISONS = [
    { id: 'K1', why: "请求参数去掉 cache:'reload'", file: 'lib/chunkHeal.ts',
        from: "export const CHUNK_HEAL_FETCH_INIT: RequestInit = { cache: 'reload', credentials: 'same-origin' };",
        to: "export const CHUNK_HEAL_FETCH_INIT: RequestInit = { credentials: 'same-origin' };",
        targets: ['c:U1', 'c:S3', ...R_ALL, 'r:HB.1', 'r:HB.2'] },
    { id: 'K2', why: '自动出口不再起自愈(退回老行为:直接刷新)', file: 'lib/errorReporter.ts',
        from: '    const healed = startChunkHeal(reason);',
        to: '    const healed = Promise.resolve();',
        targets: ['c:S1', ...R_ALL] },
    { id: 'K3', why: '「立即刷新」退回裸 reload', file: 'App.tsx',
        from: "<button onClick={() => { void startChunkHeal(this.state.error?.message || '', { force: true }).then(() => window.location.reload()); }}",
        to: "<button onClick={() => { maybeReloadOnChunkError(''); window.location.reload(); }}",
        targets: ['c:S2', 'r:HB.1', 'r:HB.2'] },
    { id: 'K4', why: '不读 chunk 源文本(不跟进清单与块间引用)', file: 'lib/chunkHeal.ts',
        from: '                if (/\\.m?js$/.test(url)) chunkRefs(await res.text(), url, env.baseUrl).forEach(push);',
        to: '                void chunkRefs;',
        targets: ['c:U1', 'c:U2', 'r:HM.4', 'r:HM.5'] },
    { id: 'K5', why: '并发常量 6 → 12', file: 'lib/chunkHeal.ts',
        from: 'export const CHUNK_HEAL_CONCURRENCY = 6;', to: 'export const CHUNK_HEAL_CONCURRENCY = 12;',
        targets: ['c:U3', 'c:S4'] },
    { id: 'K6', why: '拿掉总时限', file: 'lib/chunkHeal.ts',
        from: '        const timer = setTimeout(() => { result.timedOut = true; finish(); }, deadlineMs);',
        to: '        const timer = undefined as unknown as ReturnType<typeof setTimeout>; void deadlineMs;',
        targets: ['c:U4'] },
    { id: 'K7', why: '「已自愈」标记不再挡自动路径', file: 'lib/chunkHeal.ts',
        from: '            if (Date.now() - last < CHUNK_HEAL_WINDOW_MS) return Promise.resolve();',
        to: '            void last;',
        targets: ['c:U6'] },
];

const runNode = (args, env = {}) => {
    let out = '';
    try { out = execFileSync(process.execPath, args, { cwd: ROOT, encoding: 'utf8', timeout: 20 * 60 * 1000, env: { ...process.env, ...env } }); }
    catch (e) { out = String(e.stdout || '') + String(e.stderr || ''); }
    return out;
};
const cellsOf = (prefix, out) => {
    const cells = {};
    /* 格行缩进两格;顶格的汇总行不是格;装饰符不许跨行 */
    for (const m of out.matchAll(/^[ \t]+(OK|FAIL)[ \t]+[^A-Za-z0-9\n]*([A-Za-z0-9][^\s]*)/gm)) cells[`${prefix}:${m[2]}`] = m[1];
    return cells;
};
const run = () => {
    const c = cellsOf('c', runNode([join(ROOT, 'scripts', 'test-chunk-heal.mjs')]));
    const b = runNode([VITE, 'build', '--outDir', DIST, '--emptyOutDir', '--logLevel', 'error']);
    if (/error/i.test(b) && !/built in/i.test(b)) console.log(`  (vite build 输出:${b.trim().slice(0, 200)})`);
    const r = cellsOf('r', runNode([join(ROOT, 'scripts', 'test-chunk-heal-render.mjs')], { A276_DIST: DIST }));
    return { ...c, ...r };
};

console.log('=== 基线(两把尺子逐格读数;不看 rc)===');
const base = run();
const ids = Object.keys(base);
const red = ids.filter((k) => base[k] !== 'OK');
console.log(`  ${ids.length} 格(c ${ids.filter((i) => i.startsWith('c:')).length} · r ${ids.filter((i) => i.startsWith('r:')).length})· 红 ${red.length}${red.length ? `(${red.join(',')})` : ''}`);
const missingTargets = POISONS.flatMap((p) => p.targets).filter((t) => !(t in base));
if (!ids.length || red.length || missingTargets.length) {
    console.log(`🔴 基线不干净 / 点名格不在基线里(${missingTargets.join(',') || '—'})—— 注毒读数无意义`);
    process.exit(3);
}

let bad = 0;
for (const p of POISONS) {
    console.log(`\n=== ${p.id} ${p.why}(点名 ${p.targets.join(' / ')})===`);
    const path = F(p.file);
    const original = readFileSync(path, 'utf8');
    const pairs = [[p.from, p.to], ...(p.also || [])];
    const miss = pairs.filter(([a]) => original.split(a).length - 1 !== 1);
    if (miss.length) { console.log(`  🔴 毒没下成:${p.file} 锚命中不是恰好 1 次(${miss.map(([a]) => a.trim().slice(0, 50)).join(' | ')})—— 不是"锁没牙"`); bad += 1; continue; }
    try { assertRulerWorks(path); } catch (err) { console.log(String(err.message || err)); process.exit(3); }
    let after = {};
    let landed = true;
    try {
        writeFileSync(path, pairs.reduce((s, [a, b]) => s.replace(a, b), original), 'utf8');
        if (!syntaxOk(path)) { console.log('  🔴 毒没下成:写成了语法错'); landed = false; }
        else after = run();
    } finally {
        writeFileSync(path, original, 'utf8');
    }
    const back = readFileSync(path, 'utf8') === original;
    if (!landed || !back) { bad += 1; if (!back) console.log('  🔴 源文件没回位'); continue; }
    const flipped = p.targets.filter((k) => base[k] === 'OK' && after[k] === 'FAIL');
    const missed = p.targets.filter((k) => !flipped.includes(k));
    const drift = ids.filter((k) => !p.targets.includes(k) && after[k] !== base[k]);
    const good = !missed.length && !drift.length;
    if (!good) bad += 1;
    console.log(`  点名格:${p.targets.map((k) => `${k} ${base[k]}→${after[k] || '消失'}`).join(' · ')}`);
    console.log(`  其余格漂移:${drift.map((k) => `${k} ${base[k]}→${after[k] || '消失'}`).join(',') || '无'} · 源文件逐字回位`);
    console.log(`  ${good ? '✅ 有牙' : `🔴 不过${missed.length ? `(没红:${missed.join(',')})` : ''}${drift.length ? '(别的格跟着动了)' : ''}`}`);
}
console.log(bad ? `\n🔴 ${bad} 发不过` : `\n全部通过:${POISONS.length} 发,每发只红点名的格`);
process.exit(bad ? 1 : 0);
