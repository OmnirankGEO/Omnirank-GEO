/**
 * Social Studio 设计 token (ss-*)
 *
 * 抽自 社媒工作台主页面.tsx 的 themeTokens, 让 /pricing-plans / /subscription/manage
 * 等非 社媒工作台主页面 挂载的页面也能用 ss-* 设计语言 —— 否则它们落回全站 shadcn
 * token, 视觉上像 GEO 板块 (2026-05-13 老板反馈"搞到 GEO 板块了")
 *
 * 用法 (任何想用 ss-* token 的页面外层包一层):
 *   import { useSocialStudioTheme, socialStudioThemeStyle } from '@/lib/socialStudioTheme';
 *   const style = useSocialStudioTheme();   // 跟随用户偏好 + localStorage
 *   return <div style={style}>...</div>;
 */
import { useEffect, useMemo, useState, type CSSProperties } from 'react';

export type SocialStudioTheme = 'light' | 'dark';

const STORAGE_KEY = 'social_studio_theme';

export const socialStudioThemeTokens: Record<SocialStudioTheme, CSSProperties> = {
  light: {
    '--ss-bg': '#f7f7f5',
    '--ss-panel': '#ffffff',
    '--ss-panel-soft': '#f0f0ed',
    '--ss-panel-softer': '#eeeeeb',
    '--ss-hover': '#ededeb',
    '--ss-hover-strong': '#e4e4e1',
    '--ss-line': '#e4e4e0',
    '--ss-line-soft': '#ededeb',
    '--ss-text': '#111111',
    '--ss-text-soft': '#30352f',
    '--ss-muted': '#6b6c67',
    '--ss-quiet': '#8c8d88',
    '--ss-primary': '#111111',
    '--ss-primary-hover': '#242424',
    '--ss-primary-text': '#ffffff',
    '--ss-user-bubble': '#111111',
    '--ss-user-text': '#ffffff',
    '--ss-disabled': '#d7d7d2',
    '--ss-overlay': 'rgba(17, 17, 17, 0.16)',
    '--ss-card-shadow': '0 8px 24px rgba(0,0,0,.05)',
    '--ss-soft-shadow': '0 1px 1px rgba(0,0,0,.03)',
    '--ss-focus': '#111111',
    '--ss-danger': '#b42318',
    '--ss-danger-bg': '#fff4f2',
    '--ss-danger-line': '#ffd6d0',
    '--ss-success': '#067647',
    '--ss-success-bg': '#ecfdf3',
    '--ss-success-line': '#abefc6',
  } as CSSProperties,
  dark: {
    '--ss-bg': '#202123',
    '--ss-panel': '#2f3033',
    '--ss-panel-soft': '#292a2d',
    '--ss-panel-softer': '#303134',
    '--ss-hover': '#36373a',
    '--ss-hover-strong': '#3f4044',
    '--ss-line': '#3f4044',
    '--ss-line-soft': '#35363a',
    '--ss-text': '#f4f4f2',
    '--ss-text-soft': '#e8e8e3',
    '--ss-muted': '#c5c7bf',
    '--ss-quiet': '#a0a39b',
    '--ss-primary': '#f4f4f2',
    '--ss-primary-hover': '#ffffff',
    '--ss-primary-text': '#171719',
    '--ss-user-bubble': '#f4f4f2',
    '--ss-user-text': '#171719',
    '--ss-disabled': '#4b4c50',
    '--ss-overlay': 'rgba(0, 0, 0, 0.42)',
    '--ss-card-shadow': '0 12px 34px rgba(0,0,0,.28)',
    '--ss-soft-shadow': '0 1px 1px rgba(0,0,0,.22)',
    '--ss-focus': '#f4f4f2',
    '--ss-danger': '#ffb4a8',
    '--ss-danger-bg': 'rgba(255, 120, 105, .09)',
    '--ss-danger-line': 'rgba(255, 120, 105, .28)',
    '--ss-success': '#9be7c0',
    '--ss-success-bg': 'rgba(32, 185, 118, .11)',
    '--ss-success-line': 'rgba(32, 185, 118, .3)',
  } as CSSProperties,
};

function readInitialTheme(): SocialStudioTheme {
  if (typeof window === 'undefined') return 'dark';
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch {
    /* ignore */
  }
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

/**
 * 在 社媒工作台主页面 之外的页面使用 ss-* tokens, 跟随用户已有的偏好
 * (主页设过 dark 这里也跟 dark, 主页 light 这里也 light)
 *
 * 监听 storage 事件让用户在主页切换主题时本页跟着变 (不需要刷新)
 */
export function useSocialStudioTheme(): CSSProperties {
  const [theme, setTheme] = useState<SocialStudioTheme>(readInitialTheme);

  useEffect(() => {
    const handler = (event: StorageEvent) => {
      if (event.key !== STORAGE_KEY) return;
      if (event.newValue === 'light' || event.newValue === 'dark') {
        setTheme(event.newValue);
      }
    };
    window.addEventListener('storage', handler);
    return () => window.removeEventListener('storage', handler);
  }, []);

  return useMemo(() => socialStudioThemeTokens[theme], [theme]);
}
