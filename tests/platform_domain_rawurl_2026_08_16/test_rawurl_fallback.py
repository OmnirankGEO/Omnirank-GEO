"""[裸 URL 回落 2026-08-16] 库存拿不到域时,从媒体名回落解析 → 同一条管道 → 同一个名单。

## 生产实测的真实机制(与工单描述不同,以实测为准)

工单说「新判据解析的是 `inventory_url` 所以够不着」。实测:那 36 条的**实体域并不空**
(`toutiao.com` ×24 / `163.com` ×12),而线上判定读的正是实体域。真正的机制是:

    库存 entrance_link 为空 ⇒ 拿不到库存域 ⇒ `domain_exact` 打不出来
    ⇒ 退到名字匹配,而**实体的 canonical_name 就是域名本身**(`toutiao.com`)
    ⇒ 媒体名(一个 URL)里含这个串 ⇒ `name_alias` 命中
    ⇒ 而共享平台风险标**只作用于 domain_exact** ⇒ 永远不触发 ⇒ can_approve=true

所以修法是两半,缺一不可(单做第一半实测拦不住,因为「URL 里含域名」仍被当成名称证据):
  ① 库存域回落:`entrance_link` 空时从媒体名提 URL,喂进同一条 `registrable_domain(host_domain(…))`;
  ② URL 形态的名字**不算名称证据** —— URL 不是名字,它不能证明这个账号属于谁。

夹具按生产真名的形状编造(域名保留,用户部分换成编造值),不是手搓的。
"""
from __future__ import annotations

import pytest

from services.media_binding_candidates import (
    build_binding_candidates,
    domain_from_name,
    evaluate_candidate_live,
    is_url_like_name,
)

# ── 生产形状(域名保留,用户部分编造)────────────────────────────────────────────────
TOUTIAO_URL = "https://www.toutiao.com/c/user/token/MS4wLjABAAAAx"   # A 集合 ×24
NETEASE_MAIL = "https://163.com/users/example-account"                                      # B\A 集合 ×12

TOUTIAO_ENTITY = {"id": 1, "entity_key": "me_fead691a41bb", "canonical_name": "toutiao.com",
                  "domain": "toutiao.com", "aliases": [], "industry_key": "geo_优化服务"}
NETEASE_ENTITY = {"id": 2, "entity_key": "me_0e43e73db916", "canonical_name": "163.com",
                  "domain": "163.com", "aliases": [], "industry_key": "geo_优化服务"}


def inv(name: str, *, entrance_link: str = "", price: float = 215.0) -> dict:
    """entrance_link 为空 = 那 36 条的真实形态。"""
    return {"media_source": "mhz_wemedia", "inventory_id": 100577739, "media_name": name,
            "entrance_link": entrance_link, "price": price, "our_price_yuan": 323.0,
            "our_price_points": 41990, "is_active": True}


# ── URL 形态识别:正向 + 成对反向 ────────────────────────────────────────────
@pytest.mark.parametrize("name", [
    TOUTIAO_URL, NETEASE_MAIL, "https://www.bilibili.com/x", "http://163.com",
    "toutiao.com", "www.zhihu.com",
])
def test_url_like_names_are_detected(name):
    assert is_url_like_name(name) is True
    assert domain_from_name(name) == name.strip()


@pytest.mark.parametrize("name", [
    "中华网快讯焦点图", "新华网首发（视频）", "凤凰网健康", "CSDN", "WHYLAB", "e-works",
    "vivi慢生活", "36氪快讯", "", "   ",
])
def test_real_names_are_not_mistaken_for_urls(name):
    """🔴 成对反向:真名字一个都不许被当成 URL。"""
    assert is_url_like_name(name) is False
    assert domain_from_name(name) == ""


@pytest.mark.parametrize("name", ["36氪.科技", "abc.中文", "x.科技", "news.中国",
                                  "vivi.慢生活", "a.b中文"])
