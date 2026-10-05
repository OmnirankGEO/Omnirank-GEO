/**
 * 功能定价表
 * 38 项功能积分定价展示，按类别分组
 */
import { useState, useMemo } from 'react';
import { Search, Loader2 } from 'lucide-react';
// [P0-5 2026-07-12] 价目改为只走 PricingContext(SSOT)· 删本页自带 fetch + 硬编码 PRICING_DATA
import { usePricing } from '@/context/PricingContext';
import { cn } from '@/lib/utils';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
// CTO-15.20 v3 · G1 桥接 banner(/feature-pricing 价目表 · 客户上下文下访问时显主按钮 + 回 M3)
import { BridgeBanner } from '@/components/workbench/BridgeBanner';

// ========== 类型定义 ==========

interface PricingItem {
  feature_code: string;
  feature_name: string;
  cost_points: number;
  cost_compute: number;
  requires_paid_points: boolean;
  category: string;
}

type Category = '全部' | 'GEO' | '社媒IP' | 'AI员工' | '媒体发布' | '免费';

// [去社媒 · 2026-06-04 老板拍板] /feature-pricing = GEO 板块价目表 · 只显示 GEO 侧功能。
// 社媒/IP 创作(选题/脚本/人设/语料/AI员工/面试建档/找热点等)归社媒工作台 · 此页前端隐藏
// (社媒板块代码 / DB / billing 0 改 · 仅展示过滤)· 留「→ 社媒工作台」一个友链入口。
const CATEGORIES: Category[] = ['全部', 'GEO', '媒体发布'];
// 此页保留的 GEO 侧分类(其余 社媒IP / AI员工 / 免费 = 社媒板块功能 · 前端隐藏)
const GEO_PRICING_CATEGORIES = new Set<string>(['GEO', '媒体发布']);
// 额外按 code 隐藏:social_diagnosis(名「社媒专项诊断」· 老板:也当社媒内容去掉)
//
// 🔴 [商业边界裁决 2026-08-10] 追加两类:
//   ① 打包商品**永久下架** —— Owner 裁决:平台只提供能力与算力,
//      打包/组合/定价是服务商自己的生意。`monitor_month_10` / `rank_alert`
//      从未卖出过(生产 0 笔),后端已同步软下架 + 去种子。
//   ② 托管套餐**隐藏待未来重开** —— Owner 2026-08-10 裁:历史遗留功能,
//      先下线入口,能力保留。`managed_*_recharge` 的代码/表/数据一律不动,
//      只是不给可达入口。恢复清单见
//      `docs/AI-CONTEXT/MANAGED_CAMPAIGN_RESTORE_2026-08-10.md`。
//
// ⚠️ 前端隐藏**不是闸**(用户可直接打 API)。真闸是 `feature_pricing.is_active=false`
//    与 `get_feature_pricing` 的 `WHERE is_active = TRUE`。
const HIDDEN_FEATURE_CODES = new Set<string>([
  'social_diagnosis',
  // ① 打包商品下架(不可恢复,除非 Owner 推翻裁决)
  'monitor_month_10',
  'rank_alert',
  // ② 托管入口隐藏(可恢复 —— 删掉这两行即可)
  'managed_campaign_recharge',
  'managed_brand_recharge',
]);
function isVisibleOnGeoPricing(item: PricingItem): boolean {
  return GEO_PRICING_CATEGORIES.has(item.category) && !HIDDEN_FEATURE_CODES.has(item.feature_code);
}

function inferCategory(code: string): string {
  if (!code) return '社媒IP';
  if (/^(full_|geo_|monitor|diagnosis|keyword_expand|article_|report_|rank_|placement)/.test(code)) return 'GEO';
  if (/^(media_|publish)/.test(code)) return '媒体发布';
  if (/^(meeting_|team_|employee)/.test(code)) return 'AI员工';
  if (/^(interview|persona|compatibility|corpus)/.test(code)) return '免费';
  const freeList = ['find_trending', 'persona_card', 'interview_full', 'interview_single', 'compatibility'];
  if (freeList.includes(code)) return '免费';
  return '社媒IP';
}

// [P0-5 2026-07-12] 已删除硬编码 PRICING_DATA 兜底表(旧值 full_diagnosis 1560 / geo_diagnosis 930 /
// meeting_start 1040 已与后端 feature_pricing SSOT 漂移)。价目一律走 PricingContext(GET /api/wallet/pricing)。
// 未加载完成时展示"加载中/价目待配置"占位,绝不再回退硬编码数字。

// ========== 主组件 ==========

