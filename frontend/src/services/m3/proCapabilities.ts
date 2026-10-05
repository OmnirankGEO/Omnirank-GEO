/**
 * M3 专业能力映射表(proCapabilities)
 *
 * 老板新策略 v2(2026-04-26 · CTO-15.16):
 *   M3 不再跳老路由。专业能力变 M3 工具屏(右侧 Sheet) · 复用 API + 局部组件 ·
 *   不 iframe 老页面 · 不整页复制老 Layout。
 *
 *   - mode='m3_tool' → 走 useM3ToolOpener(toolId) · Sheet host 内渲染(M3 布局壳 已挂)
 *   - mode='professional_page' → 跳 M3 子路由(/m3/* · 老 ReferralCenter / ProfitDashboard / WhitelabelSettings 由 M3 布局壳 shell wrap)
 *   - 所有原 external_legacy(7 个能力)已升级为 m3_tool · 不再裸跳老 /writing /publish 等
 *   - 老 capabilityId 名称保留兼容(writing_advanced 等)· 调用点不强制改名
 */

import type { ToolId } from './toolId';

export type CapabilityMode =
  | 'quick_action'         // 即时动作 · 不跳页 · 在当前位置触发 callback
  | 'professional_panel'   // M3 内 Sheet/Drawer 抽屉 · 老组件 lazy 嵌入(预留 · 当前未用)
  | 'professional_page'    // 跳 M3 子路由(/m3/*) · 不离开 M3 壳
  | 'm3_tool';             // M3 工具屏 · openM3Tool(toolId) · 主流模式

export type CapabilityFrequency = 'daily' | 'weekly' | 'monthly' | 'rare';
export type CapabilityRisk = 'low' | 'medium' | 'high';
export type CapabilityRole = 'agent' | 'admin' | 'manager';

export interface CapabilityContext {
  brandId?: number | string;
  quoteId?: number | string;
  reportId?: number | string;
  articleId?: number | string;
  diagnosisId?: number | string;
  returnTo?: string;
}

export interface ProCapability {
  id: string;
  label: string;
  description: string;
  mode: CapabilityMode;
  frequency: CapabilityFrequency;
  risk: CapabilityRisk;
  ownerRole: CapabilityRole;
  allowedRoles: CapabilityRole[];
  requiresConfirmation: boolean;
  /** mode='m3_tool' 必填 · 关联工具屏 component */
  toolId?: ToolId;
  /** mode='professional_page' 必填 · M3 子路由 */
  m3Route?: string;
  buildM3Href?: (ctx: CapabilityContext) => string;
}

