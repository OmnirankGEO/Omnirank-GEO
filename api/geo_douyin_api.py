"""GEO 抖音图文管线 v1 · API

产品位置:写作中心「制作 GEO 视频图文」分区(工单 §1)。
流程:选品牌/关键词 → 生成文案+卡片要点 → 生成卡片图 → 预览(逐张可重生成)
      → 发布(复用短视频图文 lane)/ 导出后备包

🔴 发布**不在本路由实现**:下单一律走既有 `/api/meijiehezi/short-video/publish`
   (article_type=3)。本路由只提供"选号 / 频控预检 / 备图",把发布可用地址交给前端
   再调那个既有接口 —— 禁自建第二套下单/状态/退款(工单 §4.1)。

🔴 工程术语全站翻译人话(工单 §1):对外不出现 OSS / 分镜 JSON / 供应商名。
"""
from __future__ import annotations

import io
import logging
import zipfile
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from services.geo_douyin.pipeline_gate import pipeline_gate
from pydantic import BaseModel, Field

from auth.brand_access import require_brand_access
from services.geo_douyin.config import (
    ASPECT_RATIO_DEFAULT,
    ASPECT_RATIOS,
    CARD_COUNT_DEFAULT,
    CARD_COUNT_INCLUDED,
    FEATURE_CODE_IMAGE_POST,
    FEATURE_CODE_IMAGE_POST_EXTRA_CARD,
    FEATURE_CODE_IMAGE_POST_REDRAW,
    FEATURE_CODE_IMAGE_POST_REGEN,
    FEATURE_CODE_TOPIC_DISTILL,
    CARD_COUNT_MAX,
    CARD_COUNT_MIN,
    douyin_per_account_daily_limit,
    is_pipeline_enabled,
    normalize_aspect_ratio,
)
from services.geo_douyin.ranking_payload import (
    ENTITY_COUNT_DEFAULT,
    ENTITY_COUNT_MAX,
    ENTITY_COUNT_MIN,
)
from services.geo_douyin.ranking_router import ranking_template_options
from services.geo_douyin.redraw import REDRAW_LIMIT_PER_POST

logger = logging.getLogger("GEO-Douyin-API")

router = APIRouter(
    prefix="/api/geo-douyin", tags=["GEO抖音图文"],
    # 🔴 [WO_213] 总闸挂在**路由器**上,不逐条加判定。
    #    逐条加 = 同一谓词写 29 遍(必有一处没人验),而且**将来新增的路由
    #    默认不受闸** —— 合同链那 9 条正是这么漏掉的(它们是后加的)。
    dependencies=[Depends(pipeline_gate)],
)


# 「客户叫什么」这件事的键,按**出处**分成两组 —— 分组不是洁癖,是让锁有的放矢:
#
#   ① `_BRAND_ROW_COLUMNS` —— brands 表的**真列**,来自 `SELECT * FROM brands`。
#      这一组受"列名必须在真表里存在"的锁核验(v5 W1)。写错一个当场红。
#   ② `_BRAND_MAPPED_KEYS` —— **不是列**,是别处已经映射过一轮的 dict 里可能出现的键。
#      它必须被排除在列名核验之外,否则那条锁会因为 `brand_name` 不是真列而恒红;
#      反过来,它要是哪天真变成了 brands 的列,这个分组就该重审 —— 也有锁盯着。
#
# 🔴 两组的顺序合起来就是取值顺序:name → company_name → brand_name。
#    真列在前(有真数据),映射键兜底(只在别处传进来的形状里出现)。
_BRAND_ROW_COLUMNS = ("name", "company_name")
_BRAND_MAPPED_KEYS = ("brand_name",)


def _brand_display_name(brand: Optional[dict]) -> str:
    """客户对外名称。取不到返回空串。**全仓唯一取名入口。**

    🔴 [HOTFIX 2026-08-05 Deploy-CTO] 本文件原来四处都读 `.get("brand_name")`,
       而 `get_brand_by_id` 是 `SELECT * FROM brands` —— brands 表的列叫 `name`,
       **没有 brand_name 这一列**,所以客户名恒空。这是老 bug(上一版逐字相同),
       以前静默:内容照生成,只是从不点名客户(生产实证 post 14 标题
       「深圳深圳全屋定制哪家好…」而该客户是全域上榜科技)。
       geovid v4 加了「不点名客户一律判废」的 P0 闸之后,它从静默变成 100% 失败。

    🔴 [2026-08-06 收敛] hotfix 与 geovid v5 各自建过一个同用途的单点函数
       (`_brand_display_name` name→brand_name / `brand_display_name` name→company_name)。
       文本能自动合,语义不许 —— 合完会同时存在两个"单点",其中一个变零调用死函数,
       而"死函数"这一课本仓上周才付过学费。这里取**语义并集**,只留这一个:

         name          brands 表真列。绝大多数客户的名字在这里。
         company_name  brands 表真列。生产上确有 name 空、只填了公司名的行
                       (v5 实测:brand 208 两列都有,另有只填公司名的)。
         brand_name    **不是列**。别处已经映射过一轮的 dict 才有这个键
                       (hotfix 保留它的理由,原样承下)。

    ⚠️ 同名不同物:`profile_db.get_contact_info_by_brand()` 返回的 `brand_name`
       **是真的**(SQL 里 `COALESCE(b.name, cp.name) AS brand_name` 显式起的别名)。
       所以本文件里 `contact.get("brand_name")` / `info.get("brand_name")` 两处
       不能跟着一起改 —— 判"这个键真不真"要看它从哪个函数出来,不能按键名一刀切。
       (hotfix 的 test_C3 就锁着这一条,合并后仍然有效。)
    """
    if not brand:
        return ""
    for key in _BRAND_ROW_COLUMNS + _BRAND_MAPPED_KEYS:
        val = str(brand.get(key) or "").strip()
        if val:
            return val
    return ""


async def _fetch_brand_row(brand_id: Optional[int], purpose: str) -> Optional[dict]:
    """整个文件里**唯一**读 brands 行的地方。取不到不抛,返回 None —— 由调用方决定这是不是致命。

    [WO_267 之后的订正] 下单冻结行业要品牌上下文,原先在 `_create_and_dispatch_one` 里另写了一遍
    `get_brand_by_id` + 自己的 try/except,「读 brands 行只有一处」这条不变量就破了
    (锁 tests/test_geo_one_pipeline_v5_locks.py::test_only_one_place_reads_the_brand_row)。
    现在两处都走这里;`purpose` 只用于日志,说明是谁取失败的。
    """
    if not brand_id:
        return None
    import asyncio
    try:
        from db.diagnosis_db import get_brand_by_id
        return await asyncio.to_thread(get_brand_by_id, int(brand_id))
    except Exception:  # noqa: BLE001 - 数据库抖动不该以"客户没名字 / 没行业"的形式呈现
        logger.warning("[douyin-api] 读品牌行失败(%s) brand=%s", purpose, brand_id)
        return None


async def fetch_brand_display_name(brand_id: Optional[int]) -> str:
    """按 brand_id 取客户名称。取不到不抛 —— 由调用方决定这是不是致命。

    四个业务入口(蒸馏 / 创建 / 重做 / 发布预填)都走这里;读 brands 行本身只在 `_fetch_brand_row`。
    """
    brand = await _fetch_brand_row(brand_id, "取客户名称")
    return _brand_display_name(brand) if brand else ""


# [WO_213] 原 `_COMING_SOON` 已删:总闸改挂路由器级依赖,闸关统一 503 +
# PIPELINE_DISABLED(见 services/geo_douyin/pipeline_gate.py)。
# 留着一个没人用的"功能关了"常量,下一个人会以为那还是现行说法。


# ─────────────────────────────────────────────────────────────
# 失败原因 → 人话(元指令:工程术语全站翻译人话)
# ─────────────────────────────────────────────────────────────
# 任务表里的 error_msg 是给工程看的,例如
#   "卡片图 2/5 成功;首个失败: image_download_failed"
# 直接摆给代理看等于没说。这里按**阶段 + 错误关键字**翻成一句能指导下一步的话。
# 🔴 兜底那句也必须是人话,不能把原始串漏出去 —— 原始串里可能带供应商线索。
_FAILURE_HINTS = (
    ("image_download_failed", "图片生成好了但没取回来（外部通道抖动），重试一次通常就好"),
    ("image_gen_failed", "图片没生成出来，重试一次"),
    ("gen_failed", "图片没生成出来，重试一次"),
    ("poll_failed", "这次生成超时了，重试一次"),
    ("submit_failed", "没能提交到生成通道，稍后重试"),
    # 🔴 撞额度天花板必须与"服务不可用"分开报。前者重试同一条 prompt
    #    是**结构性无效**(生产 7 天 5 次全是重试再撞),话术要把人引到
    #    "少做几张 / 换个短一点的词"这类真能改变结果的动作上。
    # 口吻类作废:重试大概率就好(模型偶发跑偏),所以话术直接指向重试。
    ("brand_not_promoted", "这次写出来没提到客户，等于白做，已作废；重试一次"),
    ("self_praise_voice", "这次写成了客户自己打广告的口气，已作废；"
                          "内容要用第三方评价的写法才会被 AI 引用，重试一次"),
    ("brand_name_missing", "这个客户还没有名称，先去客户档案里补一个再做内容"),
    ("llm_truncated", "文案写太长了，这次没收住；少做一两张、或把核心词写短一点再试"),
    ("llm_unavailable", "文案没写出来（写作服务暂时不可用），稍后重试"),
    ("llm_bad_json", "文案格式没解析出来，重试一次"),
    ("llm_incomplete", "文案写得不完整，重试一次"),
    ("freeze_failed", "算力不足，充值后再试"),
)

_STAGE_HINTS = {
    "copy": "卡在写文案这一步",
    "images": "卡在生成图片这一步",
    "upload": "卡在保存图片这一步",
}


def humanize_failure(stage: Optional[str], error_msg: Optional[str]) -> str:
    """把任务表的 error_msg 翻成一句代理看得懂、且能指导下一步的话。"""
    raw = str(error_msg or "")
    for key, text in _FAILURE_HINTS:
        if key in raw:
            return text
    where = _STAGE_HINTS.get(str(stage or ""), "")
    # 🔴 兜底不回显原始串:里面可能带供应商/内部实现线索
    return f"{where}，没做成。费用已自动退回，可以重试" if where else \
        "这次没做成，费用已自动退回，可以重试"


def _user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user



def _require_post_access(request: Request, post: dict) -> None:
    """RBAC:作品所属品牌可见者才能看/操作(工单 §4.7)。

    🔴 校验的 target 就是后面真正读写的 target(post['brand_id']),不是入参里的
       brand_id —— 否则可以传一个自己有权的 brand_id 去读别人的作品。
    """
    user = _user(request)
    if post.get("brand_id"):
        require_brand_access(request, int(post["brand_id"]))
    elif int(post.get("created_by") or 0) != int(user["user_id"]) and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权访问该作品")


# ─────────────────────────────────────────────────────────────
# 请求模型
# ─────────────────────────────────────────────────────────────
class CreatePostRequest(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=60)
    # [#150 §3.1 · P0 资金规则] 前端从 POST /production-quote 拿到的那一行的指纹。
    #   🔴 **可选**:老前端不带 ⇒ 不校验、行为逐字节不变;新前端带上才享受这道闸。
    #   对不上返 409 且**零冻结**(见下方校验点的位置说明)。
    expected_price_fingerprint: Optional[str] = None
    # [#150 §3.2] 这条成品算在**哪一个已确认词**上(confirmed_keywords.id)。
    #   🔴 只收词身份,**不收 quote_id** —— 报价号由服务端从词派生。
    #      收客户端给的 quote_id 等于让前端决定这笔算谁的账。
    #   🔴 手填词留空:NULL 是诚实的"未知",不是缺陷。
    confirmed_keyword_id: Optional[int] = None
    brand_id: Optional[int] = None
    industry_key: str = ""
    city: str = ""
    card_count: int = CARD_COUNT_DEFAULT
    extra_hint: str = ""
    variant_index: int = 0
    # 画幅(规范 §8.8:3:4 / 9:16 可自选)。空 = 默认 3:4。
    # 白名单收敛在 normalize_aspect_ratio,不认识的值落默认 —— 不透传给 provider。
    aspect_ratio: str = ""
    # 卡面风格(四款之一)。空 = 默认文字卡。
    # 🔴 Owner 2026-08-04:「风格选择页我们之前不是预设了一些模板吗？现在没有这个部分」
    #    —— 四款预设 + 样例图 2026-08-02 就做好了,但**唯一入口是详情页的
    #    「再次创作」弹窗**,也就是第一次做的时候根本选不了,只能先花 390 做一版
    #    再花 100 重做才能换风格。生产侧 `run_image_post_production(style_key=...)`
    #    一直是收的,断的只有入口这一环。
    # 🔴 不在这里校验"有没有授权实拍图":`resolve_style` 已经带 fail-safe
    #    (要实拍叠字但没授权图 → 降级回文字卡),再加一道会出现两套判定。
    style_key: str = ""
    # 🔴 内容形态。空 = 卡组型(**现有行为逐字不变**);"ranking" = 走榜单主链。
    #    榜单分支只由这一个字段触发 —— 存量订单不传它,一个字都不受影响。
    content_form: str = ""
    # 榜单要点名几家。0 = 按母版默认;有值时收敛到 [3, 6](母版族的 min/max)。
    # 🔴 与 `card_count`(做几张图)**是两件事**:上一版拿张数当家数用,
    #    结果"我要 7 张图"被读成"我要 7 家",而母版最多只支持 6 家。
    ranking_entity_count: int = 0
    # 版式手动覆盖(十母版之一的 template_id)。空 = **自动路由**(默认路径)。
    # 🔴 只影响版式,不影响"能不能叫榜单" —— 无据分支手动覆盖也不许挂榜单名(D12④)。
    ranking_template: str = ""
    # 手动坚持榜单式(覆盖"这个面×行业默认不走榜单"那一层)。
    # 🔴 它**同样不能**把无据说成有据:`route_template` 里 form 由证据定,
    #    force_ranking 只作用在"默认版式偏好"这一层。所以开着它也不会出现
    #    "挂榜单名把客户放榜外"。
    ranking_force: bool = False
    # [WO_204 §1.4] 按**用户定好的选题**做。空 = 现有行为逐字不变(标题由 AI 出)。
    # 🔴 只接 id,**不接标题** —— 收前端传来的标题等于让前端决定落库内容,
    #    那样"用户改过的标题"与"前端凑的一个标题"在后端分不出来。
    #    标题从 `geo_douyin_topics` 现读,读到什么就是什么。
    # 🔴 keyword / city 仍以本请求为准,**不被选题覆盖**:
    #    价格指纹与词身份归属都是按请求里那两个值算的,
    #    用选题上的值去盖会让"报的价"和"算的账"来自两个不同的输入。
    topic_id: Optional[int] = None


