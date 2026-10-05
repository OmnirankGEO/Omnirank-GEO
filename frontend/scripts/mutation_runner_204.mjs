#!/usr/bin/env node
/**
 * 注毒自证 · #204 a1 图文选题流。
 *
 * @@R@@ 别与 build / 其他渲染闸并行(毒在工作树里);中途别 kill(还原不执行)。
 *
 * 🔴 每发标了打哪一轴:结构锁用结构毒,行为/纯函数锁用行为毒。
 *    锚必须**恰好命中一次** —— 命中多次时运行器报「毒没下成」,
 *    那和「锁没牙」在 rc 上同形,不能混(#210 栽过一次)。
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const GATES = {
    t: 'scripts/verify-image-note-topics.mjs',
    r: 'scripts/test-image-note-detail-render.mjs',
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
            cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, timeout: 12 * 60 * 1000,
        });
    } catch (e) {
        rc = typeof e.status === 'number' ? e.status : 1;
        out = String(e.stdout || '') + String(e.stderr || '');
    }
    const redSet = new Set([...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]));
    return { rc, redSet, out };
}

const PANEL = 'src/pages/Writing/ImageNoteTopicPanel.tsx';
const DOMAIN = 'src/pages/Writing/imageNoteTopics.ts';
const DETAIL = 'src/pages/Writing/DouyinPostDetail.tsx';
const PICKER = 'src/components/writing/CardStylePicker.tsx';
const PRODUCTION = 'src/pages/Writing/imageNoteProduction.ts';
const ROUTE = 'src/pages/Writing/ImageNoteDetailRoute.tsx';

/*
 * 🔴 [WO_258 · 2026-09-23] 五发改靶(每发配「改靶前 / 改靶后」两个读数,见交付单):
 *    · S4 / S8 锚早已失效 —— 命中 0 次,运行器每次都如实报「毒没下成」,两班没人回来补:
 *        S4:a9ab52b8e 把 `int(` 改名 `price(`;
 *        S8:a9ab52b8e 把请求体挪进 `imageNoteProduction.ts::productionBatch`。
 *    · S10 / S14 / S5b 的行为格打在**死代码**上:a9ab52b8e 起详情页传 `showTopics={false}`,
 *      左栏(含那个选择器)整列不挂,`pickedTopic` 恒为 null ⇒ A6 分支与左栏选择器不可达。
 *      毒下得成,可没有一格能红 —— 那是「毒仍绿」第三解「够不着」。
 *        S10 → 新形态的同一件事:点一条没做出来的选题,不许被带进任何一篇作品的详情(T9);
 *        S14 → 「当前风格」现在在「再次创作」弹窗里标出,毒打它的预选(T12);
 *        S5b → 行为臂那一态不存在了,只留纯函数臂 A6b。
 *    · S4 / S8 改锚后**第一次真落地**,随即照出闸 t 自己的病(S6 也中):闸 t 顶部静态 import 的
 *      两份子测试在 import 阶段就断言、一红就抛 ⇒ 整闸崩、一格不报,毒「打崩了闸」而不是
 *      「红在写死的那一格」。闸 t 改成逐个动态 import、失败记成有名字的一格(A0a / A0b)。
 */

