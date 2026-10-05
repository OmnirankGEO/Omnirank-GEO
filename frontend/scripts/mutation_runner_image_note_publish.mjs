#!/usr/bin/env node
/*
 * 🔴 [#204 a2] 本文件**退役了 5 发毒**,靶随「制作 GEO 图文」一起删了。逐条理由:
 *   · S3:加价说明:按数量制作随区 D 退役(WO_204 §2 选题列表替代),"前 4 张不加价"这件事在新屏上不存在
 *   · S4:「你上次做的」封面卡是制作台的部件,随屏退役
 *   · S6:主按钮全宽:老判据钉的是**类名**,而命题(不许拉成跨屏长条)已迁到 test-image-note-detail-render 的 T13 —— 那里量的是**渲染宽度**(实测 352px)
 *   · S8:裸下拉:命题升成全仓级 Z4,并由 mutation_runner_204 的 S13(选择器不出样图)配正对照
 *   · S11:步进器的 aria-label:步进器随区 D 退役
 *
 * 退役 = **整条删掉**,不留 skip 条目 —— 本文件自己的纪律就是
 * 「不要加 SKIP:那会把『没跑』伪装成『通过』」。
 * 另有 V3 / V8 两发**换靶不换意图**,跟着谓词搬到了发布中心那份实现上。
 */
/**
 * 注毒自证 · #186 图文发布面板 —— 证明三套判据**真的有牙**。
 *
 * 纪律(每一条都是踩过的坑,#178 当天刚被咬全):
 * 1. 先证**基线失败集为空**;基线本来就红时,每发毒都会显示"命中"。
 * 2. 比**失败集的差**,不比 rc;并打印是哪一条抓住的(钝杀与真抓在 rc 上同形)。
 * 3. 毒必自证下成了:下毒前后 sha256 必须不同;锚必须**恰好命中一次**
 *    (命中 0 或 ≥2 一律报「毒没下成」,而不是「锁没牙」—— 这两件事必须分得开)。
 * 4. 还原用**字节拷回**,不用 `git checkout --`(后者回的是 HEAD,不是"下毒前那份");
 *    判据是 **sha 回到下毒前** + **原文恰好回位一处**,`git diff` 只作旁证
 *    (它比的是 HEAD:对已改未提交的文件恒报有差异、对新建 untracked 的恒报干净)。
 * 5. 每发毒写死 `expectRed`:红必须落在**它那一格**。
 *
 * 跑法:cd frontend && npm run build && node scripts/mutation_runner_image_note_publish.mjs
 *      加 --no-browser 跳过浏览器臂(快;但那几条的牙就没证到,交付物要写明)
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const NO_BROWSER = process.argv.includes('--no-browser');

const GATES = {
    pub: 'scripts/verify-image-note-publish.mjs',
    browser: 'scripts/test-image-note-publish-render.mjs',
};

const abs = (rel) => join(ROOT, rel);
const sha = (p) => createHash('sha256').update(readFileSync(p)).digest('hex');
const say = (m) => console.log(m);
let failures = 0;

function run(gateRel) {
    let out = '';
    let rc = 0;
    try {
        out = execFileSync(process.execPath, [abs(gateRel)], {
            cwd: ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, timeout: 20 * 60 * 1000,
        });
    } catch (e) {
        rc = typeof e.status === 'number' ? e.status : 1;
        out = String(e.stdout || '') + String(e.stderr || '');
    }
    const redSet = new Set([...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]));
    return { rc, redSet, out };
}

/*
 * 🔴 [#188] 批量面板已删,发布搬到制作台作品卡上的**行内**组件。
 *    毒的靶子跟着搬 —— 靶子搬错的话每一发都会报「毒没下成」(锚命中 0 次),
 *    那和「锁没牙」在 rc 上同形,必须分得开。
 */
const PANEL = 'src/pages/Writing/ImageNotePublishInline.tsx';
const MOD = 'src/pages/Publishing/imageNotePublishScope.ts';

