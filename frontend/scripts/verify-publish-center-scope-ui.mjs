/**
 * verify-publish-center-scope-ui.mjs — PublishCenter.tsx 的**接线**门禁。
 *
 * 与 test-publish-center-scope.mjs 互补:那个管"喂真数据出来对不对"(行为),
 * 这个管"页面到底有没有把决策交给那套逻辑"(结构)。
 * 行为锁够不着接线层 —— 把 `selectAllUnpublishedTarget(scope)` 改成
 * `(articles)`,逻辑模块本身仍然全绿,BUG 却原地复活。这就是本文件存在的理由。
 *
 * 断的是**结构性质**(哪个符号被用在哪个位置 / 有没有 length 守卫),
 * 不是文案串 —— 换个措辞不该转红,换掉决策来源必须转红。
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const FILE = path.join(process.cwd(), 'src/pages/Publishing/PublishCenter.tsx')
const src = fs.readFileSync(FILE, 'utf8')

let failed = 0
const ok = (cond, what) => {
  if (cond) console.log(`✅ ${what}`)
  else { console.error(`❌ ${what}`); failed++ }
}

/**
 * 剥注释。**反向断言必须打在剥完之后的源码上**。
 *
 * 理由是这个文件里的中文注释专门在讲被废掉的旧做法(「加 minHeight=120px 防挤压」
 * 「占 60% 高度」「calc(100dvh-7rem)」),不剥的话
 * `ok(!/minHeight:\s*'\d+px'/)` 这类反向断言会被**注释自己**喂成恒红 ——
 * 代码明明改干净了却报红,比不测更糟(要么被无视,要么逼着人把注释删掉,史料就没了)。
 *
 * 只剥两种:块注释 `/* … *\/`(含 JSX 的 `{/* … *\/}`)和**独占一行**的 `//` 注释。
 * 不碰行内 `//`,免得误伤 `https://` 这类字符串。
 */
const stripComments = (s) => s
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^[ \t]*\/\/.*$/gm, '')

const bare = stripComments(src)

// ── 剥注释器的已知答案自检:不做这个,上面所有反向断言的判别力都是我"觉得"的。
//    正向:塞进注释里的禁词必须被剥掉(否则反向断言恒红)。
//    反向:同样的词出现在**代码**里必须留下(否则反向断言恒真 —— 那才是真正危险的一侧)。
{
  const probe = [
    "const probeA = { minHeight: '180px' };",   // 代码位:必须留下
    "// minHeight: '999px' 出现在独占行注释里",   // 注释位:必须剥掉
    "/* flex: '3 1 0%' 出现在块注释里 */",        // 注释位:必须剥掉
    "{/* 100dvh-7rem 出现在 JSX 注释里 */}",      // 注释位:必须剥掉
    "const probeB = 'calc(100dvh-7rem)';",       // 代码位:必须留下
  ].join('\n')
  const stripped = stripComments(probe)
  ok(stripped.includes("minHeight: '180px'"), '自检 剥注释器保留代码位的 minHeight(不过度剥 → 反向断言不会恒真)')
  ok(stripped.includes('calc(100dvh-7rem)'), '自检 剥注释器保留代码位的 100dvh-7rem')
  ok(!stripped.includes("'999px'"), '自检 剥注释器剥掉独占行 // 注释')
  ok(!stripped.includes("'3 1 0%'"), '自检 剥注释器剥掉块注释')
  ok(!stripped.includes('出现在 JSX 注释里'), '自检 剥注释器剥掉 JSX 注释')
  // 真源码上也要证明"确实剥到了东西",否则 stripComments 退化成 identity 也照样全绿
  ok(bare.length < src.length - 2000,
    `自检 真源码上确实剥掉了大量注释(${src.length - bare.length} 字符),不是 identity 函数`)
}

// ── 接线 1:全选的候选集必须来自 partitionArticles 的产物,不是 articles 全集
ok(/selectAllUnpublishedTarget\(\s*scope\s*\)/.test(src),
  '接线1 全选候选集取自 partitionArticles 的结果(不是 articles / availableArticles)')
