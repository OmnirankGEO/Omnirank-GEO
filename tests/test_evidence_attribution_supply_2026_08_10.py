# -*- coding: utf-8 -*-
"""D5 信源增援 · 锁。

背景(生产尖 dfe65fad 实测,319 篇 / 5,185 条 evidence item):
  · 有 publisher(来源方) **100.0%** · 有 published_at **90.0%** · 有 URL 100%
  · 但 publisher **出现在正文里只有 4.6%**(238/5,185)
断点有三处,本包三处都修,锁也分三段打:
  ① 渲染函数**从不输出** publisher / published_at → 模型没有写归属句的原料
  ② 同一份合同里规则 1 要求打 EV 编号、规则 7 要求不出现编号(相隔 6 行)
  ③ 清洗器把 EV 编号 `sub("")` 删掉**且不补归属** → 那句话变成没有来源的裸数字
"""
from __future__ import annotations

import inspect
import re

import pytest

from writing.body_internal_marker_sanitizer import (
    sanitize_article_body,
    sanitize_article_for_save,
)
from writing.evidence_pack import (
    attribution_of,
    render_evidence_pack_for_writer,
)

ITEM_REAL = {
    "evidence_id": "EV-001", "relationship": "support",
    "verification_status": "search_result", "title": "深圳电梯行业观察",
    "url": "https://example.com/a", "publisher": "中国电梯",
    "published_at": "2025-03-12", "claim": "交付率", "scope": "深圳",
    "excerpt": "摘录内容",
}
ITEM_GENERIC = {**ITEM_REAL, "evidence_id": "EV-002", "publisher": "公开记录"}
ITEM_NO_PUB = {**ITEM_REAL, "evidence_id": "EV-003", "publisher": ""}
PACK = {"version": "v1", "items": [ITEM_REAL, ITEM_GENERIC, ITEM_NO_PUB],
        "limitations": []}


# ------------------------------------------------------------ 元判据
class TestMetaCriteria:
    def test_fixture_items_really_carry_publisher_and_date(self):
        assert ITEM_REAL["publisher"] and ITEM_REAL["published_at"], (
            "夹具的 item 没有 publisher/日期,后面的断言全是空的"
        )

    def test_fixture_covers_all_three_publisher_shapes(self):
        """真来源 / 泛化类型词 / 空 —— 三种都要有,否则测不出分支。"""
        assert ITEM_REAL["publisher"] not in ("", "公开记录")
        assert ITEM_GENERIC["publisher"] == "公开记录"
        assert ITEM_NO_PUB["publisher"] == ""


# ------------------------------------------------------------ ① 归属三要素
class TestAttributionOf:
    def test_real_publisher_produces_prose(self):
        a = attribution_of(ITEM_REAL)
        assert a["publisher"] == "中国电梯"
        assert a["date"] == "2025 年 3 月"
        assert a["prose"] == "据中国电梯 2025 年 3 月"

    def test_generic_publisher_is_not_a_source(self):
        """🔴「公开记录」填在来源方位上等于没标注 —— 不许产出归属句。"""
        a = attribution_of(ITEM_GENERIC)
        assert a["publisher"] == ""
        assert a["prose"] == ""

    def test_missing_publisher_produces_no_prose(self):
        assert attribution_of(ITEM_NO_PUB)["prose"] == ""

    def test_missing_date_still_produces_prose(self):
        """日期缺失只写来源方 —— **不许编日期**。"""
        a = attribution_of({**ITEM_REAL, "published_at": ""})
        assert a["prose"] == "据中国电梯"
        assert a["date"] == ""

    @pytest.mark.parametrize("raw,expect", [
        ("2025-03-12T00:00:00Z", "2025 年 3 月"),
        ("2025/3/1", "2025 年 3 月"),
        ("2025年12月", "2025 年 12 月"),
        ("2025", "2025 年"),
        ("昨天", ""),          # 认不出就不写,不编
        ("", ""),
    ])
    def test_date_normalization(self, raw, expect):
        assert attribution_of({"publisher": "X", "published_at": raw})["date"] == expect


