#!/usr/bin/env node
/**
 * 变异重放 · 静默 reload **行为锁**(Playwright 真渲染那一组)的判别力自证
 * [WO_NO_SILENT_RELOAD_DIRTY_GUARD 2026-08-16 §2「变异」· 2026-08-18 落成为脚本]
 *
 * ── 为什么补这一个 ────────────────────────────────────────────────────
 * 出口枚举锁已有 `mutation_runner_silent_reload_outlets.mjs` 自证判别力,
 * 但**行为锁(真浏览器那一组)一直只有手跑记录**:交付书里写着「MU1-MU4 全转红」,
 * 而那是**人手改一次、看一眼、改回来**。下一个人重构 dirtyGuard 时,
 * 没有任何东西会告诉他"你把锁弄成恒真了" —— 9 条照样绿。
 *
 * 本仓这条教训已经很贵:「只写进文档的规则,读过也会踩」。
 * ⇒ 把 MU1-MU4 连同新增的两条(全局未提交标志 / 500ms 二次守卫)固化成可重放脚本。
 *
 * 🔴 与出口枚举锁的分工:枚举锁保证**没人偷加一处出口**(静态);
 *   本脚本保证**守卫真的在拦**(动态)。两者都绿才叫修好了。
 *
 * 🔴 不进 build 链:要真浏览器 + vite dev server(与 verify:publish-layout-interaction 同级)。
 *
 * 用法:node scripts/mutation_runner_silent_reload_behavior.mjs
 */
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const FE = resolve(HERE, '..');
const CONFIG = 'playwright.no-silent-reload.config.ts';

/** 跑那组行为锁,返回 {code, passed, failed, redLocks, out}。永远不抛 —— 要的就是退出码。
 *
 * 🔴 `redLocks` 必须从 **JSON reporter** 解析,不许扫 line reporter 的文本。
 *   2026-08-18 我第一版就是扫文本的:过滤"含 spec 文件名的行"再抽 `锁N` ——
 *   而 line reporter **给每条用例都打一行进度**(`[3/10] … › 锁3 …`),
 *   于是 redLocks 恒等于全部 10 条,`expectLocks` 那道检查**恒真**。
 *   讽刺的是它本来就是为了防"顶包"而加的,自己先变成了一把恒真锁。
 *   露馅点是输出不合常理:10 passed 却报"实际红:锁1…锁9"。
 *   ⇒ 换成结构化输出;并在基线处加**反向对照**(全绿时 redLocks 必须为空),
 *     那条对照当场就能抓住这种解析失效。
 */
function runLocks() {
  let out = '';
  let code = 0;
  const jsonPath = join(FE, 'node_modules', '.cache', `nsr-behavior-${process.pid}.json`);
  mkdirSync(dirname(jsonPath), { recursive: true });
  rmSync(jsonPath, { force: true });
  try {
    out = execFileSync('npx', ['playwright', 'test', '--config', CONFIG, '--reporter=line,json'],
      { cwd: FE, encoding: 'utf8', stdio: 'pipe', shell: process.platform === 'win32',
        env: { ...process.env, PLAYWRIGHT_JSON_OUTPUT_NAME: jsonPath } });
  } catch (e) {
    code = e.status ?? -1;
    out = (e.stdout || '') + (e.stderr || '');
  }
  const passed = Number((out.match(/(\d+) passed/) || [0, 0])[1]);
  const failed = Number((out.match(/(\d+) failed/) || [0, 0])[1]);

  const redTitles = [];
  let parsed = false;
  try {
    const report = JSON.parse(readFileSync(jsonPath, 'utf8'));
    parsed = true;
    const walk = (suite) => {
      for (const spec of suite.specs || []) {
        const bad = (spec.tests || []).some((t) =>
          (t.results || []).some((res) => res.status !== 'passed' && res.status !== 'skipped'));
        if (bad) redTitles.push(spec.title || '');
      }
      for (const child of suite.suites || []) walk(child);
    };
    for (const s of report.suites || []) walk(s);
  } catch { /* parsed 保持 false,下面会因此判整轮失败 */ }
  rmSync(jsonPath, { force: true });

  const redLocks = [...new Set(
    redTitles.flatMap((t) => [...t.matchAll(/锁(\d+R?)/g)].map((m) => m[1])),
  )];
  return { code, passed, failed, redLocks, parsed, out };
}

/**
 * 🔴 `apply` 必须断言**内容真的变了**(同族教训 2026-08-17 实测):
 *   替换模式对不上真实写法时替换是 no-op,锁照旧绿,报出来像"这个维度恒真"。
 *   **变异没落地时的绿和红都不算数** —— 在这一步就炸,不许流进结论。
 */
