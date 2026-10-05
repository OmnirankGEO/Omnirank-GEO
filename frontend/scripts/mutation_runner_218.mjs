#!/usr/bin/env node
/**
 * WO_218-a1 注毒 —— 证明两臂**有牙**,以及**哪几面没牙**。
 *
 * 🔴 列毒的来源是**交付抬头承诺的每一样**,不是我判据覆盖的那些面 ——
 *    "自己的毒全红"只证明判据与毒自洽(本仓 an-authors-poisons-come-from-an-authors-criteria)。
 *    所以下面有两发是我在跑之前就**预判会存活**的,`expectSurvive` 写在表里,
 *    不是跑完之后补的说辞。
 *
 * 用法:node scripts/mutation_runner_218.mjs
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const HALL = join(ROOT, 'src/pages/Writing/WritingHall.tsx');
const MOD = join(ROOT, 'src/pages/Writing/topicGenCharge.ts');
const MOCK = join(ROOT, 'src/sandbox/mockData.ts');

const POISONS = [
    {
        id: 'N254-1', file: MOD, why: '🔴 原缺陷字面复现:拿掉「都有题就不是补救」那一支',
        from: "    if (rows.every(rowHasTitle)) return { face: 'has-title', generationRequestId: null };\n",
        to: '',
        expect: 'P5f',
        note: '这一发就是 380/2900 个词在生产上走的那条路:判成补救 → 后端 403 → '
            + 'toast 指向一个不存在的按钮。',
    },
    {
        id: 'N254-2', file: MOD, why: '🔴 `every` 改成 `some` —— 比原缺陷更贵的那种"修好了"',
        from: 'if (rows.every(rowHasTitle))', to: 'if (rows.some(rowHasTitle))',
        expect: 'P5g',
        note: '混着缺题行的词会被判成 has-title ⇒ **真正缺题的补不回来**。'
            + 'P5f 单独看照样绿 —— 所以 P5g 必须与它成对,这一发就是来证这件事的。',
    },
    {
        id: 'N254-3', file: HALL, why: '🔴 按钮的闸退回**过滤后**的集合(成因字面复现)',
        from: '{canGenerateTitlesNow && (', to: '{hasNoTopics && (',
        expect: 'P5j',
        note: '闸读 filteredByKeyword、面孔读未过滤 topics —— 两个数各自看都对,'
            + '合起来就是「待写页签上有题的词冒出按钮」。',
    },
    {
        id: 'N254-4', file: MOD, why: '标题判空不再 trim ⇒ 一行空格就算"有题"',
        from: "typeof r.optimized_title === 'string' && r.optimized_title.trim() !== ''",
        to: "typeof r.optimized_title === 'string' && r.optimized_title !== ''",
        expect: 'P5h',
    },
    {
        id: 'N254-5', file: MOD, why: '🔴 悄悄删掉行类型上那一列(**运行期毫无变化**的那种改法)',
        from: '    optimized_title?: string | null;\n', to: '',
        expect: 'P5m',
        note: '删掉类型声明不影响 JS 运行 ⇒ P5f/P5g/P5h **全绿**。'
            + '真实调用点哪天换成不带这一列的行,每一行都读作「缺题」,修**静默失效** —— '
            + '只有 P5m 这种「生产方那一列还在不在」的格子看得见它。',
    },
    {
        id: 'M1', file: MOD, why: '「不走计费」被说成「免费」',
        from: "    if (!charge) return null;", to: "    if (!charge) return '本次免费';",
        expect: 'P1',
    },
    {
        id: 'M2', file: MOD, why: '显示实扣而不是上限',
        from: '`本次将扣 ${charge.ceiling_points.toLocaleString()} 算力`',
        to: '`本次将扣 ${charge.estimated_points.toLocaleString()} 算力`',
        expect: 'P2b',
    },
    {
        id: 'M3', file: MOD, why: '上限小于实扣的违规回包被放行',
        from: '    if (c.ceiling_points < c.estimated_points) return null;', to: '',
        expect: 'P2c',
    },
    {
        id: 'M4', file: HALL, why: '三元那颗不传**面孔** ⇒ 两面共用默认面,子集面按整表取价',
        from: 'chargeText(titleGenCharge, { face: titleGenFace })', to: 'chargeText(titleGenCharge)',
        all: true, expect: 'E3',
    },
    {
        id: 'M5', file: HALL, why: '删掉一处**收费**入口的价徽标(「为这些词生成标题」那颗)',
        from: `                                        {chargeText(topicGenCharge) && (
                                            <span data-testid="topic-gen-charge" className="ml-1.5 text-xs opacity-90">
                                                · {chargeText(topicGenCharge)}
                                            </span>
                                        )}`,
        to: '', expect: 'E1',
    },
    {
        id: 'M6', file: HALL, why: '价改成「只在有值时才写」⇒ 加完词停在旧价上',
        from: '            setTopicGenCharge(parseTopicGenCharge(data.charge));',
        to: '            if (data.charge) setTopicGenCharge(parseTopicGenCharge(data.charge));',
        /* 🔴 第一轮写的是 `expect: 'C6'`,而它**存活了** —— C6 换的是价的数值,
              两次回包都带 charge,条件写照样触发。补了 C7(价从有到无)才咬住。
              这一条留在表里当记号:**"预期它红"本身也可能是错的**。 */
        expect: 'C7',
    },
    {
        id: 'M7', file: MOCK, why: '沙盒只挂一个分支(教程另一半路上无价)',
        from: '            charge: sandboxTopicGenCharge(3),\n', to: '',
        expect: 'S1b',
    },
    {
        id: 'M8', file: HALL, why: '去掉一处余额就绪判断 ⇒ 「不知道」又被显示成「只够 0 篇」',
        from: "!isAdmin && pricingReady && walletStatus === 'ready' && totalPoints",
        to: '!isAdmin && pricingReady && totalPoints', once: true, expect: 'W1',
    },
    {
        id: 'M9', file: MOD, why: '三个面孔的收费判断整体判反(收费面变免费、补救面变收费)',
        from: "    return face !== 'recover-missing';", to: "    return face === 'recover-missing';",
        expect: 'P3',
    },
    {
        id: 'M10', file: MOD, why: '前端自己乘,但换个写法绕开黑名单',
        from: '`本次将扣 ${charge.ceiling_points.toLocaleString()} 算力`',
        to: '`本次将扣 ${[charge.base_points].map((b) => b * charge.keyword_count)[0].toLocaleString()} 算力`',
        expectSurvive: 'E4',
        note: 'E4 是黑名单,对"换写法"天生瞎 —— 预期它绿。真正该抓住的是 P2b(上限≠实扣时显示上限):'
            + '自己乘出来的是 240,而那一格喂的上限是 300。**黑名单只当第二道。**',
    },
    {
        id: 'M11', file: HALL,
        why: '🔴 徽标翻了**端点没翻**:重试不显示价,却仍打 generateTitles(整批重跑再收一次)',
        from: "onClick={() => { void generateTopicForKeyword(topic.keyword_id, keywords.find((k) => k.id === topic.keyword_id)?.keyword || ''); }}>",
        to: 'onClick={() => { void generateTitles(); }}>',
        expect: 'E2b',
        note: '🔴 这是本单最贵的那种状态:**照收钱而屏幕上没有任何数字**。'
            + 'R1a(按行为认免费面)与 E2b/E2c2 应同时红。',
    },
    {
        id: 'M12', file: HALL, why: '新词面读**整表价**(说 10 份而实扣 2 份)',
        from: 'const titleGenCharge = isNewKwOnly ? topicGenChargeNewOnly : topicGenCharge;',
        to: 'const titleGenCharge = topicGenCharge;',
        expect: 'E3b',
    },
    {
        id: 'M13', file: HALL, why: '子集外的词**省略**而不是显式置 0 ⇒ 后端按缺省照出照收',
        from: 'slots: onlyKeywordIds.includes(kw.id) ? (kw.required_articles ?? 0) : 0,',
        to: 'slots: kw.required_articles ?? 0,',
        expect: 'E5b',
    },
    {
        id: 'M14', file: HALL, why: '补救请求不带 `generation_request_id` ⇒ 闸第一行就 403',
        from: '                    generation_request_id: generationRequestId,\n', to: '',
        expect: 'E5d',
    },
    {
        id: 'MSYN', file: MOD, why: '🔴 **故意写坏语法** —— 这一发是那道语法前置自己的牙证',
        /*
         * 🔴 锚**不钉任何具体名字**。上一版钉的是
         *    `export function isChargedClick(opts: { isNewKwOnly: boolean })` ——
         *    那个函数后来改名成 `isChargedFace`,于是这一发**当场没下成**,
         *    而它是语法前置自己的牙证 ⇒ 那道前置有一阵子**没有任何东西证明它在工作**
         *    (NC 检查如实报了「没下成」,没冒充 CAUGHT —— 这次是它救的场)。
         *    `export function ` 在任何 ES 模块里都必然出现,改名重构动不了它。
         *
         * 🔴 中间还试过裸的 `export ` —— **它先命中了注释里的那一处**,
         *    于是"下毒后语法仍然通过",牙证等于没有(锚被无关行满足,本仓惯犯)。
         *    带上前导换行钉在**行首**,注释行是 ` * …` 开头,匹配不到。
         */
        from: '\nexport function ', to: '\nexport function function ', once: true,
        expectSyntaxFail: true,
        note: '不加前置的话,它会让门 rc=1,被读成「锁咬住了」—— 而实际上**什么都没测到**。C 2026-09-20 就栽在这里。',
    },
];

