/**
 * wangjieTerminology — 报告 / 报价 / 诊断侧「人话」词典(WJ 横切 · 2026-05-31)
 *
 * 分工(别和 v35Terminology 混):
 *   - `v35Terminology.ts` 管 钱包 / 库存 / 定价 / 结算(paid/bonus/SKU/出厂价…三角色 HIDDEN)
 *   - 本词典管 报告 / 报价 / 诊断 的客户面人话(AI 体检 / AI 推荐频次 / 选词黑话 / 等级)
 *
 * 规则(已对齐 boss memory,勿擅改):
 *   - 出现率话术 `occurrencePhrase` 仅【报价页 / 报告摘要】用;
 *     监测 / 交付 / 客户门户 / 履约页用**精确百分比**(feedback_data_driven_after_quote 2026-05-25)
 *   - 屏上短标签统一叫「AI 推荐频次」(feedback 4 · MASTER §0.1)
 *   - 客户面禁技术黑话 / 供应商名(见 §4.3 客户面硬禁区)
 *
 * 接入点(批 1 报告净化 / 批 3 报告·报价·选词人话):
 *   SelectionPage 选词方法论、report_html_renderer 摘要(后端另有 Python 版)、TierSelector 套餐说明
 */

// ============================================================
// GEO 指标 · 屏上短标签(统一叫法 · SSOT)
// ============================================================
export const WJ = {
  diagnosis: 'AI 体检',          // 诊断 / GEO / 品牌体检 / AI搜索诊断
  occurrenceRate: 'AI 推荐频次', // 出现率 / SOV / 命中率 / 提及率
  coreKeyword: '主要优化词',     // 核心词 / 核心监控词
  coveredKeyword: '相关搜索参考',  // 覆盖词 / 长尾词 / 变体 · [B2 2026-06-05] 去"附赠/赠送"措辞
  cluster: '主题包',             // 聚类 / 簇
  superRedOcean: '超级红海词',    // §4.2 竞争极度饱和词(占比≥9成·命中测量上限)
} as const;

// ============================================================
// §4.2 完全红海词(竞争极度饱和)· 客户面话术(王姐口径 · 禁裸露 ≥90%/SOV · 用「9 成以上」)
// [2026-06-06 老板] 一期【标注清楚即可】:不裸显 ¥0·明确标「完全红海词·需深度报价」+ 不保证出现率;
//   真正的深度定价(尽力价/深搜真实篇数)= 二期【深度报价系统】。命中 → 报价页挂 🔴 标 + 警示条;普通报价确认链路不直接成交。
// ============================================================
export const SUPER_RED_OCEAN_COPY = {
  badge: '🔴 完全红海词 · 竞争极高 · 需深度报价',
  priceLabel: '需深度报价',
  warning:
    '这个词搜索结果几乎全是竞争对手(9 成以上),市场极度饱和。常规篇数很难保证效果,需要单独做「深度报价」评估真实投放量——这类词我们不承诺出现率。深度报价即将上线;当前可先联系为您服务的人单独沟通方案。',
  confirmTitle: (kw: string) => `「${kw}」需要单独深度报价`,
  confirmBody:
    '这个词竞争对手几乎占满搜索结果,真实竞争规模超出常规测量范围。按常规篇数投放,效果可能远低于其他词,我们无法承诺出现率或达标。当前不会计入普通报价总价,请联系为您服务的人单独核价。是否先标记出来?',
  confirmOk: '我已知悉 · 先标记待报价',
  confirmCancel: '暂不选择',
} as const;

// [2026-06-06 一期] 通用 ¥0/无价 兜底提示(非完全红海·如聚类取价缺数据)· 杜绝裸显「¥0」
export const ZERO_PRICE_COPY = {
  label: '需核价',
  hint: 'AI 数据稀缺,暂无法自动定价 · 请联系为您服务的人单独核价',
} as const;

