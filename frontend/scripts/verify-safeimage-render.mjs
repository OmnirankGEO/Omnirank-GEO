/**
 * 真渲染判别锁 —— 返修单 REWORK_T4_SAFEMARKDOWN_IMAGE_2026-07-29 §5。
 *
 * 🔴 挂载的是**真的** `SafeMarkdown` 组件（写作大厅预览用的同一个），
 * 断言用 `naturalWidth`（解码后的真实像素）——不接受"DOM 里有 <img> 标签"
 * 这类断言：历史踩过 `ui/progress.tsx` 没透传 Radix 导致 aria-valuenow 恒 0，
 * **量 DOM 属性 ≠ 量渲染**。
 *
 * 用法：node scripts/verify-safeimage-render.mjs
 * 前置：本脚本自己 build 取证页、起本地静态服务、拉真实资产字节。
 */
import { execFileSync } from 'node:child_process'
import { createServer } from 'node:http'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { unevaluated } from './_verify_exit.mjs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND = path.resolve(HERE, '..')
const DIST = path.join(FRONTEND, 'dist-safeimage-probe')
const ASSET_REL = 'uploads/article-images/662/5d3c0f9e8b7a41c2a6e4f1b2c3d4e5f6_safe.jpg'
const ASSET_URL = `https://omnirank.top/${ASSET_REL}`
const PORT = Number(process.env.SAFEIMAGE_PROBE_PORT || 8793)

const failures = []
const ok = (label) => console.log(`✅ ${label}`)
const bad = (label, detail) => {
  failures.push(`${label} — ${detail}`)
  console.log(`❌ ${label} — ${detail}`)
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
}

async function ensureProbeBuilt() {
  execFileSync('npx', ['vite', 'build', '--config', 'vite.safeimage-probe.config.ts'], {
    cwd: FRONTEND,
    stdio: 'pipe',
    shell: process.platform === 'win32',
  })
  // build 会清空 outDir，资产必须在 build 之后落盘
  const assetPath = path.join(DIST, ASSET_REL)
  await mkdir(path.dirname(assetPath), { recursive: true })
  const cached = path.join(FRONTEND, '.safeimage-asset-cache.jpg')
  if (!existsSync(cached)) {
    const response = await fetch(ASSET_URL)
    if (!response.ok) throw new Error(`拉取真实资产失败：HTTP ${response.status}`)
    await writeFile(cached, Buffer.from(await response.arrayBuffer()))
  }
  await writeFile(assetPath, await readFile(cached))
}

function serve() {
  const server = createServer(async (req, res) => {
    const rel = decodeURIComponent((req.url || '/').split('?')[0]).replace(/^\/+/, '') || 'safeimage-probe.html'
    const file = path.join(DIST, rel)
    if (!file.startsWith(DIST)) {
      res.writeHead(403).end()
      return
    }
    try {
      const body = await readFile(file)
      res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' }).end(body)
    } catch {
      res.writeHead(404).end('not found')
    }
  })
  // 🔴 [#95] 起不了自己的探针服务 = **未评估**(exit 3),不是失败(exit 1)。
  //    原来 listen 失败会走 Node 的 'error' 事件 ⇒ **未捕获异常**崩退,
  //    报文是一坨栈,退出码与「真的渲染出问题」一模一样,而处置相反
  //    (腾端口 / 换 SAFEIMAGE_PROBE_PORT  vs  改组件)。
  //    实测就撞到过:EADDRINUSE 127.0.0.1:8793(上一轮残留或别的窗口占着)。
  return new Promise((resolve) => {
    server.once('error', (e) => {
      unevaluated(
        [`探针服务起不来(${e && e.code ? e.code : e})@127.0.0.1:${PORT} —— ` +
         `腾出该端口,或设 SAFEIMAGE_PROBE_PORT=<其它端口> 重跑`],
        'SafeImage 渲染探针',
      )
    })
    server.listen(PORT, '127.0.0.1', () => resolve(server))
  })
}

