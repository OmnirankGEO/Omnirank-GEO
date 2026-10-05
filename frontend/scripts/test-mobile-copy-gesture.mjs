/**
 * WO_WHITELABEL_COPY_UX 项2 · 手机复制手势栈锁(2026-08-05)
 *
 * 病灶:iOS Safari / 微信 webview 要求剪贴板写入发生在用户手势的同步调用栈里,
 * 「先 await 接口再 copyToClipboard」在真机必挂,桌面 Chrome 永远测不出来。
 *
 * 两层锁:
 *  A. 功能锁:esbuild 编译真实 copyUtils.ts,在 mock 环境里跑 copyAsyncText,
 *     断言 navigator.clipboard.write 在 getText 还没 resolve 时(=手势同步栈内)就已发起。
 *  B. 静态锁:被迁移的调用点不得再出现「await 接口 → copyToClipboard」旧序,
 *     且失败兜底 ManualCopyDialog 已接线。剥注释后扫描(判据别被自己的注释骗)。
 *
 * 每条"必须命中"配成对的"必须不命中"(反向对照),变异 runner 见
 * mutation_runner_mobile_copy.mjs。
 *
 * ⚠️ 本锁证不了真机行为 —— 上线后仍必须 iOS Safari + 微信内置浏览器各真跑一次(工单硬要求)。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import esbuild from 'esbuild';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND = join(__dirname, '..');

// ---------------- 工具 ----------------

/** 剥 // 与 /* *\/ 注释(粗粒度,足够本锁用;不处理字符串内的伪注释边角) */
export function stripComments(src) {
    return src
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/^\s*\/\/.*$/gm, '')
        .replace(/([^:'"])\/\/[^\n]*/g, '$1');
}

// ---------------- A. 功能锁 ----------------

/**
 * @param {string} copyUtilsSource copyUtils.ts 源码(变异 runner 会传入被改过的版本)
 * @returns {Promise<{failures: string[]}>}
 */
export async function runFunctionalChecks(copyUtilsSource) {
    const failures = [];
    const { code } = await esbuild.transform(copyUtilsSource, { loader: 'ts', format: 'esm' });
    const mod = await import(
        'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
    );
    if (typeof mod.copyAsyncText !== 'function') {
        return { failures: ['copyUtils 未导出 copyAsyncText'] };
    }

    // 每个场景独立 mock 环境
    const setupEnv = ({ withClipboardItem, writeRejects }) => {
        const state = {
            textResolved: false,
            writeCalledBeforeTextResolved: null,
            writeTextCalls: 0,
            getTextCalls: 0,
        };
        globalThis.window = { isSecureContext: true };
        globalThis.document = {
            createElement: () => ({ style: {}, focus() {}, select() {}, value: '' }),
            body: { appendChild() {}, removeChild() {} },
            execCommand: () => false,
        };
        // Node 21+ 的 globalThis.navigator 是只读 getter → 必须 defineProperty 覆盖
        Object.defineProperty(globalThis, 'navigator', {
            configurable: true,
            value: {
                clipboard: {
                    write: async () => {
                        state.writeCalledBeforeTextResolved = !state.textResolved;
                        if (writeRejects) throw new Error('NotAllowedError');
                    },
                    writeText: async () => { state.writeTextCalls += 1; },
                },
            },
        });
        if (withClipboardItem) {
            globalThis.ClipboardItem = class ClipboardItem { constructor(items) { this.items = items; } };
        } else {
            delete globalThis.ClipboardItem;
        }
        globalThis.Blob = globalThis.Blob || class Blob { constructor(parts) { this.parts = parts; } };
        const getText = () => {
            state.getTextCalls += 1;
            return new Promise((resolve) => {
                setTimeout(() => { state.textResolved = true; resolve('https://example.test/share/abc'); }, 10);
            });
        };
        return { state, getText };
    };

    // 场景1(必须命中):Safari 形态 —— 写入在 getText resolve 之前(同步手势栈)发起
    {
        const { state, getText } = setupEnv({ withClipboardItem: true, writeRejects: false });
        const res = await mod.copyAsyncText(getText);
        if (res.ok !== true) failures.push(`场景1:期望 ok=true,得到 ${JSON.stringify(res)}`);
        if (res.text !== 'https://example.test/share/abc') failures.push('场景1:text 不是 getText 的返回值');
        if (state.writeCalledBeforeTextResolved !== true) {
            failures.push('场景1(核心):clipboard.write 不是在 getText resolve 之前发起的 —— 手势栈已丢,真机必挂');
        }
        if (state.getTextCalls !== 1) failures.push(`场景1:getText 被调了 ${state.getTextCalls} 次(应恰好 1 次)`);
    }

    // 场景2(必须命中):Safari 拒了 write → 回落旧路,getText 不重复打
    {
        const { state, getText } = setupEnv({ withClipboardItem: true, writeRejects: true });
        const res = await mod.copyAsyncText(getText);
        if (res.ok !== true) failures.push(`场景2:回落 writeText 应成功,得到 ${JSON.stringify(res)}`);
        if (state.writeTextCalls !== 1) failures.push(`场景2:writeText 应被调 1 次,实际 ${state.writeTextCalls}`);
        if (state.getTextCalls !== 1) failures.push(`场景2:getText 被调了 ${state.getTextCalls} 次(接口重复打 = 重复扣费风险)`);
    }

    // 场景3(必须命中):Chrome 形态(无 ClipboardItem)→ 旧路可用
    {
        const { state, getText } = setupEnv({ withClipboardItem: false, writeRejects: false });
        const res = await mod.copyAsyncText(getText);
        if (res.ok !== true) failures.push(`场景3:Chrome 旧路应成功,得到 ${JSON.stringify(res)}`);
        if (state.writeTextCalls !== 1) failures.push('场景3:应走 writeText 旧路');
    }

    // 场景4(必须命中):接口失败 → ok=false + text=null + errorMessage 透传
    {
        setupEnv({ withClipboardItem: true, writeRejects: false });
        const res = await mod.copyAsyncText(() => Promise.reject(new Error('接口炸了')));
        if (res.ok !== false || res.text !== null) failures.push(`场景4:接口失败应 {ok:false,text:null},得到 ${JSON.stringify(res)}`);
        if (res.errorMessage !== '接口炸了') failures.push('场景4:errorMessage 未透传');
    }

    // 场景5(反向对照 · 必须不命中):证明场景1的判据有判别力 ——
    // 模拟"旧写法"(先 await 文本再写剪贴板):此时 writeCalledBeforeTextResolved 必为 false。
    {
        const { state, getText } = setupEnv({ withClipboardItem: true, writeRejects: false });
        const buggyOrder = async (gt) => {
            const t = await gt();               // 旧写法:先等接口
            await globalThis.navigator.clipboard.write([t]); // 再写剪贴板 → 手势栈已丢
            return { ok: true, text: t };
        };
        await buggyOrder(getText);
        if (state.writeCalledBeforeTextResolved !== false) {
            failures.push('场景5(反向对照):旧写法竟然也被判为"同步栈内" —— 场景1判据恒真,作废');
        }
    }

    return { failures };
}

// ---------------- B. 静态锁 ----------------

// 迁移过的「先 await 接口再复制」调用点:必须用 copyAsyncText,禁旧序
const ASYNC_SITES = [
    'src/pages/Diagnosis/DiagnosisReport.tsx',
    'src/pages/Diagnosis/DiagnosisProgress.tsx',
    'src/pages/Agent/QuotePreview.tsx',
    'src/pages/Reports/index.tsx',
    'src/pages/Brands/MarketingTab.tsx',
    // [开源 E3 · 前端 · 2026-10-01 · WO_322] M3 方案页 · M3 资料邀请卡 · M3 报价管理工具三处随 pages/M3 / components/m3 整删
];
// 失败兜底弹窗必须接线的文件(同上,ManualCopyDialog)
const DIALOG_SITES = ASYNC_SITES;
// 裸 navigator.clipboard 换成带兜底 util 的文件
// [开源 E3 · 前端 · 2026-10-01 · WO_322] 原有的三处 M3 组件(路由隔离件 · 可复制话术 · 门户二维码)随 components/m3 整删,名单只剩 ASYNC_SITES
const RAW_CLIPBOARD_BANNED = [
    ...ASYNC_SITES,
];
// 旧序特征:await 后拿接口返回值直接喂 copyToClipboard(剥注释后扫)。
// ⚠️ 不含 fullUrl:MarketingTab.handleCopyLink 复制的是已在手里的 state(合法同步复制),
//    网络派生的 fullUrl 旧形状由 OLD_FULLURL_RE 单独抓(`${origin}${data.url}` 的拼法)。
const OLD_ORDER_RE = /await\s+copyToClipboard\(\s*(?:data\.(?:share_url|url)|portalUrl|newUrl)\b/;
const OLD_FULLURL_RE = /const\s+fullUrl\s*=\s*`\$\{window\.location\.origin\}\$\{data\.url\}`[\s\S]{0,200}?await\s+copyToClipboard\(\s*fullUrl\b/;

/**
 * @param {(rel: string) => string} readFile 相对 frontend 的读文件函数(变异 runner 可注入改过的内容)
 * @returns {{failures: string[]}}
 */
export function runStaticChecks(readFile) {
    const failures = [];

    for (const rel of ASYNC_SITES) {
        const src = stripComments(readFile(rel));
        if (!src.includes('copyAsyncText(')) failures.push(`${rel}:未使用 copyAsyncText`);
        if (OLD_ORDER_RE.test(src)) failures.push(`${rel}:仍有「await 接口 → copyToClipboard」旧序`);
        if (OLD_FULLURL_RE.test(src)) failures.push(`${rel}:仍有「fetch → fullUrl → copyToClipboard」旧序`);
    }
    for (const rel of DIALOG_SITES) {
        const src = stripComments(readFile(rel));
        if (!src.includes('ManualCopyDialog')) failures.push(`${rel}:复制失败兜底 ManualCopyDialog 未接线`);
    }
    for (const rel of RAW_CLIPBOARD_BANNED) {
        const src = stripComments(readFile(rel));
        if (/navigator\.clipboard\s*\.\s*write/.test(src)) {
            failures.push(`${rel}:仍在裸调 navigator.clipboard(无 execCommand 兜底)`);
        }
    }

    // 项3 静态锁:设置页透明化三件(JSX 字符串,不剥注释也在;剥了更稳)
    {
        const src = readFile('src/pages/Agent/WhitelabelSettings.tsx');
        if (!src.includes('客户目前看到的仍是平台品牌')) {
            failures.push('WhitelabelSettings:保存成功提示缺「已保存但客户还看不到」的直说文案');
        }
        if (!src.includes('FieldSourceTag')) {
            failures.push('WhitelabelSettings:预览缺字段→位置逐处对应标注(FieldSourceTag)');
        }
        if (!src.includes('优先显示产品名')) {
            failures.push('WhitelabelSettings:product_name 缺「客户面优先用它」的归属说明');
        }
    }

    // 反向对照1:判据对旧代码形状必须响 —— 用工单里 DiagnosisReport 的旧片段自测
    {
        const oldSnippet = `
            const res = await authFetch('/api/diagnosis/1/share-link', { method: 'POST' });
            const data = await res.json();
            const ok = await copyToClipboard(data.share_url);
        `;
        if (!OLD_ORDER_RE.test(stripComments(oldSnippet))) {
            failures.push('反向对照1:OLD_ORDER_RE 连工单原始旧代码都抓不到 —— 判据恒绿,作废');
        }
        const oldFullUrlSnippet = [
            'const data = await res.json();',
            'const fullUrl = `${window.location.origin}${data.url}`;',
            'const ok = await copyToClipboard(fullUrl);',
        ].join('\n');
        if (!OLD_FULLURL_RE.test(stripComments(oldFullUrlSnippet))) {
            failures.push('反向对照1b:OLD_FULLURL_RE 抓不到 MarketingTab 旧形状 —— 判据恒绿,作废');
        }
    }
    // 反向对照2:允许保留的同步复制(SharePosterDialog 复制的是已在手里的 state)不该被误伤
    {
        const src = stripComments(readFile('src/components/share/SharePosterDialog.tsx'));
        if (!src.includes('copyToClipboard(')) {
            failures.push('反向对照2:SharePosterDialog 的同步 copyToClipboard 不见了(判据口径可能被改宽)');
        }
    }

    return { failures };
}

// ---------------- main ----------------

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];
if (isMain) {
    const readFile = (rel) => readFileSync(join(FRONTEND, rel), 'utf8');
    const copyUtilsSource = readFile('src/lib/copyUtils.ts');
    const funcRes = await runFunctionalChecks(copyUtilsSource);
    const staticRes = runStaticChecks(readFile);
    const failures = [...funcRes.failures, ...staticRes.failures];
    if (failures.length) {
        console.error(`❌ mobile-copy 锁 ${failures.length} 条失败:`);
        for (const f of failures) console.error('  · ' + f);
        process.exit(1);
    }
    console.log('✅ mobile-copy 锁全绿:功能 5 场景(含反向对照)+ 静态 %d 文件 + 项3 透明化 3 条', ASYNC_SITES.length);
}
