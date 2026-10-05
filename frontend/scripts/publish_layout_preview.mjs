/**
 * publish_layout_preview.mjs — 给人看的本地预览(不是门禁,不进 build 链)。
 *
 * 一页里放「改前 / 改后」两版,顶上可切换。用的是真 CSS(dist/assets/index-*.css)
 * 和真组件:改后 = ArticleStatusFilter / MediaAdviceDrawer 本体;
 * 改前 = `git show <底>:…ArticleGroupSection.tsx` 取回来的生产那一版。
 * 改后那一版是**可交互的**:点分段控件换列表、点把手开抽屉、拖分隔条改高度。
 *
 * 跑法:node scripts/publish_layout_preview.mjs   (先 npm run build)
 * 端口固定 5273,方便直接开 http://127.0.0.1:5273/
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import http from 'node:http'
import { execFileSync } from 'node:child_process'
import esbuild from 'esbuild'

const root = process.cwd()
const srcDir = path.join(root, 'src')
const cache = path.join(root, 'node_modules/.cache/publish-layout-preview')
fs.mkdirSync(cache, { recursive: true })
const PORT = Number(process.env.PORT || 5273)
const BASE_SHA = process.env.BASE_SHA || 'e16ba335f35af0b2b1e8d007770de2e91216695f'

// 取回"改前"那一版组件(本包里已删,只有 git 里还有)
const beforeGroup = path.join(cache, 'ArticleGroupSectionBefore.tsx')
fs.writeFileSync(beforeGroup, execFileSync('git',
  ['show', `${BASE_SHA}:frontend/src/components/publishing/ArticleGroupSection.tsx`],
  { cwd: path.join(root, '..'), maxBuffer: 1 << 20 }))

const entry = path.join(cache, 'entry.tsx')
fs.writeFileSync(entry, `
import { useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArticleStatusFilter } from '@/components/publishing/ArticleStatusFilter';
import { MediaAdviceDrawer } from '@/components/publishing/MediaAdviceDrawer';
import { ArticleGroupSection } from ${JSON.stringify(beforeGroup.replace(/\\\\/g, '/'))};

// 老板截图里的真实分布
const N = { unpublished: 46, inProgress: 0, published: 13, rejected: 0 };
const mk = (p, n) => Array.from({length: n}, (_, i) => ({ id: p + i, title: '示例文章标题 ' + (i+1) + ' · 用于看行高', kw: '示例关键词' }));
const DATA = {
  unpublished: mk('U', N.unpublished), inProgress: mk('P', N.inProgress),
  published: mk('D', N.published), rejected: mk('R', N.rejected),
};

function Row({ a, dim }) {
  return (
    <div data-row={a.id} className={'flex items-start gap-2 px-2 py-2 rounded-md cursor-pointer transition-colors hover:bg-secondary/50 ' + (dim ? 'opacity-60' : '')}>
      <div className="flex items-center justify-center shrink-0 -my-2 -ml-2 px-3 py-2 rounded-l-md">
        <div className="size-4 rounded border border-border" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-sm leading-snug line-clamp-2 break-words">{a.title}</div>
        <div className="flex gap-1.5 mt-1"><span className="text-[10px] text-muted-foreground">{a.kw}</span></div>
      </div>
    </div>
  );
}

/** 左栏公共外框(两版共用,保证只有内部结构不同) */
function LeftShell({ children }) {
  return (
    <div className="w-full min-w-0 shrink-0 rounded-lg border border-border md:rounded-none md:border-y-0 md:border-l-0 md:border-r flex flex-1 flex-col overflow-hidden md:max-h-full">
      <div className="p-3 border-b border-border space-y-2">
        <select className="w-full px-3 py-1.5 rounded-md border border-border bg-background text-sm">
          <option>全域上榜（深圳）科技有限公司 (46 篇)</option>
        </select>
      </div>
      {children}
      <div className="p-3 border-t border-border text-xs text-muted-foreground shrink-0">共 46 篇可选</div>
    </div>
  );
}

const AuxBlocks = () => (<>
  <div className="rounded-md border border-border px-3 py-2 space-y-1">
    <div className="w-full text-xs font-medium text-foreground flex items-center justify-between">
      <span>AI 最常引用的网站</span><span className="text-muted-foreground">▾</span>
    </div>
  </div>
  <div className="rounded-md border border-border px-3 py-2 space-y-1">
    <div className="w-full text-xs font-medium text-foreground flex items-center justify-between">
      <span>这篇建议怎么搭配媒体</span><span className="text-muted-foreground">▾</span>
    </div>
  </div>
  <div className="rounded-md border border-border px-3 py-2">
    <div className="text-xs font-medium text-foreground mb-1">🏆 本行业 AI 真实引用媒体榜</div>
    {['投资家首发 · 有效分 82', '周口网 · 有效分 82', 'IT之家 · 有效分 65', '界面新闻 · 有效分 65', '搜狐网 · 有效分 61'].map((t, i) => (
      <div key={i} className="rounded border border-border/60 px-2 py-2 mb-1">
        <div className="text-xs text-foreground">{i+1}. {t}</div>
        <div className="text-[10px] text-muted-foreground mt-0.5">答案采纳 28 次 · 被 AI 明确引用 2 次 · 覆盖 3 个引擎</div>
      </div>
    ))}
  </div>
</>);

