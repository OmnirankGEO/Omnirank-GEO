#!/usr/bin/env node
/**
 * 缺口作战计划 · 前端门禁的判别力自证
 *
 * 跑法:cd frontend && node scripts/mutation_runner_gap_plan.mjs
 *
 * 三关(与 mutation_runner_publish_layout.mjs 同形态,不另造第二套):
 *   ① 变异真的改到了字节
 *   ② 门禁脚本退出码非 0(真转红)
 *   ③ 还原后逐字节一致
 *
 * 🔴 全程 Buffer 二进制读写:文本模式在 Windows 上会把 LF 翻成 CRLF,
 *    于是第 ③ 关恒红,然后人就会去把第 ③ 关关掉。本包已经因为这个坑
 *    在后端 runner 上栽过一次(M7 锚点被 CRLF 打歪)。
 * 🔴 变异期间禁止任何 git 操作。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
// 🔴 两道门禁一起跑(返工 2026-08-11):源码门禁只能扫写法,**真渲染门禁才跑得了行为**。
//    上一轮的假修复正是"写法门禁全绿 + 没有任何一条真跑过 bug 场景"造出来的。
//    变异只要被其中任一道抓住就算杀死;都不红才是判别力不足。
const GATES = [
  join(ROOT, 'scripts', 'test-gap-plan-ui.mjs'),
  join(ROOT, 'scripts', 'test-gap-plan-render.mjs'),
];

const MUTATIONS = [
  {
    id: 'F1',
    file: 'src/components/gapPlan/GapTaskCard.tsx',
    desc: '把「去写这篇」写死成 JSX 文本(绕开服务端 action.label)',
    from: '                {action.label}',
    to: '                去写这篇',
  },
  {
    id: 'F2',
    file: 'src/components/gapPlan/tone.ts',
    desc: '硬编码原型暗色色值(违反 B8 主题合规)',
    from: "    dot: 'bg-emerald-500',",
    to: "    dot: 'bg-[#4ade80]',",
  },
  {
    id: 'F3',
    file: 'src/components/gapPlan/GapPlanExecutionView.tsx',
    desc: '界面上直出供应商名(术语泄漏)',
    from: '          <p className="text-sm text-foreground leading-relaxed">{snapshot.summary.headline}</p>',
    to: '          <p className="text-sm text-foreground leading-relaxed">DeepSeek 说：{snapshot.summary.headline}</p>',
  },
  {
    id: 'F4',
    file: 'src/components/gapPlan/GapTaskCard.tsx',
    desc: '占位按钮点击不再 return(会发写请求)',
    from: '    if (!action.enabled) return;                    // 占位按钮:不发任何请求',
    to: '    // MUTATION F4:守卫拆掉',
  },
  {
    id: 'F5',
    file: 'src/components/gapPlan/EvidenceChain.tsx',
    desc: 'R9 未启用态改成失败话术',
    from: '          {evidence.notice}',
    to: '          回查失败',
  },
  {
    id: 'F6',
    file: 'src/components/xiaobang/XiaobangDrawer.tsx',
    desc: '小榜不再传客户上下文(工单 R7 只改一头)',
    /*
     * 🔴 [2026-09-20 重锚] 旧锚 `contextRefs: gapContextRefs,` —— 那个变量名
     *    后来就叫 `contextRefs`(不再有 `gap` 前缀)⇒ 锚命中 0,这一发**一直没下成**。
     *    而本 runner 把「锚没命中」计进了**未被杀死**(与"锁没牙"混成一个数)——
     *    正是「没下成 ≠ 存活」那件事的同款。
     *    ⇒ 钉「上下文到底传没传给 useXiaobangChat」这件行为。
     */
    from: '    contextRefs,\n    onMessagesChange: upsertSession,',
    to: '    onMessagesChange: upsertSession,',
  },

  // ── WO_GAPPLAN_RELOCATION_A 2026-08-10 新增(阶段 1 + 阶段 2)──
  {
    id: 'F7',
    file: 'src/components/gapPlan/GapPlanExecutionView.tsx',
    desc: '阶段 1:分档失效 → 未付款报价在报价页照样展开整段执行面',
    from: "  return gate.open ? 'execution' : 'gate_hint';",
    to: "  return 'execution';  // MUTATION F7:分档拆掉",
  },
  {
    id: 'F13',
    file: 'src/components/gapPlan/GapPlanExecutionView.tsx',
    // 🔴 返工 WO_GAPPLAN_REWORK_2026-08-11 修 4 点名的那一条。
    //    它打的是 **bug 场景本身**:删掉"gate 缺失 → 旧行为"那一分支,
    //    旧后端组合会退回上一轮那个假修复的形态(要么崩、要么错退成提示)。
    //    旧 F12(把 `?.` 改成 `.`)已删除 —— 它证明的只是"门禁认得出守卫写法"。
    desc: '🔴 阶段 1:删掉 gate 缺失回退分支 → 旧后端组合不再回退到旧行为',
    from: "  if (!gate) return 'execution';          // 旧后端不下发 → 旧行为",
    to: "  // MUTATION F13:回退分支删掉",
  },
  {
    id: 'F8',
    file: 'src/components/gapPlan/GapPlanExecutionView.tsx',
    desc: '阶段 1:提示文案写死成前端中文串(第二份字典,会与服务端字典漂移)',
    from: '            {snapshot.execution_gate?.hint}',
    to: '            客户确认报价后自动开放执行计划',
  },
  {
    id: 'F9',
    file: 'src/components/gapPlan/GapPlanPreview.tsx',
    desc: '阶段 2:售前版改用 GapTaskCard → 执行按钮渲染给客户',
    from: "import { GapTaskRationale } from './GapTaskRationale';",
    to: "import { GapTaskCard } from './GapTaskCard';  // MUTATION F9",
  },
  {
    id: 'F10',
    file: 'src/components/gapPlan/GapTaskCard.tsx',
    desc: '阶段 1:四列断点改回按视口(xl:)→ 1366/1440 笔记本上重新撑破',
    from: '        <div className="flex flex-col gap-4 @min-[1184px]:flex-row @min-[1184px]:items-start">',
    to: '        <div className="flex flex-col gap-4 xl:flex-row xl:items-start">',
  },
  {
    id: 'F11',
    file: 'src/pages/Selection/SelectionPage.tsx',
    desc: '阶段 2:客户页摘掉售前版挂载(后端算了但没人渲染 = 整包惰性)',
    from: '        <GapPlanPreview plan={data.delivery_plan} />',
    to: '        {/* MUTATION F11:挂载摘掉 */}',
  },
];

