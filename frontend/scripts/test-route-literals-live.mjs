#!/usr/bin/env node
/**
 * 结构臂 · WO_260 开源 E0/E1:在役代码不许再指向要删的路由,也不许再 import components/m3/**。
 * 只读源码 + 真调 TypeScript 解析,不起浏览器 —— 在 build 链末尾。
 *
 * 「在役代码」= 从在役路由根出发的静态闭包(不是「不在废弃目录的文件」):
 *   根 = App.tsx 的静态 import(壳 / Provider)+ 在役路由 element 里每个组件的模块 + main.tsx 的其余 import;
 *   边 = 静态 import / export-from / 动态 import() / import type(类型边也算)。
 *   「要删的路由」= 冻结表 RETIRED_ROUTES(scripts/lib/retired-routes.mjs,与 verify-dead-interactions B8 共用一份;
 *   OSS_01 J 章 C 桶社媒 + D 桶左栏已取消,共 105 条路径,含 socialStudioModulePaths 展开的 36 条)——
 *   **冻结而不现读**:E3 把它们从 App.tsx 删掉之后,这把尺子照样认得出「指向已删路由」;
 *   在役路由表则每次现读 App.tsx(减去冻结表)。
 *
 * S 段 · 真判据:
 *   S0 分母自证:路由表 / 闭包 / 字面量都真扫到了(下限见常量),且 105 条 DEP 路由一条不少
 *   S1 🔴 非 API 调用语境里的路由字面量,解析到 DEP 路由的 = 0(接替 OSS_12「146 处硬编码废弃路由」;
 *      startsWith / includes 语境按「段前缀」解析:只要有在役路由落在这个前缀下就算在役 —— `/s/` 因
 *      在役的 `/s/:token`(客户选词报价)而合法,`/social` / `/c/` / `/m3` 不合法)
 *   S2 🔴 导航语境里的路由字面量必须解析到在役路由(不许 DEP、也不许谁都解析不到)
 *   M  🔴 在役代码 import components/m3/** = 0(值与类型都算)
 *   S3 🔴 [WO_322] 从 App.tsx **全部**路由(含仍挂着的 DEP 页)出发的闭包里,直接或经同文件变量 / 返回值
 *      (至多三跳)流向导航的站内路由字面量,必须解析到 App.tsx 现存的某条路由(详见 S3 段注释)
 *   E1 冻结差分表 114 行逐行成立(改指 / 删除 / 保留 / 出闭包),外加 4 条坏链修复逐行成立;
 *      行数写成独立字面量,多一行少一行都红
 *   E0 上提映射逐项成立:8 个文件在新位置;旧位置要么不存在、要么是唯一一句 `export * from '<新位置>'` 的转发;
 *      ToolId 独立类型文件在,proCapabilities 从它取;BackToClientButton 里没有 /m3/
 *
 * C 段 · 锁自己的牙(合成源,同一组函数,不改任何文件):
 *   C1 navigate('/m3/queue') ⇒ S1 红(工单点名的反臂)· C2 navigate('/definitely-not-a-route') ⇒ S2 红
 *   C3 对照:navigate('/my-clients') 与模板 `/my-clients/${id}` ⇒ 都不红
 *   C4 前缀:startsWith('/s/') 合法、startsWith('/social') 红 · C5 API 语境 apiGet('/advisors') 不红、navigate('/advisors') 红
 *   C6 import '@/components/m3/parts' ⇒ M 红;'@/components/workbench/parts' ⇒ 不红
 *   C7 冻结表不依赖 App.tsx:合成路由表里没有 /m3/*,'/m3/queue' 照样判 DEP(E3 之后的语义)
 *   C8 [WO_322] 顾问团页原样写法(变量 → useCallback 返回 → navigate(fn(id)))指向不存在的路由 ⇒ S3 红
 *   C9 对照:同一写法指向现存路由 ⇒ 不红 · C10 同一个串只赋给变量、不流向导航 ⇒ 不进 S3(API 键 / 前缀数组不误伤)
 *
 * 🔴 自曝覆盖不到的形态:
 *   ① 键名是动作 id 的「键 → 路由」映射(如 publishErrorActions 的 NAV_TARGET):S2 按属性名认导航语境,认不出这种;
 *      其中指向 DEP 的仍被 S1 拦住,指向「根本不存在的路由」的看不见(本单的 /team 就是这样被人工找到的);
 *   ② 运行期拼出来的路由(变量拼接 / 后端下发的 recommended_route 等)—— 静态字面量看不见;
 *   ③ 闭包是过估(被 import ≠ 真渲染),不会漏扫,但可能多扫到死分支里的字面量。
 *
 * 三态退出码:0 全过 / 1 有失败 / 3 判据不可用(文件读不到、TypeScript 取不到 —— 不当绿灯)。
 */
import { readFileSync, existsSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { RETIRED_ROUTES, RETIRED_ROUTE_COUNT } from './lib/retired-routes.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');
const MIN_ROUTES = 150, MIN_CLOSURE = 500, MIN_LITERALS = 500;

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

/* ── 冻结:要删的路由(C = 废弃-社媒,D = 废弃-左栏已取消)────────────────── */
const DEP_ROUTES = RETIRED_ROUTES;
const DEP_COUNT = 105;  // 独立字面量:与 lib 里的数对账,lib 被人改短 / 改长这里就红

/* ── 冻结:E1 差分表(改前 114 条指向 DEP 的字面量逐条定性)───────────────
 *   [改前所在文件, 旧字面量, 处置, 新字面量, 理由键]
 *   delete  = 该文件里这个字面量已不存在(入口删了 / 死分支删了)
 *   repoint = 旧字面量不在了,新字面量在(改指在役页)
 *   keep    = 保留:仍在,且按 S1 的解析规则落到在役路由(只有 analytics 的 `/s/` 前缀一条)
 *   out     = 该文件已不在在役闭包里(E0 上提 / 撤掉死耦合后,整个文件只剩废弃代码在用,随 E3 删) */
