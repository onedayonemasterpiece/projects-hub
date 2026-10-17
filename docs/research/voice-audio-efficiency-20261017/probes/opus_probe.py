"""Bounded local codec microbenchmark, not a product implementation.

Uses the installed libopus through ctypes. Synthetic input is deliberately not
an intelligibility/ASR test. CPU totals include the Python/ctypes harness, and
packet call latency includes FFI and timer overhead. No provider/network calls.
"""
import ctypes as C
import ctypes.util
import collections
import json
import math
import os
import pathlib
import platform
import random
import statistics
import time

library_path = os.environ.get("OPUS_LIBRARY") or ctypes.util.find_library("opus")
if not library_path:
    raise RuntimeError("libopus was not found; install it or set OPUS_LIBRARY to its shared library path.")
lib = C.CDLL(library_path)
I16P, U8P = C.POINTER(C.c_int16), C.POINTER(C.c_ubyte)
for name, args, result in [
    ("opus_encoder_create", [C.c_int, C.c_int, C.c_int, C.POINTER(C.c_int)], C.c_void_p),
    ("opus_decoder_create", [C.c_int, C.c_int, C.POINTER(C.c_int)], C.c_void_p),
    ("opus_encoder_destroy", [C.c_void_p], None),
    ("opus_decoder_destroy", [C.c_void_p], None),
    ("opus_encoder_get_size", [C.c_int], C.c_int),
    ("opus_decoder_get_size", [C.c_int], C.c_int),
    ("opus_encode", [C.c_void_p, I16P, C.c_int, U8P, C.c_int], C.c_int),
    ("opus_decode", [C.c_void_p, U8P, C.c_int, I16P, C.c_int, C.c_int], C.c_int),
    ("opus_get_version_string", [], C.c_char_p),
]:
    f = getattr(lib, name)
    f.argtypes, f.restype = args, result
lib.opus_encoder_ctl.argtypes = [C.c_void_p, C.c_int]
lib.opus_encoder_ctl.restype = C.c_int

SECONDS, REPEATS, FRAME_MS = 20, 3, 20

def signal(rate):
    """Deterministic harmonic voiced-like bursts, noise bursts and silence."""
    rng = random.Random(170026)
    vals = []
    for n in range(rate * SECONDS):
        t = n / rate
        phase = t % 1.0
        if phase < 0.65:
            envelope = math.sin(math.pi * phase / 0.65) ** 2
            pitch_phase = 2 * math.pi * (130 * t + 5 * math.sin(2 * math.pi * 0.7 * t))
            x = envelope * (0.24 * math.sin(pitch_phase) + 0.10 * math.sin(2 * pitch_phase)
                            + 0.06 * math.sin(3 * pitch_phase) + 0.02 * rng.uniform(-1, 1))
        elif phase < 0.85:
            x = 0.18 * rng.uniform(-1, 1)
        else:
            x = 0.0
        vals.append(round(max(-1, min(1, x)) * 32767))
    count = rate * FRAME_MS // 1000
    return [(C.c_int16 * count)(*vals[i:i+count]) for i in range(0, len(vals), count)]

def percentile(values, q):
    values = sorted(values)
    return values[math.ceil(len(values) * q) - 1]

