/**
 * useIsMounted — 轻量级"组件是否已卸载"判断
 *
 * 用法:
 *     const isMounted = useIsMounted();
 *     useEffect(() => {
 *         fetchData().then(d => {
 *             if (!isMounted()) return;  // 组件卸载就丢弃结果
 *             setState(d);
 *         });
 *     }, []);
 *
 * 比 AbortController 更轻：不中断网络请求，但避免 stale setState 警告
 * + 状态错乱。适合大部分只读 fetch 场景。
 */
import { useRef, useEffect, useCallback } from 'react';

export function useIsMounted(): () => boolean {
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  return useCallback(() => mountedRef.current, []);
}

export default useIsMounted;
