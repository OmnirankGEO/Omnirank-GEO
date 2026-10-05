#!/usr/bin/env node
/**
 * WO_273 反臂 —— 没毒过的锁不能当证据。
 *
 * 读三把尺子,格名带前缀防撞名:
 *   s: = test-self-publish-retired.mjs(结构臂,build 链)
 *   r: = test-self-publish-retired-render.mjs(行为臂,真 chromium)
 *   t: = verify-image-note-topics.mjs(gate t,A0a 真跑 test-image-note-flow 里 publicationLayout 的纯函数契约)
 *
 * 读法(不看 rc):每一发都要「**点名的那几格 OK→FAIL ∧ 其余格一个都不许动 ∧ 源文件逐字回位**」。
 *   · 下毒前先过语法尺子对照臂(原文必须判过),下毒后再判 —— 写成语法错 = 「毒没下成」,不算咬住;
 *   · 一发把整页打崩的毒也会让点名格变红 —— 「其余不动」挡的就是这种假咬。
 *
 * 九发(工单点名的两发 = Q1 / Q2):
 *   Q1 顶栏把「自助发布」按钮加回(只加文案,不带模式键)            ⇒ s:S5 · r:T1   (文案锁)
 *   Q2 URL 白名单把 'self' 加回                                    ⇒ s:S1 · r:U1   (键数锁 + 老链白屏)
 *   Q3 发布中心挂载时真发一个 /api/extension 请求(字面量)          ⇒ s:S2 · r:N1
 *   Q3b 同上但用拼接构造 '/api/' + 'extension/…'(结构臂自曝的盲区) ⇒ 只 r:N1(证明网络锁兜住结构锁的盲区)
 *   Q4 发布记录把「重新核实」按钮加回(按后端 can_reverify 显示)    ⇒ r:R1
 *   Q5 个人设置把「插件授权」格加回                                  ⇒ r:A1
 *   Q6 订单管理把「自发记录」子分页加回                              ⇒ r:A3
 *   Q7 publicationLayout 把 `|| mode === 'self'` 加回               ⇒ t:A0a
 *   Q8 用户详情把「插件✓」徽章加回(读 extension_authorized)        ⇒ s:S3 · r:A2
 *
 * 三态退出码:0 全部有牙 / 1 有一发不过 / 3 基线不干净或尺子自己坏了。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const F = (rel) => join(ROOT, 'src', ...rel.split('/'));
const ARMS = {
    s: join(ROOT, 'scripts', 'test-self-publish-retired.mjs'),
    r: join(ROOT, 'scripts', 'test-self-publish-retired-render.mjs'),
    t: join(ROOT, 'scripts', 'verify-image-note-topics.mjs'),
};

const MOUNT_ANCHOR = '  const flowLayout = publicationLayout(mode, proxyType, mobileStep);';
const POISONS = [
    { id: 'Q1', why: '顶栏把「自助发布」按钮加回(只加文案)', file: 'pages/Publishing/PublishCenter.tsx',
        from: "          <button onClick={() => setMode('history')}",
        to: "          <button type=\"button\">自助发布</button>\n          <button onClick={() => setMode('history')}",
        targets: ['s:S5', 'r:T1'] },
    { id: 'Q2', why: "URL 白名单把 'self' 加回", file: 'pages/Publishing/PublishCenter.tsx',
        from: "    initialModeParam === 'manual' || initialModeParam === 'history' ||",
        to: "    initialModeParam === 'manual' || initialModeParam === 'self' || initialModeParam === 'history' ||",
        targets: ['s:S1', 'r:U1'] },
    { id: 'Q3', why: '挂载时真发 /api/extension 请求(字面量)', file: 'pages/Publishing/PublishCenter.tsx',
        from: MOUNT_ANCHOR,
        to: `${MOUNT_ANCHOR}\n  useEffect(() => { authFetch('/api/extension/status').catch(() => {}); }, []);`,
        targets: ['s:S2', 'r:N1'] },
    { id: 'Q3b', why: '同上但拼接构造(结构臂自曝的盲区)', file: 'pages/Publishing/PublishCenter.tsx',
        from: MOUNT_ANCHOR,
        to: `${MOUNT_ANCHOR}\n  useEffect(() => { authFetch('/api/' + 'extension/status').catch(() => {}); }, []);`,
        targets: ['r:N1'] },
    { id: 'Q4', why: '发布记录把「重新核实」按钮加回', file: 'pages/Publishing/PublishHistory.tsx',
        from: '              <RotateCcw className="mr-1 size-3" />重新发布\n            </Button>\n          )}',
        to: '              <RotateCcw className="mr-1 size-3" />重新发布\n            </Button>\n          )}\n'
            + '          {(record as any).can_reverify && (<Button variant="ghost" size="sm" className="h-7 px-2 text-xs">重新核实</Button>)}',
        targets: ['r:R1'] },
    { id: 'Q5', why: '个人设置把「插件授权」格加回', file: 'pages/Account/ProfilePage.tsx',
        from: '              <div className="text-xs text-muted-foreground">直接推荐</div>\n            </div>',
        to: '              <div className="text-xs text-muted-foreground">直接推荐</div>\n            </div>\n'
            + '            <div><div className="text-xs text-muted-foreground">插件授权</div></div>',
        targets: ['r:A1'] },
    { id: 'Q6', why: '订单管理把「自发记录」子分页加回', file: 'pages/OrderManagement/OrderManagementPage.tsx',
        from: '                    { key: "review" as const, label: "人工审核" },',
        to: '                    { key: "review" as const, label: "人工审核" },\n                    { key: "self" as any, label: "自发记录" },',
        targets: ['r:A3'] },
    { id: 'Q7', why: "publicationLayout 把 || mode === 'self' 加回", file: 'pages/Writing/imageNoteFlow.ts',
        from: "        articlePane: mode === 'proxy' && !standalone,",
        to: "        articlePane: (mode === 'proxy' && !standalone) || mode === 'self',",
        targets: ['t:A0a'] },
    { id: 'Q8', why: '用户详情把「插件✓」徽章加回', file: 'pages/Admin/AdminUserDetail.tsx',
        from: '                {user.is_admin ? <Badge className="text-xs">管理员</Badge> : null}',
        to: '                {user.is_admin ? <Badge className="text-xs">管理员</Badge> : null}\n'
            + '                {user.extension_authorized && <Badge variant="outline" className="text-xs text-green-600">插件✓</Badge>}',
        targets: ['s:S3', 'r:A2'] },
];

const runArm = (path) => {
    let out = '';
    try { out = execFileSync(process.execPath, [path], { cwd: ROOT, encoding: 'utf8', timeout: 15 * 60 * 1000 }); }
    catch (e) { out = String(e.stdout || '') + String(e.stderr || ''); }
    return out;
};
const run = () => {
    const cells = {};
    for (const [k, path] of Object.entries(ARMS)) {
        const out = runArm(path);
        /* 格行缩进两格;顶格的汇总行不是格;装饰符不许跨行(WO_258 修过的读法坑) */
        for (const m of out.matchAll(/^[ \t]+(OK|FAIL)[ \t]+[^A-Za-z0-9\n]*([A-Za-z0-9][^\s]*)/gm)) cells[`${k}:${m[2]}`] = m[1];
    }
    return cells;
};

console.log('=== 基线(三把尺子逐格读数;不看 rc)===');
const base = run();
const ids = Object.keys(base);
const red = ids.filter((k) => base[k] !== 'OK');
const per = Object.keys(ARMS).map((k) => `${k} ${ids.filter((i) => i.startsWith(`${k}:`)).length} 格`).join(' · ');
console.log(`  ${ids.length} 格(${per})· 红 ${red.length}${red.length ? `(${red.join(',')})` : ''}`);
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
    const hits = original.split(p.from).length - 1;
    if (hits !== 1) { console.log(`  🔴 毒没下成:${p.file} 锚命中 ${hits} 次(要恰好 1 次)—— 不是"锁没牙"`); bad += 1; continue; }
    try { assertRulerWorks(path); } catch (err) { console.log(String(err.message || err)); process.exit(3); }
    let after = {};
    let landed = true;
    try {
        writeFileSync(path, original.replace(p.from, p.to), 'utf8');
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
