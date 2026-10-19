from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
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
        native_offset_clock: str = "unverified",
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
        self._started = False
        self.native = False
        if native_offset_clock not in {"unverified", "connection_pcm"}:
            raise ValueError("unsupported_caption_offset_clock")
        # Server-owned capability. connection_pcm requires an exact-model real
        # two-utterance/reconnect receipt; it is never accepted from a client.
        self.native_offset_clock = native_offset_clock
        self.diagnostic_observer: Callable[[dict[str, Any], str | None], None] | None = None
        self._accepted_scope: dict[str, Any] | None = None
        self._accepted_end_us: int | None = None
        self._accepted_open = False
        self._pcm_scope: dict[str, Any] | None = None
        self._pcm_capture_start_us: int | None = None

    def start(self, *, native: bool = False) -> None:
        if self._started or self._stopped:
            return
        self._started = True
        self.native = native
        if native and self.native_offset_clock == "unverified":
            self._unavailable("CAPTION_CLOCK_UNVERIFIED", disable_provider=False)
            if self.diagnostic_observer is None:
                # Do not reserve a second model/lease for captions that cannot
                # honestly be shown. Accepted-audio lifecycle still reaches UI.
                return
        start = {
            "type": "start",
            "model": CAPTION_MODEL,
            "configuration": {
                "input_audio_transcription": {
                    "languageCodes": ["ru-RU", "en-US"],
                    "customVocabulary": self.vocabulary,
                    "mode": "VERBATIM" if native else "SMART",
                },
                "manual_activity_detection": True,
            },
        }
        if native:
            start["configuration"]["native_caption_provenance"] = {
                "offset_clock": self.native_offset_clock,
                "diagnostics": self.diagnostic_observer is not None,
            }
        if not self.reader.feed(start):
            self._unavailable("CAPTION_START_BUFFER")
            return
        self.task = asyncio.create_task(
            self._run(),
            name="projects-hub-caption-sidecar",
        )

    def _unavailable(self, code: str, *, disable_provider: bool = True) -> None:
        if disable_provider:
            self.available = False
        if self._unavailable_emitted:
            return
        self._unavailable_emitted = True
        self.emit({"type": "caption_unavailable", "code": code})

    def _observe(self, event: dict[str, Any], transcript_text: str | None = None) -> None:
        observer = self.diagnostic_observer
        if observer is not None:
            try:
                # The second argument is ephemeral, for an authorized canary's
                # marker matching only. It is never logged, persisted or emitted.
                observer(event, transcript_text)
            except Exception:
                # Optional diagnostics cannot break the main voice path.
                pass

    def _on_event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "")
        text = event.get("text")
        if self._stopped:
            return
        if kind == "caption_audio_sent":
            self._observe(event)
            return
        timing = event.get("caption_timing")
        if self.native and isinstance(timing, dict):
            self._observe(dict(timing), text if isinstance(text, str) else None)
        if kind == "ready":
            if not self.native or self.native_offset_clock != "unverified":
                self.emit({"type": "caption_ready", "model": CAPTION_MODEL})
        elif kind in {"interim_input_transcript", "input_transcript"} and isinstance(text, str) and text.strip():
            caption = {"type": "caption_interim_transcript" if kind == "interim_input_transcript"
                       else "caption_final_transcript", "text": text}
            if self.native:
                from live_interaction.caption_provenance import caption_scope

                scope = caption_scope(event.get("caption_scope"))
                if (self.native_offset_clock == "unverified" or scope is None
                        or not isinstance(timing, dict) or timing.get("assigned") is not True
                        or timing.get("offset_clock") != "connection_pcm"):
                    self._unavailable("CAPTION_ALIGNMENT_UNAVAILABLE", disable_provider=False)
                    return
                if scope != self._accepted_scope:
                    return
                caption["caption_scope"] = scope
                caption["caption_timing"] = {
                    key: timing[key] for key in ("provider_connection_generation", "offset_clock", "assigned",
                                                 "start_offset", "end_offset") if key in timing
                }
                self._unavailable_emitted = False
            self.emit(caption)
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
        item = {**item, "queued_at": round(time.time() * 1000)}
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
            item = {"type": "audio", "data": base64.b64encode(chunk).decode("ascii")}
            if self.native:
                start = self._pcm_capture_start_us
                if start is None or self._pcm_scope is None:
                    return False
                end = start + take * 1_000_000 // 32_000
                item.update(caption_scope=self._pcm_scope, capture_start_us=start, capture_end_us=end)
                self._pcm_capture_start_us = end
            if not self._queue(item):
                return False
        return True

    def feed(self, message: dict[str, Any]) -> bool:
        if self._stopped or self._native_caption_clock_unavailable():
            return False
        if self.native:
            if message.get("activity_start"):
                self._accepted_scope = None
                self._accepted_end_us = None
                self._accepted_open = False
            if (message.get("activity_end") or message.get("audio_stream_end")) and self._accepted_open:
                self._accepted_open = False
                self.emit({"type": "caption_turn_ended", "caption_scope": self._accepted_scope,
                           "capture_end_us": self._accepted_end_us})
        if not self.available or self.task is None:
            return False
        if message.get("activity_start"):
            # Never carry a partial PCM tail across user-turn boundaries.
            self._pcm_buffer.clear()
            self._pcm_scope = None
            self._pcm_capture_start_us = None
            if not self._queue({"type": "activity_start"}):
                return False
        if "audio_base64" in message:
            raw = message.get("audio_base64")
            if isinstance(raw, str) and raw:
                try:
                    pcm = base64.b64decode(raw, validate=True)
                except (ValueError, binascii.Error):
                    self._unavailable("CAPTION_INVALID_AUDIO")
                    asyncio.create_task(self.stop())
                    return False
                if not self.feed_pcm(pcm):
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

    def _native_caption_clock_unavailable(self) -> bool:
        # Production native caption offsets have not been proven across
        # reconnect. The first start() already emitted CAPTION_CLOCK_UNVERIFIED
        # and opened NO caption provider. Skip further zero-value scope checks
        # and lifecycle events, retaining canary probes with an observer.
        return (self.native and self._started and self.native_offset_clock == "unverified"
                and self.diagnostic_observer is None)

    def feed_pcm(self, pcm: bytes, *, caption_scope: dict[str, Any] | None = None,
                 capture_start_us: int | None = None) -> bool:
        """Reuse the shared decoder's PCM view without another base64 roundtrip."""
        if self._stopped or self._native_caption_clock_unavailable():
            return False
        if not isinstance(pcm, bytes) or len(pcm) % 2 or len(pcm) > 12_000:
            self._unavailable("CAPTION_INVALID_AUDIO")
            asyncio.create_task(self.stop())
            return False
        if not pcm:
            return True
        if self.native:
            from live_interaction.caption_provenance import caption_scope as validate_scope

            scope = validate_scope(caption_scope)
            if (scope is None or isinstance(capture_start_us, bool)
                    or not isinstance(capture_start_us, int) or capture_start_us < 0):
                self._unavailable("CAPTION_SOURCE_UNAVAILABLE", disable_provider=False)
                return False
            capture_end_us = capture_start_us + len(pcm) * 1_000_000 // 32_000
            if scope != self._accepted_scope:
                self._accepted_scope = scope
                self._accepted_open = True
                self.emit({"type": "caption_turn_started", "caption_scope": scope,
                           "capture_start_us": capture_start_us})
            self._accepted_end_us = capture_end_us
            if self.task is not None and self.available:
                expected = (self._pcm_capture_start_us + len(self._pcm_buffer) * 1_000_000 // 32_000
                            if self._pcm_capture_start_us is not None else None)
                if self._pcm_buffer and (scope != self._pcm_scope or capture_start_us != expected):
                    if not self._flush_audio(all_bytes=True):
                        return False
                if not self._pcm_buffer:
                    self._pcm_scope = scope
                    self._pcm_capture_start_us = capture_start_us
        if not self.available or self.task is None:
            return False
        self._pcm_buffer.extend(pcm)
        return self._flush_audio()

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
            self._pcm_buffer.clear()
