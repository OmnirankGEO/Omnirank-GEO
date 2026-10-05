/**
 * verify-svideo-upload-wiring.mjs — 短视频开闸包前端门禁(工单 2026-07-30 T4/T5)
 *
 * 仓库没有前端单测框架(无 vitest/jest),verify-*.mjs 是既有的前端断言载体,
 * 本脚本沿用同一形态并挂进 `npm run build`。任一违规即 exit 1。
 *
 * 🔴 诚实说明本门禁的能力边界：它是**结构断言**，不是行为测试。
 *    "上传真能传上去" 只能靠开上传闸后的真环境联调(工单 §6 第 3 步)来证，
 *    本脚本只保证代码没有退化回"点了只弹一句即将开放"的骨架态。
 *
 * 锁住的是最容易悄悄退化回去的几条：
 *   ① 视频上传走「POST 我们自己的 /upload-video 中转 → 落 videoUrl」，
 *      不是 toast.info('短视频上传即将开放') 骨架态，也不许回退成直传
 *      🔴 2026-08-01 架构反转:原为「必须浏览器直传、绝不过我们服务器」，
 *         该规则前提(直传可用)已被实测证伪 —— 渠道存储桶 CORS 白名单无我方域，
 *         直传对我方用户 100% 失败。改手段不改用意，详见 ① 处长注释。
 *   ② XHR + 进度回调 + **必须流式**(直接 send(file)、裸字节、禁整体物化)——
 *      这三条承接旧规则「1GB 别拖垮容器」的真实用意，比"禁止中转"更贴近要防的事
 *   ③ 🔴 前端**绝不**碰任何密钥字段：出现 AccessKeySecret / alibabaToken2 即红
 *      （照抄媒介盒子把密钥发到浏览器 = 把第三方密钥分发给我们全部用户）
 *   ④ 视频线与图片线不许串:视频路径不许出现 upload-cover，且必须含 upload-video
 *   ⑤ 发布 body 带 article_type，且图文模式 video_url 留空、image_urls 有值
 *   ⑥ 标题 45 字硬上限真的拦住提交（不是只染个红字）
 *   ⑦ 话题关键词提示语用井号，不是逗号（实测远端是 # 分隔）
 *   ⑧ 「AI图文生成视频」明确不接 —— 出现即红
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const root = path.resolve(process.cwd(), 'src')
const PANEL = 'pages/Publishing/ShortVideoPanel.tsx'
const failures = []
const fail = (msg) => failures.push(msg)

if (!fs.existsSync(path.join(root, PANEL))) {
  console.error(`❌ 缺文件: ${PANEL}`)
  process.exit(1)
}

/** 只看代码,不看注释 —— 否则解释根因的注释会把门禁自己误伤掉。 */
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .filter((line) => {
      const t = line.trim()
      return !t.startsWith('//') && !t.startsWith('*')
    })
    .join('\n')
}

const raw = fs.readFileSync(path.join(root, PANEL), 'utf8')
const src = stripComments(raw)

