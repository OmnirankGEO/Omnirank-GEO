/**
 * 通知中心 feed 契约(工单 2026-07-29 T3)
 *
 * 两类分组:
 *   system  —— 产品更新 / 公告 / 业务状态(如"效果监测已完成")→ 跳对应业务页
 *   billing —— 每笔扣费 / 退费 / 冻结释放 → 跳消费明细页
 *
 * 🔴 billing 是资金流水的**只读投影**:前端只展示与跳转,任何路径都不触发扣费/退费写操作。
 */

export type NotificationCategory = 'system' | 'billing';
export type NotificationFilter = 'all' | NotificationCategory;

export interface FeedItem {
  /** 复合 id:`sys:<id>` / `bill:<id>` —— 已读接口按这个前缀分流 */
  id: string;
  raw_id: number;
  category: NotificationCategory;
  type: string;
  title: string;
  /** 完整正文(库中原文),弹窗逐字展示、不截断 */
  content: string;
  link: string;
  level: string;
  is_read: boolean;
  created_at: string;
  business_no?: string;
}

export interface FeedPage {
  items: FeedItem[];
  total: number;
  totals: { system: number; billing: number };
  limit: number;
  offset: number;
  has_more: boolean;
}

export const CATEGORY_LABELS: Record<NotificationFilter, string> = {
  all: '全部',
  system: '系统消息',
  billing: '扣费通知',
};

/** 弹窗里跳转按钮的文案 —— 按类别给人话,不露路由 */
export function jumpLabel(item: FeedItem): string {
  if (item.category === 'billing') return '去消费明细';
  if (item.link.startsWith('/monitoring')) return '去效果监测页面';
  if (item.link.startsWith('/wallet') || item.link.startsWith('/customer/wallet')) return '去消费明细';
  if (item.link.startsWith('/reports')) return '去报告页面';
  if (item.link.startsWith('/writing')) return '去写作大厅';
  if (item.link.startsWith('/publish')) return '去发布中心';
  if (item.link.startsWith('/pricing')) return '去报价中心';
  return '去对应页面';
}

/** 站内跳转白名单:阻断 //evil.com 与 \\ 绕过;/admin 仅管理员可跳 */
export function mayOpenLink(link: string, isAdmin: boolean): boolean {
  if (!link) return false;
  const internal = link.startsWith('/') && !link.startsWith('//') && !link.includes('\\');
  if (!internal) return false;
  return !link.startsWith('/admin') || isAdmin === true;
}

export function buildFeedUrl(opts: {
  category: NotificationFilter;
  limit: number;
  offset: number;
  unreadOnly?: boolean;
}): string {
  const params = new URLSearchParams({
    category: opts.category,
    limit: String(opts.limit),
    offset: String(opts.offset),
    unread_only: String(Boolean(opts.unreadOnly)),
  });
  return `/api/user/notifications/feed?${params.toString()}`;
}

export function formatFeedTime(value: string): string {
  if (!value) return '';
  const date = new Date(value);
  const diff = Date.now() - date.getTime();
  if (diff < 60000) return '刚刚';
  if (diff < 3600000) return `${Math.floor(diff / 60000)}分钟前`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}小时前`;
  return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
}

export function formatFeedFullTime(value: string): string {
  if (!value) return '';
  return new Date(value).toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
}