const E1_REASONS = {
    'e0-registry': 'ToolId 抽成独立类型文件后,旧工具注册表与 8 个 m3 工具页不再被在役代码拖进闭包',
    'e0-lift': 'ToolGrid 上提到 components/workbench;旧路径只剩转发,在役代码不再引用',
    'e0-rename': 'BackToM3Button 改名 BackToClientButton 上提,目标改为 /my-clients/:id',
    'cend-coupling': '客户详情页撤掉 clearCEndBrandCache 死耦合后,C 端抽屉不再被拖进在役闭包',
    labels: '面包屑标题表:社媒 16 条 + D 桶 7 条随 E3 删域,都不是在役路由的前缀',
    'layout-dead': '壳里的 /social 分支从未命中(/social/* 是 App.tsx 顶层重定向,不挂本壳);/agent 特判只服务 D 桶页',
    landing: '登录落地统一 /(后端 recommended_route 只会给 / 或要删的 /m3/sales/today)',
    'dashboard-dead': 'Dashboard 第 68–601 行不可达(UserMode 只有三值,前面必 return),入口卡随死区删;社媒模式并入 FullHome',
    'brand-404': '客户已删的 404 回退:client 模式原先跳社媒客户列表(在役页的 bug),改回「我的客户」',
    'brand-social': '客户详情底部「去社媒工作台」友链 + 只在 c-end「IP 人设」tab 渲染的建档按钮(c-end 无调用方)',
    procap: '同一功能的在役页(侧栏入口):/referral /agent/profit /agent/whitelabel /settings',
    'analytics-keep': '`/s/` 是在役 `/s/:token`(客户选词报价)的前缀,照旧不计入旧版工作面 PV',
    analytics: 'PV 埋点里 /social /m3 /c/ 与 === /s 的分流,在役页永远命不中',
    'cend-hook': 'useIsCEndContext 的 /c 路径判断,在役页永远命不中(保留 ?source=c 参数判断)',
    'cend-notify': '通知铃在 C 端分屏里开老 C 端方案页的特判(只有废弃的 C 端壳传 userMode=c)',
    sidebar: '侧栏 isActive 的 /social 特判:侧栏早已没有社媒项',
    'social-entry': '在役页里通往社媒的入口(个人设置「查看完整画像」、算力价格表社媒友链)',
    'cend-home': '老 C 端对话 /c/chat 今天就重定向到 /,改直接 /(落点不变)',
    insights: '/insights 独立路由随 E3 删;同一份洞察现挂在监测中心「查看效果报告」',
    'social-back': '订阅页「返回小榜」原跳社媒首页,改回在役首页(文案不动)',
};
const E1_DIFF = [
    ['src/components/c_end/CEndDrawer.tsx', '/c/chat', 'out', '', 'cend-coupling'],  // 改前 :228
    ['src/components/c_end/CEndDrawer.tsx', '/c/history', 'out', '', 'cend-coupling'],  // 改前 :229
    ['src/components/c_end/CEndDrawer.tsx', '/c/geo-plan', 'out', '', 'cend-coupling'],  // 改前 :248
    ['src/components/c_end/CEndDrawer.tsx', '/c/history?tab=reports', 'out', '', 'cend-coupling'],  // 改前 :251
    ['src/components/c_end/CEndDrawer.tsx', '/c/', 'out', '', 'cend-coupling'],  // 改前 :341
    ['src/components/c_end/CEndDrawer.tsx', '/c', 'out', '', 'cend-coupling'],  // 改前 :341
    ['src/components/layout/AppSidebar.tsx', '/social', 'delete', '', 'sidebar'],  // 改前 :455
    ['src/components/layout/Header.tsx', '/social', 'delete', '', 'labels'],  // 改前 :42
    ['src/components/layout/Header.tsx', '/social/workshop', 'delete', '', 'labels'],  // 改前 :43
    ['src/components/layout/Header.tsx', '/social/topics', 'delete', '', 'labels'],  // 改前 :44
    ['src/components/layout/Header.tsx', '/social/scripts', 'delete', '', 'labels'],  // 改前 :45
    ['src/components/layout/Header.tsx', '/social/research', 'delete', '', 'labels'],  // 改前 :46
    ['src/components/layout/Header.tsx', '/social/trending', 'delete', '', 'labels'],  // 改前 :47
    ['src/components/layout/Header.tsx', '/social/rewrite', 'delete', '', 'labels'],  // 改前 :48
    ['src/components/layout/Header.tsx', '/social/review', 'delete', '', 'labels'],  // 改前 :49
    ['src/components/layout/Header.tsx', '/social/interview', 'delete', '', 'labels'],  // 改前 :50
    ['src/components/layout/Header.tsx', '/social/operation', 'delete', '', 'labels'],  // 改前 :51
    ['src/components/layout/Header.tsx', '/social/my', 'delete', '', 'labels'],  // 改前 :52
    ['src/components/layout/Header.tsx', '/social/team', 'delete', '', 'labels'],  // 改前 :53
    ['src/components/layout/Header.tsx', '/social/corpus', 'delete', '', 'labels'],  // 改前 :54
    ['src/components/layout/Header.tsx', '/social/creator-profile', 'delete', '', 'labels'],  // 改前 :55
    ['src/components/layout/Header.tsx', '/social/advisor-market', 'delete', '', 'labels'],  // 改前 :56
    ['src/components/layout/Header.tsx', '/social/plan', 'delete', '', 'labels'],  // 改前 :57
    ['src/components/layout/Header.tsx', '/advisors', 'delete', '', 'labels'],  // 改前 :58
    ['src/components/layout/Header.tsx', '/references', 'delete', '', 'labels'],  // 改前 :60
    ['src/components/layout/Header.tsx', '/geo-research', 'delete', '', 'labels'],  // 改前 :61
    ['src/components/layout/Header.tsx', '/insights', 'delete', '', 'labels'],  // 改前 :62
    ['src/components/layout/Header.tsx', '/placement', 'delete', '', 'labels'],  // 改前 :63
    ['src/components/layout/Header.tsx', '/agent/preview', 'delete', '', 'labels'],  // 改前 :80
    ['src/components/layout/Header.tsx', '/admin/roles', 'delete', '', 'labels'],  // 改前 :90
    ['src/components/layout/Layout.tsx', '/social', 'delete', '', 'layout-dead'],  // 改前 :129
    ['src/components/layout/Layout.tsx', '/s/chat', 'delete', '', 'layout-dead'],  // 改前 :135
    ['src/components/layout/Layout.tsx', '/social', 'delete', '', 'layout-dead'],  // 改前 :141
    ['src/components/layout/Layout.tsx', '/s/chat', 'delete', '', 'layout-dead'],  // 改前 :141
    ['src/components/layout/Layout.tsx', '/social', 'delete', '', 'layout-dead'],  // 改前 :218
    ['src/components/layout/Layout.tsx', '/agent', 'delete', '', 'layout-dead'],  // 改前 :220
    ['src/components/layout/Layout.tsx', '/social/interview', 'delete', '', 'layout-dead'],  // 改前 :253
    ['src/components/layout/Layout.tsx', '/social/creator-profile', 'delete', '', 'layout-dead'],  // 改前 :254
    ['src/components/layout/Layout.tsx', '/social/creator-test', 'delete', '', 'layout-dead'],  // 改前 :255
    ['src/components/layout/Layout.tsx', '/social/corpus', 'delete', '', 'layout-dead'],  // 改前 :256
    ['src/components/layout/Layout.tsx', '/social', 'delete', '', 'layout-dead'],  // 改前 :362
    ['src/components/layout/Layout.tsx', '/social/', 'delete', '', 'layout-dead'],  // 改前 :414
    ['src/components/layout/Layout.tsx', '/advisors', 'delete', '', 'layout-dead'],  // 改前 :422
    ['src/components/layout/Layout.tsx', '/social', 'delete', '', 'layout-dead'],  // 改前 :477
    ['src/components/layout/NotificationBell.tsx', '/c/geo-plan?task_id=${tid}', 'delete', '', 'cend-notify'],  // 改前 :262
    ['src/components/m3/ToolGrid.tsx', '/m3/', 'out', '', 'e0-lift', '旧文件已删(components/m3 随 E3 整删)'],  // 改前 :72
    ['src/components/m3/workbench/BackToM3Button.tsx', '/m3/customer/${brandId}', 'out', '', 'e0-rename', '旧文件已删(上提改名)'],  // 改前 :53
    ['src/hooks/useIsCEndContext.ts', '/c/', 'delete', '', 'cend-hook'],  // 改前 :25
    ['src/hooks/useIsCEndContext.ts', '/c', 'delete', '', 'cend-hook'],  // 改前 :25
    ['src/lib/analytics.ts', '/social', 'delete', '', 'analytics'],  // 改前 :117
    ['src/lib/analytics.ts', '/m3', 'delete', '', 'analytics'],  // 改前 :118
    ['src/lib/analytics.ts', '/c/', 'delete', '', 'analytics'],  // 改前 :119
    ['src/lib/analytics.ts', '/s', 'delete', '', 'analytics'],  // 改前 :119
    ['src/lib/analytics.ts', '/s/', 'keep', '', 'analytics-keep'],  // 改前 :119
    ['src/pages/Account/ProfilePage.tsx', '/s/profile', 'delete', '', 'social-entry'],  // 改前 :356
    ['src/pages/Brand/BrandDetailPage.tsx', '/s', 'delete', '', 'brand-social'],  // 改前 :549
    ['src/pages/Brand/BrandDetailPage.tsx', '/s/clients', 'repoint', '/my-clients', 'brand-404'],  // 改前 :549
    ['src/pages/Brand/BrandDetailPage.tsx', '/s', 'delete', '', 'brand-social'],  // 改前 :1826
    ['src/pages/Brand/BrandDetailPage.tsx', '/s', 'delete', '', 'brand-social'],  // 改前 :2639
    ['src/pages/Brand/BrandDetailPage.tsx', '/s/creator-profile', 'delete', '', 'brand-social'],  // 改前 :2642
    ['src/pages/Brand/BrandDetailPage.tsx', '/s/corpus', 'delete', '', 'brand-social'],  // 改前 :2645
    ['src/pages/Brand/BrandDetailPage.tsx', '/s', 'delete', '', 'brand-social'],  // 改前 :2715
    ['src/pages/Brand/BrandDetailPage.tsx', '/s/creator-profile', 'delete', '', 'brand-social'],  // 改前 :2718
    ['src/pages/Brand/BrandDetailPage.tsx', '/s/corpus', 'delete', '', 'brand-social'],  // 改前 :2721
    ['src/pages/Dashboard.tsx', '/s', 'delete', '', 'dashboard-dead'],  // 改前 :60
    ['src/pages/Dashboard.tsx', '/s', 'delete', '', 'dashboard-dead'],  // 改前 :184
    ['src/pages/Dashboard.tsx', '/knowledge', 'delete', '', 'dashboard-dead'],  // 改前 :185
    ['src/pages/Dashboard.tsx', '/advisors', 'delete', '', 'dashboard-dead'],  // 改前 :186
    ['src/pages/Dashboard.tsx', '/employees', 'delete', '', 'dashboard-dead'],  // 改前 :187
    ['src/pages/Dashboard.tsx', '/s', 'delete', '', 'dashboard-dead'],  // 改前 :385
    ['src/pages/Dashboard.tsx', '/s', 'delete', '', 'dashboard-dead'],  // 改前 :394
    ['src/pages/Dashboard.tsx', '/employees', 'delete', '', 'dashboard-dead'],  // 改前 :407
    ['src/pages/Dashboard.tsx', '/knowledge', 'delete', '', 'dashboard-dead'],  // 改前 :438
    ['src/pages/Login/LoginPage.tsx', '/s', 'delete', '', 'landing'],  // 改前 :143
    ['src/pages/Login/LoginPage.tsx', '/s', 'delete', '', 'landing'],  // 改前 :149
    ['src/pages/Managed/ManagedListPage.tsx', '/c/chat', 'repoint', '/', 'cend-home'],  // 改前 :131
    ['src/pages/Partner/PartnerApplyStep3.tsx', '/c/chat', 'repoint', '/', 'cend-home'],  // 改前 :189
    ['src/pages/Placement/PlacementCenter.tsx', '/insights', 'repoint', '/monitoring', 'insights'],  // 改前 :467
    ['src/pages/Pricing/PricingPage.tsx', '/s', 'delete', '', 'social-entry'],  // 改前 :155
    ['src/pages/Pricing/SubscriptionPlansPage.tsx', '/s', 'repoint', '/', 'social-back'],  // 改前 :508
    ['src/pages/Subscription/SubscriptionManagementPage.tsx', '/s', 'repoint', '/', 'social-back'],  // 改前 :214
    ['src/services/m3/proCapabilities.ts', '/m3/referral', 'repoint', '/referral', 'procap'],  // 改前 :182
    ['src/services/m3/proCapabilities.ts', '/m3/referral', 'repoint', '/referral', 'procap'],  // 改前 :188
    ['src/services/m3/proCapabilities.ts', '/m3/agent/profit', 'repoint', '/agent/profit', 'procap'],  // 改前 :196
    ['src/services/m3/proCapabilities.ts', '/m3/agent/profit', 'repoint', '/agent/profit', 'procap'],  // 改前 :202
    ['src/services/m3/proCapabilities.ts', '/m3/agent/whitelabel', 'repoint', '/agent/whitelabel', 'procap'],  // 改前 :210
    ['src/services/m3/proCapabilities.ts', '/m3/agent/whitelabel', 'repoint', '/agent/whitelabel', 'procap'],  // 改前 :216
    ['src/services/m3/proCapabilities.ts', '/m3/settings', 'repoint', '/settings', 'procap'],  // 改前 :224
    ['src/services/m3/proCapabilities.ts', '/m3/settings', 'repoint', '/settings', 'procap'],  // 改前 :230
    ['src/utils/postLoginRedirect.ts', '/s', 'delete', '', 'landing'],  // 改前 :41
    ['src/utils/postLoginRedirect.ts', '/c/chat', 'delete', '', 'landing'],  // 改前 :48
    ['src/utils/postLoginRedirect.ts', '/c', 'delete', '', 'landing'],  // 改前 :48
    ['src/utils/postLoginRedirect.ts', '/s/chat', 'delete', '', 'landing'],  // 改前 :48
    ['src/utils/postLoginRedirect.ts', '/s', 'delete', '', 'landing'],  // 改前 :48
    ['src/utils/postLoginRedirect.ts', '/s', 'delete', '', 'landing'],  // 改前 :64
    ['src/utils/postLoginRedirect.ts', '/c/chat', 'delete', '', 'landing'],  // 改前 :87
    ['src/utils/postLoginRedirect.ts', '/c/chat', 'delete', '', 'landing'],  // 改前 :98
    ['src/utils/postLoginRedirect.ts', '/c/chat', 'delete', '', 'landing'],  // 改前 :116
    ['src/utils/postLoginRedirect.ts', '/s', 'delete', '', 'landing'],  // 改前 :135
    ['src/utils/postLoginRedirect.ts', '/c/chat', 'delete', '', 'landing'],  // 改前 :140
];
// [开源 E3 · 前端 · 2026-10-01 · WO_322] 114 → 100:M3 工具件 6 个文件的 14 行(全是「出闭包」)随 components/m3 整删 ——
//   文件不在,「出闭包」恒成立,这些行不再判任何东西,删行。
const E1_ROW_COUNT = 100;