const run = (script) => {
    try {
        const out = execFileSync(process.execPath, [join(ROOT, 'scripts', script)],
            { cwd: ROOT, encoding: 'utf8', timeout: 600000, stdio: ['ignore', 'pipe', 'pipe'] });
        return { rc: 0, out };
    } catch (e) {
        return { rc: e.status === undefined ? -1 : e.status, out: String(e.stdout || '') + String(e.stderr || '') };
    }
};
/*
 * 🔴 第一版写成 `/^ {2}FAIL {2}/`(FAIL 后两个空格),而判据打的是
 *    `  FAIL name`(**一个**空格)—— 于是 `reds` 恒为空,**十一发毒全部"存活"**。
 *    判据其实全都红了,是**我的读数器坏了**。零 FAILED 行 = 全绿**或者**没读到,
 *    这两件事必须分开:所以下面**以 rc 为准**(rc=1 就是有格子红了),
 *    名字只是拿来说清"是谁红的"。读数器自己也要有自证。
 */
const redIds = (out) => [...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]);

/* ── 基线 ─────────────────────────────────────────────────────────────── */
console.log('=== 基线(下毒前必须全绿,红基线会让每一发毒都像命中)===');
const baseL = run('verify-topic-gen-charge.mjs');
const baseR = run('test-topic-gen-charge-render.mjs');
console.log(`  逻辑臂 rc=${baseL.rc} · 浏览器臂 rc=${baseR.rc}`);
if (baseL.rc !== 0 || baseR.rc !== 0) {
    console.log('🔴 基线不绿,注毒结果不作数。');
    console.log(baseL.out.split('\n').filter((l) => l.includes('FAIL')).join('\n'));
    console.log(baseR.out.split('\n').filter((l) => l.includes('FAIL')).join('\n'));
    process.exit(3);
}

