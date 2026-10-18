# Existing provider spike: bounded continuation patch

## Completed invocation — 17 October 2026

The single authorized `run_spike.sh all` invocation has already completed.
**Do not repeat it as an unfinished next step.** The retained receipt is
[spike-all-20261017.summary.json](spike-all-20261017.summary.json); the adjacent
redacted evidence is a safe terminal projection, not the original JSONL bytes.

Realtime and burst phases produced model audio and completed turns. Neither
phase yielded a currently resumable checkpoint after audio end, so recall was
skipped; idle renewal ended with `TimeoutError`. The overall acceptance result
is **NOT ACCEPTED**, even though the runner reached its final receipt. One
authority lease, three provider connection attempts and 4,147 reserved units
were recorded; release was confirmed. This is not a vendor billing audit.

Source attribution passed on the host under Python 3.14.4. This run does not
qualify the compressed Android path, 90-minute conversation, audible long-tool
progress, mobile network recovery, or production capacity. Preparation and
offline-test notes below describe the earlier patch stage. Another live
invocation requires a concrete remaining hypothesis and its own bounded plan;
a skipped/failed gate is not permission to silently repeat the completed run.

This modifies the existing `provider_spike.py`; it does not add another live
harness or change a product/shared SDK source file. No real SDK/provider call was
made while preparing this patch. The exact host original is `provider_spike.host-original.py` (SHA-256 `7c092a46d6378730d9f530142a977966067befe9ff4dd1b8c6b318c738978892`). The initially reconstructed `provider_spike.original.py` contains one extra final newline; its different hash is not a different implementation. The patch is now based on the exact host bytes.

## Run boundary

Use one invocation of the original runner with the new `all` mode to cover
realtime pacing, burst pacing, checkpoint recall and idle GoAway renewal under
one `run_guarded` call and **one authority lease**. The SDK lease object is passed
unchanged to every existing `provider.run` invocation. There is no key extraction,
comparison, persistence or logging in the runner. A second acquire is refused;
the SDK's outage fallback is disabled for this spike.

The two existing individual modes remain available for diagnosis, but their
in-memory cap starts afresh on each invocation. A second process is a separate
spend authorization decision; do not call separate `pacing` and `idle` invocations
and describe their combined limit as 150,000. This is no replacement for the
shared authority's durable ledger.

### Authority reservation cap

`SpikeControl.rpc` wraps the inspected SDK `Control.rpc` extension point:

- Before `acquire` or `grant`, check `p_tokens` against 150,000 minus known
  reservations. All actual RPCs, including retries, stay inside the SDK.
- Count the initial acquire's `data.tokens` and each subsequent grant's
  `data.tokens` separately. Do not count `resource_budget.granted_units`:
  the installed SDK emits that after adding unused carry, and the initial
  reservation produces no such event.
- A definitive `RESOURCE_TOKEN_BUDGET` denial adds no reservation and allows the
  SDK's own smaller retry. An ambiguous or cancelled mutating request retains
  the requested amount conservatively, marks accounting incomplete and forbids
  another admission. Heartbeat/fault/release remain available for cleanup.
- A lock prevents concurrent requests from both passing the same remaining cap.

This is an **authority reservation cap**, not a guarantee of a vendor billing
total. The installed SDK admits estimated input units. Provider output and token
usage arrive later; the shared setup does not currently expose an independently
verified output-token ceiling. No unverified provider parameter was injected.

The final receipt separately reports reservations, unresolved requested units,
the SDK's admitted input estimate, and SDK-collected provider input/output token
observations. Absent prompt/response usage fields are `null`, not measured zero.
The SDK's collectors use per-turn maxima and completed-turn accumulation; these
are neither summed reservation grants nor an independent billing audit.
`authority_lease_release_confirmed` means the release RPC returned successfully;
it does not claim that a final heartbeat acknowledged every latest observation.

### Six provider connection attempts