/* ── 冻结:本单顺手修掉的 4 条「解析不到任何路由」的坏链(改前就是 404)──────── */
const E1_BROKEN = [
    ['src/pages/MaterialCenter/MaterialCenterPage.tsx', '/profiles/${profileId}/edit', 'delete', '', '路由从来不存在;本页字段就在本页行内改,没有语义对得上的在役页'],
    ['src/pages/Writing/publishErrorActions.ts', '/team', 'repoint', '/organization/team', '团队管理的在役页'],
    ['src/lib/defensiveGeoActions.ts', '/support', 'repoint', '/help/home', '与「联系管理员」同落帮助中心(里面有联系方式)'],
    ['src/components/layout/Header.tsx', '/agent/quote', 'delete', '', '面包屑标题表里一条从来不存在的路由'],
];
const E1_BROKEN_COUNT = 4;

/* ── 冻结:E0 上提映射 [旧位置, 新位置, 旧位置是否留转发, (不留转发时)说明] ──────────
   第 4 列只给 scripts/gate_refs_exist.py 看:它按行认「断言不存在」,不写明「已删」会把旧位置当成悬空引用 */
const E0_LIFT = [
    ['src/components/m3/workbench/BridgeBanner.tsx', 'src/components/workbench/BridgeBanner.tsx', false, '旧位置已删'],
    ['src/components/m3/workbench/DecisionBarBridge.tsx', 'src/components/workbench/DecisionBarBridge.tsx', false, '旧位置已删'],
    ['src/components/m3/workbench/BackToM3Button.tsx', 'src/components/workbench/BackToClientButton.tsx', false, '旧位置已删'],
    ['src/components/m3/parts.tsx', 'src/components/workbench/parts.tsx', false, '旧位置已删(转发随 components/m3 整删 · WO_322)'],
    ['src/components/m3/ToolGrid.tsx', 'src/components/workbench/ToolGrid.tsx', false, '旧位置已删(转发随 components/m3 整删 · WO_322)'],
    ['src/components/m3/materials/MaterialConfirmStatusBadge.tsx', 'src/components/workbench/MaterialConfirmStatusBadge.tsx', false, '旧位置已删'],
    ['src/components/m3/sales/TestClientFilterToggle.tsx', 'src/components/workbench/TestClientFilterToggle.tsx', false, '旧位置已删(转发随 components/m3 整删 · WO_322)'],
    ['src/components/m3/sales/TodayPriorityWidget.tsx', 'src/components/workbench/TodayPriorityWidget.tsx', false, '旧位置已删'],
];

