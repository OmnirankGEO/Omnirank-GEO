/**
 * postLoginRedirect — 登录成功后决定跳转路径
 *
 * 🔴 [WO_260 · 2026-09-23] 三身份(普通用户 / 服务商 / 管理员)登录后一律落 `/`
 *    (在役首页,DefaultRouteGate 再按身份分流);显式深链仍然优先。
 *
 *   - 后端 `/api/c-end/settings/mode` 的 `recommended_route` **不再用于落地**:它只给得出两个值 ——
 *     `/`,或 `/m3/sales/today`(api/c_end_api.py 的 m3_enabled 分支:**所有管理员** + 灰度白名单服务商)。
 *     后者今天全靠 App.tsx 的 `/m3/*` → `/` 重定向接住;E3 删 M3 路由后它就是 404,管理员一登录就落空页。
 *     两个值今天的**实际落地都是 `/`**,前端直接落 `/` 行为不变,只是不再依赖一条要删的路由。
 *     (后端那条建议值的清理归 C,交付单已点名。)
 *   - 社媒粘性(`omnirank_c_end_mode === 's'` → 社媒首页)与老 C 端粘性(`'c'` → C 端对话)撤掉:
 *     老 C 端路由本就一律重定向到 `/`;社媒随 E3 删域。
 *   - `VITE_DEFAULT_APP === 'social'` 的默认路由分支撤掉(同上)。
 *   - 早退白名单只剩 `''` / `/` / `/login*`:三个调用方(登录 / 注册 / 协议更新)的默认值都已是 `/`,
 *     原来那几条老 C 端 / 社媒默认值早已无人传。
 *
 * 使用方法:
 *   import { determineTargetRoute } from '@/utils/postLoginRedirect';
 *   const target = await determineTargetRoute(fallbackRoute);
 *   navigate(target, { replace: true });
 */

import { authFetch } from '@/lib/api';

interface ModeResponse {
  user_id: number;
  agent_level: number;
  preferred_mode: 'c' | 'agent' | null;
  recommended_route: string;
  can_switch: boolean;
  m3_enabled?: boolean;
}

const HOME = '/';

/**
 * 登录后决定目标路由。
 * @param fallback 用户明确指定的回跳地址(深链)优先;`''` / `/` / `/login*` 不算指定
 * @returns 跳转路径
 */
export async function determineTargetRoute(fallback: string = HOME): Promise<string> {
  if (fallback && fallback !== HOME && !fallback.startsWith('/login')) {
    return fallback;
  }
  // 读一次后端身份,只为缓存服务商等级(给后续兜底用);落地不跟随 recommended_route(见文件头)。
  try {
    const res = await authFetch('/api/c-end/settings/mode', { method: 'GET' });
    if (!res.ok) {
      console.warn('determineTargetRoute: settings/mode API 失败,落首页');
      return HOME;
    }
    const data: ModeResponse = await res.json();
    if ((data.agent_level || 0) >= 1) {
      try { localStorage.setItem('omnirank_last_agent_level', String(data.agent_level || 0)); } catch { /* ignore */ }
    }
  } catch (e) {
    console.error('determineTargetRoute 异常:', e);
  }
  return HOME;
}

/**
 * 同步版(不等后端响应)— 仅用于极端情况。三身份一律落 `/`(同上)。
 */
export function determineTargetRouteSync(
  _user: { agent_level?: number; is_admin?: boolean } | null,
): string {
  return HOME;
}
