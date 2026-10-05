#!/usr/bin/env node
/**
 * 公开页不留内部注释 · 产物门(Review 09-27:官网与门户定了公开页不留内部注释,主站同一口径)。
 *
 * 判据站在出口:扫 vite build 之后的 dist 里**所有** .html(含 public/ 原样拷过来的静态页),
 *   · `<!--` 出现 0 次;
 *   · 内联 <script>(没有 src 的)里没有整行 JS 注释(行首 `//`、`/*`、`*`)。
 * 内部说明挪在仓内 docs/AI-CONTEXT/FRONTEND_PUBLIC_HTML_NOTES_2026-09-27.md。
 *
 * 三态:rc 0 = 干净;rc 1 = 有注释(逐文件逐行报);rc 3 = 没跑成(dist 不在 / 一个 .html 都没有 / 反臂没咬住)——
 *   没跑成不是绿。
 * 注入反臂:正式扫描前先用同一个检查函数扫几段自造的坏样本(必须报)与好样本(必须不报,含 https:// 这种
 *   「像注释的字符串」),检查器自己失灵就 rc 3。
 * 零依赖:只用 node:fs / node:path;锚点从本脚本位置向上找 package.json + src/(不看 cwd、不问 git)。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

function findFrontendRoot(startDir) {
  let dir = startDir;
  for (let i = 0; i < 12; i++) {
    let ok = false;
    try { ok = statSync(join(dir, 'package.json')).isFile() && statSync(join(dir, 'src')).isDirectory(); } catch { /* 往上找 */ }
    if (ok) return dir;
    const up = dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  return null;
}

/** 返回问题列表(空 = 干净)。每项 `行号: 说明`。 */
export function commentProblems(html) {
  const out = [];
  const lines = html.split('\n');
  lines.forEach((line, i) => {
    if (line.includes('<!--')) out.push(`${i + 1}: HTML 注释 <!--`);
  });
  const re = /<script(\s[^>]*)?>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html))) {
    const attrs = m[1] || '';
    if (/\bsrc\s*=/.test(attrs)) continue;                       // 外链脚本不看
    const startLine = html.slice(0, m.index).split('\n').length;
    m[2].split('\n').forEach((l, j) => {
      const t = l.trim();
      if (t.startsWith('//') || t.startsWith('/*') || t.startsWith('*')) {
        out.push(`${startLine + j}: 内联脚本整行注释 ${t.slice(0, 40)}`);
      }
    });
  }
  return out;
}

function walkHtml(dir, acc) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walkHtml(p, acc);
    else if (name.toLowerCase().endsWith('.html')) acc.push(p);
  }
  return acc;
}

function main() {
  // ── 注入反臂:检查器必须咬得住坏样本、放过好样本 ──
  const bad = [
    '<html><head><!-- 内部说明 --></head></html>',
    '<body>\n<!--\n  多行\n-->\n</body>',
    '<script>\n  // 内部注释\n  var a = 1;\n</script>',
    '<script>\n  /* 块注释 */\n</script>',
  ];
  const good = [
    '<html><head><title>x</title></head><body><a href="https://omnirank.cn/">官网</a></body></html>',
    '<script>\n  var u = "https://omnirank.cn/"; fetch(u);\n</script>',
    '<script type="module" src="/assets/index.js"></script>',
  ];
  const missed = bad.filter((s) => commentProblems(s).length === 0);
  const falsePos = good.filter((s) => commentProblems(s).length > 0);
  if (missed.length || falsePos.length) {
    console.error('🔴 HTML 注释产物门【没跑成·rc=3】:检查器自己失灵 —— 漏报 ' + missed.length + ' 段、误报 ' + falsePos.length + ' 段');
    process.exit(3);
  }

  const root = findFrontendRoot(dirname(fileURLToPath(import.meta.url)));
  if (!root) {
    console.error('🔴 HTML 注释产物门【没跑成·rc=3】:向上 12 层找不到 package.json + src/');
    process.exit(3);
  }
  const dist = join(root, 'dist');
  let files = [];
  try { files = walkHtml(dist, []).sort(); } catch { files = []; }
  if (!files.length) {
    console.error(`🔴 HTML 注释产物门【没跑成·rc=3】:${dist} 里一个 .html 都没有 —— 本门必须排在 vite build 之后`);
    process.exit(3);
  }
  const bads = [];
  for (const f of files) {
    const probs = commentProblems(readFileSync(f, 'utf8'));
    for (const p of probs) bads.push(`${relative(root, f).split(sep).join('/')}:${p}`);
  }
  if (bads.length) {
    console.error('🔴 HTML 注释产物门 FAIL:公开页不许带内部注释(说明挪到 docs/AI-CONTEXT/FRONTEND_PUBLIC_HTML_NOTES_2026-09-27.md)');
    for (const b of bads) console.error('  - ' + b);
    process.exit(1);
  }
  console.log(`✅ HTML 注释产物门:dist 里 ${files.length} 个 .html 都没有 <!-- 与内联脚本整行注释(反臂 ${bad.length} 坏 / ${good.length} 好 已验)`);
}

main();
