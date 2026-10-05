"""GEO 抖音图文管线 v1 · 卡片图生产(生图 API → OSS)

🔴 硬约束(工单 §3.3):**产物即传 OSS,本地磁盘零滞留**(生产盘已用 71%)。
   实现方式 = 全程 bytes 在内存里走完:
       generate_image(外部 API) → download_image(bytes) → put_object(bytes)
   **本模块任何地方都不得** open(..., 'w') / Path.write_bytes / tempfile 落盘。
   反向锁见 tests/test_geo_douyin_image_pipeline.py::test_no_local_disk_write。

🔴 真异步(工单 §3.2):外部 HTTP 走 httpx async;**oss2 SDK 是同步的**,
   必须 `asyncio.to_thread` 包起来,否则 put_object 会阻塞事件循环
   (事件循环工单同族红线,违反=打回)。

🔴 单张失败可单张重试,整任务失败由调用方 release_freeze 退积分(工单 §3.3)。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from services.geo_douyin.config import image_gen_concurrency
# [WO-ACCEPTANCE-3FIX-2026-08-05 项2] 地名/后缀去重的唯一实现,见 services/geo_title_hygiene.py
from services.geo_title_hygiene import join_city_keyword

logger = logging.getLogger("GEO-Douyin-Image")

# 抖音图文竖版。默认 3:4(与被采纳样本形态一致);规范 §8.8 起 9:16 可选。
# 🔴 原来这里是写死的 `_IMAGE_SIZE = "3:4"`,规范说的"客户可自选"那一层不存在。
#    白名单与规格文案的 SSOT 在 config.ASPECT_RATIOS —— 本模块不再自己拿字符串。
_IMAGE_RESOLUTION = "1k"


def image_size(aspect_ratio: str = "") -> str:
    """给 provider 的画幅参数。不认识的值落到默认(白名单在 config,不在这里)。"""
    from services.geo_douyin.config import ASPECT_RATIOS, normalize_aspect_ratio

    return ASPECT_RATIOS[normalize_aspect_ratio(aspect_ratio)]["size"]


def build_card_oss_key(post_id: int, idx: int, ext: str = "png") -> str:
    """卡片图 OSS key。规范: geo_douyin/{post_id}/card_{idx}_{ts}_{rand}.{ext}"""
    normalized = (ext or "png").lower().lstrip(".")
    if normalized == "jpeg":
        normalized = "jpg"
    if normalized not in ("png", "jpg", "webp"):
        raise ValueError(f"不支持的扩展名: {ext}")
    return (f"geo_douyin/{int(post_id)}/card_{int(idx)}_"
            f"{int(time.time())}_{secrets.token_hex(4)}.{normalized}")


@dataclass
class CardImageResult:
    idx: int
    ok: bool = False
    oss_key: str = ""
    error: str = ""
    cost_usd: float = 0.0
    prompt: str = ""

    def to_dict(self) -> dict:
        return {"idx": self.idx, "ok": self.ok, "oss_key": self.oss_key,
                "error": self.error, "cost_usd": self.cost_usd}


@dataclass
class CardBatchResult:
    cards: List[CardImageResult] = field(default_factory=list)
    total_cost_usd: float = 0.0

    @property
    def ok_count(self) -> int:
        return sum(1 for c in self.cards if c.ok)

    @property
    def all_ok(self) -> bool:
        return bool(self.cards) and all(c.ok for c in self.cards)

    def oss_keys(self) -> List[str]:
        return [c.oss_key for c in self.cards if c.ok and c.oss_key]


def build_card_prompt(headline: str, sub: str = "", *,
                      keyword: str = "", city: str = "") -> str:
    """[兼容保留] 单卡通用 prompt。

    新链路走 `build_prompts_for_group()` 的三类模板(封面/内容/收尾),
    本函数只服务不带结构的旧调用与测试。
    """
    # [WO-ACCEPTANCE-3FIX-2026-08-05 项2] 同 title_engine 的成因:关键词可能
    #   自己就带着城市名,裸拼会在生图 prompt 的「主题：」里出现「深圳深圳全屋定制…」。
    locale = join_city_keyword(city, keyword)
    parts = [
        "极简中文信息卡片，竖版 3:4，纯色或轻微渐变背景，",
        "中央是大号中文标题文字，排版干净留白充足，移动端一眼可读，",
        "不要出现任何水印、logo、二维码、英文乱码。",
        f"标题文字：{headline}",
    ]
    if sub:
        parts.append(f"副标题文字：{sub}")
    if locale:
        parts.append(f"主题：{locale}")
    return " ".join(parts)


def build_prompts_for_group(content, style, *, keyword: str = "",
                            city: str = "", max_cards: int = 0,
                            preset=None, contact_line: str = "",
                            aspect_ratio: str = "") -> List[dict]:
    """把一条内容(封面 + 内容卡 + 收尾卡)展开成**整组**生图 prompt。

    🔴 组内风格一致性靠**显式参数**,不靠"请保持风格一致"那种说辞 ——
       逐张独立生图时模型之间没有记忆,只有把 primary_color / header_bar /
       footer_bar 原样传给每一张,才可能出来像一组(§6b M6)。

    preset       四款风格之一(services.geo_douyin.card_templates.StylePreset);
                 None = 默认「设计文字卡」。整组**同一款**,不逐张换。
    contact_line 非空则排进收尾卡画面(「插入联系方式」开关的真落点)。
    aspect_ratio 画幅(规范 §8.8:3:4 / 9:16 客户可自选)。
                 🔴 **整组同一个画幅**,和 preset 一样逐张原样传 ——
                    一组里混着两种画幅比风格不一致还刺眼。空 = 默认画幅。

    返回 [{kind, idx, prompt, headline}],顺序即卡序。
    """
    from services.geo_douyin.card_templates import (
        build_closing_prompt, build_content_prompt, build_cover_prompt)

    out: List[dict] = []
    cover = getattr(content, "cover", None) or {}
    cards = getattr(content, "cards", None) or []
    closing = getattr(content, "closing", None) or {}

    # 组内进度识别「i/N」的分母。用**实际会出的张数**,不是用户要的张数 ——
    # 内容不全时(比如收尾卡没生成)写个假分母,组图上会出现 03/05 却只有 4 张。
    title = str(cover.get("title") or keyword or "").strip()
    head = str(closing.get("headline") or "").strip()
    total = (1 if title else 0) + len(cards) + (1 if head else 0)
    if max_cards:
        total = min(total, int(max_cards))

    if title:
        out.append({"kind": "cover", "idx": 1, "headline": title,
                    # 🔴 职责名要跟着 spec 走 —— 详情页缩略条拿它当标签。
                    #    不带出来的话前端只能把正文标题切五个字当标签(出来是半截词)。
                    "role_label": "封面",
                    "prompt": build_cover_prompt(
                        title, str(cover.get("subtitle") or ""), style,
                        preset, idx=1, total=total,
                        # 规范 §8.4:导航词是冻结文案,来自内容侧生成的 aux_labels
                        aux_labels=list(cover.get("aux_labels") or []),
                        aspect_ratio=aspect_ratio)})

    for i, c in enumerate(cards):
        card_index = len(out) + 1
        # 🔴 [WP3 · 规格 02 §10.2] 有冻结实体时,烘进图片的名字**只能**来自冻结 DTO。
        #    这里是"图片上印什么"的最后一道 —— 上游 content_generator 已经把三处写同源,
        #    但重绘/换风格等路径可能带着老卡进来,所以这一道不能省。
        #    没有冻结 DTO(问题卡 / 卖点卡)时行为逐字不变,取原可见字段。
        _frozen = c.get("frozen_entity")
        if isinstance(_frozen, dict) and _frozen.get("display_name"):
            from services.geo_douyin.frozen_entity import DISPLAY_NAME_MAX
            _headline = str(_frozen["display_name"])[:DISPLAY_NAME_MAX]
        else:
            _headline = str(c.get("entity") or c.get("headline") or "")
        out.append({
            "kind": "content", "idx": card_index,
            "headline": _headline,
            # series_plan 已按张数把职责回填在卡上,这里原样带出去
            "role_label": str(c.get("role_label") or ""),
            "prompt": build_content_prompt(
                i + 1, _headline,
                list(c.get("points") or []), style,
                metric=str(c.get("metric") or ""),
                caveat=str(c.get("caveat") or ""),
                preset=preset,
                card_index=card_index, total=total,
                # 规范 §3/§8.4:版式与职责由 series_plan 回填在卡上,代码说了算
                layout_role=str(c.get("layout_role") or ""),
                role_label=str(c.get("role_label") or ""),
                aspect_ratio=aspect_ratio),
        })

    if head:
        out.append({"kind": "closing", "idx": len(out) + 1, "headline": head,
                    "role_label": "收尾",
                    "prompt": build_closing_prompt(
                        head, str(closing.get("summary") or ""), style,
                        preset, contact_line=contact_line,
                        # 规范 §8.2:收口卡必须带 caveat 区
                        caveat=str(closing.get("caveat") or ""),
                        idx=len(out) + 1, total=total,
                        aspect_ratio=aspect_ratio)})

    # 🔴 张数预算硬约束:生成张数**绝不能超过用户要的卡数**。
    #    自审抓到的真缺陷:want=1 出 3 张、want=2 出 4 张(封面+内容+收尾恒定三段),
    #    用户要 1 张拿到 3 张 —— 而 §6 实证里单图占 23%,这条路径是常用场景不是边角。
    #    截断优先级:封面 > 内容卡 > 收尾卡(封面是钩子最重要,收尾可省)。
    if max_cards and len(out) > int(max_cards):
        budget = int(max_cards)
        cover_part = [o for o in out if o["kind"] == "cover"][:budget]
        rest = budget - len(cover_part)
        content_part = [o for o in out if o["kind"] == "content"][:max(0, rest)]
        rest -= len(content_part)
        closing_part = [o for o in out if o["kind"] == "closing"][:max(0, rest)]
        # 三段都取各自的前缀再按原序拼接 → idx 天然就是 1..N,不需要重编号。
        # (原来这里有两行"重编号",自审变异证明它永远不改变结果 = 死代码,已删。
        #  下面那条 idx 连续性的锁保留:将来若把截断改成"丢中间项",它会立刻转红。)
        out = cover_part + content_part + closing_part
    return out


async def _upload_bytes_to_oss(oss_key: str, data: bytes,
                               content_type: str = "image/png") -> str:
    """把 bytes 直传 OSS。

    🔴 oss2 是同步 SDK → 必须 to_thread,否则阻塞事件循环。
    """
    from services import oss_service

    def _put() -> str:
        bucket, _name = oss_service._get_bucket()  # noqa: SLF001 - 复用既有单例句柄
        result = bucket.put_object(
            oss_key, data,
            headers={"Content-Type": content_type,
                     "x-oss-server-side-encryption": "AES256"},
        )
        if result.status != 200:
            raise oss_service.OSSUploadError(
                f"put_object HTTP {result.status} key={oss_key}")
        return oss_key

    return await asyncio.to_thread(_put)


async def render_one_card(post_id: int, idx: int, headline: str, sub: str = "",
                          *, keyword: str = "", city: str = "",
                          prompt_override: str = "",
                          reference_urls: Optional[Sequence[str]] = None,
                          aspect_ratio: str = "") -> CardImageResult:
    """生成并上传【一张】卡片图。全程 bytes 在内存,不落盘。

    prompt_override 非空时直接用它(三类模板已在 card_templates 里拼好)。

    reference_urls 非空 = 走垫图。
    🔴🔴 **这是全仓传参考图的唯一入口**,禁抄段在这里强制附加,调用方绕不过去。
       第 0 步 A/B 实测:垫图不带禁抄段,参考图上的文字被抄进产物 3/3
       (含母版角标「模板示例」逐字印出);带上则 0/3。
       所以"传参考图"与"带禁抄段"必须是同一个动作,不能分两处写 ——
       任何"传了图忘了带禁抄"的调用路径都是事故入口。
    """
    from services.geo_douyin.card_templates import with_reference_guard
    from services.marketing.image_client import download_image, generate_image

    res = CardImageResult(idx=idx)
    res.prompt = prompt_override or build_card_prompt(
        headline, sub, keyword=keyword, city=city)

    refs = [str(u) for u in (reference_urls or []) if u]
    if refs:
        res.prompt = with_reference_guard(res.prompt)
    try:
        gen = await generate_image(res.prompt, size=image_size(aspect_ratio),
                                   resolution=_IMAGE_RESOLUTION,
                                   image_urls=refs or None)
        res.cost_usd = float(gen.get("cost_usd") or 0.0)
        if not gen.get("ok") or not gen.get("image_url"):
            res.error = str(gen.get("error") or "image_gen_failed")
            return res

        data = await download_image(gen["image_url"])
        if not data:
            res.error = "image_download_failed"
            return res

        oss_key = build_card_oss_key(post_id, idx, "png")
        res.oss_key = await _upload_bytes_to_oss(oss_key, data, "image/png")
        res.ok = True
        return res
    except Exception as e:  # noqa: BLE001 - 单张失败不拖垮整批
        res.error = f"{type(e).__name__}: {str(e)[:140]}"
        logger.warning("[douyin-image] 卡片 %s 失败: %s", idx, res.error)
        return res


async def render_prompt_group(post_id: int, specs: Sequence[dict], *,
                              concurrency: Optional[int] = None,
                              anchor_on_cover: bool = True,
                              on_card_done=None,
                              aspect_ratio: str = "") -> CardBatchResult:
    """按**已展开的整组 prompt**生成卡片图(封面/内容/收尾各用各的模板)。

    🔴 两阶段(Owner 2026-08-02 裁定②「封面垫图锚」):
        阶段 1 先单独出封面 → 阶段 2 把封面成图当参考图传给内容卡/收尾卡。
        目的是锁组内风格 —— 逐张独立生图时模型之间没有记忆,
        光靠 prompt 里的风格描述,三张出来仍可能不像一套。

    代价要说清楚:封面串行在前,整组耗时多一张图的时间(实测单张 ~50s)。
    这是拿延迟换组内一致性,是 Owner 明确要的取舍,不是我顺手加的。

    anchor_on_cover=False 时退回一次性并发(封面失败/无封面时也自动走这条)。

    on_card_done: 每出完一张就调一次(无入参;返回协程会被 await)。
        🔴 用途是**真进度**:异步化之后前端靠轮询看"第几张了",
           这个回调是那个数字的唯一来源。没有它,进度只能在整组结束时
           从 0 跳到 100 —— 那和假进度条没区别。
        🔴 回调里通常有 DB 往返,所以**必须允许它是 async 的** ——
           同步 DB 调用直接压在事件循环上是红线(§3.2)。返回协程就 await。
        🔴 回调抛异常绝不能带塌整组生图:进度是附属品,产物才是主线。
        🔴 计的是"完成"不是"成功":失败的那张也算走完了一格,
           否则一张失败之后进度条就永远停在那里。
    """
    limit = int(concurrency or image_gen_concurrency())
    sem = asyncio.Semaphore(max(1, limit))
    items = list(specs or [])

    async def _tick() -> None:
        if on_card_done is None:
            return
        try:
            r = on_card_done()
            if inspect.isawaitable(r):
                await r
        except Exception as e:  # noqa: BLE001 - 记进度失败不该让已出的图作废
            logger.warning("[douyin-image] 进度回调失败(不影响出图): %s",
                           f"{type(e).__name__}: {e}".rstrip(": "))

    async def _one(i: int, spec: dict, refs=None) -> CardImageResult:
        async with sem:
            res = await render_one_card(
                post_id, i + 1, str(spec.get("headline") or ""), "",
                prompt_override=str(spec.get("prompt") or ""),
                reference_urls=refs,
                # 🔴 整组同一个画幅。这里是唯一的入参点 ——
                #    漏掉它,封面按选的画幅出、其余几张按默认出,组内当场撕裂。
                aspect_ratio=aspect_ratio,
            )
        # 🔴 出锁之后再记:回调里有 DB 往返,压在信号量里会白占一个并发名额
        await _tick()
        return res

    cover_idx = next((i for i, s in enumerate(items)
                      if str(s.get("kind") or "") == "cover"), None)

    if not anchor_on_cover or cover_idx is None or len(items) < 2:
        results = await asyncio.gather(*(_one(i, s) for i, s in enumerate(items)))
        batch = CardBatchResult(cards=list(results))
        batch.total_cost_usd = round(sum(c.cost_usd for c in results), 6)
        return batch

    # ── 阶段 1:封面 ──
    cover_res = await _one(cover_idx, items[cover_idx])

    # ── 封面成图 → 可被 provider 取到的签名 URL ──
    #    OSS 私有桶,必须签名;实测 provider 侧能取到签名 URL(第 0 步实验已验证)。
    anchor: List[str] = []
    if cover_res.ok and cover_res.oss_key:
        try:
            anchor = await signed_card_urls([cover_res.oss_key])
        except Exception as e:  # noqa: BLE001 - 签不出来就退回无锚,不该让整组失败
            logger.warning("[douyin-image] 封面锚签名失败,退回无锚生成: %s",
                           f"{type(e).__name__}: {e}".rstrip(": "))
    if not cover_res.ok:
        # 封面没出来 → 后面几张没有锚可用。照常生成(整批仍会因封面失败而 all_ok=False),
        # 但至少错误信息完整,不会变成"整组一起失败、看不出是哪一步坏的"。
        logger.warning("[douyin-image] 封面失败,组内其余卡退回无锚生成: %s",
                       cover_res.error)

    # ── 阶段 2:其余卡并发,带封面锚 ──
    rest = [(i, s) for i, s in enumerate(items) if i != cover_idx]
    rest_results = await asyncio.gather(
        *(_one(i, s, anchor or None) for i, s in rest))

    by_index = {cover_idx: cover_res}
    for (i, _s), r in zip(rest, rest_results):
        by_index[i] = r
    results = [by_index[i] for i in range(len(items))]

    batch = CardBatchResult(cards=results)
    batch.total_cost_usd = round(sum(c.cost_usd for c in results), 6)
    return batch


async def render_cards(post_id: int, cards: Sequence[dict], *,
                       keyword: str = "", city: str = "",
                       concurrency: Optional[int] = None) -> CardBatchResult:
    """[兼容保留] 并发生成整组卡片图。并发上限走配置(工单 §3.2)。"""
    limit = int(concurrency or image_gen_concurrency())
    sem = asyncio.Semaphore(max(1, limit))

    async def _one(i: int, card: dict) -> CardImageResult:
        async with sem:
            return await render_one_card(
                post_id, i + 1,
                str(card.get("headline") or card.get("entity") or "").strip(),
                str(card.get("sub") or "").strip(),
                keyword=keyword, city=city,
            )

    results = await asyncio.gather(
        *(_one(i, c) for i, c in enumerate(cards or []))
    )
    batch = CardBatchResult(cards=list(results))
    batch.total_cost_usd = round(sum(c.cost_usd for c in results), 6)
    return batch


async def signed_card_urls(oss_keys: Sequence[str],
                           expires_seconds: int = 900) -> List[str]:
    """给前端预览用的签名 URL(私有 bucket,禁裸静态路径 —— 工单 §4.4)。

    oss2 签名是同步调用 → to_thread。

    🔴 空 key 直接返回空串,**不去签名**。部分成功交付(§15)之后,
       失败那张在 oss_keys 里是**占位空串** —— 占位是必须的:预览、卡片元数据、
       重抽的 card_index 三者按同一个下标对齐,把失败卡从数组里删掉会让
       后面所有卡的下标整体前移,用户点"重抽第 3 张"会抽到第 4 张。
    """
    from services import oss_service

    def _sign(key: str) -> str:
        return oss_service.generate_signed_url(key, expires_seconds=expires_seconds)

    return await asyncio.gather(
        *(asyncio.to_thread(_sign, k) if k else _empty_url()
          for k in (oss_keys or []))
    )


async def _empty_url() -> str:
    """占位空槽的签名结果。单独抽出来是为了让上面那行保持 gather 的同构。"""
    return ""
