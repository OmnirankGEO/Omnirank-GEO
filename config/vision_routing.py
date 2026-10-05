# -*- coding: utf-8 -*-
"""视觉调用的**单点**路由:模型名 → (端点, key 环境变量, 计价 platform, 额外请求体)。

WO_220-c1 · Owner 2026-09-15:「识别图片这里 deepseek-flash 现在升级成多模态了,
你测试一下行不行,如果可以的话就换成 deepseek」。
Review 探针取证:`REVIEW_DEEPSEEK_VISION_PROBE_2026-09-15.md`。

═══════════════════════════════════════════════════════════════════
🔴 为什么要单点,而不是在两个消费方各写一遍

  GEO 侧有**两个**视觉消费方,它们各自有一份逐字相同的 `_vision_model()`:
      · `tools/vision/image_describe.py`      客户图库资产打标
      · `services/geo_douyin/ocr_qa.py`       图文卡片 OCR 质检
  两份不一致时的表现是「一个走新线、一个走旧线」,而**没有任何东西会报错** ——
  本仓 `feedback_one_predicate_one_place_or_half_goes_unverified`。

🔴 端点、key、platform、thinking 这四样必须**一起**返回

  它们是同一件事的四个面。分开写时总有一处会被落下:
  改了模型名没改端点 ⇒ 把 qwen 的名字发给 DeepSeek ⇒ 400 → 静默 fallback,
  表现为「识别失败」而真因是路由;改了端点没改 platform ⇒ 成本记到另一家名下
  (WO_214 同族)。本仓 `a-model-rename-is-a-default-change-in-disguise`。

🔴 `thinking: disabled` 是硬条件,不是调优

  Review 探针实测:复杂截图开着默认思考时,1500 token 预算被 reasoning 吃光,
  `content` 为空、`finish_reason=length`,线上静默落 `_fallback("返回空内容")`。
  把 max_tokens 提到 6000 也一样(reasoning 10,621 字,content 仍空)。

🔴 共享的 admin 配置项 `vision_model` **不动它的默认值**

  那个键有**四个**消费方,另外两个(社媒主路径 router 的 PDF 看图 —— 现在 `services/upload_text.py`、
  默认定义 `db/social_preferences_db.py:64`)仍然只打 DashScope 端点。
  把共享默认改成 `deepseek-flash`,等于让它们发一个对面不认识的名字。
  所以这里只改**本解析器的兜底值**;管理员显式配了 Qwen 名字时,
  `resolve_vision_target` 会把它路由回 DashScope —— 而不是 400 后静默降级。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
from typing import Any, Dict, NamedTuple, Optional

from config.deepseek_models import (
    DEEPSEEK_OFFICIAL_EMITTABLE,
    DEEPSEEK_OFFICIAL_FLASH,
    normalize_deepseek_model,
)

logger = logging.getLogger("GEO-VisionRouting")

#: DeepSeek 官方线
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
#: 百炼 OpenAI 兼容线(与 social 视觉转写同源)
DASHSCOPE_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

#: 🔴 官方线上**只有 flash 能识图**。Review 探针实测:`deepseek-v4-pro` 同一张图
#:   返 **200**,但 prompt 只计 108 token(图根本没进去),回答「无法查看该图片」。
#:   这是一个**会返 200 的错答案**,比报错难发现得多 ——
#:   本仓 `a-200-can-hide-a-silently-substituted-model` 的近亲。
DEEPSEEK_VISION_CAPABLE = frozenset({DEEPSEEK_OFFICIAL_FLASH})

#: 关思考的请求体片段。只发给 DeepSeek 线 —— 百炼不认这个字段。
DEEPSEEK_NO_THINKING: Dict[str, Any] = {"thinking": {"type": "disabled"}}

#: 本解析器的兜底默认(**不是**共享 admin 配置项的默认值,见模块抬头)
DEFAULT_VISION_MODEL = DEEPSEEK_OFFICIAL_FLASH

#: 🔴 GEO 线**专用**键。**不读**共享的 `vision_model` ——
#:   那个键被 `ensure_schema()` 播种成 `qwen3.6-flash` 写进库,读它等于让 GEO 线
#:   永远留在百炼,而代码里的兜底默认看起来已经改好了。
#:   这是本仓 WO_217-c1a §2 那条「活的开关面是运行期配置不是代码默认值」的复演 ——
#:   我自己在同一天又踩了一次,是**真厂商实证**抓住的,不是判据
#:   (13 条单元判据全都桩掉了模型解析,一条没红)。
_ADMIN_SETTING_KEY = "geo_vision_model"
#: 社媒线仍用它,本模块**不碰**
_SHARED_SOCIAL_SETTING_KEY = "vision_model"


class VisionTarget(NamedTuple):
    """一次视觉调用要用的全部路由信息。四样一起给,不许分开取。"""
    model: str
    endpoint: str
    api_key_env: str
    platform: str
    extra_body: Dict[str, Any]

    @property
    def is_deepseek(self) -> bool:
        return self.platform == "deepseek"


def resolve_vision_model() -> str:
    """读共享 admin 配置项;读不到就用本模块的兜底默认(DeepSeek flash)。

    官方线上配了非 flash(如 `deepseek-v4-pro`)⇒ 拉回 flash 并**出声**:
    那条路会返 200 但看不见图,静默下去就是每张图都识别不准而无人知晓。
    """
    try:
        from db.social_preferences_db import get_admin_setting
        raw = get_admin_setting(_ADMIN_SETTING_KEY, str, DEFAULT_VISION_MODEL)
    except Exception:  # noqa: BLE001 - 配置读不到不该让识图整条挂掉
        raw = None
    model = normalize_deepseek_model(raw) or DEFAULT_VISION_MODEL

    if model in DEEPSEEK_OFFICIAL_EMITTABLE and model not in DEEPSEEK_VISION_CAPABLE:
        logger.error(
            "[vision] vision_model=%s 在 DeepSeek 官方线上不支持识图"
            "(会返 200 但答「无法查看该图片」),已拉回 %s",
            model, DEFAULT_VISION_MODEL)
        model = DEFAULT_VISION_MODEL
    return model


def resolve_vision_target(model: Optional[str] = None) -> VisionTarget:
    """**端点跟着模型走。** 传 None 则先解析当前配置的模型。"""
    if model is None:
        model = resolve_vision_model()
    if model in DEEPSEEK_OFFICIAL_EMITTABLE:
        return VisionTarget(model=model, endpoint=DEEPSEEK_ENDPOINT,
                            api_key_env="DEEPSEEK_API_KEY", platform="deepseek",
                            extra_body=dict(DEEPSEEK_NO_THINKING))
    return VisionTarget(model=model, endpoint=DASHSCOPE_ENDPOINT,
                        api_key_env="DASHSCOPE_API_KEY", platform="dashscope",
                        extra_body={})
