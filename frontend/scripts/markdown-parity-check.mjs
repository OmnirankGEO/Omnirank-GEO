/**
 * markdown-parity-check.mjs — 前后端渲染一致性 fixture 的前端半
 *
 * 用 esbuild 把 SafeMarkdown + react-dom/server 打成 CJS 包，SSR 渲染
 * tests/fixtures/markdown_parity/cases.json 的每个 case，断言：
 *  - frontend_contains 标记全部命中
 *  - not_contains 标记全部缺失
 * 后端半见 tests/test_safe_markdown_renderer.py::test_frontend_backend_parity_fixtures
 *
 * 运行：node scripts/markdown-parity-check.mjs（在 frontend/ 目录下）
 */
import { createRequire } from 'node:module'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import process from 'node:process'

const require = createRequire(import.meta.url)
const { buildSync } = require('esbuild')

const frontendRoot = process.cwd()
const casesPath = path.resolve(frontendRoot, '..', 'tests', 'fixtures', 'markdown_parity', 'cases.json')
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8')).cases

const entry = `
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import SafeMarkdown from '@/components/SafeMarkdown'
export function renderSafe(text) {
  return renderToStaticMarkup(React.createElement(SafeMarkdown, null, text))
}
`

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'md-parity-'))
const entryFile = path.join(tmpDir, 'entry.tsx')
const outFile = path.join(tmpDir, 'bundle.cjs')
fs.writeFileSync(entryFile, entry)

buildSync({
  entryPoints: [entryFile],
  bundle: true,
  format: 'cjs',
  platform: 'node',
  outfile: outFile,
  alias: { '@': path.resolve(frontendRoot, 'src') },
  nodePaths: [path.resolve(frontendRoot, 'node_modules')],
  jsx: 'automatic',
  logLevel: 'silent',
})

const { renderSafe } = require(outFile)

let failed = 0
for (const testCase of cases) {
  const html = renderSafe(testCase.markdown)
  for (const marker of testCase.frontend_contains) {
    if (!html.includes(marker)) {
      console.error(`FAIL ${testCase.id}: frontend missing ${JSON.stringify(marker)}\n${html}`)
      failed += 1
    }
  }
  for (const marker of testCase.not_contains) {
    if (html.includes(marker)) {
      console.error(`FAIL ${testCase.id}: frontend leaked ${JSON.stringify(marker)}\n${html}`)
      failed += 1
    }
  }
}

fs.rmSync(tmpDir, { recursive: true, force: true })

if (failed) {
  console.error(`markdown parity(frontend): ${failed} marker failure(s)`)
  process.exit(1)
}
console.log(`markdown parity(frontend): ${cases.length} cases passed`)
