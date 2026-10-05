/**
 * 客户图片素材区(BrandImageGallery)
 * 资料中心「图片素材」· 上传客户图片 → AI 识别 → 列表 + 可用于文章开关
 * 王姐话术:不露技术黑话 · 用「门店外观 / 适合放在 / 可用于文章」
 * 2026-06-02 GEO CTO · 客户资料中心图片素材能力
 */
import { useState, useEffect } from 'react';
import { authApi } from '@/context/AuthContext';
import { toast } from 'sonner';
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { cn } from '@/lib/utils';
import { Badge } from '@/components/ui/badge';
import { Switch } from '@/components/ui/switch';
import { RefreshCw, ImagePlus, AlertTriangle, Trash2, CheckCircle2, CircleOff } from 'lucide-react';

// 技术枚举 → 王姐能懂的人话(禁黑话)
const TYPE_CN: Record<string, string> = {
  logo: '品牌标志', product: '产品图', case: '客户案例', certificate: '证书资质',
  team: '团队人物', storefront: '门店外观', environment: '经营环境', other: '其它',
};
const PLACEMENT_CN: Record<string, string> = {
  hero: '文章首图', brand_intro: '品牌介绍', product_desc: '产品说明',
  case_proof: '案例证明', not_for_external: '不适合对外',
};
const SCENARIO_CN: Record<string, string> = {
  brand_intro: '品牌介绍', product_desc: '产品说明', case_proof: '案例证明',
  team_intro: '团队介绍', environment_show: '环境展示',
};
const RISK_CN: Record<string, string> = {
  qrcode: '二维码', phone: '电话号码', privacy: '个人隐私',
  watermark: '其它平台水印', irrelevant: '与品牌无关',
};

// 磁盘相对 key(uploads/...) → 对外 URL(/uploads/...)
const toUrl = (key?: string) => (key ? (key.startsWith('/') ? key : '/' + key) : '');

/*
 * [#220 a1 ⓑ] 一批最多传几张。
 * 🔴 后端 `/api/brand-images/upload` **每请求一图**(api/image_asset_api.py 里
 *    每次都要跑一遍 describe_image_structured),没有批量端点 ——
 *    所以「批量」在前端是**顺序发 N 次**,不是一次发 N 张,也不许并发。
 * 20 这个数来自耗时:单张含识别实测约 4~5 秒,20 张 ≈ 1.5 分钟,再多用户会以为卡死。
 * Review 09-15 裁:不提示费用(该端点无扣费,提示反而误导),超出就让用户分批。
 */
const MAX_UPLOAD_BATCH = 20;

/* 「正在识别第 n / N 张」。单张时不报张数(「第 1 / 1 张」读着像出了什么事)。 */
const uploadProgressText = (p: { done: number; total: number } | null) => {
  if (!p || p.total <= 1) return '上传中...';
  return `正在识别第 ${Math.min(p.done + 1, p.total)} / ${p.total} 张`;
};

