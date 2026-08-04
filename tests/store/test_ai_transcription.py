from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.ai import transcription


@pytest.mark.asyncio
async def test_transcription_upload_uses_downloaded_filename_and_mime(monkeypatch):
    monkeypatch.setattr(
        transcription,
        "_download_audio_url",
        AsyncMock(return_value=transcription.DownloadedAudio(
            data=b"audio-bytes",
            filename="voice-note.ogg",
            mime_type="audio/ogg",
        )),
    )
    monkeypatch.setattr(
        transcription,
        "get_config",
        lambda: SimpleNamespace(openai_api_key="sk-test"),
    )
    create = AsyncMock(return_value=SimpleNamespace(text="  Hola, tienen pijamas?  "))
    client = MagicMock()
    client.audio.transcriptions.create = create
    monkeypatch.setattr(transcription, "AsyncOpenAI", lambda **_kwargs: client)

    result = await transcription.transcribe_audio_url("https://media.example/voice.ogg")

    assert result == "Hola, tienen pijamas?"
    create.assert_awaited_once_with(
        model="gpt-4o-mini-transcribe",
        file=("voice-note.ogg", b"audio-bytes", "audio/ogg"),
    )


@pytest.mark.asyncio
async def test_transcription_rejects_empty_url_without_download(monkeypatch):
    download = AsyncMock()
    monkeypatch.setattr(transcription, "_download_audio_url", download)

    with pytest.raises(transcription.AudioTranscriptionError, match="URL is missing") as exc:
        await transcription.transcribe_audio_url("  ")

    assert exc.value.retryable is False
    download.assert_not_awaited()


@pytest.mark.parametrize(
    ("content_disposition", "url", "mime_type", "expected"),
    [
        ('attachment; filename="customer-note.m4a"', "https://media.example/file", "audio/mp4", "customer-note.m4a"),
        (None, "https://media.example/note.mp3?token=secret", "audio/mpeg", "note.mp3"),
        (None, "https://media.example/download", "audio/wav", "voice-note.wav"),
    ],
)
def test_audio_filename_uses_headers_url_or_mime(content_disposition, url, mime_type, expected):
    assert transcription._audio_filename(content_disposition, url, mime_type) == expected


@pytest.mark.parametrize(
    ("content_disposition", "url", "expected_filename", "expected_mime"),
    [
        ('attachment; filename="voice.ogg"', "https://media.example/download", "voice.ogg", "audio/ogg"),
        (None, "https://media.example/voice.m4a?token=secret", "voice.m4a", "audio/mp4"),
        (None, "https://media.example/voice.opus", "voice.opus", "audio/opus"),
    ],
)
def test_octet_stream_requires_and_uses_supported_audio_extension(
    content_disposition,
    url,
    expected_filename,
    expected_mime,
):
    assert transcription._validated_audio_metadata(
        content_disposition,
        url,
        "application/octet-stream",
    ) == (expected_filename, expected_mime)


def test_octet_stream_without_supported_extension_is_rejected():
    with pytest.raises(transcription.AudioTranscriptionError, match="unsupported content type"):
        transcription._validated_audio_metadata(
            None,
            "https://media.example/download?token=secret",
            "application/octet-stream",
        )
