/**
 * 跨包契约静态核验（不使用前端 mock，section 七.5）。
 * 直接读 AI-3 真实源：
 *   - api/geo_observation_product_api.py（真实路由/方法）
 *   - schemas/geo_observation_product.py（真实 DTO 字段）
 * 与前端 client.ts + types.ts 逐条对账：
 *   1) 前端调用的每个路径都存在于 AI-3 真实路由（方法 + 规范化路径）。
 *   2) 洞察创建为 POST /brands/{}/insights 且带 X-Request-Id；轮询为 GET /brands/{}/insights/{}（品牌作用域）。
 *   3) summary/trend/platforms/opportunities 使用 granularity（不发 days）。
 *   4) 前端 client 不调用任何 AI-3 不存在的端点。
 *   5) DTO 逐字段：解析 schema 每个 `_StrictDTO` 与前端 interface 对账 ——
 *      · 子集不变式：前端字段必须都存在于 AI-3（防造字段 + 抓 AI-3 改名/删了前端仍读的字段）；
 *      · 可空不变式：AI-3 Optional 字段前端必须可空（`| null`/`?`），防把"无数据"渲染成 0%。
 * 命中任一不一致 → 退出非 0。
 */
import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';

// The unified release is the contract SSOT.  A reviewer may still point this
// verifier at a separate analytics worktree explicitly, but CI/builds must not
// silently validate against whichever stale sibling directory happens to exist.
const AI3_ROOT = process.env.OMNIRANK_AI3_ROOT
  ? resolve(process.env.OMNIRANK_AI3_ROOT)
  : resolve(process.cwd(), '..');
const AI3_API = resolve(AI3_ROOT, 'api/geo_observation_product_api.py');
const AI3_SCHEMA = resolve(AI3_ROOT, 'schemas/geo_observation_product.py');
const CLIENT = resolve(process.cwd(), 'src/features/geoObservation/client.ts');
const TYPES = resolve(process.cwd(), 'src/features/geoObservation/types.ts');

for (const [label, p] of [['api', AI3_API], ['schema', AI3_SCHEMA]]) {
  if (!existsSync(p)) {
    console.error(`[ai3-contract] 未找到 AI-3 契约源（${label}）：${p}（AI-3 worktree 未就位）`);
    process.exit(2);
  }
}

const apiSrc = readFileSync(AI3_API, 'utf8');
const schemaSrc = readFileSync(AI3_SCHEMA, 'utf8');
const clientSrc = readFileSync(CLIENT, 'utf8');
const typesSrc = readFileSync(TYPES, 'utf8');

// —— 1. 提取 AI-3 真实路由 ——
const PREFIX = { product: '/api/geo-observation', admin: '/api/admin/geo-observation' };
const realRoutes = new Set(); // "GET /api/geo-observation/brands/{}/summary"
const routeRe = /@(product|admin)\.(get|post|patch)\(\s*"([^"]+)"/g;
let m;
while ((m = routeRe.exec(apiSrc)) !== null) {
  const [, who, method, path] = m;
  const canon = `${method.toUpperCase()} ${PREFIX[who]}${path}`.replace(/\{[^}]+\}/g, '{}');
  realRoutes.add(canon);
}
if (realRoutes.size === 0) {
  console.error('[ai3-contract] 未从 AI-3 api 解析到任何路由，解析失败');
  process.exit(2);
}

