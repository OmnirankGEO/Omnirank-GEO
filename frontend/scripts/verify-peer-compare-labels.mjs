#!/usr/bin/env node
/**
 * 判据 · #189 写作大厅「同行对比」三档改表达。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * Owner 2026-09-13 看着截图问:「这三个标签不是找同行的吗?现在的表达是不是需要换一下?」
 * 改前三枚 chip 是「已核验 / 待核验 / 仅写标准」——写的是**证据状态**,没有主语。
 *
 * 🔴 文案类判据最容易写成**存在锁**(「这句话在源码里」)。那种锁挡不住
 *    `{false && ...}`、挡不住"写了但没接到按钮上"。所以这里:
 *    · 文案由**纯函数**产出 ⇒ 判据真调它拿字符串比,不是 grep 源码;
 *    · 接线单独钉(按钮渲染的是那个函数的产物);
 *    · 颜色/禁用这类只有渲染后才成立的,交给浏览器臂(test-peer-compare-render.mjs)。
 */
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');

let bad = 0;
const ok = (cond, label, detail) => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

const blank = (m) => m.replace(/[^\n]/g, ' ');
const decomment = (s) => s
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, blank)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/^\s*\/\/.*$/gm, blank);

let M;
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(rd('src/pages/Writing/peerCompareLabels.ts'), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const tmp = mkdtempSync(join(tmpdir(), 'a189-'));
    const f = join(tmp, 'peer.mjs');
    writeFileSync(f, js, 'utf8');
    M = await import(pathToFileURL(f).href);
} catch (e) {
    console.log('  FAIL T0 🔴 文案模块加载不了:' + String((e && e.message) || e));
    console.log('       🔴 不要加 SKIP —— 那会把「没跑」伪装成「通过」。');
    process.exit(1);
}

const HALL = decomment(rd('src/pages/Writing/WritingHall.tsx'));
console.log('T #189 同行对比三档');

// ══ T1 三档文案 = 定稿措辞,且前缀「同行对比」在 ════════════════════
{
    const opts = M.peerModeOptions({ verified: 3, pending: 5 });
    ok(opts.length === 3, 'T1a 分母自证:三档都在', String(opts.length));
    const byValue = Object.fromEntries(opts.map((o) => [o.value, o.label]));
    ok(byValue.real === '点名对比 · 只写已核实的 3 家',
        'T1b 🔴 real 档 = 定稿措辞(N 为实时数)', byValue.real);
    ok(byValue.semi === '暂不点名 · 5 家还在核实',
        'T1c 🔴 semi 档 = 定稿措辞', byValue.semi);
    ok(byValue.evidence_only === '不点名 · 只写怎么选',
        'T1d 🔴 evidence_only 档 = 定稿措辞', byValue.evidence_only);
    ok(M.PEER_GROUP_LABEL === '同行对比',
        'T1e 🔴 前置标签「同行对比」—— 没有它,三档就是三个没有主语的形容词'
        + '(Owner 问的正是这件事)', M.PEER_GROUP_LABEL);
    /* 接线:前缀与三档**真的渲染出来**,不只是模块里有 */
    ok(/PEER_GROUP_LABEL/.test(HALL) && /peer-group-label/.test(HALL),
        'T1f 接线:前置标签真的画在页面上');
    ok(/peerModeOptions\(peerCountsNow\)/.test(HALL) && /\{opt\.label\}/.test(HALL),
        'T1g 🔴 接线:按钮渲染的是**那个函数的产物** —— 页面里另写一份文案的话,'
        + '模块改了屏幕不跟着改,而判据只看模块会全绿');
    /* 反臂:老文案不许还在 */
    for (const oldLabel of ['已核验', '待核验', '仅写标准']) {
        ok(!new RegExp(`>\\s*${oldLabel}\\s*<`).test(HALL),
            `T1h[${oldLabel}] 反臂:老 chip 文案「${oldLabel}」不再作为按钮文字渲染`);
    }
}

