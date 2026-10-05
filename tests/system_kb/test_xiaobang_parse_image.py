"""小榜截图解析接口测试（离线，不调用真实视觉模型）。"""
import types
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile


def _fake_request(user):
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user))


def _upload_file(content: bytes = b"fake-image", content_type: str = "image/png") -> UploadFile:
    return UploadFile(
        filename="screen.png",
        file=BytesIO(content),
        headers={"content-type": content_type},
    )


@pytest.mark.asyncio
async def test_xiaobang_parse_image_requires_login():
    from api.xiaobang_api import xiaobang_parse_image

    with pytest.raises(HTTPException) as exc:
        await xiaobang_parse_image(_fake_request(None), _upload_file())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_xiaobang_parse_image_rejects_non_image():
    from api.xiaobang_api import xiaobang_parse_image

    with pytest.raises(HTTPException) as exc:
        await xiaobang_parse_image(
            _fake_request({"id": 1, "is_admin": False}),
            _upload_file(content_type="application/pdf"),
        )
    assert exc.value.status_code == 415


@pytest.mark.asyncio
async def test_xiaobang_parse_image_success_is_free(monkeypatch):
    from api.xiaobang_api import xiaobang_parse_image
    from services.chat_attachments import AttachmentParseResult
    import services.chat_attachments as chat_attachments

    async def fake_parse_image(file_bytes: bytes, filename: str):
        assert file_bytes == b"fake-image"
        assert filename == "screen.png"
        return AttachmentParseResult(
            ok=True,
            status="parsed",
            title="截图",
            markdown_summary="### 图片参考:screen.png\n\n页面里有提现按钮和未实名提示。",
            incurred_cost=True,  # 原服务标记成本;小榜接口不做扣费,只返回 free=True。
        )

    monkeypatch.setattr(chat_attachments, "parse_image", fake_parse_image)
    result = await xiaobang_parse_image(
        _fake_request({"id": 1, "is_admin": False}),
        _upload_file(),
    )

    assert result["ok"] is True
    assert result["free"] is True
    assert "提现按钮" in result["attachment_text"]
    assert "incurred_cost" not in result
