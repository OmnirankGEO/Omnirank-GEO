#!/usr/bin/env node
/**
 * 静默 reload 出口枚举锁
 * [WO_NO_SILENT_RELOAD_DIRTY_GUARD 2026-08-16 · Review 追加判据]
 *
 * ── 为什么是"枚举锁"而不是"扫可疑形状" ────────────────────────────────
 * 工单点名了两条静默 reload 路(versionPoll 的 bfcache 路、errorReporter 的 chunk 路)。
 * 本轮实测发现**第三条**:main.tsx 的 ErrorBoundary 自带第三份正则拷贝 + 自己的
 * cooldown + 自己的 location.reload(),完全绕开共享路径。
 * 只堵前两条 = 守卫有洞,而且是最难发现的形态(明面两个口都堵了,看起来已经修好了)。
 *
 * 同型教训本仓已第五次:errorReporter.ts 的注释里就写着「别再造第四份拷贝」,
 * 而第三份一直在隔壁 main.tsx。**教训写在注释里传不出去** ——
 * 所以这条锁不写成建议,写成**构建链里会自己报错的门禁**。
 *
 * 判据形态:枚举全分母 → 冻结成白名单 → 白名单外新增即红。
 * 🔴 不做"这处是不是用户点击触发"的自动判定 —— 那需要一个启发式签名
 *    (比如"往前 260 字符里有没有 onClick"),而**签名只能证明"我找的那种形状不存在"**。
 *    本轮用它试跑过:42 处被判成"非交互",其中大量其实是用户提交后的回调。
 *    枚举锁不需要判对错,它只需要保证**没有人能悄悄加一处** —— 新增就红,人来分类。
 *
 * ── 🔴 [R1 · Deploy 预烤 NO-GO 2026-08-17] 本脚本零外部进程依赖 ──────────
 * 上一版用 `execSync('git ls-files')` 取分母、`git rev-parse --show-toplevel` 找仓根。
 * Deploy 预烤实证:**在 Docker 构建里必死**,两个独立死因 ——
 *   ① builder 基础镜像 node:20-alpine **没有 git**(`command -v git` 空;同法查 node 有 ⇒ 探法有效)
 *   ② 就算装上 git 也没用:`.dockerignore` 排除 `.git`,`COPY frontend/ ./` 进来的**不是 git 仓**
 * 而三方(交付/复审/部署)全在有 git 的本机验 ⇒ **同一把尺子三道一起瞎**。
 *
 * 修法选的是「去 git 化」而不是「无 git 时跳过」:后者在 Docker 里**永远**跳过,
 * 等于门禁形同虚设,且是**静默放行** —— 本仓最恨的那种绿(见 memory:窄口径=静默放行)。
 *
 * 去 git 化之后两个环境**逐字同行为**:
 *   · 分母 = 文件系统遍历 `<frontend>/src`(天然含未跟踪文件 ⇒ M1 语义不降级,
 *     "新建一个文件放第四份拷贝"照样被看见)+ 静态忽略表(产物目录)
 *   · 锚点 = 从**本脚本自身位置**向上找 marker(package.json + src/),不看 cwd、不问 git
 *
 * ── 剥散文 ──────────────────────────────────────────────────────────
 * 必须剥 // 与 块注释:本包自己的注释里就写了好几处 window.location.reload(),
 * 不剥的话这把尺子会把**讲代码的话**判成新出口(同族第六次)。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

// ── 锚点:向上找 marker(package.json + src/)──────────────────────────
// 🔴 不用 cwd:本脚本既可能被人从仓根手跑,也会被 npm 从 frontend/ 跑,
//   两种 cwd 下相对路径含义不同 —— 不统一的话分母会**静默变 0**(0 分母的绿最坏)。
// 🔴 也不用 git rev-parse:见文件头 R1。marker 向上找在**任何**环境都成立,
//   包括 Docker builder 里那个 `/app/frontend`(有 package.json、有 src/、没有 .git)。
function findFrontendRoot(startDir) {
  let dir = startDir;
  for (let i = 0; i < 12; i++) {
    let hasPkg = false;
    let hasSrc = false;
    try { hasPkg = statSync(join(dir, 'package.json')).isFile(); } catch { /* 往上找 */ }
    try { hasSrc = statSync(join(dir, 'src')).isDirectory(); } catch { /* 往上找 */ }
    if (hasPkg && hasSrc) return dir;
    const up = dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  console.error('🔴 静默 reload 出口枚举锁 FAIL:向上 12 层没找到 marker(package.json + src/)。' +
    '\n    锚不到就**不许**当没事发生 —— 那正是"分母 0 的绿"的入口。');
  process.exit(1);
}

