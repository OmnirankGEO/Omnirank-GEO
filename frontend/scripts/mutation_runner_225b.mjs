#!/usr/bin/env node
/**
 * 注毒自证 · #225 a1 ②③ 报价页口径下拉与文案锁。
 *
 * 🔴 开跑前后各断言一次 HEAD 与 dirty 清单(222 起的规矩)。
 * 别与 build 并行(毒在工作树里)。
 */
import { execFileSync, execSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX , proveGuardHasTeeth } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const abs = (rel) => join(ROOT, rel);
const sha = (p) => createHash('sha256').update(readFileSync(p)).digest('hex');
const say = (m) => console.log(m);
let failures = 0;

function treeState() {
    const head = execSync('git rev-parse HEAD', { cwd: REPO, encoding: 'utf8' }).trim();
    const dirty = execSync('git status --porcelain', { cwd: REPO, encoding: 'utf8' })
        .split('\n').map((s) => s.trim()).filter(Boolean).sort().join('|');
    return { head, dirty };
}
const BEFORE = treeState();
say(`开跑前 HEAD=${BEFORE.head.slice(0, 9)} · dirty ${BEFORE.dirty.split('|').filter(Boolean).length} 项`);

const GATE = 'scripts/verify-quote-perspective-copy.mjs';
function run() {
    try {
        const out = execFileSync(process.execPath, [GATE], { cwd: ROOT, encoding: 'utf8' });
        return { rc: 0, red: [...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]) };
    } catch (e) {
        const out = String(e.stdout || '') + String(e.stderr || '');
        return {
            rc: e.status === undefined ? -1 : e.status,
            red: [...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]),
        };
    }
}

const RAT = 'src/pages/Quote/utils/priceRationale.ts';
const FLOW = 'src/pages/Quote/OnlineQuoteFlow.tsx';

const POISONS = [
    {
        /* 🔴 前端自己按倍数算条数 = 与媒体组合那侧取整位置不同,两个数并排矛盾。 */
        id: 'U1', file: RAT,
        why: '条数改成前端自己算(槽 × 5),不读服务端给的合计',
        /* 🔴 锚不带前导缩进:内容本身已唯一,带上缩进等于把排版也钉进去,
           而我两次都把这个文件的 2 空格写成了 4 空格。 */
        from: 'const postsTotal = Number.isFinite(Number(mixOpts.postsEstimateTotal))',
        to: 'const postsTotal = articles * 5; const _u = Number.isFinite(Number(mixOpts.postsEstimateTotal))',
        expectRed: ['Q1d', 'Q2b'],
    },
    {
        id: 'U2', file: RAT,
        why: '认不出的口径也编一个名字出来(而不是不说)',
        from: "const perspectiveWord = PERSPECTIVE_WORD[String(mixOpts.deliveryPerspective || '')] || ''",
        to: "const perspectiveWord = PERSPECTIVE_WORD[String(mixOpts.deliveryPerspective || '')] || '自媒体'",
        expectRed: ['Q2c'],
    },
    {
        id: 'U3', file: RAT,
        why: '换算文案去掉「左右」—— 估算被说成确数',
        from: ' 条左右(运营口径)`',
        to: ' 条(运营口径)`',
        expectRed: ['Q3d-左右'],
    },
    {
        id: 'U4', file: RAT,
        why: '换算文案去掉「运营口径」四字 —— 换算被读成效果承诺',
        from: '(运营口径)`',
        to: '`',
        expectRed: ['Q1e'],
    },
    {
        /* 🔴 这一发就是工单最在意的那件事:把换算讲成效果等价。 */
        id: 'U5', file: RAT,
        why: '换算文案里给出倍数(读者会自己做「N 条 ≈ 1 篇」的等价换算)',
        from: '发布约需 ${postsTotal} 条左右',
        to: '发布约需 ${postsTotal} 条左右(一槽约 5 倍)',
        expectRed: ['Q3e-倍', 'Q3e2'],
    },
    {
        id: 'U6', file: RAT,
        why: '逐桶又标回「篇」(它们是槽的分布,不是要做几篇)',
        from: '`重点媒体锚点 ${mix.focusMediaAnchor} 槽`',
        to: '`重点媒体锚点 ${mix.focusMediaAnchor} 篇`',
        expectRed: ['Q1c'],
    },
    {
        id: 'U7', file: RAT,
        why: '合计又写回「N 条」—— 和同屏别处的槽数同一个数两个量词',
        from: ' · 本次交付 ${articles} 槽${postsPhrase}。',
        to: ' · 本次交付 ${articles} 条${postsPhrase}。',
        expectRed: ['Q1b'],
    },
    {
        /* 🔴 「接线全对但只跑了一次」:下拉看起来好用,句子永远停在第一次的口径。 */
        id: 'U8', file: FLOW,
        why: 'perspective 从依赖数组里拿掉 —— 切了口径不重取',
        from: '  }, [session.quote_id, pricingData?.generated_at, perspective]);',
        to: '  }, [session.quote_id, pricingData?.generated_at]);',
        expectRed: ['Q4d'],
    },
    {
        id: 'U9', file: FLOW,
        why: '切口径不带参数重取(下拉沦为装饰)',
        from: "          + (perspective ? `?perspective=${encodeURIComponent(perspective)}` : ''));",
        to: '          );',
        expectRed: ['Q4c'],
    },
    {
        id: 'U10', file: FLOW,
        why: '三档卡那个槽数又标回「篇」',
        from: '{t.total_articles} 槽',
        to: '{t.total_articles}篇',
        expectRed: ['Q5a', 'Q5b'],
    },
    {
        /* 🔴 内部运营参数被取进前端 —— 取了就会有人显示它。 */
        id: 'U11', file: FLOW,
        why: '前端把 posts_per_slot_bps 也取进来',
        from: '          deliveryPerspective: data?.delivery_perspective,',
        to: '          deliveryPerspective: data?.delivery_perspective,\n'
            + '          bps: data?.posts_per_slot_bps,',
        expectRed: ['Q4e'],
    },
    {
        /*
         * 🔴 [Review 09-15 点名] **结构毒**:客户面引入换算字段。
         *    Owner ③「不要看到,这是运营的事」—— 客户页 `/s/:token` 走 `Selection/**`,
         *    那里出现 `posts_estimate` / `delivery_perspective` 任何一个都是泄漏。
         *    这一发证明 Q4g 那道结构锁**真的会红**,而不是"扫了个空目录"。
         */
        /* 🔴 ④ 截图逮到的那条:去掉整单指代词,换算句就又和同句的
              「本次交付 N 槽」(这个词的槽)读成一笔账。 */
        id: 'U13', file: RAT,
        why: '换算句去掉「本单」—— 整单合计被读成这个关键词的账',
        from: ' · 本单按${perspectiveWord}发布约需',
        to: ' · 按${perspectiveWord}发布约需',
        expectRed: ['Q3f'],
    },
    {
        id: 'U12', file: 'src/pages/Selection/SelectionPage.tsx',
        why: '客户面文件引入换算字段(运营口径泄漏到客户页)',
        from: 'export',
        to: 'const __leak = { posts_estimate: 0, delivery_perspective: \'self_media\' };\nexport',
        expectRed: ['Q4g'],
    },
];