/* ══ 机器 ════════════════════════════════════════════════════════════════ */
const rel = (p) => relative(ROOT, p).split(sep).join('/');
const segs = (p) => p.split('/').filter(Boolean);
const DEP_SET = new Set(DEP_ROUTES.map((r) => r[0]));

/** App.tsx → 路由表(含 `.map(p => <Route path={`/s/${p}`}/>)` 的同文件 const 数组展开)+ 组件名 → 模块 spec */
function parseApp(appText, appFile) {
    const sf = ts.createSourceFile(appFile, appText, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const nameToSpec = new Map();
    const loaderToSpec = new Map();
    const findImportSpec = (node) => {
        let spec = null;
        const walk = (n) => {
            if (spec) return;
            if (ts.isCallExpression(n) && n.expression.kind === ts.SyntaxKind.ImportKeyword && n.arguments[0] && ts.isStringLiteral(n.arguments[0])) { spec = n.arguments[0].text; return; }
            if (ts.isIdentifier(n) && loaderToSpec.has(n.text)) { spec = loaderToSpec.get(n.text); return; }
            ts.forEachChild(n, walk);
        };
        walk(node);
        return spec;
    };
    const staticSpecs = [];
    for (const st of sf.statements) {
        if (ts.isImportDeclaration(st) && st.importClause && ts.isStringLiteral(st.moduleSpecifier)) {
            const spec = st.moduleSpecifier.text;
            if (!st.importClause.isTypeOnly) staticSpecs.push(spec);
            const ic = st.importClause;
            if (ic.name) nameToSpec.set(ic.name.text, spec);
            if (ic.namedBindings && ts.isNamedImports(ic.namedBindings)) for (const el of ic.namedBindings.elements) nameToSpec.set(el.name.text, spec);
        }
        if (ts.isVariableStatement(st)) {
            for (const d of st.declarationList.declarations) {
                if (!ts.isIdentifier(d.name) || !d.initializer) continue;
                const spec = findImportSpec(d.initializer);
                if (!spec) continue;
                if (/^load[A-Z]/.test(d.name.text)) loaderToSpec.set(d.name.text, spec);
                nameToSpec.set(d.name.text, spec);
            }
        }
    }
    const routes = [];
    const attr = (open, name) => {
        const a = open.attributes.properties.find((p) => ts.isJsxAttribute(p) && p.name.getText(sf) === name);
        if (!a) return undefined;
        if (!a.initializer) return true;
        if (ts.isStringLiteral(a.initializer)) return a.initializer.text;
        return a.initializer;
    };
    const joinPath = (base, p) => (p === undefined ? base : p.startsWith('/') ? p : (base.endsWith('/') ? base : base + '/') + p);
    const visit = (node, base) => {
        if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) {
            const open = ts.isJsxElement(node) ? node.openingElement : node;
            if (open.tagName.getText(sf) === 'Route') {
                const p = attr(open, 'path');
                const idx = attr(open, 'index');
                const full = idx ? (base || '/') : joinPath(base, typeof p === 'string' ? p : undefined);
                const comps = new Set();
                const el = attr(open, 'element');
                if (el && typeof el !== 'string' && typeof el !== 'boolean') {
                    const walk = (n) => {
                        if ((ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) && /^[A-Z]/.test(n.tagName.getText(sf))) comps.add(n.tagName.getText(sf).split('.')[0]);
                        ts.forEachChild(n, walk);
                    };
                    walk(el);
                }
                if (typeof p === 'string' || idx || p === undefined) routes.push({ full: full || '/', comps: [...comps] });
                if (p && typeof p !== 'string' && ts.isJsxExpression(p) && p.expression && ts.isTemplateExpression(p.expression)) {
                    let m = node.parent;
                    while (m && !(ts.isCallExpression(m) && ts.isPropertyAccessExpression(m.expression) && m.expression.name.text === 'map')) m = m.parent;
                    const arrName = m && ts.isIdentifier(m.expression.expression) ? m.expression.expression.text : null;
                    const decl = arrName && sf.statements.flatMap((s) => (ts.isVariableStatement(s) ? [...s.declarationList.declarations] : []))
                        .find((d) => ts.isIdentifier(d.name) && d.name.text === arrName && d.initializer && ts.isArrayLiteralExpression(d.initializer));
                    const head = p.expression.head.text;
                    const tail = p.expression.templateSpans.length === 1 ? p.expression.templateSpans[0].literal.text : null;
                    if (decl && tail !== null) {
                        for (const e of decl.initializer.elements) if (ts.isStringLiteral(e)) routes.push({ full: joinPath(base, head + e.text + tail), comps: [...comps] });
                    }
                }
                if (ts.isJsxElement(node)) node.children.forEach((c) => visit(c, full || base));
                return;
            }
            if (ts.isJsxElement(node)) { node.children.forEach((c) => visit(c, base)); return; }
        }
        ts.forEachChild(node, (c) => visit(c, base));
    };
    visit(sf, '');
    return { routes, nameToSpec, staticSpecs };
}

/** 路由表 → 解析器。LIVE = App.tsx 里不在 DEP_ROUTES 的路由;DEP = 冻结表(不管 App.tsx 里还在不在)。
 *  根兜底 `/*` 不参与:它匹配一切,留着它任何坏链都会被判成「在役」。 */
