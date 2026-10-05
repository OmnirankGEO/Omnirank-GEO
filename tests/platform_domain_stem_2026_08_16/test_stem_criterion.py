"""[补充单 P0-1/P0-2 2026-08-16] 词干判据 + 名单复检。

判据 = **域名本身能不能唯一确定发布主体**,机械实现 = 除主导主体外还站着几个独立发布者。
旧判据「同一注册域 ≥5 个不同媒体名」把**广告投放位当成了媒体**(china.com 的
「中华网快讯焦点图 / 首发 / 首页文字链」按名字算 ≥5,按主体只有一个)。

夹具全部是**生产真名**(2026-08-16 只读导出),不是手搓的 —— 手搓夹具在这类语义判定上
连续证伪过多次:你按自己的假设造名字,自然就测不出假设本身错在哪。
"""
from __future__ import annotations

import pytest

from services.media_binding_candidates import (
    B_BUCKET_MAINSTREAM_PORTALS,
    SHARED_PLATFORM_DOMAINS,
    STEM_DEMOTED_NON_PLATFORM,
    is_shared_platform_domain,
)
from services.media_name_stem import (
    MIN_NAMES_FOR_JUDGEMENT,
    OUTSIDER_STEM_MIN_DISTINCT,
    STEM_LEN,
    classify_domain,
)

# ── 生产真名夹具(2026-08-16 快照 · 只读导出)──────────────────────────────────
NEWS_CN = [  # 新华社:全「新华网X」,X 是版位
    "新华网上市公司首发（理论）", "新华网主站首发（山东稿）", "新华网大首页文字链",
    "新华网文化产业首页推荐", "新华网频道首页文字链", "新华网首发（学术中国）",
    "新华网首发（政企服务）", "新华网首发（考核专用）稳定", "新华网首发（视频频道）",
    "新华网首发（视频）",
]
CHINA_COM = [  # 中华网:全「中华网X」+ 一个「中华新闻」(同品牌)
    "中华新闻", "中华新闻（焦点图）", "中华新闻（首发）", "中华网会展", "中华网动漫",
    "中华网家电首发", "中华网家电首页文字链", "中华网家电首页焦点图", "中华网快讯焦点图",
    "中华网快讯首发", "中华网快讯首页文字链", "中华网快讯（视频）", "中华网投资",
    "中华网投资（网页）", "中华网科技大首页焦点图", "中华网科技带视频", "中华网通讯",
]
DZWWW = [  # 大众网:大众网X + 海报新闻(同属大众报业)
    "大众网东营", "大众网健康", "大众网健康(GEO)", "大众网健康首发", "大众网家电",
    "大众网新闻", "大众网民生", "大众网济宁首发", "大众网淄博首发", "大众网生活",
    "大众网聊城", "海报新闻健康", "海报新闻",
]
ZHIHU = [  # 知乎:互不相干的独立账号
    "tom新商业", "一财网评", "个人形象讲师安宁", "中国观察站", "中新网（知乎）",
    "传媒观察", "你我他", "健康大学堂", "健康生活管家", "先锋晓讯", "凌幺幺",
    "具身智能风向标", "前沿品牌先知", "千龙网传媒（知乎）",
]
IFENG = [  # 凤凰:凤凰网X(自有频道)+ 独立账号 —— 混合形态,判据必须判平台
    "凤凰网健康", "凤凰网健康首发", "凤凰网区域", "凤凰网区域（不带大风号）",
    "凤凰网区域（包收录）", "凤凰网商业", "凤凰网商业（包收录）", "凤凰网地方（随机）",
    "凤凰网科技", "PChome电脑之家", "中国山东网", "凤凰号深圳", "官方凤凰号（包收录）",
]
NEWS_QQ = [  # 腾讯新闻:入驻媒体,毫无共同词干
    "浙里生活", "延津融媒", "新华报业", "济南时报", "东营网", "六安新周报",
    "太原教育电视台", "河南手机报", "濮阳网",
]
ITOUCHTV = [  # 广东广播电视台一家(粤TV / 触电新闻都是它的产品线)—— 3 字词干会把它切开
    "广东台粤TV客户端（视频）", "广东台触电新闻", "广东台触电新闻（粤精彩）",
    "粤TV客户端（粤眼）", "触电新闻山东首发", "触电新闻广东首发",
    "触电新闻文旅首页", "触电新闻（家庭周报）",
]

ANCHORS = [
    ("news.cn", NEWS_CN, False), ("china.com", CHINA_COM, False), ("dzwww.com", DZWWW, False),
    ("zhihu.com", ZHIHU, True), ("ifeng.com", IFENG, True), ("news.qq.com", NEWS_QQ, True),
]


# ── 校准锚:正向 + 成对反向 ──────────────────────────────────────────────────
@pytest.mark.parametrize("domain,names,expect_platform", ANCHORS)
def test_calibration_anchors(domain, names, expect_platform):
    v = classify_domain(domain, names)
    assert v.is_platform is expect_platform, f"{domain}: {v.reason}"