const gateFails = () => {
  for (const gate of GATES) {
    try {
      execFileSync(process.execPath, [gate], { cwd: ROOT, stdio: 'pipe' });
    } catch {
      return true;                      // 任一道非 0 = 转红
    }
  }
  return false;                         // 两道都 0 = 门禁绿 = 变异存活
};

console.log('第 0 关 · 基线门禁必须绿(基线红的话后面全是假阳性)');
if (gateFails()) {
  console.log('🔴 基线门禁就是红的,拒绝继续');
  process.exit(1);
}
console.log('✅ 基线绿\n');

let failed = 0;
/* 🔴 牙证:前置在本 runner 里真的会红(抛异常、自还原)。 */
proveGuardHasTeeth(join(ROOT, MUTATIONS[0].file));

for (const m of MUTATIONS) {
  const path = join(ROOT, m.file);
  const original = readFileSync(path);            // Buffer,不是 string
  const text = original.toString('utf8');

  if (!text.includes(m.from)) {
    console.log(`🔴 ${m.id} 锚点没命中(实现改过了?):${m.from.trim().slice(0, 60)}`);
    failed++;
    continue;
  }

  const mutated = Buffer.from(text.replace(m.from, m.to), 'utf8');
  if (mutated.equals(original)) {
    console.log(`🔴 ${m.id} 变异没改到任何字节(空操作)`);
    failed++;
    continue;
  }

  try { assertRulerWorks(path); } catch (err) { console.log(`  ${err.message}`); process.exit(3); }
  writeFileSync(path, mutated);
  let turnedRed;
  try {
    if (!syntaxOk(path)) {
      console.log(`  ${NOT_LANDED_SYNTAX}`);
      continue;
    }
    turnedRed = gateFails();
  } finally {
    writeFileSync(path, original);
  }

  const restored = readFileSync(path);
  if (!restored.equals(original)) {
    console.log(`🔴 ${m.id} 还原后逐字节不一致 —— 停手,先修 runner`);
    process.exit(1);
  }

  if (turnedRed) {
    console.log(`✅ ${m.id} · ${m.desc} → 门禁转红`);
  } else {
    console.log(`🔴 ${m.id} · ${m.desc} → 门禁没红(变异存活)`);
    console.log('   分诊:①门禁没打在这条接线上 ②这个变异是空操作');
    failed++;
  }
}

console.log('\n' + '='.repeat(60));
if (failed) {
  console.log(`🔴 ${failed} 条未被杀死 —— 门禁判别力不足`);
  process.exit(1);
}
console.log(`✅ ${MUTATIONS.length}/${MUTATIONS.length} 条变异全部被门禁杀死`);