function makeResolver(appRoutes, depRoutes = DEP_ROUTES, depSet = DEP_SET) {
    const table = [];
    for (const r of appRoutes) {
        if (r.full === '/*' || r.full.includes('${') || r.full.startsWith('/{')) continue;
        if (depSet.has(r.full)) continue;
        table.push({ path: r.full, dep: false });
    }
    for (const [p] of depRoutes) table.push({ path: p, dep: true });
    const score = (routePath, cand) => {
        const rs = segs(routePath), cs = segs(cand);
        let s = 0;
        for (let i = 0; i < rs.length; i++) {
            const r = rs[i];
            if (r === '*') return s + 1;
            if (i >= cs.length) return null;
            if (r.startsWith(':')) { s += 2; continue; }
            if (r === cs[i]) { s += 3; continue; }
            return null;
        }
        return rs.length === cs.length ? s : null;
    };
    const exact = (cand) => {
        let best = null;
        for (const r of table) {
            const s = score(r.path, cand);
            if (s !== null && (!best || s > best.s)) best = { s, r };
        }
        return best ? (best.r.dep ? 'DEP' : 'LIVE') : 'NONE';
    };
    /* 段前缀:候选的每一段都要被路由的同位段接住(路由的 :param / * 接任何段) */
    const coversPrefix = (routePath, cand) => {
        const rs = segs(routePath), cs = segs(cand);
        if (cs.length > rs.length && rs[rs.length - 1] !== '*') return false;
        for (let i = 0; i < cs.length; i++) {
            const r = rs[i];
            if (r === undefined) return false;
            if (r === '*') return true;
            if (r.startsWith(':') || r === cs[i]) continue;
            return false;
        }
        return true;
    };
    const prefix = (cand) => {
        if (table.some((r) => !r.dep && coversPrefix(r.path, cand))) return 'LIVE';
        if (table.some((r) => r.dep && coversPrefix(r.path, cand))) return 'DEP';
        return 'NONE';
    };
    return { exact, prefix };
}

const ASSET = /\.(png|jpe?g|gif|svg|webp|ico|mp4|webm|mp3|css|js|json|md|pdf|xlsx?|csv|txt|woff2?|ttf|html|zip)(\?|#|$)/i;
function candidateFrom(raw) {
    if (!raw.startsWith('/') || raw.startsWith('//')) return null;
    if (/^\/(api|assets|static|fonts|images|img|uploads|ws|docs|tutorials)(\/|$)/.test(raw)) return null;
    if (ASSET.test(raw)) return null;
    if (!/^\/[A-Za-z0-9_\-:.~$*{}/?=&#%]*$/.test(raw)) return null;
    let c = raw.split('#')[0].split('?')[0];
    c = c.replace(/\$\{[^}]*\}/g, ':p').replace(/\/+$/, '') || '/';
    return c;
}
/** 字面量的语法上下文(穿过 + / 模板 / 三元 / 括号 / as / || / ?? 往上找) */
function contextOf(n, sf) {
    let cur = n;
    let p = n.parent;
    while (p && ((ts.isBinaryExpression(p) && [ts.SyntaxKind.PlusToken, ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken].includes(p.operatorToken.kind))
        || ts.isTemplateSpan(p) || ts.isTemplateExpression(p) || ts.isParenthesizedExpression(p) || ts.isAsExpression(p)
        || (ts.isConditionalExpression(p) && p.condition !== cur))) { cur = p; p = p.parent; }
    if (!p) return 'other:top';
    if (ts.isCallExpression(p) || ts.isNewExpression(p)) {
        if (ts.isCallExpression(p) && p.expression === cur) return 'other:callee';
        return 'call:' + p.expression.getText(sf).replace(/\s+/g, '');
    }
    if (ts.isJsxAttribute(p)) return 'jsx:' + p.name.getText(sf);
    if (ts.isJsxExpression(p) && p.parent && ts.isJsxAttribute(p.parent)) return 'jsx:' + p.parent.name.getText(sf);
    if (ts.isPropertyAssignment(p) && p.initializer === cur) return 'prop:' + p.name.getText(sf);
    if (ts.isBinaryExpression(p) && [ts.SyntaxKind.EqualsEqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsEqualsToken, ts.SyntaxKind.EqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsToken].includes(p.operatorToken.kind)) return 'cmp';
    if (ts.isBinaryExpression(p) && p.operatorToken.kind === ts.SyntaxKind.EqualsToken && p.right === cur) return 'assign:' + p.left.getText(sf).replace(/\s+/g, '');
    if (ts.isArrayLiteralExpression(p)) return 'array';
    if (ts.isVariableDeclaration(p)) return 'var';
    if (ts.isReturnStatement(p) || ts.isArrowFunction(p)) return 'return';
    return 'other:' + ts.SyntaxKind[p.kind];
}
/** API 调用语境:这些实参是接口路径片段(`apiGet('/writing/...')` 等),不是页面路由 */
const API_CALLEE = /(^|\.)(authFetch|fetch|mutate|call|request|apiGet\w*|apiPost|apiPut|apiPatch|apiDelete|sc(Get|Post|Put|Patch|Delete)|organization(Public)?Request|invalidate\w*|get|post|put|patch|delete)$/;
const isApiCtx = (ctx) => ctx.startsWith('call:') && API_CALLEE.test(ctx.slice(5));
const isPrefixCtx = (ctx) => /^call:.*\.(startsWith|includes)$/.test(ctx);
/** 导航语境:点了就跳过去的地方 */
const isNavCtx = (ctx) => /^call:(.*\.)?navigate$/.test(ctx) || /^call:(window\.)?location\.(assign|replace)$/.test(ctx)
    || /^call:(window\.)?open$/.test(ctx) || /^assign:.*location\.href$/.test(ctx)
    || /^jsx:(to|href)$/.test(ctx) || /^prop:(to|href|target|link|route|goto|return_to)$/.test(ctx);

function scanLiterals(text, fileName, resolver) {
    const kind = fileName.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
    const sf = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true, kind);
    const rows = [];
    const visit = (n) => {
        let raw = null;
        if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) raw = n.text;
        else if (ts.isTemplateExpression(n)) raw = n.getText(sf).slice(1, -1);
        if (raw !== null) {
            const p = n.parent;
            const isModuleSpec = p && (ts.isImportDeclaration(p) || ts.isExportDeclaration(p)
                || (ts.isCallExpression(p) && p.expression.kind === ts.SyntaxKind.ImportKeyword) || ts.isLiteralTypeNode(p));
            const cand = isModuleSpec ? null : candidateFrom(raw);
            if (cand) {
                const ctx = contextOf(n, sf);
                const res = isPrefixCtx(ctx) ? resolver.prefix(cand) : resolver.exact(cand);
                rows.push({ raw, cand, ctx, res, line: sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1 });
            }
            if (ts.isTemplateExpression(n)) ts.forEachChild(n, visit);
            return;
        }
        ts.forEachChild(n, visit);
    };
    visit(sf);
    return rows;
}
const violS1 = (rows) => rows.filter((r) => !isApiCtx(r.ctx) && r.res === 'DEP');
const violS2 = (rows) => rows.filter((r) => isNavCtx(r.ctx) && r.res !== 'LIVE');

/* 模块解析 + import 边 */
const EXTS = ['', '.ts', '.tsx', '.js', '.jsx', '.mjs', '/index.ts', '/index.tsx', '/index.js'];
function resolveSpec(spec, fromFile) {
    const s = spec.replace(/\?raw$/, '');
    let base;
    if (s.startsWith('@/')) base = join(SRC, s.slice(2));
    else if (s.startsWith('.')) base = resolve(dirname(fromFile), s);
    else return null;
    for (const e of EXTS) { const f = base + e; if (existsSync(f) && statSync(f).isFile()) return f; }
    return null;
}
function importEdges(text, fileName) {
    const kind = fileName.endsWith('.tsx') ? ts.ScriptKind.TSX : fileName.endsWith('.ts') ? ts.ScriptKind.TS : ts.ScriptKind.JSX;
    const sf = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true, kind);
    const out = [];
    const visit = (n) => {
        if (ts.isImportDeclaration(n) && ts.isStringLiteral(n.moduleSpecifier)) out.push(n.moduleSpecifier.text);
        else if (ts.isExportDeclaration(n) && n.moduleSpecifier && ts.isStringLiteral(n.moduleSpecifier)) out.push(n.moduleSpecifier.text);
        else if (ts.isCallExpression(n) && n.expression.kind === ts.SyntaxKind.ImportKeyword && n.arguments[0] && ts.isStringLiteral(n.arguments[0])) out.push(n.arguments[0].text);
        else if (ts.isImportTypeNode(n) && ts.isLiteralTypeNode(n.argument) && ts.isStringLiteral(n.argument.literal)) out.push(n.argument.literal.text);
        ts.forEachChild(n, visit);
    };
    visit(sf);
    return out;
}
const M3_SPEC = /^@\/components\/m3(\/|$)/;
const isM3File = (f) => rel(f).startsWith('src/components/m3/');

