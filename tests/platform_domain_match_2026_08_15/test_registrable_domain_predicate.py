"""[P0-1/P0-2 2026-08-15] 平台域判定改注册域口径 + 名单扩充。

根因(生产实证):仓内**同名两版** `normalize_domain`——
    services.media_entity_flywheel.normalize_domain    host 级,不剥子域
    services.citation_domain_weights.normalize_domain  注册域,剥子域
`is_shared_platform_domain` 用的是前者 + 精确相等,于是
    is_shared('163.com') = True 但 is_shared('c.m.163.com') = False
名单里的裸域一条都没生效(zhihu.com 578 条候选命中 0 / 163.com 573 命中 0 / sohu.com 220 命中 0),
子域几乎全逃过(zhuanlan.zhihu.com 578 / c.m.163.com 228 / news.qq.com 285 / blog.csdn.net 194);
`qq.com` 这条在实体表精确命中 0 个实体 = 死规则。

本模块每条「必须命中」都配一条「必须不命中」。B 桶与单一媒体域是**硬反向对照**:
它们判 True 就说明有人把不该收的悄悄并进了名单。
"""
from __future__ import annotations

import pytest

from services.media_binding_candidates import (
    B_BUCKET_MAINSTREAM_PORTALS,
    SHARED_PLATFORM_DOMAINS,
    is_shared_platform_domain,
)
from services.citation_domain_weights import normalize_domain as registrable_domain
from services.media_entity_flywheel import normalize_domain as host_domain


# ── 根因本体:两版归一化必须还是两版(不许有人「统一」掉其中一个语义)──────────────
def test_two_normalizers_still_differ_and_both_are_used():
    """消歧方案是显式改名,不是合并 —— 合并会丢掉其中一个语义。
    这条同时是根因的可执行留档:host 级和注册域在同一输入上必须给出不同结果。"""
    assert host_domain("c.m.163.com") == "c.m.163.com"
    assert registrable_domain("c.m.163.com") == "163.com"
    assert host_domain("blog.csdn.net") == "blog.csdn.net"
    assert registrable_domain("blog.csdn.net") == "csdn.net"
    # 多段公共后缀:注册域那版必须认得 .com.cn,否则会折成 com.cn 把全中国并成一个平台
    assert registrable_domain("aikahao.xcar.com.cn") == "xcar.com.cn"

    import services.media_binding_candidates as mbc
    assert not hasattr(mbc, "normalize_domain"), (
        "模块里又出现裸的 normalize_domain —— 同名两版并存正是本 bug 的成因,必须显式 as 改名"
    )


# ── P0-1 正向:工单 §4 点名的四条 ────────────────────────────────────────────
@pytest.mark.parametrize("domain", [
    "blog.csdn.net", "c.m.163.com", "news.qq.com", "zhuanlan.zhihu.com",
    "view.inews.qq.com", "new.qq.com", "news.sohu.com", "mp.sohu.com", "dy.163.com",
])
def test_platform_subdomains_are_now_shared(domain):
    assert is_shared_platform_domain(domain) is True


def test_bare_registrable_domains_still_shared():
    """成对的另一半:改口径不能把原来能判出来的裸域弄丢。"""
    for d in ["163.com", "qq.com", "sohu.com", "zhihu.com", "toutiao.com", "weibo.com"]:
        assert is_shared_platform_domain(d) is True, d


def test_folded_host_level_entries_still_work():
    """原名单里两条 host 级写法折成了注册域;不折的话它们改判定后会变成死规则。"""
    assert is_shared_platform_domain("baijiahao.baidu.com") is True
    assert is_shared_platform_domain("mp.weixin.qq.com") is True
    # 折算的连带效果要说清楚:baidu.com / qq.com 整域现在都算平台(Owner 拍板 ③ 已含 baidu.com)
    assert is_shared_platform_domain("baidu.com") is True
    assert is_shared_platform_domain("tieba.baidu.com") is True


# ── P0-1 反向:同尾不同域绝不能被吃进来 ───────────────────────────────────────
@pytest.mark.parametrize("domain", ["notcsdn.net", "evilcsdn.net", "xcsdn.net", "fake163.com", "myqq.com"])
def test_lookalike_domains_are_not_shared(domain):
    """判定是「折注册域后精确相等」,不是裸后缀匹配 —— 否则 notcsdn.net 会被当成 csdn.net。"""
    assert is_shared_platform_domain(domain) is False


