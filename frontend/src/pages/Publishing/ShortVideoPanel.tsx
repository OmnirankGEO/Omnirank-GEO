/**
 * ShortVideoPanel — 发布中心「短视频价格」子 tab 的自包含面板
 *
 * 设计目标：40 岁做销售的大姐拿着就会用。全程人话，不露供应商/技术名词。
 * 交付逻辑与软文/自媒体不同：短视频是「先有一个视频文件 → 传上去 → 选账号 → 发」。
 * 所以左边是「上传视频 + 填信息」的向导，右边是「短视频账号市场」。
 *
 * [2026-09-20] 目录、筛选、分页由 ShortVideoAccountMarket 与图文发布共用。
 *   本组件只保留视频表单、受控多选与原发布实现；图文不进入这里的旧发布接口。
 *
 * 独立于软文/自媒体：不进共享购物车、不碰共享快照，走独立后端入口
 *   /api/meijiehezi/short-video/*，最大化复用同时对 mhz/wemedia 零影响。
 *
 * 后端总闸（SVIDEO_PUBLISH_ENABLED / SVIDEO_UPLOAD_ENABLED）未开时：浏览资源、看价格、
 *   选账号、算算力都正常，只有「上传视频」和「发布」会友好提示"即将开放"（不扣费、不下单）。
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { toast } from 'sonner';
import { ShortVideoAccountMarket } from './ShortVideoAccountMarket';
import type { MarketAccount as SvAccount } from './shortVideoMarket';
import { authFetch } from '@/lib/api';
import { awaitConfirmedSessionToken } from '@/lib/authoritativeSession';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import {
  Loader2, UploadCloud, Video, Image as ImageIcon,
  CheckCircle2, RefreshCw, X,
} from 'lucide-react';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

const MAX_VIDEO_BYTES = 1024 * 1024 * 1024; // 1GB（与发布平台上传脚本一致）
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;   // 10MB（封面/图文单张）
const TITLE_SOFT_LIMIT = 30; // 发布通道提示：标题 30 字内成功率更高
// 45 字是发布通道的**硬上限**（超了远端直接报错）；30 字只是成功率建议。两者别混。
const TITLE_HARD_LIMIT = 45;
const MAX_NOTE_IMAGES = 9;   // 图文笔记单条图片张数上限（前端体验约束）

// 发布方式：视频直发 / 图文笔记。落到后端是 article_type 1 / 3（唯一的模式开关）。
type PublishMethod = 'video' | 'article';
const ARTICLE_TYPE_BY_METHOD: Record<PublishMethod, number> = { video: 1, article: 3 };

/** 已做好的 GEO 图文(左栏「选择图文」用)。
 *  与软文 tab 的文章列表对称 —— 那边选文章,这边选图文。 */
interface GeoPostRef {
  id: number;
  title: string | null;
  city: string | null;
  keyword: string | null;
}

interface Props {
  brandId?: number | null;
  brandName?: string;
  industry?: string;
  /** 从 AI 创作中心「制作 GEO 图文」跳过来时带的作品 id。
   *
   * 🔴 [共享主人翁制 · GEO 图文 2026-08-03] 本 prop 是**可选**的,不传时本面板
   *    行为与改造前逐字相同 —— 发布链的既有用法一处都不受影响。
   *    存在的理由:创作中心此前自带一个 50 条无分页的窄账号选择器
   *    (合格账号其实有 2,561 个),Owner 裁定发布必须用发布中心的完整池,
   *    所以那边改成跳转过来,图组/文案由本面板预填,媒体选择/筛选/价格/下单
   *    全部走本面板既有链 —— 一行不重造。
   */
  geoPostId?: number | null;
  /**
   * 🔴 [2026-08-10 P1] 预填拿到**服务端权威归属**时回报给父组件。
   *    `brandId` 是父组件的 prop,本面板改不了它;不回报的话,面板
   *    展示的是内容真正的客户、提交的却是父组件兜底选中的另一个品牌 ——
   *    「显示对、记错人」正是这个 P1 的表现。
   */
  onGeoBrandResolved?: (brandId: number) => void;
}

