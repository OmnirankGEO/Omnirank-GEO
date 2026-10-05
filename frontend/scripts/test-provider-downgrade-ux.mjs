#!/usr/bin/env node
/**
 * 锁:服务商降级向导必须「给了提示就给出路」,且顺序陷阱靠控件而不是靠文案。
 *
 * 背景(工单 WO_PROVIDER_DOWNGRADE_UNUSABLE_2026-08-06):
 *  - 拒绝话术把 12 个计数器一股脑列出来,十一个是 0,不说哪一项真非零,也不说去哪处理;
 *  - 降级有一条顺序不可换的三步链(终结渠道关系 → 降级 → 交代归属去向)。
 *    先降级会让账号 agent_level=0,change_channel_relationship 从此永久拒绝。
 *
 * 🔴 本锁只扫 frontend/src,不引用任何后端文件(引用后端文件的锁不许进 build 链)。
 * 🔴 扫描前先剥注释:否则锁会抓到向导里我自己写的说明文字,变成恒绿。
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = fileURLToPath(new URL('.', import.meta.url));
const WIZARD = join(HERE, '..', 'src', 'pages', 'Admin', 'ProviderDowngradeWizard.tsx');

let failed = 0;
const bad = (msg) => { console.log(`  ❌ ${msg}`); failed++; };
const ok = (msg) => console.log(`  ✅ ${msg}`);

/** 剥行注释与块注释(JSX 里的花括号包裹块注释也一并剥掉)。 */
export function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
}

/** 取出包含 needle 的那一个 <Button ...> 片段。判据要打在具体控件上,不是打在全文。 */
export function buttonFragment(src, needle) {
  const parts = src.split('<Button');
  const hit = parts.find((part) => part.includes(needle));
  if (!hit) return '';
  // 箭头函数的 `=>` 里也有 `>`,直接找第一个 `>` 会把开标签截断在 onClick 上,
  // 后面的 disabled 就永远扫不到(第一版实测就是这么全红的)。先把箭头挡掉。
  const end = hit.replace(/=>/g, '=@').indexOf('>');
  return end === -1 ? hit : hit.slice(0, end);
}

/** 后端 provider_downgrade_snapshot 的 7 个分类 + schema 兜底。每一个都必须有处理路径。 */
const BACKEND_CATEGORY_KEYS = [
  'clients', 'orders', 'inventory', 'earnings',
  'responsibilities', 'channel_relations', 'platform_manufacturer', 'schema',
];

/**
 * 判据全集。每条都写成 (name, predicate)，供本锁与变异 runner 共用 ——
 * 变异 runner 用同一组 predicate 才能证明「这条判据抓得到本 bug」。
 */