const FRONTEND_DIR = findFrontendRoot(dirname(fileURLToPath(import.meta.url)));
const SRC_DIR = join(FRONTEND_DIR, 'src');

/** 白名单 key(`frontend/src/...`)↔ 真实绝对路径。key 形态与环境无关,表才能冻得住。 */
const KEY_PREFIX = 'frontend/';
const keyOf = (abs) => KEY_PREFIX + relative(FRONTEND_DIR, abs).split(sep).join('/');
const pathOf = (key) => join(FRONTEND_DIR, key.slice(KEY_PREFIX.length));

// 静态忽略表:产物 / 依赖目录。git 版靠 .gitignore 排除它们,FS 版必须自己排。
// (src/ 底下正常不会有这些,留着是防"有人把 build 产物写进 src")
const IGNORED_DIRS = new Set([
  'node_modules', 'dist', 'build', 'coverage', '.vite', '.turbo', '.next', '.cache', '.git',
]);

const SCANNED_EXT = /\.(ts|tsx|js|jsx|mjs)$/;

function walk(dir, out) {
  let entries;
  try { entries = readdirSync(dir, { withFileTypes: true }); } catch { return out; }
  for (const e of entries) {
    const full = join(dir, e.name);
    if (e.isDirectory()) {
      if (IGNORED_DIRS.has(e.name)) continue;
      walk(full, out);
    } else if (e.isFile() && SCANNED_EXT.test(e.name)) {
      out.push(full);
    }
  }
  return out;
}

// 🔴 [R4 · Codex 2026-08-17] 原正则漏了两种等效形态:
//   `location.assign(url)` 与 `history.go(0)` —— 后者是**整页重载**的另一种写法。
//   漏掉它们 = 第四份拷贝换个 API 就能绕过这把锁。
const RE = /(?:location\s*\.\s*(?:reload\s*\(|replace\s*\(|assign\s*\(|href\s*=)|history\s*\.\s*go\s*\(\s*0\s*\))/g;

/** 剥 // 和 块注释,用空格填充保持偏移(行号仍可用)。 */
function stripProse(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:])\/\/[^\n]*/g, (m, p) => p + ' '.repeat(m.length - p.length));
}


// ── R4 · 「同文件删一加一」的局限,显式处置 ────────────────────────────────
// 逐文件计数挡不住"在同一个文件里删掉一处、又加一处"(总数不变)。
// 处置:除计数外再比**每处调用的形态指纹**(用的是哪个 API)。
// 删一加一时形态多半会变(reload↔href↔assign),指纹集合就会漂;
// 🔴 但如果新旧形态完全相同,指纹也挡不住 —— **这是已知残余风险,写在这里而不是假装没有**:
//   那种情况只能靠 code review 与 §H 的 Playwright 行为锁兜底。
//   (把它写明,好过让下一个人以为这把锁是密不透风的。)
function fingerprints(code) {
  const out = [];
  let m;
  const re = new RegExp(RE.source, 'g');
  while ((m = re.exec(code))) out.push(m[0].replace(/\s+/g, ''));
  return out.sort().join('|');
}