export function BrandImageGallery({
  brandId,
  embedded,
  onAssetsChange,
}: {
  brandId: number;
  embedded?: boolean;
  onAssetsChange?: (assets: any[]) => void;
}) {
  const [assets, setAssets] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [uploading, setUploading] = useState(false);
  /* [#220 a1 ⓑ] 顺序上传的进度:「正在识别第 n / N 张」。
     null = 没在上传;单张时也有值,文案里只有 N>1 才显示张数。 */
  const [uploadProgress, setUploadProgress] = useState<{ done: number; total: number } | null>(null);
  const [selectedAssetIds, setSelectedAssetIds] = useState<Set<number>>(new Set());
  const [batchUpdating, setBatchUpdating] = useState<'enable' | 'disable' | null>(null);
  const [confirmDialog, askConfirm] = useConfirmDialog();

  const fetchAssets = async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const res = await authApi.get(`/api/brand-images/list/${brandId}`);
      const nextAssets = res.data.assets || [];
      setAssets(nextAssets);
      onAssetsChange?.(nextAssets);
      const liveIds = new Set(nextAssets.map((asset: any) => Number(asset.id)));
      setSelectedAssetIds((prev) => new Set([...prev].filter((id) => liveIds.has(id))));
    } catch {
      // [D 兜底 2026-06-03] 读取失败显式置错误态 · 渲染失败提示 + 重试 · 不停在"加载中"
      setLoadError(true);
      setAssets([]);
      onAssetsChange?.([]);
      setSelectedAssetIds(new Set());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setSelectedAssetIds(new Set());
    fetchAssets();
  }, [brandId]);

  /* 单张:一次 POST。批量由下面的 handleUpload 逐张顺序调它。 */
  const uploadOne = async (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('brand_id', String(brandId));
    const res = await authApi.post('/api/brand-images/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000,
    });
    return {
      risky: Boolean(res.data?.asset?.risk_flags?.length),
      visionMissed: res.data?.vision_ok === false,
    };
  };

  /*
   * [#220 a1 ⓑ] 一次可选多张,**顺序**上传。
   * 🔴 不能 Promise.all:后端每张都要跑一次视觉识别,并发过去等于同时压 N 个识别任务。
   * 🔴 中间某张失败**不许中断**剩下的 —— 用户选了 10 张,不该因为第 3 张坏了就只传到 2 张
   *    还不告诉他是哪张坏了。所以逐张 try,最后一次性报「成功 M · 失败 N(文件名)」。
   * 🔴 列表只在**全部结束后**刷一次;每张都刷会把 N 次识别变成 N 次列表请求,
   *    而且后到的响应会覆盖先到的(竞态)。
   */
  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files || []);
    e.target.value = '';           // 先清,免得选同一批文件第二次不触发 change
    if (!picked.length) return;

    const files = picked.slice(0, MAX_UPLOAD_BATCH);
    if (picked.length > MAX_UPLOAD_BATCH) {
      toast.warning(`一次最多传 ${MAX_UPLOAD_BATCH} 张,这次先传前 ${MAX_UPLOAD_BATCH} 张,剩下的请再传一批`);
    }

    setUploading(true);
    setUploadProgress({ done: 0, total: files.length });
    let okCount = 0;
    let riskyCount = 0;
    let visionMissedCount = 0;
    const failed: string[] = [];

    for (let i = 0; i < files.length; i += 1) {
      setUploadProgress({ done: i, total: files.length });
      try {
        const r = await uploadOne(files[i]);
        okCount += 1;
        if (r.risky) riskyCount += 1;
        if (r.visionMissed) visionMissedCount += 1;
      } catch (err: any) {
        const d = err.response?.data?.detail || err.response?.data?.error;
        failed.push(`${files[i].name}${typeof d === 'string' ? `(${d})` : ''}`);
      }
    }

    setUploadProgress(null);
    setUploading(false);

    if (okCount) {
      const extra = [
        riskyCount ? `${riskyCount} 张可能含敏感内容,默认不对外` : '',
        visionMissedCount ? `${visionMissedCount} 张 AI 识别未完成,可手动补充说明` : '',
      ].filter(Boolean).join(' · ');
      const head = files.length > 1 ? `成功 ${okCount} 张` : '上传成功 · AI 已识别';
      if (riskyCount) toast.warning(extra ? `${head} · ${extra}` : head);
      else toast.success(extra ? `${head} · ${extra}` : head);
    }
    if (failed.length) {
      toast.error(`失败 ${failed.length} 张:${failed.join('、')}`);
    }
    /* 🔴 成功一张都没有也要刷:失败原因可能是重复文件,列表里那张是存在的 */
    fetchAssets();
  };

  // 「可用于文章」开关:打开 = 用户知情确认可外发(同步 publish_allowed + rights_confirmed)
  const handleToggle = async (asset: any, on: boolean) => {
    // 🔴 开启需显式确认可对外发布(老板规则·未确认不得 publish_allowed=1)
    if (on && !(await askConfirm({ title: '我确认这张图片已获授权,可以用于对外发布的文章中。', description: '含二维码 / 手机号 / 个人隐私 / 他人平台水印的图片,请勿开启' }))) {
      return;
    }
    try {
      await authApi.patch(
        `/api/brand-images/asset/${asset.id}`,
        on
          ? { publish_allowed: 1, rights_confirmed: 1 }
          : { publish_allowed: 0 },
      );
      fetchAssets();
    } catch { toast.error('更新失败'); }
  };

  const handleDelete = async (asset: any) => {
    if (!(await askConfirm({ title: '确定删除这张图片？', confirmLabel: '删除', danger: true }))) return;
    try {
      await authApi.delete(`/api/brand-images/asset/${asset.id}`);
      toast.success('已删除');
      setSelectedAssetIds((prev) => {
        const next = new Set(prev);
        next.delete(Number(asset.id));
        return next;
      });
      fetchAssets();
    } catch { toast.error('删除失败'); }
  };

  const toggleAssetSelection = (assetId: number, checked: boolean) => {
    setSelectedAssetIds((prev) => {
      const next = new Set(prev);
      if (checked) next.add(assetId);
      else next.delete(assetId);
      return next;
    });
  };

  const toggleAllAssets = (checked: boolean) => {
    setSelectedAssetIds(checked ? new Set(assets.map((asset) => Number(asset.id))) : new Set());
  };

  const handleBatchDelete = async () => {
    const assetIds = [...selectedAssetIds];
    if (assetIds.length === 0) return;
    if (!(await askConfirm({ title: `确定删除选中的 ${assetIds.length} 张图片？`, confirmLabel: '删除', danger: true }))) return;
    try {
      const results = await Promise.allSettled(
        assetIds.map((assetId) => authApi.delete(`/api/brand-images/asset/${assetId}`))
      );
      const failed = results.filter((r) => r.status === 'rejected').length;
      if (failed > 0) {
        toast.error(`${failed} 张删除失败，请稍后重试`);
      } else {
        toast.success(`已删除 ${assetIds.length} 张图片`);
      }
      setSelectedAssetIds(new Set());
      fetchAssets();
    } catch {
      toast.error('删除失败');
    }
  };

  const handleBatchArticleUsage = async (enabled: boolean) => {
    const assetIds = [...selectedAssetIds];
    if (assetIds.length === 0) return;
    if (enabled) {
      const confirmed = await askConfirm({
        title: `确认所选 ${assetIds.length} 张图片均已获授权？`,
        description: '系统会跳过含二维码、手机号、个人隐私、平台水印等风险的图片；其余图片可用于对外发布的文章。',
        confirmLabel: '确认授权并用于文章',
        cancelLabel: '取消',
      });
      if (!confirmed) return;
    }

    setBatchUpdating(enabled ? 'enable' : 'disable');
    try {
      const response = await authApi.post('/api/brand-images/assets/article-usage', {
        asset_ids: assetIds,
        enabled,
        rights_confirmed: enabled,
      });
      const result = response.data || {};
      const parts = [
        `${enabled ? '已用于文章' : '已取消'} ${result.updated || 0} 张`,
      ];
      if (result.skipped) parts.push(`跳过 ${result.skipped} 张`);
      if (result.failed) parts.push(`失败 ${result.failed} 张`);
      if (result.failed) toast.error(parts.join(' · '));
      else if (result.skipped) toast.warning(parts.join(' · '));
      else toast.success(parts.join(' · '));

      const skippedReasons = (result.items || [])
        .filter((item: any) => item.status === 'skipped' && item.reason !== '已经是目标状态')
        .map((item: any) => item.reason)
        .filter(Boolean);
      if (skippedReasons.length > 0) {
        toast.info([...new Set(skippedReasons)].join('；'));
      }
      setSelectedAssetIds(new Set());
      await fetchAssets();
    } catch (error: any) {
      const detail = error.response?.data?.detail;
      toast.error(typeof detail === 'string' ? detail : '批量更新失败，请稍后重试');
    } finally {
      setBatchUpdating(null);
    }
  };

  const selectedCount = selectedAssetIds.size;
  const allSelected = assets.length > 0 && assets.every((asset) => selectedAssetIds.has(Number(asset.id)));

  return (
    /* [2026-06-04] embedded:外层 section 已是卡(编号卡头+副标),去掉自带边框/标题/描述,避免卡中卡双标题 */
    <div className={embedded ? 'space-y-3' : 'space-y-3 rounded-2xl border border-border/60 bg-card/40 p-3 sm:p-4'}>
      {confirmDialog}
      <div className={cn('flex flex-col gap-3 sm:flex-row sm:items-center', embedded ? 'sm:justify-end' : 'sm:justify-between')}>
        {!embedded && (
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-foreground">图片素材</h3>
              <Badge variant="outline" className="text-[10px]">{assets.length} 张</Badge>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              上传门店、产品、案例、LOGO 等图片,AI 会识别内容,写文章时自动配上合适的图。
            </p>
          </div>
        )}
        {/* 🔴 上传中整块 label 也要变成不可点:input `disabled` 只挡住键盘/程序触发,
            点 label 仍会打开选择框 —— 这是本单顺手修的既有洞(批量后会放大成两批交错)。 */}
        <label className={cn(
          'flex min-h-[44px] w-full items-center justify-center gap-2 rounded-xl border border-dashed border-border/70 px-4 py-2 text-xs text-muted-foreground transition-colors sm:w-auto sm:min-w-[180px]',
          uploading ? 'cursor-not-allowed opacity-60' : 'cursor-pointer hover:bg-muted/30',
        )}>
          <input
            type="file" multiple disabled={uploading}
            accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"
            onChange={handleUpload} className="hidden"
          />
          {uploading ? (
            <><RefreshCw className="h-3.5 w-3.5 animate-spin" />{uploadProgressText(uploadProgress)}</>
          ) : (
            <><ImagePlus className="h-3.5 w-3.5" />上传客户图片</>
          )}
        </label>
      </div>

      {loading ? (
        <p className="text-xs text-muted-foreground text-center py-4">加载中...</p>
      ) : loadError ? (
        /* [D 兜底 2026-06-03] 读取失败显式提示 + 重试 · 不无限"加载中" */
        <div className="rounded-xl border border-dashed border-rose-500/40 bg-rose-500/5 py-6 text-center">
          <p className="text-sm text-rose-500">加载失败，请点重试</p>
          <button
            type="button"
            onClick={() => { void fetchAssets(); }}
            className="mt-2 inline-flex items-center gap-1 rounded-md border border-border/60 px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted/30"
          >
            <RefreshCw className="h-3.5 w-3.5" />重试
          </button>
        </div>
      ) : assets.length === 0 ? (
        /* [A 空状态可点 2026-06-03] 整块 label 包 hidden input · 点任意处即触发上传 · 复用 handleUpload */
        <label className={cn(
          'block rounded-xl border border-dashed border-border/50 bg-background/40 py-6 text-center transition-colors',
          uploading ? 'cursor-not-allowed opacity-60' : 'cursor-pointer hover:bg-muted/30',
        )}>
          <input
            type="file" multiple disabled={uploading}
            accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"
            onChange={handleUpload} className="hidden"
          />
          <p className="text-sm text-muted-foreground">
            {uploading ? uploadProgressText(uploadProgress) : '还没有上传图片 · 点这里上传'}
          </p>
          <p className="mt-1 text-xs text-muted-foreground/70">放上门店、产品、案例图,写作会自动配图。</p>
        </label>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border/30 bg-background/40 px-3 py-2">
            <label className="inline-flex min-h-[32px] cursor-pointer items-center gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                className="h-4 w-4 rounded border-border bg-background"
                checked={allSelected}
                onChange={(e) => toggleAllAssets(e.target.checked)}
              />
              <span>{allSelected ? '取消全选' : '全选'}</span>
              <span className="text-muted-foreground/70">已选 {selectedCount} / {assets.length} 张</span>
            </label>
            <div className="flex flex-wrap items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => { void handleBatchArticleUsage(true); }}
                disabled={selectedCount === 0 || batchUpdating !== null}
                className={cn(
                  'inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border px-3 text-xs transition-colors',
                  selectedCount === 0 || batchUpdating !== null
                    ? 'cursor-not-allowed border-border/40 text-muted-foreground/50'
                    : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 hover:bg-emerald-500/15'
                )}
              >
                {batchUpdating === 'enable' ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                用于文章
              </button>
              <button
                type="button"
                onClick={() => { void handleBatchArticleUsage(false); }}
                disabled={selectedCount === 0 || batchUpdating !== null}
                className={cn(
                  'inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border px-3 text-xs transition-colors',
                  selectedCount === 0 || batchUpdating !== null
                    ? 'cursor-not-allowed border-border/40 text-muted-foreground/50'
                    : 'border-border/70 text-muted-foreground hover:bg-muted/40'
                )}
              >
                {batchUpdating === 'disable' ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <CircleOff className="h-3.5 w-3.5" />}
                取消用于文章
              </button>
              <button
                type="button"
                onClick={handleBatchDelete}
                disabled={selectedCount === 0 || batchUpdating !== null}
                className={cn(
                  'inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border px-3 text-xs transition-colors',
                  selectedCount === 0 || batchUpdating !== null
                    ? 'cursor-not-allowed border-border/40 text-muted-foreground/50'
                    : 'border-red-500/30 bg-red-500/10 text-red-500 hover:bg-red-500/15'
                )}
              >
                <Trash2 className="h-3.5 w-3.5" />
                删除选中
              </button>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {assets.map((a: any) => {
              const assetId = Number(a.id);
              const selected = selectedAssetIds.has(assetId);
              const usable = a.publish_allowed === 1 && a.rights_confirmed === 1;
              const displayName = a.title || a.file_name || `图片 ${assetId}`;
              const risks: string[] = a.risk_flags || [];
              const scenarios: string[] = a.usage_scenarios || [];
              const placeText = [PLACEMENT_CN[a.suggested_placement], ...scenarios.map((s) => SCENARIO_CN[s])]
                .filter(Boolean)
                .filter((v, i, arr) => arr.indexOf(v) === i)
                .join(' / ');
              return (
                <div
                  key={a.id}
                  className={cn(
                    'flex gap-3 rounded-xl border bg-background/40 p-2.5 transition-colors',
                    selected ? 'border-emerald-500/50 bg-emerald-500/5' : 'border-border/30'
                  )}
                >
                  <div className="relative h-20 w-20 shrink-0 overflow-hidden rounded-lg border border-border/40 bg-muted/30">
                    <img src={toUrl(a.thumbnail_key) || a.public_url} alt={a.alt_text || a.title || ''}
                         className="h-full w-full object-cover" loading="lazy" />
                    <label className="absolute left-1.5 top-1.5 inline-flex h-6 w-6 cursor-pointer items-center justify-center rounded-md bg-background/85 shadow-sm">
                      <input
                        type="checkbox"
                        className="h-4 w-4 rounded border-border bg-background"
                        checked={selected}
                        aria-label={`选择图片 ${a.title || a.file_name || a.id}`}
                        onChange={(e) => toggleAssetSelection(assetId, e.target.checked)}
                      />
                    </label>
                  </div>
                  <div className="flex min-w-0 flex-1 flex-col gap-1">
                    <div className="flex min-w-0 flex-col items-stretch gap-1.5 lg:flex-row lg:items-start">
                      <details
                        data-testid={`image-name-disclosure-${assetId}`}
                        className="group/name min-w-0 flex-1"
                      >
                        <summary
                          className="min-h-11 cursor-pointer list-none rounded py-1 text-xs font-medium text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand [&::-webkit-details-marker]:hidden"
                          title={displayName}
                          aria-label={`查看完整图片名称 ${displayName}`}
                        >
                          <span className="line-clamp-2 break-words sm:block sm:truncate">{displayName}</span>
                          <span className="mt-0.5 block text-[10px] font-normal text-muted-foreground group-open/name:hidden">点击查看全称</span>
                        </summary>
                        <p className="mt-1 break-all rounded bg-muted/40 px-2 py-1.5 text-[11px] leading-4 text-foreground">
                          {displayName}
                        </p>
                      </details>
                      {a.image_type && (
                        <Badge variant="secondary" className="shrink-0 self-start text-[9px]">
                          AI 已识别:{TYPE_CN[a.image_type] || a.image_type}
                        </Badge>
                      )}
                    </div>
                    {placeText && <p className="text-[11px] text-muted-foreground">适合放在:{placeText}</p>}
                    {risks.length > 0 && (
                      <p className="flex items-center gap-1 text-[11px] text-red-500">
                        <AlertTriangle className="h-3 w-3 shrink-0" />
                        检测到{risks.map((r) => RISK_CN[r] || r).join('、')},默认不对外
                      </p>
                    )}
                    <div className="mt-auto flex items-center justify-between gap-2 pt-1">
                      {/* 2026-06-03:可点击 button 改 Switch 滑块 · 开=可用 / 关=不可用 · 逻辑完全复用 handleToggle */}
                      <label className="inline-flex cursor-pointer items-center gap-1.5">
                        <Switch
                          checked={usable}
                          onCheckedChange={(on) => handleToggle(a, on)}
                        />
                        <span className={cn('text-[11px]', usable ? 'text-emerald-600' : 'text-muted-foreground')}>
                          可用于文章
                        </span>
                      </label>
                      <button onClick={() => handleDelete(a)}
                              className="rounded-md px-2 py-1 text-[11px] text-red-400/70 hover:text-red-400">
                        删除
                      </button>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