class DistillTopicsRequest(BaseModel):
    """一键蒸馏选题。keywords 留空 = 从这个客户的报价单里取已确认的词。"""
    brand_id: int
    keywords: List[str] = Field(default_factory=list)
    city: str = ""
    industry_key: str = ""
    count: int = 5


class FrequencyCheckRequest(BaseModel):
    media_ids: List[int] = Field(default_factory=list)
    cities: List[str] = Field(default_factory=list)
    brand_id: Optional[int] = None


class PrepareMediaRequest(BaseModel):
    post_id: int


class RegenerateRequest(BaseModel):
    """重新生成。variant_index 换一个可以换句式/配色,便于用户"再来一版"。

    🔴 榜单参数**默认继承服务端冻结快照**(`generation_meta.ranking.request`),
       不靠客户端重新拼。上一版这里只收 style/hint,服务端也没回取榜单参数 ——
       于是一条付费的「再次创作」会把榜单作品做成普通图文,全程不报错。
    """
    variant_index: int = 0
    style_key: str = ""                    # 四款风格之一;空 = 沿用这条已有的风格
    extra_hint: str = Field("", max_length=200)   # 弹窗里的"补充要求"
    # 🔴 三态,`None` 与 `""` 不是一回事:
    #    None = **没显式改** → 继承快照;"" = 用户显式选了「自动」;"x" = 显式指定母版。
    #    写成 `str = ""` 会让"没改"和"改成自动"分不开,继承当场失效。
    ranking_template: Optional[str] = None
    ranking_entity_count: Optional[int] = None
    # 🔴 [返工 2026-08-18 · Codex P0-09] 合同链身份三件套。三个都是 Optional ——
    #    老链(手工作品)不传,行为逐字不变;合同链作品必须传,否则被拒。
    #    · request_id:重做是**付费**动作,双击必须只花一次钱;
    #    · expected_topic_ref:必须等于当前 active revision 冻结的那个选题。
    #      不校验就等于"重做时可以换题却仍占同一个合同单位" ——
    #      客户买的是"这个词的一篇",换了题就不是同一篇了(规格 §348);
    #    · override 白名单由 `REGENERATE_OVERRIDE_ALLOWLIST` 显式给出,
    #      **不是任意透传**:透传等于让客户端改任何生成参数。
    request_id: Optional[str] = None
    expected_post_revision: Optional[int] = None
    expected_topic_ref: Optional[str] = None
    # 🔴 [第 3 棒 · Codex R2 · §12 #6 的另一半] 规格 §5.6 的整篇重做请求带
    #    `settings_override`,且**只允许**覆盖那七个键。上一棒加了白名单常量
    #    与 DTO 三件套,但**没有任何一条语句在做集合校验** —— 理由写的是
    #    「当前 handler 只收 style/hint/ranking 三项,天然是白名单子集」。
    #    「天然是子集」是**当下**为真、加一个字段就为假的性质:它不是校验,
    #    是巧合。所以这里把 override 收成一个显式的口袋,并在 handler 内
    #    真的比集合;不在白名单里的键 → 400 且零副作用。
    settings_override: Optional[Dict[str, Any]] = None


#: 规格 02 §348 逐字:重做只允许覆盖这七个。写成**封闭集合**而不是
#: "除了某几个都能改" —— 反向写法在加新字段时会静默放行。
REGENERATE_OVERRIDE_ALLOWLIST = frozenset({
    "card_count", "aspect_ratio", "content_form", "style_key",
    "ranking_template", "ranking_entity_count", "contact_enabled",
})


class RedrawCardRequest(BaseModel):
    """单张重抽。hint 选填 —— 留空 = 原样再跑一次(随机重抽)。"""
    hint: str = Field("", max_length=200)


class UpdatePostTextRequest(BaseModel):
    """文案编辑区的「保存修改」。三个字段都可选,只改传了的那些。

    🔴 用 Optional 而不是默认空串:空串是"清空"这个**真实意图**,
       不能和"没传"混为一谈(混了就永远清不掉正文)。
    """
    title: Optional[str] = Field(None, max_length=200)
    body_text: Optional[str] = None
    hashtags: Optional[List[str]] = None
    expected_revision_id: Optional[int] = Field(None, ge=1)


class ContactToggleRequest(BaseModel):
    enabled: bool = False


class PublishResultRequest(BaseModel):
    """发布完成后回写结果。

    🔴 published_url 是**豆包引用归因锚**(工单 §5/§7):飞轮拿 source_url 反查
       "我们发的哪条被引了" 全靠它。不回写 = 验收 §7.2 的归因反查永远查不到东西。
    """
    post_id: int
    order_id: Optional[int] = None
    item_ids: List[int] = Field(default_factory=list)
    publish_status: str = ""
    published_url: str = ""


# ─────────────────────────────────────────────────────────────
# 开关状态(给前端置灰按钮 + 说明用)
# ─────────────────────────────────────────────────────────────
@router.get("/status")
async def api_status(request: Request):
    """三道闸的状态。前端据此把按钮置灰并说明原因,而不是点了才报错。

    🔴 发布依赖的两道闸在【发布链侧】(MHZ_SVIDEO_UPLOAD_ENABLED /
       MHZ_SVIDEO_PUBLISH_ENABLED),不是本包的 —— 这里只读不改。
    🔴 文案不提供应商名(供应商零暴露),统一叫「外部发布通道」。
    """
    _user(request)
    make_on = is_pipeline_enabled()
    try:
        from services.meijiehezi.config import (SVIDEO_PUBLISH_ENABLED,
                                                SVIDEO_UPLOAD_ENABLED)
        upload_on, publish_on = bool(SVIDEO_UPLOAD_ENABLED), bool(SVIDEO_PUBLISH_ENABLED)
    except Exception:  # noqa: BLE001 - 读不到闸按"关"处理(fail-closed)
        upload_on = publish_on = False

    can_publish = make_on and upload_on and publish_on
    if can_publish:
        reason = ""
    elif not make_on:
        reason = "图文制作即将开放，敬请期待"
    else:
        reason = "外部发布通道暂未开放，可以先做内容并下载成品包"
    return {
        "status": "success",
        "can_make": make_on,
        "can_publish": can_publish,
        "publish_disabled_reason": reason,
    }


# ─────────────────────────────────────────────────────────────
# 价目(实价 · 前端禁硬编码)
# ─────────────────────────────────────────────────────────────
class ProductionQuoteLine(BaseModel):
    """报价的一行。**只收影响价格的轴** —— 风格/画幅不影响价,不进报价也不进指纹。"""
    keyword: str = ""
    city: str = ""
    card_count: int = CARD_COUNT_DEFAULT


class ProductionQuoteRequest(BaseModel):
    lines: List[ProductionQuoteLine] = Field(default_factory=list)


@router.post("/production-quote")
async def api_production_quote(req: ProductionQuoteRequest, request: Request):
    """[#150 §3.1 · P0 资金规则] **服务端算总价**,前端只显示。

    🔴 名字刻意不叫 `production-preview` —— 那是合同产线的端点,
       两条产线的报价混用一个名字,下一个人会读错自己在看哪条线。

    返回逐行单价、总算力、以及每行与整批的 `price_fingerprint`。
    下单时把那一行的指纹带回 `POST /posts`;对不上 409 且零冻结。

    🔴 价目读不到 ⇒ 503 而不是「按 0 算」:按 0 算等于白送,
       一次读库抖动就是资金漏洞(fail-closed 口径来自 services.geo_douyin.pricing)。
    """
    _user(request)
    from services.geo_douyin.pricing import PricingUnavailable
    from services.geo_douyin.production_quote import quote_production

    try:
        quote = await quote_production(req.lines)
    except PricingUnavailable as e:
        logger.warning("[douyin-api] 报价失败(价目不可用): %s", e)
        raise HTTPException(status_code=503, detail={
            "code": "PRICING_UNAVAILABLE",
            "message": "价目暂时读不到，稍等一下再试（没有扣你的算力）"})
    return {"status": "success", **quote}


@router.get("/pricing")
async def api_pricing(request: Request):
    """两档的**真实价目**,给前端按钮与弹窗显示用。

    🔴 前端一律读这里,不许把 390 / 260 写死在 tsx 里 ——
       价目表是 SSOT(铁律 1),Owner 调一次价前端就得跟着改一次代码是错的。
    🔴 读不到就返回 null 而不是兜个默认数:显示一个**猜的价**比不显示更糟
       (用户按看到的价决策,扣的却是另一个数)。
    """
    _user(request)
    import asyncio

    from db.wallet_db import get_feature_pricing

    async def _one(code: str) -> Optional[dict]:
        try:
            row = await asyncio.to_thread(get_feature_pricing, code)
        except Exception as e:  # noqa: BLE001 - 价目读不到不该让整页打不开
            logger.warning("[douyin-api] 价目读取失败 %s: %s", code, e)
            return None
        if not row:
            return None
        return {"feature_code": code,
                "cost_points": int(row.get("cost_points") or 0),
                "feature_name": row.get("feature_name") or ""}

    # brand_fill = 入口页「AI 补全资料」那个按钮的实价。
    # 🔴 它是**别人家的 feature_code**(/api/brand/auto-fill 的),我们只是读价来显示 ——
    #    照样从 feature_pricing 读,绝不在前端写死;Owner 调价两边同时跟着变。
    first, regen, fill, extra, redraw, distill = await asyncio.gather(
        _one(FEATURE_CODE_IMAGE_POST), _one(FEATURE_CODE_IMAGE_POST_REGEN),
        _one("brand_fill"),
        _one(FEATURE_CODE_IMAGE_POST_EXTRA_CARD),
        _one(FEATURE_CODE_IMAGE_POST_REDRAW),
        _one(FEATURE_CODE_TOPIC_DISTILL))
    return {"status": "success", "first_generation": first, "regenerate": regen,
            "brand_fill": fill,
            # 🔴 按张数计价(Owner 2026-08-03)。前端拿 base + max(0, n-included) × extra
            #    自己算总价 —— 和后端 services/geo_douyin/pricing.py 是**同一个公式**,
            #    但两边都不写价格数字,数字只有价目表里那一份。
            "extra_card": extra,
            "included_cards": CARD_COUNT_INCLUDED,
            "card_min": CARD_COUNT_MIN, "card_max": CARD_COUNT_MAX,
            "card_default": CARD_COUNT_DEFAULT,
            # 🔴 重抽 2026-08-03 起**收费**(Owner 拍板 100 算力)。
            #    原来这里返回的是 `redraw_free: True` —— 那个字段已删除而不是留着置 False:
            #    留一个恒 False 的 "free" 字段,下一个人读到的第一眼仍是"有免费这回事"。
            "redraw": redraw, "redraw_limit": REDRAW_LIMIT_PER_POST,
            # 画幅可选项(规范 §8.8)。🔴 由后端给,前端**不许自己写死一份** ——
            #    将来加第三种画幅时,写死的那份会安静地少一个选项。
            #    画幅不影响价格(同一个 resolution 档),所以放在 /pricing 里只是搭个便车。
            "aspect_ratios": [{"key": k, "label": v["label"], "hint": v["hint"]}
                              for k, v in ASPECT_RATIOS.items()],
            "aspect_ratio_default": ASPECT_RATIO_DEFAULT,
            # 🔴 蒸馏 2026-08-03 起**收费**(Owner 拍板 130 算力)。同样只返回价目行,
            #    前端按钮上的数字从这里取,tsx 里一个价格数字都没有。
            "topic_distill": distill,
            # ── 榜单形态的可选项(与画幅同一个道理:选项由后端给)──────────
            # 🔴 十母版族 + 家数区间由后端 SSOT 给,前端**不许自己写死一份**:
            #    加第十一个母版时,写死的那份会安静地少一个选项 ——
            #    画幅那次已经踩过一模一样的坑。
            # 🔴 榜单不额外收费:它是版式选择,不是增值项,所以这里只搭便车带选项,
            #    不带任何价格字段。
            "ranking_templates": ranking_template_options(),
            "ranking_entity_count_min": ENTITY_COUNT_MIN,
            "ranking_entity_count_max": ENTITY_COUNT_MAX,
            "ranking_entity_count_default": ENTITY_COUNT_DEFAULT}


