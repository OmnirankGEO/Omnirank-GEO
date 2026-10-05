/**
 * 管理员后台 — GEO 托管套餐管理 — /admin/managed
 *
 * 功能：
 *   - 查看任意套餐
 *   - 手动触发 dormancy 扫描
 */

import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Loader2, Sparkles, Search, Activity } from 'lucide-react';
import { toast } from 'sonner';

import { authFetch } from '@/lib/api';
import { CampaignDashboard } from '@/components/managed';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

export default function ManagedCampaignsAdmin() {
  const [confirmDialog, askConfirm] = useConfirmDialog();
  const [searchId, setSearchId] = useState('');
  const [currentId, setCurrentId] = useState<number | null>(null);
  const [scanLoading, setScanLoading] = useState(false);

  function handleSearch() {
    const n = parseInt(searchId);
    if (!n) {
      toast.error('请输入有效套餐 ID');
      return;
    }
    setCurrentId(n);
  }

  async function triggerDormancyScan() {
    if (!(await askConfirm({ title: '立即扫描 12 个月休眠套餐？符合条件的会自动转赠送算力。' }))) return;
    setScanLoading(true);
    try {
      const res = await authFetch('/api/admin/managed/dormancy-scan', { method: 'POST' });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      toast.success(data.message || '已触发扫描');
    } catch (e: any) {
      toast.error(e?.message || '触发失败');
    } finally {
      setScanLoading(false);
    }
  }

  return (
    <div className="container max-w-4xl mx-auto py-6 space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-amber-500" />
            管理员 — GEO 托管套餐管理
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex gap-2">
            <input
              type="number"
              className="flex-1 rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              placeholder="输入套餐 ID"
              value={searchId}
              onChange={(e) => setSearchId(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
            />
            <Button onClick={handleSearch}>
              <Search className="h-4 w-4 mr-1" /> 查看
            </Button>
          </div>

          <div className="flex gap-2">
            <Button variant="outline" onClick={triggerDormancyScan} disabled={scanLoading}>
              {scanLoading && <Loader2 className="h-4 w-4 animate-spin mr-1" />}
              <Activity className="h-4 w-4 mr-1" />
              立即触发 12 个月休眠扫描
            </Button>
          </div>
        </CardContent>
      </Card>

      {currentId && (
        <CampaignDashboard campaignId={currentId} onClose={() => setCurrentId(null)} />
      )}
      {confirmDialog}
    </div>
  );
}
