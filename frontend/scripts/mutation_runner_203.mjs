#!/usr/bin/env node
/**
 * 注毒自证 · #203 接回图文三栏详情页 + 发布收口。
 *
 * @@R@@ 别与 build / 其他渲染闸并行(毒在工作树里);中途别 kill(还原不执行)。
 *
 * 🔴 这一单有一发毒下在**判据自己身上**(P2:把夹具换成旧壳子那套字段)。
 *    理由:D2 的命题是"这张页读的是后端真回包的方言",而验证它的唯一办法
 *    就是把夹具换成**假方言**看它红不红。夹具也是仪器,仪器也要注毒。
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const GATES = {
    d: 'scripts/verify-image-note-detail-restore.mjs',
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

const APP = 'src/App.tsx';
const ROUTE = 'src/pages/Writing/ImageNoteDetailRoute.tsx';
const DETAIL = 'src/pages/Writing/DouyinPostDetail.tsx';
const ARM = 'scripts/test-image-note-detail-render.mjs';

const POISONS = [
    {
        id: 'P1', file: APP,
        why: '路由挂回别的东西 —— 「点进去那一屏」又不是 08-02 那张页了',
        /* 🔴 锚带上 path:`<ImageNoteDetailRoute />` 在 App.tsx 里出现 **2 次**
           (a1 加了无 id 那条路由),只用组件名会报「毒没下成」。 */
        from: `path="writing/image-note/:postId" element={<ProtectedRoute requiredModule="writing"><ImageNoteDetailRoute /></ProtectedRoute>}`,
        to: `path="writing/image-note/:postId" element={<ProtectedRoute requiredModule="writing"><WritingWorkspace /></ProtectedRoute>}`,
        expectRed: { d: ['D1'] },
    },
    {
        id: 'P2', file: ARM,
        why: '把夹具换成**旧壳子那套字段**(body_text → bodyText)—— '
            + '验的是 D2 到底读没读真回包的方言',
        from: `body_text: 'QA 夹具正文的第一句。第二句。',`,
        to: `bodyText: 'QA 夹具正文的第一句。第二句。',`,
        expectRed: { r: ['D2b'] },
    },
    {
        id: 'P3', file: DETAIL,
        why: '「去发布投放」丢掉 brand_id —— 成品可能记到别的服务商的客户名下',
        from: `        const b = post?.brand_id ? \`&brand_id=\${post.brand_id}\` : '';
        navigate(\`/publish?media_type=imagenote&geo_post_id=\${postId}\${b}\`);`,
        to: `        navigate(\`/publish?media_type=imagenote&geo_post_id=\${postId}\`);`,
        expectRed: { d: ['D3b'] },
    },
    {
        id: 'P4', file: DETAIL,
        why: '跳回**短视频**那一档 —— 按钮写着去发布投放,人到了却在另一条产线上',
        from: `navigate(\`/publish?media_type=imagenote&geo_post_id=\${postId}\${b}\`);`,
        to: `navigate(\`/publish?media_type=svideo&geo_post_id=\${postId}\${b}\`);`,
        expectRed: { d: ['D3'] },
    },
    {
        id: 'P5', file: ROUTE,
        why: '旧壳子的名字又出现在**代码**里(退役不彻底,下一个人会把它当现役)',
        from: `    const [siblingIds, setSiblingIds] = useState<number[]>([]);`,
        to: `    const [siblingIds, setSiblingIds] = useState<number[]>([]);
    const legacy = 'ImageNoteProof';
    void legacy;`,
        expectRed: { d: ['D4'] },
    },
    {
        id: 'P6', file: ROUTE,
        why: '容器不再把顺序传回去 —— 上一条/下一条当场变死键(DOM 里还在,点不动)',
        from: `            siblingIds={siblingIds}`,
        to: `            siblingIds={[]}`,
        expectRed: { d: ['D6b'], r: ['D6'] },
    },
    {
        id: 'P7', file: DETAIL,
        why: '三栏栅格塌回一列 —— Owner 要的「一屏搞定」没了,而 DOM 里三栏都还在',
        from: `@5xl:grid-cols-[minmax(0,0.62fr)_minmax(0,1fr)_minmax(0,0.9fr)]`,
        to: `grid-cols-1`,
        expectRed: { r: ['D7'] },
    },
    {
        /*
         * 🔴 第一版这发毒是 `{publishable && (` → `{publishable && false && (` ——
         *    它只改**运行期**行为,而 D5/D5b 是结构判据:`studio-post-goto-publish`
         *    那串字照样在源码里 ⇒ 毒落地却全绿。
         *    「毒没牙」和「毒打错了靶」在 rc 上同形,要分清:
         *    结构锁要用**结构毒**打。
         */
        /*
         * 🔴 [#204 a2] 换靶 + 换成**注入**。原来是"制作台又把面板挂回来",
         *    制作台已删 ⇒ 那个靶不在了,毒会报「毒没下成」(和「锁没牙」rc 同形)。
         *    命题没死:同一个付费动作只许一处实现。守它的是全仓级的 Z3。
         *    ⇒ 毒改成往**活屏**注入那件不许有的事 —— 这也是 Review 09-15 ①
         *      要的正对照:没有它,否定式断言恒真恒绿,谁也看不出来。
         */
        id: 'P8', file: DETAIL,
        why: '详情页也挂上行内发布面板 —— 同一个付费动作两处实现',
        from: "import { ImageNoteTopicPanel } from './ImageNoteTopicPanel';",
        to: "import { ImageNoteTopicPanel } from './ImageNoteTopicPanel';\n"
            + "import { ImageNotePublishInline } from './ImageNotePublishInline';\n"
            + "const __inject = <ImageNotePublishInline brandId={1} postId={1} revisionId={1} keyword=\"x\" />;",
        expectRed: { d: ['Z3'] },
    },
    {
        /*
         * 🔴 [#204 a2] 同上换成注入:钉「全仓不许再有那句已成谎话的文案」(Z1)。
         *    原 P9 打的是制作台那个出口,位置已消失;而 Z1 是**乙类**——
         *    不给它一发注入,它在制作台删掉之后就是一格永远绿的假保护。
         */
        id: 'P9', file: DETAIL,
        why: '把那句「站内发布暂未开放」写回活屏 —— 图文线早恢复了,留着就是骗人',
        /*
         * 🔴 [2026-09-20 重写] 旧写法把 `<span>` 插在
         *    `{onBack && ( <Button …/> )}` **里面**的 Button 之前 ——
         *    JSX 表达式容器只容一个子元素,两个并列元素是**语法错**。
         *    于是门是被"编译失败"弄红的,而不是被 Z1 咬住的:
         *    **Z1 这把锁从来没被这发毒真正验过**(语法前置这次才把它照出来)。
         *    ⇒ 改插到同一个父 div 下**合法的兄弟位置**。
         */
        from: '                    <span className="text-sm text-muted-foreground">',
        to: '                    <span>图文笔记的站内发布暂未开放</span>\n'
            + '                    <span className="text-sm text-muted-foreground">',
        expectRed: { d: ['Z1'] },
    },
    {
        id: 'P10', file: DETAIL,
        why: '闸没给理由时出口又变回"灰着且一个字都不说"',
        from: `    const jumpHint = !canPublish ? (gateReason || '正在确认这条能不能去投放，稍等一下')`,
        to: `    const jumpHint = !canPublish ? gateReason`,
        expectRed: { d: ['D8'] },
    },
    {
        /*
         * 🔴 Review 复跑用的就是这一发:只把 `${b}` 从模板里拿掉,
         *    `const b = …` 留着当死代码 ⇒ 旧的 D3b(找字面串)**全绿**。
         *    a2 之后它必须红在「navigate 的模板真的插了 b」与「浏览器真的带着它去了」。
         */
        id: 'P11', file: DETAIL,
        why: 'brand_id 不再拼进跳转 URL(但定义留着当死代码)—— 出现过 ≠ 被用上',
        from: `navigate(\`/publish?media_type=imagenote&geo_post_id=\${postId}\${b}\`);`,
        to: `navigate(\`/publish?media_type=imagenote&geo_post_id=\${postId}\`);`,
        expectRed: { d: ['D3b'], r: ['D3r2'] },
    },
    {
        /*
         * 🔴 [#204 a2 换靶] 原来打的是「返回列表带不带 tab=douyin」。
         *    那个 tab 撤了,锚 0 命中 ⇒ 报「毒没下成」(与「锁没牙」rc 同形)。
         *    命题换成了更该守的那件:**老链接不许落空**。
         */
        id: 'P12', file: 'src/pages/Writing/WritingWorkspace.tsx',
        why: '拆掉老链 ?tab=douyin 的重定向 —— 它会静默落在「写文章」上,页面看着正常',
        from: "        if (tabFromUrl === 'douyin') navigate('/writing/image-note', { replace: true });",
        to: "        if (false) navigate('/writing/image-note', { replace: true });",
        expectRed: { d: ['D9'], r: ['D9r'] },
    },
    {
        /*
         * 🔴 [#204 a2 换靶] 撤一个入口时最容易漏的那件事:**新入口也一起没了**。
         *    撤 tab 的那一笔里只要手一滑把这条链接删掉,从 `/writing` 就到不了图文,
         *    只剩深链和空态 CTA 能进去 —— 而两者都不会有人报"找不到入口"。
         */
        id: 'P13', file: 'src/pages/Writing/WritingWorkspace.tsx',
        why: '把「一步可达图文」那条入口也撤掉 —— 从写作中心再也到不了图文',
        from: '                        data-testid="writing-goto-image-note"',
        to: '                        data-testid="writing-goto-image-note-REMOVED"',
        expectRed: { d: ['D9b'], r: ['D9r2'] },
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
        const bak = `${target}.z203bak-${p.id}`;
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