def run(rate, bitrate, complexity, application, frames):
    cpu_enc, cpu_dec, lat_enc, lat_dec, sizes = [], [], [], [], []
    mode_counts = collections.Counter()
    count = rate * FRAME_MS // 1000
    lookahead = C.c_int()
    for _ in range(REPEATS):
        err = C.c_int()
        enc = lib.opus_encoder_create(rate, 1, application, C.byref(err))
        assert enc and err.value == 0
        dec = lib.opus_decoder_create(rate, 1, C.byref(err))
        assert dec and err.value == 0
        output = (C.c_ubyte * 1276)()
        pcm = (C.c_int16 * count)()
        try:
            for ctl, value in [(4002, bitrate), (4010, complexity), (4006, 1), (4012, 0), (4016, 0)]:
                assert lib.opus_encoder_ctl(enc, ctl, C.c_int(value)) == 0
            assert lib.opus_encoder_ctl(enc, 4027, C.byref(lookahead)) == 0
            # Warm both states together for one second; no reset per packet.
            for frame in frames[:50]:
                size = lib.opus_encode(enc, frame, count, output, len(output))
                assert size > 0
                assert lib.opus_decode(dec, output, size, pcm, count, 0) == count
            packets = []
            start_cpu = time.process_time_ns()
            for frame in frames:
                start = time.perf_counter_ns()
                size = lib.opus_encode(enc, frame, count, output, len(output))
                lat_enc.append(time.perf_counter_ns() - start)
                assert size > 0
                sizes.append(size)
                toc_config = output[0] >> 3
                mode_counts['SILK' if toc_config < 12 else 'HYBRID' if toc_config < 16 else 'CELT'] += 1
                packets.append(bytes(output[:size]))
            cpu_enc.append((time.process_time_ns() - start_cpu) / 1e9)
            # Prebuild packet views so decode timings don't include input copying.
            inputs = [(C.c_ubyte * len(packet)).from_buffer_copy(packet) for packet in packets]
            start_cpu = time.process_time_ns()
            for packet in inputs:
                start = time.perf_counter_ns()
                decoded = lib.opus_decode(dec, packet, len(packet), pcm, count, 0)
                lat_dec.append(time.perf_counter_ns() - start)
                assert decoded == count
            cpu_dec.append((time.process_time_ns() - start_cpu) / 1e9)
        finally:
            lib.opus_encoder_destroy(enc)
            lib.opus_decoder_destroy(dec)
    return {
        "sample_rate_hz": rate, "channels": 1, "target_bitrate_bps": bitrate,
        "complexity": complexity, "application": "VOIP" if application == 2048 else "AUDIO",
        "vbr": True, "fec": False, "dtx": False, "frame_ms": FRAME_MS,
        "lookahead_ms": lookahead.value * 1000 / rate,
        "observed_payload_bitrate_bps": sum(sizes) * 8 / (SECONDS * REPEATS),
        "packet_bytes_mean": statistics.mean(sizes), "packet_bytes_max": max(sizes),
        "packet_mode_counts_from_toc": dict(mode_counts),
        "encode_cpu_ms_per_audio_second_median": statistics.median(cpu_enc) / SECONDS * 1000,
        "decode_cpu_ms_per_audio_second_median": statistics.median(cpu_dec) / SECONDS * 1000,
        "encode_call_us_p50": percentile(lat_enc, .5) / 1000,
        "encode_call_us_p95": percentile(lat_enc, .95) / 1000,
        "encode_call_us_max": max(lat_enc) / 1000,
        "decode_call_us_p50": percentile(lat_dec, .5) / 1000,
        "decode_call_us_p95": percentile(lat_dec, .95) / 1000,
        "decode_call_us_max": max(lat_dec) / 1000,
        "encode_cpu_seconds_trials": cpu_enc, "decode_cpu_seconds_trials": cpu_dec,
    }

all_frames = {rate: signal(rate) for rate in (16000, 24000)}
results = []
for rate, bitrate, app in [(16000, 24000, 2048), (24000, 32000, 2049)]:
    for complexity in (0, 5, 10):
        results.append(run(rate, bitrate, complexity, app, all_frames[rate]))

report = {
    "scope": "synthetic single-stream sequential native-codec cost probe; not a Go/Rust/Python comparison or capacity/quality test",
    "python": platform.python_version(), "machine": platform.machine(),
    "cpu": next((x.split(':', 1)[1].strip() for x in pathlib.Path('/proc/cpuinfo').read_text().splitlines() if x.startswith('model name')), "unknown"),
    "libopus": lib.opus_get_version_string().decode(), "libopus_library": library_path,
    "seconds_per_trial": SECONDS, "trials": REPEATS, "warmup_audio_seconds_per_trial": 1,
    "encoder_state_bytes_mono": lib.opus_encoder_get_size(1),
    "decoder_state_bytes_mono": lib.opus_decoder_get_size(1),
    "notes": ["CPU includes Python/ctypes loop and instrumentation; encode includes saving compressed bytes for decoder input.",
              "Call latencies include FFI and timer overhead; host is shared, no CPU affinity or target phone/server measurement.",
              "No TLS, WSS, fsync, provider, ASR, network, resampling, parallel-stream or battery measurement.",
              "State sizes are direct libopus state allocations, not process RSS/stack/queues or transport memory."],
    "results": results,
}
target = pathlib.Path(os.environ.get("AUDIO_PROBE_OUTPUT", "opus_probe_results.local.json")).expanduser().resolve()
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
