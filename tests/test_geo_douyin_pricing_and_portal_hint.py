"""Owner 2026-08-02 两项拍板的锁

① 制作费 = **390 算力/条**(Owner 08-02 二次调价 260→390,对齐 article_gen)
   重新生成 = **260 算力/条**(对齐 article_rewrite)
② PortalDashboard 泛化文案挂 HelpHint(Owner 点头挂)

锁的意义:这两个都是"Owner 拍过板"的数,被人顺手改回去时要立刻转红。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PRICING_SQL = ROOT / "db" / "migration_019_geo_douyin_pricing_390_2026_08_02.sql"
PORTAL = ROOT / "frontend" / "src" / "pages" / "Portal" / "PortalDashboard.tsx"
CONFIG = ROOT / "services" / "geo_douyin" / "config.py"


# ─────────────────────────────────────────────────────────────
# ① 制作费 390 + 重新生成 260(Owner 拍板值)
# ─────────────────────────────────────────────────────────────
def test_pricing_migration_exists():
    assert PRICING_SQL.exists(), "缺制作费价目迁移"


def _sql_without_comments() -> str:
    """剥掉 `--` 注释后的 SQL。

    🔴 扫描锁必须先剥注释:本文件顶部的说明里就写着「Safe to run repeatedly
       (ON CONFLICT DO UPDATE)」—— 不剥注释的话,把真正的 ON CONFLICT 子句整条删掉,
       断言照样绿(实测该变异存活)。
    """
    lines = []
    for line in PRICING_SQL.read_text(encoding="utf-8").splitlines():
        idx = line.find("--")
        lines.append(line if idx < 0 else line[:idx])
    return "\n".join(lines).upper()


def _price_row(code: str):
    """从迁移 SQL 里取某个 feature_code 的 (points, compute, requires_paid)。"""
    sql = PRICING_SQL.read_text(encoding="utf-8")
    m = re.search(
        r"VALUES\s*\(\s*'" + re.escape(code) +
        r"'\s*,\s*'[^']*'\s*,\s*(\d+)\s*,\s*([\d.]+)\s*,\s*(TRUE|FALSE)",
        sql, re.I)
    assert m, f"取不到 {code} 的定价三元组 —— 锚点失效"
    return int(m.group(1)), float(m.group(2)), m.group(3).upper()


def test_first_generation_is_390_points():
    """Owner 08-02 拍板 390(对齐 article_gen)。改成别的数必须转红。"""
    points, compute, requires_paid = _price_row("geo_douyin_image_post")
    assert points == 390, f"Owner 拍板 390 算力,实得 {points}"
    assert compute == 3.00, f"应对齐 article_gen 的 3.00,实得 {compute}"
    assert requires_paid == "FALSE", (
        "生成类特性惯例是 FALSE(允许赠送积分抵扣);"
        "TRUE 是发布费 media_proxy_publish 的口径,别搞混")


def test_regenerate_is_260_points():
    """重新生成走便宜一档 260(对齐 article_rewrite)。"""
    points, compute, requires_paid = _price_row("geo_douyin_image_post_regen")
    assert points == 260, f"重新生成应为 260,实得 {points}"
    assert compute == 3.00, f"应对齐 article_rewrite 的 3.00,实得 {compute}"
    assert requires_paid == "FALSE"


def test_regen_is_cheaper_than_first_generation():
    """两档的**相对关系**才是真机制:重做必须比首次便宜,否则用户没有重做的理由。"""
    first, _, _ = _price_row("geo_douyin_image_post")
    regen, _, _ = _price_row("geo_douyin_image_post_regen")
    assert regen < first, f"重新生成({regen})必须便宜于首次({first})"


def test_cost_compute_is_not_assumed_to_be_points_over_130():
    """🔴 反向面 · 订正我自己写过的假不变量。

    我在 018 里断言过 `cost_compute == cost_points/130`,那是**错的** ——
    生产实测反例:article_rewrite 260/3.00、report_regen 130/2.00 都不满足。
    该字段语义未经验证,只能按同类条目照抄,不能按公式推。
    这条锁把"别再重新引入那个公式"钉死。
    """
    # 🔴 必须剥注释再断言(第五次踩同一个坑):迁移顶部那段解释里就写着
    #    "它并不等于 cost_points/130",不剥注释的话这条锁会命中自己的说明文字。
    assert "COST_POINTS/130" not in _sql_without_comments().replace(" ", "")
    # 两条都用 3.00(照抄 article_gen / article_rewrite),而非 points/130 推出来的值
    assert _price_row("geo_douyin_image_post")[1] == 3.00
    assert _price_row("geo_douyin_image_post_regen")[1] == 3.00


def test_pricing_is_idempotent():
    """价目迁移必须可重复跑(ON CONFLICT),否则重跑会炸。"""
    sql = _sql_without_comments()
    assert "ON CONFLICT" in sql, "缺 ON CONFLICT,迁移不可重复跑"
    assert "DO UPDATE" in sql, "缺 DO UPDATE,冲突时不会更新为拍板价"


def test_comment_stripper_has_power():
    """必须不命中面:注释里的 ON CONFLICT 不能被算数。"""
    fake = "-- Safe to run repeatedly(ON CONFLICT DO UPDATE)\nINSERT INTO t VALUES (1);"
    stripped = "\n".join(
        (l if l.find("--") < 0 else l[:l.find("--")]) for l in fake.splitlines()).upper()
    assert "ON CONFLICT" not in stripped, "剥注释没生效 = 恒真锁"


def test_feature_code_matches_code_constant():
    """迁移里的 feature_code 必须与代码常量一致 —— 对不上会 raise 未知功能编码。"""
    sql = PRICING_SQL.read_text(encoding="utf-8")
    cfg = CONFIG.read_text(encoding="utf-8")
    assert "'geo_douyin_image_post'" in sql
    assert 'FEATURE_CODE_IMAGE_POST = "geo_douyin_image_post"' in cfg, (
        "config 里的 feature_code 常量与迁移不一致")


def test_pricing_lock_has_discriminating_power():
    """必须不命中面:把 260 换成别的数,同一套正则必须解析出不同值。"""
    fake = ("INSERT INTO feature_pricing (feature_code, feature_name, cost_points, "
            "cost_compute, requires_paid_points)\n"
            "VALUES ('geo_douyin_image_post', 'x', 130, 1.00, FALSE)")
    m = re.search(
        r"VALUES\s*\(\s*'geo_douyin_image_post'\s*,\s*'[^']*'\s*,\s*(\d+)", fake, re.I)
    assert m and int(m.group(1)) == 130, "正则抓不到被改动的定价 = 恒真锁"


# ─────────────────────────────────────────────────────────────
# ② PortalDashboard 泛化文案(Owner 点头挂)
# ─────────────────────────────────────────────────────────────
def _portal() -> str:
    return PORTAL.read_text(encoding="utf-8")


def test_portal_has_generalization_notice():
    """Owner 点头的那句话必须在,且挂在 HelpHint 里(不占版面)。"""
    src = _portal()
    assert "AI 平台普遍存在个性化推荐" in src, "泛化文案缺失"
    assert "不同用户实际看到的结果可能有所差异" in src, "泛化文案缺失后半句"
    assert "<HelpHint" in src, "文案没挂 HelpHint"


def test_portal_hint_is_imported():
    """挂了组件却没 import = 白屏。"""
    src = _portal()
    assert "from '@/components/onboarding/HelpHint'" in src, "HelpHint 没 import"


def test_notice_attached_to_detection_rate_card():
    """必须挂在【平均出现率】那张卡里 —— 挂到别处等于没解决"客户拿这个数去实测"的场景。

    🔴 判据打在真结构上,不用"字符距离 < N"那种拍脑袋阈值:
       第一处「平均出现率」其实是上面的注释块,按距离算会被注释长度带偏(实测差 1070)。
       真机制 = 提示必须夹在【标签文本】与【该卡渲染的数值表达式】之间。
    """
    src = _portal()
    i_hint = src.index("AI 平台普遍存在个性化推荐")
    # 该卡真正渲染数值的表达式,是这张卡的唯一身份标识
    i_value = src.index("data?.avg_detection_rate ?? 0")
    assert i_hint < i_value, "提示不在平均出现率数值之前 —— 没挂在这张卡里"

    between = src[i_hint:i_value]
    assert "平均出现率" not in between, (
        "提示与数值之间又出现了一次「平均出现率」,说明挂到了上一张卡")
    # 标签与提示必须在同一个 <p> 里(标签行内挂 HelpHint)
    label_block = src[:i_hint]
    assert label_block.rstrip().endswith(">") or "平均出现率" in label_block[-400:], (
        "提示没有紧跟在标签之后")


def test_notice_does_not_expose_percentage_talk_track():
    """元指令 11:面向非代理不裸露 SOV/出现率话术层 —— 这段文案本身不得引入话术百分比。"""
    src = _portal()
    seg_start = src.index("AI 平台普遍存在个性化推荐")
    seg = src[seg_start:seg_start + 600]
    assert "SOV" not in seg, "泛化文案里出现了 SOV"


def test_portal_notice_lock_has_power():
    """必须不命中面:删掉文案后同一断言必须失败。"""
    fake = "<p>平均出现率</p>"
    assert "AI 平台普遍存在个性化推荐" not in fake


# ─────────────────────────────────────────────────────────────
# 两档必须各走各的(端点级)
# ─────────────────────────────────────────────────────────────
def _fn_src(name: str) -> str:
    import ast as _ast
    api = (ROOT / "api" / "geo_douyin_api.py").read_text(encoding="utf-8")
    tree = _ast.parse(api)
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.AsyncFunctionDef) and n.name == name)
    return _ast.unparse(fn)


def test_regen_endpoint_charges_regen_tier():
    """🔴 重新生成端点必须真的传便宜那一档,否则用户重做被按首次价 390 扣。

    断言用 `feature_code=X` 的**完整赋值形式**,不用裸标识符 ——
    `FEATURE_CODE_IMAGE_POST` 是 `FEATURE_CODE_IMAGE_POST_REGEN` 的前缀,
    裸串判断会互相误命中(实测:该变异因此存活)。
    """
    body = _fn_src("api_regenerate_post")
    assert "feature_code=FEATURE_CODE_IMAGE_POST_REGEN" in body, "重做端点没走便宜档"
    assert "feature_code=FEATURE_CODE_IMAGE_POST)" not in body, "重做端点误传了首次档"
    assert "feature_code=FEATURE_CODE_IMAGE_POST," not in body, "重做端点误传了首次档"


def test_first_generation_endpoint_charges_first_tier():
    """反向面:首次制作端点必须走首次档,不能两条路都走便宜档。"""
    body = _fn_src("api_create_and_produce")
    assert "feature_code=FEATURE_CODE_IMAGE_POST" in body
    assert "FEATURE_CODE_IMAGE_POST_REGEN" not in body, "首次制作误走了便宜档"


def test_both_feature_codes_match_code_constants():
    """迁移里的两个 feature_code 必须与代码常量一致 —— 对不上会 raise 未知功能编码。"""
    cfg = CONFIG.read_text(encoding="utf-8")
    assert 'FEATURE_CODE_IMAGE_POST = "geo_douyin_image_post"' in cfg
    assert 'FEATURE_CODE_IMAGE_POST_REGEN = "geo_douyin_image_post_regen"' in cfg
    sql = PRICING_SQL.read_text(encoding="utf-8")
    assert "'geo_douyin_image_post'" in sql and "'geo_douyin_image_post_regen'" in sql
