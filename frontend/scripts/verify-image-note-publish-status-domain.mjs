#!/usr/bin/env node
/**
 * 闸 · `geo_douyin_posts.publish_status` 的**取值域漂移**(#195)。
 *
 * 🔴 **本闸不进 build 链**:它要读后端源码,而 build 镜像里只有 `frontend/`
 *    (`verify-no-backend-refs-in-build-chain` 会拦)。它由我与复审手动跑。
 *
 * ## 为什么需要它
 *
 * 后端自己写着(`db/geo_douyin_db.py`):这一列**没有 CHECK 约束**,取值域靠代码维持,
 * 而且「前端对 publish_status 没有白名单,新加一个裸串会直接上屏」。
 * 前端现在有白名单了(`imageNotePublishStatus.ts`),于是新的风险换了形状:
 * 后端加一个新状态,前端**不报错**,只是把它显示成「状态未知」——
 * 一个没人会发现的降级。所以要有一把闸,把「后端加了新状态」变成**红**。
 *
 * ## 怎么枚举(先列形态,再写锚)
 *
 * 🔴 **绝对不能**全仓 grep `publish_status` 的字符串字面量:`writing_style_flywheel`
 *    也有一列同名(`db/writing_style_flywheel_db.py`),取值是 `measured` /
 *    `insufficient` 之类,由 `services/writing_strategy_assignment.py` 写 ——
 *    **另一张表、另一套语义**。混进来就是"验对了动作、验错了对象",
 *    而且会造出一串看着合理的假状态。我写这一层时差点就那么干了。
 *
 * 所以这里用**登记制**:每个写入点写清文件、形态、以及一条 `requires` 前提锚。
 * 前提不成立就整条报错退出 —— 宁可说"枚举不可信",不要给一个漂亮的错数字。
 *
 * 跑法:cd frontend && node scripts/verify-image-note-publish-status-domain.mjs [--base=<rev>]
 */
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);

let bad = 0;
const ok = (cond, label, detail) => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};
function unusable(why, detail) {
    console.log(`FAIL 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 1200));
    process.exit(1);
}

/**
 * 🔴 后端契约的**基线 rev**。
 *    默认钉在 C 的 195-c1 那一笔(`452b8e812`);它是我抽契约、写夹具时读的那一份。
 *    复审/后续要对别的树核就传 `--base=<rev>`;传 `worktree` 读工作树。
 *
 * 🔴 为什么钉 sha 而不是读工作树:我的 worktree 落后于 0913b(不带 C 的后端),
 *    读工作树会拿一份**过期**的后端去证"前端白名单齐全"——那是验错了对象。
 */
const DEFAULT_BASE = '452b8e812';
const baseArg = (process.argv.find((a) => a.startsWith('--base=')) || '').slice(7);
const BASE = baseArg || DEFAULT_BASE;

const REPO = join(ROOT, '..');           // frontend/.. = 仓库根
function readBackend(relFromRepoRoot) {
    if (BASE === 'worktree') return readFileSync(join(REPO, relFromRepoRoot), 'utf8');
    return execFileSync('git', ['show', `${BASE}:${relFromRepoRoot}`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });
}

console.log('闸 · publish_status 取值域(前端白名单 vs 后端写入点)');
console.log(`  (后端基线 = ${BASE})`);

/* ── 前端白名单(真调,不 grep)────────────────────────────────────── */
let M;
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(
        readFileSync(join(ROOT, 'src/pages/Publishing/imageNotePublishStatus.ts'), 'utf8'),
        { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } },
    ).outputText;
    const tmp = mkdtempSync(join(tmpdir(), 'a195-dom-'));
    const f = join(tmp, 'status.mjs');
    writeFileSync(f, js, 'utf8');
    M = await import(pathToFileURL(f).href);
} catch (e) { unusable('前端状态模块加载不了', e); }

const WHITELIST = new Set(M.KNOWN_PUBLISH_STATUSES || []);
ok(WHITELIST.size >= 4, 'D0a 分母自证:前端白名单非空(空的话下面每条都恒真)',
    `${WHITELIST.size} 个:${[...WHITELIST].join(',')}`);

/**
 * 写入点登记表。
 *
 * `requires`:这条形态的**前提锚**。前提没了(函数改名/写法变了)说明我这条读法已经过期,
 *            必须整条报错 —— 继续用一条过期的读法,会给出一个"看着齐全"的取值域。
 * `re`:捕获组 1 是状态字面量。
 */
const SITES = [
    {
        kind: 'writer',
        why: 'bind_publish_submission 的 SQL:下单成功即 publishing',
        file: 'db/geo_douyin_db.py',
        requires: 'def bind_publish_submission',
        re: /publish_status\s*=\s*'([a-z_]+)'/g,
    },
    {
        /* 🔴 这是**声明**不是写入方:它只说"这些值算已经发出去了",
           不代表有代码往列里写。两者分开记,否则 `measured` 会被当成"有人写"。 */
        kind: 'declaration',
        why: '后端自己声明的「已经发出去了」终态集 PUBLISH_TERMINAL_STATUSES',
        file: 'db/geo_douyin_db.py',
        requires: 'PUBLISH_TERMINAL_STATUSES = (',
        re: /PUBLISH_TERMINAL_STATUSES = \(([^)]*)\)/g,
        multi: true,
    },
    {
        kind: 'writer',
        why: 'mhz 下单回写:submitted/pending_sync ⇒ publishing,否则 failed',
        file: 'api/meijiehezi_api.py',
        requires: 'publish_status=("publishing" if',
        re: /publish_status=\("([a-z_]+)" if [^)]*else "([a-z_]+)"\)/g,
        two: true,
    },
    {
        kind: 'writer',
        why: '客户端自报的线索值 SELF_REPORTED_UNVERIFIED',
        file: 'api/geo_douyin_api.py',
        requires: 'SELF_REPORTED_UNVERIFIED = ',
        re: /SELF_REPORTED_UNVERIFIED = "([a-z_]+)"/g,
    },
    {
        kind: 'writer',
        why: '收敛器按 mhz item 终态推出的作品终态(classify_order_items 的判决)',
        file: 'services/geo_douyin/publish_convergence.py',
        requires: 'def classify_order_items',
        re: /return "([a-z_]+)",/g,
    },
];

const found = new Map();          // status -> [{kind, why}]
const writers = new Set();        // 真的有代码往列里写的那些
for (const site of SITES) {
    let src;
    try { src = readBackend(site.file); }
    catch (e) { unusable(`读不到 ${BASE}:${site.file}`, e); }
    if (!src.includes(site.requires)) {
        unusable(`枚举前提不成立:${site.why}\n`
            + `     期望在 ${site.file} 里看到 \`${site.requires}\`,没看到。\n`
            + '     🔴 这条读法过期了 ⇒ 取值域不可信。先核对再跑,别拿一个漂亮的错数字交差。');
    }
    site.re.lastIndex = 0;
    let m;
    let hits = 0;
    while ((m = site.re.exec(src)) !== null) {
        hits += 1;
        const vals = site.multi
            ? [...m[1].matchAll(/"([a-z_]+)"/g)].map((x) => x[1])
            : (site.two ? [m[1], m[2]] : [m[1]]);
        for (const v of vals) {
            if (!found.has(v)) found.set(v, []);
            found.get(v).push(`${site.kind === 'writer' ? '写' : '声明'}:${site.why}`);
            if (site.kind === 'writer') writers.add(v);
        }
    }
    ok(hits >= 1, `D1[${site.file}] 这条形态**真的命中了**(0 次 = 读法失效而不是"没有值")`,
        `${hits} 处 · ${site.why}`);
}

