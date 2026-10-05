/**
 * 截图模式(Screenshot Mode) · 2026-05-23 重做
 *
 * 用途:
 *   demo_shot 等演示号在沙盒里录截图/视频时, 需要把"沙盒额外加的所有视觉提示"
 *   (顶部黄条 / 引导 spotlight / 右下任务卡 / 看教程按钮 / 撒花等)全部藏起来,
 *   让画面看上去跟"真实代理工作台"一模一样, 同时背后跑沙盒 mock 数据.
 *
 * 控制方式(浏览器 console):
 *   __screenshotMode.on()   开启 (会刷)
 *   __screenshotMode.off()  关闭 (会刷)
 *   __screenshotMode.is()   查询
 *
 * 实现:
 *   localStorage key 'omnirank_screenshot_mode' = '1' 开 · 其它/缺失 关
 *   通过自定义事件 + storage 事件触发订阅者刷新
 *   各沙盒 UI 组件读 hook → 截图模式开时 return null 提前退出
 *
 * 注意:
 *   - 不影响沙盒数据加载逻辑 · 拦截器照常返 mock
 *   - 不影响真实工作台 · 真实代理 localStorage 没设这个 key 也不受影响
 *   - 推荐配合沙盒一起开:沙盒(数据假) + 截图模式(UI 不漏沙盒标识)
 */
import { useEffect, useState } from 'react';

const STORAGE_KEY = 'omnirank_screenshot_mode';
const EVENT = 'screenshot-mode:change';

export function isScreenshotMode(): boolean {
    try { return localStorage.getItem(STORAGE_KEY) === '1'; }
    catch { return false; }
}

export function setScreenshotMode(on: boolean): void {
    try {
        if (on) localStorage.setItem(STORAGE_KEY, '1');
        else localStorage.removeItem(STORAGE_KEY);
        if (typeof window !== 'undefined') {
            window.dispatchEvent(new Event(EVENT));
        }
    } catch (e) {
        console.warn('[ScreenshotMode] set failed:', e);
    }
}

/** React Hook · 订阅截图模式变化, 自动 re-render */
export function useScreenshotMode(): boolean {
    const [on, setOn] = useState<boolean>(isScreenshotMode);
    useEffect(() => {
        const sync = () => setOn(isScreenshotMode());
        window.addEventListener(EVENT, sync);
        window.addEventListener('storage', sync); // 跨 tab 同步
        return () => {
            window.removeEventListener(EVENT, sync);
            window.removeEventListener('storage', sync);
        };
    }, []);
    return on;
}

/** 暴露 console 玩具 · 方便 demo_shot 拍摄时秒切 */
if (typeof window !== 'undefined') {
    (window as unknown as { __screenshotMode?: unknown }).__screenshotMode = {
        on:  () => { setScreenshotMode(true);  setTimeout(() => location.reload(), 100); },
        off: () => { setScreenshotMode(false); setTimeout(() => location.reload(), 100); },
        is:  () => isScreenshotMode(),
    };
}
