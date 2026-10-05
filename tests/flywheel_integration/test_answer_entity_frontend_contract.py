"""[答案实体] 前端契约(静态):调用新端点 + 不渲染完整 answer_text。"""
from pathlib import Path

TSX = Path("frontend/src/pages/Admin/GeoPlacementFlywheel.tsx")


def test_frontend_calls_answer_entity_endpoints():
    src = TSX.read_text(encoding="utf-8")
    assert "/answer-entities/summary" in src
    assert "/answer-entities/health" in src
    assert "/answer-entities/rebuild" in src
    assert 'value="answer-entities"' in src, "应有独立答案实体 tab"


def test_frontend_answer_entity_block_never_renders_full_answer_text():
    src = TSX.read_text(encoding="utf-8")
    start = src.index('<TabsContent value="answer-entities"')
    end = src.index("</TabsContent>", start)
    block = src[start:end]
    # 真正的风险 = 渲染 AI 原文/摘要内容字段;覆盖率指标(answer_text_coverage)是数字,不是内容。
    assert "answer_excerpt" not in block, "🔴 答案实体区块不得渲染答案摘要原文"
    assert "full_response" not in block, "🔴 答案实体区块不得渲染 AI 完整回答"