export function ShortVideoPanel({ brandId, brandName, industry, geoPostId,
                                 onGeoBrandResolved }: Props) {
  // ── 已选账号：存整个账号对象(不是只存 id)——翻页后仍能拿到名字/价格，算力小计与提交名单不丢 ──
  const [selected, setSelected] = useState<Map<number, SvAccount>>(new Map());

  // ── 上传 + 表单 ──
  const [publishMethod, setPublishMethod] = useState<PublishMethod>('video');
  const [videoUrl, setVideoUrl] = useState('');
  const [videoName, setVideoName] = useState('');
  const [uploading, setUploading] = useState(false);
  const [uploadPct, setUploadPct] = useState(0);
  const [coverImage, setCoverImage] = useState('');
  const [coverUploading, setCoverUploading] = useState(false);
  const [noteImages, setNoteImages] = useState<string[]>([]);
  const [noteUploading, setNoteUploading] = useState(false);
  const coverInputRef = useRef<HTMLInputElement | null>(null);
  const noteInputRef = useRef<HTMLInputElement | null>(null);
  const [title, setTitle] = useState('');
  const [keyword, setKeyword] = useState('');
  const [content, setContent] = useState('');
  const [customerName, setCustomerName] = useState('');
  const [remark, setRemark] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // ── GEO 图文预填(只在带 geoPostId 时发生)──
  // 🔴 走的是创作中心既有的 prepare-publish-media —— 那个接口**不下单、不扣费**,
  //    只把私有桶里的存档图转成发布可用地址。下单/扣费仍然是本面板的既有链。
  // 🔴 预填**只在首次**生效(prefilled ref 守卫):用户跳过来之后自己改了标题
  //    再切个筛选,不该把他改的东西again覆盖回去。
  const [geoPrefillError, setGeoPrefillError] = useState('');
  const [geoPosts, setGeoPosts] = useState<GeoPostRef[]>([]);
  const [geoPickedId, setGeoPickedId] = useState<number | null>(null);
  const [geoLoading, setGeoLoading] = useState(false);

  /** 把一条 GEO 图文预填进本面板的表单。
   *
   * 🔴 **只有这一份实现**。两条入口都走它:
   *     ① 从创作中心详情页跳过来(URL 带 geo_post_id)
   *     ② 在本面板左栏「选择图文」里选一条
   *    写两份的话,改一处另一处就不一样了(同一事实写两处)。
   */
  const applyGeoPost = useCallback(async (postId: number) => {
    setGeoPrefillError('');
    setGeoLoading(true);
    try {
      const r = await authFetch('/api/geo-douyin/prepare-publish-media', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ post_id: postId }),
      });
      const d = await r.json();
      if (!r.ok || d?.status === 'coming_soon') {
        setGeoPrefillError(
          typeof d?.detail === 'string' ? d.detail
            : d?.detail?.message || d?.message || '这条内容的图没取到，回创作中心看看');
        return;
      }
      const imgs: string[] = Array.isArray(d.image_urls) ? d.image_urls : [];
      if (!imgs.length) {
        setGeoPrefillError('这条内容还没有可发布的图');
        return;
      }
      setPublishMethod('article');          // 图文笔记 = article_type 3
      setNoteImages(imgs.slice(0, MAX_NOTE_IMAGES));
      if (d.cover_image) setCoverImage(String(d.cover_image));
      if (d.title) setTitle(String(d.title));
      if (d.content) setContent(String(d.content));
      const tags: string[] = Array.isArray(d.hashtags) ? d.hashtags : [];
      if (tags.length) setKeyword(tags.map(t => String(t).replace(/^#/, '')).join(' '));
      if (d.customer_name) setCustomerName(String(d.customer_name));
      // 🔴 [2026-08-10 P1] `d.brand_id` 是**服务端给的权威归属**,此前被原样丢掉 ——
      //    这个面板拿到了 customer_name 就只更新展示,归属字段(brandId)是父组件 prop,
      //    从不回写 → 提交时送出去的还是父组件兜底选中的那个**错**品牌。
      //    不是"前端不知道",是知道了扔掉。这里回报给父组件,让选中项目跟着内容走。
      if (d.brand_id) onGeoBrandResolved?.(Number(d.brand_id));
      setGeoPickedId(postId);
    } catch {
      setGeoPrefillError('这条内容的图没取到，稍后再试');
    } finally {
      setGeoLoading(false);
    }
  }, [onGeoBrandResolved]);

  // 入口①:从详情页跳过来。**只在首次**生效 —— 用户跳过来之后自己改了标题
  // 再切个筛选,不该把他改的东西覆盖回去。
  const geoPrefilled = useRef(false);
  useEffect(() => {
    if (!geoPostId || geoPrefilled.current) return;
    geoPrefilled.current = true;
    /**
     * 🔴 [#179 第 14 条 · #184 X3 止血 2026-09-12] **这条深链不再取图文素材**。
     *
     *    图文笔记的站内发布今天是断的:提交口 `article_type==3` 自 08-19 起在
     *    `api/meijiehezi_api.py:2430` **无条件 400**。原来这里会调
     *    `prepare-publish-media`(要等 ~10 秒),用户填完表单再撞 400 ——
     *    取一次必然白费的素材,比直接说"这条路暂时不通"更糟。
     *    所以带 `geo_post_id` 进来时只显示说明 + 指向导出成品包,不发那个请求。
     *    Owner 若拍板恢复图文线(#184 X2),把 `void applyGeoPost(geoPostId)` 加回来即可
     *    —— `applyGeoPost` 本体刻意保留,没有删。
     */
  }, [geoPostId]);

  // ── 左栏「选择图文」列表:与软文 tab 选文章**结构对称** ──
  // 🔴 只做跳转预填等于只打通了一半:用户直接点短视频 tab 仍然只能手动传图。
  // 🔴 按当前项目的客户过滤 —— 客户是唯一作用域(SSOT 铁律 1)。
  //
  // 🔴 [#179 第 14 条 · #184 X3 止血] 这个列表**不再取**。
  //    选了也发不出去(`article_type==3` 无条件 400),列出来就是把人往断路上引。
  //    保留 `geoPosts` 这个 state 与 `applyGeoPost` 本体,是为了 Owner 拍板恢复
  //    图文线(#184 X2)时能一行改回来;现在它恒为空数组 ⇒ 选择栏不渲染。
  useEffect(() => {
    setGeoPosts([]);
  }, [brandId]);

  useEffect(() => {
    if (brandName && !customerName) setCustomerName(brandName);
  }, [brandName]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectedAccounts = Array.from(selected.values());
  // 小计跨页保留(存的是整个账号对象)；下单时后端仍按权威价重算，此处仅供用户预估
  const estPoints = selectedAccounts.reduce((s, a) => s + (a.price_points || 0), 0);

  // ── 上传 ──
  // [2026-08-01] 此处原有「取签名 → 浏览器直传对方存储」的一套（fetchUploadPolicy + postToStorage），
  //   已删除：渠道存储桶 的 CORS 白名单没有 omnirank.top，bucket 又不是我们的（改不了）
  //   → 直传对我方用户 **100% 失败**，留着只会浪费一次往返并误导后人。
  //   实测依据：docs/AI-CONTEXT/SSOT_MHZ_SHORT_VIDEO_UPLOAD_PUBLISH_2026-08-01.md
  //   后端 /short-video/upload-policy 端点**仍保留**（对方哪天加了白名单可零改动切回），
  //   前端要切回去的话从 git 历史取这两个函数即可。

  // [2026-08-01 方案B] 视频走**我方服务端中转**：浏览器只跟自己的域说话 → 根本不存在 CORS 问题。
  // 请求体是裸文件字节（不是 FormData）：后端可以把请求流直接接到 OSS，1GB 不落盘不进内存。
  // 用 XHR 而不是 fetch 是为了 upload.onprogress —— 同源请求，进度条照常可用。
  const postVideoViaServer = async (file: File, onPct?: (p: number) => void) => {
    // 🔴 token 必须走 awaitConfirmedSessionToken()（与 authFetch 同一条路）。
    //   直接读 localStorage 会绕过会话确认，拿到未确认/过期 token → 401 且难查。
    const token = await awaitConfirmedSessionToken();
    return new Promise<string>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `/api/meijiehezi/short-video/upload-video?filename=${encodeURIComponent(file.name)}`, true);
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.onprogress = e => {
        if (e.lengthComputable && onPct) onPct(Math.round((e.loaded / e.total) * 100));
      };
      xhr.onload = () => {
        let url = '';
        try { url = (JSON.parse(xhr.responseText) || {}).url || ''; } catch { /* 非 JSON 当失败处理 */ }
        if (xhr.status >= 200 && xhr.status < 300 && url) resolve(url);
        else reject(new Error(`relay_${xhr.status}`));
      };
      xhr.onerror = () => reject(new Error('relay_network'));
      xhr.ontimeout = () => reject(new Error('relay_timeout'));
      xhr.send(file);
    });
  };

  const onPickFile = () => fileInputRef.current?.click();

  const handleFile = async (file: File) => {
    const nameLower = file.name.toLowerCase();
    if (!nameLower.endsWith('.mp4') && !nameLower.endsWith('.flv')) {
      toast.error('这个视频发不了，换个 MP4 或 FLV 格式的');
      return;
    }
    if (file.size > MAX_VIDEO_BYTES) {
      toast.error('视频有点大，单个别超过 1GB');
      return;
    }
    setUploading(true);
    setUploadPct(0);
    try {
      // [2026-08-01 方案B] 改走服务端中转。原先是「取签名 → 浏览器直传 OSS」，但 渠道存储桶的
      // CORS 白名单里没有我们的域（bucket 是媒介盒子的，改不了）→ 直传对我方用户**永远失败**。
      // 现在浏览器只把文件交给我们自己的后端，由后端转投 OSS（服务器间无 CORS）。
      // 传不上去仍然如实告知，绝不让用户以为传上去了然后在发布环节才炸。
      const url = await postVideoViaServer(file, setUploadPct);
      setVideoUrl(url);
      setVideoName(file.name);
      toast.success('视频已就绪');
    } catch {
      toast.error('视频没传上去，点这儿重试（填的东西不会丢）');
    } finally {
      setUploading(false);
      setUploadPct(0);
    }
  };

  // 图片（封面 / 图文笔记配图）：先直传，直传不成再退回后端代理（视频没有这条退路）
  const uploadImage = async (file: File): Promise<string | null> => {
    const nameLower = file.name.toLowerCase();
    if (!/\.(jpg|jpeg|png|webp)$/.test(nameLower)) {
      toast.error('图片换个 JPG 或 PNG 的试试');
      return null;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      toast.error('图片太大了，单张别超过 10MB');
      return null;
    }
    // [2026-08-01] 原先先试「直传 OSS」再降级到后端代理。但实测直传对我方域**必然失败**
    // （渠道存储桶 CORS 白名单没有 omnirank.top）→ 那一次尝试是 100% 浪费，还让用户多等一个往返。
    // 现在直接走后端代理（媒介盒子自己的图片上传接口也是服务端代理，这条路实测稳定）。
    try {
      const fd = new FormData();
      fd.append('file', file);
      const r = await authFetch('/api/meijiehezi/short-video/upload-cover', { method: 'POST', body: fd });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.url) return d.url as string;
      toast.error(d.detail || '图片没传上去，稍后再试');
      return null;
    } catch {
      toast.error('图片没传上去，稍后再试');
      return null;
    }
  };

  const onCoverSelected = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (e.target) e.target.value = '';
    if (!file) return;
    setCoverUploading(true);
    try {
      const url = await uploadImage(file);
      if (url) setCoverImage(url);
    } finally { setCoverUploading(false); }
  };

  const onNoteImagesSelected = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (e.target) e.target.value = '';
    if (!files.length) return;
    const room = MAX_NOTE_IMAGES - noteImages.length;
    if (room <= 0) { toast.error(`最多 ${MAX_NOTE_IMAGES} 张图`); return; }
    setNoteUploading(true);
    try {
      const picked: string[] = [];
      for (const f of files.slice(0, room)) {
        const url = await uploadImage(f);
        if (url) picked.push(url);
      }
      if (picked.length) setNoteImages(prev => [...prev, ...picked]);
    } finally { setNoteUploading(false); }
  };

  const onFileSelected = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (e.target) e.target.value = ''; // 允许重选同名文件
    if (file) void handleFile(file);
  };

  // 拖拽上传：必须 preventDefault，否则浏览器会直接打开视频文件、整页跳走(填的东西全丢)
  const onDragOver = (e: React.DragEvent) => { e.preventDefault(); };
  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const file = e.dataTransfer.files?.[0];
    if (file && !uploading) void handleFile(file);
  };

  const clearVideo = () => { setVideoUrl(''); setVideoName(''); };

  // ── 发布 ──
  const isNote = publishMethod === 'article';
  const materialReady = isNote ? noteImages.length > 0 : !!videoUrl;
  const titleOverHard = title.length > TITLE_HARD_LIMIT;
  const canSubmit = materialReady && !!title.trim() && !titleOverHard && selected.size > 0 && !submitting;
  // 按钮灰着时告诉用户还差哪一步，不让人对着点不动的按钮发懵
  const missingHint = !materialReady ? (isNote ? '先传至少 1 张图' : '先把视频传上去')
    : !title.trim() ? '起个标题'
    : titleOverHard ? `标题最多 ${TITLE_HARD_LIMIT} 个字`
    : selected.size === 0 ? '在右边选至少 1 个账号' : '';

  const submit = async () => {
    if (!title.trim()) { toast.error('起个标题吧'); return; }
    if (titleOverHard) { toast.error(`标题最多 ${TITLE_HARD_LIMIT} 个字，先删短一点`); return; }
    if (isNote) {
      if (!noteImages.length) { toast.error('先传至少 1 张图'); return; }
    } else if (!videoUrl) { toast.error('先把视频传上去'); return; }
    if (selected.size === 0) { toast.error('至少选一个账号'); return; }
    setSubmitting(true);
    try {
      // 全部从 selected Map 取(跨页选择也齐)，media_ids/media_names/cost_* 按同一列表对齐
      const ids = selectedAccounts.map(a => a.id);
      const body = {
        title: title.trim(),
        content,
        keyword,
        media_ids: ids,
        media_names: selectedAccounts.map(a => a.media_name),
        // 图文笔记：article_type=3 + 只传 image_urls，video_url 必须留空（后端会拒"两样都带"）
        article_type: ARTICLE_TYPE_BY_METHOD[publishMethod],
        video_url: isNote ? '' : videoUrl,
        image_urls: isNote ? noteImages : [],
        cover_image: coverImage,
        customer_name: customerName,
        remark,
        brand_id: brandId ?? null,
        // 🔴 [2026-08-10 P1] 带上**内容来源**。服务端据此反查权威归属并以自己为准 ——
        //    这是治本的那一层:前端下次再取错品牌,服务端也能纠正。
        //    `geoPickedId` 是本面板已有的 state(预填时记下的来源作品 id),不新造。
        geo_post_id: geoPickedId ?? null,
        cost_points: selectedAccounts.map(a => a.price_points || 0),
        // [D0-b] 不再上送进货价(服务端本就不信客户端价)
        cost_yuan: [],
        request_id: safeRandomUUID(),
      };
      const r = await authFetch('/api/meijiehezi/short-video/publish', {
        method: 'POST', body: JSON.stringify(body),
      });
      const d = await r.json();
      if (d.status === 'coming_soon') { toast.info(d.message || '短视频发布即将开放，敬请期待'); return; }
      if (!r.ok) { toast.error((d.detail && (d.detail.message || d.detail)) || '发布失败，请稍后重试'); return; }
      if (d.status === 'failed') {
        // 全部没发出去(费用已由后端原路退回)：如实相告，保留已填内容让用户改完再发，绝不谎报"已提交"
        toast.error(d.message || '这次没发出去，费用已原路退回，请稍后再试');
        return;
      }
      toast.success(d.message || '已提交');
      // 成功后清空本次视频与选择（保留客户名/账号筛选，方便连着发下一条）
      clearVideo(); setCoverImage(''); setNoteImages([]);
      setTitle(''); setKeyword(''); setContent(''); setRemark('');
      setSelected(new Map());
    } catch {
      toast.error('发布失败，请稍后重试');
    } finally {
      setSubmitting(false);
    }
  };

  const titleOverLimit = title.length > TITLE_SOFT_LIMIT;


  return (
    // 根容器兜底拦截拖拽：用户没拖准上传框时，防止浏览器直接打开视频文件整页跳走
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden lg:flex-row" onDragOver={onDragOver} onDrop={onDrop}>
      {/* ===== 左：上传视频 + 填信息（向导） ===== */}
      <div className="w-full shrink-0 overflow-y-auto border-b border-border p-3 sm:p-4 lg:w-[380px] lg:border-b-0 lg:border-r">
        {/* [共享主人翁制 · GEO 图文 2026-08-03] 选已做好的图文。
            与软文/自媒体 tab 的「选择文章」结构对称 —— 那边选文章,这边选图文。
            🔴 只做"从详情页跳过来预填"等于只打通一半:用户直接点短视频 tab
               仍然只能手动传图。这一栏补的就是那另一半。
            🔴 没有图文时整栏不渲染 —— 老用法(手动传视频)一个像素都不变。 */}
        {/* 🔴 [#179 第 14 条 · #184 X3 止血 2026-09-12] 原来这里是「选择图文」列
            (:584-594)。已撤 —— 图文笔记的提交口 `article_type==3` 自 08-19 起在
            `api/meijiehezi_api.py:2430` **无条件 400**,而新入口前端 09-08 已撤,
            于是用户今天的完整体验是:等 10 秒取图 → 填完表单 → 撞 400。
            列一个选不出结果的列表,比不列更糟。原位只留这一句说明。
            🔴 **视频直发一个像素不变** —— 下面的上传/填表/提交全没动。
            Owner 若拍板恢复图文线(#184 X2),把 geoPosts 的取数与这一栏一起恢复。 */}
        {/*
          * 🔴 [#204 a2] 这句话原文是:
          *    「图文笔记的站内发布暂未开放；在「制作 GEO 图文」里导出成品包后可手动发布。」
          *    到 2026-09-15 它**两句都不成立**了:
          *      ① 图文站内发布 #203 已收口到**发布中心**(`ImageNoteList` 挂发布面板);
          *      ② 它指路去的「制作 GEO 图文」这块屏本单就要删掉 —— 留着等于把人
          *         送去一个不存在的地方。
          *
          * 🔴 它为什么一直没被判据抓住:E1c / G11e 钉的是「**制作台里**没有这句话」——
          *    那是一个**位置**。命题是「**全仓**不许再有这句话」。
          *    制作台里确实早就删干净了,所以两格一直是绿的,而这一处安然活着。
          *    ⇒ 改钉命题之后,这一处才会被守住(配注入正对照)。
          */}
        <p className="mb-4 rounded-md border border-border bg-muted/40 px-2.5 py-2 text-xs text-muted-foreground"
           data-testid="sv-image-note-closed-note">
            这里上传并发布视频文件。已做好的 GEO 图文在上方「已制作图文」里。
        </p>

        {/* 发布方式：发视频 / 发图文笔记（落到后端是 article_type 1/3） */}
        <div className="mb-4">
          <div className="mb-1.5 text-sm font-medium">怎么发</div>
          <div className="flex gap-2">
            {/* 🔴 [#179 第 14 条] 「发图文笔记」这一档已撤(:630-634 原有两档)——
                选它必然 400。数组里只留视频一档;`PublishMethod`/`ARTICLE_TYPE_BY_METHOD`
                与下面 isNote 的分支**都保留**,Owner 恢复图文线时把 'article' 加回数组即可。 */}
            {([['video', '发视频']] as [PublishMethod, string][]).map(([m, label]) => (
              <button
                key={m}
                type="button"
                role="radio"
                aria-checked={publishMethod === m}
                data-testid={`sv-publish-method-${m}`}
                onClick={() => setPublishMethod(m)}
                className={cn(
                  'flex-1 rounded-lg border px-3 py-2 text-sm transition-colors',
                  publishMethod === m
                    ? 'border-primary bg-primary/10 font-medium text-primary'
                    : 'border-border text-muted-foreground hover:border-primary/40 hover:text-foreground'
                )}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {/* 第 1 步：传素材（视频 or 图片，按上面选的方式切） */}
        {isNote ? (
          <div className="mb-4">
            <div className="mb-1.5 flex items-center gap-2 text-sm font-medium">
              <span className="flex size-5 items-center justify-center rounded-full bg-primary/15 text-[11px] text-primary">1</span>
              上传图片 <span className="text-xs font-normal text-muted-foreground">（最多 {MAX_NOTE_IMAGES} 张）</span>
            </div>
            <input
              ref={noteInputRef} type="file" multiple accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp"
              className="hidden" onChange={onNoteImagesSelected}
            />
            {noteImages.length > 0 && (
              <div className="mb-2 flex flex-wrap gap-2">
                {noteImages.map((u, i) => (
                  <div key={`${u}-${i}`} className="relative">
                    <img src={u} alt={`图 ${i + 1}`} className="size-14 rounded border border-border object-cover" />
                    <button
                      type="button"
                      onClick={() => setNoteImages(prev => prev.filter((_, j) => j !== i))}
                      className="absolute -right-1.5 -top-1.5 flex size-5 items-center justify-center rounded-full bg-background shadow ring-1 ring-border"
                      aria-label={`移除第 ${i + 1} 张图`}
                    >
                      <X className="size-3" />
                    </button>
                  </div>
                ))}
              </div>
            )}
            {noteImages.length < MAX_NOTE_IMAGES && (
              <button
                type="button"
                onClick={() => noteInputRef.current?.click()}
                disabled={noteUploading}
                className="flex w-full flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed border-border py-6 text-center transition-colors hover:border-primary/50 hover:bg-secondary/40 disabled:opacity-60"
              >
                {noteUploading ? <Loader2 className="size-7 animate-spin text-primary" /> : <ImageIcon className="size-7 text-muted-foreground" />}
                <div className="text-sm font-medium">{noteUploading ? '上传中，请勿关闭页面…' : '点这里选图片'}</div>
                <div className="text-xs text-muted-foreground">支持 JPG / PNG · 单张不超过 10MB</div>
              </button>
            )}
          </div>
        ) : (
        <div className="mb-4">
          <div className="mb-1.5 flex items-center gap-2 text-sm font-medium">
            <span className="flex size-5 items-center justify-center rounded-full bg-primary/15 text-[11px] text-primary">1</span>
            上传视频
          </div>
          <input ref={fileInputRef} type="file" accept="video/mp4,video/x-flv,.mp4,.flv" className="hidden" onChange={onFileSelected} />
          {videoUrl ? (
            <div className="flex items-center gap-3 rounded-lg border border-primary/40 bg-primary/5 p-3">
              <Video className="size-8 shrink-0 text-primary" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1 text-sm font-medium text-primary"><CheckCircle2 className="size-4" /> 视频已就绪</div>
                <div className="truncate text-xs text-muted-foreground" title={videoName}>{videoName}</div>
              </div>
              <Button variant="ghost" size="sm" onClick={onPickFile} className="shrink-0 text-xs">换一个</Button>
              <Button variant="ghost" size="icon" onClick={clearVideo} className="size-7 shrink-0"><X className="size-4" /></Button>
            </div>
          ) : (
            <button
              type="button"
              onClick={onPickFile}
              onDragOver={onDragOver}
              onDrop={onDrop}
              disabled={uploading}
              className="flex w-full flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed border-border py-8 text-center transition-colors hover:border-primary/50 hover:bg-secondary/40 disabled:opacity-60"
            >
              {uploading ? <Loader2 className="size-8 animate-spin text-primary" /> : <UploadCloud className="size-8 text-muted-foreground" />}
              <div className="text-sm font-medium">
                {uploading ? `上传中 ${uploadPct}%，请勿关闭页面…` : '点这里选视频，或把视频拖进来'}
              </div>
              <div className="text-xs text-muted-foreground">支持 MP4 / FLV · 单个不超过 1GB · 建议 5 分钟以内</div>
            </button>
          )}
        </div>
        )}

        {/* 第 2 步：封面（可选） */}
        <div className="mb-4">
          <div className="mb-1.5 flex items-center gap-2 text-sm font-medium">
            <span className="flex size-5 items-center justify-center rounded-full bg-secondary text-[11px] text-muted-foreground">2</span>
            封面 <span className="text-xs font-normal text-muted-foreground">
              {isNote ? '（可选，不传用第一张图）' : '（可选，不传自动用视频首帧）'}
            </span>
          </div>
          <input
            ref={coverInputRef} type="file" accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp"
            className="hidden" onChange={onCoverSelected}
          />
          {coverImage ? (
            <div className="flex items-center gap-3 rounded-lg border border-border p-2">
              <img src={coverImage} alt="封面" className="size-12 rounded object-cover" />
              <span className="flex-1 text-xs text-muted-foreground">封面已选</span>
              <Button variant="ghost" size="sm" onClick={() => setCoverImage('')} className="text-xs">移除</Button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => coverInputRef.current?.click()}
              disabled={coverUploading}
              className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-border py-3 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground disabled:opacity-60"
            >
              {coverUploading
                ? <><Loader2 className="size-4 animate-spin" /> 上传中…</>
                : <><ImageIcon className="size-4" /> 上传封面</>}
            </button>
          )}
        </div>

        {/* 第 3 步：填信息 */}
        <div className="space-y-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <span className="flex size-5 items-center justify-center rounded-full bg-secondary text-[11px] text-muted-foreground">3</span>
            填信息
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">{isNote ? '笔记标题' : '视频标题'}</label>
            <Input value={title} onChange={e => setTitle(e.target.value)}
                   placeholder={isNote ? '一句话说清这条笔记讲什么' : '一句话说清这条视频讲什么'} />
            {/* 30 字 = 成功率建议（橙字提示）；45 字 = 发布通道硬上限（红字 + 按钮直接拦住） */}
            <div className={cn('mt-0.5 text-right text-[11px]',
              titleOverHard ? 'text-destructive' : titleOverLimit ? 'text-orange-500' : 'text-muted-foreground')}>
              {title.length} 字
              {titleOverHard ? ` · 最多 ${TITLE_HARD_LIMIT} 个字，超了发不出去`
                : titleOverLimit ? ' · 30 字内成功率更高' : ''}
            </div>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">话题关键词</label>
            {/* 发布通道用井号分隔话题，不是逗号 —— 提示语必须写对，否则用户填的逗号会被当成一整个话题 */}
            <Input value={keyword} onChange={e => setKeyword(e.target.value)} placeholder="用井号隔开，例如：#装修避坑 #亲子教育（可留空）" />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">内容描述</label>
            <textarea
              value={content} onChange={e => setContent(e.target.value)}
              placeholder={isNote ? '简单描述一下笔记内容（不要留电话、微信、QQ）' : '简单描述一下视频内容（不要留电话、微信、QQ）'}
              className="min-h-[64px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">所属客户</label>
            <Input value={customerName} onChange={e => setCustomerName(e.target.value)} placeholder="这条视频是给哪个客户发的" />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">备注（可选）</label>
            <Input value={remark} onChange={e => setRemark(e.target.value)} placeholder="给自己或平台留句备注" />
          </div>
        </div>
      </div>

      {/* ===== 右：短视频账号市场（筛选/搜索/表格/分页/结算，与软文/自媒体同一套设计语言） ===== */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <div className="min-w-0 overflow-y-auto p-3 sm:p-4">
          <ShortVideoAccountMarket selected={selectedAccounts} multiple locked={submitting}
            onChange={accounts => setSelected(new Map(accounts.map(a => [a.id, a])))} />
        </div>

        {/* 底部结算（样式对齐代发底部栏） */}
        <div className="sticky bottom-0 z-20 flex shrink-0 items-center justify-between gap-3 border-t border-border bg-card/95 px-3 py-3 backdrop-blur sm:px-4">
          <div className="min-w-0 text-sm">
            已选 <span className="font-medium">{selected.size}</span> 个账号
            {selected.size > 0 && <span className="ml-2 text-muted-foreground">预计 <span className="font-medium text-primary">{estPoints.toLocaleString()}</span> 算力</span>}
            {!canSubmit && !submitting && missingHint && (
              <span className="ml-2 text-xs text-muted-foreground">· {missingHint}</span>
            )}
          </div>
          <Button onClick={submit} disabled={!canSubmit} className="min-w-[120px] shrink-0">
            {submitting ? <Loader2 className="mr-1 size-4 animate-spin" /> : null}
            发布短视频
          </Button>
        </div>
      </div>
    </div>
  );
}

export default ShortVideoPanel;
