# Shared resource rollout status

Controller candidate: `onedayonemasterpiece/ai-resource-control` commit `26a541eea7def758f406b38c518e2377d1f56d36` (follow-up commits may add packaging/readiness tests). Runtime requirements: private controller wheel and a verified resource-guard-aware live-interaction release; do not download an unrelated similarly named PyPI package.

Existing live-interaction `cc38a1173bedc73892b5711572f28cb1f1972db3` lacks the mandatory resource_guard hook. A write adding that hook was blocked and not retried. Therefore this repository's integration is staged and fail-closed, not a successful provider connection. SDK returns RESOURCE_TRANSPORT_INCOMPATIBLE before reservation when run with the old transport.

No production SQL migration, provider numeric Live policy, application deployment or key installation was performed. The six-key/six-scope registry exists in the current shared authority; Flash limits must not substitute for missing Live limits. Finish the canonical controller's docs/rollout.md gates before enabling a microphone session.

ProjectScope is created by an authenticated server handler AFTER checking project access. Its binding is pseudonymous, not a source of authorization. Google/Supabase secrets stay only in the backend environment. This module accepts neither keys nor repository paths from model tool calls.
