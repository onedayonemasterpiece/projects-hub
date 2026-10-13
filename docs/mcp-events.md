# MCP Events P0 connection

Implementation follows the [OpenAI MCP Events contract](https://developers.openai.com/plugins/build/mcp-events)
and [OAuth connection requirements](https://developers.openai.com/plugins/build/auth).
P0 acceptance requires an actual idle dot, not a local test or callback 2xx.
The legacy-event P0 passed on 2026-10-13; see the retained/live evidence in
`docs/reports/mcp-events-p0-20261013.md`. The stable contract follows
`docs/prompts/projects-hub-mcp-events-stable-contract-20261013.md` and needs its
own managed subscription transition and live acceptance.

## Server and scope

Endpoint: `https://projects-hub.kenigevents.ru/mcp`. Python MCP SDK minimum 2.3;
tested version 2.3.0. The SDK owns the
2026-07-28 envelope and transport. Its capabilities model lacks draft Events;
the adapter restores `capabilities.events` after discovery serialization and
registers the three draft handlers on the same authenticated SDK transport.

The service credential's baseline scope remains `projects-hub:test`: read saved
test requests and save nonce responses. It does not automatically authorize live
discussion access. Per the owner's
updated requirement, the connection represents the service integration, not an
individual user. An administrator provisions one confidential OAuth client with
access to these test operations across all active projects, including new ones.
Event subscriptions still require an explicit `project_id` filter. The service
actor has no browser session, workspace membership, or platform-owner privileges;
responses record its identity instead of impersonating a user. Client enablement,
project status, grant lifetime and resource binding are checked on every tool
call and before delivery. Live requests additionally require a current explicit
owner-approved binding to a single real `note_id`, service client and confirmed
Slack thread. The binding owner must still have editor access; the saved contextual
actor must still have project access. Browser cookies are never machine credentials. Only the
exact `/mcp` and OAuth register/token/revoke paths have separate machine auth;
existing edge checks and Origin protection remain on browser writes and consent.

OAuth uses a provided confidential client (`client_secret_basic` or
`client_secret_post`), S256 PKCE, one-use codes (60 seconds), hashed opaque access
tokens (one hour) and rotating refresh tokens. Grants last at most 365 days and
can be revoked immediately. This is the host-supported authorization-code flow;
it does not use `client_credentials`. Authorization redirects directly to
ChatGPT without any Projects Hub user login. A code alone cannot acquire access:
the token endpoint also requires the provisioned client secret. Public dynamic
registration and personal-account linking are disabled at HTTP boundaries.
Redirects are limited to the official stable ChatGPT redirect and official
callback-ID paths; the issuer is echoed. Refresh-token reuse revokes the grant.
Legacy owner grants remain readable until expiration/revocation; no new personal
grants can be created through the deployed OAuth routes.

OAuth metadata is served as JSON at `/.well-known/oauth-authorization-server`.
The OIDC discovery endpoint returns an explicit JSON 404 because this service
does not issue OIDC ID tokens. It must never fall through to the application's
HTML page. Denied MCP requests log only enumerated RPC method/protocol facts,
request correlation and release identity; no credentials or request payloads.

Legacy user context travels as `actor_id` alongside `project_id` and `request_id`.
The permanent tools infer it from the saved request. It is not a login identity or an authorization
override: the backend stores that context when creating a request, checks the
actor's current project access, and rejects mismatched tool parameters. Reading
returns the saved actor ID/name and project context. `response_actor_id` remains
the service integration. Trusted request creation can set `--context-actor-id`
to an existing project participant without requiring that participant to log in.

## Connect the service once

### Secure MCP Tunnel (existing Mail MCP deployment pattern)

Use the same `tunnel-client` HTTP configuration and private local Authorization
header pattern as `mail-mcp-bridge`. A distinct tunnel identity and systemd unit
route to the existing backend at `http://127.0.0.1:8196/mcp`; tools, Events,
saved context and durable SQLite state remain the same. The tunnel client uses
the existing private runtime API-key file. It does not use personal OAuth.

After deploying the source release, the operator runs its
`scripts/install_mcp_tunnel.py --tunnel-id <id>`. The installer provisions a
dedicated service grant and mode-0600 local bearer/header, validates configuration
with `tunnel-client doctor`, and enables `projects-hub-tunnel.service`.
Credentials stay in `/home/dev/.config/projects-hub-tunnel`, never in ChatGPT,
source or reports. The credential expires after 365 days and is immediately
revocable with `disable-service --client-id ph_service_tunnel_projects_hub`.
Installer retries preserve the same tunnel and credential; differing existing
configuration is refused rather than overwritten.

In **Add custom MCP server**, choose **Tunnel**, enter its `tunnel_id`, and
choose **No authentication**. Tunnel organization/workspace access controls
the host connection; the local private header authenticates the product service.
No Client ID, Client Secret, user invite or personal Projects Hub login is
required in this form. Event subscriptions still need explicit project filters
and the live idle-dot acceptance described below.

### Public OAuth URL

1. The trusted backend operator runs `scripts/run_mcp_test_request.sh
   provision-service --client-id ph_service_chatgpt --credential-file
   <private-new-file>`. It requires the existing platform owner and writes the
   secret to an exclusive mode-0600 file; stdout never contains the secret.
   Retrying never overwrites a secret. The default redirect is ChatGPT's stable
   callback; use `--redirect-uri <official-callback>` if the connection form
   explicitly provides a connection-specific one.
2. In ChatGPT's normal **Add custom MCP server** form enter the endpoint above,
   choose OAuth, and use the **provided/registered client** option. Enter the
   Client ID and Client Secret from the private file once. Use Basic token
   endpoint authentication when the form offers a choice; POST is also accepted.
   Do not choose DCR or No authentication. Endpoints and scope are discovered
   automatically; no Projects Hub invite code, personal login, or same-browser
   session is needed. The previously uploaded desktop-only package is not needed
   for this connection. Rescan tools and events after updating the server.
3. Read the test `project_id` from its Projects Hub URL or ask the backend
   operator for that project's ID. Do not paste secrets into chat.
4. In a dot or Work Cloud chat request: “Subscribe to
   `projects_hub.request.created` for project_id `<id>`. On an event, read
   `get_project_request(project_id, request_id)`; verify its saved project and
   event_id. For mode test, call `record_request_result` with the same project,
   request and event IDs and result `{kind: test_ack, nonce: <saved nonce>,
   text: <ACK containing nonce>}`. Do not send test acknowledgements to Slack.”
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
Disconnect the MCP connection. Its advertised RFC 7009 revocation endpoint
requires the same client authentication and revokes the connection's grant when
the host calls it. For immediate global integration revocation, the trusted
operator runs `scripts/run_mcp_test_request.sh disable-service --client-id
ph_service_chatgpt`. It disables the client and all its grants atomically,
invalidating access/refresh tokens and cancelling future sends. Already sent
events cannot be recalled, but their tools recheck access. Domain requests and
responses are retained. The existing browser grant-revocation routes continue
to serve legacy owner grants only; browser Origin protection is preserved.

## Permanent request contract and legacy migration

`projects_hub.request.created` uses exactly one subscription argument `project_id`.
Payload data requires `project_id`, `request_id`, `mode` (`test` or `live`); unknown
optional fields are compatible. Nonce, body, permissions and destinations stay in
backend. Both modes use the same subscription, SQLite request/outbox and tools.
`get_project_request(project_id, request_id)` returns verified context and state.
`record_request_result(project_id, request_id, event_id, result)` checks correlation,
mode and strict result variants. Test results permit only `test_ack` with nonce and
text. Saved replies have a deterministic stable `response_id`; identical result
retries reuse it and conflicting writes fail atomically.

Legacy event/tools/data/subscriptions stay operational. Subscription identity
includes grant, callback, event name and project filter. Existing identities are
unchanged. New CLI requests default to the permanent event; `--event-name
projects_hub.test_requested` explicitly selects legacy delivery. One request
emits through only its chosen event, even when both subscriptions exist. A new
event requires one owner-approved subscription; never silently stop the old one.
Refresh and secret rotation preserve the permanent subscription ID. No new
credential/grant is minted by this migration.

## Owner-approved discussion binding and destination results

The trusted installed runner accepts `bind-discussion --project-id <id>
--note-id <real-note-id> --client-id ph_service_tunnel_projects_hub --workspace-id
<confirmed-Slack-workspace> --channel-id <channel> --thread-ts <root>`. This is
an explicit owner confirmation, never a public MCP write. Verify the supplied
Slack workspace/thread with the existing connector before provisioning a live
route. Each discussion and Slack thread have a unique binding. Different routes
require `--expected-revision <current>`; versions and owner changes are audited.
`revoke-binding --binding-id <id> --expected-revision <current>` stops new sends
and tools. Existing requests remain tied to their original binding revision.

Use `create --project-id <id> --binding-id <id> --command-id <stable-id>` only after
the specific live route is authorized. It requires a callback-verified permanent
subscription belonging to the bound client. An unbound test grant cannot read or
write discussions, even with a substituted note, binding, project or result kind.

For live, the dot reads the saved discussion and writes `{kind: discussion_reply,
text: <answer>}`. The transaction saves one `project_discussion_entries` reply,
the correlated result and destination states. Backend is `stored`; Mira is
`available` through its existing `project_note_get` reply read. No active Mira
delivery or new Live transport is implied. Saving a reply never creates another
request.created; no answer loop is introduced.

Slack sending stays with the dot's existing authorized Slack connector. First
record `{kind: slack_claim, response_id: <saved>}`. Send to the returned exact
workspace/channel/thread, include `response_id` in the text, then record
`{kind: slack_receipt, response_id, claim_id, status, message_id, error,
workspace_id, channel_id, thread_ts}` using the real receipt. Confirmed state
requires a response message timestamp; destination substitutions and conflicting
receipts fail. Known explicit Slack rejections can mark `error`; unknown errors
mark `unconfirmed`. A repeated outstanding claim is blocked until thread readback
and reconciliation. Restart preserves that state. A confirmed message is never
claimed for another send. Slack failure does not erase the stored backend/Mira
reply; backend failure leaves no partial reply or destination record.

Local tests cover two discussion routes, restart, strict SDK schemas, atomic
failure, version conflicts, revocation and uncertain-send reconciliation. Live
MVP0 PASS still requires actual dot/Slack receipts and two separate owner-approved
discussion/thread routes. P0 alone is not that acceptance.