// [报价解释层一期 2026-06-13] guarantee_unavailable(护栏剥保证价)· 仅服务商端可见(已从客户面脱敏)。
//   搜索项入门价也超过自动承诺范围,或多重杠杆叠乘 → 给参考价不作保证价 · 发客户前人工核一次。
export const GUARANTEE_UNAVAILABLE_COPY = {
  badge: '⚠️ 参考价 · 建议人工核价',
  label: '参考价 · 待人工核',
  reason:
    '这个搜索项的入门价也超过自动承诺范围,或同时叠加了多项高风险因素。目前给到的是参考价、不作为保证价。发给客户前,建议你按真实投放量人工确认一次定价(客户侧只会看到「需核价」,不会看到这条内部说明)。',
} as const;

// [P0-D 2026-06-14] 信任资产/引用难度因子人话(代理端 WhyThisPrice + 客户面转人话可复用)。
//   ⚠️ 绝不展示 trust_asset_score / citation_readiness_score 等裸分数(已后端脱敏)· 只用资产 label。
//   中性双向措辞:有背书 → 引用基础好;缺背书 → 需补外部证据(已按更高内容量估算)· 不承诺效果。
export const TRUST_ASSET_COPY = {
  rowTitle: '可信背书',
  verifiedPrefix: '已检测到',
  verifiedSuffix: ',AI 引用基础较好',
  missingPrefix: '公开可信背书还不足',
  missingNeed: (labels: string[]) =>
    labels.length ? `(可补充${labels.slice(0, 3).join('、')})` : '',
  missingSuffix: ',AI 引用需要先补外部证据,本方案已按更高内容量估算',
  /** 构建一行人话(verified/missing 均为已转好的资产 label 数组;都空 → 返回 null) */
  phrase(verified?: string[], missing?: string[]): string | null {
    const v = (verified || []).filter(Boolean);
    const m = (missing || []).filter(Boolean);
    if (!v.length && !m.length) return null;
    const parts: string[] = [];
    if (v.length) parts.push(`${this.verifiedPrefix}${v.slice(0, 3).join('、')}${this.verifiedSuffix}`);
    if (m.length) parts.push(`${this.missingPrefix}${this.missingNeed(m)}${this.missingSuffix}`);
    return parts.join(';') + '。';
  },
} as const;

// ============================================================
// 出现率 → 销售话术
// ⚠️ 仅【报价页 / 报告摘要】用;监测 / 交付 / 门户用精确 %（数据驱动）
// [2026-06-05] 阈值比例对齐(前后端统一 · 同 probability.describeProbability / transparent_pricing.describe_probability)
//   三档目标出现率 50/65/75 → 问2次1次 / 问3次2次 / 问4次3次(各异)· 与「目标出现率 X%」并存(老板定稿)
// ============================================================
export function occurrencePhrase(pct: number): string {
  if (!Number.isFinite(pct)) return '';
  if (pct >= 90) return '几乎每次问都能看到你';
  if (pct >= 73) return '问 4 次大概 3 次能看到你';
  if (pct >= 58) return '问 3 次大概 2 次能看到你';
  if (pct >= 43) return '问 2 次大概 1 次能看到你';
  if (pct > 0) return '偶尔能看到你';
  return '目前几乎搜不到你';
}

// ============================================================
// 客户面黑话替换(报告 / 报价 / 选词页 · WJ-12/31)
// 用法:wjDejargon(text) 把整段文本里的黑话换成人话
// ============================================================
export const WJ_JARGON: Record<string, string> = {
  SEM竞价: '别人买这个词的广告出价',
  SEM: '别人买这个词的广告出价',
  转化漏斗: '客户从看到你到联系你的过程',
  品牌漏斗: '客户从看到你到联系你的过程',
  三层漏斗: '客户从看到你到联系你的过程',
  三维数据交叉验证: '从搜索量、竞争、成交意向三方面挑词',
  三维交叉验证: '从搜索量、竞争、成交意向三方面挑词',
  AI搜索竞争度: '同行在 AI 里抢这个词的激烈程度',
  持有版: '当前未投放状态',
};

/** 把一段客户面文本里的黑话替换成人话(批 3 选词/报价摘要用) */
export function wjDejargon(text: string): string {
  if (!text) return text;
  let out = text;
  // 长词优先,避免 "SEM竞价" 被 "SEM" 先替换截断
  for (const k of Object.keys(WJ_JARGON).sort((a, b) => b.length - a.length)) {
    out = out.split(k).join(WJ_JARGON[k]);
  }
  return out;
}
