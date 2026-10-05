# -*- coding: utf-8 -*-
"""P2-1 历史事实资产复用 + P2-2 运营助手 · 判别锁(2026-08-14 · R2 翻转 2026-08-15)。

🔴 R2-1 翻转声明(返修单 §3-3 要求单独说明):
  旧锁 `test_only_verified_reused` 用**无 binding_state** 的夹具断言「可复用」——
  把「缺绑定仍进正文链」锁成了正确行为(生产 1,097 条 verified 全缺绑定,
  等于放行全部历史条目冒充当前品牌证据)。新锁锁的是:**只有
  entity_confirmed 才复用**;缺绑定/unverified/different 三态一律候选不复用。

🔴 R2-5 翻转声明:旧 `test_ladder_order` 把「有提示先 review」锁成正确 ——
  新语义:有可发布文章时「去发布」是主动作,普通提示是次要入口,
  且可发布数不再被提示数扣减。

变异点:
  M1 拆核验门 → test_asset_four_state_behavior 红;
  M2 拆 confirmed 门(退回只排 different)→ 四态锁 + DB 真入口测试红;
  M3 拆相关性初筛 → test_unrelated_not_reused 红;
  M4 拆阶梯次序(review 抢回发布前)→ test_publish_beats_advisory 红;
  M5 拆接线 → test_wiring 红。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.writing_next_step import derive_next_step
from writing.brand_evidence_asset import load_brand_verified_evidence, select_reusable_items

ROOT = Path(__file__).resolve().parents[1]




def _seed_user_adaptive(cur, uid: int, uname: str) -> None:
    """按 users 表**实际列集**播种(A/B 臂库上别的测试会建无 password_hash 的
    users 变体 —— v5 实测 UndefinedColumn;固定列 INSERT 在共享测试库上必碎)。"""
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='users'")
    cols = {r[0] for r in cur.fetchall()}
    desired = {"id": uid, "username": uname, "password_hash": "x", "display_name": uname}
    use = {k: v for k, v in desired.items() if k in cols}
    fields = ", ".join(use)
    marks = ", ".join(["%s"] * len(use))
    cur.execute(f"INSERT INTO users ({fields}) VALUES ({marks}) ON CONFLICT DO NOTHING",
                list(use.values()))

def _verified_item(url: str, title: str, binding: str | None = None) -> dict:
    item = {
        "evidence_id": "EV-001", "relationship": "support",
        "claim": title, "title": title, "url": url,
        "verification_status": "claim_span_verified",
        "canonical_body_hash": "a" * 64, "body_hash_algorithm": "sha256",
        "span": {"type": "canonical_body_exact_quote", "start": 0, "end": 5},
        "verification_version": "v1", "verification_provider": "p", "verification_model": "m",
        "scope": "原 scope",
    }
    if binding:
        item["binding_state"] = binding
    return item


_Q = "观光电梯交付周期哪家快"


def test_asset_four_state_behavior() -> None:
    """R2-1 四态行为锁:confirmed 复用;缺绑定/unverified/different 全部候选不复用。"""
    packs = [{"items": [
        _verified_item("https://confirmed.com", "观光电梯交付周期调研", binding="entity_confirmed"),
        _verified_item("https://missing.com", "观光电梯交付周期调研"),
        _verified_item("https://unv.com", "观光电梯交付周期调研", binding="entity_unverified"),
        _verified_item("https://diff.com", "观光电梯交付周期调研", binding="different_entity"),
    ]}]
    urls = [i["url"] for i in select_reusable_items(packs, question=_Q)]
    assert urls == ["https://confirmed.com"], (
        f"四态门破:复用了 {urls} —— 只有 entity_confirmed 可作当前品牌证据复用"
    )


def test_missing_binding_is_candidate_not_reused() -> None:
    """🔴 翻转锁(旧 test_only_verified_reused 的反命题):缺 binding_state 的
    verified 历史条目 = 候选,**不复用**。生产 1,097 条全是这个形态。"""
    packs = [{"items": [_verified_item("https://a.com", "观光电梯交付周期调研")]}]
    assert select_reusable_items(packs, question=_Q) == []


def test_unverified_status_never_reused() -> None:
    # 核验门仍在:哪怕 confirmed 绑定,未核验状态也不复用
    packs = [{"items": [
        {**_verified_item("https://b.com", "观光电梯交付周期问答", binding="entity_confirmed"),
         "verification_status": "search_result_only"},
    ]}]
    assert select_reusable_items(packs, question=_Q) == []


def test_unrelated_not_reused() -> None:
    packs = [{"items": [
        _verified_item("https://x.com", "完全无关的另一个话题", binding="entity_confirmed"),
    ]}]
    assert select_reusable_items(packs, question=_Q) == []


def test_dedupe_and_exclude() -> None:
    packs = [{"items": [
        _verified_item("https://a.com", "观光电梯交付周期调研", binding="entity_confirmed"),
        _verified_item("https://a.com", "观光电梯交付周期调研", binding="entity_confirmed"),
        _verified_item("https://c.com", "观光电梯交付周期白皮书", binding="entity_confirmed"),
    ]}]
    out = select_reusable_items(packs, question=_Q, exclude_urls={"https://c.com"})
    assert [i["url"] for i in out] == ["https://a.com"]
    assert "历史核验资产复用" in out[0]["scope"], "复用条目没打来源注记(lineage 不可追溯)"


@pytest.mark.integration
def test_real_entry_confirmed_gate_via_db() -> None:
    """R2-1 判据:真实写作入口路径(ags 调的就是 load_brand_verified_evidence,
    真 PG 真 articles/quotes/brands 行,非单元桩)。删掉 confirmed 过滤 → 本测试红。
    """
    import json

    import psycopg2

    url = os.environ["TEST_DATABASE_URL"]
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()
    # 自举最小表(同仓 p3a 先例)。🔴 **建过的必须在本测试末尾 DROP**:
    # 最小表留在库里会毒化 init_db(它假设已存在的表是全列的,建索引撞
    # UndefinedColumn —— G1 门禁被本测试第一版当场炸过)。
    _created: list[str] = []
    cur.execute("SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='brands')")
    _brands_preexisting = cur.fetchone()[0]
    from db.brands_schema import ensure_brands_schema  # 零副作用叶子模块
    ensure_brands_schema(cur)
    if not _brands_preexisting:
        _created.append("brands")
    for name, ddl in (
        ("users", "CREATE TABLE users (id SERIAL PRIMARY KEY, username TEXT, password_hash TEXT, display_name TEXT)"),
        # [R5 ⑤ 微单 2026-08-21] brands 不在这里手搞了（原来 3 列，生产 32 列），
        #   改由下面的 ensure_brands_schema()（生产 SSOT 出口）建；
        #   仍登记进 _created，保持本文件「建过必拆」的清理契约不变。
        ("quotes", "CREATE TABLE quotes (id SERIAL PRIMARY KEY, brand_id INTEGER)"),
        ("articles", "CREATE TABLE articles (id SERIAL PRIMARY KEY, quote_id INTEGER, evidence_pack JSONB)"),
    ):
        cur.execute("SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=%s)", (name,))
        if not cur.fetchone()[0]:
            cur.execute(ddl)
            _created.append(name)
    try:
        # 幂等种子:品牌/报价/两篇文章(一篇缺绑定的 verified pack,一篇 confirmed)
        _seed_user_adaptive(cur, 97001, "r2asset")
        cur.execute("INSERT INTO brands (id, name, owner_user_id) "
                    "VALUES (97001,'资产测试品牌',97001) ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO quotes (id, brand_id) VALUES (97001, 97001) ON CONFLICT DO NOTHING")
        legacy_pack = {"items": [_verified_item("https://legacy.example.com", "观光电梯交付周期调研")]}
        confirmed_pack = {"items": [
            _verified_item("https://confirmed.example.com", "观光电梯交付周期白皮书",
                           binding="entity_confirmed"),
        ]}
        cur.execute("DELETE FROM articles WHERE id IN (97001, 97002)")
        cur.execute("INSERT INTO articles (id, quote_id, evidence_pack) VALUES (%s,%s,%s)",
                    (97001, 97001, json.dumps(legacy_pack)))
        cur.execute("INSERT INTO articles (id, quote_id, evidence_pack) VALUES (%s,%s,%s)",
                    (97002, 97001, json.dumps(confirmed_pack)))
        out = load_brand_verified_evidence(97001, question=_Q)
        urls = [i["url"] for i in out]
        assert "https://confirmed.example.com" in urls, "confirmed 条目没被真实入口复用(门过严)"
        assert "https://legacy.example.com" not in urls, (
            "缺 binding 的 verified 历史条目穿过了真实入口 —— 生产 1,097 条同形态会整体放行"
        )
    finally:
        cur.execute("DELETE FROM articles WHERE id IN (97001, 97002)")
        for name in reversed(_created):
            cur.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
        conn.close()


# ------------------------------------------------------------------ P2-2 阶梯
def _facts(**kw) -> dict:
    base = dict(keywords=3, topics=5, topics_untitled=0, topics_ready=0,
                topics_writing=0, topics_failed=0, articles=5, advisory_open=0,
                eligible_unpublished=0, published=0, monitoring_tasks=0)
    base.update(kw)
    return base


def test_ladder_order() -> None:
    assert derive_next_step(_facts(keywords=0))["stage"] == "keywords"
    assert derive_next_step(_facts(topics=0))["stage"] == "titles"
    assert derive_next_step(_facts(topics_untitled=2))["stage"] == "titles"
    assert derive_next_step(_facts(topics_failed=1))["stage"] == "retry"
    assert derive_next_step(_facts(topics_ready=3))["stage"] == "write"
    assert derive_next_step(_facts(topics_writing=2))["stage"] == "writing"
    assert derive_next_step(_facts(advisory_open=2))["stage"] == "review"
    assert derive_next_step(_facts(eligible_unpublished=3))["stage"] == "publish"
    assert derive_next_step(_facts(published=2))["stage"] == "monitor"
    assert derive_next_step(_facts(published=2, monitoring_tasks=1))["stage"] == "observe"


def test_publish_beats_advisory() -> None:
    """🔴 翻转锁(R2-5,返修单 §0② 反例原样):旧锁把「有提示先 review」锁成
    正确 —— 普通提示不影响发布资格,把它排在发布前 = 提示变隐性闸。
    新语义:advisory_open=2 且 eligible_unpublished=3 → **publish** 是主动作,
    提示以次要入口出现在 why 里。"""
    step = derive_next_step(_facts(advisory_open=2, eligible_unpublished=3))
    assert step["stage"] == "publish", f"提示又抢回发布前了: {step}"
    assert "提示" in step["why"], "提示的次要入口没在 why 里点名(信息不能凭空消失)"


def test_eligible_not_reduced_by_advisory() -> None:
    """R2-5:可发布数不被普通提示扣减(源码锁:facts 计算无 advisory 扣项)。"""
    src = (ROOT / "services/writing_next_step.py").read_text(encoding="utf-8")
    assert '0, facts.get("articles", 0) - facts.get("published", 0)' in src, (
        "eligible_unpublished 公式被改动(应 = articles - published)"
    )
    assert "- advisory_open - facts.get" not in src, "可发布数又被提示数扣减了"


def test_ladder_takes_first_hit() -> None:
    # 反向对照:多个条件同时成立 → 只报最上游那档(不给用户一堆待办)
    step = derive_next_step(_facts(topics_untitled=1, advisory_open=5, published=9))
    assert step["stage"] == "titles"


def test_step_copy_is_human() -> None:
    step = derive_next_step(_facts(advisory_open=2))
    for field in ("title", "why", "cta"):
        assert step[field]
        assert "stage" not in step[field] and "advisory" not in step[field], (
            "工程术语裸露给用户(M3 渐进披露原则)"
        )


def test_wiring() -> None:
    ags = (ROOT / "writing/article_generator_service.py").read_text(encoding="utf-8")
    # 🔴 必须同时锁 import 与真调用 —— 只 grep 函数名会被
    # `load_brand_verified_evidence = None` 这类假接线骗过(变异 M5 实证)。
    assert "from writing.brand_evidence_asset import load_brand_verified_evidence" in ags, (
        "P2-1 资产层没从模块导入(死函数/假接线)"
    )
    assert "_asset_items = load_brand_verified_evidence(" in ags, "P2-1 没有真调用点"
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    assert '/api/writing/projects/{quote_id}/next-step' in server, "P2-2 端点不存在"
    marker = server.index('/api/writing/projects/{quote_id}/next-step')
    assert "require_quote_access" in server[marker:marker + 900], "P2-2 端点没有归属校验"
    wh = (ROOT / "frontend/src/pages/Writing/WritingHall.tsx").read_text(encoding="utf-8")
    assert "/next-step" in wh and "下一步" in wh, "P2-2 端点没有前端入口(死功能)"
    # [R2-5] CTA 必须是真动作按钮:带 testid 的 <button> + onClick 分发
    # (真渲染点击判据另由 Playwright spec 覆盖;这里锁源码形态防退化成死 <span>)。
    assert 'data-testid="next-step-cta"' in wh, "CTA testid 丢失(R2-5 §0② 反例回潮)"
    cta_block = wh[wh.index('data-testid="next-step-card"'):]
    cta_block = cta_block[:cta_block.index('</button>') + 10]
    assert "<button" in cta_block and "onClick" in cta_block, "CTA 不是可点按钮"
    assert "navigate(`/publish?quote_id=" in cta_block, "publish 档 CTA 没接真导航"