async function main() {
  await ensureProbeBuilt()
  const server = await serve()
  const { chromium } = await import('playwright')
  const browser = await chromium.launch()
  try {
    const page = await browser.newPage()
    // 断掉一切对外网络：外部图若真发了请求，这里会暴露成 request 事件。
    const externalRequests = []
    page.on('request', (r) => {
      const url = r.url()
      if (!url.startsWith(`http://127.0.0.1:${PORT}`) && !url.startsWith('data:')) {
        externalRequests.push(url)
      }
    })
    await page.goto(`http://127.0.0.1:${PORT}/safeimage-probe.html`, { waitUntil: 'networkidle' })

    const probe = await page.evaluate(() => {
      const article = document.querySelector('[data-probe="article"]')
      const imgs = [...article.querySelectorAll('img')]
      const rows = [...document.querySelectorAll('[data-vector-index]')].map((el) => {
        const img = el.querySelector('img')
        return {
          idx: Number(el.dataset.vectorIndex),
          here: el.dataset.vectorHere === 'true',
          renderedImg: !!img,
        }
      })
      return {
        ownedImgs: imgs.map((i) => ({
          src: i.getAttribute('src'),
          naturalWidth: i.naturalWidth,
          naturalHeight: i.naturalHeight,
        })),
        placeholders: [...article.querySelectorAll('.safe-markdown-img-placeholder')].map((p) => p.textContent),
        rows,
        forbiddenImgs: document.querySelectorAll(
          'img[src*="evil.example"], img[src^="data:"], img[src^="blob:"], img[src^="//"]',
        ).length,
      }
    })

    // 锁 1：自有图库资产渲染为真实 <img> 且 naturalWidth > 0
    const owned = probe.ownedImgs.filter((i) => (i.src || '').includes('/uploads/article-images/662/'))
    if (owned.length === 1 && owned[0].naturalWidth > 0 && owned[0].naturalHeight > 0) {
      ok(`锁1 自有图库图真渲染：naturalWidth=${owned[0].naturalWidth} naturalHeight=${owned[0].naturalHeight}`)
    } else {
      bad('锁1 自有图库图真渲染', `owned=${JSON.stringify(probe.ownedImgs)}`)
    }

    // 锁 2/3：外部域名 / data: / blob: / 协议相对 仍是文字占位
    if (probe.placeholders.length === 4 && probe.placeholders.every((t) => t.includes('外部图片不加载'))) {
      ok(`锁2/3 外部图维持文字占位：${probe.placeholders.length} 处`)
    } else {
      bad('锁2/3 外部图维持文字占位', JSON.stringify(probe.placeholders))
    }
    if (probe.forbiddenImgs === 0) ok('锁2/3 DOM 里零个外部/data:/blob: <img>')
    else bad('锁2/3 DOM 里零个外部 <img>', `实际 ${probe.forbiddenImgs} 个`)

    if (externalRequests.length === 0) ok('锁2/3 零对外网络请求（跟踪像素没被打出去）')
    else bad('锁2/3 零对外网络请求', JSON.stringify(externalRequests.slice(0, 5)))

    // 反漂移：渲染结果必须逐条等于策略判定（20 条共享向量）
    const drift = probe.rows.filter((r) => r.here !== r.renderedImg)
    if (probe.rows.length >= 15 && drift.length === 0) {
      ok(`反漂移 渲染与策略判定逐条一致：${probe.rows.length} 条向量`)
    } else {
      bad('反漂移 渲染与策略判定一致', `向量 ${probe.rows.length} 条，不一致 ${JSON.stringify(drift)}`)
    }
  } finally {
    await browser.close()
    server.close()
  }

  if (failures.length) {
    console.error(`\n🔴 SafeMarkdown 图片真渲染锁失败 ${failures.length} 项`)
    process.exit(1)
  }
  console.log('\n✅ SafeMarkdown 图片真渲染锁全绿')
}

main().catch((error) => {
  console.error('🔴 取证脚本异常：', error)
  process.exit(1)
})
