/**
 * V3.5 W4 · Admin 绑定争议处置
 *
 * 路由: /admin/binding-disputes
 * 责任: 显示 pending dispute 队列 · admin 处置 keep_old / reassign / reject
 * 历史订单 attribution 不回改 · future 生效
 */
import { useEffect, useState } from 'react';
import { adminW4Api } from '@/lib/v35w3Api';
import { formatApiErrorForDisplay } from '@/lib/api';
import { sourceLabel, statusLabel } from '@/lib/v35Terminology';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Gavel, RefreshCw, ChevronsRight, RotateCcw, X as XIcon } from 'lucide-react';
import { toast } from 'sonner';

interface Dispute {
  id: number;
  customer_user_id: number;
  customer_name?: string | null;
  old_agent_user_id: number;
  old_agent_name?: string | null;
  new_agent_user_id: number;
  new_agent_name?: string | null;
  binding_source?: string | null;
  source_token?: string | null;
  status: string;
  admin_decision?: string | null;
  note?: string | null;
  created_at: string;
  resolved_at?: string | null;
}

const STATUS_LABEL: Record<string, string> = {
  pending: '待处置',
  resolved_keep_old: '维持原服务方',
  resolved_reassign: '改绑新服务方',
  rejected: '已拒绝处理',
};

const DECISION_LABEL: Record<string, string> = {
  keep_old: '维持原服务方',
  reassign: '改绑新服务方',
  reject: '拒绝处理',
};

export default function BindingDisputes() {
  const [tab, setTab] = useState('pending');
  const [items, setItems] = useState<Dispute[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  const reload = async (status?: string) => {
    setLoading(true);
    try {
      const r = await adminW4Api.disputes(status, 100, 0);
      setItems(r.items || []);
      setTotal(r.total || 0);
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '加载失败 · 请重试', 'admin'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { reload(tab); }, [tab]);

  return (
    <div className="container mx-auto py-6 space-y-6 max-w-7xl">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Gavel className="w-6 h-6" />
          <h1 className="text-2xl font-bold">客户归属争议</h1>
        </div>
        <Button variant="ghost" size="sm" onClick={() => reload(tab)}>
          <RefreshCw className="w-4 h-4 mr-1" />刷新
        </Button>
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          {Object.entries(STATUS_LABEL).map(([k, v]) => (
            <TabsTrigger key={k} value={k}>{v}</TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value={tab} className="pt-4">
          <Card>
            <CardHeader><CardTitle className="text-base">共 {total} 条</CardTitle></CardHeader>
            <CardContent className="p-0">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/50">
                    <th className="text-left p-3">编号</th>
                    <th className="text-left p-3">客户</th>
                    <th className="text-left p-3">原服务方</th>
                    <th className="text-left p-3">新服务方</th>
                    <th className="text-left p-3">来源</th>
                    <th className="text-left p-3">状态</th>
                    <th className="text-left p-3">时间</th>
                    <th className="text-right p-3">处置</th>
                  </tr>
                </thead>
                <tbody>
                  {items.length === 0 && (
                    <tr><td colSpan={8} className="text-center p-6 text-muted-foreground">无 {STATUS_LABEL[tab]} 争议</td></tr>
                  )}
                  {items.map((d) => (
                    <tr key={d.id} className="border-b">
                      <td className="p-3 font-mono text-xs">#{d.id}</td>
                      <td className="p-3">{d.customer_name || `客户编号 ${d.customer_user_id}`}</td>
                      <td className="p-3">{d.old_agent_name || `服务方编号 ${d.old_agent_user_id}`}</td>
                      <td className="p-3">{d.new_agent_name || `服务方编号 ${d.new_agent_user_id}`}</td>
                      <td className="p-3 text-xs">
                        {d.binding_source ? sourceLabel(d.binding_source, 'admin') : '未知来源'}
                        {d.source_token ? ' · 含凭证' : ''}
                      </td>
                      <td className="p-3"><Badge variant="outline">{STATUS_LABEL[d.status] || statusLabel(d.status, 'dispute', 'admin')}</Badge></td>
                      <td className="p-3 text-xs">{new Date(d.created_at).toLocaleString()}</td>
                      <td className="p-3 text-right">
                        {d.status === 'pending' ? (
                          <ResolveButtons dispute={d} onDone={() => reload(tab)} />
                        ) : (
                          <span className="text-xs text-muted-foreground">{d.admin_decision ? (DECISION_LABEL[d.admin_decision] || '已处置') : '-'}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function ResolveButtons({ dispute, onDone }: { dispute: Dispute; onDone: () => void }) {
  const [open, setOpen] = useState<'keep' | 'reassign' | 'reject' | null>(null);

  const submit = async (action: 'keep_old' | 'reassign' | 'reject', note: string) => {
    try {
      await adminW4Api.resolveDispute(dispute.id, { action, note });
      toast.success(`已处置 · ${DECISION_LABEL[action] || '完成'}`);
      setOpen(null);
      onDone();
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '处置失败 · 请重试', 'admin'));
    }
  };

  return (
    <div className="flex gap-1 justify-end">
      <Button size="sm" variant="outline" onClick={() => setOpen('keep')}>
        <RotateCcw className="w-3 h-3 mr-1" />维持
      </Button>
      <Button size="sm" variant="default" onClick={() => setOpen('reassign')}>
        <ChevronsRight className="w-3 h-3 mr-1" />改绑
      </Button>
      <Button size="sm" variant="ghost" className="text-red-600" onClick={() => setOpen('reject')}>
        <XIcon className="w-3 h-3 mr-1" />拒绝
      </Button>
      <Dialog open={open !== null} onOpenChange={(visible) => !visible && setOpen(null)}>
        {open && (
          <NoteDialog
            title={
              open === 'reassign'
                ? `改绑给新服务方 · 编号 ${dispute.new_agent_user_id}`
                : open === 'keep'
                  ? '维持原服务归属'
                  : '拒绝本次归属申请'
            }
            onConfirm={(note) => submit(
              open === 'keep' ? 'keep_old' : open,
              note,
            )}
          />
        )}
      </Dialog>
    </div>
  );
}

function NoteDialog({ title, onConfirm }: { title: string; onConfirm: (note: string) => void }) {
  const [note, setNote] = useState('');
  return (
    <DialogContent>
      <DialogHeader><DialogTitle>{title}</DialogTitle></DialogHeader>
      <p className="text-sm text-muted-foreground">本次决定会保留关系前后版本；历史订单归属不回改。</p>
      <div>
        <Label>处置原因（必填）</Label>
        <Input value={note} onChange={(e) => setNote(e.target.value)} />
      </div>
      <DialogFooter>
        <Button disabled={note.trim().length < 2} onClick={() => onConfirm(note.trim())}>确认</Button>
      </DialogFooter>
    </DialogContent>
  );
}
