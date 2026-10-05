#!/usr/bin/env node
/**
 * 包H 前端静态闸(**纯前端 · 零越界**,已接进 build 链)。
 *
 * ## 为什么跨层那几条不在这里
 *
 * Dockerfile 的 `frontend-builder` 阶段只 `COPY frontend/ ./` ——
 * 后端源码不在那一层,`verify-no-backend-refs-in-build-chain` 会把读后端的
 * 脚本判红(判得对)。所以「前端文案 == 后端 registry」「入口常量前后端一致」
 * 这两条**跨层**判据搬到了后端 pytest(全仓可达):
 *   `tests/defensive_geo_pkgh_2026_08_23/test_pkgh_cross_layer.py`
 * —— 与窗D 把 UI-37 同源判据搬去 pytest 是同一处置。
 *
 * ## 留在这里的:纯前端、能在 build 链里跑
 *
 *   L1 文案三禁:恐吓词 / 工程术语 / 供应商穿透(扫**中文字面量**)
 *   L2 规格逐字句禁硬编码:必须走 DEFGEO_COPY(生成物),不许在 tsx 里再抄一份
 *   L3 禁 inline style(CLAUDE.md 前端设计系统段)
 *   L4 Z-3.3 形态锁:「看 AI 改好的版本」在场时**不许**已经有可编辑输入框
 *   L5 U-9 前缀不在前端拼(【】必须来自服务端下发的 prefix)
 *   L6 移动端 fallback:包H 的页面级组件必须有三档断点
 *   L7 [V5-A] 恢复按钮按 `nextAction.kind` 分发,不按 `error.code` 猜
 *   L8 [V5-A] preview 幂等键两种生命周期(用户重新发起换键 / 网络重试同键)
 *   L9 [V5-A/V5-B] confirm 幂等键跨重试冻结 —— 声明**与调用点**都打
 *   L10 [V5-B] 分档表里的落点必须是 App.tsx 里的**真实路由**(不给点了 404 的按钮)
 *
 * 每条「必须命中」都配一条「必须不命中」;`--selftest` 逐把注毒,
 * 证明它真能转红 —— 没有自证的负向锁与恒绿无法区分。
 * L7 那一发的注毒**逐字复原被测缺陷本体**(把按钮改回按 code 分发的那一行),
 * 不是随便找个地方改坏 —— 锁抓不住缺陷本体就等于挡不住它复发。
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');

const FILES = {
    copy: 'lib/defensiveGeoCopy.ts',
    todoPage: 'pages/DefensivePublish/DeliveryTodoList.tsx',
    todoPure: 'pages/DefensivePublish/deliveryTodo.ts',
    deepLink: 'pages/DefensivePublish/PublishDeepLink.tsx',
    entryGate: 'pages/DefensivePublish/publishEntryGate.ts',
    links: 'components/defensiveGeo/CustomerLinksPanel.tsx',
    facts: 'components/defensiveGeo/FactAutofillCard.tsx',
    legal: 'components/defensiveGeo/LegalRepairPanel.tsx',
    progress: 'components/defensiveGeo/CommercialProgressBar.tsx',
    launch: 'components/defensiveGeo/LaunchPanel.tsx',
    // [V5-B · P2-NEW-5] 分档规则(纯逻辑)与它要落到的真实路由表
    nextAction: 'components/defensiveGeo/nextActionRoute.ts',
    app: 'App.tsx',
};

// 🔴 取不到源文件时**明说「判据不可用」**并 exit 1,不要抛一个 ENOENT 栈。
//    [V5-B] 红臂实测:把 nextActionRoute.ts 退回底(那时它还不存在)之后,
//    这道闸是**崩**的 —— 一个 Node 栈。崩溃与「跑完全红」长得不一样,
//    读的人第一反应会是"脚本坏了",而不是"这一版缺东西"。
//    同一条纪律在浏览器那套里早就有(unusable()),这里补上。
const read = (rel) => {
    try {
        return readFileSync(join(SRC, rel), 'utf8');
    } catch (err) {
        console.log(`\u{1F534} 判据不可用(不当绿灯):读不到 src/${rel} —— ${err.code || err.message}`);
        process.exit(1);
    }
};

/** 只留代码,去掉注释 —— 注释里引规格原文不该判红(本仓记过这一条)。 */
function stripComments(text) {
    return text
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .split('\n')
        .filter((l) => !l.trim().startsWith('//'))
        .join('\n');
}