ok(!/selectAllUnpublishedTarget\(\s*(articles|availableArticles|filteredArticles)\b/.test(src),
  '接线1 反向 全选没有被接回任何"更大的集合"')

// ── 接线 2:全选按钮的点击与勾选态都走那套逻辑,不许就地重写
ok(/onClick=\{\(\)\s*=>\s*\{\s*setBatchArticleIds\(prev\s*=>\s*toggleSelectAllUnpublished\(/.test(src),
  '接线2 全选点击走 toggleSelectAllUnpublished')
ok(/checked=\{isSelectAllChecked\(batchArticleIds,\s*selectAllTarget\)\}/.test(src),
  '接线2 勾选态走 isSelectAllChecked')
ok(!/articles\.filter\(a\s*=>\s*a\.publication_eligible\)/.test(src),
  '接线2 反向 页面里不再有"对 articles 全集过滤 eligible"的残留')

// ── 接线 3:四个状态必须走顶部分段筛选器,计数不带 length 守卫
//
// [WO-PUBCENTER-LAYOUT 2026-08-04] 承载体从 ArticleGroupSection(四段堆叠)换成
// ArticleStatusFilter(一行分段控件)。锁的语义不变、而且更严:
// 原来只要求"空组也渲染标题",现在要求"四个计数同时可见"。
//
// 🔴 断言打在**剥掉注释之后**的源码上。这个文件里的中文注释大量出现
// `minHeight` / `length > 0` 这类字样(讲的正是被废掉的旧做法),
// 不剥注释的话反向断言会被自己的注释喂成恒红。
ok(/<ArticleStatusFilter/.test(bare), '接线3 四个状态走顶部分段筛选器')
for (const [name, key] of [
  ['unpublishedArticles', 'unpublished'],
  ['inProgressArticles', 'inProgress'],
  ['publishedArticles', 'published'],
  ['rejectedArticles', 'rejected'],
]) {
  ok(new RegExp(`key:\\s*'${key}'[\\s\\S]{0,160}count:\\s*${name}\\.length`).test(bare),
    `接线3 ${key} 的计数直接取 ${name}.length,没有中间加工`)
  ok(!new RegExp(`\\{${name}\\.length\\s*>\\s*0\\s*&&`).test(bare),
    `接线3 反向 ${key} 没有被 length>0 守卫包起来(空即不渲染是 2026-07-30 T2 修过的病)`)
}
// 反向:切换筛选只能改筛选,不许顺手动购物车/勾选态(工单 §4 L2「切换不许清空已勾选」)
ok(/onChange=\{setArticleStatusFilter\}/.test(bare),
  '接线3 反向 切换筛选的 onChange 就是 setArticleStatusFilter 本身,没有夹带副作用')
// 反向:硬下限补丁不许再出现。2026-05-05 加 minHeight 是补丁,复发证明它解不了根因。
ok(!/minHeight:\s*'\d+px'/.test(bare),
  '接线3 反向 左栏不再有 minHeight 硬下限(不许用"加 minHeight"再打一次补丁)')
ok(!/flex:\s*'[23] 1 0%'/.test(bare),
  '接线3 反向 四个状态不再按 3/2/2/2 瓜分固定比例高度')

// ── 接线 3b:页面高度不许再自己算,必须跟外壳走(工单 §4 L1)
ok(!/100dvh-7rem/.test(bare),
  '接线3b 反向 写死的 calc(100dvh-7rem) 已清除(实测 Header 只有 48px,7rem=112px 差出的 64px 就是老板看到的底部黑边)')
ok(!/min-h-\[calc\(100dvh/.test(bare),
  '接线3b 反向 页面不再用 100dvh 自算高度(改由外壳定高 + h-full 继承)')

// ── 接线 3c:辅助面板合进底部抽屉,且**不许**移到右栏(老板已明确否掉)
ok(/<MediaAdviceDrawer/.test(bare), '接线3c 三块媒体建议走底部抽屉')
{
  // 结构判据:抽屉必须落在左栏那个 div 里(它带 data-left-pane),而不是右栏。
  // 用"左栏开标签位置 < 抽屉位置 < 右栏首个真实控件位置"三点定位 ——
  // 只 grep 到 `<MediaAdviceDrawer` 是没有判别力的:把它搬到右栏照样绿,
  // 而"搬到右栏"正是老板明确否掉的那个方案。
  //
  // 🔴 右栏锚点必须挑**代码里的真东西**(软文/自媒体切换按钮的文案),
  // 不能挑 `{/* ===== 右侧：代发 ===== */}` 那种注释 —— bare 已经把注释剥了,
  // 拿被剥掉的串当锚点 = indexOf 恒 -1 = 断言恒假。
  const leftAt = bare.indexOf('data-left-pane')
  const drawerAt = bare.indexOf('<MediaAdviceDrawer')
  const rightAt = bare.indexOf('软文价格')
  ok(leftAt > -1, '接线3c 左栏锚点 data-left-pane 存在')
  ok(rightAt > -1, '接线3c 右栏锚点「软文价格」存在(锚点没被剥注释误删)')
  ok(drawerAt > leftAt && rightAt > drawerAt,
    '接线3c 反向 抽屉挂在左栏内(在 data-left-pane 之后、右栏起始之前)—— 老板否掉了"移到右边"')
}

// ── 接线 4:隐藏提示挂在真实的 hidden 明细上
ok(/<HiddenArticlesNotice[\s\S]{0,200}hidden=\{scope\.hidden\}/.test(src),
  '接线4 隐藏提示行喂的是 partitionArticles 算出来的 hidden 明细')

// ── 接线 5:严审拦截走面板,且 message 不再被降级成 toast
ok(/<StrictPresubmitPanel/.test(src), '接线5 严审拦截渲染 StrictPresubmitPanel')
ok(/parseStrictPresubmitBlock\(detail\)/.test(src), '接线5 提交失败分支里先解析预审载荷')
ok(!/lazyToast\.error\(\s*detail\.message[\s\S]{0,40}严审/.test(src),
  '接线5 反向 严审拦截不再退回单条 toast')

// ── 接线 6:提交层把"已分发过"的清单交给确认弹窗
ok(/alreadyDistributed=\{cartAlreadyDistributed\}/.test(src),
  '接线6 风险确认弹窗拿到"清单里哪些此前已分发过"')

console.log(failed ? `\n🔴 ${failed} 条接线断言未通过` : '\n✅ 发布中心接线门禁全绿')
process.exit(failed ? 1 : 0)
