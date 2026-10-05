/**
 * 我的品牌 — 加载 self 品牌后跳转到 BrandDetailPage
 */
import { useEffect, useState, useCallback } from 'react';
import { authApi } from '@/context/AuthContext';
import BrandDetailPage from './BrandDetailPage';

export default function MyBrandPage() {
  const [brandId, setBrandId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [needSetup, setNeedSetup] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setNeedSetup(false);
    try {
      const res = await authApi.get('/api/my-brand');
      if (res.data.need_setup) {
        setNeedSetup(true);
      } else if (res.data.brand) {
        setBrandId(res.data.brand.id);
      } else {
        setError('后端返回了意外的响应格式');
      }
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || '品牌加载失败，请检查网络');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) return <div className="flex items-center justify-center h-64 text-muted-foreground text-sm">加载中...</div>;

  if (needSetup) {
    return (
      <div className="flex items-center justify-center h-64 text-center">
        <div>
          <p className="text-foreground text-lg font-semibold mb-2">还没有创建你的品牌</p>
          <p className="text-muted-foreground text-sm mb-4">前往客户管理创建你的第一个品牌</p>
          <a href="/my-clients" className="text-sm text-foreground underline">去客户管理</a>
        </div>
      </div>
    );
  }

  if (error || !brandId) {
    return (
      <div className="flex items-center justify-center h-64 text-center">
        <div className="space-y-3">
          <p className="text-foreground text-lg font-semibold">品牌加载失败</p>
          {error && <p className="text-muted-foreground text-sm max-w-md">{error}</p>}
          <button
            type="button"
            onClick={load}
            className="inline-flex items-center gap-2 rounded-lg border border-border px-4 py-2 text-sm hover:bg-muted/40 transition-colors cursor-pointer"
          >
            重新加载
          </button>
        </div>
      </div>
    );
  }

  return <BrandDetailPage overrideBrandId={brandId} mode="self" />;
}
