# Identity, membership and Android login UX — open product design

Status: **OPEN / requires product design before multi-user beta**  
Tracked in: GitHub issue #38.

This document records a physical Android acceptance finding from 3 October 2026. It is deliberately **not** a finished RBAC/auth design. The current Yandex-only login and workspace-wide membership behavior are implementation facts, not final product decisions.

## What the backend knows today

Public authentication currently uses Supabase Auth with provider `custom:yandex`.

Projects Hub verifies the Supabase access token through `/auth/v1/user` and receives a Supabase user UUID. The durable external identity key is:

```text
provider = supabase:custom:yandex
subject  = <Supabase user UUID>
```

Projects Hub then maps `(provider, subject)` to its own stable `actor_id`.

Email and display name are stored as user-facing metadata. They are **not** the authorization identity and must not be used to infer elevated privileges.

Consequences:

- a repeated login that returns the same verified `(provider, subject)` maps to the same actor;
- a new unseen identity creates a new actor;
- today there is no separate concept of “this is the product owner/operator” at platform level;
- therefore a successful Yandex login by itself does not prove that the logged-in person is the Projects Hub owner.

## Current membership model is insufficient

The current schema has `memberships(actor_id, workspace_id, role)`.

On first external login Projects Hub creates a new personal workspace, gives the actor `owner` in that workspace and creates the default projects.

Project listing checks workspace membership and then returns all projects in the workspace. There is currently no project-specific membership/grant table.

Therefore these questions are unresolved:

1. How does the system identify the platform owner/operator?
2. How does the owner invite another person?
3. How is the invited person matched to an actor after authentication?
4. Can a person belong only to one project, or does workspace membership intentionally grant all projects?
5. Which roles are required at platform, workspace and project scope?
6. How does the same human link Yandex, Google, passkey/email or another IdP without becoming duplicate actors?
7. How are identity links and access grants revoked safely?

## Candidate direction — not yet an accepted final design

Keep authentication identity separate from product authorization.

### Internal identity

Use one stable `actor_id` as the product identity. One actor may have multiple verified `external_identities`.

External providers are login mechanisms, not the product identity itself.

### Platform owner/operator

A platform-level privileged role is probably useful for the product owner, but it must be explicitly bound to a verified actor and must never be inferred from email, name or “first user wins”.

Candidate:

- `platform_owner` / `operator`: system-wide administrative capability;
- `workspace_owner` / possibly `workspace_admin`: membership and workspace configuration;
- ordinary workspace/project roles scoped to collaboration.

Do not build a generic enterprise RBAC system unless concrete beta workflows require it.

### Invitations

Candidate flow:

```text
owner creates invitation
→ invitation names intended workspace/project scope and role
→ recipient authenticates with any supported provider
→ recipient explicitly accepts invitation
→ verified actor_id receives the exact grant
→ readback confirms membership/project access
```

Email may be used to deliver or label an invitation, but authority should attach to the authenticated actor that claims the invite, not to a mutable email string alone.

### Project access

Before multi-user beta, explicitly decide whether workspace membership intentionally means access to every project or project grants are required.

The current implementation behaves as workspace-wide access. That must not be mistaken for an accepted product decision.

## Android login UX finding

The Android app currently hosts Projects Hub in a WebView and leaves ordinary HTTP/HTTPS navigation inside that same WebView. Therefore the Yandex OAuth page also opens inside the app WebView.

Android WebView has its own cookie/state model and does not automatically share the user's default-browser session. This explains the physical acceptance observation: even though the user is already signed into Yandex Mail and the normal browser, the Projects Hub OAuth page can appear logged out.

This UX is not accepted as final.

Android documentation recommends Custom Tabs for third-party sign-in/external web flows because Custom Tabs use the user's browser and share browser state/cookies. Yandex also provides a mobile LoginSDK which can use Yandex accounts already saved in installed Yandex applications.

References:

- https://developer.android.com/develop/ui/views/layout/webapps/overview-of-android-custom-tabs
- https://developer.android.com/develop/ui/views/layout/webapps/in-app-browsing-embedded-web
- https://yandex.ru/dev/id/doc/ru/mobileauthsdk/about
- https://yandex.ru/dev/id/doc/ru/register-auth

## Auth UX options to evaluate

### Option A — provider-neutral browser auth

Use Android Custom Tabs + PKCE for external IdP login and return to Projects Hub via an app/deep-link callback.

Benefits:

- reuses the user's normal browser state;
- works with multiple identity providers;
- keeps third-party pages outside the privileged Projects Hub WebView;
- aligns with provider-neutral internal actor identity.

### Option B — Yandex LoginSDK

If Yandex remains an important first-class provider, use Yandex LoginSDK on Android. It can reuse Yandex accounts saved in installed Yandex apps.

Benefit: best Yandex-native account picker/login UX.

Cost: provider-specific native integration and therefore not sufficient by itself as the general identity architecture.

### Likely shape

Keep Supabase (or another identity broker) only as the common verified identity/session boundary if it remains useful, but remove the assumption that Projects Hub identity equals Yandex. Android should not perform third-party authentication inside the main Projects Hub WebView.

## Required outcomes before multi-user beta

- a returning login maps to the same actor reliably;
- a platform owner/operator identity is explicitly provisioned and cannot self-elect;
- owner can invite/add a user;
- owner can grant only the intended workspace/project access;
- project visibility is explicit and tested;
- one actor can link multiple verified login methods without duplication;
- logout/revocation/removal terminates access consistently across Live, tools, devices and delegated resources;
- Android third-party sign-in does not unexpectedly present a clean WebView session when a reusable browser/Yandex account session exists;
- microphone permission remains restricted to the exact Projects Hub origin regardless of the auth surface.