/** 改前:四段堆叠 + 辅助面板在流里(filledStyle 逐字照抄生产那一版) */
function Before() {
  const [collapsed, setCollapsed] = useState({ unpublished: false, inProgress: false, published: true, rejected: true });
  const t = k => setCollapsed(p => ({ ...p, [k]: !p[k] }));
  return (
    <LeftShell>
      <div className="flex flex-col shrink-0" style={collapsed.unpublished ? undefined : { flex: '3 1 0%', minHeight: '120px' }}>
        <button onClick={() => t('unpublished')} className="w-full px-3 py-1.5 text-[10px] font-medium text-muted-foreground tracking-wider border-b border-border hover:bg-secondary/50 transition-colors flex items-center justify-between shrink-0">
          <span>未分发 ({N.unpublished})</span><span>{collapsed.unpublished ? '▾' : '▴'}</span>
        </button>
        {!collapsed.unpublished && (
          <div data-list-scroll="" className="flex-1 overflow-y-auto p-2 space-y-1">
            {DATA.unpublished.map(a => <Row key={a.id} a={a} />)}
          </div>
        )}
      </div>
      <ArticleGroupSection groupKey="inProgress" label="发布中" count={N.inProgress} open={!collapsed.inProgress}
        onToggle={() => t('inProgress')} tone="progress" emptyText="本项目暂无发布中的文章"
        filledStyle={{ flex: '2 1 0%', minHeight: '180px' }} />
      <ArticleGroupSection groupKey="published" label="已分发" count={N.published} open={!collapsed.published}
        onToggle={() => t('published')} emptyText="本项目暂无已分发文章"
        filledStyle={{ flex: '2 1 0%', minHeight: '180px' }}>
        {DATA.published.map(a => <Row key={a.id} a={a} dim />)}
      </ArticleGroupSection>
      <ArticleGroupSection groupKey="rejected" label="已拒稿" count={N.rejected} open={!collapsed.rejected}
        onToggle={() => t('rejected')} tone="danger" emptyText="本项目暂无已拒稿文章"
        filledStyle={{ flex: '2 1 0%', minHeight: '180px' }} />
      <div className="flex min-h-0 shrink-0 flex-col gap-2"><AuxBlocks /></div>
    </LeftShell>
  );
}

/** 改后:分段筛选器 + 单列表 + 底部抽屉(可拖) */
function After() {
  const [active, setActive] = useState('unpublished');
  const [open, setOpen] = useState(false);
  const leftPaneRef = useRef(null);
  const rows = DATA[active];
  const EMPTY = { unpublished: '全部已分发', inProgress: '本项目暂无发布中的文章', published: '本项目暂无已分发文章', rejected: '本项目暂无已拒稿文章' };
  return (
    <div ref={leftPaneRef} className="flex min-h-0 w-full flex-1 flex-col overflow-hidden">
      <LeftShell>
        <ArticleStatusFilter className="mx-2 mt-2" active={active} onChange={setActive}
          options={[
            { key: 'unpublished', label: '未分发', count: N.unpublished },
            { key: 'inProgress', label: '发布中', count: N.inProgress, tone: 'progress' },
            { key: 'published', label: '已分发', count: N.published },
            { key: 'rejected', label: '已拒稿', count: N.rejected, tone: 'danger' },
          ]} />
        <div data-list-scroll="" data-article-list-pane="" data-active-status={active}
             className="flex min-h-0 flex-1 flex-col overflow-y-auto p-2 space-y-1">
          {rows.length === 0
            ? <div className="text-center text-xs text-muted-foreground py-4">{EMPTY[active]}</div>
            : rows.map(a => <Row key={a.id} a={a} dim={active === 'published'} />)}
        </div>
        <MediaAdviceDrawer open={open} onToggle={() => setOpen(v => !v)}
          storageKey="publish_center_media_advice_h_v1:preview" boundsRef={leftPaneRef}
          sections={[{ key: 'aux', node: <AuxBlocks /> }]} />
      </LeftShell>
    </div>
  );
}