export default function PricingPage() {
  const [searchQuery, setSearchQuery] = useState('');
  const [activeCategory, setActiveCategory] = useState<Category>('全部');

  // 价格口径:/feature-pricing 前台对所有身份只展示「算力」· 不展示人民币换算
  // (真实换算/成本仅 admin 财务后台可见 · 此处不区分身份)

  // [P0-5 2026-07-12] 价目单一权威源 = PricingContext(GET /api/wallet/pricing)· 不再本页自 fetch / 硬编码
  const { pricingMap, loading, error } = usePricing();

  // 从后端 pricingMap 派生列表:补 category(后端不返)→ 只保留 GEO 侧可见项
  const items = useMemo<PricingItem[]>(() => {
    return Object.values(pricingMap)
      .filter(p => p.is_active !== false)
      .map(p => ({
        feature_code: p.feature_code,
        feature_name: p.feature_name,
        cost_points: p.cost_points,
        cost_compute: p.cost_compute,
        requires_paid_points: p.requires_paid_points,
        category: inferCategory(p.feature_code),
      }))
      .filter(isVisibleOnGeoPricing);
  }, [pricingMap]);

  // 过滤逻辑
  const filteredItems = items.filter(item => {
    const matchCategory = activeCategory === '全部' || item.category === activeCategory;
    const matchSearch = !searchQuery || item.feature_name.includes(searchQuery) || item.feature_code.includes(searchQuery);
    return matchCategory && matchSearch;
  });

  // 分类统计
  const categoryCounts: Record<string, number> = {};
  for (const item of items) {
    categoryCounts[item.category] = (categoryCounts[item.category] ?? 0) + 1;
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  // [P0-5 2026-07-12] 加载完成但价目为空(接口失败 / 未配置)· 显式占位,绝不回退硬编码数字
  if (items.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 min-h-[60vh] text-center px-4">
        <p className="text-sm font-medium text-foreground">价目待配置</p>
        <p className="text-xs text-muted-foreground max-w-sm">
          {error ? '价目表暂时加载不出来 · 请刷新页面重试' : '价目表还没配置好 · 请稍后再来查看'}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6 p-3 sm:p-4 md:p-6 pb-24 lg:pb-6">
      {/* CTO-15.20 v3 · G1 桥接 banner · URL 带 ?brand_id 时显主按钮 + 回 M3 客户工作台 */}
      <BridgeBanner />

      {/* 标题 */}
      <div>
        <h1 className="text-2xl font-bold text-foreground">算力价格表</h1>
        <p className="text-sm text-muted-foreground mt-1">
          {/* 🔴 [#81 2026-09-05] 原文案「都在这查」是假的:items 是按 category 过滤后的
              可见集(:58),生产 is_active 共 66 行、这里只显 19 ——
              而它打印的又正是过滤后的数,读起来像「总共就这些」。
              不放开 HIDDEN 过滤(那几条是 Owner 裁决下架的),改成如实说范围。 */}
          GEO 与媒体发布相关 {items.length} 项 · 这些功能各消耗多少算力都在这查
        </p>
      </div>

      {/* [WO_260] 原「社媒 / IP 创作功能在社媒工作台 · 去看看 →」友链(href=/s)已删:社媒随 E3 删域。 */}

      {/* 搜索 */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 size-4 text-muted-foreground" />
        <input
          type="text"
          placeholder="搜索功能名称..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="w-full h-9 pl-9 pr-3 rounded-lg border border-border bg-card text-foreground text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </div>

      {/* 分类标签 */}
      <Tabs value={activeCategory} onValueChange={(v) => setActiveCategory(v as Category)}>
        <TabsList className="flex-wrap h-auto gap-1">
          {CATEGORIES.map((cat) => (
            <TabsTrigger key={cat} value={cat} className="text-xs">
              {cat}
              {cat !== '全部' && categoryCounts[cat] != null && (
                <span className="ml-1 text-muted-foreground">({categoryCounts[cat]})</span>
              )}
            </TabsTrigger>
          ))}
        </TabsList>

        {/* 定价表 -- 所有 tab 共享同一渲染 */}
        {CATEGORIES.map((cat) => (
          <TabsContent key={cat} value={cat}>
            <PricingTable items={filteredItems} />
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}

// ========== 定价表组件 ==========

function PricingTable({ items }: { items: PricingItem[] }) {
  if (items.length === 0) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-muted-foreground text-sm">
          未找到匹配的功能
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-0">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          共 {items.length} 项
        </CardTitle>
      </CardHeader>
      <CardContent className="pt-4">
        {/* 桌面端表格 */}
        <div className="hidden sm:block overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                <th className="text-left py-3 px-2 font-medium">功能名称</th>
                <th className="text-right py-3 px-2 font-medium">算力</th>
                <th className="text-center py-3 px-2 font-medium">备注</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.feature_code} className="border-b border-border/50 hover:bg-muted/30 transition-colors">
                  <td className="py-3 px-2 text-foreground">{item.feature_name}</td>
                  <td className="py-3 px-2 text-right tabular-nums font-medium text-foreground">
                    {item.cost_points === 0 && !item.requires_paid_points ? (
                      <Badge className="bg-emerald-500/15 text-emerald-400 border-emerald-500/20">
                        免费
                      </Badge>
                    ) : item.cost_points === 0 && item.requires_paid_points ? (
                      <span className="text-muted-foreground">按媒体报价</span>
                    ) : (
                      item.cost_points.toLocaleString()
                    )}
                  </td>
                  <td className="py-3 px-2 text-center">
                    {item.requires_paid_points && (
                      <Badge variant="secondary" className="text-xs">
                        仅充值算力
                      </Badge>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* 移动端卡片列表 */}
        <div className="sm:hidden space-y-2">
          {items.map((item) => (
            <div
              key={item.feature_code}
              className="flex items-center justify-between py-3 border-b border-border/50 last:border-0"
            >
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-foreground truncate">
                  {item.feature_name}
                </p>
                <div className="flex items-center gap-2 mt-0.5">
                  {item.cost_points === 0 && !item.requires_paid_points ? (
                    <Badge className="bg-emerald-500/15 text-emerald-400 border-emerald-500/20 text-xs">
                      免费
                    </Badge>
                  ) : item.cost_points === 0 && item.requires_paid_points ? (
                    <span className="text-xs text-muted-foreground">按媒体报价</span>
                  ) : (
                    <span className="text-xs text-muted-foreground">算力</span>
                  )}
                  {item.requires_paid_points && (
                    <Badge variant="secondary" className="text-xs">
                      仅充值算力
                    </Badge>
                  )}
                </div>
              </div>
              <div className={cn(
                'text-right font-medium tabular-nums text-sm',
                item.cost_points === 0 && !item.requires_paid_points ? 'text-emerald-400' : 'text-foreground'
              )}>
                {item.cost_points === 0 && !item.requires_paid_points ? '0' : item.cost_points === 0 ? '--' : item.cost_points.toLocaleString()}
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