const TOUCHED = [...new Set(POISONS.map((p) => p.file))];
const SHA0 = Object.fromEntries(TOUCHED.map((f) => [f, sha(abs(f))]));
const SRC0 = Object.fromEntries(TOUCHED.map((f) => [f, readFileSync(abs(f), 'utf8')]));
say('被碰文件开跑前 sha:');
for (const f of TOUCHED) say(`  ${f} ${SHA0[f].slice(0, 12)}`);

say('\n=== 0. 基线(失败集必须为空) ===');
const base = run();
const clean = base.rc === 0 && base.red.length === 0;
if (!clean) failures += 1;
say(`  ${clean ? 'OK  ' : 'FAIL'} rc=${base.rc} 失败集=${base.red.join(',') || '空'}`);
if (!clean) { say('\n🔴 基线不干净 —— 红基线会让每发毒都像命中,不往下跑。'); process.exit(1); }
const baseSet = new Set(base.red);

/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红。
 *    少了它,`syntaxOk()` 平时永远返回 true —— 一把恒 true 的尺子
 *    与「每一发毒都下成了」读数完全同形。
 *    (对照臂 `assertRulerWorks` 管反方向:恒 false。两条臂缺一不可。)
 */
proveGuardHasTeeth(abs(POISONS[0].file), say);

for (const p of POISONS) {
    say(`\n=== ${p.id} ${p.why} ===`);
    const n = SRC0[p.file].split(p.from).length - 1;
    if (n !== 1) {
        say(`  FAIL 毒没下成:${p.file} 的锚命中 ${n} 次(要恰好 1 次)—— 不是"锁没牙"`);
        failures += 1;
        continue;
    }
    try { assertRulerWorks(abs(p.file)); } catch (err) { say(`  ${err.message}`); process.exit(3); }
    writeFileSync(abs(p.file), SRC0[p.file].replace(p.from, p.to), 'utf8');
    if (!syntaxOk(abs(p.file))) {
        say(`  FAIL ${NOT_LANDED_SYNTAX}`);
        failures += 1;
        writeFileSync(abs(p.file), SRC0[p.file], 'utf8');
        continue;
    }
    say('  毒已落地(1 处改动)');
    const r = run();
    const missing = p.expectRed.filter((w) => !r.red.includes(w));
    const fresh = r.red.filter((x) => !baseSet.has(x));
    const good = r.rc !== 0 && missing.length === 0;
    if (!good) failures += 1;
    say(`  ${good ? 'OK  ' : 'FAIL'} rc=${r.rc} 期望红=[${p.expectRed.join(',')}]`
        + `${missing.length ? ` 🔴 没红=[${missing.join(',')}]` : ' 全中'}`);
    say(`       新增报红 ${fresh.length} 条:${fresh.join(',') || '(无)'}`);
    writeFileSync(abs(p.file), SRC0[p.file], 'utf8');
    const back = sha(abs(p.file)) === SHA0[p.file];
    if (!back) failures += 1;
    say(`  ${back ? 'OK  ' : 'FAIL'} 还原:逐文件 sha 与下毒前一致`);
}

say('\n=== 收尾 ===');
for (const f of TOUCHED) {
    const now = sha(abs(f));
    const good = now === SHA0[f];
    if (!good) failures += 1;
    say(`  ${good ? 'OK  ' : 'FAIL'} ${f} ${now.slice(0, 12)}`);
}
const AFTER = treeState();
const headSame = AFTER.head === BEFORE.head;
const dirtySame = AFTER.dirty === BEFORE.dirty;
if (!headSame) failures += 1;
if (!dirtySame) failures += 1;
say(`  ${headSame ? 'OK  ' : 'FAIL'} HEAD 与开跑前一致 ${AFTER.head.slice(0, 9)}`);
say(`  ${dirtySame ? 'OK  ' : 'FAIL'} dirty 清单与开跑前一致`);

say('');
if (failures > 0) { say(`FAIL 注毒自证 ${failures} 项不过`); process.exit(1); }
say(`全部通过:${POISONS.length} 发毒,每发都红在写死的那一格,树状态逐字回位`);
process.exit(0);
