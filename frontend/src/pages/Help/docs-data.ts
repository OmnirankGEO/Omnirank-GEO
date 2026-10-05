// 帮助中心文档树。
// 顺序和命名跟随现役 AppSidebar，避免帮助文档自己发明另一套产品结构。
// 正文由 /api/help/docs/{slug} 按真实身份返回，前端 bundle 不包含受限正文。

import {
  Activity,
  BookOpen,
  BriefcaseBusiness,
  Compass,
  Contact,
  Stethoscope,
  UsersRound,
  Wallet,
  Wrench,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

export type DocAudience = 'normal_user' | 'agent' | 'l2' | 'both' | 'admin'

export type DocNode = {
  slug: string
  title: string
  videoId?: string
  audience?: DocAudience
}

export type DocCategory = {
  id: string
  title: string
  icon: LucideIcon
  isAdmin?: boolean
  audience?: DocAudience
  nodes: DocNode[]
}

export type HelpViewer = {
  isAdmin: boolean
  isAgent: boolean
  isL2: boolean
  hiddenSlugs?: Set<string>
}

export const docCategories: DocCategory[] = [
  {
    id: 'role-guide',
    title: '按身份开始',
    icon: Compass,
    nodes: [
      { slug: 'provider-guide', title: '服务商完整操作说明书', audience: 'agent' },
      { slug: 'standard-guide', title: '普通用户完整操作说明书', audience: 'normal_user' },
      { slug: 'registration', title: '注册与登录' },
      { slug: 'today-dashboard', title: '今日工作台' },
      { slug: 'sandbox-mode', title: '新手引导' },
    ],
  },
  {
    id: 'delivery-flow',
    title: '客户交付 · 按顺序做',
    icon: Stethoscope,
    nodes: [
      { slug: 'first-client', title: '添加与切换客户' },
      { slug: 'diagnosis', title: '1. 品牌体检', videoId: 'V1a' },
      { slug: 'read-report', title: '看懂诊断报告', videoId: 'V1b' },
      { slug: 'pricing', title: '2. 报价方案', videoId: 'V2a' },
      { slug: 'writing', title: '3. AI 写文章', videoId: 'V3a' },
      { slug: 'article-review-and-repair', title: '文章提示、局部修复与继续' },
      { slug: 'publishing', title: '4. 发布投放', videoId: 'V3b' },
    ],
  },
  {
    id: 'monitoring',
    title: '效果复盘',
    icon: Activity,
    nodes: [
      { slug: 'monitoring', title: '效果监测', videoId: 'V4' },
      { slug: 'monitoring-retry-and-brand-confirmation', title: '单格重试与品牌确认' },
      { slug: 'customer-portal', title: '客户门户与报告链接' },
      { slug: 'retest', title: '复测与历史对比' },
      { slug: 'data-export', title: '数据与报告导出' },
    ],
  },
  {
    id: 'resources',
    title: '资料与获客',
    icon: Contact,
    nodes: [
      { slug: 'my-clients', title: '我的客户' },
      { slug: 'my-brand', title: '我的品牌' },
      { slug: 'geo-content-center', title: 'GEO 获客内容' },
      { slug: 'whitelabel', title: '对外品牌' },
      { slug: 'demo-cases', title: '演示案例' },
      /* 🔴 [#222 a1b'] 客户线索**保持服务商专属**(2026-09-16 撤回一次误放开)。
         我原来的理由是「路由裸挂、功能可达,文档不该藏」—— 那是拿**可达性**代替了**内容判断**。
         C 读了正文:该页讲的是「留联系方式入口会让服务方暴露、中间差价穿帮」,属供应商零暴露内容;
         而普通账号可能同时是别人的终端客户。可达性说明不了正文该给谁看。 */
      { slug: 'leads', title: '客户线索', audience: 'agent' },
    ],
  },
  {
    id: 'team',
    title: '团队与席位',
    icon: UsersRound,
    nodes: [
      { slug: 'team-and-seats', title: '创建团队与分工' },
      { slug: 'employee-invitation', title: '邀请员工与登录' },
      { slug: 'team-members', title: '平台团队治理', audience: 'admin' },
      { slug: 'roles', title: '平台角色权限', audience: 'admin' },
      { slug: 'audit-log', title: '平台审计日志', audience: 'admin' },
    ],
  },
  {
    id: 'provider-operations',
    title: '经营后台',
    icon: BriefcaseBusiness,
    audience: 'agent',
    nodes: [
      { slug: 'profit', title: '经营总览', audience: 'agent' },
      { slug: 'stock-up', title: '算力库存', audience: 'agent' },
      { slug: 'set-pricing', title: '客户售价', audience: 'agent' },
      { slug: 'agent-settlement', title: '提现结算', audience: 'agent' },
      { slug: 'agent-promotion', title: '推广获客', audience: 'agent' },
      { slug: 'agent-agreement', title: '合作协议', audience: 'agent' },
      { slug: 'agent-wallet', title: '服务商钱包', audience: 'agent' },
      { slug: 'wallet-bank-cards', title: '银行卡管理', audience: 'agent' },
      { slug: 'wallet-service-fee-history', title: '服务费流水', audience: 'l2' },
    ],
  },
  {
    id: 'compute-account',
    title: '算力与账号',
    icon: Wallet,
    nodes: [
      { slug: 'customer-wallet', title: '我的算力', audience: 'normal_user' },
      { slug: 'customer-recharge', title: '购买算力', audience: 'normal_user' },
      { slug: 'wallet', title: '我的钱包与账单' },
      { slug: 'feature-pricing', title: '算力价格表' },
      { slug: 'referral', title: '推荐有礼' },
      { slug: 'profile', title: '个人设置' },
      { slug: 'feedback', title: '问题反馈' },
    ],
  },
  {
    id: 'geo-basics',
    title: '了解 GEO',
    icon: BookOpen,
    nodes: [
      { slug: 'what-is-geo', title: '什么是 GEO', videoId: 'C1' },
      { slug: 'four-engines', title: '监测平台' },
      { slug: 'scoring', title: '评分与指标', videoId: 'C2' },
    ],
  },
  {
    id: 'troubleshooting',
    title: '遇到问题',
    icon: Wrench,
    nodes: [
      { slug: 'payment-failed', title: '扣费、退款或订单异常' },
      { slug: 'publish-failed', title: '发布失败' },
      { slug: 'no-monitoring-data', title: '监测无数据' },
      { slug: 'cant-find', title: '入口、按钮或权限找不到' },
    ],
  },
]

export type DocLookup = { node: DocNode; category: DocCategory }

function allCategories(): DocCategory[] {
  return docCategories
}

function effectiveAudience(node: DocNode, category: DocCategory): DocAudience {
  if (node.audience) return node.audience
  if (category.audience) return category.audience
  if (category.isAdmin) return 'admin'
  return 'both'
}

export function canViewDoc(
  node: DocNode,
  category: DocCategory,
  viewer: HelpViewer,
): boolean {
  if (viewer.hiddenSlugs?.has(node.slug)) return false
  const audience = effectiveAudience(node, category)
  if (audience === 'admin') return viewer.isAdmin
  if (audience === 'l2') return viewer.isL2 || viewer.isAdmin
  if (audience === 'agent') return viewer.isAgent || viewer.isAdmin
  if (audience === 'normal_user') return viewer.isAdmin || !viewer.isAgent
  return true
}

export function getVisibleCategories(viewer: HelpViewer): DocCategory[] {
  return docCategories
    .map((category) => ({
      ...category,
      nodes: category.nodes.filter((node) => canViewDoc(node, category, viewer)),
    }))
    .filter((category) => category.nodes.length > 0)
}

export function findDoc(slug: string): DocLookup | null {
  for (const category of allCategories()) {
    const node = category.nodes.find((candidate) => candidate.slug === slug)
    if (node) return { node, category }
  }
  return null
}

export function getAllDocs(): DocLookup[] {
  return allCategories().flatMap((category) =>
    category.nodes.map((node) => ({ node, category })),
  )
}

export function getVisibleDocs(viewer: HelpViewer): DocLookup[] {
  return allCategories().flatMap((category) =>
    category.nodes
      .filter((node) => canViewDoc(node, category, viewer))
      .map((node) => ({ node, category })),
  )
}

export function getNeighbors(
  slug: string,
  viewer: HelpViewer,
): { prev: DocLookup | null; next: DocLookup | null } {
  const all = getVisibleDocs(viewer)
  const index = all.findIndex(({ node }) => node.slug === slug)
  if (index < 0) return { prev: null, next: null }
  return {
    prev: index > 0 ? all[index - 1] : null,
    next: index < all.length - 1 ? all[index + 1] : null,
  }
}

export const defaultHelpSlug = (viewer: HelpViewer): string =>
  viewer.isAgent || viewer.isAdmin ? 'provider-guide' : 'standard-guide'