const POISONS = [
    {
        id: 'V1', file: MOD,
        why: '往提交体里塞 title(= 开一条绕过冻结版本改内容的路:预览看到 A,发出去 B,两边都不报错)',
        from: `            expected_price_fingerprint: fp,
        });`,
        to: `            expected_price_fingerprint: fp,
            title: 'poisoned',
        } as BatchItem);`,
        expectRed: { pub: ['N1b', 'N1d'], browser: ['P4d', 'P4e'] },
    },
    {
        id: 'V2', file: MOD,
        why: '把那个零读取的第八键发回去(= 让下一个人以为它是承重的)',
        from: `            media_id: c.mediaId,
            expected_price_fingerprint: fp,`,
        to: `            media_id: c.mediaId,
            expected_price_points: 0,
            expected_price_fingerprint: fp,`,
        expectRed: { pub: ['N1b', 'N1e'], browser: ['P4d'] },
    },
    {
        /*
         * 🔴 [#204 a2 换靶] 原来打制作台那份 `canPublishCard`。那个模块随屏退役已删。
         *    「没有版本就发不出去」这个谓词**第三次搬家**:
         *      toSelectableRows(批量面板)→ canPublishCard(制作台)→ 现在 canPublishRow(发布中心)。
         *    每一次都是宿主死了而判据留在原地继续绿 —— 那正是「毒够不着」的形状。
         *    这一发跟着搬到活的那份上。
         */
        id: 'V3', file: 'src/pages/Publishing/imageNotePublishStatus.ts',
        why: '没有版本也当成能发(= 拿 0 去提交,后端整批拒,而用户以为是自己点错)',
        from: '    if (!row || row.revisionId === null) return false;',
        to: '    if (!row) return false;',
        expectRed: { browser: ['P1d'] },
    },
    {
        id: 'V4', file: MOD,
        why: '账号不再滤 can_tuwen(= 下拉里全是选了必被拒的死选项)',
        from: `        if (int(r?.can_tuwen) !== 1) continue;          // 不能发图文 —— 永远不会变可用`,
        to: `        // poisoned`,
        expectRed: { pub: ['N3a'], browser: ['P2b'] },
    },
    {
        id: 'V5', file: MOD,
        why: '算不出价时显示 0(= 告诉用户免费)',
        from: `    return typeof v === 'number' && Number.isFinite(v) ? v : null;`,
        to: `    return typeof v === 'number' && Number.isFinite(v) ? v : 0;`,
        expectRed: { pub: ['N4b'] },
    },
    {
        id: 'V6', file: MOD,
        why: '认不出的状态当成终态(= 轮询提前停,用户永远看不到最终结果)',
        from: `    return TERMINAL.has(s);`,
        to: `    return true;`,
        expectRed: { pub: ['N5c'] },
    },
    {
        id: 'V7', file: PANEL,
        why: '禁用理由挪回按钮里(= 跟着 disabled 的透明度一起变淡,#179 根因①同款)',
        from: `                {gate.disabled && gate.reason && (
                    <span className="text-sm text-foreground" data-testid="inp-disabled-reason">
                        {gate.reason}
                    </span>
                )}`,
        to: `                {/* poisoned: 理由塞回按钮里 */}`,
        expectRed: { pub: ['N7c'], browser: ['P7b'] },
    },
    {
        /*
         * 🔴 [#204 a2 换靶] 原来打制作台区 E 的区域级说明。那块屏已删。
         *    命题没死,而且更要紧了:发布收口到发布中心之后,用户是在**那一屏**
         *    看这排内容的 —— 一排没有「发布」的行 + 零解释,和"坏了"长得一模一样。
         */
        id: 'V8', file: 'src/pages/Publishing/imageNotePublishStatus.ts',
        why: '撤掉「发不了」那句解释 —— 屏幕上只剩一排没有发布键的行,零线索',
        from: "export const BLOCKED_ALL_NOTE = '旧作品待系统回填,暂不能发';",
        to: "export const BLOCKED_ALL_NOTE = '';",
        expectRed: { browser: ['P1e'] },
    },
    {
        id: 'V9', file: PANEL,
        why: '发布请求不带 brand_id(= 成品落到别的服务商的客户名下 —— 生产实证 3 条里 2 条)',
        /*
         * 🔴 [#188] 老靶子是制作台那条跨页链里的 `&brand_id=${'$'}{brandId}`。
         *    发布改成行内之后**没有那条链了**,风险搬进了提交体:`previewPublish({ brand_id })`。
         *    毒跟着搬 —— 靶子不搬的话每发都报「毒没下成」,而那和「锁没牙」在 rc 上同形。
         */
        from: `            brand_id: brandId,`,
        to: `            brand_id: 0,`,
        expectRed: { pub: ['N6a2'] },
    },
    {
        id: 'V10', file: PANEL,
        why: '错误只显示 message 不显示码(= 只剩一句笼统的话,定位信息没了)',
        /*
         * 🔴 第一版的毒只把 testid 改了个名 —— 码**照样渲染在屏幕上**,
         *    所以查文本的 P6b 当然不红。那是"毒没打中目标",不是"锁没牙"。
         *    现在把码真的从渲染里拿掉。
         */
        from: `                        {submitErr.contract.code}`,
        to: `                        {''}`,
        expectRed: { pub: ['N5g'], browser: ['P6b'] },
    },
    /* ══ #188 设计 §6 的 S 系毒 ══════════════════════════════════════
     * 🔴 每一发都写死红在哪一格。靶子分布在三个文件:
     *   CardStylePicker(样图)/ ImageNoteStudio(剪影、卡带、资料、主按钮)/
     *   imageNoteStudioApi(加价起点)。
     */
    {
        id: 'S1', file: 'src/components/writing/CardStylePicker.tsx',
        why: 'S1 · 样图不渲染(= 退回"四行文字",正是 Owner 点名废掉的那一格)',
        from: `                        {STYLE_SAMPLES[s.key] && (`,
        to: `                        {false && STYLE_SAMPLES[s.key] && (`,
        expectRed: { studioBrowser: ['S1a'] },
    },
    {
        id: 'S5', file: 'src/components/client/ClientKnowledgeCard.tsx',
        why: 'S5 · 撤掉缺项清单(= 只剩「5/8」,说了有多少、没说差什么)',
        from: `                        {showMissing && (() => {`,
        to: `                        {false && showMissing && (() => {`,
        expectRed: { studioBrowser: ['S5b'] },
    },
    {
        id: 'W1', file: PANEL,
        why: '#192 · 账号请求不带 can_tuwen=1(= 回到现场那一格:在两万条供应商目录的'
            + '第一页里找能发图文的号,永远找不到,面板永远说"还没有能发图文的账号")',
        from: "                can_tuwen: '1', limit: '50', page: String(page),",
        to: "                limit: '50', page: String(page),",
        expectRed: { pub: ['N3e'], browser: ['P8a'] },
    },
    {
        id: 'W2', file: PANEL,
        why: '#192 · 不渲染错误合同里的 actions(= 403 时用户只看到"你没权限",'
            + '拿不到后端已经给出的两条出路)',
        from: '                    {hasActions(submitErr.contract.actions) && (',
        to: '                    {false && hasActions(submitErr.contract.actions) && (',
        expectRed: { browser: ['P8f'] },
    },
    {
        id: 'W3', file: 'src/pages/Writing/publishErrorActions.ts',
        why: '#192 · 认不出的 action 也渲染成按钮(= 又造一颗点了没反应的死按钮 ——'
            + '本程序一路在修的就是这个)',
        from: "        out.push({ kind: 'text', label, id });",
        to: "        out.push({ kind: 'nav', label, to: '/publish', id });",
        expectRed: { browser: ['P8h'] },
    },
];

