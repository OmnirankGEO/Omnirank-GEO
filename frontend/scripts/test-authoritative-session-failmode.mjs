/**
 * test-authoritative-session-failmode.mjs — [BUG-3 2026-07-27] 会话确认三态的**可执行**断言。
 *
 * 仓库没有前端单测框架，沿用 test-publication-contract-logic.mjs 的做法：
 * 用 esbuild 现场把 src/lib/authoritativeSession.ts 转成 JS 再 import，真跑一遍行为。
 *
 * 事故背景：/api/auth/me 一慢，requireConfirmedSessionToken() 直接 throw →
 * 冒到 ErrorBoundary → 整页白屏；客户门户表现为"数据全丢失"。
 *
 * 三态各一条：
 *   1. /me 在途        → 不抛错，调用方能 await 到结果
 *   2. /me 返回 401    → token 被清、走匿名/登录流程，不白屏
 *   3. /me 超时/网络错 → 重试耗尽后给【可恢复】错误，不白屏
 *
 * 🔴 最重要的一条是 fail-open 安全锁：任何状态下，未经 /me 确认的 token
 *    都不得被返回（返回即会进 Authorization 头）。
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { pathToFileURL } from 'node:url'
import esbuild from 'esbuild'

const root = process.cwd()
const src = path.join(root, 'src/lib/authoritativeSession.ts')
const outDir = path.join(root, 'node_modules/.cache/authoritative-session-test')
fs.mkdirSync(outDir, { recursive: true })

let failed = 0
const ok = (cond, what) => {
  if (cond) console.log(`✅ ${what}`)
  else { console.error(`❌ ${what}`); failed++ }
}
const eq = (actual, expected, what) => ok(
  JSON.stringify(actual) === JSON.stringify(expected),
  `${what}（期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}）`,
)

// ---------------------------------------------------------------- 浏览器环境替身
function installBrowserGlobals(initialToken) {
  const store = new Map()
  if (initialToken !== null) store.set('omnirank_token', initialToken)
  globalThis.localStorage = {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: k => store.delete(k),
  }
  globalThis.window = { dispatchEvent: () => true }
  globalThis.CustomEvent = class CustomEvent {
    constructor(type, init) { this.type = type; this.detail = init?.detail }
  }
  if (!globalThis.crypto?.randomUUID) {
    globalThis.crypto = { randomUUID: () => `stub-${Math.random().toString(36).slice(2)}` }
  }
  return store
}

// 每个场景都要一份全新的模块实例（模块级状态是有意的“this tab”语义）
let moduleSeq = 0
async function freshModule(initialToken) {
  const store = installBrowserGlobals(initialToken)
  const outFile = path.join(outDir, `session-${moduleSeq++}.mjs`)
  esbuild.buildSync({
    entryPoints: [src], outfile: outFile, format: 'esm',
    platform: 'node', bundle: false, loader: { '.ts': 'ts' },
  })
  return { mod: await import(pathToFileURL(outFile).href), store }
}

const TOKEN = 'jwt-token-abc'

// ================================================================ 态 1：/me 在途
{
  const { mod } = await freshModule(TOKEN)
  eq(mod.getSessionConfirmationPhase(), 'pending', '有 token 的初始态是 pending，不是 failed 也不是 confirmed')

  // 同步入口仍 fail-closed（抛错），但抛的是【可恢复】的 pending 错误
  let sync = null
  try { mod.requireConfirmedSessionToken() } catch (e) { sync = e }
  ok(sync !== null, '态1 同步入口未确认时仍不返回 token（fail-closed 不变）')
  eq(sync?.code, 'AUTHORITATIVE_SESSION_PENDING', '态1 抛的是 pending 错')
  ok(mod.isRecoverableSessionError(sync), '态1 pending 错被判定为可恢复（ErrorBoundary 据此不显示白屏）')

  // 异步入口：不抛错，挂起等待；/me 落定后拿到 token
  let resolved = 'still-waiting'
  const pending = mod.awaitConfirmedSessionToken().then(v => { resolved = v })
  await new Promise(r => setTimeout(r, 20))
  eq(resolved, 'still-waiting', '态1 /me 在途时 await 挂起等待，没有立刻抛错')

  mod.confirmAuthoritativeSessionToken(TOKEN)   // /me 成功回来
  await pending
  eq(resolved, TOKEN, '态1 /me 确认后等待方拿到 token')
  eq(mod.getSessionConfirmationPhase(), 'confirmed', '态1 确认后 phase=confirmed')
  eq(mod.requireConfirmedSessionToken(), TOKEN, '态1 确认后同步入口也放行')
}

// ================================================================ 态 2：/me 返回 401
{
  const { mod, store } = await freshModule(TOKEN)
  let resolved = 'still-waiting'
  const pending = mod.awaitConfirmedSessionToken().then(v => { resolved = v }, e => { resolved = e })
  await new Promise(r => setTimeout(r, 10))

  // AuthContext 在硬 401 分支做的事：标 unauthorized + 清 token
  mod.markSessionConfirmationFailed('unauthorized')
  mod.clearAuthoritativeSessionToken()
  await pending

  eq(resolved, null, '态2 401 → 等待方拿到 null（按匿名走 / 跳登录），不是异常')
  eq(store.get('omnirank_token'), undefined, '态2 401 → token 已从 localStorage 清掉')
  eq(mod.getSessionConfirmationPhase(), 'anonymous', '态2 清完 token 后是匿名态')
  eq(mod.requireConfirmedSessionToken(), null, '态2 匿名请求放行，同步入口不抛错（不白屏）')
}

// ================================================================ 态 3：/me 超时 / 网络错
{
  const { mod, store } = await freshModule(TOKEN)
  let outcome = 'still-waiting'
  const pending = mod.awaitConfirmedSessionToken().then(v => { outcome = { value: v } }, e => { outcome = e })
  await new Promise(r => setTimeout(r, 10))

  // AuthContext 退避重试耗尽后做的事：标 unreachable、【保住】token（不踢用户下线）
  mod.markSessionConfirmationFailed('unreachable')
  await pending

  ok(outcome instanceof Error, '态3 重试耗尽 → 抛错而不是静默放行')
  eq(outcome?.code, 'AUTHORITATIVE_SESSION_UNAVAILABLE', '态3 抛的是 unavailable 错')
  ok(mod.isRecoverableSessionError(outcome), '态3 该错误可恢复 → ErrorBoundary 给"重试/重新登录"，不是 stack trace')
  eq(store.get('omnirank_token'), TOKEN, '态3 网络问题不清 token（不把抖动当登出）')

  // 同步入口在该态抛的也是可恢复错
  let sync = null
  try { mod.requireConfirmedSessionToken() } catch (e) { sync = e }
  eq(sync?.code, 'AUTHORITATIVE_SESSION_UNAVAILABLE', '态3 同步入口抛 unavailable')

  // 重试（retryAuth）重新起飞 → 回到 pending，等待方继续等而不是拿旧失败态
  mod.markSessionConfirmationPending()
  eq(mod.getSessionConfirmationPhase(), 'pending', '态3 重试起飞后回到 pending')
  let retryResolved = 'still-waiting'
  const retry = mod.awaitConfirmedSessionToken().then(v => { retryResolved = v })
  await new Promise(r => setTimeout(r, 10))
  eq(retryResolved, 'still-waiting', '态3 重试期间等待方继续等')
  mod.confirmAuthoritativeSessionToken(TOKEN)
  await retry
  eq(retryResolved, TOKEN, '态3 重试成功后恢复可用 —— 这就是"可恢复"的实际含义')
}

// ================================================================ 🔴 fail-open 安全锁
{
  // 确认失败后，绝不能因为"等到了"就把未确认的 token 放出去
  const { mod } = await freshModule(TOKEN)
  const p = mod.awaitConfirmedSessionToken().then(v => ({ value: v }), e => ({ error: e }))
  mod.markSessionConfirmationFailed('unreachable')
  const r = await p
  ok(r.error !== undefined && r.value === undefined, '安全锁：确认失败时绝不返回未确认的 token')

  // 即便 phase 被标成 confirmed，只要 confirmedToken 没对上（比如 /me 确认的是另一个 token），也不放行
  const { mod: m2, store: s2 } = await freshModule(TOKEN)
  m2.confirmAuthoritativeSessionToken('some-other-token')  // 与 storage 不符 → 不该确认成功
  eq(m2.getSessionConfirmationPhase(), 'pending', '安全锁：确认对象与 storage 不符时不得进 confirmed')
  s2.set('omnirank_token', TOKEN)
  let leaked = null
  try { leaked = m2.requireConfirmedSessionToken() } catch { leaked = 'threw' }
  eq(leaked, 'threw', '安全锁：token 未被本 tab 确认过就必须继续 fail-closed')

  // peek 版永不抛错，但同样不得泄漏未确认 token
  eq(m2.peekConfirmedSessionToken(), null, '安全锁：peek 不抛错，但未确认时返回 null 而不是 token')
}

// ========== 🔴 fail-open 安全锁 · 等到 'confirmed' 但 settled !== confirmedToken ==========
// Review-CTO 变异实测抓出的缺口：把 awaitConfirmedSessionToken 末尾的
//   `return settled !== null && settled === confirmedToken ? settled : null;`
// 改成 `return settled;`（"等到了就放行"），原有 52 条断言一条都不红。
//
// 触发场景是真实的跨 tab 竞态：本 tab 在等 /me，等待期间另一个 tab 换了账号
// （localStorage 被改成另一个 token）。phase 确实落到了 'confirmed'，
// 但那是【上一个 token】的确认结果，storage 里已经是别人的 token 了。
// 此时若 `return settled`，就会把一个从没被本 tab 确认过的 token 放进 Authorization 头。
{
  const TOKEN_B = 'jwt-token-from-another-tab'
  const { mod, store } = await freshModule(TOKEN)

  let outcome = 'still-waiting'
  const pending = mod.awaitConfirmedSessionToken().then(v => { outcome = { value: v } }, e => { outcome = e })
  await new Promise(r => setTimeout(r, 10))
  eq(outcome, 'still-waiting', '前置：/me 在途，await 挂起中')

  // 同一个同步块内：先让 A 的 /me 确认成功（phase → 'confirmed'，唤醒等待方），
  // 紧接着另一个 tab 把 storage 换成 B。等待方恢复执行时，phase 是 'confirmed'，
  // 但 storage(B) 与 confirmedToken(A) 不一致 —— 这正是守卫要拦的那一刻。
  mod.confirmAuthoritativeSessionToken(TOKEN)
  store.set('omnirank_token', TOKEN_B)
  await pending

  ok(!(outcome instanceof Error), '该场景不该抛错（另一个 tab 换号不是故障）')
  eq(outcome?.value, null,
    '🔴 安全锁：等到 confirmed 但 settled !== confirmedToken 时必须返回 null，不得返回 settled')
  ok(outcome?.value !== TOKEN_B,
    '🔴 安全锁：绝不返回从未被本 tab 确认过的 token（fail-open 会在这里泄漏 TOKEN_B）')
  eq(mod.getSessionConfirmationPhase(), 'pending',
    '换号后新候选回到 pending，等它自己的 /me —— 不是继承上一个 token 的 confirmed')

  // 新候选完成确认后应当正常放行，证明上面的拦截不是把功能拦死了
  mod.confirmAuthoritativeSessionToken(TOKEN_B)
  eq(mod.requireConfirmedSessionToken(), TOKEN_B, '新候选自己确认后正常放行（拦的是未确认，不是拦所有）')
}

// ================================================================ 等待上限（不许永久挂起）
{
  const { mod } = await freshModule(TOKEN)
  const started = Date.now()
  const phase = await mod.waitForSessionConfirmation(60)
  eq(phase, 'failed_unreachable', '等待超时按不可达处理（永久挂起 = 另一种白屏）')
  ok(Date.now() - started >= 55, '等待确实等满了超时时间')
  ok(mod.SESSION_CONFIRM_WAIT_TIMEOUT_MS >= 10000, '默认等待上限覆盖 /me 首请求 + 退避重试的最坏路径')
  ok(mod.SESSION_CONFIRM_RETRY_BACKOFF_MS === 2000, '退避 2s（事故实测新建连接 2.0~2.7s，复用后 0.57s）')
}

// ================================================================ 匿名不受影响
{
  const { mod } = await freshModule(null)
  eq(mod.getSessionConfirmationPhase(), 'anonymous', '无 token → 匿名态')
  eq(mod.requireConfirmedSessionToken(), null, '匿名请求照常放行')
  eq(await mod.awaitConfirmedSessionToken(), null, '匿名 await 立即返回 null，不等待')
}

console.log('')
if (failed > 0) {
  console.error(`❌ 会话三态断言 ${failed} 条未通过`)
  process.exit(1)
}
console.log('✅ 会话确认三态断言全部通过')