export const PRO_CAPABILITIES: Record<string, ProCapability> = {
  // ============================================================
  // M3 工具屏(7 个 · M3 不再跳老 /writing /publish /reports 等)
  // ============================================================
  writing_advanced: {
    id: 'writing_advanced',
    label: '写作工具',
    description: '文章查看 / 编辑 / 重写 / 事实核验 / 标已审',
    mode: 'm3_tool',
    toolId: 'writing_tool',
    frequency: 'weekly',
    risk: 'low',
    ownerRole: 'agent',
    allowedRoles: ['agent', 'admin'],
    requiresConfirmation: false,
  },

  publish_advanced: {
    id: 'publish_advanced',
    label: '发布工具',
    description: '手动发布证据 / 截图上传 / 发布状态 / 渠道概览',
    mode: 'm3_tool',
    toolId: 'publish_tool',
    frequency: 'weekly',
    risk: 'medium',
    ownerRole: 'agent',
    allowedRoles: ['agent', 'admin'],
    requiresConfirmation: false,
  },

  report_editor: {
    id: 'report_editor',
    label: '报告工具',
    description: '报告预览 / 8 模块 / 发送 / 下载 / 重新生成',
    mode: 'm3_tool',
    toolId: 'report_tool',
    frequency: 'monthly',
    risk: 'low',
    ownerRole: 'agent',
    allowedRoles: ['agent', 'admin'],
    requiresConfirmation: false,
  },

  diagnosis_report_full: {
    id: 'diagnosis_report_full',
    label: '诊断报告完整版',
    description: '诊断报告 / 8 模块 / 历史趋势 / 重新生成 / 分享',
    mode: 'm3_tool',
    toolId: 'report_tool',
    frequency: 'rare',
    risk: 'low',
    ownerRole: 'agent',
    allowedRoles: ['agent', 'admin'],
    requiresConfirmation: false,
  },

  article_history: {
    id: 'article_history',
    label: '文章历史',
    description: '文章搜索 / 查看 / 复用',
    mode: 'm3_tool',
    toolId: 'article_history_tool',
    frequency: 'rare',
    risk: 'low',
    ownerRole: 'agent',
    allowedRoles: ['agent', 'admin'],
    requiresConfirmation: false,
  },

  client_profile_full: {
    id: 'client_profile_full',
    label: '客户档案',
    description: '完整度 / 缺失字段 / 补齐入口 / 保存',
    mode: 'm3_tool',
    toolId: 'customer_profile_tool',
    frequency: 'weekly',
    risk: 'low',
    ownerRole: 'agent',
    allowedRoles: ['agent', 'admin'],
    requiresConfirmation: false,
  },

  monitor_config: {
    id: 'monitor_config',
    label: '监测工具',
    description: '异常告警 / 调度状态 / 轻量配置',
    mode: 'm3_tool',
    toolId: 'monitor_tool',
    frequency: 'rare',
    risk: 'high',
    ownerRole: 'admin',
    allowedRoles: ['admin'],
    requiresConfirmation: false,
  },

  managed_dashboard: {
    id: 'managed_dashboard',
    label: 'GEO 托管管理',
    description: '托管套餐 / 客户 / 状态 / 暂停 / 加充 / 24h 撤回',
    mode: 'm3_tool',
    toolId: 'managed_tool',
    frequency: 'weekly',
    risk: 'medium',
    ownerRole: 'agent',
    allowedRoles: ['agent', 'admin'],
    requiresConfirmation: false,
  },

  // 报价管理 · 替代裸跳 /quote · 销售端 M3 选词页 / ActivationDialog 用
  agent_quote: {
    id: 'agent_quote',
    label: '报价管理',
    description: '选词链接 / 复制 / 加词 / 改价 / 审核 / 撤回',
    mode: 'm3_tool',
    toolId: 'quote_management_tool',
    frequency: 'weekly',
    risk: 'medium',
    ownerRole: 'agent',
    allowedRoles: ['agent', 'admin'],
    requiresConfirmation: false,
  },

  // ============================================================
  // M3 子路由(M3 布局壳 shell wrap · 仍走 navigate · 不动)
  // ============================================================
  // 🔴 [WO_260 · 2026-09-23] 下面 4 条原指 /m3/referral /m3/agent/profit /m3/agent/whitelabel /m3/settings;
  //    M3 路由只剩重定向到首页、E3 删域后 404。改指同一功能的在役页(侧栏里就是这 4 个入口),
  //    字段名 m3Route / buildM3Href 沿用(原调用方都在 components/m3/**,已随开源 E3 删)。
  agent_referral: {
    id: 'agent_referral',
    label: '推荐中心',
    description: '邀请链接 / 服务收益 / 收益结算明细',
    mode: 'professional_page',
    m3Route: '/referral',
    frequency: 'monthly',
    risk: 'low',
    ownerRole: 'agent',
    allowedRoles: ['agent', 'admin'],
    requiresConfirmation: false,
    buildM3Href: () => '/referral',
  },

  agent_profit: {
    id: 'agent_profit',
    label: '利润看板',
    description: '代理利润 / 服务期 / 续费 / 财务汇总',
    mode: 'professional_page',
    m3Route: '/agent/profit',
    frequency: 'weekly',
    risk: 'low',
    ownerRole: 'agent',
    allowedRoles: ['agent', 'admin'],
    requiresConfirmation: false,
    buildM3Href: () => '/agent/profit',
  },

  agent_whitelabel: {
    id: 'agent_whitelabel',
    label: '白标设置',
    description: '品牌定制 / Logo / 主题色 / 落地页',
    mode: 'professional_page',
    m3Route: '/agent/whitelabel',
    frequency: 'rare',
    risk: 'low',
    ownerRole: 'agent',
    allowedRoles: ['agent', 'admin'],
    requiresConfirmation: false,
    buildM3Href: () => '/agent/whitelabel',
  },

  settings_advanced: {
    id: 'settings_advanced',
    label: '系统设置',
    description: '完整账户 / 团队 / 通知 / API / 安全',
    mode: 'professional_page',
    m3Route: '/settings',
    frequency: 'rare',
    risk: 'low',
    ownerRole: 'agent',
    allowedRoles: ['agent', 'admin'],
    requiresConfirmation: false,
    buildM3Href: () => '/settings',
  },
};

export function getCapability(id: string): ProCapability | undefined {
  return PRO_CAPABILITIES[id];
}

export const M3_RETURN_TO_KEY = 'm3.professional_return_to';
