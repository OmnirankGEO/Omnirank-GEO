/**
 * verify-no-backend-refs-in-build-chain.mjs
 *   —— build 链里的脚本一律不许伸手到 frontend/ 之外(WO-LATENT-TRAPS §3 返工 · 2026-08-18)
 *
 * ## 它防的是什么
 *
 * Dockerfile 的 frontend-builder 阶段只 `COPY frontend/ ./` —— **后端源码不在那一层**。
 * 一个引后端文件的锁进了 build 链,只有两种下场:
 *   ① 整个 `npm run build` ENOENT 挂掉;
 *   ② 有人给它加一个「够不到就 SKIP」的分支 —— 于是它**每次构建都大声跳过**,
 *      打印「这不是通过,是没跑」然后 exit 0。构建绿 ≠ 判据过,而大家只看得到构建绿。
 * ② 正是 2026-08-17 清扫掉的那两条裸奔判据(`test-diagnosis-launch-ui.mjs` §1 引 server.py,
 * `test-referral-wiring.mjs` 引 api/referral_api.py)的真实下场。
 *
 * 清掉一次不等于清干净 —— 下一个人照样会再加回来。所以这条闸**留在 build 链里**,
 * 谁再往 build 链的脚本里塞后端引用,**构建当场红**。
 *
 * ## 判据口径
 *
 * 结构锚 = 从 `package.json` 的 `build` 里解析出**全部** `node scripts/*.mjs`,
 * 不写死文件名(只钉那两条会漏第三条)。逐个扫源码,命中任一即红:
 *   · 任何 `.py` 路径字面量        —— Python 文件按定义就是后端
 *   · `../<后端顶层目录>` 字面量   —— 目录级越界(api/ db/ services/ …)
 *
 * 🔴 **先剥注释再判**:本文件和 `test-diagnosis-launch-ui.mjs` 的说明注释里都写着
 *    `server.py` 这几个字。按字面扫会命中**解释它自己的注释** —— 那是 2026-08 反复踩过的形态。
 *    剥注释又带来第二个风险:剥过头把真代码也吃掉 → 判据恒绿。所以下面**两个方向都自证**。
 *
 * 用法(在 frontend/ 下):node scripts/verify-no-backend-refs-in-build-chain.mjs
 */
import { readFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';

const root = process.cwd();
const PKG = path.join(root, 'package.json');

/** 与仓内其它锁同口径:块注释 / 整行 // / 行尾 //(不吃 URL 里的 //) */
function stripComments(src) {
    return src
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/^\s*\/\/.*$/gm, '')
        .replace(/([^:'"])\/\/[^\n]*/g, '$1');
}

const BACKEND_DIRS = [
    'api', 'db', 'services', 'tools', 'writing', 'middleware',
    'auth', 'agents', 'workflows', 'config', 'scripts', 'utils', 'advisors',
];
const PY_LITERAL = /['"`][^'"`\n]*\.py['"`]/g;
const DOTDOT_BACKEND = new RegExp(
    `['"\`]\\.\\./(?:${BACKEND_DIRS.join('|')})(?:/[^'"\`\\n]*)?['"\`]`, 'g');

/** 返回该源码里的越界引用列表(已剥注释)。 */
export function findBackendRefs(rawSource) {
    const src = stripComments(rawSource);
    const hits = new Set();
    for (const m of src.matchAll(PY_LITERAL)) hits.add(m[0]);
    for (const m of src.matchAll(DOTDOT_BACKEND)) hits.add(m[0]);
    return [...hits];
}

function buildChainScripts() {
    const pkg = JSON.parse(readFileSync(PKG, 'utf8'));
    const build = pkg.scripts?.build ?? '';
    return [...build.matchAll(/node (scripts\/[\w\-.]+\.mjs)/g)].map(m => m[1]);
}

// ------------------------------------------------------------------ main
let failed = 0;
const say = (ok, msg) => { if (!ok) failed++; console.log(`  ${ok ? '✅' : '🔴'} ${msg}`); };

console.log('=== 0. 判据可用性(先证判据不是空的,再谈结论)===');
const scripts = buildChainScripts();
say(existsSync(PKG), `读得到 package.json`);
say(scripts.length >= 10, `build 链解析出 ${scripts.length} 个 node 脚本(<10 就近乎空即通过)`);

console.log('=== 1. 探测器判别力自证(两个方向都要证)===');
{
    // 🔴 自测样本一律**运行时拼**,不写成字面量。
    //    第一版写成字面量,结果这条闸把**自己**判红了 —— 它自己的样本就是货真价实的
    //    `.py` 字面量。与 §2 普查把自己的自测样本报成"必修踩中"是同一种自指洞。
    //    正确解法不是"把本文件加进排除名单"(那等于给判据开后门,下一个人加真引用也一起放过),
    //    而是让样本落在扫描口径之外、运行时才成形 —— 探测器拿到的仍是完整字符串,判别力一点没少。
    //    代价:本文件里搜不到字面量样本。收益:本文件和别人受**同一条**规则管,没有豁免。
    const PY = '.' + 'py';
    const AP = '..' + '/api/referral_api' + PY;   // 同理:指向后端目录的相对路径也不写成字面量

    // 正样本:真代码里的后端引用,必须抓到
    const positive = `const SERVER_PY = path.resolve(root, '../server${PY}');\nconst A = join(REPO, 'api', 'referral_api${PY}');`;
    const pHits = findBackendRefs(positive);
    say(pHits.length >= 2, `正样本被抓到 ${pHits.length} 处:${pHits.join(' ')}`);

    // 反向对照①:只出现在**注释**里的,不许命中(否则解释这条闸的注释自己会把构建打红)
    const commentOnly = `// 这行注释提到 '../server${PY}' 和 'referral_api${PY}'\n/* 块注释里也写一次 '${AP}' */\nconst ok = 1;`;
    say(findBackendRefs(commentOnly).length === 0,
        `纯注释里的后端路径不命中(实得 ${findBackendRefs(commentOnly).length} 处)`);

    // 反向对照②:剥注释不许把真代码一起吃掉(否则上一条是"恒绿"换来的)
    const mixed = `// 注释里的 '../server${PY}'\nconst REAL = '${AP}';`;
    const mHits = findBackendRefs(mixed);
    say(mHits.length === 1 && mHits[0].includes(AP),
        `注释与真代码混排时只抓真代码(实得 ${JSON.stringify(mHits)})`);

    // 反向对照③:frontend 内部的合法相对路径不许误报
    const legit = `const P = path.join(__dirname, '..', 'src/pages/Diagnosis/NewDiagnosis.tsx');`;
    say(findBackendRefs(legit).length === 0, `frontend 内部路径不误报`);
}

console.log('=== 2. build 链逐个脚本扫越界引用 ===');
for (const rel of scripts) {
    const p = path.join(root, rel);
    if (!existsSync(p)) { say(false, `${rel} 在 build 链里但文件不存在`); continue; }
    const hits = findBackendRefs(readFileSync(p, 'utf8'));
    say(hits.length === 0, hits.length
        ? `${rel} 伸手到 frontend/ 之外:${hits.join(' ')} —— 它在 frontend-builder 层够不到,` +
          `要么让 build 挂掉,要么被加个 SKIP 分支变成裸奔判据。把这条判据挪到后端 pytest。`
        : `${rel}`);
}

console.log('');
if (failed) {
    console.error(`🔴 build 链越界闸未通过:${failed} 条`);
    process.exit(1);
}
console.log(`✅ build 链越界闸全绿:${scripts.length} 个脚本均未伸手到 frontend/ 之外`);