// ══ T2 evidence_only 不带告警色 ═══════════════════════════════════
{
    /*
     * 🔴 这一条源码层只能钉"三档的类名里没有 red/yellow";
     *    「选中时是什么颜色」要浏览器臂量 computed style。两层都要。
     */
    const groupStart = HALL.indexOf('data-testid="peer-mode-group"');
    const groupEnd = HALL.indexOf('peer-research-btn', groupStart);
    const win = groupStart >= 0 && groupEnd > groupStart ? HALL.slice(groupStart, groupEnd) : '';
    ok(win.length > 200, 'T2a 分母自证:定位到三档那一块', `${win.length} 字符`);
    ok(!/bg-red-|text-red-|bg-yellow-|text-yellow-|bg-green-500/.test(win),
        'T2 🔴 三档**无红黄告警色**(「不点名 · 只写怎么选」是正当选择,'
        + '原来配红点红字读起来像出错 —— 用告警色劝退一个合法选项等于误导)');
    const noteStart = HALL.indexOf('data-testid="peer-evidence-only-note"');
    const noteWin = noteStart >= 0 ? HALL.slice(noteStart - 400, noteStart + 900) : '';
    ok(noteWin.length > 200 && !/bg-red-|text-red-|border-red-/.test(noteWin),
        'T2b 🔴 evidence_only 的提示行也去掉了红色(它描述的是后果,不是错误)');
    ok(M.EVIDENCE_ONLY_NOTE === '文章不点名同行,只写怎么选;想点名就先联网核实',
        'T2c 提示行 = 定稿措辞', M.EVIDENCE_ONLY_NOTE);
}

// ══ T3 0 家已核实时 real 不可选,且原因可见 ═════════════════════════
{
    const zero = M.peerModeOptions({ verified: 0, pending: 4 });
    const real0 = zero.find((o) => o.value === 'real');
    ok(real0 && real0.disabled === true,
        'T3 🔴 一家都没核实时「点名对比」不可选 —— 选了正文也点不了名,'
        + '后端那道证据硬门会打回来');
    ok(real0 && real0.disabledReason === '还没有核实过的同行,先联网核实',
        'T3b 🔴 不可用**同屏给原因**(只置灰不说话 = 用户反复点一个永远不动的按钮)',
        real0 ? real0.disabledReason : '(缺)');
    const some = M.peerModeOptions({ verified: 2, pending: 1 });
    ok(some.find((o) => o.value === 'real').disabled === false,
        'T3c 反臂:有已核实的同行时它**可选**(否则这把锁恒真)');
    ok(/peer-mode-disabled-reason/.test(HALL) && /disabledReason/.test(HALL),
        'T3d 接线:原因真的渲染在页面上');
}

