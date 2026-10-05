#!/usr/bin/env node
/**
 * 结构臂 · WO_273 浏览器插件自助发布退役(Owner 09-23「直接退役。把前端这个 tab 也去掉。」)。
 * 只读源码 + 真调 TypeScript 转译,不起浏览器 —— 在 build 链末尾。D 段(WO_288)另扫 vite build 的产物。行为臂(真 chromium:顶栏无「自助」、
 * `/publish?mode=self` 落代发、历史自助记录仍在、全程零 `/api/extension` 请求)见
 * `test-self-publish-retired-render.mjs`(npm run verify:self-publish-retired:render,进 browser:arms)。
 *
 * S 段 · 真判据(扫真源码):
 *   S0 分母自证:frontend/src 扫到的文件数过下限且含 PublishCenter.tsx —— 否则下面的「0 处」是空话
 *   S1 PublishCenter.tsx 里 'self' / "self" 模式键 = 0 行(与工单 `grep -cE "['\"]self['\"]"` 同一量法:
 *      原始文本逐行数,注释也算 —— 类型联合 / URL 白名单 / setMode / 渲染分支,少摘一处 TS 就抓不到)
 *   S2 frontend/src 全部文件 `/api/extension` = 0 行(那组接口由后端同单删除,前端一处都不许再请求)
 *   S3 frontend/src 全部文件 `extension_authorized` = 0 行(插件授权位前端不再消费;字段留在响应里无妨)
 *   S4 已删文件不许回来:SelfPublishPanel.tsx(0 引用死组件)/ publishSelfAxis.ts(只服务自助面板的判读)
 *   S5 PublishCenter 去掉注释后(真转译,removeComments)界面文字里没有「自助」
 *      —— 源码里剩下的「自助」只许在注释里(退役说明、自助调研的历史注释),不许上屏
 *
 * C 段 · 锁自己的牙(同一组扫描函数,喂合成输入 —— 不改任何文件):
 *   C1 种一处 '/api/extension/x' ⇒ S2 扫描器报 1;种在反引号模板里也报 1
 *   C2 种 `mode === 'self'` ⇒ S1 扫描器报 1;对照:注释里的老链写法 `?mode=self` 报 0(那不是模式键)
 *   C3 种 `<button>自助发布</button>` ⇒ S5 扫描器报命中;对照:同样的字只在 JSX 注释里 ⇒ 不命中
 *      (证明 S5 不是裸 grep —— 裸 grep 会被退役注释本身打红,那样的锁只能靠删注释过关)
 *   C4 对照臂:干净合成源 ⇒ S1/S2/S3 扫描器全报 0
 *
 * 🔴 自曝覆盖不到的形态(这些由行为臂的「全程零 /api/extension 请求」网络锁兜住,不是这里):
 *    ① 拼接构造:'/api/' + 'extension' + … ;② 走变量的 base URL;③ 第三方包里发的请求。
 *
 * 三态退出码:0 全过 / 1 有失败 / 3 判据不可用(文件读不到、TypeScript 取不到 —— 不当绿灯)。
 */
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');
const CENTER_REL = 'pages/Publishing/PublishCenter.tsx';
const MIN_FILES = 400; // 本树 src 下 ts/tsx 远超此数;掉到下限以下说明扫描范围坏了,不是"变干净了"

