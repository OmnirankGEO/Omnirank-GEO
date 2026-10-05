"""GEO 抖音图文管线 v1 · 发布接入(复用媒介盒子短视频 lane 的图文笔记模式)

🔴🔴 **禁自建下单 / 状态回流 / 退款**(工单 §4.1)。本模块只做三件事:
    1. 选号:按抖音账号类目 + 可发图文筛媒体目录;
    2. 频控:抖音侧参数校验(单账号日发上限 / 同内容多账号必须城市变体错开);
    3. 备图:把我们 OSS 里的存档图,经**供应商代理上传**换成发布可用地址。
  真正的下单一律交给既有 `/api/meijiehezi/short-video/publish`(article_type=3),
  它自带:服务端权威算价 / 扣费 / 去重 / 六个退款出口 / 独立状态回流 job。

🔴 两条图片通道不合并(工单 §4.2):
    - **发布用图** → 供应商代理上传,产物在渠道自己的存储域,受
      `SVIDEO_ALLOWED_MEDIA_HOSTS` fail-closed 白名单约束(要加域先与发布链 AI 对齐);
    - **作品库存档** → 我们自己的 OSS(services/geo_douyin/image_pipeline.py)。
  两个用途两条路,别把 OSS 地址直接拿去下单(会被我方白名单当场拒掉)。

🔴 供应商零暴露(工单 §4.4 / SSOT §6):本模块任何**用户可见**文案都过
   `services/publish_channel_privacy.py`,对外统一叫「外部发布通道」。

🔴 退款幂等键只认 `refund_for_publish_order` + `item:<item_id>` —— 本模块不实现退款,
   仅在文档里点明,防止后来者照抄仓内另外两套错键。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from services.geo_douyin.config import (
    douyin_batch_max_accounts,
    douyin_per_account_daily_limit,
    douyin_same_content_min_city_gap,
)

logger = logging.getLogger("GEO-Douyin-Publish")

# 图文笔记模式(SSOT §7):1 视频直发 / 3 图文笔记 / 2 原创寄拍(显式拒绝)
ARTICLE_TYPE_IMAGE_NOTE = 3


def _neutral(text: str) -> str:
    """任何用户可见文案的收口(供应商零暴露硬红线)。"""
    from services.publish_channel_privacy import scrub_text
    return scrub_text(text)


# ─────────────────────────────────────────────────────────────
# 1. 选号:抖音账号 + 可发图文
# ─────────────────────────────────────────────────────────────
def escape_like(raw: str) -> str:
    """转义 LIKE 的元字符,供账号名搜索用。

    🔴 不转义的话,用户搜「100%」会变成"匹配任意",搜「a_b」会把 a1b 也搜出来 ——
       搜索结果多得离谱,用户以为是我们乱搜。
    🔴 反斜杠必须**先**转,否则后面补进去的 `\\%` 里的反斜杠会被二次转义。
    🔴 调用处必须同时写 `ESCAPE '\\'`:PG 的 LIKE **默认没有转义符**,
       只转义不声明 ESCAPE 等于没转。
    """
    return (str(raw or "").strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_"))


def list_douyin_image_accounts(*, industry: str = "", location: str = "",
                               keyword: str = "",
                               limit: int = 50, offset: int = 0) -> List[Dict]:
    """从既有短视频媒体目录里筛"抖音 + 能发图文"的账号。

    判据用真实列(已核对 mhz_short_video schema):
      platform='抖音' AND can_tuwen=1 AND is_active=TRUE
    🔴 `can_tuwen` 是"能不能发图文"的权威位 —— 不筛它会把只能发视频的号选进来,
       下单到 article_type=3 必被远端拒。
    """
    from db.connection import get_connection

    clauses = ["is_active = TRUE", "platform = '抖音'", "can_tuwen = 1",
               "COALESCE(blacklist, 0) = 0"]
    params: List = []
    if industry:
        clauses.append("industry = %s")
        params.append(industry)
    if location:
        clauses.append("location LIKE %s")
        params.append(f"%{location}%")
    if keyword:
        clauses.append("media_name LIKE %s ESCAPE '\\'")
        params.append(f"%{escape_like(keyword)}%")
    params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT id, media_name, platform, location, industry,
                       fans_num_text, price, our_price_points, avg_publish_time
                  FROM mhz_short_video
                 WHERE {' AND '.join(clauses)}
                 ORDER BY fans_num DESC NULLS LAST
                 LIMIT %s OFFSET %s""",
            tuple(params),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# 2. 抖音侧频控(工单 §4.5 · 配置化不硬编码)