def test_anchors_cover_both_directions():
    """反向对照的前提:锚里两个方向都得有,否则「全对」可能是恒真。"""
    assert any(e for _, _, e in ANCHORS) and any(not e for _, _, e in ANCHORS)


# ── 🔴 补充单 §1 要求的反向对照:拆掉判据核心,非平台锚必须转回平台 ──────────────
@pytest.mark.parametrize("domain,names", [("news.cn", NEWS_CN), ("china.com", CHINA_COM),
                                          ("dzwww.com", DZWWW)])
def test_removing_stem_grouping_flips_non_platform_anchors(domain, names):
    """把「归并主体」拆掉 = 退回旧判据「数不同媒体名」⇒ 这三个立刻被误判成平台。
    这就是本次要修的病,也是判据在干活的证据。"""
    assert classify_domain(domain, names).is_platform is False
    assert classify_domain(domain, names, group_by_stem=False).is_platform is True


@pytest.mark.parametrize("domain,names", [("zhihu.com", ZHIHU), ("news.qq.com", NEWS_QQ)])
def test_platform_anchors_are_platform_under_both_criteria(domain, names):
    """成对的另一半:真平台在新旧判据下都是平台 —— 证明上面那条翻转不是「所有域都翻」。"""
    assert classify_domain(domain, names).is_platform is True
    assert classify_domain(domain, names, group_by_stem=False).is_platform is True


# ── 阈值 / 词干长敏感性:证明现值不是骑在悬崖边上 ──────────────────────────────
def test_threshold_sensitivity():
    """阈值 2/3/4 六锚全对(窗口宽 3 档);1 会把 china.com / dzwww.com 误判平台。"""
    for t in (2, 3, 4):
        for domain, names, expect in ANCHORS:
            v = classify_domain(domain, names, outsider_stem_min_distinct=t)
            assert v.is_platform is expect, f"阈值 {t} 下 {domain} 判错:{v.reason}"
    bad = [d for d, n, e in ANCHORS
           if classify_domain(d, n, outsider_stem_min_distinct=1).is_platform is not e]
    assert set(bad) == {"china.com", "dzwww.com"}, f"阈值 1 的失效面变了:{bad}"
    assert 2 <= OUTSIDER_STEM_MIN_DISTINCT <= 4


def test_stem_len_sensitivity():
    """词干长 2/3 六锚全对;4 会把「新华网上市公司」「新华网大首页」当成两个主体。"""
    for sl in (2, 3):
        for domain, names, expect in ANCHORS:
            assert classify_domain(domain, names, stem_len=sl).is_platform is expect, \
                f"词干长 {sl} 下 {domain} 判错"
    bad = [d for d, n, e in ANCHORS if classify_domain(d, n, stem_len=4).is_platform is not e]
    assert set(bad) == {"news.cn", "china.com", "dzwww.com"}, f"词干长 4 的失效面变了:{bad}"
    assert STEM_LEN == 3


def test_threshold_3_fixes_the_local_media_group_false_positive():
    """P0-4 抽样发现的假阳性形态:3 字词干把同一地方媒体集团切开。
    阈值 2 会把广东台(粤TV/触电新闻)误判平台,阈值 3 修好 —— 这就是 2→3 的依据。"""
    assert classify_domain("itouchtv.cn", ITOUCHTV, outsider_stem_min_distinct=2).is_platform is True
    assert classify_domain("itouchtv.cn", ITOUCHTV).is_platform is False


# ── 样本不足:不判平台,交人工 ────────────────────────────────────────────────
def test_too_few_names_is_never_platform():
    v = classify_domain("tiny.example.com", ["甲媒体", "乙媒体"])
    assert v.is_platform is False and "样本不足" in v.reason
    # 成对:补够样本且互不相干,立刻判平台(证明上面不是「小域永远非平台」)
    v2 = classify_domain("tiny.example.com", ["甲媒体", "乙媒体", "丙资讯", "丁观察", "戊快报"])
    assert v2.is_platform is True


def test_empty_input_is_not_platform():
    assert classify_domain("x.com", []).is_platform is False


# ── 名单三集合:互不相交 + 降级项确实不再判平台 ──────────────────────────────
def test_three_buckets_are_pairwise_disjoint():
    assert not (SHARED_PLATFORM_DOMAINS & B_BUCKET_MAINSTREAM_PORTALS)
    assert not (SHARED_PLATFORM_DOMAINS & STEM_DEMOTED_NON_PLATFORM)
    assert not (B_BUCKET_MAINSTREAM_PORTALS & STEM_DEMOTED_NON_PLATFORM)


@pytest.mark.parametrize("domain", sorted(STEM_DEMOTED_NON_PLATFORM))
def test_demoted_domains_are_no_longer_shared_platforms(domain):
    """P0-2 复检降级的域:必须真的不再要求名称证据(不只是从集合里删掉)。"""
    assert is_shared_platform_domain(domain) is False
    assert is_shared_platform_domain(f"sub.{domain}") is False


