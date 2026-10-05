#!/usr/bin/env node
/**
 * 判据 · #222 a1b「普通账号权限 = 服务商(除经营后台)」(结构臂)。
 *
 * Owner 2026-09-15 裁定。本闸只守**对齐桶** —— 那些**不是**经营后台、
 * 却对普通账号藏着/禁着/改道的东西。
 *
 * 🔴 经营后台(进货 / 定价 / 结算 / 推广 / 协议 / 经营总览)**仍然只给服务商**,
 *    本闸配反臂盯着它别被顺手放开 —— 「对齐」不是「全开」。
 *
 * 两态退出码:0 全过 / 1 有失败(本闸进 build 链,rc=3 会让后面的闸不跑)。
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const decomment = (s) => s
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');

let bad = 0;
const ok = (cond, label, detail = '') => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`);
    if (!cond) bad += 1;
};

const CARDS = decomment(rd('src/pages/Monitoring/components/ActionCards.tsx'));
const CARDS_RAW = rd('src/pages/Monitoring/components/ActionCards.tsx');
const WELCOME = decomment(rd('src/components/onboarding/WelcomeChoiceModal.tsx'));
const ONBOARD = decomment(rd('src/components/onboarding/OnboardingWelcomeModal.tsx'));
const LAYOUT = decomment(rd('src/components/layout/Layout.tsx'));
const SIDEBAR = decomment(rd('src/components/layout/AppSidebar.tsx'));
/* 🔴 分组标记是注释(`// ===== 普通用户 =====`),decomment 会把它剥掉 ——
   拿 SIDEBAR 去切分组会得到空串,而空串上的「没有 WALLET_ITEM」是**真空通过**。
   切分组一律用原文;这一处是 W2a 分母自证当场抓出来的。 */
const SIDEBAR_RAW = rd('src/components/layout/AppSidebar.tsx');
/* 🔴 切片用原文(分组标记是注释),**判定去注释**:
   我在摘掉 WALLET_ITEM 的地方留了一句解释性注释,里面写着 WALLET_ITEM,
   拿原文判定就会被自己的注释打红 —— 注释不是代码,别让它参与判定。 */
const NORMAL_MENU = decomment(SIDEBAR_RAW.slice(
    SIDEBAR_RAW.indexOf('// ========== 普通用户 =========='),
    SIDEBAR_RAW.indexOf('// ========== 服务商(代理) ==========')));

