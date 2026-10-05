/**
 * [工单 2026-08-06 §4] `crypto.randomUUID()` 的底线安全替代。
 *
 * 🔴 为什么需要它 —— 工单 §4.4 的表把 `crypto.randomUUID` 归为「底线内安全」,
 * 依据是 Safari 15.4 起支持。**这条只对了一半**:Owner 拍的底线同时包含
 * `chrome >= 90` / `edge >= 90`,而 `crypto.randomUUID()` 落地于 **Chrome 92**。
 * 接上 eslint-plugin-compat 后它当场报出 33 处
 * “Crypto.randomUUID() is not supported in Edge 90, Chrome 90”。
 *
 * 两条出路:把底线抬到 Chrome 92,或者给一个兜底。底线是 Owner 拍的、改要回去问,
 * 所以走兜底 —— 既不动底线,也不靠「反正没人用 Chrome 90」这种口头豁免。
 *
 * 退化顺序(每一层都保持 UUID v4 形状,调用方不必分辨):
 *   1. `crypto.randomUUID()`        —— 现代浏览器
 *   2. `crypto.getRandomValues()`   —— Chrome 90/91、Safari 15.0-15.3 等
 *   3. `Math.random()`              —— 无 WebCrypto 的极老环境
 *
 * ⚠️ 第 3 层不是密码学安全的。本函数只用于**幂等 ID / 草稿 ID / 请求追踪 ID**;
 * 任何涉及资金、鉴权、token 的随机数都不许走这里。
 */
const HEX: string[] = Array.from({ length: 256 }, (_, i) => (i + 0x100).toString(16).slice(1));

function fromBytes(bytes: Uint8Array): string {
  // RFC 4122 v4:第 7 字节高 4 位固定 0100,第 9 字节高 2 位固定 10
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const h = HEX;
  return (
    h[bytes[0]] + h[bytes[1]] + h[bytes[2]] + h[bytes[3]] + '-' +
    h[bytes[4]] + h[bytes[5]] + '-' +
    h[bytes[6]] + h[bytes[7]] + '-' +
    h[bytes[8]] + h[bytes[9]] + '-' +
    h[bytes[10]] + h[bytes[11]] + h[bytes[12]] + h[bytes[13]] + h[bytes[14]] + h[bytes[15]]
  );
}

export function safeRandomUUID(): string {
  const c: Crypto | undefined = (globalThis as { crypto?: Crypto }).crypto;
  try {
    if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  } catch {
    /* 某些 webview 里 randomUUID 存在但在非安全上下文调用会抛 —— 落到下一层 */
  }
  try {
    if (c && typeof c.getRandomValues === 'function') {
      return fromBytes(c.getRandomValues(new Uint8Array(16)));
    }
  } catch {
    /* 落到下一层 */
  }
  const bytes = new Uint8Array(16);
  for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  return fromBytes(bytes);
}

export default safeRandomUUID;
