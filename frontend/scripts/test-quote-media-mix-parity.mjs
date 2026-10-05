#!/usr/bin/env node
/**
 * 跨语言一致性门禁 · 投放组合拆分（WO_QUOTE_MEDIA_MIX_DYNAMIC_2026-08-12 v2 §4.3）
 *
 * 工单原文:「Python 与 TypeScript 必须共享 round_half_up 夹具,禁止一端 banker rounding、
 * 一端 Math.round 造成漂移。」这条门禁就是那个夹具的执行体:
 *
 *   同一份 fixture（tests/quote_media_mix_2026_08_12/fixture_v2.csv）
 *     → 喂 TS 的 planMediaMix
 *     → 与 fixture 的 expected_anchor/coverage/douyin 对拍
 *
 * Python 侧由 `tests/quote_media_mix_2026_08_12/test_quote_media_mix.py` 用**同一份 fixture**
 * 对拍。两端各自对上同一组期望值 = 两端彼此一致,不需要跨进程互调。
 *
 * 🔴 为什么不是"跑一遍看看没报错":每条断言都配一条**必失败**的反向对照,
 *    证明这套判据真的能分辨对错(否则公式改坏了它也照样绿)。
 *
 * 跑法:node scripts/test-quote-media-mix-parity.mjs
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { pathToFileURL } from 'node:url'

const ROOT = process.cwd()
const FIXTURE = path.join(ROOT, '..', 'tests', 'quote_media_mix_2026_08_12', 'fixture_v2.csv')

let pass = 0
let fail = 0
const ok = (msg) => { console.log(`  [ OK ] ${msg}`); pass++ }
const bad = (msg) => { console.log(`  [FAIL] ${msg}`); fail++ }
const check = (cond, msg) => (cond ? ok(msg) : bad(msg))

/** 把 TS 源码转成可 import 的 ESM（只做类型擦除,不引 tsc 依赖） */
function loadTsModule(relPath) {
  const src = fs.readFileSync(path.join(ROOT, relPath), 'utf8')
  const js = src
    .replace(/^\s*export\s+interface[\s\S]*?\n\}\n/gm, '')       // 去掉 interface 块
    .replace(/:\s*MediaMixRatio\b/g, '')
    .replace(/:\s*MediaMix\b/g, '')
    .replace(/(\)|\w)\s*:\s*(number|string|boolean)\b/g, '$1')   // 去参数/返回类型注解
    .replace(/\bexport const (\w+)\s*=\s*/g, 'export const $1 = ')
  const tmp = path.join(ROOT, 'node_modules', '.cache', 'quote-media-mix-parity.mjs')
  fs.mkdirSync(path.dirname(tmp), { recursive: true })
  fs.writeFileSync(tmp, js, 'utf8')
  return import(pathToFileURL(tmp).href)
}

function readFixture() {
  const text = fs.readFileSync(FIXTURE, 'utf8').replace(/^﻿/, '')
  const [head, ...lines] = text.trim().split(/\r?\n/)
  const cols = head.split(',')
  return lines.map(line => {
    const cells = line.split(',')
    return Object.fromEntries(cols.map((c, i) => [c, cells[i]]))
  })
}

const { planMediaMix, roundHalfUp, clampRatio, RATIO_CLAMP_MIN, RATIO_CLAMP_MAX } =
  await loadTsModule('src/lib/quoteMediaMix.ts')

console.log('── 1. round_half_up 语义(不能是 banker rounding) ──')
check(roundHalfUp(2.5) === 3, 'roundHalfUp(2.5) === 3(Python 内置 round 给 2,所以两端都不能用它)')
check(roundHalfUp(4.5) === 5, 'roundHalfUp(4.5) === 5')
check(roundHalfUp(-2.5) === -3, 'roundHalfUp(-2.5) === -3(Math.round 给 -2,这里必须 half-up)')
check(Math.round(-2.5) === -2, '【反向对照】Math.round(-2.5) 确实 === -2 → 上一条不是恒真')

console.log('\n── 2. 六场景:从 fixture 计数跑公式,对拍 fixture 期望 ──')
const rows = readFixture()
check(rows.length === 6, `fixture 六行(实得 ${rows.length})`)
for (const r of rows) {
  const anchorN = Number(r.authoritative_n) + Number(r.portal_n)
  const coverageN = Number(r.vertical_n) + Number(r.self_media_n) - Number(r.douyin_n)
  // 口径自检:抖音是子集,不是第六层
  check(coverageN === Number(r.coverage_n_excluding_douyin),
        `${r.scenario}: 覆盖数(已扣抖音)与 fixture 一致`)
  const ratioUsed = clampRatio(coverageN / anchorN)
  const mix = planMediaMix(Number(r.capacity_total), ratioUsed, Number(r.douyin_share_used))
  const got = `${mix.focusMediaAnchor}/${mix.industryPlatformCoverage}/${mix.douyinDoubaoOnly}`
  const want = `${r.expected_anchor}/${r.expected_coverage}/${r.expected_douyin}`
  check(got === want, `${r.scenario}: TS 算出 ${got},fixture 期望 ${want}`)
  check(mix.focusMediaAnchor + mix.industryPlatformCoverage + mix.douyinDoubaoOnly
        === Number(r.capacity_total), `${r.scenario}: 三类之和 === 交付额度`)
}

console.log('\n── 3. clamp 真的会夹(六场景一次都没触发,必须构造) ──')
check(clampRatio(9.9) === RATIO_CLAMP_MAX, `上界:clampRatio(9.9) === ${RATIO_CLAMP_MAX}`)
check(clampRatio(0.2) === RATIO_CLAMP_MIN, `下界:clampRatio(0.2) === ${RATIO_CLAMP_MIN}`)
check(clampRatio(2.5) === 2.5, '【反向对照】区间内的值不被改动 → clamp 不是恒定返回边界')

console.log('\n── 4. 不改总额度(任意容量三类之和恒等) ──')
for (const total of [0, 1, 2, 3, 5, 7, 12, 21, 56, 100]) {
  const m = planMediaMix(total, clampRatio(2.1154429789), 0)
  check(m.focusMediaAnchor + m.industryPlatformCoverage + m.douyinDoubaoOnly === total,
        `容量 ${total}:三类之和 === ${total}`)
}

console.log('\n' + '='.repeat(60))
if (fail > 0) {
  console.error(`🔴 投放组合跨语言门禁失败 · ${fail} 条(通过 ${pass})`)
  process.exit(1)
}
console.log(`✅ 投放组合跨语言门禁全绿 · 判据 ${pass} 条(机器计数,不含本行)`)
