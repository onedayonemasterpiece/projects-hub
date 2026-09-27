# Shared resource rollout status

Runtime contract: private `onedayonemasterpiece/ai-resource-control 0.1.5` plus resource-guard-aware `onedayonemasterpiece/live-interaction 0.1.4`. The canonical shared authority has migrations 001–008, six verified Live scopes, bounded retention and Vault-backed wrapped-key delivery. Projects Hub must not create a second quota ledger.

The version above describes the current scaffold boundary, not the desired future Live-agent architecture. Before the first complete Projects Hub Live runtime is implemented, adopt the current released `live-interaction` contract and the canonical `docs/live-agent-architecture.md` from that repository. Prompt layering, progressive tool/capability disclosure, provider session continuity/resumption, visual-context policy, resource-accounting semantics and sanitized audit logging are shared concerns; do not fork them into Projects Hub. Projects Hub owns only its authorized project/context capabilities and UI semantics.

Normal Live uses `AI_RESOURCE_CONTROL_URL` + `AI_RESOURCE_CONTROL_SERVICE_KEY` (or their compatibility aliases). Projects Hub owns exactly one authority-outage fallback alias: `GOOGLE_API_KEY4`. The trusted server boundary reads only `GOOGLE_API_KEY4` as the Projects Hub source alias and maps that value to `AI_RESOURCE_CONTROL_FALLBACK_KEY`; the original alias name and keys 1–3/5–6 are never forwarded to `ai-resource-control`.

The fallback belongs to the shared SDK, not this application. It may activate only when the initial read-only authority capability probe returns `RESOURCE_CONTROL_UNAVAILABLE`, before any mutating acquire. It is forbidden after a successful authority probe, for admission/quota/429/capacity/credential decisions, after a lost mutating acquire response, and after provider ready. Emergency mode uses the same `live-interaction` resource guard, allows one local Projects Hub fallback session per host and expires after two hours.

If `GOOGLE_API_KEY4` is absent during an authority outage, Live fails explicitly; Projects Hub must not borrow another consumer's emergency key. Browser/Android never receive the authority service credential or provider key.

`ProjectScope` is created by an authenticated server handler AFTER checking project access. Its binding is pseudonymous, not a source of authorization. Model/tool arguments cannot choose a repository, authority credential or fallback alias.

This repository is still a product scaffold rather than a completed Android/backend deployment. Real provider/microphone acceptance belongs to the eventual runtime rollout; offline contract tests are not that acceptance.
