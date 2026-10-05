/**
 * [P1-3 媒体形态分档 2026-08-14] 媒体域名目录治理
 *
 * /admin/media-directory 路由
 *
 * 功能:
 * - 查看 media_domain_directory(域名 → 中文名 / 一句话 / 形态档)
 * - 订正形态档(门户主站 / 平台号 / 垂直媒体 / 自站;发布渲染层按档适配)
 * - 人工订正后 source 升为 admin,LLM 蒸馏不再覆盖(后端既有语义)
 *
 * 关联 endpoints:
 * - GET   /api/admin/media-domain-directory?query=&limit=
 * - PATCH /api/admin/media-domain-directory/{domain}
 */

import { useState, useEffect, useCallback } from 'react';
import { authApi } from '@/context/AuthContext';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { toast } from 'sonner';
import { RefreshCw, Search, Newspaper } from 'lucide-react';

interface DirectoryEntry {
  domain: string;
  zh_name: string;
  one_liner: string;
  source: string;
  media_form: string;
  updated_at: string | null;
}

const FORM_LABELS: Record<string, string> = {
  '': '未分档',
  portal_site: '门户主站',
  platform_account: '平台号',
  vertical_media: '垂直媒体',
  self_site: '自站',
};

export default function MediaDirectoryAdmin() {
  const [entries, setEntries] = useState<DirectoryEntry[]>([]);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [savingDomain, setSavingDomain] = useState<string | null>(null);

  const load = useCallback(async (q: string) => {
    setLoading(true);
    try {
      const res = await authApi.get('/api/admin/media-domain-directory', {
        params: { query: q || undefined, limit: 200 },
      });
      setEntries((res.data?.entries as DirectoryEntry[]) || []);
    } catch {
      toast.error('媒体目录加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(''); }, [load]);

  const setForm = async (domain: string, mediaForm: string) => {
    setSavingDomain(domain);
    try {
      await authApi.patch(`/api/admin/media-domain-directory/${encodeURIComponent(domain)}`, {
        media_form: mediaForm,
      });
      setEntries(prev => prev.map(e =>
        e.domain === domain ? { ...e, media_form: mediaForm, source: 'admin' } : e,
      ));
      toast.success(`${domain} 已定档:${FORM_LABELS[mediaForm] || '未分档'}`);
    } catch {
      toast.error('定档失败,请重试');
    } finally {
      setSavingDomain(null);
    }
  };

  return (
    <div className="p-4 space-y-4 max-w-5xl">
      <div className="flex items-center gap-2">
        <Newspaper className="w-5 h-5" />
        <h1 className="text-lg font-semibold">媒体域名目录 · 形态分档</h1>
        <Button variant="outline" size="sm" onClick={() => void load(query)} disabled={loading}>
          <RefreshCw className={loading ? 'w-4 h-4 animate-spin' : 'w-4 h-4'} />
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">
        形态档决定发布渲染:门户主站/垂直媒体自动收紧联系方式;自站追加品牌署名;
        平台号一律不产出编辑归属形态。未分档 = 渲染层不做形态化处理。
      </p>
      <div className="flex items-center gap-2">
        <Search className="w-4 h-4 text-muted-foreground" />
        <input
          className="border rounded px-2 py-1 text-sm w-64 bg-background"
          placeholder="按域名或中文名搜索"
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') void load(query); }}
        />
        <Button size="sm" onClick={() => void load(query)} disabled={loading}>搜索</Button>
      </div>
      <Card className="divide-y">
        {entries.length === 0 && (
          <div className="p-4 text-sm text-muted-foreground">
            {loading ? '加载中…' : '目录暂无条目(蒸馏尚未产出或搜索无命中)'}
          </div>
        )}
        {entries.map(entry => (
          <div key={entry.domain} className="flex items-center gap-3 p-3 text-sm">
            <div className="flex-1 min-w-0">
              <div className="font-medium truncate">
                {entry.zh_name || entry.domain}
                {entry.zh_name && (
                  <span className="ml-2 text-xs text-muted-foreground">{entry.domain}</span>
                )}
                {entry.source === 'admin' && (
                  <span className="ml-2 text-xs rounded bg-secondary px-1.5 py-0.5">人工已订正</span>
                )}
              </div>
              {entry.one_liner && (
                <div className="text-xs text-muted-foreground truncate">{entry.one_liner}</div>
              )}
            </div>
            <select
              className="border rounded px-2 py-1 text-sm bg-background"
              value={entry.media_form || ''}
              disabled={savingDomain === entry.domain}
              onChange={e => void setForm(entry.domain, e.target.value)}
            >
              {Object.entries(FORM_LABELS).map(([value, label]) => (
                <option key={value || 'none'} value={value}>{label}</option>
              ))}
            </select>
          </div>
        ))}
      </Card>
    </div>
  );
}
