#!/usr/bin/env node
/**
 * R3 · 社媒板块**永久剔除**负向锁。
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │ 这是 Owner 2026-08-17 的**铁律**，不是技术限制。                       │
 * │ 「员工席位白名单永不含社媒板块」。                                      │
 * │ 社媒 IP 板块由独立 CTO 分身负责，与 GEO 板块互不借指标、互不共享席位；   │
 * │ 员工席位是 GEO 交付协作用的，社媒创作链路不在其授权范围内 —— 永远。      │
 * │ 因此本锁**没有「加一条也行」的例外流程**：要放开必须先由 Owner 改铁律。  │
 * └──────────────────────────────────────────────────────────────────────┘
 *
 * 判据形态：**负向枚举 · 命中数恒 0，新增即红。**
 *   正向白名单锁只能保证「登记了的都对」，管不住「有人又登记了一条」。
 *   社媒边界要的恰恰是后者，所以这里打的是**恒 0**，不是「都在表里」。
 *
 * 判据成对（工单要求 + 本仓铁律「每个必须命中配一个必须不命中」）：
 *   `node verify-organization-social-boundary.mjs`            → 必须全绿
 *   `node verify-organization-social-boundary.mjs --selftest` → **逐个命名空间**
 *      往契约里注入一条人造社媒路由，每一条都必须让锁转红。
 *      任何一个命名空间注入后仍然绿 = 那条轴是恒真的假锁。
 *
 * 口径来源：`docs/AI-CONTEXT/SOCIAL_STUDIO_ACTIVE_PATH_2026-05-10.md`（主线口径）
 *   + 各 router 的实际 `prefix=`（机器可核，见 checkProvenance）。
 */
import { readFileSync, existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '..', '..');
const CONTRACT_PATH = 'services/organization_route_contract.py';

/**
 * 社媒命名空间全集。每条都标了它从哪个 router 的 `prefix=` 取来，
 * prefix 改了就要回来同步 —— `checkProvenance()` 会机械核对，不同步就红。
 */
const SOCIAL_NAMESPACES = [
  { prefix: '/api/content', note: '社媒内容生成主线（content_api）' },
  { prefix: '/api/social', note: '社媒审阅 · 人设 · 主链路（review / personality / social_mainpath）' },
  { prefix: '/api/advisors', note: '社媒顾问 RAG（advisor_api）' },
  { prefix: '/api/research', note: '拆解 · 素材卡 · 热点 · 评论分析（原研究 router，E3 已删；前缀仍在禁区）' },
  { prefix: '/api/profile', note: '飞轮 / 速访 / 上传解析（social_mainpath；单数 profile，与复数 profiles 不同）' },
  { prefix: '/api/products', note: '社媒产品库（social_mainpath）' },
];

/**
 * ⚠️ 共用前缀 `/api/profiles`（**复数**）—— 单独冻结，不并进上面的恒 0。
 *
 * 事实（2026-08-17 实测，两头都查过）：
 *   · 文件自称「社媒操盘手 v3.0 - 客户档案 API」(`api/profile_api.py:1`)；
 *   · 但 GEO 侧真在用：`pages/Brands/MarketingTab.tsx`（客户营销资料读写）、
 *     `components/brand/{AiFillDialog,PolishButton,BrandWizardBanner}.tsx`、
 *     `pages/Brand/BatchUpgradeDialog.tsx`、`components/managed/api.ts`；
 *   · 路由契约里现有 3 条，resource_kind 全是 `client_profile`（GEO 客户档案）。
 *
 * 所以它既不是纯社媒也不是纯 GEO。**归属由 Owner 裁定，不由这把锁替他决定。**
 * 裁定前本锁做的是**冻结**：只认下面 3 条，出现第 4 条即红。
 * 既不误删 GEO 在用的能力，也堵死「借共用前缀夹带社媒端点」这条路。
 */
const SHARED_PREFIX = '/api/profiles';
const SHARED_PREFIX_FROZEN = [
  'GET /api/profiles',
  'GET /api/profiles/{profile_id}',
  'PUT /api/profiles/{profile_id}',
];

