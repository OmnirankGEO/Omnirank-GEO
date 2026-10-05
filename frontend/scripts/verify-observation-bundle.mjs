/**
 * 观测候选生产包硬门扫描（Frontend-A）。
 * 扫描 dist-observation-preview/ 的产出资产，确认：
 *  1) 无 regex lookbehind（老 iOS/微信兼容）。
 *  2) 无冻结 fixture / 合成示例数据兜底（示例品牌名、demo id、示例 brand_id）。
 *  3) 无跨客户 / 上游 / 内部成本 / provider trace 等隐私残留字段。
 * 命中任一即失败退出非 0。
 */

import { readdir, readFile, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { unevaluated } from './_verify_exit.mjs';
import { resolve } from 'node:path';

const distDir = resolve(process.cwd(), 'dist-observation-preview');

// 🔴 [#95] 缺产物 = **未评估**(exit 3),不是失败(exit 1)。
//    原来这里 exit 1 ⇒ 与「真的扫到 lookbehind」同码,而处置相反(去构建 vs 改代码)。
if (!existsSync(distDir)) {
  unevaluated([`${distDir} 不存在 —— 先 npm run obs:build 生成 observation 预览产物`],
              'observation 产物扫描');
}

const lookbehind = ['(?<=', '(?<!'];

// 生产 bundle 绝不应出现的 fixture / 示例响应内容（品牌名、demo id、答案节选等）。
// 注：预览壳传给真实 API 的 brand/diagnosis 路由参数不算"示例响应数据"，不在此列。
const fixtureResidue = [
  '华南智能制造技术服务示例品牌',
  '华南智能制造技术服务',
  'obs-demo-',
  'evt-demo-',
  'insight-demo-',
  'opp-demo-',
  'req-demo-',
  '示例品牌甲',
  '示例竞品',
  '现有公开资料不足以核验', // 冻结 fixture 里的答案节选
  '深圳市晨光富士电梯', // 参考图示例品牌，绝不硬编码
];

// 隐私/上游残留字段（用户/服务商端绝不出现）
const privacyResidue = [
  'owner_user_id',
  'agent_user_id',
  'upstream_user_id',
  'provider_trace_id',
  'cost_multiplier',
  'internal_cost',
  'service_account_code',
  'channel_account_code',
  'seller_account_code',
];

async function collect(dir) {
  const files = [];
  for (const entry of await readdir(dir)) {
    const p = resolve(dir, entry);
    const s = await stat(p);
    if (s.isDirectory()) files.push(...(await collect(p)));
    else if (/\.(?:js|css|html)$/i.test(entry)) files.push(p);
  }
  return files;
}

const files = await collect(distDir);
if (!files.length) {
  console.error(`[obs-bundle] ${distDir} 没有可扫描资产`);
  process.exit(1);
}

const violations = [];
for (const file of files) {
  const src = await readFile(file, 'utf8');
  for (const t of lookbehind) if (src.includes(t)) violations.push(`${file}: regex lookbehind ${t}`);
  for (const t of fixtureResidue) if (src.includes(t)) violations.push(`${file}: fixture/示例数据残留 "${t}"`);
  for (const t of privacyResidue) if (src.includes(t)) violations.push(`${file}: 隐私/上游字段残留 "${t}"`);
}

if (violations.length) {
  console.error('[obs-bundle] 生产包硬门失败：');
  for (const v of violations) console.error('  - ' + v);
  process.exit(1);
}

console.log(`[obs-bundle] OK：${files.length} 个生产资产，无 lookbehind / fixture / 隐私残留。`);
