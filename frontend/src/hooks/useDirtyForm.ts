/**
 * 表单脏状态接入 hook —— 配合 `lib/dirtyGuard`。
 *
 * [WO_NO_SILENT_RELOAD_DIRTY_GUARD 2026-08-16 ①]
 *
 * 用法:
 *   useDirtyForm('monitoring.add-keyword', () => newKeyword.trim().length > 0);
 *
 * 🔴 传**函数**不传布尔值:探针是在"要不要静默刷新"那一刻**现场求值**的,
 *   传布尔值会把注册那一刻的旧值冻住,用户后来输入的内容守卫就看不见了 ——
 *   那正是"锁看着在,其实一直是假的"的经典形态。
 *
 * 🔴 dirtyGuard 本身保持 0 依赖(它被 errorReporter / versionPoll 这类独立模块 import),
 *   所以 React 这一层单独放在这里,不塞进 lib/dirtyGuard.ts。
 */
import { useEffect, useRef } from 'react';

import { registerDirtyProbe } from '@/lib/dirtyGuard';

export function useDirtyForm(id: string, isDirty: () => boolean): void {
    // 用 ref 存最新的探针:依赖数组里只放 id,避免每次渲染都重新注册一遍
    const probeRef = useRef(isDirty);
    probeRef.current = isDirty;

    useEffect(() => {
        const unregister = registerDirtyProbe(id, () => {
            try {
                return probeRef.current();
            } catch {
                return false;
            }
        });
        return unregister;   // 卸载即注销 —— 页面走了就不该再拦刷新
    }, [id]);
}
