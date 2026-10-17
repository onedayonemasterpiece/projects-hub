# Provenance and offline reproduction

The three original measurement files below are preserved **byte-for-byte**.
These synthetic probes do not call a provider, load credentials/private SDK, run
a product server, write recordings, or access production data. Their results are
not mobile battery, ASR quality, end-to-end latency or concurrent VPS capacity.

| Original file | Measurement |
|---|---|
| `serialization-results.json` | 11 Python helper cases × five timed trials; Python 3.12.14 |
| `browser-helpers-results.json` | Five JS helper cases × five timed trials; Node v24.19.0 |
| `opus_probe_results.json` | Six codec profiles × three trials of 20 synthetic audio seconds; Python 3.12.14, libopus 1.4 |

Helper source baseline: live-interaction
`3b85224962c396e1a52977d6b81eee659db0cacc`, projects-hub code
`4a4d658cc5209429c7ed584ed0c1a8b51700743d` (documentation head `ffa3b508`).
Actual source SHA256 maps are in the JSON. The original Python map uses scratch
paths; the portable script records imported module names and `module.__file__`.
The Opus probe is independent of those checkouts and records libopus version and
codec parameters. Literal reference labels alone do not verify a future checkout.

## Original invocation

From `/workspace/scratch/0702e2310172`:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=continuation-final/framework/python:continuation-final/consumer/src timeout 45 checkpoint-review/venv/bin/python -B audio-efficiency-local/serialization_benchmark.py > audio-efficiency-local/serialization-summary.json
node --expose-gc audio-efficiency-local/browser_helpers_benchmark.mjs > audio-efficiency-local/browser-helpers-summary.json
```

The original `/workspace/scratch/0702e2310172/opus_probe.py` wrote the adjacent
`opus_probe_results.json`. Its exact interpreter executable path/argv was not
captured in this appendix's evidence; the JSON confirms Python 3.12.14.
`python3 opus_probe.py` is an equivalent invocation, not the verified original argv.

The original browser script/result gained source-hash metadata after timing;
no measured operation or timing value changed. That metadata is retained here.

## Portability adjustments

Only source/output discovery and provenance metadata changed in `../probes/`:

- Python uses `PYTHONPATH` and hashes the modules actually imported.
- Node requires `FRAMEWORK_ROOT` and imports the same helper exports.
- Opus discovers local libopus or uses `OPUS_LIBRARY`; missing libopus gets a clear error.
- `AUDIO_PROBE_OUTPUT` selects each output; defaults are new `*.local.json` files in cwd.

Inputs, waveform, codec settings, warmups, trials and timed operations remain
unchanged. `portability-adjustments.patch` is the exact original→portable diff.
`provenance.json` records all script/measurement hashes and verification: Python
measured function ASTs match; the complete Node timed region is byte-identical.

## Run from any working directory

Verified on Linux x86_64 with Python 3.12.14, `websockets==16.1.1`, Node v24.19.0
and libopus 1.4. The helper probes need both source checkouts and websockets;
the codec probe needs Python standard library plus installed libopus. No full
application/private SDK is needed. Other OS/runtime combinations were not tested.

```sh
AUDIO_CONSUMER_ROOT=/path/to/projects-hub
FRAMEWORK_ROOT=/path/to/live-interaction
AUDIO_PROBES_ROOT="$AUDIO_CONSUMER_ROOT/docs/research/voice-audio-efficiency-20261017/probes"
AUDIO_OUTPUT_ROOT=/tmp/voice-audio-probe-results
mkdir -p "$AUDIO_OUTPUT_ROOT"

PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH="$FRAMEWORK_ROOT/python:$AUDIO_CONSUMER_ROOT/src" \
AUDIO_PROBE_OUTPUT="$AUDIO_OUTPUT_ROOT/serialization.local.json" \
python3.12 -B "$AUDIO_PROBES_ROOT/serialization_benchmark.py"

FRAMEWORK_ROOT="$FRAMEWORK_ROOT" \
AUDIO_PROBE_OUTPUT="$AUDIO_OUTPUT_ROOT/browser-helpers.local.json" \
node --expose-gc "$AUDIO_PROBES_ROOT/browser_helpers_benchmark.mjs"

AUDIO_PROBE_OUTPUT="$AUDIO_OUTPUT_ROOT/opus.local.json" \
python3.12 -B "$AUDIO_PROBES_ROOT/opus_probe.py"
```

`OPUS_LIBRARY=/absolute/path/to/libopus.so` can select a fixed installed codec build.

## Portable invocation check

All three scripts completed sequentially from `/tmp`, outside both checkouts:
11 serialization cases, five browser cases and six Opus profiles. The receipt
`portability-check-result.json` records argv, environment, exit status and wall
time. Helper source hashes and libopus version match the original measurements;
original JSON files remain byte-identical.

`portability-check-serialization.json`, `portability-check-browser-helpers.json`
and `portability-check-opus.json` are **new portability smoke-check runs**. They do
not replace the original research values. No new product performance claim is
made from them.