# ─────────────────────────────────────────────────────────────
# 交付计划(规格 02 §4.2)
#
# 🔴 [裁定 2026-08-17 · P0-4] 路径改名:规格原文写的
#    `GET /api/quotes/{quote_id}/delivery-plan` **已被** api/gap_plan_api 占用
#    (server.py:772 挂载,前缀 /api/quotes,服务文章侧缺口作战计划)。
#    改挂本 router(前缀 /api/geo-douyin)避开撞车。
#    全仓路由零冲突证明见 tests/geo_image_note_2026_08_17/test_route_collision_p0_4.py
#    —— 它枚举 server.app.routes 全集求交,并配了三条反向对照
#    (枚举非空 / 被占路径确实被占 / 合成重复必须报冲突)。
# ─────────────────────────────────────────────────────────────
@router.get("/quotes/{quote_id}/delivery-plan")
async def api_quote_delivery_plan(quote_id: int, request: Request,
                                  channel: str = "douyin_image_note"):
    """这张报价分配给图文的可制作容量,以及每一项做到哪一步了。

    权限:当前用户必须对该 quote 的 brand **现时**有授权(不看 created_by)。

    🔴 当前只有降级分支:dormant slot sidecar 的激活要先过窄 RFC
       (工单 §1.4「批了才许动」),未批复前返回 `activation_pending` +
       可执行下一步,且对 geo_article_* 六表零读零写。
       详见 services/geo_douyin/delivery_plan.py 模块 docstring。
    """
    import asyncio

    from services.geo_douyin.delivery_plan import (
        CHANNEL_ARTICLE, CHANNEL_IMAGE_NOTE, SidecarNotApproved, build_delivery_plan,
    )

    _user(request)
    if channel not in (CHANNEL_IMAGE_NOTE, CHANNEL_ARTICLE):
        raise HTTPException(status_code=400, detail=f"不支持的渠道:{channel}")

    from db.diagnosis_db import get_quote

    quote = await asyncio.to_thread(get_quote, int(quote_id))
    if not quote:
        raise HTTPException(status_code=404, detail="报价不存在")

    # 🔴 授权校验的 target 就是后面真正读的 target(quote 自己的 brand_id),
    #    不是入参里的任何 id —— 否则可以拿一个自己有权的 brand 去读别人的报价。
    brand_id = quote.get("brand_id")
    if not brand_id:
        raise HTTPException(status_code=409, detail="这张报价还没有关联客户,暂时无法生成制作计划")
    require_brand_access(request, int(brand_id))

    # 🔴 [热修 P0 2026-08-20] `fetch_brand_display_name` **本来就是 `async def`**
    #    (本文件 103 行)。`asyncio.to_thread` 会在线程里**调用**它 —— 调用一个
    #    协程函数拿到的是**协程对象**,不是结果;`await to_thread(...)` 等到的
    #    也只是那个协程对象。于是 `brand` 是一个 coroutine,
    #    `{"name": <coroutine>}` 一路走到 JSON 序列化才炸 ⇒ **该端点 100% 500**,
    #    附带一条 "coroutine was never awaited" 的 RuntimeWarning。
    #    这一形态的可怕之处在于它**静默到序列化才炸**:类型检查、单测 mock、
    #    甚至 handler 里的任何 `if brand:` 都看不出问题(协程对象是真值)。
    #    直接 await 它。全仓同形态 census + 锁见
    #    `tests/hotfix_to_thread_2026_08_20/test_to_thread_async_target.py`。
    brand = await fetch_brand_display_name(int(brand_id))
    brand_dto = {"id": int(brand_id), "name": brand}
    try:
        plan = await asyncio.to_thread(
            build_delivery_plan, quote_id=int(quote_id), brand=brand_dto, channel=channel,
        )
    except SidecarNotApproved as e:
        # 配置开了闸但 RFC 没批 —— 响亮失败,不静默走 sidecar
        logger.error("[douyin-delivery-plan] %s", e)
        raise HTTPException(status_code=503, detail={
            "code": "SCHEMA_NOT_READY",
            "message": "制作计划功能尚未开通",
            "reason": "交付槽位能力的激活审批未完成",
            "impact": "本次未创建任何制作任务,未冻结算力",
            "repair_hint": "联系管理员核对开通状态",
            "actions": [{"id": "contact_admin", "label": "联系管理员", "type": "contact"}],
            "next_action": "contact_admin",
            "retryable": False,
        })
    return {"status": "success", **plan}


# ─────────────────────────────────────────────────────────────
# 四款风格(实拍叠字要有已授权实拍图才可选)
# ─────────────────────────────────────────────────────────────
@router.get("/styles")
async def api_styles(request: Request, brand_id: Optional[int] = None):
    """风格选择器的四个选项。

    「实拍叠字」是否可选取决于这个客户**有没有已授权且已确权的实拍图** ——
    不是随便置灰,是有真判据的(没图还硬做,模型会编一张假实景图冒充客户现场)。
    """
    _user(request)
    import asyncio

    from services.geo_douyin.card_templates import style_choices

    has_photo = False
    if brand_id:
        require_brand_access(request, int(brand_id))
        try:
            from services.geo_douyin.knowledge_context import load_authorized_images
            imgs = await asyncio.to_thread(load_authorized_images, int(brand_id))
            has_photo = bool(imgs)
        except Exception as e:  # noqa: BLE001 - 查不到按"没有"处理(fail-closed)
            logger.warning("[douyin-api] 授权图查询失败 brand=%s: %s", brand_id, e)
    return {"status": "success", "styles": style_choices(has_authorized_photo=has_photo)}


# ─────────────────────────────────────────────────────────────
# 要做哪几条(按城市铺开)· 不扣费,纯本地计算
# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# 一键蒸馏选题(**收费** 130 · Owner 2026-08-03 拍板)
# ─────────────────────────────────────────────────────────────
# 形态:2026-08-05 由 **A 类同步短任务** 改为 **异步任务**
# (WO-DISTILL-TIMEOUT-ASYNC-2026-08-05)。原因见端点 docstring:
# 同步版生产成功率 0/5,真实耗时 58.0s > 42s 窗口,而 nginx 60s 是硬墙。
#
# 🔴 计费形态**没变**:仍是 `charge_on_success`「完成才扣」,只是搬进后台任务里
#    包着蒸馏本身。**没有**改成"提交即扣"(工单红线)。
#
# 🔴 节流也**没变**性质,变的是释放时机 —— 这是本次最容易改坏的一点:
#    同步版把 in-flight 占位放在**请求的 finally** 里放,那时蒸馏刚好跑完;
#    改异步后请求 2 秒就返回,占位若仍跟着请求放,连点两下就是**两次 130**。
#    → 占位改由**后台任务**结束时释放(services/geo_douyin/distill_task.py),
#      并加 DB 部分唯一索引 `uq_geo_douyin_distill_inflight` 作第二道
#      (WORKERS 被调回 4、或两请求真并发时,进程内那道就失效了)。
#    判定与占位之间仍然**一个 await 都不能有**(理由同前:中间 await 一次,
#    两个并发请求就双双通过、双双扣费)。
#
# 老的 `_DISTILL_LAST_AT` / `_DISTILL_INFLIGHT` / `_distill_throttle_*` 已随本次
# 改造删除 —— 改异步后它们零调用方。留着就是死代码,而这个仓刚因"死函数被当成
# 已接线"栽过(`ef275b80` 复审漏接线)。锁在 tests/ 里同步改成锁新行为。


@router.post("/distill-topics")
async def api_distill_topics(req: DistillTopicsRequest, request: Request):
    """一键蒸馏选题:提交即返回 task_id,**不同步等 LLM**。进度走 GET /distill-tasks/{id}。

    🔴 为什么必须异步(WO-DISTILL-TIMEOUT-ASYNC-2026-08-05):
       同步版生产成功率 **0/5**,5 次全部精确顶到 42s 窗口上限;放开窗口独立复测
       真实耗时 **58.0s**。nginx `proxy_read_timeout 60s` 是硬墙 ——
       任何同步方案都贴着墙走,调大 42 只会把"干净失败"换回"504 乱码 + 扣费"。
       原作者在 topic_distiller.py 里写的正解就是改异步。

    **收费 130**(Owner 2026-08-03 拍板)· `charge_on_success` **原样保留**,
    只是搬进后台任务里包着蒸馏本身:成功走完才扣,失败一分不扣。
    这里**没有**改成"提交即扣"。提交时只做一次纯读的余额预检,
    好让"算力不足"仍在提交那一刻就告诉用户,而不是等 60 秒才发现。
    """
    user = _user(request)
    if not req.brand_id:
        raise HTTPException(status_code=400, detail={
            "code": "BRAND_REQUIRED",
            "message": "先选一个客户 —— 选题要从这个客户的资料里蒸馏出来"})
    brand_id = int(req.brand_id)
    require_brand_access(request, brand_id)

    import asyncio

    from db import geo_douyin_db as ddb
    from middleware.billing import check_balance_only
    from services.geo_douyin.distill_task import (dispatch_distill,
                                                  release_inflight,
                                                  stale_after_seconds,
                                                  try_acquire_inflight)
    from services.geo_douyin.topic_distiller import (TOPIC_COUNT_DEFAULT,
                                                     load_quote_keywords)

    # 🔴 占位必须在**所有 await 之前**:判定与写入之间隔一个 await,
    #    两个并发请求就会双双通过 → 那是两次真调用、两次 130。
    # 🔴 与同步版的关键差别:这个占位由**后台任务**结束时释放(run_distill_task 的
    #    finally),不是请求结束时。请求 2 秒就返回了,跟着请求放等于没锁。
    if not try_acquire_inflight(brand_id):
        raise HTTPException(status_code=429, detail={
            "code": "TOO_FREQUENT",
            "message": "这个客户正在蒸选题，等这一单出来再来"})

    task_id = None
    try:
        # 词的默认来源是**报价单**(Owner:「根据前面报价这里添加几条图文」),
        # 用户显式传了就用传的。
        keywords = [k.strip() for k in (req.keywords or []) if k.strip()]
        if not keywords:
            keywords = await load_quote_keywords(brand_id)
        if not keywords:
            raise HTTPException(status_code=400, detail={
                "code": "NO_KEYWORDS",
                "message": "这个客户还没有确认的关键词，先在报价里选词，或者自己填一个"})

        # ── 余额预检(纯读,不扣费)──
        # 🔴 这**不是**"提交即扣":真扣费仍由后台任务里的 charge_on_success 在
        #    蒸馏成功后执行。这里只是把"算力不足"提前到提交那一刻告诉用户 ——
        #    否则他要等 60 秒才知道自己余额不够,而那 60 秒的成本已经真花出去了。
        try:
            await check_balance_only(int(user["user_id"]), FEATURE_CODE_TOPIC_DISTILL)
        except HTTPException:
            raise   # 平台那条 402 带着"需要 130 / 你还有多少",比我自己编的强
        except Exception as e:  # noqa: BLE001
            # 价目行缺失(漏跑 023)会走到这里。fail-closed:报不可用,**不是**免费放行。
            logger.warning("[douyin-api] 蒸馏价目不可用 brand=%s: %s", brand_id, e)
            raise HTTPException(status_code=503, detail={
                "code": "PRICING_UNAVAILABLE",
                "message": "这个功能的价目暂时读不到，稍后再试"}) from e

        # [WO_282-C] 入口归一:语料 key 是飞轮 normalize_industry_key 的 13 个枚举(+ general),
        #   load_fewshot 与 detect_sample_taint 都拿它精确匹配;前端送的是品牌档案里的自由文本。
        #   与下单链(freeze_industry / card_templates._ikey)同一个归一器 —— 不改语义、不改语料;空值仍落 general。
        #   判据:tests/distill_industry_key_normalized_2026_09_23。
        from services.media_entity_flywheel import normalize_industry_key
        industry_key = normalize_industry_key(req.industry_key or "")
        brand_name = await fetch_brand_display_name(brand_id)

        # 超龄回收:进程被杀时后台 finally 不执行,留下的 running 行会被部分唯一索引
        # 认成"在飞" → 那个客户从此再也蒸不了,且静默。先回收再建行。
        try:
            await asyncio.to_thread(ddb.reap_stale_distill_tasks, stale_after_seconds())
        except Exception as e:  # noqa: BLE001 - 回收失败不该挡住本次提交
            logger.warning("[douyin-api] 蒸馏任务回收失败: %s", e)

        try:
            task_id = await asyncio.to_thread(
                ddb.create_distill_task, brand_id=brand_id,
                user_id=int(user["user_id"]), keywords=keywords)
        except ddb.DistillInFlight:
            # DB 级兜底:进程内锁没挡住(WORKERS>1 / 两请求真并发)时由唯一索引挡。
            raise HTTPException(status_code=429, detail={
                "code": "TOO_FREQUENT",
                "message": "这个客户正在蒸选题，等这一单出来再来"})

        dispatch_distill(
            task_id=task_id, user_id=int(user["user_id"]), brand_id=brand_id,
            keywords=keywords, brand_name=brand_name,
            city=(req.city or "").strip(), industry_key=industry_key,
            want=int(req.count or 0) or TOPIC_COUNT_DEFAULT,
            feature_code=FEATURE_CODE_TOPIC_DISTILL)
        return {"status": "accepted", "task_id": task_id,
                "keywords_used": keywords}
    except BaseException:
        # 🔴 任务没派出去 → 后台 finally 不会跑 → 占位必须在这里放,
        #    否则这个客户被锁到自愈上限才恢复。派出去之后由后台负责放。
        if task_id is None:
            release_inflight(brand_id)
        raise


@router.get("/distill-tasks/{task_id}")
async def api_distill_task_status(task_id: int, request: Request):
    """蒸馏任务进度 / 结果。终态(succeeded/failed)前端停轮询。

    成功时把 topics 一并带回 —— 不再让前端多打一次"取结果"的请求
    (多一跳就多一处"完成了但结果取不回来"的机会,那是验收判据之一)。
    """
    _user(request)

    import asyncio

    from db import geo_douyin_db as ddb
    from services.geo_douyin.distill_task import describe_distill_progress

    task = await asyncio.to_thread(ddb.get_distill_task, int(task_id))
    if task:
        # 🔴 越权核验的 target 就是要读的那一行的 brand_id(不是请求里传的值)
        require_brand_access(request, int(task["brand_id"]))

    progress = describe_distill_progress(task)
    payload = {"task_id": int(task_id), **progress}
    if task and str(task.get("status")) == "succeeded":
        result = task.get("result") or {}
        if isinstance(result, str):
            import json as _json
            try:
                result = _json.loads(result)
            except (TypeError, ValueError):
                result = {}
        payload.update(result)
    return payload