export const CHECKS = [
  {
    id: 'only-unresolved-rendered',
    desc: '逐项状态只渲染未处理项(不许再把已归零的分类一起摊开)',
    test: (s) => /categories\s*\|\|\s*\[\]\)\s*\.filter\(\s*\(item\)\s*=>\s*!item\.resolved\s*\)/.test(s)
      && /blockedCategories\.map\(/.test(s),
  },
  {
    id: 'no-blind-category-dump',
    desc: '必须不命中:不许直接把 snapshot.categories 全量 map 出来',
    test: (s) => !/snapshot\.categories\.map\(/.test(s),
  },
  {
    id: 'every-category-has-handling',
    desc: '每个后端分类键都配了「去哪里处理」',
    test: (s) => {
      const block = s.match(/CATEGORY_HANDLING[\s\S]*?\n\};/);
      if (!block) return false;
      return BACKEND_CATEGORY_KEYS.every((key) => new RegExp(`\\b${key}\\s*:`).test(block[0]));
    },
  },
  {
    id: 'ordering-trap-is-driven-by-a-control',
    desc: '顺序陷阱由控件驱动:向导自己提供「终结直属上游」动作,打到渠道关系端点',
    test: (s) => /const terminateChannelRelationship = async/.test(s)
      && /channel-relationship`/.test(s)
      && /upstream_user_id:\s*null/.test(s),
  },
  {
    id: 'one-step-downgrade-when-clean',
    desc: '十二项全零时有一步降级出口(不必先建耐久方案)',
    test: (s) => /const downgradeDirectly = async/.test(s)
      && /business-identity`/.test(s)
      && /business_identity:\s*'ordinary_user'/.test(s),
  },
  {
    id: 'direct-downgrade-gated',
    desc: '必须不命中:一步降级按钮必须同时受 readiness / 原因 / 归属去向三道闸',
    // 🔴 不能只问「源码里有没有这串 disabled」—— 两个降级按钮的 disabled 表达式
    //    逐字相同,那样写只要留下任意一个就恒绿(第一版就是这么漏的)。按按钮定位。
    test: (s) => {
      const frag = buttonFragment(s, 'data-testid="downgrade-direct"');
      return Boolean(frag) && /!snapshot\?\.ready/.test(frag)
        && /!reasonReady/.test(frag) && /!destinationReady/.test(frag);
    },
  },
  {
    id: 'plan-confirm-gated',
    desc: '必须不命中:耐久方案的确认按钮同样受三道闸(不许留一条绕过通道)',
    test: (s) => {
      const frag = buttonFragment(s, 'void confirm()');
      return Boolean(frag) && /!snapshot\?\.ready/.test(frag)
        && /!reasonReady/.test(frag) && /!destinationReady/.test(frag);
    },
  },
  {
    id: 'attribution-destination-required',
    desc: '有上游时归属去向必选,且直接卡住降级动作(不是只写一句提醒)',
    test: (s) => /const destinationRequired = Boolean\(initialUpstream\)/.test(s)
      && /const destinationReady = !destinationRequired \|\| Boolean\(destination\)/.test(s)
      && /if \(!reasonReady \|\| !destinationReady\) return;/.test(s),
  },
  {
    id: 'attribution-settled-after-downgrade',
    desc: '降级后必须交代上游归属去向:回绑走商业归属 SSOT,或显式声明解除',
    test: (s) => /const awaitingAttribution = downgraded && Boolean\(initialUpstream\)/.test(s)
      && /commercial-service-binding`/.test(s)
      && /declare_released/.test(s),
  },
  {
    id: 'no-silent-attribution-default',
    desc: '必须不命中:归属去向不许有静默默认值(必须由管理员显式选)',
    test: (s) => /useState<UpstreamDestination \| null>\(null\)/.test(s)
      && !/useState<UpstreamDestination>\('rebind_upstream'\)/.test(s)
      && !/useState<UpstreamDestination>\('declare_released'\)/.test(s),
  },
  {
    id: 'fresh-versions-per-write',
    desc: '连做三步写入前要现取乐观锁版本(用打开那一刻的快照第二步就撞 409)',
    test: (s) => (s.match(/const fresh = await loadDetail\(\);/g) || []).length >= 3,
  },
];

const raw = readFileSync(WIZARD, 'utf8');
const src = stripComments(raw);

console.log('§1 服务商降级向导 · 判据');
for (const check of CHECKS) {
  if (check.test(src)) ok(`${check.id} — ${check.desc}`);
  else bad(`${check.id} — ${check.desc}`);
}

console.log('§2 反向对照(判据自证 · 不依赖变异 runner)');
{
  // 把「只渲染未处理项」改回全量摊开,两条判据必须同时报红。
  const regressed = src
    .replace(/categories\s*\|\|\s*\[\]\)\s*\.filter\(\s*\(item\)\s*=>\s*!item\.resolved\s*\)/, 'categories || [])')
    .replace(/blockedCategories\.map\(/, 'snapshot.categories.map(');
  const stillGreen = CHECKS
    .filter((c) => c.id === 'only-unresolved-rendered' || c.id === 'no-blind-category-dump')
    .filter((c) => c.test(regressed));
  if (stillGreen.length === 0) ok('回退成「全量摊开」后 2 条判据都报红 → 有判别力');
  else bad(`回退后仍有 ${stillGreen.length} 条判据是绿的:${stillGreen.map((c) => c.id).join(', ')}`);
}
{
  // 把归属去向改成有默认值,「不许静默默认」必须报红。
  const regressed = src.replace(
    /useState<UpstreamDestination \| null>\(null\)/,
    "useState<UpstreamDestination>('rebind_upstream')",
  );
  const check = CHECKS.find((c) => c.id === 'no-silent-attribution-default');
  if (!check.test(regressed)) ok('给归属去向加默认值后判据报红 → 有判别力');
  else bad('给归属去向加默认值后判据仍绿 → 锁写废了');
}
{
  // 必须不命中:未改动的源码不该有任何一条判据是红的(证明没有假阳)。
  const reds = CHECKS.filter((c) => !c.test(src));
  if (reds.length === 0) ok('未改动源码零假阳');
  else bad(`未改动源码上有 ${reds.length} 条判据报红:${reds.map((c) => c.id).join(', ')}`);
}
{
  // 必须不命中:剥注释必须真的生效 —— 只写在注释里的接线不算数。
  const commentOnly = `// const terminateChannelRelationship = async () => {};\n/* upstream_user_id: null */\n`;
  const check = CHECKS.find((c) => c.id === 'ordering-trap-is-driven-by-a-control');
  if (!check.test(stripComments(commentOnly))) ok('只写在注释里的接线不被判为命中');
  else bad('注释里的文字被当成了真接线 → 剥注释没生效');
}

console.log(failed ? `\n🔴 provider-downgrade-ux 锁失败(${failed})` : '\n✅ provider-downgrade-ux 锁通过');
process.exit(failed ? 1 : 0);
