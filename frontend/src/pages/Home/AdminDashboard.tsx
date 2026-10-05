/**
 * AdminDashboard — 管理员运营控制台
 * 6 大维度：用户增长 / 财务 / 功能热度 / API成本 / 代发业务 / 推荐排行
 */

import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { authFetch } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import {
  Users, TrendingUp, DollarSign, Cpu, Send, Share2,
  Loader2, ExternalLink, RefreshCw, CheckCircle2, XCircle, Clock,
  Coins, Receipt,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
// B2.3 (CTO-15.9 session 3 · 2026-04-25) · 9 步交付漏斗 · 消费 /api/dashboard/flow-funnel
import { FlowFunnelCard } from '@/components/dashboard/FlowFunnelCard';

export function AdminDashboard() {
  const navigate = useNavigate();
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    authFetch('/api/admin/dashboard?period=month').then(r => r.json()).then(d => {
      if (d.status === 'success') setData(d);
    }).catch(() => {}).finally(() => setLoading(false));
  };
  useEffect(load, []);

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="size-6 animate-spin text-muted-foreground" /></div>;
  if (!data) return <div className="text-center py-20 text-muted-foreground">加载失败</div>;

  const { users, finance, feature_usage, api_costs, publishing, referrals, activities } = data;

  const StatCard = ({ label, value, sub, icon: Icon, onClick }: any) => (
    <Card className={onClick ? 'cursor-pointer hover:bg-secondary/30 transition-colors' : ''} onClick={onClick}>
      <CardContent className="p-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs text-muted-foreground">{label}</span>
          <Icon className="size-4 text-muted-foreground" />
        </div>
        <div className="text-2xl font-bold">{value}</div>
        {sub && <div className="text-xs text-muted-foreground mt-1">{sub}</div>}
      </CardContent>
    </Card>
  );

  const maxFeature = feature_usage?.[0]?.count || 1;

  return (
    <div className="p-4 sm:p-6 space-y-6 max-w-7xl mx-auto">
      {/* 标题 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold">运营控制台</h1>
          <p className="text-xs text-muted-foreground mt-0.5">
            {data.cached_at ? `数据更新于 ${new Date(data.cached_at).toLocaleTimeString('zh-CN')}` : ''}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={load}><RefreshCw className="size-3.5 mr-1" />刷新</Button>
      </div>

      {/* 用户增长 */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard label="总用户" value={users.total} sub={`+${users.week_new} 本周 · 点击看用户`} icon={Users}
          onClick={() => navigate('/admin/users?from=dashboard')} />
        <StatCard label="今日新增" value={users.today_new} sub="点击看今日注册" icon={TrendingUp}
          onClick={() => navigate('/admin/users?from=dashboard&created=today')} />
        <StatCard label="7日活跃" value={users.active_7d} sub={`活跃率 ${(users.active_rate * 100).toFixed(0)}% · 点击看用户`} icon={Users}
          onClick={() => navigate('/admin/users?from=dashboard&activity=active_7d')} />
        <StatCard label="付费用户" value={users.paid_count} sub={`转化率 ${(users.paid_rate * 100).toFixed(0)}% · 点击看已充值用户`} icon={DollarSign}
          onClick={() => navigate('/admin/users?from=dashboard&has_paid=true')} />
      </div>

      {/* 财务看板 · 2026-05-29 老板 P0(v1.0)→ v1.3 复审收口
          v1.0:旧"本月成本"误导(只是 LLM 费)· 拆成本 3 字段
          v1.3 老板拍【现金口径】:
             - 本月营收(现金)= 充值 recharge · 发布收入【不并入】(发布扣的 cost_points 来自已充值 paid_points · 并入双算)
             - 总运营成本 = LLM API 费 + 发布外采(cost_yuan 媒体原价)
             - 本月毛利 = 营收(现金) - 总运营成本 · 毛利率 = 毛利 / 营收
             - 发布业务收入/成本/毛利 单独在下方"本月代发业务"卡显示 · 不并入总收入
          兼容老后端:新字段缺失时回退 revenue_yuan / cost_yuan */}

      {/* Row 1 · 营收(现金) + 利润 4 卡 */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard
          label="本月营收(现金)"
          value={`¥${finance.total_revenue_yuan ?? finance.recharge_revenue_yuan ?? finance.revenue_yuan}`}
          sub="充值实收 · 点击看账单"
          icon={DollarSign}
          onClick={() => navigate('/admin/finance?from=dashboard&tab=recharges&period=month&basis=cash')}
        />
        <StatCard
          label="本月总成本"
          value={`¥${finance.total_cost_yuan ?? finance.total_operating_cost_yuan ?? finance.cost_yuan}`}
          sub="LLM + 代发采购 · 点击看构成"
          icon={Receipt}
          onClick={() => navigate('/admin/finance?from=dashboard&tab=cost&period=month')}
        />
        {/* [CTO-15.23 2026-05-29 Q2] 真实代发采购成本并入后,毛利需合并月度合约收入(后续 phase)·
            不显示"营收-总成本"避免误导成净亏(老板 margin_note 拍板)· 老后端无 total_cost_yuan 时回退旧毛利 */}
        <StatCard
          label="本月毛利"
          value={finance.total_cost_yuan !== undefined ? '—' : `¥${finance.profit_yuan}`}
          sub={finance.total_cost_yuan !== undefined ? `${finance.cost_margin_note ?? '待合并月度合约'} · 点击看利润表` : '营收(现金) - 总成本 · 点击看利润表'}
          icon={Coins}
          onClick={() => navigate('/admin/finance?from=dashboard&tab=pnl&period=month&basis=cash')}
        />
        <StatCard
          label="毛利率"
          value={finance.total_cost_yuan !== undefined ? '—' : (finance.profit_rate !== null && finance.profit_rate !== undefined ? `${(finance.profit_rate * 100).toFixed(0)}%` : '—')}
          sub={finance.total_cost_yuan !== undefined ? '待合并月度合约 · 点击看利润表' : '基于现金营收 · 点击看利润表'}
          icon={TrendingUp}
          onClick={() => navigate('/admin/finance?from=dashboard&tab=pnl&period=month&basis=cash')}
        />
      </div>

      {/* Row 2 · 成本明细 + ARPU 3 卡 */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard
          label="LLM API 调用费"
          value={`¥${finance.llm_api_cost_yuan ?? finance.cost_yuan}`}
          sub="模型 token + 监测 · 点击看明细"
          icon={Cpu}
          onClick={() => navigate('/admin/llm-cost?from=dashboard&period=month')}
        />
        <StatCard
          label="代发采购成本"
          value={`¥${finance.cost_breakdown?.publish_media ?? finance.publish_external_cost_yuan ?? 0}`}
          sub="发布通道实付 · 点击看成本中心"
          icon={Send}
          onClick={() => navigate('/admin/finance?from=dashboard&tab=cost&period=month&cost=media')}
        />
        <StatCard label="ARPU" value={`¥${finance.arpu}`} sub="现金营收/活跃用户 · 点击看单位经济" icon={DollarSign}
          onClick={() => navigate('/admin/finance?from=dashboard&tab=balance&period=month&basis=cash')} />
      </div>

      {/* B2.3 9 步交付漏斗(全平台 · admin 视角) · M1a KR1-KR3 看板 */}
      <FlowFunnelCard scope="global" days={90} />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* 功能使用 TOP 10 */}
        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-sm">功能使用热度</CardTitle></CardHeader>
          <CardContent className="space-y-2">
            {feature_usage?.length > 0 ? feature_usage.map((f: any, i: number) => (
              <div key={f.code} className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground w-4">{i + 1}</span>
                <span className="text-sm w-28 truncate">{f.name}</span>
                <div className="flex-1 bg-secondary rounded-full h-2">
                  <div className="bg-primary/60 rounded-full h-2" style={{ width: `${(f.count / maxFeature) * 100}%` }} />
                </div>
                <span className="text-xs text-muted-foreground w-12 text-right">{f.count}次</span>
              </div>
            )) : <p className="text-sm text-muted-foreground">暂无数据</p>}
          </CardContent>
        </Card>

        {/* API 成本 + 代发 */}
        <div className="space-y-4">
          <Card className="cursor-pointer hover:bg-secondary/30 transition-colors"
            onClick={() => navigate('/admin/orders?from=dashboard&tab=orders')}>
            <CardHeader className="pb-3"><CardTitle className="text-sm">LLM API 调用明细（本月）</CardTitle></CardHeader>
            <CardContent className="space-y-1.5">
              {api_costs?.length > 0 ? api_costs.map((c: any) => (
                <div key={c.model} className="flex items-center justify-between text-sm">
                  <span className="truncate">{c.model}</span>
                  <span className="text-muted-foreground">¥{c.cost_yuan} <span className="text-xs">({c.calls}次)</span></span>
                </div>
              )) : <p className="text-sm text-muted-foreground">暂无 API 调用</p>}
              {api_costs?.length > 0 && (
                <div className="flex justify-between text-sm font-medium pt-2 border-t border-border">
                  <span>合计 LLM API 费</span>
                  <span>¥{finance.llm_api_cost_yuan ?? finance.cost_yuan}</span>
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-sm flex items-center gap-2">
                本月代发业务
                {publishing.session_valid !== null && (
                  publishing.session_valid
                    ? <Badge variant="outline" className="text-[10px] text-green-600 border-green-300">Session ✓</Badge>
                    : <Badge variant="destructive" className="text-[10px]">Session ✗</Badge>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {/* 2026-05-29 老板 v1.2 P1-1 · 全部本月口径(created_at / published_at >= month_start)
                  · 不再混合"历史 vs 本月"导致 profit = 历史收入 - 本月成本 无意义 */}
              <div className="grid grid-cols-3 gap-3 text-center text-sm">
                <div><div className="font-bold">{publishing.total_orders}</div><div className="text-xs text-muted-foreground">本月新订单</div></div>
                <div><div className="font-bold text-green-600">{publishing.published}</div><div className="text-xs text-muted-foreground">本月已发布</div></div>
                <div><div className="font-bold text-red-500">{publishing.rejected}</div><div className="text-xs text-muted-foreground">本月已拒稿</div></div>
              </div>
              {/* v1.4 Codex 复审 · 发布卡用【已发布】item 集算毛利(跟顶部【全状态】总外采成本区分):
                  - 本月发布收入:SUM(cost_points)/130(用户实付积分折现)
                  - 本月已发布外采:SUM(cost_yuan) 已发布子集(配对收入算毛利 · ≠ 顶部全状态总外采)
                  - 本月发布毛利:收入 - 已发布外采 = 媒体价 × 0.5(真 markup margin · 正数) */}
              <div className="grid grid-cols-3 gap-3 text-center text-sm mt-3 pt-3 border-t border-border">
                <div>
                  <div className="font-bold text-green-600">¥{publishing.revenue_yuan ?? 0}</div>
                  <div className="text-xs text-muted-foreground">本月发布收入</div>
                </div>
                <div>
                  <div className="font-bold text-orange-600">¥{publishing.cost_yuan ?? 0}</div>
                  <div className="text-xs text-muted-foreground">本月已发布外采</div>
                </div>
                <div>
                  <div className={`font-bold ${(publishing.profit_yuan ?? 0) >= 0 ? 'text-blue-600' : 'text-red-600'}`}>
                    ¥{publishing.profit_yuan ?? 0}
                  </div>
                  <div className="text-xs text-muted-foreground">本月发布毛利</div>
                </div>
              </div>
              <div className="text-xs text-muted-foreground mt-2 text-center">
                媒体库 {publishing.media_count.toLocaleString()} 个
              </div>
            </CardContent>
          </Card>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* 推荐排行 */}
        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-sm">推荐排行</CardTitle></CardHeader>
          <CardContent>
            <div className="grid grid-cols-3 gap-4">
              {/* 直接推荐 */}
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-2">直接推荐</p>
                {referrals.direct_leaderboard?.length > 0 ? referrals.direct_leaderboard.slice(0, 5).map((r: any, i: number) => (
                  <div key={r.user_id} className="flex items-center justify-between text-xs py-1 cursor-pointer hover:text-primary"
                    onClick={() => navigate(`/admin/users/${r.user_id}`)}>
                    <span>{i + 1}. {r.display_name}</span>
                    <span className="text-muted-foreground">{r.count}人</span>
                  </div>
                )) : <p className="text-xs text-muted-foreground">暂无</p>}
              </div>
              {/* 间接推荐 */}
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-2">间接推荐</p>
                {referrals.indirect_leaderboard?.length > 0 ? referrals.indirect_leaderboard.slice(0, 5).map((r: any, i: number) => (
                  <div key={r.user_id} className="flex items-center justify-between text-xs py-1 cursor-pointer hover:text-primary"
                    onClick={() => navigate(`/admin/users/${r.user_id}`)}>
                    <span>{i + 1}. {r.display_name}</span>
                    <span className="text-muted-foreground">{r.count}人</span>
                  </div>
                )) : <p className="text-xs text-muted-foreground">暂无</p>}
              </div>
              {/* 佣金 */}
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-2">佣金总榜</p>
                {referrals.commission_leaderboard?.length > 0 ? referrals.commission_leaderboard.slice(0, 5).map((r: any, i: number) => (
                  <div key={r.user_id} className="flex items-center justify-between text-xs py-1 cursor-pointer hover:text-primary"
                    onClick={() => navigate(`/admin/users/${r.user_id}`)}>
                    <span>{i + 1}. {r.display_name}</span>
                    <span className="text-muted-foreground">¥{r.commission_yuan}</span>
                  </div>
                )) : <p className="text-xs text-muted-foreground">暂无</p>}
              </div>
            </div>
            <div className="text-xs text-muted-foreground mt-3 pt-2 border-t border-border">
              用户身份：普通用户 {(users.level_distribution?.free || 0) + (users.level_distribution?.paid || 0)} · 服务方 {(users.level_distribution?.agent_l1 || 0) + (users.level_distribution?.agent_l2 || 0)}
            </div>
          </CardContent>
        </Card>

        {/* 最近活动 */}
        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-sm">最近活动</CardTitle></CardHeader>
          <CardContent className="space-y-2">
            {activities?.length > 0 ? activities.map((a: any) => (
              <div key={`${a.type || 'activity'}-${a.user || ''}-${a.title || ''}-${a.time || ''}`} className="flex items-start gap-2 text-sm cursor-pointer hover:bg-secondary/30 rounded p-1.5 -m-1.5 transition-colors"
                onClick={() => a.link && navigate(a.link)}>
                <div className={`size-2 rounded-full mt-1.5 shrink-0 ${
                  a.type === 'register' ? 'bg-green-500' :
                  a.type === 'diagnosis' ? 'bg-blue-500' :
                  a.type === 'recharge' ? 'bg-amber-500' : 'bg-muted-foreground'
                }`} />
                <div className="flex-1 min-w-0">
                  <span className="font-medium">{a.user}</span>
                  <span className="text-muted-foreground ml-1.5">{a.title}</span>
                </div>
                <span className="text-xs text-muted-foreground shrink-0">{a.time}</span>
              </div>
            )) : <p className="text-sm text-muted-foreground">暂无活动</p>}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
