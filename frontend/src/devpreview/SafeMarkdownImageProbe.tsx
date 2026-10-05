/**
 * 真渲染取证入口 —— 返修单 REWORK_T4_SAFEMARKDOWN_IMAGE_2026-07-29 §5。
 *
 * 🔴 这里挂载的是**真的** `SafeMarkdown` 组件（写作大厅预览用的同一个），
 * 不是复刻一份 HTML。上一轮 T4 的 Playwright 锁就是栽在"取证页不是用户
 * 实际看到的那条渲染路径"：锁在另一条路上绿着，`SafeMarkdown` 这条路上
 * 图片从来就渲染不出来。
 *
 * 页面只在本地取证时构建（`vite.safeimage-probe.config.ts`），不进生产包。
 */
import { createRoot } from 'react-dom/client'

import { SafeMarkdown } from '@/components/SafeMarkdown'
import { OWNED_IMAGE_TEST_VECTORS, isOwnedImageUrl } from '@/lib/ownedImagePolicy'

/** 生产真实资产路径（brand 662 / asset 217）——取证页本地也放了同一份字节。 */
const REAL_ASSET_PATH = '/uploads/article-images/662/5d3c0f9e8b7a41c2a6e4f1b2c3d4e5f6_safe.jpg'

// 与生产同形态：文章正文里的自有图库图片是 markdown 图片语法 + 斜体 caption。
const ARTICLE_MARKDOWN = [
  '# 深圳全屋定制哪家靠谱？多品牌同字段对比与选择建议',
  '',
  '## 客户品牌概况',
  '',
  // 逐字用生产真实资产：brand 662 / asset 217 / alt_text 与 caption 均取自 DB。
  '![深红色背景上的白色QZQZ品牌Logo，下方标注美学定制及具体产品类型](/uploads/article-images/662/5d3c0f9e8b7a41c2a6e4f1b2c3d4e5f6_safe.jpg)',
  '*QZQZ美学定制品牌标识，涵盖橱柜、衣柜、酒柜、墙板和隐形门等全屋定制业务*',
  '',
  '正文段落。',
  '',
  '## 外部图片（必须仍是文字占位）',
  '',
  '![跟踪像素](https://evil.example/px.gif)',
  '',
  '![内联数据图](data:image/gif;base64,R0lGODlhAQABAAAAACw=)',
  '',
  '![协议相对](//evil.example/uploads/article-images/662/x.jpg)',
  '',
  '![非图库目录](/uploads/avatars/1/a.png)',
  '',
].join('\n')

function Probe() {
  return (
    <div style={{ maxWidth: 760, margin: '24px auto', padding: '0 16px', fontFamily: 'system-ui' }}>
      <h2 data-probe="heading">SafeMarkdown 真渲染取证</h2>
      <div data-probe="article">
        <SafeMarkdown>{ARTICLE_MARKDOWN}</SafeMarkdown>
      </div>
      <hr />
      <h3>共享向量逐条渲染（与后端读同一份 JSON）</h3>
      {/*
        向量分两层核，别混：
          · data-vector-json  = JSON 里写的判定（生产 origin 口径，与后端逐条比对）
          · data-vector-here  = 本页 origin 下的判定（决定这里该不该渲染出 <img>）
        绝对 URL 向量在 localhost 取证页上本就是跨源 → 这里判 false 是**正确行为**，
        不是漂移。首轮取证把这两层混成一个断言，报了 4 个假 mismatch。
        naturalWidth 只对"文件真存在"的那条有意义，另立 data-file-exists 标注。
      */}
      {OWNED_IMAGE_TEST_VECTORS.map((vector, index) => (
        <div
          key={index}
          data-vector-index={index}
          data-vector-json={String(vector.owned)}
          data-vector-here={String(isOwnedImageUrl(vector.url, window.location.origin))}
          data-file-exists={String(vector.url === REAL_ASSET_PATH)}
        >
          <code style={{ fontSize: 11 }}>{vector.url || '(空串)'}</code>
          <SafeMarkdown>{`![v${index}](${vector.url})`}</SafeMarkdown>
        </div>
      ))}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Probe />)
