#!/usr/bin/env node
/**
 * 缺口作战计划 · 前端门禁(P4)
 *
 * 🔴 不接 build 链(照 mobilepay-20260807 先例):frontend/package.json 被
 *    publayout-20260804 + natdlg-20260806 双声明,本包刻意不碰它。
 *    接线交 Deploy —— 建议 build 串里加:`node scripts/test-gap-plan-ui.mjs`
 *
 * 跑法:cd frontend && node scripts/test-gap-plan-ui.mjs
 * 退出码:0=全绿 1=有红
 *
 * 判据设计原则:每条"必须命中"都配一条"必须不命中"。单向断言证明不了判别力
 * (.deploy_toolkit/README 那条铁律)。
 */
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (p) => readFileSync(join(ROOT, p), 'utf8');

let failed = 0;
// 🔴 判据条数**机器统计**(返工订正 2):上一轮我把输出里的 ✅ 数了一遍报"74 条全绿",
//    其中一条是末尾那行总结,真实判据 73 条。人数数字会错,计数器不会。
let asserted = 0;
const ok = (m) => console.log(`  ✅ ${m}`);
const bad = (m) => { console.log(`  🔴 ${m}`); failed++; };
const check = (cond, m) => { asserted += 1; return cond ? ok(m) : bad(m); };

// [WO_GAPPLAN_RELOCATION_A 2026-08-10] GapPlanSection.tsx 已改名 GapPlanExecution.tsx,
// 并新增售前三件(preview / rationale / 客户页落点)。锚点跟着实现走 ——
// 🔴 但**判别力不靠这份路径表证明**,靠 mutation_runner_gap_plan.mjs 重跑:
//    改完锚点还能把 F1-F11 全杀掉,才算这份门禁没有被改绿。
const FILES = {
  section: 'src/components/gapPlan/GapPlanExecution.tsx',
  // [返工 2026-08-11] 渲染层拆到 View:容器自己 fetch 时 bug 场景根本渲染不出来,
  // 判据只能退化成扫写法。拆开后 scripts/test-gap-plan-render.mjs 能真跑三态。
  sectionView: 'src/components/gapPlan/GapPlanExecutionView.tsx',
  preview: 'src/components/gapPlan/GapPlanPreview.tsx',
  rationale: 'src/components/gapPlan/GapTaskRationale.tsx',
  card: 'src/components/gapPlan/GapTaskCard.tsx',
  chain: 'src/components/gapPlan/EvidenceChain.tsx',
  tone: 'src/components/gapPlan/tone.ts',
  api: 'src/lib/gapPlanApi.ts',
  quote: 'src/pages/Quote/OnlineQuoteFlow.tsx',
  selection: 'src/pages/Selection/SelectionPage.tsx',
  writing: 'src/pages/Writing/WritingHall.tsx',
  drawer: 'src/components/xiaobang/XiaobangDrawer.tsx',
  hook: 'src/hooks/useXiaobangChat.ts',
};

console.log('── 0. 文件族齐全(判据可用性:文件不在,后面全是"空即通过") ──');
for (const [k, p] of Object.entries(FILES)) {
  check(existsSync(join(ROOT, p)), `${k}: ${p}`);
}
if (failed) { console.log('🔴 文件缺失,后续判据不可信'); process.exit(1); }

const section = read(FILES.section);
const preview = read(FILES.preview);
const rationale = read(FILES.rationale);
const card = read(FILES.card);
const chain = read(FILES.chain);
const tone = read(FILES.tone);
const api = read(FILES.api);
const quote = read(FILES.quote);
const selection = read(FILES.selection);
const writing = read(FILES.writing);
const drawer = read(FILES.drawer);
const hook = read(FILES.hook);
const gapAll = [section, read(FILES.sectionView), preview, rationale, card, chain, tone, api]
  .join('\n');

/**
 * 剥注释(块注释 + 行注释),**保留字符串字面量**。
 * 🔴 存在的理由:第一版直接扫原文,结果被自己写的两行注释
 *    (「能不能"去写这篇"由服务端决定」/「深链:去写这篇」)判成"硬编码"。
 *    注释里提到一个词 ≠ 界面上渲染这个词 —— 判据必须打在会渲染的那一层。
 * 🔴 但只剥注释不够:真正要防的是**字符串字面量/JSX 文本**里出现它,
 *    所以下面用的是带引号/尖括号的锚点,不是裸子串。
 */
const stripComments = (src) => src
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .split('\n').filter((l) => !l.trim().startsWith('//')).join('\n');
const gapCode = stripComments(gapAll);