/* ── 逐发 ─────────────────────────────────────────────────────────────── */
let surprises = 0;
for (const p of POISONS) {
    const original = readFileSync(p.file, 'utf8');
    /* 🔴 对照臂:**没动过的文件**必须判得过 —— 恒 false 的尺子会把每一发都报成「没下成」。 */
    try { assertRulerWorks(p.file); } catch (e) {
        console.log(`\n${p.id} ${e.message}`);
        process.exit(3);
    }
    const n = original.split(p.from).length - 1;
    if (n === 0) { console.log(`\n${p.id} 🔴 锚没命中,这一发没下成(不是"有牙")`); surprises += 1; continue; }
    const poisoned = p.all || !p.once
        ? (p.once ? original.replace(p.from, p.to) : original.split(p.from).join(p.to))
        : original.replace(p.from, p.to);
    writeFileSync(p.file, p.once ? original.replace(p.from, p.to) : poisoned, 'utf8');
    try {
        /* 🔴 下毒后先验语法:「毒没下成」与「锁咬住了」在 rc 上完全同形。 */
        const parses = syntaxOk(p.file);
        if (p.expectSyntaxFail) {
            /* 这一发就是来验这道前置的:它**必须**被前置拦下,不该走到门那一步。 */
            if (parses) surprises += 1;
            console.log(`\n${p.id} ${p.why}`);
            console.log(`   命中 ${n} 处 · ${parses ? '🔴 前置没拦住 —— 那道前置是瞎的' : '✅ 被**语法前置**拦下(未计入 CAUGHT)'}`);
            if (p.note) console.log(`   ${p.note}`);
            continue;
        }
        if (!parses) {
            console.log(`\n${p.id} ${NOT_LANDED_SYNTAX}`);
            surprises += 1;
            continue;
        }
        const L = run('verify-topic-gen-charge.mjs');
        const R = run('test-topic-gen-charge-render.mjs');
        const reds = [...redIds(L.out), ...redIds(R.out)];
        /* 🔴 以退出码为准:门红了 rc 就是 1。名字只用来说清是谁红的 ——
              靠名字判"抓没抓住"的话,一个正则写错就会把十一发全报成存活。 */
        const wentRed = L.rc === 1 || R.rc === 1;
        const want = p.expect || p.expectSurvive;
        const caught = wentRed && (reds.length === 0 || reds.some((r) => r.startsWith(want.split('[')[0])));
        const verdict = p.expectSurvive
            ? (!wentRed ? '存活(如预期)' : `被 ${reds.join(',') || 'rc=1'} 抓住(比预期强)`)
            : (caught ? `被 ${reds.join(',') || 'rc=1'} 抓住`
                : `🔴 存活 —— 预期 ${want} 转红,rc(逻辑/浏览器)=${L.rc}/${R.rc},红的是 ${reds.join(',') || '(无)'}`);
        if (!p.expectSurvive && !caught) surprises += 1;
        console.log(`\n${p.id} ${p.why}\n   命中 ${n} 处 · ${verdict}`);
        if (p.note) console.log(`   ${p.note}`);
    } finally {
        writeFileSync(p.file, original, 'utf8');
    }
}

console.log(`\n=== 复原自证 ===`);
try {
    const st = execFileSync('git', ['status', '--porcelain', 'src/pages/Writing/topicGenCharge.ts',
        'src/pages/Writing/WritingHall.tsx', 'src/sandbox/mockData.ts'],
    { cwd: ROOT, encoding: 'utf8' });
    console.log(st.trim() ? st.trim() : '(三个文件都回到下毒前)');
} catch { console.log('(git 读不到,自行核对)'); }
const afterL = run('verify-topic-gen-charge.mjs');
const afterR = run('test-topic-gen-charge-render.mjs');
console.log(`收尾复跑:逻辑臂 rc=${afterL.rc} · 浏览器臂 rc=${afterR.rc}`);
console.log(surprises === 0 ? '\n没有意外' : `\n🔴 ${surprises} 发出乎预料,按结果改判据`);