@router.get("/clients/{brand_id}/latest-topics")
async def api_latest_distilled_topics(brand_id: int, request: Request):
    """这个客户最近一次蒸出来的选题。**没有就返回空,不是 404。**

    🔴 为什么要有:选题原来只活在前端组件 state 里 —— 切一次 tab 就没了,
       而这一次蒸馏是花了算力的(生产实证:切 tab 后 `distilled-topics` 节点消失,
       结果却一直躺在 `geo_douyin_distill_tasks.result` 里)。
       用户能看到的不该比我们存下来的少。
    🔴 本端点**不产生任何费用**:只读已经算过的结果,不触发新的蒸馏。
    """
    _user(request)
    require_brand_access(request, int(brand_id))

    import asyncio

    from db import geo_douyin_db as ddb

    task = await asyncio.to_thread(ddb.get_latest_distill_task, int(brand_id))
    if not task:
        return {"status": "success", "topics": [], "fewshot_degraded": ""}
    result = task.get("result") or {}
    if isinstance(result, str):
        import json as _json
        try:
            result = _json.loads(result)
        except (TypeError, ValueError):
            result = {}
    return {
        "status": "success",
        "task_id": int(task["id"]),
        "topics": result.get("topics") or [],
        "fewshot_degraded": result.get("fewshot_degraded") or "",
        "distilled_at": task.get("finished_at"),
    }


# ─────────────────────────────────────────────────────────────
# [WO_204 §1.3] 选题列表(和写文章的「待写」一样:AI 出、人可改、按标题做)
#
# 🔴 这一组**一分钱都不花**:只读/只改已经蒸出来的选题行。
#    花钱的只有两处,都没动:蒸馏 130(charge_on_success)、制作 freeze/commit。
# ─────────────────────────────────────────────────────────────
class AddTopicRequest(BaseModel):
    """手加一条选题。"""
    title: str = Field(..., min_length=1, max_length=120)
    keyword: str = ""
    city: str = ""
    angle: str = ""
    card_outline: List[str] = Field(default_factory=list)
    confirmed_keyword_id: Optional[int] = None


class PatchTopicRequest(BaseModel):
    """改标题。**只收标题** —— 别的字段改了会让这条题与它的成品对不上。"""
    title: str = Field(..., min_length=1, max_length=120)


async def _topic_or_404(topic_id: int, request: Request) -> dict:
    """读一条并校验归属。

    🔴 先读行、再按行上的 brand_id 查权限 —— 不信请求里的 brand_id。
       信了的话,拿别人的 topic_id 配自己的 brand_id 就能读到别人的选题。
    """
    import asyncio

    from db import geo_douyin_db as ddb

    row = await asyncio.to_thread(ddb.get_topic, int(topic_id))
    if not row:
        raise HTTPException(status_code=404, detail={
            "code": "TOPIC_NOT_FOUND", "message": "这条选题找不到了，刷新一下列表"})
    require_brand_access(request, int(row["brand_id"]))
    return row


@router.get("/clients/{brand_id}/topics")
async def api_list_topics(brand_id: int, request: Request,
                          status: str = "", limit: int = 50, offset: int = 0):
    """某客户的选题列表。`status` 空 = 全部。"""
    _user(request)
    require_brand_access(request, int(brand_id))

    import asyncio

    from db import geo_douyin_db as ddb

    page = await asyncio.to_thread(
        ddb.list_topics, brand_id=int(brand_id), status=str(status or ""),
        limit=min(200, max(1, int(limit))), offset=max(0, int(offset)))
    return {"status": "success", **page}


@router.post("/clients/{brand_id}/topics")
async def api_add_topic(brand_id: int, req: AddTopicRequest, request: Request):
    """手加一条。与写文章大厅「自己加一个标题」同义。"""
    user = _user(request)
    require_brand_access(request, int(brand_id))

    import asyncio

    from db import geo_douyin_db as ddb

    new_id = await asyncio.to_thread(
        ddb.create_topic, brand_id=int(brand_id), created_by=int(user["user_id"]),
        title=req.title, keyword=req.keyword, city=req.city, angle=req.angle,
        card_outline=list(req.card_outline or []),
        confirmed_keyword_id=req.confirmed_keyword_id)
    return {"status": "success", "topic_id": new_id}


@router.patch("/topics/{topic_id}")
async def api_patch_topic(topic_id: int, req: PatchTopicRequest, request: Request):
    """改标题。做中/已做 ⇒ 409。

    🔴 409 的话要说清**为什么点不动**:只说"改不了"会让用户反复点。
    """
    _user(request)
    await _topic_or_404(topic_id, request)

    import asyncio

    from db import geo_douyin_db as ddb

    outcome = await asyncio.to_thread(
        ddb.update_topic_title, topic_id=int(topic_id), title=req.title)
    if outcome == ddb.TOPIC_OK:
        row = await asyncio.to_thread(ddb.get_topic, int(topic_id))
        return {"status": "success", "topic": row}
    if outcome == ddb.TOPIC_LOCKED:
        raise HTTPException(status_code=409, detail={
            "code": "TOPIC_LOCKED",
            "message": "这条已经开始做了，改标题要先换一条没做的"})
    if outcome == ddb.TOPIC_EMPTY_TITLE:
        raise HTTPException(status_code=400, detail={
            "code": "TOPIC_TITLE_EMPTY", "message": "标题不能是空的"})
    raise HTTPException(status_code=404, detail={
        "code": "TOPIC_NOT_FOUND", "message": "这条选题找不到了，刷新一下列表"})


@router.delete("/topics/{topic_id}")
async def api_delete_topic(topic_id: int, request: Request):
    """删一条。只有没开始做的能删 —— 做中/已做删掉会让成品成孤儿。"""
    _user(request)
    await _topic_or_404(topic_id, request)

    import asyncio

    from db import geo_douyin_db as ddb

    outcome = await asyncio.to_thread(ddb.delete_topic, int(topic_id))
    if outcome == ddb.TOPIC_OK:
        return {"status": "success"}
    if outcome == ddb.TOPIC_LOCKED:
        raise HTTPException(status_code=409, detail={
            "code": "TOPIC_LOCKED",
            "message": "这条已经开始做了，删不了；做完可以在作品里处理"})
    raise HTTPException(status_code=404, detail={
        "code": "TOPIC_NOT_FOUND", "message": "这条选题找不到了，刷新一下列表"})


# ─────────────────────────────────────────────────────────────
# 生产(B 类异步:freeze → commit/release)
# ─────────────────────────────────────────────────────────────
async def _create_and_dispatch_one(req: CreatePostRequest,
                                  request: Request) -> dict:
    """建作品,把生产丢到后台,**立刻**返回 post_id;进度走 GET /posts/{id}/task。

    🔴 为什么不同步等:生产 nginx 对 /api/geo-douyin/* 走默认 `location /api/`,
       proxy_read_timeout = **60s**;而一组卡实测 ≈137s(封面串行 + 其余并发)。
       同步跑**必然 504** —— 用户看到"请求失败",后台还在跑、积分还冻着。
    🔴 计费一个字没改:冻结/扣费/退款仍全在 run_image_post_production 里,
       只是它现在跑在后台协程上。余额不足这类失败,前端从任务行读到人话。
    """
    user = _user(request)
    user_id = int(user["user_id"])

    # 🔴 客户必选(SSOT《GEO 内容生产是一条流水线》铁律 1 · Owner 2026-08-03 拍板)。
    #    与写文章一致:先选客户,资料才通。
    #    这不是"多校验一道" —— 生产实测 6 条作品 brand_id 全 NULL,导致
    #    知识库引用 / 联系方式 / 黄点一致性 / 发布客户栏 / require_brand_access
    #    **全部空转**。一个缺口把整条知识库链打空了,所以从入口就堵死。
    if not req.brand_id:
        raise HTTPException(status_code=400, detail={
            "code": "BRAND_REQUIRED",
            "message": "先选一个客户再做 —— 内容要引用这个客户的资料、"
                       "联系方式和图片,不选就只能做一条跟客户无关的通用内容"})
    require_brand_access(request, int(req.brand_id))

    # 🔴 夹取走 pricing 的唯一实现:张数直接决定加价额,
    #    "前端夹一套、后端夹另一套"是最容易出计价分歧的地方。
    from services.geo_douyin.pricing import clamp_card_count

    card_count = clamp_card_count(req.card_count)

    import asyncio

    from db import geo_douyin_db as ddb
    from services.geo_douyin.production_task import dispatch_production

    # 🔴 名字必须在**下单前**拿到,不能等到花完 390 才发现没有。
    #    生产侧「产出不点名客户就判废」是硬规则,名字为空 = 这一单结构上做不成。
    #    与其冻结→跑 LLM→判废→退款(用户等 2 分钟看一句含糊话),不如当场说清楚。
    brand_name = await fetch_brand_display_name(req.brand_id)
    if not brand_name:
        raise HTTPException(status_code=400, detail={
            "code": "BRAND_NAME_MISSING",
            "message": "这个客户还没有名称，先去客户档案里补一个再做内容"})

    # 🔴 [#150 §3.1] 价格指纹比对**必须在这里** —— 在 `ddb.create_post` 与
    #    `dispatch_production` 之前。冻结发生在后台的 run_image_post_production 里,
    #    所以「零冻结」这句话的实现方式就是**根本走不到那一步**:
    #    放到建行之后会留下孤儿 post 行,放到 dispatch 之后钱就已经冻了,
    #    用户看到的是被扣了又退。
    #    ⚠️ 这个性质在顺利路径上**不可观测**(指纹对得上时,校验在前在后读数完全相同),
    #       所以判据配了结构臂比行号,毒是「把它挪到后面」而不是「删掉它」。
    from services.geo_douyin.production_quote import fingerprint_matches

    if not await fingerprint_matches(req, req.expected_price_fingerprint):
        raise HTTPException(status_code=409, detail={
            "code": "PRICE_FINGERPRINT_MISMATCH",
            "message": "价格已经变了，请刷新一下再提交（这次没有扣你的算力）"})

    # [#150 §3.2] 词身份 → 报价号,**服务端派生 + 校验归属**。
    #   校验的是「这个词确实属于这个客户、且报价处于 confirmed/paid」——
    #   否则把别人的报价号记到自己的成品上,交付统计就串了。
    from services.geo_douyin.topic_distiller import (
        resolve_quote_for_confirmed_keyword)

    quote_id_for_post = None
    confirmed_keyword_id = req.confirmed_keyword_id
    if confirmed_keyword_id:
        quote_id_for_post = await resolve_quote_for_confirmed_keyword(
            int(confirmed_keyword_id), brand_id=int(req.brand_id))
        if quote_id_for_post is None:
            raise HTTPException(status_code=400, detail={
                "code": "CONFIRMED_KEYWORD_NOT_FOR_BRAND",
                "message": "这个词不属于该客户已确认的报价，请刷新后重新选词"})

    aspect_ratio = normalize_aspect_ratio(req.aspect_ratio)

    # 🔴 2026-08-08 包 B:**行业归并放在这里**(下单前),不放在后台执行期。
    #    理由不是延迟(这条链本来就在密集调 LLM),而是:执行期归并必须只读
    #    (否则污染共享行业表),而只读就永远不沉淀别名 —— 每一单从头烧一遍,
    #    第二次不会更便宜。归并只有放在能安全落库的时点才有复利。
    #    这里是付费提交动作,不是浏览,不存在 FIX1-01 那条"浏览就烧 LLM"的成本泄漏。
    #    冻结件随任务一路传到 `build_ranking_plan`,执行期只消费、不再归并 ——
    #    期间 admin 改了行业名也不影响这一单。
    from services.industry_canonical import freeze_industry

    # [WO_267] 行业判定吃品牌上下文(品牌已在上面 require_brand_access 过)⇒ 走行业路由前段,
    #   与付费点亮写路径同一份顺序;冻结件口径版本随之 v1.1。
    import asyncio            # 本文件惯例:局部 import(模块级没有)

    # 读 brands 行只走 _fetch_brand_row(取不到 ⇒ None ⇒ 行业按原文判,与原来的兜底一致)
    _freeze_brand = await _fetch_brand_row(req.brand_id, "下单冻结行业的品牌上下文")
    frozen_industry = (await freeze_industry(req.industry_key, taxonomy=True,
                                             brand=_freeze_brand)).to_meta()

    # ── [WO_204 §1.4] 按选题制作 ──────────────────────────────────────────
    # 🔴 抢在**所有前置闸之后**:品牌 / 名称 / 价格指纹 / 词身份任一不过,
    #    这条选题都不该被锁成"制作中" —— 锁了用户就再也点不动它,
    #    而列表上只显示"制作中",看不出是被一个 400 顺手锁死的。
    # 🔴 抢单本身就是"不许重复做"的闸:守卫写在 UPDATE 的 WHERE 里,
    #    双击时数据库裁决谁是第一次。「先查 status 再建单」在双击下两次都读到
    #    pending、两次都建单、两次都扣算力。
    topic_row = None
    topic_title = ""
    extra_hint = str(req.extra_hint or "")
    if req.topic_id:
        topic_row = await asyncio.to_thread(ddb.claim_topic_for_production, int(req.topic_id))
        if not topic_row:
            existing = await asyncio.to_thread(ddb.get_topic, int(req.topic_id))
            if not existing:
                raise HTTPException(status_code=404, detail={
                    "code": "TOPIC_NOT_FOUND",
                    "message": "这条选题找不到了，刷新一下列表再试"})
            raise HTTPException(status_code=409, detail={
                "code": "TOPIC_ALREADY_TAKEN",
                "message": "这条选题已经在做或者做过了，换一条吧"})
        if int(topic_row.get("brand_id") or 0) != int(req.brand_id):
            # 归属不符就放回去 —— 抢都抢了,不放回它会永远卡在制作中。
            await asyncio.to_thread(ddb.release_topic,
                                    topic_id=int(req.topic_id), failed=False)
            raise HTTPException(status_code=400, detail={
                "code": "TOPIC_NOT_FOR_BRAND",
                "message": "这条选题不属于这个客户，刷新后重新选"})
        topic_title = str(topic_row.get("title") or "").strip()
        # angle / card_outline 当提示喂给生成,不覆盖用户自己填的 extra_hint。
        _angle = str(topic_row.get("angle") or "").strip()
        _outline = topic_row.get("card_outline") or []
        if isinstance(_outline, str):
            import json as _json_outline
            try:
                _outline = _json_outline.loads(_outline)
            except Exception:  # noqa: BLE001
                _outline = []
        _bits = [b for b in ([_angle] + [str(x) for x in _outline if str(x).strip()]) if b]
        if _bits:
            extra_hint = (extra_hint + " " if extra_hint else "") + "；".join(_bits)

    try:
        post_id = await asyncio.to_thread(
            ddb.create_post, created_by=user_id, brand_id=req.brand_id,
            quote_id=quote_id_for_post, confirmed_keyword_id=confirmed_keyword_id,
            # 🔴 落库仍是**用户原文** —— 归并结果只进冻结件,不覆盖档案里那句话。
            #    (工单 §6 边界:不改存量、原文保留可回溯。)
            keyword=req.keyword.strip(), industry_key=req.industry_key,
            city=req.city.strip(), aspect_ratio=aspect_ratio,
        )
        # 建完就先置"制作中",别让列表在后台任务落地前显示成 draft
        await asyncio.to_thread(ddb.set_post_status, post_id, "generating")
    except Exception:
        # 🔴 抢过的选题必须放回。建行失败时不放,它就永远卡在"制作中" ——
        #    用户点不动、列表也不解释为什么。放回 `pending` 而不是 `failed`:
        #    这一次根本没开始做,不是做失败了。
        if topic_row:
            await asyncio.to_thread(ddb.release_topic,
                                    topic_id=int(req.topic_id), failed=False)
        raise

    dispatch_production(
        post_id=post_id, user_id=user_id, keyword=req.keyword.strip(),
        brand_id=req.brand_id, brand_name=brand_name, city=req.city.strip(),
        card_count=card_count, extra_hint=extra_hint,
        variant_index=req.variant_index,
        # [WO_204 §1.4] 空 = 现有行为逐字不变。非空 = 标题以选题为准,
        # 成败由 dispatch 那层统一收尾(done 写 post_id / failed 放回)。
        topic_id=int(req.topic_id) if req.topic_id else None,
        topic_title=topic_title,
        feature_code=FEATURE_CODE_IMAGE_POST,   # 首次制作档(390)
        aspect_ratio=aspect_ratio,
        industry_key=req.industry_key,
        frozen_industry=frozen_industry,
        content_form=str(req.content_form or '').strip(),
        ranking_entity_count=int(req.ranking_entity_count or 0),
        ranking_template=str(req.ranking_template or '').strip(),
        ranking_force=bool(req.ranking_force),
        # 🔴 原样透传,不在这里兜白名单:`resolve_style` 认不出的 key 落默认款,
        #    而真正生效的那一款由 production_task 用 `preset.key` 写回库
        #    —— 落库的是**实际用了什么**,不是用户请求了什么(降级过就该记降级后的)。
        style_key=str(req.style_key or "").strip(),
    )
    return {
        "status": "accepted", "post_id": post_id,
        "message": "开始制作了，做好会自动出现在下面",
        # 前端拿这个去轮询;分母先给用户要的张数,真实张数由后台定稿后修正
        "cards_total": card_count,
    }


