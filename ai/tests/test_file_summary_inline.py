import asyncio
from unittest.mock import AsyncMock

import pytest

from app.service.file_summary import summarizer
from app.service.file_summary.hwp_extractor import HwpContent
from app.service.file_summary.office_extractor import OfficeContent, ExtractedImage
from app.client.gemini_client import GeminiClient


@pytest.mark.parametrize("extension,function", [(".pdf", "summarize_pdf"), (".png", "summarize_image")])
def test_file_is_sent_inline_without_files_api(tmp_path, extension, function):
    path = tmp_path / ("input" + extension)
    path.write_bytes(b"\xff\x00payload")
    client = AsyncMock()
    client.generate_structured.return_value = summarizer.ImagePayload(description="image") if extension == ".png" else summarizer.SummaryPayload(summary="pdf")
    asyncio.run(getattr(summarizer, function)(client, path))
    client.upload_file.assert_not_awaited()
    client.delete_file.assert_not_awaited()
    part = client.generate_structured.await_args.kwargs["contents"][0]
    assert part.inline_data.data == path.read_bytes()


def test_oversized_file_does_not_call_provider(tmp_path, monkeypatch):
    path = tmp_path / "input.pdf"
    path.write_bytes(b"1234")
    monkeypatch.setattr(summarizer.settings, "gemini_inline_max_bytes", 3)
    client = AsyncMock()
    with pytest.raises(Exception, match="FILE_TOO_LARGE_FOR_INLINE"):
        asyncio.run(summarizer.summarize_pdf(client, path))
    client.generate_structured.assert_not_awaited()


@pytest.mark.parametrize("content_type,function", [(OfficeContent, "summarize_office"), (HwpContent, "summarize_hwp")])
def test_embedded_image_bytes_are_stable_in_provider_input(tmp_path, monkeypatch, content_type, function):
    from tests.test_gemini_client import install_settings
    install_settings(monkeypatch)
    data = b"\xff\x00image"
    content = content_type(text="한글", images=[ExtractedImage(data=data, mime_type="image/png", origin="test")])
    fake = AsyncMock()
    fake.generate_structured.return_value = summarizer.SummaryPayload(summary="ok")
    async def run():
        for _ in range(2):
            await getattr(summarizer, function)(fake, tmp_path / "object.docx", content)
    asyncio.run(run())
    values = [call.kwargs["contents"] for call in fake.generate_structured.await_args_list]
    assert values[0] == values[1]
    assert values[0][1].inline_data.data == data
