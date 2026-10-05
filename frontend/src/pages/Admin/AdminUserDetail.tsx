/**
 * AdminUserDetail — 管理员查看用户完整画像
 * 路由 /admin/users/:id
 * 5 个 Tab：财务 / 推荐链 / 使用统计 / 画像 / 品牌
 */

import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { authApi } from '@/context/AuthContext';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import {
  ArrowLeft, Loader2, Wallet, BarChart3, Users, Sparkles, Building2,
  ExternalLink, Clock,
} from 'lucide-react';

export default function AdminUserDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState('finance');

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    authApi.get(`/api/admin/users/${id}/detail`).then(res => {
      if (res.data.success) setData(res.data);
    }).catch(e => console.error(e)).finally(() => setLoading(false));
  }, [id]);

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="size-6 animate-spin text-muted-foreground" /></div>;
  if (!data) return <div className="text-center py-20 text-muted-foreground">用户不存在</div>;

  const { user, finance, referrals, usage, personality, creator_info, brands, team } = data;
  const wallet = finance?.wallet || {};

  const tabs = [
    { key: 'finance', label: '财务', icon: Wallet },
    { key: 'referrals', label: '推荐', icon: Users },
    { key: 'usage', label: '使用', icon: BarChart3 },
    { key: 'personality', label: '画像', icon: Sparkles },
    { key: 'brands', label: '品牌', icon: Building2 },
  ];

  const FEATURE_NAMES: Record<string, string> = {
    geo_diagnosis: 'GEO诊断', article_gen: '文章生成', topic_gen: '选题生成',
    script_gen: '脚本生成', hook_gen: '开篇生成', monitor_single: '监测检测',
    learn_viral: '学爆款', find_trending: '找热点', ai_coach: 'AI教练',
    author_breakdown: '博主拆解',
  };

  return (
    <div className="max-w-5xl mx-auto p-4 sm:p-6 space-y-4">
      {/* 头部 */}
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="icon" onClick={() => navigate('/admin/users')}>
          <ArrowLeft className="size-4" />
        </Button>
        <h1 className="text-lg font-bold">用户详情</h1>
      </div>

      {/* 用户卡片 */}
      <Card>
        <CardContent className="p-5">
          <div className="flex items-start gap-4">
            <div className="size-14 rounded-xl bg-secondary flex items-center justify-center text-lg font-bold">
              {user.display_name?.[0] || '?'}
            </div>
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <span className="text-lg font-bold">{user.display_name}</span>
                <span className="text-sm text-muted-foreground">({user.username})</span>
                {user.is_admin ? <Badge className="text-xs">管理员</Badge> : null}
              </div>
              <div className="text-sm text-muted-foreground mt-1">
                {user.company && <span>{user.company}</span>}
                {user.job_title && <span> · {user.job_title}</span>}
                {user.city && <span> · {user.city}</span>}
                {user.industry && <span> · {user.industry}</span>}
              </div>
              <div className="text-xs text-muted-foreground mt-1">
                注册: {new Date(user.created_at).toLocaleDateString('zh-CN')}
                {user.last_login && <span> · 最后登录: {new Date(user.last_login).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</span>}
                {user.register_city && <span> · IP: {user.register_province}{user.register_city}</span>}
                {user.register_source && user.register_source !== 'direct' && <span> · 来源: {user.register_source}</span>}
              </div>
            </div>
          </div>

          {/* 摘要指标 */}
          <div className="grid grid-cols-4 gap-4 mt-4 text-center">
            <div>
              <div className="text-lg font-bold">¥{(wallet.total_recharged || 0) / 100}</div>
              <div className="text-xs text-muted-foreground">累计充值</div>
            </div>
            <div>
              <div className="text-lg font-bold">{((wallet.paid_points || 0) + (wallet.bonus_points || 0)).toLocaleString()}</div>
              <div className="text-xs text-muted-foreground">算力余额</div>
            </div>
            <div>
              <div className="text-lg font-bold">{usage?.total_operations || 0}</div>
              <div className="text-xs text-muted-foreground">总操作</div>
            </div>
            <div>
              <div className="text-lg font-bold">L{wallet.agent_level || 0}</div>
              <div className="text-xs text-muted-foreground">代理等级</div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Tab 切换 */}
      <div className="flex gap-1 border-b border-border">
        {tabs.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              tab === t.key ? 'border-primary text-primary' : 'border-transparent text-muted-foreground hover:text-foreground'
            }`}>
            <t.icon className="inline size-3.5 mr-1.5 -mt-0.5" />{t.label}
          </button>
        ))}
      </div>

      {/* Tab 内容 */}
      {tab === 'finance' && (
        <div className="space-y-4">
          <Card>
            <CardHeader><CardTitle className="text-sm">本月消耗</CardTitle></CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{(finance?.month_consumed || 0).toLocaleString()} 算力</div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle className="text-sm">最近交易</CardTitle></CardHeader>
            <CardContent>
              {finance?.recent_transactions?.length > 0 ? (
                <div className="space-y-1.5 max-h-[400px] overflow-y-auto">
                  {finance.recent_transactions.map((tx: any) => (
                    <div key={tx.id} className="flex items-center justify-between text-sm py-1.5 border-b border-border/50">
                      <div>
                        <span className={tx.type === 'consume' ? 'text-red-500' : 'text-green-500'}>
                          {tx.type === 'consume' ? '-' : '+'}{Math.abs(tx.amount)}
                        </span>
                        <span className="text-muted-foreground ml-2">{tx.description || tx.feature_code}</span>
                      </div>
                      <span className="text-xs text-muted-foreground">
                        {new Date(tx.created_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
                      </span>
                    </div>
                  ))}
                </div>
              ) : <p className="text-sm text-muted-foreground">暂无交易记录</p>}
            </CardContent>
          </Card>
        </div>
      )}

      {tab === 'referrals' && (
        <div className="space-y-4">
          <Card>
            <CardHeader><CardTitle className="text-sm">直接推荐 ({referrals?.direct?.length || 0})</CardTitle></CardHeader>
            <CardContent>
              {referrals?.direct?.length > 0 ? (
                <div className="space-y-2">
                  {referrals.direct.map((r: any) => (
                    <div key={r.referred_id} className="flex items-center justify-between text-sm p-2 rounded border border-border/50">
                      <div>
                        <span className="font-medium">{r.display_name}</span>
                        <span className="text-xs text-muted-foreground ml-2">{r.username}</span>
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {r.recharged > 0 ? <Badge variant="default" className="text-[10px]">已充值 ¥{r.recharged / 100}</Badge> : <Badge variant="secondary" className="text-[10px]">未充值</Badge>}
                        <span className="ml-2">{new Date(r.created_at).toLocaleDateString('zh-CN')}</span>
                      </div>
                    </div>
                  ))}
                </div>
              ) : <p className="text-sm text-muted-foreground">暂无直接推荐</p>}
            </CardContent>
          </Card>
          {referrals?.indirect?.length > 0 && (
            <Card>
              <CardHeader><CardTitle className="text-sm">间接推荐 ({referrals.indirect.length})</CardTitle></CardHeader>
              <CardContent>
                <div className="space-y-2">
                  {referrals.indirect.map((r: any) => (
                    <div key={r.referred_id} className="flex items-center justify-between text-sm p-2 rounded border border-border/50">
                      <span className="font-medium">{r.display_name}</span>
                      <span className="text-xs text-muted-foreground">{new Date(r.created_at).toLocaleDateString('zh-CN')}</span>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          )}
          <Card>
            <CardContent className="p-4 text-sm">
              累计佣金算力：<span className="font-bold">{referrals?.total_commission_points || 0}</span>
              · 推荐链深度：<span className="font-bold">{referrals?.chain_depth || 0}</span> 层
            </CardContent>
          </Card>
        </div>
      )}

      {tab === 'usage' && (
        <div className="space-y-4">
          <Card>
            <CardHeader><CardTitle className="text-sm">功能使用热度</CardTitle></CardHeader>
            <CardContent>
              {usage?.top_features?.length > 0 ? (
                <div className="space-y-2">
                  {usage.top_features.map((f: any, i: number) => {
                    const max = usage.top_features[0]?.count || 1;
                    return (
                      <div key={f.feature_code} className="flex items-center gap-3">
                        <span className="text-xs text-muted-foreground w-6">{i + 1}.</span>
                        <span className="text-sm w-24 truncate">{FEATURE_NAMES[f.feature_code] || f.feature_code}</span>
                        <div className="flex-1 bg-secondary rounded-full h-2.5">
                          <div className="bg-primary/60 rounded-full h-2.5" style={{ width: `${(f.count / max) * 100}%` }} />
                        </div>
                        <span className="text-xs text-muted-foreground w-16 text-right">{f.count}次 / {(f.points || 0).toLocaleString()}算力</span>
                      </div>
                    );
                  })}
                </div>
              ) : <p className="text-sm text-muted-foreground">暂无使用记录</p>}
            </CardContent>
          </Card>
          {usage?.active_hours?.length > 0 && (
            <Card>
              <CardHeader><CardTitle className="text-sm">活跃时段</CardTitle></CardHeader>
              <CardContent>
                <div className="flex gap-2 flex-wrap">
                  {usage.active_hours.map((h: any) => (
                    <Badge key={h.hour} variant="secondary" className="text-xs">
                      <Clock className="size-3 mr-1" />{h.hour}:00 ({h.count}次)
                    </Badge>
                  ))}
                </div>
              </CardContent>
            </Card>
          )}
          {usage?.publish_stats && usage.publish_stats.total > 0 && (
            <Card>
              <CardContent className="p-4 text-sm">
                代发：{usage.publish_stats.total} 次 · 成功 {usage.publish_stats.published} · 拒稿 {usage.publish_stats.rejected}
              </CardContent>
            </Card>
          )}
        </div>
      )}

      {tab === 'personality' && (
        <Card>
          <CardContent className="p-6">
            {personality && Object.keys(personality).length > 0 ? (
              <div className="space-y-4">
                <div className="flex gap-2 flex-wrap">
                  {personality.level && <Badge>Lv.{personality.level}</Badge>}
                  {personality.creator_type && <Badge variant="secondary">{personality.creator_type}</Badge>}
                  {personality.mbti && <Badge variant="outline">{personality.mbti}</Badge>}
                </div>
                {personality.radar && (
                  <div className="space-y-2">
                    {Object.entries(personality.radar).map(([k, v]: [string, any]) => (
                      <div key={k} className="flex items-center gap-3">
                        <span className="text-xs text-muted-foreground w-12">{k}</span>
                        <Progress value={v} className="flex-1 h-2" />
                        <span className="text-xs w-8 text-right">{v}</span>
                      </div>
                    ))}
                  </div>
                )}
                {personality.soul_tags && (
                  <div className="flex flex-wrap gap-1.5">
                    {personality.soul_tags.map((t: string) => <Badge key={t} variant="outline" className="text-xs">#{t}</Badge>)}
                  </div>
                )}
                {creator_info && (
                  <div className="text-sm text-muted-foreground space-y-1">
                    {creator_info.positioning && <p>定位：{creator_info.positioning}</p>}
                    {creator_info.business && <p>业务：{creator_info.business}</p>}
                    {creator_info.tone && <p>风格：{creator_info.tone}</p>}
                  </div>
                )}
              </div>
            ) : (
              <p className="text-center text-muted-foreground py-8">该用户尚未完成创作者画像</p>
            )}
          </CardContent>
        </Card>
      )}

      {tab === 'brands' && (
        <div className="space-y-2">
          {brands?.length > 0 ? brands.map((b: any) => (
            <Card key={b.id}>
              <CardContent className="p-4 flex items-center justify-between">
                <div>
                  <span className="font-medium">{b.name}</span>
                  {b.industry && <span className="text-sm text-muted-foreground ml-2">{b.industry}</span>}
                </div>
                <div className="flex items-center gap-3 text-xs text-muted-foreground">
                  {b.latest_score && <span>GEO {b.latest_score}分</span>}
                  <span>{b.diagnosis_count || 0}次诊断</span>
                  <span>{b.article_count || 0}篇文章</span>
                </div>
              </CardContent>
            </Card>
          )) : <p className="text-center text-muted-foreground py-8">暂无品牌</p>}
          {team && (
            <Card>
              <CardContent className="p-4 text-sm">
                <Users className="inline size-4 mr-1.5 text-muted-foreground" />
                {team.team_name} · {team.role === 'leader' ? '团队长' : '成员'} · {team.member_count}人
                <Badge variant="outline" className="ml-2 text-xs font-mono">{team.team_code}</Badge>
              </CardContent>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}
