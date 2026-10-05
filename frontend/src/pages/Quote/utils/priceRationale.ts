/**
 * priceRationale — 报价解释层一期「为什么是这个价」人话构建器(代理端)
 *
 * 纯函数 · 无 React/lucide 依赖 · 只把【代理可见】字段翻成王姐口径人话。
 * 客户端这些字段(effective_competition/value_score/intent 等)已被后端脱敏拿不到,
 * 故本构建器仅在代理端报价页(OnlineQuoteFlow · 带 JWT)调用。
 * 规则:不裸露 value_multiplier=1.7 / effective_competition=58 这类机器字段 · 不显工厂单篇成本数字。
 */
import { GUARANTEE_UNAVAILABLE_COPY, SUPER_RED_OCEAN_COPY, TRUST_ASSET_COPY } from '@/lib/wangjieTerminology'
import { GLOBAL_RATIO_RAW, clampRatio, planMediaMix } from '@/lib/quoteMediaMix'

export type RationaleTier = 'entry' | 'standard' | 'flagship'
export type RationaleTone = 'info' | 'warn'

export interface RationaleRow {
  kind: 'articles' | 'value' | 'cost' | 'markup' | 'trust' | 'recheck'
  title: string
  body: string
}

export interface RationaleRisk {
  tone: RationaleTone
  body: string
}

export interface PriceRationaleInput {
  intent?: string
  funnel_stage?: string
  value_score?: number
  effective_competition?: number
  super_red_ocean?: boolean
  guarantee_unavailable?: boolean
  should_quote?: boolean
  needs_review?: boolean
  // [P0-D] 信任资产人话 label(已转人话·无裸分数)· 后端 trust_verified_labels/trust_missing_labels
  trust_verified_labels?: string[]
  trust_missing_labels?: string[]
  entry?: { articles?: number }
  standard?: { articles?: number }
  flagship?: { articles?: number }
}

const TIER_LABEL: Record<RationaleTier, string> = { entry: '入门版', standard: '标准版', flagship: '旗舰版' }

/** 竞争强度 → 人话(不裸露 effective_competition 数字) */
function competitionPhrase(comp?: number): string {
  const c = typeof comp === 'number' && Number.isFinite(comp) ? comp : 0
  if (c >= 50) return 'AI 搜索结果里已有大量同类内容,想被稳定引用需要较高的内容密度'
  if (c >= 20) return 'AI 搜索结果里相关内容较多,需要一定的内容量才容易被引用'
  if (c >= 8) return 'AI 搜索结果里有一些相关内容,常规内容量即可建立存在感'
  return 'AI 搜索结果里相关内容还不多,少量优质内容就能先占位'
}

/** 商业价值 → 人话(不裸露 value_multiplier · 用意图/阶段/价值分综合) */
function valuePhrase(intent?: string, funnel?: string, value?: number): string {
  const i = (intent || '').toLowerCase()
  const f = (funnel || '').toLowerCase()
  if (i === 'transactional' || i === 'commercial' || f === 'decision')
    return '客户搜这个词时更接近「咨询 / 下单」,离成交近,所以系统给了更高的价值权重'
  if (i === 'comparison' || f === 'consideration')
    return '客户搜这个词时还在「对比挑选」,价值居中——抢下来有机会影响他的最终选择'
  if (i === 'local')
    return '这个词带本地意向,搜的人通常就在你的服务范围内,转化机会不错'
  if (i === 'informational' || f === 'awareness')
    return '客户搜这个词更多是「了解信息」,离成交较远,价值偏基础'
  const v = typeof value === 'number' ? value : 0
  if (v >= 6) return '这个词的成交价值较高,系统给了更高权重'
  if (v >= 3) return '这个词的成交价值适中'
  return '这个词偏基础信息,价值权重较低'
}

