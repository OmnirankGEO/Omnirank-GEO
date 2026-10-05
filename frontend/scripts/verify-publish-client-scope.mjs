#!/usr/bin/env node
/**
 * 判据 · #180 发布中心跟随左上角「当前客户」,撤页面自带下拉。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 Owner 09-12 图 3:「这两个选择是完全割裂的,应该统一客户选择入口」。
 *    割裂的代价不是"多一个下拉框":发布中心自己维护一份 `selectedProject`,
 *    三个分支都落空时兜底 **`list[0]`** —— 与左上角毫无关系的第一个项目。
 *    于是页面在 A 上工作、左上角显示 B,而提交体的 `brand_id` 取页面这一份:
 *    **「显示对、记错人」**。同一条教训在 `DouyinPostDetail` 的注释里有生产实证:
 *    svideo 3 条里 2 条把内容记到了别的服务商的客户名下,受害双方分属不同服务商。
 *
 * 本文件是 build 链那一半(纯函数真调 + 接线边);
 * 渲染与深链那几条在 `test-publish-client-scope-render.mjs`(真 chromium,不进 build 链)。
 */
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label, detail) => {
    console.log(`${cond ? '  OK ' : '  FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

const blank = (m) => m.replace(/[^\n]/g, ' ');
const decomment = (s) => s
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, blank)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/^\s*\/\/.*$/gm, blank);

const PAGE = 'src/pages/Publishing/PublishCenter.tsx';
const MOD = 'src/pages/Publishing/publishClientScope.ts';

let M;
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(rd(MOD), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const tmp = mkdtempSync(join(tmpdir(), 'a180-'));
    const f = join(tmp, 'scope.mjs');
    writeFileSync(f, js, 'utf8');
    M = await import(pathToFileURL(f).href);
} catch (e) {
    console.log('  FAIL M0 🔴 判定模块加载不了:' + String((e && e.message) || e));
    console.log('       🔴 不要加 SKIP —— 那会把「没跑」伪装成「通过」。');
    process.exit(1);
}
ok(['pickProjectForBrand', 'resolveDeepLinkBrand', 'shouldSwitchClient', 'scopeState']
    .every((k) => typeof M[k] === 'function'), 'M1 四个判定函数都导出了');

const page = decomment(rd(PAGE));
const PROJECTS = [
    { id: 11, brand_id: 101, brand_name: 'A 客户', industry: '教育', quote_ids: [11, 12] },
    { id: 21, brand_id: 202, brand_name: 'B 客户', industry: '医美', quote_ids: [21] },
];

// ══ A 选项目:没有 list[0] 兜底 ═══════════════════════════════════════
console.log('\nA 选项目');
{
    ok(M.pickProjectForBrand(PROJECTS, 202) === 21, 'A1 当前客户 = B ⇒ 选 B 的项目');
    ok(M.pickProjectForBrand(PROJECTS, 101) === 11, 'A2 当前客户 = A ⇒ 选 A 的项目');
    ok(M.pickProjectForBrand(PROJECTS, 999) === null,
        'A3 🔴🔴 当前客户没有写作项目 ⇒ **null,不落 list[0]**。'
        + '老代码那一行兜底正是"显示对、记错人"的来源:左上角选了 B,页面默默在 A 上工作,'
        + '而提交体的 brand_id 取页面这一份');
    ok(M.pickProjectForBrand(PROJECTS, null) === null && M.pickProjectForBrand(PROJECTS, 0) === null,
        'A4 没选客户 ⇒ null(同样不猜)');
    ok(M.pickProjectForBrand([], 101) === null && M.pickProjectForBrand(null, 101) === null,
        'A5 边界:项目列表为空 / 为 null 不抛');
}

// ══ B 深链解析 + 反向同步 ═══════════════════════════════════════════
console.log('\nB 深链');
{
    ok(M.resolveDeepLinkBrand({ preBrandId: 202, projects: PROJECTS }) === 202, 'B1 直接给 brand_id');
    ok(M.resolveDeepLinkBrand({ preQuoteId: 12, projects: PROJECTS }) === 101,
        'B2 给 quote_id ⇒ 反查到它所属项目的 brand');
    ok(M.resolveDeepLinkBrand({ preQuoteId: 21, projects: PROJECTS }) === 202, 'B3 quote_id = 项目 id 也认');
    ok(M.resolveDeepLinkBrand({ projects: PROJECTS }) === null, 'B4 什么都没带 ⇒ null');
    ok(M.resolveDeepLinkBrand({ preQuoteId: 999, projects: PROJECTS }) === null,
        'B5 quote 对不上任何项目 ⇒ null(不猜)');
    ok(M.shouldSwitchClient({ deepLinkBrandId: 202, currentBrandId: 101 }) === true,
        'B6 🔴 深链客户 ≠ 左上角 ⇒ **反向把左上角切过去**('
        + '方向很重要:不是页面自己记一个别的客户,而是让两处一致)');
    ok(M.shouldSwitchClient({ deepLinkBrandId: 202, currentBrandId: 202 }) === false,
        'B7 已经一致 ⇒ 不动(否则每次渲染都 switch 一遍)');
    ok(M.shouldSwitchClient({ deepLinkBrandId: null, currentBrandId: 101 }) === false,
        'B8 深链没带客户 ⇒ 不动(不许把用户当前选的客户冲掉)');
}

// ══ C 四格状态 ═══════════════════════════════════════════════════════
console.log('\nC 四格状态');
{
    const cells = [
        ['全部客户模式', { isAllClientsMode: true, currentBrandId: 101, projects: PROJECTS }, 'all_clients'],
        ['还没选客户', { isAllClientsMode: false, currentBrandId: null, projects: PROJECTS }, 'no_client'],
        ['选了客户但他没有写作项目', { isAllClientsMode: false, currentBrandId: 999, projects: PROJECTS }, 'no_project'],
        ['正常', { isAllClientsMode: false, currentBrandId: 202, projects: PROJECTS }, 'ready'],
    ];
    let allOk = true;
    for (const [name, input, want] of cells) {
        const got = M.scopeState(input);
        const good = got.kind === want
            && (want === 'ready' ? got.projectId === 21 && got.message === ''
                : got.projectId === null && got.message.length > 0);
        console.log(`     ${good ? '✓' : '✗'} ${name} ⇒ ${got.kind} / ${got.projectId} / "${got.message}"`);
        if (!good) allOk = false;
    }
    ok(allOk, 'C1 🔴 四格各自不同,且**非 ready 的三格都有一句话** ——'
        + '页面变空而不说为什么,用户只会以为功能坏了', `${cells.length} 格`);
    ok(/全部客户/.test(M.scopeState({ isAllClientsMode: true, currentBrandId: 101, projects: PROJECTS }).message)
        || /左上角/.test(M.scopeState({ isAllClientsMode: true, currentBrandId: 101, projects: PROJECTS }).message),
        'C2 「全部客户」那格指路去**左上角**(唯一入口)');
    ok(/写文章/.test(M.scopeState({ isAllClientsMode: false, currentBrandId: 999, projects: PROJECTS }).message),
        'C3 「没有可发布文章」那格给的是**下一步**,不是"暂无数据"');
}

// ══ D 接线边 ═════════════════════════════════════════════════════════
console.log('\nD 接线边');
{
    ok(/import \{ useClientContext \} from '@\/context\/ClientContext'/.test(page),
        'D1 页面从 ClientContext 取客户(此前它根本不 import 这个 context)');
    ok(/const selectedProject = clientScope\.projectId;/.test(page),
        'D2 🔴 selectedProject 是**派生值**,不是 state');
    ok(!/setSelectedProject/.test(page),
        'D3 🔴🔴 全文件**一处 setSelectedProject 都没有** —— 有 setter 就意味着'
        + '还存在第二个"当前客户"的真相源');
    ok(!/useState<WritingProject\[\]>\(\[\]\)[\s\S]{0,200}selectedProject, setSelectedProject/.test(page),
        'D4 旧的 selectedProject state 声明已撤');
    ok(!/list\[0\]\.id/.test(page),
        'D5 🔴 `list[0]` 兜底那一行已不在(病根本身)');
    ok(!/publish-project-combobox/.test(page) && !/projectDropdownOpen/.test(page)
        && !/projectSearchQuery/.test(page) && !/projectComboboxRef/.test(page),
        'D6 🔴 页面级下拉整块已撤:combobox、两个 state、那个 ref **和它的点外关闭 effect**'
        + '(只删 JSX 不删 effect 会留一个监听 document mousedown 的死 effect)');
    ok(/shouldSwitchClient\(\{ deepLinkBrandId: deepBrand/.test(page) && /switchClient\(deepBrand as number\)/.test(page),
        'D7 🔴 深链走**反向同步**:把左上角切到深链指的那个客户');
    /**
     * 🔴 D7b 是**实测长出来的**一格:一开始我把反向同步塞在取项目的 `.then` 里,
     *    它只在取项目回来的那一刻跑一次 —— 而那会儿 ClientContext 的品牌列表往往还没到,
     *    `switchClient` 立不住(provider 要等列表确认该品牌仍归属本人),
     *    于是深链**静默无效**。纯函数与接线锁全绿,是浏览器臂把它抓出来的。
     *    所以这里钉两件事:它是**独立 effect**,且依赖里有 `clients.length`。
     */
    const deepAt = page.indexOf('const deepBrand = resolveDeepLinkBrand');
    const deepStart = page.lastIndexOf('useEffect(() =>', deepAt);
    const deepEnd = page.indexOf(']);', deepAt);
    const deepEffect = deepStart >= 0 && deepEnd > deepStart ? page.slice(deepStart, deepEnd + 3) : '';
    ok(deepEffect.startsWith('useEffect(() =>') && /resolveDeepLinkBrand/.test(deepEffect) && !/\.then\(/.test(deepEffect),
        'D7b 🔴 反向同步是**独立 effect**,不寄生在取项目的 .then 里');
    ok(/clients\.length === 0\) return;/.test(deepEffect) && /clients\.length,/.test(deepEffect),
        'D7c 🔴 列表没到时不动,且 `clients.length` 在依赖里 —— 列表一到要再判一次'
        + '(少了这一条深链就是静默无效:switchClient 立不住而没有任何报错)');
    ok(/onGeoBrandResolved=\{\(bid\) => \{[\s\S]{0,400}switchClient\(bid\)/.test(page),
        'D8 🔴 面板回报服务端权威归属时也切左上角 —— 老写法只改页面自己那份,'
        + '左上角仍停在别人身上,于是又回到"两处不一致"');
    ok(/data-testid="publish-current-client"/.test(page),
        'D9 原位留一行只读的当前客户(撤掉一个控件而不说,用户会以为功能丢了)');
    ok(/data-testid="publish-mini-client-picker"/.test(page) && /switchClient\(bid\)/.test(page),
        'D10 「全部客户」那格给迷你选择,且选中即 switchClient(不自建第二份列表)');
    // 下游语义不变:提交体的 brand_id 仍从选中项目取
    // 🔴 [WO_273 · 2026-09-23 重锚] 原锚点 `brand_id: projects.find(p => p.id === selectedProject)?.brand_id`
    //    全文件只有一处 —— 浏览器插件自助发布 `/api/extension/publish` 的提交体,随自助发布整档退役删了
    //    (不重锚,这格只能红着,或被人改成空转)。同一条语义在**现役**代发链路上的落点是决策快照
    //    buildDecisionSnapshotPayload:它的 brand_id 随 /api/publish/decision-snapshots 落库、批量下单按快照扣费,
    //    这才是计费面。三件一起钉:从选中项目取 ∧ 算成 brandId ∧ 真进了快照体。
    const snapAt = page.indexOf('const buildDecisionSnapshotPayload');
    const snapEnd = snapAt >= 0 ? page.indexOf('const handleCartBatchPublish', snapAt) : -1;
    const snap = snapAt >= 0 && snapEnd > snapAt ? page.slice(snapAt, snapEnd) : '';
    ok(/const curProj = projects\.find\(p => p\.id === selectedProject\);/.test(snap)
        && /const brandId = curProj\?\.brand_id \|\|/.test(snap)
        && /brand_id: brandId,/.test(snap),
        'D11 反臂:下游(提交体 brand_id)**只换来源、不改语义** —— 本单不许顺手改计费面'
        + '(WO_273 重锚:现役落点 = 决策快照 buildDecisionSnapshotPayload)');
}

// ══ E 报价流程页(Owner 09-12 扩到两页) ═══════════════════════════════
//
// 🔴 本页这一版**保留**那个下拉、只换真相源。理由不是偷懒:
//    它被教程的 coach mark 罩着(`sandbox_quote_select_brand`,文案「点输入框,
//    在下拉里选…」)。直接删,教程第二步会指着一个不存在的控件 —— 那正是 #176
//    刚修完的那类事故(镜像面被主线改动悄悄弄坏,且是在真人面前)。
//    所以先做"一个真相源、两个视图",撤控件与改教程文案一起排(见交付物 §未评估)。
console.log('');
console.log('E 报价流程页');
{
    const quote = decomment(rd('src/pages/Quote/OnlineQuoteFlow.tsx'));
    ok(/import \{ useClientContext \} from '@\/context\/ClientContext'/.test(quote),
        'E1 报价页也从 ClientContext 取客户');
    ok(/const selectedBrandId = currentBrandId \?\? null;/.test(quote),
        'E2 🔴 `selectedBrandId` 是**派生值** —— 页面不再自己存一份');
    ok(!/const \[selectedBrandId, setSelectedBrandId\] = useState/.test(quote),
        'E3 🔴 旧的 useState 已撤(留着就是第二个真相源)');
    ok(!/setSelectedBrandId\(/.test(quote),
        'E4 🔴🔴 全文件**一处 setSelectedBrandId 都没有**');
    // 定义行是 `const selectBrand = useCallback(`,不含 `selectBrand(` ⇒ 调用点恰好 3 处
    const writes = (quote.match(/selectBrand\(/g) || []).length;
    ok(/const selectBrand = useCallback/.test(quote), 'E5a 有且只有一个写入口 selectBrand');
    /**
     * 🔴 E5 **重锚(#180-B)**:原来钉「**三个**写入点(深链 / 清空 / 选项)」。
     *    撤掉客户下拉之后,「清空」与「选项」两处随控件一起没了,只剩**深链**一处。
     *    数字从 3 变 1 不是变松 —— 这条真正要挡的是
     *    「有写入点绕开 selectBrand、去写页面自己的旧状态」,
     *    而那件事由 **E4(全文件一处 setSelectedBrandId 都没有)** 承担,
     *    E5 只负责"写入口唯一"。所以这里改钉:
     *      · 写入口有且只有一个(`const selectBrand = useCallback`);
     *      · 还剩下的写入点**全部**走它(≥1,且与 E4 合起来即"没有别的路")。
     */
    ok(writes >= 1,
        'E5 🔴 剩下的写入点(撤下拉后只剩深链)仍然过同一个 selectBrand',
        `${writes} 处调用`);
    ok(!/setCurrentBrandId|setSelectedBrandId/.test(quote),
        'E5b 🔴 反臂:没有任何一处绕开它直接写状态'
        + '(E5 的数字会随控件增减而变,这条不会)');
    ok(/shouldSwitchClient\(\{ deepLinkBrandId: bid/.test(quote) && /switchClient\(bid\)/.test(quote),
        'E6 🔴 选客户 = **改左上角**(两处永远一致),且已经一致时不重复 switch');
    /**
     * 🔴 E7 **翻面(#180-B · Review 09-13 裁定 B)**。
     *
     *    它原来是一条**反臂**:要求那个下拉**仍在**,因为撤它会让教程第二步
     *    指着一个不存在的控件。那条反臂写明了撤除的两个前提:
     *    「撤它必须与教程那一步的文案、沙盒客户列表一起改」。
     *
     *    到点了我**先核前提再撤**,不是照着计划做(取证 A_180B_BLOCKER_2026-09-13.md):
     *      · 沙盒态侧栏渲染的是**静态卡**不是选择器(AppSidebar:490)⇒ 前提②不成立;
     *      · ClientContext 当时完全不认沙盒 ⇒ 没有任何东西自动选中教程客户;
     *      · `step2-online-form` 阶段**零消费者** ⇒ 撤了下拉,第二步页内引导归零。
     *    三条都补上之后才撤。所以这一格现在钉的是**撤干净了 + 替代品真的在**,
     *    而不是"不许撤"。意图一次没变:**不许把用户送进一条没有指引的路**。
     */
    ok(!/featureId="sandbox_quote_select_brand"/.test(quote)
        && !/选择客户<\/label>/.test(quote),
        'E7 🔴 页面上那个客户下拉**已撤**(当前客户唯一来源是左上角)');
    ok(/tutorialStage !== 'step2-online-form'/.test(quote),
        'E7b 🔴 第二步的页内 coach mark **有人接**了 —— 撤下拉之前'
        + '`step2-online-form` 这个阶段全仓零消费者,撤了就等于把第二步的指引删光');
    {
        /**
         * 🔴 E7c **改钉真正的机制,而不是我加的那段代码**。
         *
         *    我原来在 ClientContext 里加了一段"沙盒进入即 switchClient(111)",
         *    并让 E7c 钉住它。**注毒把它戳穿了**:把那句 switchClient 删掉,
         *    B1(沙盒态打开 /pricing 当前客户 = 111)**照样绿** ——
         *    因为真正起作用的是:沙盒拦截器对 `/api/client-context/list`
         *    只回**一个**客户(111),ClientContext 既有的自动选中就把它选上了。
         *    我那段是**冗余代码**,而判据指着它 ⇒ 下一个人会以为它是承重的。已删。
         *
         *    🔴 我在 A_180B_BLOCKER 里那条推断也因此是错的:
         *    「ClientContext 完全不认沙盒 ⇒ 没有任何东西自动选中教程客户」——
         *    前半句(没有沙盒代码)是核过的,后半句是**推的**,没测。
         *    撤下拉的前提其实**早就满足**了。(同族:verified-half-a-claim-then-asserted-the-rest)
         */
        const icp = decomment(rd('src/sandbox/sandboxInterceptor.ts'));
        ok(/client-context\/list/.test(icp) && /id: 111/.test(icp),
            'E7c 🔴 沙盒态当前客户的**真实**来源:拦截器对 client-context/list 只回一个客户(111),'
            + 'ClientContext 既有的自动选中把它选上 —— 不是我另加一段代码');
        const ctx = decomment(rd('src/context/ClientContext.tsx'));
        ok(!/SANDBOX_BRAND_ID/.test(ctx),
            'E7d 🔴 反臂:ClientContext 里**没有**我那段冗余的沙盒自动选中 ——'
            + '留着它=判据指着一个不承重的东西,下一个人会照它改');
    }
}

console.log('');
if (bad > 0) {
    console.log(`FAIL ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exitCode = 1;
} else if (notEvaluated > 0) {
    console.log(`未完成:${notEvaluated} 项未评估`);
    process.exitCode = 3;
} else {
    console.log('全部通过');
    process.exitCode = 0;
}
