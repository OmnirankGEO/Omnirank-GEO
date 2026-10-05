#!/usr/bin/env node
/**
 * 锁:HTTP header 值不许出现非 latin1(ISO-8859-1)字符。
 *
 * 背景(2026-08-06 真事故):`ProviderDowngradeWizard.tsx` 把中文写进
 * `X-Governance-Reason`,浏览器 `setRequestHeader` 当场抛
 * "String contains non ISO-8859-1 code point" → 服务商降级向导**打开即死**,
 * 管理员完全无法降级任何服务商,只能找工程代执行。
 *
 * 🔴 本锁只扫 frontend/src,**不引用任何后端文件** —— 引用后端文件的锁
 *    不许进 frontend build 链(Dockerfile 的 frontend-builder 阶段只 COPY frontend/,
 *    后端源码不在那一层,会当场炸掉预烤)。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = fileURLToPath(new URL('.', import.meta.url));
const SRC = join(HERE, '..', 'src');

let failed = 0;
const bad = (msg) => { console.log(`  ❌ ${msg}`); failed++; };
const ok = (msg) => console.log(`  ✅ ${msg}`);

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, out);
    else if (/\.(ts|tsx)$/.test(name)) out.push(p);
  }
  return out;
}

/** 剥掉 // 行注释与 block 注释,避免锁抓到自己写的说明文字(2026-08-05 踩过) */
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
}

/**
 * 找形如  'X-Foo': <值>  的 header 赋值,取出值里的字符串字面量。
 * 只认 X- 前缀的自定义 header(Content-Type 之类不会写中文)。
 */
const HEADER_RE = /['"](X-[A-Za-z0-9-]+)['"]\s*:\s*([^,}\n]+)/g;
/* 🔴 [#195] 原来这里写的是一个**裸 NUL 字节**(源码里看不见,改写工具一碰就丢)。
   语义没错(非 Latin-1 才算命中),但换成显式转义才读得懂、也不会被下一次改写吃掉。 */
const NON_LATIN1 = /[^\u0000-\u00ff]/;
/* 判据自证:中文命中、ASCII 与 Latin-1 高位都不命中(正则被改坏时本条先红)。 */
if (!NON_LATIN1.test('中') || NON_LATIN1.test('abc') || NON_LATIN1.test('ÿ')) {
    console.log('  FAIL H0 🔴 非 Latin-1 判定正则自证失败 —— 本闸读数不可信');
    process.exit(1);
}

const files = walk(SRC);
const hits = [];

for (const f of files) {
  const src = stripComments(readFileSync(f, 'utf8'));
  let m;
  HEADER_RE.lastIndex = 0;
  while ((m = HEADER_RE.exec(src)) !== null) {
    const [, header, rawValue] = m;
    // 模板串里的 ${...} 是运行时值,静态判不了;但字面量部分仍要干净
    const literalPart = rawValue.replace(/\$\{[^}]*\}/g, '');
    if (NON_LATIN1.test(literalPart)) {
      const line = src.slice(0, m.index).split('\n').length;
      hits.push(`${relative(SRC, f)}:${line}  ${header} = ${rawValue.trim().slice(0, 60)}`);
    }
  }
}

console.log('§1 自定义 HTTP header 值必须是 latin1');
if (hits.length) {
  bad(`${hits.length} 处 header 值含非 latin1 字符(浏览器会当场抛 setRequestHeader 异常):`);
  hits.forEach((h) => console.log(`       ${h}`));
} else {
  ok(`扫描 ${files.length} 个文件 · 未发现非 latin1 的 header 值`);
}

// 🔴 反向对照:证明这条正则真的能抓到中文 header,而不是恒绿。
console.log('§2 反向对照(判据自证)');
{
  const sample = `headers: { 'X-Request-ID': \`abc-\${id}\`, 'X-Governance-Reason': '管理员打开服务商降级向导' },`;
  let caught = 0;
  HEADER_RE.lastIndex = 0;
  let m;
  while ((m = HEADER_RE.exec(sample)) !== null) {
    if (NON_LATIN1.test(m[2].replace(/\$\{[^}]*\}/g, ''))) caught++;
  }
  if (caught === 1) ok('人造中文 header 被抓到 1 处 → 本锁有判别力');
  else bad(`人造中文 header 应被抓到 1 处,实际 ${caught} 处 → 锁写废了`);
}
{
  // 必须不命中:纯 ASCII 值 + 模板串里带中文变量名(变量是运行时值,不该报)
  const clean = `headers: { 'X-Governance-Reason': 'admin-provider-downgrade-readiness' },`;
  HEADER_RE.lastIndex = 0;
  let m, caught = 0;
  while ((m = HEADER_RE.exec(clean)) !== null) {
    if (NON_LATIN1.test(m[2].replace(/\$\{[^}]*\}/g, ''))) caught++;
  }
  if (caught === 0) ok('纯 ASCII header 未被误报 → 无假阳');
  else bad(`纯 ASCII header 被误报 ${caught} 处 → 假阳`);
}

console.log(failed ? `\n🔴 header-latin1 锁失败(${failed})` : '\n✅ header-latin1 锁通过');
process.exit(failed ? 1 : 0);
