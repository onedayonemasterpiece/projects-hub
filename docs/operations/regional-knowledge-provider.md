# Optional Regional Knowledge provider

`create_app()` now constructs the concrete HTTP provider when
`PROJECTS_HUB_RKB_CONFIG` names an explicit private descriptor. Absent/malformed
configuration keeps the resource unavailable and existing product startup intact.
No credential, RKB link, grant, service restart or activation is performed by this
change. The provider uses the existing shared Live adapter/tool boundary; it does
not add a voice transport, proxy, model, IAM registry or canonical ingestion path.

The receiver must be the existing RKB loopback app. The only accepted endpoint is
`http://127.0.0.1:PORT/internal/projects-hub/knowledge-search`. No redirects, public
HTTPS hop, proxy environment, browser OAuth, PH cookie forwarding or owner bearer
is used. The dedicated service credential authenticates PH's server and technical
scope. It grants no human rights. RKB independently resolves the exact current
verified `projects_hub` account link for this PH actor and configured tenant.

A reviewer must substitute the actual existing RKB loopback port and stable private
state paths. Both descriptor and credential file must be absolute regular files,
mode 0600, outside source control. The credential JSON must be issued by the
existing RKB local owner auth CLI (`personal_auth_cli service issue`), codec
`rkb.service-credential.v1`; a hand-written token file is insufficient. RKB
independently verifies the provisioned ID/epoch, token digest, current scope,
enabled state and expiry at final issuance. No disposable artifact/runtime env is allowed.
The descriptor is bounded to 32 actor/workspace scopes; changing its bytes,
permissions or credential invalidates existing adapters until a deliberate restart.

```json
{
  "codec": "ph.rkb-provider.v2",
  "endpoint": "http://127.0.0.1:PORT/internal/projects-hub/knowledge-search",
  "service_id": "projects-hub-reader",
  "service_token_file": "/ABSOLUTE/PRIVATE/ph-rkb-credential.json",
  "service_credential_id": "PROVISIONED_TECHNICAL_CREDENTIAL_UUID",
  "service_credential_epoch": 1,
  "provider_tenant": "REVIEWED_STABLE_PH_TENANT",
  "bindings": [{
    "actor_sub": "usr_VERIFIED_PH_ACTOR",
    "workspace_id": "ws_VERIFIED_PH_WORKSPACE",
    "link_id": "CANONICAL_VERIFIED_LINK_UUID",
    "link_revision": 2
  }]
}
```

Use the existing RKB participant binding flow, independently authenticated on both
sides: AccountBegin for the canonical current personal identity, trusted PH
provider observation with audience `rkb-account-binding`, then AccountConfirm with
that observation and exact revision. Inspect the resulting current link revision
and `public_search` grant. Never infer a canonical UUID from email, actor ID or a
model claim. The paired receiver descriptor uses codec `rkb.ph-reader.v2` and the
same service ID, tenant and bindings, under `RKB_PROJECTS_HUB_READER_CONFIG`; see
RKB `docs/operations/projects-hub-knowledge-reader.md`. The read endpoint cannot
create links or grants.

`rkb.knowledge-search.v2` preserves request ID, turn generation, explicit status
and reason, actual observed data/projection sequences, requested minimum sequence,
prior-write-pending, context gap and continuation. Failures never become empty
hybrid success. Hybrid retrieval is required for normal product acceptance;
`lexical_degraded` is explicit degradation. Source text is preserved exactly and
opaque evidence IDs/revisions stay attached. The same absolute 2-second monotonic
deadline begins when the shared framework observes the tool call, including time
waiting for its tool lock. New turns/Stop/cancellation retire pending optional reads;
late generation results cannot reach the model. There is no automatic search retry.

The mandatory existing utterance commitment still happens before tool execution.
Optional evidence context is coalesced into the existing source-event journal by
a bounded asynchronous writer (32 pending records, 512 KiB, 20ms lock acquisition).
Only an exact committed utterance can receive a checkpoint: source, conversation,
session, turn, tool call, request ID, ordered evidence refs/revisions and result hash
are retained. Disposition is `computed`, never an invented transport/completion/
audible ACK. Busy/failing checkpoints leave an explicit context gap and cannot
block the canonical utterance journal indefinitely. On a new/rebuilt model context,
old evidence is not reissued without current RKB authority: the initial context
explicitly reports `evidence_context_unavailable`. Provider handles and nearby
transcripts are not evidence recovery. There is no claim of automatic evidence
revalidation/replay across process restart.

Observed running PH release when this work began: `aacce79d010a91a694cdd0a033953d87c9ae0141`
from the actual systemd process working directory/command. It is an ancestor of
this branch's main base `08e8f74e9fbd0db78156e2161ffc0557f8f9f138`.
Before a reviewed deployment, repeat ancestry checks against the then-running
releases and a merged main candidate. Keep current product features, shared Live
SDK and utterance recovery. Rollback removes the optional env key and restores the
previous reviewed main release; retain voice/ingestion state.

Focused unit/ASGI tests establish the inert wiring, codec and fences. No real
credential/link/config was changed, no downstream provider request was made and no
production service was restarted. Actual PH→RKB hybrid E2E, phone/microphone and
non-owner acceptance remain separate checks after reviewed activation.
