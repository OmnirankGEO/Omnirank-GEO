/**
 * useEmbeddedNavigate — navigate 的 panel 友好替代
 *
 * 行为：
 *   - 在 C 端 panel 内：navigate('/xxx') 改走 panel.openInPanel；navigate(-1) 改走 panel.closePanel
 *   - 在代理端/独立路由：行为完全等同于 useNavigate
 *
 * 用法（兼容 useNavigate 调用签名，零业务逻辑改动）：
 *   const navigate = useEmbeddedNavigate();
 *   navigate('/wallet/recharge');  // panel 内则在 panel 切换，否则全局跳
 *   navigate(-1);                  // panel 内则关 panel，否则浏览器后退
 *
 * 为什么不用 useNavigate + 在 panel 内手动判断？
 *   18 个组件每个都有 navigate 调用，逐个加 if-embedded 分支会污染业务逻辑。
 *   这个 hook 在 useNavigate 上面套一层，调用代码不需要改。
 */

import { useNavigate, NavigateOptions } from 'react-router-dom';
import { useCallback } from 'react';
import { useCEndPanel } from '@/context/CEndPanelContext';

export function useEmbeddedNavigate() {
  const navigate = useNavigate();
  const panel = useCEndPanel(); // 代理端外层无 CEndPanelProvider 时返回 null

  return useCallback(
    (to: string | number, options?: NavigateOptions) => {
      // 数字 = 后退
      if (typeof to === 'number') {
        if (panel?.panelUrl && to < 0) {
          panel.closePanel();
          return;
        }
        navigate(to);
        return;
      }
      // 字符串路径
      if (panel?.panelUrl) {
        // 已在 panel 内，跳转改在 panel 切换（panelUrl 更新触发顶部标签同步）
        panel.openInPanel(to);
        return;
      }
      navigate(to, options);
    },
    [navigate, panel],
  );
}