/** 抽出所有含中文的字面量(单/双引号 + JSX 文本节点粗抽)。 */
function chineseLiterals(code) {
    const out = [];
    const quoted = code.match(/(['"`])(?:(?!\1)[^\\]|\\.)*\1/g) || [];
    for (const q of quoted) if (/[一-龥]/.test(q)) out.push(q.slice(1, -1));
    // JSX 文本:`>中文...<`
    const jsx = code.match(/>[^<>{}]*[一-龥][^<>{}]*</g) || [];
    for (const j of jsx) out.push(j.slice(1, -1).trim());
    return out;
}

const SCARY = ['错误', '异常', '崩溃', '严重', '警告', '禁止', '非法', '无效'];
const ENGINEERING = ['snapshot', 'revision', 'idempotency', 'canonical', 'runState',
    'fundingState', 'planItemKey', 'commandId', 'previewId', 'token'];
const VENDORS = ['qwen', 'deepseek', 'kimi', '豆包', 'doubao', 'openrouter', 'dashscope', '通义'];

/** 规格逐字句 —— 这些**只能**从 DEFGEO_COPY 取,不许在 tsx 里再抄一份。 */
// 🔴 只放**整句/整标签**,不放词片。第一版放了「重新签发」这个片段,
//    结果把面板里那句合法的失败态文案(「这次没能重新签发；没有扣除任何算力…」)
//    也判红了 —— 词片锁抓的是词,不是"有没有第二份 SSOT"。
const SPEC_SENTENCES = [
    '价格没变，直接确认',
    '重新签发一个新链接',
    '原媒体临时没档期，已经自动换成同角色',
    '这句话可能违反广告法（已标出位置）',
    '每一项各自确认、各自计费，不会一次性全扣',
];

const failures = [];
const fail = (id, msg) => failures.push(`${id}: ${msg}`);

function run(sources) {
    failures.length = 0;
    const uiFiles = ['todoPage', 'deepLink', 'links', 'facts', 'legal', 'progress', 'launch'];

    // ── L1 文案三禁 ────────────────────────────────────────────────
    for (const key of uiFiles) {
        const code = stripComments(sources[key]);
        for (const lit of chineseLiterals(code)) {
            const low = lit.toLowerCase();
            const scary = SCARY.filter((w) => lit.includes(w));
            const eng = ENGINEERING.filter((w) => low.includes(w.toLowerCase()));
            const ven = VENDORS.filter((w) => low.includes(w.toLowerCase()));
            if (scary.length) fail('L1/scary', `${key} 文案含恐吓词 ${scary.join('/')}:${lit.slice(0, 40)}`);
            if (eng.length) fail('L1/engineering', `${key} 文案含工程术语 ${eng.join('/')}:${lit.slice(0, 40)}`);
            if (ven.length) fail('L1/vendor', `${key} 文案供应商穿透 ${ven.join('/')}:${lit.slice(0, 40)}`);
        }
    }

    // ── L2 规格逐字句必须走 registry 生成物 ────────────────────────
    for (const key of uiFiles) {
        const code = stripComments(sources[key]);
        for (const s of SPEC_SENTENCES) {
            if (code.includes(s)) {
                fail('L2/hardcoded-copy',
                    `${key} 里硬编码了规格逐字句「${s}」—— 必须走 DEFGEO_COPY(单一 SSOT)`);
            }
        }
    }

    // ── L3 禁 inline style ─────────────────────────────────────────
    for (const key of uiFiles) {
        if (/style=\{\{/.test(stripComments(sources[key]))) {
            fail('L3/inline-style', `${key} 用了 inline style(CLAUDE.md 前端设计系统段禁止)`);
        }
    }

    // ── L4 Z-3.3 形态锁:第一颗按钮不是编辑器 ───────────────────────
    {
        const code = stripComments(sources.legal);
        if (!/legal-repair-generate/.test(code)) {
            fail('L4/no-generate', '广告法面板没有「看 AI 改好的版本」这颗按钮 —— 退回纯手工文本框了');
        }
        // 编辑器只允许出现在 picked 分支里。粗判:Textarea 之前必须先出现
        // `phase === 'picked'`,否则就是"一上来就给空输入框"。
        const ta = code.indexOf('<Textarea');
        const picked = code.indexOf("phase === 'picked'");
        if (ta !== -1 && (picked === -1 || picked > ta)) {
            fail('L4/editor-first',
                '可编辑输入框出现在「选中一条候选」之前 —— Z-3.3 明令禁止实现成纯手工文本框');
        }
    }

    // ── L5 U-9 前缀不在前端拼 ──────────────────────────────────────
    {
        const code = stripComments(sources.links);
        if (/【/.test(code)) {
            fail('L5/prefix-hardcoded',
                '客户链接面板里出现了【 —— 人话前缀必须用服务端下发的 prefix,不在前端拼第二份');
        }
        if (!/link\.prefix/.test(code)) {
            fail('L5/prefix-missing', '没有渲染服务端下发的 prefix(判据活性)');
        }
    }

    // ── L6 移动端 fallback 三档 ────────────────────────────────────
    for (const key of ['todoPage', 'links', 'facts', 'legal', 'progress']) {
        if (!/\blg:/.test(sources[key])) {
            fail('L6/breakpoint', `${key} 没有 lg: 断点 —— PC 优先 + 移动 fallback 三档(CLAUDE.md)`);
        }
    }

    // ── L7 [工单 V5-A · P2-1] 恢复按钮按 nextAction.kind 分发,不按 code 猜 ──
    //
    // 被测缺陷:`onClick={error.code === 'PREVIEW_EXPIRED' ? () => runPreview() : confirm}`。
    // 后端给 inactive pricing 返的是 503 POLICY_UNAVAILABLE + nextAction.kind=new_preview,
    // 而这颗按钮**确定性地**再点一次 confirm —— 同一个 503,一万次都一样。
    // 锁的范围刻意收在这颗按钮那一段:`error.code === 'PREVIEW_EXPIRED'` 在
    // confirm() 的 catch 里是**合法**用法(那里是自动重建,不是按钮分发)。
    {
        const code = stripComments(sources.launch);
        const at = code.indexOf('error.nextActionLabel');
        if (at === -1) {
            fail('L7/anchor', 'LaunchPanel 里找不到 error.nextActionLabel —— 锚点过期,L7 无处可打');
        } else {
            const region = code.slice(at, at + 1200);
            if (/error\.code\s*===/.test(region)) {
                fail('L7/code-dispatch',
                    '恢复按钮仍按 error.code 分发 —— 换一个新错误码就会再错一次;要按 nextAction.kind');
            }
            // [V5-B · P2-NEW-5] 分档规则搬进了纯逻辑模块。组件里只按结果渲染,
            // 不许再写第二份 kind 判断 —— 两份判断迟早分叉,而分叉的那一份没人验。
            if (!/planNextAction\(/.test(region)) {
                fail('L7/kind-missing',
                    '恢复按钮没有走 planNextAction 分档 —— 它是"点了会去哪"的唯一判断处');
            }
            if (/nextAction\?\.kind\s*===\s*'/.test(region)) {
                fail('L7/second-dispatch',
                    '组件里又出现了一份 kind 字面量判断 —— 分档规则只能有一处(nextActionRoute.ts)');
            }
        }
    }

    // ── L8 [工单 V5-A · P2-1] preview 幂等键的两种生命周期 ─────────
    //
    // 「用户重新发起」必须换新键:沿用旧键会命中后端幂等唯一约束并回读**原来那条**
    // (已过期/已作废的 preview),于是"重新预览"永远重放旧的那一份。
    // 「网络重试」必须保持同键:换键 = 同一次预览在后端变成两条命令。
    {
        const code = stripComments(sources.launch);
        if (!/const\s*\[\s*previewIdempotencyKey\s*,\s*setPreviewIdempotencyKey\s*\]/.test(code)) {
            fail('L8/frozen-preview-key',
                'preview 幂等键没有 setter —— 用户重新发起时换不了键,只会重放那条旧 preview');
        }
        if ((code.match(/newLogical/g) || []).length < 2) {
            fail('L8/no-logical-flag',
                'runPreview 没有区分「用户重新发起」与「网络重试」两种键生命周期');
        }
    }

    // ── L9 confirm 键必须**仍然**冻结(G4 存量,不许被 L8 顺手改坏)─────
    //
    // 🔴 [V5-B · Codex fix-of-fix3 P3-NEW-1] 光看 state 声明不够。
    //    Codex 的反例:把 confirm 的**调用点**改成每次现算一把新键,
    //    声明还在那儿,静态闸与真 Chromium 都仍然 rc=0。
    //    所以这里加打**调用点**:传进 confirmRunPreview 的必须是那个 state 变量本身。
    {
        const code = stripComments(sources.launch);
        if (!/const\s*\[\s*idempotencyKey\s*\]\s*=\s*useState\(\s*newIdempotencyKey\s*\)/.test(code)) {
            fail('L9/confirm-key-unstable',
                'confirm 的幂等键不再是跨重试冻结的 —— 每次重试都会变成一条新命令(G4 回归)');
        }
        const call = code.match(/confirmRunPreview\(([\s\S]{0,200}?)\)/);
        if (!call) {
            fail('L9/anchor', 'LaunchPanel 里找不到 confirmRunPreview 调用 —— 锚点过期');
        } else {
            if (/newIdempotencyKey\s*\(/.test(call[1])) {
                fail('L9/confirm-key-at-call-site',
                    'confirm 调用点现算了一把新键 —— 每次重试都是一条新命令,'
                    + '而 state 里那个冻结的声明还在,光看声明看不出来');
            }
            if (!/\bidempotencyKey\b/.test(call[1])) {
                fail('L9/confirm-key-not-passed',
                    'confirm 调用点没有把那把冻结的键传进去');
            }
        }
    }

    // ── L10 [V5-B · P2-NEW-5] KIND_ROUTES 里的落点必须是**站内真实路由** ──
    //
    // 给一颗点了 404 的按钮,比不给按钮更糟:她会以为自己操作错了。
    // 所以每一个被我们"接上路由"的动作,落点都要能在 App.tsx 里找到对应的 <Route>。
    {
        const routes = stripComments(sources.nextAction);
        const app = sources.app;
        const declared = [...routes.matchAll(/:\s*'(\/[a-z0-9/_-]+)'/g)].map((m) => m[1]);
        const viaConst = /CONTACT_ACTION\.target/.test(routes);
        if (!declared.length && !viaConst) {
            fail('L10/no-routes', 'KIND_ROUTES 一个落点都没有 —— 判据活性(它应当至少接上 /wallet)');
        }
        for (const href of declared) {
            const seg = href.replace(/^\//, '');
            const re = new RegExp(`path=["']/?${seg}["']`);
            if (!re.test(app)) {
                fail('L10/dead-route',
                    `nextActionRoute 把动作接到了 ${href},但 App.tsx 里没有这条路由 —— 点了会 404`);
            }
        }
        // 配对的必须不命中:组织预算那一档**刻意**不接个人钱包。
        if (/request_budget_approval\s*:/.test(routes)) {
            fail('L10/org-to-personal-wallet',
                'request_budget_approval 被接上了路由 —— 服务端给它的 target 是个人钱包页,'
                + '把组织成员导过去等于让他自己掏钱替组织付');
        }
    }

    // ── 生成物身份:defensiveGeoCopy.ts 必须自带"别手改"的抬头 ──────
    if (!/机械生成/.test(sources.copy)) {
        fail('GEN/header', 'defensiveGeoCopy.ts 丢了"机械生成,不要手改"的抬头');
    }

    return failures.slice();
}

function loadAll() {
    const out = {};
    for (const [k, rel] of Object.entries(FILES)) out[k] = read(rel);
    return out;
}

const sources = loadAll();
const real = run(sources);

// ── --selftest:逐把注毒,证明每条锁真能转红 ───────────────────────
if (process.argv.includes('--selftest')) {
    const POISONS = [
        // 🔴 注毒必须落在**提取器看得见**的地方。第一版打在 JSX 文本
        //    `还有 {summary.countFromItems} 项…` 上 —— 那段含 `{`,
        //    chineseLiterals 的 JSX 分支刻意跳过它,于是"注了毒但锁看不见"。
        ['L1/scary', 'todoPage',
            (s) => s.replace('这一单的待办清单这次没能取出来', '这一单的待办清单出现错误')],
        ['L1/vendor', 'links', (s) => s.replace('正在取链接', '正在从豆包取链接')],
        ['L2/hardcoded-copy', 'launch',
            (s) => s.replace('DEFGEO_COPY.confirmUnchangedPrice', "'价格没变，直接确认'")],
        ['L3/inline-style', 'progress', (s) => s.replace('<ol className=', '<ol style={{gap:4}} className=')],
        ['L4/editor-first', 'legal',
            (s) => s.replace("{phase === 'picked' && (", '{true && (')],
        ['L5/prefix-hardcoded', 'links',
            (s) => s.replace('{link.prefix}', "{`【${link.kindLabel}】`}")],
        ['L6/breakpoint', 'progress', (s) => s.split('lg:').join('xx-')],
        // 🔴 [V5-A] L7 的注毒 = 把恢复按钮**退回**改动前那一行(逐字复原缺陷)。
        //    这不是随便找个地方改坏,是把被测缺陷本体放回去 ——
        //    锁抓不住它就说明这条锁根本挡不住这个 finding 复发。
        // 锚点随组件形态更新过一次:V5-B 把分档改成了 IIFE。
        // 上一版锚点失配时 selftest 报的是「注毒锚点没命中 —— 这一发**没跑**」,
        // 不是悄悄绿 —— 那正是这个自证机制该有的样子。
        ['L7/code-dispatch', 'launch', (s) => s.replace(
            /\{error\.nextActionLabel && \(\(\) => \{[\s\S]*?\n                        \}\)\(\)\}/,
            `{error.nextActionLabel && (
                            <Button type="button" size="sm" variant="outline"
                                    onClick={error.code === 'PREVIEW_EXPIRED' ? () => runPreview() : confirm}>
                                {error.nextActionLabel}
                            </Button>
                        )}`)],
        // [V5-B] 组件里再写一份 kind 判断 —— 两份规则迟早分叉,分叉那份没人验。
        ['L7/second-dispatch', 'launch', (s) => s.replace(
            'const plan = planNextAction(error.envelope.nextAction);',
            "const plan = planNextAction(error.envelope.nextAction);\n"
            + "                            if (error.envelope.nextAction?.kind === 'top_up') return null;")],
        ['L8/frozen-preview-key', 'launch', (s) => s.replace(
            'const [previewIdempotencyKey, setPreviewIdempotencyKey] = useState(newIdempotencyKey);',
            'const [previewIdempotencyKey] = useState(newIdempotencyKey);')],
        ['L8/no-logical-flag', 'launch', (s) => s.split('newLogical').join('someOtherFlag')],
        ['L9/confirm-key-unstable', 'launch', (s) => s.replace(
            'const [idempotencyKey] = useState(newIdempotencyKey);',
            'const [idempotencyKey, setIdempotencyKey] = useState(newIdempotencyKey);')],
        // 🔴 [V5-B · P3-NEW-1] Codex 的反例逐字复原:**调用点**现算一把新键,
        //    state 里那句冻结声明原样留着 —— 上一版只看声明,所以这一发是绿的。
        ['L9/confirm-key-at-call-site', 'launch', (s) => s.replace(
            'preview.previewId, preview.canonicalHash, idempotencyKey);',
            'preview.previewId, preview.canonicalHash, newIdempotencyKey());')],
        // [V5-B · P2-NEW-5] 把动作接到一条**不存在**的路由上 —— 点了 404。
        ['L10/dead-route', 'nextAction', (s) => s.replace(
            "    top_up: '/wallet',", "    top_up: '/no-such-page',")],
        // 配对的必须不命中:组织预算被接上个人钱包 —— 让成员自己掏钱替组织付。
        ['L10/org-to-personal-wallet', 'nextAction', (s) => s.replace(
            "    top_up: '/wallet',",
            "    top_up: '/wallet',\n    request_budget_approval: '/wallet',")],
        ['GEN/header', 'copy', (s) => s.replace('机械生成', '手写')],
    ];
    let bad = 0;
    console.log('=== --selftest:逐把注毒 ===');
    for (const [id, key, mutate] of POISONS) {
        const poisoned = { ...sources, [key]: mutate(sources[key]) };
        if (poisoned[key] === sources[key]) {
            console.log(`  🔴 ${id} 注毒锚点没命中 —— 这一发**没跑**`);
            bad++;
            continue;
        }
        const got = run(poisoned);
        const hit = got.some((f) => f.startsWith(id));
        console.log(hit ? `  ✅ ${id} 注毒转红` : `  🔴 ${id} 注毒后仍绿(恒绿锁)`);
        if (!hit) bad++;
    }
    // 还原真实结果
    run(sources);
    if (bad) {
        console.log(`\n🔴 selftest 未通过:${bad} 把锁没有判别力`);
        process.exit(1);
    }
    console.log('\n✅ selftest 通过:每把锁都能转红');
}

if (real.length) {
    console.log('🔴 包H 前端静态闸未通过:');
    for (const f of real) console.log(`  · ${f}`);
    process.exit(1);
}
console.log(`✅ 包H 前端静态闸通过(${Object.keys(FILES).length} 个文件 · 10 类锁)`);
process.exit(0);