const patch = (file, fn) => {
  const p = join(FE, file);
  const orig = readFileSync(p, 'utf8');
  return {
    apply: () => {
      const next = fn(orig);
      if (next === orig) {
        throw new Error(`变异没落地:${file} 内容未改变(替换模式对不上真实写法)—— ` +
          '这不是"锁恒真",是夹具坏了,先修夹具再谈结论');
      }
      assertRulerWorks(p);
      writeFileSync(p, next);
      if (!syntaxOk(p)) {
        writeFileSync(p, orig);
        throw new Error(`${NOT_LANDED_SYNTAX} · ${file}`);
      }
    },
    undo: () => writeFileSync(p, orig),
  };
};

const results = [];
/**
 * @param expectLocks 期望被打红的锁号,如 ['1','6']
 *
 * 🔴 为什么不能只看退出码:本套件里 **锁1 实测会偶发红**
 *   (2026-08-18:全量连跑观察到约 1/6;单独跑 6/6 全绿 ⇒ 是全量下的时序偶发,不是真缺陷)。
 *   只要判据写成"退出码非 0 就算打红",一次偶发红就能替某条变异**顶包** ——
 *   那个维度实际上已经锁不住了,报表上却是 ✅,而且**越 flaky 越容易全绿**。
 *   同族教训见 memory `feedback_compare_error_signatures_not_just_node_sets`:
 *   **红了还要比"红因"**,不能只比"红没红"。
 *   ⇒ 本函数要求:红的锁里**必须包含**预期那条;只红了别的一律判本轮失败。
 */
function expectRed(name, m, expectLocks, expectHint) {
  let r;
  try {
    m.apply();
    r = runLocks();
  } catch (e) {
    m.undo();
    console.error(`🔴 变异未能施加:${name}\n     ${e.message}`);
    process.exit(1);
  } finally {
    m.undo();
  }
  const red = r.code !== 0;
  const hit = r.redLocks.filter((x) => expectLocks.includes(x));
  const ok = red && hit.length > 0;
  results.push({ name, ok, red, hit, code: r.code, passed: r.passed, failed: r.failed, redLocks: r.redLocks });
  const tag = ok ? '✅ 打红' :
    (red ? '🔴 红了但红错了(预期那条没红 —— 很可能是偶发红顶了包)' : '🔴 没打红(锁在这个维度是恒真的)');
  console.log(`${tag}  ${name}`);
  console.log(`     exit=${r.code} · ${r.passed} passed / ${r.failed} failed`);
  console.log(`     期望红:锁${expectLocks.join(' / 锁')}(${expectHint})` +
    ` · 实际红:${r.redLocks.length ? '锁' + r.redLocks.join(' / 锁') : '无'}`);
}

// ── 基线:未变异必须全绿,且条数对得上(否则后面的"红"没有对照意义)────────
console.log('基线(未变异)· 跑全部行为锁…');
/* 🔴 牙证:这道语法前置在本 runner 里真的会红(抛异常、自还原)。 */
proveGuardHasTeeth(resolve(FE, 'src/lib/versionPoll.ts'));

