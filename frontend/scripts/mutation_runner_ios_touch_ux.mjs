#!/usr/bin/env node
/**
 * 变异验证 · 证明 test-ios-touch-ux.mjs 的每条判据都有判别力
 * [WO_IOS_TOUCH_UX 2026-08-05]
 *
 * 为什么必须有这个:35 条锁全绿**不能**证明锁在工作 —— 恒真的判据也是全绿。
 * 这里逐条把修复破坏掉,断言锁**必须转红**。杀不掉的变异 = 那条锁是假的。
 *
 * 术语(2026-07-29 踩过:把 SKIP 当成 SURVIVED 报了假成绩):
 *   KILLED   变异后锁转红 = 判据有效
 *   SURVIVED 变异后锁仍绿 = 🔴 判据是瞎的
 *   SKIP     锚点没找到,**没能施加变异** = 既不算杀也不算活,单独计数并当失败处理
 *
 * 跑法:node scripts/mutation_runner_ios_touch_ux.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX , proveGuardHasTeeth } from './lib/poison-syntax-guard.mjs';

const HERE = import.meta.dirname;
const SRC = path.resolve(HERE, '../src');
const GATE = path.join(HERE, 'test-ios-touch-ux.mjs');

const F = {
  report: path.join(SRC, 'pages/Diagnosis/DiagnosisReport.tsx'),
  openUtil: path.join(SRC, 'lib/openAsyncUrl.ts'),
  css: path.join(SRC, 'index.css'),
  mc: path.join(SRC, 'pages/MaterialConfirm/MaterialConfirmPage.tsx'),
  hook: path.join(SRC, 'hooks/useSoftKeyboardOpen.ts'),
  invite: path.join(SRC, 'pages/Referral/InviteCenter.tsx'),
  // [开源 E3 · 前端 · 2026-10-01 · WO_322] 原靶子(M3 方案页)随 pages/M3 删;同步直开的靶子换成监测门户令牌弹窗 TokenDialog
  token: path.join(SRC, 'pages/Monitoring/components/TokenDialog.tsx'),
  gate: GATE,
};

/** 锁是否通过 */
function gatePasses() {
  try {
    execFileSync(process.execPath, [GATE], { stdio: 'pipe' });
    return true;
  } catch {
    return false;
  }
}