function scan() {
  // 🔴 分母 = `<frontend>/src` 下的真实文件。
  //   变异 M1 实测(git 版时代):只用 `git ls-files` 时,**新建的未跟踪文件是隐形的** ——
  //   而"第四份拷贝"最可能的出现方式恰恰就是新建一个文件。
  //   FS 遍历天然包含未跟踪文件,这条语义**不降反升**,不需要额外的 --others 分支。
  const files = walk(SRC_DIR, []).sort();
  const hits = {};
  const prints = {};
  for (const f of files) {
    let src;
    try { src = readFileSync(f, 'utf8'); } catch { continue; }
    const code = stripProse(src);
    let m, n = 0;
    RE.lastIndex = 0;
    while ((m = RE.exec(code))) n++;
    if (n) { hits[keyOf(f)] = n; prints[keyOf(f)] = fingerprints(code); }
  }
  return { hits, prints, denominator: files.length };
}

// ── 冻结白名单(2026-08-16 实测建表)──────────────────────────────────
// 🔴 这张表**不宣称这 75 处每一处都安全**。它宣称的是:今天就这些。
//    新增 / 挪窝 / 数量变化 → 红 → 必须回来说明新那处是"用户点的"还是"静默的";
//    静默的必须走 guardedSilentReload,否则它会绕开脏表单守卫刷掉用户输入。
const FROZEN = {
  // 本包持有的两条**真静默**路径(下面 assertGuarded 另有更强断言)
  'frontend/src/lib/versionPoll.ts': 4,
  'frontend/src/lib/errorReporter.ts': 1,
  // 其余为用户点击 / 导航 / 登录跳转等既有路径,本轮原样冻结
  // 4 → 5 [WO_293 · 2026-09-27]:新增 ExternalRedirect 的 location.replace(官网)。定性:导航,不是静默 reload ——
  //   只在访客自己打开 /landing 时渲染,整页离开应用去官网,没有要保护的表单输入。
  'frontend/src/App.tsx': 5,
  'frontend/src/components/c_end/CEndDrawer.tsx': 2,
  'frontend/src/components/c_end/TrialPassBanner.tsx': 1,
  'frontend/src/components/layout/M3BackBanner.tsx': 1,
  'frontend/src/components/managed/QuickRechargeDialog.tsx': 1,
  'frontend/src/components/onboarding/OnboardingWelcomeModal.tsx': 1,
  'frontend/src/components/onboarding/WelcomeChoiceModal.tsx': 1,
  'frontend/src/components/publishing/ResearchSelfserveDialog.tsx': 1,
  'frontend/src/components/wallet/WalletEntitlementsPanel.tsx': 3,
  'frontend/src/features/publicReportPremium/components/sections/IdentityCalibration.tsx': 1,
  'frontend/src/lib/api.ts': 1,
  'frontend/src/lib/openAsyncUrl.ts': 1,
  'frontend/src/lib/wechatJsapi.ts': 1,
  'frontend/src/pages/Admin/HelpCenterAdmin/FAQItemsTab.tsx': 1,
  'frontend/src/pages/Agent/LeadsPage.tsx': 1,
  'frontend/src/pages/Agent/WhitelabelSettings.tsx': 1,
  'frontend/src/pages/Customer/BuyCredit.tsx': 1,
  'frontend/src/pages/Diagnosis/DiagnosisReport.tsx': 1,
  'frontend/src/pages/Help/HelpCenter.tsx': 1,
  'frontend/src/pages/Help/HelpFAQ.tsx': 1,
  'frontend/src/pages/Login/ChangePasswordPage.tsx': 1,
  'frontend/src/pages/Login/LoginPage.tsx': 1,
  'frontend/src/pages/Monitoring/index.tsx': 1,
  'frontend/src/pages/Pricing/SubscriptionPlansPage.tsx': 4,
  'frontend/src/pages/Publishing/PublishHistory.tsx': 1,
  /*
   * 🔴 [#204 a2 · 2026-09-15] 这里**删掉**了一条:
   *      'frontend/src/pages/Writing/ImageNoteStudio.tsx': 1
   *    (原定性:制作台风格画廊里「不可用款 → 上传实拍图 →」那一条,
   *     是用户点的整页导航,不是静默刷新。)
   *
   *    冻结表的规矩是**只许变长** —— 变短通常意味着"当初那一格绿是借来的"。
   *    这一次是**合法的例外,记在这里**:
   *      被记录的那处调用点所在的**整块屏(制作 GEO 图文)本单退役、文件已删**,
   *      不是那处调用点被悄悄改没了。
   *      判据与删文件在**同一笔**提交里(删除探针证明过:分开做的话,
   *      中间那一刻六把闸是"崩"不是"红",没人看得出发生了什么)。
   *
   *    🔴 下一个想删条目的人:除非你也能说出"整块宿主没了"这种理由,
   *       否则变短就是退化 —— 先去查那处调用点到哪儿去了。
   */
  'frontend/src/pages/Subscription/SubscriptionSigningPage.tsx': 1,
  /*
   * 🔴 [开源 E3 · 前端 · 2026-10-01 · WO_322] 这里删掉了 16 条:被记录的调用点所在的**整块宿主**整目录或整文件删除
   *    (旧 C 端布局 1 · M3 组件 1 · 社媒组件 3 · M3 页面 5 · 旧社媒操盘手页面 4 · 社媒工作台页面 2),
   *    不是调用点被悄悄改没了;判据与删文件在同一笔提交里。逐条清单见交付单 E3F_FRONTEND_DELIVERY。
   */
  'frontend/src/sandbox/SandboxBanner.tsx': 2,
  'frontend/src/sandbox/screenshotMode.ts': 2,
  // ── [R4 2026-08-17] 扩正则后新浮出的 `location.assign` 站点,逐个读过调用点定性 ──
  //   这 6 处旧正则(只认 reload/replace/href)完全看不见 —— Codex 点的盲区实测存在。
  //   全部是**导航**(用户点动作卡 / 点治理提示的跳转 / C 端分屏关闭后整页跳),
  //   不是"自愈式重载",不经守卫。
  'frontend/src/components/gapPlan/GapPlanExecution.tsx': 2,   // 动作卡:去写作任务 / 去发布
  'frontend/src/components/ui/governance-alert.tsx': 1,        // 治理提示里的 nav action
  'frontend/src/features/geoObservation/nav.ts': 1,            // 观测面板内部跳转
  // [开源 E3 · 前端 · 2026-10-01 · WO_322] 原 C 端整页跳转工具(2 处)随旧 C 端孤儿一起删,上面「这 6 处」现为 4 处
};