# ------------------------------------------------------------ ② 下发给模型
class TestWriterBlockSupply:
    def test_publisher_and_date_are_rendered(self):
        out = render_evidence_pack_for_writer(PACK)
        assert "来源方：中国电梯" in out, "publisher 没下发,模型不知道 XX 是谁"
        assert "日期：2025 年 3 月" in out
        assert "归属句照抄：据中国电梯 2025 年 3 月" in out

    def test_generic_publisher_is_marked_unusable(self):
        """[fail-closed 终态] 泛化类型词条目标「未确认来源」并明示禁止外部归属。"""
        out = render_evidence_pack_for_writer(PACK)
        assert "来源可信状态：未确认来源" in out
        assert "不得写成外部归属" in out

    def test_block_no_longer_forbids_date_in_body(self):
        """🔴 矛盾 C:旧块写「来源 URL 与日期只落 manifest,不进正文」——
        那条与"挂日期归属"直接打架,已删。"""
        out = render_evidence_pack_for_writer(PACK)
        assert "日期只落 evidence manifest" not in out
        assert "不进正文" not in out

    def test_block_still_forbids_ev_numbers(self):
        """反向对照:内部编号仍然禁止进正文。"""
        out = render_evidence_pack_for_writer(PACK)
        assert "不得出现 Evidence ID" in out

    # -------- D4 claim 级立场纪律 --------
    # -------- [端到端 A/B 实证补锁] 域名 / 客户自有站 --------
    def test_bare_domain_publisher_gets_no_attribution(self):
        """🔴 [复审终态 · 工单 §6E] 裸域名**不得伪装成媒体**。

        中间版曾把裸域名回退成《文章标题》—— 生产实测 5,185 条里裸域名占
        88.9%,前几名是 cnblogs.com(博客平台)/baike.baidu.com(UGC)/
        holike.com(**竞品自己的官网**),包装成《载体》= 假第三方引用,
        比「企业提交资料」更危险。终态 = fail-closed:证不出独立第三方
        就不产出归属句,事实照发但只能走降级阶梯。
        [R7 演进 2026-08-11] 人工核过的域名(163.com 等)另走映射具名档,
        本条改用**未收录域名**继续钉 fail-closed 腿 —— 两腿并存,互为反例。"""
        a = attribution_of({"publisher": "holike.com", "title": "2026年全屋定制行业观察报告",
                            "published_at": "2026-07"})
        assert a["prose"] == "", "未收录裸域名被包装成了可引用载体 —— 假第三方引用"
        assert a["trust"] == "unverified"
        assert "《" not in a["publisher"], "不得用《标题》给裸域名冒充媒体名"

    def test_real_media_name_is_not_rewritten(self):
        """反向对照:真媒体名不许被改成标题 —— 否则就是恒改。"""
        assert attribution_of(ITEM_REAL)["publisher"] == "中国电梯"

    def test_unmapped_domain_short_title_yields_no_attribution(self):
        """未收录域名 + 短标题 → 不产出归属,**不编**。
        [R7 演进] news.qq.com 已被映射具名(腾讯网),换未收录域名钉这条腿。"""
        assert attribution_of({"publisher": "some-unknown-site.cn", "title": "短",
                               "published_at": "2026-04"})["prose"] == ""

    def test_client_own_site_is_never_an_external_source(self):
        """🔴 A/B 实证:`qzqzwood.com` 被写成「据qzqzwood.com…发布」——
        那是客户自己的官网,自曝的变形,比「企业提交资料」更隐蔽
        (它看起来像第三方引用)。终态三道防线逐条验:
        ① URL 命中客户域 → self;② publisher 带客户名字 → self;
        ③ 反向对照:真媒体名 + 非客户站照常产出(否则是恒空)。"""
        item = {"publisher": "qzqzwood.com", "title": "公司简介",
                "url": "https://www.qzqzwood.com/a", "published_at": "2026-04"}
        a = attribution_of(item, client_domains=("qzqzwood.com",))
        assert a["prose"] == "" and a["trust"] == "self"
        named = {"publisher": "KZ木作定制官网", "title": "公司简介",
                 "url": "https://example.com/a", "published_at": "2026-04"}
        assert attribution_of(named, self_names=("KZ木作定制",))["trust"] == "self"
        # 反向对照(必须不命中):真媒体名 + 非客户站 → 照常产出归属句
        real = {**ITEM_REAL, "url": "https://media.example.com/x"}
        assert attribution_of(real, client_domains=("qzqzwood.com",))["prose"]

    def test_same_site_matches_subdomain_only(self):
        from writing.evidence_pack import _same_site
        assert _same_site("https://news.qzqzwood.com/a", ("qzqzwood.com",)) is True
        assert _same_site("https://qzqzwood.com.evil.com/a", ("qzqzwood.com",)) is False
        assert _same_site("", ("qzqzwood.com",)) is False

    def test_renderer_passes_client_domains_down(self):
        """接线锁:渲染器必须把 client_domains 传进 attribution_of。"""
        src = inspect.getsource(render_evidence_pack_for_writer)
        assert "client_domains=client_domains" in src

    def test_stance_discipline_is_explained(self):
        """🔴 [D4] `relationship` 三态早就有,但一直只以英文裸词下发,
        也没说过"反驳条目不许当支持用" —— 同一条证据可能被拿去支撑
        一个它其实在反驳的结论,那是最难发现的一类不实。"""
        out = render_evidence_pack_for_writer(PACK)
        assert "support" in out and "refute" in out and "background" in out
        assert "严禁" in out and "当成支持证据引用" in out
        assert "不要跨主张复用" in out

    def test_stance_words_match_the_module_vocabulary(self):
        """反向对照:块里解释的三态必须与 `VALID_RELATIONSHIPS` 同源,
        不许块里写一套、代码认另一套。"""
        from writing.evidence_pack import VALID_RELATIONSHIPS
        out = render_evidence_pack_for_writer(PACK)
        for word in VALID_RELATIONSHIPS:
            assert f"`{word}`" in out, f"立场词 {word} 没在块里解释"

    def test_attribution_comes_from_ssot_not_literal(self, monkeypatch):
        """反向对照:改 `attribution_of` 的产出,渲染必须跟着变。"""
        import writing.evidence_pack as ep
        monkeypatch.setattr(
            ep, "attribution_of",
            lambda item, **_kw: {"publisher": "＿哨兵＿", "date": "", "prose": "＿哨兵句＿"},
        )
        assert "＿哨兵句＿" in ep.render_evidence_pack_for_writer(PACK)


