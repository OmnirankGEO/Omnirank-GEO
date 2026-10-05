"""
联系方式占位符渲染 — 单元测试(老板规格八 · 8 项 + 最严策略)

直接跑:python tests/test_contact_placeholder.py
覆盖:
  1. add_contact=false → 文章不出现 NEED_CONTACT / CLIENT_CONTACT / 电话 / 微信
  2. add_contact=true 但客户没填 → 不插联系方式
  3. add_contact=true + self → 显示完整联系方式
  4. media + none → 软化(不出现电话/微信/官网链接)
  5. media + website_only → 只出现官网
  6. media + full_contact → 完整联系方式
  7. awaiting confirmation 重投 → 同样规则渲染(与首次一致)
  8. 正文手写 [CLIENT_CONTACT] / 裸电话 不绕过规则
  + strictest_policy 多媒体取最严

2026-06-02 GEO CTO
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.contact_placeholder as _cp  # noqa: E402 (patch.object render 用)
from services.contact_placeholder import (  # noqa: E402
    fill_contact_placeholders, render_contact_for_publish, render_contact_for_preview,
    strip_contact_placeholders, strictest_policy, has_contact_placeholders,
    safe_render_contact_for_publish,
)

PHONE = "13800138000"
WECHAT = "my_wechat_id_xyz"
SITE = "https://example-brand.com"
ADDR = "深圳市南山区科技路1号"
CONTACT = {"phone": PHONE, "wechat": WECHAT, "website": SITE, "address": ADDR, "brand_name": "测试品牌"}
EMPTY = {"phone": "", "wechat": "", "website": "", "address": "", "brand_name": "测试品牌"}


def _patch(info):
    # _get_contact 内部 from db.profile_db import get_contact_info_by_brand
    return patch("db.profile_db.get_contact_info_by_brand", return_value=info)


_results = []


def _check(name, cond):
    _results.append((name, bool(cond)))
    print(("  [PASS] " if cond else "  [FAIL] ") + name)


def test_1_strip():
    content = "正文段落\n\n[NEED_CONTACT]\n\n[CLIENT_CONTACT]\n\n结尾"
    out = strip_contact_placeholders(content)
    _check("1. add_contact=false strip 删占位",
           "[NEED_CONTACT]" not in out and "[CLIENT_CONTACT]" not in out and PHONE not in out)


def test_2_fill_empty():
    with _patch(EMPTY):
        out = fill_contact_placeholders("正文\n\n[NEED_CONTACT]\n\n结尾", brand_id=1)
    _check("2. add_contact=true 但没填 → 不插",
           "[NEED_CONTACT]" not in out and "[CLIENT_CONTACT]" not in out)


def test_2b_fill_has():
    with _patch(CONTACT):
        out = fill_contact_placeholders("正文\n\n[NEED_CONTACT]\n\n结尾", brand_id=1)
    _check("2b. 填了 → [NEED_CONTACT] 转 [CLIENT_CONTACT]",
           "[CLIENT_CONTACT]" in out and "[NEED_CONTACT]" not in out)


def test_3_self_full():
    content = "正文\n\n[CLIENT_CONTACT]\n\n结尾"
    with _patch(CONTACT):
        out = render_contact_for_publish(content, brand_id=1, channel="self")
    _check("3. self → 完整联系方式",
           PHONE in out and WECHAT in out and SITE in out and ADDR in out and "[CLIENT_CONTACT]" not in out)


def test_4_media_none():
    content = "正文\n\n[CLIENT_CONTACT]\n\n结尾"
    with _patch(CONTACT):
        out = render_contact_for_publish(content, brand_id=1, channel="media", contact_policy="none")
    _check("4. media+none → 软化(无电话/微信/官网链接)",
           PHONE not in out and WECHAT not in out and SITE not in out
           and "测试品牌" in out and "[CLIENT_CONTACT]" not in out)


def test_5_media_website_only():
    content = "正文\n\n[CLIENT_CONTACT]\n\n结尾"
    with _patch(CONTACT):
        out = render_contact_for_publish(content, brand_id=1, channel="media", contact_policy="website_only")
    _check("5. media+website_only → 只官网(无电话/微信)",
           SITE in out and PHONE not in out and WECHAT not in out)


def test_6_media_full():
    content = "正文\n\n[CLIENT_CONTACT]\n\n结尾"
    with _patch(CONTACT):
        out = render_contact_for_publish(content, brand_id=1, channel="media", contact_policy="full_contact")
    _check("6. media+full_contact → 完整",
           PHONE in out and WECHAT in out and SITE in out)


def test_7_resubmit_same():
    content = "正文\n\n[CLIENT_CONTACT]\n\n结尾"
    with _patch(CONTACT):
        first = render_contact_for_publish(content, brand_id=1, channel="media", contact_policy="none")
        resub = render_contact_for_publish(content, brand_id=1, channel="media", contact_policy="none")
    _check("7. 重投渲染与首次一致 + 仍软化",
           first == resub and PHONE not in resub)


def test_8_no_bypass():
    # 手写 [CLIENT_CONTACT] → media+none 仍软化(占位符不带数据)
    with _patch(CONTACT):
        out1 = render_contact_for_publish("正文 [CLIENT_CONTACT] 想绕过\n\n尾", brand_id=1, channel="media", contact_policy="none")
    cond1 = PHONE not in out1 and WECHAT not in out1 and "[CLIENT_CONTACT]" not in out1
    # 正文直接写客户裸电话 → media+none 兜底清掉
    with _patch(CONTACT):
        out2 = render_contact_for_publish(f"正文 联系电话 {PHONE} 找我\n\n尾", brand_id=1, channel="media", contact_policy="none")
    cond2 = PHONE not in out2
    _check("8. 手写 CLIENT_CONTACT / 裸电话 不绕过 none", cond1 and cond2)


def test_8b_no_brand():
    # 拿不到 brand_id → 删占位符(不裸发原文)
    out = render_contact_for_publish("正文\n\n[CLIENT_CONTACT]\n\n尾", brand_id=None, channel="self")
    _check("8b. 拿不到 brand_id → 删占位符", "[CLIENT_CONTACT]" not in out)


def test_9_strictest():
    ok = (strictest_policy(["full_contact", "none"]) == "none"
          and strictest_policy(["full_contact", "website_only"]) == "website_only"
          and strictest_policy(["full_contact", "full_contact"]) == "full_contact"
          and strictest_policy([]) == "none")
    _check("9. strictest_policy 多媒体取最严", ok)


def test_10_preview():
    content = "正文\n\n[CLIENT_CONTACT]\n\n结尾"
    with _patch(CONTACT):
        out = render_contact_for_preview(content, brand_id=1)
    _check("10. 预览 → 完整 + 软化提示",
           PHONE in out and "媒体" in out and "[CLIENT_CONTACT]" not in out)


def test_11_safe_fail_closed_media():
    # 🔴 Codex 复审:render 抛异常时 safe helper 不得把原文 [CLIENT_CONTACT] 发到媒体
    content = "正文\n\n[CLIENT_CONTACT]\n\n尾"
    with patch.object(_cp, "render_contact_for_publish", side_effect=RuntimeError("boom")):
        out = safe_render_contact_for_publish(content, brand_id=1, channel="media", contact_policy="none")
    _check("11. safe render 异常(media)→ fail-closed strip,不发 [CLIENT_CONTACT]/[NEED_CONTACT] 原文",
           "[CLIENT_CONTACT]" not in out and "[NEED_CONTACT]" not in out)


def test_12_safe_fail_closed_self():
    content = "正文\n\n[CLIENT_CONTACT]\n\n尾"
    with patch.object(_cp, "render_contact_for_publish", side_effect=RuntimeError("boom")):
        out = safe_render_contact_for_publish(content, brand_id=1, channel="self")
    _check("12. safe render 异常(self)→ 也 strip 占位符(不发字面量)", "[CLIENT_CONTACT]" not in out)


def test_13_safe_media_none_scrub():
    # safe helper 正常路径:media none 正文裸电话/微信被清(防 LLM 写进正文裸发)
    with _patch(CONTACT):
        out = safe_render_contact_for_publish(f"正文 联系 {PHONE} 微信 {WECHAT} 找我\n\n尾", brand_id=1, channel="media", contact_policy="none")
    _check("13. safe media none 正文裸电话/微信被清", PHONE not in out and WECHAT not in out)


def test_14_generic_scrub_no_info():
    # 🔴 Codex 保留点:联系方式查询失败(info 不可读)+ media none + 正文裸手机号/微信号 → 通用清扫兜底
    content = "正文 联系电话 13912345678 或 微信号:bosswx888 详谈\n\n尾"
    with _patch(EMPTY):  # 模拟查询返回空/不可读(_get_contact → None,拿不到客户具体值)
        out = safe_render_contact_for_publish(content, brand_id=1, channel="media", contact_policy="none")
    _check("14. 查询失败(info不可用)+media none → 通用清扫裸手机号/微信号",
           "13912345678" not in out and "bosswx888" not in out)


def test_15_generic_no_false_positive():
    # 通用清扫不误删正文正常内容(年份/金额/「微信生态」泛指/参考链接)
    content = "2024 年市场规模 13800 万元，微信生态用户增长，参考 https://ref.example.com/report\n\n尾"
    with _patch(EMPTY):
        out = safe_render_contact_for_publish(content, brand_id=1, channel="media", contact_policy="none")
    _check("15. 通用清扫不误删 年份/金额/微信生态/参考链接",
           "2024" in out and "13800" in out and "微信生态" in out and "ref.example.com" in out)


if __name__ == "__main__":
    print("== contact_placeholder 单元测试 ==")
    for fn in [test_1_strip, test_2_fill_empty, test_2b_fill_has, test_3_self_full,
               test_4_media_none, test_5_media_website_only, test_6_media_full,
               test_7_resubmit_same, test_8_no_bypass, test_8b_no_brand,
               test_9_strictest, test_10_preview,
               test_11_safe_fail_closed_media, test_12_safe_fail_closed_self, test_13_safe_media_none_scrub,
               test_14_generic_scrub_no_info, test_15_generic_no_false_positive]:
        try:
            fn()
        except Exception as e:
            _check(fn.__name__ + " (异常)", False)
            print(f"     EXC: {e}")
    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print(f"\n== 结果: {passed}/{total} PASS ==")
    sys.exit(0 if passed == total else 1)
