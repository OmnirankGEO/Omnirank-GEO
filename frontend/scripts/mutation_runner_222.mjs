#!/usr/bin/env node
/**
 * 注毒自证 · #222 a1b「普通账号权限 = 服务商(除经营后台)」。
 *
 * 🔴 **开跑前后各断言一次 HEAD 与 dirty=0**(Review 09-15 要求,起因是我上一轮的事故:
 *    工具超时把前台命令挪到后台,我把那条通知读成「跑完了」,一发毒留在了工作树里。
 *    它的痕迹极难发现 —— 没有 .bak 残留,`git status` 只显示 M(那个文件本来就在改动里),
 *    运行器最后一行是「毒已落地」,读起来像正常进度而不是中断。
 *    ⇒ 不能靠"记得别中途 kill",要靠**跑完时自己核一遍**。)
 *
 * 别与 build / 其他渲染闸并行(毒在工作树里)。
 */
import { execFileSync, execSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX , proveGuardHasTeeth } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const abs = (rel) => join(ROOT, rel);
const sha = (p) => createHash('sha256').update(readFileSync(p)).digest('hex');
const say = (m) => console.log(m);
let failures = 0;

/* ── 树自证:HEAD + dirty 清单 ─────────────────────────────────── */
function treeState() {
    const head = execSync('git rev-parse HEAD', { cwd: REPO, encoding: 'utf8' }).trim();
    const dirty = execSync('git status --porcelain', { cwd: REPO, encoding: 'utf8' })
        .split('\n').map((s) => s.trim()).filter(Boolean).sort().join('|');
    return { head, dirty };
}
const BEFORE = treeState();
say(`开跑前 HEAD=${BEFORE.head.slice(0, 9)} · dirty ${BEFORE.dirty ? BEFORE.dirty.split('|').length + ' 项' : '0 项'}`);

const GATES = {
    s: 'scripts/verify-normal-account-parity.mjs',
    r: 'scripts/test-normal-account-parity-render.mjs',
};

function run(gateRel) {
    try {
        const out = execFileSync(process.execPath, [gateRel], { cwd: ROOT, encoding: 'utf8' });
        return { rc: 0, red: [...out.matchAll(/^\s*FAIL\s+([A-Z]\d+\w*(?:-\S+)?)/gm)].map((m) => m[1]) };
    } catch (e) {
        const out = String(e.stdout || '') + String(e.stderr || '');
        return {
            rc: e.status === undefined ? -1 : e.status,
            red: [...out.matchAll(/^\s*FAIL\s+([A-Z]\d+\w*(?:-\S+)?)/gm)].map((m) => m[1]),
            crashed: /SyntaxError|不可用/.test(out),
        };
    }
}

const CARDS = 'src/pages/Monitoring/components/ActionCards.tsx';
const WELCOME = 'src/components/onboarding/WelcomeChoiceModal.tsx';
const ONBOARD = 'src/components/onboarding/OnboardingWelcomeModal.tsx';
const LAYOUT = 'src/components/layout/Layout.tsx';
const APP = 'src/App.tsx';
const SIDE = 'src/components/layout/AppSidebar.tsx';
const HELPC = 'src/pages/Help/HelpCenter.tsx';
const DOCS = 'src/pages/Help/docs-data.ts';
const HALL = 'src/pages/Writing/WritingHall.tsx';
const QPREV = 'src/pages/Agent/QuotePreview.tsx';

