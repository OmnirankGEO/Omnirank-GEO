import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * 从任意错误数据中安全提取人话错误信息。
 * 防止 JSON 对象直接渲染给用户（如 billing 的 402 错误）。
 *
 * 支持: string / {message} / {detail} / {detail: {message}} / 数组 / 任意嵌套
 */
export function extractErrorMessage(err: unknown, fallback = '操作失败，请稍后重试'): string {
  if (!err) return fallback;
  if (typeof err === 'string') return err;
  if (typeof err !== 'object') return String(err);

  const obj = err as Record<string, unknown>;

  // 直接有 message 字段（最常见）
  if (typeof obj.message === 'string') return obj.message;

  // detail 字段（FastAPI HTTPException 格式）
  const detail = obj.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') {
    const d = detail as Record<string, unknown>;
    if (typeof d.message === 'string') return d.message;
    if (typeof d.msg === 'string') return d.msg;
  }

  // error 字段
  if (typeof obj.error === 'string') return obj.error;

  // 数组（Pydantic 验证错误）
  if (Array.isArray(err)) {
    return err.map(e => (typeof e === 'object' && e?.msg) ? e.msg : String(e)).join(', ') || fallback;
  }

  return fallback;
}

// =====================================================================
// 日期时区统一处理
// 后端返回的时间通常是 UTC ISO 字符串（或 naive local）。
// 这里统一用北京时间展示 (Asia/Shanghai)，避免用户机器时区不同显示不一致。
// =====================================================================

const BEIJING_TZ = 'Asia/Shanghai';

/** 安全解析各种后端返回的时间格式 */
function parseDate(input: string | Date | null | undefined): Date | null {
  if (!input) return null;
  if (input instanceof Date) return isNaN(input.getTime()) ? null : input;
  // 后端常见格式:
  //   "2026-04-08T17:10:27.000Z" — UTC ISO
  //   "2026-04-08T17:10:27"      — naive 局部时间（Postgres timestamp without tz）
  //   "2026-04-08 17:10:27"      — naive 局部时间
  let s = String(input).trim();
  if (!s) return null;
  // 把 "YYYY-MM-DD HH:MM:SS" 变成 "YYYY-MM-DDTHH:MM:SS"，浏览器才能识别
  if (s.includes(' ') && !s.includes('T')) s = s.replace(' ', 'T');
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

/** 格式化为北京时间 YYYY-MM-DD HH:mm */
export function formatDateTime(input: string | Date | null | undefined, fallback = '—'): string {
  const d = parseDate(input);
  if (!d) return fallback;
  try {
    const fmt = new Intl.DateTimeFormat('zh-CN', {
      timeZone: BEIJING_TZ,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false,
    });
    // zh-CN 默认输出 "2026/04/08 17:10"，统一换成 "2026-04-08 17:10"
    return fmt.format(d).replace(/\//g, '-');
  } catch {
    return d.toISOString().slice(0, 16).replace('T', ' ');
  }
}

/** 格式化为北京时间 YYYY-MM-DD */
export function formatDate(input: string | Date | null | undefined, fallback = '—'): string {
  const d = parseDate(input);
  if (!d) return fallback;
  try {
    const fmt = new Intl.DateTimeFormat('zh-CN', {
      timeZone: BEIJING_TZ,
      year: 'numeric', month: '2-digit', day: '2-digit',
    });
    return fmt.format(d).replace(/\//g, '-');
  } catch {
    return d.toISOString().slice(0, 10);
  }
}

/** 相对时间 "3 分钟前" / "昨天" / "2 小时前" */
export function formatRelativeTime(input: string | Date | null | undefined, fallback = ''): string {
  const d = parseDate(input);
  if (!d) return fallback;
  const diff = Date.now() - d.getTime();
  if (diff < 0) return formatDateTime(d);
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return '刚刚';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.floor(hr / 24);
  if (day === 1) return '昨天';
  if (day < 7) return `${day} 天前`;
  if (day < 30) return `${Math.floor(day / 7)} 周前`;
  return formatDate(d);
}