const failures = [];
const { hits, prints, denominator } = scan();

// 判据 0:分母非空。扫到 0 处 = 尺子坏了,不是"没有出口"。
const total = Object.values(hits).reduce((a, b) => a + b, 0);
if (denominator === 0 || total === 0) {
  failures.push(`分母为 0(扫描文件 ${denominator} · 命中 ${total})= 尺子坏了,不是"没有出口"`);
}

// 判据 1:白名单外新增 / 数量漂移即红
for (const [f, n] of Object.entries(hits)) {
  if (FROZEN[f] === undefined) {
    failures.push(`新出现的 reload/跳转调用点:${f}(${n} 处)\n` +
      `    → 它是"用户点的"还是"静默的"?静默的必须走 lib/dirtyGuard.guardedSilentReload,\n` +
      `      否则会绕开脏表单守卫,把用户填了一半的内容刷掉(本包修的正是这个)。\n` +
      `      定性后把它加进 verify-silent-reload-outlets.mjs 的 FROZEN 表。`);
  } else if (FROZEN[f] !== n) {
    failures.push(`调用点数变了:${f} 期望 ${FROZEN[f]} 实测 ${n} —— 同一文件里新增一处也要重新定性`);
  }
}
for (const [f, n] of Object.entries(FROZEN)) {
  if (hits[f] === undefined) failures.push(`白名单里有已消失的条目(该清理):${f}(原 ${n} 处)`);
}

