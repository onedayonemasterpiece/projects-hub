# MCP Events P0 connection

Implementation follows the [OpenAI MCP Events contract](https://developers.openai.com/plugins/build/mcp-events)
and [OAuth connection requirements](https://developers.openai.com/plugins/build/auth).
P0 acceptance requires an actual idle dot, not a local test or callback 2xx.
MVP0 discussion bindings are intentionally gated on that acceptance.

## Server and scope

Endpoint: `https://projects-hub.kenigevents.ru/mcp`. Python MCP SDK minimum 2.3;
tested version 2.3.0. The SDK owns the
2026-07-28 envelope and transport. Its capabilities model lacks draft Events;
the adapter restores `capabilities.events` after discovery serialization and
registers the three draft handlers on the same authenticated SDK transport.

This connection has only `projects-hub:test`: read saved test requests, save
nonce responses and subscribe to `projects_hub.test_requested`. Per the owner's
updated requirement, the connection represents the service integration, not an
individual user. An administrator provisions one confidential OAuth client with
access to these test operations across all active projects, including new ones.
Event subscriptions still require an explicit `project_id` filter. The service
actor has no browser session, workspace membership, or platform-owner privileges;
responses record its identity instead of impersonating a user. Client enablement,
project status, grant lifetime and resource binding are checked on every tool
call and before delivery. Browser cookies are never machine credentials. Only the
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

User context travels as `actor_id` alongside `project_id` and `request_id` in
each event and both tool calls. It is not a login identity or an authorization
override: the backend stores that context when creating a request, checks the
actor's current project access, and rejects mismatched tool parameters. Reading
returns the saved actor ID/name and project context. `response_actor_id` remains
the service integration. Trusted request creation can set `--context-actor-id`
to an existing project participant without requiring that participant to log in.

## Connect the service once

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
   `projects_hub.test_requested` for project_id `<id>`. On a test event, read
   `get_test_request` with request_id, project_id and actor_id from the event;
   verify the saved actor, project and eventId, then call `record_test_response`
   with the same context, the saved nonce and a response containing it.”
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

No project discussion binding, Slack message, or active Mira delivery is claimed
by P0. Those are the subsequent MVP0 implementation after live P0 PASS.
