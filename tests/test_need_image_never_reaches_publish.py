# -*- coding: utf-8 -*-
"""[Review-CTO 2026-07-26 · D11 ④ 收口] 图片需求占位不得进入对外发布正文。

D11 ④ 让"品牌无已授权图"时留下 `[NEED_IMAGE ... status=awaiting_client_asset]` 保底占位，
前端据此提示补图。但发布渲染原本只认 `[CLIENT_IMAGE]`，于是两件坏事：
  ① 占位符原文被当正文发到媒体渠道（客户可见乱码）；
  ② `platform_safety_profiles` 见 "[NEED_IMAGE" 即判 hard → rewrite_required（渠道级
     不可覆盖）→ 保底占位反而成了新的发布阻断，正是 D8/D11 要消灭的形态。

本锁证明发布期两条渲染路径都剥除需求占位，且不误伤真实图片与正文。
"""
from services.image_placeholder import (
    render_for_publish,
    strip_client_images,
    strip_image_requests,
)

_FALLBACK = "[NEED_IMAGE role=brand_intro purpose=品牌形象或经营场景图 status=awaiting_client_asset]"
_PLAIN_REQUEST = "[NEED_IMAGE role=case purpose=案例、资质或产品细节图]"


def test_fallback_placeholder_stripped_by_helper():
    body = f"第一段正文。\n\n{_FALLBACK}\n\n第二段正文。"
    out = strip_image_requests(body)
    assert "[NEED_IMAGE" not in out
    assert "第一段正文。" in out and "第二段正文。" in out


def test_plain_request_placeholder_also_stripped():
    assert "[NEED_IMAGE" not in strip_image_requests(f"a\n\n{_PLAIN_REQUEST}\n\nb")


def test_strip_client_images_also_drops_requests():
    # 发布预案(平台不支持外链图)路径同样不得留需求占位
    body = f"正文。\n\n{_FALLBACK}\n\n[CLIENT_IMAGE asset_id=7 role=case]\n\n尾段。"
    out = strip_client_images(body)
    assert "[NEED_IMAGE" not in out and "[CLIENT_IMAGE" not in out
    assert "正文。" in out and "尾段。" in out


def test_render_for_publish_without_brand_id_strips_requests():
    # brand_id 缺失走 strip 分支：仍不得留需求占位
    out = render_for_publish(f"正文。\n\n{_FALLBACK}", None)
    assert "[NEED_IMAGE" not in out


def test_body_without_markers_is_untouched():
    body = "一段完全干净的正文，含 [方括号] 与中文标点。"
    assert strip_image_requests(body) == body
    assert strip_client_images(body) == body


def test_platform_profile_would_have_blocked_before_fix():
    """反证:平台档确实把 [NEED_IMAGE 判为 hard —— 所以发布期必须先剥除。

    这条锁死"剥除是必要的"这个前提:若哪天平台档不再 hard,本测试会提醒复核，
    而不是让保底占位悄悄流到渠道。
    """
    from writing.platform_safety_profiles import review_for_platform

    verdict = review_for_platform(
        title="正常标题",
        content="正文" * 200 + _FALLBACK,
        profile="sohu_geo_strict_v1",
    )
    codes = [str(item.get("code")) for item in (verdict.hard_failures or [])]
    assert "sohu_client_image_placeholder_detected" in codes, codes
    assert verdict.decision == "rewrite_required"
