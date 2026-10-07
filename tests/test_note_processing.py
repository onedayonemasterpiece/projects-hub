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
