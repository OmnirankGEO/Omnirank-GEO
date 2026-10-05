/**
 * useSoftKeyboardOpen — 检测移动端软键盘是否弹出
 *
 * [WO_IOS_TOUCH_UX 2026-08-05]
 * iOS Safari 弹软键盘时**不改变** layout viewport,只缩小 visual viewport。
 * 于是 `position: fixed; bottom: 0` 的底部操作栏留在原地 → **被键盘整条盖住**。
 * 客户在 /intake/:token 或 /material-confirm/:token 里填资料时,
 * 点进输入框之后就看不见"下一步 / 提交"了,体感就是"卡在这一步动不了"。
 * 这两个页面是**客户视角、token 无登录态** —— 客户填不完不会来报 bug,只会流失。
 *
 * 判据:visualViewport.height 比 window.innerHeight 矮出 120px 以上 = 键盘占了位置。
 *   · 120px 这个阈值是为了不把 iOS 顶部/底部工具栏的收放(约 40-90px)误判成键盘。
 *   · 不用 focusin/focusout 判:那个在 iOS 上会被 select 下拉、日期选择器等误触发。
 *
 * 没有 visualViewport 的环境(桌面老浏览器 / SSR)恒返 false → 行为与改动前一致。
 */
import { useEffect, useState } from 'react';

/** 低于这个差值不算键盘(iOS 工具栏收放约 40-90px) */
const KEYBOARD_MIN_INSET = 120;

export interface SoftKeyboardState {
    /** 键盘是否弹出 */
    open: boolean;
    /**
     * 键盘遮住的高度(px)。open=false 时为 0。
     * 用法:给 fixed 底部条加 `transform: translateY(-inset px)`,把它顶到键盘上方 ——
     * 🔴 当底部条**里面**就有输入框时(MaterialConfirmPage 的备注 textarea 就是),
     *    必须这么做:改成 static 会让用户正在打字的框跑到文档末尾要滚动才看得见。
     */
    inset: number;
}

export function useSoftKeyboardOpen(): SoftKeyboardState {
    const [state, setState] = useState<SoftKeyboardState>({ open: false, inset: 0 });

    useEffect(() => {
        const vv = typeof window !== 'undefined' ? window.visualViewport : undefined;
        if (!vv) return;

        const update = () => {
            // window.innerHeight = layout viewport(键盘弹出时不变)
            // vv.height          = 可视区(键盘弹出时变矮)
            // vv.offsetTop       = 页面被系统上推的量(iOS 会推)—— 不减掉它底部条会顶过头
            const raw = window.innerHeight - vv.height - vv.offsetTop;
            const open = raw > KEYBOARD_MIN_INSET;
            setState((prev) => {
                const next = { open, inset: open ? Math.round(raw) : 0 };
                // 键盘高度会随输入法候选栏抖动,差值 <8px 不触发重渲染
                if (prev.open === next.open && Math.abs(prev.inset - next.inset) < 8) return prev;
                return next;
            });
        };

        update();
        vv.addEventListener('resize', update);
        vv.addEventListener('scroll', update);
        return () => {
            vv.removeEventListener('resize', update);
            vv.removeEventListener('scroll', update);
        };
    }, []);

    return state;
}
