/**
 * wechatJsapi.ts · 微信 JSAPI 支付前端 helper
 * 微信 JSAPI 支付(服务号 AppID 由后端配置下发)
 *
 * 流程:
 *   1. ensureOpenid() - 检查 localStorage 缓存 / 拿不到时跳网页授权
 *   2. createJsapiOrder(orderId, openid) - 调后端 /wallet/wechat-jsapi/create
 *   3. invokeWxPay(invokeParams) - 调起 wx.requestPayment
 *   4. payJsapi(orderId) - 上述 3 步整合便捷入口
 */
import { authFetch } from '@/lib/api';

const OPENID_CACHE_KEY = 'omnirank_wx_openid';

/** 微信 JS SDK / wx 全局对象类型(在公众号网页环境内) */
declare global {
  interface Window {
    WeixinJSBridge?: {
      invoke: (
        method: string,
        params: Record<string, unknown>,
        callback: (res: { err_msg?: string; errMsg?: string }) => void,
      ) => void;
    };
  }
}

export interface JsapiInvokeParams {
  appId: string;
  timeStamp: string;
  nonceStr: string;
  package: string;
  signType: 'RSA';
  paySign: string;
}

/** 检测当前环境是否在微信公众号内(需公众号 webview · 否则 WeixinJSBridge 不存在) */
export function isInWechatBrowser(): boolean {
  if (typeof navigator === 'undefined') return false;
  const ua = navigator.userAgent.toLowerCase();
  return ua.includes('micromessenger');
}

/** 获取缓存 openid · 没有时返 null */
export function getCachedOpenid(): string | null {
  try {
    return localStorage.getItem(OPENID_CACHE_KEY);
  } catch {
    return null;
  }
}

export function setCachedOpenid(openid: string): void {
  try {
    localStorage.setItem(OPENID_CACHE_KEY, openid);
  } catch { /* ignore */ }
}

/**
 * 跳转服务号网页授权 · 拿 code → 后端换 openid
 *
 * 用法:
 *   - 在 RechargePage 用户选 "微信 JSAPI" 但无 openid 缓存时调
 *   - redirectUri 必须在公众号 "网页授权域名" 白名单(omnirank.top)
 *   - state 透传(回调后用)
 */
export async function startOauthFlow(redirectUri: string, state: string = ''): Promise<void> {
  const res = await authFetch(
    `/api/wallet/wechat-jsapi/oauth-url?redirect_uri=${encodeURIComponent(redirectUri)}&state=${encodeURIComponent(state)}`,
  );
  const data = await res.json();
  if (!data?.success || !data?.data?.oauth_url) {
    throw new Error(data?.detail || '获取授权链接失败');
  }
  window.location.href = data.data.oauth_url;
}

/** 用授权 code 换 openid · 缓存到 localStorage */
export async function exchangeOpenid(code: string): Promise<string> {
  const res = await authFetch('/api/wallet/wechat-jsapi/exchange-openid', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  });
  const data = await res.json();
  const openid = data?.data?.openid;
  if (!openid) {
    throw new Error(data?.detail || '换 openid 失败');
  }
  setCachedOpenid(openid);
  return openid;
}

/** 创建 JSAPI 订单 · 返调起参数 */
export async function createJsapiOrder(
  orderId: string,
  openid: string,
): Promise<JsapiInvokeParams> {
  const res = await authFetch('/api/wallet/wechat-jsapi/create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ order_id: orderId, openid }),
  });
  const data = await res.json();
  if (!data?.success || !data?.data?.invoke_params) {
    throw new Error(data?.detail || 'JSAPI 下单失败');
  }
  return data.data.invoke_params as JsapiInvokeParams;
}

function waitForWeixinJSBridge(timeoutMs = 5000): Promise<void> {
  return new Promise((resolve, reject) => {
    if (window.WeixinJSBridge) {
      resolve();
      return;
    }

    const timer = window.setTimeout(() => {
      document.removeEventListener('WeixinJSBridgeReady', onReady);
      reject(new Error('当前不在微信公众号环境 · WeixinJSBridge 不可用'));
    }, timeoutMs);

    const onReady = () => {
      window.clearTimeout(timer);
      resolve();
    };

    document.addEventListener('WeixinJSBridgeReady', onReady, { once: true });
  });
}

/** 调起 wx.requestPayment · 返支付结果 */
export async function invokeWxPay(params: JsapiInvokeParams): Promise<{ ok: boolean; errMsg: string }> {
  await waitForWeixinJSBridge();
  const bridge = window.WeixinJSBridge;
  if (!bridge) {
    throw new Error('当前不在微信公众号环境 · WeixinJSBridge 不可用');
  }
  return new Promise((resolve) => {
    bridge.invoke(
      'getBrandWCPayRequest',
      {
        appId: params.appId,
        timeStamp: params.timeStamp,
        nonceStr: params.nonceStr,
        package: params.package,
        signType: params.signType,
        paySign: params.paySign,
      },
      (res) => {
        const errMsg = res.err_msg || res.errMsg || '';
        // 'get_brand_wcpay_request:ok' = 支付成功
        if (errMsg === 'get_brand_wcpay_request:ok') {
          resolve({ ok: true, errMsg });
        } else if (errMsg === 'get_brand_wcpay_request:cancel') {
          resolve({ ok: false, errMsg: '用户取消支付' });
        } else {
          resolve({ ok: false, errMsg: errMsg || '支付失败' });
        }
      },
    );
  });
}

/**
 * 一键支付 · 整合 openid 流程 + 下单 + 调起
 *
 * Args:
 *   orderId - 已 create 的充值 order_id(POST /wallet/recharge 返回的 order.id)
 *   redirectUri - 无 openid 时跳授权用 · 默认当前页
 */
export async function payJsapi(orderId: string, redirectUri?: string): Promise<{ ok: boolean; errMsg: string }> {
  if (!isInWechatBrowser()) {
    throw new Error('请在微信公众号内打开本页面 · JSAPI 支付仅微信 webview 可用');
  }
  let openid = getCachedOpenid();
  if (!openid) {
    // 无 openid · 跳授权(不返回 · 用户授权后回到 redirectUri 再调本函数)
    const back = redirectUri || window.location.href;
    await startOauthFlow(back, orderId);
    // startOauthFlow 会跳转 · 不会返回
    return { ok: false, errMsg: '跳转授权中' };
  }
  const params = await createJsapiOrder(orderId, openid);
  return invokeWxPay(params);
}