// ══ 0. 基线 ═══════════════════════════════════════════════════════════
say('=== 0. 基线(三套判据的失败集都必须为空) ===');
const baseline = {};
for (const [name, gate] of Object.entries(GATES)) {
    if (name === 'browser' && NO_BROWSER) { say('  browser: --no-browser,跳过(牙未证,交付物须写明)'); continue; }
    const r = run(gate);
    baseline[name] = r.redSet;
    const good = r.rc === 0 && r.redSet.size === 0;
    say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 失败集=${r.redSet.size ? [...r.redSet].join(',') : '空'}`);
    if (!good) { say('  🔴 基线不干净,注毒读数无意义。先修基线。'); process.exit(1); }
}

// ══ 1. 逐发注毒 ═══════════════════════════════════════════════════════
/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红(恒 true 的尺子与
 *    「每一发毒都下成了」读数完全同形)。它抛异常、自己还原。
 */
proveGuardHasTeeth(abs(POISONS[0].file), say);

for (const p of POISONS) {
    say(`\n=== ${p.id} ${p.why} ===`);
    const target = abs(p.file);
    const bak = `${target}.a179bak-${p.id}`;
    const before = sha(target);
    copyFileSync(target, bak);
    const src = readFileSync(target, 'utf8');
    const hits = src.split(p.from).length - 1;
    if (hits !== 1) {
        say(`  FAIL 毒没下成:锚命中 ${hits} 次(要恰好 1 次)—— 锚不唯一/已漂移,不是"锁没牙"`);
        unlinkSync(bak); failures += 1; continue;
    }
    try { assertRulerWorks(target); } catch (e) { say(`  ${e.message}`); copyFileSync(bak, target); unlinkSync(bak); process.exit(3); }
    writeFileSync(target, src.replace(p.from, p.to), 'utf8');
    if (!syntaxOk(target)) {
        say(`  FAIL ${NOT_LANDED_SYNTAX}`);
        copyFileSync(bak, target); unlinkSync(bak); failures += 1; continue;
    }
    const after = sha(target);
    if (after === before) {
        say('  FAIL 毒没下成:sha256 没变');
        copyFileSync(bak, target); unlinkSync(bak); failures += 1; continue;
    }
    say(`  毒已落地 sha ${before.slice(0, 12)} → ${after.slice(0, 12)}`);

    let caught = true;
    for (const [name, gate] of Object.entries(GATES)) {
        const want = p.expectRed[name] || [];
        if (!want.length) continue;
        if (name === 'browser' && NO_BROWSER) { say(`  browser: 跳过(--no-browser)`); continue; }
        const r = run(gate);
        const newRed = [...r.redSet].filter((x) => !baseline[name].has(x));
        const missed = want.filter((w) => ![...r.redSet].some((x) => x === w || x.startsWith(w)));
        const good = missed.length === 0 && r.rc !== 0;
        if (!good) caught = false;
        say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 期望红=[${want.join(',')}]`
            + `${missed.length ? ` 🔴 没红=[${missed.join(',')}]` : ' 全中'}`);
        say(`       新增报红 ${newRed.length} 条:${newRed.slice(0, 8).join(',') || '(无)'}`);
        const okCount = (r.out.match(/^\s*OK/gm) || []).length;
        if (okCount < 5) {
            say(`       🔴 钝杀嫌疑:这一轮只打印了 ${okCount} 条 OK —— 判据可能是崩了不是抓住了`);
            caught = false;
        }
    }
    if (!caught) failures += 1;

    // 还原
    copyFileSync(bak, target);
    unlinkSync(bak);
    const restored = sha(target);
    const backHits = readFileSync(target, 'utf8').split(p.from).length - 1;
    let gitState = 'n/a';
    try {
        execFileSync('git', ['diff', '--quiet', '--', join('frontend', p.file).split('\\').join('/')],
            { cwd: REPO, stdio: 'pipe' });
        gitState = '与 HEAD 无差异';
    } catch { gitState = '与 HEAD 有差异(本单未提交的改动,预期如此)'; }
    const good = restored === before && backHits === 1;
    say(`  ${good ? 'OK  ' : 'FAIL'} 还原:sha vs 下毒前 ${restored === before ? '一致' : '🔴 不一致'}`
        + ` · 原文回位 ${backHits === 1 ? '是' : `🔴 否(${backHits} 处)`}`
        + ` · (旁证 git:${gitState})`);
    if (!good) failures += 1;
}

say('');
if (failures > 0) { say(`FAIL 注毒自证 ${failures} 项不过`); process.exit(1); }
say(`全部通过:${POISONS.length} 发毒,每发都红在写死的那一格,且逐发字节还原`
    + (NO_BROWSER ? '(--no-browser:浏览器臂的牙本轮未证)' : ''));
process.exit(0);
