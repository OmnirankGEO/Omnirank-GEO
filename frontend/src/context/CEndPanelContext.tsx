/**
 * CEndPanelContext — C 端分屏/覆盖面板状态（v3.2 Week 2）
 *
 * 作用：
 *   C 端用户点 AI 推荐的"去发布"/"查钱包"等跳转动作时，
 *   不改变主路由（保留对话），而是在右侧打开分屏（桌面）或全屏覆盖（手机）
 *
 * 对比：
 *   代理端 旧对话动作卡 用 navigate() 全屏跳转
 *   C 端 旧对话动作卡 用 openInPanel() 打开嵌入模式
 *
 * 使用：
 *   const panel = useCEndPanel();
 *   panel?.openInPanel('/wallet?embedded=true');
 */

import { createContext, useContext, useState, useCallback, useMemo, ReactNode } from 'react';

interface CEndPanelContextType {
  panelUrl: string | null;
  openInPanel: (url: string) => void;
  closePanel: () => void;
  /** Panel URL 的 query params (给 Panel 内嵌组件用 · CTO-15.5 Phase 4 PLAN 05 Task 5.1)
   *  说明: PANEL_COMPONENTS 映射的组件挂在主 Router 里,useSearchParams 读的是
   *        浏览器地址栏(/c/chat 或 /c/history),不是 panelUrl 的 query.
   *        需要读 task_id 等 query 时,优先用 panelSearchParams,fallback 到 useSearchParams. */
  panelSearchParams: URLSearchParams;
}

const CEndPanelContext = createContext<CEndPanelContextType | null>(null);

export function CEndPanelProvider({ children }: { children: ReactNode }) {
  const [panelUrl, setPanelUrl] = useState<string | null>(null);

  const openInPanel = useCallback((url: string) => {
    // 确保 URL 带 embedded=true 和 source=c
    // - embedded=true: Layout 走无 Sidebar 模式
    // - source=c:      嵌入页面识别出"来自 C 端"，隐藏下载等功能
    let finalUrl = url;
    if (!finalUrl.includes('embedded=true')) {
      finalUrl = finalUrl.includes('?') ? `${finalUrl}&embedded=true` : `${finalUrl}?embedded=true`;
    }
    if (!finalUrl.includes('source=')) {
      finalUrl = `${finalUrl}&source=c`;
    }
    setPanelUrl(finalUrl);
  }, []);

  const closePanel = useCallback(() => setPanelUrl(null), []);

  // 解析 panelUrl 的 query 部分 → URLSearchParams (memoized)
  const panelSearchParams = useMemo(() => {
    if (!panelUrl) return new URLSearchParams();
    const qIdx = panelUrl.indexOf('?');
    if (qIdx < 0) return new URLSearchParams();
    return new URLSearchParams(panelUrl.slice(qIdx + 1));
  }, [panelUrl]);

  return (
    <CEndPanelContext.Provider value={{ panelUrl, openInPanel, closePanel, panelSearchParams }}>
      {children}
    </CEndPanelContext.Provider>
  );
}

/**
 * 返回 null 时表示不在 C 端，调用者应 fallback 到 navigate()
 */
export function useCEndPanel(): CEndPanelContextType | null {
  return useContext(CEndPanelContext);
}
