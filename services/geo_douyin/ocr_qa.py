"""GEO 抖音图文 · 卡面文字 OCR 逐字核验(杂志级连续组图规范 §8.6 第 11 条)

规范原文:「"逐字一致"(#5)的核法 = 既有 OCR QA 口径(OCR+人眼),
          不收"我看了觉得对"」。本模块就是那个 OCR 那一半;人眼那一半靠
          详情页把「哪一张少了哪一行」指出来,让人**知道该看哪张**。

## 核的是什么

`card_templates.frozen_text_lines()` 列出的那几行 —— 客户内容里**给定的字**:
封面主标题 / 副标题 / 导航词、内容卡主标题 / 要点 / 数字 / 提醒、收尾卡标题 /
小结 / 提醒 / 联系方式。风格条(header_bar / footer_bar)和进度标识 01/04
**不核**:那是模板常量和生成物,不是"客户给的字有没有被改写"这个问题。

## 三条设计约束(都不是随手定的)

🔴 **提示级,永不阻断**。与广告法扫描、资料库黄点同一档
   (Owner 2026-08-03 二次拍板:发不发由客户,平台只尽提示义务)。
   OCR 本身会错(尤其小字、艺术字、竖排),拿一个会错的判据去拦付费产出,
   造成的损失比它挡住的问题大。

🔴 **`checked=False` 与 `ok=True` 必须分得开**。视觉服务不可用 / 没配 key /
   图还没出 —— 这些是"没核",不是"核过没问题"。前端据此**什么都不显示**,
   绝不把没核渲染成绿灯。这条是本模块最容易写错的地方:
   把失败 return 成 `ok=True` 的报告,整套核验就永久静默失效且没人会发现。

🔴 **不收费,也不自动跑**。
   - 不收费:这是核验**我们自己**有没有把客户的字印对,是我方 QA,
     不是给用户的交付物。让用户付钱来检查我们排版对不对是说不通的。
   - 不自动跑:每张卡一次视觉调用,4-9 张一组。挂在每次生产上等于给
     每一单加一笔没进价目表的成本。所以是详情页**按需触发**,并带节流。
   ⚠️ 若 Owner 认为该定价,加一个 feature_code 即可,本模块不需要改。

🔴 走**签名 URL** 而不是把图下载进本进程再 base64:
   工单 §3.3「产物即传 OSS,本地磁盘零滞留」,顺带省掉一次往返。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("GEO-Douyin-OCR")

_OCR_PROMPT = (
    "把这张图里出现的所有文字逐字抄下来，保持原样。"
    "每行一条，按从上到下的顺序。"
    "不要解释、不要翻译、不要改写、不要补充任何图上没有的字。"
)

# 一次核验最多看几张。防"9 张卡 × 反复点"把视觉调用打上去。
OCR_MAX_CARDS = 9
# 判定一行"印上了"的最低匹配比例。见 _line_hit 的说明。
OCR_MATCH_RATIO = 0.8

# 归一化时要抹掉的东西:空白、各类引号书名号、以及中英标点的写法差异。
# 🔴 只抹**写法**差异,不抹字。抹掉汉字就等于把判据放空。
_STRIP_RE = re.compile(r"[\s　「」『』《》（）()【】\[\]，,。.、;；:：!！?？~～\-—_/|·]+")


@dataclass
class CardOcrCheck:
    card_index: int
    checked: bool = False
    ok: bool = True
    reason: str = ""                                   # checked=False 时说明原因
    missing: List[str] = field(default_factory=list)   # 没在图上找到的冻结文案

    def to_dict(self) -> dict:
        return {"card_index": self.card_index, "checked": self.checked,
                "ok": self.ok, "reason": self.reason, "missing": list(self.missing)}


@dataclass
class OcrQaReport:
    checked: bool = False
    reason: str = ""
    cards: List[CardOcrCheck] = field(default_factory=list)

    @property
    def flagged_count(self) -> int:
        return sum(1 for c in self.cards if c.checked and not c.ok)

    @property
    def checked_count(self) -> int:
        return sum(1 for c in self.cards if c.checked)

    def to_dict(self) -> dict:
        return {"checked": self.checked, "reason": self.reason,
                "flagged_count": self.flagged_count,
                "checked_count": self.checked_count,
                "cards": [c.to_dict() for c in self.cards]}


def normalize_for_match(text: str) -> str:
    """比对用归一化:去空白与标点写法差异,全角数字/字母转半角,英文转小写。

    🔴 刻意**不做**同义改写、不做繁简转换、不删汉字 —— 那些会让"模型把文案
       改写了"这件事被归一化掉,而那正是本模块要抓的东西。
    """
    s = str(text or "")
    s = "".join(
        chr(ord(ch) - 0xFEE0) if "！" <= ch <= "～" else ch
        for ch in s
    )
    return _STRIP_RE.sub("", s).lower()


def _line_hit(line: str, ocr_norm: str) -> bool:
    """这一行算不算印上了。

    整行原样命中最理想。但 OCR 断行/漏一个标点/把长句拆两行都很常见,
    整句 in 会因此大量误报。所以退一步:把这一行切成若干片段,
    **多数片段命中**就算印上了。
    🔴 阈值 0.8 是保守取的(宁可漏报不要误报)—— 提示级判据一旦开始乱叫,
       用户就再也不看它了,那才是真的失效。
       ⚠️ 这个阈值**没有用真实 OCR 输出标定过**(本机三个视觉 key 全 401,
          见交付单)。它需要生产上跑几组真图回来再定,现在只是个保守起点。
    """
    norm = normalize_for_match(line)
    if not norm:
        return True
    if norm in ocr_norm:
        return True
    # 按 4 字一片切(中文 4 字基本是一个词组),看命中比例
    chunks = [norm[i:i + 4] for i in range(0, len(norm), 4)]
    chunks = [c for c in chunks if len(c) >= 2]
    if not chunks:
        return norm in ocr_norm
    hit = sum(1 for c in chunks if c in ocr_norm)
    return hit / len(chunks) >= OCR_MATCH_RATIO


def compare_card(expected_lines: List[str], ocr_text: str) -> List[str]:
    """返回**没找到**的那几行(已按原文返回,便于直接显示给人看)。"""
    ocr_norm = normalize_for_match(ocr_text)
    if not ocr_norm:
        return list(expected_lines or [])
    return [ln for ln in (expected_lines or []) if not _line_hit(ln, ocr_norm)]


def expected_lines_for_post(content: dict, contact_line: str = "") -> Dict[int, List[str]]:
    """{卡序(0基): 该卡的冻结文案}。卡序与 oss_keys / 预览 URL 同序。

    🔴 顺序必须与 image_pipeline.build_card_specs 完全一致(封面 → 内容卡 → 收尾卡),
       否则会拿第 2 张的答案去核第 3 张的图,报出一堆假问题。
       tests 里有一条锁按同一份内容比对两边的顺序。
    """
    from services.geo_douyin.card_templates import frozen_text_lines

    c = content or {}
    cover = c.get("cover") or {}
    cards = list(c.get("cards") or [])
    closing = c.get("closing") or {}

    out: Dict[int, List[str]] = {}
    idx = 0
    if str(cover.get("title") or "").strip():
        out[idx] = frozen_text_lines("cover", cover)
        idx += 1
    for card in cards:
        if not isinstance(card, dict):
            idx += 1
            continue
        out[idx] = frozen_text_lines("content", card)
        idx += 1
    if str(closing.get("headline") or "").strip():
        payload = dict(closing)
        if contact_line:
            payload["contact_line"] = contact_line
        out[idx] = frozen_text_lines("closing", payload)
    return out


#: 🔴 [WO_220-c1] 原来这里有一份与 `tools/vision/image_describe._vision_model`
#:   **逐字相同**的实现。同一谓词两处,不一致时的表现是「一个走新线、一个走旧线」,
#:   而没有任何东西会报错(本仓 feedback_one_predicate_one_place_or_half_goes_unverified)。
#:   现在模型解析与端点路由都由 `config.vision_routing` 单点给出。


async def ocr_image_url(image_url: str, *, timeout_s: float = 60.0) -> Optional[str]:
    """对一张图做 OCR。**失败返回 None**(不是空串)——

    🔴 None 与 "" 必须分开:None = 没核成,"" = 核了但图上一个字都没有。
       后者是真问题(整张图文字没排上),前者不是。混成一个值,
       视觉服务一挂就会把每张卡都报成"文字全丢",用户看到一片红。
    """
    import os

    import httpx

    from config.vision_routing import resolve_vision_target

    target = resolve_vision_target()
    model = target.model
    api_key = os.environ.get(target.api_key_env, "")
    if not api_key or not image_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                target.endpoint,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role": "user", "content": [
                          {"type": "image_url", "image_url": {"url": image_url}},
                          {"type": "text", "text": _OCR_PROMPT},
                      ]}],
                      "max_tokens": 800, "temperature": 0.0,
                      #: DeepSeek 线必须关思考(见 config.vision_routing 抬头);
                      #: 百炼线这里是空字典。
                      **target.extra_body},
            )
        if resp.status_code != 200:
            logger.warning("[douyin-ocr] 视觉服务 HTTP %s: %s",
                           resp.status_code, resp.text[:200])
            return None
        _payload = resp.json()
        _echoed = str(_payload.get("model") or "").strip()
        if _echoed and _echoed != model:
            #: 🔴 回显 != 请求名 = 供应商静默换了模型。OCR 的**整个用途**是逐字核验,
            #:   核验器本身被换掉却照常出结果,比核不成更糟:
            #:   它会给出一份看起来核过的报告。返 None = 「没核成」,不是「一个字都没有」。
            logger.warning("[douyin-ocr] 回显模型 %s != 请求 %s,本次不采信", _echoed, model)
            return None
        content = _payload["choices"][0]["message"]["content"]
        return content if isinstance(content, str) else None
    except Exception as e:  # noqa: BLE001 - 核验失败不该影响任何主链
        logger.warning("[douyin-ocr] 视觉调用异常: %s", e)
        return None


async def run_post_ocr_qa(post: dict) -> OcrQaReport:
    """按作品跑一遍 OCR 逐字核验。任何一步缺基准 → checked=False,**不报绿灯**。"""
    import asyncio

    from services.geo_douyin.image_pipeline import signed_card_urls

    meta = post.get("generation_meta") or {}
    content = meta.get("content") if isinstance(meta.get("content"), dict) else {}
    if not content:
        return OcrQaReport(checked=False, reason="这条内容还没有生成过卡片文案，无法核验")

    oss_keys = [k for k in (post.get("oss_keys") or [])]
    if not any(oss_keys):
        return OcrQaReport(checked=False, reason="这条内容还没有出图，出图后才能核验")

    # 🔴 联系方式来自 `generation_meta.contact_line`,**不是**表上的列 ——
    #    第一版我按直觉写了 `post["contact_display"]`,库里根本没这一列
    #    (列名靠猜不会报错,只会永远取到空 → 联系方式那一行永远"缺失")。
    #    取法与 api_regenerate_post 的那处逐字同源。
    contact = (str((post.get("generation_meta") or {}).get("contact_line") or "")
               if post.get("contact_enabled") else "")
    expected = expected_lines_for_post(content, contact_line=contact)
    if not expected:
        return OcrQaReport(checked=False, reason="这条内容没有可核验的冻结文案")

    urls = await signed_card_urls(oss_keys[:OCR_MAX_CARDS])

    async def _one(i: int, url: str) -> CardOcrCheck:
        lines = expected.get(i) or []
        if not url:
            # 空槽 = 那一张没出成(部分成功交付会留占位)。没图不是"文字丢了"。
            return CardOcrCheck(card_index=i, checked=False, reason="这一张还没有出图")
        if not lines:
            return CardOcrCheck(card_index=i, checked=False,
                                reason="这一张没有需要逐字核对的文案")
        text = await ocr_image_url(url)
        if text is None:
            return CardOcrCheck(card_index=i, checked=False,
                                reason="文字识别服务暂时不可用")
        missing = compare_card(lines, text)
        return CardOcrCheck(card_index=i, checked=True, ok=not missing,
                            missing=missing)

    checks = await asyncio.gather(*(_one(i, u) for i, u in enumerate(urls)))
    report = OcrQaReport(cards=list(checks))
    report.checked = any(c.checked for c in checks)
    if not report.checked:
        # 一张都没核成 —— 把第一条具体原因带出去,别只说"失败了"
        report.reason = next((c.reason for c in checks if c.reason),
                             "这次没能核验卡面文字")
    return report