const base = runLocks();
console.log(`基线: exit ${base.code} · ${base.passed} passed / ${base.failed} failed`);
if (base.code !== 0) {
  // 🔴 基线红时必须把**红在哪**打出来,不许只说"基线红了" ——
  //   2026-08-18 实测:锁1 在机器有负载时会偶发红(13 次全量跑里 2 次;
  //   单独跑 6/6 绿、空闲窗口连跑 8/8 绿)。不打详情的话,下一个人只会看到
  //   一句"先修基线",完全无从判断是真缺陷还是偶发 —— 那就变成劝人无视它。
  console.error(`🔴 基线就是红的 —— 变异重放无意义。红的是:${
    base.redLocks.length ? '锁' + base.redLocks.join(' / 锁') : '(解析不出锁号)'}`);
  const detail = base.out.split('\n').filter((l) =>
    /Timed out|Error:|expect\(|Received|dirty-guard\.spec\.ts:/.test(l)).slice(0, 14);
  if (detail.length) console.error(detail.map((l) => '     ' + l.trim()).join('\n'));
  console.error('     ⇒ 若只有锁1、且重跑即绿,是已知偶发(见本文件 expectRed 注释);' +
    '若稳定红或红的是别的锁,那是真缺陷。');
  process.exit(1);
}
if (base.passed !== 10) {
  console.error(`🔴 基线只跑了 ${base.passed} 条(期望 10)—— 分母不对,拒绝在此之上谈"全红"。`);
  process.exit(1);
}
// 🔴 反向对照:解析器自己必须有判别力。全绿这一趟里 redLocks **必须为空** ——
//   若非空,说明它把"跑过的用例"当成了"红了的用例"(我第一版正是如此),
//   那么后面每条变异的 expectLocks 检查都会恒真,整个"红对了"的结论作废。
if (!base.parsed) {
  console.error('🔴 JSON reporter 解析失败 —— 拿不到"红的是哪几条",拒绝继续。');
  process.exit(1);
}
if (base.redLocks.length) {
  console.error(`🔴 解析器失效:基线 10 条全绿,却解析出"红锁"${base.redLocks.join('/')} —— ` +
    '说明它在数"跑过的"而不是"红了的",expectLocks 会恒真。先修解析器。');
  process.exit(1);
}
console.log(`     反向对照:全绿这趟解析出的红锁 = 空 ✅(解析器不是把跑过的当成红的)`);

// MU1 拆 versionPoll 的守卫 —— bfcache 那条静默路直接刷
expectRed('MU1 拆 versionPoll 守卫(bfcache 路不问守卫,直接 hardReload)',
  patch('src/lib/versionPoll.ts', (s) =>
    s.replace(/guardedSilentReload\(\(\) => \{ void hardReload\(\); \}, showUpdateBanner\);/,
      'void hardReload();')),
  ['1', '6'], 'dirty 时仍被刷掉');

// MU2 拆 errorReporter 的守卫 —— chunk 404 那条静默路直接刷
expectRed('MU2 拆 errorReporter 守卫(chunk 路不问守卫)',
  patch('src/lib/errorReporter.ts', (s) =>
    s.replace(/const scheduled = guardedSilentReload\(/, 'const scheduled = ((doIt) => { doIt(); return true; })(')
     .replace(/\n        showUpdateBanner,\n    \);/, '\n    );')),
  ['3', '5', '7'], 'chunk 路 dirty 时仍被刷掉');

// MU3 hardReload 退回跳首页 —— ② 那条改动
expectRed('MU3 hardReload 退回写死跳首页 `/?_r=`(丢路由)',
  patch('src/lib/versionPoll.ts', (s) =>
    s.replace(/        const url = new URL\(window\.location\.href\);\n        url\.searchParams\.set\('_r', String\(Date\.now\(\)\)\);\n        window\.location\.href = url\.pathname \+ url\.search \+ url\.hash;/,
      "        window.location.href = `/?_r=${Date.now()}`;")),
  ['2'], 'URL 不再是原路径');

// MU4 拆掉 activeElement 退化兜底 —— 未接入 registry 的表单失去保护
expectRed('MU4 拆掉 activeElement 退化兜底(未接入表单裸奔)',
  patch('src/lib/dirtyGuard.ts', (s) =>
    s.replace(/export function activeElementLooksEditable\(\): boolean \{\n    try \{/,
      'export function activeElementLooksEditable(): boolean {\n    if (1 as number) return false;\n    try {')),
  ['9'], '聚焦未打字 · 2026-08-18 专为隔离这一层补的');

// MU5 拆掉全局未提交标志 —— Codex 复现①(失焦后仍应受保护)
expectRed('MU5 全局未提交标志恒 false(失焦即判净 · Codex 复现①的旧行为)',
  patch('src/lib/dirtyGuard.ts', (s) =>
    s.replace(/export function hasUncommittedInput\(\): boolean \{\n    return _uncommittedInput;\n\}/,
      'export function hasUncommittedInput(): boolean {\n    return false;\n}')
     .replace(/    return _uncommittedInput \|\| activeElementLooksEditable\(\);/,
      '    return activeElementLooksEditable();')),
  ['6'], '失焦后 bfcache 复活');

// MU6 拆掉 500ms 回调里的二次守卫 —— Codex 复现②(定时器窗口内开始打字)
expectRed('MU6 拆掉 chunk 路 500ms 回调里的二次守卫(定时器窗口裸奔 · Codex 复现②)',
  patch('src/lib/errorReporter.ts', (s) =>
    s.replace(/            setTimeout\(\(\) => \{\n                if \(isAnythingDirty\(\)\) \{\n                    try \{ showUpdateBanner\(\); \} catch \{ \/\* banner 弹不出也不能刷掉输入 \*\/ \}\n                    return;\n                \}\n                window\.location\.reload\(\);\n            \}, 500\);/,
      '            setTimeout(() => { window.location.reload(); }, 500);')),
  ['7'], '排定时器之后才开始打字');

// ── 收口:必须全红 ─────────────────────────────────────────────────────
const notRed = results.filter((r) => !r.ok);
console.log(`\n变异重放:${results.length - notRed.length}/${results.length} 打红【且红对了】`);
if (notRed.length) {
  console.error('🔴 以下维度不成立(拆了还是全绿 = 保护没了;或红的不是预期那条 = 判据被顶包):\n' +
    notRed.map((r) => `  - ${r.name}【实际红:${r.redLocks.length ? '锁' + r.redLocks.join('/锁') : '无'}】`).join('\n'));
  process.exit(1);
}
console.log(`✅ 每条变异都被行为锁打红 —— 这 ${base.passed} 条锁不是恒真的`);
