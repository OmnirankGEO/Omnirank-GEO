/**
 * AgreementViewer — 代理合作协议全文页（支持历史版本查看）
 *
 * URL: /partner/agreement/:version
 *
 * - flag off 时也可访问（老代理可查自己签过的 v1.0）
 * - 公开 markdown 渲染, 不涉及签署操作
 */

import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ArrowLeft, Loader2 } from 'lucide-react';
import ReactMarkdown from '@/components/SafeMarkdown';
import { Button } from '@/components/ui/button';
import { authFetch } from '@/lib/api';

export default function AgreementViewer() {
  const { version = 'v2.0' } = useParams<{ version: string }>();
  const [text, setText] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const resp = await authFetch(`/api/partner/agreement/${version}`);
        if (!resp.ok) {
          setError(resp.status === 404 ? '协议版本不存在' : '加载失败');
          return;
        }
        const data = await resp.json();
        setText(data.text || '');
      } catch (e) {
        setError((e as Error)?.message || '网络错误');
      } finally {
        setLoading(false);
      }
    })();
  }, [version]);

  return (
    <div className="max-w-3xl mx-auto p-6 space-y-4">
      <div className="flex items-center gap-2">
        <Button asChild variant="ghost" size="sm">
          <Link to="/partner/status">
            <ArrowLeft className="h-4 w-4 mr-1" />
            返回
          </Link>
        </Button>
        <div className="flex-1">
          <h1 className="text-lg font-semibold">服务商申请协议 {version}</h1>
        </div>
      </div>

      {loading && (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      )}

      {error && (
        <div className="text-sm text-red-400 p-4 border border-red-500/30 bg-red-500/5 rounded-lg">
          {error}
        </div>
      )}

      {!loading && !error && (
        <div className="prose prose-sm dark:prose-invert max-w-none border border-border rounded-lg p-6 bg-card">
          <ReactMarkdown>{text}</ReactMarkdown>
        </div>
      )}
    </div>
  );
}
