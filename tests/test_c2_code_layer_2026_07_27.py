"""工单 C-2 代码层判别锁(2026-07-27)。

T1 · 图片占位符交付链:生产 10 篇深档实证 8/10 正文成品裸露
     `[CLIENT_IMAGE asset_id=...]`。修复面:预览渲染剥 [NEED_IMAGE]、
     preview 端点去 fail-open 短路、渲染失败 fail-closed 剥离、
     详情 API 交付副本 content_export、前端显示/ZIP 下载零裸露、
     rewrite 保存链补配图管线。
T2 · 逐家检索证据喂进竞品卡:根因1 深档预判恒 False(逐家 query 生产从未发出)、
     根因2 source_cap 头部截断切掉竞品结果、根因3 核验空选择整篇归零、
     根因4 证据平铺不分组模型捞不出。

每条锁都对应一个可执行变异(见交付报告 · 变异脚本逐条转红)。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", autouse=True)
def _preload_server_module():
    """server 在本文件任何测试跑之前导入一次(行为级端点锁要调真 handler)。

    不预载的话,先跑的检索/核验用例会把日志流绑到早已关闭的 pytest 捕获区,
    轮到端点锁再导入 server 时炸 "I/O operation on closed file" —— 与被测行为无关。
    """
    import server  # noqa: F401
    yield


# ===========================================================================
# T1 · 占位符交付链
# ===========================================================================
def test_render_for_preview_strips_need_image_requests():
    """预览与发布同口径:[NEED_IMAGE](含保底 awaiting_client_asset)绝不裸露。

    变异:render_for_preview 去掉 strip_image_requests → 本锁转红。
    """
    from services.image_placeholder import render_for_preview

    body = (
        "正文段落。\n\n"
        "[NEED_IMAGE role=brand_intro purpose=品牌形象 status=awaiting_client_asset]\n\n"
        "尾段。"
    )
    out = render_for_preview(body, None)
    assert "[NEED_IMAGE" not in out
    assert "正文段落。" in out and "尾段。" in out


def test_render_for_preview_without_brand_strips_client_images():
    """brand_id 缺席时 fail-closed:逐图归属校验不过 → 静默删,不裸返。"""
    from services.image_placeholder import render_for_preview

    out = render_for_preview("a\n\n[CLIENT_IMAGE asset_id=7 role=case]\n\nb", None)
    assert "[CLIENT_IMAGE" not in out


def test_render_fail_closed_wrappers_strip_on_exception(monkeypatch):
    """渲染链抛任何异常都必须剥离占位符,不许把原文透出去。

    变异:fail_closed 包装改成 except: return content → 本锁转红。
    """
    import services.image_placeholder as ph

    def _boom(*args, **kwargs):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(ph, "_render", _boom)
    body = "x\n[CLIENT_IMAGE asset_id=3 role=hero]\n[NEED_IMAGE role=case]\ny"
    for fn in (ph.render_for_preview_fail_closed, ph.render_for_publish_fail_closed):
        out = fn(body, 5)
        assert "[CLIENT_IMAGE" not in out and "[NEED_IMAGE" not in out
        assert "x" in out and "y" in out


def test_preview_endpoints_have_no_fail_open_ternary():
    """image_asset_api 两处 `if brand_id else content` fail-open 短路必须绝迹。"""
    src = (ROOT / "api" / "image_asset_api.py").read_text(encoding="utf-8")
    assert "if brand_id else content" not in src
    assert "if brand_id else new_content" not in src
    assert src.count("render_for_preview_fail_closed") >= 2


def test_awaiting_detail_render_is_fail_closed():
    """媒介盒子「待确认」详情:渲染异常不得 except:pass 裸返原文。"""
    src = (ROOT / "api" / "meijiehezi_api.py").read_text(encoding="utf-8")
    assert "render_for_preview(article_content, _preview_bid)" not in src
    assert "render_for_preview_fail_closed(article_content, _preview_bid)" in src


def test_article_detail_exposes_delivery_copy():
    """GET /api/articles/{id} 必须带 content_export(发布口径渲染·失败剥离)。

    content 是编辑 SSOT 保留占位符;下载/导出面只许拿 content_export。
    变异:删掉 content_export 组装段 → 本锁转红。
    """
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    start = src.index("def api_get_article(")
    segment = src[start:start + 4000]
    assert 'article["content_export"]' in segment
    assert "render_for_publish_fail_closed" in segment
    assert "strip_client_images" in segment  # 兜底分支也必须剥离


def test_zip_download_uses_delivery_copy_not_raw_content():
    """批量下载 ZIP 是唯一"成品拿走"出口:必须用 content_export/剥离兜底。"""
    tsx = (ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx").read_text(
        encoding="utf-8"
    )
    start = tsx.index("downloadSelectedArticles")
    segment = tsx[start:start + 2500]
    assert "content_export" in segment
    assert "stripInternalPlaceholders" in segment
    assert "data.article?.content || data.content;" not in segment


def test_preview_fallback_never_shows_raw_placeholders():
    """渲染副本缺席/请求失败时,前端显示走剥离兜底,不裸显 content 原文。"""
    tsx = (ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx").read_text(
        encoding="utf-8"
    )
    assert "previewData.contentRendered || previewData.content}" not in tsx
    assert "stripInternalPlaceholders(previewData.content)" in tsx
    # 剥离函数本体必须同时覆盖两种占位符
    match = re.search(r"function stripInternalPlaceholders[\s\S]{0,400}?\n\}", tsx)
    assert match, "stripInternalPlaceholders 函数缺失"
    assert "CLIENT_IMAGE" in match.group(0) and "NEED_IMAGE" in match.group(0)


def test_rewrite_save_path_has_image_placeholder_pipeline():
    """保存路径 2/3(rewrite)补配图管线:模型新吐的 [NEED_IMAGE] 不得原样入库。

    定位到 rewrite 的 INSERT 语句之前的段落,断言选图 + 非保底剥除都在。
    """
    src = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")
    anchor = src.index("保存路径 2/3 补配图占位管线")
    segment = src[anchor:anchor + 2000]
    assert "select_images_for_article" in segment
    assert "status=awaiting_client_asset" in segment  # 保底占位保留口径与首存一致


# ===========================================================================
# T2 · 逐家检索证据 → 竞品卡
# ===========================================================================
def test_deep_probe_fires_on_candidate_capacity_before_research():
    """[根因1] 研究前判定不再被 verified 锁死:候选容量轴独立触发逐家检索。

    生产复现:candidates=9、无 pack → 研究前 plan target=3500 → 旧判定恒 False。
    变异:probe 去掉 count_verified_candidates 分支 → 本锁转红。
    """
    from writing.evidence_research import deep_tier_research_probe

    topic = {
        "_competitor_candidate_pool": [
            {"name": f"候选{i}", "name_verified": True} for i in range(8)
        ],
    }
    assert deep_tier_research_probe("ranking_v2", topic) is True


def test_deep_probe_stays_compact_for_thin_rosters_and_other_families():
    """反向锁:候选不足 3 家(含客户)不触发;非榜单族永远不触发(零成本溢出)。"""
    from writing.evidence_research import deep_tier_research_probe

    thin = {"_competitor_candidate_pool": [{"name": "唯一家", "name_verified": True}]}
    assert deep_tier_research_probe("ranking_v2", thin) is False

    rich_pool = {
        "_competitor_candidate_pool": [
            {"name": f"候选{i}", "name_verified": True} for i in range(8)
        ],
    }
    assert deep_tier_research_probe("practical_guide", rich_pool) is False
    assert deep_tier_research_probe(None, rich_pool) is False


def test_probe_threshold_stays_aligned_with_length_contract_gate():
    """门槛常量与 plan 榜单族 `candidates<=2→紧凑` 是同一条界,防两处漂移。"""
    from writing.article_length_contract import (
        RANKING_DEEP_RESEARCH_MIN_CANDIDATES,
        build_article_length_plan,
    )

    assert RANKING_DEEP_RESEARCH_MIN_CANDIDATES == 3
    # candidates 恰在门槛上 + verified 证据到位 → plan 必须能进深档(default deep floor)
    pack = {
        "items": [
            {
                "evidence_id": f"EV-{i:03d}",
                "claim": "x",
                "url": f"https://example.com/{i}",
                "verification_status": "human_verified",
                "human_reviewed_by": "reviewer",
                "human_reviewed_at": "2026-07-27",
                "human_review_reason": "工单C-2判别锁",
                "publisher": f"媒体{i}",
            }
            for i in range(3)
        ]
    }
    plan = build_article_length_plan(
        "ranking_v2", evidence_pack=pack,
        verified_candidate_count=RANKING_DEEP_RESEARCH_MIN_CANDIDATES,
    )
    assert "few_verified_candidates_no_padding" not in plan["reasons"]


def test_deep_pack_without_entity_coverage_still_needs_research():
    """[根因1 补] 主题式旧 pack 凑够 3 条也不能让深档跳过逐家检索。"""
    from writing.evidence_research import evidence_pack_needs_research

    stale = {"items": [{"evidence_id": f"EV-{i}"} for i in range(5)]}
    assert evidence_pack_needs_research(stale, deep_tier=True) is True

    covered = dict(stale, evidence_supply={"deep_tier": True})
    assert evidence_pack_needs_research(covered, deep_tier=True) is False

    # 紧凑档行为逐字不变:够 3 条就不再研究
    assert evidence_pack_needs_research(stale, deep_tier=False) is False
    assert evidence_pack_needs_research({"items": [{}]}, deep_tier=False) is True
    assert evidence_pack_needs_research(None, deep_tier=False) is True


def test_interleave_guarantees_every_entity_survives_source_cap():
    """[根因2] cap 内每家实体都必须有结果进入 items,头部截断转红。

    构造:4 条主题 lane 各 5 条结果先占位(旧逻辑 cap=12 时实体全灭)。
    """
    from writing.evidence_research import _interleave_discovered_by_lane

    discovered = []
    for lane_i in range(4):  # 主题 lane 在前,各 5 条
        for res_i in range(5):
            discovered.append(("support", {"url": f"https://t{lane_i}-{res_i}.com"},
                               f"主题query{lane_i}", ""))
    entities = ["客户品牌", "竞品甲", "竞品乙", "竞品丙"]
    for name in entities:  # 每家 2 条 lane 各 3 条结果
        for lane_j in range(2):
            for res_i in range(3):
                discovered.append(("support", {"url": f"https://{name}-{lane_j}-{res_i}.com"},
                                   f"{name} query{lane_j}", name))

    out = _interleave_discovered_by_lane(discovered, 12)
    assert len(out) == 12
    covered = {tup[3] for tup in out if tup[3]}
    assert covered == set(entities), f"实体覆盖不全: {covered}"
    # 旧逻辑对照:头部截断 12 条全是主题结果,实体零覆盖
    old = discovered[:12]
    assert {t[3] for t in old if t[3]} == set(), "构造前提失效:头部截断本应切掉全部实体结果"


def test_collect_pipeline_keeps_entity_coverage_under_source_cap(monkeypatch):
    """[根因2 调用点锁] collect_evidence_pack 深档必须走轮转选源,不是头部截断。

    构造:3 实体 → source_cap=17,主题 4 lane × 5 条 = 20 条先占位 ——
    头部截断会让 items 里实体覆盖为空;轮转必须三家全覆盖。
    变异:调用点改回 discovered[:source_cap] → 本锁转红。
    """
    import tools.search.metaso_mcp as metaso
    import writing.evidence_research as er

    async def fake_search(query, size=5):
        return {"citations": [
            {"url": f"https://ev.example/{abs(hash(query)) % 999983}/{i}",
             "title": f"{query} 结果{i}", "snippet": "摘要"}
            for i in range(size)
        ]}

    async def fake_reader(url, format="markdown"):
        raise RuntimeError("本用例不读正文")

    monkeypatch.setattr(metaso, "metaso_search_with_citations", fake_search, raising=False)
    monkeypatch.setattr(metaso, "metaso_web_reader", fake_reader, raising=False)
    monkeypatch.setattr(er, "corpus_lead_terms", lambda industry, limit=6: [])

    whitelist = ["客户品牌", "竞品甲", "竞品乙"]
    pack = asyncio.run(er.collect_evidence_pack(
        title="行业哪家好", keyword="行业关键词", industry="行业",
        client_brand="客户品牌", competitor_names=["竞品甲", "竞品乙"],
        request_id="c2-interleave-lock", force=True,
        deep_tier=True, whitelist_names=whitelist,
    ))
    covered = {item.get("entity") for item in pack["items"] if item.get("entity")}
    assert covered == set(whitelist), f"实体覆盖不全: {covered}"
    # 主题 lane 也不许被挤光(轮转是公平配额,不是实体独占)
    assert any(not item.get("entity") for item in pack["items"])


def test_evidence_items_carry_entity_and_survive_normalize():
    """[根因4 前置] 条目的实体归属必须活过 normalize_evidence_pack(会重建键集)。"""
    from writing.evidence_pack import normalize_evidence_pack

    pack = normalize_evidence_pack({
        "items": [
            {"evidence_id": "EV-001", "claim": "x", "url": "https://a.com", "entity": "甲公司"},
            {"evidence_id": "EV-002", "claim": "y", "url": "https://b.com"},
        ],
    }, request_id="c2-lock")
    assert pack["items"][0]["entity"] == "甲公司"
    assert pack["items"][1]["entity"] is None


def test_render_evidence_pack_by_entity_groups_and_flags_gaps():
    """[根因4] 分组块:有证据的家列 标题|媒体|日期,零证据的家显式"留白收短"。"""
    from writing.evidence_pack import render_evidence_pack_by_entity

    pack = {
        "items": [
            {
                "evidence_id": "EV-001", "title": "甲公司获省级认证", "publisher": "某某日报",
                "published_at": "2026-05-01", "verification_status": "claim_span_verified",
                # [R3-1 2026-08-16] 分组块与 for_writer 同门:带实体归属的条目
                # 只有 entity_confirmed 进分组(缺 binding = 候选不进 —— 生产
                # 1,097 条形态)。本锁判的是分组格式/留白收短,与绑定轴正交,
                # 夹具按新门补 confirmed;绑定轴四态另有专锁(test_p0_2)。
                "entity": "甲公司", "scope": "", "binding_state": "entity_confirmed",
            },
            {
                "evidence_id": "EV-002", "title": "行业白皮书", "publisher": "研究院",
                "published_at": None, "verification_status": "search_result_only",
                "entity": None, "scope": "搜索问题：乙公司 案例",
            },
        ],
    }
    block = render_evidence_pack_by_entity(pack, ["甲公司", "乙公司", "丙公司"])
    assert "【逐家检索证据" in block
    assert "甲公司获省级认证 | 某某日报 | 2026-05-01" in block
    assert "乙公司" in block and "行业白皮书" in block          # scope 兜底匹配
    assert "丙公司：本次检索未获可用证据" in block
    assert "留白收短" in block
    assert render_evidence_pack_by_entity(pack, []) == ""


def test_deep_spec_embeds_per_entity_evidence_next_to_roster():
    """[根因4] 分组块必须嵌进深档规格(成卡名单旁),不是丢回一维证据池。

    变异:build_deep_ranking_structure_spec 忽略 per_entity_evidence_block → 转红。
    """
    from writing.templates.canonical_family_templates import (
        build_deep_ranking_structure_spec,
    )

    plan = {"target_chars": 16000, "verified_candidate_count": 7}
    whitelist = [f"品牌{i}" for i in range(7)]
    block = "【逐家检索证据（按品牌分组 · 写卡时本家优先用本组）】\n- 品牌0：..."
    spec = build_deep_ranking_structure_spec(
        plan, whitelist=whitelist, per_entity_evidence_block=block,
    )
    assert "【逐家检索证据" in spec
    roster_pos = spec.index("成卡名单")
    evidence_pos = spec.index("【逐家检索证据")
    assert 0 < evidence_pos - roster_pos < 600, "分组块必须紧跟成卡名单,不许离散"

    spec_plain = build_deep_ranking_structure_spec(plan, whitelist=whitelist)
    assert "【逐家检索证据" not in spec_plain


def test_generator_wires_grouped_evidence_into_deep_spec():
    """接线锁:生成器把分组块传给规格构造(不是构造了不接)。"""
    src = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")
    assert "render_evidence_pack_by_entity" in src
    anchor = src.index("per_entity_evidence_block=_per_entity_block")
    assert anchor > 0


def test_verifier_retries_once_on_empty_selection(monkeypatch):
    """[根因3] 空 selections(生产 ~25%)重试一次;有产出/API 全挂都不重试。

    变异:删掉 retry 分支 → 本锁转红。
    """
    import tools.multi_llm_caller as mlc
    from writing.evidence_verifier import select_and_verify_claim_spans

    body = "甲公司二〇二六年通过省级质量认证并公开产能数据满足行业标准要求。"
    candidates = [{"evidence_id": "EV-001", "title": "报道", "relationship": "support",
                   "body": body}]
    quote = body[:20]

    calls = {"n": 0}

    class _FakeCaller:
        def __init__(self, *args, **kwargs):
            pass

        async def call(self, prompt, verbose=False):
            calls["n"] += 1
            if calls["n"] == 1:
                return '{"selections":[]}', "DeepSeek"
            return (
                '{"selections":[{"evidence_id":"EV-001","exact_quote":"%s"}]}' % quote,
                "DeepSeek",
            )

    monkeypatch.setattr(mlc, "MultiLLMCaller", _FakeCaller)
    result = asyncio.run(select_and_verify_claim_spans(
        candidates, target_question="甲公司怎么样",
    ))
    assert calls["n"] == 2, "空选择必须重试一次"
    assert "EV-001" in result
    assert result["EV-001"]["verification_status"] == "claim_span_verified"

    # 第一次就有产出 → 不重试
    calls["n"] = 0

    class _FirstShot(_FakeCaller):
        async def call(self, prompt, verbose=False):
            calls["n"] += 1
            return (
                '{"selections":[{"evidence_id":"EV-001","exact_quote":"%s"}]}' % quote,
                "DeepSeek",
            )

    monkeypatch.setattr(mlc, "MultiLLMCaller", _FirstShot)
    result = asyncio.run(select_and_verify_claim_spans(
        candidates, target_question="甲公司怎么样",
    ))
    assert calls["n"] == 1
    assert "EV-001" in result

    # API 全挂 → 直接空,不再烧第二次
    calls["n"] = 0

    class _AllDown(_FakeCaller):
        async def call(self, prompt, verbose=False):
            calls["n"] += 1
            return "[所有API均失败]", ""

    monkeypatch.setattr(mlc, "MultiLLMCaller", _AllDown)
    result = asyncio.run(select_and_verify_claim_spans(
        candidates, target_question="甲公司怎么样",
    ))
    assert calls["n"] == 1 and result == {}


def test_generator_probe_call_site_uses_research_probe():
    """接线锁:生成器的深档预判走 deep_tier_research_probe,不再只看研究前 plan。"""
    src = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")
    assert "deep_tier_research_probe(style_code, topic)" in src
    assert "evidence_pack_needs_research(" in src
    # 旧的恒假判定形态必须绝迹
    assert "_is_deep_tier_topic = int(_pre.get(" not in src


# ===========================================================================
# T1 · 行为级端点锁(二审返工 ②):mock-row 调真 handler,断言输出零占位符。
# 源码字符串断言换皮即绕(Review 已变异实证)——以下三锁只看行为,
# 对"语义等同 fail-open"变异(保留全部魔法字符串、仅改数据流)必须转红。
# ===========================================================================
class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _FakeConn:
    def __init__(self, rows):
        self._cursor = _FakeCursor(rows)

    def cursor(self):
        return self._cursor

    def close(self):
        pass


_PLACEHOLDER_BODY = (
    "开头段。\n\n"
    "[CLIENT_IMAGE asset_id=7 role=case caption=\"车间\"]\n\n"
    "[NEED_IMAGE role=brand_intro status=awaiting_client_asset]\n\n"
    "结尾段。"
)


def test_endpoint_article_preview_behavior_zero_placeholders(monkeypatch):
    """article-preview 真 handler:brand_id=NULL 的 mock row → 响应零占位符。

    语义等同 fail-open 变异(brand 为空绕过渲染、字符串全保留)→ 本锁转红。
    """
    import api.image_asset_api as api_mod
    import db.connection as dbc
    import db.brand_image_assets_db as assets_db

    row = {"content": _PLACEHOLDER_BODY, "generation_request_snapshot": None, "brand_id": None}
    monkeypatch.setattr(dbc, "get_connection", lambda: _FakeConn([row]))
    monkeypatch.setattr(api_mod, "require_brand_access", lambda *a, **k: None)
    monkeypatch.setattr(api_mod, "get_image_asset", lambda aid: None)
    monkeypatch.setattr(assets_db, "get_image_asset", lambda aid: None, raising=False)

    resp = asyncio.run(api_mod.render_article_preview(1, object()))
    assert resp["success"] is True
    assert "[CLIENT_IMAGE" not in resp["content"] and "[NEED_IMAGE" not in resp["content"]
    assert "开头段。" in resp["content"] and "结尾段。" in resp["content"]


class _TolerantCursor:
    """server 模块导入期的吸收式游标(init_db 等启动链 SQL 全吸收)。"""

    rowcount = 0
    description = None

    def execute(self, sql, params=None):
        pass

    def executemany(self, sql, seq=None):
        pass

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, n=None):
        return []

    def close(self):
        pass

    def __iter__(self):
        return iter([])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _TolerantConn:
    autocommit = False
    closed = 0

    def cursor(self, *a, **k):
        return _TolerantCursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _import_server_with_neutered_db(monkeypatch):
    """导入 server(启动链在 conftest 指向的测试库上自建 schema;与既有
    server-route 测试同一导入方式)。handler 调用期的 DB 全量 mock。"""
    import sys

    if "server" not in sys.modules:
        import server  # noqa: F401
    return sys.modules["server"]


def test_endpoint_article_detail_export_behavior_zero_placeholders(monkeypatch):
    """GET /api/articles/{id} 真 handler:content_export(ZIP 下载源)零占位符;
    content 本体按设计保留占位符(编辑 SSOT)——两者必须同时成立。"""
    server = _import_server_with_neutered_db(monkeypatch)
    import db.diagnosis_db as ddb

    art = {
        "id": 5, "quote_id": 9, "content": _PLACEHOLDER_BODY,
        "generation_request_snapshot": None,
    }
    monkeypatch.setattr(ddb, "get_connection", lambda: _FakeConn([art, {"brand_id": None}]))
    monkeypatch.setattr(server, "_require_article_access", lambda *a, **k: None)

    resp = server.api_get_article(5, object())
    export = resp["article"]["content_export"]
    assert "[CLIENT_IMAGE" not in export and "[NEED_IMAGE" not in export
    assert "开头段。" in export and "结尾段。" in export
    assert "[CLIENT_IMAGE" in resp["article"]["content"]  # 编辑 SSOT 不动


def test_endpoint_awaiting_detail_behavior_zero_placeholders(monkeypatch):
    """媒介盒子待确认详情真 handler:正常路径与渲染链爆炸路径都零占位符。"""
    import api.meijiehezi_api as mhz
    import db.brand_image_assets_db as assets_db
    import services.image_placeholder as ph

    monkeypatch.setattr(mhz, "_get_user", lambda r: {"user_id": 1})
    monkeypatch.setattr(mhz, "_require_writing", lambda u: None)
    monkeypatch.setattr(mhz, "get_awaiting_item", lambda i, u: {"id": i, "article_id": 5})
    monkeypatch.setattr(
        mhz, "_serialize_awaiting_item",
        lambda item: {"article_id": 5, "pending_msg": ""},
    )
    monkeypatch.setattr(
        mhz, "_fetch_article_preview_context",
        lambda aid: (_PLACEHOLDER_BODY, None, None),
    )
    monkeypatch.setattr(assets_db, "get_image_asset", lambda aid: None, raising=False)

    resp = asyncio.run(mhz.api_awaiting_confirmation_detail(3, object()))
    body = resp["article"]["content"]
    assert "[CLIENT_IMAGE" not in body and "[NEED_IMAGE" not in body
    assert "开头段。" in body and "结尾段。" in body

    # 渲染链任何异常也不许裸露(旧 except:pass 的行为级复活在此转红)
    def _boom(*a, **k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(ph, "_render", _boom)
    resp2 = asyncio.run(mhz.api_awaiting_confirmation_detail(3, object()))
    body2 = resp2["article"]["content"]
    assert "[CLIENT_IMAGE" not in body2 and "[NEED_IMAGE" not in body2