// 判据 1b:形态指纹 —— 挡「同文件删一加一」(总数不变但用的 API 变了)
const FROZEN_PRINTS = JSON.parse(process.env.OMNIRANK_RELOAD_PRINTS || '{}');
if (Object.keys(FROZEN_PRINTS).length) {
  for (const [f, fp] of Object.entries(prints)) {
    if (FROZEN_PRINTS[f] && FROZEN_PRINTS[f] !== fp) {
      failures.push(`调用形态变了(总数没变):${f}
      期望 ${FROZEN_PRINTS[f]}
      实测 ${fp}`);
    }
  }
}

// 判据 2(更强):两条真静默路径必须真的经过守卫
function assertGuarded(key, marker) {
  let raw;
  try { raw = readFileSync(pathOf(key), 'utf8'); }
  catch { failures.push(`${key} 读不到 —— 判据无法执行(不当成绿)`); return; }
  const code = stripProse(raw);
  // 🔴 变异 M3 实测:第一版只查"guardedSilentReload 在 marker 之前出现过" ——
  //   **import 那一行就满足了**,把真正的调用拆掉照样绿 = 恒真锁。
  //   改成要求重载动作**落在 guardedSilentReload( 的实参括号内**:
  //   从每个调用点起做括号配平,marker 必须落在某个调用的实参区间里。
  const callRe = /guardedSilentReload\s*\(/g;
  const spans = [];
  let c;
  while ((c = callRe.exec(code))) {
    let depth = 0;
    let i = c.index + c[0].length - 1;      // 落在 '(' 上
    for (; i < code.length; i++) {
      if (code[i] === '(') depth++;
      else if (code[i] === ')') { depth--; if (depth === 0) break; }
    }
    spans.push([c.index, i]);
  }
  if (!spans.length) {
    failures.push(`${key} 没有 guardedSilentReload **调用点**(只 import 不调用不算)—— 静默路径绕开了脏表单守卫`);
    return;
  }
  const at = code.indexOf(marker);
  if (at < 0) { failures.push(`${key} 锚点消失,判据失效:${marker}`); return; }
  if (!spans.some(([a, b]) => at > a && at < b)) {
    failures.push(`${key} 的重载动作(${marker})不在 guardedSilentReload(...) 的实参里 —— ` +
      '守卫被绕过了(它可能只是还挂在 import 里)');
  }
}
assertGuarded('frontend/src/lib/versionPoll.ts', 'hardReload()');
assertGuarded('frontend/src/lib/errorReporter.ts', 'window.location.reload()');

// 判据 3:main.tsx 不许再持有自己的一份(第三份拷贝的墓碑)
const mainCode = stripProse(readFileSync(pathOf('frontend/src/main.tsx'), 'utf8'));
RE.lastIndex = 0;
if (RE.test(mainCode)) {
  failures.push('main.tsx 又出现了直接的 location 重载/跳转 —— 第三份拷贝复活,' +
    '它会绕开 errorReporter 的守卫(2026-08-16 实测过一次)');
}

console.log(`静默 reload 出口枚举锁:分母 ${denominator} 个文件 · 命中 ${Object.keys(hits).length} 文件 / ${total} 处 · 锚点 ${FRONTEND_DIR}`);
if (failures.length) {
  console.error('\n🔴 静默 reload 出口枚举锁 FAIL:\n' + failures.map((f) => '  - ' + f).join('\n'));
  process.exit(1);
}
console.log('✅ 出口集合与白名单一致 · 两条静默路径都在守卫之后 · main.tsx 无自有拷贝');
