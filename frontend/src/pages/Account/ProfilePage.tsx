/**
 * ProfilePage — 个人资料页
 * 聚合展示：基础信息 + 创作者画像 + 品牌 + 团队 + 钱包 + 使用统计
 */

import { useState, useEffect, useRef } from 'react';
import { useEmbeddedNavigate } from '@/hooks/useEmbeddedNavigate';
import { authFetch } from '@/lib/api';
import { useIsMounted } from '@/hooks/useIsMounted';
import { useWallet } from '@/context/WalletContext';
import { useAuth } from '@/context/AuthContext';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Progress } from '@/components/ui/progress';
import { ChannelTierBadge } from '@/components/agent/ChannelTierBadge';
import { WalletStatusNotice } from '@/components/wallet/WalletStatusNotice';
import { bonusRateLabel, founderLabel, tierLabel } from '@/lib/channelTierTerminology';
import {
  User, Mail, Phone, Building2, Briefcase, MapPin, MessageSquare,
  Edit2, Save, X, Camera, Users, Send, Shield,
  Sparkles, BarChart3, ExternalLink, Loader2, AlertTriangle, RefreshCw, ArrowLeft,
} from 'lucide-react';

interface ProfileData {
  user: any;
  personality: any;
  creator_info: any;
  speaking_style: any;
  brands: any[];
  team: any;
  wallet: any;
  top_features: any[];
  total_operations: number;
  direct_referrals: number;
  publish_stats: any;
}

