#!/usr/bin/env node
/**
 * 变异 runner:证明 test-provider-downgrade-ux.mjs 的每条判据都真能抓到「本 bug」。
 *
 * 做法:把真文件按每种回退方式改一次 → 跑锁 → 必须报红(KILLED)→ 还原。
 * 🔴 锁跑在子进程里,避免 ESM 模块缓存让「已杀死」被误报成「存活」。
 * 🔴 任何一条变异改不上去(锚点没命中)一律记 ERROR,不许当成 SKIP 放过 ——
 *    改不上去说明锚点已经漂了,那条判据的判别力这一轮根本没被验证。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const HERE = fileURLToPath(new URL('.', import.meta.url));
const WIZARD = join(HERE, '..', 'src', 'pages', 'Admin', 'ProviderDowngradeWizard.tsx');
const LOCK = join(HERE, 'test-provider-downgrade-ux.mjs');

/** [名字, 锚点, 替换成什么, 'last'?] —— 每条都是一种真实的退化写法。
 *  🔴 两个降级按钮的 disabled 表达式逐字相同,所以要分别打第一处(耐久方案确认)
 *     和最后一处(一步降级);只改一处的变异会被另一处掩护成「存活」。 */
const MUTATIONS = [
  ['M01 逐项状态回退成全量摊开',
    'blockedCategories.map(', 'snapshot.categories.map('],
  ['M02 耐久方案确认按钮不再看 readiness',
    'disabled={busy || !snapshot?.ready || !reasonReady || !destinationReady}',
    'disabled={busy || !reasonReady || !destinationReady}'],
  ['M03 一步降级按钮不再看 readiness',
    'disabled={busy || !snapshot?.ready || !reasonReady || !destinationReady}',
    'disabled={busy || !reasonReady || !destinationReady}', 'last'],
  ['M14 一步降级按钮不再看归属去向',
    'disabled={busy || !snapshot?.ready || !reasonReady || !destinationReady}',
    'disabled={busy || !snapshot?.ready || !reasonReady}', 'last'],
  ['M15 耐久方案确认按钮不再看归属去向',
    'disabled={busy || !snapshot?.ready || !reasonReady || !destinationReady}',
    'disabled={busy || !snapshot?.ready || !reasonReady}'],
  ['M04 归属去向给了静默默认值',
    'useState<UpstreamDestination | null>(null)',
    "useState<UpstreamDestination>('rebind_upstream')"],
  ['M05 拿掉「终结直属上游」动作',
    'const terminateChannelRelationship = async',
    'const unusedLegacyHandler = async'],
  ['M06 终结动作不再置空上游',
    'upstream_user_id: null,', 'upstream_user_id: 0,'],
  ['M07 渠道关系分类丢了处理路径',
    '  channel_relations:', '  channel_relations_renamed:'],
  ['M08 拿掉一步降级出口',
    'const downgradeDirectly = async', 'const unusedDirect = async'],
  ['M09 降级动作不再拦归属去向未选',
    'if (!reasonReady || !destinationReady) return;', 'if (!reasonReady) return;'],
  ['M10 降级后不再回绑归属',
    '/commercial-service-binding`', '/legacy-noop-binding`'],
  ['M11 归属交代步骤永不出现',
    'const awaitingAttribution = downgraded && Boolean(initialUpstream)',
    'const awaitingAttribution = false && downgraded && Boolean(initialUpstream)'],
  ['M12 写入前不再现取版本',
    'const fresh = await loadDetail();', 'const fresh = versions && null;'],
  ['M13 归属去向条件恒真(等于没必选)',
    'const destinationReady = !destinationRequired || Boolean(destination);',
    'const destinationReady = true;'],
];

const original = readFileSync(WIZARD, 'utf8');
const runLock = () => spawnSync(process.execPath, [LOCK], { encoding: 'utf8' });

// 前置:未变异时锁必须是绿的,否则后面的「红」证明不了任何事。
const baseline = runLock();
if (baseline.status !== 0) {
  console.log('🔴 基线就是红的 —— 变异结果没有判别力,先修锁或修实现');
  console.log(baseline.stdout);
  process.exit(1);
}
console.log('✅ 基线绿 · 开始变异\n');

/* 🔴 牙证:前置在本 runner 里真的会红(抛异常、自还原)。 */
proveGuardHasTeeth(WIZARD);

let killed = 0; let survived = 0; let errored = 0;
try {
  for (const [name, anchor, replacement, where] of MUTATIONS) {
    const at = where === 'last' ? original.lastIndexOf(anchor) : original.indexOf(anchor);
    if (at === -1) {
      console.log(`  ⚠️  ERROR ${name} — 锚点未命中(判别力本轮未验证):${anchor.slice(0, 50)}`);
      errored++;
      continue;
    }
    if (where === 'last' && original.indexOf(anchor) === at) {
      console.log(`  ⚠️  ERROR ${name} — 只找到 1 处锚点,「最后一处」变异退化成「第一处」`);
      errored++;
      continue;
    }
    const mutated = original.slice(0, at) + replacement + original.slice(at + anchor.length);
    try { assertRulerWorks(WIZARD); } catch (e) { console.log(`  ${e.message}`); process.exit(3); }
    writeFileSync(WIZARD, mutated, 'utf8');
    if (!syntaxOk(WIZARD)) {
      console.log(`  ${NOT_LANDED_SYNTAX}`);
      errored++;
      writeFileSync(WIZARD, original, 'utf8');
      continue;
    }
    const result = runLock();
    if (result.status !== 0) { console.log(`  ✅ KILLED   ${name}`); killed++; }
    else { console.log(`  ❌ SURVIVED ${name}`); survived++; }
  }
} finally {
  writeFileSync(WIZARD, original, 'utf8');
}

const restored = readFileSync(WIZARD, 'utf8');
if (restored !== original) {
  console.log('\n🔴 还原失败 —— 源文件与变异前不一致');
  process.exit(1);
}

console.log(`\n变异 ${MUTATIONS.length}:KILLED ${killed} · SURVIVED ${survived} · ERROR ${errored}`);
const bad = survived + errored;
console.log(bad ? '🔴 变异未全红' : '✅ 变异全红 · 判据有判别力');
process.exit(bad ? 1 : 0);
