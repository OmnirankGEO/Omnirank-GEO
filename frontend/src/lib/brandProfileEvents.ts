/**
 * brandProfileEvents · 跨入口客户数据更新事件总线
 *
 * Phase 06 (CTO-15.23 2026-05-03 T8) · 老板诉求:
 *   任一入口写客户字段 → 其他打开的页面/tab 不 F5 自动刷新
 *
 * 与 customerEvents.ts(C 端公开页埋点 SDK)不同概念:
 *   - customerEvents.ts: 外部数据上报(C 端访客行为信号)
 *   - brandProfileEvents.ts: 内部状态广播(代理端各页面客户数据同步)
 *
 * 用法:
 *   写入点(创建/修改客户):
 *     import { emitBrandUpdated } from '@/lib/brandProfileEvents';
 *     emitBrandUpdated(brandId);  // 提交成功后调用
 *
 *   读取点(列表/详情):
 *     import { useOnBrandUpdated } from '@/lib/brandProfileEvents';
 *     useOnBrandUpdated((brandId) => {
 *       // 重新拉数据 / setReloadKey(k => k + 1) / fetchClients() 等
 *     });
 *
 * 事件机制:Browser CustomEvent · 同窗口跨组件 · 不跨 tab(跨 tab 需 BroadcastChannel)
 *
 * Phase 1(本次) · 写入点仅 CustomerIntakeDialog 接入 · 监听点仅 MyClientsPage
 * Phase 2(后续) · M3 新增客户页 / ClientMaterialsEditor / MarketingTab / M3 客户工作台 / WritingHall 全接入
 */

import { useEffect, useRef } from 'react';

const EVENT_NAME = 'omnirank:brand-profile-updated';

interface BrandUpdatedDetail {
  brand_id: number;
  /** 来源标识(可选 · 调试用 · 例:'quick-write' / 'add-client-m3' / 'client-materials-editor') */
  source?: string;
}

/** 写入点调用 · 通知所有监听者 brand 数据已变 */
export function emitBrandUpdated(brand_id: number, source?: string): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(
    new CustomEvent<BrandUpdatedDetail>(EVENT_NAME, { detail: { brand_id, source } }),
  );
}

/** React hook · 读取点用 · 监听 brand 更新事件 */
export function useOnBrandUpdated(handler: (brand_id: number, source?: string) => void): void {
  // 用 ref 保存 handler · 避免每次 handler 变化都解绑/重绑监听器
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  useEffect(() => {
    const listener = (e: Event) => {
      const ce = e as CustomEvent<BrandUpdatedDetail>;
      if (ce.detail && typeof ce.detail.brand_id === 'number') {
        handlerRef.current(ce.detail.brand_id, ce.detail.source);
      }
    };
    window.addEventListener(EVENT_NAME, listener);
    return () => window.removeEventListener(EVENT_NAME, listener);
  }, []);
}
