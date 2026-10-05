/**
 * 在 node 里**原样执行**生产源文件 `frontend/src/hooks/useXiaobangHandoff.ts`。
 *
 * 🔴 为什么不把拼装逻辑抄一份到 Python 判据里:
 *    同一个谓词写两处,必有一处没人验 —— 而漂的方向一定是判据那份更宽松。
 *    所以这里连**复制文件**都不做:resolve 钩子只把两个 import
 *    (`react` / `@/lib/api`)换成空壳,被测的那几十行**逐字**是线上跑的那几十行。
 *    把 `buildHandoffMessage` 里的封顶删掉,下面的判据必须当场变红。
 *
 * 两种模式:
 *   build  —— 只调 `buildHandoffMessage(payload)`,吐出拼好的 message。
 *   submit —— 调**真的** `useXiaobangHandoff().submit(payload)`,把它交给
 *             `authFetch` 的**请求体原样**吐出来。
 *             这一路才证明「拼装结果真的被送出去了」:光验 `buildHandoffMessage`
 *             只证明那个函数会算,不证明有人把它的结果放进 body ——
 *             把 `message:` 那一行改成 `payload.question`,build 模式一条都不会红。
 *
 * 用法:node handoff_message_probe.mjs <build|submit> <hook.ts 绝对路径> <payload JSON>
 * 输出:{"message": ..., "length": ..., "max": ..., "body": <submit 模式才有>}
 */
import { registerHooks } from 'node:module'
import { pathToFileURL } from 'node:url'

/** `authFetch` 收到的请求体(submit 模式下由 stub 写进来)。 */
globalThis.__probeCapturedBody = null

const STUBS = new Map([
  [
    'react',
    'export const useCallback = (f) => f;\n' +
    'export const useState = (init) => [typeof init === "function" ? init() : init, () => {}];\n',
  ],
  [
    '@/lib/api',
    'export const authFetch = async (url, init) => {\n' +
    '  globalThis.__probeCapturedBody = { url, body: JSON.parse(init.body), method: init.method };\n' +
    '  return { ok: true, json: async () => ({ id: 4242, status: "new" }) };\n' +
    '};\n',
  ],
])

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (STUBS.has(specifier)) {
      return { url: 'stub:' + specifier, shortCircuit: true }
    }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.startsWith('stub:')) {
      return {
        format: 'module',
        source: STUBS.get(url.slice('stub:'.length)),
        shortCircuit: true,
      }
    }
    return nextLoad(url, context)
  },
})

const [mode, hookPath, payloadJson] = process.argv.slice(2)
if (!mode || !hookPath || !payloadJson) {
  console.error('用法: node handoff_message_probe.mjs <build|submit> <hook.ts> <payload JSON>')
  process.exit(2)
}

const mod = await import(pathToFileURL(hookPath).href)
for (const name of ['buildHandoffMessage', 'useXiaobangHandoff']) {
  if (typeof mod[name] !== 'function') {
    console.error(`生产模块没有导出 ${name} —— 接线断了`)
    process.exit(3)
  }
}

const payload = JSON.parse(payloadJson)
const out = { max: mod.HANDOFF_MESSAGE_MAX ?? null }

if (mode === 'submit') {
  const hook = mod.useXiaobangHandoff()
  const result = await hook.submit(payload)
  const captured = globalThis.__probeCapturedBody
  if (!captured) {
    console.error('submit() 没有打过 authFetch —— 提交链断了')
    process.exit(4)
  }
  out.body = captured.body
  out.url = captured.url
  out.method = captured.method
  out.result = result
  out.message = captured.body.message
} else {
  out.message = mod.buildHandoffMessage(payload)
}

out.length = out.message.length
process.stdout.write(JSON.stringify(out))
