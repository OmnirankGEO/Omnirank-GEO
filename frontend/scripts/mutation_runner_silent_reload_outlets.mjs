#!/usr/bin/env node
/**
 * 变异重放 · 静默 reload 出口枚举锁的**判别力自证**
 * [R3 · Deploy 预烤 NO-GO 返修 2026-08-17]
 *
 * 为什么要有这个:R1 把门禁从 `git ls-files` 改成文件系统遍历。
 * "改完还是绿" 什么都证明不了 —— **绿可能是因为它变成了恒真锁**。
 * 每条变异都必须把它打红;有一条打不红,就说明那个维度的保护没了。
 *
 * 🔴 M1 是专门盯 git→FS 这次改动的:git 版时代 `git ls-files` **看不见未跟踪文件**,
 *    而"第四份拷贝"最可能的出现方式恰恰是新建一个文件。FS 版必须仍然看得见。
 * 🔴 M6 盯的是本次修法自己的新风险:锚点从 `git rev-parse` 换成 marker 向上找 ——
 *    锚不到时**必须红**,不许"找不到就当没事"(那就是 (b) 方案的静默放行)。
 *
 * 用法:node scripts/mutation_runner_silent_reload_outlets.mjs
 */
import { execFileSync } from 'node:child_process';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const FE = resolve(HERE, '..');
const GATE = join(HERE, 'verify-silent-reload-outlets.mjs');

/** 跑门禁,返回 {code, out}。永远不抛 —— 我们要的就是它的退出码。 */
function runGate(cwd = FE, script = GATE) {
  try {
    const out = execFileSync(process.execPath, [script], { cwd, encoding: 'utf8', stdio: 'pipe' });
    return { code: 0, out };
  } catch (e) {
    return { code: e.status ?? -1, out: (e.stdout || '') + (e.stderr || '') };
  }
}

const results = [];
function expectRed(name, mutate, restore, opts = {}) {
  let r;
  try {
    mutate();
    r = runGate(opts.cwd, opts.script);
  } catch (e) {
    restore();
    // 变异自己坏了 ⇒ 既不算红也不算绿,直接判整轮失败(不许当成"这维度没问题")
    console.error(`🔴 变异未能施加:${name}\n     ${e.message}`);
    process.exit(1);
  } finally {
    restore();
  }
  const red = r.code !== 0;
  results.push({ name, red, code: r.code, msg: (r.out.match(/  - .*/) || [''])[0].slice(0, 120) });
  console.log(`${red ? '✅ 打红' : '🔴 没打红(锁在这个维度是恒真的)'}  ${name}  [exit ${r.code}]`);
  if (red) console.log(`     ${(r.out.match(/  - [^\n]*/) || ['(无明细)'])[0].trim().slice(0, 150)}`);
}

// ── 基线:未变异时必须绿(否则后面的"红"没有对照意义)────────────────────
/* 🔴 牙证:前置在本 runner 里真的会红(抛异常、自还原)。 */
proveGuardHasTeeth(resolve(FE, 'src/lib/api.ts'));

const base = runGate();
console.log(`基线(未变异): exit ${base.code} · ${base.out.split('\n')[0]}`);
if (base.code !== 0) {
  console.error('🔴 基线就是红的 —— 变异重放无意义,先修基线。');
  process.exit(1);
}

// 🔴 [自证 · 2026-08-17 实测踩过] `apply` 必须断言**内容真的变了**。
//   第一版 M5 的正则写成 `window.location.reload()`,而 screenshotMode.ts 里是不带
//   `window.` 的 `location.reload()` ⇒ 替换是 no-op ⇒ 门禁照旧绿 ⇒ 报出来像"锁在这个维度恒真"。
//   **变异没落地时的绿和红都不算数** —— 让它在这一步就大声炸,不许流到结论里。
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
        throw new Error(`${NOT_LANDED_SYNTAX} · ${p}`);
      }
    },
    undo: () => writeFileSync(p, orig),
  };
};

// M1 新建**未跟踪**文件放一处静默 reload —— git 版会瞎,FS 版必须看见
{
  const rogue = join(FE, 'src', '__mutation_rogue_reload.ts');
  expectRed('M1 新建未跟踪文件里藏一处 location.reload()(git 版曾在此失明)',
    () => writeFileSync(rogue, 'export function boom() { window.location.reload(); }\n'),
    () => rmSync(rogue, { force: true }));
}

// M2 既有白名单文件里多加一处 —— 计数漂移
{
  const m = patch('src/lib/api.ts', (s) => s + '\nexport function __mut() { window.location.reload(); }\n');
  expectRed('M2 白名单文件里多加一处(总数 +1)', m.apply, m.undo);
}

// M3 拆掉 versionPoll 的守卫调用 —— 判据 2(不许只 import 不调用)
{
  const m = patch('src/lib/versionPoll.ts', (s) =>
    s.replace(/guardedSilentReload\(\(\) => \{\s*void hardReload\(\);\s*\}, showUpdateBanner\);/,
      'void hardReload();'));
  expectRed('M3 把静默路的 guardedSilentReload 拆掉,直接 hardReload()', m.apply, m.undo);
}

// M4 main.tsx 复活第三份拷贝(换 API,专打 R4 扩的正则)—— 判据 3
{
  const m = patch('src/main.tsx', (s) => s + '\nexport function __mut() { location.assign("/"); }\n');
  expectRed('M4 main.tsx 复活自有拷贝(用 location.assign 换皮)', m.apply, m.undo);
}

// M5 删掉白名单里的一处 —— 反方向漂移同样要红(白名单不许悄悄烂掉)
{
  // 注意写法:这个文件里是不带 `window.` 的裸 `location.reload()`(第一版正则在此扑空)
  const m = patch('src/sandbox/screenshotMode.ts', (s) =>
    s.replace(/setTimeout\(\(\) => location\.reload\(\), 100\)/, 'void 0'));
  expectRed('M5 白名单文件里少一处(总数 -1 · 反方向漂移)', m.apply, m.undo);
}

// M6 锚不到 marker 时必须红 —— 本次修法自己的新风险
{
  const sandbox = mkdtempSync(join(tmpdir(), 'reload-gate-noanchor-'));
  const solo = join(sandbox, 'verify-silent-reload-outlets.mjs');
  expectRed('M6 把门禁拷到没有 marker(package.json + src/)的目录 → 必须红,不许"找不到就当没事"',
    () => copyFileSync(GATE, solo),
    () => rmSync(sandbox, { recursive: true, force: true }),
    { cwd: sandbox, script: solo });
}

// ── 收口:必须全红 ─────────────────────────────────────────────────────
const notRed = results.filter((r) => !r.red);
console.log(`\n变异重放:${results.length - notRed.length}/${results.length} 打红`);
if (notRed.length) {
  console.error('🔴 以下维度锁不住(改完还是绿 = 那个维度的保护已经没了):\n' +
    notRed.map((r) => '  - ' + r.name).join('\n'));
  process.exit(1);
}
console.log('✅ 每条变异都被打红 —— 门禁去 git 化之后判别力未降级');