/**
 * 从契约源码机械取出全部已登记路由。`_p(` 是唯一构造器（已核）。
 *
 * 🔴 `_p(` 与首个字符串之间**允许夹注释行** —— 契约里真有这种写法
 *   (`/api/monitoring/identity-reviews/{result_id}/full-response` 就是)。
 *   第一版正则没吃注释，于是**静默漏掉那一条**：解析 118 vs 真值 119。
 *   「在 _p( 后面加一行注释」正是藏一条社媒路由最省事的姿势，所以这里
 *   ① 正则显式吃掉注释行；② 再做一次**自洽核对**（解析条数必须等于
 *   文件里 _p( 的声明次数），同类洞再出现就直接红。
 */
const ROUTE_PATTERN = new RegExp(
  String.raw`_p\((?:\s*#[^\n]*\n)*\s*"([A-Z]+)"\s*,(?:\s*#[^\n]*\n)*\s*"([^"]+)"`,
  'g',
);

function parseRoutes(source) {
  const routes = [...source.matchAll(ROUTE_PATTERN)]
    .map(m => ({ method: m[1], path: m[2], key: `${m[1]} ${m[2]}` }));
  // 刻意不用后行断言 —— 本仓 build 链里有 verify-no-lookbehind 门禁。
  const declared = [...source.matchAll(/_p\(/g)].length
    - [...source.matchAll(/def\s+_p\(/g)].length;
  routes.parseGap = declared - routes.length;
  return routes;
}

/** 命名空间命中判定：等于前缀本身，或落在该前缀之下。 */
function underPrefix(route, prefix) {
  return route.path === prefix || route.path.startsWith(prefix + '/');
}

function evaluate(source, { injectNamespace = null, injectShared = false } = {}) {
  const baseRoutes = parseRoutes(source);
  let routes = baseRoutes;
  if (injectNamespace) {
    routes = [...baseRoutes, {
      method: 'POST',
      path: `${injectNamespace}/selftest-injected`,
      key: `POST ${injectNamespace}/selftest-injected`,
    }];
  }
  if (injectShared) {
    routes = [...routes, {
      method: 'POST',
      path: `${SHARED_PREFIX}/selftest-injected`,
      key: `POST ${SHARED_PREFIX}/selftest-injected`,
    }];
  }

  const failures = [];

  // 分母自证：取数正则失效 → 「零命中 = 全绿」，最典型的假绿。
  if (baseRoutes.length < 100) {
    failures.push(
      `取数失效：只从 ${CONTRACT_PATH} 解析出 ${baseRoutes.length} 条路由（预期 ≥100）。`
      + '正则对不上 = 这把锁在空集上恒绿，先修取数。',
    );
    return failures;
  }
  // 自洽守卫：解析条数 ≠ 声明条数 ⇒ 有路由从正则底下溜过去了。
  if (baseRoutes.parseGap !== 0) {
    failures.push(
      `取数不自洽：文件里有 ${baseRoutes.length + baseRoutes.parseGap} 处 _p( 声明，`
      + `只解析出 ${baseRoutes.length} 条。有路由从正则底下溜过去了 —— `
      + '在 _p( 后面夹一行注释就能做到，而那正是藏一条社媒路由最省事的姿势。先修取数。',
    );
    return failures;
  }

  for (const ns of SOCIAL_NAMESPACES) {
    const hits = routes.filter(r => underPrefix(r, ns.prefix));
    if (hits.length) {
      failures.push(
        `【Owner 铁律 · 社媒板块永不进员工席位白名单】命名空间 ${ns.prefix}（${ns.note}）`
        + ` 在员工路由契约里出现了 ${hits.length} 条：${hits.map(h => h.key).join('、')}`,
      );
    }
  }

  const sharedHits = routes.filter(r => underPrefix(r, SHARED_PREFIX));
  const unexpected = sharedHits.filter(r => !SHARED_PREFIX_FROZEN.includes(r.key));
  if (unexpected.length) {
    failures.push(
      `【共用前缀冻结】${SHARED_PREFIX} 归属尚待 Owner 裁定，现冻结为 3 条既有 GEO 客户档案路由。`
      + ` 新出现：${unexpected.map(r => r.key).join('、')}。`
      + ' 要新增必须先拿到归属裁定，不许借共用前缀夹带社媒端点。',
    );
  }
  if (!injectNamespace && !injectShared) {
    const missing = SHARED_PREFIX_FROZEN.filter(key => !sharedHits.some(r => r.key === key));
    if (missing.length) {
      failures.push(
        `【共用前缀冻结】${SHARED_PREFIX} 的既有 3 条被删掉了：${missing.join('、')}。`
        + ' GEO 侧 MarketingTab 等在用；如确为有意删除，请同步更新本锁的冻结清单并说明理由。',
      );
    }
  }
  return failures;
}

/** 口径出处机械核对：router prefix 换了就红，避免这张表打空。 */
function checkProvenance() {
  const failures = [];
  // [开源 E3 · B1b-2a] 研究 / 数据复盘 / 人设三个 router 已删:它们的 prefix 没有文件可核了,从这里去掉;
  //   对应命名空间(/api/research、/api/social)仍在 SOCIAL_NAMESPACES 禁区表里,恒 0 与 --selftest 照查。
  const expectations = [
    ['api/content_api.py', '/api/content'],
    ['api/advisor_api.py', '/api/advisors'],
    ['api/profile_api.py', '/api/profiles'],
  ];
  for (const [file, prefix] of expectations) {
    const full = resolve(repo, file);
    if (!existsSync(full)) {
      failures.push(`口径出处失效：${file} 不存在了，社媒命名空间表需要重新取值`);
      continue;
    }
    if (!readFileSync(full, 'utf8').includes(`prefix="${prefix}"`)) {
      failures.push(
        `口径出处漂移：${file} 的 router prefix 已不是 "${prefix}"。`
        + ' 本锁的命名空间表按各 router 的 prefix 取值，prefix 变了必须回来同步，否则锁会打空。',
      );
    }
  }
  return failures;
}

/* ── 主流程 ───────────────────────────────────────────────────────────── */
const source = readFileSync(resolve(repo, CONTRACT_PATH), 'utf8');

if (process.argv.includes('--selftest')) {
  const blind = [];
  for (const ns of SOCIAL_NAMESPACES) {
    if (!evaluate(source, { injectNamespace: ns.prefix }).some(line => line.includes(ns.prefix))) {
      blind.push(ns.prefix);
    }
  }
  if (!evaluate(source, { injectShared: true }).some(line => line.includes(SHARED_PREFIX))) {
    blind.push(`${SHARED_PREFIX}（冻结轴）`);
  }
  if (blind.length) {
    console.error('❌ 自证失败：以下命名空间注入人造社媒路由后仍然全绿（= 假锁）：' + blind.join('、'));
    process.exit(1);
  }
  if (evaluate(source).length || checkProvenance().length) {
    console.error('❌ 自证失败：未注入时本来就是红的，反向对照没有意义。先修真问题。');
    process.exit(1);
  }
  console.log(
    `✅ 自证通过：${SOCIAL_NAMESPACES.length} 个社媒命名空间 + 1 条共用前缀冻结轴，`
    + '各自注入一条人造社媒路由后都能报红，未注入时全绿。',
  );
  process.exit(0);
}

if (process.argv.includes('--counts')) {
  const routes = parseRoutes(source);
  console.log(`员工路由契约总条数：${routes.length}（声明 ${routes.length + routes.parseGap} 处，差 ${routes.parseGap}）`);
  for (const ns of SOCIAL_NAMESPACES) {
    console.log(`  ${ns.prefix.padEnd(16)} 命中 ${routes.filter(r => underPrefix(r, ns.prefix)).length}   <- 应恒为 0`);
  }
  const shared = routes.filter(r => underPrefix(r, SHARED_PREFIX));
  console.log(`  ${SHARED_PREFIX.padEnd(16)} 命中 ${shared.length}   <- 共用前缀，冻结在 ${SHARED_PREFIX_FROZEN.length} 条，待 Owner 裁定`);
}

const failures = [...evaluate(source), ...checkProvenance()];
if (failures.length) {
  console.error('❌ 员工席位社媒边界被破坏：');
  for (const line of failures) console.error('   · ' + line);
  console.error('');
  console.error('   ⚠️ 这是 Owner 2026-08-17 的**铁律**，不是技术限制：');
  console.error('      「员工席位白名单永不含社媒板块」。');
  console.error('      社媒 IP 板块由独立 CTO 分身负责，与 GEO 板块互不借指标、互不共享席位。');
  console.error('      不存在「例外流程」—— 要放开必须先由 Owner 改铁律，改完再改这把锁。');
  process.exit(1);
}
console.log(
  `✅ 员工席位社媒边界完好：${SOCIAL_NAMESPACES.length} 个社媒命名空间命中数恒 0，`
  + `共用前缀 ${SHARED_PREFIX} 冻结在 ${SHARED_PREFIX_FROZEN.length} 条既有 GEO 路由。`,
);
