/**
 * M3 灰度发布 feature flags(老板红线 · 必须 default off)
 *
 * 老板原话(2026-04-25 发布红线):
 *   "M3 必须挂到独立本地/灰度路由 /m3 /m3/sales /m3/delivery /m3/customer/:brandId"
 *   "所有 M3 新入口必须受 feature flag 或灰度开关控制"
 *   "上线前必须有回滚路径:关闭 feature flag 后 · 用户仍回到现有前端"
 *
 * 当前实现(本地/灰度):
 *   - 读 import.meta.env.VITE_M3_ENABLED · default 'false'
 *   - 读 localStorage 'omnirank_m3_enabled' 灰度名单(代理 opt-in)
 *   - 任一开启即 enabled
 *
 * 上线流程:
 *   1. 本地开发(.env.local 设 VITE_M3_ENABLED=true)
 *   2. 灰度名单(代理用浏览器 console 设 localStorage.setItem('omnirank_m3_enabled','true'))
 *   3. 全量(.env 设 VITE_M3_ENABLED=true 或后端 settings 控制)
 *
 * 回滚:
 *   - 关 VITE_M3_ENABLED + 让代理清 localStorage → 用户回到现有 Dashboard
 */

export type M3Flag = 'm3_workspace_enabled' | 'm3_sales_enabled' | 'm3_delivery_enabled';

const ENV_FLAG = 'VITE_M3_ENABLED';
const STORAGE_KEY = 'omnirank_m3_enabled';

interface ImportMetaEnv {
  [k: string]: string | boolean | undefined;
}

function readEnv(): boolean {
  try {
    // Vite 注入 import.meta.env
    const env = (import.meta as unknown as { env?: ImportMetaEnv }).env;
    if (!env) return false;
    const v = env[ENV_FLAG];
    return v === 'true' || v === true;
  } catch {
    return false;
  }
}

function readLocal(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    return localStorage.getItem(STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

/**
 * 主开关 · 控制 /m3 路由组是否激活
 */
export function isM3Enabled(): boolean {
  return readEnv() || readLocal();
}

/**
 * 子开关(预留 · 当前所有子开关都跟 isM3Enabled 走 · 后续可拆细)
 */
export function isFlagEnabled(_flag: M3Flag): boolean {
  return isM3Enabled();
}

/**
 * 给代理 / 测试人员开启灰度(浏览器 console 调用)
 *
 * @example
 *   import { enableM3Locally } from '@/services/m3'
 *   enableM3Locally()  // window.location.reload() 后生效
 */
export function enableM3Locally(): void {
  try {
    localStorage.setItem(STORAGE_KEY, 'true');
    // eslint-disable-next-line no-console
    console.log('[M3] 灰度已启用 · 刷新页面后访问 /m3');
  } catch {
    /* ignore */
  }
}

export function disableM3Locally(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
    // eslint-disable-next-line no-console
    console.log('[M3] 灰度已关闭 · 刷新页面回到现有前端');
  } catch {
    /* ignore */
  }
}

// 浏览器 console 暴露(开发期方便代理 opt-in)
if (typeof window !== 'undefined') {
  (window as unknown as { enableM3?: () => void; disableM3?: () => void }).enableM3 = enableM3Locally;
  (window as unknown as { enableM3?: () => void; disableM3?: () => void }).disableM3 = disableM3Locally;
}