const MUTATIONS = [
  {
    name: 'M1 把「打开报告」改回 await 之后 window.open',
    file: F.report,
    apply: (s) => s.replace(
      /void openAsyncUrl\(async \(\) => \{/,
      'void (async () => { const _u = await Promise.resolve(""); window.open(_u, "_blank"); })(); void (async () => {'),
    expect: '锁1(新增未豁免断裂点)+ 锁2(没走 openAsyncUrl)',
  },
  {
    name: 'M2 占位 window.open 加上 noopener(规范会返回 null → 保窗失效)',
    file: F.openUtil,
    apply: (s) => s.replace(`win = window.open('', '_blank');`, `win = window.open('', '_blank', 'noopener');`),
    expect: '锁3-反向 占位 open 不带 noopener',
  },
  {
    name: 'M3 被拦时回落 location.href(会顶掉用户当前页面)',
    file: F.openUtil,
    apply: (s) => s.replace(
      'return { ok: false, url, blocked: true };',
      'window.location.href = url;\n    return { ok: false, url, blocked: true };'),
    expect: '锁3-反向 无 location.href 回落',
  },
  {
    name: 'M4 断掉 opener 置空(安全回退)',
    file: F.openUtil,
    apply: (s) => s.replace(/try \{ win\.opener = null; \}[^\n]*\n/, ''),
    expect: '锁3 手动断 opener',
  },
  {
    name: 'M5 CSS 去掉大字号排除(会把 text-2xl 输入框压成 16px)',
    file: F.css,
    apply: (s) => s.replace(/:not\(\.text-lg\):not\(\.text-xl\):not\(\.text-2xl\):not\(\.text-3xl\):not\(\.text-4xl\)/g, ''),
    expect: '锁4-反向 排除 text-lg 及以上',
  },
  {
    name: 'M6 CSS 改用 !important 硬压',
    file: F.css,
    apply: (s) => s.replace(/(@media \(hover: none\) and \(pointer: coarse\)[\s\S]*?)font-size: 16px;/,
      '$1font-size: 16px !important;'),
    expect: '锁4-反向 不用 !important',
  },
  {
    name: 'M7 把触屏规则包进 @layer utilities(压不过 utilities)',
    file: F.css,
    apply: (s) => s.replace(/@media \(hover: none\) and \(pointer: coarse\) \{/,
      '@layer utilities {\n@media (hover: none) and (pointer: coarse) {'),
    // 只加不闭合会让 CSS 语法坏,但本锁只做静态文本判定;这里要验的是"是否在 layer 内"
    expect: '锁4-反向 规则未被包进 @layer',
  },
  {
    name: 'M8 CSS 改用 [class*="text-"](会被 text-center 误命中 → 规则形同虚设)',
    file: F.css,
    apply: (s) => s.replace(/input:not\(\.text-lg\)/, 'input:not([class*="text-"]):not(.text-lg)'),
    expect: '锁4-反向 不用 [class*="text-"]',
  },
  {
    name: 'M9 键盘位移从 framer 的 y 挪到 style.transform(会被 motion 覆盖 = 假修复)',
    file: F.mc,
    apply: (s) => s
      .replace(/animate=\{\{ y: keyboardInset > 0 \? -keyboardInset : 0, opacity: 1 \}\}/,
        'animate={{ y: 0, opacity: 1 }}')
      .replace(`style={{ paddingBottom: 'env(safe-area-inset-bottom, 0px)' }}`,
        "style={{ paddingBottom: 'env(safe-area-inset-bottom, 0px)', transform: `translateY(-${keyboardInset}px)` }}"),
    expect: '锁5-反向 位移走 framer animate y',
  },
  {
    name: 'M10 hook 不再扣 visualViewport.offsetTop(iOS 上底部条顶过头)',
    file: F.hook,
    apply: (s) => s.replace('window.innerHeight - vv.height - vv.offsetTop', 'window.innerHeight - vv.height'),
    expect: '锁5-反向 扣掉 offsetTop',
  },
  {
    name: 'M11 客户填表页拿了 hook 但不用键盘高度',
    file: path.join(SRC, 'pages/Intake/IntakeFillPage.tsx'),
    apply: (s) => s.replace(
      /style=\{keyboardInset > 0 \? \{ transform: `translateY\(-\$\{keyboardInset\}px\)` \} : undefined\}/,
      ''),
    expect: '锁5 真的用了键盘高度做位移',
  },
  {
    name: 'M12 推荐码复制失败只报错、不把内容交还用户',
    file: F.invite,
    apply: (s) => s.replace('else setManualCopyText(text);', "else toast.error('复制失败');"),
    expect: '锁6 复制失败把内容交还用户',
  },
  {
    name: 'M13 顺手把同步手势内的裸 window.open 也改掉(扩大化误改)',
    file: F.token,
    apply: (s) => s.replace('onClick={() => window.open(`/portal/${tokenInfo.token}`', 'onClick={() => void openAsyncUrl(async () => `/portal/${tokenInfo.token}`'),
    expect: '锁2-反向 同步直开保持不动',
  },
  {
    // 🔴 第一版这条变异写错了:`const DANGER = [].concat([...])` 并没有清空数组,
    //    内容原样还在 → 变异等于没施加,却报了 SURVIVED(冤枉了锁)。
    //    改成真正把列表清空。
    name: 'M14 让扫描器失效(真清空危险 API 列表)—— 验锁1 的"0 个意外断裂点"不是恒真',
    file: F.gate,
    apply: (s) => s.replace(/const DANGER = \[[\s\S]*?\n\];/, 'const DANGER = [];'),
    expect: '锁1-反向 SAFE 桶 > 35(扫描器不是恒空)',
  },
  {
    name: 'M15 去掉可选链归一化 —— 验 clipboard?.writeText 不会整片漏检',
    file: F.gate,
    apply: (s) => s.replace(".replace(/\\?\\./g, '.')", ''),
    expect: '锁1-反向 可选链写法的调用点也扫得到(BrandDetailPage;原靠社媒工作台那处白名单,随其删除改点名)',
  },
];

let killed = 0, survived = 0, skipped = 0;
const log = [];

// 先确认基线是绿的 —— 基线红的话所有变异都会"看起来被杀",全是假成绩
if (!gatePasses()) {
  console.error('🔴 基线就没通过门禁,变异验证无意义。先修好再跑。');
  process.exit(1);
}
console.log('  基线 = 门禁通过 ✅(反向对照:基线若为红,后面每条都会假装被杀)\n');

/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红。
 *    少了它,`syntaxOk()` 平时永远返回 true —— 一把恒 true 的尺子
 *    与「每一发毒都下成了」读数完全同形。
 *    (对照臂 `assertRulerWorks` 管反方向:恒 false。两条臂缺一不可。)
 */
proveGuardHasTeeth(MUTATIONS[0].file, console.log);

for (const m of MUTATIONS) {
  const orig = fs.readFileSync(m.file, 'utf8');
  const mutated = m.apply(orig);
  if (mutated === orig) {
    skipped++;
    log.push(`  SKIP     ${m.name}\n           🔴 锚点没匹配上,**变异没施加成功** —— 不算杀也不算活,当失败处理`);
    continue;
  }
  try { assertRulerWorks(m.file); } catch (e) { log.push(`  ${e.message}`); console.log(log.join('\n')); process.exit(3); }
  try {
    fs.writeFileSync(m.file, mutated, { encoding: 'utf8' });
    if (!syntaxOk(m.file)) {
      skipped++;
      log.push(`  SKIP     ${m.name}\n           ${NOT_LANDED_SYNTAX}`);
      continue;
    }
    const stillGreen = gatePasses();
    if (stillGreen) {
      survived++;
      log.push(`  SURVIVED ${m.name}\n           🔴 期望转红的判据: ${m.expect} —— 它没响,是瞎的`);
    } else {
      killed++;
      log.push(`  KILLED   ${m.name}  (${m.expect})`);
    }
  } finally {
    fs.writeFileSync(m.file, orig, { encoding: 'utf8' });
  }
}

console.log(log.join('\n'));
console.log(`\n  ${MUTATIONS.length} 个变异 · KILLED=${killed} SURVIVED=${survived} SKIP=${skipped}`);

// 恢复后必须回到绿 —— 否则说明 finally 没把文件写回去,后面所有工作都建在脏树上
if (!gatePasses()) {
  console.error('\n🔴 变异恢复后门禁没回到绿 —— 工作树被污染了,立刻 git checkout 相关文件');
  process.exit(1);
}
console.log('  恢复后门禁回到绿 ✅(证明 finally 真的把文件写回去了)');

if (survived || skipped) {
  console.error(`\n🔴 有 ${survived} 个变异存活 / ${skipped} 个没施加成功 —— 判据不合格`);
  process.exit(1);
}
console.log('  ✅ 全部变异被杀 —— 每条判据都验证过有判别力');