# ------------------------------------------------------------ ② 合同矛盾
class TestContractContradictionFixed:
    def test_rule1_no_longer_demands_ev_number(self):
        from writing.evidence_precision_policy import render_evidence_precision_prompt
        prompt = render_evidence_precision_prompt(PACK, {})
        rule1 = [l for l in prompt.splitlines() if l.startswith("1. ")]
        assert rule1, "找不到规则 1,锁失效"
        assert "〔EV-001〕" not in rule1[0], "规则 1 仍在要求打 EV 编号(与规则 7 打架)"
        assert "来源方" in rule1[0] and "日期" in rule1[0]

    def test_rule7_still_forbids_ev_number(self):
        """反向对照:规则 7 那一侧不许被顺手删掉。"""
        from writing.evidence_precision_policy import render_evidence_precision_prompt
        prompt = render_evidence_precision_prompt(PACK, {})
        assert "不出现 Evidence ID" in prompt


# ------------------------------------------------------------ ③ 清洗器补归属
class TestSanitizerRestoresAttribution:
    BODY = ("深圳该厂商准时交付率为 98.6%〔EV-001〕。"
            "行业均值为 91%〔EV-002〕。另一处 [EV-009] 无归属。")

    def test_marker_replaced_with_real_attribution(self):
        out, markers = sanitize_article_body(self.BODY, PACK)
        assert "（据中国电梯 2025 年 3 月）" in out, "编号被删了但没补归属"
        assert "EV-001" not in out, "编号本身不许留在正文"
        assert markers.get("restored_attributions")

    def test_generic_publisher_marker_is_deleted_not_faked(self):
        """🔴 反向对照:泛化来源不许被伪造成归属句。"""
        out, _ = sanitize_article_body(self.BODY, PACK)
        assert "据公开记录" not in out

    # 🔴🔴 [复审返工 2026-08-10 · P1-2] 本条是**倒转**过来的。
    #
    # 上一版这里断言 `assert "行业均值为 91%。" in out` —— 也就是把
    # 「擦掉来源、留下裸数字」钉成了正确行为。复审原话:
    #   *正确修法是 claim 级处理：有可靠信源就替换归属；没有就自动缩小或
    #    去掉完整数字主张，不能只擦掉来源。*
    # 那样洗出来的句子把**可识别的单方材料**变成**看起来客观的无来源事实**,
    # 比自曝更糟,因为读者和 AI 搜索引擎都认不出它没有依据。
    # 教训记在这里:我的锁编码了错误行为,而且它一直是绿的。
    def test_unsourced_number_claim_survives_without_fake_source(self):
        """🔴🔴 挂不上独立第三方时:**主张留下,但不许伪造来源**。

        本条在 24 小时内被改过两次方向,把理由钉在这里,别再来回改:

        · v1 原始:留数字、留假来源 → 错(伪造第三方)
        · v1 返工:整条删掉        → **也错**,而且更伤 ——
          中小企业客户的具体事实**只可能来自自己的材料**(没有第三方会去报道
          一家小公司的交付量/质保/响应时长)。删掉 = 把客户唯一能被 AI 复述成
          推荐理由的素材删光。文章更干净了,AI 更没理由推荐这家。
        · 现行(P0-5):**摘掉假归属,主张保留**。
          「去伪精度」由 prompt 侧三级事实合同承担,不在清洗器做语义改写。

        第一性原理:我们要的是**客户被 AI 推荐**,不是文章通过审计。
        """
        out, _ = sanitize_article_body(self.BODY, PACK)
        assert "91%" in out, "客户素材被删 = 违背第一性原理(见 docstring)"
        assert "行业均值为 91%" in out, "主张必须完整保留"
        assert "据公开记录" not in out, "但绝不许给它安一个假的第三方来源"
        assert "EV-002" not in out, "内部编号仍然不许留在正文"

    def test_sourced_number_claim_survives(self):
        """🔴 反向对照(必须不命中):挂得上独立第三方的数字**不许**被删。"""
        out, _ = sanitize_article_body(self.BODY, PACK)
        assert "98.6%" in out, "有归属的数字被误删 = 反方向的谎言"
        assert "（据中国电梯 2025 年 3 月）" in out

    def test_bare_year_is_not_a_hard_number(self):
        """元判据:裸年份是时间状语不是数量主张,不得触发整句删除。"""
        from writing.body_internal_marker_sanitizer import _has_hard_number_claim
        assert not _has_hard_number_claim("公司于 2018 年成立。")
        assert _has_hard_number_claim("满意度达到 95%。")

    def test_no_claim_level_deletion_wiring_remains(self):
        """接线锁(反向):claim 级整条删除**必须已从保存链拔掉**。

        只断言"函数不存在"是弱锁 —— 函数留着不调用也算拔掉。
        这里断言的是**调用点**:`sanitize_article_body` 源码里不得再调它。
        """
        import inspect
        src = inspect.getsource(sanitize_article_body)
        assert "_drop_unsourced_number_claims(" not in src, (
            "claim 级整条删除又被接回保存链了 —— 它删的是客户唯一的具体事实"
        )

    def test_unknown_id_falls_back_to_deletion(self):
        out, _ = sanitize_article_body(self.BODY, PACK)
        assert "EV-009" not in out

    def test_without_pack_falls_back_to_old_behaviour(self):
        """向后兼容:不传 pack 时仍是删除,不炸。"""
        out, _ = sanitize_article_body(self.BODY)
        assert "EV-001" not in out
        assert "据中国电梯" not in out
        assert "98.6%" in out

    def test_bad_pack_never_raises(self):
        for bad in (None, "", [], {"items": "x"}, {"items": [None, 3]}):
            out, _ = sanitize_article_body(self.BODY, bad)  # type: ignore[arg-type]
            assert "98.6%" in out

    # -------- 接线锁:打在实参上,不打在函数存在上 --------
    def test_save_entry_passes_the_pack(self):
        src = inspect.getsource(sanitize_article_for_save)
        assert re.search(r'sanitize_article_body\(\s*original,\s*article\.get\(\s*["\']evidence_pack["\']\s*\)',
                         src), "保存入口没有把 evidence_pack 传下去(第 N 例「接线没接」)"

    def test_save_entry_really_restores_end_to_end(self):
        article = {"content": self.BODY, "evidence_pack": PACK}
        sanitize_article_for_save(article)
        assert "（据中国电梯 2025 年 3 月）" in article["content"]

    def test_save_entry_without_pack_still_saves(self):
        article = {"content": self.BODY}
        sanitize_article_for_save(article)
        assert "98.6%" in article["content"]