# ─────────────────────────────────────────────────────────────
@dataclass
class FrequencyVerdict:
    ok: bool
    reason: str = ""
    violations: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason,
                "violations": list(self.violations)}


def load_today_counts(media_ids: Sequence[int]) -> Dict[int, int]:
    """每个账号**今天已经发了几条**(真值,来自既有发布链的订单明细)。

    🔴 这个函数是"单账号日发上限"那条频控的唯一真数据源。
       在它之前,API 从来没给 check_frequency 传过 per_account_today ——
       于是那条规则恒为"没有超限的账号",**校验形同虚设**(锁得住的规则才叫规则)。

    SQL 四维已核(生产实测):
      1. 列名  mhz_publish_order_items(media_id, status, created_at) 三列都在;
      2. 类型  created_at 是 **timestamp without time zone** → 用 CURRENT_DATE 比,
               不用 NOW()(那是 timestamptz,跨类型比较容易踩时区偏移);
      3. 归属  media_id 在 items 表(不是 orders 表);
      4. dry-run 该查询已在生产库实跑通过(不报错)。

    🔴 已取消/已撤稿/失败/被拒的**不算占用当日额度** —— 那些稿子根本没发出去,
       算进去等于用一次失败惩罚用户一整天。生产实测 status 取值共 8 种,
       这里排除的 4 种全部是"没发成"的终态。
    """
    ids = [int(m) for m in (media_ids or []) if m]
    if not ids:
        return {}
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT media_id, count(*) AS today_used
                 FROM mhz_publish_order_items
                WHERE media_id = ANY(%s::int[])
                  AND created_at >= CURRENT_DATE
                  AND status NOT IN ('cancelled', 'withdrawn', 'failed', 'rejected')
                GROUP BY media_id""",
            (ids,),
        )
        return {int(r["media_id"]): int(r["today_used"]) for r in cur.fetchall()}
    finally:
        conn.close()


def check_frequency(*, media_ids: Sequence[int],
                    per_account_today: Optional[Dict[int, int]] = None,
                    cities: Optional[Sequence[str]] = None) -> FrequencyVerdict:
    """发布前的抖音侧频控校验。

    三条(全部走配置,默认值见 services/geo_douyin/config.py):
      1. 单次选号不超过 batch_max_accounts(防一把梭);
      2. 单账号当日发布不超过 per_account_daily_limit;
      3. 同一条内容投多个账号时,城市变体数必须 >= min_city_gap
         —— 防"只换账号不换内容"被平台判重复铺量(§4.5 明确要求)。
    """
    v = FrequencyVerdict(ok=True)
    ids = [int(m) for m in (media_ids or [])]

    if not ids:
        return FrequencyVerdict(ok=False, reason="请先选择要发布的账号",
                                violations=["no_account"])

    max_accounts = douyin_batch_max_accounts()
    if len(ids) > max_accounts:
        v.violations.append("batch_too_large")
        v.reason = f"一次最多选 {max_accounts} 个账号，请分批发"

    daily = douyin_per_account_daily_limit()
    over = [mid for mid in ids if int((per_account_today or {}).get(mid, 0)) >= daily]
    if over:
        v.violations.append("account_daily_limit")
        v.reason = v.reason or f"有 {len(over)} 个账号今天已达发布上限（每号每天 {daily} 条）"

    gap = douyin_same_content_min_city_gap()
    distinct_cities = len({(c or "").strip() for c in (cities or []) if (c or "").strip()})
    if len(ids) > 1 and gap > 1 and distinct_cities < min(gap, len(ids)):
        v.violations.append("city_variant_insufficient")
        v.reason = v.reason or (
            f"同一条内容投 {len(ids)} 个账号时，至少要有 {gap} 个城市版本，"
            f"当前只有 {distinct_cities} 个（避免被平台判为重复内容）")

    v.ok = not v.violations
    return v


# ─────────────────────────────────────────────────────────────
# 3. 备图:OSS 存档图 → 供应商代理上传 → 发布可用地址
# ─────────────────────────────────────────────────────────────
@dataclass
class PreparedMedia:
    ok: bool
    image_urls: List[str] = field(default_factory=list)
    cover_image: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "image_urls": list(self.image_urls),
                "cover_image": self.cover_image, "error": self.error}


async def _read_oss_bytes(oss_key: str) -> bytes:
    """从我们自己的 OSS 读回存档图。oss2 同步 → to_thread(不占事件循环)。"""
    from services import oss_service

    def _get() -> bytes:
        bucket, _ = oss_service._get_bucket()  # noqa: SLF001 - 复用既有单例句柄
        return bucket.get_object(oss_key).read()

    return await asyncio.to_thread(_get)


async def prepare_publish_images(oss_keys: Sequence[str]) -> PreparedMedia:
    """把作品库存档图转成"发布可用"的地址。

    🔴 必须经供应商代理上传:我们 OSS 的地址不在 `SVIDEO_ALLOWED_MEDIA_HOSTS` 里,
       直接拿去下单会被我方 fail-closed 白名单当场拒(这是设计,不是 bug)。
    fail-closed:任何一张失败就整体失败 —— 少一张图的图文帖不该发出去。
    """
    keys = [k for k in (oss_keys or []) if k]
    if not keys:
        return PreparedMedia(ok=False, error="没有可发布的图片")

    from api.meijiehezi_api import _get_client, _is_allowed_svideo_media_url

    client = _get_client()
    urls: List[str] = []
    for i, key in enumerate(keys):
        try:
            data = await _read_oss_bytes(key)
            if not data:
                return PreparedMedia(ok=False, error=f"第 {i+1} 张图读取为空")
            url = await client.upload_short_video_image_via_proxy(
                filename=f"card_{i+1}.png", content=data, content_type="image/png",
            )
        except Exception as e:  # noqa: BLE001
            # 🔴 只用 _neutral 一道收口:`safe_error_detail` 内部也调 scrub_text,
            #    两道叠着等于双重脱敏 —— 那样任意一道失效都测不出来(变异会存活)。
            #    单一收口才能让"去掉脱敏"这件事在测试里立刻现形。
            return PreparedMedia(
                ok=False,
                error=_neutral(f"第 {i+1} 张图上传失败：{e}"))

        # 产物域也必须过同一道白名单:远端换域时就地暴露,不让一个下单会被拒的地址流下去
        if not _is_allowed_svideo_media_url(url):
            logger.error("[douyin-publish] 代理上传产物域不在白名单: %s", str(url)[:120])
            return PreparedMedia(ok=False, error="图片没传上去，请稍后再试")
        urls.append(url)

    return PreparedMedia(ok=True, image_urls=urls, cover_image=urls[0])


# ─────────────────────────────────────────────────────────────
# 4. 组装下单入参(交给既有 /short-video/publish,不自建下单)
# ─────────────────────────────────────────────────────────────
def build_publish_payload(*, title: str, body_text: str, hashtags: Sequence[str],
                          media_ids: Sequence[int], image_urls: Sequence[str],
                          cover_image: str = "", brand_id: Optional[int] = None,
                          keyword: str = "", customer_name: str = "",
                          request_id: str = "") -> dict:
    """按既有 ShortVideoPublishRequest 的形状组装入参。

    article_type 固定 3(图文笔记)。video_url 必须为空 —— 图文模式传视频会被 400。
    hashtag 拼进正文尾部(抖音的话题就是正文里的 #xxx)。
    """
    tags = " ".join(f"#{t.lstrip('#')}" for t in (hashtags or []) if t)
    content = f"{body_text}\n{tags}".strip() if tags else (body_text or "")
    return {
        "title": (title or "")[:45],          # 45 = 发布平台硬上限(SSOT §7)
        "content": content,
        "keyword": keyword or "",
        "media_ids": [int(m) for m in (media_ids or [])],
        "media_names": [],                    # 服务端权威取名,不由本模块编造
        "video_url": "",                      # 图文模式必须为空
        "image_urls": [str(u) for u in (image_urls or [])],
        "article_type": ARTICLE_TYPE_IMAGE_NOTE,
        "cover_image": cover_image or "",
        "customer_name": customer_name or "",
        "brand_id": brand_id,
        "request_id": request_id or "",
    }