def test_cjk_guard_blocks_names_the_regex_alone_would_swallow(name):
    """🔴 CJK 守卫的判别力锁 —— 这些是**构造**输入,不是生产真名。

    为什么需要构造:Python 的 `\\w` **匹配中日韩字符**,所以
    `36氪.科技` 整串能被 host.tld 那条正则吃掉。没有守卫,这类名字会被判成「URL」⇒
    名称证据被抹掉 ⇒ 明明有名字的候选被误拦。

    ⚠️ 生产实测:当前 1,687 个去重媒体名里**触发这道守卫的有 0 个**(见交付单)。
    守卫无当下影响但**失效模式可达** —— 这与上一轮删掉的去噪不同:
    去噪是任何输入都无判别力,这条是今天没数据踩到、明天会踩。
    """
    assert is_url_like_name(name) is False, f"{name} 被当成了 URL"
    assert domain_from_name(name) == ""


def test_url_detector_is_not_the_normalizer():
    """🔴 不能拿 `host_domain(name) 非空` 当 URL 探测器 —— 它对任何字符串都返回非空。
    这条锁把那个诱人但恒真的写法钉死。"""
    from services.media_entity_flywheel import normalize_domain as host_domain
    assert host_domain("中华网快讯") != ""          # 恒真的反例
    assert is_url_like_name("中华网快讯") is False   # 正确判定


# ── §3 正向:那 36 条改后必须被拦 ────────────────────────────────────────────
@pytest.mark.parametrize("entity,name", [(TOUTIAO_ENTITY, TOUTIAO_URL),
                                         (NETEASE_ENTITY, NETEASE_MAIL)])
def test_platform_url_named_candidates_are_blocked(entity, name):
    v = evaluate_candidate_live(entity, inv(name), {"inventory": {"inventory_id": 100577739}})
    assert v["block_reason"] == "共享平台域名需要名称证据"
    c = v["candidate"]
    assert c["can_approve"] is False
    assert c["match_method"] == "domain_exact", "回落后应打出 domain_exact,而不是 name_alias"


# ── 🔴 §3 反向:自有域 URL 必须仍放行(证明不是「空就拦」的一刀切)────────────────
def test_own_domain_url_name_still_passes():
    own = {"id": 3, "entity_key": "me_own", "canonical_name": "某农业媒体",
           "domain": "agri.example-news.com", "aliases": [], "industry_key": "general"}
    v = evaluate_candidate_live(own, inv("https://agri.example-news.com/a/1"),
                                {"inventory": {"inventory_id": 100577739}})
    assert v["block_reason"] == "", f"自有域被误拦:{v['block_reason']}"
    assert v["candidate"]["can_approve"] is True
    assert v["candidate"]["match_method"] == "domain_exact"


def test_own_domain_with_real_name_still_passes():
    """再配一条:自有域 + 真名字(不是 URL)—— 回落根本不该介入。"""
    own = {"id": 4, "entity_key": "me_own2", "canonical_name": "某农业媒体",
           "domain": "agri.example-news.com", "aliases": [], "industry_key": "general"}
    v = evaluate_candidate_live(own, inv("某农业媒体", entrance_link="https://agri.example-news.com/x"),
                                {"inventory": {"inventory_id": 100577739}})
    assert v["block_reason"] == ""


# ── 🔴 §3 拆层:去掉回落 → 那 36 条那侧必须红 ────────────────────────────────
def test_removing_the_fallback_lets_platform_urls_through(monkeypatch):
    """🔴 亲手拆这一层:把 `domain_from_name` 打成恒空(= 回落不存在),
    那 36 条必须**放行回去** —— 这就是改前的病,也是这一层在干活的证据。"""
    import services.media_binding_candidates as mbc

    payload = {"inventory": {"inventory_id": 100577739}}
    blocked = evaluate_candidate_live(TOUTIAO_ENTITY, inv(TOUTIAO_URL), payload)
    assert blocked["candidate"]["match_method"] == "domain_exact"
    assert blocked["block_reason"] == "共享平台域名需要名称证据"

    monkeypatch.setattr(mbc, "domain_from_name", lambda name: "")
    fell_through = evaluate_candidate_live(TOUTIAO_ENTITY, inv(TOUTIAO_URL), payload)
    assert fell_through["candidate"]["match_method"] == "name_alias", "拆层后应退回 name_alias"
    assert fell_through["block_reason"] == "", "拆掉回落后仍被拦 ⇒ 这一层没有判别力"