@router.post("/posts")
async def api_create_and_produce(req: CreatePostRequest, request: Request):
    """单条入口 —— 保留给重试与详情页。

    🔴 [#150 §3.3] 真正的逻辑在 `_create_and_dispatch_one`,批量端点调**同一份**:
       品牌校验 / 名称校验 / 价格指纹 / 词身份归属这四道闸写两份就一定会漂,
       而漂开的表现是「单条挡住了、批量放过去了」,两边各自看都正常。
    """
    return await _create_and_dispatch_one(req, request)


class BatchCreateRequest(BaseModel):
    """一次提交 N 条。**item 直接复用 `CreatePostRequest`** —— 校验只有一份。"""
    request_id: str = Field(..., min_length=8, max_length=64)
    items: List[CreatePostRequest] = Field(..., min_length=1, max_length=20)


@router.post("/posts/batch")
async def api_create_batch(req: BatchCreateRequest, request: Request):
    """[#150 §3.3] 一次提交 N 条:一个 `request_id` 幂等、逐条独立、失败即停。

    语义(工单 §3.3 逐字):
      · **幂等**:同一个 `request_id` 重放 ⇒ 原样返回上次的逐条结果,不重复建单;
      · **逐条独立**:每条都走 `_create_and_dispatch_one`(与单条端点**同一份**);
      · **失败即停**:某条失败 ⇒ 该条与**其后各条**不建,**前面已建的照跑**
        (已经在后台跑的不回滚 —— 那是用户已经要到的东西)。

    🔴 余额只做**只读预检**,不在这里冻结。冻结仍在后台的
       `run_image_post_production`(工单 §3:freeze/commit 路径不动)。
       所以预检是**建议性**的:它挡住"明显不够"的那几条,
       真正的裁决仍在冻结那一刻。写成"预检通过就一定能扣"是假的,
       本函数不做那个承诺。
    """
    user = _user(request)
    user_id = int(user["user_id"])

    import asyncio

    from db import geo_douyin_db as ddb

    # 幂等认领:抢到才干活。没抢到 = 这个 request_id 已经来过。
    existing = await asyncio.to_thread(
        ddb.claim_post_batch, created_by=user_id, request_id=req.request_id,
        brand_id=(req.items[0].brand_id if req.items else None))
    if existing is not None:
        return {"status": "success", "idempotent_replay": True,
                "batch_status": existing.get("status"),
                "items": existing.get("result") or []}

    # 只读余额预检的起点(见 docstring:建议性,不是承诺)。
    from services.geo_douyin.production_quote import quote_production

    try:
        balance = await asyncio.to_thread(
            __import__("db.wallet_db", fromlist=["get_wallet_balance"]
                       ).get_wallet_balance, user_id)
        remaining = int(balance.get("paid_points") or 0) + int(
            balance.get("bonus_points") or 0)
    except Exception as exc:  # noqa: BLE001
        # 🔴 读不到余额**不拦**:预检是建议性的,把它变成硬闸会让
        #    一次读库抖动挡住所有人下单。真闸在冻结那一刻。
        logger.warning("[douyin-batch] 余额预检读取失败 user=%s: %s", user_id, exc)
        remaining = None

    results: List[dict] = []
    stopped = False
    for idx, item in enumerate(req.items):
        if stopped:
            results.append({"index": idx, "post_id": None,
                            "code": "SKIPPED_AFTER_FAILURE",
                            "message": "前面有一条没成功，这条没有提交（没有扣算力）"})
            continue
        # 逐条报价 —— 走**同一份**计价实现(services.geo_douyin.production_quote)。
        try:
            line_cost = (await quote_production([item]))["total_points"]
        except Exception as exc:  # noqa: BLE001 价目不可用 ⇒ 这条判失败并停
            logger.warning("[douyin-batch] 报价失败 idx=%s: %s", idx, exc)
            results.append({"index": idx, "post_id": None,
                            "code": "PRICING_UNAVAILABLE",
                            "message": "价目暂时读不到，这条没有提交（没有扣算力）"})
            stopped = True
            continue
        if remaining is not None and line_cost > remaining:
            results.append({"index": idx, "post_id": None,
                            "code": "INSUFFICIENT_BALANCE",
                            "message": "算力不够做这一条了，充值后再继续（没有扣算力）"})
            stopped = True
            continue
        try:
            created = await _create_and_dispatch_one(item, request)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {
                "code": "REJECTED", "message": str(exc.detail)}
            results.append({"index": idx, "post_id": None,
                            "code": detail.get("code") or "REJECTED",
                            "message": detail.get("message") or ""})
            stopped = True
            continue
        if remaining is not None:
            remaining -= line_cost
        results.append({"index": idx, "post_id": created.get("post_id"),
                        "code": None, "message": ""})

    await asyncio.to_thread(ddb.finish_post_batch, created_by=user_id,
                            request_id=req.request_id, result=results)
    return {"status": "success", "idempotent_replay": False,
            "items": results,
            "created": sum(1 for r in results if r.get("post_id")),
            "stopped_at": next((r["index"] for r in results
                                if r.get("code") and r["code"] != "SKIPPED_AFTER_FAILURE"),
                               None)}


@router.get("/posts/{post_id}/task")
async def api_post_task(post_id: int, request: Request):
    """查这条内容当前的制作进度。前端 2s 轮一次,必须**轻**。

    🔴 这里刻意不签预览 URL:签名有外部往返,2s 一次会把它放大成常驻负载。
       图好了前端自己去拉一次 GET /posts/{id}。
    """
    import asyncio

    from db import geo_douyin_db as ddb
    from services.geo_douyin.task_progress import describe_task_progress

    post = await asyncio.to_thread(ddb.get_post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="作品不存在")
    _require_post_access(request, post)

    task = await asyncio.to_thread(ddb.get_task_by_post, post_id)
    progress = describe_task_progress(task, post_status=str(post.get("status") or ""))
    return {
        "status": "success",
        "post_id": post_id,
        "post_status": post.get("status"),
        **progress,
        # 失败原因翻人话的**唯一**出口就是 humanize_failure,不在前端再写一套
        "failure_reason": (humanize_failure((task or {}).get("stage"),
                                            (task or {}).get("error_msg"))
                           if progress["state"] in ("failed", "stalled") else ""),
    }


@router.get("/posts")
async def api_list_posts(request: Request, brand_id: Optional[int] = None,
                         status: str = "", limit: int = 20, offset: int = 0,
                         publication_bucket: str = ""):
    user = _user(request)
    if brand_id:
        require_brand_access(request, int(brand_id))
    import asyncio

    from db import geo_douyin_db as ddb
    # 🔴 [返工 2026-08-18 · Codex P0-10] 从 `created_by`(操作者)改成
    #    **租户**作用域:组织成员与 owner 看的是同一个租户的作品库。
    #    admin 仍不加作用域(平台权限)。
    _org = getattr(request.state, "organization_identity", None)
    _tenant = (int(_org.principal_user_id) if _org is not None
               else int(user["user_id"]))
    scope = dict(brand_id=brand_id,
                 tenant_owner_user_id=None if user.get("is_admin") else _tenant,
                 status=status, limit=limit, offset=offset)
    page = {}
    if publication_bucket:
        try:
            page = await asyncio.to_thread(ddb.list_publication_posts, **scope, bucket=publication_bucket)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        rows = page["posts"]
    else:
        rows = await asyncio.to_thread(ddb.list_posts, **scope)

    # 失败条目要给**人话原因**,而原因在任务表的 error_msg 上(元指令:工程术语全站翻人话)。
    # 一次查完所有作品的最新任务,不在循环里逐条查。
    from services.geo_douyin.task_progress import describe_task_progress

    tasks = await asyncio.to_thread(
        ddb.get_latest_tasks_for_posts, [int(r["id"]) for r in rows])
    for r in rows:
        t = tasks.get(int(r["id"]))
        # 🔴 进度语义走**和详情页同一个纯函数**。列表页自己算一套百分比,
        #    就会出现"列表说 40%、详情说 60%"这种谁也说不清的分歧。
        prog = describe_task_progress(t, post_status=str(r.get("status") or ""))
        r["progress"] = prog
        # [#151] 发布结果待确认:**展示态**,由现有字段现场派生。
        #   不落库、不进 publish_status 取值域(前端对它无白名单,裸串会上屏)。
        from services.geo_douyin.publish_convergence import pending_confirm_display

        r["publish_pending_confirm"] = pending_confirm_display(r)
        r["failure_reason"] = humanize_failure((t or {}).get("stage"),
                                               (t or {}).get("error_msg")) \
            if prog["state"] in ("failed", "stalled") else ""
        # 老字段保留:已有调用方(详情页 siblingIds 之外的地方)还在读
        r["progress_done"] = (t or {}).get("progress_done")
        r["progress_total"] = (t or {}).get("progress_total")
        # [#184 c1] 卡片数:列表要显示「N 张」,别让前端自己去数两个数组
        #   (`cards` 与 `oss_keys` 长度在部分成功交付时可能不同,数错就是显示错)。
        #   以 `oss_keys` 为准 —— 它是**交付物**那一侧,失败卡在里面是占位空串。
        r["card_count"] = len(r.get("oss_keys") or [])

    # [#195 c1] 发布失败的**原话**(供应商 reject_reason),给发布中心图文列表显示。
    # 🔴 与同一行上的 `failure_reason` **不是一回事**:那个是**制作**任务的 error_msg
    #    (上面几行刚填)。两者混用会让"发布被拒"显示成"生成失败",反过来也一样。
    # 🔴 关联走**新链口径** `items.source_geo_post_id`,不走 posts.publish_order_id:
    #    后者对一篇多单只装得下一个,而且老链才用它。
    # 🔴 只在 publish_status='failed' 时取;其余状态一律空串 ——
    #    不是 None:前端对空串与缺键的处理不同,给一个稳定形状。
    _failed_ids = [int(r["id"]) for r in rows
                   if str(r.get("publish_status") or "") == "failed"]
    _reasons: dict = {}
    if _failed_ids:
        try:
            _reasons = await asyncio.to_thread(ddb.latest_publish_reject_reasons,
                                               _failed_ids)
        except Exception as e:  # noqa: BLE001 - 展示面,拿不到不该让列表 500
            logger.warning("[douyin-api] 发布失败原因读取跳过: %s", e)
    # 🔴 只有**一道**守卫:`_failed_ids` 那一步。原来这里还有一个
    #    `if publish_status == 'failed'`,两道守卫管同一件事 ——
    #    后一道永远不承重,注毒实测把它去掉判据全绿(冗余守卫)。
    #    留着它会让人以为这里有把关,而真正承重的在上面那个筛选。
    for r in rows:
        r["publish_failure_reason"] = str(_reasons.get(int(r["id"])) or "")

    # [#184 c1] 列表封面签名 URL(私有 bucket,禁裸静态路径 —— 与详情页同一只签名器)。
    # 🔴 一次 gather 签完,不在循环里逐条 await:20 条 × 一次同步签名串行就是 20 个往返。
    # 🔴 签名失败**不让列表 500**:封面是展示面,拿不到就给空串,前端按空处理。
    try:
        from services.geo_douyin.image_pipeline import signed_card_urls

        _covers = await signed_card_urls([str(r.get("cover_oss_key") or "") for r in rows])
        for r, url in zip(rows, _covers):
            r["cover_url"] = url
    except Exception as e:  # noqa: BLE001
        logger.warning("[douyin-api] 列表封面签名失败(不影响列表): %s", e)
        for r in rows:
            r["cover_url"] = ""
    return {**page, "status": "success", "posts": rows}


