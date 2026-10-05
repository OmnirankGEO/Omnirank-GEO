export type RechargeChannel = 'auto';

export interface JsapiPayCandidate {
  actual_channel?: string | null;
  needs_openid?: boolean | null;
}

export function isWechatUserAgent(userAgent: string | undefined | null): boolean {
  return /MicroMessenger/i.test(userAgent || '');
}

export function selectRechargeChannel(_userAgent: string | undefined | null): RechargeChannel {
  return 'auto';
}

export function shouldStartJsapiPayment(info: JsapiPayCandidate | null, isWechat: boolean): boolean {
  return Boolean(isWechat && info?.actual_channel === 'wechat_jsapi' && info?.needs_openid);
}

export function buildCleanOauthReturnUrl(rawUrl: string): string {
  const url = new URL(rawUrl);
  url.searchParams.delete('code');
  url.searchParams.delete('state');
  const query = url.searchParams.toString();
  return url.origin + url.pathname + (query ? '?' + query : '') + url.hash;
}
