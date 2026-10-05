from contextlib import asynccontextmanager

import pytest

from services.research_monitor import platforms


class _Tracker:
    def record(self, **_kwargs):
        return None


@asynccontextmanager
async def _fake_track(*_args, **_kwargs):
    yield _Tracker()


class _Response:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    def __init__(self, response, captured):
        self._response = response
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, _url, **kwargs):
        self._captured.update(kwargs)
        return self._response


@pytest.mark.asyncio
async def test_doubao_uses_raw_positions_and_keeps_duplicate_positions(monkeypatch):
    payload = {
        "output": [
            {
                "type": "doubao_app_call",
                "blocks": [
                    {"type": "output_text", "text": "Duplicate source adopted [3], next source [5]."},
                    {
                        "type": "search",
                        "results": [
                            {"text_card": {"url": "https://a.example/1", "title": "A"}},
                            {"text_card": {"url": "", "title": "No URL still occupies position 2"}},
                            {"text_card": {"url": "https://a.example/1", "title": "A duplicate"}},
                            {"text_card": "not-json"},
                            {"text_card": {"url": "https://b.example/5", "title": "B"}},
                        ],
                    },
                ],
            }
        ]
    }
    captured = {}
    response = _Response(payload)

    monkeypatch.setenv("VOLC_API_KEY", "test-only")
    monkeypatch.setattr(platforms, "_get_doubao_app_model", lambda: "test-model")
    monkeypatch.setattr(platforms, "llm_track", _fake_track)
    monkeypatch.setattr(
        platforms.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(response, captured),
    )

    result = await platforms.query_doubao(7, "test prompt")

    assert captured["json"]["input"].endswith(platforms.DOUBAO_INLINE_MARKER_SUFFIX)
    assert len(result["citations"]) == 2
    first, second = result["citations"]
    assert first["rank"] == 1
    assert first["answer_ranks"] == [1, 3]
    assert first["is_answer_cited"] is True
    assert first["adoption_rank"] == 3
    assert second["rank"] == 5
    assert second["is_answer_cited"] is True
    assert second["adoption_rank"] == 5


def test_kimi_prompt_requires_inline_reference_markers():
    assert "必须在该处内联标注 [n]" in platforms.KIMI_SYSTEM_PROMPT
    assert "n 等于\"引用来源\"列表中对应网页的序号" in platforms.KIMI_SYSTEM_PROMPT
