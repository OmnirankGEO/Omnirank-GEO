/**
 * server-guard 的**反向对照**(R3-P5 判据纪律)。
 *
 * 护栏最危险的失败方式是"稳定地说没问题"。这两条把护栏按到该红的场景里,
 * 证明它真的会红、而且**没有**顺手杀掉不该杀的东西。
 *
 *   node --experimental-strip-types tests/xiaobang-unified/guard-selfcheck.mts probe-failclosed
 *   node --experimental-strip-types tests/xiaobang-unified/guard-selfcheck.mts foreign-no-kill
 *
 * 退出码 0 = 该红的确实红了(反向对照成立);非 0 = 护栏是瞎的。
 */
import { spawn } from 'node:child_process'
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  PORT, PROBE_BREAK_ENV,
  assertPortFreeBeforeServerStarts, globalTeardown, identityOf, listenersOn,
} from './server-guard.ts'

const STATE_FILE = join(tmpdir(), 'xbvnext-pw', `port-${PORT}.json`)

function expectThrow(label: string, fn: () => void): string {
  try {
    fn()
  } catch (exc) {
    const message = (exc as Error).message
    if (!message.includes('[基础设施')) {
      throw new Error(`${label}:抛了,但没自报是基础设施红 → ${message}`)
    }
    console.log(`  ✅ ${label} 报红:${message.split('\n')[0]}`)
    return message
  }
  throw new Error(`${label}:**没有抛** —— 该红没红,护栏是瞎的`)
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** ② 探测器失效必须 fail-closed:前置检查与 teardown 两个入口都要红。 */
function probeFailClosed(): void {
  console.log(`[反向对照 ②] 模拟探测器失效(${PROBE_BREAK_ENV}=1)`)
  process.env[PROBE_BREAK_ENV] = '1'
  delete process.env.XBVNEXT_PORT_GUARD_DONE
  expectThrow('前置检查', () => assertPortFreeBeforeServerStarts())
  expectThrow('teardown', () => globalTeardown())
  delete process.env[PROBE_BREAK_ENV]

  // 正向:探测器好的时候,同样两个入口在干净环境下**不该**红。
  delete process.env.XBVNEXT_PORT_GUARD_DONE
  try { rmSync(STATE_FILE, { force: true }) } catch { /* noop */ }
  assertPortFreeBeforeServerStarts()
  globalTeardown()
  console.log('  ✅ 探测器正常 + 端口空闲时不红(证明上面的红来自失效,不是恒红)')
}

/** ① 身份不匹配的进程:必须只报红、**不许杀**。 */
async function foreignNoKill(): Promise<void> {
  console.log('[反向对照 ①] 端口上放一个身份不匹配的进程')
  const fake = spawn(process.execPath, [
    '-e',
    `require('net').createServer().listen(${PORT},'127.0.0.1',()=>{` +
    "process.stdout.write('up');setTimeout(()=>process.exit(0),120000)})",
  ], { stdio: ['ignore', 'pipe', 'ignore'] })
  await new Promise<void>((resolve, reject) => {
    fake.stdout.once('data', () => resolve())
    fake.once('error', reject)
    setTimeout(() => reject(new Error('假监听进程没起来')), 15_000)
  })
  const fakePid = fake.pid as number
  console.log(`  假进程 PID ${fakePid} 已占住 ${PORT}`)

  const real = identityOf(fakePid)
  if (!real) throw new Error('拿不到假进程身份 —— 这条对照没法做')

  // 🔴 关键点:状态文件里写一条 **pid 相同、创建时间与命令行 hash 不同** 的身份。
  //    这正是 pid 复用的形态 —— 只认 pid 的实现会在这里误杀。
  mkdirSync(join(tmpdir(), 'xbvnext-pw'), { recursive: true })
  writeFileSync(STATE_FILE, JSON.stringify({
    ours: [{ pid: fakePid, createdAt: real.createdAt - 999_999, cmdlineHash: 'deadbeefdeadbeef', cmdline: '(伪造的本轮身份)' }],
  }), 'utf-8')

  const message = expectThrow('teardown(身份不匹配)', () => globalTeardown())
  if (!message.includes('禁杀')) {
    throw new Error(`报红了,但没说明"禁杀" → ${message}`)
  }

  await sleep(500)
  const still = listenersOn(PORT)
  if (!still.ok) throw new Error(`复核探测失败:${still.reason}`)
  if (!still.pids.includes(fakePid)) {
    throw new Error(`🔴 假进程被杀了(PID ${fakePid} 已不在端口上)—— 身份判据没起作用`)
  }
  console.log(`  ✅ 假进程仍在(PID ${fakePid}),没被误杀`)

  fake.kill()
  await sleep(300)
  try { rmSync(STATE_FILE, { force: true }) } catch { /* noop */ }
}

const mode = process.argv[2]
try {
  if (mode === 'probe-failclosed') probeFailClosed()
  else if (mode === 'foreign-no-kill') await foreignNoKill()
  else throw new Error(`未知模式 ${mode};可用:probe-failclosed | foreign-no-kill`)
  console.log('反向对照通过')
} catch (exc) {
  console.error('反向对照失败:', (exc as Error).message)
  process.exit(1)
}