# ------------------------------------------------ R7:已验证域名→载体映射
class TestVerifiedDomainCarriers:
    def test_mapped_platform_domain_gets_honest_named_attribution(self):
        """① 映射命中 → 自然载体名进归属句;分档如实(平台内容不称报道)。"""
        a = attribution_of({"publisher": "cnblogs.com", "title": "x",
                            "url": "https://www.cnblogs.com/p/1", "published_at": "2026-01"})
        assert a["trust"] == "platform_ugc"
        assert a["publisher"] == "博客园"
        assert a["prose"].startswith("据博客园上的公开内容")
        assert "cnblogs.com" not in a["prose"], "裸域名漏进了归属句"

    def test_mapped_portal_media_prose_is_plain_named(self):
        # [R3-A1 适配 2026-08-11] 原夹具用 163.com —— Review 裁定其 portal_media
        # 档 584 items 实测几乎全是网易号等自发布频道,「据网易新闻」=假冒第三方
        # 归属(Owner 亲裁类别②),已降档 platform_ugc(降档锁在
        # test_rework_r3_verdict_locks)。本锁原意图=门户档产出平实具名归属句,
        # 夹具换真编辑媒体澎湃新闻,意图不变。
        a = attribution_of({"publisher": "thepaper.cn", "title": "x",
                            "url": "https://www.thepaper.cn/n_1", "published_at": "2025-07"})
        assert a["trust"] == "portal_media" and a["prose"] == "据澎湃新闻 2025 年 7 月"
        a2 = attribution_of({"publisher": "163.com", "title": "x",
                             "url": "https://news.163.com/a", "published_at": "2025-07"})
        assert a2["trust"] == "platform_ugc", "163.com 降档被退回(A1)"

    def test_subdomain_matches_longest_suffix_first(self):
        """mp.weixin.qq.com 必须命中「微信公众号」而不是父域「腾讯网」。"""
        a = attribution_of({"publisher": "mp.weixin.qq.com", "title": "x",
                            "url": "https://mp.weixin.qq.com/s/1"})
        assert a["publisher"] == "微信公众号" and a["trust"] == "platform_ugc"

    def test_unmapped_domain_stays_fail_closed(self):
        """② 未收录域名 → 维持省略归属(不回退、不编造)。"""
        a = attribution_of({"publisher": "holike.com", "title": "竞品官网页",
                            "url": "https://holike.com/a"})
        assert a["prose"] == "" and a["trust"] == "unverified"

    def test_missing_map_file_degrades_to_fail_closed_without_crash(self, monkeypatch):
        """③ 变异:映射文件缺失 → 全部退回省略归属,**不炸不编造**。"""
        import writing.evidence_pack as ep

        monkeypatch.setattr(ep, "_CARRIER_MAP_PATH", ep._CARRIER_MAP_PATH.with_name("nonexistent.json"))
        monkeypatch.setattr(ep, "_carrier_cache", {})
        a = attribution_of({"publisher": "cnblogs.com", "title": "x",
                            "url": "https://www.cnblogs.com/p/1"})
        assert a["prose"] == "" and a["trust"] == "unverified"

    def test_client_site_never_enters_mapping_path(self):
        """反向:客户自站优先级高于映射(自站永不具名)。"""
        a = attribution_of(
            {"publisher": "cnblogs.com", "url": "https://www.qzqzwood.com/a"},
            client_domains=("qzqzwood.com",),
        )
        assert a["trust"] == "self" and a["prose"] == ""

    def test_renderer_marks_platform_ugc_usage(self):
        out = render_evidence_pack_for_writer({"version": "v1", "items": [
            {"evidence_id": "EV-U1", "relationship": "support",
             "verification_status": "search_result", "title": "选型清单",
             "url": "https://www.cnblogs.com/p/1", "publisher": "cnblogs.com",
             "published_at": "2026-01", "claim": "载重是选型核心", "scope": "",
             "excerpt": "……"}], "limitations": []})
        assert "来源可信状态：平台内容" in out
        assert "不写“报道”" in out