const POISONS = [
    {
        /* 轴:纯函数 + 行为。假进度条让"卡住了"和"正在跑"长得一模一样。 */
        id: 'S1', file: DOMAIN,
        why: '进度百分比改成前端自己按 stage 猜,不用服务端给的 percent',
        from: '    const percent = Number.isFinite(pctRaw) ? Math.max(0, Math.min(100, Math.floor(pctRaw))) : 0;',
        to: `    const GUESS = { queued: 10, gathering: 55, distilling: 80, done: 100 };
    const percent = (GUESS)[str(o.stage)] ?? 0;`,
        expectRed: { t: ['A2'], r: ['T7'] },
    },
    {
        /* 轴:结构 + 行为。只改本地 state:屏幕上像成功了,刷新打回原形。 */
        id: 'S2', file: PANEL,
        why: '改完标题只改本地 state,不重新读回',
        /* 🔴 锚带上前一行的注释封口:光 `if (brandId) await load(brandId);`
           这一句在文件里出现 **2 次**(saveTitle 与 doStart),运行器会报
           「毒没下成」—— 那和「锁没牙」在 rc 上同形,不能混。
           替换文本也必须把那个注释封口写回去,不然毒把封口删了,
           判据是因为**编译不过**才红的,不是因为锁咬住了。 */
        from: `             */
            if (brandId) await load(brandId);`,
        to: `             */
            setRows((prev) => prev.map((r) => (r.id === editingId
                ? { ...r, title: editingTitle } : r)));`,
        expectRed: { t: ['A3'], r: ['T8'] },
    },
    {
        /* 轴:纯函数。价拿不到就编一个数 —— 那是"静默免费"的假象。 */
        id: 'S3', file: DOMAIN,
        why: '价拿不到时按数量编一个价,而不是明说读不到',
        /* 🔴 第一版锚假设这里是 `if (p === null) return …;`,真代码是**三元**
           (imageNoteTopics.ts:142)⇒ 0 命中、运行器报「毒没下成」。
           取窗自证只答「我切对窗了没」,答不了「我以为的形态对不对」。 */
        from: '    return p === null ? ' + '`' + '开始制作 (${count}) · 价目读不到' + '`' + ' : ' + '`' + '开始制作 (${count}) · ${p} 算力' + '`' + ';',
        to: '    return ' + '`' + '开始制作 (${count}) · ${count * 130} 算力' + '`' + ';',
        expectRed: { t: ['A4d'] },
    },
    {
        /* 轴:纯函数。给一颗按下去必然失败的按钮。 */
        id: 'S4', file: DOMAIN,
        why: '价没拿到也让点「开始制作」',
        /* [WO_258 重锚] a9ab52b8e 把 `int(` 改名 `price(`,旧锚命中 0 次。 */
        from: "    return (input?.selectedCount || 0) > 0 && price(input?.points) !== null && !input?.busy;",
        to: "    return (input?.selectedCount || 0) > 0 && !input?.busy;",
        expectRed: { t: ['A4f'] },
    },
    {
        /* 轴:纯函数 + 行为。不写原因的禁用 = 第 11 条要清的那种猜。 */
        /* 🔴 拆两发。第一版一发打「待做」那句却同时指望行为臂 T10 红 ——
         *    而 T10 点的是**制作中**那一行:**轴对不上**,毒下成了 T10 也不动。
         *    纯函数臂按态各打一发,行为臂只挂在它真的点到的那一态上。 */
        id: 'S5a', file: DOMAIN,
        why: '「待做」态禁用编辑区但不给原因',
        from: "        reason: '这一条还没做，先勾上它点「开始制作」',",
        to: "        reason: '',",
        expectRed: { t: ['A6'] },
    },
    {
        id: 'S5b', file: DOMAIN,
        why: '「制作中」态禁用编辑区但不给原因',
        from: "            reason: '这一条正在制作，做完才能改文案',",
        to: "            reason: '',",
        /* [WO_258] 撤 r:T10 —— 这段只被详情页 A6 分支读(`selectionView(pickedTopic)`),
           而 `pickedTopic` 自 a9ab52b8e 起恒为 null,行为臂够不着;T10 改锚到了第 1 步
           「下一步」的灰因,不是这段代码。纯函数臂 A6b 照旧咬它。 */
        expectRed: { t: ['A6b'] },
    },
    {
        /* 轴:纯函数。N 是要扣钱的数,多一条就是多扣一条的钱。 */
        /* 🔴 换靶。第一版打 `archived` 那道过滤,**毒确实下成了、判据却全绿** ——
         *    因为 `pick()` 是精确匹配,那道过滤本来就冗余:「毒仍绿」第四解
         *    「目标冗余」。顺着它查,真正会让人花钱的口子是 `topicRow` 的兜底。 */
        id: 'S6', file: DOMAIN,
        why: '认不出的状态又兜底成「待做」—— 用户会为一个我们不认识的东西付钱',
        from: "    const status = (STATUS_LABEL[s] ? s : 'unknown') as TopicStatus;",
        to: "    const status = (STATUS_LABEL[s] ? s : 'pending') as TopicStatus;",
        expectRed: { t: ['A9b'] },
    },
    {
        /* 轴:纯函数。c1 对 making/done 的 PATCH 与建单都是 409。 */
        id: 'S7', file: DOMAIN,
        why: '「制作中 / 已完成」也让改标题、也让勾选(按下去必然 409)',
        from: `        editable: status === 'pending',`,
        to: '        editable: true,',
        expectRed: { t: ['A9c'] },
    },
    {
        /* 轴:结构。请求体不带 topic_id 就回到"标题由 LLM 自由生成"。 */
        id: 'S8', file: PRODUCTION,
        why: '「开始制作」的请求体不再逐项带 topic_id —— 「按标题制作」就没了',
        /* [WO_258 重锚] a9ab52b8e 把请求体挪进 `productionBatch`,旧锚命中 0 次。 */
        from: ': { topic_id: row.id }),',
        to: ': {}),',
        expectRed: { t: ['A4'] },
    },
    {
        /* 轴:结构 + 行为。样图没了就回到"有形态的选项只给名字"。 */
        id: 'S9', file: PANEL,
        why: '每行风格不再给可见样图(退回只给风格名)',
        from: '                                        {STYLE_SAMPLES[r.styleKey] ? (',
        to: '                                        {false ? (',
        expectRed: { r: ['T5'] },
    },
    {
        /*
         * 轴:几何。🔴 这三发是**看了交付截图之后**补的:
         *    原来只有 S9 打"样图在不在",而截图里那张样图实际只有 36×28 ——
         *    判据(元素存在)绿着,用户却分不出这是哪一款。
         *    「可见」不是「看得出」:量错了东西,锁就只锁住一个字面。
         */
        id: 'S11', file: PANEL,
        why: '行内样图缩回一个小色块(元素还在,尺寸看不出形态)',
        from: '                                                className="block h-16 w-12 shrink-0 rounded border border-border object-cover" />',
        to: '                                                className="block h-4 w-3 shrink-0 rounded border border-border object-cover" />',
        expectRed: { r: ['T5b'] },
    },
    {
        /* 轴:结构 + 行为。退回"只给色块不给名字"。 */
        id: 'S12', file: PANEL,
        why: '行上不写风格真名,退回一句笼统的「这一条的卡面风格」',
        /*
         * 🔴 [2026-09-20 重锚] 旧锚是**单行**的
         *    `{styleNameOf(r.styleKey) || '系统推荐的卡面风格'}`。
         *    产品后来把它拆成两行、兜底也细分成「有 styleKey / 没有」两句
         *    ⇒ 锚命中 0 次,这一发**一直没下成**(runner 如实报了,没人回来补)。
         *    ⇒ 只钉「那次取真名的调用还在被用」这一件事,不钉它后面跟着什么兜底。
         */
        from: '                                            {styleNameOf(r.styleKey)',
        to: '                                            {(false && styleNameOf(r.styleKey))',
        expectRed: { r: ['T5c'] },
    },
    {
        /*
         * 轴:结构 + 行为。🔴 这一发替我自己的夹具事故站岗:
         *    后端 key 与 `STYLE_SAMPLES` 对不上时,整块**静默**退化成裸按钮。
         *    毒直接切掉出图这条路,T11 必须红 —— 否则那格是摆设。
         */
        id: 'S13', file: PICKER,
        why: '风格选择器不再出样图(退回裸按钮 —— Owner 两次点名不许出现的样子)',
        from: '                        {STYLE_SAMPLES[s.key] && (',
        to: '                        {false && (',
        expectRed: { r: ['T11'] },
    },
    {
        /* 轴:结构 + 行为。当前风格没标出来 ⇒ 用户不知道自己在跟什么比。 */
        id: 'S14', file: DETAIL,
        why: '风格选择器不再标出「当前这一款」(顶栏照样写着它的名字)',
        /* [WO_258 改靶] 旧靶是左栏选择器的 `value={detail?.style.key || ''}` —— 左栏不挂了,
           毒下得成、没有一格能红。「当前这一款」现在由「再次创作」弹窗的预选标出。 */
        from: "            setRegenStyle(data.style?.key || '');",
        to: "            setRegenStyle('');",
        expectRed: { r: ['T12'] },
    },
    {
        /* 轴:结构 + 行为。把一条没做出来的选题当成一篇作品打开 = 张冠李戴。 */
        id: 'S10', file: PANEL,
        why: '点一条还没做出来的选题,勾上的同时把人带进了一篇作品的详情',
        /* [WO_258 改靶] 旧靶是详情页 A6 分支的 `showStyleSample` —— `pickedTopic` 恒为 null,
           够不着。新形态里张冠李戴要两处一起松才会发生(面板把没做出来的也交出去 +
           路由不再只放有 post_id 的),所以这一发下两处:只下一处的话另一处兜住,
           证明的是「两道闸有一道还在」,不是「T9 咬得住」。 */
        from: '                        if (r.selectable) setSelected(prev => toggleTopicSelection(prev, r.id));',
        to: '                        if (r.selectable) { setSelected(prev => toggleTopicSelection(prev, r.id)); onSelectTopic(r); }',
        also: [{
            file: ROUTE,
            from: 'onSelectTopic={row => { if (row.postId) navigate(`/writing/image-note/${row.postId}`); }}',
            to: 'onSelectTopic={row => { navigate(`/writing/image-note/${row.postId || Math.abs(row.id)}`); }}',
        }],
        expectRed: { r: ['T9'] },
    },
    {
        /* 轴:结构 + 行为。心跳退回首读那条路 = Owner 2026-09-22 看到的「一直闪」。 */
        id: 'S15', file: PANEL,
        why: '4 秒心跳改回走 read(每跳 setLoading(true),列表整块换成占位符)',
        from: "        const timer = window.setInterval(() => { void load(brandId, ac.signal, 'refresh'); }, 4000);",
        to: '        const timer = window.setInterval(() => { void load(brandId, ac.signal); }, 4000);',
        expectRed: { t: ['A12b'], r: ['T14'] },
    },
    {
        /* 轴:纯函数 + 行为。计划本身说谎:refresh 也出占位符。 */
        id: 'S16', file: DOMAIN,
        why: 'topicReadPlan 让 refresh 也 showLoading',
        from: "    if (mode === 'refresh') return { showLoading: false, silentErrors: true };",
        to: "    if (mode === 'refresh') return { showLoading: true, silentErrors: true };",
        /* 🔴 gate t 对这发毒**会红但不带格名**:verify-image-note-topics.mjs 顶部静态 import 的
           test-image-note-flow.mjs 先在 deepEqual 上抛(rc=1、没有「FAIL A12」那一行,A12 根本没跑到)。
           2026-09-22 手工实测:rc=1 · 失败集=空。所以这发只让 r 的 T14(真浏览器)来抓。
           [WO_258 · A] 同一个病还让 S4 / S6 / S8 全军覆没;闸 t 已改成逐个动态 import、失败记成
           有名字的一格(A0a / A0b),本闸自己的格照常跑完 ⇒ A12 跑得到了,补回 t:A12
           (实测:S16 下去 t 红 A0a + A12)。改的是 Review 写的这一发,交付单单列。 */
        expectRed: { t: ['A12'], r: ['T14'] },
    },
];

