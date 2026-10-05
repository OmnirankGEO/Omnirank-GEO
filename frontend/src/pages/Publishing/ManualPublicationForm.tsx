/**
 * ManualPublicationForm — 手动确认已发布 form
 *
 * P0.4b · 代理在平台外发布后（线下/微信群/邮件外投）手动补录 media_publications
 * POST /api/publications/manual
 *
 * 接入位置：PublishCenter 顶部 Tab "手动确认"
 * URL 深链：/publish?mode=manual&article_id=X&quote_id=Y → 预填对应字段
 *
 * 状态(2026-04-26 trust-layer 复核):
 * - OSS bucket `omnirank-publish-evidence` 已建 + CORS 已配 + 预签名直传已接通(commit b2373f1)
 * - 截图字段:选图 → /api/publications/upload-screenshot/sign 拿预签名 → PUT 直传 OSS
 * - 兜底:手动粘贴任意图床 URL 仍可用
 */

import { useState, useEffect, useCallback } from 'react';
import { toast } from 'sonner';
import { authFetch } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Loader2, CheckCircle2, ExternalLink, Upload } from 'lucide-react';

interface QuoteOption {
  id: number;
  brand_name: string;
  monthly_price?: number;
  industry?: string;
  city?: string;
  status?: string;
}

interface ArticleOption {
  id: number;
  title: string;
  keyword?: string;
  article_style?: string;
}

interface Props {
  /** URL 预填：article_id */
  defaultArticleId?: number;
  /** URL 预填：quote_id */
  defaultQuoteId?: number;
  /** 提交成功回调 · 用于跳回 history 或 toast */
  onSubmitted?: (publicationId: number, isNew: boolean) => void;
}

const COMMON_PLATFORMS = [
  '小红书',
  '微信公众号',
  '知乎',
  '微博',
  '百家号',
  '搜狐网',
  '今日头条',
  '网易号',
  '豆瓣',
  'CSDN',
  '掘金',
  '其他',
];