function App() {
  const [variant, setVariant] = useState('after');
  const isAfter = variant === 'after';
  return (
    <div className="flex h-[100dvh] w-full bg-background">
      <aside className="hidden w-64 shrink-0 border-r border-border bg-card md:block">
        <div className="p-4 text-sm font-semibold">全域上榜</div>
        <div className="px-4 py-2 text-xs text-muted-foreground">（侧边栏占位）</div>
      </aside>
      {/* 外壳:改后走定高分支,改前走 overflow-y-auto 分支 —— 与 Layout.tsx 一致 */}
      <div className={'flex flex-col flex-1 min-w-0 h-[100dvh] bg-card ' + (isAfter ? 'overflow-hidden' : 'overflow-y-auto overflow-x-hidden')}>
        <header className="bg-background/80 sticky top-0 z-20 flex h-12 shrink-0 items-center gap-3 border-b px-4">
          <span className="text-sm font-medium">发布中心</span>
          <div className="ml-auto flex items-center gap-1 rounded-md border border-border bg-secondary/40 p-0.5">
            {[['before', '改前(线上现状)'], ['after', '改后(本包)']].map(([v, label]) => (
              <button key={v} onClick={() => setVariant(v)}
                className={'rounded px-3 py-1 text-xs font-medium transition-colors ' + (variant === v ? 'bg-background shadow-sm text-foreground' : 'text-muted-foreground hover:text-foreground')}>
                {label}
              </button>
            ))}
          </div>
        </header>
        <main className={isAfter ? 'flex-1 min-h-0' : 'flex-1'}>
          <div className={isAfter
            ? 'flex h-full min-h-0 max-w-full flex-col overflow-hidden p-2 sm:p-4 md:p-6'
            : 'flex min-h-[calc(100dvh-7rem)] max-w-full flex-col overflow-x-hidden p-2 pb-24 sm:p-4 md:h-[calc(100dvh-7rem)] md:min-h-0 md:overflow-hidden md:p-6 md:pb-6'}>
            <div className="flex min-h-0 w-full min-w-0 flex-1 flex-col gap-3 overflow-visible md:flex-row md:gap-0 md:overflow-hidden">
              {isAfter ? <After /> : <Before />}
              <div className="hidden min-h-0 min-w-0 flex-1 flex-col overflow-hidden md:flex">
                <div className="px-4 py-2 border-b border-border text-xs text-muted-foreground">右栏 · 媒体列表（本次一个字没动）</div>
                <div className="flex-1 overflow-y-auto p-4 space-y-2">
                  {Array.from({length: 24}, (_, i) => (
                    <div key={i} className="flex items-center gap-3 rounded border border-border/60 px-3 py-2 text-xs">
                      <span className="text-foreground">凤凰网{['区域','商业','财经','科技'][i % 4]}</span>
                      <span className="ml-auto text-muted-foreground">{(21450 + i * 1200).toLocaleString()} 算力</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
            <div className="sticky bottom-0 z-30 shrink-0 border-t border-border bg-card/95 px-4 py-2.5 text-sm backdrop-blur">
              🛒 3 篇文章 · 6 个媒体 · <span className="text-primary font-medium">128,400 算力</span>
              <span className="float-right text-xs text-muted-foreground">（底部购物车条 · 抽屉展开也盖不住它）</span>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
createRoot(document.getElementById('root')).render(<App />);
`)

await esbuild.build({
  entryPoints: [entry], outfile: path.join(cache, 'app.js'),
  bundle: true, format: 'iife', platform: 'browser', jsx: 'automatic',
  alias: { '@': srcDir }, logLevel: 'silent',
  define: { 'process.env.NODE_ENV': '"production"' },
})

const cssFile = fs.readdirSync(path.join(root, 'dist/assets')).find(f => /^index-.*\.css$/.test(f))
const HTML = `<!doctype html><html class="dark"><head><meta charset="utf-8">
<title>发布中心布局 · 改前/改后对比</title>
<link rel="stylesheet" href="/${cssFile}"></head>
<body class="bg-background"><div id="root"></div><script src="/app.js"></script></body></html>`

http.createServer((req, res) => {
  const p = new URL(req.url, 'http://127.0.0.1').pathname
  if (p === '/app.js') { res.writeHead(200, { 'Content-Type': 'text/javascript' }); return res.end(fs.readFileSync(path.join(cache, 'app.js'))) }
  const asset = path.join(root, 'dist/assets', path.basename(p))
  if (p.endsWith('.css') && fs.existsSync(asset)) { res.writeHead(200, { 'Content-Type': 'text/css' }); return res.end(fs.readFileSync(asset)) }
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(HTML)
}).listen(PORT, '127.0.0.1', () => {
  console.log(`预览已起:http://127.0.0.1:${PORT}/   (顶栏右上角切「改前 / 改后」· Ctrl+C 停)`)
})