const POISONS = [
    {
        /* 轴:结构 + 行为。把交付工具重新挡回去 = 本单白做。 */
        id: 'Q1', file: CARDS,
        why: '「操作日志」又按身份挡起来(而它给非 admin 看的本来就只有自己的操作)',
        from: '                        <ActionTile icon={ClipboardList}',
        to: '                        {dangerousOrAdmin && <ActionTile icon={ClipboardList}',
        also: [{ from: ' onClick={onLogs} />', to: ' onClick={onLogs} />}' }],
        expectRed: { s: ['R1b-操作日志'], r: ['N1b-操作日志'] },
    },
    {
        /*
         * 🔴 [a1b' 2026-09-16] 这一发**翻面了**。
         *    上一版的毒是「把数据回退也放开」,期望 R1c/N1c 红 ——
         *    Owner 09-16 点头之后,那个「毒」正是**期望态**,继续留着等于拿期望态当缺陷。
         *    现在毒的是反方向:把它重新挡回去。
         *    毒表跟着裁定翻面,不能只翻判据不翻毒 —— 那样毒表会在下一轮悄悄全绿。
         */
        id: 'Q2', file: CARDS,
        why: '[翻面]「数据回退」又被挡回去 —— Owner 已放开,挡回去就是回退',
        from: '                        <ActionTile icon={RotateCcw}',
        to: '                        {isAdmin && <ActionTile icon={RotateCcw}',
        also: [{ from: ' onClick={onRollback} />', to: ' onClick={onRollback} />}' }],
        /* 🔴 期望红只写 N1c:这一发用 `isAdmin` 挡,而 N2b/N3b 量的是 **L0 与服务商的差**
              —— admin 闸对两种身份**同时**生效,差集仍为空,它俩看不见这一发。
              第一版我把 N2b/N3b 也写进期望红,报「没红」—— 那是我把期望写宽了,
              不是判据没牙。期望红要按「这一发**改变了谁看到的东西**」来写。 */
        expectRed: { s: ['R1c'], r: ['N1c'] },
    },
    {
        /* 🔴 反向毒仍然要有:「对齐」很容易滑成「全开」。
              仅 admin 的三样(加词/清数据/归档)是**系统参数**,放开它们是另一种错。 */
        id: 'Q2b', file: CARDS,
        why: '[反向] 仅 admin 的「清除数据」也放开 —— 对齐滑成全开',
        from: '                        {adminOnly && (\n                            <ActionTile icon={ShieldAlert}',
        to: '                        {true && (\n                            <ActionTile icon={ShieldAlert}',
        expectRed: { s: ['R1c2'] },
    },
    {
        /* 轴:纯数字。放开了卡却不改计数,按钮上的数字就是错的。 */
        id: 'Q3', file: CARDS,
        why: '「更多工具」的计数没跟着可见性走',
        from: '        + 2 /* 媒体投放 + 操作日志 —— [#222 a1b] 全员可见 */',
        to: '        + 0 /* 媒体投放 + 操作日志 —— [#222 a1b] 全员可见 */',
        expectRed: { s: ['R1f'], r: ['N3a'] },
    },
    {
        /* 轴:结构。引导又只给一部分人看。 */
        id: 'Q4', file: WELCOME,
        why: '新手引导又加回 `&& isAgent` —— 普通账号重新看不到引导',
        from: '        && !!user\n        && !isSandbox',
        to: '        && !!user\n        && (user?.agent_level ?? 0) >= 1\n        && !isSandbox',
        expectRed: { s: ['R2a-WelcomeChoiceModal'] },
    },
    {
        /* 轴:文案。对普通账号断言他是服务商。 */
        id: 'Q5', file: WELCOME,
        why: '标题又对读者断言身份(原文就是「…服务商工作台」)',
        from: '                    欢迎使用全域上榜 GEO 交付系统',
        to: '                    欢迎使用全域上榜 GEO 交付系统服务商工作台',
        expectRed: { s: ['R2b'] },
    },
    {
        /*
         * 轴:结构。**经营后台被顺手放开** —— 这是本单最该挡住的反向错误。
         */
        id: 'Q6', file: 'src/pages/Agent/ProfitDashboard.tsx',
        why: '经营总览/结算面的 AgentLevelGate 被撤 —— 经营后台不再是例外',
        from: '    <AgentLevelGate requiredLevel={1}>',
        to: '    <>',
        also: [{ from: '    </AgentLevelGate>', to: '    </>' }],
        expectRed: { s: ['R3b'] },
    },
    {
        /* 轴:文案/事实。那句假话又回来。 */
        id: 'Q7', file: LAYOUT,
        why: '那句「Dashboard 内部已对 agent_level<1 隐藏…」又被写回去',
        from: '   * 🔴 [#222 a1b] 这里原来还有一句话,声称 Dashboard 内部按 agent_level 做过裁剪、',
        to: '   * Dashboard 内部已对 agent_level<1 隐藏社媒/客户CRM等板块\n'
            + '   * 🔴 [#222 a1b] 这里原来还有一句话,声称 Dashboard 内部按 agent_level 做过裁剪、',
        expectRed: { s: ['R4a'] },
    },
    {
        /* 轴:结构。另一个引导弹窗也要守住(同一命题两处,漏一处等于没守)。 */
        id: 'Q8', file: ONBOARD,
        why: '另一个引导弹窗加回身份条件',
        from: '    && !!user\n    && typeof window',
        to: '    && !!user\n    && (user?.agent_level ?? 0) >= 1\n    && typeof window',
        expectRed: { s: ['R2a-OnboardingWelcomeModal'] },
    },

    /* ═══ a1b' / a1b'' 新增 ═══
     * 🔴 这一批毒**从抬头承诺的每一样来**,不是从「我已经写了哪些判据」倒推。
     *    上一次(#220 a1'')我那 20 发全是按自己的判据反推的,结果复审两发一打就穿。
     */
    {
        /* 🔴 「改道」和「有守卫」是两件事:否决页也算有守卫,但它把有书签的人扔进死胡同。 */
        id: 'Q9', file: APP,
        why: '[钱包] /wallet 改成落否决页(而不是改道零售面)—— 书签死胡同回来了',
        from: '<AgentLevelGate requiredLevel={1} fallback={<Navigate to="/customer/wallet" replace />}>',
        to: '<AgentLevelGate requiredLevel={1}>',
        expectRed: { s: ['W1'] },
    },
    {
        id: 'Q10', file: SIDE,
        why: '[钱包] 服务商钱包面又挂回普通用户菜单 —— 路由关着、菜单留着 = 死链',
        from: "      ORGANIZATION_ITEM,\n      /* 🔴 [#222 a1b'] 这里原来有 WALLET_ITEM",
        to: "      ORGANIZATION_ITEM,\n      WALLET_ITEM,\n      /* 🔴 [#222 a1b'] 这里原来有 WALLET_ITEM",
        expectRed: { s: ['W2'] },
    },
    {
        /* 🔴 这一发盯的是「别关成两边都没有」—— 本单最坏的失败模式 */
        id: 'Q11', file: APP,
        why: '[钱包] 零售面也一起关掉 —— 普通账号从此看不到自己的钱包',
        from: '<Route path="customer/wallet" element={<CustomerCreditWallet />} />',
        to: '<Route path="customer/wallet" element={<ProtectedRoute requiresAgent><CustomerCreditWallet /></ProtectedRoute>} />',
        expectRed: { s: ['W3'] },
    },
    {
        id: 'Q12', file: HELPC,
        why: '[帮助] 视频清单又按身份过滤 —— 普通账号重新看不到教他用自己功能的那几个',
        from: '  const visibleVideos = videos\n',
        to: "  const visibleVideos = videos.filter((v) => v.audience === 'all')\n",
        /* 🔴 期望红是 **H1b** 不是 H1:这一发一个 `isAgent` 都没出现,
              而 H1 钉的正是那个**名字**。钉名字的格看不见「换个写法做同一件事」,
              钉命题的格才看得见 —— 这一发就是用来把这个区别打出来的。 */
        expectRed: { s: ['H1b'] },
    },
    {
        /* 🔴 「只改数据不改布局」这一面:路径恒 4 步而栅格回 2 列,4 张卡会挤进 2 列。 */
        id: 'Q13', file: HELPC,
        why: '[帮助] 学习路径栅格回到 2 列 —— 拉平顺带带出来的错版',
        from: "              'md:grid-cols-4',",
        to: "              'sm:grid-cols-2',",
        expectRed: { s: ['H2'] },
    },
    {
        /* 🔴 反向毒:「拉平」不许把**排除项**一起放开。 */
        id: 'Q14', file: DOCS,
        why: '[帮助·反向] 经营后台那一节也拉平 —— 进货价/佣金/结算对所有人可见',
        from: "      { slug: 'profit', title: '经营总览', audience: 'agent' },",
        to: "      { slug: 'profit', title: '经营总览' },",
        expectRed: { s: ['H4'] },
    },
    {
        id: 'Q15', file: DOCS,
        why: '[帮助·反向] provider-guide 被放开 —— 它正文不在仓里,核不了就不该放',
        from: "      { slug: 'provider-guide', title: '服务商完整操作说明书', audience: 'agent' },",
        to: "      { slug: 'provider-guide', title: '服务商完整操作说明书' },",
        expectRed: { s: ['H5'] },
    },
    {
        /* 🔴 这一发盯的是「零改动那一项」:本单没动推荐有礼,正因为没动,才最容易被下一个人顺手对齐掉。 */
        id: 'Q16', file: SIDE,
        why: '[推荐] 推荐有礼被顺手「对齐」掉 —— L0 即时 bonus 是活制度',
        from: "      { to: '/referral', icon: Share2, label: '推荐有礼' },\n      ...ACCOUNT_HELP_ITEMS,",
        to: '      ...ACCOUNT_HELP_ITEMS,',
        expectRed: { s: ['R5'] },
    },
    {
        /* 🔴 结构毒:身份 prop 回来就红,比钉「某一张卡可见」更难绕 —— 它钉的是**整个维度**。 */
        id: 'Q17', file: CARDS,
        why: '[监测] ActionCards 又接收 isAgent —— 这一面整个维度不该再有',
        from: '    isAdmin: boolean;',
        to: '    isAgent: boolean;\n    isAdmin: boolean;',
        expectRed: { s: ['M2'] },
    },
    {
        /* 🔴 同一谓词两个拷贝:只补一处的话,另一条路上的用户仍然没有出路。 */
        id: 'Q18', file: HALL,
        why: '[标题码] 「编辑标题」只补一个渲染点(另一处漏掉)',
        /* 🔴 这一发的锚**必须带缩进**:两个渲染点的这一行文字完全相同,只差缩进
              (76 / 72)。第一版我用不带缩进的锚,命中 2 次 ⇒ 运行器报「毒没下成」。
              而「毒没下成」和「锁没牙」在报文上同形 —— 这就是为什么运行器坚持锚必须恰好命中一次。
              取缩进 72 那一处(另一处留着,正好模拟「只补了一半」)。
              🔴 而且锚要用 `\n` 钉在**行首**:72 空格的串是 76 空格那行的**子串**,
              不钉行首的话它仍然同时命中两处。 */
        from: "\n                                                                        {topic.status === 'failed' && isTitleAlignmentRejected(topic) && (",
        to: "\n                                                                        {false && isTitleAlignmentRejected(topic) && (",
        expectRed: { s: ['T2b'] },
    },
    {
        id: 'Q19', file: QPREV,
        why: "[白标] 报价导出又被整页门控 —— Owner 09-17「白标放开所有人」",
        from: '  return <QuotePreviewInner />;',
        to: '  return <AgentLevelGate requiredLevel={1}><QuotePreviewInner /></AgentLevelGate>;',
        expectRed: { s: ['V4'] },
    },
    {
        /* 🔴 这两发盯的是**作废清单的余波**:白标与 organization 曾被列进「经营后台专属」,
              而那份清单里这两项在代码里从来没有对应的闸。谁照旧清单「对齐」一次就收回去了。 */
        id: 'Q20', file: SIDE,
        why: '[白标·余波] 普通账号菜单里的白标入口被按作废清单收回',
        from: "      { to: '/my-brand', icon: Building2, label: '我的品牌' },\n      WHITELABEL_ITEM,",
        to: "      { to: '/my-brand', icon: Building2, label: '我的品牌' },",
        expectRed: { s: ['V1'] },
    },
    {
        id: 'Q21', file: SIDE,
        why: '[组织·余波] 团队与席位被按作废清单收回(后端本来零闸)',
        from: "      ORGANIZATION_ITEM,\n      /* 🔴 [#222 a1b'] 这里原来有 WALLET_ITEM",
        to: "      /* 🔴 [#222 a1b'] 这里原来有 WALLET_ITEM",
        expectRed: { s: ['V2'] },
    },
];

