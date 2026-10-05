"""归因账本接线的锁(WO_DELIVERY_FLYWHEEL_CLOSURE §2.1)。

判据设计原则(本轮踩过的坑,写进测试里防复发):
  - **每条"必须命中"都配一条"必须不命中"**。只断言"能出结果"的判据,在实现被改坏成
    "恒真"时照样绿。
  - 零行报警那条闸尤其危险:工单原话「恒绿的探针视同没有」。所以它的测试是
    **四种输入 → 四种不同结论**,而不是四种输入一个结论。
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════
# 1. 实体映射:跨 brand_id 归因
# ══════════════════════════════════════════════════════════════
def _fixture(now):
    publications = [{
        "article_id": 1365,
        "publish_url": "https://Example.com/QZQZ/Case?a=1&utm_source=x#frag",
        "published_at": now - timedelta(days=6),
        "status": "published",
        "publication_snapshot_hash": "a" * 64,
        "body_observation_level": "channel_submission_snapshot",
        "publication_source": "mhz_publish_order_items",
        "publication_source_id": 501,
        "brand_id": 662,            # 发布挂 662
        "industry": "家居制造业",
        "style_family": "comparison_ranking",
    }]
    monitoring = [{
        "id": 9001,
        "brand_id": 10,             # 监测历史挂 10 —— 生产实况
        "tested_at": now - timedelta(days=1),
        "lineage_status": "complete",
        "sent_question_snapshot": "深圳全屋定制哪家好？",
        "question_family": "recommendation_selection",
        "provider": "moonshot",
        "target_outcome": "recommended",
        "search_citations": [{"url": "https://example.com/QZQZ/Case?a=1"}],
    }]
    return publications, monitoring


def test_identity_map_bridges_split_brand_ids():
    """QZQZ 生产实况:发布挂 662、监测挂 10。有实体映射时必须归得上。"""
    from services.strict_article_outcomes import attribute_observations

    now = datetime.now(timezone.utc)
    publications, monitoring = _fixture(now)

    mapped = attribute_observations(
        publications, monitoring, now=now,
        identity_by_brand={662: "qzqz", 10: "qzqz"},
    )
    assert mapped["event_count"] == 1, "两个 brand_id 映射到同一实体后必须归因成功"


def test_without_identity_map_strict_brand_equality_still_applies():
    """反向对照:不传映射 → 回到严格 brand_id 相等 → 归不上。

    这条是上一条的对照物。缺了它,`_owner_key` 被改成恒相等也照样绿。
    """
    from services.strict_article_outcomes import attribute_observations

    now = datetime.now(timezone.utc)
    publications, monitoring = _fixture(now)

    unmapped = attribute_observations(publications, monitoring, now=now)
    assert unmapped["event_count"] == 0, "没有映射时必须保持严格 brand_id 相等(既有行为不许被放宽)"


def test_partial_identity_map_does_not_leak_across_entities():
    """只映射一边 → 仍归不上(防"有映射就放行"型放宽)。"""
    from services.strict_article_outcomes import attribute_observations

    now = datetime.now(timezone.utc)
    publications, monitoring = _fixture(now)

    half = attribute_observations(
        publications, monitoring, now=now, identity_by_brand={662: "qzqz"},
    )
    assert half["event_count"] == 0

    other = attribute_observations(
        publications, monitoring, now=now,
        identity_by_brand={662: "qzqz", 10: "qishe"},
    )
    assert other["event_count"] == 0, "映射到不同实体必须归不上"


# ══════════════════════════════════════════════════════════════
# 2. 渠道维度字段(§2.3 的原料)
# ══════════════════════════════════════════════════════════════
def test_event_carries_normalized_url_and_domain():
    from services.strict_article_outcomes import attribute_observations

    now = datetime.now(timezone.utc)
    publications, monitoring = _fixture(now)
    out = attribute_observations(
        publications, monitoring, now=now, identity_by_brand={662: "k", 10: "k"},
    )
    event = out["events"][0]
    assert event["publish_domain"] == "example.com"
    assert event["publish_url_normalized"].startswith("https://example.com/QZQZ/Case")
    # 归一化必须真的剥掉 tracking 与 fragment,否则渠道聚合会把同一篇算成多篇
    assert "utm_source" not in event["publish_url_normalized"]
    assert "#" not in event["publish_url_normalized"]
    assert event["identity_key"] == "k"


def test_domain_extraction_rejects_garbage():
    """反向对照:不可解析的 URL 必须给空域名,而不是抛或者编一个。"""
    from services.strict_article_outcomes import publication_domain

    assert publication_domain("https://example.com/x") == "example.com"
    assert publication_domain("not-a-url") == ""
    assert publication_domain("") == ""


# ══════════════════════════════════════════════════════════════
# 2b. 证据等级:无正文快照的行必须"记而不混"
# ══════════════════════════════════════════════════════════════
def _fixture_no_snapshot(now):
    publications, monitoring = _fixture(now)
    publications[0]["publication_snapshot_hash"] = None   # 晨光富士 58/59 条的形状
    return publications, monitoring


def test_missing_snapshot_is_dropped_by_default():
    """默认严格:没有正文快照 = 无法自证,主链一行都不许出。"""
    from services.strict_article_outcomes import attribute_observations

    now = datetime.now(timezone.utc)
    publications, monitoring = _fixture_no_snapshot(now)
    strict = attribute_observations(
        publications, monitoring, now=now, identity_by_brand={662: "k", 10: "k"},
    )
    assert strict["event_count"] == 0
    assert strict["quality_counts"]["publication_snapshot_missing"] == 1


def test_missing_snapshot_is_recorded_but_never_counted_as_proven():
    """开启降级后:行要出得来(客户看得见),但绝不能算进 proven。"""
    from services.strict_article_outcomes import attribute_observations

    now = datetime.now(timezone.utc)
    publications, monitoring = _fixture_no_snapshot(now)
    lenient = attribute_observations(
        publications, monitoring, now=now,
        identity_by_brand={662: "k", 10: "k"}, include_unverified_body=True,
    )
    assert lenient["event_count"] == 1, "降级后必须记得下这一行"
    assert lenient["proven_event_count"] == 0, "🔴 绝不能算进主指标 —— 那是伪造证据等级"
    assert lenient["unverified_body_event_count"] == 1
    event = lenient["events"][0]
    assert event["body_proof"] is False
    assert event["url_match"] == "exact_normalized_no_body_proof"


def test_proven_rows_still_count_as_proven():
    """反向对照:有快照的行必须仍然算 proven,否则上一条断言可以靠"恒 0"作弊。"""
    from services.strict_article_outcomes import attribute_observations

    now = datetime.now(timezone.utc)
    publications, monitoring = _fixture(now)
    out = attribute_observations(
        publications, monitoring, now=now,
        identity_by_brand={662: "k", 10: "k"}, include_unverified_body=True,
    )
    assert out["proven_event_count"] == 1
    assert out["unverified_body_event_count"] == 0
    assert out["events"][0]["url_match"] == "exact_normalized"


def test_flywheel_metric_filters_on_body_proof():
    """写作飞轮的被引计数 SQL 必须带 body_proof 过滤 —— 少了它,主指标就被污染。"""
    import inspect

    from services.article_attribution_ledger import citation_counts_for_articles

    source = inspect.getsource(citation_counts_for_articles)
    assert "AND body_proof" in source


# ══════════════════════════════════════════════════════════════
# 3. 账本落库参数:缺 NOT NULL 事实必须丢弃,不许补默认值
# ══════════════════════════════════════════════════════════════
@pytest.mark.parametrize("missing", [
    "monitoring_result_id", "publication_source", "publication_source_id",
    "publish_url_normalized", "published_at", "tested_at",
])
def test_row_params_drops_incomplete_events(missing):
    from services.article_attribution_ledger import _row_params

    now = datetime.now(timezone.utc)
    event = {
        "metric_version": "v1", "monitoring_result_id": 1,
        "publication_source": "mhz_publish_order_items", "publication_source_id": 2,
        "publish_url_normalized": "https://example.com/a", "publish_domain": "example.com",
        "published_at": now - timedelta(days=2), "tested_at": now,
    }
    assert _row_params(dict(event), "u1") is not None, "完整事件必须能落库(反向对照)"

    broken = dict(event)
    broken[missing] = None
    assert _row_params(broken, "u1") is None, f"缺 {missing} 必须丢弃,不许补默认值凑一行"


# ══════════════════════════════════════════════════════════════
# 4. 零行报警闸:四种输入必须给四种结论
# ══════════════════════════════════════════════════════════════
def test_ledger_silence_gate_fires_and_clears(monkeypatch):
    """工单 §2.1.4「恒绿的探针视同没有」—— 这条测试就是那个反向对照。"""
    import services.flywheel_heartbeat as hb

    def _finding(fake_freshness):
        monkeypatch.setattr(
            "services.article_attribution_ledger.ledger_freshness",
            lambda: fake_freshness,
        )
        findings = hb.evaluate_gate_health()
        matched = [f for f in findings if f.get("key") == "attribution_ledger_silence"]
        assert matched, "闸必须对每种输入都产出一条 finding(没产出 = 闸静默失效)"
        return matched[0]

    # ① 表不可读 → firing + error
    unavailable = _finding({"available": False, "reason": "relation does not exist"})
    assert unavailable["firing"] is True and unavailable["severity"] == "error"

    # ② 有表但 0 行 → firing + error
    empty = _finding({"available": True, "total_rows": 0, "age_seconds": None})
    assert empty["firing"] is True and empty["severity"] == "error"

    # ③ 有行但超 48h → firing + warn(注意严重度与 ①② 不同,防"一律 error"式偷懒)
    stale = _finding({"available": True, "total_rows": 12, "age_seconds": 49 * 3600})
    assert stale["firing"] is True and stale["severity"] == "warn"

    # ④ 有行且新鲜 → **不 firing**(这条是全套判据的反向对照物:
    #    没有它,把 firing 写死成 True 也能让上面三条全绿)
    fresh = _finding({"available": True, "total_rows": 12, "age_seconds": 3600})
    assert fresh["firing"] is False


def test_silence_threshold_is_48h_not_something_else():
    from services.flywheel_heartbeat import LEDGER_SILENCE_ALERT_SECONDS

    assert LEDGER_SILENCE_ALERT_SECONDS == 48 * 3600


# ══════════════════════════════════════════════════════════════
# 5. 接线锁:job 必须注册,且必须早于 04:20 的回写
# ══════════════════════════════════════════════════════════════
def _strip_comments(source: str) -> str:
    """🔴 锁必须先剥注释 —— 2026-08-05 踩过:锁抓到的是我自己写的注释,恒绿。

    🔴 必须用 tokenize 而不是 `line.split('#')`:后者会切断字符串字面量里的 `#`
    (本仓 api/scheduler.py 就有 f-string 里带 `#` 的行),把源码切成语法错误 ——
    我第一版就是这么写的,当场 SyntaxError。
    """
    import io
    import tokenize

    pieces: list[str] = []
    last_row, last_col = 1, 0
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        srow, scol = tok.start
        if srow > last_row:
            pieces.append("\n" * (srow - last_row))
            last_col = 0
        if scol > last_col:
            pieces.append(" " * (scol - last_col))
        pieces.append("" if tok.type == tokenize.COMMENT else tok.string)
        last_row, last_col = tok.end
    return "".join(pieces)


def test_attribution_sync_job_registered_and_ordered_before_backfill():
    source = _strip_comments((ROOT / "api/scheduler.py").read_text(encoding="utf-8"))
    assert 'id="article_attribution_sync"' in source, "同步 job 必须注册,否则账本永远没水"
    assert 'id="writing_outcome_backfill"' in source

    tree = ast.parse(source)
    minutes: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        job_id = None
        for kw in node.keywords:
            if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                job_id = kw.value.value
        if job_id not in ("article_attribution_sync", "writing_outcome_backfill"):
            continue
        for kw in node.keywords:
            if kw.arg != "trigger" or not isinstance(kw.value, ast.Call):
                continue
            hour = minute = None
            for tkw in kw.value.keywords:
                if tkw.arg == "hour" and isinstance(tkw.value, ast.Constant):
                    hour = tkw.value.value
                if tkw.arg == "minute" and isinstance(tkw.value, ast.Constant):
                    minute = tkw.value.value
            if isinstance(hour, int) and isinstance(minute, int):
                minutes[job_id] = hour * 60 + minute

    assert set(minutes) == {"article_attribution_sync", "writing_outcome_backfill"}, minutes
    assert minutes["article_attribution_sync"] < minutes["writing_outcome_backfill"], (
        "归因同步必须排在写作效果回写之前 —— 否则每天的回写读到的都是前一天的账本"
    )


def test_attribution_sync_in_heartbeat_registry():
    """未登记的飞轮 job,watchdog 看不见它 —— 那就等于回到"坏了也没人知道"。"""
    from services.flywheel_heartbeat import FLYWHEEL_JOBS

    assert "article_attribution_sync" in FLYWHEEL_JOBS
    assert "writing_outcome_backfill" in FLYWHEEL_JOBS  # 反向对照:注册表本身是活的


def test_writing_backfill_reads_attribution_ledger():
    """飞轮回写必须真的读账本 —— 只建账本不接线,等于把同一个坑再挖一遍。"""
    source = _strip_comments(
        (ROOT / "services/writing_outcome_backfill.py").read_text(encoding="utf-8")
    )
    assert "from services.article_attribution_ledger import citation_counts_for_articles" in source
    # 且原 research 池路径必须仍在(两池相加,不是替换)
    assert "geo_research_article_citations" in source