@router.get("/posts/{post_id}")
async def api_get_post(post_id: int, request: Request):
    import asyncio

    from db import geo_douyin_db as ddb
    from services.geo_douyin.image_pipeline import signed_card_urls

    post = await asyncio.to_thread(ddb.get_post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="作品不存在")
    _require_post_access(request, post)

    # 预览用签名 URL(私有 bucket,禁裸静态路径)
    preview: List[str] = []
    try:
        preview = await signed_card_urls(post.get("oss_keys") or [])
    except Exception as e:  # noqa: BLE001 - 签名失败不该让整个详情 500
        logger.warning("[douyin-api] 预览签名失败 post=%s: %s", post_id, e)

    task = await asyncio.to_thread(ddb.get_task_by_post, post_id)

    # ── 详情页要的额外几块(全部 additive,老调用方不受影响)──
    from services.geo_douyin.card_templates import STYLE_PRESETS, DEFAULT_STYLE_KEY

    siblings = await asyncio.to_thread(ddb.list_city_siblings, post_id)

    style_key = str(post.get("style_key") or DEFAULT_STYLE_KEY)
    preset = STYLE_PRESETS.get(style_key) or STYLE_PRESETS[DEFAULT_STYLE_KEY]

    # 联系方式:走既有唯一入口 db.profile_db.get_contact_info_by_brand,不另起一套。
    # 🔴 传的是服务端从作品表读出来的 brand_id,不是前端传的 —— 防越权读别客户联系方式。
    contact = {"phone": "", "wechat": "", "website": "", "address": "", "brand_name": ""}
    if post.get("brand_id"):
        try:
            from db.profile_db import get_contact_info_by_brand
            contact = await asyncio.to_thread(
                get_contact_info_by_brand, int(post["brand_id"])) or contact
        except Exception as e:  # noqa: BLE001 - 联系方式取不到不该让详情页打不开
            logger.warning("[douyin-api] 联系方式读取失败 post=%s: %s", post_id, e)

    used = int(post.get("redraw_count") or 0)
    # 榜单窄 DTO(P2-2):再次创作弹窗的默认值从这里取,前端不读原始 generation_meta。
    from services.geo_douyin.ranking_router import ranking_summary

    return {
        "status": "success", "post": post, "preview_urls": preview,
        "task": task,
        "siblings": siblings,
        "ranking": ranking_summary(post.get("generation_meta")),
        "style": {"key": preset.key, "label": preset.label},
        "redraw": {"used": used, "limit": REDRAW_LIMIT_PER_POST,
                   "remaining": max(0, REDRAW_LIMIT_PER_POST - used)},
        "contact": {
            # 只回"配置了没有"和已配置的值,不回任何供应商/内部字段
            "configured": bool(contact.get("phone") or contact.get("wechat")),
            "display": " · ".join(
                x for x in (contact.get("brand_name"), contact.get("phone")) if x),
            "enabled": bool(post.get("contact_enabled")),
            "closing_stale": bool(post.get("closing_stale")),
        },
    }


@router.get("/clients/{brand_id}/keywords")
async def api_client_keywords(brand_id: int, request: Request,
                              with_production: bool = False, limit: int = 100, offset: int = 0):
    """这个客户**买了的词**(入口页第 2 步用)。

    🔴 Owner 2026-08-03:「不应该是让客户自己填,而是我们直接帮他按照他
       **购买的词**来进行生成选题,这是我们的事,不是需要客户填」。
       在这之前入口页的「核心词」是一个空输入框 —— 等于把我们已经知道的事
       退回去让代理再敲一遍,而且敲错了还会做出他没买的词。

    🔴 数据源与「写文章」逐字同源:`confirmed_keywords` JOIN `quotes`,
       状态闸 `confirmed/paid`,且 `is_core IS NOT FALSE`(覆盖词不产内容)。
       同一件事不许两个口径 —— 两边显示的词必须一样,不然代理会以为系统算错了。

    🔴 空列表是**正常返回**,不是错误:这个客户可能还没确认报价。
       前端据此显示"去报价里选词"并放开手填,不阻断(元指令 13 永远不中断对话)。
    """
    user = _user(request)
    require_brand_access(request, int(brand_id))

    if with_production:
        import asyncio
        from services.geo_douyin.keyword_worklist import load_keyword_worklist
        org = getattr(request.state, "organization_identity", None)
        tenant = int(org.principal_user_id) if org is not None else int(user["user_id"])
        try:
            page = await asyncio.to_thread(load_keyword_worklist, brand_id=int(brand_id),
                                           tenant_owner_user_id=None if user.get("is_admin") else tenant,
                                           limit=min(200, max(1, limit)), offset=max(0, offset))
        except Exception:
            logger.exception("[douyin-keywords] worklist read failed brand=%s", brand_id)
            raise HTTPException(status_code=503, detail={
                "code": "KEYWORD_LIST_UNAVAILABLE", "message": "关键词和制作进度暂时没读到，请重试。也可以先自己写一个选题。"})
        return {"status": "success", **page}

    from services.geo_douyin.topic_distiller import load_purchased_keywords

    from services.geo_douyin.topic_distiller import (parse_cities,
                                                     resolve_client_cities)

    rows = await load_purchased_keywords(int(brand_id))

    # 🔴 城市来自**客户自己的资料**(brands.cities),报价单只作兜底,
    #    **不给默认列表**。取数与清洗都在 resolve_client_cities 一处。
    quote_cities: List[str] = []
    for r in rows:
        for c in parse_cities(r.get("quote_city") or ""):
            if c not in quote_cities:
                quote_cities.append(c)
    cities = await resolve_client_cities(int(brand_id), quote_cities)
    return {"status": "success", "keywords": rows, "total": len(rows),
            "cities": cities[:8]}


@router.get("/clients/{brand_id}/plan")
async def api_client_plan(brand_id: int, request: Request):
    """这个客户的图文规划:每个买了的词还差几条(入口页第 2 步用)。

    🔴 Owner 2026-08-03:「用户这边确定词以后,应该做出规划,需要多少篇图文」。
       在这之前这一步是**缺的** —— 代理选完客户直接填张数下单,
       "这个词到底要做几条"全靠他自己拍,而这个数报价时就算好了。

    🔴 篇数**复用报价的饱和曲线**(`confirmed_keywords.required_articles`),
       不另造一个公式。再发明一个,就会出现"文章说 8 篇、图文说 5 篇"
       两个口径打架,而代理无从判断信哪个。
    """
    _user(request)
    require_brand_access(request, int(brand_id))

    from services.geo_douyin.content_plan import build_content_plan

    plan = await build_content_plan(int(brand_id))
    return {"status": "success", **plan.to_dict()}


@router.get("/clients/{brand_id}/knowledge")
async def api_client_knowledge(brand_id: int, request: Request):
    """选客户那一步的知识库摘要(入口页第 1 步用)。

    🔴 这是**薄壳**:资料本体、图片、联系方式全部来自客户档案那套既有能力,
       本端点不自己定义"什么算一份资料" —— 那会变成第二套口径,
       和写文章那边显示不一致(SSOT《GEO 内容生产是一条流水线》铁律:
       知识库读取不得两套)。
    🔴 读不到就如实回 0 + 空摘要,**不编数字**。
    """
    _user(request)
    require_brand_access(request, int(brand_id))
    import asyncio

    from services.client_knowledge import build_client_knowledge

    # 🔴 与详情页右栏那张卡走**同一份**计算(SSOT 铁律 L6:知识库读取不得两套)。
    #    差别只在:这里不要缩略图(选客户那一步用不上,签名/取图是白花的往返)。
    # 🔴 with_thumbs=True:这个端点现在同时服务**写文章**那张卡(共用组件
    #    `<ClientKnowledgeCard/>`),那边要显示图片缩略图。
    #    端点挂在 `/api/geo-douyin/` 前缀下纯属历史原因(RBAC 映射已覆盖该前缀,
    #    实测过:未映射前缀会被中间件挡成 403 UNMAPPED_ROUTE)。它服务两条链,
    #    不是抖音专属 —— 名字改前缀要连 RBAC 一起改,那是另一单。
    kb = await asyncio.to_thread(build_client_knowledge, int(brand_id),
                                 with_thumbs=True)
    m = kb.get("materials") or {}
    return {
        "status": "success",
        "brand_id": brand_id,
        **kb,
        # 扁平几个字段给老调用方直接用;口径仍来自上面那一份,不另算
        "filled": int(m.get("filled") or 0),
        "total": int(m.get("total") or 0),
        "images_count": int((kb.get("images") or {}).get("count") or 0),
        "summary": (kb.get("summary")
                    or ("资料暂时读不出来，稍后再看" if kb.get("load_failed") else "")),
    }


@router.get("/posts/{post_id}/consistency")
async def api_post_consistency(post_id: int, request: Request):
    """卡面文字与客户资料库的轻量核对(详情页黄点的**唯一**数据源)。

    🔴 单独一条路由而不是塞进详情:核对要查客户知识库(RAG),比详情慢得多。
       塞进去会让整个详情页等它 —— 黄点是锦上添花,不该拖住主内容。
    🔴 `checked=false` 时前端**整体隐藏**黄点(工单:禁止摆假数据)。
    🔴 同时返回规范 §8.6 第 10 条的「卡面 vs 文案」对齐核验(`alignment`)——
       两者核的是**不同的轴**:上面那份核卡面对不对得上**客户资料**,
       alignment 核卡面对不对得上**这条帖子自己的文案**。
       合成一个字段会让"资料里没这个数"和"图上文案自相矛盾"混成一句话,
       而这两件事该做的处理完全不同。
    """
    import asyncio

    from db import geo_douyin_db as ddb
    from services.geo_douyin.kb_consistency import (
        build_report_for_post, check_headline_caption_alignment)

    post = await asyncio.to_thread(ddb.get_post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="作品不存在")
    _require_post_access(request, post)

    report = await build_report_for_post(post)

    meta = post.get("generation_meta") or {}
    snap = meta.get("content") if isinstance(meta.get("content"), dict) else {}
    alignment = check_headline_caption_alignment(
        body=str(snap.get("body") or post.get("body") or ""),
        cover=snap.get("cover") or {},
        cards=list(snap.get("cards") or []),
        closing=snap.get("closing") or {})
    return {"status": "success", **report.to_dict(),
            "alignment": alignment.to_dict()}


# 每条内容多久能核一次卡面文字。核一次 = 每张卡一次视觉调用(不收费),
# 所以要拦住"点着玩"。与蒸馏那个节流同款:进程内、不落库。
_OCR_LAST_AT: dict = {}
_OCR_MIN_GAP_S = 30.0


@router.post("/posts/{post_id}/ocr-check")
async def api_post_ocr_check(post_id: int, request: Request):
    """卡面文字 OCR 逐字核验(规范 §8.6 第 11 条)。**不收费**。

    🔴 为什么不收费:它核的是**我们有没有把客户给的字印对**,是我方 QA,
       不是给用户的交付物。让用户付钱来检查我们排版对不对说不通。
    🔴 为什么不在生产时自动跑:一组 4-9 张,每张一次视觉调用。挂在每次生产上
       等于给每一单加一笔**没进价目表**的成本。所以做成按需 + 节流。
    🔴 提示级:结果只用来告诉人"该看哪一张",不参与任何阻断/改写。
    """
    import asyncio
    import time

    from db import geo_douyin_db as ddb
    from services.geo_douyin.ocr_qa import run_post_ocr_qa

    post = await asyncio.to_thread(ddb.get_post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="作品不存在")
    _require_post_access(request, post)

    now = time.monotonic()
    last = _OCR_LAST_AT.get(post_id)
    if last is not None and now - last < _OCR_MIN_GAP_S:
        raise HTTPException(status_code=429, detail={
            "code": "TOO_FREQUENT", "message": "刚核过一次，等一会儿再来"})
    _OCR_LAST_AT[post_id] = now

    report = await run_post_ocr_qa(post)
    return {"status": "success", **report.to_dict()}


@router.get("/posts/{post_id}/knowledge")
async def api_post_knowledge(post_id: int, request: Request):
    """右栏「知识库 / 写作资料」卡的明细:资料填充度 / 已授权图片 / 这条用了哪些来源。

    🔴 单独一条路由(与 /consistency 同样考虑):它要打两次 client 表 + 一次图片表,
       塞进详情会让主内容等它。右栏是次要信息,不该拖住左中两栏。
    🔴 「用了哪些来源」取的是 `generation_meta.kb_sources` —— 生产时**真实留痕**的那份,
       不是"这个客户有什么资料"。两者不是一回事:有资料 ≠ 这条内容用上了。
    """
    import asyncio

    from db import geo_douyin_db as ddb
    from services.client_knowledge import build_client_knowledge
    from services.geo_douyin.knowledge_context import describe_sources

    post = await asyncio.to_thread(ddb.get_post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="作品不存在")
    _require_post_access(request, post)

    # 🔴 「这个客户有什么」走**共享的那一份**(build_client_knowledge),
    #    与入口页客户卡同一个计算 —— 两处各算一遍就是同一事实两套口径,
    #    改一处另一处不跟着变,两边显示早晚打架(SSOT 铁律 L6)。
    kb = await asyncio.to_thread(build_client_knowledge, post.get("brand_id"),
                                 with_thumbs=True)
    return {
        "status": "success",
        **kb,
        # 🔴 这一项是**本条内容特有**的,不属于共享部分:
        #    它读的是生产时真实留痕的 kb_sources,答的是"这条用了什么",
        #    不是"这个客户有什么"。
        "sources_used": describe_sources(post.get("generation_meta")),
    }


