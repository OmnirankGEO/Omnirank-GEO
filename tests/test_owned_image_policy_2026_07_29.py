"""判别锁 · 返修单 `REWORK_T4_SAFEMARKDOWN_IMAGE_2026-07-29`（Python 侧）。

前端侧的真渲染锁在 `frontend/src/components/__tests__/SafeMarkdown.image.spec.tsx`
（Playwright + `naturalWidth`），两边跑同一份 `config/owned_image_asset_policy.json`
的测试向量 —— 任一侧口径漂移即转红（返修单 §4：白名单必须同一份 SSOT）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
POLICY_JSON = ROOT / "config" / "owned_image_asset_policy.json"


def _policy() -> dict:
    return json.loads(POLICY_JSON.read_text(encoding="utf-8"))


def test_shared_policy_file_exists_and_carries_test_vectors():
    """SSOT 文件本身是锁的一部分：没了它两边只能各写一份白名单。"""
    policy = _policy()
    assert policy["path_prefix"] == "/uploads/article-images/"
    assert set(policy["allowed_schemes"]) == {"http", "https"}
    vectors = policy["test_vectors"]
    assert len(vectors) >= 15, "向量太少覆盖不住绕过面"
    assert any(v["owned"] for v in vectors) and any(not v["owned"] for v in vectors)


def test_python_side_matches_every_shared_vector():
    """锁 2/3 的后端侧：逐条跑共享向量，外部域名 / data: / blob: / 协议相对全拦。"""
    from services.owned_image_policy import is_owned_image_url

    failures = []
    for vector in _policy()["test_vectors"]:
        actual = is_owned_image_url(vector["url"], site_origin="https://site.example")
        if actual != vector["owned"]:
            failures.append(f"{vector['url']!r} 期望 {vector['owned']} 实际 {actual}（{vector['note']}）")
    assert not failures, "\n".join(failures)


def test_backend_renderer_keeps_owned_image_and_drops_everything_else():
    """锁 5：后端 bleach 渲染器不再无条件剥 <img>，但只留自有图库的。

    返修单 §4 要求的"后端同步改"就是这条：若只修前端，写作大厅能看见了，
    但走 `render_safe_markdown` 的任何交付面仍然没图。
    """
    from services.safe_markdown_renderer import render_safe_markdown

    owned = render_safe_markdown("![Logo](/uploads/article-images/662/a_safe.jpg)")
    assert "<img" in owned and 'src="/uploads/article-images/662/a_safe.jpg"' in owned
    assert 'alt="Logo"' in owned

    owned_abs = render_safe_markdown(
        "![L](http://127.0.0.1:8001/uploads/article-images/662/a_safe.jpg)"
    )
    assert "<img" in owned_abs

    for blocked in (
        "![px](https://evil.example/px.gif)",
        "![px](https://evil.example/uploads/article-images/662/x.jpg)",
        "![x](data:image/gif;base64,R0lGODlhAQABAAAAACw=)",
        "![y](//evil.example/uploads/article-images/662/x.jpg)",
        "![z](/uploads/avatars/1/a.png)",
        "![w](https://user:pass@site.example/uploads/article-images/662/x.jpg)",
    ):
        rendered = render_safe_markdown(blocked)
        assert "<img" not in rendered, f"{blocked} 不该渲染出 img：{rendered}"
        assert "evil.example" not in rendered, rendered


def test_backend_never_emits_a_sourceless_img():
    """bleach 摘掉不合规 src 会留下无源 <img>（浏览器显示碎图标）——必须整块删。

    前端对同一情况给的是文字占位，两边语义要一致。
    """
    from services.safe_markdown_renderer import render_safe_markdown

    rendered = render_safe_markdown("![外部](https://evil.example/px.gif)")
    assert "<img" not in rendered, rendered


def test_tracking_pixel_risk_is_not_reopened():
    """🔴 返修单 §3 约束 3：不得放行任意 http(s) 图片。

    这条单独立锁，因为"把白名单放宽成只要 http(s) 就渲染"是最容易被后人
    顺手做掉的一步（变异 M2 就是它）。
    """
    from services.owned_image_policy import is_owned_image_url

    for url in (
        "https://tracker.example/pixel.gif",
        "http://tracker.example/pixel.gif",
        "https://cdn.jsdelivr.net/a.png",
        "https://site.example.evil.example/uploads/article-images/1/a.jpg",
    ):
        assert not is_owned_image_url(url, site_origin="https://site.example"), url


def test_zero_asset_brand_still_gets_zero_placeholder():
    """锁 4：T4 既有口径（零图品牌 → 全文零占位符）不得因本单回退。"""
    from writing.article_generator_service import (
        _insert_default_image_need_placeholder,
        _no_asset_image_placeholders,
    )

    body = "# 标题\n\n正文第一段。\n\n正文第二段。"
    assert _no_asset_image_placeholders() == []
    assert _insert_default_image_need_placeholder(body, []) == body


def test_frontend_reads_the_same_policy_file_not_a_second_copy():
    """🔴 反漂移：前端实现必须 import 那份 JSON，不许自己抄一份常量。

    这条是**结构锁**不是行为锁 —— 行为锁在前端 Playwright 那边跑。
    它守的是"两边同一份 SSOT"这个约束本身（返修单 §4/§6 明写）。
    """
    ts = (ROOT / "frontend" / "src" / "lib" / "ownedImagePolicy.ts").read_text(encoding="utf-8")
    assert "owned_image_asset_policy.json" in ts, "前端没读共享口径文件"
    assert "policy.path_prefix" in ts and "policy.allowed_schemes" in ts, (
        "前端没用共享口径里的字段，说明它自己抄了一份"
    )
    # 前端不得出现硬编码的第二份前缀字面量
    assert ts.count('"/uploads/article-images/') == 0
    assert ts.count("'/uploads/article-images/") == 0

    safe_markdown = (ROOT / "frontend" / "src" / "components" / "SafeMarkdown.tsx").read_text(
        encoding="utf-8"
    )
    assert "isOwnedImageUrl" in safe_markdown, "SafeMarkdown 没接上判定"
    assert "ownedImagePolicy" in safe_markdown


def test_backend_emits_exactly_the_markdown_the_frontend_probe_renders():
    """端到端衔接锁：后端真吐的那串 markdown，就是取证页渲染的那串。

    不加这条的话，取证页里的 markdown 是我手写的 —— "前端能渲染我写的串"
    证明不了"前端能渲染后端产的串"。这正是上一轮 T4 栽的同一类坑
    （取证走的不是真实那条路）。

    用生产真实资产（brand 662 / asset 217，字段取自只读库）跑真实的
    `render_for_preview`，再断言它产出的 img URL 被自有图库策略放行。
    """
    from unittest import mock

    from services.image_placeholder import render_for_preview
    from services.owned_image_policy import is_owned_image_url

    asset_217 = {
        "id": 217,
        "brand_id": 662,
        "status": "active",
        "publish_allowed": 1,
        "rights_confirmed": 1,
        "public_url": "/uploads/article-images/662/5d3c0f9e8b7a41c2a6e4f1b2c3d4e5f6_safe.jpg",
        "alt_text": "深红色背景上的白色QZQZ品牌Logo，下方标注美学定制及具体产品类型",
        "caption": "QZQZ美学定制品牌标识，涵盖橱柜、衣柜、酒柜、墙板和隐形门等全屋定制业务",
    }
    content = (
        "## 客户品牌概况\n\n"
        '[CLIENT_IMAGE asset_id=217 role=brand_intro caption="QZQZ美学定制品牌标识"]\n\n'
        "正文段落。\n"
    )
    with mock.patch("db.brand_image_assets_db.get_image_asset", return_value=asset_217):
        rendered = render_for_preview(content, brand_id=662)

    # 后端产的是 markdown 图片语法 + 相对 URL（预览口径）
    assert "![" in rendered and asset_217["public_url"] in rendered, rendered
    assert "[CLIENT_IMAGE" not in rendered, rendered
    # 而这个 URL 必须被自有图库策略放行 —— 否则前端照样画不出来
    assert is_owned_image_url(asset_217["public_url"]), asset_217["public_url"]


@pytest.mark.parametrize(
    "url",
    [
        "/uploads/article-images/662/../../../etc/passwd",
        "/uploads/article-images/662/..%2f..%2fetc%2fpasswd",
        "/uploads/article-images//a.jpg",
        "/uploads/article-images/662/sub/dir/a.jpg",
    ],
)
def test_path_traversal_and_nesting_are_rejected(url: str):
    """路径前缀白名单不能被穿越或多层嵌套绕开（前缀匹配的经典漏洞面）。"""
    from services.owned_image_policy import is_owned_image_url

    assert not is_owned_image_url(url, site_origin="https://site.example"), url