A temporary wrapper around `websockets.connect` counts attempts **before socket
creation** and rejects the seventh with `SPIKE_PROVIDER_CONNECTION_CAP`.
This includes initial setup, normal GoAway renewal, error reconnects and the
shared provider's cold retry after a rejected checkpoint. The wrapper is scoped
to this isolated runner process and is restored in `finally`. The shared
provider imports `connect` inside `run`, so counting key reads or only Python
`Session()` objects would miss reconnects.

### Input and lifecycle corrections

All queued commands now carry `queued_at`; one positive
`audio_turn_generation` accompanies activity start, every PCM chunk and activity
end. The pacing receipt waits for the matching provider `input_timing`, checks
the actual sent chunk count and rejects an observed dropped input. Turn latency
starts at `activity_end_sent_at`, rather than end enqueue. It uses worker/runner
wall-clock milliseconds on one host and is not phone/network latency.

Checkpoint recall only uses a currently resumable handle observed after the
audio-end event; `resumable=false` clears it. Recall flags require successful
checkpoint mode without a rejected-resumption event; `forty` alone no longer
counts as `forty two`. The synthetic transcript remains in the receipt so a human
can assess these simple marker checks. An early setup checkpoint is insufficient.

Each existing scenario uses `try/finally` to stop and join its provider task.
Cancellation already in progress cancels the child immediately; cancellation
arriving during normal Stop is propagated, so it cannot start the next scenario.
The SDK still owns heartbeat cancellation, lease release and fencing. The run has
an 18-minute deadline plus SDK/transport cleanup; the idle parameter is bounded
to 13 minutes, and the PCM input is bounded to 15 seconds.

## Verified source boundary

Before reading environment files or acquiring a lease, the runner hashes the
actually imported sources and aborts with `SPIKE_SOURCE_MISMATCH` unless they
match the inspected versions:

| Source | SHA-256 |
|---|---|
| Tested continuation framework `provider.py` (`3b852249`) | `22b8aa8d3901d09953bc4378383af8c121b875a81ad13dc7b430e5af7d1d59f5` |
| Installed SDK `client.py`, 651 lines (host receipt) | `7bb64af520ffe10593a44dc2deb4c2f16941611398057eecc67c6960f0c608b8` |
| Installed SDK `live.py`, 473 lines (host receipt; same as current repo) | `a21d8e366a7670b95c6bb467cdad648f906aeb557d02d1c5ce76fcdefb68867d` |

The main SDK repository client has 700 lines and is not asserted equivalent to
the installed client. The sanitized `sdk-reference/*client*` files are read-only
reference material and are deliberately not imported or copied into a runtime.

Relevant inspected installed SDK locations:

- `installed-client-sanitized-reference.py:129`: RPC/retry boundary.
- `:251–265`: central acquire requests `config.grant_tokens` as `p_tokens`.
- `:436–487`: shared pre-send estimator/grant/carry accounting.
- `:489–496`: budget observer exceptions are swallowed, so callbacks cannot fence grants.
- `:498–513`: provider usage observation semantics.
- `live.py:299–317`: acquire and sanitized budget-event forwarding.
- `live.py:340–417`: provider/heartbeat cleanup and startup failover path.

## Offline verification

`test_provider_spike.py`: **14 passed in 0.38 s**, Python 3.12 test venv. The
runtime host was independently reported as Python 3.14.4; these are distinct
execution environments. See `offline-tests-result.json` for the exact command.

The tests cover initial/incremental reservation admission, carry exclusion,
definitive refusal, ambiguous and cancelled mutations, concurrent admission,
single acquire/fallback suppression, seventh-connect rejection and restoration,
both cancellation timings, source attribution and pre-environment source mismatch,
handle privacy/invalidation, and unknown usage fields. One integration test runs
the real candidate provider through all four original scenario phases with fake
sockets and a fake SDK: five connection attempts, one lease, every socket closed,
correlated activity-end receipts, successful synthetic checkpoint markers and a
synthetic GoAway renewal.

The fake SDK intentionally models only the inspected interface. These tests do
not certify a live authority transaction, a real provider semantic result,
microphone behavior, host capacity, or the SDK's full release/accounting behavior.
The original runner and changed runner were never run against real providers
during this preparation. Source hashes, the runner diff and offline test receipt
are preserved alongside this handoff.
