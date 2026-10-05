"""
SAP 域名权威 AI 判定器单测(纯逻辑 + monkeypatch 降级/AI 命中;真 DB 缓存 happy path 留 green)。

跑法:
  $env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'
  python -m pytest tests/test_domain_authority_ai.py -q
"""
import services.domain_authority_ai as dai


# ---------- 种子快通(硬编码命中 / 未知返 None)----------
def test_seed_tier_known_domains():
    assert dai.seed_tier("people.com.cn") == "tier1_national"          # S
    assert dai.seed_tier("baike.baidu.com") == "structured_encyclopedia"
    assert dai.seed_tier("36kr.com") == "tier2_portal_vertical"        # A
    assert dai.seed_tier("163.com") == "tier2_portal_vertical"         # B
    assert dai.seed_tier("csdn.net") == "tier3_small_media_wemedia"    # C
    assert dai.seed_tier("douyin.com") == "tier3_small_media_wemedia"  # social(非短链)


def test_seed_tier_unknown_returns_none():
    # D + gray(真正不认识的域名)→ None → 交 AI 判(这正是原来全被打成 tier3 的那批)
    assert dai.seed_tier("some-unknown-vertical-news.example") is None
    assert dai.seed_tier("地方行业门户xyz.com") is None


def test_seed_tier_blacklist_is_risk_not_ai():
    # D + 黑名单(短链/低质)→ risk_low_quality(硬编码兜住,不交 AI)
    assert dai.seed_tier("t.cn") == "risk_low_quality"
    assert dai.seed_tier("") == "tier3_small_media_wemedia"  # 空域名保守默认


# ---------- LLM JSON 容错解析 ----------
def test_parse_ai_json_plain():
    out = dai._parse_ai_json('{"a.com": "tier2_portal_vertical", "b.cn": "tier3_small_media_wemedia"}')
    assert out == {"a.com": "tier2_portal_vertical", "b.cn": "tier3_small_media_wemedia"}


def test_parse_ai_json_markdown_fence():
    raw = '```json\n{"news-portal.com": "tier1_national"}\n```'
    assert dai._parse_ai_json(raw) == {"news-portal.com": "tier1_national"}


def test_parse_ai_json_drops_invalid_tier():
    # 非法 tier value 必须丢弃(防 LLM 乱吐污染)
    out = dai._parse_ai_json('{"a.com": "VERY_AUTHORITATIVE", "b.com": "tier2_portal_vertical"}')
    assert out == {"b.com": "tier2_portal_vertical"}


def test_parse_ai_json_garbage():
    assert dai._parse_ai_json("不是 JSON 的胡言乱语") == {}
    assert dai._parse_ai_json("") == {}


# ---------- 主入口:全已知域名 → 不碰 DB/AI ----------
def test_classify_seed_only_no_db(monkeypatch):
    def _boom():
        raise RuntimeError("DB 不应被调用")
    monkeypatch.setattr(dai, "_get_conn", _boom)
    monkeypatch.setattr(dai, "_ai_classify", lambda d: (_ for _ in ()).throw(AssertionError("AI 不应被调用")))
    out = dai.classify_domains_ai(["people.com.cn", "36kr.com", "csdn.net"])
    assert out == {
        "people.com.cn": "tier1_national",
        "36kr.com": "tier2_portal_vertical",
        "csdn.net": "tier3_small_media_wemedia",
    }


# ---------- 主入口:未知域名 + DB 不可用 + AI 无结果 → 回落 tier3(不抛)----------
def test_classify_unknown_fallback_no_db_no_ai(monkeypatch):
    monkeypatch.setattr(dai, "_get_conn", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    monkeypatch.setattr(dai, "_ai_classify", lambda domains: {})
    out = dai.classify_domains_ai(["weird-unknown-xyz.example"])
    assert out == {"weird-unknown-xyz.example": "tier3_small_media_wemedia"}


# ---------- 主入口:AI 命中(DB down 时仍生效,只是不缓存)----------
def test_classify_unknown_ai_hit_db_down(monkeypatch):
    monkeypatch.setattr(dai, "_get_conn", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    monkeypatch.setattr(
        dai, "_ai_classify",
        lambda domains: {"niche-industry-portal.example": "tier2_portal_vertical"},
    )
    out = dai.classify_domains_ai(["niche-industry-portal.example"])
    assert out["niche-industry-portal.example"] == "tier2_portal_vertical"


# ---------- 主入口:已知 + 未知混合 ----------
def test_classify_mixed(monkeypatch):
    monkeypatch.setattr(dai, "_get_conn", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    monkeypatch.setattr(dai, "_ai_classify", lambda domains: {"unknown-biz.example": "tier1_national"})
    out = dai.classify_domains_ai(["People.com.cn", "unknown-biz.example", "T.CN"])
    assert out["people.com.cn"] == "tier1_national"        # 种子(归一化小写)
    assert out["t.cn"] == "risk_low_quality"               # 种子黑名单
    assert out["unknown-biz.example"] == "tier1_national"  # AI


def test_classify_empty():
    assert dai.classify_domains_ai([]) == {}
    assert dai.classify_domains_ai(None) == {}
