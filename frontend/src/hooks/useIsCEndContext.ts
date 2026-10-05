/**
 * useIsCEndContext — 判断当前页面是否在 C 端上下文中（v3.2 Week 2）
 *
 * 触发 C 端模式的场景：URL 有 source=c 参数（被 C 端 旧 C 端嵌入面板 打开的嵌入页面）
 * [WO_260] 原第一种场景「URL 路径是 /c/*」已删:老 C 端路由一律重定向到 `/`、随 E3 删域。
 *
 * 用途：
 *   - 隐藏下载按钮（C 端产品设计上不提供下载）
 *   - 显示"回到对话"按钮
 *   - 发布时强制勾选免责声明
 *
 * 用法：
 *   const isCEnd = useIsCEndContext();
 *   if (isCEnd) { return null; }  // 隐藏
 */

import { useSearchParams } from 'react-router-dom';

export function useIsCEndContext(): boolean {
  const [searchParams] = useSearchParams();

  // [WO_260] 原「在 /c/* 主路由下」判断已删:老 C 端路由一律重定向到 `/`、随 E3 删域,在役页永远命不中。

  // 被 C 端分屏打开的嵌入页面（带 source=c 参数）
  if (searchParams.get('source') === 'c') {
    return true;
  }

  return false;
}
