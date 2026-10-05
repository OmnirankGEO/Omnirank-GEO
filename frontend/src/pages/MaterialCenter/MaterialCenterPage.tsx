/**
 * v3.4 物料中心页 — /material-center/:profileId
 *
 * 12 项三层架构（v2.1 锁定）：
 *   🔴 必填核心 5 项 (60 分): company_intro, selling_points, cases, competitors_real, industry+city
 *   🟡 选填增强 7 项 (+28 分): real_data_points, brand_story, milestones, team_core, testimonials, service_flow, price_packages
 *   🟢 AI 自动挖 3 项 (+12 分): target_keywords, industry_position, target_customer_profile
 *
 * 完成度评分 0-100，60/80/100 三档引导
 */

import { useEffect, useState, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { Badge } from '@/components/ui/badge';
import {
  Loader2, ArrowLeft, ChevronDown, ChevronRight, CheckCircle2,
  Circle, Sparkles, Save, Plus, Trash2,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';

import { managedApi } from '@/components/managed';
import type { GeoAssetsPayload } from '@/components/managed/api';

const REQUIRED_FIELDS = [
  { key: 'company_intro',     name: '公司介绍',           desc: '500-1000 字简介' },
  { key: 'selling_points',    name: '核心卖点 + 证据',     desc: 'V9 prompt 核心输入' },
  { key: 'cases',             name: '客户案例（≥3 个）',  desc: '必带真实数字' },
  { key: 'competitors_real',  name: '真实竞品名 + 评价',  desc: 'V9 严禁虚构' },
  { key: 'industry+city',     name: '行业 + 城市',         desc: '在档案主信息里' },
];

const RECOMMENDED_FIELDS = [
  { key: 'real_data_points',          name: '真实数据点（成立年/客户数/资质）', desc: 'V9 信任锚点' },
  { key: 'brand_story',               name: '品牌故事 / 创始故事',           desc: 'brand_softarticle 必需' },
  { key: 'milestones',                name: '里程碑事件',                    desc: 'company_profile 必需' },
  { key: 'team_core',                 name: '核心团队成员',                  desc: '企业事实与品牌说明需要' },
  { key: 'testimonials',              name: '客户证言',                      desc: 'recommendation_review 必需' },
  { key: 'service_flow',              name: '服务流程 SOP',                  desc: 'buying_guide 必需' },
  { key: 'price_packages',            name: '价格套餐结构',                  desc: 'direct_answer 必需' },
];

const AI_AUTO_FIELDS = [
  { key: 'target_keywords',           name: '目标关键词',          desc: 'AI 从画像挖' },
  { key: 'industry_position',         name: '行业位置',            desc: 'AI 从行业数据推断' },
  { key: 'target_customer_profile',   name: '目标客户画像',        desc: 'AI 从案例反推' },
];

export default function MaterialCenterPage() {
  const { profileId: pid } = useParams();
  const navigate = useNavigate();
  const profileId = parseInt(pid || '0');

  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [score, setScore] = useState(0);
  const [detail, setDetail] = useState<any>({});
  const [assets, setAssets] = useState<GeoAssetsPayload>({});
  const [industry, setIndustry] = useState<string>('');
  const [city, setCity] = useState<string>('');

  const [openSections, setOpenSections] = useState({
    required: true,
    recommended: false,
    ai_auto: false,
  });

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const r = await managedApi.getGeoAssets(profileId);
      setAssets(r.geo_assets || {});
      setScore(r.completeness_score);
      setDetail(r.completeness_detail || {});
      setIndustry(r.industry || '');
      setCity(r.city || '');
    } catch (e: any) {
      toast.error(e?.message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, [profileId]);

  useEffect(() => {
    if (profileId) void refresh();
  }, [refresh, profileId]);

  async function saveField(field: string, value: any) {
    setSaving(true);
    try {
      const r = await managedApi.updateGeoAssets(profileId, { [field]: value } as any);
      toast.success(`已保存 ${field}（完成度 ${r.completeness_score} 分）`);
      setAssets(prev => ({ ...prev, [field]: value }));
      setScore(r.completeness_score);
      setDetail(r.completeness_detail || {});
    } catch (e: any) {
      toast.error(e?.message || '保存失败');
    } finally {
      setSaving(false);
    }
  }

  if (!profileId) return <div className="p-6 text-center">参数错误</div>;
  if (loading) return <div className="py-12 text-center"><Loader2 className="h-5 w-5 animate-spin mx-auto" /></div>;

  return (
    <div className="container max-w-4xl mx-auto py-6 space-y-4">
      <Button variant="ghost" size="sm" onClick={() => navigate(-1)}>
        <ArrowLeft className="h-4 w-4 mr-1" /> 返回
      </Button>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-amber-500" />
            GEO 物料中心
          </CardTitle>
          <p className="text-xs text-muted-foreground">
            填得越全 AI 写出来的 GEO 文章质量越高（基于 V9/V10 + 9 种文体真实输入需求设计）
          </p>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-4">
            <div className="flex-1">
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs text-muted-foreground">完成度</span>
                <span className="text-sm font-bold">
                  {score} / 100
                  <span className="ml-2 text-xs text-muted-foreground">
                    {score >= 100 ? '🎉 强势档' : score >= 80 ? '⭐ 推荐档' : score >= 60 ? '✅ 基础档' : '⚠️ 待补全'}
                  </span>
                </span>
              </div>
              <Progress value={score} className="h-2" />
            </div>
          </div>
          <div className="text-[11px] text-muted-foreground mt-3">
            🎯 <strong>60 分</strong> 基础档可生成基础质量 ·
            <strong> 80 分</strong> 推荐档（AI 写得更准） ·
            <strong> 100 分</strong> 强势档（所有 9 种文体最优）
          </div>
        </CardContent>
      </Card>

      {/* 必填 5 项 */}
      <Section
        title="🔴 必填核心 5 项（决定能否生成基础质量）"
        score={`${(detail.filled_required || []).length}/${REQUIRED_FIELDS.length}`}
        open={openSections.required}
        onToggle={() => setOpenSections(s => ({ ...s, required: !s.required }))}
      >
        <div className="space-y-2">
          {REQUIRED_FIELDS.map(f => (
            <FieldRow
              key={f.key}
              field={f}
              filled={detail.filled_required?.includes(f.key)}
              value={(assets as any)[f.key]}
              industry={industry}
              city={city}
              profileId={profileId}
              onSave={saveField}
            />
          ))}
        </div>
      </Section>

      {/* 选填 7 项 */}
      <Section
        title="🟡 选填增强 7 项（决定文章质量上限）"
        score={`${(detail.filled_recommended || []).length}/${RECOMMENDED_FIELDS.length}`}
        open={openSections.recommended}
        onToggle={() => setOpenSections(s => ({ ...s, recommended: !s.recommended }))}
      >
        <div className="space-y-2">
          {RECOMMENDED_FIELDS.map(f => (
            <FieldRow
              key={f.key}
              field={f}
              filled={detail.filled_recommended?.includes(f.key)}
              value={(assets as any)[f.key]}
              industry={industry}
              city={city}
              profileId={profileId}
              onSave={saveField}
            />
          ))}
        </div>
      </Section>

      {/* AI 自动挖 3 项 */}
      <Section
        title="🟢 AI 自动挖 3 项（可不填，AI 推断）"
        score={`${(detail.ai_auto_filled || []).length}/${AI_AUTO_FIELDS.length}`}
        open={openSections.ai_auto}
        onToggle={() => setOpenSections(s => ({ ...s, ai_auto: !s.ai_auto }))}
      >
        <div className="space-y-2">
          {AI_AUTO_FIELDS.map(f => (
            <FieldRow
              key={f.key}
              field={f}
              filled={detail.ai_auto_filled?.includes(f.key)}
              value={(assets as any)[f.key]}
              industry={industry}
              city={city}
              profileId={profileId}
              onSave={saveField}
            />
          ))}
        </div>
      </Section>

      {saving && (
        <div className="fixed bottom-4 right-4 rounded-lg bg-card border shadow-lg px-3 py-2 text-xs flex items-center gap-2">
          <Loader2 className="h-3 w-3 animate-spin" /> 保存中...
        </div>
      )}
    </div>
  );
}

function Section({
  title, score, open, onToggle, children,
}: {
  title: string;
  score: string;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <Card>
      <CardHeader
        className="cursor-pointer flex flex-row items-center justify-between py-3"
        onClick={onToggle}
      >
        <CardTitle className="text-sm flex items-center gap-2">
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          {title}
        </CardTitle>
        <Badge variant="outline" className="text-[10px]">{score}</Badge>
      </CardHeader>
      {open && <CardContent>{children}</CardContent>}
    </Card>
  );
}

function FieldRow({
  field, filled, value, industry, city, profileId, onSave,
}: {
  field: { key: string; name: string; desc: string };
  filled: boolean;
  value: any;
  industry: string;
  city: string;
  profileId: number;
  onSave: (field: string, value: any) => void;
}) {
  const Icon = filled ? CheckCircle2 : Circle;

  // industry+city 是主表字段，引导跳到品牌编辑
  if (field.key === 'industry+city') {
    return (
      <div className="flex items-center justify-between p-2 border rounded">
        <div className="flex items-start gap-2">
          <Icon className={cn('h-4 w-4 mt-0.5', filled ? 'text-emerald-500' : 'text-muted-foreground')} />
          <div>
            <div className="text-sm font-medium">{field.name}</div>
            <div className="text-[11px] text-muted-foreground">
              {industry || '未填行业'} / {city || '未填城市'}
            </div>
          </div>
        </div>
        {/* [WO_260] 原「去编辑」按钮跳 /profiles/:id/edit —— 这条路由从来不存在(点了就 404);
            本页字段就在本页行内改,没有语义对得上的在役页,删掉这个坏入口。 */}
      </div>
    );
  }

  // 其他字段简化为"已填/未填" + 详情编辑入口
  return (
    <div className="flex items-center justify-between p-2 border rounded">
      <div className="flex items-start gap-2 flex-1 min-w-0">
        <Icon className={cn('h-4 w-4 mt-0.5', filled ? 'text-emerald-500' : 'text-muted-foreground')} />
        <div>
          <div className="text-sm font-medium">{field.name}</div>
          <div className="text-[11px] text-muted-foreground">{field.desc}</div>
        </div>
      </div>
      <FieldEditor field={field} value={value} onSave={onSave} />
    </div>
  );
}

function FieldEditor({
  field, value, onSave,
}: {
  field: { key: string; name: string; desc: string };
  value: any;
  onSave: (field: string, value: any) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState<string>(() => {
    if (typeof value === 'string') return value;
    if (value) return JSON.stringify(value, null, 2);
    return '';
  });

  function handleSave() {
    let parsed: any = text;
    // 尝试 JSON 解析（数组/对象类字段）
    if (text.trim().startsWith('{') || text.trim().startsWith('[')) {
      try {
        parsed = JSON.parse(text);
      } catch {
        toast.error('JSON 格式错误，请检查');
        return;
      }
    } else if (field.key === 'company_intro') {
      parsed = { text };
    } else if (field.key === 'industry_position') {
      parsed = text;
    } else if (field.key === 'target_keywords') {
      parsed = text.split(/[,，\s]+/).filter(Boolean);
    }
    onSave(field.key, parsed);
    setEditing(false);
  }

  if (!editing) {
    return (
      <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
        {value ? '编辑' : '填写'}
      </Button>
    );
  }

  return (
    <div className="absolute inset-0 z-10 bg-background/95 p-4 flex flex-col">
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-medium">编辑：{field.name}</span>
        <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>关闭</Button>
      </div>
      <textarea
        className="flex-1 rounded border bg-background p-2 text-xs font-mono"
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={20}
      />
      <div className="flex justify-end gap-2 mt-2">
        <Button size="sm" variant="outline" onClick={() => setEditing(false)}>取消</Button>
        <Button size="sm" onClick={handleSave}>
          <Save className="h-3 w-3 mr-1" /> 保存
        </Button>
      </div>
    </div>
  );
}
