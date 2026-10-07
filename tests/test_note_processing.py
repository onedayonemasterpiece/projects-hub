from __future__ import annotations

import httpx
import pytest

from projects_hub.note_processing import GeminiNoteProcessor, NoteProcessingError


@pytest.mark.asyncio
async def test_shared_limiter_empty_success_receipts_are_valid_for_mutations():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path in {
            "/rest/v1/rpc/google_ai_mark_sent",
            "/rest/v1/rpc/google_ai_release_unsent_v2",
            "/rest/v1/rpc/google_ai_report_provider_429",
            "/rest/v1/rpc/google_ai_finalize",
        }:
            return httpx.Response(204, request=request)
        return httpx.Response(404, json={"error": "unexpected"}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    processor = GeminiNoteProcessor(
        environment={
            "AI_SUPABASE_URL": "https://limiter.example",
            "AI_SUPABASE_SECRET_KEY": "test-only",
            "GOOGLE_API_KEY4": "test-google-key",
        },
        client=client,
    )
    try:
        assert await processor._rpc(
            "google_ai_mark_sent",
            {"p_request_uid": "req-1", "p_attempt_no": 1},
            allow_empty=True,
        ) is None
        await processor._release_unsent("req-1", 1, "test")
        await processor._rpc(
            "google_ai_report_provider_429",
            {
                "p_request_uid": "req-1",
                "p_attempt_no": 1,
                "p_retry_after_ms": None,
            },
            allow_empty=True,
        )
        await processor._finalize(
            request_uid="req-1",
            attempt_no=1,
            usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            status="succeeded",
        )
        assert seen == [
            "/rest/v1/rpc/google_ai_mark_sent",
            "/rest/v1/rpc/google_ai_release_unsent_v2",
            "/rest/v1/rpc/google_ai_report_provider_429",
            "/rest/v1/rpc/google_ai_finalize",
        ]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_shared_limiter_empty_response_still_fails_for_json_rpc():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    processor = GeminiNoteProcessor(
        environment={
            "AI_SUPABASE_URL": "https://limiter.example",
            "AI_SUPABASE_SECRET_KEY": "test-only",
            "GOOGLE_API_KEY4": "test-google-key",
        },
        client=client,
    )
    try:
        with pytest.raises(NoteProcessingError) as exc:
            await processor._rpc("google_ai_limiter_capabilities", {})
        assert exc.value.code == "NOTE_LIMITER_INVALID_RESPONSE"
    finally:
        await client.aclose()


def test_provider_error_detail_is_bounded_and_redacts_key_like_values():
    detail = GeminiNoteProcessor._provider_error_detail(
        {
            "error": {
                "status": "INVALID_ARGUMENT",
                "message": "bad config api_key=secret-value AIza123456789012345678901234567890",
            }
        },
        400,
    )
    assert detail.startswith("HTTP 400 INVALID_ARGUMENT")
    assert "secret-value" not in detail
    assert "AIza123" not in detail
    assert "[REDACTED]" in detail
    assert len(detail) < 420


@pytest.mark.asyncio
async def test_provider_4xx_is_non_retryable_and_preserves_sanitized_reason(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "generativelanguage.googleapis.com" in str(request.url):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "status": "INVALID_ARGUMENT",
                        "message": "Structured output request rejected",
                    }
                },
                request=request,
            )
        return httpx.Response(204, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    processor = GeminiNoteProcessor(
        environment={"GOOGLE_API_KEY4": "test-google-key"},
        client=client,
    )

    async def reserve(**_kwargs):
        return "req-4xx", {"contract": "test"}, "test-google-key"

    async def rpc(*_args, **_kwargs):
        return None

    async def finalize(**_kwargs):
        return None

    monkeypatch.setattr(processor, "_reserve", reserve)
    monkeypatch.setattr(processor, "_rpc", rpc)
    monkeypatch.setattr(processor, "_finalize", finalize)
    try:
        with pytest.raises(NoteProcessingError) as exc:
            await processor.summarize(
                note_id="note_provider_4xx",
                project_name="Projects Hub",
                author_display_name="Owner",
                author_roles=["owner"],
                source_text="Тестовый текст заметки.",
                suggested_title="Тест",
            )
        assert exc.value.code == "NOTE_PROCESSOR_PROVIDER_REJECTED"
        assert exc.value.retryable is False
        assert "HTTP 400 INVALID_ARGUMENT" in str(exc.value)
        assert "Structured output request rejected" in str(exc.value)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_provider_5xx_remains_retryable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "generativelanguage.googleapis.com" in str(request.url):
            return httpx.Response(
                503,
                json={
                    "error": {
                        "code": 503,
                        "status": "UNAVAILABLE",
                        "message": "temporary outage",
                    }
                },
                request=request,
            )
        return httpx.Response(204, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    processor = GeminiNoteProcessor(
        environment={"GOOGLE_API_KEY4": "test-google-key"},
        client=client,
    )

    async def reserve(**_kwargs):
        return "req-5xx", {"contract": "test"}, "test-google-key"

    async def rpc(*_args, **_kwargs):
        return None

    async def finalize(**_kwargs):
        return None

    monkeypatch.setattr(processor, "_reserve", reserve)
    monkeypatch.setattr(processor, "_rpc", rpc)
    monkeypatch.setattr(processor, "_finalize", finalize)
    try:
        with pytest.raises(NoteProcessingError) as exc:
            await processor.summarize(
                note_id="note_provider_5xx",
                project_name="Projects Hub",
                author_display_name="Owner",
                author_roles=["owner"],
                source_text="Тестовый текст заметки.",
                suggested_title="Тест",
            )
        assert exc.value.code == "NOTE_PROCESSOR_UNAVAILABLE"
        assert exc.value.retryable is True
        assert "HTTP 503 UNAVAILABLE" in str(exc.value)
    finally:
        await client.aclose()
