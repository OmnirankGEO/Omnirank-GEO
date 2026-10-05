/**
 * useCompactMode — 简洁模式开关(Phase E.6 · v2 §14)
 *
 * CTO-15.10 · 2026-04-27
 *
 * 用途:
 *   20-30 岁手机为主代理偏好简洁 · 30-40 岁桌面为主代理偏好全景
 *   localStorage 'omnirank_m3_compact' 持久化 · 跨页面跨刷新一致
 *
 * 影响维度(由各组件按需读 · 不强制):
 *   - 客户卡片:9 步生命周期 / 详尽 KV → 折叠为"当前 + 下一步"
 *   - 数字:积分 + 人民币 → 仅人民币
 *   - 客户标识:名字 + BRD-id + 行业 + 评分 → 仅名字 + 行业
 *   - Banner / 长任务文案:静默 toast 替代常驻
 *
 * 不影响业务逻辑 · 仅纯视觉浓度
 */

import { useEffect, useState, useCallback } from 'react';

const STORAGE_KEY = 'omnirank_m3_compact';

let listeners: Array<(v: boolean) => void> = [];

export function useCompactMode(): {
  compact: boolean;
  setCompact: (next: boolean) => void;
  toggle: () => void;
} {
  const [compact, setCompactState] = useState<boolean>(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) === '1';
    } catch {
      return false;
    }
  });

  useEffect(() => {
    const listener = (v: boolean) => setCompactState(v);
    listeners.push(listener);
    return () => {
      listeners = listeners.filter((l) => l !== listener);
    };
  }, []);

  const setCompact = useCallback((next: boolean) => {
    try {
      if (next) localStorage.setItem(STORAGE_KEY, '1');
      else localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
    listeners.forEach((l) => l(next));
  }, []);

  const toggle = useCallback(() => setCompact(!compact), [compact, setCompact]);

  return { compact, setCompact, toggle };
}

export function isCompactMode(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}
