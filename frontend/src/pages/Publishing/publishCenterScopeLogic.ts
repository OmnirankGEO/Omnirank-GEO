/**
 * publishCenterScopeLogic — 发布中心「文章作用域」的纯决策层。
 *
 * 为什么要把这段从 PublishCenter.tsx 抽出来(2026-07-30 工单 T1/T2):
 *   老板报「未分发 (5)，点全选变成已选 26」。根因是同一屏上**两个集合**在打架 ——
 *   「全选」读 `articles`(项目全集),四个分组读 `availableArticles`(经 preselected /
 *   onlyOptimize / 购物车三道过滤后的子集)。数字对不上是必然,不是偶发。
 *   选中不可见项 → 直接进购物车 → 一次性扣费(生产实测:单媒体中位 3900 算力,
 *   一张订单均 4820),所以这是资金面的 P1,不是文案问题。
 *
 * 抽出来的第二个理由:分组/隐藏计数是**可断言的行为**,留在 JSX 里就只能靠
 * 源码串断言(换皮即绕过、重构就误伤)。这里出来的每个数字都能被锁直接喂数据核。
 *
 * 🔴 分类优先级(已发布 > 发布中 > 已拒稿 > 未分发)是 CTO-15.23 多轮修出来的,
 *    本模块**原样搬运**,不重新发明。fallback 分支(stats 未加载时退回
 *    published/rejected 两个 id 集合)也一并保留。
 */

export interface ScopeArticle {
  id: number;
  article_id?: number;
  /** 这篇来自哪一份报价(#210)。不带 = 老数据 / 没标,按"不被报价筛选排除"处理。 */
  quoteId?: number;
  publication_eligible?: boolean;
  is_optimize?: boolean;
  title?: string;
}

/**
 * 与 /api/meijiehezi/article-publish-stats 的主状态**同域**。
 *
 * 🔴 R4 §D:这里少一个值,后端返回该值的文章就会**从四个 tab 里同时消失** ——
 *    四个分组都是 `st.status === X` 的等值过滤,没有任何一支收容"未知状态"。
 *    R2 给后端加了 `reported_success_unverified` 却没加这一格,那 26 篇于是
 *    既不在"已分发"也不在"未分发",在发布中心整个不见了(比标错还坏:
 *    标错还能看见并纠正,消失连"我的文章去哪了"都答不上来)。
 *
 * 🔴 增删这个联合类型时,`SCOPE_BUCKET_BY_STATUS` 必须同步 ——
 *    有一条奇偶锁在守「每个后端 primary 值都有前端桶」。
 */
export type ScopeStatStatus =
  | 'published'
  | 'in_progress'
  | 'reported_success_unverified'
  | 'rejected'
  | 'none';

/** 分组名(`PartitionResult` 上那几个数组的键)。 */
export type ScopeBucket = 'published' | 'inProgress' | 'reportedUnverified' | 'rejected' | 'unpublished';

/**
 * 后端主状态 → 前端分组的**全射**映射。
 *
 * 🔴 这张表是奇偶锁的锚点:锁从 `api/meijiehezi_api.py` 里机械取出所有可能的
 *    `primary` 取值,逐个要求在这里有桶。任何一边加值而另一边没跟上,当场红。
 *    (「文章从四个 tab 消失」这种 bug 在 UI 上是静默的 —— 没有报错、没有空态,
 *      只是数字少了几个,所以必须由锁来发现,不能靠肉眼。)
 */
export const SCOPE_BUCKET_BY_STATUS: Record<ScopeStatStatus, ScopeBucket> = {
  published: 'published',
  in_progress: 'inProgress',
  reported_success_unverified: 'reportedUnverified',
  rejected: 'rejected',
  none: 'unpublished',
};

export interface ScopeStat {
  status: ScopeStatStatus;
  [extra: string]: unknown;
}