def test_demotions_did_not_touch_the_owner_pending_b_bucket():
    """🔴 回归锁:Owner 正在过目的 B 桶仍是 20 项,内容一字未改 ——
    降级项走 STEM_DEMOTED_NON_PLATFORM,不许混进去把清单身份搅糊。"""
    assert len(B_BUCKET_MAINSTREAM_PORTALS) == 20
    assert {"china.com", "dzwww.com", "news.cn"} <= B_BUCKET_MAINSTREAM_PORTALS


@pytest.mark.parametrize("domain", ["china.com", "dzwww.com"])
def test_owner_decided_domains_stay_non_platform(domain):
    """🔴 Owner 2026-08-16 明确拍板这两个留 B 桶,这是回归锁。"""
    assert is_shared_platform_domain(domain) is False
    assert domain in B_BUCKET_MAINSTREAM_PORTALS


# ── 平台锚仍然要求名称证据(判据接到真链路上)──────────────────────────────────
@pytest.mark.parametrize("domain", ["blog.csdn.net", "c.m.163.com", "news.qq.com",
                                    "zhuanlan.zhihu.com", "post.m.smzdm.com"])
def test_platform_subdomains_still_shared_after_recheck(domain):
    """P0-2 复检不许把上一轮修好的东西改坏。"""
    assert is_shared_platform_domain(domain) is True


def test_min_names_constant_is_the_one_used():
    assert MIN_NAMES_FOR_JUDGEMENT == 5
    assert classify_domain("x.com", ["甲", "乙", "丙", "丁"]).is_platform is False


# ── [根治尝试 2026-08-16] jia.com 手工降级 · 硬出口 ──────────────────────────
def test_jia_com_is_not_a_shared_platform():
    """🔴 本单硬出口:`jia.com` 必须判非平台。

    它是**手工降级** —— 词干判据至今仍判它平台,而且这不是判据没写好:
    `jia.com` 与 `smzdm.com` 的名字形态完全一样(前缀各异 + 尾部同一个括号词),
    区别只在现实事实(`(齐家网)` 是发布主体 / `(什么值得买)` 是托管平台)。
    名字里没有能区分两者的信息,任何基于尾括号的规则修好一个必然弄坏另一个。
    """
    assert is_shared_platform_domain("jia.com") is False
    assert is_shared_platform_domain("www.jia.com") is False
    assert "jia.com" in STEM_DEMOTED_NON_PLATFORM
    assert "jia.com" not in SHARED_PLATFORM_DOMAINS


def test_smzdm_stays_platform_while_jia_is_demoted():
    """🔴 成对反向 —— 这一条是整件事的要害:
    降级 jia.com 绝不能连带把同形态的 smzdm.com 弄成非平台(645 条候选 vs 14 条)。
    任何「从尾部括号推主体」的规则都会在这里转红。"""
    assert is_shared_platform_domain("smzdm.com") is True
    assert is_shared_platform_domain("post.smzdm.com") is True
    # 两者名字形态同构的可执行留档
    jia = ["GEO家居排名分析（齐家网）", "家居看点（齐家网）", "齐家商讯（齐家网）",
           "齐家网APP", "齐家网家居频道推荐", "齐家网（家居）", "家居GEO好物分享（齐家网）"]
    smzdm = ["品牌导向（什么值得买）", "消浪指南针（什么值得买）", "化妆品推荐官（什么值得买）",
             "天天上好货V（什么值得买）", "灰度认知社(什么值得买)", "球长好物志(什么值得买)"]
    assert classify_domain("jia.com", jia).is_platform is True, "判据本身仍判 jia 平台(所以才需要手工降级)"
    assert classify_domain("smzdm.com", smzdm).is_platform is True
    # ⇒ 判据对两者给出**相同**结论 ⇒ 只能靠名单区分,这就是手工降级的理由


def test_demotion_layer_has_discriminating_power():
    """🔴 反向对照:拆掉「降级集合」这一层 → 至少一个域的判定必须翻转。
    翻转 0 个就说明这一层是装饰(和上一轮删去噪同一条纪律)。"""
    from services.citation_domain_weights import normalize_domain as registrable_domain
    from services.media_binding_candidates import (
        SHARED_PLATFORM_DOMAINS as A, STEM_DEMOTED_NON_PLATFORM as D)
    from services.media_entity_flywheel import normalize_domain as host_domain

    def without_demotion(domain: str) -> bool:
        """把降级项塞回 A 桶 = 拆掉这一层。"""
        return registrable_domain(host_domain(domain)) in (A | D)

    flipped = [d for d in sorted(D) if without_demotion(d) is not is_shared_platform_domain(d)]
    assert len(flipped) >= 1, "拆掉降级层没有任何域翻转 ⇒ 这一层没有判别力,应删掉"
    assert set(flipped) == set(D), f"降级项应全部翻转,实得 {flipped}"
    assert "jia.com" in flipped