/* ══ 读真树 ═══════════════════════════════════════════════════════════════ */
const APP = join(SRC, 'App.tsx');
const MAIN = join(SRC, 'main.tsx');
let appText;
try { appText = readFileSync(APP, 'utf8'); } catch (err) { unusable('App.tsx 读不到', err); }
const app = parseApp(appText, APP);
const resolver = makeResolver(app.routes);
const liveRoutes = app.routes.filter((r) => !DEP_SET.has(r.full));
const roots = new Set();
for (const spec of app.staticSpecs) { const f = resolveSpec(spec, APP); if (f) roots.add(f); }
for (const r of liveRoutes) for (const c of r.comps) { const spec = app.nameToSpec.get(c); const f = spec && resolveSpec(spec, APP); if (f) roots.add(f); }
if (existsSync(MAIN)) for (const spec of importEdges(readFileSync(MAIN, 'utf8'), MAIN)) { const f = resolveSpec(spec, MAIN); if (f && f !== APP) roots.add(f); }
const texts = new Map();
const readText = (f) => { if (!texts.has(f)) texts.set(f, readFileSync(f, 'utf8')); return texts.get(f); };
const closure = new Set();
const m3Imports = [];
{
    const stack = [...roots];
    while (stack.length) {
        const f = stack.pop();
        if (closure.has(f)) continue;
        closure.add(f);
        if (!/\.(tsx?|jsx?|mjs)$/.test(f)) continue;
        for (const spec of importEdges(readText(f), f)) {
            const to = resolveSpec(spec, f);
            if (!isM3File(f) && (M3_SPEC.test(spec) || (to && isM3File(to)))) m3Imports.push(`${rel(f)} → ${spec}`);
            if (to && !closure.has(to)) stack.push(to);
        }
    }
}
const scanned = [...closure].filter((f) => f !== APP && f !== MAIN && /\.(tsx?|jsx?)$/.test(f));
const allRows = [];
const perFile = new Map();
for (const f of scanned) {
    const rows = scanLiterals(readText(f), f, resolver);
    perFile.set(rel(f), rows);
    for (const r of rows) allRows.push({ ...r, file: rel(f) });
}

/* ── S3 [开源 E3 · 前端 · 2026-10-01 · WO_322]:流向导航的站内路由字面量,必须解析到 App.tsx 里**现存**的某条路由 ──
 *   来由:顾问对话页随 E3 删了,还挂着路由的顾问团页里两个出口
 *   (`const base = cond ? `/s/…` : `/advisors/chat/${id}``,再 `navigate(advisorChatPath(id))`)成了死链,
 *   build / S1 / S2 全绿 —— S1/S2 只扫**在役**闭包(顾问团在 DEP 表里,不进闭包),S2 又只认字面量**直接**
 *   出现在导航语境里的形态,先赋给变量 / 当函数返回值、再传给 navigate 的看不见。
 *   S3 补两处:
 *     ① 闭包从 App.tsx **全部**路由出发(DEP 路由只要还挂在 App.tsx 上,用户就点得到);
 *     ② 语境 = 导航语境,或字面量所在的变量 / 函数**流向**导航(同文件内,变量 → 返回值 → 函数名,至多三跳,
 *        终点是 navigate / location.assign·replace / window.open / location.href = / JSX to·href 里出现这个名字)。
 *   解析表 = App.tsx 现存全部路由(不分在役 / DEP;根兜底 `/*` 不算,它匹配一切)。
 *   🔴 自曝:跨文件的流向(A 文件导出路由串、B 文件 navigate)看不见;键 → 路由映射(上面自曝 ①)仍看不见。 */
const makeExistResolver = (appRoutes) => makeResolver(appRoutes, [], new Set());
const TRANSPARENT = (p, cur) => (ts.isBinaryExpression(p) && [ts.SyntaxKind.PlusToken, ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken].includes(p.operatorToken.kind))
    || ts.isTemplateSpan(p) || ts.isTemplateExpression(p) || ts.isParenthesizedExpression(p) || ts.isAsExpression(p)
    || (ts.isConditionalExpression(p) && p.condition !== cur);
