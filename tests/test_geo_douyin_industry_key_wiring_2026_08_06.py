# -*- coding: utf-8 -*-
"""`industry_key` 全链接线锁 · WO_GEO_DOUYIN_RANKING_TEMPLATES_2026-08-06 v3 §6.1-1

背景:后端 `CreatePostRequest.industry_key` 一直收着、蒸馏那一跳一直在传,
**断的只有付费创建这一跳**。后果是每条真金白银的内容都走 `general` 档
(钩子兜底 / 分不出 B·C 端 / 配色 fallback),榜单的面×行业路由也会恒走默认版式。

🔴 本文件同时锁**依赖数组** —— 同一个文件里 2026-08-04 已经因为
`useCallback` 依赖不全把画幅选择器变成摆设一次(闭包锁死首渲染的空值,
**全程不报错**)。补传而不进依赖数组 = 补了等于没补。
"""
from __future__ import annotations

import pathlib
import re

import pytest


REPO = pathlib.Path(__file__).resolve().parent.parent
# [WO_271 · 2026-09-23] 旧入口页 DouyinImagePost.tsx 已删(6b491ab23)。付费创建与蒸馏两跳搬到
# 选题面板,整批下单的请求体由 imageNoteProduction.productionBatch 拼;判据改指这两处、按后继重判。
WRITING = REPO / "frontend" / "src" / "pages" / "Writing"
TSX = WRITING / "ImageNoteTopicPanel.tsx"
PRODUCTION = WRITING / "imageNoteProduction.ts"
API = REPO / "api" / "geo_douyin_api.py"


