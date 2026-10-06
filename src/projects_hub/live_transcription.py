from __future__ import annotations

import asyncio
import base64
import binascii
import json
from typing import Any, Awaitable, Callable

CAPTION_MODEL = "gemini-3.5-transcribe-live"
MAX_PENDING_WIRE_BYTES = 1_200_000
TRANSCRIBE_PCM_CHUNK_BYTES = 3_200  # 100 ms of PCM16 mono at 16 kHz.


class _BoundedJsonReader:
    def __init__(self, max_pending_bytes: int = MAX_PENDING_WIRE_BYTES):
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._pending_bytes = 0
        self._max_pending_bytes = max_pending_bytes
        self._closed = False

    @property
    def pending_bytes(self) -> int:
        return self._pending_bytes

    def feed(self, message: dict[str, Any]) -> bool:
        if self._closed:
            return False
        encoded = (
            json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        if self._pending_bytes + len(encoded) > self._max_pending_bytes:
            return False
        self._pending_bytes += len(encoded)
        self._queue.put_nowait(encoded)
        return True

    async def readline(self) -> bytes:
        item = await self._queue.get()
        if item is None:
            return b""
        self._pending_bytes = max(0, self._pending_bytes - len(item))
        return item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)


def _mirror_messages(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure mapping used by tests/documentation; CaptionSidecar batches audio itself."""
    mirrored: list[dict[str, Any]] = []
    if message.get("activity_start"):
        mirrored.append({"type": "activity_start"})
    if "audio_base64" in message:
        audio = message.get("audio_base64")
        if isinstance(audio, str) and audio:
            mirrored.append({"type": "audio", "data": audio})
    if message.get("activity_end"):
        mirrored.append({"type": "activity_end"})
    if message.get("audio_stream_end"):
        mirrored.append({"type": "audio_stream_end"})
    return mirrored


class CaptionSidecar:
    """Fail-open transcription sidecar for one already-authorized Live session."""

    def __init__(
        self,
        *,
        environment: dict[str, str],
        binding: str,
        vocabulary: list[str],
        emit: Callable[[dict[str, Any]], None],
        provider_run: Callable[..., Awaitable[None]],
        guarded_runner: Callable[..., Awaitable[None]] | None = None,
        max_pending_bytes: int = MAX_PENDING_WIRE_BYTES,
    ):
        self.environment = environment
        self.binding = binding
        self.vocabulary = list(vocabulary)[:100]
        self.emit = emit
        self.provider_run = provider_run
        self.guarded_runner = guarded_runner
        self.reader = _BoundedJsonReader(max_pending_bytes=max_pending_bytes)
        self.task: asyncio.Task | None = None
        self.available = True
        self._unavailable_emitted = False
        self._pcm_buffer = bytearray()
        self._stop_lock = asyncio.Lock()
        self._stopped = False

    def start(self) -> None:
        if self.task is not None:
            return
        start = {
            "type": "start",
            "model": CAPTION_MODEL,
            "configuration": {
                "input_audio_transcription": {
                    "languageCodes": ["ru-RU", "en-US"],
                    "customVocabulary": self.vocabulary,
                    "mode": "SMART",
                },
                "manual_activity_detection": True,
            },
        }
        if not self.reader.feed(start):
            self._unavailable("CAPTION_START_BUFFER")
            return
        self.task = asyncio.create_task(
            self._run(),
            name="projects-hub-caption-sidecar",
        )

    def _unavailable(self, code: str) -> None:
        self.available = False
        if self._unavailable_emitted:
            return
        self._unavailable_emitted = True
        self.emit({"type": "caption_unavailable", "code": code})

    def _on_event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "")
        text = event.get("text")
        if kind == "ready":
            self.emit({"type": "caption_ready", "model": CAPTION_MODEL})
        elif kind == "interim_input_transcript" and isinstance(text, str) and text.strip():
            self.emit({"type": "caption_interim_transcript", "text": text})
        elif kind == "input_transcript" and isinstance(text, str) and text.strip():
            self.emit({"type": "caption_final_transcript", "text": text})
        elif kind == "error":
            self._unavailable(str(event.get("code") or "CAPTION_PROVIDER_ERROR")[:80])

    async def _run(self) -> None:
        try:
            runner = self.guarded_runner
            if runner is None:
                from ai_resource_control import run_guarded

                runner = run_guarded
            await runner(
                consumer="projects-hub",
                environment=self.environment,
                reader=self.reader,
                on_event=self._on_event,
                provider_run=self.provider_run,
                binding=self.binding,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._unavailable(
                str(getattr(exc, "code", "") or type(exc).__name__ or "CAPTION_ERROR")[:80]
            )
        finally:
            self.available = False
            self.reader.close()

    def _queue(self, item: dict[str, Any]) -> bool:
        if self.reader.feed(item):
            return True
        self._unavailable("CAPTION_BACKPRESSURE")
        asyncio.create_task(self.stop())
        return False

    def _flush_audio(self, *, all_bytes: bool = False) -> bool:
        while len(self._pcm_buffer) >= TRANSCRIBE_PCM_CHUNK_BYTES or (
            all_bytes and self._pcm_buffer
        ):
            take = (
                TRANSCRIBE_PCM_CHUNK_BYTES
                if len(self._pcm_buffer) >= TRANSCRIBE_PCM_CHUNK_BYTES
                else len(self._pcm_buffer)
            )
            chunk = bytes(self._pcm_buffer[:take])
            del self._pcm_buffer[:take]
            if not self._queue(
                {"type": "audio", "data": base64.b64encode(chunk).decode("ascii")}
            ):
                return False
        return True

    def feed(self, message: dict[str, Any]) -> bool:
        if not self.available or self.task is None:
            return False
        if message.get("activity_start"):
            # Never carry a partial PCM tail across user-turn boundaries.
            self._pcm_buffer.clear()
            if not self._queue({"type": "activity_start"}):
                return False
        if "audio_base64" in message:
            raw = message.get("audio_base64")
            if isinstance(raw, str) and raw:
                try:
                    self._pcm_buffer.extend(base64.b64decode(raw, validate=True))
                except (ValueError, binascii.Error):
                    self._unavailable("CAPTION_INVALID_AUDIO")
                    asyncio.create_task(self.stop())
                    return False
                if not self._flush_audio():
                    return False
        if message.get("activity_end"):
            if not self._flush_audio(all_bytes=True):
                return False
            if not self._queue({"type": "activity_end"}):
                return False
        if message.get("audio_stream_end"):
            if not self._flush_audio(all_bytes=True):
                return False
            if not self._queue({"type": "audio_stream_end"}):
                return False
        return True

    async def stop(self) -> None:
        async with self._stop_lock:
            if self._stopped:
                return
            task = self.task
            if task is None:
                self.reader.close()
                self._stopped = True
                return
            if not task.done():
                self.reader.feed({"type": "stop"})
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.25)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.reader.close()
            self._stopped = True
