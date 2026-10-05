"""标准类证据 lane(秘塔 document)+ 标题级证据写作铁律 —— 行为级判别锁。

工单 ``docs/AI-CONTEXT/WORKORDER_STANDARD_EVIDENCE_LANE_2026-07-29.md``

口径(§6):**不按"命中条数"验收** —— 秘塔结果跨时段会漂移。这里锁的是
"给定输入域名/标题,放行与否是否符合 §2",全部行为级,**不做源码串断言**。

夹具 ``tests/fixtures/metaso_document_probe_2026_07_29.json`` 是 2026-07-29 对
``scope=document`` 的真实调用原样落盘(内层键 ``documents``、条目字段
``authorityDomain/authorityType/authors/link/title/summary``),不是手写的。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tools.search.provider_router as pr  # noqa: E402
import writing.evidence_research as er  # noqa: E402
from services import standard_citation_guard as guard  # noqa: E402
from writing.evidence_pack import (  # noqa: E402
    STANDARD_CITATION_TIER,
    normalize_evidence_pack,
    render_evidence_pack_for_writer,
)

FIXTURE = Path(__file__).parent / "fixtures" / "metaso_document_probe_2026_07_29.json"


def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("SEARCH_PROVIDER_") or key.startswith("GEO_ARTICLE_EVIDENCE_"):
            monkeypatch.delenv(key, raising=False)


def _doc(**over):
    """一条 document lane 条目(默认权威且合规),按用例覆盖字段。"""
    entry = {
        "title": "住宅性能评定技术标准",
        "link": "https://www.gov.cn/xinwen/2016-12/21/5151038/files/x.docx",
        "url": "",
        "authorityDomain": "www.gov.cn",
        "authorityType": "government",
        "authors": ["中华人民共和国住房和城乡建设部"],
        "date": "",
        "snippet": "本标准规定了住宅适用性能、环境性能等评定指标。",
        "docId": "cfe09409",
        "source": pr.STANDARD_LANE_SOURCE,
    }
    entry.update(over)
    return entry


# ---------------------------------------------------------------------------
# §2 放行条件:source == 'metaso.document' AND gov_host(item)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field", ["link", "url", "authorityDomain"])
def test_gov_cn_admitted_on_any_of_three_fields(field):
    """三个来源字段分别判定,任一命中即真。"""
    entry = _doc(link="", url="", authorityDomain="")
    entry[field] = "https://std.samr.gov.cn/gb/search" if field != "authorityDomain" else "std.samr.gov.cn"
    assert er.standard_lane_admits(entry) is True


def test_bare_domain_without_scheme_is_admitted():
    """§5-2 裸域名:authorityDomain 常无 scheme,归一化必须在函数体内做。

    不补 '//' 的话 urlparse 得 hostname=None → 静默 False → 把真权威源判掉。
    """
    assert er._gov_host("www.wuda.gov.cn") is True
    assert er.standard_lane_admits(
        _doc(link="", url="", authorityDomain="www.wuda.gov.cn")
    ) is True


@pytest.mark.parametrize("link", [
    "https://evil.com/?ref=x.gov.cn",          # 查询串里带 .gov.cn
    "https://fake.gov.cn.attacker.io/std.pdf",  # 后缀伪装
    "https://gov.cn.evil.net/a.pdf",
])
def test_in_match_style_forgeries_are_rejected(link):
    """🔴 这是证据放行闸,不是日志过滤:`in` 匹配会放行的形态必须全拒。"""
    assert er._gov_host(link) is False
    assert er.standard_lane_admits(_doc(link=link, url="", authorityDomain="")) is False


def test_us_gov_domain_is_rejected():
    """§0-3 Owner 裁定:本 lane 只收 .gov.cn,不含 .gov。"""
    assert er._gov_host("https://www.whitehouse.gov/x.pdf") is False
    assert er.standard_lane_admits(
        _doc(link="https://www.whitehouse.gov/x.pdf", url="", authorityDomain="")
    ) is False


def test_site_builder_subdomain_with_legit_title_is_rejected():
    """§1.2:建站平台随机子域名 + 看起来完全正规的标准标题 → 必须被拒。

    `pro5323b5d3-pic11` 是易搜建站分配的站点 ID:任何人都能注册开站、传任意 PDF、
    标题随便写。**光看标题分辨不出来源是否权威** —— 这就是"标题含编号"不能单独
    承载放行权的证明,所以 v2 把那条路砍了。
    """
    for host in ("pro5323b5d3-pic11.ysjianzhan.cn", "www.weboos.cn", "www.ds-101.com"):
        entry = _doc(
            title="住宅性能评定标准 Standard for performance assessment of residential buildings",
            link=f"http://{host}/assets/basicStandard/std_3037712.pdf",
            url="", authorityDomain="", authorityType="",
        )
        assert er.standard_lane_admits(entry) is False, host


def test_non_document_source_never_gets_the_no_body_privilege():
    """🔴 §2 外层 source 是本路径的**唯一入口约束**。

    去掉它,任意 .gov.cn 网页会从 evidence / research 别的 lane 混进来白拿免正文特权。
    """
    assert er.standard_lane_admits(_doc(source="metaso_mcp")) is False
    assert er.standard_lane_admits(_doc(source="doubao_search")) is False
    assert er.standard_lane_admits(_doc(source="")) is False


def test_real_probe_fixture_splits_exactly_by_gov_cn():
    """拿真实探针夹具跑一遍闸:凡放行的,link/authorityDomain 必须真是 .gov.cn。"""
    inner = json.loads(FIXTURE.read_text(encoding="utf-8"))
    docs = inner["documents"]
    assert docs, "夹具必须是真实非空样本"
    admitted, rejected = [], []
    for raw in docs:
        entry = _doc(
            title=raw.get("title") or "",
            link=raw.get("link") or "",
            url=raw.get("url") or "",
            authorityDomain=raw.get("authorityDomain") or "",
            authorityType=raw.get("authorityType") or "",
        )
        (admitted if er.standard_lane_admits(entry) else rejected).append(raw)
    # 不断言条数(§6:结果跨时段漂移),只断言规则一致性。
    for raw in admitted:
        hosts = [raw.get("link") or "", raw.get("url") or "", raw.get("authorityDomain") or ""]
        assert any(er._gov_host(h) for h in hosts), raw.get("title")
    for raw in rejected:
        hosts = [raw.get("link") or "", raw.get("url") or "", raw.get("authorityDomain") or ""]
        assert not any(er._gov_host(h) for h in hosts), raw.get("title")


# ---------------------------------------------------------------------------
# §2.2 STD_CODE —— 只用于 §4.2 分类,不产生放行权
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["GBP 汇率 1234", "GBK 编码表 2024", "DB 数据库设计 2018"])
def test_std_code_does_not_misfire(text):
    assert er.STD_CODE.search(text) is None


@pytest.mark.parametrize("text,expected", [
    ("GB 50210-2018 验收", "GB 50210"),
    ("T/CECS 1234-2022", "T/CECS 1234"),
    ("DB11/T 1234-2020 北京市地方标准", "DB11/T 1234"),
])
def test_std_code_matches_real_forms(text, expected):
    match = er.STD_CODE.search(text)
    assert match is not None, text
    assert match.group(0).replace(" ", "").startswith(expected.replace(" ", ""))


def test_std_code_grants_no_admission():
    """标题含编号**不产生任何放行权**(v1 用它放行,v2 已废)。"""
    entry = _doc(
        title="GB/T 50362-2022 住宅性能评定标准",
        link="http://www.weboos.cn/assets/basicStandard/std_3037712.pdf",
        url="", authorityDomain="", authorityType="",
    )
    assert er.STD_CODE.search(entry["title"]) is not None
    assert er.standard_lane_admits(entry) is False


def test_local_administrative_document_classified_apart():
    """§4.2:含〔年份〕文号 且不匹配 STD_CODE → 地方性具体行政文件。"""
    assert er.classify_standard_document(
        "深圳市罗湖区住房和建设局文件 罗住建[2017]375号 关于推进整治“房中房”违法改建行为的通知"
    ) == er.STANDARD_CLASS_LOCAL_ADMIN
    assert er.classify_standard_document("住宅性能评定技术标准 GB/T 50362-2022") == \
        er.STANDARD_CLASS_STANDARD
    # 有文号但也有标准编号 → 仍归标准类(国标可交叉验证,风险等级不同)。
    assert er.classify_standard_document("DB11/T 1234-2020 建筑节能通知〔2020〕15号") == \
        er.STANDARD_CLASS_STANDARD


# ---------------------------------------------------------------------------
# §3 lane 接线:进 pack、不进 verified、不动 543-548 主链
# ---------------------------------------------------------------------------
def _run_collect(monkeypatch, *, deep_tier, doc_entries, citations=None):
    _clean_env(monkeypatch)
    calls: list[str] = []

    async def fake_citation_search(query, *, size=5, scope="webpage", scenario=None):
        calls.append(query)
        return {"citations": list(citations or [])}

    async def fake_scholar(query, *, size=8):
        return []

    async def fake_document(query, *, size=10):
        return [dict(e) for e in doc_entries]

    monkeypatch.setattr(pr, "citation_search", fake_citation_search)
    monkeypatch.setattr(pr, "scholar_evidence_search", fake_scholar)
    monkeypatch.setattr(pr, "document_evidence_search", fake_document)
    pack = asyncio.run(er.collect_evidence_pack(
        title="全屋定制板材怎么选", keyword="全屋定制板材", industry="全屋定制",
        client_brand="测试品牌", competitor_names=["竞品甲"], request_id="std-t",
        force=True, deep_tier=deep_tier, whitelist_names=["测试品牌", "竞品甲"],
    ))
    return pack, calls


def test_deep_tier_lane_lands_standard_citation_items(monkeypatch):
    pack, _ = _run_collect(monkeypatch, deep_tier=True, doc_entries=[
        _doc(),
        _doc(title="伪权威站点的标准", link="http://www.weboos.cn/a.pdf",
             authorityDomain="", authorityType=""),
    ])
    std_items = [i for i in pack["items"] if i.get("source_tier") == STANDARD_CITATION_TIER]
    assert len(std_items) == 1, "只有 .gov.cn 那条能进"
    item = std_items[0]
    assert item["verification_status"] == "search_result_only"
    assert item["standard_document_class"] == er.STANDARD_CLASS_STANDARD
    assert "存在性引用" in item["scope"]
    assert pack["evidence_supply"]["standard_item_count"] == 1
    assert pack["evidence_supply"]["standard_queries_issued"] >= 1


def test_standard_items_never_count_as_verified(monkeypatch):
    """§3.2:标准 lane 走旁路进 pack,不参与 verified 计数,不影响深档 >=3 门槛。"""
    pack, _ = _run_collect(monkeypatch, deep_tier=True, doc_entries=[_doc(), _doc(
        title="建筑装饰装修工程质量验收标准",
        link="https://zjt.fj.gov.cn/itp/x.pdf", authorityDomain="zjt.fj.gov.cn",
    )])
    assert pack["evidence_supply"]["standard_item_count"] == 2
    assert pack["evidence_supply"]["verified_count"] == 0
    assert pack["research_status"] == "verification_insufficient"


def test_compact_tier_issues_zero_document_calls(monkeypatch):
    """既有锁口径不许回退:紧凑档零新增调用(document 单次 3 credits)。"""
    _clean_env(monkeypatch)

    async def fake_citation_search(query, *, size=5, scope="webpage", scenario=None):
        return {"citations": []}

    async def boom(query, *, size=10):
        raise AssertionError("紧凑档不应触发文库检索")

    monkeypatch.setattr(pr, "citation_search", fake_citation_search)
    monkeypatch.setattr(pr, "scholar_evidence_search", lambda *a, **k: None)
    monkeypatch.setattr(pr, "document_evidence_search", boom)
    pack = asyncio.run(er.collect_evidence_pack(
        title="t", keyword="k", industry="i", client_brand="b",
        request_id="std-c", force=True, deep_tier=False,
    ))
    assert not [q for q in pack["queries"] if q.get("lane_kind") == "standard"]


def test_gov_cn_from_the_evidence_lane_gets_no_standard_tier(monkeypatch):
    """端到端版的入口约束:同一个 .gov.cn URL 走 citation lane 拿不到 standard_citation。"""
    pack, _ = _run_collect(
        monkeypatch, deep_tier=True, doc_entries=[],
        citations=[{
            "url": "https://www.gov.cn/zhengce/standard.html",
            "title": "住宅性能评定技术标准", "snippet": "摘要", "source": "www.gov.cn",
        }],
    )
    assert not [i for i in pack["items"] if i.get("source_tier") == STANDARD_CITATION_TIER]


def test_writing_rule_reaches_the_writer(monkeypatch):
    """§4 铁律与 lane 同批:pack 里一旦有标题级标准条目,写作侧就必须看到铁律。"""
    pack, _ = _run_collect(monkeypatch, deep_tier=True, doc_entries=[_doc()])
    rendered = render_evidence_pack_for_writer(pack)
    assert "存在性引用" in rendered and "内容性引用" in rendered
    assert "住宅性能评定技术标准" in rendered


def test_writing_rule_absent_without_standard_items(monkeypatch):
    """反向锁:别把"有铁律"写成"永远都贴铁律"。"""
    pack, _ = _run_collect(monkeypatch, deep_tier=True, doc_entries=[])
    assert "内容性引用" not in render_evidence_pack_for_writer(pack)


def test_normalize_keeps_marker_and_does_not_drift_legacy_hash():
    """标记活过 normalize;且历史 pack(无该键)的 manifest_hash 一个 bit 不变。"""
    marked = normalize_evidence_pack({"items": [{
        "evidence_id": "EV-001", "title": "住宅性能评定技术标准",
        "url": "https://www.gov.cn/a.docx", "source_tier": STANDARD_CITATION_TIER,
        "standard_document_class": er.STANDARD_CLASS_STANDARD,
        "created_at": "2026-07-29T00:00:00+00:00",
    }], "created_at": "2026-07-29T00:00:00+00:00"})
    assert marked["items"][0]["standard_document_class"] == er.STANDARD_CLASS_STANDARD

    legacy = {"items": [{
        "evidence_id": "EV-001", "title": "旧条目", "url": "https://example.com/a",
        "source_tier": "public_web_unclassified",
    }], "created_at": "2026-07-01T00:00:00+00:00"}
    out = normalize_evidence_pack(dict(legacy))
    assert "standard_document_class" not in out["items"][0]


# ---------------------------------------------------------------------------
# §4.3 判别锁
# ---------------------------------------------------------------------------
def _pack_with_standard(title="住宅性能评定技术标准 GB/T 50362-2022"):
    return normalize_evidence_pack({"items": [{
        "evidence_id": "EV-001", "title": title,
        "url": "https://www.gov.cn/a.docx",
        "source_tier": STANDARD_CITATION_TIER,
        "verification_status": "search_result_only",
        "standard_document_class": er.STANDARD_CLASS_STANDARD,
    }]})


CROSS_SENTENCE_FABRICATION = (
    "选板材要看环保等级。依据 GB 50210-2018《建筑装饰装修工程质量验收标准》，"
    "行业已有成熟规范。该标准要求甲醛释放量不超过 0.03mg/m³，这条硬指标决定了选材下限。"
)
SAME_SENTENCE_FABRICATION = "GB 50210-2018 规定甲醛释放量≤0.03mg/m³，属于强制要求。"
INNOCENT_NEARBY_DATA = (
    "依据 GB 50210-2018《建筑装饰装修工程质量验收标准》，验收有章可循。\n"
    "我们在 2024 年交付了 120 个整装项目，客户复购率 38%，平均工期 45 天。"
)
EXISTENCE_ONLY = (
    "依据 GB 50210-2018《建筑装饰装修工程质量验收标准》，装修验收环节有国家标准可依，"
    "选择施工方时可以要求对方按此执行。"
)


def _pack_for_gb50210():
    return normalize_evidence_pack({"items": [{
        "evidence_id": "EV-001",
        "title": "GB 50210-2018 建筑装饰装修工程质量验收标准",
        "url": "https://www.mohurd.gov.cn/a.pdf",
        "source_tier": STANDARD_CITATION_TIER,
        "verification_status": "search_result_only",
    }]})


def test_marker_is_actually_readable_from_the_gate_side():
    """🔴 §5.6:造一条 source_tier='standard_citation' 的证据 → 门必须能识别它是 title_only。

    不接线的话锁拿不到这个字段,会永远不触发而全绿。
    """
    names = guard.collect_title_only_standard_names(_pack_for_gb50210())
    assert names, "标记没被读到 —— 锁等于没装"
    assert any("GB 50210" in n for n in names)
    # 同一条若已拿到正文核验,就不该再受这条锁约束。
    verified = normalize_evidence_pack({"items": [{
        "evidence_id": "EV-001", "title": "GB 50210-2018 建筑装饰装修工程质量验收标准",
        "url": "https://www.mohurd.gov.cn/a.pdf", "source_tier": STANDARD_CITATION_TIER,
        "verification_status": "body_retrieved_claim_unverified",
    }]})
    assert guard.collect_title_only_standard_names(verified) == []


# --- 正文来源豁免(Deploy-CTO 部署前小修 2026-07-29)-------------------------
#
# 为什么必须有:本锁拦的是「只拿到标题就往下写条款数值」。§6 已把豆包定位成唯一能
# 支撑**内容性引用**的源 —— C 臂一上线,同一份标准会同时存在「标题级条目」和
# 「带正文条目」。没有豁免的话,这把锁会**精确拦掉这条 lane 被设计来允许的那个用例**,
# 而且 overridable=False 没有逃生口,现象是"明明有正文来源却被拦、还签不了字"。
_BODY_LONG_ENOUGH = (
    "本条目为带正文来源。GB 50210-2018《建筑装饰装修工程质量验收标准》正文全文抓取成功，"
    "其中对甲醛释放量、验收分项划分、材料进场复验等条款均有明确规定。" * 4
)


def _pack_gb50210_plus_bodied_source(status="body_retrieved_claim_unverified"):
    """同一份标准:一条标题级 + 一条带正文。"""
    return normalize_evidence_pack({"items": [
        {
            "evidence_id": "EV-001",
            "title": "GB 50210-2018 建筑装饰装修工程质量验收标准",
            "url": "https://www.mohurd.gov.cn/a.pdf",
            "source_tier": STANDARD_CITATION_TIER,
            "verification_status": "search_result_only",
        },
        {
            "evidence_id": "EV-002",
            # 正文里写的是不带年份的 `GB 50210` —— 换个写法也必须能对上,
            # 否则豁免形同虚设。
            "title": "装修验收怎么做：GB 50210 全文解读",
            "url": "https://example.com/read",
            "source_tier": "public_web_unclassified",
            "verification_status": status,
            "excerpt": _BODY_LONG_ENOUGH,
        },
    ]})


def test_bodied_source_for_same_standard_exempts_the_lock():
    """🔴 同一标准另有带正文来源 → 该标准不进 title_only 集合,锁对它不触发。"""
    pack = _pack_gb50210_plus_bodied_source()
    assert guard.collect_title_only_standard_names(pack) == [], "带正文来源在场时不该再算 title_only"
    assert guard.scan_standard_citation_misuse(CROSS_SENTENCE_FABRICATION, pack) == []
    assert guard.evaluate_standard_citation_discipline(CROSS_SENTENCE_FABRICATION, pack) is None


def test_same_case_without_the_bodied_source_still_turns_red():
    """🔴 配对锁:去掉那条带正文条目,同一篇正文必须转红。

    证明上面那条绿不是"把锁废了",而是豁免真的按来源在判。
    """
    assert guard.scan_standard_citation_misuse(CROSS_SENTENCE_FABRICATION, _pack_for_gb50210())


#: VERIFIED 态在 normalize 里有 fail-closed:provenance 不全会被降级
#: (evidence_pack.py:134-140)。要测"更强的状态也豁免",夹具就得把 provenance 备齐,
#: 否则测的其实是降级后的 search_result_only —— 那是假绿。
_VERIFIED_PROVENANCE = {
    "claim_span_verified": {
        "canonical_body_hash": "a" * 64,
        "body_hash_algorithm": "sha256",
        "span": {"type": "canonical_body_exact_quote", "start": 0, "end": 20},
        "verification_version": "v1",
        "verification_provider": "deploycto-test",
        "verification_model": "test-model",
    },
    "official_record": {"official_record_id": "REC-001"},
    "human_verified": {
        "human_reviewed_by": "reviewer",
        "human_reviewed_at": "2026-07-29T00:00:00+00:00",
        "human_review_reason": "人工确认正文与标准条款一致",
    },
}


@pytest.mark.parametrize("status", sorted(_VERIFIED_PROVENANCE))
def test_stronger_than_body_statuses_also_exempt(status):
    """比"抓到正文"更强的状态(已核验/官方记录/人工确认)同样算有来源可依。

    只认 body_retrieved 一个状态的话,标准被完整核验之后反而还被拦 —— 越对越挨打。
    """
    pack = normalize_evidence_pack({"items": [
        {
            "evidence_id": "EV-001",
            "title": "GB 50210-2018 建筑装饰装修工程质量验收标准",
            "url": "https://www.mohurd.gov.cn/a.pdf",
            "source_tier": STANDARD_CITATION_TIER,
            "verification_status": "search_result_only",
        },
        {
            "evidence_id": "EV-002",
            "title": "装修验收怎么做：GB 50210 全文解读",
            "url": "https://example.com/read",
            "source_tier": "public_web_unclassified",
            "verification_status": status,
            "excerpt": _BODY_LONG_ENOUGH,
            **_VERIFIED_PROVENANCE[status],
        },
    ]})
    # 前置:夹具真的把状态带过了 normalize(不然下面的绿是降级后的假绿)。
    assert pack["items"][1]["verification_status"] == status
    assert guard.collect_title_only_standard_names(pack) == []


def test_snippet_only_item_never_buys_an_exemption():
    """🔴 只有搜索摘要(无正文)的条目不许换来豁免 —— 否则等于回到"摘要即证据"。

    excerpt 够长但 verification_status 仍是 search_result_only:必须照拦。
    """
    pack = _pack_gb50210_plus_bodied_source(status="search_result_only")
    assert guard.collect_title_only_standard_names(pack), "摘要不能买豁免"
    assert guard.scan_standard_citation_misuse(CROSS_SENTENCE_FABRICATION, pack)


def test_body_source_for_a_different_standard_does_not_exempt():
    """带正文来源讲的是**别的**标准 → 本标准照拦(豁免必须按标准逐个算)。"""
    pack = normalize_evidence_pack({"items": [
        {
            "evidence_id": "EV-001",
            "title": "GB 50210-2018 建筑装饰装修工程质量验收标准",
            "url": "https://www.mohurd.gov.cn/a.pdf",
            "source_tier": STANDARD_CITATION_TIER,
            "verification_status": "search_result_only",
        },
        {
            "evidence_id": "EV-002",
            "title": "JGJ/T 304-2013 住宅室内装饰装修工程质量验收规范全文",
            "url": "https://example.com/other",
            "source_tier": "public_web_unclassified",
            "verification_status": "body_retrieved_claim_unverified",
            "excerpt": "JGJ/T 304-2013 正文。" * 40,
        },
    ]})
    assert guard.collect_title_only_standard_names(pack), "别的标准的正文不该豁免本标准"
    assert guard.scan_standard_citation_misuse(CROSS_SENTENCE_FABRICATION, pack)


def test_body_threshold_matches_the_verified_gate():
    """口径锁:门槛数字与 evidence_research 的 verified 门槛是同一个,别各造一个数。"""
    assert guard.BODY_EVIDENCE_MIN_CHARS == 300


def test_cross_sentence_anaphoric_fabrication_turns_red():
    findings = guard.scan_standard_citation_misuse(CROSS_SENTENCE_FABRICATION, _pack_for_gb50210())
    assert findings, "跨句 + 回指的编造必须转红"
    assert findings[0]["distance"] >= 1, "这条正是跨句形态"


def test_same_sentence_fabrication_turns_red():
    assert guard.scan_standard_citation_misuse(SAME_SENTENCE_FABRICATION, _pack_for_gb50210())


def test_unrelated_numbers_after_standard_name_stay_green():
    """设计理由 1+3:文章本来就该有数据。没有回指、没挂在标准名下的数字不许误伤。"""
    assert guard.scan_standard_citation_misuse(INNOCENT_NEARBY_DATA, _pack_for_gb50210()) == []


def test_existence_only_citation_stays_green():
    assert guard.scan_standard_citation_misuse(EXISTENCE_ONLY, _pack_for_gb50210()) == []


def test_publication_year_is_existence_metadata_not_a_content_claim():
    """`该标准于 2022 年发布` 是检索结果自带的**存在性元信息**,拿它转红属误伤。"""
    text = (
        "依据 GB 50210-2018《建筑装饰装修工程质量验收标准》，装修验收有据可依。"
        "该标准于 2018 年发布，是现行国家标准。"
    )
    assert guard.scan_standard_citation_misuse(text, _pack_for_gb50210()) == []


def test_duration_years_still_count_as_a_content_claim():
    """反向锁:别把"年一律不算"写成漏网 —— `该标准要求质保不少于 5 年` 仍是条款。"""
    text = (
        "依据 GB 50210-2018《建筑装饰装修工程质量验收标准》。"
        "该标准要求防水工程质保不少于 5 年。"
    )
    assert guard.scan_standard_citation_misuse(text, _pack_for_gb50210())


def test_code_without_year_suffix_still_matched():
    """标题是 `GB 50210-2018`、正文只写 `GB 50210` —— 改个写法不许绕过整把锁。"""
    text = "依据 GB 50210 的验收要求。该标准规定甲醛释放量不超过 0.03mg/m³。"
    assert guard.scan_standard_citation_misuse(text, _pack_for_gb50210())


def test_no_standard_items_means_no_scanning():
    plain = normalize_evidence_pack({"items": [{
        "evidence_id": "EV-001", "title": "某媒体报道", "url": "https://example.com/a",
        "source_tier": "public_web_unclassified",
    }]})
    assert guard.scan_standard_citation_misuse(CROSS_SENTENCE_FABRICATION, plain) == []


def test_publication_gate_blocks_and_is_not_human_overridable():
    """挂载点:走既有发布前 fail-closed 通道,签发覆盖不了。"""
    from services.article_review_gate import evaluate_publication_eligibility

    verdict = _evaluate_with_fake_row(
        evaluate_publication_eligibility,
        content=CROSS_SENTENCE_FABRICATION,
        evidence_pack=_pack_for_gb50210(),
        human="approved",
        machine="approved",
    )
    assert verdict["eligible"] is False
    assert verdict["reason"] == "standard_citation_content_claim"
    assert verdict["overridable"] is False
    assert "存在性引用" in verdict["message"]


def test_publication_gate_lets_existence_only_through():
    from services.article_review_gate import evaluate_publication_eligibility

    verdict = _evaluate_with_fake_row(
        evaluate_publication_eligibility,
        content=EXISTENCE_ONLY,
        evidence_pack=_pack_for_gb50210(),
        human="", machine="approved",
    )
    assert verdict["eligible"] is True


class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self._last = None

    def execute(self, sql, params=None):
        self._last = sql

    def fetchone(self):
        return self._row


def _evaluate_with_fake_row(fn, *, content, evidence_pack, human, machine):
    import hashlib

    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    row = {
        "id": 1, "content": content, "current_content_hash": content_hash,
        "style_code": "ranking", "style": "ranking", "style_family": "multi_brand_comparison",
        "article_review_status": machine, "article_human_review_status": human,
        "article_review": {
            "reviewed_content_hash": content_hash,
            "reviewed_evidence_manifest_hash": "mh",
        },
        "evidence_manifest_hash": "mh",
        "publication_profile": "standard", "platform_review": {},
        "evidence_pack": evidence_pack,
        # human=='approved' 分支要读的签发快照(第二次 fetchone 也返同一行,足够本用例)
        "reviewed_content_hash": content_hash,
    }
    return fn(1, cursor=_FakeCursor(row), _evaluate_when_rollout_disabled=True)


# ---------------------------------------------------------------------------
# §5.7 变异(五个,全部必须转红)
# ---------------------------------------------------------------------------
def test_mutation_1_endswith_back_to_in(monkeypatch):
    """① `endswith` 改回 `in` → 伪装域名被放行。"""
    def mutated(value: str) -> bool:
        return ".gov.cn" in str(value or "")

    monkeypatch.setattr(er, "_gov_host", mutated)
    assert er.standard_lane_admits(
        _doc(link="https://evil.com/?ref=x.gov.cn", url="", authorityDomain="")
    ) is True, "变异①没被本锁抓到"


def test_mutation_2_drop_optional_slash_t(monkeypatch):
    """② 去掉 `(?:/T)?` → DB11/T 1234-2020 被错分成地方性行政文件。"""
    mutated = re.compile(
        r'(GB/T|GBT|GB|JGJ/T|JGJ|CJJ|DB[0-9]{2}|T/[A-Z]{2,6})\s*[-—]?\s*[0-9]{4,5}'
    )
    title = "DB11/T 1234-2020 建筑节能通知〔2020〕15号"
    assert er.classify_standard_document(title) == er.STANDARD_CLASS_STANDARD
    monkeypatch.setattr(er, "STD_CODE", mutated)
    assert er.classify_standard_document(title) == er.STANDARD_CLASS_LOCAL_ADMIN, \
        "变异②没被本锁抓到"


def test_mutation_3_window_shrunk_to_same_sentence(monkeypatch):
    """③ "3 句以内" 缩回 "同句" → 跨句编造漏网。"""
    assert guard.scan_standard_citation_misuse(CROSS_SENTENCE_FABRICATION, _pack_for_gb50210())
    monkeypatch.setattr(guard, "_window_indices", lambda sentences, start: [start])
    assert guard.scan_standard_citation_misuse(CROSS_SENTENCE_FABRICATION, _pack_for_gb50210()) == [], \
        "变异③没被本锁抓到"


def test_mutation_4_reuse_government_suffixes(monkeypatch):
    """④ 本 lane 常量换成 `_GOVERNMENT_SUFFIXES`(即放进 `.gov`)→ 美国政府域被放行。"""
    monkeypatch.setattr(er, "_STANDARD_LANE_GOV_SUFFIX", er._GOVERNMENT_SUFFIXES)
    assert er._gov_host("https://www.whitehouse.gov/x.pdf") is True, "变异④没被本锁抓到"


def test_mutation_5_drop_the_source_entry_constraint():
    """⑤ 去掉外层 `source == 'metaso.document'` → 任意 .gov.cn 网页白拿免正文特权。

    v1 强调这是最重要的闸却没配锁,v2 补上。
    """
    def mutated(item) -> bool:
        return any(er._gov_host(item.get(f)) for f in er._STANDARD_LANE_HOST_FIELDS)

    from_other_lane = _doc(source="metaso_mcp")
    assert er.standard_lane_admits(from_other_lane) is False
    assert mutated(from_other_lane) is True, "变异⑤没被本锁抓到"


def test_mutation_6_drop_the_body_source_exemption(monkeypatch):
    """⑥ 去掉正文来源豁免 → 明明另有带正文来源的标准仍被拦(且不可签发覆盖)。

    这是 C 臂上线后最贵的那个假阳性:overridable=False 没有逃生口。
    """
    pack = _pack_gb50210_plus_bodied_source()
    assert guard.collect_title_only_standard_names(pack) == []

    monkeypatch.setattr(guard, "_standards_covered_by_a_bodied_source", lambda items: set())
    assert guard.collect_title_only_standard_names(pack), "变异⑥没被本锁抓到"
    blocked = guard.evaluate_standard_citation_discipline(CROSS_SENTENCE_FABRICATION, pack)
    assert blocked and blocked["overridable"] is False, "变异⑥没被本锁抓到"