@pytest.mark.parametrize("domain", [
    "agri.example-news.com", "07358.com", "0745news.cn", "19lou.com", "21jingji.com", "39.net",
])
def test_single_media_domains_are_not_shared(domain):
    """148 个单一媒体域(媒体名=1)一律不收 —— 抽样自生产。"""
    assert is_shared_platform_domain(domain) is False


# ── P0-2 硬反向对照:B 桶一个都不许进名单 ────────────────────────────────────
@pytest.mark.parametrize("domain", sorted(B_BUCKET_MAINSTREAM_PORTALS))
def test_b_bucket_mainstream_portals_are_not_shared(domain):
    """🔴 B 桶 = 主流媒体主站(多频道但同一发布主体),Owner 未拍板前**不许**并进名单。
    这条是「有没有被悄悄塞进去」的可断言事实,不是文档承诺。"""
    assert is_shared_platform_domain(domain) is False, f"{domain} 被并进了 A 桶名单"
    assert domain not in SHARED_PLATFORM_DOMAINS


def test_a_and_b_buckets_are_disjoint():
    assert not (SHARED_PLATFORM_DOMAINS & B_BUCKET_MAINSTREAM_PORTALS)


def test_b_bucket_subdomains_are_not_shared():
    """B 桶的子域同样必须 False —— 否则「不收 B 桶」只在裸域上成立,子域照样被吃。"""
    for d in ["sports.news.cn", "tech.gmw.cn", "bj.huanqiu.com", "dongying.dzwww.com"]:
        assert is_shared_platform_domain(d) is False, d


# ── P0-2 A 桶:每个新增域一条正向 + 同域「有名称证据」的反向 ────────────────────
#: [补充单 P0-2 2026-08-16] 原 33 项已按词干判据复检,7 项降级(含 2026-08-16 手工降级的 jia.com)(见 STEM_DEMOTED_NON_PLATFORM),
#: 本清单同步减去它们 —— 留着会与降级断言互相打架。
A_BUCKET_NEW = sorted({
    "360kuai.com",
    "52hrtt.com",
    "autohome.com.cn",
    "baidu.com",
    "chooseauto.com.cn",
    "csdn.net",
    "ctrip.com",
    "digitaling.com",
    "dongchedi.com",
    "douban.com",
    "eastmoney.com",
    "ifeng.com",
    "jianshu.com",
    "mafengwo.cn",
    "meipian.cn",
    "pcauto.com.cn",
    "qctt.cn",
    "sina.cn",
    "sina.com.cn",
    "smzdm.com",
    "taobao.com",
    "weibo.cn",
    "xcar.com.cn",
    "xueqiu.com",
    "yoojia.com",
    "zcool.com.cn",
})


@pytest.mark.parametrize("domain", A_BUCKET_NEW)
def test_a_bucket_domain_and_its_subdomain_are_shared(domain):
    assert is_shared_platform_domain(domain) is True, domain
    assert is_shared_platform_domain(f"account.{domain}") is True, domain


@pytest.mark.parametrize("domain", A_BUCKET_NEW)
def test_a_bucket_entry_is_a_registrable_domain(domain):
    """名单元素本身必须已是注册域,否则它自己会变成死规则(原 baijiahao.baidu.com 就是这么死的)。"""
    assert registrable_domain(domain) == domain, f"{domain} 不是注册域,折算后是 {registrable_domain(domain)}"


def test_every_list_entry_is_registrable():
    bad = {d: registrable_domain(d) for d in SHARED_PLATFORM_DOMAINS if registrable_domain(d) != d}
    assert not bad, f"名单里有非注册域写法,改判定后会变成死规则:{bad}"


# ── 边界:jina 包装 / 移动前缀 / URL ─────────────────────────────────────────
def test_jina_wrapped_and_mobile_prefixed_inputs():
    """两级归一化缺一不可:注册域那版不认 r.jina.ai 前缀,单独喂会得到 r.jina.ai。"""
    assert is_shared_platform_domain("https://r.jina.ai/https://blog.csdn.net/x/y") is True
    assert is_shared_platform_domain("post.m.smzdm.com") is True
    assert is_shared_platform_domain("https://www.zhuanlan.zhihu.com/p/1") is True
    # 反向:jina 包着一个非平台域,仍必须 False(证明不是「见 jina 就 True」)
    assert is_shared_platform_domain("https://r.jina.ai/https://agri.example-news.com/x") is False


def test_empty_input_is_not_shared():
    for d in ["", None, "   "]:
        assert is_shared_platform_domain(d) is False  # type: ignore[arg-type]
