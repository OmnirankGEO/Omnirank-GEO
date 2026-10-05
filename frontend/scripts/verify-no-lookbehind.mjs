import {readdirSync, readFileSync, statSync, existsSync} from 'node:fs'
import { unevaluated } from './_verify_exit.mjs'
import {join} from 'node:path'

const assetsDir = join(process.cwd(), 'dist', 'assets')
// 🔴 [#95] 没有产物可扫 = **未评估**,不是失败。
//    原来直接 readdirSync 抛 ENOENT 后 exit 1 ⇒ 与「真的扫到了 lookbehind」同码,
//    而两者处置相反(去构建 vs 改代码)。
if (!existsSync(assetsDir)) {
  unevaluated([`${assetsDir} 不存在 —— 先 \`npm run build\` 生成 dist/assets 再跑本闸`],
              'dist assets 里不许有 regex lookbehind')
}
const forbidden = ['(?<=', '(?<!']
const failures = []

function walk(dir) {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry)
    const stat = statSync(path)

    if (stat.isDirectory()) {
      walk(path)
      continue
    }

    if (!path.endsWith('.js')) continue

    const source = readFileSync(path, 'utf8')

    for (const token of forbidden) {
      const index = source.indexOf(token)

      if (index !== -1) {
        const start = Math.max(0, index - 80)
        const end = Math.min(source.length, index + 120)
        failures.push({
          path,
          token,
          snippet: source.slice(start, end).replace(/\s+/g, ' ')
        })
      }
    }
  }
}

try {
  walk(assetsDir)
} catch (error) {
  console.error(`[no-lookbehind] Unable to scan ${assetsDir}`)
  console.error(error instanceof Error ? error.message : String(error))
  process.exit(1)
}

if (failures.length > 0) {
  console.error('[no-lookbehind] Legacy iOS/WeChat incompatible regex found.')
  for (const failure of failures) {
    console.error(`- ${failure.path}: ${failure.token}`)
    console.error(`  ${failure.snippet}`)
  }
  process.exit(1)
}

console.log('[no-lookbehind] OK: dist assets contain no regex lookbehind.')
