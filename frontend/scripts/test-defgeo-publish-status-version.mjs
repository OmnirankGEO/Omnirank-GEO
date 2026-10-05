/**
 * test-defgeo-publish-status-version.mjs —— UI-34 statusVersion 单调守卫的**可执行**判据。
 *
 * 仓里没有 vitest / jest,所以沿用本仓既有办法(见 test-publication-contract-logic.mjs):
 * 用 esbuild 现场把 `src/pages/DefensivePublish/statusVersionGuard.ts` 转成 JS 再 import,
 * **真的喂数据跑一遍**,不是 grep 源码。
 *
 * 🔴 判据自证:`--mutate` 会把守卫里那句 `<=` 改成 `<`(也就是「相等也接纳」)
 *    再跑一遍,**必须转红**。转不红说明这些用例没有区分力,和恒绿没区别。
 *    (本仓记过:没有自证的锁与恒绿无法区分。)
 */

import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { pathToFileURL } from 'node:url'
import esbuild from 'esbuild'

const root = process.cwd()
const SRC = path.join(root, 'src/pages/DefensivePublish/statusVersionGuard.ts')
const MUTATE = process.argv.includes('--mutate')

async function loadGuard(source) {
    const outDir = path.join(root, 'node_modules/.cache/defgeo-status-version')
    fs.mkdirSync(outDir, { recursive: true })
    const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    const inFile = path.join(outDir, `guard-${stamp}.ts`)
    const outFile = path.join(outDir, `guard-${stamp}.mjs`)
    fs.writeFileSync(inFile, source, 'utf8')
    esbuild.buildSync({
        entryPoints: [inFile], outfile: outFile,
        format: 'esm', platform: 'node', bundle: false, loader: { '.ts': 'ts' },
    })
    return import(pathToFileURL(outFile).href)
}

let failed = 0
const check = (cond, what) => {
    if (cond) { console.log(`  PASS  ${what}`) }
    else { console.error(`  FAIL  ${what}`); failed++ }
}

const CMD = 'pcmd_abc123'
const at = (v, extra = {}) => ({ publishCommandId: CMD, statusVersion: v, ...extra })

async function run(source, label) {
    console.log(`\n== ${label} ==`)
    const { acceptStatusUpdate } = await loadGuard(source)

    // ① 首份必须收
    const first = acceptStatusUpdate(null, at(1), CMD)
    check(first.verdict === 'accepted' && first.changed && first.next.statusVersion === 1,
        '首份响应被接纳')

    // ② 更高版本必须收
    const up = acceptStatusUpdate(at(3), at(4), CMD)
    check(up.verdict === 'accepted' && up.next.statusVersion === 4, '更高 statusVersion 被接纳')

    // ③ 🔴 核心:慢响应(更低版本)必须丢,且手上那份**原样保留**
    const cur = at(7, { commandState: 'completed' })
    const stale = acceptStatusUpdate(cur, at(5, { commandState: 'running' }), CMD)
    check(stale.verdict === 'stale_version', '更低 statusVersion 被判 stale_version')
    check(stale.changed === false, 'stale 时不换 state')
    check(stale.next === cur, 'stale 时引用不变(completed 没被 running 顶回去)')
    check(stale.next.commandState === 'completed', 'completed 仍然是 completed')

    // ④ 🔴 相等也丢(严格单调)
    const same = acceptStatusUpdate(at(7), at(7), CMD)
    check(same.verdict === 'stale_version' && same.changed === false, '相同 statusVersion 也被丢弃')

    // ⑤ 别的命令的响应必须丢 —— 哪怕它的版本号更高
    const other = acceptStatusUpdate(at(2), { publishCommandId: 'pcmd_other', statusVersion: 99 }, CMD)
    check(other.verdict === 'other_command' && other.changed === false,
        '另一条命令的高版本响应被丢弃')

    // ⑥ 形状不对的一律丢(不能因为「没有 current」就照单全收)
    for (const [bad, why] of [
        [null, 'null'],
        ['x', '字符串'],
        [{ statusVersion: 1 }, '缺 publishCommandId'],
        [at('3'), 'statusVersion 是字符串'],
        [at(1.5), 'statusVersion 非整数'],
        [at(-1), 'statusVersion 为负'],
        [at(Number.NaN), 'statusVersion 是 NaN'],
    ]) {
        const r = acceptStatusUpdate(null, bad, CMD)
        check(r.verdict === 'malformed' && r.changed === false, `形状不对被拒:${why}`)
    }
}

const original = fs.readFileSync(SRC, 'utf8')
await run(original, '真实守卫')

if (MUTATE) {
    /**
     * 两发变异,不是一发。
     *
     * 第一发只把「相等」这一档翻过来,它**杀不掉**核心用例③(更低版本仍然被拒)——
     * 也就是说单靠第一发被杀,证明不了③有区分力。所以必须补第二发:
     * 整条单调守卫拆掉(照单全收),③④必须一起转红。
     * (本仓记过:两把锁叠在同一条路径上时,「相关判据全绿」证明不了那一行被验过。)
     */
    const MUTANTS = [
        {
            name: '① <= 改成 <(相等也接纳)',
            from: 'candidate.statusVersion <= current.statusVersion',
            to: 'candidate.statusVersion < current.statusVersion',
            mustKill: ['相同 statusVersion 也被丢弃'],
        },
        {
            name: '② 整条单调守卫拆掉(照单全收)',
            from: 'if (current !== null && candidate.statusVersion <= current.statusVersion) {',
            to: 'if (false) {',
            mustKill: ['更低 statusVersion 被判 stale_version', '相同 statusVersion 也被丢弃'],
        },
    ]
    let allKilled = true
    for (const m of MUTANTS) {
        const mutated = original.split(m.from).join(m.to)
        if (mutated === original) {
            console.error(`\n变异 ${m.name} 没有命中目标行 —— 判据自证失败(锚点漂了)`)
            process.exit(3)
        }
        const before = failed
        await run(mutated, `变异版 ${m.name}`)
        const killed = failed > before
        console.log(`变异自证 ${m.name}:${killed ? 'PASS · 被杀' : 'FAIL · 存活 ⇒ 无区分力'}`)
        if (!killed) allKilled = false
    }
    console.log(`\n变异总判:${allKilled ? 'PASS · 两发全被杀' : 'FAIL · 有变异存活'}`)
    process.exit(allKilled ? 0 : 4)
}

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 条失败`)
process.exit(failed === 0 ? 0 : 1)