export interface PartitionInput<T extends ScopeArticle = ScopeArticle> {
  articles: T[];
  /** URL ?articles=1,2,3 带进来的预选集合(写作大厅「去发布」按钮的主路径)。 */
  preselected?: number[];
  /**
   * 只看某一份报价(#210)。`null` / 不传 = **全部报价**(默认)。
   *
   * 🔴 默认必须是"全部":真客户那次就是因为一次只看一份,
   *    另一份下的 18 篇(含他刚补的)一篇都看不见。
   *    筛选是**收窄**,收窄得由用户主动做,不能是缺省。
   */
  quoteFilter?: number | null;
  onlyOptimize?: boolean;
  cartArticleIds?: Iterable<number>;
  stats?: Map<number, ScopeStat>;
  /**
   * 「别重复发」的占位集(端点 `article_ids`)。
   *
   * 🔴 它**不是**「已发布」的集合:端点里 UNION 了自助发布的回执成功行
   *    (R2 §③ + Owner 明确保留的防重复发布占位)。拿它当已发布,
   *    未核实的自报就直接进了已发布桶。
   */
  publishedArticleIds?: Iterable<number>;
  /** [R6 补充①] 已**核实**发布(端点 `verified_published_article_ids`)。 */
  verifiedPublishedArticleIds?: Iterable<number>;
  /** [R6 补充①] 回执成功但未核实(端点 `reported_success_unverified_article_ids`)。 */
  reportedUnverifiedArticleIds?: Iterable<number>;
  rejectedArticleIds?: Iterable<number>;
}

export interface HiddenBreakdown {
  /**
   * 被「只看某一份报价」过滤掉的篇数(#210)。
   * 🔴 必须进这本账:加一道过滤而不记账,用户就又回到"我的文章去哪了"。
   */
  byQuote: number;
  /** 被 URL ?articles= 预选过滤掉的篇数。 */
  byPreselect: number;
  /** 被「只看优化文章」过滤掉的篇数。 */
  byOptimize: number;
  /** 已在购物车里、因此从四个分组中移除的篇数。 */
  byCart: number;
  total: number;
}

/**
 * 🔴 泛型透传调用方的文章类型(`ArticleItem`)。
 * 早先写成固定的 `ScopeArticle[]` 再在调用点 `as ArticleItem[]` 硬转 —— 四个分组转过去了,
 * `filteredArticles` 漏了,self 模式那支拿不到 `keyword` / `style_family` 等字段,
 * **`tsc -b` 一跑就是 9 个 TS2339**。硬转本来就是把类型问题往后推,这里改成泛型透传。
 */
export interface PartitionResult<T extends ScopeArticle = ScopeArticle> {
  /** 项目全集 —— 只为「全选读错了哪个集合」这件事可被断言而带出来。 */
  allArticles: T[];
  filteredArticles: T[];
  availableArticles: T[];
  published: T[];
  inProgress: T[];
  /** [R4 §D] 浏览器回报过成功、服务端没核实过 —— 独立第五桶,不并进任何一组。 */
  reportedUnverified: T[];
  rejected: T[];
  unpublished: T[];
  hidden: HiddenBreakdown;
}

/** 文章在发布链路上的稳定主键:优先 article_id,回落 topic_id。 */
export function articleKey(a: ScopeArticle): number {
  return a.article_id || a.id;
}

function toSet(v: Iterable<number> | undefined): Set<number> {
  return v instanceof Set ? v : new Set(v || []);
}

/**
 * 一次算清「过滤链 + 四分组 + 每一道过滤各藏了几篇」。
 *
 * 过滤顺序必须与线上一致:articles → preselected → onlyOptimize → 购物车。
 * 顺序变了,`hidden` 的归因就会错(同一篇可能同时满足两道)。
 */