@router.patch("/posts/{post_id}")
async def api_update_post_text(post_id: int, req: UpdatePostTextRequest,
                               request: Request):
    """文案编辑区「保存修改」。只改文字,不重新生成、不扣费、不动图。"""
    import asyncio

    from db import geo_douyin_db as ddb

    post = await asyncio.to_thread(ddb.get_post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="作品不存在")
    _require_post_access(request, post)
    if post.get("status") == "published":
        raise HTTPException(status_code=400, detail="已发布的内容不能再改，请新建一条")

    title = req.title
    if title is not None:
        # 45 = 发布平台标题硬上限(SSOT §7)。这里就地截断而不是报错 ——
        # 用户在编辑区多打了几个字不该被一个红框拦住。
        title = title.strip()[:45]

    from services.geo_douyin.post_revisions import RevisionConflict
    try:
        revision_id = await asyncio.to_thread(
            ddb.update_post_text, post_id, created_by=int(_user(request)["user_id"]),
            title=title, body_text=req.body_text,
            hashtags=[str(h).lstrip("#").strip() for h in req.hashtags if str(h).strip()]
            if req.hashtags is not None else None,
            expected_revision_id=req.expected_revision_id,
            check_revision="expected_revision_id" in req.model_fields_set,
        )
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "POST_REVISION_CONFLICT",
            "message": str(exc), "next_action": "reload_latest_and_compare"}) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "success", "active_revision_id": revision_id}


@router.post("/posts/{post_id}/contact")
async def api_toggle_contact(post_id: int, req: ContactToggleRequest,
                             request: Request):
    """「插入联系方式」开关。

    🔴 拨开关**不会**改变那张已经渲染好的收尾卡 —— 后端同时把 closing_stale 置位,
       前端据此提示"要重抽一次收尾卡才生效"。这是诚实做法;
       只改个 boolean 就宣称"已插入"是假联动。
    """
    import asyncio

    from db import geo_douyin_db as ddb

    post = await asyncio.to_thread(ddb.get_post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="作品不存在")
    _require_post_access(request, post)

    contact_line = ""
    if req.enabled and post.get("brand_id"):
        try:
            from db.profile_db import get_contact_info_by_brand
            info = await asyncio.to_thread(
                get_contact_info_by_brand, int(post["brand_id"])) or {}
            contact_line = " ".join(
                x for x in (info.get("brand_name"), info.get("phone")) if x).strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("[douyin-api] 联系方式读取失败 post=%s: %s", post_id, e)
    if req.enabled and not contact_line:
        raise HTTPException(status_code=400,
                            detail="这个客户还没填联系方式，先去客户资料里补上")

    # 把要排进画面的那行联系方式存进 generation_meta,重抽收尾卡时直接用
    await asyncio.to_thread(ddb.update_post_content, post_id,
                            generation_meta={**(post.get("generation_meta") or {}),
                                             "contact_line": contact_line})
    await asyncio.to_thread(ddb.set_contact_enabled, post_id, bool(req.enabled))

    fresh = await asyncio.to_thread(ddb.get_post, post_id)
    return {
        "status": "success",
        "enabled": bool((fresh or {}).get("contact_enabled")),
        "closing_stale": bool((fresh or {}).get("closing_stale")),
        "message": ("联系方式会排在收尾卡上，重抽一次收尾卡就生效"
                    if req.enabled else "收尾卡重抽一次后就不带联系方式了"),
    }


@router.post("/posts/{post_id}/cards/{card_index}/redraw")
async def api_redraw_card(post_id: int, card_index: int,
                          req: RedrawCardRequest, request: Request):
    """重抽【一张】卡。**收费**(Owner 2026-08-03 拍板 100 算力),且每条内容仍有累计限额。

    🔴 2026-08-03 由免费改为收费。这是本包**唯一新增的资金路径**,所以形态必须与
       既有 B 类长任务完全一致:freeze → 成功 commit / 失败 release,
       "完成才扣"一个字不破。
    🔴 限额**保留**:收费不等于可以无限刷。收费解决的是成本转嫁,
       限额解决的是单条内容被反复重画到失控。两件事,都要。
    🔴 计费只在本端点做,`services/geo_douyin/redraw.py` 仍是零计费模块
       (AST 锁 `test_redraw_is_free_no_billing_import` 照旧成立)——
       资金动作集中在一处才审得动。
    🔴 只替换这一张,其他卡一个字节都不动(落库走 jsonb_set 定点替换)。
    """
    import asyncio

    from db import geo_douyin_db as ddb
    from services.geo_douyin.redraw import REDRAW_LIMIT_PER_POST, dispatch_redraw

    post = await asyncio.to_thread(ddb.get_post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="作品不存在")
    _require_post_access(request, post)
    if post.get("status") == "published":
        raise HTTPException(status_code=400, detail="已发布的内容不能再改，请新建一条")
    # 🔴 [#184 d1c] 整篇重做在跑时**不许**重抽单张。
    #    d1 之后建重抽任务会走 `create_task_with_generation` ⇒ 它会把在跑的那一代
    #    接管掉;那一代按 d1 的输家口径作废并退款 —— 用户点一张卡的重做,
    #    结果是**整篇正在做的内容被作废**,而他完全不知道。
    #    在这里同步拦一道:当场判得出来的事不该丢到后台去产生副作用。
    #    (只拦 generating;completing/failed 等仍按原行为走。)
    if str(post.get("status") or "") == "generating":
        raise HTTPException(status_code=409, detail={
            "code": "POST_IS_GENERATING",
            "message": "这条内容正在制作中，等它做完再重做某一张",
        })

    # 🔴 额度用满是**当场就能判**的,不该丢到后台再让用户等着轮询才知道 ——
    #    先在这里同步拦一道,前端立刻拿到 429。真正的生图才进后台。
    used_now = int(post.get("redraw_count") or 0)
    if used_now >= REDRAW_LIMIT_PER_POST:
        raise HTTPException(status_code=429, detail={
            "code": "REDRAW_LIMIT",
            "message": f"这条内容的重做已经用满 {REDRAW_LIMIT_PER_POST} 次了，"
                       f"想继续调整可以整条重新创作",
            "redraw_used": used_now, "redraw_limit": REDRAW_LIMIT_PER_POST,
            "limit_reached": True})

    # ── 冻结这次重抽的算力 ──
    # 🔴 顺序:先判限额(上面)再冻结。反过来会在"额度已满"时先冻一笔再退,
    #    用户余额上会闪一下,而且多一次可能失败的资金动作。
    from middleware.billing import commit_freeze, freeze_points, release_freeze

    user_id = int(_user(request)["user_id"])
    redraw_ref = f"douyin_redraw:{post_id}:{card_index}:{used_now}"
    try:
        frozen = await freeze_points(
            user_id, FEATURE_CODE_IMAGE_POST_REDRAW,
            task_ref=redraw_ref, brand_id=post.get("brand_id"),
            reason="抖音图文单张重抽",
        )
        # 🔴 [#79] 记下这次到底扣没扣 —— 错误响应的信封从这里取。
        #    没写过 ⇒ 处理器兜底 `no`(还没走到扣费那步),那是安全的默认。
        try:
            from middleware.billing import charged_envelope as _chg_env
            request.state.charged_envelope = _chg_env(frozen)
        except Exception:
            pass        # 信封是附加信息,绝不因它影响主流程
    except Exception as e:  # noqa: BLE001 - 余额不足是最常见的一种,给人话
        logger.warning("[douyin-api] 重抽冻结失败 post=%s: %s", post_id, e)
        raise HTTPException(status_code=402, detail={
            "code": "INSUFFICIENT_POINTS",
            "message": "算力不够，充值后再重抽这一张"}) from e
    redraw_freeze_id = frozen.get("freeze_id")

    # 单张实测 49-73s > nginx 60s → 必须异步,否则会出现"前端报失败、
    # 图其实出了、额度也扣了"这种最难解释的状态
    # 🔴 §15-3「补齐全组才 commit」:用户手动补齐最后一张时要把原来那笔生产
    #    冻结结算掉。结算逻辑放在 settlement,**不放进 redraw 模块** ——
    #    重抽模块一行计费代码都不许有(AST 锁着),所以方向是 API → settlement。
    from services.geo_douyin.settlement import try_settle

    async def _settle() -> None:
        # ① 这次重抽自己的那笔冻结 → 提交(成功才扣)
        if redraw_freeze_id:
            await commit_freeze(freeze_id=redraw_freeze_id, task_ref=redraw_ref,
                                reason="抖音图文单张重抽完成")
        # ② §15:如果这一张正好补齐了整组,把**原来那笔生产冻结**一并结算
        await try_settle(post_id)

    async def _refund() -> None:
        if redraw_freeze_id:
            await release_freeze(freeze_id=redraw_freeze_id, task_ref=redraw_ref,
                                 reason="抖音图文单张重抽失败")

    task_id, _ = await dispatch_redraw(post, int(card_index), hint=req.hint,
                                       user_id=user_id,
                                       freeze_id=redraw_freeze_id,
                                       on_settled=_settle, on_failed=_refund)
    return {"status": "accepted", "post_id": post_id, "task_id": task_id,
            "card_index": int(card_index),
            "message": "正在重画这一张，好了会自动替换"}


@router.post("/posts/{post_id}/regenerate")
async def api_regenerate_post(post_id: int, req: RegenerateRequest, request: Request):
    """重新生成这条内容(换一版文案+卡片图)。

    🔴 走**便宜一档**的价目 `geo_douyin_image_post_regen`(260),
       与首次制作 `geo_douyin_image_post`(390)分开 ——
       结构对齐写文章那条链的 article_gen(390) / article_rewrite(260)。
    🔴 计费仍是 B 类冻结:freeze → commit/release,失败自动退,与首次制作同一套。
    """
    user = _user(request)
    import asyncio

    from db import geo_douyin_db as ddb
    from services.geo_douyin.production_task import dispatch_production

    post = await asyncio.to_thread(ddb.get_post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="作品不存在")
    _require_post_access(request, post)
    if post.get("status") == "published":
        raise HTTPException(status_code=400, detail="已发布的内容不能重做，请新建一条")

    # ── 合同链身份闸(P0-09)。只对**合同链作品**生效:老链作品
    #    `production_batch_id` 为 NULL,一个字都不变。 ────────────────────
    if post.get("production_batch_id"):
        if not req.request_id:
            raise HTTPException(status_code=400, detail={
                "code": "IDEMPOTENCY_KEY_REQUIRED",
                "message": "这次重做缺少请求标识",
                "reason": "重做是付费动作,必须能识别重复提交",
                "impact": "本次未重做、未冻结算力",
                "repair_hint": "刷新页面后重试"})
        _active_ref = str(post.get("topic_ref") or "")
        if _active_ref and str(req.expected_topic_ref or "") != _active_ref:
            # 🔴 不回显 `_active_ref`:它是服务端签发的身份,回显等于告诉
            #    客户端"正确答案是什么",校验就变成了摆设。
            raise HTTPException(status_code=409, detail={
                "code": "TOPIC_REF_STALE",
                "message": "这篇的选题已经更新过了,请刷新后再重做",
                "reason": "提交的选题引用与当前生效版本不一致",
                "impact": "本次未重做、未冻结算力、未联系任何外部服务",
                "repair_hint": "刷新作品详情后重新点「再次创作」",
                "actions": [{"id": "refresh", "label": "刷新", "type": "retry"}],
                "next_action": "refresh", "retryable": True})

    # 🔴 override 白名单**集合校验**(规格 §5.6 · 03 §12 #6)。
    #    位置在 `set_post_status('generating')` 与 `dispatch_production` 之前 ——
    #    零副作用是判据的一半:被拒的那一次不许改状态、不许冻结、不许排队。
    # 🔴 闸对**两条链都生效**,不只合同链:白名单说的是"重做能改什么",
    #    与这篇归哪条链无关。老调用方从不发这个字段(它是本轮新增的),
    #    所以对存量行为逐字为零影响 —— 而只在合同链上校验会留下
    #    "换一条链就能绕过"的口子。
    if req.settings_override is not None:
        unknown = sorted(set(req.settings_override) - REGENERATE_OVERRIDE_ALLOWLIST)
        if unknown:
            raise HTTPException(status_code=400, detail={
                "code": "OVERRIDE_NOT_ALLOWED",
                "message": "这次重做里有不能修改的设置",
                "reason": "不在允许覆盖范围内的设置:" + "、".join(unknown),
                "impact": "本次未重做、未冻结算力、未联系任何外部服务",
                "repair_hint": "只调整卡数 / 画幅 / 形态 / 风格 / 榜单母版 /"
                               " 上榜家数 / 联系方式后重试",
                "actions": [{"id": "back", "label": "返回修改", "type": "nav"}],
                "next_action": "back", "retryable": True})

    # 重做与首次同规则:名字为空这一单必然判废,当场说清楚而不是花完钱再退。
    brand_name = await fetch_brand_display_name(post.get("brand_id"))
    if post.get("brand_id") and not brand_name:
        raise HTTPException(status_code=400, detail={
            "code": "BRAND_NAME_MISSING",
            "message": "这个客户还没有名称，先去客户档案里补一个再做内容"})

    override = dict(req.settings_override or {})
    card_count = len(post.get("oss_keys") or []) or CARD_COUNT_DEFAULT
    if override.get("card_count"):
        # 🔴 校验完必须**真的生效**:只验不用 = 又一处"接了但没接线"
        #    (这一整轮返工的病根)。判据因此打的是 dispatch 收到的值。
        card_count = int(override["card_count"])

    # ── 榜单合同继承(P1-3)────────────────────────────────────────────
    # 🔴 榜单参数不在 posts 的列上,它在 `generation_meta.ranking.request` 里。
    #    取不到 = 这条本来就是卡组型 → 三个参数全走默认,行为逐字不变。
    from services.geo_douyin.ranking_router import RankingRequest

    inherited = RankingRequest.from_meta(post.get("generation_meta") or {})
    if inherited is None:
        rk_form, rk_count, rk_tpl, rk_force = "", 0, "", False
    else:
        rk_form = inherited.content_form
        # 显式改过就用改的,没改(None)就继承 —— 三态在这里落地。
        rk_count = (int(req.ranking_entity_count)
                    if req.ranking_entity_count is not None else inherited.entity_count)
        rk_tpl = (str(req.ranking_template)
                  if req.ranking_template is not None else inherited.template)
        # 🔴 显式选了具体母版 = 明示要榜单式;显式选「自动」时**沿用原来的坚持与否**,
        #    不因为切了一次版式就把用户原本的覆盖意图抹掉。
        rk_force = bool(rk_tpl) if req.ranking_template is not None else inherited.force_ranking

    await asyncio.to_thread(ddb.set_post_status, post_id, "generating")
    # 与首次制作同因异步:整组重做同样是 ~137s,同步必然被 nginx 60s 掐断
    dispatch_production(
        post_id=post_id, user_id=int(user["user_id"]),
        keyword=post.get("keyword") or "",
        brand_id=post.get("brand_id"), brand_name=brand_name,
        city=post.get("city") or "",
        card_count=card_count,
        variant_index=int(req.variant_index or 0),
        feature_code=FEATURE_CODE_IMAGE_POST_REGEN,   # ← 便宜一档
        extra_hint=req.extra_hint,
        # 空 = 沿用这条已有的风格,不悄悄退回默认款
        style_key=(str(override.get("style_key") or "") or req.style_key
                   or str(post.get("style_key") or "")),
        contact_line=(str((post.get("generation_meta") or {}).get("contact_line") or "")
                      if override.get("contact_enabled",
                                      post.get("contact_enabled")) else ""),
        # 🔴 整条重做**沿用这条原来的画幅**,不给用户"重做一次画幅自己变了"的意外。
        #    与 style_key 同样的取法。
        aspect_ratio=str(override.get("aspect_ratio")
                         or post.get("aspect_ratio") or ""),
        industry_key=str(post.get("industry_key") or ""),
        # 🔴 2026-08-08 包 B:行业冻结件**跟着一起继承**,与 style_key / aspect_ratio 同理。
        #    不继承的话,重做会现场重新归并 —— 期间别人改过行业名 / 沉淀过新别名,
        #    这条就会换一个候选池,用户看到的是"同一条重做了一次,榜单里的同行全变了"。
        #    取不到(老作品 / 当初没走榜单)→ 传 None,`build_ranking_plan` 现场归并兜底。
        frozen_industry=((post.get("generation_meta") or {}).get("ranking") or {}).get("industry"),
        # 🔴 P1-3:榜单形态与三个旋钮一起继承。少传任何一个,这条付费重做
        #    都会安静地退化成普通图文 —— 用户花了 260 拿到一个不是他那条的东西。
        content_form=str(override.get("content_form") or rk_form),
        ranking_entity_count=int(override.get("ranking_entity_count") or rk_count or 0),
        ranking_template=str(override.get("ranking_template") or rk_tpl or ""),
        ranking_force=rk_force,
    )
    return {"status": "accepted", "post_id": post_id,
            "cards_total": card_count,
            "content_form": rk_form,
            "message": "开始重新创作了，做好会自动刷新"}