/**
 * 「为什么是这个价」里**永远不许出现**的措辞 —— 只有这两处是真的泄露点。
 *
 * 🔴 [Owner 2026-08-11 亲裁 · R5] 处置从「藏起来」改成「删掉」:
 *   · 「单篇成本」(含公式「基础成本 = 建议篇数 × 单篇成本」)—— 服务商的进货价,删;
 *   · 「你账号的报价系数」—— 内部加价倍率,删;
 *   · 剩下的一句只是**基本的成本构成**(内容成本按媒体/写作行情估算、最终报价还会
 *     结合商业价值),客户看见也无所谓 → **不再受眼睛控制,直接常显**。
 *
 * 演变轨迹(别再往回改):
 *   R2 把整块挂到隐私态后面 → R4 删掉「报价系数」整行、去掉隐藏态的星号占位行
 *   → R5 连「单篇成本」措辞一起删,剩下的常显。
 *   真正的隐藏是**这几个词根本不存在**,不是挂在眼睛后面等人点开,更不是留个星号
 *   提醒客户来问(此地无银三百两)。
 *   ⚠️ 仍受眼睛控制的是**顶栏**那三样(默认报价系数 / 毛利 / 单篇成本输入框)与
 *      本次报价系数编辑器 —— 那才是数值本身。这里只是文案。
 */
export const RATIONALE_FORBIDDEN_PHRASES: readonly string[] = [
  '单篇成本',
  '你账号的报价系数',
  '报价系数',
  /*
   * [#225 §8 a1-3 文案锁] 换算不许被讲成**效果承诺**。
   *   「5 条覆盖等于 1 篇锚点」这类说法把「初始组合先验」偷换成「效果等价」,
   *   而我们从来没有任何数据支持后者(裁定书 v2 §7 六条「不得宣称」)。
   *
   * 🔴 「倍」与倍数数字**不在这张表里**,在下面那张 `CONVERSION_*` 里 ——
   *    工单 §158 的主语是「**换算文案**」。本文件「投放节奏」那一行有一句
   *    在产的老文案讲的是**初始组合先验**(见本文件 :114 的注释),
   *    一刀切会把它也圈进来,而那是另一件事。
   *    (这次缩域已单独报 Review:那句老文案要不要一起改由他判。)
   */
  '等于',
  '抵',
  '效果相同',
  '效果一样',
  /* 内部运营参数与桶内部名:连服务商页面都只在内部审核视图出现,客户面绝对不出。 */
  'bps',
  'focus_media_anchor',
  'industry_platform_coverage',
  'douyin_doubao_only',
]

/** 换算文案**必含**的两个词(工单 §8 a1-3):它们把估算说成估算。 */
export const RATIONALE_REQUIRED_HEDGES: readonly string[] = ['约', '左右']

/**
 * 只管**换算那句话**的禁用词(工单 §158 主语 =「换算文案」)。
 *
 * 🔴 一旦在换算里给出倍数,读者会自己做「N 条 ≈ 1 篇」的效果等价换算 ——
 *    而换算表是**运营口径**(一槽按哪类媒体发几条),不是效果当量。
 */
export const CONVERSION_FORBIDDEN_PHRASES: readonly string[] = ['倍', '×', 'x ']

/**
 * 构建「为什么是这个价」人话拆解 + 风险提示。
 *
 * 🔴 本函数**没有** revealed 参数,也不该有(R5):这块里已经不存在需要遮的内容,
 *    加回一个开关就意味着有人又把敏感措辞塞回来了。
 *
 * @param kw   代理端关键词(含 intent/funnel_stage/value_score/effective_competition/状态标/各档篇数)
 * @param tier 当前档位(entry/standard/flagship)
 */
/**
 * 交付口径的人话名。
 *
 * 🔴 只有这三档,与后端 `PERSPECTIVES` 逐字对应;认不出来就**不说**这句话 ——
 *    宁可少一句,也不能编一个口径名出来。
 * 🔴 文案锁(工单 §8 a1-3):这里不许出现倍数、「倍」「等于」「抵」「效果」、
 *    bps、桶内部名(focus_media_anchor 之类)。
 */
