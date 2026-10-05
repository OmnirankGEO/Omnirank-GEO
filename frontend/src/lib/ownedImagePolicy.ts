/**
 * 自有图库图片判定 —— 前后端同一份口径的 TypeScript 侧实现。
 *
 * [返修单 REWORK_T4_SAFEMARKDOWN_IMAGE_2026-07-29 §3/§4]
 *
 * `SafeMarkdown` 挡外部图片的设计是对的（跟踪像素风险真实存在），问题是它把
 * 「外部不可信图片」和「我们自己图库里的图」一刀切了 —— 连 `src` 都没接收。
 * 这里按**来源**区分：同源 + 路径前缀白名单放行，其它一切维持文字占位。
 *
 * 🔴 判定口径（路径前缀、允许协议、brand 段规则、测试向量）读的是
 * `config/owned_image_asset_policy.json` —— 与后端 `services/owned_image_policy.py`
 * **同一份文件**。不许任何一侧再写第二份白名单：
 * `SafeMarkdown.tsx` 里那句「与后端 bleach 剥离 <img> 对齐」的注释，
 * 正是"各写一份、然后一起错"的产物。
 */
import policy from '../../../config/owned_image_asset_policy.json'

/** URL 里一旦出现这些字符就直接拒（与 safeHttpUrl 同款：控制字符 + 反斜杠）。 */
const UNSAFE_CHARS_RE = /[\u0000-\u0020\u007f\\]/

const PATH_PREFIX: string = policy.path_prefix
const ALLOWED_SCHEMES: string[] = policy.allowed_schemes
const BRAND_SEGMENT_RE = new RegExp(policy.brand_segment_pattern)

export const OWNED_IMAGE_POLICY_VERSION: string = policy.version

function hostOf(origin: string): string {
  return origin.replace(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//, '').replace(/\/+$/, '').toLowerCase()
}

/**
 * 这个 URL 是不是我们自己图库里的图？
 *
 * `siteOrigin` 只在测试里显式传；浏览器里走 `window.location.origin`。
 */
export function isOwnedImageUrl(value: string | undefined, siteOrigin?: string): boolean {
  const url = String(value ?? '')
  if (!url || UNSAFE_CHARS_RE.test(url)) return false
  // 协议相对 URL：跟着当前协议走外部域名，是绕过同源判定的经典口子。
  if (url.startsWith('//')) return false

  let path: string
  if (url.startsWith('/')) {
    path = url // 相对路径 = 同源，天然满足 origin 条件
  } else {
    let parsed: URL
    try {
      parsed = new URL(url)
    } catch {
      return false
    }
    const scheme = parsed.protocol.replace(/:$/, '').toLowerCase()
    if (!ALLOWED_SCHEMES.includes(scheme)) return false
    if (parsed.username || parsed.password) return false
    const expected =
      siteOrigin ?? (typeof window !== 'undefined' ? window.location.origin : '')
    if (!expected) return false
    // 同源按 host 比，忽略协议差异（http/https 同一站点都算自有）。
    if (hostOf(parsed.origin) !== hostOf(expected)) return false
    path = parsed.pathname
  }

  // 查询串/片段不参与路径判定，也不允许夹带（保持判定面最小）。
  if (path.includes('?') || path.includes('#')) return false
  if (!path.startsWith(PATH_PREFIX)) return false
  if (path.includes('..')) return false
  const lowered = path.toLowerCase()
  if (lowered.includes('%2f') || lowered.includes('%5c')) return false

  const segments = path.slice(PATH_PREFIX.length).split('/')
  if (segments.length !== 2) return false
  const [brandSegment, filename] = segments
  return BRAND_SEGMENT_RE.test(brandSegment) && filename.trim().length > 0
}

/** 判别锁用：共享测试向量（与后端读同一份，任一侧漂移即转红）。 */
export const OWNED_IMAGE_TEST_VECTORS: { url: string; owned: boolean; note: string }[] =
  policy.test_vectors
