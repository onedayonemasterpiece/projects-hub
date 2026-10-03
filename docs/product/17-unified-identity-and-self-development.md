# Unified identity, authorization and owner-only self-development

Status: product requirement / implementation target.

## 1. Problem found during first physical Android acceptance

The current public Projects Hub login is not the long-term identity model.

Today Projects Hub validates a Supabase access token obtained through `custom:yandex`,
maps `(provider, subject)` to an internal actor, and on first external login creates
a fresh personal workspace where that actor becomes workspace `owner`. It also
creates the same default project names for every new external user.

That distinguishes sessions but does not express the real product model: one
product-family owner, app/MCP entitlements, resource scopes, invitations,
workspace memberships, project memberships, and owner-only capabilities.

External identity providers are authentication methods, not the authority for
product identity or authorization.

## 2. Target identity model

Use one first-party product-family identity plane with a stable internal
`user_id` / OIDC `sub`. Every mobile app, web app and MCP resource trusts that
issuer while keeping its own audience and authorization boundary.

The identity plane owns stable user identity, attached authentication methods,
platform roles such as the single `platform_owner`, app/service entitlements,
OAuth clients/audiences/scopes, invite/revoke lifecycle, and device/session
credential revocation.

A resource owns its domain permissions. Projects Hub owns its workspace/project
memberships; other products own their own ACLs.

No product may use a Yandex/Google/Supabase identifier, email address or other
provider-specific identifier as the canonical cross-product user identity.

## 3. Authentication methods are replaceable

The first-party identity is independent of the authentication method. Preferred
target for mobile/web is first-party passkey/WebAuthn through the system browser
or platform credential UI. Owner bootstrap may use a one-time recovery/local
credential to enroll the first passkey. New users join through short-lived
owner-issued invitations and enroll their own credentials.

External IdPs such as Yandex or Google may later be linked as optional convenience
methods to an existing first-party identity. They are never mandatory identity
authorities.

Android authentication must not depend on cookies inside the app WebView. This
also explains the first acceptance symptom: being signed into the Yandex Mail app
or the phone browser does not imply that an embedded WebView owns the same Yandex
cookie jar, so the current flow can still look logged out.

## 4. Authorization model

Authorization is layered:

1. `platform_role` — e.g. the unique `platform_owner`.
2. `resource_grant` — application/MCP resource + scopes/role.
3. Projects Hub workspace membership.
4. Projects Hub explicit project membership.
5. Narrow repository/device/knowledge grants.

Creating an account must not automatically grant all applications, all projects,
or an owner workspace. Access is explicitly granted by invitation, entitlement
or owner action.

The platform owner is explicitly bound to the stable first-party subject. It is
never inferred from email, display name or whichever external IdP authenticated
the session.

Street Story authentication is not migrated in this slice; its current token flow
remains until its functional acceptance is complete.

## 5. Existing identity service: useful foundation, not finished solution

`identity.kenigevents.ru` already runs an OAuth 2.1/OIDC authorization-server
foundation with issuer/JWKS, client registration policy, resource audiences,
scopes, refresh-token rotation and an explicit owner subject/operator model.

However, the current implementation is intentionally a single-owner server. It is
therefore a strong reusable base, not yet the multi-user product-family identity
service required here. The migration should evolve that service rather than add a
third unrelated auth stack.

Direct Projects Hub dependence on a Supabase project and `custom:yandex` should
be removed once the first-party issuer has multi-user authentication and client
support ready.

## 6. Projects Hub memberships

Projects Hub already has actors, workspaces and workspace memberships, but the
current first external login creates a new personal workspace, grants that actor
workspace `owner`, and seeds Projects Hub / Wonderful Lections / KenigEvents.

That behavior is temporary beta bootstrap behavior and must not become the
multi-user authorization contract.

Target behavior:

- account creation alone creates no product/project access;
- an owner/invitation grants one or more resource entitlements;
- Projects Hub maintains explicit `project_membership` rows;
- project roles can be read/write/admin as required by the project;
- repository/device/knowledge capabilities are still narrower than membership;
- list/search/focus/memory operations fail closed when membership is absent.

## 7. Owner-only self-development

Projects Hub should help develop itself.

When the owner discusses Projects Hub and Mira identifies a missing capability,
bug or desired improvement, she may create a typed durable
`development_request` with problem, desired user outcome, relevant
project/repository, acceptance criteria, evidence/context and priority/status.

Conversation alone is not execution authorization. Only the platform owner may:

- create/refine development requests;
- list and triage the backlog;
- group selected requests into a bounded development batch;
- explicitly start a development batch;
- observe implementation/tests/CI/deployment;
- mark work delivered after production/readback evidence.

A started batch uses the existing GitHub + DevCoveer implementation path and the
protected Projects Hub requirements. Android changes should end in a signed
release/update manifest so the installed app can self-update.

Ordinary users have no development fallback. Unsupported requests remain
unsupported; development tools are not present in their Live tool surface and
cannot be reached by guessing API calls.

## 8. Near-term sequence

1. Preserve current Android functional acceptance.
2. Add explicit platform-owner and project-membership concepts to Projects Hub.
3. Add owner-only development-request/backlog capabilities.
4. Evolve the existing `identity.kenigevents.ru` authorization-server foundation
   from its current single-owner model into a multi-user product-family issuer.
5. Register apps/MCP resources with separate audiences/scopes.
6. Add first-party passkey/invite authentication; external IdPs become optional.
7. Migrate Projects Hub away from direct Supabase/Yandex identity.
8. Migrate other products one by one after their product acceptance is stable.

## 9. Acceptance invariants

- One person has one stable first-party subject across apps and MCP.
- App/service/project access is independently grantable and revocable.
- A new user gets no implicit owner/all-project access.
- The platform owner is explicit and uniquely privileged.
- Projects Hub lists only projects granted to the actor.
- Tokens are audience-bound and never reused across resources.
- Ordinary users cannot create or launch development work.
- Owner development execution requires an explicit owner command and durable
  request/batch/result evidence.
- Android self-update remains the normal path after self-development releases.