console.log('R1 监测:交付工具对全员开放,破坏性动作仍挡着');
{
    /*
     * 🔴 这四张卡在 #222 之前被 `agentOrAdmin` 挡着,而上一版注释自己写的是
     *    「代理 + admin(**交付工具**)」—— 交付工具不是经营后台。
     *    锚钉的是「这张卡不在任何身份条件里」,不是「文件里出现过这个标题」:
     *    后者被一句注释就能满足。
     */
    const gatedTitles = [];
    const lines = CARDS.split('\n');
    for (let i = 0; i < lines.length; i += 1) {
        const m = lines[i].match(/title="([^"]+)"/);
        if (!m) continue;
        /*
         * 往上找最近的条件渲染开头 —— **连本行一起看**。
         * 🔴 注毒 Q1 照出来的:第一版只看上方三行,而毒把条件写在**同一行**
         *    (`{dangerousOrAdmin && <ActionTile … title="操作日志" …}`)就躲过去了。
         *    锁钉的是一个**排版形状**,不是「这张卡在不在身份条件里」。
         */
        const IDENT = '(dangerousOrAdmin|agentOrAdmin|isAgent|isAdmin|adminOnly'
            + '|agent_level|agentLevel)';
        const sameLine = new RegExp('\\{\\s*' + IDENT + '[^}]{0,40}&&').exec(lines[i]);
        if (sameLine) {
            gatedTitles.push(m[1] + '←' + sameLine[1] + '(同行)');
            continue;
        }
        for (let k = 1; k <= 3 && i - k >= 0; k += 1) {
            const prev = lines[i - k].trim();
            if (new RegExp('^\\{\\s*' + IDENT + '\\s*&&\\s*\\(?$').test(prev)) {
                gatedTitles.push(m[1] + '←' + prev.replace(/[{}&(\s]/g, ''));
                break;
            }
            if (/^\{[^}]*&&\s*\(?$/.test(prev)) break;
        }
    }
    ok(gatedTitles.length > 0,
        'R1a 分母自证:确实数到了受条件保护的卡(数到 0 说明扫描器坏了,'
        + '而坏掉的扫描器和"全都放开了"在报文上同形)', gatedTitles.join(' / ') || '零张');

    for (const t of ['交付设置', '媒体投放', '操作日志']) {
        ok(!gatedTitles.some((g) => g.startsWith(t)),
            `R1b-${t} 🔴 「${t}」对**全员**可见(交付工具,不是经营后台)`,
            gatedTitles.find((g) => g.startsWith(t)) || '不在任何身份条件里');
    }
    /*
     * 🔴 [#222 a1b' 2026-09-16] 这一格**方向反过来了**。
     *    上一版钉的是「数据回退仍然挡着(待 Owner)」—— Owner 已点头放开
     *    (删的是自己账号的监测历史,与服务商同权;不可逆的风险由 RollbackDialog 的
     *    二次确认承担,不由身份承担)。
     *    判据与修法互斥时,要改的是判据方向,不是绕开判据:
     *    旧判据留着会一直红,红着的判据会盖住下一个真缺陷。
     */
    ok(!gatedTitles.some((g) => g.startsWith('数据回退')),
        'R1c 🔴 「数据回退」对全员可见(Owner 09-16 放开)',
        gatedTitles.find((g) => g.startsWith('数据回退')) || '不在任何身份条件里');
    /*
     * 🔴 反臂搬到这里:放开一张 ≠ 全开。
     *    仍然只给 admin 的三样是**系统参数**(加词 / 清除数据 / 数据归档),不是身份差 ——
     *    「加词」尤其:老板 2026-06-05 定的是走报价/合同/服务变更流程,不是权限问题。
     */
    for (const t of ['添加关键词', '清除数据']) {
        ok(gatedTitles.some((g) => g.startsWith(t) && g.includes('adminOnly')),
            `R1c2 反臂:「${t}」**仍然**只给 admin —— 「对齐」不是「全开」`,
            gatedTitles.find((g) => g.startsWith(t)) || '🔴 它被顺手放开了');
    }
    /*
     * 🔴「数据归档」单独一格,不能塞进上面那个循环 —— 我第一版就是那么写的,当场红了,
     *    而红的是**判据**不是产品:上面的提取器只认「条件独占一行」的形状
     *    (`^{adminOnly && (`),而数据归档的条件是 `{adminOnly && archivesCount > 0 && (`,
     *    多了一个子条件就抽不到,于是「它被顺手放开了」——一句因为仪器看不见而产生的假红。
     *    判据读不到目标时要说「我没看见」,不能说「它不在」。
     */
    {
        const at = CARDS.indexOf('title="数据归档"');
        const before = at >= 0 ? CARDS.slice(Math.max(0, at - 300), at) : '';
        ok(at >= 0 && /\{\s*adminOnly\s*&&/.test(before),
            'R1c2b 反臂:「数据归档」**仍然**只给 admin(它的条件带 archivesCount,形状与上面两张不同)',
            at < 0 ? '🔴 这张卡找不到了' : (before.split('\n').filter((l) => l.includes('adminOnly')).pop() || '🔴 附近没有 adminOnly').trim());
    }

    /* 客户门户那张卡没有 title=,用 featureId 认 */
    const portalGated = /\{\s*(dangerousOrAdmin|agentOrAdmin|isAgent)\s*&&\s*\(\s*\n\s*<FeatureTooltip/.test(CARDS);
    ok(!portalGated,
        'R1d 🔴 「让客户自己看实时数据」(客户门户 Token 卡)对全员可见 —— '
        + '给客户发实时数据链接是交付动作');

    ok(!/agentOrAdmin/.test(CARDS),
        'R1e 旧的 `agentOrAdmin` 这个名字已不在代码里 —— '
        + '它把"危不危险"和"是不是服务商"混成了一个词');

    /* 计数行要跟可见性一致,否则「更多工具 (N)」会数错 */
    ok(/\+ 2 \/\* 媒体投放 \+ 操作日志/.test(CARDS_RAW)
        && /\+ 1 \/\* 数据回退/.test(CARDS_RAW)
        && !/dangerousOrAdmin/.test(CARDS_RAW),
        'R1f 🔴 「更多工具」的计数与可见性同源 —— '
        + '放开了卡却不改计数,按钮上的数字就是错的;'
        + '这一格随 R1c 一起翻面:卡无条件渲染,计数也必须是无条件的 +1',
        (CARDS_RAW.split('\n').find((l) => l.includes('数据回退') && l.includes('+')) || '(没找到计数行)').trim());
}

console.log('\nR2 新手引导:普通账号也看得到,且不对他断言身份');
{
    for (const [name, src] of [['WelcomeChoiceModal', WELCOME], ['OnboardingWelcomeModal', ONBOARD]]) {
        const win = src.slice(src.indexOf('shouldShow'), src.indexOf('shouldShow') + 500);
        /*
         * 🔴 注毒 Q4/Q8 照出来的:第一版只认 `isAgent` 这个**名字**,
         *    而毒直接写 `(user?.agent_level ?? 0) >= 1` 就绕过去了。
         *    身份判定有好几种写法 —— 钉名字不是钉命题。
         */
        const ident = win.match(/isAgent|agent_level|agentLevel|requiresAgent/);
        ok(!ident,
            'R2a-' + name + ' 🔴 shouldShow 里**没有任何身份判定**(不管写成哪种形态)'
            + ' —— 原来带身份条件时,普通账号根本看不到引导',
            ident ? '🔴 命中 ' + ident[0] : '零处');
    }
    ok(!/服务商工作台|服务方工作台|代理/.test(WELCOME + ONBOARD),
        'R2b 🔴 引导文案不对读者断言身份(原标题写着「…服务商工作台」),'
        + '也不出现术语铁律禁用的「代理」');
    /* 引导把人带去哪:沙盒白名单必须只含主流程,不含经营后台 */
    /* 🔴 按**真常量名**取窗。第一版用「往前 900 字符」,扫进了旁边别的内容
       当场假红 —— 取窗自证只答「我切对窗了没」,答不了「我切的是不是那一段」。 */
    const wlStart = SIDEBAR.indexOf('const SANDBOX_ALLOWED_PREFIXES');
    const wl = wlStart < 0 ? '' : SIDEBAR.slice(wlStart, SIDEBAR.indexOf('];', wlStart) + 2);
    const wlItems = (wl.match(/'[^']+'/g) || []);
    ok(wlItems.length >= 5,
        'R2c0 分母自证:沙盒白名单取到了', wlItems.join(' ') || '🔴 取不到,不出结论');
    ok(wlItems.length >= 5 && !wlItems.some((x) => x.includes('/agent/')),
        'R2c 🔴 引导(沙盒)白名单里**没有任何 `/agent/*`** —— '
        + '不许把普通账号引到他进不去的页',
        wlItems.filter((x) => x.includes('/agent/')).join(' ') || '零条');
}

console.log('\nR3 经营后台仍然只给服务商(反臂:别顺手全开)');
{
    const ROUTES = decomment(rd('src/App.tsx'));
    const guarded = (ROUTES.match(/requiresAgent/g) || []).length;
    ok(guarded >= 5,
        'R3a 🔴 `agent/*` 那几条路由**仍然**带 `requiresAgent`(经营后台例外)',
        `${guarded} 处`);
    const gate = decomment(rd('src/pages/Agent/ProfitDashboard.tsx'));
    ok(/<AgentLevelGate requiredLevel=\{1\}>/.test(gate),
        'R3b 🔴 经营总览/结算面仍由 `AgentLevelGate` 守着');
    ok(/BIZ_ITEMS/.test(SIDEBAR) && /isAgent/.test(SIDEBAR),
        'R3c 🔴 侧栏的经营后台分组仍按身份分流');
}

console.log('\nR4 过期注释不许再有');
{
    ok(!/Dashboard 内部已对 agent_level<1 隐藏/.test(rd('src/components/layout/Layout.tsx')),
        'R4a 🔴 那句「Dashboard 内部已对 agent_level<1 隐藏…L0 看到的是裁剪过的视图」已删 —— '
        + '2026-09-15 实测 Dashboard.tsx 里一处身份分支都没有,它是假的');
    /* 正样本臂:主流程页确实是 0,断言本身不是空的 */
    const files = ['src/pages/Dashboard.tsx'];
    for (const d of ['src/pages/Writing', 'src/pages/Publishing', 'src/pages/Pricing']) {
        for (const f of readdirSync(join(ROOT, d))) {
            if (f.endsWith('.tsx') || f.endsWith('.ts')) files.push(d + '/' + f);
        }
    }
    ok(files.length > 10, 'R4b 分母自证:扫到主流程文件', `${files.length} 个`);
    const hits = files.filter((f) => /\bisAgent\b|\bagent_level\b|\bagentLevel\b/.test(decomment(rd(f))));
    ok(hits.length === 0,
        'R4b2 🔴 主流程页面内部**没有**身份分流(这条为真,上面那句注释才是假的)',
        hits.join(', ') || '零处');
}

/* ══════════ 以下为 222-a1b'(Owner 放行 §6 待 Owner 桶后)新增 ══════════ */

const APP = decomment(rd('src/App.tsx'));
const APP_RAW = rd('src/App.tsx');
const HELPC = decomment(rd('src/pages/Help/HelpCenter.tsx'));
const DOCS = rd('src/pages/Help/docs-data.ts');
const MON = decomment(rd('src/pages/Monitoring/index.tsx'));
const HALL = decomment(rd('src/pages/Writing/WritingHall.tsx'));

console.log('');
console.log('W 钱包:服务商面对普通账号关死,但零售面必须仍然可达');
{
    /* 钉的是**改道**,不是「有守卫」:否决页也算有守卫,但它把有书签的人扔进死胡同。 */
    ok(/<Route path="wallet" element=\{\s*\n\s*<AgentLevelGate requiredLevel=\{1\} fallback=\{<Navigate to="\/customer\/wallet" replace \/>\}>/.test(APP_RAW),
        'W1 🔴 `/wallet` 对普通账号**改道到零售面**,不是裸挂、也不是落否决页 —— '
        + '它此前一直可达,有书签的人撞上否决页而那页不告诉他自己的钱包在哪',
        (APP_RAW.split('\n').find((l) => l.includes('path="wallet"')) || '(没找到)').trim().slice(0, 80));

    /* 🔴 反臂之一:菜单。路由关了而菜单还留着 = 一条通往改道的死链,用户会以为自己点错了。 */
    const normalAccount = NORMAL_MENU;
    ok(normalAccount.length > 200, 'W2a 分母自证:切到了普通用户那一段菜单', `${normalAccount.length} 字符`);
    ok(!/WALLET_ITEM/.test(normalAccount),
        'W2 🔴 普通用户菜单里没有 `WALLET_ITEM`(服务商钱包面)',
        (normalAccount.split('\n').find((l) => l.includes('WALLET_ITEM')) || '不在普通用户段').trim());

    /* 🔴 反臂之二:**不许关成两边都没有**。这是本格存在的全部理由。 */
    for (const r of ['customer/wallet', 'customer/recharge']) {
        const line = APP_RAW.split('\n').find((l) => l.includes(`path="${r}"`)) || '';
        ok(line.length > 0 && !/AgentLevelGate|requiresAgent|requiredModule/.test(line),
            `W3 反臂:零售面 \`/${r}\` **仍然裸挂** —— 关掉服务商面的前提是普通账号还有地方看钱包`,
            line.trim().slice(0, 90) || '🔴 这条路由不见了');
    }
    ok(/WALLET_ITEM/.test(SIDEBAR),
        'W3b 反臂:`WALLET_ITEM` 本身还在(服务商/管理员仍要看得见自己的钱包)');
}

console.log('');
console.log('M 监测:标题不再按身份分叉');
{
    ok(!/我的排名看板/.test(MON), 'M1 🔴 监测页标题不按身份分叉');
    /* 反臂:分叉不是靠改字符串绕过去的 —— 旧串在全仓任何地方都不该再有 */
    const all = ['src/pages/Monitoring/index.tsx', 'src/components/layout/AppSidebar.tsx'];
    ok(all.every((f) => !/我的排名看板/.test(rd(f))),
        'M1r 反臂:旧串「我的排名看板」在相关文件里零命中(不是挪了个地方)');
    /* 结构锁:身份 prop 一旦回归就红 —— 比钉某一张卡更难绕 */
    ok(!/isAgent/.test(CARDS) && !/isAgent=\{/.test(MON),
        'M2 🔴 `ActionCards` 不再接收 `isAgent`:这一面**整个维度**没了,'
        + '留一个没人读的身份 prop,下一个人会以为这儿还有分流',
        (CARDS.split('\n').find((l) => l.includes('isAgent')) || '零处').trim());
}

console.log('');
console.log('H 帮助中心:内容分层拉平,但经营后台那层仍挡着');
{
    ok(!/\bisAgent\b/.test(HELPC),
        'H1 🔴 帮助中心首页不再有身份分流(视频清单 / 学习路径 / 文案)',
        (HELPC.split('\n').find((l) => l.includes('isAgent')) || '零处').trim().slice(0, 80));
    /*
     * 🔴 H1 钉的是 `isAgent` 这个**名字**,不是「视频清单没被过滤」这个**命题**。
     *    注毒 Q12 照出来的:毒写成 `videos.filter((v) => v.audience === 'all')` ——
     *    一个 `isAgent` 都没出现,H1 照样绿,而普通账号重新看不到那 7 个主流程视频。
     *    本闸自己在 R2a 那里就写过这句「钉名字不是钉命题」,我还是又犯了一次。
     *    ⇒ 这一格钉命题:视频清单必须是**整份** `videos`,不许再挂任何过滤。
     */
    ok(/const visibleVideos = videos\s*$/m.test(HELPC)
        && !/visibleVideos\s*=\s*videos\s*\.filter/.test(HELPC),
        'H1b 🔴 视频清单是**整份**,没有任何过滤 —— '
        + '7 条原 agent-only 视频全是主流程交付内容(诊断/报价/写作/监测),'
        + '普通账号本来就能用这些功能,却只看得到 15 个里的 8 个',
        (HELPC.split('\n').find((l) => l.includes('visibleVideos =')) || '(没找到)').trim().slice(0, 90));
    /* 🔴 这一格是拉平**顺带带出来的错版**:路径恒 4 步,栅格还按 2 列会挤 */
    ok(/'md:grid-cols-4',/.test(rd('src/pages/Help/HelpCenter.tsx'))
        && !/isAgent \? 'md:grid-cols-4'/.test(rd('src/pages/Help/HelpCenter.tsx')),
        'H2 🔴 学习路径栅格定成 4 列 —— 路径恒为 4 步,只改数据不改布局的话 4 张卡挤进 2 列');
    ok(/const learningSteps = providerSteps/.test(HELPC),
        'H3 学习路径恒为服务商那 4 步(诊断→报价→写作→监测)');

    /* 🔴 反臂:拉平不许把**排除项**一起放开。经营后台那一节必须还是 agent。 */
    /*
     * 🔴 切到**本节结束**为止,不是切到文件末尾。
     *    第一版 `slice(indexOf(...))` 一路切到 EOF,把后面「算力」「常见问题」几节
     *    全扫了进来 —— 23 条里 14 条"裸奔",而它们本来就该裸奔(那些是全员文档)。
     *    原来的 `>= 8` 门槛恰好把这个切片错**盖住**了:数够了,就没人问数的是谁。
     *    取窗必须有**右边界**,而且要配一句「我切的是不是那一段」的自证。
     */
    const bizStart = DOCS.indexOf("title: '经营后台'");
    const biz = bizStart < 0 ? '' : DOCS.slice(bizStart, DOCS.indexOf('\n    ],', bizStart));
    ok(bizStart >= 0 && biz.includes("slug: 'stock-up'") && !biz.includes("slug: 'referral'"),
        'H4a 分母自证:切到的确实是「经营后台」那一节(含算力库存、不含推荐有礼)——'
        + '切过头的话下面那格量的是别节的文档',
        `${biz.length} 字符`);
    /*
     * 🔴 门槛式断言(`>= 8`)对「少掉一条」天生没分辨力 —— 注毒 Q14 只放开
     *    `profit` 一条,剩 8 条仍然 `>= 8`,这一格照样绿,而进货价那一节已经漏了。
     *    ⇒ 改成:该节里**每一个 slug 条目都必须带 audience**,一条都不许漏。
     *    钉「全都有」比钉「有几个」难绕,而且不用维护一个会过期的数字。
     */
    const bizSlugs = (biz.match(/\{ slug: '[^']+'[^}]*\}/g) || []);
    const bizNaked = bizSlugs.filter((x) => !/audience: '(agent|l2|admin)'/.test(x));
    ok(DOCS.includes("title: '经营后台'") && bizSlugs.length >= 8 && bizNaked.length === 0,
        'H4 反臂:`docs-data` 的「经营后台」一节**每一条**都仍带 audience —— '
        + '「拉平」不是「全开」,排除项(进货价/佣金/结算)靠这套机制挡着;'
        + '漏一条就是漏一条,不是"还剩很多条"',
        `该节 ${bizSlugs.length} 条 · 裸奔 ${bizNaked.length} 条`
        + (bizNaked.length ? ':' + bizNaked.join(' ').slice(0, 80) : ''));
    for (const slug of ['provider-guide', 'leads']) {
        const line = DOCS.split('\n').find((l) => l.includes(`slug: '${slug}'`)) || '';
        ok(/audience: 'agent'/.test(line),
            `H5 反臂:\`${slug}\` **仍然**服务商专属`
            + (slug === 'provider-guide' ? '(正文不在仓里,核不了就不放)' : '(正文属供应商零暴露内容)'),
            line.trim().slice(0, 80) || '🔴 这一条不见了');
    }
}

console.log('');
console.log('R 推荐有礼:普通账号**保留**(零改动,这一格防的是后面谁顺手对齐掉它)');
{
    const normalAccount = NORMAL_MENU;
    ok(/'\/referral'/.test(normalAccount),
        'R5 反向对照:普通用户菜单里 `/referral`(推荐有礼)**仍在** —— '
        + 'L0 即时 bonus 是活制度;本单不改它,但没有这一格,谁顺手"对齐"掉它不会有人红',
        (normalAccount.split('\n').find((l) => l.includes('/referral')) || '🔴 它没了').trim().slice(0, 70));
}

console.log('');
console.log('T 标题失败码:按码分支,且两个渲染点都给出路');
{
    ok(/const TITLE_ALIGNMENT_REJECTED = 'TITLE_ALIGNMENT_REJECTED'/.test(HALL)
        && /function isTitleAlignmentRejected/.test(HALL),
        'T1 🔴 前端**认得**复核拒这个码(后端 writing/title_ai_only.py:58)');
    /* 🔴 分母:这条路径上的码恰好两个,不能只认一个 —— 另一个必须仍然走原来的展示 */
    ok(/topic\.generation_error_code \|\| 'TITLE_GENERATION_FAILED'/.test(HALL),
        'T1b 反臂:另一个码(`TITLE_AI_UNAVAILABLE`)**仍然**带码前缀展示 —— '
        + '它对用户没有可操作含义,码留给客服排查;不是所有码都该去掉前缀');

    const edit = (HALL.match(/edit-title-after-alignment-reject/g) || []).length;
    const retry = (HALL.match(/retry-title-generation/g) || []).length;
    ok(retry >= 2, 'T2a 分母自证:失败行有多个渲染点', `重试入口 ${retry} 处`);
    ok(edit === retry,
        'T2 🔴 「编辑标题」出路在**每个**渲染点都有 —— '
        + '同一谓词两个拷贝,第一次我只补了一处(两处 onClick 写法不同,锚只中了一个)',
        `编辑 ${edit} 处 / 重试 ${retry} 处`);
    /*
     * 🔴 T2 数的是 **testid 出现次数** —— 那是「按钮**在不在源码里**」,不是「它**会不会渲染**」。
     *    注毒 Q18 把其中一处的条件改成 `{false && …}`:按钮原样躺在那儿,testid 还是 2 个,
     *    T2 照样绿,而那条路上的用户一个出路都没有。**存在 ≠ 可达** ——
     *    本闸开头 A 段就是栽在这件事上的,我在同一个文件里又写了一次。
     *    ⇒ 连**守卫条件**一起数:两处都必须真的挂在 `isTitleAlignmentRejected` 上。
     */
    const guard = (HALL.match(/status === 'failed' && isTitleAlignmentRejected\(topic\) && \(/g) || []).length;
    ok(guard === retry,
        'T2b 🔴 两处的编辑入口都**真的挂在那个码的守卫上**(不是躺在 `{false && …}` 里)—— '
        + '数 testid 只能证明按钮写过,证明不了它会渲染',
        `守卫 ${guard} 处 / 重试 ${retry} 处`);
}

console.log('');
console.log('V 白标与团队席位:Owner 09-17 放开,这几格防的是**将来按作废清单顺手收回**');
{
    /*
     * 🔴🔴 下面这几格**看起来像冗余判据**(它们钉的是「今天本来就成立的事」),
     *      下一个人很可能顺手删掉。所以把理由写在这儿:
     *
     *      #222 有过一份「经营后台专属」清单,白标与 organization 都在上面。
     *      清单**两次**被查出里面的项在代码里根本没有对应的闸:
     *        · 白标:活菜单 `AppSidebar` 的 `WHITELABEL_ITEM` **两组都有**,
     *          路由自 2026-06-06 P1 起对所有 operator 开放;
     *          「只在普通账号菜单、放反了」那句是从**全仓零引用**的 `Sidebar.tsx` 读来的。
     *        · organization:后端 `organization_api` / `organization_service` 零身份闸,
     *          帮助文档自己写着「普通用户和服务商都可以创建团队」。
     *      Owner 09-17 据订正后的事实拍板:**白标放开所有人,团队席位不用收回**,旧清单作废。
     *
     *      ⇒ 这几格的作用不是描述现状,是**挡住那份作废清单的余波**:
     *        它还躺在 WO 的历史章节里,谁照着它「对齐」一次,就把这两样收回去了,
     *        而收回去是**对客能力回退**,不会有任何别的判据红。
     *        删这几格之前,请先确认 Owner 改了口径。
     */
    ok(/WHITELABEL_ITEM/.test(NORMAL_MENU),
        'V1 反向对照:普通账号菜单里**仍有**白标入口(Owner 09-17「白标放开所有人」)',
        (NORMAL_MENU.split('\n').find((l) => l.includes('WHITELABEL_ITEM')) || '🔴 它没了').trim());
    ok(/ORGANIZATION_ITEM/.test(NORMAL_MENU),
        'V2 反向对照:普通账号菜单里**仍有**团队与席位(Owner 09-17「不用收回」;后端本来也零闸)',
        (NORMAL_MENU.split('\n').find((l) => l.includes('ORGANIZATION_ITEM')) || '🔴 它没了').trim());

    const wlRoute = APP_RAW.split('\n').find((l) => l.includes('path="agent/whitelabel"')) || '';
    ok(wlRoute.length > 0 && !/requiresAgent|AgentLevelGate|requiredModule/.test(wlRoute),
        'V3 反向对照:`/agent/whitelabel` 路由**仍然**对普通账号可达(菜单有、路由关着 = 死链)',
        wlRoute.trim().slice(0, 90) || '🔴 这条路由不见了');

    const QP = decomment(rd('src/pages/Agent/QuotePreview.tsx'));
    ok(!/AgentLevelGate/.test(QP),
        'V4 🔴 白标**报价导出**不再整页门控 —— 能设置对外品牌却不能用它导出报价,自相矛盾;'
        + '这个闸原本建在「白标放反了」那个**不成立**的事实上',
        (QP.split('\n').find((l) => l.includes('AgentLevelGate')) || '零处').trim());

    /* 🔴 正样本臂:上面四格都是「某某**仍在**」,全靠读文件。
          文件读空了的话它们会一起假红;而经营后台那五条闸仍在,证明我读的是真文件。 */
    ok((APP_RAW.match(/requiresAgent/g) || []).length >= 5 && NORMAL_MENU.length > 200,
        'V5 正样本臂:经营后台那 5 条 `requiresAgent` 仍在、普通用户菜单段非空 —— '
        + '否则上面四格是在空字符串上做的判断',
        `requiresAgent ${(APP_RAW.match(/requiresAgent/g) || []).length} 处 · 菜单段 ${NORMAL_MENU.length} 字符`);
}

console.log('');
if (bad > 0) {
    console.log(`FAIL ${bad} 项不通过`);
    process.exit(1);
}
console.log('全部通过');
process.exit(0);