const backendVals = [...found.keys()].sort();
ok(backendVals.length >= 4, 'D2a 分母自证:后端枚举到的状态不止一两个',
    `${backendVals.length} 个:${backendVals.join(',')}`);

/* ── D3 后端有、前端白名单没有 ⇒ 会被显示成「状态未知」═══════════════ */
{
    const missing = backendVals.filter((v) => !WHITELIST.has(v));
    ok(missing.length === 0,
        'D3 🔴 后端能写出的每个状态,前端白名单里**都有** ——'
        + '少一个就会在界面上显示成「状态未知」,而没有任何东西会报警',
        missing.length ? missing.map((v) => `${v}(${found.get(v).join(' / ')})`).join(' · ')
            : `${backendVals.length} 个全在`);
}

/* ── D4 前端有、后端找不到写入方 ⇒ 要么过期,要么是**有意的预留** ══════ */
{
    /**
     * 🔴 `measured` 是**有意预留**:后端把它列进 `PUBLISH_TERMINAL_STATUSES`
     *    (已经发出去了的终态之一)但**今天没有任何写入方**。
     *    白名单里留着它是为了哪天真有人写时不掉进「状态未知」。
     *    —— 这条例外必须写在这里、被这把闸盯着;不然它就是一条没人复核的猜测。
     */
    const ANTICIPATED = { measured: '后端 PUBLISH_TERMINAL_STATUSES 里有,今天无写入方(有意预留)' };
    const extra = [...WHITELIST].filter((v) => !found.has(v) && !ANTICIPATED[v]);
    ok(extra.length === 0,
        'D4 🔴 前端白名单里没有**凭空多出来**的状态'
        + '(多出来的要么是后端撤了、要么是我当初猜的 —— 两种都要当场说清)',
        extra.length ? extra.join(',') : '没有多余项');
    /* 🔴 预留项要**自己会过期**:哪天真有写入方了,这里提醒把它从预留里删掉。
       判"有没有写入方"看的是 `writers`(写入站点),不是 `found`(含声明)——
       拿声明当写入方,就会一直以为它已经落地了。 */
    for (const [v, whyKept] of Object.entries(ANTICIPATED)) {
        console.log(`  ..   预留项 ${v} — ${whyKept}`
            + `${writers.has(v) ? ' 🔴 后端**已经有写入方**了,把它从预留里删掉' : '(仍无写入方)'}`);
    }
    /* 🔴 这里**故意不写一条 ok()**:`!writers.has(x) || true` 那种恒真断言
       只会在屏幕上多一行绿,而它什么都没验 —— 本仓最不缺的就是这种绿。
       预留项的过期提醒是上面那行 `..`,它归 D4 的读数,不假装是一条判据。 */
}

/* ── D5 桶映射的语义锚:自报值**不许**落进已发布桶 ════════════════════ */
{
    /*
     * 🔴 取值域齐全 ≠ 映射正确。这条钉的是**唯一一处有业务后果**的映射:
     *    `self_reported_unverified` 归到已发布桶,就是替服务商向客户断言
     *    一件我们没有证据的事(后端那段注释写得很清楚:它的全部含义是
     *    「有人说发了,还没核实」)。
     */
    ok(M.bucketOf('self_reported_unverified') !== 'published',
        'D5 🔴 `self_reported_unverified` **不在已发布桶** ——'
        + '后端原话:它的全部含义是「有人说发了,还没核实」',
        `bucket=${M.bucketOf('self_reported_unverified')}`);
    ok(M.bucketOf('published') === 'published' && M.bucketOf('failed') === 'failed',
        'D5a 反向控制:真终态仍映射到对应桶(否则 D5 可以靠"全都不算已发布"作弊)');
}

console.log('');
console.log('后端写入点读数:');
for (const v of backendVals) console.log(`  ${v.padEnd(26)} ← ${found.get(v).join(' / ')}`);

console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