export function ProfilePage() {
  const navigate = useEmbeddedNavigate();
  const {
    channelTier,
    totalPoints,
    status: walletStatus,
    errorMessage: walletError,
    lastUpdatedAt: walletLastUpdatedAt,
    refreshBalance,
  } = useWallet();
  const { user: authUser } = useAuth();
  // [2026-06-07] 报价偏好(报价倍数)已迁出个人资料 → 报价中心(/pricing)统一管理 · 此处不再展示/编辑
  const [data, setData] = useState<ProfileData | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadStatus, setLoadStatus] = useState<'initial' | 'loading' | 'ready' | 'stale' | 'error'>('initial');
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState<Record<string, string>>({});
  const isMounted = useIsMounted();
  const profileRequestSequence = useRef(0);
  const canDisplayWallet = walletStatus === 'ready' || walletStatus === 'stale' || walletLastUpdatedAt !== null;

  useEffect(() => {
    loadProfile();
  }, []);

  const loadProfile = async () => {
    const requestSequence = ++profileRequestSequence.current;
    setLoading(true);
    setLoadStatus('loading');
    setLoadError(null);
    try {
      const res = await authFetch('/api/auth/profile');
      if (!res.ok) {
        if (res.status === 401) throw new Error('登录状态已过期，请重新登录后再查看个人设置');
        if (res.status === 403) throw new Error('当前账号没有查看个人设置的权限，请联系管理员');
        if (res.status === 429) throw new Error('请求太频繁，请稍后再试；已加载的资料不会丢失');
        throw new Error(res.status >= 500 ? '个人设置服务暂时不可用，请稍后重试' : '暂时无法读取个人设置，请重试');
      }
      const d = await res.json().catch(() => { throw new Error('个人设置响应无法读取，请重试'); });
      if (!isMounted() || requestSequence !== profileRequestSequence.current) return;
      if (!d?.success || !d.user) throw new Error('个人设置响应不完整，请重试');
      setData(d);
      if (!editing) {
        setForm({
          display_name: d.user?.display_name || '',
          real_name: d.user?.real_name || '',
          email: d.user?.email || '',
          wechat_id: d.user?.wechat_id || '',
          company: d.user?.company || '',
          job_title: d.user?.job_title || '',
          industry: d.user?.industry || '',
          city: d.user?.city || '',
          bio: d.user?.bio || '',
        });
      }
      setLoadStatus('ready');
    } catch (error) {
      if (!isMounted() || requestSequence !== profileRequestSequence.current) return;
      setLoadStatus(data ? 'stale' : 'error');
      const rawMessage = error instanceof Error ? error.message : '';
      setLoadError(/Failed to fetch|NetworkError|Load failed/i.test(rawMessage)
        ? '网络连接异常，请检查网络后重试'
        : (rawMessage || '网络连接异常，请检查网络后重试'));
    } finally {
      if (isMounted() && requestSequence === profileRequestSequence.current) setLoading(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      // 报价倍数已迁至报价中心(/pricing)· 此页 form 不含 quote_markup_ratio · 不在此提交
      const res = await authFetch('/api/auth/profile', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      });
      const d = await res.json();
      if (d.success) {
        setEditing(false);
        loadProfile();
      }
    } catch (e) {
      console.error('保存失败:', e);
    } finally {
      setSaving(false);
    }
  };

  const handleAvatarUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    try {
      const res = await authFetch('/api/auth/avatar', { method: 'POST', body: fd });
      const d = await res.json();
      if (d.success) loadProfile();
    } catch (err) {
      console.error('上传失败:', err);
    }
  };

  if (loading && !data) return <div className="flex justify-center py-20" role="status"><Loader2 className="size-6 animate-spin text-muted-foreground" /><span className="sr-only">正在读取个人设置</span></div>;
  if (!data) return (
    <div className="mx-auto flex max-w-lg flex-col items-center gap-4 px-6 py-20 text-center">
      <AlertTriangle className="size-10 text-destructive" />
      <h1 className="text-xl font-semibold">个人设置暂时无法读取</h1>
      <p className="text-sm text-muted-foreground">{loadError || '请检查网络后重试，未保存的修改不会被提交。'}</p>
      <div className="flex flex-wrap justify-center gap-3">
        <Button type="button" onClick={() => void loadProfile()}><RefreshCw className="size-4" />重试读取资料</Button>
        {loadError?.includes('登录状态已过期') && (
          <Button type="button" variant="outline" onClick={() => navigate('/login')}>重新登录</Button>
        )}
        <Button type="button" variant="outline" onClick={() => navigate('/')}><ArrowLeft className="size-4" />返回主菜单</Button>
      </div>
      <p className="text-xs text-muted-foreground">仍无法恢复时，请到“帮助与反馈”联系团队管理员。</p>
    </div>
  );

  const { user, personality, creator_info, speaking_style, brands, team, top_features, total_operations, direct_referrals, publish_stats } = data;
  const isAgentAccount = (authUser?.agent_level ?? 0) > 0;
  const tierProgress = channelTier?.next_threshold_yuan
    ? Math.max(0, Math.min(100, (Number(channelTier.rolling_12m_yuan || 0) / Number(channelTier.next_threshold_yuan || 1)) * 100))
    : 100;
  const radar = personality?.radar || {};
  const radarDims = [
    { key: 'professional', label: '专业性' },
    { key: 'expression', label: '表达力' },
    { key: 'commercial', label: '商业力' },
    { key: 'storytelling', label: '故事力' },
    { key: 'empathy', label: '共情力' },
    { key: 'creativity', label: '创造力' },
  ];

  const FEATURE_NAMES: Record<string, string> = {
    geo_diagnosis: 'GEO诊断', article_gen: '文章生成', topic_gen: '选题生成',
    script_gen: '脚本生成', hook_gen: '开篇生成', monitor_single: '监测检测',
    learn_viral: '学爆款', find_trending: '找热点', ai_coach: 'AI教练',
    author_breakdown: '博主拆解', interview_full: 'AI面试',
  };
  return (
    <div className="max-w-4xl mx-auto p-4 sm:p-6 space-y-6">
      {loadStatus === 'stale' && (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-4 text-sm" role="status">
          <p className="font-medium">当前显示上次成功加载的资料</p>
          <p className="mt-1 text-muted-foreground">{loadError}；你的已加载资料和编辑内容未被清空。</p>
          <Button type="button" variant="outline" size="sm" className="mt-3" onClick={() => void loadProfile()}>
            <RefreshCw className="size-4" />重试更新
          </Button>
        </div>
      )}
      {/* 头部 */}
      <Card>
        <CardContent className="p-6">
          <div className="flex items-start gap-4">
            {/* 头像 */}
            <div className="relative group">
              <div className="size-20 rounded-2xl bg-secondary flex items-center justify-center overflow-hidden">
                {user.avatar_url ? (
                  <img src={user.avatar_url} alt="" className="size-full object-cover" />
                ) : (
                  <User className="size-8 text-muted-foreground" />
                )}
              </div>
              <label className="absolute inset-0 flex items-center justify-center bg-black/50 rounded-2xl opacity-0 group-hover:opacity-100 cursor-pointer transition-opacity">
                <Camera className="size-5 text-white" />
                <span className="sr-only">更换头像</span>
                <input type="file" accept="image/*" className="hidden" onChange={handleAvatarUpload} />
              </label>
            </div>
            <div className="flex-1">
              <h1 className="text-xl font-bold">{user.display_name}</h1>
              <p className="text-sm text-muted-foreground mt-0.5">
                {user.company || ''}{user.company && user.job_title ? ' · ' : ''}{user.job_title || ''}
                {user.city ? ` · ${user.city}` : ''}
              </p>
              {user.bio && <p className="text-sm text-muted-foreground mt-1">{user.bio}</p>}
              <p className="text-xs text-muted-foreground mt-2">
                注册于 {new Date(user.created_at).toLocaleDateString('zh-CN')}
                {user.register_city ? ` · ${user.register_province || ''}${user.register_city}` : ''}
              </p>
            </div>
            <Button variant="outline" size="sm" onClick={() => setEditing(!editing)}>
              <Edit2 className="size-3.5 mr-1" /> {editing ? '取消' : '编辑'}
            </Button>
          </div>
        </CardContent>
      </Card>

      {isAgentAccount && walletStatus !== 'ready' && (
        <WalletStatusNotice
          status={walletStatus}
          errorMessage={walletError}
          lastUpdatedAt={walletLastUpdatedAt}
          onRetry={refreshBalance}
        />
      )}
      {isAgentAccount && canDisplayWallet && (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-sm">渠道成长等级</CardTitle>
            <ChannelTierBadge
              tier={channelTier?.effective_tier || 'none'}
              enabled={channelTier?.enabled}
              founder={channelTier?.is_founder}
              founderRank={channelTier?.founder_rank}
            />
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
              <div>
                <p className="text-xs text-muted-foreground">当前等级</p>
                <p className="font-semibold">{tierLabel(channelTier?.effective_tier || 'none')}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">近 12 个月进货</p>
                <p className="font-semibold tabular-nums">{Number(channelTier?.rolling_12m_yuan || 0).toLocaleString()} 元</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">配货比例</p>
                <p className="font-semibold text-emerald-500">{bonusRateLabel(channelTier?.bonus_rate ?? 0)}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">创始席位</p>
                <p className="font-semibold">{founderLabel(channelTier?.is_founder, channelTier?.founder_rank)}</p>
              </div>
            </div>
            {channelTier?.next_tier ? (
              <div className="space-y-1.5">
                <div className="flex items-center justify-between text-xs text-muted-foreground">
                  <span>距离 {tierLabel(channelTier.next_tier)} 还差 {Number(channelTier.gap_to_next_yuan || 0).toLocaleString()} 元</span>
                  <span>{Math.round(tierProgress)}%</span>
                </div>
                <Progress value={tierProgress} className="h-2" />
              </div>
            ) : (
              <p className="rounded-lg bg-emerald-500/10 px-3 py-2 text-xs text-emerald-500">已达到当前最高渠道等级。</p>
            )}
          </CardContent>
        </Card>
      )}

      {/* 基本信息（编辑模式） */}
      {editing && (
        <Card>
          <CardHeader><CardTitle className="text-sm">编辑个人信息</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            {[
              { key: 'display_name', label: '显示名称', icon: User },
              { key: 'real_name', label: '真实姓名', icon: User },
              { key: 'email', label: '邮箱', icon: Mail },
              { key: 'wechat_id', label: '微信号', icon: MessageSquare },
              { key: 'company', label: '公司', icon: Building2 },
              { key: 'job_title', label: '职位', icon: Briefcase },
              { key: 'industry', label: '行业', icon: BarChart3 },
              { key: 'city', label: '所在城市', icon: MapPin },
              { key: 'bio', label: '一句话介绍', icon: Sparkles },
            ].map(({ key, label, icon: Icon }) => (
              <div key={key} className="flex items-center gap-3">
                <Icon className="size-4 text-muted-foreground shrink-0" />
                <label className="text-sm text-muted-foreground w-20 shrink-0">{label}</label>
                <Input
                  value={form[key] || ''}
                  onChange={e => setForm({ ...form, [key]: e.target.value })}
                  className="flex-1 h-8 text-sm"
                  placeholder={`请输入${label}`}
                />
              </div>
            ))}
            <div className="flex gap-2 pt-2">
              <Button size="sm" onClick={handleSave} disabled={saving}>
                {saving ? <Loader2 className="size-3.5 animate-spin mr-1" /> : <Save className="size-3.5 mr-1" />}
                保存
              </Button>
              <Button size="sm" variant="outline" onClick={() => setEditing(false)}>
                <X className="size-3.5 mr-1" /> 取消
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* 基本信息（查看模式） */}
      {!editing && (
        <Card>
          <CardHeader><CardTitle className="text-sm">基本信息</CardTitle></CardHeader>
          <CardContent>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
              {[
                { label: '手机号', value: user.username ? user.username.replace(/(\d{3})\d{4}(\d{4})/, '$1****$2') : '', icon: Phone },
                { label: '邮箱', value: user.email, icon: Mail },
                { label: '微信号', value: user.wechat_id, icon: MessageSquare },
                { label: '公司', value: user.company, icon: Building2 },
                { label: '职位', value: user.job_title, icon: Briefcase },
                { label: '行业', value: user.industry, icon: BarChart3 },
                { label: '所在城市', value: user.city, icon: MapPin },
              ].filter(item => item.value).map(({ label, value, icon: Icon }) => (
                <div key={label} className="flex items-center gap-2">
                  <Icon className="size-3.5 text-muted-foreground" />
                  <span className="text-muted-foreground">{label}：</span>
                  <span>{value}</span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* 创作者画像 */}
      {personality && Object.keys(personality).length > 0 && (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-sm">创作者画像</CardTitle>
            {/* [WO_260] 原「查看完整画像」按钮跳社媒画像页(/s/profile)已删:社媒随 E3 删域;卡片本身的数据展示不动。 */}
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center gap-3 flex-wrap">
              {(() => {
                // 安全提取：level/mbti/creator_type/expression_type 可能是嵌套对象
                const lvl = typeof personality.level === 'object' && personality.level
                  ? (personality.level.current ?? personality.level.value ?? null)
                  : personality.level;
                const mbtiStr = typeof personality.mbti === 'object' && personality.mbti
                  ? (personality.mbti.type ?? personality.mbti.value ?? '')
                  : personality.mbti;
                const creatorStr = typeof personality.creator_type === 'object' && personality.creator_type
                  ? (personality.creator_type.type ?? personality.creator_type.value ?? '')
                  : personality.creator_type;
                const exprStr = typeof personality.expression_type === 'object' && personality.expression_type
                  ? (personality.expression_type.type ?? personality.expression_type.value ?? '')
                  : personality.expression_type;
                return (
                  <>
                    {lvl != null && lvl !== '' && <Badge>Lv.{lvl}</Badge>}
                    {creatorStr && <Badge variant="secondary">{creatorStr}</Badge>}
                    {mbtiStr && <Badge variant="outline">{mbtiStr}</Badge>}
                    {exprStr && exprStr !== creatorStr && <Badge variant="outline">{exprStr}</Badge>}
                  </>
                );
              })()}
            </div>

            {/* 6维雷达（简化为进度条） */}
            {Object.keys(radar).length > 0 && (
              <div className="space-y-2">
                {radarDims.map(({ key, label }) => {
                  const raw = radar[key];
                  const val = typeof raw === 'object' && raw !== null ? (raw.score ?? 0) : (raw || 0);
                  return (
                    <div key={key} className="flex items-center gap-3">
                      <span className="text-xs text-muted-foreground w-12">{label}</span>
                      <Progress value={val} className="flex-1 h-2" />
                      <span className="text-xs font-medium w-8 text-right">{val}</span>
                    </div>
                  );
                })}
              </div>
            )}

            {/* 灵魂标签 */}
            {personality.soul_tags && personality.soul_tags.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {personality.soul_tags.map((tag: string) => (
                  <Badge key={tag} variant="outline" className="text-xs">#{tag}</Badge>
                ))}
              </div>
            )}

            {/* 说话风格 */}
            {speaking_style && (
              <div className="text-xs text-muted-foreground">
                <span>语料 {speaking_style.corpus_count || 0} 条</span>
                {speaking_style.top_quotes && (
                  <span className="ml-2">常用语：{Array.isArray(speaking_style.top_quotes) ? speaking_style.top_quotes.slice(0, 3).join('、') : ''}</span>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* 品牌档案 */}
      {brands && brands.length > 0 && (
        <Card>
          <CardHeader><CardTitle className="text-sm">品牌档案</CardTitle></CardHeader>
          <CardContent className="space-y-2">
            {brands.map((b: any) => (
              <div key={b.id} className="flex items-center justify-between p-3 rounded-lg border border-border hover:bg-secondary/30 transition-colors cursor-pointer"
                onClick={() => navigate(`/brands/${b.id}`)}>
                <div>
                  <span className="font-medium text-sm">{b.name}</span>
                  {b.industry && <span className="text-xs text-muted-foreground ml-2">{b.industry}</span>}
                </div>
                <div className="flex items-center gap-3 text-xs text-muted-foreground">
                  {b.latest_score && <span>GEO {b.latest_score}分</span>}
                  {b.diagnosis_count > 0 && <span>{b.diagnosis_count}次诊断</span>}
                  <ExternalLink className="size-3" />
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {/* 团队 */}
      {team && (
        <Card>
          <CardContent className="p-4 flex items-center gap-3">
            <Users className="size-5 text-muted-foreground" />
            <div className="flex-1">
              <span className="font-medium text-sm">{team.team_name}</span>
              <span className="text-xs text-muted-foreground ml-2">{team.role === 'leader' ? '团队长' : '成员'} · {team.member_count} 人</span>
            </div>
            <Badge variant="outline" className="text-xs font-mono">{team.team_code}</Badge>
          </CardContent>
        </Card>
      )}

      {/* 账户信息 */}
      <Card>
        <CardHeader><CardTitle className="text-sm">账户</CardTitle></CardHeader>
        <CardContent>
          {/* [WO_273] 原第四格「插件授权」随浏览器插件自助发布退役一并下线,四格改三格。 */}
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 text-center">
            <button type="button" className="min-h-11 rounded-md hover:bg-muted" onClick={() => navigate('/wallet')}>
              <div className="text-lg font-bold">
                {canDisplayWallet ? totalPoints.toLocaleString() : '待确认'}
              </div>
              <div className="text-xs text-muted-foreground">算力余额</div>
            </button>
            <div>
              <div className="text-lg font-bold">{isAgentAccount ? `L${authUser?.agent_level}` : '普通账号'}</div>
              <div className="text-xs text-muted-foreground">服务商身份</div>
            </div>
            <div>
              <div className="text-lg font-bold">{direct_referrals}</div>
              <div className="text-xs text-muted-foreground">直接推荐</div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* 使用统计 */}
      <Card>
        <CardHeader><CardTitle className="text-sm">使用统计</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div className="text-sm text-muted-foreground">
            总操作 {total_operations} 次
            {publish_stats && publish_stats.total > 0 && (
              <span className="ml-3">代发 {publish_stats.total} 次（成功 {publish_stats.published}）</span>
            )}
          </div>
          {top_features && top_features.length > 0 && (
            <div className="space-y-1.5">
              {top_features.slice(0, 5).map((f: any) => {
                const maxCount = top_features[0]?.count || 1;
                return (
                  <div key={f.feature_code} className="flex items-center gap-2">
                    <span className="text-xs text-muted-foreground w-20 truncate">{FEATURE_NAMES[f.feature_code] || f.feature_code}</span>
                    <div className="flex-1 bg-secondary rounded-full h-2">
                      <div className="bg-primary/60 rounded-full h-2" style={{ width: `${(f.count / maxCount) * 100}%` }} />
                    </div>
                    <span className="text-xs text-muted-foreground w-10 text-right">{f.count}次</span>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* 安全设置 */}
      <Card>
        <CardContent className="p-4 flex gap-3">
          <Button variant="outline" size="sm" onClick={() => navigate('/change-password')}>
            <Shield className="size-3.5 mr-1" /> 修改密码
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