def _strip_comments(src: str) -> str:
    """剥 // 与 /* */ 注释 —— 否则锚点会撞到我自己写的说明,判据永远绿。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"//.*", "", src)


@pytest.fixture(scope="module")
def tsx_code() -> str:
    return _strip_comments(TSX.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def production_code() -> str:
    return _strip_comments(PRODUCTION.read_text(encoding="utf-8"))


def _produce_call_body(code: str) -> str:
    """取付费创建那一跳的请求体。

    [WO_271] 后继是整批下单(`/api/geo-douyin/posts/batch`),请求体由
    `imageNoteProduction.productionBatch` 拼 —— 面板那一跳只是把它 JSON.stringify 出去。
    所以这里取的是 productionBatch 的函数体。
    """
    idx = code.find("export function productionBatch(")
    assert idx >= 0, "找不到付费创建的请求体 —— 判据失效,先修判据"
    return code[idx: idx + 1200]


def test_paid_creation_sends_industry_key(tsx_code, production_code):
    body = _produce_call_body(production_code)
    assert "industry_key" in body, (
        "付费创建调用没传 industry_key —— 每条内容都会走 general 档")
    # [WO_271] 面板那一跳确实是把这份请求体发出去,不是另拼一份
    assert "'/api/geo-douyin/posts/batch'" in tsx_code
    assert "body: JSON.stringify(productionBatch(" in tsx_code, "付费创建没用 productionBatch 拼的请求体"


def test_industry_key_value_is_not_hardcoded(tsx_code, production_code):
    """反向对照:传的必须是真实值,不能是写死的空串/'general' 糊弄判据。"""
    body = _produce_call_body(production_code)
    m = re.search(r"industry_key\s*:\s*([A-Za-z_$][\w$.?\[\]']*)", body)
    assert m, f"industry_key 不是变量引用:{body[:200]}"
    assert m.group(1) not in ("''", '""', "'general'"), m.group(1)
    # [WO_271] 后继传的是 settings.industry:它必须来自当前客户的档案,不是面板里写死的值
    assert m.group(1) == "settings.industry", f"industry_key 的来源变了:{m.group(1)} —— 下面的来源判据要跟着改"
    assert re.search(r"industry:\s*clientContext\?\.brand\.id === brandId \? clientContext\.brand\.industry", tsx_code), \
        "settings.industry 不是取自当前客户的档案"


def test_industry_key_source_is_in_the_dependency_array(tsx_code):
    """🔴 补传的变量必须进 useCallback 依赖数组,否则被闭包锁死(2026-08-04 同款坑)。

    [WO_271] 后继的来源是 `settings`(useMemo),行业取自 clientContext:锁它的依赖数组;
    下单函数 doStart 是每次渲染新建的普通函数,读到的就是当次的 settings —— 它要是改成
    useCallback,这里必须跟着锁它的依赖数组,所以把它的形态也钉住。
    """
    m = re.search(r"const settings = useMemo\(\(\) => \(\{(.*?)\}\),\s*\[([^\]]*)\]\);", tsx_code, flags=re.S)
    assert m, "找不到 settings 的 useMemo —— 判据失效"
    assert "industry:" in m.group(1) and "clientContext" in m.group(1), "settings 里的行业不是取自 clientContext"
    deps = {d.strip() for d in m.group(2).split(",") if d.strip()}
    assert {"clientContext", "brandId"} <= deps, \
        f"行业来源不在依赖数组 {deps} 里 —— 会被闭包锁死在首渲染的空值"
    assert "const doStart = async () =>" in tsx_code, \
        "下单函数的形态变了 —— 改成 useCallback 的话,它的依赖数组也要锁"


def _distill_industry_problem(code: str) -> str:
    """蒸馏请求体里的行业有问题 ⇒ 返回一句描述;没问题返回空串。

    问题 = 丢了 / 写成字面量 / 取值不是 `settings.industry`(下单体用的就是它:同一个客户档案的行业原文,一处取值)。
    """
    idx = code.find("'/api/geo-douyin/distill-topics'")
    if idx <= 0:
        return "找不到蒸馏调用点 —— 判据失效,先修判据"
    block = code[idx: idx + 500]
    m = re.search(r"industry_key\s*:\s*([A-Za-z_$][\w$.?\[\]]*)", block)
    if not m:
        return f"蒸馏调用的 industry_key 丢了或被写成字面量:{block[:200]}"
    if m.group(1) != "settings.industry":
        return f"蒸馏的行业不是 settings.industry(与下单体同源),而是 {m.group(1)}"
    return ""


def test_distill_call_still_sends_industry_key(tsx_code):
    """反向对照:蒸馏那一跳本来就在传,不许被这次改动弄丢。

    🔴 这里**不能**只查「'industry_key' 这个串在不在」—— 那是"某处有 X"型弱锁,
    把值换成 `'general'` 字面量照样绿。实测:变异 M2 就是从这个洞里活过去的。

    [WO_271] 改指选题面板:后继的蒸馏请求体只有 `{ brand_id: brandId }`,行业丢了 ——
    后端 `api_distill_topics` 于是恒走 "general"。
    [WO_282-F1 已修] 请求体带 `industry_key: settings.industry`,与下单体、与旧页同源
    (客户档案的行业原文)。后端拿它精确匹配语料、语料用的是飞轮枚举 key,那一半由 C 在
    蒸馏入口归一(WO_282-C),不在前端另写一份归一。
    """
    problem = _distill_industry_problem(tsx_code)
    assert not problem, problem


def test_distill_industry_lock_has_power(tsx_code):
    """反臂(WO_282):去掉蒸馏请求体里的行业 / 写成 'general' 字面量 / 换一个来源 ⇒ 上一格必须命中;
    现役源码不命中(对照)。"""
    assert _distill_industry_problem(tsx_code) == ""
    body = "body: JSON.stringify({ brand_id: brandId, industry_key: settings.industry }),"
    assert tsx_code.count(body) == 1, "对照前提:蒸馏请求体恰好一处"
    for poison in ("body: JSON.stringify({ brand_id: brandId }),",
                   "body: JSON.stringify({ brand_id: brandId, industry_key: 'general' }),",
                   "body: JSON.stringify({ brand_id: brandId, industry_key: clientContext?.brand.industry_category }),"):
        assert _distill_industry_problem(tsx_code.replace(body, poison)), f"换成 {poison!r} 却没报"


def test_backend_accepts_industry_key_on_create():
    """后端契约面:`CreatePostRequest` 必须有这个字段(它一直有,锁住别被删)。"""
    src = API.read_text(encoding="utf-8")
    i = src.find("class CreatePostRequest")
    assert i > 0
    block = src[i: i + 1200]
    assert re.search(r"^\s*industry_key\s*:", block, re.M), "后端不收 industry_key 了"


# ===========================================================================
# 🔴 2026-08-08 补:接线接对了,**键空间没对** —— 上面那半修完之后仍然失效
#
# 本文件开头写的目标是「让钩子/B·C端/配色按行业生效」。前端确实开始传了,
# 但传的是**品牌档案里的自由文本**(生产 `geo_douyin_posts.id=20` 实值:
# 「家居制造业 / 高端整木全屋定制/木作高定行业(住宅室内木作整装…)」),
# 而 `card_templates` 四张表用的是**受控枚举**。于是 `.get(自由文本)` 一律落兜底 ——
# 和"根本不传"的结果**逐字相同**,而且同样一声不响。
#
# 判据用**生产实值**做样本,不用我编的样例;每条配成对反向对照。
# ===========================================================================

#: 逐字复制自生产 `geo_douyin_posts.id=20`(2026-08-08 只读取证)。
PROD_FREE_TEXT = (
    "家居制造业 / 高端整木全屋定制/木作高定行业"
    "（住宅室内木作整装、实木定制家具设计生产安装一体化服务）"
)


def test_the_sample_really_needs_normalizing():
    """判别力自检:样本若本来就等于枚举键,下面几条全是恒真。"""
    from services.media_entity_flywheel import normalize_industry_key
    assert normalize_industry_key(PROD_FREE_TEXT) == "home_improvement"
    assert PROD_FREE_TEXT != "home_improvement"


def test_palette_follows_the_industry_not_the_fallback():
    from services.geo_douyin.card_templates import industry_palette, _PALETTE_FALLBACK
    got = industry_palette(PROD_FREE_TEXT)
    assert got == industry_palette("home_improvement"), got
    # 反向面:不认识的行业**仍然**走兜底(不许硬塞进某个行业)
    assert industry_palette("般若波罗蜜多") == _PALETTE_FALLBACK


def test_b_side_detection_follows_the_industry():
    from services.geo_douyin.card_templates import is_b_side
    # 别名表里的行业:自由文本写法也必须判对
    assert is_b_side("我们是做企业服务的") is True        # → business_service
    assert is_b_side("软件与互联网") is True              # → technology
    assert is_b_side("business_service") is True          # 幂等
    # 反向面:家装是 C 端,不许被误判成 B 端
    assert is_b_side(PROD_FREE_TEXT) is False
    assert is_b_side("") is False


def test_known_residual_pool_only_keys_still_need_exact_text():
    """🔴 如实记录本次**没有**解决的那一半,免得下一个人以为归一是全覆盖。

    `normalize_industry_key` 的别名表只有 13 个行业;候选池里另外 4 个中文键
    (`电梯行业` / `时尚美妆` / `母婴亲子` / `文娱游戏` / `geo_优化服务`)**不在别名表里**,
    只有品牌行业文本与它逐字相同才命中 —— 「深圳电梯行业整梯供应」仍然对不上。

    没往别名表加词,是因为那张表被飞轮等多处共用,加键是全局影响,不该在本包里拍。
    留成残留 + 另立单,比悄悄扩表安全。
    """
    from services.geo_douyin.card_templates import is_b_side
    assert is_b_side("电梯行业") is True                   # 逐字相同 → 命中
    assert is_b_side("深圳电梯行业整梯与配件供应") is False  # 自由文本 → 仍然对不上


def test_hook_follows_the_industry():
    from services.geo_douyin.card_templates import pick_hook, DEFAULT_HOOK
    assert pick_hook("教育培训机构招生") == "question"      # education → 疑问式
    assert pick_hook("education") == "question"
    # 反向面:不在表里的行业仍走避坑式兜底
    assert pick_hook(PROD_FREE_TEXT) == DEFAULT_HOOK
    assert pick_hook("般若波罗蜜多") == DEFAULT_HOOK


def test_every_industry_lookup_goes_through_the_normalizer():
    """形态面:四张表的查表处**一处都不许**再用裸 `.strip()` 当键。

    漏一处就等于那一项继续走兜底,而且照旧不报错。
    """
    import ast
    import pathlib as _p
    src = (_p.Path(__file__).resolve().parent.parent
           / "services" / "geo_douyin" / "card_templates.py").read_text(encoding="utf-8")
    code = ast.unparse(ast.parse(src))          # 剥注释,免得撞到上面那段说明
    for bad in ('INDUSTRY_HOOK.get(str(industry_key',
                '_INDUSTRY_PALETTE.get(str(industry_key',
                'str(industry_key or "").strip() in _B_SIDE_INDUSTRIES'):
        assert bad not in code, f"还有裸查表:{bad}"
    assert code.count("_ikey(industry_key)") >= 3, "归一跳数不够,漏了查表处"