// ══════════════════════════════════════════════════════════════
console.log('\n── 1. 容量守卫在服务端,前端不自己判(合同判据 #10) ──');
// 前端**不得**出现按容量决定按钮的逻辑;按钮从服务端 actions 渲染。
check(!/available_articles\s*[><=]/.test(gapAll),
  '前端没有按 available_articles 做条件判断(容量判断只在服务端)');
check(!/capacity[^\n]*\?\s*['"]去写/.test(gapAll),
  '没有"按容量决定要不要显示去写"的三元表达式');
// 「去写这篇」这几个字**不得**硬编码在前端 —— 它是服务端 action 的 label。
// 锚点带引号/尖括号:只抓**字符串字面量与 JSX 文本**,不抓注释里的提及。
const HARDCODED_LABEL = /(['"`]去写这篇['"`]|>\s*去写这篇\s*<)/;
check(!HARDCODED_LABEL.test(gapCode),
  '「去写这篇」没有作为字面量/JSX 文本写死在前端(它是服务端下发的 action.label)');
// 反向对照:证明这条正则真的会响
check(HARDCODED_LABEL.test("const x = '去写这篇'"), '反向对照:字面量锚点对样例确实命中');
// 反向对照:证明上面那条不是恒真 —— 前端确实在渲染 action.label
check(/action\.label/.test(card), '反向对照:任务卡确实渲染 action.label(判据有东西可查)');
// 🔴 锚点必须是**这条守卫本身**,不是"文件里有 !action.enabled 且有 return;"。
//    第一版就是那种松写法 —— 变异 F4 把守卫整行删掉后,
//    `disabled={!action.enabled || busy}` 和别处的 return; 仍然让它绿。
//    松判据 = 静默放行,比没有判据更坏。
// [返工自查 2026-08-11] 这条原来钉死 `return;` 逐字形态,大括号写法会误红。
// 与 §9 那批"写法锁"同族(虽然它不与任何正确修法冲突),顺手放宽到允许 `{ return; }`,
// 判别力不变:反向对照证明 disabled 表达式仍然不会被误判成守卫。
const ENABLED_GUARD = /if\s*\(!action\.enabled\)\s*\{?\s*return;?/;
check(ENABLED_GUARD.test(card),
  '占位按钮(enabled=false)点击直接 return,不发任何请求(合同 §13.3 · 写法不限)');
// 反向对照:证明这条正则不是恒真
check(!ENABLED_GUARD.test('disabled={!action.enabled || busy}'),
  '反向对照:disabled 表达式不会被误判成守卫');

// ══════════════════════════════════════════════════════════════
console.log('\n── 2. 主题合规:走语义 token,不抄原型暗色(B8 / 合同 §13.5) ──');
const hexes = gapAll.match(/#[0-9a-fA-F]{6}\b/g) || [];
check(hexes.length === 0, `gapPlan 组件里没有硬编码色值(实测 ${hexes.length} 处)`);
check(!/style=\{\{[^}]*background/.test(gapAll), '没有 inline style 写背景色');
// 双态:每个 tone 都要同时给亮色和 dark: 变体
check(/dark:text-emerald-300/.test(tone) && /dark:text-amber-300/.test(tone),
  'tone 表给了 dark: 变体(亮/暗双态)');
check(/text-muted-foreground|border-border|bg-card|bg-muted/.test(gapAll),
  '用的是语义 token(muted-foreground / border / card)');
// 反向对照:证明这条 hex 扫描真的会响
check(/#[0-9a-fA-F]{6}\b/.test('color: #4ade80'), '反向对照:hex 正则对 #4ade80 确实命中');

// ══════════════════════════════════════════════════════════════
console.log('\n── 3. 术语泄漏(D3:枚举/供应商名/成本词零命中) ──');
const FORBIDDEN = [
  'DeepSeek', 'deepseek', 'Kimi', '豆包', '千问', '通义', 'moonshot', 'dashscope',
  'qwen', 'openrouter', '进货价', '采购成本', '上游成本', 'markup',
  'our_price_yuan', 'our_price_points', 'upstream_cost',
  'target_share', 'SOV',
];
for (const term of FORBIDDEN) {
  if (gapCode.includes(term)) bad(`gapPlan 组件里出现内部术语:${term}`);
}
if (!FORBIDDEN.some((t) => gapCode.includes(t))) ok(`${FORBIDDEN.length} 个内部术语零命中`);
// 内部状态枚举不得硬编码成界面文案(它们只能作为 code 字段比较)
const ENUM_AS_TEXT = /['"`](attack_absence|entity_gap|source_gap|ready_to_execute|capacity_zero)['"`]\s*}/;
check(!ENUM_AS_TEXT.test(gapCode), '内部状态枚举没有被当成界面文案直接渲染');
// 反向对照:证明这套扫描不是恒绿
check(FORBIDDEN.some((t) => 'DeepSeek 生成'.includes(t)), '反向对照:术语表对样例串确实命中');

// ══════════════════════════════════════════════════════════════
console.log('\n── 4. 唯一落点:不新增导航(合同 §13.1) ──');
check(!/<Route\s/.test(gapAll), 'gapPlan 组件没有注册任何新路由');
check(!/sidebar|Sidebar/.test(gapAll), '没有碰 sidebar 分组');
// 🔴 剥注释再断言(本文件第 56 行那条注释里的同一个坑,这轮又踩了一次):
//    OnlineQuoteFlow 里留了一行「组件改名 GapPlanSection → GapPlanExecution」的注释,
//    直接扫原文会把它判成"旧名还在引用"。判据必须打在会编译的那一层。
const quoteCode = stripComments(quote);
check(quoteCode.includes('<GapPlanExecution'), '交付计划执行面挂在现役 OnlineQuoteFlow 里');
// 反向对照:旧名已彻底退场,不留一个两边都能满足的模糊态
check(!/GapPlanSection/.test(quoteCode)
      && !existsSync(join(ROOT, 'src/components/gapPlan/GapPlanSection.tsx')),
  '旧名 GapPlanSection 已彻底退场(真代码与文件都不在)');
// 落点必须在「客户已选词清单」之后、签约确认之前。
// 🔴 判据锚点必须是**真代码**,不能是任何一段中文串 —— 第一版用 indexOf('签约确认')
//    命中的是我自己写的那行注释("…签约确认之前"),于是顺序判断恒错。
//    同一个坑记忆里写过:剥注释再断言,别让注释自己把判据打歪。
const idxList = quoteCode.indexOf('<span>客户已选词清单</span>');
const idxGap = quoteCode.indexOf('<GapPlanExecution');
const idxSign = quoteCode.indexOf('<CardTitle className="text-base">签约确认</CardTitle>');
check(idxList > 0 && idxGap > 0 && idxSign > 0, '三个锚点都是真代码(判据有靶子)');
check(idxList > 0 && idxGap > idxList && idxSign > idxGap,
  `落点顺序正确:已选词清单(${idxList}) → 交付计划(${idxGap}) → 签约确认(${idxSign})`);

// ══════════════════════════════════════════════════════════════
console.log('\n── 5. 写作中心:清参数【之前】消费 plan_item_id(B6) ──');
// 🔴 同上:先剥掉 // 注释再定位,否则命中的是我自己那行
//    「必须在下面那个 setSearchParams({}) 之前读掉」的注释(第一版实测踩到)。
const writingCode = writing.split('\n').filter((l) => !l.trim().startsWith('//')).join('\n');
const idxRead = writingCode.indexOf("searchParams.get('plan_item_id')");
const idxClear = writingCode.indexOf('setSearchParams({})');
check(idxRead > 0, 'WritingHall 读了 plan_item_id(剥注释后的真代码)');
check(idxClear > 0, 'WritingHall 里确有 setSearchParams({}) 这一步(判据有靶子)');
check(idxRead > 0 && idxClear > 0 && idxRead < idxClear,
  `plan_item_id 在清空参数之前被读掉(读=${idxRead} < 清=${idxClear})`);
check(/fetchPlanItem\(/.test(writing), '通过 fetchPlanItem 校验计划项 + 代际');
check(/gapPlanNotice/.test(writing),
  '计划打不开时给可执行解释(不落空白页,合同 §3.2)');
// 预填不得触发生成/扣费/发布
check(!/gapPlanItem[\s\S]{0,400}?(generateTitles|startWriting|handlePublish)\(/.test(writing),
  '预填卡不触发生成/发布(只做定位与说明)');

// ══════════════════════════════════════════════════════════════
console.log('\n── 6. 小榜:两头都改了(工单 R7) ──');
check(/contextRefs/.test(drawer), 'XiaobangDrawer 传了 contextRefs(调用处这头)');
check(/context_refs/.test(hook), 'useXiaobangChat 请求体带 context_refs(请求体那头)');
check(/gap_assistant/.test(hook), 'hook 认识服务端下发的结构化建议');
check(/target_route/.test(hook),
  '跳转路由来自服务端签发的 target_route(模型文本不携带可执行 URL)');
// LLM 不得拼 URL:hook 里不能出现"从模型文本里抠路径"的逻辑
check(!/content\.match\(\/\\\/[a-z]/.test(hook), 'hook 不从模型文本里抠路径');

// ══════════════════════════════════════════════════════════════
console.log('\n── 7. 证据链组件可复用 + R9 未启用态 ──');
check(!/gapPlanApi|OnlineQuoteFlow|quote_id/.test(chain.replace(/import type.*\n/g, '')),
  'EvidenceChain 不依赖报价业务(门户后续可直接复用)');
check(/notice/.test(chain), 'R9:渲染服务端下发的"回查未启用"态文案');
check(!/失败|错误|error/i.test(chain.replace(/\/\*[\s\S]*?\*\//g, '')),
  'R9:长期无进展不显示成失败/错误');
check(/aria-label|sr-only/.test(chain), '可访问性:状态不只靠颜色(有文字/aria)');

// ══════════════════════════════════════════════════════════════
console.log('\n── 8. 可访问性与响应式(合同 §10/§11) ──');
// [返工 2026-08-11] 折叠控件与 aria-live 随渲染层搬到了 View,判据跟着搬(不是放宽)
check(/aria-expanded/.test(read(FILES.sectionView)), '折叠控件带 aria-expanded');
check(/aria-live/.test(read(FILES.sectionView)) || /aria-live/.test(chain), '状态更新用 aria-live');
check(/h-11 sm:h-10/.test(card), '主按钮移动端 44px / 桌面 40px');
check(/@min-\[1184px\]:w-\[112px\]/.test(card) && /@min-\[1184px\]:min-w-\[420px\]/.test(card)
      && /@min-\[1184px\]:w-\[380px\]/.test(card) && /@min-\[1184px\]:w-\[224px\]/.test(card),
  '四列宽度按合同 §4:112 / 420 / 380 / 224');
check(/@min-\[1184px\]:flex-row/.test(card), '四列档按**容器**宽度触发,以下自动折行');

// ══════════════════════════════════════════════════════════════
console.log('\n── 8b. 任务卡横向溢出(工单阶段 1 · 顺带解决) ──');
// 🔴 病根不是"断点选小了",是断点问错了对象:`xl:` 问的是**视口**,
//    而报价页左侧另占 md:w-72(288px),1366/1440 笔记本上卡片实宽 ~1000px
//    就已经进了四列档 → 每次都撑破。判据因此必须钉在"问容器"这件事上。
check(/@container/.test(card), '任务卡开了容器查询上下文(@container)');
check(!/\bxl:(w-|min-w-|flex-row)/.test(card),
  '四列相关断点里不再有按视口的 xl:(问错对象的那一版已退场)');
// 反向对照:证明这条正则确实认得出 xl: 那种写法
check(/\bxl:(w-|min-w-|flex-row)/.test('<div className="xl:w-[112px]">'),
  '反向对照:xl: 正则对样例确实命中');
check(/overflow-x-auto/.test(card),
  '固定宽列外层有 overflow-x-auto 兜底(列宽被调大时卡内滚动,不撑破整页)');
check((card.match(/min-w-0/g) || []).length >= 2,
  '固定宽/弹性列都给了 min-w-0(缺它 flex 子项 min-width:auto 会把行顶开)');

// ══════════════════════════════════════════════════════════════
console.log('\n── 9. 阶段 1:报价页按执行闸分档(不是无条件摘) ──');
/**
 * 🔴🔴 返工 WO_GAPPLAN_REWORK_2026-08-11 · 修 2:**这一节原来全是"写法锁"**。
 *
 * 原来的三条:`/if\s*\(!snapshot\.execution_gate\?\.open\)/` 要求守卫逐字长这样、
 * `!/snapshot\.execution_gate\.open/` 要求必须带可选链、
 * `/\{snapshot\.execution_gate\.hint\}/` 要求 hint **原样无可选链**出现。
 * 后两条互相打架:第三条把「给 hint 也加保护」这个正确修法**反向锁死** ——
 * 谁修那个 bug,门禁自己先转红。这就是假修复能溜过去的机制性原因之一。
 *
 * 现在改成:**行为在 scripts/test-gap-plan-render.mjs 里真渲染验证**(三态各跑一遍),
 * 这里只留"结构上还在不在"的粗判据,一律不钉具体表达式写法。
 */
const sectionView = read(FILES.sectionView);
const sectionViewCode = stripComments(sectionView);
// 分档决策必须是一个可被语义断言的具名出口,而不是散落在 JSX 里的条件
check(/export function resolveExecutionMode/.test(sectionViewCode),
  '分档决策有具名出口 resolveExecutionMode(真渲染判据按语义断言它的三态)');
// 三态齐全:gate 缺失 / 关 / 开 —— 只查"字面量都出现过",具体写法不管
check(/'gate_hint'/.test(sectionViewCode) && /'execution'/.test(sectionViewCode),
  '两个终态字面量都在(gate_hint / execution)');
// hint 由服务端下发:只要求"渲染的是一个含 hint 的表达式",不要求写法
check(/\{[^}\n]*\bhint\b[^}\n]*\}/.test(sectionViewCode),
  '提示文案渲染服务端下发的 hint 表达式(前端不写第二份中文字典 · 写法不限)');
// 反向对照:证明上面那条不会把硬编码中文串判成合格
check(!/\{[^}\n]*\bhint\b[^}\n]*\}/.test('<p>客户确认报价后自动开放执行计划</p>'),
  '反向对照:硬编码中文串不满足 hint 表达式判据');
// 分档后锚点必须还在,否则小榜「交付计划在哪」的向导会指向不存在的 DOM。
// 🔴 计数必须在**剥注释后**的源码上做:上一轮报"实测 4 处"其中一处命中在注释里,
//    真实 JSX 只有 3 处(加载态当时没有锚点)。本轮已给加载态补上,真值 = 4。
const helpAnchors = (sectionViewCode.match(/data-help-target="gap-plan-section"/g) || []).length;
check(helpAnchors >= 4,
  `加载/错误/分档/正常四态都保留 data-help-target 锚点(剥注释后实测 ${helpAnchors} 处 JSX)`);
// 容量判断仍然只在服务端:分档读的是 execution_gate,不是自己算额度
check(!/execution_gate[^\n]*available_articles/.test(sectionViewCode),
  '分档没有顺手在前端拿 available_articles 兜底');
// 类型不许撒谎:execution_gate 必须是可选,否则 tsc 对未守卫解引用一声不吭
check(/execution_gate\?:\s*GapExecutionGate/.test(api),
  'GapPlanSnapshot.execution_gate 声明为**可选**(让 tsc 从撒谎变成第二道守卫)');
check(!/\n\s*execution_gate:\s*GapExecutionGate/.test(api),
  '反向对照:没有留下必填声明(必填 = build exit 0 不构成任何证据)');

// ══════════════════════════════════════════════════════════════
console.log('\n── 10. 阶段 2:客户售前版(纯展示 · 零写操作 · 零请求) ──');
// 同上:先剥注释 —— GapPlanPreview 的文件头解释了"为什么不复用 GapTaskCard",
// 扫原文会被这句解释判成"复用了"。
const previewCode = stripComments(preview);
check(!/GapTaskCard/.test(previewCode),
  '售前版**不复用** GapTaskCard(它内建去写/填链接/渠道不可用三个写操作)');
// 反向对照:证明这条正则真的会响
check(/GapTaskCard/.test("import { GapTaskCard } from './GapTaskCard'"),
  '反向对照:GapTaskCard 正则对样例确实命中');
check(/GapTaskRationale/.test(previewCode) && /GapTaskRationale/.test(stripComments(card)),
  '售前版与执行面共用同一个纯展示块 GapTaskRationale(不各写一份文案)');
check(!/onAction|action\.|actions\.map/.test(previewCode),
  '售前版代码里没有任何 action 渲染路径');
check(!/api\.(get|post|put|delete)|fetch\(|\/api\//.test(previewCode),
  '售前版不发任何请求(数据随 GET /api/s/:token 一起下发)');
// 客户页落点
check(/<GapPlanPreview/.test(selection), '售前版挂在客户选词报价页 SelectionPage 上');
check(/delivery_plan/.test(selection), '客户页读的是服务端下发的 delivery_plan 字段');
// 🔴 客户侧不得出现「能否进入」话术的硬编码兜底:屏蔽是服务端做的,
//    前端一旦自己写一份 access 文案,就等于把服务端裁剪掉的东西又补回来了。
const ACCESS_WORDS = ['能否进入', '进不去', '渠道尚未核实', '需要代发', '自行发布'];
const previewLeaks = ACCESS_WORDS.filter((w) => previewCode.includes(w));
check(previewLeaks.length === 0, `售前版没有硬编码 access 话术(命中 ${previewLeaks.join('/') || '无'})`);
check(ACCESS_WORDS.some((w) => '这个网站进不去'.includes(w)),
  '反向对照:access 词表对样例串确实命中');

console.log('\n' + '='.repeat(60));
if (failed) { console.log(`🔴 ${asserted} 条判据中 ${failed} 条未通过`); process.exit(1); }
console.log(`✅ 缺口作战计划前端门禁全绿 · 判据 ${asserted} 条(机器计数,不含本行)`);
