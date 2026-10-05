from writing.article_generator_service import _insert_default_image_need_placeholder
from writing.production_style_v09 import compose_r6_v09_default_prompt
from services.article_image_selector import select_images_for_article

import re


def test_r6_v09_prompt_keeps_customer_image_placeholder_rule():
    prompt = compose_r6_v09_default_prompt("ranking_v2", "legacy details")

    assert "[NEED_IMAGE role=X purpose=Y]" in prompt
    assert "只使用客户已确认可发布的真实图片" in prompt
    assert "不要写 Markdown 图片" in prompt
    assert "每篇最多 2 张" in prompt


def test_backend_inserts_safe_image_need_placeholder_when_model_omits_it():
    content = "# 2026年深圳装修公司推荐\n\n这是一篇介绍客户品牌能力的文章。"
    assets = [{"id": 101, "image_type": "case", "usage_scenarios": ["case_proof"]}]

    output = _insert_default_image_need_placeholder(content, assets)

    assert "[NEED_IMAGE role=case purpose=客户案例或资质图片]" in output
    assert output.startswith("# 2026年深圳装修公司推荐")


def test_backend_does_not_insert_placeholder_without_publishable_assets_or_when_existing_marker():
    content = "# 标题\n\n正文。"

    assert _insert_default_image_need_placeholder(content, []) == content

    existing = "# 标题\n\n[NEED_IMAGE role=hero purpose=品牌形象]\n\n正文。"
    assets = [{"id": 102, "image_type": "storefront", "usage_scenarios": ["brand_intro"]}]
    assert _insert_default_image_need_placeholder(existing, assets) == existing


def test_article_image_selector_rotates_assets_across_article_keys(monkeypatch):
    assets = [
        {
            "id": idx,
            "image_type": "case",
            "usage_scenarios": ["case_proof"],
            "caption": f"客户案例图 {idx}",
        }
        for idx in range(1, 6)
    ]
    monkeypatch.setattr(
        "db.brand_image_assets_db.list_publishable_assets",
        lambda brand_id: assets,
    )
    content = "# 标题\n\n[NEED_IMAGE role=case purpose=客户案例]\n\n正文。"

    picked_ids = []
    for idx in range(12):
        output = select_images_for_article(content, 615, article_key=f"topic-{idx}")
        match = re.search(r"asset_id=(\d+)", output)
        assert match
        picked_ids.append(int(match.group(1)))

    assert len(set(picked_ids)) >= 4
    assert select_images_for_article(content, 615, article_key="topic-3") == select_images_for_article(
        content,
        615,
        article_key="topic-3",
    )


def test_brand_logo_is_placed_in_customer_section_not_after_title(monkeypatch):
    monkeypatch.setattr(
        "db.brand_image_assets_db.list_publishable_assets",
        lambda brand_id: [
            {
                "id": 194,
                "image_type": "logo",
                "usage_scenarios": ["brand_intro"],
                "caption": "全域上榜品牌标识",
            }
        ],
    )
    content = (
        "# 2026年GEO服务商TOP10推荐榜单\n\n"
        "[NEED_IMAGE role=brand_intro purpose=客户品牌形象]\n\n"
        "这是一段行业背景导语,不应该在这里展示客户 LOGO。\n\n"
        "## 1. 全域上榜 GEO 引擎\n\n"
        "全域上榜是一套面向品牌 AI 搜索优化的服务。\n\n"
        "## 2. 其他服务商\n\n"
        "这里是竞品内容。"
    )

    output = select_images_for_article(content, 208, article_key="t1", brand_name="全域上榜")

    marker = "[CLIENT_IMAGE asset_id=194"
    assert marker in output
    assert output.index(marker) > output.index("## 1. 全域上榜 GEO 引擎")
    assert output.index(marker) < output.index("## 2. 其他服务商")
    assert output.index(marker) > output.index("这是一段行业背景导语")


def test_case_image_is_placed_near_case_section_not_after_title(monkeypatch):
    monkeypatch.setattr(
        "db.brand_image_assets_db.list_publishable_assets",
        lambda brand_id: [
            {
                "id": 188,
                "image_type": "case",
                "usage_scenarios": ["case_proof"],
                "caption": "复式客厅设计案例",
            }
        ],
    )
    content = (
        "# 2026年深圳装修公司推荐\n\n"
        "[NEED_IMAGE role=case purpose=客户案例]\n\n"
        "深圳装修市场选择很多,开头先讲筛选方法。\n\n"
        "## 成功案例与交付效果\n\n"
        "栖舍设计在复式住宅项目中展示了设计施工一体化能力。"
    )

    output = select_images_for_article(content, 615, article_key="t2", brand_name="栖舍设计")

    marker = "[CLIENT_IMAGE asset_id=188"
    assert marker in output
    assert output.index(marker) > output.index("## 成功案例与交付效果")
    assert output.index(marker) > output.index("深圳装修市场选择很多")
