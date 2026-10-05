"""[Phase 2B 2026-06-07] Kimi ---REFERENCES--- prompt 来源采集 + 正文剥离 · 函数级 mock

Kimi $web_search 无官方 citation API(诊断侧 search_citations 此前恒为 [])。Phase 2B 移植
调研系统做法:system prompt 逼模型在正文后吐 ---REFERENCES--- 列表 → 正则解析。
解析后【正文剥离 references】再跑品牌检出,使 brand_detected/检出率只看正文不被来源污染。

注:本文件为函数级 mock(不发真实 Kimi 请求)。system prompt 改回答带来的真实
brand_detected/检出率漂移,需 Deploy-CTO 在 staging/prod 用真 API 单独回归验证。
"""
import asyncio
import json


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def _kimi_stop_payload(content):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}]}


def _patch_kimi_io(monkeypatch, ai_tester, content, analyze=None):
    """mock 网络(单轮直接 stop)+ 可见度分析(默认按 check_brand 是否在传入文本判检出)。
    返回 captured dict 以便断言检出实际跑在哪段文本上。"""
    captured = {}

    async def _fake_tracked_post(*args, **kwargs):
        return _FakeResp(_kimi_stop_payload(content))

    async def _fake_analyze(ai_response, query, check_brand, engine_name):
        captured["ai_response"] = ai_response
        if analyze is not None:
            return analyze
        hit = bool(check_brand and check_brand in ai_response)
        return {
            "brand_detected": hit,
            "mentioned_brands": [check_brand] if hit else [],
            "answer_summary": "",
        }

    monkeypatch.setattr(ai_tester, "_tracked_post", _fake_tracked_post)
    monkeypatch.setattr(ai_tester, "_analyze_visibility", _fake_analyze)
    return captured


# ---------------- parser 单元 ----------------

def test_parse_kimi_references_basic():
    from tools.ai_visibility import ai_tester
    content = (
        "某行业推荐 A 公司和 B 公司,服务不错。\n\n"
        "---REFERENCES---\n"
        "1. [央视报道](https://www.cctv.com/a)\n"
        "2. [新华网](https://www.xinhuanet.com/b)\n"
    )
    answer, cits = ai_tester._parse_kimi_references(content)
    assert "REFERENCES" not in answer
    assert answer == "某行业推荐 A 公司和 B 公司,服务不错。"
    assert len(cits) == 2
    assert cits[0] == {"url": "https://www.cctv.com/a", "title": "央视报道", "rank": 1}
    assert cits[1]["url"] == "https://www.xinhuanet.com/b"


def test_parse_kimi_references_dedupe():
    from tools.ai_visibility import ai_tester
    content = (
        "正文\n---REFERENCES---\n"
        "1. [t1](https://x.com/a)\n"
        "2. [t2](https://x.com/a)\n"  # 重复 URL
        "3. [t3](https://x.com/b)\n"
    )
    _, cits = ai_tester._parse_kimi_references(content)
    assert [c["url"] for c in cits] == ["https://x.com/a", "https://x.com/b"]


def test_parse_kimi_references_none_or_empty():
    from tools.ai_visibility import ai_tester
    answer, cits = ai_tester._parse_kimi_references("纯正文无来源段")
    assert answer == "纯正文无来源段" and cits == []
    a2, c2 = ai_tester._parse_kimi_references("")
    assert a2 == "" and c2 == []


# ---------------- query_kimi_search 端到端 ----------------

def test_kimi_search_captures_references(monkeypatch):
    from tools.ai_visibility import ai_tester
    content = (
        "推荐某品牌,口碑好。\n---REFERENCES---\n"
        "1. [来源一](https://www.cctv.com/x)\n"
        "2. [来源二](https://www.xinhuanet.com/y)\n"
    )
    _patch_kimi_io(monkeypatch, ai_tester, content)
    resp = asyncio.run(ai_tester.query_kimi_search("问题", "某品牌"))
    data = json.loads(resp.content[0]["text"])
    cits = data.get("search_citations") or []
    assert len(cits) == 2
    assert {c["url"] for c in cits} == {"https://www.cctv.com/x", "https://www.xinhuanet.com/y"}
    assert data.get("web_search_enabled") is True


def test_kimi_detection_runs_on_stripped_answer(monkeypatch):
    """检出口径只看正文:品牌名只出现在 references → 不算检出(防来源污染检出率)。"""
    from tools.ai_visibility import ai_tester
    content = (
        "这个行业有不少服务商,可以多对比。\n---REFERENCES---\n"
        "1. [某品牌官网介绍](https://brand.com/p)\n"
    )
    captured = _patch_kimi_io(monkeypatch, ai_tester, content)
    resp = asyncio.run(ai_tester.query_kimi_search("问题", "某品牌"))
    data = json.loads(resp.content[0]["text"])
    assert "REFERENCES" not in captured["ai_response"]
    assert "某品牌" not in captured["ai_response"]  # 检出只跑剥离后的正文
    assert data.get("brand_detected") is False
    assert len(data.get("search_citations") or []) == 1  # 但来源仍采集


def test_kimi_no_references_empty_citations(monkeypatch):
    """模型没吐 ---REFERENCES--- → search_citations=[](降级 · 兼容原行为);正文检出照常。"""
    from tools.ai_visibility import ai_tester
    content = "纯散文回答,推荐某品牌,但没有给来源列表。"
    _patch_kimi_io(monkeypatch, ai_tester, content)
    resp = asyncio.run(ai_tester.query_kimi_search("问题", "某品牌"))
    data = json.loads(resp.content[0]["text"])
    assert data.get("search_citations") == []
    assert data.get("brand_detected") is True  # 正文含品牌 → 检出逻辑不变


def test_kimi_citations_flow_into_diagnosis_citations():
    """闭环:kimi 引擎来源经 Phase 1 _extract_diagnosis_citations 进聚合(platform=kimi)。"""
    from services.source_authority_analyzer import _extract_diagnosis_citations
    detail_table = [
        {
            "question": "推荐这个行业的品牌",
            "results": {
                "kimi": {
                    "brand_detected": True,
                    "search_citations": [{"url": "https://www.cctv.com/x", "title": "央视"}],
                }
            },
        }
    ]
    flat = _extract_diagnosis_citations(detail_table)
    hit = [c for c in flat if c.get("platform") == "kimi" and c.get("url") == "https://www.cctv.com/x"]
    assert hit, "kimi 引擎来源未进入 diagnosis_citations"
    assert hit[0]["is_detected"] is True
    assert hit[0]["mention_type"] == "mentioned"   # [P0-3 2026-07-26] 词表归一 direct → mentioned