/* 被碰文件:按**文件**备份,不按 edit —— 同文件两处时按 edit 还原会把中间态写回树 */
const TOUCHED = [...new Set(POISONS.flatMap((p) => [p.file]))];
const SHA0 = Object.fromEntries(TOUCHED.map((f) => [f, sha(abs(f))]));
const SRC0 = Object.fromEntries(TOUCHED.map((f) => [f, readFileSync(abs(f), 'utf8')]));
say('被碰文件开跑前 sha:');
for (const f of TOUCHED) say(`  ${f} ${SHA0[f].slice(0, 12)}`);

say('\n=== 0. 基线(两把闸的失败集都必须为空) ===');
const baseline = {};
for (const [name, gate] of Object.entries(GATES)) {
    const r = run(gate);
    baseline[name] = new Set(r.red);
    const good = r.rc === 0 && r.red.length === 0;
    if (!good) failures += 1;
    say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 失败集=${r.red.length ? r.red.join(',') : '空'}`);
}
if (failures) {
    say('\n🔴 基线不干净 —— 红基线会让每发毒都像命中,不往下跑。');
    process.exit(1);
}

/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红。
 *    少了它,`syntaxOk()` 平时永远返回 true —— 一把恒 true 的尺子
 *    与「每一发毒都下成了」读数完全同形。
 *    (对照臂 `assertRulerWorks` 管反方向:恒 false。两条臂缺一不可。)
 */
proveGuardHasTeeth(abs(POISONS[0].file), say);

for (const p of POISONS) {
    say(`\n=== ${p.id} ${p.why} ===`);
    const edits = [{ from: p.from, to: p.to }, ...(p.also || [])];
    let src = SRC0[p.file];
    let okLand = true;
    for (const e of edits) {
        const n = src.split(e.from).length - 1;
        if (n !== 1) {
            say(`  FAIL 毒没下成:${p.file} 的锚命中 ${n} 次(要恰好 1 次)—— 不是"锁没牙"`);
            okLand = false;
            failures += 1;
            break;
        }
        src = src.replace(e.from, e.to);
    }
    if (!okLand) continue;
    try { assertRulerWorks(abs(p.file)); } catch (err) { say(`  ${err.message}`); process.exit(3); }
    writeFileSync(abs(p.file), src, 'utf8');
    if (!syntaxOk(abs(p.file))) {
        say(`  FAIL ${NOT_LANDED_SYNTAX}`);
        failures += 1;
        writeFileSync(abs(p.file), SRC0[p.file], 'utf8');
        continue;
    }
    say(`  毒已落地(${edits.length} 处改动)`);

    for (const [name, want] of Object.entries(p.expectRed)) {
        const r = run(GATES[name]);
        const fresh = r.red.filter((x) => !baseline[name].has(x));
        const missing = want.filter((w) => !r.red.includes(w));
        const good = r.rc !== 0 && missing.length === 0;
        if (!good) failures += 1;
        say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 期望红=[${want.join(',')}]`
            + `${missing.length ? ` 🔴 没红=[${missing.join(',')}]` : ' 全中'}`);
        say(`       新增报红 ${fresh.length} 条:${fresh.join(',') || '(无)'}`);
    }
    writeFileSync(abs(p.file), SRC0[p.file], 'utf8');
    const back = sha(abs(p.file)) === SHA0[p.file];
    if (!back) failures += 1;
    say(`  ${back ? 'OK  ' : 'FAIL'} 还原:逐文件 sha 与下毒前一致`);
}