// —— 2. 提取前端 client 调用 ——
// 匹配每个 req<...>(`...`, { ... }) 调用：拿模板路径 + 后随选项里的 method
const clientCalls = [];
const callRe = /req<[^>]*>\(\s*`([^`]+)`\s*,?\s*(\{[\s\S]*?\})?\s*\)/g;
while ((m = callRe.exec(clientSrc)) !== null) {
  const tpl = m[1];
  const opts = m[2] || '';
  const method = /method:\s*'POST'/.test(opts) ? 'POST' : /method:\s*'PATCH'/.test(opts) ? 'PATCH' : 'GET';
  const hasXReqId = /'X-Request-Id'/.test(opts);
  clientCalls.push({ tpl, method, opts, hasXReqId });
}
if (clientCalls.length === 0) {
  console.error('[ai3-contract] 未从 client.ts 解析到任何 req() 调用');
  process.exit(2);
}

function canonicalize(tpl) {
  return tpl
    .replace(/\$\{base\}/g, PREFIX.product)
    .replace(/\$\{admin\}/g, PREFIX.admin)
    .replace(/\?[\s\S]*$/, '') // 去 query
    .replace(/\$\{[^}]+\}/g, '{}'); // 模板占位符
}

const failures = [];

for (const call of clientCalls) {
  const path = canonicalize(call.tpl);
  const canon = `${call.method} ${path}`;
  if (!realRoutes.has(canon)) {
    failures.push(`前端调用不存在于 AI-3 真实路由：${canon}（模板 ${call.tpl}）`);
  }
}

// —— 3. 专项断言 ——
const create = clientCalls.find((c) => /\/insights$/.test(canonicalize(c.tpl)) && c.method === 'POST');
if (!create) failures.push('未找到洞察创建调用 POST .../insights');
else if (!create.hasXReqId) failures.push('洞察创建缺少 X-Request-Id 请求头');

const poll = clientCalls.find((c) => /\/brands\/\{\}\/insights\/\{\}$/.test(canonicalize(c.tpl)) && c.method === 'GET');
if (!poll) failures.push('未找到品牌作用域轮询 GET /brands/{}/insights/{}');

// granularity 使用：这些方法的模板必须含 granularity=（直接或经 g() 助手）
const usesGranularity = (tpl) => /granularity=/.test(tpl) || /\$\{g\(/.test(tpl);
for (const key of ['/summary', '/trend', '/platforms', '/opportunities']) {
  const c = clientCalls.find((x) => canonicalize(x.tpl).endsWith(`/brands/{}${key}`));
  if (c && !usesGranularity(c.tpl)) failures.push(`${key} 未使用 granularity（应发 granularity 而非 days）`);
}
// 不得出现 days= query
for (const call of clientCalls) {
  if (/[?&]days=/.test(call.tpl)) failures.push(`前端仍在发送被忽略的 days 参数：${call.tpl}`);
}

// —— 4. DTO 字段核验（真读 schemas/geo_observation_product.py，非仅路由） ——
// 解析 AI-3 `class X(_StrictDTO):` 下 4 空格缩进的 `field: type` 行（多行类型只取字段名）。
function parsePyDtos(src) {
  const out = {};
  const re = /\nclass (\w+)\(_StrictDTO\):\n([\s\S]*?)(?=\nclass |\n# =|$)/g;
  let mm;
  while ((mm = re.exec('\n' + src)) !== null) {
    const [, name, body] = mm;
    const fields = {};
    for (const line of body.split('\n')) {
      const fm = line.match(/^ {4}(\w+)\s*:\s*(.+)$/);
      if (fm) fields[fm[1]] = fm[2];
    }
    out[name] = fields;
  }
  return out;
}
// 解析 types.ts `export interface X {` 内 2 空格缩进的 `field?: type;` 行（跳过注释）。
function parseTsIfaces(src) {
  const out = {};
  const re = /export interface (\w+)\s*\{([\s\S]*?)\n\}/g;
  let mm;
  while ((mm = re.exec(src)) !== null) {
    const [, name, body] = mm;
    const fields = {};
    for (const line of body.split('\n')) {
      const t = line.trim();
      if (t.startsWith('//') || t.startsWith('*') || t.startsWith('/*')) continue;
      const fm = line.match(/^ {2}(\w+)(\??)\s*:\s*(.+?);?\s*$/);
      if (fm) fields[fm[1]] = { optional: fm[2] === '?', type: fm[3] };
    }
    out[name] = fields;
  }
  return out;
}

// AI-3 DTO → 前端消费类型（只列前端确有对应 interface 的）
const DTO_MAP = {
  WindowDTO: 'WindowDTO', OutcomeCountDTO: 'OutcomeCount', NextActionDTO: 'NextAction', BrandRefDTO: 'BrandRef',
  BrandSummaryMetricsDTO: 'BrandSummaryMetrics', ComparisonDTO: 'Comparison', BrandSummaryDTO: 'BrandSummary',
  TrendPointDTO: 'TrendPoint', BrandTrendDTO: 'BrandTrend',
  PlatformItemDTO: 'PlatformItem', HistoricalPlatformDTO: 'HistoricalPlatform', BrandPlatformsDTO: 'BrandPlatforms',
  QuestionItemDTO: 'QuestionItem', QuestionsPageDTO: 'QuestionsPage', CitationDTO: 'Citation',
  ChannelDisclosureDTO: 'ChannelDisclosure', EvidenceNextActionDTO: 'EvidenceNextAction', EvidenceDetailDTO: 'EvidenceDetail',
  OpportunityItemDTO: 'Opportunity', OpportunitiesDTO: 'OpportunitiesResponse',
  IndustryBaselineDTO: 'IndustryBaseline', PublicBaselineResponseDTO: 'PublicBaselineResponse',
  InsightJobStatusDTO: 'InsightJobStatus',
  AdminPlatformHealthDTO: 'AdminPlatformHealth', AdminCountsDTO: 'AdminCounts',
  CollectionReadinessDTO: 'CollectionReadiness', AdminOverviewDTO: 'AdminOverview',
  AdminModelShiftDTO: 'AdminModelShift', AdminModelShiftsDTO: 'AdminModelShifts',
  AdminAggregateDiffItemDTO: 'AdminAggregateDiffItem', AdminAggregateDiffDTO: 'AdminAggregateDiff',
};
const pyDtos = parsePyDtos(schemaSrc);
const tsIfaces = parseTsIfaces(typesSrc);
if (Object.keys(pyDtos).length === 0) failures.push('未从 AI-3 schema 解析到任何 DTO（解析失败）');

// 完备性守卫：从 client.ts 每个 req<X>() 顶层返回类型出发，**传递闭包**沿 types.ts 各 interface
// 字段类型里引用的其它 interface 递归收集，得到前端会反序列化/渲染的**全部** DTO（含仅作嵌套字段
// 出现的 DTO，如 AdminAggregateDiffItem），要求它们全部进 DTO_MAP —— 防 DTO_MAP 手工维护漏项。
const mappedTs = new Set(Object.values(DTO_MAP));
const ifaceNames = new Set(Object.keys(tsIfaces));
const refsOf = (name) => {
  const out = new Set();
  for (const meta of Object.values(tsIfaces[name] || {})) {
    for (const id of meta.type.match(/[A-Za-z_][A-Za-z0-9_]*/g) || []) {
      if (id !== name && ifaceNames.has(id)) out.add(id);
    }
  }
  return out;
};
const reqTypeRe = /req<([A-Za-z0-9_]+)(?:\[\])?>/g;
const stack = [];
let rm;
while ((rm = reqTypeRe.exec(clientSrc)) !== null) if (ifaceNames.has(rm[1])) stack.push(rm[1]);
const reachable = new Set();
while (stack.length) {
  const n = stack.pop();
  if (reachable.has(n)) continue;
  reachable.add(n);
  for (const r of refsOf(n)) if (!reachable.has(r)) stack.push(r);
}
for (const t of reachable) {
  if (!mappedTs.has(t)) {
    failures.push(`前端会消费的 DTO ${t}（req<> 顶层或经嵌套字段可达）未纳入 DTO_MAP 字段核验（漂移不设防；请加入 DTO_MAP）`);
  }
}

let dtoChecked = 0;
for (const [py, ts] of Object.entries(DTO_MAP)) {
  const a = pyDtos[py];
  const f = tsIfaces[ts];
  if (!a) { failures.push(`AI-3 schema 缺少 DTO ${py}（改名/删除 → 前端 ${ts} 需同步）`); continue; }
  if (!f) { failures.push(`前端 types.ts 缺少接口 ${ts}（对应 AI-3 ${py}）`); continue; }
  const aFields = a;
  for (const [ff, meta] of Object.entries(f)) {
    // 子集不变式：前端每个字段都必须存在于 AI-3（同时抓「造字段」与「AI-3 改名/删了前端仍读的字段」）
    if (!(ff in aFields)) {
      failures.push(`前端 ${ts}.${ff} 不在 AI-3 ${py}（造字段，或 AI-3 已改名/删除该字段）`);
      continue;
    }
    // 可空不变式（null≠0）：AI-3 为 Optional 的字段，前端必须可空（`| null`/`| undefined` 或 `?`）
    const aType = aFields[ff];
    const aNullable = /Optional\[/.test(aType) || /=\s*None/.test(aType);
    const fNullable = meta.optional || /\|\s*(null|undefined)/.test(meta.type);
    if (aNullable && !fNullable) {
      failures.push(`前端 ${ts}.${ff} 未标可空，但 AI-3 ${py}.${ff} 为 Optional（null≠0，会把"无数据"渲染成 0）`);
    }
  }
  dtoChecked += 1;
}

if (failures.length) {
  console.error('[ai3-contract] 跨包契约不一致：');
  for (const f of failures) console.error('  - ' + f);
  process.exit(1);
}

console.log(
  `[ai3-contract] OK：${clientCalls.length} 个前端调用全部命中 AI-3 真实路由（共 ${realRoutes.size} 条）；` +
    `granularity/X-Request-Id/品牌作用域轮询一致；` +
    `${dtoChecked} 个 DTO 逐字段核验（子集不变式防造字段/抓改名 + Optional→可空不变式，防 null 渲染成 0%）。`,
);