let failed = 0;
const ok = (m, d = '') => console.log(`  OK   ${m}${d ? ` — ${d}` : ''}`);
const bad = (m, d = '') => { console.log(`  FAIL ${m}${d ? ` — ${d}` : ''}`); failed++; };
const check = (c, m, d = '') => (c ? ok(m, d) : bad(m, d));
function unusable(why, detail) {
    console.log(`FAIL 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 800));
    process.exit(3);
}

let ts;
try { ts = createRequire(import.meta.url)('typescript'); } catch (err) { unusable('typescript 取不到', err); }

/* ── 扫描函数(S 段与 C 段共用同一份,C 段喂合成输入)──────────────────── */
const MODE_KEY = /['"]self['"]/;
const linesMatching = (text, needle) => text.split(/\r?\n/).filter((l) => (typeof needle === 'string' ? l.includes(needle) : needle.test(l))).length;
const countModeKeys = (text) => linesMatching(text, MODE_KEY);
const countExtensionApi = (text) => linesMatching(text, '/api/extension');
const countExtensionAuth = (text) => linesMatching(text, 'extension_authorized');
/**
 * 去注释后的"会上屏/会执行"的代码里有没有「自助」:真转译(removeComments),JSX 文本会变成字符串字面量留下。
 * 🔴 JSX 文本转译出来是 `自助…` 转义(普通字符串字面量却保留原字)—— 不先解码,
 *    `includes('自助')` 对 JSX 永远 false,S5 就是恒绿的空锁(C3 第一版当场抓到的就是这个)。
 */
function uiHasSelfServe(tsxSource) {
    const out = ts.transpileModule(tsxSource, {
        compilerOptions: { jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, removeComments: true },
        fileName: 'x.tsx',
    }).outputText;
    const decoded = out
        .replace(/\\u\{([0-9A-Fa-f]+)\}/g, (_, h) => String.fromCodePoint(parseInt(h, 16)))
        .replace(/\\u([0-9A-Fa-f]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)));
    return decoded.includes('自助');
}

/* ── 分母:frontend/src 下全部源文件(不按"我觉得会有"挑)───────────────── */
const files = [];
(function walk(dir) {
    for (const name of readdirSync(dir)) {
        const p = join(dir, name);
        const st = statSync(p);
        if (st.isDirectory()) walk(p);
        else if (/\.(ts|tsx|js|jsx|mjs|cjs|json|css|html|md)$/.test(name)) files.push(p);
    }
})(SRC);
const rel = (p) => relative(SRC, p).split('\\').join('/');
const centerPath = join(SRC, CENTER_REL);

console.log('S 段 · 真判据');
const codeFiles = files.filter((p) => /\.(ts|tsx)$/.test(p));
check(codeFiles.length >= MIN_FILES && files.some((p) => rel(p) === CENTER_REL),
    'S0 分母自证:frontend/src 扫到的 ts/tsx 过下限且含 PublishCenter.tsx', `${files.length} 个文件(其中 ts/tsx ${codeFiles.length})`);

let center;
try { center = readFileSync(centerPath, 'utf8'); } catch (err) { unusable('PublishCenter.tsx 读不到', err); }
const keyLines = center.split(/\r?\n/).map((l, i) => [i + 1, l]).filter(([, l]) => MODE_KEY.test(l));
check(countModeKeys(center) === 0, "S1 🔴 PublishCenter 里 'self' 模式键 = 0 行(工单同一量法,注释也算)",
    keyLines.length ? keyLines.slice(0, 5).map(([n, l]) => `:${n} ${l.trim().slice(0, 60)}`).join(' | ') : '0 行');

const extHits = [];
const authHits = [];
for (const p of files) {
    const text = readFileSync(p, 'utf8');
    const e = countExtensionApi(text);
    const a = countExtensionAuth(text);
    if (e) extHits.push(`${rel(p)}×${e}`);
    if (a) authHits.push(`${rel(p)}×${a}`);
}
check(extHits.length === 0, 'S2 🔴 frontend/src 全部文件 `/api/extension` = 0 行', extHits.join(', ') || `0 行 / ${files.length} 个文件`);
check(authHits.length === 0, 'S3 frontend/src 全部文件 `extension_authorized` = 0 行', authHits.join(', ') || `0 行 / ${files.length} 个文件`);

/* 每条都写成同行的 !existsSync(…) 断言:scripts/gate_refs_exist.py 按行认「断言不存在」,
   路径放在数组里、判断在下两行时,它会把这两条已删路径当成「引用了不存在的文件」报红。 */
const goneOk = {
    'components/publishing/SelfPublishPanel.tsx': !existsSync(join(SRC, 'components/publishing/SelfPublishPanel.tsx')),
    'pages/Publishing/publishSelfAxis.ts': !existsSync(join(SRC, 'pages/Publishing/publishSelfAxis.ts')),
};
const back = Object.keys(goneOk).filter((g) => !goneOk[g]);
check(back.length === 0, 'S4 已删文件没回来(SelfPublishPanel.tsx / publishSelfAxis.ts)', back.join(', ') || '都不在');

check(!uiHasSelfServe(center), 'S5 🔴 PublishCenter 去注释后(真转译)界面文字里没有「自助」',
    uiHasSelfServe(center) ? '去注释后仍有「自助」' : '只剩注释里有');

console.log('\nC 段 · 锁自己的牙(合成输入,同一组扫描函数)');
check(countExtensionApi("const u = '/api/extension/profiles';") === 1 && countExtensionApi('fetch(`/api/extension/${id}/attest`)') === 1,
    'C1 种 /api/extension(单引号 / 反引号模板)⇒ S2 扫描器各报 1');
check(countModeKeys("if (mode === 'self') return;") === 1 && countModeKeys('// 老链 ?mode=self 落回代发') === 0,
    "C2 种 mode === 'self' ⇒ S1 扫描器报 1;对照:注释里的 ?mode=self 报 0");
check(uiHasSelfServe('export const A = () => <button>自助发布</button>;') === true
    && uiHasSelfServe('export const A = () => <div>{/* 自助发布退役说明 */}<span>代发</span></div>;') === false,
    'C3 种 <button>自助发布</button> ⇒ S5 命中;对照:同样的字只在 JSX 注释里 ⇒ 不命中');
const clean = "export function A() { const mode = 'proxy'; return fetch('/api/meijiehezi/publish-history'); }";
check(countModeKeys(clean) === 0 && countExtensionApi(clean) === 0 && countExtensionAuth(clean) === 0,
    'C4 对照臂:干净合成源 ⇒ S1/S2/S3 扫描器全报 0');

/*
 * D 段 · WO_288:扫**产物**(dist/assets/*.js),不扫源码 —— 用户下载到的是产物。
 *   本闸在 build 链里排在 vite build 之后;dist 不在 = 没法判,rc=3,不当绿灯。
 *   🔴 esbuild 默认把中文转成 \uXXXX,先解码再找,否则「找不到」是解码没做,不是真没有。
 *   D1 帮助中心视频清单里没有已退役的「自助发布 · 用自己的号一键发」(标题 / 视频文件名都不许在)
 *   D2 看板埋点说明不再说「其他自助发布入口尚未打通埋点」
 *   D0 正控(分母自证):同一批产物里找得到活着的邻居 —— V3b 教程标题 + 改后的埋点说明
 *   D3 牙:同一个扫描函数喂合成 chunk(\u 转义的旧标题)⇒ 命中;对照:干净 chunk ⇒ 不命中
 */
const decodeChunk = (s) => s
    .replace(/\\u\{([0-9A-Fa-f]+)\}/g, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/\\u([0-9A-Fa-f]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)));
const RETIRED_COPY = ['用自己的号一键发', 'v3c-self-publish', '其他自助发布入口尚未打通埋点'];
const distHits = (chunks) => RETIRED_COPY.filter((needle) => chunks.some((c) => decodeChunk(c).includes(needle)));
console.log('\nD 段 · 产物里不许再教 / 再提已退役的自助发布(WO_288)');
const ASSETS = join(ROOT, 'dist', 'assets');
if (!existsSync(ASSETS)) unusable('dist/assets 不在 —— 本段扫产物,须排在 vite build 之后');
const chunks = readdirSync(ASSETS).filter((n) => n.endsWith('.js')).map((n) => readFileSync(join(ASSETS, n), 'utf8'));
const all = chunks.map(decodeChunk);
check(chunks.length > 20 && all.some((c) => c.includes('发布文章 · 加购物车')) && all.some((c) => c.includes('发布通道代发自动同步尚未打通埋点')),
    'D0 正控:产物里找得到活着的邻居(V3b 教程标题、改后的埋点说明)—— 否则下面的「没有」是空话', `${chunks.length} 个 js chunk`);
const hits = distHits(chunks);
check(!hits.includes('用自己的号一键发') && !hits.includes('v3c-self-publish'),
    'D1 🔴 帮助中心视频清单里没有已退役的「自助发布」教程', hits.join(' / ') || '0 处');
check(!hits.includes('其他自助发布入口尚未打通埋点'), 'D2 🔴 看板埋点说明不再提「自助发布入口」', hits.join(' / ') || '0 处');
const esc = (s) => [...s].map((ch) => (ch.charCodeAt(0) > 127 ? `\\u${ch.charCodeAt(0).toString(16).padStart(4, '0')}` : ch)).join('');
check(distHits([`const v={title:"${esc('自助发布 · 用自己的号一键发')}"}`]).length === 1 && distHits(['const v={title:"x"}']).length === 0,
    'D3 牙:\\u 转义的旧标题喂进同一个扫描函数 ⇒ 命中;对照:干净 chunk ⇒ 不命中');

console.log('');
if (failed > 0) { console.log(`FAIL ${failed} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
