/**
 * test-publish-layout-interaction.mjs — 真浏览器里**点得动/拖得动**的行为锁。
 *
 * 前面 test-publish-center-layout.mjs 是静态渲染断言(renderToStaticMarkup),
 * 它证明得了"渲染出什么",证明不了"点了会怎样、拖了会怎样"。
 * 而老板这次的原话就是「让用户可以拖动，以及收缩，这样他可以自由变化」——
 * 拖动这件事只能在真浏览器里、用真指针事件证。
 *
 *   交互1 点分段控件 → 下面的列表真的换成那个状态的内容
 *   交互2 抽屉把手 → 点开/收起,收起时内容体真的消失
 *   交互3 拖分隔条 → 抽屉高度真的变,且列表相应变矮(两者分同一份高度)
 *   交互4 拖完写进 localStorage;重新挂载后**读回上次的高度**(工单 L4 硬要求)
 *   交互5 拖过头 → 被 clamp 住,列表不会被拖没,也不会出现负高度
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import http from 'node:http'
import esbuild from 'esbuild'
import { chromium } from 'playwright'

const root = process.cwd()
const srcDir = path.join(root, 'src')
const cache = path.join(root, 'node_modules/.cache/publish-layout-interaction')
fs.mkdirSync(cache, { recursive: true })

const STORAGE_KEY = 'publish_center_media_advice_h_v1:113'

// 客户端入口:把两个真组件挂进一个最小的左栏骨架里(状态接线照抄 PublishCenter 的形状)
const entry = path.join(cache, 'entry.tsx')
fs.writeFileSync(entry, `
import { useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArticleStatusFilter } from '@/components/publishing/ArticleStatusFilter';
import { MediaAdviceDrawer } from '@/components/publishing/MediaAdviceDrawer';

const DATA = {
  unpublished: Array.from({length: 46}, (_, i) => 'U' + i),
  inProgress: Array.from({length: 5}, (_, i) => 'P' + i),
  published: Array.from({length: 13}, (_, i) => 'D' + i),
  rejected: Array.from({length: 3}, (_, i) => 'R' + i),
};

function App() {
  const [active, setActive] = useState('unpublished');
  const [open, setOpen] = useState(false);
  const leftPaneRef = useRef(null);
  const rows = DATA[active];
  return (
    <div className="flex h-[100dvh] flex-col">
      <div ref={leftPaneRef} data-left-pane="" className="flex min-h-0 w-80 flex-1 flex-col overflow-hidden border-r border-border">
        <ArticleStatusFilter
          className="mx-2 mt-2"
          active={active}
          onChange={setActive}
          options={[
            { key: 'unpublished', label: '未分发', count: DATA.unpublished.length },
            { key: 'inProgress', label: '发布中', count: DATA.inProgress.length, tone: 'progress' },
            { key: 'published', label: '已分发', count: DATA.published.length },
            { key: 'rejected', label: '已拒稿', count: DATA.rejected.length, tone: 'danger' },
          ]}
        />
        <div data-article-list-pane="" data-active-status={active}
             className="flex min-h-0 flex-1 flex-col overflow-y-auto p-2 space-y-1">
          {rows.map(r => <div key={r} data-row={r} className="px-2 py-2 text-sm">{r}</div>)}
        </div>
        <MediaAdviceDrawer
          open={open}
          onToggle={() => setOpen(v => !v)}
          storageKey=${JSON.stringify(STORAGE_KEY)}
          boundsRef={leftPaneRef}
          sections={[{ key: 'aux', node: <div data-aux="" style={{height: 430}} /> }]}
        />
      </div>
    </div>
  );
}
createRoot(document.getElementById('root')).render(<App />);
`)

await esbuild.build({
  entryPoints: [entry],
  outfile: path.join(cache, 'app.js'),
  bundle: true, format: 'iife', platform: 'browser', jsx: 'automatic',
  alias: { '@': srcDir }, logLevel: 'silent',
  define: { 'process.env.NODE_ENV': '"production"' },
})

const cssFile = fs.readdirSync(path.join(root, 'dist/assets')).find(f => /^index-.*\.css$/.test(f))
const HTML = `<!doctype html><html class="dark"><head><meta charset="utf-8">
<link rel="stylesheet" href="/${cssFile}"></head>
<body class="bg-background"><div id="root"></div><script src="/app.js"></script></body></html>`

const server = http.createServer((req, res) => {
  const p = new URL(req.url, 'http://127.0.0.1').pathname
  if (p === '/' || p === '/index.html') { res.writeHead(200, {'Content-Type':'text/html; charset=utf-8'}); return res.end(HTML) }
  if (p === '/app.js') { res.writeHead(200, {'Content-Type':'text/javascript'}); return res.end(fs.readFileSync(path.join(cache, 'app.js'))) }
  const asset = path.join(root, 'dist/assets', path.basename(p))
  if (p.endsWith('.css') && fs.existsSync(asset)) { res.writeHead(200, {'Content-Type':'text/css'}); return res.end(fs.readFileSync(asset)) }
  res.writeHead(404); res.end('nope')
})
await new Promise(r => server.listen(0, '127.0.0.1', r))
const ORIGIN = `http://127.0.0.1:${server.address().port}`

let failed = 0
const ok = (cond, what) => { if (cond) console.log(`✅ ${what}`); else { console.error(`❌ ${what}`); failed++ } }
const eq = (got, want, what) => ok(got === want, `${what}${got === want ? '' : ` (got ${JSON.stringify(got)} want ${JSON.stringify(want)})`}`)

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1366, height: 768 } })
const pg = await ctx.newPage()
await pg.goto(ORIGIN + '/', { waitUntil: 'load' })
await pg.waitForSelector('[data-status-filter-root]')

// ── 交互 1:点分段控件真的换列表
{
  eq(await pg.locator('[data-article-list-pane]').getAttribute('data-active-status'), 'unpublished', '交互1 初始停在未分发')
  eq(await pg.locator('[data-row]').count(), 46, '交互1 初始渲染 46 条未分发')

  await pg.click('[data-status-filter="published"]')
  eq(await pg.locator('[data-article-list-pane]').getAttribute('data-active-status'), 'published', '交互1 点"已分发"后列表切过去了')
  eq(await pg.locator('[data-row]').count(), 13, '交互1 已分发渲染 13 条')
  ok(await pg.locator('[data-row="D0"]').count() === 1, '交互1 渲染的确实是已分发那批(D*)')
  ok(await pg.locator('[data-row="U0"]').count() === 0, '交互1 反向 未分发那批(U*)已经不在 DOM 里')

  await pg.click('[data-status-filter="rejected"]')
  eq(await pg.locator('[data-row]').count(), 3, '交互1 已拒稿渲染 3 条')
  // 四个计数在任何选中态下都还在(T2 不回退,真浏览器里再确认一次)
  eq(await pg.locator('[data-status-filter]').count(), 4, '交互1 反向 切到任何状态,四个计数按钮都还在')

  await pg.click('[data-status-filter="unpublished"]')
  eq(await pg.locator('[data-row]').count(), 46, '交互1 切回未分发,内容完整回来')
}

// ── 交互 2:抽屉把手点得动
{
  eq(await pg.locator('[data-media-advice-drawer]').getAttribute('data-drawer-open'), 'false', '交互2 默认收起')
  eq(await pg.locator('[data-drawer-body]').count(), 0, '交互2 收起时没有内容体')
  const listClosed = (await pg.locator('[data-article-list-pane]').boundingBox()).height

  await pg.click('[data-drawer-handle]')
  eq(await pg.locator('[data-media-advice-drawer]').getAttribute('data-drawer-open'), 'true', '交互2 点把手展开了')
  eq(await pg.locator('[data-drawer-body]').count(), 1, '交互2 展开后出现内容体')
  const listOpen = (await pg.locator('[data-article-list-pane]').boundingBox()).height
  ok(listOpen < listClosed, `交互2 展开抽屉后列表相应变矮(${Math.round(listClosed)} → ${Math.round(listOpen)})`)

  await pg.click('[data-drawer-handle]')
  eq(await pg.locator('[data-drawer-body]').count(), 0, '交互2 再点一次收回去')
  const listAgain = (await pg.locator('[data-article-list-pane]').boundingBox()).height
  ok(Math.abs(listAgain - listClosed) < 2, '交互2 反向 收回后列表高度回到原值(抽屉真的不再占地方)')
}

// ── 交互 3 + 4 + 5:拖 + 持久化 + clamp
{
  await pg.click('[data-drawer-handle]')  // 展开
  const before = (await pg.locator('[data-media-advice-drawer]').boundingBox()).height
  const listBefore = (await pg.locator('[data-article-list-pane]').boundingBox()).height
  const rz = await pg.locator('[data-drawer-resizer]').boundingBox()

  // 往上拖 80px = 抽屉变高 80px
  await pg.mouse.move(rz.x + rz.width / 2, rz.y + rz.height / 2)
  await pg.mouse.down()
  await pg.mouse.move(rz.x + rz.width / 2, rz.y + rz.height / 2 - 80, { steps: 8 })
  await pg.mouse.up()

  const after = (await pg.locator('[data-media-advice-drawer]').boundingBox()).height
  const listAfter = (await pg.locator('[data-article-list-pane]').boundingBox()).height
  ok(Math.abs((after - before) - 80) <= 3, `交互3 往上拖 80px,抽屉高度 +${Math.round(after - before)}px`)
  ok(listAfter < listBefore, `交互3 列表相应变矮(${Math.round(listBefore)} → ${Math.round(listAfter)})—— 两者分同一份高度`)

  // 交互 4:写进去了吗 + 重新挂载读得回来吗
  const stored = await pg.evaluate(k => localStorage.getItem(k), STORAGE_KEY)
  ok(stored !== null, `交互4 拖完写进了 localStorage(${stored})`)
  ok(Math.abs(Number(stored) - after) <= 3, '交互4 存的就是拖到的那个高度')

  await pg.reload({ waitUntil: 'load' })
  await pg.waitForSelector('[data-drawer-handle]')
  await pg.click('[data-drawer-handle]')  // 重新展开
  const restored = (await pg.locator('[data-media-advice-drawer]').boundingBox()).height
  ok(Math.abs(restored - after) <= 3,
    `交互4 刷新后展开,高度回到上次拖的位置(${Math.round(after)} → ${Math.round(restored)})· 工单:"不记 = 比没有更烦"`)
  // 反向对照:清掉存储后必须回到默认值,否则"记住了"这条判据是恒真的
  await pg.evaluate(k => localStorage.removeItem(k), STORAGE_KEY)
  await pg.reload({ waitUntil: 'load' })
  await pg.waitForSelector('[data-drawer-handle]')
  await pg.click('[data-drawer-handle]')
  const dflt = (await pg.locator('[data-media-advice-drawer]').boundingBox()).height
  ok(Math.abs(dflt - restored) > 3,
    `交互4 反向对照 清掉存储后回到默认高度 ${Math.round(dflt)}px(≠ 上次的 ${Math.round(restored)}px → 判据不是恒真)`)

  // 交互 5:往下拖到底 —— 列表不许被拖没
  const rz2 = await pg.locator('[data-drawer-resizer]').boundingBox()
  await pg.mouse.move(rz2.x + rz2.width / 2, rz2.y + rz2.height / 2)
  await pg.mouse.down()
  await pg.mouse.move(rz2.x + rz2.width / 2, rz2.y - 2000, { steps: 12 })  // 疯狂往上拖
  await pg.mouse.up()
  const drawerMax = (await pg.locator('[data-media-advice-drawer]').boundingBox()).height
  const listMin = (await pg.locator('[data-article-list-pane]').boundingBox()).height
  ok(drawerMax > 0, `交互5 拖到极限抽屉高度仍 > 0(${Math.round(drawerMax)}px,没有负高度)`)
  ok(listMin >= 100, `交互5 拖到极限列表仍留得下内容(${Math.round(listMin)}px,列表没被拖没)`)
  ok(await pg.locator('[data-row]').count() === 46, '交互5 反向 拖到极限后列表内容还在')
}

await browser.close()
server.close()
console.log(failed ? `\n🔴 ${failed} 条交互断言未通过` : '\n✅ 发布中心布局交互五条全绿(点得动 / 拖得动 / 记得住 / 夹得住)')
process.exit(failed ? 1 : 0)
