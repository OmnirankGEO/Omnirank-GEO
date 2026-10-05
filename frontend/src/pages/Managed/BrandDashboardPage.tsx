/**
 * v3.4 全品牌托管看板 — /managed/brand/:brandId
 *
 * 含：
 *   - 品牌套餐总价 / 余额 / 已发文章数
 *   - 子套餐列表
 *   - AI 本周洞察（brand_strategies）
 *   - 5 引擎运行状态
 */

import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Loader2, ArrowLeft, Sparkles, FileText, TrendingUp,
  ChevronRight, Brain,
} from 'lucide-react';

import { managedApi } from '@/components/managed';
import type { BrandDashboard } from '@/components/managed/types';

export default function BrandDashboardPage() {
  const { brandId: bid } = useParams();
  const navigate = useNavigate();
  const brandId = parseInt(bid || '0');

  const [data, setData] = useState<BrandDashboard | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!brandId) return;
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [brandId]);

  async function load() {
    setLoading(true);
    try {
      const r = await managedApi.getBrandDashboard(brandId);
      setData(r);
    } catch {
      setData(null);
    } finally {
      setLoading(false);
    }
  }

  if (!brandId) return <div className="p-6 text-center">参数错误</div>;
  if (loading) return <div className="py-12 text-center"><Loader2 className="h-5 w-5 animate-spin mx-auto" /></div>;
  if (!data) return (
    <div className="container max-w-3xl mx-auto py-6">
      <Button variant="ghost" size="sm" onClick={() => navigate('/managed')}>
        <ArrowLeft className="h-4 w-4 mr-1" /> 返回
      </Button>
      <Card className="mt-4"><CardContent className="py-12 text-center text-sm text-muted-foreground">
        该品牌无活跃全托管套餐
      </CardContent></Card>
    </div>
  );

  const pkg = data.brand_package;

  return (
    <div className="container max-w-4xl mx-auto py-6 space-y-4">
      <Button variant="ghost" size="sm" onClick={() => navigate('/managed')}>
        <ArrowLeft className="h-4 w-4 mr-1" /> 返回列表
      </Button>

      {/* 品牌套餐概览 */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-purple-500" />
            🚀 全品牌 AI 自动运营中
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-3 gap-3">
            <Stat label="品牌套餐价" value={`¥${pkg.total_price_yuan.toLocaleString()}`} />
            <Stat label="余额" value={`¥${data.total_balance_yuan.toFixed(0)}`} />
            <Stat label="已发文章" value={data.delivered_articles} icon={<FileText className="h-4 w-4" />} />
          </div>

          <div className="text-xs text-muted-foreground">
            含 {pkg.campaign_ids.length} 个关键词子套餐 ·
            上次复盘：{pkg.last_review_at ? new Date(pkg.last_review_at).toLocaleString('zh-CN') : '尚未复盘'}
          </div>
        </CardContent>
      </Card>

      {/* AI 本周洞察 */}
      {data.this_week_insights && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <Brain className="h-4 w-4 text-amber-500" />
              💡 AI 本周洞察
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm">{data.this_week_insights}</p>
          </CardContent>
        </Card>
      )}

      {/* 子套餐列表 */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">📦 子关键词套餐（{data.subcampaigns.length}）</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-2">
            {data.subcampaigns.map(sc => (
              <div
                key={sc.id}
                className="flex items-center justify-between p-2 border rounded cursor-pointer hover:border-primary/50"
                onClick={() => navigate(`/managed/${sc.id}`)}
              >
                <div className="flex items-center gap-2 flex-1 min-w-0">
                  <Badge variant="outline" className="text-[10px]">{sc.target_display_label}</Badge>
                  <span className="text-sm font-medium truncate">{sc.keyword}</span>
                </div>
                <div className="flex items-center gap-3 text-xs text-muted-foreground shrink-0">
                  <span>¥{sc.balance_yuan.toFixed(0)}/¥{sc.total_recharged_yuan.toFixed(0)}</span>
                  <span>{sc.delivered_articles} 篇</span>
                  <ChevronRight className="h-3 w-3" />
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* 5 引擎说明 */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">⚙️ 5 引擎运行状态</CardTitle>
        </CardHeader>
        <CardContent className="text-xs space-y-1.5">
          <EngineRow name="选题引擎" status="每周自动选题" />
          <EngineRow name="创作引擎" status="按需 GEO 文章生产" />
          <EngineRow name="投放引擎" status="AI 动态决策媒体组合" />
          <EngineRow name="监控引擎" status="每日排名 + 检出率追踪" />
          <EngineRow name="复盘引擎" status={pkg.last_review_at ? `上次复盘 ${new Date(pkg.last_review_at).toLocaleDateString('zh-CN')}` : '尚未触发'} />
        </CardContent>
      </Card>
    </div>
  );
}

function Stat({ label, value, icon }: { label: string; value: string | number; icon?: React.ReactNode }) {
  return (
    <div className="rounded border p-3 text-center">
      {icon && <div className="text-muted-foreground">{icon}</div>}
      <div className="text-xl font-bold mt-1">{value}</div>
      <div className="text-[10px] text-muted-foreground mt-1">{label}</div>
    </div>
  );
}

function EngineRow({ name, status }: { name: string; status: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="font-medium">⚙️ {name}</span>
      <span className="text-muted-foreground">{status}</span>
    </div>
  );
}