export function ManualPublicationForm({ defaultArticleId, defaultQuoteId, onSubmitted }: Props) {
  const [quotes, setQuotes] = useState<QuoteOption[]>([]);
  const [articles, setArticles] = useState<ArticleOption[]>([]);
  const [loadingQuotes, setLoadingQuotes] = useState(false);
  const [loadingArticles, setLoadingArticles] = useState(false);

  // Form fields
  const [quoteId, setQuoteId] = useState<number | ''>(defaultQuoteId ?? '');
  const [articleId, setArticleId] = useState<number | ''>(defaultArticleId ?? '');
  const [platformName, setPlatformName] = useState('');
  const [platformOther, setPlatformOther] = useState('');
  const [platformUrl, setPlatformUrl] = useState('');
  const [articleTitle, setArticleTitle] = useState('');
  const [publishDate, setPublishDate] = useState<string>(() => new Date().toISOString().slice(0, 10));
  const [screenshotPath, setScreenshotPath] = useState('');
  // P0.4b.2 (CTO-15.9 session 3 · 2026-04-25) · OSS 直传截图状态
  const [uploading, setUploading] = useState(false);
  const [notes, setNotes] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [lastResult, setLastResult] = useState<{ publicationId: number; isNew: boolean; firstPublished: boolean } | null>(null);

  // ========== 加载 quotes ==========
  useEffect(() => {
    const loadQuotes = async () => {
      setLoadingQuotes(true);
      try {
        // /api/quotes 支持 status 过滤 · 默认只看有效报价(P0.5b 软删已过滤)
        const res = await authFetch('/api/quotes?limit=100');
        const data = await res.json();
        const items: QuoteOption[] = data.items || data.data || [];
        setQuotes(items);
        if (defaultQuoteId && items.some(q => q.id === defaultQuoteId)) {
          setQuoteId(defaultQuoteId);
        }
      } catch (e) {
        console.error('加载报价列表失败:', e);
      } finally {
        setLoadingQuotes(false);
      }
    };
    loadQuotes();
  }, [defaultQuoteId]);

  // ========== 加载 articles(按 quote 过滤) ==========
  // Codex bug 3 修: /api/articles?project_id=X 不存在 · 改用 /api/writing/projects/{quote_id}
  // 后端 get_writing_project_detail 返 {quote, keywords, topics} · topics 含 article_id + optimized_title
  const loadArticlesForQuote = useCallback(async (qid: number) => {
    setLoadingArticles(true);
    try {
      const res = await authFetch(`/api/writing/projects/${qid}`);
      if (!res.ok) {
        setArticles([]);
        return;
      }
      const data = await res.json();
      const topics: Array<Record<string, any>> = data.topics || [];
      // topics → ArticleOption · 只保留有 article_id(已生成文章)的 topic
      const items: ArticleOption[] = topics
        .filter(t => t.article_id)
        .map(t => ({
          id: t.article_id,
          title: t.optimized_title || t.title || `文章 #${t.article_id}`,
          keyword: t.keyword || '',
          article_style: t.article_style || '',
        }));
      setArticles(items);
      if (defaultArticleId && items.some(a => a.id === defaultArticleId)) {
        setArticleId(defaultArticleId);
        const match = items.find(a => a.id === defaultArticleId);
        if (match && match.title) setArticleTitle(match.title);
      }
    } catch (e) {
      console.error('加载文章列表失败:', e);
      setArticles([]);
    } finally {
      setLoadingArticles(false);
    }
  }, [defaultArticleId]);

  useEffect(() => {
    if (typeof quoteId === 'number' && quoteId > 0) {
      loadArticlesForQuote(quoteId);
    } else {
      setArticles([]);
      setArticleId('');
    }
  }, [quoteId, loadArticlesForQuote]);

  // 选文章时自动填 title
  useEffect(() => {
    if (typeof articleId === 'number' && articles.length > 0) {
      const match = articles.find(a => a.id === articleId);
      if (match && match.title && !articleTitle) {
        setArticleTitle(match.title);
      }
    }
  }, [articleId, articles, articleTitle]);

  // ========== 校验 ==========
  const resolvedPlatform = platformName === '其他' ? platformOther.trim() : platformName;
  const urlValid = (() => {
    if (!platformUrl.trim()) return false;
    try {
      const u = new URL(platformUrl.trim());
      return u.protocol === 'http:' || u.protocol === 'https:';
    } catch {
      return false;
    }
  })();

  const canSubmit =
    typeof quoteId === 'number' &&
    quoteId > 0 &&
    resolvedPlatform.length > 0 &&
    urlValid &&
    !submitting;

  // ========== 提交 ==========
  const handleSubmit = async () => {
    if (!canSubmit) {
      toast.error('请填写报价、平台和有效的发布 URL');
      return;
    }
    setSubmitting(true);
    setLastResult(null);
    try {
      const body: Record<string, unknown> = {
        quote_id: quoteId,
        platform_name: resolvedPlatform,
        platform_url: platformUrl.trim(),
      };
      if (typeof articleId === 'number' && articleId > 0) body.article_id = articleId;
      if (articleTitle.trim()) body.article_title = articleTitle.trim();
      if (publishDate) body.publish_date = publishDate;
      if (screenshotPath.trim()) body.screenshot_path = screenshotPath.trim();
      if (notes.trim()) body.notes = notes.trim();

      const res = await authFetch('/api/publications/manual', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });

      const raw = await res.text();
      let data: Record<string, unknown> = {};
      try { data = raw ? JSON.parse(raw) : {}; } catch { data = {}; }

      if (!res.ok) {
        const detail = typeof data.detail === 'string' ? data.detail : null;
        if (res.status === 401) toast.error('登录已过期 · 请重新登录');
        else if (res.status === 403) toast.error(detail || '该报价不属于当前服务方 · 无权限');
        else if (res.status === 400) toast.error(detail || '请检查必填字段');
        else toast.error(detail || '提交失败 · 请稍后重试');
        return;
      }

      const publicationId = data.publication_id as number;
      const isNew = data.is_new === true;
      const firstPublished = data.first_published === true;
      setLastResult({ publicationId, isNew, firstPublished });

      if (isNew) {
        toast.success(
          firstPublished
            ? `已补录 · publication #${publicationId} · 同时更新首次发布时间`
            : `已补录 · publication #${publicationId}`
        );
      } else {
        toast.info(`该 URL 已登记过 · 返已有发布记录 #${publicationId}`);
      }

      // 清空 URL + screenshot + notes · 保留 quote/article 方便连续补录
      setPlatformUrl('');
      setScreenshotPath('');
      setNotes('');

      onSubmitted?.(publicationId, isNew);
    } catch (e) {
      console.error('手动确认发布失败:', e);
      toast.error('提交失败 · 网络错误');
    } finally {
      setSubmitting(false);
    }
  };

  // ========== UI ==========
  return (
    <div className="p-4 md:p-6 space-y-4 max-w-3xl mx-auto">
      <Card className="border border-border">
        <CardHeader className="p-5 pb-3">
          <CardTitle className="text-lg flex items-center gap-2">
            <CheckCircle2 className="size-5 text-green-600" />
            手动确认已发布
          </CardTitle>
          <p className="text-sm text-muted-foreground mt-1">
            平台外已发稿（微信群、邮件外投、代投等）补录发布证据 · 写入 media_publications · 同步首次发布时间
          </p>
        </CardHeader>
        <CardContent className="p-5 pt-0 space-y-4">
          {/* OSS 直传已接通 · 仅作信息提示 */}
          <div className="flex items-start gap-2 p-3 rounded-md bg-emerald-500/10 border border-emerald-500/30 text-xs">
            <CheckCircle2 className="size-4 text-emerald-600 shrink-0 mt-0.5" />
            <div>
              <div className="font-medium text-emerald-700 dark:text-emerald-300">截图直传已接通</div>
              <div className="text-emerald-700/80 dark:text-emerald-300/80">
                直接选图自动上传到平台云存储 · 也可手动粘贴任意图床 URL 兜底。
              </div>
            </div>
          </div>

          {/* 报价 quote_id */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">
              报价 <span className="text-destructive">*</span>
            </label>
            <select
              value={quoteId}
              onChange={e => setQuoteId(e.target.value ? Number(e.target.value) : '')}
              disabled={loadingQuotes}
              className="w-full px-3 py-2 rounded-md border border-border bg-background text-sm"
            >
              <option value="">{loadingQuotes ? '加载中…' : '选择报价（必填）'}</option>
              {quotes.map(q => (
                <option key={q.id} value={q.id}>
                  #{q.id} · {q.brand_name}
                  {q.monthly_price ? ` · ¥${q.monthly_price}` : ''}
                  {q.industry ? ` · ${q.industry}` : ''}
                  {q.status ? ` · ${q.status}` : ''}
                </option>
              ))}
            </select>
          </div>

          {/* 文章 article_id(可选) */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">关联文章（可选）</label>
            <select
              value={articleId}
              onChange={e => {
                const v = e.target.value ? Number(e.target.value) : '';
                setArticleId(v);
                if (typeof v === 'number') {
                  const m = articles.find(a => a.id === v);
                  if (m?.title) setArticleTitle(m.title);
                }
              }}
              disabled={typeof quoteId !== 'number' || loadingArticles}
              className="w-full px-3 py-2 rounded-md border border-border bg-background text-sm"
            >
              <option value="">
                {typeof quoteId !== 'number'
                  ? '先选报价'
                  : loadingArticles
                    ? '加载中…'
                    : articles.length === 0
                      ? '（该报价无已完成文章 · 可跳过)'
                      : '选择文章（可选）'}
              </option>
              {articles.map(a => (
                <option key={a.id} value={a.id}>
                  #{a.id} · {a.title}
                  {a.keyword ? ` · ${a.keyword}` : ''}
                </option>
              ))}
            </select>
          </div>

          {/* 平台名 */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">
                发布平台 <span className="text-destructive">*</span>
              </label>
              <select
                value={platformName}
                onChange={e => setPlatformName(e.target.value)}
                className="w-full px-3 py-2 rounded-md border border-border bg-background text-sm"
              >
                <option value="">选择平台</option>
                {COMMON_PLATFORMS.map(p => (
                  <option key={p} value={p}>{p}</option>
                ))}
              </select>
              {platformName === '其他' && (
                <Input
                  value={platformOther}
                  onChange={e => setPlatformOther(e.target.value)}
                  placeholder="手动输入平台名"
                  className="mt-2"
                />
              )}
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">发布日期</label>
              <Input
                type="date"
                value={publishDate}
                onChange={e => setPublishDate(e.target.value)}
              />
            </div>
          </div>

          {/* URL */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">
              发布 URL <span className="text-destructive">*</span>
            </label>
            <div className="flex gap-2">
              <Input
                value={platformUrl}
                onChange={e => setPlatformUrl(e.target.value)}
                placeholder="https://www.example.com/article/123"
                className={!platformUrl || urlValid ? '' : 'border-destructive'}
              />
              {platformUrl && urlValid && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => window.open(platformUrl.trim(), '_blank', 'noopener,noreferrer')}
                  title="在新标签打开"
                >
                  <ExternalLink className="size-4" />
                </Button>
              )}
            </div>
            {platformUrl && !urlValid && (
              <p className="text-xs text-destructive">需要合法的 http(s) URL</p>
            )}
          </div>

          {/* 文章标题 */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">文章标题（可选 · 不关联 article 时填）</label>
            <Input
              value={articleTitle}
              onChange={e => setArticleTitle(e.target.value)}
              placeholder="例：罗平装修攻略避坑 8 条 · 实战版"
            />
          </div>

          {/* 截图 · 直传 OSS(P0.4b.2 · 老板 2026-04-25 建好 bucket) */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium flex items-center gap-1.5">
              <Upload className="size-3.5" />
              截图证据(直接上传 · 推荐)
            </label>
            <div className="flex items-center gap-2">
              <input
                type="file"
                accept="image/jpeg,image/png,image/webp"
                onChange={async (e) => {
                  const file = e.target.files?.[0];
                  if (!file) return;
                  if (file.size > 10 * 1024 * 1024) {
                    toast.error('截图过大 · 请压缩到 10MB 以内');
                    return;
                  }
                  if (!quoteId || quoteId <= 0) {
                    toast.error('请先选择/填入 quote_id');
                    return;
                  }
                  const ext = (file.name.split('.').pop() || 'jpg').toLowerCase();
                  setUploading(true);
                  try {
                    // 1. 拿预签名 URL
                    const signRes = await authFetch('/api/publications/upload-screenshot/sign', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({
                        quote_id: quoteId,
                        article_id: articleId || null,
                        ext,
                        content_type: file.type || 'image/jpeg',
                      }),
                    });
                    const signData = await signRes.json();
                    if (!signRes.ok || !signData?.upload_url) {
                      toast.error(signData?.detail || '获取上传链接失败');
                      return;
                    }
                    // 2. PUT 直传 OSS
                    const putRes = await fetch(signData.upload_url, {
                      method: 'PUT',
                      body: file,
                      headers: { 'Content-Type': signData.content_type },
                    });
                    if (!putRes.ok) {
                      toast.error(`OSS 上传失败 HTTP ${putRes.status}`);
                      return;
                    }
                    // 3. 写入 oss_key 字段
                    setScreenshotPath(signData.oss_key);
                    toast.success(`截图已上传 · ${signData.oss_key.split('/').pop()}`);
                  } catch (err) {
                    toast.error('上传失败 · ' + (err instanceof Error ? err.message : ''));
                  } finally {
                    setUploading(false);
                  }
                }}
                disabled={uploading || !quoteId}
                className="text-sm"
              />
              {uploading && <Loader2 className="size-4 animate-spin text-muted-foreground" />}
            </div>
            <Input
              value={screenshotPath}
              onChange={e => setScreenshotPath(e.target.value)}
              placeholder="自动填入 publications/... · 或手动粘贴公开图床 URL"
              className="text-xs font-mono"
            />
            <p className="text-xs text-muted-foreground">
              支持 JPG/PNG/WebP · 限 10MB · 选图后直传 OSS · 不经过我们后端
            </p>
          </div>

          {/* Notes */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">备注（可选）</label>
            <textarea
              value={notes}
              onChange={e => setNotes(e.target.value)}
              placeholder="发布渠道备注 · 代投人 · 特殊约定等"
              rows={3}
              className="w-full px-3 py-2 rounded-md border border-border bg-background text-sm resize-y"
            />
          </div>

          {/* 提交 */}
          <div className="flex items-center gap-3 pt-2">
            <Button onClick={handleSubmit} disabled={!canSubmit}>
              {submitting ? (
                <Loader2 className="mr-2 size-4 animate-spin" />
              ) : (
                <CheckCircle2 className="mr-2 size-4" />
              )}
              提交补录
            </Button>
            {lastResult && (
              <span className="text-xs text-muted-foreground">
                最近提交：#{lastResult.publicationId}{' '}
                {lastResult.isNew ? '（新建）' : '（幂等命中）'}
                {lastResult.firstPublished ? ' · 已更新首次发布时间' : ''}
              </span>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

export default ManualPublicationForm;
