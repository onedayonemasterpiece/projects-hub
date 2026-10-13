# MCP Events P0 connection

Implementation follows the [OpenAI MCP Events contract](https://developers.openai.com/plugins/build/mcp-events)
and [OAuth connection requirements](https://developers.openai.com/plugins/build/auth).
P0 acceptance requires an actual idle dot, not a local test or callback 2xx.
MVP0 discussion bindings are intentionally gated on that acceptance.

## Server and scope

Endpoint: `https://projects-hub.kenigevents.ru/mcp` (available after activation of
this change). Python MCP SDK minimum 2.3; tested version 2.3.0. The SDK owns the
2026-07-28 envelope and transport. Its capabilities model lacks draft Events;
the adapter restores `capabilities.events` after discovery serialization and
registers the three draft handlers on the same authenticated SDK transport.

This connection has only `projects-hub:test`: read saved test requests, save
nonce responses and subscribe to `projects_hub.test_requested`. The owner
selects one project on the first-party consent page. Current project-owner ACL,
membership, grant lifetime and resource binding are checked on every tool call
and before delivery. Browser cookies are never machine credentials. Only the
exact `/mcp` and OAuth register/token/revoke paths have separate machine auth;
existing edge checks and Origin protection remain on browser writes and consent.

OAuth uses DCR public clients, S256 PKCE, one-use codes (60 seconds), hashed opaque
access tokens (one hour) and rotating refresh tokens. Consent lasts at most 30
days. Redirects are limited to the official stable ChatGPT redirect and official
callback-ID paths; the issuer is echoed. No integration grant exists until the
owner explicitly submits consent. Refresh-token reuse revokes the grant.

## Connect as owner

1. Sign into Projects Hub with your existing owner session.
2. Add the private plugin from `plugins/projects-hub` / the supplied ZIP, or use
   the supported plugin developer connection flow with the endpoint above.
   Select OAuth and DCR; no client secret or API key is required. Rescan tools
   and events after updating the server.
3. Complete the Projects Hub consent screen and select the test project. Read
   its `project_id` from the Projects Hub project URL, or ask the backend operator
   to return that project's ID. Do not paste tokens or cookies into chat.
4. In a dot or Work Cloud chat request: “Subscribe to
   `projects_hub.test_requested` for project_id `<id>`. On a test event, read
   `get_test_request`; verify project_id and eventId, then call
   `record_test_response` using the saved nonce and a response containing it.”
5. The backend operator checks successful callback verification and persistent
   subscription using the command below. Finish setup and leave the dot idle.
   Do not send a fresh user message after triggering the acceptance request.

The callback must use HTTPS and resolve entirely to public addresses. The
sender pins the validated destination IP on each connection and verifies TLS
against the original hostname; it ignores proxies and does not follow redirects.
Standard Webhooks 1.0.0 signs exact serialized bytes with the supplied `whsec_`
secret. Rotation dual-signs for five minutes. Secrets are encrypted in SQLite
using a domain-separated key derived from the existing private session key.
Keep that key stable across deployment/restart; never include it in a report.

## One backend request

Run the existing installed release's Python with its normal service environment
(through the existing on-host administrative runner). Never create an anonymous
HTTP endpoint or call note/analysis tools for this check:

```bash
scripts/run_mcp_test_request.sh subscriptions
scripts/run_mcp_test_request.sh create --project-id <id> --command-id p0-20261013-one
scripts/run_mcp_test_request.sh status --request-id <returned-request-id>
```

Creation requires the existing platform owner, current project ACL and at least
one active verified subscription. Reuse the command id after a lost response.
The command deliberately omits the nonce. Creation and delivery intent share
one SQLite transaction. Subscription TTL is capped at 24 hours (including a
request for no expiry). The request defaults to 15 minutes. There is no replay
cursor; existing pending outbox entries persist across restart. Delivery tries
at most five times with bounded backoff and the same eventId. `410`, `413`, other
permanent 4xx and redirects terminate that delivery. HTTP 2xx records `accepted`
while the request remains `awaiting_response` until the write tool succeeds.

Agree an observation window with the owner before the live request. Record
release SHA, SDK version, endpoint, request_id, eventId, subscription id, created
and accepted times, response author/time and stored response. If a callback
accepts but no dot response is saved, report that exact boundary. A late response
or one prompted by another user message does not establish idle-dot acceptance.
The outbox worker only sends persisted intents; it does not poll ChatGPT.

## Disable

Ask the subscribed dot to stop monitoring; `events/unsubscribe` is idempotent.
Disconnect the plugin. Its advertised RFC 7009 revocation endpoint revokes the
integration grant when the host calls it. For immediate server-side revocation,
use the existing signed-in owner's browser to GET `/api/mcp/grants`, then POST
`/api/mcp/grants/<grant-id>/revoke` from the same Origin. Revocation invalidates
access/refresh tokens and cancels future sends. Already sent events cannot be
recalled, but their tools recheck access. Domain requests/responses are retained.

No project discussion binding, Slack message, or active Mira delivery is claimed
by P0. Those are the subsequent MVP0 implementation after live P0 PASS.