@router.delete("/posts/{post_id}")
async def api_delete_post(post_id: int, request: Request):
    """软删除(工单 §5:作品库留全量,不物理删)。"""
    import asyncio

    from db import geo_douyin_db as ddb
    user = _user(request)
    post = await asyncio.to_thread(ddb.get_post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="作品不存在")
    _require_post_access(request, post)
    ok = await asyncio.to_thread(ddb.soft_delete_post, post_id, int(user["user_id"]))
    if not ok:
        raise HTTPException(status_code=403, detail="只能删除自己创建的作品")
    return {"status": "success"}


# ─────────────────────────────────────────────────────────────
# 选号 / 频控 / 备图(发布前三步,发布本体走既有接口)
# ─────────────────────────────────────────────────────────────
@router.get("/accounts")
async def api_list_accounts(request: Request, industry: str = "",
                            location: str = "", q: str = "",
                            limit: int = 50, offset: int = 0):
    """可发图文的抖音账号。对外只叫"可投放账号",不提供应商。

    q = 账号名模糊搜索(详情页中栏那个搜索框)。**在 SQL 里过滤**,不是把 50 条
    拉回来前端 filter —— 前端过滤只能搜到"这一页里的",用户搜不到就以为没有。
    """
    _user(request)
    import asyncio

    from services.geo_douyin.publish_adapter import list_douyin_image_accounts
    rows = await asyncio.to_thread(
        list_douyin_image_accounts, industry=industry, location=location,
        keyword=q, limit=limit, offset=offset,
    )
    return {"status": "success", "accounts": rows}


@router.post("/frequency-check")
async def api_frequency_check(req: FrequencyCheckRequest, request: Request):
    """发布前频控预检(不扣费,不下单)。"""
    _user(request)
    import asyncio

    from db import geo_douyin_db as ddb
    from services.geo_douyin.publish_adapter import check_frequency, load_today_counts

    today_count = 0
    if req.brand_id:
        require_brand_access(request, int(req.brand_id))
        today_count = await asyncio.to_thread(
            ddb.count_today_posts_for_brand, int(req.brand_id))

    # 🔴 单账号日发上限必须喂**真值**。不传这个参数时 check_frequency 里那条规则
    #    恒为"没有账号超限" —— 校验在,判据不在,等于没校验。
    per_account = await asyncio.to_thread(load_today_counts, req.media_ids)

    verdict = check_frequency(media_ids=req.media_ids, cities=req.cities,
                              per_account_today=per_account)
    return {"status": "success", "brand_today_count": today_count,
            "per_account_today": per_account,
            "per_account_daily_limit": douyin_per_account_daily_limit(),
            **verdict.to_dict()}


#: [第 4 棒 · P0-7] 客户端自报的发布结果落进 `publish_status` 时用的**线索值**。
#: 🔴 刻意**不是** `published` / `publishing`:那两个是终态事实,只有 managed 链
#:    (worker + 渠道回执)才有资格写。这个值的全部含义是「有人说发了,还没核实」。
SELF_REPORTED_UNVERIFIED = "self_reported_unverified"


@router.post("/publish-result")
async def api_bind_publish_result(req: PublishResultRequest, request: Request):
    """浏览器/客户端**自报**的发布结果 —— 只当**线索**收,不当终态事实。

    🔴 [第 4 棒 · P0-7 · Owner 2026-08-18 裁决] 原文:「浏览器自报
       (`/publish-result` 与 extension `PUBLISH_PROGRESS`)**降级为线索**:
       可记录、可展示为「待核实」,**不得**直接写 publish_records success /
       不作为发布终态事实;终态只能由 managed 链(worker + provider 回执)落。」

       这个接口此前收的是**客户端传来的** `publish_status` / `published_url`,
       并据此把作品直接置 `published` + 落 `published_at` + 落归因锚。
       也就是说:任何拿得到自己作品 id 的调用方,都能宣布"这篇已发布" ——
       而 `published_url` 是飞轮反查引用归因的锚,假锚会污染整条归因链。

    🔴 改法(降级三条):
       ① **不再**写 `published_url` / `published_at` / 作品 `published|publishing` 态;
       ② 只把 `publish_status` 落成**非终态的线索值** `self_reported_unverified`,
          界面据此显示「待核实」——「记录下来」和「认账」是两件事;
       ③ 自报的原始内容(声称的 URL / 订单号)进 `ai_ops_alerts` 的 payload,
          **可查询、可人工核实**,但不进任何被当作事实读的列。

    🔴 本接口仍**不下单、不扣费、不退款**。
    """
    import asyncio

    from db import geo_douyin_db as ddb

    post = await asyncio.to_thread(ddb.get_post, req.post_id)
    if not post:
        raise HTTPException(status_code=404, detail="作品不存在")
    _require_post_access(request, post)

    # 🔴 [第 5 棒 · Codex R5 P0-4a] 上一版这里的注释与代码**相反**:注释写
    #    「order_id / item_ids 是服务端产物」,而传进去的 `req.order_id` /
    #    `req.item_ids` **恰恰是客户端自报的**。于是"降级为线索"只降了状态那一半,
    #    权威关联(作品 ↔ 投放订单/订单项)照样被自报的 ID 写进去 ——
    #    拿别人的 order_id 就能把自己的作品挂到别人的投放单上。
    #
    #    改法:自报**一个 ID 都不写**进 canonical linkage。
    #    自报的全部内容(声称的 URL / 订单号 / 状态)只进独立的待核实记录。
    #
    # 🔴 而且**不许降级既有发布态**:managed 链若已经写过 `publishing` /
    #    `published`,一条自报不能把它盖成"待核实"。`bind_publish_result` 用的是
    #    `COALESCE(NULLIF(...))`,给什么就盖什么 —— 所以判空要在**调用之前**做。
    _existing = str(post.get("publish_status") or "").strip()
    _may_record_lead = _existing in ("", "none", SELF_REPORTED_UNVERIFIED)
    if _may_record_lead:
        await asyncio.to_thread(
            ddb.bind_publish_result, req.post_id,
            order_id=None, item_ids=None,
            publish_status=SELF_REPORTED_UNVERIFIED, published_url="",
        )
    from services.geo_douyin.contract_worker import raise_wiring_alert

    await asyncio.to_thread(
        raise_wiring_alert,
        rule_key="geo_imgnote_self_reported_publish",
        fingerprint="post:%s" % int(req.post_id),
        severity="info",
        title="图文发布:收到客户端自报结果(待核实)",
        detail=("作品 %s 收到一条自报的发布结果。已按线索记录,**未**写入发布终态 —— "
                "终态只能由 managed 链(worker + 渠道回执)落。" % req.post_id),
        payload={"post_id": int(req.post_id),
                 "claimed_publish_status": str(req.publish_status or ""),
                 "claimed_published_url": str(req.published_url or ""),
                 # 🔴 自报的 ID 只留在**告警 payload** 里供人工核实,
                 #    绝不进 `geo_douyin_posts` 的 publish_order_id / publish_item_ids。
                 "claimed_order_id": req.order_id,
                 "claimed_item_ids": list(req.item_ids or []),
                 "existing_publish_status": _existing,
                 "recorded_as_lead": _may_record_lead,
                 "source": "client_self_report"},
    )
    return {"status": "recorded", "verified": False,
            "publish_status": (SELF_REPORTED_UNVERIFIED if _may_record_lead
                               else _existing),
            "message": ("已记录为待核实线索；发布是否成功以发布渠道回执为准"
                        if _may_record_lead else
                        "这条已有正式发布记录，自报只作线索留存，不改变发布状态")}


@router.post("/prepare-publish-media")
async def api_prepare_publish_media(req: PrepareMediaRequest, request: Request):
    """把作品的存档图转成发布可用地址。

    返回的 image_urls 直接喂给既有 `/api/meijiehezi/short-video/publish`
    (article_type=3)。**本接口不下单、不扣费。**
    """
    import asyncio

    from db import geo_douyin_db as ddb
    from services.geo_douyin.publish_adapter import prepare_publish_images
    from services.geo_douyin.settlement import STATUS_COMPLETING, card_is_ok

    post = await asyncio.to_thread(ddb.get_post, req.post_id)
    if not post:
        raise HTTPException(status_code=404, detail="作品不存在")
    _require_post_access(request, post)
    # 🔴 §15 之后作品多了一个 `completing`(补齐中)态:已达成品门槛、还差几张在补。
    #    它**绝不能发布** —— 发出去就是缺图的残组,而且那笔冻结还没 commit。
    #    这条闸在 §15 的 EXIT 里显式挂过账,落在这里(发布链侧),
    #    不是留给前端 checklist 兜 —— 前端能绕过,后端不能。
    if post.get("status") == STATUS_COMPLETING:
        pending = len([c for c in (post.get("cards") or []) if not card_is_ok(c)])
        raise HTTPException(status_code=400, detail={
            "code": "POST_COMPLETING",
            "message": f"这条还有 {pending} 张没出来，补齐了再发；"
                       f"进详情页把缺的那几张重做一下",
            "pending_cards": pending})
    if post.get("status") != "ready":
        raise HTTPException(status_code=400, detail="这条内容还没做好，做好了才能发")

    prepared = await prepare_publish_images(post.get("oss_keys") or [])
    if not prepared.ok:
        raise HTTPException(status_code=502, detail=prepared.error or "图片准备失败")

    # 所属客户:发布中心的「客户」栏预填用。取不到不阻断发布(只是少填一格)。
    customer_name = await fetch_brand_display_name(post.get("brand_id"))

    return {
        "status": "success",
        "image_urls": prepared.image_urls,
        "cover_image": prepared.cover_image,
        "article_type": 3,
        "title": post.get("title") or "",
        "content": post.get("body_text") or "",
        "hashtags": post.get("hashtags") or [],
        "customer_name": customer_name,
        "brand_id": post.get("brand_id"),
    }


# ─────────────────────────────────────────────────────────────
# 后备:导出 zip(走鉴权路由,禁裸静态路径 —— 工单 §4.7)
# ─────────────────────────────────────────────────────────────
@router.get("/posts/{post_id}/export")
async def api_export_pack(post_id: int, request: Request):
    """导出成品包:图集 + 文案.txt + 标题.txt + hashtag.txt。

    🔴 走本鉴权路由流式返回,**不落磁盘、不暴露任何静态路径**
       (既符合"本地零滞留",也不给出可被猜到的公开 URL)。
    """
    import asyncio

    from db import geo_douyin_db as ddb
    from fastapi.responses import StreamingResponse
    from services import oss_service

    post = await asyncio.to_thread(ddb.get_post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="作品不存在")
    _require_post_access(request, post)

    oss_keys = post.get("oss_keys") or []

    def _read(key: str) -> bytes:
        bucket, _ = oss_service._get_bucket()  # noqa: SLF001
        return bucket.get_object(key).read()

    images: List[tuple] = []
    for i, key in enumerate(oss_keys):
        try:
            images.append((f"图片/{i+1:02d}.png", await asyncio.to_thread(_read, key)))
        except Exception as e:  # noqa: BLE001 - 单图失败不该让整包导不出
            logger.warning("[douyin-api] 导出读图失败 post=%s key=%s: %s",
                           post_id, key, e)

    tags = post.get("hashtags") or []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in images:
            zf.writestr(name, data)
        zf.writestr("文案.txt", post.get("body_text") or "")
        zf.writestr("标题.txt", post.get("title") or "")
        zf.writestr("话题标签.txt",
                    "\n".join(f"#{str(t).lstrip('#')}" for t in tags))
    buf.seek(0)

    filename = f"geo_douyin_post_{post_id}.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