const TOUCHED_FILES = [...new Set(POISONS.flatMap(
    (p) => [p.file, ...(p.also || []).map((a) => a.file || p.file)]))];
const SHA_AT_START = Object.fromEntries(TOUCHED_FILES.map((f) => [f, sha(abs(f))]));
say('被碰文件开跑前 sha:');
for (const f of TOUCHED_FILES) say(`  ${f} ${SHA_AT_START[f].slice(0, 12)}`);

say('=== 0. 基线(三把闸的失败集都必须为空) ===');
const baseline = {};
for (const [name, gate] of Object.entries(GATES)) {
    const r = run(gate);
    baseline[name] = r.redSet;
    /* rc=3 是"有未评估",不是失败;判基线干净只看失败集。 */
    const good = (r.rc === 0 || r.rc === 3) && r.redSet.size === 0;
    say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 失败集=${r.redSet.size ? [...r.redSet].join(',') : '空'}`);
    if (!good) {
        say('  🔴 基线不干净,注毒读数无意义(红基线会让每发毒都像命中)。');
        process.exit(1);
    }
}

/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红。它抛异常、自己还原。
 */
proveGuardHasTeeth(abs(POISONS[0].file), say);

for (const p of POISONS) {
    say(`\n=== ${p.id} ${p.why} ===`);
    /*
     * 一发毒可能动**同一个文件的两处**(主锚 + also)。
     *
     * 🔴 第一版是「逐条 edit 各备份一次、再按顺序逐个还原」—— **那是错的**:
     *    同一个文件被备份两次时,bak#2 存的是**第一处已经下毒后**的中间态,
     *    顺序还原会把中间态又写回去。Z3 实测:`genFailure: str(o.failure_reason)`
     *    留在了工作树里,而运行器照样打印「还原:sha 一致、原文回位」——
     *    因为它比的是那个中间态的 sha。**运行器自己撒了谎。**
     *  ⇒ 改成:**按文件**各备份一次(下毒前的原始态),还原也按文件一次;
     *    并在最后逐文件核 sha **与原始态**相等、逐条核原文回位。
     */
    const edits = [{ file: p.file, from: p.from, to: p.to },
        ...(p.also || []).map((a) => ({ file: a.file || p.file, from: a.from, to: a.to }))];
    const touched = [...new Set(edits.map((e) => e.file))];
    const backups = touched.map((rel) => {
        const target = abs(rel);
        const bak = `${target}.z204bak-${p.id}`;
        copyFileSync(target, bak);
        return { rel, target, bak, before: sha(target) };
    });
    const restoreAll = () => {
        for (const b of backups) { copyFileSync(b.bak, b.target); unlinkSync(b.bak); }
    };
    /*
     * 🔴 对照臂**一发一次**,不是一条 edit 一次:放进循环的话,第二条 edit 之前
     *    文件已被第一条改过,对照臂看到**中间态**就会喊「没下毒的文件都判不过 ⇒
     *    尺子坏了」,而尺子好好的。退出前先还原,否则半截毒留在树上,
     *    下一次跑基线当场不干净 —— 那个读数和「产品真坏了」完全同形。
     */
    for (const rel of touched) {
        try {
            assertRulerWorks(abs(rel));
        } catch (err) {
            restoreAll();
            say(`  ${err.message}`);
            process.exit(3);
        }
    }
    let landed = true;
    for (const e of edits) {
        const target = abs(e.file);
        const src = readFileSync(target, 'utf8');
        const hits = src.split(e.from).length - 1;
        if (hits !== 1) {
            say(`  FAIL 毒没下成:${e.file} 的锚命中 ${hits} 次(要恰好 1 次)—— 不是"锁没牙"`);
            landed = false;
            break;
        }
        writeFileSync(target, src.replace(e.from, e.to), 'utf8');
        if (sha(target) === createHash('sha256').update(src).digest('hex')) {
            say(`  FAIL 毒没下成:${e.file} 内容没变`);
            landed = false;
            break;
        }
    }
    if (!landed) { restoreAll(); failures += 1; continue; }
    /*
     * 🔴 语法闸放在**所有 edit 都落完之后**:一发毒可以由多条 edit 组成,
     *    中间态本来就是不平衡的,逐条验会把好毒误报成「写成语法错」。
     */
    const brokeSyntax = touched.filter((rel) => !syntaxOk(abs(rel)));
    if (brokeSyntax.length > 0) {
        say(`  FAIL ${brokeSyntax.join(', ')} ${NOT_LANDED_SYNTAX}`);
        restoreAll(); failures += 1; continue;
    }
    say(`  毒已落地(${edits.length} 处改动 · ${touched.length} 个文件)`);

    let caught = true;
    for (const [name, gate] of Object.entries(GATES)) {
        const want = p.expectRed[name] || [];
        if (!want.length) continue;
        const r = run(gate);
        const newRed = [...r.redSet].filter((x) => !baseline[name].has(x));
        const missed = want.filter((w) => ![...r.redSet].some((x) => x === w || x.startsWith(w)));
        const good = missed.length === 0 && r.rc !== 0 && r.rc !== 3;
        if (!good) caught = false;
        say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 期望红=[${want.join(',')}]`
            + `${missed.length ? ` 🔴 没红=[${missed.join(',')}]` : ' 全中'}`);
        say(`       新增报红 ${newRed.length} 条:${newRed.slice(0, 6).join(',') || '(无)'}`);
    }
    if (!caught) failures += 1;

    restoreAll();
    /* 🔴 还原核对**按文件**比下毒前的原始 sha,并逐条确认原文真的回位了。 */
    let restored = true;
    for (const b of backups) {
        if (sha(b.target) !== b.before) {
            restored = false;
            say(`  FAIL 还原:${b.rel} 的 sha 与下毒前不一致`);
        }
    }
    for (const e of edits) {
        const hits = readFileSync(abs(e.file), 'utf8').split(e.from).length - 1;
        if (hits !== 1) {
            restored = false;
            say(`  FAIL 还原:${e.file} 的原文没回位(${hits} 处)`);
        }
    }
    say(`  ${restored ? 'OK  ' : 'FAIL'} 还原:`
        + `${restored ? '逐文件 sha 与下毒前一致、原文逐条回位' : '🔴 有文件没回位'}`);
    if (!restored) failures += 1;
}

say('');
say('=== 收尾:被碰文件逐个核回开跑前的 sha ===');
for (const f of TOUCHED_FILES) {
    const now = sha(abs(f));
    const good = now === SHA_AT_START[f];
    say(`  ${good ? 'OK  ' : 'FAIL'} ${f} ${now.slice(0, 12)}`
        + `${good ? '' : ' 🔴 与开跑前不一致 —— 树里留了东西'}`);
    if (!good) failures += 1;
}

say('');
if (failures > 0) { say(`FAIL 注毒自证 ${failures} 项不过`); process.exit(1); }
say(`全部通过:${POISONS.length} 发毒,每发都红在写死的那一格,且逐发字节还原`);
process.exit(0);
