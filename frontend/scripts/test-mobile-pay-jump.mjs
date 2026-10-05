#!/usr/bin/env node
/**
 * 门禁 · 移动端支付「点了没反应」修复锁(可执行行为断言)
 * [WO_MOBILE_PAY_JUMP 2026-08-07]
 *
 * 与 tests/test_mobile_pay_jump_2026_08_07.py 互补:
 *   那边管"源码里必须/不许有什么"(含跨语言 token 对齐),
 *   这边**把 paymentEnv.ts 真的转译出来跑一遍** —— 喂生产实测 UA,看判据出什么。
 *   纯文本判据抓不到"实现写反了但字面都在"的那类退化,只有真跑能抓。
 *
 * 跑法:node scripts/test-mobile-pay-jump.mjs
 * ⚠️ 本锁**未接进 package.json 的 build 链** —— frontend/package.json 现有
 *    natdlg-20260806 + publayout-20260804 双声明,刻意避让。接线交 Deploy 后补。
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import process from 'node:process'
import { pathToFileURL } from 'node:url'
import esbuild from 'esbuild'

const root = process.cwd()
const src = path.join(root, 'src/lib/paymentEnv.ts')
// 🔴 不写 node_modules/.cache:本包的 node_modules 可能是指向别的 worktree 的 junction,
//    往那里写会污染别人的树。
const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'paylock-'))
const outFile = path.join(outDir, 'paymentEnv.mjs')

esbuild.buildSync({
  entryPoints: [src],
  outfile: outFile,
  format: 'esm',
  platform: 'node',
  bundle: false,
  loader: { '.ts': 'ts' },
})

const env = await import(pathToFileURL(outFile).href)

let failed = 0
const eq = (actual, expected, what) => {
  if (actual !== expected) {
    console.error(`❌ ${what}\n   期望 ${JSON.stringify(expected)}  实际 ${JSON.stringify(actual)}`)
    failed++
  } else {
    console.log(`✅ ${what}`)
  }
}

// ── 生产实测 UA(工单 §1 两个案例的真实形态)────────────────────────────────
const UA = {
  macWechat:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) '
    + 'Version/16.3 Safari/605.1.15 MicroMessenger/6.8.0(0x16080000) MacWechat/3.8.6(0x13080610) '
    + 'UnifiedPCMacWechat(0xf2640611) Concurrent',
  winWechat:
    'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 '
    + 'Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090c11)',
  androidWechat:
    'Mozilla/5.0 (Linux; Android 13; PGT-AN10; wv) AppleWebKit/537.36 (KHTML, like Gecko) '
    + 'Version/4.0 Chrome/107.0.5304.141 Mobile Safari/537.36 MicroMessenger/8.0.42.2460(0x28002A35)',
  iosWechat:
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) '
    + 'Mobile/15E148 MicroMessenger/8.0.42(0x18002a2f) NetType/WIFI Language/zh_CN',
  iosSafari:
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) '
    + 'Version/18.0 Mobile/15E148 Safari/604.1',
  androidChrome:
    'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) '
    + 'Chrome/126.0.0.0 Mobile Safari/537.36',
  macSafari:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) '
    + 'Version/17.4 Safari/605.1.15',
  winChrome:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
    + 'Chrome/126.0.0.0 Safari/537.36',
}

console.log('§1 桌面微信识别(案例 A)')
eq(env.isDesktopWechatUa(UA.macWechat), true, 'Mac 微信 → 桌面微信')
eq(env.isDesktopWechatUa(UA.winWechat), true, 'Windows 微信 → 桌面微信')
eq(env.isMobileWechatUa(UA.macWechat), false, 'Mac 微信不算手机微信(不许再走 JSAPI)')

console.log('\n§1-反向 必须不命中(工单 §3:不许把移动微信一起改掉)')
eq(env.isDesktopWechatUa(UA.androidWechat), false, '安卓微信 ≠ 桌面微信')
eq(env.isDesktopWechatUa(UA.iosWechat), false, 'iOS 微信 ≠ 桌面微信')
eq(env.isMobileWechatUa(UA.androidWechat), true, '安卓微信仍是手机微信(11 秒付成那条路)')
eq(env.isMobileWechatUa(UA.iosWechat), true, 'iOS 微信仍是手机微信')
eq(env.isDesktopWechatUa(UA.macSafari), false, '普通 Mac Safari ≠ 桌面微信(判据没退化成"只要是桌面")')
eq(env.isWechatUa(UA.macWechat), true, '桌面微信**仍然是**微信(摘出去的是路由,不是身份)')

console.log('\n§2 移动端判据')
eq(env.isMobileUa(UA.iosSafari), true, 'iPhone Safari → 移动端')
eq(env.isMobileUa(UA.androidChrome), true, '安卓 Chrome → 移动端')
eq(env.isMobileUa(UA.androidWechat), true, '安卓微信 → 移动端')
eq(env.isMobileUa(UA.macWechat), false, '🔴 桌面微信不许被当成移动端(否则会掉进虎皮椒)')
eq(env.isMobileUa(UA.winChrome), false, 'Windows Chrome → 非移动端')
eq(env.isMobileUa(''), false, '空 UA → 非移动端(与后端一致)')

console.log('\n§3 支付链接开不开新窗口(案例 B 的修复)')
eq(env.shouldOpenPayLinkInNewTab(UA.iosSafari), false, 'iPhone Safari → 同窗跳(_blank 会被弹窗拦截吃掉)')
eq(env.shouldOpenPayLinkInNewTab(UA.androidChrome), false, '安卓 → 同窗跳')
eq(env.shouldOpenPayLinkInNewTab(UA.winChrome), true, '桌面 → 保持新窗口(不顶掉当前页)')
eq(env.shouldOpenPayLinkInNewTab(UA.macWechat), true, '桌面微信 → 按桌面处理')

console.log('\n§4 摊进 <a> 的 props 形态')
eq(JSON.stringify(env.payLinkTargetProps(UA.iosSafari)), '{}', '移动端**不产出** target(不是产出 target="_self")')
eq(env.payLinkTargetProps(UA.winChrome).target, '_blank', '桌面产出 _blank')
eq(env.payLinkTargetProps(UA.winChrome).rel, 'noreferrer', '桌面同时带 rel')

console.log('\n§5 判据自证(证明上面这些断言不是恒真)')
{
  // 把"桌面微信"换成一个不存在的 token,§1 那组必须立刻塌掉
  const mutated = fs.readFileSync(src, 'utf8')
    .replace("['windowswechat', 'macwechat']", "['__never_matches__']")
  const mFile = path.join(outDir, 'mutated.mjs')
  esbuild.buildSync({
    stdin: { contents: mutated, resolveDir: path.dirname(src), loader: 'ts' },
    outfile: mFile, format: 'esm', platform: 'node', bundle: false,
  })
  const m = await import(pathToFileURL(mFile).href)
  const stillDetects = m.isDesktopWechatUa(UA.macWechat)
  eq(stillDetects, false, '反向对照:token 表被掏空后桌面微信识别确实失效 → 上面的断言有判别力')
  eq(m.shouldOpenPayLinkInNewTab(UA.macWechat), true, '反向对照:桌面微信仍按 UA 判成桌面(不靠 token 表)')
}

fs.rmSync(outDir, { recursive: true, force: true })

if (failed > 0) {
  console.error(`\n🔴 ${failed} 条判据失败`)
  process.exit(1)
}
console.log('\n✅ mobile-pay-jump 锁通过')