function ownerName(fn) {
    if (ts.isFunctionDeclaration(fn) && fn.name) return fn.name.text;
    let cur = fn, p = fn.parent;
    while (p && (ts.isCallExpression(p) || ts.isParenthesizedExpression(p) || ts.isAsExpression(p))) { cur = p; p = p.parent; }
    if (p && ts.isVariableDeclaration(p) && ts.isIdentifier(p.name)) return p.name.text;
    if (p && ts.isPropertyAssignment(p)) return p.name.getText();
    return null;
}
const isFnLike = (n) => ts.isArrowFunction(n) || ts.isFunctionExpression(n) || ts.isFunctionDeclaration(n) || ts.isMethodDeclaration(n);
/** 一个表达式节点「往上走」落在哪个名字上:变量初值 ⇒ 变量名;return / 箭头体 ⇒ 所在函数名 */
function sinkName(node) {
    let cur = node, p = node.parent;
    while (p && TRANSPARENT(p, cur)) { cur = p; p = p.parent; }
    if (!p) return null;
    if (ts.isVariableDeclaration(p) && p.initializer === cur && ts.isIdentifier(p.name)) return p.name.text;
    if (ts.isReturnStatement(p)) { let f = p.parent; while (f && !isFnLike(f)) f = f.parent; return f ? ownerName(f) : null; }
    if (ts.isArrowFunction(p) && p.body === cur) return ownerName(p);
    return null;
}
function navSinkIds(sf) {
    const ids = new Set();
    const collect = (n) => { if (ts.isIdentifier(n)) ids.add(n.text); ts.forEachChild(n, collect); };
    const visit = (n) => {
        if (ts.isCallExpression(n)) {
            const callee = n.expression.getText(sf).replace(/\s+/g, '');
            if (/^(.*\.)?navigate$/.test(callee) || /^(window\.)?location\.(assign|replace)$/.test(callee) || /^(window\.)?open$/.test(callee)) n.arguments.forEach(collect);
        }
        if (ts.isBinaryExpression(n) && n.operatorToken.kind === ts.SyntaxKind.EqualsToken && /location\.href$/.test(n.left.getText(sf).replace(/\s+/g, ''))) collect(n.right);
        if (ts.isJsxAttribute(n) && /^(to|href)$/.test(n.name.getText(sf)) && n.initializer) collect(n.initializer);
        ts.forEachChild(n, visit);
    };
    visit(sf);
    return ids;
}
function scanS3(text, fileName, res) {
    const kind = fileName.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
    const sf = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true, kind);
    const sinks = navSinkIds(sf);
    // 名字 → 它又流到哪些名字(同文件一跳):在 return / 变量初值里出现了这个名字
    const next = new Map();
    const visitIds = (n) => {
        if (ts.isIdentifier(n) && !(ts.isVariableDeclaration(n.parent) && n.parent.name === n)) {
            const to = sinkName(n);
            if (to && to !== n.text) { if (!next.has(n.text)) next.set(n.text, new Set()); next.get(n.text).add(to); }
        }
        ts.forEachChild(n, visitIds);
    };
    visitIds(sf);
    const flows = (name) => {
        let frontier = new Set([name]);
        const seen = new Set(frontier);
        for (let hop = 0; hop <= 3 && frontier.size; hop++) {
            for (const x of frontier) if (sinks.has(x)) return true;
            const nf = new Set();
            for (const x of frontier) for (const y of next.get(x) || []) if (!seen.has(y)) { seen.add(y); nf.add(y); }
            frontier = nf;
        }
        return false;
    };
    const rows = [];
    const visit = (n) => {
        let raw = null;
        if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) raw = n.text;
        else if (ts.isTemplateExpression(n)) raw = n.getText(sf).slice(1, -1);
        if (raw !== null) {
            const p = n.parent;
            const isModuleSpec = p && (ts.isImportDeclaration(p) || ts.isExportDeclaration(p)
                || (ts.isCallExpression(p) && p.expression.kind === ts.SyntaxKind.ImportKeyword) || ts.isLiteralTypeNode(p));
            // S3 先把 `${…}` 归一成占位再判形状:candidateFrom 的字符集不收括号,`/s?${params.toString()}` 这种
            //   插值里带调用的整串会被它扔掉(WIP 对照臂上顾问团页 :147 就这样漏过);S1/S2 读数不受影响
            const cand = isModuleSpec ? null : candidateFrom(raw.replace(/\$\{[^}]*\}/g, '${p}'));
            if (cand) {
                const ctx = contextOf(n, sf);
                const via = isNavCtx(ctx) ? 'nav' : (() => { const nm = sinkName(n); return nm && flows(nm) ? `flow:${nm}` : null; })();
                if (via) rows.push({ raw, cand, ctx, via, res: res.exact(cand), line: sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1 });
            }
            if (ts.isTemplateExpression(n)) ts.forEachChild(n, visit);
            return;
        }
        ts.forEachChild(n, visit);
    };
    visit(sf);
    return rows;
}
const violS3 = (rows) => rows.filter((r) => r.res === 'NONE');
const routedRoots = new Set(roots);
for (const r of app.routes) for (const c of r.comps) { const spec = app.nameToSpec.get(c); const f = spec && resolveSpec(spec, APP); if (f) routedRoots.add(f); }
const routedClosure = new Set();
{
    const stack = [...routedRoots];
    while (stack.length) {
        const f = stack.pop();
        if (routedClosure.has(f)) continue;
        routedClosure.add(f);
        if (!/\.(tsx?|jsx?|mjs)$/.test(f)) continue;
        for (const spec of importEdges(readText(f), f)) { const to = resolveSpec(spec, f); if (to && !routedClosure.has(to)) stack.push(to); }
    }
}
const existResolver = makeExistResolver(app.routes);
const s3Rows = [];
for (const f of routedClosure) {
    if (f === APP || f === MAIN || !/\.(tsx?|jsx?)$/.test(f)) continue;
    for (const r of scanS3(readText(f), f, existResolver)) s3Rows.push({ ...r, file: rel(f) });
}

console.log('S 段 · 真判据');
check(app.routes.length >= MIN_ROUTES && DEP_ROUTES.length === DEP_COUNT && RETIRED_ROUTE_COUNT === DEP_COUNT && closure.size >= MIN_CLOSURE && allRows.length >= MIN_LITERALS,
    'S0 分母自证:路由表 / 在役闭包 / 路由字面量都真扫到了,冻结的 DEP 路由一条不少',
    `路由 ${app.routes.length}(在役 ${liveRoutes.length})· DEP ${DEP_ROUTES.length}/${DEP_COUNT} · 闭包 ${closure.size} 个文件 · 字面量 ${allRows.length} 处`);
const s1 = violS1(allRows);
check(s1.length === 0, 'S1 🔴 在役代码里指向要删路由(C+D)的字面量 = 0(API 调用语境除外;startsWith/includes 按段前缀)',
    s1.length ? s1.slice(0, 6).map((r) => `${r.file}:${r.line} ${r.raw}`).join(' | ') + (s1.length > 6 ? ` …共 ${s1.length}` : '') : `0 / ${allRows.length} 处`);
const s2 = violS2(allRows);
check(s2.length === 0, 'S2 🔴 导航语境(navigate / to / href / 导航属性)里的路由都解析到在役路由',
    s2.length ? s2.slice(0, 6).map((r) => `${r.file}:${r.line} ${r.raw}→${r.res}`).join(' | ') + (s2.length > 6 ? ` …共 ${s2.length}` : '') : `${allRows.filter((r) => isNavCtx(r.ctx)).length} 处全在役`);
check(routedClosure.size >= closure.size && s3Rows.length >= allRows.filter((r) => isNavCtx(r.ctx)).length,
    'S3a 分母自证:全部路由闭包 ⊇ 在役闭包,流向导航的字面量不少于在役闭包里的导航语境字面量',
    `闭包 ${routedClosure.size} 个文件 · 流向导航 ${s3Rows.length} 处(其中经变量 / 返回值 ${s3Rows.filter((r) => r.via !== 'nav').length})`);
const s3 = violS3(s3Rows);
check(s3.length === 0, 'S3 🔴 流向导航的站内路由(含仍挂着路由的 DEP 页、含先赋变量再导航)都解析到 App.tsx 现存路由',
    s3.length ? s3.slice(0, 6).map((r) => `${r.file}:${r.line} ${r.raw}(${r.via})`).join(' | ') + (s3.length > 6 ? ` …共 ${s3.length}` : '') : `0 / ${s3Rows.length} 处`);
check(m3Imports.length === 0, 'M 🔴 在役代码 import components/m3/** = 0(值与类型都算)',
    m3Imports.length ? m3Imports.slice(0, 6).join(' | ') + (m3Imports.length > 6 ? ` …共 ${m3Imports.length}` : '') : '0 处');

console.log('\nE1 冻结差分表');
const litIn = (file, raw) => (perFile.get(file) || []).some((r) => r.raw === raw);
const fileIn = (file) => perFile.has(file);
function rowHolds([file, old, act, neu]) {
    if (act === 'out') return !fileIn(file);
    if (!existsSync(join(ROOT, file))) return act === 'delete';
    const txt = readText(join(ROOT, file));
    const rows = fileIn(file) ? perFile.get(file) : scanLiterals(txt, join(ROOT, file), resolver);
    const has = (x) => rows.some((r) => r.raw === x);
    if (act === 'delete') return !has(old);
    if (act === 'repoint') return !has(old) && has(neu);
    if (act === 'keep') return has(old) && rows.filter((r) => r.raw === old).every((r) => r.res === 'LIVE');
    return false;
}
check(E1_DIFF.length === E1_ROW_COUNT, 'E1a 差分表行数 = 100(改前 114,E3 删 M3 工具件 14 行;多一行少一行都红)', `${E1_DIFF.length} 行`);
check(E1_DIFF.every((r) => E1_REASONS[r[4]]), 'E1b 每一行都有理由(理由键都在理由表里)');
const e1Bad = E1_DIFF.filter((r) => !rowHolds(r));
check(e1Bad.length === 0, 'E1c 🔴 100 行逐行成立(改指 / 删除 / 保留 / 出闭包)',
    e1Bad.length ? e1Bad.slice(0, 5).map((r) => `${r[0]} ${r[1]} ${r[2]}`).join(' | ') : `${E1_DIFF.filter((r) => r[2] === 'delete').length} 删 · ${E1_DIFF.filter((r) => r[2] === 'repoint').length} 改指 · ${E1_DIFF.filter((r) => r[2] === 'keep').length} 保留 · ${E1_DIFF.filter((r) => r[2] === 'out').length} 出闭包`);
