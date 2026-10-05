#!/usr/bin/env node
/**
 * 客户面净区检查 · WJ 横切 · CI 防回归(2026-05-31)
 *
 * 扫客户可见组件源码,出现高信号禁区词即 exit 1。
 * 用途:pre-commit / CI 拦截「报告露 <summary>、报价露 ¥0、客户面露供应商名」回归。
 * 注:运行时 HTML(report_html_renderer 后端输出)需 prod 真机 grep 补充,
 *     本脚本管前端源码层;后端净化由 report_html_renderer 自身守卫(WJ-08)。
 *
 * 运行:node scripts/check_customer_surface_clean.mjs
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

// 纯客户可见目录(代理经营页不在此列 — 代理可见供应商名是经营信息,客户面才禁)
const ROOTS = [
  'frontend/src/pages/Public',
  'frontend/src/pages/Customer',
  'frontend/src/pages/Invite',
];

// 客户面硬禁区(§4.3)· 高信号(在客户面源码出现基本即 bug)
const FORBIDDEN = [
  /<\/?summary>/i,
  /<\/?details>/i,
  /¥\s*0\s*\/\s*月/,
  /节省\s*100\s*%/,
  /DeepSeek/i,
  /\bKimi\b/i,
  /豆包/,
  /通义/,
  /\bqwen\b/i,
  /tikhub/i,
  /metaso/i,
  /dashscope/i,
];

const hits = [];

function walk(dir) {
  let entries;
  try { entries = readdirSync(dir); } catch { return; }
  for (const e of entries) {
    const p = join(dir, e);
    let s;
    try { s = statSync(p); } catch { continue; }
    if (s.isDirectory()) walk(p);
    else if (/\.(tsx?|jsx?)$/.test(e)) scan(p);
  }
}

function scan(file) {
  const lines = readFileSync(file, 'utf8').split('\n');
  lines.forEach((line, i) => {
    const t = line.trim();
    // 跳过注释行(逻辑/类型里的英文不算客户面露出)
    if (t.startsWith('//') || t.startsWith('*') || t.startsWith('/*')) return;
    for (const re of FORBIDDEN) {
      if (re.test(line)) {
        hits.push(`${file}:${i + 1}  ${t.slice(0, 100)}`);
        break;
      }
    }
  });
}

ROOTS.forEach(walk);

// [WJ-08 P0 2026-06-01 · Codex smoke 补] 组件级结构断言:SharedReport v2 报告禁降级 Markdown
//   leak 是运行时(report.content 来自 API · 词扫抓不到)· 故断言源码结构上 v2 报告无法走 ReactMarkdown 降级路径
//   契约绑定本批变量名(isV2 / V2LoadingState / V2ErrorState)· 未来误删守卫即 fail
function checkSharedReportV2Guard() {
  const file = 'frontend/src/pages/Public/SharedReport.tsx';
  let src;
  try { src = readFileSync(file, 'utf8'); }
  catch { hits.push(`${file}  [WJ-08] 找不到文件 · v2 防降级断言无法执行`); return; }
  const hasIsV2 = /const\s+isV2\s*=\s*report\.report_version\s*===\s*['"]v2['"]/.test(src);
  // Markdown 降级块必须带 !isV2 守卫(v2 报告永不渲染 report.content 原始 markdown)
  const markdownGuarded = /!isV2\s*&&\s*!report\.v2_html_content\s*&&\s*!report\.html_content/.test(src);
  // v2 未就绪必须有骨架 + 错误态(不留空 + 不降级)
  const hasPending = /V2LoadingState/.test(src) && /V2ErrorState/.test(src);
  if (!hasIsV2 || !markdownGuarded || !hasPending) {
    hits.push(`${file}  [WJ-08] v2 防降级结构断言失败(isV2=${hasIsV2} markdownGuarded=${markdownGuarded} skeleton/error=${hasPending}) · v2 报告可能再次降级露原始 Markdown/折叠标签/模型名`);
  }
}
checkSharedReportV2Guard();

if (hits.length) {
  console.error('❌ 客户面净区检查失败 · 发现禁区词(客户不该看到):');
  hits.forEach((h) => console.error('  ' + h));
  console.error(`\n共 ${hits.length} 处。客户面禁:raw HTML 标签 / ¥0假价 / 节省100% / 模型供应商名。`);
  process.exit(1);
}
console.log(`✅ 客户面净区检查通过(${ROOTS.join(', ')})`);
