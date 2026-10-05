// [2026-05-18 帮助中心 Layer 3] FAQ 静态配置
// Phase 2: items 列表从 GET /api/faq/items 拉, 不再硬编码
// 这里只留分类元数据 · 后端 + 前端约定一致

export type FAQCategoryId = 'billing' | 'operation' | 'data' | 'account'

export const faqCategories: { id: FAQCategoryId | 'all'; title: string }[] = [
  { id: 'all', title: '全部' },
  { id: 'billing', title: '计费' },
  { id: 'operation', title: '操作' },
  { id: 'data', title: '数据' },
  { id: 'account', title: '账号' },
]