check(E1_BROKEN.length === E1_BROKEN_COUNT && E1_BROKEN.every((r) => rowHolds(r)), 'E1d 4 条「解析不到任何路由」的坏链修复逐行成立',
    E1_BROKEN.filter((r) => !rowHolds(r)).map((r) => `${r[0]} ${r[1]}`).join(' | ') || '4/4');

console.log('\nE0 上提映射');
const shimOk = (oldRel, newRel) => {
    const f = join(ROOT, oldRel);
    if (!existsSync(f)) return true;
    const code = ts.transpileModule(readFileSync(f, 'utf8'), { compilerOptions: { removeComments: true, module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.Preserve } }).outputText
        .split('\n').map((l) => l.trim()).filter(Boolean);
    const target = '@/' + newRel.replace(/^src\//, '').replace(/\.tsx?$/, '');
    return code.length === 1 && code[0] === `export * from '${target}';`;
};
const e0Bad = E0_LIFT.filter(([o, n, keepShim]) => !existsSync(join(ROOT, n)) || (keepShim ? !shimOk(o, n) : existsSync(join(ROOT, o))));
check(e0Bad.length === 0, 'E0a 🔴 8 个上提件都在新位置;要留转发的旧位置只剩一句 `export * from 新位置`,其余旧位置已不存在',
    e0Bad.map((r) => r[0]).join(' | ') || '8/8');
const toolIdFile = join(SRC, 'services/m3/toolId.ts');
const procap = existsSync(join(SRC, 'services/m3/proCapabilities.ts')) ? readText(join(SRC, 'services/m3/proCapabilities.ts')) : '';
check(existsSync(toolIdFile) && /export type ToolId\s*=/.test(readFileSync(toolIdFile, 'utf8')) && /from '\.\/toolId'/.test(procap) && !importEdges(procap, 'proCapabilities.ts').some((x) => /components\/m3\//.test(x)),
    'E0b ToolId 抽成独立类型文件,proCapabilities 只从它取(不再拖旧工具注册表进闭包)');
const backBtn = join(SRC, 'components/workbench/BackToClientButton.tsx');
check(existsSync(backBtn) && !/\/m3\//.test(readFileSync(backBtn, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')) && /navigate\(`\/my-clients\/\$\{brandId\}`\)/.test(readFileSync(backBtn, 'utf8')),
    'E0c 🔴 BackToClientButton 代码里没有 /m3/(不留指向 M3 的默认值),返回目标是 /my-clients/:id');

console.log('\nC 段 · 锁自己的牙(合成源,同一组函数)');
const synth = (code) => scanLiterals(code, 'synthetic.tsx', resolver);
check(violS1(synth("export const A = () => navigate('/m3/queue');")).length === 1, "C1 塞一个 navigate('/m3/queue') ⇒ S1 红(工单点名的反臂)");
check(violS2(synth("export const A = () => navigate('/definitely-not-a-route');")).length === 1, "C2 navigate('/definitely-not-a-route') ⇒ S2 红(谁都解析不到)");
check(violS1(synth('export const A = (id: number) => [navigate(\'/my-clients\'), navigate(`/my-clients/${id}`)];')).length === 0
    && violS2(synth('export const A = (id: number) => [navigate(\'/my-clients\'), navigate(`/my-clients/${id}`)];')).length === 0,
    'C3 对照:在役路由(含模板参数)⇒ S1 / S2 都不红');
check(violS1(synth("export const A = (p: string) => p.startsWith('/s/');")).length === 0
    && violS1(synth("export const A = (p: string) => p.startsWith('/social');")).length === 1,
    "C4 段前缀:startsWith('/s/') 合法(在役 /s/:token),startsWith('/social') 红");
check(violS1(synth("export const A = () => apiGet('/advisors');")).length === 0
    && violS1(synth("export const A = () => navigate('/advisors');")).length === 1,
    "C5 API 调用语境 apiGet('/advisors') 不算路由;同一个串进 navigate 就红");
const m3Probe = (code) => importEdges(code, 'synthetic.tsx').filter((s) => M3_SPEC.test(s)).length;
check(m3Probe("import { StagePill } from '@/components/m3/parts';") === 1 && m3Probe("import { StagePill } from '@/components/workbench/parts';") === 0,
    "C6 import '@/components/m3/parts' ⇒ M 探测器命中;'@/components/workbench/parts' ⇒ 不命中");
const noM3App = makeResolver([{ full: '/', comps: [] }, { full: '/my-clients', comps: [] }]);
check(noM3App.exact('/m3/queue') === 'DEP' && noM3App.exact('/my-clients') === 'LIVE',
    'C7 冻结表不靠 App.tsx:合成路由表里没有 /m3/*,/m3/queue 照样判 DEP(E3 删路由之后的语义)');
const s3Res = makeExistResolver([{ full: '/', comps: [] }, { full: '/advisors', comps: [] }, { full: '/advisors/chat/:advisorId', comps: [] }]);
const hubShape = (target) => 'export function Hub({ cond }: { cond: boolean }) {\n'
    + '  const chatPath = useCallback((advisorId: string, conv?: string) => {\n'
    + '    const base = cond ? `/s/market/chat/${advisorId}` : `' + target + '/${advisorId}`;\n'
    + '    return conv ? `${base}?conv=${conv}` : base;\n'
    + '  }, [cond]);\n'
    + '  return <Card onChat={(id: string) => navigate(chatPath(id))} />;\n}\n';
const c8 = violS3(scanS3(hubShape('/advisors/gone'), 'synthetic.tsx', s3Res));
check(c8.some((r) => r.raw.startsWith('/advisors/gone')) && c8.some((r) => r.raw.startsWith('/s/market')),
    'C8 顾问团页原样写法(变量 → useCallback 返回 → navigate(fn(id)))指向不存在的路由 ⇒ S3 红(两个分支都抓到)',
    c8.map((r) => `${r.raw}(${r.via})`).join(' | '));
const c9 = violS3(scanS3(hubShape('/advisors/chat'), 'synthetic.tsx', s3Res));
check(c9.length === 1 && c9[0].raw.startsWith('/s/market'), 'C9 对照:同一写法改指现存路由 ⇒ 那一支不红(另一支 /s/market 仍红)',
    c9.map((r) => r.raw).join(' | '));
const c10 = scanS3("export const KEYS = ['/writing/not-a-route']; const p = '/also-not-a-route'; export const f = () => apiGet(p);", 'synthetic.tsx', s3Res);
check(c10.length === 0, 'C10 同样解析不到的串,只进数组 / 只赋变量后传给 API 调用 ⇒ 不进 S3(不误伤 SWR 键与接口路径)',
    c10.map((r) => r.raw).join(' | ') || '0 处');
const c11 = violS3(scanS3('export const A = (q: URLSearchParams) => { const mk = () => `/gone?${q.toString()}`; return navigate(mk()); };', 'synthetic.tsx', s3Res));
check(c11.length === 1, 'C11 插值里带调用的模板(`/gone?${q.toString()}`)经函数返回流向导航 ⇒ S3 红(WIP 对照臂上 :147 那种形态)',
    c11.map((r) => r.raw).join(' | ') || '0 处(漏了)');

console.log('');
if (failed > 0) { console.log(`FAIL ${failed} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