say('\n=== 收尾:被碰文件逐个核回开跑前的 sha ===');
for (const f of TOUCHED) {
    const now = sha(abs(f));
    const good = now === SHA0[f];
    if (!good) failures += 1;
    say(`  ${good ? 'OK  ' : 'FAIL'} ${f} ${now.slice(0, 12)}${good ? '' : ' 🔴 树里留了东西'}`);
}

/* 🔴 树自证:HEAD 与 dirty 清单必须和开跑前**逐字**一致 */
const AFTER = treeState();
const headSame = AFTER.head === BEFORE.head;
const dirtySame = AFTER.dirty === BEFORE.dirty;
if (!headSame) failures += 1;
if (!dirtySame) failures += 1;
say(`  ${headSame ? 'OK  ' : 'FAIL'} HEAD 与开跑前一致 ${AFTER.head.slice(0, 9)}`);
say(`  ${dirtySame ? 'OK  ' : 'FAIL'} dirty 清单与开跑前一致`
    + (dirtySame ? '' : `\n       前:${BEFORE.dirty}\n       后:${AFTER.dirty}`));

say('');
if (failures > 0) { say(`FAIL 注毒自证 ${failures} 项不过`); process.exit(1); }
say(`全部通过:${POISONS.length} 发毒,每发都红在写死的那一格,逐发字节还原,树状态逐字回位`);
process.exit(0);
