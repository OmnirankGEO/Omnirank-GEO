/**
 * 纯展示格式化（不是指标计算）。
 * 只把后端算好的 *_bps / 时间戳 / 计数变成人可读文本，不做任何比例/排名/阈值推导。
 */

/** 后端 bps（0..10000）→ 展示百分比字符串。仅四舍五入到一位小数，不改变数值语义。 */
export function bpsToPercent(bps: number | null | undefined, digits = 1): string {
  if (bps === null || bps === undefined || Number.isNaN(bps)) return '—';
  return `${(bps / 100).toFixed(digits)}%`;
}

/** 后端 bps → 数值百分比（用于图表宽度等，不用于任何再计算展示）。 */
export function bpsToNumber(bps: number | null | undefined): number {
  if (bps === null || bps === undefined || Number.isNaN(bps)) return 0;
  return bps / 100;
}

/** 变化量 bps → 带符号 pp 文本。仅在 comparison_allowed 时使用。 */
export function bpsChangeToPP(bps: number | null | undefined): string {
  if (bps === null || bps === undefined || Number.isNaN(bps)) return '—';
  const pp = bps / 100;
  const sign = pp > 0 ? '+' : '';
  return `${sign}${pp.toFixed(1)}pp`;
}

const CN = 'zh-CN';

/**
 * 解析时间：仅 YYYY-MM-DD 的"纯日期"按本地日历解析（避免 new Date 按 UTC 解析导致早一天）；
 * 带时区的完整时间戳保持为时刻。
 */
function parseTime(iso: string): Date {
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return new Date(`${iso}T00:00:00`);
  return new Date(iso);
}

/** ISO 时间 → 相对/绝对可读中文。null 安全。 */
export function formatUpdatedAt(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = parseTime(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString(CN, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = parseTime(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString(CN, { year: 'numeric', month: '2-digit', day: '2-digit' });
}

export function formatShortDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = parseTime(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString(CN, { month: 'short', day: 'numeric' });
}

/** 秒 → 人可读延迟（管理员用）。 */
export function formatDelaySeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return s ? `${m} 分 ${s} 秒` : `${m} 分`;
}

/** 排名位置 → 文本（null = 未上榜/不适用）。 */
export function formatPosition(pos: number | null | undefined): string {
  if (pos === null || pos === undefined) return '—';
  return `第 ${pos} 位`;
}

/** 数字千分位。 */
export function formatCount(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return n.toLocaleString(CN);
}
