#!/usr/bin/env node
/**
 * 注毒自证 · #195 发布中心图文列表 —— 证明 L/R/D 三层**真的有牙**。
 *
 * 工单点名四发(L1/L2/L3/L4 各一),我另加四发守住最容易变成摆设的地方:
 *   Z1 状态按 `published_url` 非空自推「已发布」        ⇒ L1b/L1c 红(工单 L1)
 *   Z2 链接改成站内详情页                              ⇒ L2a/R2a 红(工单 L2)
 *   Z3 失败行拿**制作**失败原因(`failure_reason`)顶替  ⇒ L3b/R3b 红(工单 L3)
 *   Z4 chip 不按状态过滤(永远返回全部)                ⇒ L4c/R4b 红(工单 L4)
 *   Z5 「自报 · 待核实」归进已发布桶                     ⇒ D5 红(替服务商断言没证据的事)
 *   Z6 白名单里删掉一个后端真的会写的状态               ⇒ D3 红(界面会显示「状态未知」)
 *   Z7 列表里加一颗「发布」按钮                          ⇒ L5a/R5a 红(只看不发)
 *   Z8 切客户不重拉(effect 依赖里去掉 brandId)          ⇒ L6c/R6b 红(#180 机制)
 *
 * 🔴 浏览器臂(R*)与结构臂(L*)都要跑:结构臂只证到源码与纯函数,
 *    "屏幕上真的变了"只有浏览器臂看得见。两层都不红的毒才叫"没牙"。
 *
 * 🔴 别与 build / 其他渲染闸并行(毒在工作树里);中途别 kill(还原不执行)。
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const GATES = {
    l: 'scripts/verify-image-note-publish-list.mjs',
    r: 'scripts/test-image-note-publish-list-render.mjs',
    d: 'scripts/verify-image-note-publish-status-domain.mjs',
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

const PROJ = 'src/pages/Publishing/imageNotePublishStatus.ts';
const LIST = 'src/pages/Publishing/ImageNoteList.tsx';

const POISONS = [
    {
        id: 'Z1', file: PROJ,
        why: '状态按 `published_url` 非空自推「已发布」(工单 L1 点名的那一发)——'
            + '那会把"客户端自己说发了、我们没核实"显示成「已发布」',
        from: `export function statusView(publishStatus: unknown): StatusView {
    const raw = typeof publishStatus === 'string' ? publishStatus.trim() : '';`,
        to: `export function statusView(publishStatus: unknown, url?: unknown): StatusView {
    if (typeof url === 'string' && url.trim()) return { bucket: 'published', label: '已发布' };
    const raw = typeof publishStatus === 'string' ? publishStatus.trim() : '';`,
        also: [{
            from: '    const view = statusView(o.publish_status);',
            to: '    const view = statusView(o.publish_status, o.published_url);',
        }],
        expectRed: { l: ['L1b'], r: ['R1d'] },
    },
    {
        id: 'Z2', file: LIST,
        why: '「看已发布的那篇」改指站内详情页(工单 L2)—— 点了看不到发出去的那篇,'
            + '而界面看起来一切正常',
        from: '                            href={row.url} target="_blank" rel="noopener noreferrer">',
        to: '                            href={row.proofHref} target="_blank" rel="noopener noreferrer">',
        expectRed: { l: ['L2a'], r: ['R2a'] },
    },
    {
        id: 'Z3', file: LIST,
        why: '失败行拿**制作**失败原因顶替发布失败原话(工单 L3)——'
            + '用户按那句会去重做内容,而发布被拒真正要做的是换账号或改文案',
        from: `                        {row.failureReason
                            ? \`发布失败 · \${row.failureReason}\`
                            : FAILED_REASON_FALLBACK}`,
        to: `                        {row.failureReason
                            ? \`发布失败 · \${row.failureReason}\`
                            : \`发布失败 · \${(row as unknown as { genFailure?: string }).genFailure
                                || FAILED_REASON_FALLBACK}\`}`,
        /* 🔴 这一发要**真的把制作原因喂进去**才够得着,所以同时让投影层带上它 */
        also: [{
            from: '        failureReason: str(o.publish_failure_reason),',
            to: '        failureReason: str(o.publish_failure_reason),\n'
                + '        genFailure: str(o.failure_reason),',
            file: PROJ,
        }, {
            from: '    failureReason: string;',
            to: '    failureReason: string;\n    genFailure?: string;',
            file: PROJ,
        }],
        /* 🔴 期望的是 **L3f**(投影层不许读 `o.failure_reason`),不是 L3e:
           这发毒从投影层把制作原因带进来、组件里只出现 `genFailure`,
           所以 L3e(组件不出现裸 `failure_reason`)够不着。两格都要留:
           组件直接读是一条路,绕投影层是另一条 —— 堵一处等于没堵。 */
        expectRed: { l: ['L3f'], r: ['R6d'] },
    },
    {
        id: 'Z4', file: PROJ,
        why: 'chip 不按状态过滤(永远返回全部)(工单 L4)—— chip 上写着 1,点进去 7 条',
        from: `export function filterRows(rows: readonly PublishRow[], chip: string): PublishRow[] {
    if (chip === 'all') return [...(rows || [])];
    return (rows || []).filter((r) => r.bucket === chip);
}`,
        to: `export function filterRows(rows: readonly PublishRow[], chip: string): PublishRow[] {
    void chip;
    return [...(rows || [])];
}`,
        expectRed: { l: ['L4c'], r: ['R4b'] },
    },
    {
        id: 'Z5', file: PROJ,
        why: '把「自报 · 待核实」归进已发布桶 —— 替服务商向客户断言一件我们没有证据的事',
        from: "    self_reported_unverified: { bucket: 'inflight', label: '已自报 · 待核实' },",
        to: "    self_reported_unverified: { bucket: 'published', label: '已自报 · 待核实' },",
        expectRed: { d: ['D5'], l: ['L1c'] },
    },
    {
        id: 'Z6', file: PROJ,
        why: '白名单里删掉一个后端**真的会写**的状态 —— 界面上它会变成「状态未知」,'
            + '而没有任何东西会报警',
        from: "    self_reported_unverified: { bucket: 'inflight', label: '已自报 · 待核实' },\n",
        to: '',
        expectRed: { d: ['D3'], l: ['L0a'] },
    },
    {
        id: 'Z7', file: LIST,
        why: '列表里加一颗「发布」按钮 —— 同一个付费动作两处实现,'
            + '必有一处的幂等或价格没人验',
        from: '                    <a className="text-sm underline" data-testid="pub-imagenote-proof" href={row.proofHref}>校对</a>',
        to: '                    <a className="text-sm underline" data-testid="pub-imagenote-proof" href={row.proofHref}>校对</a>\n'
            + '                    <button type="button" className="text-sm underline">发布</button>',
        expectRed: { l: ['L5a'], r: ['R5a'] },
    },
    {
        id: 'Z8', file: LIST,
        why: '切客户不重拉(effect 依赖里去掉 brandId)—— 屏幕上留着上一个客户的作品,'
            + '「显示对、记错人」',
        from: '    }, [brandId, load]);',
        to: '        // eslint-disable-next-line react-hooks/exhaustive-deps\n    }, [load]);',
        expectRed: { l: ['L6c'], r: ['R6b'] },
    },
];

/*
 * 🔴 **开跑前给所有会被碰的文件拍一张 sha**,跑完逐个核回来。
 *    为什么需要这一层:逐发毒的还原核对只比它自己那一发的备份,
 *    而「上一轮跑挂在半路」或「还原逻辑本身有 bug」这两种它看不见 ——
 *    实测就发生过:旧版还原按顺序回写同一文件的两个备份,把中间态写回了树。
 *    残毒(一个没人调用的 `url?: unknown` 参数 + 一个永不执行的分支)
 *    **行为上是哑的**,所以所有闸照样全绿;而这些文件还是 untracked,
 *    `git status` 也看不出内容变化。
 *    🔴 全局 sha 是唯一能把「树被我自己弄脏了」变成红的东西。
 */
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
        const bak = `${target}.z195bak-${p.id}`;
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