def test_removing_the_name_evidence_half_also_lets_them_through(monkeypatch):
    """🔴 另一半也拆一次:URL 若仍算名称证据,风险标会被抵消 ⇒ 照样放行。
    两半缺一不可 —— 这条锁保证以后没人把其中一半当冗余删掉。"""
    import services.media_binding_candidates as mbc

    payload = {"inventory": {"inventory_id": 100577739}}
    # 🔴 只拆名称证据这一半。**不能**改打 `is_url_like_name` —— 两半都用它,
    #    打了它等于两半一起拆,那样测不出第二半单独的贡献(第一版就是这么写的,不算隔离)。
    monkeypatch.setattr(mbc, "_has_name_evidence", lambda entity, row: True)
    v = evaluate_candidate_live(TOUTIAO_ENTITY, inv(TOUTIAO_URL), payload)
    assert v["candidate"]["match_method"] == "domain_exact", "域回落仍应生效(证明只拆了一半)"
    assert v["block_reason"] == "", "只拆「URL 不算名称证据」这半,应放行(证明它也在干活)"


# ── §2.2 保守默认:两处都拿不到域,不许静默放行 ───────────────────────────────
def test_no_domain_anywhere_is_not_silently_approved():
    """🔴 实体域空 + 库存域空(且媒体名不是 URL)⇒ 无从判断挂在谁名下 ⇒ 必须要人工核对,
    不许默认 can_approve=true。"""
    nodomain = {"id": 5, "entity_key": "me_nodomain", "canonical_name": "某某资讯",
                "domain": "", "aliases": [], "industry_key": "general"}
    v = evaluate_candidate_live(nodomain, inv("某某资讯"), {"inventory": {"inventory_id": 100577739}})
    assert v["block_reason"] == "无法确定域名归属需人工核对"
    assert v["candidate"]["can_approve"] is False


def test_domain_present_anywhere_does_not_trigger_the_conservative_flag():
    """成对反向:只要**任一侧**拿得到域,这条保守闸就不许响 —— 否则它会误伤正常候选。"""
    own = {"id": 6, "entity_key": "me_own3", "canonical_name": "某农业媒体",
           "domain": "agri.example-news.com", "aliases": [], "industry_key": "general"}
    v = evaluate_candidate_live(own, inv("某农业媒体"), {"inventory": {"inventory_id": 100577739}})
    assert "无法确定域名归属需人工核对" not in (v["candidate"]["risk_flags"] or [])
    assert v["block_reason"] == ""


# ── 回归:上三包修好的东西不许被这一层改坏 ────────────────────────────────────
def test_existing_behaviour_unchanged_for_normal_rows():
    """带真名字 + 真 entrance_link 的正常候选,判定与本包无关。"""
    ent = {"id": 7, "entity_key": "me_csdn", "canonical_name": "CSDN博客",
           "domain": "blog.csdn.net", "aliases": [], "industry_key": "general"}
    # 平台域 + 无名称证据 → 仍被拦(上一包的行为)
    v1 = evaluate_candidate_live(ent, inv("扬道财经", entrance_link="https://blog.csdn.net/a"),
                                 {"inventory": {"inventory_id": 100577739}})
    assert v1["block_reason"] == "共享平台域名需要名称证据"
    # 平台域 + 有名称证据 → 仍放行
    v2 = evaluate_candidate_live(ent, inv("CSDN博客官方号", entrance_link="https://blog.csdn.net/a"),
                                 {"inventory": {"inventory_id": 100577739}})
    assert v2["block_reason"] == ""


def test_fallback_does_not_fabricate_a_domain_for_plain_names():
    """回落只在名字**确实是 URL** 时介入;普通名字不许被凭空解析出域名。"""
    cands = build_binding_candidates(
        {"entity_key": "me_x", "canonical_name": "某农业媒体", "domain": "agri.example-news.com",
         "aliases": []},
        [inv("某农业媒体")])
    assert cands and cands[0]["inventory"]["domain"] == "", "普通名字被解析出了域名"
