/**
 * 定价中心(双 SSOT 管理端 · 最小可用版)
 *
 * 管理员按目录类型(零售 / 进货)+ scope 查看目录版本历史,
 * 并执行 起草 → 校验 → 发布 → 回滚 生命周期动作。
 *
 * 只调后端 admin 端点,前端不做任何价格计算。功能优先,样式从简。
 */
import { useEffect, useState, useCallback } from 'react';
import { Loader2, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { formatDateTime } from '@/lib/utils';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from '@/components/ui/table';
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/select';
import {
  usePricingSSOT,
  type CatalogType,
  type CatalogVersion,
  type CatalogVersionStatus,
} from '@/hooks/usePricingSSOT';

const STATUS_LABEL: Record<CatalogVersionStatus, string> = {
  draft: '草稿',
  validated: '已校验',
  published: '已发布',
  archived: '已归档',
  rolled_back: '已回滚',
};

function statusVariant(status: CatalogVersionStatus): 'default' | 'secondary' | 'outline' {
  if (status === 'published') return 'default';
  if (status === 'draft' || status === 'validated') return 'secondary';
  return 'outline';
}

export default function PricingCenterSSOT() {
  const { listVersions, createDraft, validateDraft, publish, rollback } = usePricingSSOT();

  const [catalogType, setCatalogType] = useState<CatalogType>('retail');
  const [scopeKey, setScopeKey] = useState('default');
  const [versions, setVersions] = useState<CatalogVersion[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    const res = await listVersions(catalogType, scopeKey.trim() || 'default');
    setLoading(false);
    if (res.ok) {
      setVersions(res.data.versions ?? []);
    } else {
      setVersions([]);
      setError(res.code === 'SSOT_DISABLED' ? '定价 SSOT 功能未启用(flag 关)' : res.message);
    }
  }, [listVersions, catalogType, scopeKey]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleCreateDraft = useCallback(async () => {
    setBusyId(-1);
    const res = await createDraft({ catalog_type: catalogType, scope_key: scopeKey.trim() || 'default' });
    setBusyId(null);
    if (res.ok) {
      toast.success('草稿已创建');
      void refresh();
    } else {
      toast.error(res.message || '创建草稿失败');
    }
  }, [createDraft, catalogType, scopeKey, refresh]);

  const handleValidate = useCallback(
    async (id: number) => {
      setBusyId(id);
      const res = await validateDraft(id);
      setBusyId(null);
      if (res.ok) {
        const errs = res.data.validation_errors;
        if (errs && errs.length > 0) {
          toast.error(`校验未通过:${errs.join('; ')}`);
        } else {
          toast.success('校验通过');
        }
        void refresh();
      } else {
        toast.error(res.message || '校验失败');
      }
    },
    [validateDraft, refresh],
  );

  const handlePublish = useCallback(
    async (id: number) => {
      setBusyId(id);
      const res = await publish(id, true);
      setBusyId(null);
      if (res.ok) {
        toast.success('已发布');
        void refresh();
      } else {
        toast.error(res.message || '发布失败');
      }
    },
    [publish, refresh],
  );

  const handleRollback = useCallback(
    async (id: number) => {
      const newCode = window.prompt('输入回滚后的新版本号(new_version_code):');
      if (!newCode || !newCode.trim()) return;
      setBusyId(id);
      const res = await rollback(id, newCode.trim(), true);
      setBusyId(null);
      if (res.ok) {
        toast.success('已回滚');
        void refresh();
      } else {
        toast.error(res.message || '回滚失败');
      }
    },
    [rollback, refresh],
  );

  return (
    <div className="space-y-6 p-3 sm:p-4 md:p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-foreground">定价中心</h1>
        <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={loading}>
          {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
          <span className="ml-1.5">刷新</span>
        </Button>
      </div>

      {/* 选择器 + 起草 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">目录选择</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-end gap-4">
            <div className="space-y-1.5">
              <Label htmlFor="catalog-type">目录类型</Label>
              <Select value={catalogType} onValueChange={(v) => setCatalogType(v as CatalogType)}>
                <SelectTrigger id="catalog-type" className="w-40">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="retail">零售(购买算力)</SelectItem>
                  <SelectItem value="procurement">进货(可售库存)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="scope-key">Scope</Label>
              <Input
                id="scope-key"
                value={scopeKey}
                onChange={(e) => setScopeKey(e.target.value)}
                placeholder="default"
                className="w-56"
              />
            </div>
            <Button onClick={() => void handleCreateDraft()} disabled={busyId === -1}>
              {busyId === -1 ? <Loader2 className="size-4 animate-spin" /> : '新建草稿'}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* 版本历史 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">版本历史</CardTitle>
        </CardHeader>
        <CardContent>
          {error && (
            <p className="mb-3 rounded-md border border-border bg-muted/40 p-3 text-sm text-muted-foreground">
              {error}
            </p>
          )}

          {loading ? (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 className="size-5 animate-spin" />
              <span className="ml-2 text-sm">加载中…</span>
            </div>
          ) : versions.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">暂无版本记录</p>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>版本号</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>生效起</TableHead>
                    <TableHead>生效止</TableHead>
                    <TableHead className="text-right">操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {versions.map((v) => {
                    const isBusy = busyId === v.id;
                    return (
                      <TableRow key={v.id}>
                        <TableCell className="font-mono text-sm">{v.version_code}</TableCell>
                        <TableCell>
                          <Badge variant={statusVariant(v.status)}>
                            {STATUS_LABEL[v.status] ?? v.status}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {v.effective_from ? formatDateTime(v.effective_from) : '—'}
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {v.effective_to ? formatDateTime(v.effective_to) : '—'}
                        </TableCell>
                        <TableCell className="text-right">
                          <div className="flex flex-wrap justify-end gap-1.5">
                            {v.status === 'draft' && (
                              <Button
                                size="sm"
                                variant="outline"
                                disabled={isBusy}
                                onClick={() => void handleValidate(v.id)}
                              >
                                校验
                              </Button>
                            )}
                            {(v.status === 'draft' || v.status === 'validated') && (
                              <Button
                                size="sm"
                                disabled={isBusy}
                                onClick={() => void handlePublish(v.id)}
                              >
                                发布
                              </Button>
                            )}
                            {v.status === 'published' && (
                              <Button
                                size="sm"
                                variant="outline"
                                disabled={isBusy}
                                onClick={() => void handleRollback(v.id)}
                              >
                                回滚
                              </Button>
                            )}
                            {isBusy && <Loader2 className="size-4 animate-spin text-muted-foreground" />}
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