export function partitionArticles<T extends ScopeArticle>(input: PartitionInput<T>): PartitionResult<T> {
  const articles = input.articles || [];
  const preselected = input.preselected || [];
  const cartIds = toSet(input.cartArticleIds);
  const publishedIds = toSet(input.publishedArticleIds);
  const verifiedIds = toSet(input.verifiedPublishedArticleIds);
  const reportedIds = toSet(input.reportedUnverifiedArticleIds);
  const rejectedIds = toSet(input.rejectedArticleIds);
  const stats = input.stats;

  /*
   * 🔴 报价筛选排在**最前面**:它回答的是"我们在看这个客户的哪几份报价",
   *    是作用域;后面几道(预选 / 只看优化 / 购物车)才是在作用域内挑。
   *    顺序变了 `hidden` 的归因就会错(同一篇可能同时满足两道)。
   */
  const qf = typeof input.quoteFilter === 'number' && input.quoteFilter > 0 ? input.quoteFilter : null;
  const afterQuote = qf === null ? articles : articles.filter(a => a.quoteId === qf);
  const afterPreselect = preselected.length > 0
    ? afterQuote.filter(a => preselected.includes(a.id) || preselected.includes(a.article_id as number))
    : afterQuote;
  const afterOptimize = input.onlyOptimize
    ? afterPreselect.filter(a => a.is_optimize)
    : afterPreselect;
  const availableArticles = afterOptimize.filter(a => !cartIds.has(articleKey(a)));

  const stat = (a: T) => stats?.get(articleKey(a));

  // [CTO-15.23 2026-05-18] 分类优先级原样保留:已发布 > 发布中 > 已拒稿 > 未分发
  const published = availableArticles.filter(a => {
    const st = stat(a);
    if (st) return st.status === 'published';
    // 🔴 R6 补充①:**fallback 这条路也要过核实位**。
    //    这条路不是边角:首屏(stats 还没到)、stats 请求失败、切客户的空窗,
    //    走的全是它 —— 也就是说用户看到的第一屏恰恰是没过核实位的那一屏。
    //    `publishedIds` 里 UNION 了自报回执,所以要减掉"回报过但没核实"的那批;
    //    `verifiedIds` 单独并回来,保证已核实的自报照样算已发布。
    const k = articleKey(a);
    if (verifiedIds.has(k)) return true;
    return publishedIds.has(k) && !reportedIds.has(k);
  });
  const inProgress = availableArticles.filter(a => {
    const st = stat(a);
    return !!st && st.status === 'in_progress';
  });
  // [R4 §D] 第五桶。
  // 🔴 R6 补充① 订正:R4 写这段时的判断是"fallback 那条路拿不出核实位,
  //    所以第五桶只在 stats 到位时成立"。那个判断**错了** —— 端点
  //    `/published-articles` 从 R2 §② 起就同时返回了
  //    `verified_published_article_ids` 与 `reported_success_unverified_article_ids`,
  //    前端只是**从没接过**。于是 fallback 路上这些文章既不在第五桶、
  //    又因为在占位集里而被算进"已发布" —— 静默的旧路。现在两条路都过核实位。
  const reportedUnverified = availableArticles.filter(a => {
    const st = stat(a);
    if (st) return st.status === 'reported_success_unverified';
    return reportedIds.has(articleKey(a)) && !verifiedIds.has(articleKey(a));
  });
  const rejected = availableArticles.filter(a => {
    const st = stat(a);
    if (st) return st.status === 'rejected';
    return rejectedIds.has(articleKey(a)) && !publishedIds.has(articleKey(a));
  });
  const unpublished = availableArticles.filter(a => {
    const st = stat(a);
    if (st) return st.status === 'none';
    return !publishedIds.has(articleKey(a)) && !rejectedIds.has(articleKey(a));
  });

  /* 🔴 每一道各算各的差,别拿 `articles.length` 去减后面几道 ——
     那样同一篇会被记进两道,`total` 就会大于真的被藏起来的篇数。 */
  const byQuote = articles.length - afterQuote.length;
  const byPreselect = afterQuote.length - afterPreselect.length;
  const byOptimize = afterPreselect.length - afterOptimize.length;
  const byCart = afterOptimize.length - availableArticles.length;

  return {
    allArticles: articles,
    filteredArticles: afterOptimize,
    availableArticles,
    published,
    inProgress,
    reportedUnverified,
    rejected,
    unpublished,
    hidden: {
      byQuote,
      byPreselect,
      byOptimize,
      byCart,
      total: byQuote + byPreselect + byOptimize + byCart,
    },
  };
}

export interface SelectAllTarget {
  /** 真正会被勾上的 id —— 未分发**且**已过发布审核。 */
  ids: number[];
  /** 🔴 **点了会勾几篇**。按钮上的主数字就是它。 */
  selectableCount: number;
  /** 🔴 **这组有几篇**。与「未分发 (N)」分组标签同源同数。 */
  groupCount: number;
  label: string;
}

/**
 * 「全选」的候选集 —— 只能是当前屏幕上那批未分发文章,且只勾已过发布审核的那批
 * (没过审的行本来就点不动,勾上等于制造一个提交必失败的选中态)。
 *
 * 🔴 入参刻意收整个 `PartitionResult` 而不是一个数组:选**哪个集合**正是老板踩的那个 BUG
 * (原实现读 `articles` 全集),把选择权留在这里,锁才能直接证伪"读错集合"。
 *
 * 🔴🔴 **按钮数字 ≠ 分组标签数字,这两个语义本来就不同**(2026-07-30 复审订正):
 *   - 分组标签 `未分发 (5)` 答的是「**这组有几篇**」;
 *   - 按钮答的是「**点了会勾几篇**」。
 *   本单要消灭的病正是"看到的数 ≠ 选到的数",强求两者相等只是把病从分组挪到按钮上
 *   —— 那等于让按钮说谎。两数不等时显示 `全选未分发 (3/5)`,用户一眼看出另外 2 篇是
 *   **未过审**、不是被系统漏掉了;相等时不必啰嗦,显示 `全选未分发 (5)`。
 */