// ① 视频上传走【服务端中转】三步:打我们自己的 /upload-video → 落 videoUrl → 不退回骨架
//
// 🔴 架构反转(2026-08-01 Owner 拍板 · Deploy-CTO 执行):
//   原断言 = 「必须浏览器直传对方存储、绝不过我们服务器」(2026-07-30 定)。
//   那条规则的【前提已被实测证伪】:渠道存储桶 的 CORS 白名单没有我们的域,
//   bucket 又不归我们 —— 浏览器直传对我方用户 **100% 失败**,功能因此一直不可用。
//   所以改的是【手段】,不是【用意】:
//     旧用意「1GB 别拖垮容器」→ 由下面 ② 的「必须流式、不得物化」正面承担,
//     比"禁止中转"更贴近真正要防的事(内存爆掉),而且中转是唯一可行路径。
//   ⚠️ 本条【刻意不向后兼容】:旧的直传写法从此会被判红 —— 那是有意的,
//      因为直传已被证明不可能成功,留着只会让人再走一遍死路。
//   🔁 若哪天对方把我们的域加进 CORS 白名单,再议是否回退。
if (!/short-video\/upload-video/.test(src)) fail('视频没有走服务端中转端点 /short-video/upload-video')
if (!/xhr\.open\('POST',\s*`\/api\/meijiehezi\/short-video\/upload-video/.test(src)) {
  fail('没有用 XHR 把文件 POST 到我们自己的中转端点')
}
if (!/setVideoUrl\(url\)/.test(src)) fail('上传完成后没有把地址落进 videoUrl —— 等于没传')
if (/toast\.info\('短视频上传即将开放/.test(src)) fail('视频上传又退回「即将开放」骨架态')
if (/upload-policy/.test(src)) fail('仍残留直传取凭据 /upload-policy —— 直传对我方用户 100% 失败,不许回退')

// ② 必须【流式】+ 裸字节 + 进度回调 —— 这三条承接旧规则「1GB 别拖垮容器」的真实用意
if (!/xhr\.send\(file\)/.test(src)) {
  fail('没有直接 xhr.send(file) —— 必须把 File 原样交给浏览器流式发出')
}
if (!/setRequestHeader\('Content-Type',\s*'application\/octet-stream'\)/.test(src)) {
  fail('中转请求体不是裸字节 application/octet-stream —— 后端就没法把请求流直接接到 OSS')
}
for (const materialize of ['arrayBuffer()', 'readAsArrayBuffer', 'new Blob([']) {
  if (src.includes(materialize)) {
    fail(`出现整体物化 ${materialize} —— 1GB 会把内存吃爆(这正是旧规则真正要防的事)`)
  }
}
if (!/xhr\.upload\.onprogress/.test(src)) fail('1GB 上传没有进度回调，用户会以为卡死')

// ③ 🔴 前端绝不碰密钥
for (const forbidden of ['AccessKeySecret', 'accessKeySecret', 'alibabaToken2', 'SecurityToken =']) {
  if (src.includes(forbidden)) {
    fail(`前端出现密钥相关标识 ${forbidden} —— 签名必须留在后端，绝不下发密钥到浏览器`)
  }
}

// ④ 视频路径与图片路径不许串线(切块判,别整文件扫)
const videoFn = src.slice(src.indexOf('const handleFile'), src.indexOf('const uploadImage'))
if (!videoFn) fail('切不出视频路径(handleFile/uploadImage 边界被改名?)—— 本条判据失效,不许放行')
if (videoFn && /upload-cover/.test(videoFn)) {
  fail('视频路径里出现了图片端点 upload-cover —— 两条线串了')
}
// 🔴 中转的 XHR 在 handleFile **之前**的辅助函数 postVideoViaServer 里,不在 handleFile 体内。
//   第一版我直接在 videoFn 里找 upload-video → 正向就红了(变异自验当场抓到)。
//   正确判法:handleFile 必须调用中转函数,且中转函数体内确实打 upload-video。
if (videoFn && !/postVideoViaServer\(/.test(videoFn)) {
  fail('视频路径没有调用服务端中转 postVideoViaServer —— 中转没接进视频这条线')
}
const relayFn = src.slice(src.indexOf('const postVideoViaServer'), src.indexOf('const handleFile'))
if (!relayFn) fail('切不出中转函数 postVideoViaServer —— 本条判据失效,不许放行')
if (relayFn && !/short-video\/upload-video/.test(relayFn)) {
  fail('中转函数没有打 /short-video/upload-video')
}
if (!/short-video\/upload-cover/.test(src)) fail('图片缺后端代理降级路径')

// ⑤ 发布 body 的模式字段
if (!/article_type:\s*ARTICLE_TYPE_BY_METHOD\[publishMethod\]/.test(src)) {
  fail('发布 body 没有按发布方式传 article_type —— 图文模式切不过去')
}
if (!/video_url:\s*isNote\s*\?\s*''\s*:\s*videoUrl/.test(src)) {
  fail('图文模式没有把 video_url 留空 —— 后端会拒「图文带视频」')
}
if (!/image_urls:\s*isNote\s*\?\s*noteImages\s*:\s*\[\]/.test(src)) {
  fail('视频模式没有把 image_urls 清空 —— 后端会拒「视频带图片」')
}
if (!/ARTICLE_TYPE_BY_METHOD[\s\S]{0,80}video:\s*1[\s\S]{0,40}article:\s*3/.test(src)) {
  fail('发布方式到 article_type 的映射不是 video→1 / article→3')
}

// ⑥ 45 字硬上限真的拦住提交（canSubmit 里带 !titleOverHard，不是只染红字）
if (!/const\s+titleOverHard\s*=\s*title\.length\s*>\s*TITLE_HARD_LIMIT/.test(src)) {
  fail('没有 45 字硬上限判定')
}
if (!/const\s+canSubmit\s*=[^\n]*!titleOverHard/.test(src)) {
  fail('45 字硬上限没有拦住提交按钮 —— 只染红字等于没拦，用户照样发出去被远端报错')
}
if (!/TITLE_HARD_LIMIT\s*=\s*45/.test(src) || !/TITLE_SOFT_LIMIT\s*=\s*30/.test(src)) {
  fail('标题软硬上限不是 30 / 45')
}

// ⑦ 话题关键词是井号分隔
const kwPlaceholder = src.match(/placeholder="([^"]*井号[^"]*)"/)
if (!kwPlaceholder) fail('话题关键词提示语没写「井号」—— 实测远端是 # 分隔，写成逗号会让用户填错')

// ⑧ 明确不接的东西
if (/AI图文生成视频/.test(src)) fail('接了「AI图文生成视频」—— 明确不在本包范围')

if (failures.length) {
  console.error('❌ 短视频前端接线门禁未通过:')
  failures.forEach((m) => console.error(`   - ${m}`))
  process.exit(1)
}
console.log('✅ verify-svideo-upload-wiring: 短视频上传/图文接线门禁通过')