export const PERSPECTIVE_WORD: Record<string, string> = {
  self_media: '自媒体',
  portal: '门户',
  mixed: '混合',
}

export function buildPriceRationale(
  kw: PriceRationaleInput,
  tier: RationaleTier,
  mixOpts: {
    ratioUsed?: number; douyinShare?: number; ratioSource?: string;
    /* [#225 a1 ②] 交付口径与按口径估算的**条**数 —— 都由服务端给,前端不算。
       取 `/api/quotes/{id}/media-mix` 回包的 `delivery_perspective`
       与 `posts_estimate_total`(在 C 尖 2f3307a3e 上按 sha 核过)。
       🔴 `posts_per_slot_bps` **不传也不显示**:内部运营参数(工单 §6)。 */
    deliveryPerspective?: string;
    postsEstimateTotal?: number;
  } = {},
): { rows: RationaleRow[]; risk: RationaleRisk | null } {
  const articles = kw[tier]?.articles ?? kw.standard?.articles ?? 0

  /* [WO_QUOTE_MEDIA_MIX_DYNAMIC 2026-08-12 v2] 「发布篇数」升级为「投放组合」。
     🔴 交付条数**一个字都没改** —— 这里只是把同一个数讲清楚:它会怎么分配到
        重点媒体锚点 / 行业与平台覆盖 / 抖音图文(豆包专项)。
        总条数 SSOT 仍是 tools/pricing_bands.py,本包对它零差异(工单 §1.2)。
     🔴 比例是**初始组合先验**,不是效果等价 —— 文案里不许出现"N 篇覆盖等于 1 篇锚点"。 */
  const mix = planMediaMix(
    articles,
    clampRatio(mixOpts.ratioUsed ?? GLOBAL_RATIO_RAW),
    mixOpts.douyinShare ?? 0,
  )
  const mixParts = [
    `重点媒体锚点 ${mix.focusMediaAnchor} 槽`,
    `行业与平台覆盖 ${mix.industryPlatformCoverage} 槽`,
  ]
  if (mix.douyinDoubaoOnly > 0) mixParts.push(`抖音图文 ${mix.douyinDoubaoOnly} 组`)
  /*
   * 🔴 [#225 a1 ②] 上面几个数是**槽**(合同分配),口径不改它们;原来标「篇」,
   *    而同屏下面「本次交付 N 槽」会和它打架(文案锁:同屏槽/篇不混)。
   * 🔴 逐桶**不带条数**:`self_media` 口径下所有槽都记作覆盖,服务端给的
   *    `posts_estimate` 是 {锚点 0, 覆盖 总×5, 抖音 0} —— 逐桶写条数会出现
   *    「重点媒体锚点 2 槽(0 条)」这种读不懂的东西。条数只在**合计**上说一次。
   * 🔴 条数读服务端的 `posts_estimate_total`,前端不乘不加:换算带取整,
   *    自己算一遍就会和别处差 1,而没有任何东西会报错。
   */
  const perspectiveWord = PERSPECTIVE_WORD[String(mixOpts.deliveryPerspective || '')] || ''
  const postsTotal = Number.isFinite(Number(mixOpts.postsEstimateTotal))
    ? Number(mixOpts.postsEstimateTotal) : null
  /*
   * 🔴 「本单」二字不能省 —— 截图上逮到的:这句挂在**每个关键词**的卡片里,
   *    引的却是**整单**合计。画面上是「本次交付 4 槽 · 按自媒体发布约需 29 条左右」,
   *    4 是这个词的槽、29 是整单的条,两个不同量级的数并排,
   *    读起来就是「这 4 槽要发 29 条」—— 一个谁都不会去核的错。
   *    静态判据看不见这件事(两个数各自都对),是把真页面截出来才看见的。
   */
  const postsPhrase = (perspectiveWord && postsTotal !== null && postsTotal > 0)
    ? ` · 本单按${perspectiveWord}发布约需 ${postsTotal} 条左右(运营口径)`
    : ''

  const rows: RationaleRow[] = [
    {
      kind: 'articles',
      title: '投放组合',
      body: `${mixParts.join(' · ')} · 本次交付 ${articles} 槽${postsPhrase}。${competitionPhrase(kw.effective_competition)}。`,
    },
    {
      kind: 'recheck',
      title: '投放节奏',
      /* 🔴 这段是**客户可以看**的价值说明,不受隐私眼睛控制;里面不许出现
            R / anchor_n / unclassified / ratio_source 这类内部字段名(工单 §6)。

         🔴 [2026-08-12 Review 判红后订正] 句子**由实际取到的比例来源决定**,
            不再无条件写"系统会按行业和目标 AI 调整"。
            判红原文:后端算法零生产调用、前端固定全局比例、抖音恒 0,
            文案却宣称按行业和目标 AI 调整 —— 那是**承诺了没接的行为**。
            现在:取到行业级数据才说"已按你所在行业调整";没取到就如实说是全行业平均估算。
            ⚠️ 想把这句改回无条件版本之前,先确认比例真的按行业接进来了。 */
      body: (mixOpts.ratioSource === 'industry_engine' || mixOpts.ratioSource === 'industry')
        ? '这一组配比已按你所在行业的 AI 引用数据算出：行业与平台覆盖内容多于重点媒体锚点，先用重点媒体建立可检索锚点，再用多个平台覆盖客户真实问题。首批完成后于第 7/14/30 天回查，剩余条数转向仍未补齐的问题。'
        : '当前按全行业平均值估算：行业与平台覆盖内容初始通常约为重点媒体锚点的 2 倍。攒到你所在行业的引用数据后会自动按行业调整。首批完成后于第 7/14/30 天回查，剩余条数转向仍未补齐的问题。',
    },
    {
      kind: 'value',
      title: '商业价值',
      body: `${valuePhrase(kw.intent, kw.funnel_stage, kw.value_score)}。`,
    },
    {
      // 🔴 常显。标题与正文都**不许**出现 RATIONALE_FORBIDDEN_PHRASES 里的词 ——
      //    只讲成本大致由什么决定,不讲进货价数字,也不提加价倍率。
      kind: 'cost',
      title: '成本构成',
      body: '内容成本按当前媒体 / 写作行情估算,最终报价还会结合这个词的商业价值。',
    },
  ]

  // [P0-D] 信任资产/引用难度(有数据才插一行 · 中性双向 · 不裸分数 · 不承诺效果)
  const trustBody = TRUST_ASSET_COPY.phrase(kw.trust_verified_labels, kw.trust_missing_labels)
  if (trustBody) {
    rows.push({ kind: 'trust', title: TRUST_ASSET_COPY.rowTitle, body: trustBody })
  }

  // 风险提示(优先级:超红海 > 放飞参考价 > 信息型 > 需复核)
  let risk: RationaleRisk | null = null
  if (kw.super_red_ocean) {
    risk = { tone: 'warn', body: SUPER_RED_OCEAN_COPY.warning }
  } else if (kw.guarantee_unavailable) {
    risk = { tone: 'warn', body: GUARANTEE_UNAVAILABLE_COPY.reason }
  } else if (kw.should_quote === false) {
    risk = { tone: 'info', body: '这个词更偏「信息了解」型,系统不建议作为付费报价词——可放进内容规划,但别单独卖钱。' }
  } else if (kw.needs_review) {
    risk = { tone: 'warn', body: '这个词的数据证据不太完整(或两个模型判断有分歧),建议发给客户前人工确认一次定价。' }
  }
  return { rows, risk }
}