export function selectAllUnpublishedTarget(scope: PartitionResult<ScopeArticle>): SelectAllTarget {
  const list = scope.unpublished || [];
  const ids = list.filter(a => a.publication_eligible).map(a => a.id);
  const selectableCount = ids.length;
  const groupCount = list.length;
  return {
    ids,
    selectableCount,
    groupCount,
    label: selectableCount === groupCount
      ? `全选未分发 (${groupCount})`
      : `全选未分发 (${selectableCount}/${groupCount})`,
  };
}

/** 全选按钮的 checked 判据:候选集非空且全部已在选中集里。 */
export function isSelectAllChecked(selected: Iterable<number>, target: SelectAllTarget): boolean {
  const set = toSet(selected);
  return target.ids.length > 0 && target.ids.every(id => set.has(id));
}

/**
 * 点一下「全选未分发」之后的新选中集。
 *
 * 只加/只减候选集里的 id —— 用户在别的分组手点的勾选**不被清掉**
 * (「已发布的文章可以单独勾选」是保留能力,见 self 模式注释)。
 */
export function toggleSelectAllUnpublished(
  selected: Iterable<number>,
  target: SelectAllTarget,
): Set<number> {
  const next = new Set(toSet(selected));
  if (isSelectAllChecked(next, target)) {
    target.ids.forEach(id => next.delete(id));
  } else {
    target.ids.forEach(id => next.add(id));
  }
  return next;
}

export interface CartDistributedItem {
  articleId: number;
  articleTitle: string;
  /**
   * 🔴 R6 补充①:`reported_unverified` 是**新的第三态**,不许再并进 `published`。
   *    并进去的后果在 `PublishRiskConfirmDialog` 里逐字可见:
   *    对一篇从没被核实过的文章印「已发布」。要明示"你发过别再发",
   *    用它自己的话说就够了,不需要冒充已发布。
   */
  state: 'published' | 'in_progress' | 'reported_unverified';
}

/**
 * 购物车里有哪些文章**此前已经分发过**。
 *
 * 为什么需要(§1.4 生产实测):后端去重只挡「同文章 + 同媒体」——
 * 全库 0 行同文同媒并存活跃项,证明那道确实在挡;但「已发布文章换一家媒体」
 * 是**合法的一文多投**,24 张订单就是这么产生的,真实扣费 29,640 算力。
 * 所以这条路必须由用户**看见**再确认,不能静默扣。
 */
export function cartArticlesAlreadyDistributed(
  cart: Array<{ articleId: number; articleTitle?: string }>,
  opts: {
    stats?: Map<number, ScopeStat>;
    publishedArticleIds?: Iterable<number>;
    /** [R6 补充①] fallback 路的核实位。 */
    verifiedPublishedArticleIds?: Iterable<number>;
    reportedUnverifiedArticleIds?: Iterable<number>;
  } = {},
): CartDistributedItem[] {
  const publishedIds = toSet(opts.publishedArticleIds);
  const verifiedIds = toSet(opts.verifiedPublishedArticleIds);
  const reportedIds = toSet(opts.reportedUnverifiedArticleIds);
  const out: CartDistributedItem[] = [];
  for (const entry of cart || []) {
    const st = opts.stats?.get(entry.articleId);
    let state: CartDistributedItem['state'] | null = null;
    if (st) {
      if (st.status === 'published') state = 'published';
      else if (st.status === 'in_progress') state = 'in_progress';
      // [R4 §D] 回报过成功但没核实 —— 提交前**照样要明示**。
      // 它没核实不代表没发出去;这里要防的是"用户不知道自己发过就又发一遍"
      // 然后被真扣一次费(§1.4 实测换媒体重投合法且真扣费)。宁可多提示一次。
      else if (st.status === 'reported_success_unverified') state = 'reported_unverified';
    } else if (reportedIds.has(entry.articleId) && !verifiedIds.has(entry.articleId)) {
      state = 'reported_unverified';
    } else if (verifiedIds.has(entry.articleId)) {
      state = 'published';
    } else if (publishedIds.has(entry.articleId)) {
      state = 'published';
    }
    if (state) {
      out.push({ articleId: entry.articleId, articleTitle: entry.articleTitle || '', state });
    }
  }
  return out;
}