// ══ T4 检索独立成按钮;切档不再触发检索 ═════════════════════════════
{
    ok(M.PEER_RESEARCH_LABEL === '联网找同行并核实',
        'T4a 独立按钮 = 定稿措辞', M.PEER_RESEARCH_LABEL);
    ok(/data-testid="peer-research-btn"/.test(HALL) && /researchAndVerifyPeers/.test(HALL),
        'T4b 🔴 检索**独立成按钮**(原来藏在「已核验」chip 里:一个按钮两种含义,'
        + '用户以为只是在切显示,结果发起了一次联网检索)');
    /*
     * 🔴 T4c 是本单最要紧的一条:**切档不许再触发检索**。
     *    钉法是"switchCompetitorMode 函数体里不出现那两个检索调用" ——
     *    钉"按钮 onClick 是 switchCompetitorMode"没用:检索是在函数**里面**发生的。
     */
    const swStart = HALL.indexOf('const switchCompetitorMode');
    const swEnd = HALL.indexOf('\n    const ', swStart + 10);
    const swBody = swStart >= 0 ? HALL.slice(swStart, swEnd > swStart ? swEnd : swStart + 2000) : '';
    ok(swBody.length > 100, 'T4c0 分母自证:定位到 switchCompetitorMode 函数体',
        `${swBody.length} 字符`);
    ok(!/researchCompetitors\(|verifyCurrentCompetitors\(/.test(swBody),
        'T4c 🔴 切档**不再触发联网检索** —— 钉的是函数体,不是 onClick 绑了谁'
        + '(检索是在函数里面发生的)');
    /* 反臂:那两个检索函数仍然存在且被独立按钮用着(别把功能删了当"修好") */
    ok(/const researchAndVerifyPeers/.test(HALL)
        && /researchCompetitors\(\)/.test(HALL) && /verifyCurrentCompetitors\(\)/.test(HALL),
        'T4d 反臂:检索能力**没被删掉**,只是搬到独立按钮后面');
}

// ══ T5 页面「竞品 / 竞争对手」计数 = 0 ═══════════════════════════════
{
    const raw = rd('src/pages/Writing/WritingHall.tsx');
    const hits = (raw.match(/竞品|竞争对手/g) || []).length;
    ok(hits === 0,
        'T5 🔴 页内「竞品 / 竞争对手」字样计数 = 0(Owner 口径是「同行」;'
        + '后端字段名 competitor_mode / research-competitors **不动**)', `${hits} 处`);
    ok(/competitor_mode|research-competitors|competitors\//.test(raw),
        'T5b 🔴 反臂:后端字段/路径名**仍然是英文原样** ——'
        + '把 URL 里的 competitors 也一起改掉的话,接口当场 404');
    ok(M.peerListTitle('real', 5) === '已核实的同行 5 家'
        && M.peerListTitle('semi', 3) === '还在核实的同行 3 家',
        'T5c 列表标题 = 定稿措辞',
        `${M.peerListTitle('real', 5)} / ${M.peerListTitle('semi', 3)}`);
    ok(M.ADD_PEER_PLACEHOLDER === '加一个同行,系统会联网核实',
        'T5d 手动添加占位 = 定稿措辞', M.ADD_PEER_PLACEHOLDER);
}

// ══ T6 两个实时数怎么算的(定稿里的 N) ═══════════════════════════════
{
    const c = M.peerCounts([
        { name: 'a', name_verified: true },
        { name: 'b', human_verified_name: true },
        { name: 'c' },
        { name: 'd', name_verified: true, excluded: true },
    ]);
    ok(c.verified === 2 && c.pending === 1,
        'T6 🔴 已核实 = 机器核过**或**人工确认过;已排除的两边都不算'
        + '(用户划掉了还报进数里,两个数就和眼前的列表对不上)', JSON.stringify(c));
    const empty = M.peerCounts(null);
    ok(empty.verified === 0 && empty.pending === 0, 'T6b 边界:垃圾输入不抛');
}

// ══ T7 前端的"算作已核实"与**后端同一个谓词** ═══════════════════════
{
    /*
     * 🔴 [Review 09-13:夹具别手写,从生产契约抽] 三档文案里的 N 是我算的,
     *    而"哪些算已核实"这件事后端也有一份判断。两边不一致的话,
     *    屏幕上写「只写已核实的 2 家」而后端实际只认 1 家 —— 数字对不上,
     *    且**两边各自都不会报错**。
     * 🔴 后端原文(services/writing_competitor_verification.py):
     *      `not item.get("excluded")` 且
     *      `(item.get("name_verified") is True or item.get("human_verified_name") is True)`
     *    这里每次跑都去读它,字段名漂了就红。
     * 🔴 本段读后端源码 ⇒ 这把闸**不能进 build 链**(链上禁引后端路径,
     *    构建镜像里只有 frontend/)。它进手跑清单与班车枚举门。
     */
    const rdRepo = (rel) => readFileSync(join(ROOT, '..', rel), 'utf8');
    let py = '';
    try { py = rdRepo('services/writing_competitor_verification.py'); }
    catch { py = ''; }
    ok(py.length > 500, 'T7a 分母自证:读到了后端那份核验逻辑'
        + '(读不到的话下面每条都恒真)', `${py.length} 字符`);
    for (const f of ['name_verified', 'human_verified_name', 'excluded']) {
        ok(py.includes(f), `T7b[${f}] 后端真的用这个字段名`);
    }
    ok(/name_verified.*is True.*or.*human_verified_name.*is True/s.test(py),
        'T7c 🔴 后端判"算已核实"用的是**两个位任一为真** —— 前端 peerCounts 同款;'
        + '只认 name_verified 的话,人工确认过的名字会被算成"还在核实"');
    ok(/not item\.get\("excluded"\)/.test(py),
        'T7d 后端也把已排除的剔除掉 —— 两边同一口径,屏幕上的 N 才对得上眼前的列表');
}

console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
console.log('全部通过:#189 同行对比三档');
process.exit(0);
