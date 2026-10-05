// [2026-05-18 帮助中心 Layer 3] 视频清单单一来源
// 帮助中心主页 / 文档子页(节点引用视频) 都从这里读
// 已录好的视频接 src(OSS 公共读) · 没 src 的卡片点击弹"制作中"
//
// [2026-05-26 P1.2-b] 视频迁阿里云 OSS · git 仓库不再放 mp4
//   - 默认 base: 深圳 bucket 直链
//   - 要走 CDN(自定义域名): 设 VITE_TUTORIAL_CDN_BASE=https://cdn.omnirank.top
//   - bucket 配置: 公共读 / CORS allowlist 含 omnirank.top + localhost:5173

const CDN_BASE =
  import.meta.env.VITE_TUTORIAL_CDN_BASE ??
  'https://omnirank-tutorials.oss-cn-shenzhen.aliyuncs.com'

/** 拼教程视频公共 URL · 默认走 OSS · 设 VITE_TUTORIAL_CDN_BASE 走 CDN */
export const tutorialUrl = (filename: string) => `${CDN_BASE}/tutorials/${filename}`
const url = tutorialUrl

export type VideoItem = {
  id: string
  title: string
  duration: string
  category: 'hero' | 'step' | 'concept'
  step?: 1 | 2 | 3 | 4
  /** 服务商专属视频不会出现在普通用户帮助主页。 */
  audience: 'all' | 'agent'
  /** 视频公共 URL · 没有则卡片点击弹"制作中" */
  src?: string
}

export const videos: VideoItem[] = [
  {
    id: 'V5',
    title: '5 分钟跑完整 4 步 · 全流程一镜到底',
    duration: '4-5 分钟',
    category: 'hero',
    audience: 'agent',
    src: url('v5-overview.mp4'),
  },
  // Step 1 · 做品牌诊断
  {
    id: 'V1a',
    title: '30 秒发起一份品牌诊断',
    duration: '1:00',
    category: 'step',
    step: 1,
    audience: 'agent',
    src: url('v1-diagnosis.mp4'),
  },
  {
    id: 'V1b',
    title: '看懂诊断报告 · 怎么拿去谈单',
    duration: '1:30',
    category: 'step',
    step: 1,
    audience: 'agent',
    src: url('v1b-diagnosis-report.mp4'),
  },
  // Step 2 · 出报价
  {
    id: 'V2a',
    title: '在线报价 · 让客户在线选档位',
    duration: '1:30',
    category: 'step',
    step: 2,
    audience: 'agent',
    src: url('v2a-online-quote.mp4'),
  },
  // 2026-05-25 删:V2b 快速录单视频已废弃 · step 2 只保留在线报价(V2a)
  // Step 3 · AI 写文章 + 发布
  {
    id: 'V3a',
    title: 'AI 写文章 · 从报价到一篇成稿',
    duration: '1:30',
    category: 'step',
    step: 3,
    audience: 'agent',
    src: url('v3a-writing.mp4'),
  },
  {
    id: 'V3b',
    title: '发布文章 · 加购物车 → 选平台 → 投放',
    duration: '1:00',
    category: 'step',
    step: 3,
    audience: 'all',
    src: url('v3b-publish.mp4'),
  },
  // 2026-09-24 删(WO_288):V3c「自助发布」教程 —— 浏览器插件自助发布已于 WO_273 整档退役,
  //   不留一条教已砍功能的教程。产物里不许再出现由 build 链 test-self-publish-retired.mjs D 段扫 dist 守。
  // Step 4 · 加监测追踪
  {
    id: 'V4',
    title: '开始监测 · 开自动监测 · 看结果发客户',
    duration: '1:30',
    category: 'step',
    step: 4,
    audience: 'agent',
    src: url('v4-monitoring.mp4'),
  },
  {
    id: 'V4b',
    title: '词没达标?选词优化、补文章重新投放',
    duration: '1:20',
    category: 'step',
    step: 4,
    audience: 'all',
    src: url('v4b-optimize.mp4'),
  },
  {
    id: 'V4c',
    title: '把效果交付给客户 · 白标链接 + 月报',
    duration: '1:00',
    category: 'step',
    step: 4,
    audience: 'agent',
    src: url('v4c-deliver.mp4'),
  },
  {
    id: 'C1',
    title: '什么是 GEO',
    duration: '1:30',
    category: 'concept',
    audience: 'all',
    src: url('c1-what-is-geo.mp4'),
  },
  {
    id: 'C2',
    title: 'GEO 评分怎么算的',
    duration: '1:00',
    category: 'concept',
    audience: 'all',
    src: url('c2-scoring.mp4'),
  },
  {
    id: 'C4',
    title: '监测多久能看效果',
    duration: '0:45',
    category: 'concept',
    audience: 'all',
    src: url('c4-monitoring-timing.mp4'),
  },
  {
    id: 'C5',
    title: '算力怎么用 · 各功能扣多少 · 怎么充值',
    duration: '1:00',
    category: 'concept',
    audience: 'all',
    src: url('c5-points.mp4'),
  },
]

export function getVideo(id: string): VideoItem | undefined {
  return videos.find((v) => v.id === id)
}
